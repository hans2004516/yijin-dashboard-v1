# 本機會員 CSV 去識別化工具

這個工具完全在本機執行，不會連線 GCP，也不會自動上傳任何檔案。它只輸出
會員分析流程需要的欄位，其他欄位一律移除。

## 安全規則

- `member_id` 會轉成 `ANON-M-...`，`transaction_id` 會轉成 `ANON-T-...`。
- 代碼使用 HMAC-SHA256；相同專案使用相同密鑰時，同一會員會得到相同代碼。
- 姓名、電話、Email、地址、生日、身分證等非分析欄位不會寫入輸出。
- 交易明細模式只保留交易分析允許欄位；會員特徵模式只保留 Dashboard v1 必要欄位。
- CSV 使用 UTF-8 BOM，可供 Cloud Run Job 與常見試算表軟體讀取。
- 任一列格式錯誤時以非零狀態結束，且不留下不完整的正式輸出。
- 稽核報告只記錄筆數、欄位名稱與檔案雜湊，不包含原始會員編號或密鑰。
- 品類欄若疑似包含 Email、電話或試算表公式，會拒絕產出並標示列號。

去識別化密鑰和 Dashboard API Key 是兩個不同用途的秘密，絕對不要共用。
每個顧客／專案應使用不同的去識別化密鑰。

## 第一次使用

準備一組至少 32 bytes 的隨機密鑰，存進公司核准的密碼管理器。之後同一專案
每次轉檔都必須使用同一組密鑰，會員代碼才會保持一致。不要把密鑰寫進 Git、
`.env.local`、CSV、檔名、操作畫面截圖或聊天內容。

從專案根目錄執行：

~~~powershell
.\.venv-gcp\Scripts\python.exe .\tools\deidentify-member-csv\deidentify.py `
  --input "<本機原始CSV完整路徑>" `
  --output "<本機安全輸出資料夾>\member_features.deidentified.csv"
~~~

程式會要求輸入同一組密鑰兩次，輸入時畫面不會顯示內容。完成後會產生：

~~~text
member_features.deidentified.csv
member_features.deidentified.csv.report.json
~~~

先人工抽查輸出只有允許欄位，且 `member_id` 全部以 `ANON-M-` 開頭，再決定是否
上傳。請勿把原始檔放入本專案資料夾。

## 自動化使用

若內部流程已能安全提供環境變數，可設定 `YIJIN_DEIDENTIFICATION_SECRET`；工具
不會把它寫入輸出或紀錄：

~~~powershell
$env:YIJIN_DEIDENTIFICATION_SECRET = "<從受保護來源取得的密鑰>"
.\.venv-gcp\Scripts\python.exe .\tools\deidentify-member-csv\deidentify.py `
  --input "<來源CSV>" `
  --output "<安全輸出CSV>"
Remove-Item Env:YIJIN_DEIDENTIFICATION_SECRET
~~~

也可用 `--secret-file <受保護檔案>`，但該檔案不得放進專案、雲端同步資料夾或 Git。

## 本專案選定的來源 CSV

目前選定來源是專案上一層 `貼標籤\member_features.csv`。它屬於「會員特徵模式」，
不是逐筆交易明細。從 `yijin-dashboard` 根目錄執行：

~~~powershell
$InputCsv = (Resolve-Path "..\貼標籤\member_features.csv").Path
$OutputDirectory = "<本機受控輸出資料夾>"
$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$OutputCsv = Join-Path $OutputDirectory "member_features.deidentified.$Timestamp.csv"
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null

& ".\.venv-gcp\Scripts\python.exe" ".\tools\deidentify-member-csv\deidentify.py" `
  --input "$InputCsv" `
  --output "$OutputCsv"
~~~

程式會自動辨識會員特徵模式、去識別化 `member_id`、刪除未列入 Dashboard
契約的其他欄位，並移除會員編號及特徵都相同的重複列。會員編號大小寫有意義，
不會合併只差大小寫的編號；同一編號若出現不一致特徵，整次轉檔會失敗且不留下
正式輸出。

## 支援的來源欄位

### 交易明細模式

至少要有會員編號、交易金額與品類；日期、交易編號、促銷旗標及高單價旗標為選填。
支援現有分析 Job 所接受的英文與繁體中文欄名。日期支援 `YYYY-MM-DD`、
`YYYY/MM/DD`、`YYYYMMDD`；旗標支援 0/1、是/否、true/false。

### 會員特徵模式

必須包含：

~~~text
member_id,total_tx,total_amount,avg_amount,active_years,tx_per_year,
avg_gap_days,sale_ratio,premium_ratio,top_category,top_cat_ratio,is_loyal
~~~

可另外包含 `last_purchase_date`、`recency_days`。比例必須介於 0 到 1，數值不得
為負數，且 `is_loyal` 必須符合既有規則：`active_years >= 3`。其餘來源欄位
只會記在稽核報告的移除清單，不會寫入安全輸出。

工具只接受 UTF-8 或 UTF-8 BOM。金額、日期或旗標有錯時會指出列號，但不會在
錯誤訊息中顯示會員編號或原始欄位值。
