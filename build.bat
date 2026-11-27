@echo off
setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo [1/3] install dependencies
"%PY%" -m pip install -r requirements.txt pyinstaller || goto :fail

echo [2/3] build GUI exe (dist\Hysteria2Deployer.exe)
"%PY%" -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name Hysteria2Deployer --add-data "schema;schema" --hidden-import yaml main.py || goto :fail

echo [3/3] build CLI exe (dist\hy2ctl.exe)
"%PY%" -m PyInstaller --noconfirm --clean --onefile --console ^
  --name hy2ctl --add-data "schema;schema" --hidden-import yaml main.py || goto :fail

echo.
echo OK. GUI: dist\Hysteria2Deployer.exe   CLI: dist\hy2ctl.exe
echo Put both exe files in the SAME folder; profiles\bin\logs are created beside them.
goto :eof

:fail
echo BUILD FAILED
exit /b 1
