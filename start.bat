@echo off
cd /d "%~dp0"
echo Installing dependencies...
pip install -r requirements.txt -q
if %errorlevel% neq 0 (
    echo.
    echo pip failed. Trying pip3...
    pip3 install -r requirements.txt -q
)
echo.
echo   Open http://localhost:5055 in your browser
echo.
python app.py
if %errorlevel% neq 0 (
    python3 app.py
)
pause
