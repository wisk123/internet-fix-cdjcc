@echo off
cd /d "%~dp0"
echo Campus authentication test runs locally even without Internet.
echo Connect to your campus Wi-Fi, wait for the connection, then press a key.
echo If already online, no login will be attempted.
pause
python -B "%~dp0campus_recovery.py" once
pause