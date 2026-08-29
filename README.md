# EPUB TTS Converter

Converts EPUB files to EPUB3 with synchronized text-to-speech audio. The output includes Media Overlays (SMIL) so each word is highlighted as it's read aloud in compatible EPUB3 Media Overlays readers. Can also export plain per-chapter MP3s, and convert `.txt` / `.md` files to a single MP3.

## Features

- Word-level highlight sync — each word is highlighted in real time as it's spoken
- Two TTS engines:
  - **Edge TTS** (online) — Microsoft's free neural voices, no API key required
  - **Kokoro** (local) — a [Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI) server, fully offline
- Adjustable speech rate (−50% to +50%)
- Paragraph and sentence pause controls
- Skips front matter (cover, title page, copyright, TOC…) — optional
- Skips back matter (notes, bibliography, index…) — optional
- Batch conversion with progress bar and ETA, pause/resume/cancel
- Drag-and-drop file list (when `tkinterdnd2` is installed)

## Download

Download the latest `EPUB_TTS_Converter.exe` from the [Releases](../../releases) page. No installation required — just run it.

## Usage

1. Run `EPUB_TTS_Converter.exe`
2. Add one or more `.epub` (or `.txt` / `.md`) files
3. Choose an engine and voice, adjust settings as needed
4. Click **Start Conversion**
5. Output is saved next to the original file (or in the chosen output directory)

## Settings

| Setting | Description |
|---------|-------------|
| Engine | Edge TTS (online) or Kokoro (local server URL) |
| Voice | Any voice offered by the selected engine |
| Rate | Speech speed, −50% to +50% |
| Workers | Parallel synthesis requests (Kokoro defaults to 1) |
| Quality | Edge MP3 bitrate (48 kbps compact or 96 kbps); Kokoro always outputs 128 kbps |
| Paragraph pause | Silence inserted between paragraphs (None / Short / Medium / Long) |
| Sentence pause | Silence inserted between sentences within a paragraph |
| Output | EPUB3 with Media Overlay, or per-chapter MP3 files |
| Save to | Output directory (blank = same folder as the input file) |
| Skip front matter | Skips cover, title page, copyright, TOC, dedication, etc. |
| Skip back matter | Skips notes, bibliography, index, appendix, etc. |

Text and Markdown files are always converted to a single MP3.

## Building from Source

Requirements: Python 3.9+

```bash
pip install -r requirements.txt
```

Run directly:
```bash
python epub_tts_converter.py
```

Build a standalone `.exe`:
```bash
build.bat
```

Run the tests:
```bash
pip install -r requirements-dev.txt
pytest
```

## Dependencies

- [websocket-client](https://github.com/websocket-client/websocket-client) — WebSocket connection to Edge TTS service
- [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/) + [lxml](https://lxml.de/) — HTML/XML parsing
- [tkinterdnd2](https://github.com/Eliav2/tkinterdnd2) — drag-and-drop support (optional at runtime, bundled into the exe)
