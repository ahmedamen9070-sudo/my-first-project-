@echo off
setlocal
cd /d "%~dp0"
if exist ".desktop-venv\Scripts\python.exe" goto deps
where py >nul 2>&1
if errorlevel 1 (
  python -m venv .desktop-venv
) else (
  py -3 -m venv .desktop-venv
)
if errorlevel 1 goto failed
:deps
if exist ".desktop-venv\am-desktop-6.2.1.ready" goto launch
".desktop-venv\Scripts\python.exe" -m pip install -r requirements-desktop.txt
if errorlevel 1 goto failed
".desktop-venv\Scripts\python.exe" -c "import webview"
if errorlevel 1 goto failed
echo 6.2.1> ".desktop-venv\am-desktop-6.2.1.ready"
:launch
".desktop-venv\Scripts\python.exe" run.py
if errorlevel 1 goto failed
exit /b 0
:failed
echo A^&M could not start. Python 3.10+ and Microsoft Edge WebView2 are required.
echo The first desktop setup needs Internet access to download its components.
pause
exit /b 1
