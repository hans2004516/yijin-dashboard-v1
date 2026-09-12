# Dashboard API Cloud Run Service

這個目錄是可部署為 Cloud Run Service 的唯一業務 API：

~~~http
GET /v1/dashboard
~~~

API 只讀取私人 GCS 的 processed/member_features.json，不會在請求中重新分析會員資料，
也不會呼叫 OpenAI。資料會依 GCS generation 與 updated metadata 快取；
物件更新後，下一次請求會載入新版本。

## 必要設定

- GCS_BUCKET：私人 GCS bucket 名稱，必填。
- ALLOWED_ORIGINS：允許的前端 origin，可用逗號分隔；正式環境請列出明確網域。
- DASHBOARD_API_KEY：共享 API Key，必填；正式環境必須由 Secret Manager 注入。
- LOCAL_STORAGE_ROOT：僅供本機測試的 GCS 目錄模擬；正式服務不可設定。
- PORT：服務監聽埠，Cloud Run 會自動提供。

API Key 只接受 `Authorization: Bearer <API_KEY>`，不接受網址查詢參數，避免金鑰
出現在瀏覽紀錄或存取紀錄。未提供或錯誤時回傳 HTTP 401；服務未綁定金鑰時
採預設拒絕並回傳 HTTP 503。

不要把 bucket 名稱、API Key 或任何憑證放入 NEXT_PUBLIC 變數。前端公開設定
只需要 Cloud Run 服務網址。

## 查詢參數

支援 category、loyalty、activeYears、frequency、search、page、pageSize，
以及 download=csv。pageSize 的有效數值會限制在 10 到 50；
格式錯誤的參數回傳 HTTP 400。

成功的 JSON 會保留 v1 契約中的 meta、summary、distributions、insights、
records 與 pagination，也保留既有 applied_filters 與 filter_options。
CSV 使用 UTF-8 BOM，讓繁體中文可直接由常見試算表軟體開啟。

processed/member_features.json 不存在、無法讀取或格式錯誤時回傳 HTTP 503，
回應不包含 bucket、服務帳戶或內部錯誤堆疊。

## 本機啟動

先依 member-analysis README 產生本機 processed 檔，再執行：

~~~powershell
$env:GCS_BUCKET = "local-test-bucket"
$env:LOCAL_STORAGE_ROOT = (Join-Path $env:TEMP "yijin-gcs-local")
$env:ALLOWED_ORIGINS = "http://localhost:3000"
$env:DASHBOARD_API_KEY = "<至少 32 個隨機字元的本機測試金鑰>"
$env:PORT = "8080"
python .\app.py
~~~

測試網址：

~~~text
Invoke-RestMethod "http://localhost:8080/v1/dashboard" -Headers @{ Authorization = "Bearer $env:DASHBOARD_API_KEY" }
~~~

瀏覽器跨網域請求會先執行 CORS preflight；允許的 origin 會收到
`Access-Control-Allow-Headers: Authorization, Content-Type`。

正式建置、IAM、CORS 與部署流程請見專案根目錄的 GCP_DEPLOYMENT.md。
