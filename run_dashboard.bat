@echo off
REM One-click launcher for the Tally Sync Dashboard on the host PC.
REM Binds to 0.0.0.0 so teammates on the LAN can open http://<this-pc-ip>:8501

cd /d "%~dp0"

echo.
echo ============================================================
echo  Tally Sync Dashboard
echo ============================================================
echo.
echo  Local URL:    http://localhost:8501
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4 Address"') do (
    echo  Network URL: http://%%a:8501
)
echo.
echo  Make sure Tally Prime is running with HTTP/XML enabled.
echo  Press Ctrl+C in this window to stop the dashboard.
echo ============================================================
echo.

streamlit run tools\sync_dashboard\app.py --server.address 0.0.0.0 --server.port 8501

pause
