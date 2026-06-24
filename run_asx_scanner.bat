@echo off
REM ===================================================================
REM ASX 周报扫描器启动器 (供 Windows 计划任务调用)
REM 每次运行的终端输出追加写入 asx_scanner_run.log，便于排查
REM ===================================================================
set "PYEXE=C:\Users\ROG\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\python.exe"
set "SCRIPT=C:\Users\ROG\Documents\Codex\2026-06-24\an-2\asx_scanner.py"
set "LOG=C:\Users\ROG\Documents\Codex\2026-06-24\an-2\asx_scanner_run.log"

echo. >> "%LOG%"
echo ======== Run started: %DATE% %TIME% ======== >> "%LOG%"
"%PYEXE%" "%SCRIPT%" >> "%LOG%" 2>&1
echo ======== Run finished: %DATE% %TIME% (exit %ERRORLEVEL%) ======== >> "%LOG%"
