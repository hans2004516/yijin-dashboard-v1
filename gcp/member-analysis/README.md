# Member Analysis Cloud Run Job

這個目錄是可部署為 Cloud Run Job 的會員特徵分析程式。它會讀取環境變數指定的
GCS raw CSV，自動辨識交易明細或已計算會員特徵，完成清理與整理後，先寫入歷史
版本，再原子性更新 processed/member_features.json。若來源 CSV 內容未改變，
Job 只整理到期歷史版本，不重新分析、建立歷史版本或覆寫 latest。

## 必要設定

- GCS_BUCKET：私人 GCS bucket 名稱，必填。
- GCS_SOURCE_OBJECT：raw 底下的來源物件；預設為 `raw/member_transactions.csv`。
  本專案選定的會員特徵來源部署時應設為 `raw/member_features.csv`。
- HISTORY_RETENTION_DAYS：歷史版本保留天數；預設為 `90`。
- HISTORY_MIN_COUNT：不論檔案年齡，至少保留的最新歷史版本數；預設為 `3`。
- LOCAL_STORAGE_ROOT：僅供本機測試的 GCS 目錄模擬；正式 Cloud Run Job 不可設定。

程式使用 Cloud Run 掛載的服務帳戶與 Application Default Credentials，
不讀取也不需要服務帳戶 JSON 金鑰。

## 輸入 CSV 模式

### 交易明細模式

必要的邏輯欄位為會員編號、交易金額與品類；日期、交易編號、促銷旗標與高單價旗標為選填。
支援英文欄名及常見繁體中文欄名，檔案必須使用 UTF-8 或 UTF-8 BOM。

建議欄位：

~~~text
member_id,transaction_id,transaction_date,amount,category,is_sale,is_premium
~~~

交易日期支援 YYYY-MM-DD、YYYY/MM/DD 與 YYYYMMDD。若有 transaction_id，
相同會員的重複交易編號會被排除。空會員編號、無效或負數金額會被略過，
清理數量會寫入結構化執行紀錄。

### 會員特徵模式

適用於已先完成貼標籤與特徵計算的來源。必要欄位如下：

~~~text
member_id,total_tx,total_amount,avg_amount,active_years,tx_per_year,
avg_gap_days,sale_ratio,premium_ratio,top_category,top_cat_ratio,is_loyal
~~~

可選欄位為 `last_purchase_date`、`recency_days`。Job 會驗證數值、比例與既有
忠誠會員規則，並移除會員編號及特徵都相同的重複列；會員編號大小寫有意義，
不會合併只差大小寫的編號。相同編號若有衝突特徵則整次失敗，不更新 latest。
真實會員編號必須先去識別化；可使用本機工具，或先上傳至隔離來源 bucket 後由
`member-deidentify` Cloud Run Job 轉換。含真實編號的原始檔不得直接上傳到此
Job 讀取的 `raw/` 路徑。

## 特徵規則

- total_tx：清理後交易列數。
- total_amount、avg_amount：累積與平均交易金額。
- active_years：有交易的不同曆年數；沒有日期欄位時為 1。
- tx_per_year：total_tx 除以 active_years。
- avg_gap_days：不同購買日期之間的平均天數。
- sale_ratio、premium_ratio：對應旗標占比；來源沒有欄位時為 0。
- top_category、top_cat_ratio：交易列數最多的品類與占比；同數時依金額、名稱決定。
- is_loyal：沿用現有儀表板邏輯，active_years 大於等於 3。
- last_purchase_date、recency_days：來源有日期欄位時產生。

## 本機執行

以下為 PowerShell 範例；只使用本目錄的合成去識別化資料：

~~~powershell
$localRoot = Join-Path $env:TEMP "yijin-gcs-local"
New-Item -ItemType Directory -Force (Join-Path $localRoot "raw") | Out-Null
Copy-Item .\testdata\member_transactions.csv (Join-Path $localRoot "raw\member_transactions.csv")
$env:GCS_BUCKET = "local-test-bucket"
$env:LOCAL_STORAGE_ROOT = $localRoot
python .\main.py
~~~

若要測試會員特徵模式，將去識別化後的檔案複製為
`$localRoot\raw\member_features.csv`，並額外設定：

~~~powershell
$env:GCS_SOURCE_OBJECT = "raw/member_features.csv"
python .\main.py
Remove-Item Env:GCS_SOURCE_OBJECT
~~~

成功後會產生：

~~~text
processed/member_features.json
processed/history/member_features_<UTC日期時間版本>.json
~~~

每次分析成功、歷史版本與 latest 都寫入完成後，Job 才會整理歷史版本：最新
`HISTORY_MIN_COUNT` 份永遠保留，其餘版本只有在超過 `HISTORY_RETENTION_DAYS`
後才刪除。其他檔名不會被自動刪除。若分析或寫入失敗，整理程序不會執行。

來源變更以 CSV 內容的 SHA-256 判斷，不只比較檔名或上傳時間。因此重新上傳內容
完全相同的檔案也不會產生重複版本。舊版 latest 尚未記錄 SHA-256 時，程式會先以
不可變的 GCS generation 相容判斷。未變更的執行會記錄 `analysis_skipped` 與
`reason: source_unchanged`；歷史保留整理仍會照常執行。

正式 GCS 執行時，服務帳戶除了 processed 前綴的讀寫刪除權限，還需要 bucket
層級的 `storage.objects.list`。該權限只能列出物件名稱與中繼資料，無法用
`resource.name` 條件限制前綴；程式本身只列出
`processed/history/member_features_` 前綴。

正式建置、IAM 與部署流程請見專案根目錄的 GCP_DEPLOYMENT.md。
