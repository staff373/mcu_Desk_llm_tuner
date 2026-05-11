@echo off
set "APP_DIR=%~dp0"
cd /d "%APP_DIR%"
where pythonw.exe >nul 2>nul
if %ERRORLEVEL%==0 (
  start "MCU Bluetooth Tuning Console" pythonw.exe "%APP_DIR%tuning_panel.py"
) else (
  start "MCU Bluetooth Tuning Console" python.exe "%APP_DIR%tuning_panel.py"
)
