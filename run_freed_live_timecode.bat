@echo off
cd /d "%~dp0"
python freed_reader.py --convert --ignore-checksum --clear --timecode 24
pause
