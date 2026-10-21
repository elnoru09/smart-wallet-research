@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 "%~dp0master.py"
goto finish
:use_python
python "%~dp0master.py"
:finish
echo.
pause
endlocal
