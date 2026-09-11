@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo 无界面端到端自检（自动搜索 / 遥测 / 画面）...
".venv\Scripts\python.exe" -m app --core-test
pause
