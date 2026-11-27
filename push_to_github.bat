@echo off
setlocal
cd /d "%~dp0"

set "REPO_URL=%~1"
if "%REPO_URL%"=="" set "REPO_URL=https://github.com/wcwtfhvk/hy2-win-deploy.git"

echo Target: %REPO_URL%
git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
  echo Not a git repository. Run this from the project folder.
  exit /b 1
)

git remote remove origin >nul 2>&1
git remote add origin "%REPO_URL%"
git branch -M main
git push -u origin main
if errorlevel 1 (
  echo.
  echo Push failed. Create an EMPTY repository on GitHub first, then rerun:
  echo   push_to_github.bat https://github.com/YOUR_NAME/YOUR_REPO.git
  exit /b 1
)
echo Done. Repository is on branch main.
