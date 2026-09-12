# Member Deidentification Cloud Run Job

此 Job 從獨立的私人來源 bucket 讀取含真實會員編號的 CSV，使用 Secret Manager
提供的固定版本 HMAC 密鑰，輸出去識別化 CSV 到既有分析 bucket。分析 Job 與
Dashboard API 都不需要、也不應取得來源 bucket 權限。

## 資料路徑

```text
私人來源 bucket / incoming/member_features.csv
        |
        | member-deidentify-sa + Secret Manager HMAC key
        v
分析資料 bucket / raw/member_features.csv
        |
        v
member-analysis -> processed/member_features.json
```

稽核報告會寫入分析資料 bucket 的
`audit/deidentification/`，只包含雜湊、欄位名稱、筆數與 GCS generation，不含
原始會員編號、姓名、電話、Email、地址或 HMAC 密鑰。

## 必要設定

- `SOURCE_BUCKET`：只存放可識別來源的私人 bucket。
- `SOURCE_OBJECT`：必須位於 `incoming/` 且為 CSV；預設
  `incoming/member_features.csv`。
- `DESTINATION_BUCKET`：既有分析資料 bucket。
- `DESTINATION_OBJECT`：必須位於 `raw/` 且為 CSV；預設
  `raw/member_features.csv`。
- `DEIDENTIFICATION_KEY_VERSION`：Secret Manager 固定版本編號；用於輸出中繼資料
  及明確的密鑰輪替。
- `YIJIN_DEIDENTIFICATION_SECRET`：由 Secret Manager 注入，不可直接設為一般環境
  變數或放入映像。
- `MAX_SOURCE_BYTES`：來源上限，預設 512 MiB。

## 安全與一致性

- 來源與輸出使用不同 bucket，便於實施最小權限。
- 來源下載鎖定不可變 GCS generation；處理期間若來源變更，本次不發布。
- 只有完整通過欄位、數值與 PII 檢查後才覆寫安全 CSV。
- 目的物件使用 generation precondition，並行執行不會靜默互相覆寫。
- 第一次切換時若既有 `raw/member_features.csv` 尚無雲端去識別化中繼資料，輸出
  必須與既有安全 CSV 的 SHA-256 完全相同；不同時拒絕覆寫，避免密鑰輸入錯誤。
- 相同 generation 不下載；相同內容的新 generation 以 SHA-256 判斷後跳過。
- HMAC 密鑰必須沿用既有本機密鑰，否則所有匿名會員代碼都會改變。
- Job 不會自動刪除可識別來源；來源保留期限應在 bucket lifecycle 中另行決定。

完整建置、IAM、Secret Manager 與 Workflows 步驟見專案根目錄
`GCP_CLOUD_DEIDENTIFICATION.md`。
