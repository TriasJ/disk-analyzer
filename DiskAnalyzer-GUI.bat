@echo off
REM ============================================================
REM  Disk Analyzer - GUI launcher
REM  Double-click to open the graphical interface.
REM  You can also drag a folder onto this file to pre-fill it.
REM ============================================================
start "" pythonw "%~dp0disk_gui.py" %*
