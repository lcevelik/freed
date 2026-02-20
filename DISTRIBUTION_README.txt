================================================================================
FreeD Protocol Reader - Portable Distribution
================================================================================

WHAT YOU NEED TO SHARE:
-----------------------
Copy these files/folders to another computer:

📁 dist\FreeDReader.exe     ← The main executable (REQUIRED)
📄 run_freed_live.bat        ← Optional: Quick launch script
📄 run_freed_live_timecode.bat ← Optional: Launch with timecode

That's it! No Python installation needed on the other computer!


RUNNING ON ANOTHER COMPUTER:
-----------------------------

METHOD 1: Double-click the FreeDReader.exe
  - This will show you the help menu with all options

METHOD 2: Use command prompt with options
  - Open Command Prompt (cmd)
  - Navigate to the folder with FreeDReader.exe
  - Run: FreeDReader.exe --convert --ignore-checksum --clear

METHOD 3: Use the provided batch files
  - Just double-click run_freed_live.bat


COMMON COMMANDS:
----------------

Basic live monitoring (recommended):
  FreeDReader.exe --convert --ignore-checksum --clear

With timecode at 24fps:
  FreeDReader.exe --convert --ignore-checksum --clear --timecode 24

Debug mode to see raw packets:
  FreeDReader.exe --debug --convert

Different port (default is 45000):
  FreeDReader.exe --port 40000 --convert --clear

Show all options:
  FreeDReader.exe --help


SYSTEM REQUIREMENTS:
--------------------
✓ Windows 10/11 (64-bit)
✓ Network access to FreeD source
✓ No Python or other software needed!


FILE SIZES:
-----------
FreeDReader.exe: ~4-7 MB (includes everything needed)


TROUBLESHOOTING:
----------------
If Windows blocks the .exe:
1. Right-click FreeDReader.exe
2. Select Properties
3. Check "Unblock" at the bottom
4. Click OK

If firewall blocks network:
1. Allow FreeDReader.exe through Windows Firewall
2. Or temporarily disable firewall for testing


NOTES:
------
- The executable is completely self-contained
- All calibration settings are built-in (Fujinon Premista 28-100mm)
- Port default is 45000 (can change with --port option)
- Works on any Windows computer without installation

================================================================================
