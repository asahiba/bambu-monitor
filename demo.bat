@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo 启动演示模式：内置 4 台虚拟打印机（访问代码 12345678），不会写入真实配置。
".venv\Scripts\python.exe" -m app --sim %*
exit /b %errorlevel%
