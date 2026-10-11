@echo off
REM tsproj2fla drag-and-drop launcher (Windows).
REM
REM Drag one or more ToonSquid .tsproj files onto this file to convert them to
REM Adobe Animate .fla. The window stays open so you can read the results.
REM
REM On first run it installs the one Python library the converter needs
REM (lz4), so you do not have to run pip yourself.
REM
REM It looks for tsproj2fla.py beside this file, then in ..\dist\, then falls
REM back to an installed 'tsproj2fla' command.

setlocal
cd /d "%~dp0"

set "SCRIPT="
REM Accept tsproj2fla.py and near-misses (e.g. tsproj2fla.py.txt, which is what
REM you get when a browser saves the raw script instead of downloading it).
for %%F in ("%~dp0tsproj2fla*.py*") do if not defined SCRIPT if exist "%%~fF" set "SCRIPT=%%~fF"
for %%F in ("%~dp0..\dist\tsproj2fla*.py*") do if not defined SCRIPT if exist "%%~fF" set "SCRIPT=%%~fF"

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py"
if not defined PY where python3 >nul 2>nul && set "PY=python3"

if "%~1"=="" (
    echo Drag a .tsproj file onto this launcher, or run:
    echo     "%~nx0" MyProject.tsproj
    goto :end
)

if not defined SCRIPT (
    where tsproj2fla >nul 2>nul
    if errorlevel 1 (
        echo Could not find tsproj2fla.py next to this launcher, and the
        echo 'tsproj2fla' command is not installed.
        echo.
        echo Grab the drag-and-drop bundle from the repo's dist\ folder:
        echo     tsproj2fla-drag-and-drop.zip
        echo Unzip it and keep tsproj2fla.py in the same folder as this file.
        echo.
        echo If you downloaded the raw tsproj2fla.py and it saved as
        echo tsproj2fla.py.txt, rename it to tsproj2fla.py.
        goto :end
    )
    echo Ready.
    goto :run
)

if not defined PY (
    echo Python was not found. Install it from https://www.python.org/downloads/
    echo and be sure to tick "Add Python to PATH" during setup.
    goto :end
)

REM --- make sure the required library is present ------------------------
"%PY%" -c "import lz4" >nul 2>nul
if errorlevel 1 (
    echo Installing the required library ^(lz4^). This happens once...
    "%PY%" -m pip install --quiet --disable-pip-version-check lz4
    "%PY%" -c "import lz4" >nul 2>nul
    if errorlevel 1 (
        echo.
        echo Could not install lz4 automatically.
        echo Please run this command, then try again:
        echo.
        echo     "%PY%" -m pip install lz4
        echo.
        goto :end
    )
    echo Library installed.
    echo.
)

:run
set "STATUS=0"
:loop
if "%~1"=="" goto :done
echo ==^> Converting: %~1
"%PY%" "%SCRIPT%" "%~1"
if errorlevel 1 set "STATUS=1"
echo.
shift
goto :loop

:done
if "%STATUS%"=="1" echo One or more conversions failed ^(see the messages above^).

:end
echo.
pause
endlocal
