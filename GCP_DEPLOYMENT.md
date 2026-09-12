# 億進寢具會員行為洞察中心：GCP MVP 部署指南

本指南只提供操作步驟，不會自動建立任何 GCP 資源。先使用
`gcp/member-analysis/testdata/member_transactions.csv` 這份合成資料驗證部署。
本專案後續選定的來源為上一層 `貼標籤/member_features.csv`；它含原始會員編號，
只能在本機受控環境去識別化並通過人工核准後，才可上傳安全輸出。原始檔不得上傳。

最終資料流：

~~~text
私人 GCS：raw/member_transactions.csv（合成測試）
       或 raw/member_features.csv（去識別化會員特徵）
        ↓
Cloud Run Job：member-analysis
        ↓
私人 GCS：processed/member_features.json
        ↓
Cloud Run Service：dashboard-api
        ↓
GET /v1/dashboard
        ↓
Next.js 儀表板
~~~

官方參考：

- Cloud Run Job 執行：https://cloud.google.com/run/docs/execute/jobs
- Cloud Run Service 部署：https://cloud.google.com/run/docs/deploying
- Cloud Run 服務身分：https://cloud.google.com/run/docs/configuring/services/service-identity
- GCS bucket 建立：https://cloud.google.com/storage/docs/creating-buckets
- GCS Uniform bucket-level access：https://cloud.google.com/storage/docs/using-uniform-bucket-level-access
- GCS IAM Conditions：https://cloud.google.com/storage/docs/access-control/iam

## 1. 必要服務與本機工具

GCP 端需要：

- Cloud Storage
- Cloud Run
- Cloud Build
- Artifact Registry
- IAM
- Cloud Logging
- Secret Manager

本機需要 Google Cloud CLI、可用的計費 GCP project，以及具備建立上述資源的部署者帳號。
容器執行時使用使用者管理的服務帳戶，不使用 JSON 金鑰。

## 2. 設定 placeholder 變數

以下命令以 PowerShell 為例。請先填入所有角括號值；不要直接使用範例字串。
bucket 名稱必須全球唯一。

~~~powershell
$GcpProjectId = "<GCP_PROJECT_ID>"
$GcpRegion = "<GCP_REGION，例如 asia-east1>"
$GcsBucket = "<全球唯一的私人 GCS_BUCKET_NAME>"
$ArtifactRepository = "<ARTIFACT_REPOSITORY，例如 yijin-dashboard>"
$ImageTag = "<IMAGE_TAG，例如 mvp-001>"
$FrontendOrigin = "<前端 Origin，例如 https://dashboard.example.com>"
$RawObject = "raw/member_transactions.csv"
$HistoryRetentionDays = "90"
$HistoryMinCount = "3"
$HistoryListRoleId = "yijinHistoryObjectLister"

$AnalysisServiceAccountId = "member-analysis-sa"
$ApiServiceAccountId = "dashboard-api-sa"
$SchedulerServiceAccountId = "member-analysis-scheduler-sa"
$AnalysisServiceAccount = "$AnalysisServiceAccountId@$GcpProjectId.iam.gserviceaccount.com"
$ApiServiceAccount = "$ApiServiceAccountId@$GcpProjectId.iam.gserviceaccount.com"
$SchedulerServiceAccount = "$SchedulerServiceAccountId@$GcpProjectId.iam.gserviceaccount.com"

gcloud config set project $GcpProjectId
gcloud config set run/region $GcpRegion
~~~

## 3. 啟用 API 與建立 Artifact Registry

~~~powershell
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com storage.googleapis.com iam.googleapis.com logging.googleapis.com secretmanager.googleapis.com cloudscheduler.googleapis.com

gcloud artifacts repositories create $ArtifactRepository --repository-format=docker --location=$GcpRegion --description="Yijin dashboard MVP images"
~~~

如果 repository 已存在，第二個命令會回報衝突，可改用 describe 確認，不要重複建立。

## 4. 建立私人 GCS bucket

~~~powershell
gcloud storage buckets create "gs://$GcsBucket" --project=$GcpProjectId --location=$GcpRegion --uniform-bucket-level-access
gcloud storage buckets describe "gs://$GcsBucket"
~~~

請確認：

- uniform_bucket_level_access 為 enabled。
- IAM policy 沒有 allUsers 或 allAuthenticatedUsers。
- 不執行任何 make-public、allUsers binding 或公開物件 ACL 命令。

第一版歷史檔由 Job 寫入 processed/history/；不需要額外開啟 BigQuery、排程或物件公開存取。

## 5. 建立服務帳戶與最小 GCS 權限

建立兩個互不共用的服務帳戶：

~~~powershell
gcloud iam service-accounts create $AnalysisServiceAccountId --display-name="Yijin member analysis job"
gcloud iam service-accounts create $ApiServiceAccountId --display-name="Yijin dashboard API"
~~~

先建立條件式資源名稱：

~~~powershell
$ObjectRoot = "projects/_/buckets/$GcsBucket/objects"
$RawReadCondition = "expression=resource.name == '$ObjectRoot/$RawObject',title=read-analysis-source,description=Read only the selected analysis source object"
$ProcessedWriteCondition = "expression=resource.name.startsWith('$ObjectRoot/processed/'),title=write-processed-member-features,description=Manage only processed analysis objects"
$LatestReadCondition = "expression=resource.name == '$ObjectRoot/processed/member_features.json',title=read-latest-member-features,description=Read only the dashboard latest object"
~~~

分析 Job 只讀 raw 來源，並只管理 processed 前綴：

~~~powershell
gcloud storage buckets add-iam-policy-binding "gs://$GcsBucket" --member="serviceAccount:$AnalysisServiceAccount" --role="roles/storage.objectViewer" --condition=$RawReadCondition
gcloud storage buckets add-iam-policy-binding "gs://$GcsBucket" --member="serviceAccount:$AnalysisServiceAccount" --role="roles/storage.objectUser" --condition=$ProcessedWriteCondition
~~~

歷史版本整理需要列出物件名稱。建立只含 `storage.objects.list` 的 custom role，
並授予分析 Job：

~~~powershell
gcloud iam roles create $HistoryListRoleId --project=$GcpProjectId --title="Yijin history object lister" --description="List history metadata for retention cleanup" --permissions="storage.objects.list" --stage="GA"
gcloud storage buckets add-iam-policy-binding "gs://$GcsBucket" --member="serviceAccount:$AnalysisServiceAccount" --role="projects/$GcpProjectId/roles/$HistoryListRoleId"
~~~

若 custom role 已存在，第一個命令會回報衝突；改用
`gcloud iam roles describe $HistoryListRoleId --project=$GcpProjectId` 確認即可。

Dashboard API 只讀 latest：

~~~powershell
gcloud storage buckets add-iam-policy-binding "gs://$GcsBucket" --member="serviceAccount:$ApiServiceAccount" --role="roles/storage.objectViewer" --condition=$LatestReadCondition
~~~

注意：`storage.objects.list` 是 bucket 層級權限，無法用 `resource.name` 前綴縮小。
此 custom role 只能列出物件名稱與中繼資料，不能讀取內容；Job 程式也只要求
`processed/history/member_features_` 前綴。Dashboard API 仍只對固定 latest 物件
執行 get，不需要 list 權限。

不要執行 gcloud iam service-accounts keys create，也不要下載或提交服務帳戶金鑰。

## 6. 上傳去識別化測試 CSV

從 yijin-dashboard 專案根目錄執行：

~~~powershell
$RawObject = "raw/member_transactions.csv"
gcloud storage cp .\gcp\member-analysis\testdata\member_transactions.csv "gs://$GcsBucket/raw/member_transactions.csv"
~~~

上傳前再次確認檔案中的 member_id 全部以 TEST-M- 開頭。正式資料上線不屬於本 MVP。

### 正式資料上線前的本機去識別化

本節保留既有本機流程。若改用雲端去識別化，請依
`GCP_CLOUD_DEIDENTIFICATION.md` 建立隔離來源 bucket 與 `member-deidentify` Job；
不可把含真實會員編號的檔案直接上傳至下列 `raw/` 路徑。

真實來源檔不得直接上傳。目前選定來源為上一層 `貼標籤\member_features.csv`。
先使用 `tools/deidentify-member-csv/deidentify.py` 在本機轉檔；工具不連線 GCP，
會自動辨識會員特徵模式並只輸出 Dashboard v1 允許欄位：

~~~powershell
$InputCsv = (Resolve-Path "..\貼標籤\member_features.csv").Path
$OutputDirectory = "<本機受控輸出資料夾>"
$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$SafeCsv = Join-Path $OutputDirectory "member_features.deidentified.$Timestamp.csv"
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null

& ".\.venv-gcp\Scripts\python.exe" ".\tools\deidentify-member-csv\deidentify.py" `
  --input "$InputCsv" `
  --output "$SafeCsv"
~~~

同一顧客專案固定使用自己的去識別化密鑰；不同顧客不得共用，且不得使用
Dashboard API Key 代替。完成後人工確認：

- `member_id` 全部以 `ANON-M-` 開頭。
- 稽核報告的 `input_mode` 是 `member-features`。
- 沒有姓名、電話、Email、地址、生日或身分證欄位。
- 稽核報告的輸出筆數為去重後會員數，`duplicate_member_rows` 符合預期。
- 原始會員編號沒有出現在安全輸出或稽核報告。

目前已檢查版本有 172,594 列及 172,594 個大小寫有別的唯一會員編號。另有 12 組
編號只差大小寫但特徵不同，必須視為不同會員，不可合併；
若來源檔之後更新，應以新一輪資料負責人核對結果為準，不可硬套這組數字。

只有在資料負責人完成核准後，才設定來源物件並上傳安全輸出：

~~~powershell
$RawObject = "raw/member_features.csv"
gcloud storage cp "$SafeCsv" "gs://$GcsBucket/$RawObject"
~~~

不要將原始檔、密鑰或轉檔結果提交 Git。若先前 IAM 只允許讀取
`raw/member_transactions.csv`，切換來源前必須由管理員將 Job 的條件式讀取權限
改成 `raw/member_features.csv`；不要同時放寬為整個 bucket。

## 7. 建置與部署 Cloud Run Job

設定映像路徑：

~~~powershell
$AnalysisImage = "$GcpRegion-docker.pkg.dev/$GcpProjectId/$ArtifactRepository/member-analysis:$ImageTag"
~~~

建置：

~~~powershell
gcloud builds submit .\gcp\member-analysis --tag=$AnalysisImage
~~~

部署：

~~~powershell
gcloud run jobs deploy member-analysis --image=$AnalysisImage --region=$GcpRegion --service-account=$AnalysisServiceAccount --set-env-vars="GCS_BUCKET=$GcsBucket,GCS_SOURCE_OBJECT=$RawObject,HISTORY_RETENTION_DAYS=$HistoryRetentionDays,HISTORY_MIN_COUNT=$HistoryMinCount" --memory=1Gi --tasks=1 --max-retries=1 --task-timeout=30m
~~~

Job 不設定 LOCAL_STORAGE_ROOT。Cloud Run 會透過服務帳戶的 Application Default
Credentials 存取 GCS。完整會員特徵約 17 萬筆，建議 Job 至少配置 1 GiB 記憶體；
實際上線後再依 Cloud Monitoring 峰值調整。

## 8. 手動執行分析與查看紀錄

執行並等待完成：

~~~powershell
gcloud run jobs execute member-analysis --region=$GcpRegion --wait
~~~

查看 execution：

~~~powershell
gcloud run jobs executions list --job=member-analysis --region=$GcpRegion
~~~

查看結構化紀錄：

~~~powershell
gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="member-analysis"' --project=$GcpProjectId --limit=50 --order=desc
~~~

來源有變更時，成功紀錄應包含 `history_cleanup_succeeded` 與
`analysis_succeeded`，其中可確認保留天數、最低份數、刪除份數、member_count、
generated_at、latest_object 與 history_object。來源內容未變更時則應包含
`analysis_skipped`、`reason: source_unchanged`，且不會建立歷史版本或覆寫 latest。
失敗時容器以非零狀態結束，既有 latest 不會被不完整內容覆蓋。

首次成功後可在不更動 raw CSV 的情況下再次執行 Job，確認第二次出現
`analysis_skipped`，並比較 `processed/history/` 的物件數量沒有增加。來源內容以
SHA-256 判斷，所以相同內容即使重新上傳為新的 GCS generation 也會跳過。

確認輸出存在：

~~~powershell
gcloud storage ls "gs://$GcsBucket/processed/"
gcloud storage ls "gs://$GcsBucket/processed/history/"
~~~

不要把 GCS 物件設為公開，也不要下載正式會員資料到未受控裝置。

歷史整理規則不是 GCS Lifecycle：Lifecycle 能按年齡刪除，但不能保證不同檔名的
最新三份永遠保留。本 Job 會在每次成功執行後保護最新三份，再刪除其餘已超過
90 天的標準歷史檔。因此檔案是在下一次成功執行時整理，若 Job 一段時間未執行，
舊檔可能暫時保留超過 90 天，但不會因此少於三份。bucket 的 soft delete 若維持
啟用，誤刪版本仍可在其保留期間內復原。

## 9. 建置與部署 Dashboard API

設定映像：

~~~powershell
$ApiImage = "$GcpRegion-docker.pkg.dev/$GcpProjectId/$ArtifactRepository/dashboard-api:$ImageTag"
~~~

建置：

~~~powershell
gcloud builds submit .\gcp\dashboard-api --tag=$ApiImage
~~~

建立一組不提交到 Git 的隨機 API Key，並存入 Secret Manager。以下 PowerShell
只會將金鑰短暫寫入系統暫存檔；請勿顯示在共享畫面或貼入聊天紀錄：

~~~powershell
$DashboardSecret = "dashboard-api-key"
$SecretBytes = New-Object byte[] 32
$RandomGenerator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$RandomGenerator.GetBytes($SecretBytes)
$RandomGenerator.Dispose()
$DashboardApiKey = [Convert]::ToBase64String($SecretBytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
$SecretTempFile = Join-Path $env:TEMP "yijin-dashboard-api-key.txt"
[System.IO.File]::WriteAllText($SecretTempFile, $DashboardApiKey, (New-Object System.Text.UTF8Encoding($false)))

gcloud secrets create $DashboardSecret --replication-policy=automatic
gcloud secrets versions add $DashboardSecret --data-file=$SecretTempFile
Remove-Item -LiteralPath $SecretTempFile
~~~

只在這一個 secret 上授予 Dashboard API 服務帳戶讀取權：

~~~powershell
gcloud secrets add-iam-policy-binding $DashboardSecret --member="serviceAccount:$ApiServiceAccount" --role="roles/secretmanager.secretAccessor"
~~~

部署 API，並將 Secret Manager 的明確版本注入 `DASHBOARD_API_KEY`：

~~~powershell
gcloud run deploy dashboard-api --image=$ApiImage --region=$GcpRegion --service-account=$ApiServiceAccount --set-env-vars="GCS_BUCKET=$GcsBucket,ALLOWED_ORIGINS=$FrontendOrigin" --update-secrets="DASHBOARD_API_KEY=$DashboardSecret:1" --memory=1Gi --allow-unauthenticated
~~~

Cloud Run 此處保留 `allow-unauthenticated`，讓瀏覽器能送出請求；應用程式會在讀取
GCS 前驗證 Bearer API Key。沒有正確金鑰時只會收到 HTTP 401，不會取得會員資料。
這是共享金鑰驗證，不是個人帳號登入；無法辨識是哪一位使用者操作。正式顧客資料
上線前仍須由資料負責人確認共享金鑰、CSV 下載與稽核方式符合內部規範。
API 會在記憶體中快取完整會員特徵；使用約 17 萬筆正式候選資料時，建議先配置
至少 1 GiB，並以實測記憶體峰值與併發量決定是否提高。

取得網址：

~~~powershell
$DashboardApiUrl = gcloud run services describe dashboard-api --region=$GcpRegion --format="value(status.url)"
$DashboardApiUrl
~~~

API 路徑是：

~~~text
<CLOUD_RUN_SERVICE_URL>/v1/dashboard
~~~

ALLOWED_ORIGINS 必須是 origin，不含路徑，例如 https://dashboard.example.com。
如果有多個 origin，可在 Cloud Run 環境變數中用逗號分隔；正式環境不要設為星號。

測試 API 時，將 Secret Manager 中的值讀入目前 PowerShell 變數，不要輸出：

~~~powershell
$DashboardApiKey = gcloud secrets versions access 1 --secret=$DashboardSecret
$DashboardHeaders = @{ Authorization = "Bearer $DashboardApiKey" }
Invoke-RestMethod "$DashboardApiUrl/v1/dashboard" -Headers $DashboardHeaders
~~~

更換金鑰時新增 secret version、將 Cloud Run 更新到新版本，再停用舊版本。不要直接
修改或重用已外洩的版本。

## 10. 設定 Next.js 前端 API 網址

在本機建立不提交 Git 的 .env.local：

~~~dotenv
NEXT_PUBLIC_DASHBOARD_API_URL=<CLOUD_RUN_SERVICE_URL>
~~~

只填 Cloud Run Service 根網址，前端會自動加上 /v1/dashboard。若暫時填了完整
/v1/dashboard，前端也會避免重複附加。

NEXT_PUBLIC 變數會進入瀏覽器 bundle，只能放公開服務網址。不得放 GCS bucket、
服務帳戶、API key、access token 或任何秘密。

正式 GCP API 模式會顯示 API Key 輸入畫面。金鑰只保留在目前分頁的
`sessionStorage`，以 Authorization header 傳送；關閉分頁後需重新輸入。

未設定 NEXT_PUBLIC_DASHBOARD_API_URL 時，儀表板繼續呼叫本機 /api/dashboard。

部署前端平台時，應在平台的公開環境變數設定同一個服務根網址並重新 build。

## 11. 完整測試流程

### A. 本機、完全不連 GCP

1. 建立暫存目錄並複製合成 CSV。
2. 設定 GCS_BUCKET=local-test-bucket 與 LOCAL_STORAGE_ROOT；會員特徵模式另設
   GCS_SOURCE_OBJECT=raw/member_features.csv。
3. 執行 gcp/member-analysis/main.py。
4. 檢查 processed/member_features.json 及 processed/history/。
5. 安裝 gcp/dashboard-api/requirements.txt。
6. 設定本機 `DASHBOARD_API_KEY` 後啟動 gcp/dashboard-api/app.py。
7. 呼叫未篩選、五種篩選、分頁、CSV 下載、錯誤參數及資料不存在案例。
8. 在專案根目錄執行 npm run lint、npm run typecheck、npm run build。
9. 不設定 NEXT_PUBLIC_DASHBOARD_API_URL 啟動前端，確認本機模擬模式。
10. 將它設為 http://localhost:8080，重新啟動前端，確認 GCP API 模式與資料更新時間。

專案已附標準函式庫 unittest：

~~~powershell
python -m unittest discover -s .\gcp\tests -v
~~~

### B. GCP 去識別化測試部署

1. 確認 bucket 私人且只有 TEST-M- 資料。
2. 執行 member-analysis Job，等待成功。
3. 檢查 latest 與一份帶時間的 history。
4. 先讀入金鑰，再呼叫：

~~~powershell
$DashboardApiKey = gcloud secrets versions access 1 --secret=$DashboardSecret
$DashboardHeaders = @{ Authorization = "Bearer $DashboardApiKey" }
Invoke-RestMethod "$DashboardApiUrl/v1/dashboard" -Headers $DashboardHeaders
Invoke-RestMethod "$DashboardApiUrl/v1/dashboard?category=枕頭&loyalty=loyal&page=1&pageSize=12" -Headers $DashboardHeaders
Invoke-WebRequest "$DashboardApiUrl/v1/dashboard?download=csv" -Headers $DashboardHeaders -OutFile .\yijin-members-filtered.csv
~~~

5. 確認 CSV 開頭為 UTF-8 BOM，繁體中文沒有亂碼。
6. 設定前端服務根網址並 build，逐一測試篩選、搜尋、分頁、重新整理與下載。
7. 以未允許的 Origin 呼叫，確認沒有 Access-Control-Allow-Origin。
8. 不帶 Authorization header 與使用錯誤金鑰，確認都回傳 HTTP 401。

## 12. 常見錯誤與排查

### Job 顯示找不到 raw 來源物件

- 確認物件路徑大小寫完全一致。
- 確認 GCS_BUCKET 沒有 gs:// 前綴。
- 確認 GCS_SOURCE_OBJECT 與實際上傳路徑一致；合成測試使用
  `raw/member_transactions.csv`，去識別化會員特徵使用 `raw/member_features.csv`。

### Job 回傳 403

- 確認 Cloud Run Job 使用 member-analysis-sa。
- 檢查 raw exact-object 條件是否指向目前的 GCS_SOURCE_OBJECT，以及 processed
  prefix 的條件式 IAM binding。
- 檢查 bucket 已啟用 uniform bucket-level access。

### API 回傳 503 ANALYSIS_DATA_UNAVAILABLE

- 先確認 Job 成功。
- 確認 processed/member_features.json 存在且為完整 JSON。
- 確認 Dashboard API 使用 dashboard-api-sa。
- 檢查 latest exact-object viewer binding。

### API 回傳 400 INVALID_PARAMETER

- loyalty 只能是 all、loyal、non-loyal。
- activeYears、page、pageSize 必須是正整數；activeYears 也可用 all。
- frequency 必須使用 API 回傳的 frequency_bands。
- download 只支援 csv。

### API 回傳 401 UNAUTHORIZED

- 確認使用 `Authorization: Bearer <API_KEY>`，不要把金鑰放在網址參數。
- 確認使用中的金鑰對應 Cloud Run 綁定的 Secret Manager 版本。
- 若金鑰可能外洩，立即建立新版本、更新 Cloud Run，再停用舊版本。

### API 回傳 503 AUTH_NOT_CONFIGURED

- Cloud Run 尚未把 `DASHBOARD_API_KEY` 綁定到 Secret Manager。
- 確認 dashboard-api-sa 對該單一 secret 有 `roles/secretmanager.secretAccessor`。
- 確認 Cloud Run revision 使用正確且已啟用的 secret version。

### 瀏覽器顯示 CORS 錯誤

- ALLOWED_ORIGINS 必須與瀏覽器 Origin 完全一致，包含 scheme 與 port。
- 不要在 origin 後加斜線或 /v1/dashboard。
- 更新 Cloud Run 環境變數後要等待新 revision 就緒。

### 前端呼叫到錯誤路徑

- NEXT_PUBLIC_DASHBOARD_API_URL 應填 Cloud Run 根網址。
- 變更 NEXT_PUBLIC 變數後必須重新 build。
- 未設定時應看到本機模擬模式並呼叫 /api/dashboard。

### CSV 亂碼

- 輸入 CSV 必須為 UTF-8 或 UTF-8 BOM。
- API 下載檔已加 UTF-8 BOM；若中間代理重新編碼，需停用該轉換。

### API 看不到剛完成的分析

- 確認 latest 的 GCS generation 或 updated 已改變。
- API 每次請求先檢查物件 metadata；版本改變才重新下載。
- 檢查呼叫的 Cloud Run region、project 與 service URL 是否正確。

## 13. 設定每日 Cloud Scheduler

建立專用排程服務帳戶，並只對 `member-analysis` Job 授予 invoker：

~~~powershell
gcloud iam service-accounts create $SchedulerServiceAccountId --project=$GcpProjectId --display-name="Yijin Member Analysis Scheduler" --description="Cloud Scheduler identity used only to start member-analysis"

gcloud run jobs add-iam-policy-binding member-analysis --region=$GcpRegion --project=$GcpProjectId --member="serviceAccount:$SchedulerServiceAccount" --role="roles/run.invoker"
~~~

每天台北時間凌晨 3:00 透過 Cloud Run Jobs v2 endpoint 檢查來源：

~~~powershell
$SchedulerJobName = "member-analysis-daily"
$JobRunUri = "https://run.googleapis.com/v2/projects/$GcpProjectId/locations/$GcpRegion/jobs/member-analysis:run"

gcloud scheduler jobs create http $SchedulerJobName --location=$GcpRegion --project=$GcpProjectId --schedule="0 3 * * *" --time-zone="Asia/Taipei" --description="Daily source check for the Yijin member dashboard" --uri=$JobRunUri --http-method=POST --headers="Content-Type=application/json" --message-body="{}" --oauth-service-account-email=$SchedulerServiceAccount --oauth-token-scope="https://www.googleapis.com/auth/cloud-platform" --attempt-deadline="60s" --max-retry-attempts=0
~~~

目標是 Google Run API，因此使用 OAuth，而不是 Cloud Run service URL 的 OIDC。
不設定 Scheduler 自動重試，避免不確定回應造成重複 execution。Job 本身會以來源
generation 與 SHA-256 判斷內容；來源未變時記錄 `analysis_skipped`，不新增歷史版本。

手動驗證一次：

~~~powershell
gcloud scheduler jobs run $SchedulerJobName --location=$GcpRegion --project=$GcpProjectId
gcloud scheduler jobs describe $SchedulerJobName --location=$GcpRegion --project=$GcpProjectId --format="yaml(state,lastAttemptTime,status,scheduleTime)"
gcloud run jobs executions list --job=member-analysis --region=$GcpRegion --project=$GcpProjectId --limit=3
~~~

排程應為 `ENABLED`、status 沒有錯誤；由
`member-analysis-scheduler-sa` 啟動的 execution 應完成 `1/1`。若手動執行
`scheduler jobs run` 多次，會產生多個 execution，但來源未變時不會產生重複版本。

## 14. 日常資料一鍵更新

完成上述環境建置後，日常資料負責人不必重貼 GCP 指令。請雙擊專案根目錄的
`更新會員儀表板資料.cmd`，或將新的原始 `member_features.csv` 拖曳到該檔案。

一鍵工具會：

1. 呼叫既有本機去識別化工具，並產生獨立稽核報告。
2. 驗證輸入模式、欄位白名單、會員筆數及輸出 SHA-256。
3. 在人工確認後，只上傳去識別化 CSV 到 `raw/member_features.csv`。
4. 以上傳前讀到的 GCS generation 作為前置條件，避免覆蓋同時發生的更新。
5. 執行 `member-analysis` 並等待成功完成。

本機輸出預設保留在 `D:\Yijin-Secure\deidentified-output`，不可提交 Git。工具不讀取
Dashboard API Key，也不建立或保存 Google 服務帳戶金鑰。詳細操作與測試方式見
`tools/update-member-data/README.md`。
