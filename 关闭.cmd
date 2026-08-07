@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0ops\local\Stop-Local.ps1" %*
if errorlevel 1 (
    echo.
    echo 关闭失败，请查看上面的错误信息。
    pause
    exit /b 1
)
exit /b 0
