@echo off
set "APP_DIR=%~dp0"
if "%~1"=="" (
  echo Drag mcu_tuning_plan.yaml onto this launcher, or run:
  echo "%~nx0" "E:\path\to\mcu_tuning_plan.yaml"
  pause
  exit /b 1
)
cd /d "%APP_DIR%"
where pythonw.exe >nul 2>nul
if %ERRORLEVEL%==0 (
  start "MCU Bluetooth Auto Tune" pythonw.exe "%APP_DIR%tuning_panel.py" --plan "%~1" --mode run --auto-start
) else (
  start "MCU Bluetooth Auto Tune" python.exe "%APP_DIR%tuning_panel.py" --plan "%~1" --mode run --auto-start
)
