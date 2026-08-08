@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0ops\upstream\Update-Upstreams.ps1" %*
set "RESULT=!ERRORLEVEL!"
if "!RESULT!"=="3" (
  echo.
  echo 预览完成。若要执行已校验、可回滚的更新，请输入 UPDATE 后按回车。
  set /p "CONFIRM=确认文字: "
  if /I "!CONFIRM!"=="UPDATE" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0ops\upstream\Update-Upstreams.ps1" -ConfirmUpdate %*
    set "RESULT=!ERRORLEVEL!"
  )
)
if not "!RESULT!"=="0" pause
exit /b !RESULT!
