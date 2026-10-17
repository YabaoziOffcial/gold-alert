@echo off
chcp 65001 >nul
cd /d "%~dp0"
git remote remove origin >nul 2>&1
git remote add origin https://github.com/YabaoziOffcial/gold-alert.git
echo 正在推送到 https://github.com/YabaoziOffcial/gold-alert.git
echo 如果弹出 GitHub 登录窗口，请按提示授权。
echo.
git push -u origin main
echo.
if %ERRORLEVEL%==0 (
  echo 推送成功！接下来去仓库网页添加 Secret: NTFY_TOPIC
) else (
  echo 推送失败，请把上面的报错发给我。
)
pause
