# 前端遷移至 Cloud Run 與 IAP

## 目標架構

```text
使用者 Google 登入
        |
        v
Cloud Run dashboard-web（IAP）
        |
        | Cloud Run 服務帳號 + ID token
        v
Cloud Run dashboard-api-internal（IAM 私人服務）
        |
        v
GCS processed/member_features.json
```

- 瀏覽器不再保存或傳送共享 Dashboard API Key。
- `dashboard-web` 只可呼叫指定的私人 API。
- `dashboard-api-internal` 不接受匿名流量，並繼續使用既有
  `dashboard-api-sa` 讀取 GCS。
- 舊 Sites 與舊 `dashboard-api` 在新入口驗證完成前保留，避免中斷服務。

## 0. PowerShell 變數

```powershell
$GCLOUD = "C:\Users\hans2\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
$PROJECT_ID = "yijin-member-insights-2026"
$PROJECT_NUMBER = "556814349452"
$REGION = "asia-east1"
$REPOSITORY = "yijin-containers"
$BUCKET = "yijin-member-insights-2026-data"
$WEB_SERVICE = "dashboard-web"
$INTERNAL_API_SERVICE = "dashboard-api-internal"
$WEB_SA_NAME = "dashboard-web-sa"
$WEB_SA = "$WEB_SA_NAME@$PROJECT_ID.iam.gserviceaccount.com"
$TAG = Get-Date -Format "yyyyMMdd-HHmmss"
$WEB_IMAGE = "$REGION-docker.pkg.dev/$PROJECT_ID/$REPOSITORY/dashboard-web:$TAG"
$API_IMAGE = "$REGION-docker.pkg.dev/$PROJECT_ID/$REPOSITORY/dashboard-api:iam-$TAG"
```

## 1. 建立前端專用身分

```powershell
& $GCLOUD iam service-accounts create $WEB_SA_NAME `
  --project=$PROJECT_ID `
  --display-name="Yijin Dashboard Web"
```

若顯示服務帳戶已存在，可直接進入下一步。

## 2. 建置支援 IAM 的 API 映像

```powershell
& $GCLOUD builds submit ".\gcp\dashboard-api" `
  --project=$PROJECT_ID `
  --tag=$API_IMAGE
```

## 3. 建立新的私人 API

```powershell
& $GCLOUD run deploy $INTERNAL_API_SERVICE `
  --project=$PROJECT_ID `
  --region=$REGION `
  --image=$API_IMAGE `
  --service-account="dashboard-api-sa@$PROJECT_ID.iam.gserviceaccount.com" `
  --set-env-vars="GCS_BUCKET=$BUCKET,DASHBOARD_AUTH_MODE=cloud-run-iam" `
  --no-allow-unauthenticated `
  --ingress=all

$INTERNAL_API_URL = & $GCLOUD run services describe $INTERNAL_API_SERVICE `
  --project=$PROJECT_ID `
  --region=$REGION `
  --format="value(status.url)"

& $GCLOUD run services add-iam-policy-binding $INTERNAL_API_SERVICE `
  --project=$PROJECT_ID `
  --region=$REGION `
  --member="serviceAccount:$WEB_SA" `
  --role="roles/run.invoker"
```

## 4. 建置並部署 GCP 前端

專案根目錄的 `Dockerfile` 會建立 Next.js Cloud Run 版本。

```powershell
& $GCLOUD builds submit "." `
  --project=$PROJECT_ID `
  --tag=$WEB_IMAGE

& $GCLOUD services enable "iap.googleapis.com" `
  --project=$PROJECT_ID

& $GCLOUD run deploy $WEB_SERVICE `
  --project=$PROJECT_ID `
  --region=$REGION `
  --image=$WEB_IMAGE `
  --service-account=$WEB_SA `
  --set-env-vars="DASHBOARD_API_URL=$INTERNAL_API_URL,DASHBOARD_API_AUDIENCE=$INTERNAL_API_URL" `
  --no-allow-unauthenticated `
  --iap
```

## 5. 授權 IAP 與使用者

```powershell
$IAP_SERVICE_AGENT = "service-$PROJECT_NUMBER@gcp-sa-iap.iam.gserviceaccount.com"

& $GCLOUD run services add-iam-policy-binding $WEB_SERVICE `
  --project=$PROJECT_ID `
  --region=$REGION `
  --member="serviceAccount:$IAP_SERVICE_AGENT" `
  --role="roles/run.invoker"

& $GCLOUD iap web add-iam-policy-binding `
  --project=$PROJECT_ID `
  --region=$REGION `
  --resource-type="cloud-run" `
  --service=$WEB_SERVICE `
  --member="user:hans2004516@gmail.com" `
  --role="roles/iap.httpsResourceAccessor"
```

此專案若沒有 Google Cloud Organization，第一次啟用 IAP 通常要建立 External
OAuth 同意畫面與 Web OAuth client。自訂 client 必須套用到 Cloud Run 服務層級，
因此 `gcloud iap settings set` 需要同時指定
`--resource-type=cloud-run --region=asia-east1 --service=dashboard-web`；只設定
project 層級不會提供這個服務所需的登入憑證。OAuth client secret 不得放入 Git、
PowerShell 歷史或本文件，套用時應使用自動刪除的一次性暫存檔。

## 6. 驗證後才切換

驗證下列項目：

1. 未登入時會先出現 Google 登入。
2. 只有被授權的 Google 帳號能開啟網站。
3. 網頁不再要求 Dashboard API Key。
4. KPI、篩選、分頁與 CSV 下載正常。
5. 直接匿名開啟 `dashboard-api-internal/v1/dashboard` 會被拒絕。
6. Cloud Scheduler 與 `member-analysis` 仍可正常更新資料。

全部通過後，再另外停用舊 Sites 入口、移除舊公開 `dashboard-api` 與停用共享
API Key。不要在同一次操作中同時建立新入口與刪除舊入口。
