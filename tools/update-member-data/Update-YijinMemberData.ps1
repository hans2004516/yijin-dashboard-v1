[CmdletBinding()]
param(
    [Parameter()]
    [string]$InputCsv,

    [Parameter()]
    [string]$OutputDirectory,

    [Parameter()]
    [string]$PythonPath,

    [Parameter()]
    [string]$GcloudPath,

    [Parameter()]
    [string]$ProjectId = "yijin-member-insights-2026",

    [Parameter()]
    [string]$Region = "asia-east1",

    [Parameter()]
    [string]$Bucket = "yijin-member-insights-2026-data",

    [Parameter()]
    [string]$RawObject = "raw/member_features.csv",

    [Parameter()]
    [string]$JobName = "member-analysis",

    [Parameter()]
    [switch]$LocalOnly,

    [Parameter()]
    [switch]$ApproveUpload,

    [Parameter()]
    [switch]$NoDialogs
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:UploadCompleted = $false
$script:SafeCsvPath = $null
$script:ReportPath = $null

function Write-Step {
    param(
        [Parameter(Mandatory)]
        [int]$Number,

        [Parameter(Mandatory)]
        [string]$Message
    )

    Write-Host ""
    Write-Host ("[{0}/5] {1}" -f $Number, $Message) -ForegroundColor Cyan
}

function Show-UserMessage {
    param(
        [Parameter(Mandatory)]
        [string]$Text,

        [Parameter(Mandatory)]
        [string]$Title,

        [ValidateSet("Info", "Warning", "Error")]
        [string]$Kind = "Info"
    )

    if ($NoDialogs) {
        return
    }

    Add-Type -AssemblyName System.Windows.Forms
    $icon = switch ($Kind) {
        "Warning" { [System.Windows.Forms.MessageBoxIcon]::Warning }
        "Error" { [System.Windows.Forms.MessageBoxIcon]::Error }
        default { [System.Windows.Forms.MessageBoxIcon]::Information }
    }
    [void][System.Windows.Forms.MessageBox]::Show(
        $Text,
        $Title,
        [System.Windows.Forms.MessageBoxButtons]::OK,
        $icon
    )
}

function Test-IsInsideDirectory {
    param(
        [Parameter(Mandatory)]
        [string]$Candidate,

        [Parameter(Mandatory)]
        [string]$Directory
    )

    $candidatePath = [System.IO.Path]::GetFullPath($Candidate).TrimEnd('\', '/')
    $directoryPath = [System.IO.Path]::GetFullPath($Directory).TrimEnd('\', '/')
    return $candidatePath.StartsWith(
        $directoryPath + [System.IO.Path]::DirectorySeparatorChar,
        [System.StringComparison]::OrdinalIgnoreCase
    ) -or $candidatePath.Equals(
        $directoryPath,
        [System.StringComparison]::OrdinalIgnoreCase
    )
}

function Resolve-Executable {
    param(
        [Parameter()]
        [string]$RequestedPath,

        [Parameter(Mandatory)]
        [string[]]$CommandNames,

        [Parameter()]
        [string[]]$FallbackPaths = @(),

        [Parameter(Mandatory)]
        [string]$FriendlyName
    )

    if ($RequestedPath) {
        $candidate = [System.IO.Path]::GetFullPath($RequestedPath)
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
        throw "找不到 $FriendlyName：$candidate"
    }

    foreach ($commandName in $CommandNames) {
        $command = Get-Command $commandName -ErrorAction SilentlyContinue
        if ($null -ne $command) {
            return $command.Source
        }
    }

    foreach ($fallback in $FallbackPaths) {
        if ($fallback -and (Test-Path -LiteralPath $fallback -PathType Leaf)) {
            return [System.IO.Path]::GetFullPath($fallback)
        }
    }

    throw "找不到 $FriendlyName。請先完成安裝，再重新執行。"
}

function Select-SourceCsv {
    param(
        [Parameter(Mandatory)]
        [string]$InitialDirectory
    )

    if ($NoDialogs) {
        throw "未指定 InputCsv；無視窗模式不能開啟檔案選擇器。"
    }

    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    $dialog.Title = "選擇原始 member_features.csv"
    $dialog.Filter = "CSV 檔案 (*.csv)|*.csv"
    $dialog.Multiselect = $false
    $dialog.CheckFileExists = $true
    if (Test-Path -LiteralPath $InitialDirectory -PathType Container) {
        $dialog.InitialDirectory = [System.IO.Path]::GetFullPath($InitialDirectory)
    }
    if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
        return $null
    }
    return $dialog.FileName
}

function Get-SourceMemberIdSample {
    param(
        [Parameter(Mandatory)]
        [string]$Path
    )

    $firstRow = Import-Csv -LiteralPath $Path | Select-Object -First 1
    if ($null -eq $firstRow) {
        throw "來源 CSV 沒有資料列。"
    }

    $memberAliases = @("member_id", "會員編號", "會員id", "會員代碼")
    foreach ($property in $firstRow.PSObject.Properties) {
        if ($memberAliases -contains $property.Name.Trim()) {
            return [string]$property.Value
        }
    }
    return $null
}

function Invoke-NativeChecked {
    param(
        [Parameter(Mandatory)]
        [string]$Executable,

        [Parameter(Mandatory)]
        [string[]]$Arguments,

        [Parameter(Mandatory)]
        [string]$FailureMessage,

        [Parameter()]
        [switch]$CaptureOutput
    )

    if ($CaptureOutput) {
        $output = @(& $Executable @Arguments)
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0) {
            throw "$FailureMessage（結束碼 $exitCode）"
        }
        return $output
    }

    & $Executable @Arguments
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "$FailureMessage（結束碼 $exitCode）"
    }
}

function Confirm-CloudUpload {
    param(
        [Parameter(Mandatory)]
        [string]$SourceName,

        [Parameter(Mandatory)]
        [long]$Rows,

        [Parameter(Mandatory)]
        [long]$UniqueMembers,

        [Parameter(Mandatory)]
        [string]$Destination
    )

    if ($ApproveUpload) {
        return $true
    }
    if ($NoDialogs) {
        throw "無視窗模式必須明確加上 -ApproveUpload 才能上傳。"
    }

    Add-Type -AssemblyName System.Windows.Forms
    $message = @"
本機去識別化與安全檢查已完成。

來源檔：$SourceName
輸出筆數：$($Rows.ToString('N0'))
唯一會員：$($UniqueMembers.ToString('N0'))
雲端目的地：$Destination

只會上傳去識別化 CSV，不會上傳原始 CSV 或密鑰。
是否現在上傳並執行雲端分析？
"@
    $choice = [System.Windows.Forms.MessageBox]::Show(
        $message,
        "確認更新會員儀表板",
        [System.Windows.Forms.MessageBoxButtons]::YesNo,
        [System.Windows.Forms.MessageBoxIcon]::Question,
        [System.Windows.Forms.MessageBoxDefaultButton]::Button2
    )
    return $choice -eq [System.Windows.Forms.DialogResult]::Yes
}

try {
    $repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
    $converterPath = Join-Path $repoRoot "tools\deidentify-member-csv\deidentify.py"
    if (-not (Test-Path -LiteralPath $converterPath -PathType Leaf)) {
        throw "找不到本機去識別化程式：$converterPath"
    }

    Write-Host "億進會員儀表板｜本機一鍵資料更新" -ForegroundColor Green
    Write-Host "原始 CSV 與去識別化密鑰只留在本機。" -ForegroundColor DarkGray

    Write-Step -Number 1 -Message "選擇並檢查來源 CSV"
    if (-not $InputCsv) {
        $defaultSourceDirectory = Join-Path (Split-Path -Parent $repoRoot) "貼標籤"
        $InputCsv = Select-SourceCsv -InitialDirectory $defaultSourceDirectory
        if (-not $InputCsv) {
            Write-Host "已取消，沒有產生或上傳任何檔案。" -ForegroundColor Yellow
            exit 0
        }
    }
    $InputCsv = [System.IO.Path]::GetFullPath($InputCsv)
    if (-not (Test-Path -LiteralPath $InputCsv -PathType Leaf)) {
        throw "找不到來源 CSV：$InputCsv"
    }
    if ([System.IO.Path]::GetExtension($InputCsv) -ne ".csv") {
        throw "來源檔必須是 CSV。"
    }

    $sourceMemberId = Get-SourceMemberIdSample -Path $InputCsv
    if ($sourceMemberId -and $sourceMemberId.StartsWith("ANON-M-", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "選到的檔案已經去識別化。請選擇原始 member_features.csv，避免會員代碼被再次轉換。"
    }
    Write-Host "來源：$InputCsv"

    if (-not $OutputDirectory) {
        if (Test-Path -LiteralPath "D:\") {
            $OutputDirectory = "D:\Yijin-Secure\deidentified-output"
        }
        else {
            $OutputDirectory = Join-Path $env:LOCALAPPDATA "Yijin-Secure\deidentified-output"
        }
    }
    $OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
    if (Test-IsInsideDirectory -Candidate $OutputDirectory -Directory $repoRoot) {
        throw "安全輸出資料夾不可放在 yijin-dashboard 專案內，請改用 D:\Yijin-Secure\deidentified-output。"
    }
    [void](New-Item -ItemType Directory -Force -Path $OutputDirectory)

    $PythonPath = Resolve-Executable `
        -RequestedPath $PythonPath `
        -CommandNames @("python.exe", "python") `
        -FallbackPaths @((Join-Path $repoRoot ".venv-gcp\Scripts\python.exe")) `
        -FriendlyName "Python"

    $timestamp = Get-Date -Format "yyyyMMdd-HHmmssfff"
    $script:SafeCsvPath = Join-Path $OutputDirectory "member_features.deidentified.$timestamp.csv"
    $script:ReportPath = "$($script:SafeCsvPath).report.json"

    Write-Step -Number 2 -Message "在本機去識別化"
    Write-Host "接下來請輸入同一組『去識別化專用密鑰』兩次；輸入時畫面不會顯示字元。" -ForegroundColor Yellow
    $converterOutput = Invoke-NativeChecked `
        -Executable $PythonPath `
        -Arguments @(
            $converterPath,
            "--input", $InputCsv,
            "--output", $script:SafeCsvPath
        ) `
        -FailureMessage "本機去識別化失敗" `
        -CaptureOutput

    $successLine = $converterOutput | Where-Object { $_ -match '"event"\s*:\s*"deidentification_succeeded"' } | Select-Object -Last 1
    if (-not $successLine) {
        throw "去識別化程式沒有回傳成功結果。"
    }
    [void]($successLine | ConvertFrom-Json)

    Write-Step -Number 3 -Message "驗證安全輸出"
    if (-not (Test-Path -LiteralPath $script:SafeCsvPath -PathType Leaf)) {
        throw "去識別化輸出檔不存在。"
    }
    if (-not (Test-Path -LiteralPath $script:ReportPath -PathType Leaf)) {
        throw "去識別化稽核報告不存在。"
    }
    $report = Get-Content -LiteralPath $script:ReportPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($report.schema_version -ne "yijin-deidentified-csv-v2") {
        throw "稽核報告版本不符，已停止上傳。"
    }
    if ($report.input_mode -ne "member-features") {
        throw "來源 CSV 不是會員特徵格式，已停止上傳。"
    }

    $requiredColumns = @(
        "member_id", "total_tx", "total_amount", "avg_amount", "active_years",
        "tx_per_year", "avg_gap_days", "sale_ratio", "premium_ratio",
        "top_category", "top_cat_ratio", "is_loyal"
    )
    $allowedColumns = $requiredColumns + @("last_purchase_date", "recency_days")
    $outputColumns = @($report.output_columns)
    foreach ($requiredColumn in $requiredColumns) {
        if ($outputColumns -notcontains $requiredColumn) {
            throw "安全輸出缺少必要欄位：$requiredColumn"
        }
    }
    foreach ($outputColumn in $outputColumns) {
        if ($allowedColumns -notcontains $outputColumn) {
            throw "安全輸出包含未允許欄位：$outputColumn"
        }
    }

    [long]$outputRows = $report.stats.output_rows
    [long]$uniqueMembers = $report.stats.unique_members
    if ($outputRows -le 0 -or $uniqueMembers -le 0 -or $outputRows -ne $uniqueMembers) {
        throw "安全輸出的會員筆數不合理，已停止上傳。"
    }
    $actualHash = (Get-FileHash -LiteralPath $script:SafeCsvPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne ([string]$report.output_sha256).ToLowerInvariant()) {
        throw "安全輸出的 SHA-256 與稽核報告不符，已停止上傳。"
    }
    $safeSample = Import-Csv -LiteralPath $script:SafeCsvPath | Select-Object -First 1
    if ($null -eq $safeSample -or -not ([string]$safeSample.member_id).StartsWith("ANON-M-")) {
        throw "安全輸出的會員代碼格式不符，已停止上傳。"
    }

    Write-Host ("驗證完成：{0:N0} 筆、{1:N0} 位唯一會員" -f $outputRows, $uniqueMembers) -ForegroundColor Green
    Write-Host "安全輸出：$($script:SafeCsvPath)"
    Write-Host "稽核報告：$($script:ReportPath)"
    $removedSensitive = @($report.sensitive_columns_removed)
    if ($removedSensitive.Count -gt 0) {
        Write-Host ("已移除的敏感欄位：" + ($removedSensitive -join "、")) -ForegroundColor Yellow
    }

    if ($LocalOnly) {
        Write-Step -Number 4 -Message "本機驗證模式：略過雲端上傳"
        Write-Step -Number 5 -Message "完成"
        Write-Host "本機安全輸出與報告均已建立；沒有連線或變更 GCP。" -ForegroundColor Green
        exit 0
    }

    $destination = "gs://$Bucket/$RawObject"
    if (-not (Confirm-CloudUpload `
        -SourceName ([System.IO.Path]::GetFileName($InputCsv)) `
        -Rows $outputRows `
        -UniqueMembers $uniqueMembers `
        -Destination $destination)) {
        Write-Step -Number 4 -Message "已取消雲端上傳"
        Write-Step -Number 5 -Message "完成"
        Write-Host "本機安全輸出與報告已保留；GCP 沒有變更。" -ForegroundColor Yellow
        exit 0
    }

    $gcloudFallback = Join-Path $env:LOCALAPPDATA "Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
    $GcloudPath = Resolve-Executable `
        -RequestedPath $GcloudPath `
        -CommandNames @("gcloud.cmd", "gcloud") `
        -FallbackPaths @($gcloudFallback) `
        -FriendlyName "Google Cloud CLI"

    Write-Step -Number 4 -Message "上傳去識別化 CSV"
    $generationOutput = Invoke-NativeChecked `
        -Executable $GcloudPath `
        -Arguments @(
            "storage", "objects", "describe", $destination,
            "--project=$ProjectId",
            "--format=value(generation)"
        ) `
        -FailureMessage "無法讀取目前的雲端來源版本" `
        -CaptureOutput
    $currentGeneration = (($generationOutput | Select-Object -Last 1) | Out-String).Trim()
    if ($currentGeneration -notmatch '^\d+$') {
        throw "雲端來源版本格式不正確，已停止上傳。"
    }

    Invoke-NativeChecked `
        -Executable $GcloudPath `
        -Arguments @(
            "storage", "cp", $script:SafeCsvPath, $destination,
            "--project=$ProjectId",
            "--if-generation-match=$currentGeneration"
        ) `
        -FailureMessage "安全 CSV 上傳失敗；雲端來源可能已被其他人更新"
    $script:UploadCompleted = $true

    $remoteMetadataOutput = Invoke-NativeChecked `
        -Executable $GcloudPath `
        -Arguments @(
            "storage", "objects", "describe", $destination,
            "--project=$ProjectId",
            "--format=json(generation,size,updateTime)"
        ) `
        -FailureMessage "已上傳，但無法驗證雲端物件" `
        -CaptureOutput
    $remoteMetadata = (($remoteMetadataOutput -join [Environment]::NewLine) | ConvertFrom-Json)
    $localSize = (Get-Item -LiteralPath $script:SafeCsvPath).Length
    if ([long]$remoteMetadata.size -ne [long]$localSize) {
        throw "已上傳，但雲端物件大小與本機檔案不符。"
    }
    Write-Host "上傳完成；雲端 generation：$($remoteMetadata.generation)" -ForegroundColor Green

    Write-Step -Number 5 -Message "執行雲端分析並等待完成"
    Invoke-NativeChecked `
        -Executable $GcloudPath `
        -Arguments @(
            "run", "jobs", "execute", $JobName,
            "--region=$Region",
            "--project=$ProjectId",
            "--wait"
        ) `
        -FailureMessage "雲端分析 Job 執行失敗"

    $completionMessage = @"
會員資料更新流程已完成。

安全輸出：$($script:SafeCsvPath)
輸出筆數：$($outputRows.ToString('N0'))
雲端來源 generation：$($remoteMetadata.generation)
Cloud Run Job：$JobName 已成功完成

若來源內容與上一版相同，Job 會安全略過重算；若內容有變，儀表板會讀取新的分析版本。
"@
    Write-Host "會員資料更新流程完成。" -ForegroundColor Green
    Show-UserMessage -Text $completionMessage -Title "更新完成" -Kind Info
    exit 0
}
catch {
    $detail = $_.Exception.Message
    Write-Host ""
    Write-Host "更新失敗：$detail" -ForegroundColor Red
    if ($script:SafeCsvPath -and (Test-Path -LiteralPath $script:SafeCsvPath -PathType Leaf)) {
        Write-Host "本機安全輸出仍保留在：$($script:SafeCsvPath)" -ForegroundColor Yellow
    }

    if ($script:UploadCompleted) {
        $message = "去識別化 CSV 已上傳，但後續驗證或雲端分析未完成。`r`n既有儀表板版本不會被不完整分析覆寫。`r`n`r`n錯誤：$detail"
    }
    else {
        $message = "更新未完成，雲端來源尚未被本工具改寫。`r`n`r`n錯誤：$detail"
    }
    Show-UserMessage -Text $message -Title "更新失敗" -Kind Error
    exit 1
}
