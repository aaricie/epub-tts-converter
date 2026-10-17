# EPUB TTS Converter

Converts EPUB files to EPUB3 with synchronized text-to-speech audio. The output includes Media Overlays (SMIL) so each word is highlighted as it's read aloud in compatible EPUB3 Media Overlays readers. Can also export plain per-chapter MP3s, and convert `.txt` / `.md` files to a single MP3.

## Features

- Word highlighting where the engine supplies usable timestamps, with continuous chapter playback
- Two TTS engines:
  - **Edge TTS** (online) — Microsoft's free neural voices, no API key required
  - **Kokoro** (local) — a [Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI) server, fully offline
- Adjustable speech rate (−50% to +50%)
- Paragraph and sentence pause controls
- Skips front matter (cover, title page, copyright, TOC…) — optional
- Skips back matter (notes, bibliography, index…) — optional
- Batch conversion with progress bar and ETA, pause/resume/cancel
- Drag-and-drop file list (when `tkinterdnd2` is installed)
- Disk checkpoints let cancelled or interrupted jobs reuse completed requests and chapters
- Kokoro PCM is encoded once per chapter, with 48 kbps compact output
- Stop on synthesis errors by default, or explicitly continue with a saved warnings report

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
| Quality | Final MP3 bitrate; compact 48 kbps is available for both engines |
| Paragraph pause | Silence inserted between paragraphs (None / Short / Medium / Long) |
| Sentence pause | Silence inserted between sentences within a paragraph |
| Output | EPUB3 with Media Overlay, or per-chapter MP3 files |
| Save to | Output directory (blank = same folder as the input file) |
| Skip front matter | Skips cover, title page, copyright, TOC, dedication, etc. |
| Skip back matter | Skips notes, bibliography, index, appendix, etc. |
| Batch short Kokoro paragraphs | Off by default (experimental). Sends up to four adjacent short paragraphs (English voices) as one request and splits the audio back per paragraph; anything that can't be split exactly is synthesized paragraph by paragraph instead. Measured at most ~3% faster on dialogue-heavy fiction and no faster on history, because requests are already prefetched. |
| Continue after failed passages | Off by default. When enabled, omit passages that still fail after retries and save a warnings report. Invalid configuration and unsupported timestamp responses still stop conversion. |
| Keep checkpoints after success | Off by default in the GUI. Enable to retain completed audio for later re-runs. Failed, partial and cancelled jobs retain checkpoints regardless. |

Text and Markdown files are always converted to a single MP3.

## Kokoro audio and compatibility

Kokoro conversion requests 24 kHz mono signed 16-bit PCM. The client inserts pauses in samples and encodes each completed chapter once to MP3. It checks that the MP3 decodes to exactly the source sample count before publishing it; the SMIL timeline includes the pauses and audio tails. A chapter that fails this check is not published. The encoder supports 48, 64, 96 and 128 kbps; the GUI offers the quality presets shown in its menu.

At 48 kbps, audio uses approximately 21.6 MB per hour, compared with 57.6 MB per hour at 128 kbps. Lower bitrate primarily saves space. The default encoder effort is unchanged; faster encoder settings remain an audition/benchmark option in the Python API rather than an automatic quality change.

Tested with Kokoro-FastAPI 0.2.4 (June 2025) and **0.9.0 (September 2026, recommended)**: `docker run -d --name kokoro-fastapi --gpus all -p 8880:8880 ghcr.io/remsky/kokoro-fastapi-gpu:v0.9.0`. Its default CUDA 12.6 build still runs on Pascal cards such as the GTX 1060 (the `cu128` variant targets RTX 50-series cards). Both provide usable English word timestamps, with small timing imperfections that the client checks and bounds, but 0.2.x loses the timestamps of a whole internal chunk whenever one token has no timing: in a measured history excerpt about one word in nine got no highlight on 0.2.4, against essentially none on 0.9.0. 0.9.0 also returns French word timestamps (0.2.x does not, so use Edge for synchronized French books there). Plain MP3 export does not require word timestamps. Numbers, abbreviations and other spoken normalization can still make highlighting less precise than ordinary words; model timestamps are estimates, not forced alignment.

Kokoro-FastAPI (every version through 0.9.0) logs an internal chunk failure and still returns the remaining audio as a success. The converter therefore compares each response's word timestamps with the text it sent: if eight or more consecutive words are missing, the request is retried, and a persistent omission is handled by the **Continue after failed passages** setting instead of silently shortening the book. Normal rewrites ("1945" spoken as "nineteen forty-five") and scripts an English voice cannot pronounce are not counted as omissions. This check works with the stock server; the optional [server patch](kokoro_server_patch/README.md) adds the same guarantee on the server side for the June 2025 image only.

## Speed

With one Kokoro worker, the next request is already queued while the converter encodes and maps the previous paragraph, so the GPU does not wait for MP3 encoding. On the tested GTX 1060 this made real-book excerpts 14–20% faster; conversion time is now essentially Kokoro's own synthesis time, so faster MP3 encoder settings would gain nothing and the default encoder quality is kept. The optional short-paragraph batching splits each batch using the word timestamps: each cut falls in the quietest part of the pause between two paragraphs, rewritten words inside a paragraph are allowed, and a paragraph whose opening words are likely to be rewritten (numbers, abbreviations such as "Dr.", acronyms) starts a new batch so that a batch is rarely synthesized only to be discarded.

## Resume, disk space and failures

Run the same input again with the same settings to resume. The cache is `.epub_tts_cache` inside the selected output directory (beside the chapter folder for MP3 export). It uses the input content, provider/endpoint/revision, voice, speed, format and relevant processing settings, so a new automatically numbered output filename does not start synthesis over. Cache records include hashes and lengths; damaged records are ignored and regenerated.

Successful requests are checkpointed to disk. Once a complete chapter MP3 and its metadata are committed, its intermediate PCM is removed. Cancelled or failed chapters retain successful request audio for retry. During conversion, allow space for the output, compressed checkpoints and PCM for unfinished chapters; PCM requires about 172.8 MB per hour. The GUI removes the successful run's checkpoints after publishing its output unless **Keep checkpoints after success** is enabled. Partial, failed and cancelled runs always retain checkpoints. Python API calls retain checkpoints by default; pass `backend['keep_checkpoints'] = False` to select successful-run cleanup.

When no conversion is running, deleting `.epub_tts_cache` reclaims retained space and makes later runs synthesize again. Checkpoints are kept per Kokoro-FastAPI version (read from the server), so upgrading the server does not reuse audio from the old one. If you manually change model weights, voice files or server configuration without changing its version, clear the affected cache before resuming.

Audio is streamed to staged files. Memory depends on the number of workers and the largest outstanding paragraphs, plus the parsed book text and metadata, rather than the full audiobook. Very large individual paragraphs can still require substantial memory. One Kokoro worker is the recommended starting point for the tested GTX 1060; more workers did not improve the measured throughput.

EPUB and single-MP3 outputs replace their destination only after successful assembly. Chapter MP3 exports publish each completed chapter separately. Cancellation and failures leave existing destination files intact and retain checkpoints. An HTTP request already in flight may finish after cancellation and save its result; new requests and retries stop.

With **Continue after failed passages** enabled, the final log says **Completed WITH WARNINGS**. Missing passages are listed beside an EPUB/single MP3 as `<output filename>.warnings.txt`, or in `warnings.txt` inside a chapter MP3 folder. Partially narrated chapters are never treated as completed checkpoints; a later run retries their missing passages while reusing successful requests. If every text passage fails, no empty MP3 is published.

Existing narration created by this converter is replaced when safely identifiable. Other author-provided media overlays are rejected before synthesis; use the original un-narrated book in that case. DRM-protected books are not supported.

Every chapter is checked as well-formed XHTML before any synthesis starts. Common OCR debris is repaired automatically: an unescaped `<` in scanned text makes readers treat the following words as invisible attribute names, which XML cannot hold, so those attribute names are dropped (the visible and narrated text is unchanged). A chapter that still isn't well-formed stops the conversion with its file name.

## Building from Source

Requirements: Python 3.10+ (tested with Python 3.12 on Windows)

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
- [PyAV](https://pyav.org/) 17.0.1 — Kokoro PCM/MP3 encoding and sample-count verification; its Windows wheel includes FFmpeg libraries, so no separate `ffmpeg` executable is needed. Edge-only synthesis does not import PyAV.
