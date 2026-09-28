@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   Music Downloader Web 启动中...
echo   浏览器访问: http://127.0.0.1:8899
echo   按 Ctrl+C 停止服务
echo ============================================
python app.py
pause
