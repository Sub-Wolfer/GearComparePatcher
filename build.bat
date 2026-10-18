@echo off
rem Builds dist\GearComparePatcher.exe (Windows, Python 3.10+).
python -m pip install pyinstaller==6.22.3
python -m PyInstaller --onefile --console --name GearComparePatcher --exclude-module numpy --exclude-module PIL --exclude-module tkinter src\gc_patcher.py
