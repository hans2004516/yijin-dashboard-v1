Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$toolRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $toolRoot "..\.."))
$scriptPath = Join-Path $toolRoot "Update-YijinMemberData.ps1"
$fixturePath = Join-Path $PSScriptRoot "member_features.sample.csv"
$pythonPath = Join-Path $repoRoot ".venv-gcp\Scripts\python.exe"
$testRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("yijin-update-tool-test-" + [Guid]::NewGuid().ToString("N"))

if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
    throw "找不到受測腳本。"
}
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "找不到專案 Python。"
}

$tokens = $null
$parseErrors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile(
    $scriptPath,
    [ref]$tokens,
    [ref]$parseErrors
)
if ($parseErrors.Count -ne 0) {
    throw ("PowerShell 語法錯誤：" + (($parseErrors | ForEach-Object Message) -join "；"))
}

[void](New-Item -ItemType Directory -Force -Path $testRoot)
$oldSecret = $env:YIJIN_DEIDENTIFICATION_SECRET
try {
    $env:YIJIN_DEIDENTIFICATION_SECRET = "TEST-ONLY-SECRET-0123456789-ABCDEFGHIJ"
    & $scriptPath `
        -InputCsv $fixturePath `
        -OutputDirectory $testRoot `
        -PythonPath $pythonPath `
        -LocalOnly `
        -NoDialogs
    if ($LASTEXITCODE -ne 0) {
        throw "本機整合測試執行失敗。"
    }

    $outputs = @(Get-ChildItem -LiteralPath $testRoot -Filter "*.csv" -File)
    $reports = @(Get-ChildItem -LiteralPath $testRoot -Filter "*.report.json" -File)
    if ($outputs.Count -ne 1 -or $reports.Count -ne 1) {
        throw "測試輸出檔案數量不正確。"
    }

    $report = Get-Content -LiteralPath $reports[0].FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($report.input_mode -ne "member-features") {
        throw "測試報告的輸入模式不正確。"
    }
    if ([long]$report.stats.output_rows -ne 2 -or [long]$report.stats.unique_members -ne 2) {
        throw "測試報告筆數不正確。"
    }
    if (@($report.sensitive_columns_removed) -notcontains "姓名") {
        throw "測試沒有確認敏感欄位移除。"
    }

    $rows = @(Import-Csv -LiteralPath $outputs[0].FullName)
    if ($rows.Count -ne 2) {
        throw "測試安全 CSV 筆數不正確。"
    }
    if (@($rows | Where-Object { -not $_.member_id.StartsWith("ANON-M-") }).Count -ne 0) {
        throw "測試安全 CSV 包含未去識別化的會員代碼。"
    }
    if ($rows[0].PSObject.Properties.Name -contains "姓名") {
        throw "測試安全 CSV 仍包含敏感欄位。"
    }

    Write-Host "本機一鍵更新工具測試通過。" -ForegroundColor Green
}
finally {
    if ($null -eq $oldSecret) {
        Remove-Item Env:YIJIN_DEIDENTIFICATION_SECRET -ErrorAction SilentlyContinue
    }
    else {
        $env:YIJIN_DEIDENTIFICATION_SECRET = $oldSecret
    }

    $resolvedTemp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd('\')
    $resolvedTest = [System.IO.Path]::GetFullPath($testRoot)
    if ($resolvedTest.StartsWith($resolvedTemp + "\yijin-update-tool-test-", [System.StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $resolvedTest -Recurse -Force -ErrorAction SilentlyContinue
    }
}
