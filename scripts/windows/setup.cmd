@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
set "exitCode=%ERRORLEVEL%"
if not "%exitCode%"=="0" if "%~1"=="" (
    echo.
    echo Blockpedia setup failed. See the error above.
    pause
)
endlocal & exit /b %exitCode%
