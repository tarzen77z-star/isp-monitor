@echo off
echo ============================================
echo   ISP Monitor - EXE Builder
echo ============================================
echo.
echo [1/3] Installing required packages...
pip install pyinstaller speedtest-cli --quiet
if errorlevel 1 (
    echo ERROR: pip not found. Make sure Python is installed.
    pause & exit /b 1
)
echo [2/3] Building EXE (~30 seconds)...
pyinstaller --onefile --windowed --name "ISP_Monitor" --hidden-import speedtest isp_monitor.py
if errorlevel 1 (
    echo ERROR: Build failed. See output above.
    pause & exit /b 1
)
echo.
echo [3/3] Done!
echo ============================================
echo   dist\ISP_Monitor.exe  is ready to share!
echo   isp_log.csv saves next to the .exe
echo ============================================
pause
