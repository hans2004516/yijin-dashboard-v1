# 本機一鍵更新會員儀表板

這個工具把既有的本機去識別化、GCS 上傳與 Cloud Run Job 執行串成一個流程。
一般使用者不需要開啟 PowerShell 或貼上指令。

## 一般使用

1. 雙擊專案根目錄的 `更新會員儀表板資料.cmd`。
2. 選擇原始的 `member_features.csv`。檔案選擇器預設會開啟專案上一層的
   `貼標籤` 資料夾。
3. 在黑色視窗輸入相同的去識別化專用密鑰兩次。密鑰不會顯示。
4. 查看筆數及雲端目的地，在確認視窗按「是」。
5. 等待上傳與雲端分析完成，再按任意鍵關閉視窗。

也可以把原始 CSV 拖曳到 `更新會員儀表板資料.cmd` 上，省略檔案選擇步驟。

去識別化檔案與稽核報告預設保留在：

~~~text
D:\Yijin-Secure\deidentified-output
~~~

## 安全措施

- 原始 CSV 與去識別化密鑰不會上傳。
- 密鑰只交給既有的本機去識別化程式，不會寫入腳本、報告或 GCP。
- 只接受 `member-features` 格式，並檢查欄位白名單、會員筆數與 SHA-256。
- 若選到已經以 `ANON-M-` 開頭的檔案，工具會停止，避免重複轉換會員代碼。
- 安全輸出不可放進 Git 專案資料夾。
- 真正上傳前一定會顯示確認視窗；預設按鈕為「否」。
- 上傳使用目前 GCS generation 作為前置條件。若其他人同時更新來源，舊畫面不會
  靜默覆寫較新的資料。
- 上傳完成後會等待 `member-analysis` Job 完整結束。分析失敗時，既有 Dashboard
  latest 仍保持上一個完整版本。

## 管理與測試參數

一般使用不需要參數。維護人員可直接執行 PowerShell 腳本：

~~~powershell
& ".\tools\update-member-data\Update-YijinMemberData.ps1" `
  -InputCsv "<來源 CSV>" `
  -OutputDirectory "<本機受控輸出資料夾>" `
  -LocalOnly
~~~

`-LocalOnly` 只建立並驗證安全輸出，不連線 GCP。`-NoDialogs` 適合自動測試；若要
在無視窗模式真的上傳，還必須同時明確加上 `-ApproveUpload`，避免意外更新。

本工具固定使用目前正式環境：

~~~text
project: yijin-member-insights-2026
region: asia-east1
bucket: yijin-member-insights-2026-data
raw object: raw/member_features.csv
Cloud Run Job: member-analysis
~~~

它不需要也不會讀取 Dashboard API Key。執行上傳的 Windows 使用者仍須先完成
Google Cloud CLI 登入，且帳號必須具備更新該 GCS 物件與執行 Job 的既有權限。
