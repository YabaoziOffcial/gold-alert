@echo off
chcp 65001 >nul
if "%~1"=="" (
  echo 用法: push_to_github.bat https://github.com/用户名/仓库名.git
  pause
  exit /b 1
)
cd /d "%~dp0"
git remote remove origin >nul 2>&1
git remote add origin "%~1"
echo 正在推送到 %~1 ...
git push -u origin main
echo.
echo 推送结束。若失败，检查仓库是否已在 GitHub 上创建（不要勾选 README）。
pause
