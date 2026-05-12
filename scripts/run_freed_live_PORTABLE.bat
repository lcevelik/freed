@echo off
cd /d "%~dp0\.."
REM FreeD Reader - Portable Version (uses .exe)
REM No Python installation required!

echo Starting FreeD Reader (Portable)...
echo.
dist\FreeD_Reader_V1.9.1.exe --convert --ignore-checksum --clear --timecode 24

pause
