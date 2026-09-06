@echo off
setlocal

cd /d "%~dp0"

echo === HP Battle Chess -- Windows x64 build ===
echo.

REM --- 1. Python 3.13 x64 check ---------------------------------------
py -3.13 -c "import struct,sys; print('Python', sys.version); assert struct.calcsize('P')*8==64, 'Not 64-bit Python'" || goto :error
echo.

REM --- 2. Virtual env (keeps the build clean/reproducible) ------------
if not exist ".buildvenv" (
    echo Creating build venv...
    py -3.13 -m venv .buildvenv || goto :error
)
call .buildvenv\Scripts\activate.bat || goto :error

echo Installing/upgrading pip, PyInstaller, and game requirements...
python -m pip install --upgrade pip >nul || goto :error
python -m pip install --upgrade pyinstaller || goto :error
python -m pip install --upgrade -r requirements.txt || goto :error
echo.

REM --- 3. Clean previous build -----------------------------------------
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"

REM --- 4. Build ----------------------------------------------------------
echo Building with PyInstaller (--onedir --windowed)...
pyinstaller --noconfirm hp_chess.spec || goto :error
echo.

echo === Build finished ===
echo Executable: dist\HP Battle Chess\HP Battle Chess.exe
echo.
echo Next steps (see WINDOWS_BUILD.md for the full checklist):
echo   1. Run the exe from dist\HP Battle Chess\ and confirm it starts.
echo   2. Start a new game, open Settings, pick music/background.
echo   3. Test LAN host+join (allow it through Windows Firewall when prompted).
echo   4. If Ollama is installed, confirm the Local AI option detects it.
echo.
goto :eof

:error
echo.
echo BUILD FAILED. See the error above.
exit /b 1
