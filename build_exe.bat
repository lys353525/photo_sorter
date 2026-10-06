@echo off
setlocal
cd /d "%~dp0"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install pyinstaller
pyinstaller --noconfirm --clean --onefile --windowed --name "사진동영상정리도우미" photo_sorter_gui.py

echo.
echo Build complete. Check the dist folder.
pause
