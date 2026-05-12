@echo off
cd /d "%~dp0\.."
REM Build standalone executable from freed_reader.py
REM This creates a portable .exe that can run on any Windows computer

echo ========================================
echo Building FreeD_Reader_V1.9.1.exe...
echo ========================================
echo.

REM Remove old build files
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo Cleaning old build files...
echo.

REM Build the executable using the versioned spec
echo Creating executable with PyInstaller...
python -m PyInstaller specs\FreeD_Reader_V1.9.1.spec

echo.
echo ========================================
echo Build Complete!
echo ========================================
echo.
echo Your executable is located at:
echo   dist\FreeD_Reader_V1.9.1.exe
echo.
echo You can copy this file to any Windows computer and run it!
echo No Python installation needed on the target computer.
echo.

pause
