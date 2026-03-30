# EPUB TTS Converter

Converts EPUB files to EPUB3 with synchronized text-to-speech audio using Microsoft Edge TTS. The output includes Media Overlays (SMIL) so each word is highlighted as it's read aloud — compatible with Apple Books, Thorium Reader, and other EPUB3 Media Overlays readers.

## Features

- Word-level highlight sync — each word is highlighted in real time as it's spoken
- Uses Edge TTS voices (free, no API key required)
- Supports all Edge TTS voices and languages
- Adjustable speech rate, volume, and pitch
- Paragraph and sentence pause controls
- Skips back matter (notes, bibliography, index, etc.) — optional
- Apple Books mode: M4A audio at 256 kbps AAC with flat SMIL structure
- ffmpeg auto-downloads on first use when Apple Books mode is selected (no manual install needed)

## Download

Download the latest `EPUB_TTS_Converter.exe` from the [Releases](../../releases) page. No installation required — just run it.

## Usage

1. Run `EPUB_TTS_Converter.exe`
2. Select an EPUB file
3. Choose a voice, adjust settings as needed
4. Click **Convert**
5. The output EPUB3 file is saved next to the original

## Settings

| Setting | Description |
|---------|-------------|
| Voice | Any Edge TTS voice (e.g. `en-US-AriaNeural`) |
| Rate / Volume / Pitch | Prosody controls for the TTS voice |
| Paragraph pause | Silence inserted between paragraphs (None / Short / Medium / Long) |
| Sentence pause | Silence inserted between sentences within a paragraph |
| Skip back matter | Skips notes, bibliography, index, appendix, etc. |
| Target | **Standard EPUB3** (MP3) or **Apple Books** (M4A, requires ffmpeg) |

## Apple Books Mode

Apple Books requires M4A audio. When you select **Apple Books** as the target, the converter will:

1. Look for `ffmpeg` on your system PATH or next to the `.exe`
2. If not found, automatically download a lightweight build to `%APPDATA%\EPUB_TTS_Converter\ffmpeg.exe` (one-time, ~3 MB)
3. Convert all audio to M4A (256 kbps AAC) and generate a flat SMIL structure

## Building from Source

Requirements: Python 3.9+

```bash
pip install edge-tts beautifulsoup4 lxml mutagen tkinter
```

Run directly:
```bash
python epub_tts_converter.py
```

Build a standalone `.exe`:
```bash
build.bat
```

> Place `ffmpeg.exe` next to `epub_tts_converter.py` before running `build.bat` to bundle it inside the `.exe` (optional — it will auto-download otherwise).

## Dependencies

- [edge-tts](https://github.com/rany2/edge-tts) — free Microsoft Edge TTS
- [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/) + [lxml](https://lxml.de/) — HTML/XML parsing
- [mutagen](https://mutagen.readthedocs.io/) — MP3 duration metadata
- ffmpeg — required for Apple Books M4A mode (auto-downloaded if needed)
