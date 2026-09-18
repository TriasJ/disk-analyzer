@echo off
REM ============================================================
REM  Disk Analyzer - double-click launcher
REM  * Double-click to analyze a folder you type in.
REM  * OR drag a folder onto this .bat file to analyze it.
REM  * OR from a terminal:  analyze.bat "C:\some\folder" --top 40
REM ============================================================
setlocal enabledelayedexpansion

if not "%~1"=="" (
    set "TARGET=%~1"
) else (
    echo.
    echo   Disk Analyzer
    echo   -------------
    echo   Tip: you can also DRAG a folder onto analyze.bat
    echo.
    set /p "TARGET=  Folder to analyze (leave blank for your Documents): "
)
if "!TARGET!"=="" set "TARGET=%USERPROFILE%\Documents"

echo.
echo   Analyzing: !TARGET!
echo.
python "%~dp0disk_analyzer.py" "!TARGET!"

echo.
echo   (The HTML report should have opened in your browser.)
pause
endlocal
