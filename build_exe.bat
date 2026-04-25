@echo off
REM ---------------------------------------------------------------
REM  Build gpx_photo_map.exe with PyInstaller
REM  Run this once to create the standalone executable.
REM ---------------------------------------------------------------

setlocal
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

echo === Installing PyInstaller ===
conda install -n gpxphotomap -c conda-forge pyinstaller -y
if errorlevel 1 (
    echo ERROR: Failed to install PyInstaller
    pause
    exit /b 1
)

echo.
echo === Building exe ===
conda run -n gpxphotomap pyinstaller ^
    --onefile ^
    --name gpx_photo_map ^
    --add-data "viewer.html;." ^
    --hidden-import PIL ^
    --hidden-import PIL.Image ^
    --hidden-import PIL.ExifTags ^
    --hidden-import PIL.ImageOps ^
    --hidden-import gpxpy ^
    --hidden-import gpxpy.gpx ^
    --hidden-import gpxpy.parser ^
    --console ^
    --clean ^
    gpx_photo_map.py

if errorlevel 1 (
    echo.
    echo ERROR: Build failed
    pause
    exit /b 1
)

echo.
echo === Done ===
echo Executable: %SCRIPT_DIR%dist\gpx_photo_map.exe
echo.
echo Usage:
echo   dist\gpx_photo_map.exe "X:\path\to\photos"
echo.
pause
