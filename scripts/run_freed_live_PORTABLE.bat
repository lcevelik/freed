@echo off
cd /d "%~dp0\.."
REM FreeD Reader - Portable Version (uses .exe)
REM No Python installation required!

echo Starting FreeD Reader (Portable)...
echo.
dist\FreeD_Reader_V2.0.0.exe --convert --ignore-checksum --clear --timecode 24

pause
