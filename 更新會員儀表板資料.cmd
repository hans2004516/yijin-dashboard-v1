@echo off
setlocal
chcp 65001 >nul
title 億進會員儀表板資料更新

if "%~1"=="" goto PICK_FILE

powershell.exe -NoLogo -NoProfile -ExecutionPolicy RemoteSigned -File "%~dp0tools\update-member-data\Update-YijinMemberData.ps1" -InputCsv "%~f1"
goto FINISH

:PICK_FILE
powershell.exe -NoLogo -NoProfile -ExecutionPolicy RemoteSigned -File "%~dp0tools\update-member-data\Update-YijinMemberData.ps1"

:FINISH
set "RESULT=%ERRORLEVEL%"
echo.
if "%RESULT%"=="0" (
  echo 流程已結束，可以關閉此視窗。
) else (
  echo 流程未完成，請保留上方錯誤訊息供排查。
)
pause
exit /b %RESULT%
