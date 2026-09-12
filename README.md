# 億進寢具會員行為洞察中心

這個網站只負責呼叫 API 與呈現儀表板。資料清理、欄位轉換、指標彙整與權限控制應留在 GCP。

## 本機測試資料

- `data/member_features_test.json`：從 `貼標籤/member_features.csv` 等距抽樣 720 筆後重新編碼測試會員 ID。
- 原始會員 ID 未放入網站測試檔。
- 本機 `/api/dashboard` 會模擬 GCP API，支援篩選、分頁、彙整與 CSV 匯出。
- 這份抽樣資料只用來驗證網站，不能代替全體會員分析結論。

## 切換成 GCP API

將 `.env.example` 複製為 `.env.local`，把 `NEXT_PUBLIC_DASHBOARD_API_URL`
設為 Cloud Run Service 根網址。前端會自動呼叫 `/v1/dashboard`，並帶入下列查詢參數：

- `category`
- `loyalty`
- `activeYears`
- `frequency`
- `search`
- `page`
- `pageSize`
- `download=csv`（下載篩選後資料）

GCP 必須回傳與本機 `GET /api/dashboard` 相同的 v1 JSON 契約，並允許正式網站網域的 CORS。不要把服務帳戶金鑰或其他秘密放在 `NEXT_PUBLIC_*` 變數。

設定正式 GCP API 網址後，畫面會要求使用者輸入共享 API Key，並以
`Authorization: Bearer` 傳送。金鑰只保留在該瀏覽器分頁的 session storage；
關閉分頁後需重新輸入。本機 `/api/dashboard` 模擬模式不要求金鑰。

共享金鑰必須存放在 GCP Secret Manager，由 Cloud Run 注入；不得寫入
`.env.local`、Sites 環境變數、`NEXT_PUBLIC_*`、Git 或網址查詢參數。

## GCP MVP

可部署程式位於：

- `gcp/member-analysis/`：Cloud Run Job，從私人 GCS raw 交易或會員特徵 CSV 產生 latest 與歷史會員特徵 JSON。
- `gcp/member-deidentify/`：Cloud Run Job，從隔離的可識別來源 bucket 產生安全 CSV 與稽核報告。
- `gcp/dashboard-api/`：Cloud Run Service，只提供 `GET /v1/dashboard`。
- `gcp/member-analysis/testdata/`：全新合成、去識別化交易資料，只供 MVP 驗證。

完整的 bucket、服務帳戶、最小 IAM、建置、部署、測試與排查步驟請見
`GCP_DEPLOYMENT.md`。專案不包含服務帳戶 JSON 金鑰，也不會自動建立 GCP 資源。
雲端去識別化與串接流程另見 `GCP_CLOUD_DEIDENTIFICATION.md`。

## 本機去識別化轉檔

`tools/deidentify-member-csv/` 提供完全離線的會員 CSV 轉檔工具。它同時支援
交易明細與已計算會員特徵，使用 HMAC-SHA256 將會員與交易編號轉成穩定代碼，
只輸出分析允許欄位，並移除姓名、電話、Email、地址等其他欄位。轉檔不會連線
GCP，也不會自動上傳資料。

目前正式候選來源定為上一層 `貼標籤/member_features.csv`。該檔案仍含原始
`member_id`，不可直接上傳到既有分析 bucket 的 `raw/member_features.csv`。
正式流程可選擇本機去識別化，或上傳到隔離的可識別來源 bucket 後交由
`member-deidentify` Cloud Run Job 轉換。

同一顧客專案必須固定使用自己的去識別化密鑰；不同顧客不得共用密鑰，且該密鑰
不得與 Dashboard API Key 共用。詳細操作與安全檢查請見工具目錄的 README。

## 本機一鍵更新（雲端去識別化切換前）

日常更新可直接雙擊專案根目錄的 `更新會員儀表板資料.cmd`。工具會依序選取來源、
本機去識別化、驗證稽核報告、要求人工確認、上傳安全 CSV，最後等待 Cloud Run
分析完成。也可以將來源 CSV 拖曳到該啟動器上。

原始 CSV 與去識別化密鑰不會上傳；若使用者在確認視窗選「否」，GCP 完全不會
變更。操作畫面與維護參數請見 `tools/update-member-data/README.md`。

## 儀表板範圍

- 動態 KPI：會員數、交易次數、消費金額、平均交易金額、忠誠會員占比。
- 圖表：主要品類、品類消費金額、活躍年數、累積交易頻次、忠誠會員結構。
- 互動：5 種篩選／搜尋、API 重新整理、會員表格分頁、下載篩選後 CSV。
- 說明：KPI 分母、抽樣限制與正式 API 介接方式。
