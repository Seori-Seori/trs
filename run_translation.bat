@echo off
setlocal
set "SEORI_SCRIPT_DIR=%~dp0"
py -3 "%SEORI_SCRIPT_DIR%main.py" %*
exit /b %ERRORLEVEL%
