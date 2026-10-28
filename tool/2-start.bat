@echo off
chcp 65001 >nul
echo Стежу за папкою iCloud Drive\Книги. Не закривайте це вікно.
py audiobook_tool.py --watch
pause
