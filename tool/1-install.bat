@echo off
chcp 65001 >nul
echo Встановлюю потрібні бібліотеки...
py -m pip install -r requirements.txt
echo.
echo Готово. Далі запускайте 2-start.bat
pause
