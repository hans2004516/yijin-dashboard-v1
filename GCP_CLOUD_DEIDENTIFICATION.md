# GCP 雲端去識別化部署

## 目標

將原本在 Windows 執行的去識別化移入 GCP，但保持目前 Dashboard、私人 API 與
分析輸出契約不變。正式切換前，舊的本機上傳方式仍保留為復原路徑。

```text
人工上傳原始 CSV
        v
私人 GCS source bucket
        v
Cloud Run Job: member-deidentify
        v
GCS raw/member_features.csv（僅去識別化欄位）
        v
Cloud Run Job: member-analysis
        v
Cloud Run dashboard-web + IAP
```

## 重要決策

1. 必須把目前使用中的同一組 HMAC 去識別化密鑰加入 Secret Manager。改用新密鑰
   會讓所有會員匿名代碼改變，造成跨版本會員無法對應。
2. Cloud Run Job 的 secret 必須綁定明確版本，例如 `:1`，不使用 `:latest`。
3. 可識別 CSV 使用獨立 bucket。`member-analysis-sa`、`dashboard-api-sa` 與
   `dashboard-web-sa` 都不授予此 bucket 權限。
4. 程式不自動刪除來源。完成正式驗證後，再依資料治理需求設定來源保留天數。

## 1. 變數與 API

```powershell
$PROJECT_ID = "yijin-member-insights-2026"
$REGION = "asia-east1"
$DATA_BUCKET = "yijin-member-insights-2026-data"
$SOURCE_BUCKET = "yijin-member-insights-2026-sensitive-source"
$DEID_SA_NAME = "member-deidentify-sa"
$DEID_SA = "$DEID_SA_NAME@$PROJECT_ID.iam.gserviceaccount.com"
$DEID_SECRET = "member-deidentification-hmac"
$DEID_SECRET_VERSION = "1"
$DEID_TAG = Get-Date -Format "yyyyMMdd-HHmmss"
$DEID_IMAGE = "$REGION-docker.pkg.dev/$PROJECT_ID/yijin-containers/member-deidentify:$DEID_TAG"

& $GCLOUD services enable `
  "run.googleapis.com" `
  "cloudbuild.googleapis.com" `
  "artifactregistry.googleapis.com" `
  "secretmanager.googleapis.com" `
  "workflows.googleapis.com" `
  "workflowexecutions.googleapis.com" `
  --project="$PROJECT_ID"
```

## 2. 建立可識別來源專用 bucket

先確認名稱尚未存在，再建立單一區域、統一 bucket 層級權限且禁止公開存取的
bucket：

```powershell
& $GCLOUD storage buckets describe "gs://$SOURCE_BUCKET" --project="$PROJECT_ID"

& $GCLOUD storage buckets create "gs://$SOURCE_BUCKET" `
  --project="$PROJECT_ID" `
  --location="$REGION" `
  --uniform-bucket-level-access

& $GCLOUD storage buckets update "gs://$SOURCE_BUCKET" `
  --project="$PROJECT_ID" `
  --public-access-prevention="enforced"
```

若 describe 顯示 bucket 已存在，必須確認它確實屬於本專案，不能直接沿用他人
bucket。

## 3. 建立專用服務帳號與固定 HMAC secret

```powershell
& $GCLOUD iam service-accounts create "$DEID_SA_NAME" `
  --project="$PROJECT_ID" `
  --display-name="Yijin Member Deidentification"

& $GCLOUD secrets create "$DEID_SECRET" `
  --project="$PROJECT_ID" `
  --replication-policy="automatic"
```

接著把「目前本機實際使用的同一組密鑰」安全加入 secret version。不要在指令列
直接輸入明文，也不要建立新的密鑰取代舊密鑰。PowerShell 的管線可能附加換行，
雲端程式會只移除結尾換行以維持與本機輸入一致。

```powershell
$DEID_SECRET_SECURE = Read-Host "輸入既有去識別化密鑰（畫面不顯示）" -AsSecureString
$DEID_SECRET_POINTER = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($DEID_SECRET_SECURE)
try {
  [Runtime.InteropServices.Marshal]::PtrToStringBSTR($DEID_SECRET_POINTER) |
    & $GCLOUD secrets versions add "$DEID_SECRET" `
      --project="$PROJECT_ID" `
      --data-file=-
  if ($LASTEXITCODE -ne 0) { throw "Secret version 建立失敗" }
}
finally {
  [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($DEID_SECRET_POINTER)
  $DEID_SECRET_SECURE = $null
}
```

只對 Job 身分授權讀取這個 secret：

```powershell
& $GCLOUD secrets add-iam-policy-binding "$DEID_SECRET" `
  --project="$PROJECT_ID" `
  --member="serviceAccount:$DEID_SA" `
  --role="roles/secretmanager.secretAccessor"
```

## 4. 最小 GCS 權限

來源 bucket 只允許讀取固定物件：

```powershell
& $GCLOUD storage buckets add-iam-policy-binding "gs://$SOURCE_BUCKET" `
  --project="$PROJECT_ID" `
  --member="serviceAccount:$DEID_SA" `
  --role="roles/storage.objectViewer" `
  --condition="expression=resource.name == 'projects/_/buckets/$SOURCE_BUCKET/objects/incoming/member_features.csv',title=read-member-source,description=Read only the member source CSV"
```

分析 bucket 只允許管理安全 CSV 與去識別化稽核報告：

```powershell
& $GCLOUD storage buckets add-iam-policy-binding "gs://$DATA_BUCKET" `
  --project="$PROJECT_ID" `
  --member="serviceAccount:$DEID_SA" `
  --role="roles/storage.objectUser" `
  --condition="expression=resource.name == 'projects/_/buckets/$DATA_BUCKET/objects/raw/member_features.csv' || resource.name.startsWith('projects/_/buckets/$DATA_BUCKET/objects/audit/deidentification/'),title=write-deidentified-member-data,description=Manage only deidentified CSV and audit reports"
```

## 5. 建置與部署 Job

映像的 build context 是專案根目錄，讓雲端 Job 與既有本機工具共用同一份轉換
邏輯：

```powershell
& $GCLOUD builds submit "." `
  --project="$PROJECT_ID" `
  --config=".\gcp\member-deidentify\cloudbuild.yaml" `
  --substitutions="_IMAGE=$DEID_IMAGE"

& $GCLOUD run jobs deploy "member-deidentify" `
  --project="$PROJECT_ID" `
  --region="$REGION" `
  --image="$DEID_IMAGE" `
  --service-account="$DEID_SA" `
  --set-env-vars="SOURCE_BUCKET=$SOURCE_BUCKET,SOURCE_OBJECT=incoming/member_features.csv,DESTINATION_BUCKET=$DATA_BUCKET,DESTINATION_OBJECT=raw/member_features.csv,REPORT_PREFIX=audit/deidentification,DEIDENTIFICATION_KEY_VERSION=$DEID_SECRET_VERSION" `
  --set-secrets="YIJIN_DEIDENTIFICATION_SECRET=$DEID_SECRET`:$DEID_SECRET_VERSION" `
  --memory="1Gi" `
  --tasks="1" `
  --max-retries="0" `
  --task-timeout="30m"
```

## 6. 第一次安全驗證

先把一份已確認格式的原始 CSV 上傳到來源 bucket，再只執行去識別化 Job：

```powershell
& $GCLOUD storage cp "<原始 member_features.csv 完整路徑>" `
  "gs://$SOURCE_BUCKET/incoming/member_features.csv" `
  --project="$PROJECT_ID"

& $GCLOUD run jobs execute "member-deidentify" `
  --project="$PROJECT_ID" `
  --region="$REGION" `
  --wait
```

成功後驗證結構化紀錄、輸出欄位及稽核報告，再執行既有 `member-analysis`。正式
切換前不要改 Scheduler。

## 7. 串成單一雲端流程

`gcp/member-pipeline/workflow.yaml` 會先等待去識別化成功，再執行分析。去識別化
失敗時分析不會啟動。驗證 Job 後才部署 Workflow，並把既有 Scheduler 的目標從
直接執行 `member-analysis` 改為建立 `member-pipeline` execution。

這個切換需另外建立 `member-pipeline-sa`，只授予兩個 Job 的
`roles/run.invoker`，並對既有 `member-analysis-scheduler-sa` 授予
`roles/workflows.invoker`。切換與回復指令應在第一次 Job 驗證完成後再執行。
