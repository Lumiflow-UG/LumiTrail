@echo off
setlocal

REM ---------------------------------------------------------------
REM  GPX Photo Map - Launcher
REM  Usage:  gpx_photo_map.bat "X:\path\to\photos_and_gpx"
REM          gpx_photo_map.bat "X:\dir1" "X:\dir2"
REM ---------------------------------------------------------------

set "SCRIPT_DIR=%~dp0"
set "OUTPUT_DIR=%SCRIPT_DIR%map_output"
set "CONDA_ENV=gpxphotomap"

if "%~1"=="" (
    echo Usage: %~nx0 "input_directory" ["input_directory2" ...]
    echo.
    echo Example: %~nx0 "X:\Bilder_Videos"
    exit /b 1
)

echo.
echo === GPX Photo Map ===
echo.

REM Build preprocess arguments from all input directories
set "INPUT_ARGS="
:build_args
if "%~1"=="" goto run_preprocess
set "INPUT_ARGS=%INPUT_ARGS% "%~1""
shift
goto build_args

:run_preprocess
echo [1/2] Preprocessing...
call conda run -n %CONDA_ENV% python "%SCRIPT_DIR%preprocess.py" %INPUT_ARGS% -o "%OUTPUT_DIR%"
if errorlevel 1 (
    echo ERROR: Preprocessing failed.
    pause
    exit /b 1
)

echo.
echo [2/2] Starting server...
call conda run -n %CONDA_ENV% python "%SCRIPT_DIR%server.py" -d "%OUTPUT_DIR%" -p 8080

endlocal
