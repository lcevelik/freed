@echo off
REM Build standalone executable from freed_reader.py
REM This creates a portable .exe that can run on any Windows computer

echo ========================================
echo Building FreeDReader.exe...
echo ========================================
echo.

REM Remove old build files
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist FreeDReader.spec del FreeDReader.spec

echo Cleaning old build files...
echo.

REM Build the executable
echo Creating executable with PyInstaller...
python -m PyInstaller --onefile --name FreeDReader --noconsole freed_reader.py

echo.
echo ========================================
echo Build Complete!
echo ========================================
echo.
echo Your executable is located at:
echo   dist\FreeDReader.exe
echo.
echo You can copy this file to any Windows computer and run it!
echo No Python installation needed on the target computer.
echo.

pause
