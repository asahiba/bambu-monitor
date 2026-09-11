@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo 启动虚拟打印机（供本机软件连接测试）...
".venv\Scripts\python.exe" -m app.sim %*
exit /b %errorlevel%
