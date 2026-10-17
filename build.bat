@echo off
echo Installing dependencies...
pip install -r requirements.txt pyinstaller

echo.
echo Building EPUB TTS Converter...

set HIDDEN=--hidden-import=lxml --hidden-import=lxml.etree ^
    --hidden-import=websocket --hidden-import=websocket._abnf ^
    --hidden-import=websocket._core --hidden-import=websocket._exceptions ^
    --hidden-import=websocket._handshake --hidden-import=websocket._http ^
    --hidden-import=websocket._logging --hidden-import=websocket._socket ^
    --hidden-import=websocket._ssl_compat --hidden-import=websocket._utils

rem --collect-all tkinterdnd2 bundles the native tkdnd binaries; without it
rem the built exe silently loses drag-and-drop.
pyinstaller --onefile --windowed --name "EPUB_TTS_Converter" ^
    %HIDDEN% ^
    --collect-all tkinterdnd2 --collect-all av ^
    epub_tts_converter.py

echo.
echo Done! Output: dist\EPUB_TTS_Converter.exe
pause
