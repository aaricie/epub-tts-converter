@echo off
echo Installing PyInstaller...
pip install pyinstaller

echo.
echo Building EPUB TTS Converter...

:: If ffmpeg.exe is present, bundle it for Apple Books M4A support
if exist ffmpeg.exe (
    echo Found ffmpeg.exe — bundling for Apple Books M4A support.
    pyinstaller --onefile --windowed --name "EPUB_TTS_Converter" ^
        --hidden-import=lxml --hidden-import=lxml.etree ^
        --add-binary "ffmpeg.exe;." ^
        epub_tts_converter.py
) else (
    echo ffmpeg.exe not found — Apple Books M4A mode will require ffmpeg on PATH.
    pyinstaller --onefile --windowed --name "EPUB_TTS_Converter" ^
        --hidden-import=lxml --hidden-import=lxml.etree ^
        epub_tts_converter.py
)

echo.
echo Done! Output: dist\EPUB_TTS_Converter.exe
pause
