"""
EPUB / Text to Audio Converter
===============================
Converts DRM-free EPUBs into EPUB3 with Media Overlays (synchronized TTS
narration), or exports audio as MP3 files.  Also supports plain text and
Markdown files (MP3 output only).

Uses Microsoft Edge's free neural TTS service — no API key required.

Requirements:
    pip install websocket-client beautifulsoup4 lxml
"""

# ── Standard library ──────────────────────────────────────────────────────────
import hashlib
import json
import os
import posixpath
import queue
import re
import secrets
import shutil
import ssl
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
import uuid
import zipfile
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from io import BytesIO
from tkinter import filedialog, ttk
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

# ── Third-party ───────────────────────────────────────────────────────────────
try:
    import websocket
except ImportError:
    raise ImportError(
        "websocket-client is required.\n"
        "Install:  pip install websocket-client"
    )

try:
    from bs4 import BeautifulSoup
except ImportError:
    raise ImportError(
        "beautifulsoup4 and lxml are required.\n"
        "Install:  pip install beautifulsoup4 lxml"
    )


# ═════════════════════════════════════════════════════════════════════════════
#  Section 1 — Edge TTS constants
# ═════════════════════════════════════════════════════════════════════════════

BASE_URL = "speech.platform.bing.com/consumer/speech/synthesize/readaloud"
TRUSTED_CLIENT_TOKEN = "6A5AA1D4EAFF4E9FB37E23D68491D6F4"
WSS_URL = f"wss://{BASE_URL}/edge/v1?TrustedClientToken={TRUSTED_CLIENT_TOKEN}"

CHROMIUM_FULL_VERSION = "143.0.3650.75"
CHROMIUM_MAJOR = CHROMIUM_FULL_VERSION.split(".")[0]
SEC_MS_GEC_VERSION = f"1-{CHROMIUM_FULL_VERSION}"

USER_AGENT = (
    f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    f" (KHTML, like Gecko) Chrome/{CHROMIUM_MAJOR}.0.0.0 Safari/537.36"
    f" Edg/{CHROMIUM_MAJOR}.0.0.0"
)

WIN_EPOCH = 11644473600
S_TO_NS = 1e9

VOICE_LIST_URL = (
    f"https://{BASE_URL}/voices/list"
    f"?trustedclienttoken={TRUSTED_CLIENT_TOKEN}"
)

# Fallback voices used when the network fetch fails
FALLBACK_VOICES = OrderedDict([
    ("en-US | Ava Multilingual (Female)",   "en-US-AvaMultilingualNeural"),
    ("en-US | Emma Multilingual (Female)",  "en-US-EmmaMultilingualNeural"),
    ("en-US | Andrew Multilingual (Male)",  "en-US-AndrewMultilingualNeural"),
    ("en-US | Aria (Female)",               "en-US-AriaNeural"),
    ("en-US | Guy (Male)",                  "en-US-GuyNeural"),
    ("en-GB | Sonia (Female)",              "en-GB-SoniaNeural"),
    ("en-GB | Ryan (Male)",                 "en-GB-RyanNeural"),
    ("fr-FR | Denise (Female)",             "fr-FR-DeniseNeural"),
    ("fr-FR | Henri (Male)",                "fr-FR-HenriNeural"),
    ("fr-CA | Sylvie (Female)",             "fr-CA-SylvieNeural"),
    ("es-ES | Elvira (Female)",             "es-ES-ElviraNeural"),
    ("de-DE | Katja (Female)",              "de-DE-KatjaNeural"),
])


def _build_voice_menu(voices_json):
    """Build OrderedDict  'locale | Name (Gender)' -> ShortName  from Microsoft JSON."""
    items = []
    for v in voices_json:
        short = v.get("ShortName", "")
        locale = v.get("Locale", "")
        gender = v.get("Gender", "")
        parts = short.split("-", 2)
        if len(parts) >= 3:
            raw_name = parts[2]
            friendly = raw_name.replace("Neural", "").strip()
            friendly = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', friendly)
        else:
            friendly = short
        label = f"{locale} | {friendly} ({gender})"
        items.append((label, short, locale))

    items.sort(key=lambda x: (x[2], x[0]))
    menu = OrderedDict()
    for label, short, _ in items:
        menu[label] = short
    return menu


def fetch_voices():
    """Fetch the full voice list from Microsoft. Returns OrderedDict or None."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "identity",
        "Accept-Language": "en-US,en;q=0.9",
        "Authority": "speech.platform.bing.com",
        "Sec-CH-UA": (
            f'" Not;A Brand";v="99", "Microsoft Edge"'
            f';v="{CHROMIUM_MAJOR}", "Chromium"'
            f';v="{CHROMIUM_MAJOR}"'
        ),
        "Sec-CH-UA-Mobile": "?0",
        "Accept": "*/*",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    req = Request(VOICE_LIST_URL, headers=headers)
    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, list) and len(data) > 10:
            return _build_voice_menu(data)
    except Exception:
        pass
    return None


# ═════════════════════════════════════════════════════════════════════════════
#  Section 2 — Edge TTS token generation
# ═════════════════════════════════════════════════════════════════════════════

_clock_skew = 0.0


def _get_unix_timestamp():
    return datetime.now(timezone.utc).timestamp() + _clock_skew


def generate_sec_ms_gec():
    ticks = _get_unix_timestamp() + WIN_EPOCH
    ticks -= ticks % 300
    ticks *= S_TO_NS / 100
    return hashlib.sha256(
        f"{ticks:.0f}{TRUSTED_CLIENT_TOKEN}".encode("ascii")
    ).hexdigest().upper()


def generate_muid():
    return secrets.token_hex(16).upper()


def connect_id():
    return uuid.uuid4().hex


def date_to_string():
    return time.strftime(
        "%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)",
        time.gmtime(),
    )


# ═════════════════════════════════════════════════════════════════════════════
#  Section 3 — SSML utilities
# ═════════════════════════════════════════════════════════════════════════════

def normalise_voice(short_name):
    m = re.match(r"^([a-z]{2,})-([A-Z]{2,})-(.+Neural)$", short_name)
    if m:
        lang, region, name = m.group(1), m.group(2), m.group(3)
        if "-" in name:
            region = f"{region}-{name[:name.index('-')]}"
            name = name[name.index('-') + 1:]
        return (
            "Microsoft Server Speech Text to Speech Voice"
            f" ({lang}-{region}, {name})"
        )
    return short_name


def make_ssml(voice, text, rate="+0%", volume="+0%", pitch="+0Hz"):
    v = normalise_voice(voice)
    return (
        "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis'"
        " xml:lang='en-US'>"
        f"<voice name='{v}'>"
        f"<prosody pitch='{pitch}' rate='{rate}' volume='{volume}'>"
        f"{escape(text)}"
        "</prosody></voice></speak>"
    )


def ssml_request(ssml):
    rid = connect_id()
    ts = date_to_string()
    return (
        f"X-RequestId:{rid}\r\n"
        "Content-Type:application/ssml+xml\r\n"
        f"X-Timestamp:{ts}Z\r\n"
        "Path:ssml\r\n\r\n"
        f"{ssml}"
    )


def config_request(audio_format="audio-24khz-48kbitrate-mono-mp3",
                    word_boundary=False):
    ts = date_to_string()
    wb = "true" if word_boundary else "false"
    return (
        f"X-Timestamp:{ts}\r\n"
        "Content-Type:application/json; charset=utf-8\r\n"
        "Path:speech.config\r\n\r\n"
        '{"context":{"synthesis":{"audio":{"metadataoptions":{'
        f'"sentenceBoundaryEnabled":"true","wordBoundaryEnabled":"{wb}"'
        '},'
        f'"outputFormat":"{audio_format}"'
        '}}}}\r\n'
    )


# Audio format presets: name -> (format_string, bytes_per_ms, bitrate_kbps)
AUDIO_FORMATS = {
    "Compact (48 kbps)":        ("audio-24khz-48kbitrate-mono-mp3", 6.0,  48),
    "Higher quality (96 kbps)": ("audio-24khz-96kbitrate-mono-mp3", 12.0, 96),
}

# Viewport sizes for fixed-layout Apple Books
VIEWPORTS = {
    "iPad":   (768, 1024),
    "iPhone": (375, 667),
}

# Target reader presets: name -> (audio_format_key, apple_books, viewport_key|None)
TARGET_READERS = OrderedDict([
    ("Standard EPUB3 — MP3 48 kbps",           ("Compact (48 kbps)",        False, None)),
    ("Standard EPUB3 — MP3 96 kbps",           ("Higher quality (96 kbps)", False, None)),
    ("Apple Books iPad — M4A (requires ffmpeg)",  ("Higher quality (96 kbps)", True,  "iPad")),
    ("Apple Books iPhone — M4A (requires ffmpeg)", ("Higher quality (96 kbps)", True,  "iPhone")),
])



# ═════════════════════════════════════════════════════════════════════════════
#  Section 4 — Text sanitizer & splitter
# ═════════════════════════════════════════════════════════════════════════════

def _sanitize_for_ssml(text):
    text = re.sub(r'[\u200b\u200c\u200d\u200e\u200f\ufeff\u00ad\u2060]', '', text)
    text = text.replace('\u2018', "'").replace('\u2019', "'")
    text = text.replace('\u201c', '"').replace('\u201d', '"')
    text = text.replace('\u2014', ' - ').replace('\u2013', ' - ')
    text = text.replace('\u2026', '...')
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', text)
    text = re.sub(r'  +', ' ', text)
    return text


SILENCE_SECTION_MS = 850
SILENCE_PARA_MS = 450

# Pause between paragraphs (ms of silence inserted after each paragraph's audio)
PAUSE_PRESETS = OrderedDict([
    ("None",   0),
    ("Short",  350),
    ("Medium", 600),
    ("Long",   1000),
])

# Sentence pause: ms of silence between sentences within a paragraph
# "Default" means send the whole paragraph to Edge TTS as one request
SENTENCE_PAUSE_PRESETS = OrderedDict([
    ("Default", 0),
    ("Short",   200),
    ("Medium",  400),
    ("Long",    700),
])
_SILENCE = "__silence__"


def _is_header(line):
    s = line.strip()
    if not s:
        return False
    if re.match(r'^#{1,4}\s', s):
        return True
    alpha = re.sub(r'[^a-zA-Z]', '', s)
    return len(alpha) >= 3 and alpha == alpha.upper() and len(s) < 120


def _clean_header(line):
    return re.sub(r'^#{1,4}\s+', '', line.strip())


def _split_long_body(text, max_chars):
    sentences = re.split(r'(?<=[.!?])\s+', text)
    parts = []
    current = ""
    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue
        if not current:
            current = sent
        elif len(current) + 1 + len(sent) <= max_chars:
            current += " " + sent
        else:
            parts.append(current)
            current = sent
    if current:
        while len(current) > max_chars:
            split_at = current.rfind(' ', 0, max_chars)
            if split_at <= 0:
                split_at = max_chars
            parts.append(current[:split_at].strip())
            current = current[split_at:].strip()
        if current:
            parts.append(current)
    return parts


def generate_mp3_silence(duration_ms, bitrate_kbps=48):
    """Generate silent MP3 data matching Edge TTS output format.

    Format: MPEG-2 Layer III, 24 kHz, mono.
    Frame size = 72 * bitrate / samplerate (MPEG-2, not MPEG-1's 144).
    This ensures bytes_to_ms(len(silence), bytes_per_ms) == actual duration.
    """
    # MPEG-2 Layer III headers for supported bitrates at 24 kHz mono
    _HEADERS = {
        48: b'\xff\xf3\x64\xc0',  # 48 kbps
        96: b'\xff\xf3\x84\xc0',  # 96 kbps
    }
    header = _HEADERS.get(bitrate_kbps, _HEADERS[48])
    frame_size = (72 * bitrate_kbps * 1000) // 24000
    frame = header + b'\x00' * (frame_size - len(header))
    # Each frame = 576 samples at 24 kHz = 24 ms
    ms_per_frame = 24.0
    num_frames = max(1, round(duration_ms / ms_per_frame))
    return frame * num_frames


def split_text_for_tts(text, max_chars=1500):
    if not text or not text.strip():
        return []

    text = _sanitize_for_ssml(text)
    text = text.replace('\r\n', '\n').replace('\r', '\n')

    segments = []
    for block in re.split(r'\n\s*\n', text):
        block = block.strip()
        if not block:
            continue
        for line in block.split('\n'):
            line = line.strip()
            if not line:
                continue
            if _is_header(line):
                segments.append(('header', _clean_header(line)))
            else:
                segments.append(('body', re.sub(r'\s+', ' ', line)))

    if not segments:
        return []

    items = []
    current_parts = []
    current_len = 0

    def _flush():
        nonlocal current_parts, current_len
        if current_parts:
            items.append(' '.join(current_parts))
            current_parts = []
            current_len = 0

    for stype, stext in segments:
        if stype == 'body' and len(stext) > max_chars:
            _flush()
            for sub in _split_long_body(stext, max_chars):
                items.append(sub)
            continue

        if stype == 'header':
            _flush()
            if items:
                items.append((_SILENCE, SILENCE_SECTION_MS))
            header_text = stext.rstrip('.') + '.'
            items.append(header_text)
            items.append((_SILENCE, SILENCE_PARA_MS))
            continue

        if current_len + len(stext) > max_chars and current_parts:
            _flush()
            items.append((_SILENCE, SILENCE_PARA_MS))

        current_parts.append(stext)
        current_len += len(stext)

    _flush()

    return [it for it in items
            if isinstance(it, tuple) or (isinstance(it, str) and it.strip())]


# ═════════════════════════════════════════════════════════════════════════════
#  Section 5 — WebSocket synthesis
# ═════════════════════════════════════════════════════════════════════════════

def synthesize_chunk(text, voice, rate="+0%", volume="+0%", pitch="+0Hz",
                     retries=4, audio_format="audio-24khz-48kbitrate-mono-mp3",
                     word_boundary=False):
    """Synthesize a single text chunk.

    Returns:
        If word_boundary is False: raw MP3 bytes
        If word_boundary is True:  (mp3_bytes, boundaries) where boundaries
            is a list of {"type": "word"|"sentence", "offset": ms, "duration": ms, "text": str}
    """
    last_error = None
    for attempt in range(retries + 1):
        try:
            return _synthesize_once(text, voice, rate, volume, pitch,
                                    audio_format, word_boundary)
        except Exception as e:
            last_error = e
            err_str = str(e)
            if "403" in err_str and attempt < retries:
                global _clock_skew
                _clock_skew += 300
            if attempt < retries:
                # Back off more on repeated failures
                time.sleep(1 + attempt * 2)
    raise last_error


def _synthesize_once(text, voice, rate, volume, pitch,
                     audio_format, word_boundary):
    import json as _json

    url = (
        f"{WSS_URL}"
        f"&ConnectionId={connect_id()}"
        f"&Sec-MS-GEC={generate_sec_ms_gec()}"
        f"&Sec-MS-GEC-Version={SEC_MS_GEC_VERSION}"
    )
    headers = {
        "Pragma": "no-cache",
        "Cache-Control": "no-cache",
        "Origin": "chrome-extension://jdiccldimpdaibmpdkjnbmckianbfold",
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": f"muid={generate_muid()};",
    }

    audio_data = BytesIO()
    boundaries = []
    done = threading.Event()
    error_box = [None]

    def on_open(ws):
        ws.send(config_request(audio_format, word_boundary))
        ws.send(ssml_request(make_ssml(voice, text, rate, volume, pitch)))

    def on_message(ws, message):
        if isinstance(message, bytes):
            if len(message) < 2:
                return
            hlen = struct.unpack(">H", message[:2])[0]
            body = message[2 + hlen:]
            if body:
                audio_data.write(body)
        else:
            if "Path:turn.end" in message:
                done.set()
                ws.close()
            elif word_boundary and "Path:audio.metadata" in message:
                # Parse word/sentence boundary metadata
                try:
                    json_start = message.index("{")
                    meta = _json.loads(message[json_start:])
                    md = meta.get("Metadata", [])
                    for entry in md:
                        mtype = entry.get("Type", "")
                        data = entry.get("Data", {})
                        # Offset is in 100-nanosecond ticks
                        offset_ticks = data.get("Offset", 0)
                        offset_ms = offset_ticks / 10000.0
                        dur_ticks = data.get("Duration", 0)
                        dur_ms = dur_ticks / 10000.0
                        text_val = data.get("text", {})
                        if isinstance(text_val, dict):
                            text_val = text_val.get("Text", "")
                        boundaries.append({
                            "type": mtype.lower(),  # "word" or "sentence"
                            "offset": offset_ms,
                            "duration": dur_ms,
                            "text": text_val,
                        })
                except Exception:
                    pass

    def on_error(ws, error):
        error_box[0] = error
        done.set()

    def on_close(ws, *_):
        done.set()

    ws = websocket.WebSocketApp(
        url, header=headers,
        on_open=on_open, on_message=on_message,
        on_error=on_error, on_close=on_close,
    )
    wst = threading.Thread(
        target=ws.run_forever,
        kwargs={"sslopt": {"context": ssl.create_default_context()}},
        daemon=True,
    )
    wst.start()
    done.wait(timeout=60)

    if error_box[0]:
        raise ConnectionError(f"Edge TTS: {error_box[0]}")
    result = audio_data.getvalue()
    if not result:
        raise RuntimeError("No audio received")
    if word_boundary:
        return result, boundaries
    return result


# ═════════════════════════════════════════════════════════════════════════════
#  Section 5b — M4A conversion (Apple Books)
# ═════════════════════════════════════════════════════════════════════════════

def _get_ffmpeg_path():
    """Find ffmpeg executable: AppData cache, next to script/exe, then PATH."""
    # 1. Persistent AppData cache (downloaded on first use)
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        cached = os.path.join(appdata, "EPUB_TTS_Converter", "ffmpeg.exe")
        if os.path.isfile(cached):
            return cached

    # 2. Next to the running script or PyInstaller bundle
    if getattr(sys, 'frozen', False):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))

    local = os.path.join(base, "ffmpeg.exe")
    if os.path.isfile(local):
        return local

    # 3. System PATH
    found = shutil.which("ffmpeg")
    if found:
        return found

    return None


def _download_ffmpeg(progress_cb=None):
    """Download ffmpeg.exe from BtbN GitHub releases into AppData cache.

    Returns the path to the downloaded ffmpeg.exe, or None on failure.
    """
    import urllib.request
    import zipfile
    import tempfile

    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return None

    dest_dir = os.path.join(appdata, "EPUB_TTS_Converter")
    dest_exe = os.path.join(dest_dir, "ffmpeg.exe")
    os.makedirs(dest_dir, exist_ok=True)

    url = (
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
        "ffmpeg-master-latest-win64-lgpl.zip"
    )

    if progress_cb:
        progress_cb("Downloading ffmpeg (one-time setup, ~85 MB)...")

    try:
        tmp_zip = os.path.join(dest_dir, "_ffmpeg_download.zip")
        downloaded = [0]

        def _report(block_num, block_size, total_size):
            if total_size > 0:
                downloaded[0] += block_size
                pct = min(100, int(downloaded[0] * 100 / total_size))
                if progress_cb and pct % 10 == 0:
                    progress_cb(f"  Downloading ffmpeg... {pct}%")

        urllib.request.urlretrieve(url, tmp_zip, _report)

        if progress_cb:
            progress_cb("  Extracting ffmpeg.exe...")

        with zipfile.ZipFile(tmp_zip, "r") as zf:
            names = [n for n in zf.namelist() if n.endswith("/ffmpeg.exe")]
            if not names:
                return None
            data = zf.read(names[0])
            with open(dest_exe, "wb") as f:
                f.write(data)

        os.remove(tmp_zip)

        if progress_cb:
            progress_cb("  ffmpeg downloaded successfully.")

        return dest_exe

    except Exception as e:
        if progress_cb:
            progress_cb(f"  ffmpeg download failed: {e}")
        return None


def mp3_to_m4a(mp3_bytes: bytes) -> bytes:
    """Convert MP3 bytes to M4A (AAC) bytes via ffmpeg.

    Apple Books requires M4A audio at 256 kbps AAC.
    The M4A (ipod) muxer requires seekable output, so we use temp files.
    """
    import tempfile

    ffmpeg = _get_ffmpeg_path()
    if not ffmpeg:
        # Try downloading (silent — convert_epub_to_epub3 already notified user)
        ffmpeg = _download_ffmpeg()
    if not ffmpeg:
        raise RuntimeError(
            "ffmpeg not found. Install ffmpeg and add it to PATH."
        )

    tmp_in = None
    tmp_out = None
    try:
        tmp_in = tempfile.NamedTemporaryFile(
            suffix=".mp3", delete=False,
        )
        tmp_in.write(mp3_bytes)
        tmp_in.close()

        tmp_out = tempfile.NamedTemporaryFile(
            suffix=".m4a", delete=False,
        )
        tmp_out.close()

        cmd = [
            ffmpeg,
            '-hide_banner', '-loglevel', 'error',
            '-y',
            '-i', tmp_in.name,
            '-acodec', 'aac',
            '-ar', '24000',
            '-ac', '1',
            '-b:a', '256k',
            '-movflags', '+faststart',
            tmp_out.name,
        ]
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Generous timeout — ffmpeg is fast but may share CPU with
        # parallel workers.  10 s per MB, minimum 120 s.
        size_mb = max(1, len(mp3_bytes) / (1024 * 1024))
        timeout_s = max(120, int(size_mb * 10) + 120)
        _, err = proc.communicate(timeout=timeout_s)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg conversion failed: {err.decode()}")

        with open(tmp_out.name, "rb") as f:
            return f.read()
    finally:
        for tmp in (tmp_in, tmp_out):
            if tmp:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass


# ═════════════════════════════════════════════════════════════════════════════
#  Section 6 — EPUB processing
# ═════════════════════════════════════════════════════════════════════════════

OPF_NS  = "http://www.idpf.org/2007/opf"
DC_NS   = "http://purl.org/dc/elements/1.1/"
EPUB_NS = "http://www.idpf.org/2007/ops"
NCX_NS  = "http://www.daisy.org/z3986/2005/ncx/"

for _pfx, _uri in [
    ("",    OPF_NS),
    ("dc",  DC_NS),
    ("epub", EPUB_NS),
    ("ncx", NCX_NS),
]:
    ET.register_namespace(_pfx, _uri)

CONTENT_TYPES = {"application/xhtml+xml", "text/html"}


def bytes_to_ms(n: int, bytes_per_ms: float = 6.0) -> float:
    return n / bytes_per_ms


def ms_to_clock(ms: float) -> str:
    ms = max(0.0, ms)
    total_s, ms_r = divmod(int(ms), 1000)
    h, rem = divmod(total_s, 3600)
    m, s   = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}.{ms_r:03d}"


def ms_to_human(ms: float) -> str:
    m, s = divmod(ms / 1000, 60)
    return f"{int(m)}m {s:.0f}s"


def find_opf_path(zf: zipfile.ZipFile) -> str:
    with zf.open("META-INF/container.xml") as f:
        tree = ET.parse(f)
    ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
    rf = tree.find(".//c:rootfile", ns)
    if rf is None:
        raise ValueError("Cannot find rootfile in META-INF/container.xml")
    return rf.attrib["full-path"]


def parse_opf(zf: zipfile.ZipFile, opf_path: str):
    with zf.open(opf_path) as f:
        root = ET.fromstring(f.read())
    opf_dir = posixpath.dirname(opf_path)

    ns = OPF_NS
    manifest = {
        item.get("id"): {
            "href":       item.get("href", ""),
            "media-type": item.get("media-type", ""),
        }
        for item in root.findall(f".//{{{ns}}}manifest/{{{ns}}}item")
    }
    spine = [
        ir.get("idref")
        for ir in root.findall(f".//{{{ns}}}spine/{{{ns}}}itemref")
    ]
    return root, manifest, spine, opf_dir


def epub_path(opf_dir: str, href: str) -> str:
    return posixpath.join(opf_dir, href) if opf_dir else href


_BACKMATTER_EPUB_TYPES = {
    "backmatter", "endnotes", "footnotes", "rearnotes", "notes",
    "bibliography", "appendix", "index", "glossary", "colophon",
}

_BACKMATTER_FILENAME_RE = re.compile(
    r'(?:notes?|endnotes?|footnotes?|bibliograph\w*|appendix|glossary'
    r'|references?|colophon|backmatter)',
    re.IGNORECASE,
)

_BACKMATTER_HEADING_RE = re.compile(
    r'^\s*(?:notes?|endnotes?|footnotes?|bibliography|references?'
    r'|appendix|index|glossary|colophon|works?\s+cited'
    r'|further\s+reading|about\s+the\s+author)\s*$',
    re.IGNORECASE,
)


_BACKMATTER_EPUBTYPE_RE = re.compile(
    r'epub:type\s*=\s*["\']([^"\']*(?:'
    + "|".join(_BACKMATTER_EPUB_TYPES)
    + r')[^"\']*)["\']',
    re.IGNORECASE,
)


def _is_backmatter(html_bytes: bytes, href: str) -> bool:
    """Heuristic detection of back matter sections (notes, bibliography, etc.)."""
    # Check filename — only match if the backmatter keyword is the main part
    # of the filename (not just a substring of a longer word or a link target)
    basename = posixpath.basename(href).lower()
    # Strip leading digits/underscores (e.g. "22_Notes.xhtml" -> "notes.xhtml")
    stripped = re.sub(r'^[\d_]+', '', basename)
    if _BACKMATTER_FILENAME_RE.match(stripped):
        return True

    # Check epub:type attributes on body/section elements (not in href links)
    html_str = html_bytes[:4096].decode("utf-8", errors="replace")
    if _BACKMATTER_EPUBTYPE_RE.search(html_str):
        return True

    # Check first heading text
    soup = BeautifulSoup(html_bytes[:4096], "lxml")
    for h in soup.find_all(["h1", "h2", "h3"], limit=2):
        if _BACKMATTER_HEADING_RE.match(h.get_text(strip=True)):
            return True

    return False


def process_chapter(html_bytes: bytes, ch_idx: int):
    """Parse a chapter, inject IDs on paragraphs, extract text.

    Returns (modified_html_bytes, paragraphs) where paragraphs is a list of
    {"id": str, "text": str, "tag": BeautifulSoup Tag (for word/sentence injection)}.
    """
    soup = BeautifulSoup(html_bytes, "lxml")
    paragraphs = []
    counter = 1

    for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p"]):
        text = " ".join(tag.get_text(" ", strip=True).split())
        if len(text) < 2:
            continue
        pid = f"mo_ch{ch_idx:03d}p{counter:04d}"
        tag["id"] = pid
        paragraphs.append({"id": pid, "text": text, "tag": tag})
        counter += 1

    return soup, paragraphs


def _make_fixed_page(viewport_w, viewport_h, title="", body_content=""):
    """Create a minimal fixed-layout XHTML page string."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops">\n'
        '<head>\n'
        f'  <title>{escape(title)}</title>\n'
        f'  <meta name="viewport" content="width={viewport_w}, '
        f'height={viewport_h}"/>\n'
        '  <style type="text/css">\n'
        '    body {\n'
        f'      margin: {_FXL_MARGIN // 2}px;\n'
        '      font-family: Georgia, serif;\n'
        f'      font-size: {_FXL_FONT_SIZE}px;\n'
        f'      line-height: {_FXL_LINE_HEIGHT};\n'
        '      overflow: hidden;\n'
        '    }\n'
        '    p { margin: 0.4em 0; text-indent: 1.5em; }\n'
        '    p.noindent { text-indent: 0; }\n'
        '    p.cont { text-indent: 0; margin-top: 0; }\n'
        '    .-epub-media-overlay-active {\n'
        '      background-color: #FFFF99;\n'
        '      color: #000000;\n'
        '    }\n'
        '  </style>\n'
        '</head>\n'
        '<body>\n'
        f'{body_content}\n'
        '</body>\n'
        '</html>'
    )


_FXL_FONT_SIZE   = 14
_FXL_LINE_HEIGHT = 1.5
_FXL_MARGIN      = 50  # total left + right (or top + bottom)
_FXL_PARA_GAP    = 14  # margin between paragraphs


def _estimate_para_height(text, viewport_w):
    """Rough estimate of paragraph height in pixels for fixed layout."""
    usable_w = viewport_w - _FXL_MARGIN
    # Average char width ≈ 0.43 * font_size for proportional serif
    chars_per_line = max(1, int(usable_w / (_FXL_FONT_SIZE * 0.43)))
    num_lines = max(1, -(-len(text) // chars_per_line))  # ceil division
    return int(num_lines * _FXL_FONT_SIZE * _FXL_LINE_HEIGHT) + _FXL_PARA_GAP


def _prepare_words(para_text, bounds, ch_idx, para_idx):
    """Convert word boundaries into word records preserving punctuation.

    Returns list of dicts with keys: text, id, offset, duration, prefix.
    Offset/duration are in ms, relative to start of the paragraph's audio.
    """
    word_bounds = [b for b in bounds
                   if b["type"] == "wordboundary" and b.get("text")]
    words = []
    search_pos = 0

    for wi, wb in enumerate(word_bounds):
        word = wb["text"]
        idx = para_text.find(word, search_pos)
        if idx < 0:
            idx = para_text.lower().find(word.lower(), search_pos)
        if idx < 0:
            idx = search_pos

        prefix = para_text[search_pos:idx] if idx > search_pos else ""

        word_end = idx + len(word)
        while (word_end < len(para_text)
               and para_text[word_end] in ".,;:!?\"')]-\u2019\u201d"):
            word_end += 1

        wid = f"mo_ch{ch_idx:03d}p{para_idx:04d}w{wi+1:04d}"
        words.append({
            "text":     para_text[idx:word_end],
            "id":       wid,
            "offset":   wb["offset"],
            "duration": wb["duration"],
            "prefix":   prefix,
        })
        search_pos = word_end

    # Trailing text after last word
    if search_pos < len(para_text) and words:
        words[-1]["suffix"] = para_text[search_pos:]

    return words


def _flow_chapter_into_pages(paragraphs, audio_chunks, all_boundaries,
                             viewport, bytes_per_ms, ch_idx):
    """Flow chapter content into fixed-layout pages.

    Paragraphs are split at word boundaries when they overflow a page,
    so text flows naturally across pages like a real book.

    Returns list of pages.  Each page is a list of fragment dicts:
        {"words": [...], "para_id": str, "css_class": str,
         "audio_base": float (ms), "is_continuation": bool}
    Words without boundary data use a fallback with para_text / audio range.
    """
    vw, vh = viewport
    usable_h = vh - _FXL_MARGIN
    chars_per_line = max(1, int((vw - _FXL_MARGIN) / (_FXL_FONT_SIZE * 0.43)))
    line_h = int(_FXL_FONT_SIZE * _FXL_LINE_HEIGHT)

    pages = []
    current_page = []
    current_h = 0
    audio_cursor = 0.0  # ms offset of current paragraph in chapter audio

    for pi, (para, audio_bytes, bounds) in enumerate(
            zip(paragraphs, audio_chunks, all_boundaries)):
        para_dur = bytes_to_ms(len(audio_bytes), bytes_per_ms)
        words = _prepare_words(para["text"], bounds, ch_idx, pi + 1)

        # Extract CSS class from original tag
        tag = para.get("tag")
        css_class = ""
        if tag:
            cls = tag.get("class", [])
            if isinstance(cls, list) and cls:
                css_class = cls[0]
            elif isinstance(cls, str):
                css_class = cls

        if not words:
            # No word data — treat whole paragraph as indivisible
            h = _estimate_para_height(para["text"], vw)
            if current_page and current_h + h > usable_h * 1.15:
                pages.append(current_page)
                current_page, current_h = [], 0
            current_page.append({
                "words":      [],
                "para_text":  para["text"],
                "para_id":    para["id"],
                "css_class":  css_class,
                "audio_base": audio_cursor,
                "audio_end":  audio_cursor + para_dur,
                "is_continuation": False,
            })
            current_h += h
            audio_cursor += para_dur
            continue

        # Estimate full paragraph height
        para_h = _estimate_para_height(para["text"], vw)

        if current_h + para_h <= usable_h * 1.15:
            # Whole paragraph fits on current page
            current_page.append({
                "words":      words,
                "para_id":    para["id"],
                "css_class":  css_class,
                "audio_base": audio_cursor,
                "is_continuation": False,
            })
            current_h += para_h
        else:
            # Split paragraph across pages at word boundaries
            remaining = list(words)
            is_first = True

            while remaining:
                avail_h = (usable_h - current_h) if current_page else usable_h

                # Less than ~2 lines left? Start fresh page.
                if avail_h < line_h * 2 and current_page:
                    pages.append(current_page)
                    current_page, current_h = [], 0
                    avail_h = usable_h

                # How many chars fit in available space?
                avail_lines = max(1, int(avail_h / line_h))
                avail_chars = avail_lines * chars_per_line

                # Find word-boundary split point
                char_count = 0
                split_at = len(remaining)
                for wi, w in enumerate(remaining):
                    char_count += len(w.get("prefix", "")) + len(w["text"])
                    if char_count > avail_chars and wi > 0:
                        split_at = wi
                        break

                frag_words = remaining[:split_at]
                remaining = remaining[split_at:]

                if frag_words:
                    frag_chars = sum(
                        len(w.get("prefix", "")) + len(w["text"])
                        for w in frag_words
                    )
                    frag_h = _estimate_para_height("x" * frag_chars, vw)

                    current_page.append({
                        "words":      frag_words,
                        "para_id":    para["id"],
                        "css_class":  css_class,
                        "audio_base": audio_cursor,
                        "is_continuation": not is_first,
                    })
                    current_h += frag_h
                    is_first = False

                if remaining:
                    pages.append(current_page)
                    current_page, current_h = [], 0

        audio_cursor += para_dur

    if current_page:
        pages.append(current_page)

    return pages


def _render_page_xhtml(fragments, viewport, ch_idx, pg_idx):
    """Build fixed-layout XHTML for a page from fragment data."""
    vw, vh = viewport
    body_parts = []

    for frag in fragments:
        css = frag["css_class"]
        if frag["is_continuation"]:
            css = "cont"

        cls_attr = f' class="{css}"' if css else ""

        if frag["words"]:
            spans = []
            for w in frag["words"]:
                prefix = escape(w.get("prefix", ""))
                suffix = escape(w.get("suffix", ""))
                spans.append(
                    f'{prefix}<span id="{w["id"]}">'
                    f'{escape(w["text"])}</span>{suffix}'
                )
            body_parts.append(f"<p{cls_attr}>{''.join(spans)}</p>")
        else:
            # Whole paragraph, no word data
            pid = frag["para_id"]
            body_parts.append(
                f'<p id="{pid}"{cls_attr}>{escape(frag["para_text"])}</p>'
            )

    title = f"Chapter {ch_idx} — Page {pg_idx}"
    return _make_fixed_page(vw, vh, title, "\n".join(body_parts))


def _render_page_smil(fragments, html_zip, audio_zip, smil_dir):
    """Build SMIL XML for a page.  Returns (smil_xml, duration_ms)."""
    html_rel  = posixpath.relpath(html_zip,  smil_dir)
    audio_rel = posixpath.relpath(audio_zip, smil_dir)
    indent = "      "
    pars = []
    par_n = 0
    earliest = None
    latest = 0.0

    for frag in fragments:
        if frag["words"]:
            base = frag["audio_base"]
            for w in frag["words"]:
                par_n += 1
                abs_begin = base + w["offset"]
                abs_end   = base + w["offset"] + w["duration"]
                if earliest is None or abs_begin < earliest:
                    earliest = abs_begin
                if abs_end > latest:
                    latest = abs_end
                pars.append(
                    f'{indent}<par id="par{par_n:04d}">\n'
                    f'{indent}  <text src="{html_rel}#{w["id"]}"/>\n'
                    f'{indent}  <audio src="{audio_rel}"'
                    f' clipBegin="{ms_to_clock(abs_begin)}"'
                    f' clipEnd="{ms_to_clock(abs_end)}"/>\n'
                    f'{indent}</par>'
                )
        else:
            # Whole paragraph fallback
            par_n += 1
            abs_begin = frag["audio_base"]
            abs_end   = frag.get("audio_end", abs_begin)
            if earliest is None or abs_begin < earliest:
                earliest = abs_begin
            if abs_end > latest:
                latest = abs_end
            pars.append(
                f'{indent}<par id="par{par_n:04d}">\n'
                f'{indent}  <text src="{html_rel}#{frag["para_id"]}"/>\n'
                f'{indent}  <audio src="{audio_rel}"'
                f' clipBegin="{ms_to_clock(abs_begin)}"'
                f' clipEnd="{ms_to_clock(abs_end)}"/>\n'
                f'{indent}</par>'
            )

    duration_ms = (latest - earliest) if earliest is not None else 0.0
    profile = '\n      profile="http://www.idpf.org/epub/30/profile/content/"'
    smil = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<smil xmlns="http://www.w3.org/ns/SMIL"\n'
        '      xmlns:epub="http://www.idpf.org/2007/ops"\n'
        f'      version="3.0"{profile}>\n'
        '  <body>\n'
        f'    <seq id="seq001" epub:textref="{html_rel}">\n'
        + "\n".join(pars) + "\n"
        + '    </seq>\n'
          '  </body>\n'
          '</smil>\n'
    )
    return smil, duration_ms


def inject_word_spans(soup, para, boundaries, ch_idx, para_idx):
    """Replace a paragraph's content with <span> elements for each word.

    Matches Edge TTS word boundaries back to the original text to preserve
    all punctuation.  Returns a list of span records.
    """
    from bs4 import NavigableString

    tag = para["tag"]
    original = para["text"]
    word_bounds = [b for b in boundaries if b["type"] == "wordboundary" and b["text"]]

    if not word_bounds:
        return []

    tag.clear()
    spans = []
    search_pos = 0

    for wi, wb in enumerate(word_bounds):
        word = wb["text"]
        # Find word in original text
        idx = original.find(word, search_pos)
        if idx < 0:
            idx = original.lower().find(word.lower(), search_pos)
        if idx < 0:
            idx = search_pos

        # Any text before this word (spaces, opening punctuation)
        if idx > search_pos:
            before = original[search_pos:idx]
            tag.append(NavigableString(before))

        # Include trailing punctuation attached to the word
        word_end = idx + len(word)
        while word_end < len(original) and original[word_end] in ".,;:!?\"')]-\u2019\u201d":
            word_end += 1

        word_with_punct = original[idx:word_end]

        sid = f"mo_ch{ch_idx:03d}p{para_idx:04d}w{wi+1:04d}"
        span_tag = soup.new_tag("span", id=sid)
        span_tag.string = word_with_punct
        tag.append(span_tag)
        spans.append({"id": sid, "offset": wb["offset"], "duration": wb["duration"]})
        search_pos = word_end

    # Any trailing text after the last word
    if search_pos < len(original):
        tag.append(NavigableString(original[search_pos:]))

    return spans



def _split_into_sentences(text):
    """Split text into sentences at .!? boundaries."""
    parts = re.split(r'(?<=[.!?])\s+(?=[A-Z"\u201c(])', text)
    return [s.strip() for s in parts if s and s.strip()]


def _synthesize_with_sentence_pauses(text, voice, rate, audio_format,
                                     need_boundaries, sentence_pause_ms,
                                     bitrate_kbps):
    """Split paragraph into sentences, synthesize each, add silence between.

    Returns same format as synthesize_chunk: bytes or (bytes, boundaries).
    Word boundary offsets are adjusted to be relative to the combined audio.
    """
    sentences = _split_into_sentences(text)
    if len(sentences) <= 1:
        return synthesize_chunk(
            _sanitize_for_ssml(text), voice, rate,
            audio_format=audio_format, word_boundary=need_boundaries,
        )

    # Resolve bytes_per_ms from audio format
    bytes_per_ms = 6.0
    for _, (fmt, bpm, _kbps) in AUDIO_FORMATS.items():
        if fmt == audio_format:
            bytes_per_ms = bpm
            break

    silence = generate_mp3_silence(sentence_pause_ms, bitrate_kbps)

    combined_audio = b""
    combined_bounds = []
    cursor_ms = 0.0

    for si, sent in enumerate(sentences):
        try:
            result = synthesize_chunk(
                _sanitize_for_ssml(sent), voice, rate,
                audio_format=audio_format, word_boundary=need_boundaries,
            )
        except Exception:
            # Skip failed sentence
            continue

        if need_boundaries:
            audio_bytes, bounds = result
        else:
            audio_bytes = result
            bounds = []

        for b in bounds:
            combined_bounds.append({
                "type": b["type"],
                "offset": b["offset"] + cursor_ms,
                "duration": b["duration"],
                "text": b["text"],
            })

        combined_audio += audio_bytes
        cursor_ms += bytes_to_ms(len(audio_bytes), bytes_per_ms)

        # Add silence between sentences (not after the last one)
        if si < len(sentences) - 1:
            combined_audio += silence
            # Use bytes_to_ms for consistency with build_smil's timing
            cursor_ms += bytes_to_ms(len(silence), bytes_per_ms)

    if need_boundaries:
        return (combined_audio, combined_bounds)
    return combined_audio



def synthesize_paragraphs(paragraphs, voice, rate, workers, progress_cb,
                          cancel_event=None, audio_format="audio-24khz-48kbitrate-mono-mp3",
                          need_boundaries=False,
                          sentence_pause_ms=0, bitrate_kbps=48):
    """Synthesize every paragraph via Edge TTS using a thread pool.

    Returns:
        If need_boundaries is False: list of raw MP3 bytes in paragraph order
        If need_boundaries is True:  list of (mp3_bytes, boundaries) in paragraph order

    When sentence_pause_ms > 0, each paragraph is split into sentences,
    each sentence is synthesized individually, and silence is inserted between them.
    """
    total   = len(paragraphs)
    results = {}

    def _do(idx, text):
        if cancel_event and cancel_event.is_set():
            if need_boundaries:
                return idx, (b"", [])
            return idx, b""

        if sentence_pause_ms > 0:
            return idx, _synthesize_with_sentence_pauses(
                text, voice, rate, audio_format, need_boundaries,
                sentence_pause_ms, bitrate_kbps,
            )

        result = synthesize_chunk(
            _sanitize_for_ssml(text), voice, rate,
            audio_format=audio_format, word_boundary=need_boundaries,
        )
        return idx, result

    failed = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_do, i, p["text"]): i
            for i, p in enumerate(paragraphs)
        }
        done_count = 0
        for fut in as_completed(futures):
            if cancel_event and cancel_event.is_set():
                pool.shutdown(wait=False, cancel_futures=True)
                return None
            try:
                idx, result = fut.result()
            except Exception as e:
                idx = futures[fut]
                failed.append(idx)
                progress_cb(f"    WARNING: paragraph {idx+1} failed ({e}), skipping")
                if need_boundaries:
                    result = (b"", [])
                else:
                    result = b""
            results[idx] = result
            done_count += 1
            if done_count % max(1, total // 10) == 0 or done_count == total:
                progress_cb(f"    {done_count}/{total} paragraphs synthesized")

    if failed:
        progress_cb(f"    {len(failed)} paragraph(s) failed and were skipped")

    return [results[i] for i in range(total)]


def build_smil(paragraphs, audio_chunks,
               html_zip_path, audio_zip_path, smil_zip_dir,
               bytes_per_ms=6.0, span_data=None, apple_books=False,
               audio_offset_ms=0.0):
    """Generate SMIL XML.

    span_data: if provided, a list (one per paragraph) of span record lists.
        Each span record has {"id", "offset", "duration"}.  When present,
        SMIL entries are generated per-span instead of per-paragraph.
    apple_books: if True, adds profile attribute (Apple requirement).
    audio_offset_ms: offset into the shared audio file (for split pages).
    """
    html_rel  = posixpath.relpath(html_zip_path,  smil_zip_dir)
    audio_rel = posixpath.relpath(audio_zip_path, smil_zip_dir)

    indent = "      "

    pars   = []
    cursor = audio_offset_ms
    par_counter = 0

    for i, (para, audio) in enumerate(zip(paragraphs, audio_chunks)):
        para_dur = bytes_to_ms(len(audio), bytes_per_ms)

        if span_data and i < len(span_data) and span_data[i]:
            for sp in span_data[i]:
                par_counter += 1
                begin = ms_to_clock(cursor + sp["offset"])
                end   = ms_to_clock(cursor + sp["offset"] + sp["duration"])
                pars.append(
                    f'{indent}<par id="par{par_counter:04d}">\n'
                    f'{indent}  <text src="{html_rel}#{sp["id"]}"/>\n'
                    f'{indent}  <audio src="{audio_rel}"'
                    f' clipBegin="{begin}"'
                    f' clipEnd="{end}"/>\n'
                    f'{indent}</par>'
                )
        else:
            par_counter += 1
            begin = ms_to_clock(cursor)
            end   = ms_to_clock(cursor + para_dur)
            pars.append(
                f'{indent}<par id="par{par_counter:04d}">\n'
                f'{indent}  <text src="{html_rel}#{para["id"]}"/>\n'
                f'{indent}  <audio src="{audio_rel}"'
                f' clipBegin="{begin}"'
                f' clipEnd="{end}"/>\n'
                f'{indent}</par>'
            )

        cursor += para_dur

    profile = '\n      profile="http://www.idpf.org/epub/30/profile/content/"'

    smil = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<smil xmlns="http://www.w3.org/ns/SMIL"\n'
        '      xmlns:epub="http://www.idpf.org/2007/ops"\n'
        f'      version="3.0"{profile}>\n'
        '  <body>\n'
        f'    <seq id="seq001" epub:textref="{html_rel}">\n'
        + "\n".join(pars) + "\n"
        + '    </seq>\n'
          '  </body>\n'
          '</smil>\n'
    )
    # Return duration of just this segment (not including the offset)
    return smil, cursor - audio_offset_ms


def patch_opf(root, chapter_items, total_duration_ms, voice_name,
              apple_books=False, extra_spine_items=None):
    ns = OPF_NS
    root.set("version", "3.0")
    manifest = root.find(f"{{{ns}}}manifest")
    metadata = root.find(f"{{{ns}}}metadata")
    spine_el = root.find(f"{{{ns}}}spine")

    audio_media_type = "audio/m4a" if apple_books else "audio/mpeg"

    # Apple Books: add prefix for rendition + ibooks vocabularies
    if apple_books:
        existing = root.get("prefix", "")
        prefixes = (
            "rendition: http://www.idpf.org/vocab/rendition/# "
            "ibooks: http://vocabulary.itunes.apple.com/rdf/ibooks/"
            "vocabulary-extensions-1.0"
        )
        if existing:
            root.set("prefix", f"{existing} {prefixes}")
        else:
            root.set("prefix", prefixes)

        # Fixed-layout metadata
        layout = ET.SubElement(metadata, f"{{{ns}}}meta")
        layout.set("property", "rendition:layout")
        layout.text = "pre-paginated"

        spread = ET.SubElement(metadata, f"{{{ns}}}meta")
        spread.set("property", "rendition:spread")
        spread.text = "auto"

        fonts = ET.SubElement(metadata, f"{{{ns}}}meta")
        fonts.set("property", "ibooks:specified-fonts")
        fonts.text = "true"

    # Track audio IDs already added to avoid duplicates (pages share audio)
    added_audio_ids = set()

    for ch in chapter_items:
        is_page = ch.get("is_page", False)

        if is_page:
            # Add page XHTML to manifest with media-overlay
            pi = ET.SubElement(manifest, f"{{{ns}}}item")
            pi.set("id",            ch["spine_id"])
            pi.set("href",          ch["page_href"])
            pi.set("media-type",    "application/xhtml+xml")
            pi.set("media-overlay", ch["smil_id"])
        else:
            # Standard mode: find existing manifest item, add media-overlay
            for item in manifest.findall(f"{{{ns}}}item"):
                if item.get("id") == ch["spine_id"]:
                    item.set("media-overlay", ch["smil_id"])

        # Add SMIL to manifest
        si = ET.SubElement(manifest, f"{{{ns}}}item")
        si.set("id",         ch["smil_id"])
        si.set("href",       ch["smil_href"])
        si.set("media-type", "application/smil+xml")

        # Add audio to manifest (only once per chapter)
        if ch["audio_id"] not in added_audio_ids:
            ai = ET.SubElement(manifest, f"{{{ns}}}item")
            ai.set("id",         ch["audio_id"])
            ai.set("href",       ch["audio_href"])
            ai.set("media-type", audio_media_type)
            added_audio_ids.add(ch["audio_id"])

        # Per-SMIL duration metadata
        m = ET.SubElement(metadata, f"{{{ns}}}meta")
        m.set("property", "media:duration")
        m.set("refines",  f"#{ch['smil_id']}")
        m.text = ms_to_clock(ch["duration_ms"])

    # Apple Books: replace original chapter spine entries with page entries
    if extra_spine_items and spine_el is not None:
        for orig_id, page_items in extra_spine_items:
            # Find the <itemref> for the original chapter
            orig_ref = None
            for itemref in spine_el.findall(f"{{{ns}}}itemref"):
                if itemref.get("idref") == orig_id:
                    orig_ref = itemref
                    break

            if orig_ref is not None:
                # Insert page itemrefs before the original, then remove it
                insert_idx = list(spine_el).index(orig_ref)
                for offset, (pg_id, _pg_href) in enumerate(page_items):
                    ir = ET.Element(f"{{{ns}}}itemref")
                    ir.set("idref", pg_id)
                    spine_el.insert(insert_idx + offset, ir)
                spine_el.remove(orig_ref)

                # Remove original chapter from manifest too
                for item in manifest.findall(f"{{{ns}}}item"):
                    if item.get("id") == orig_id:
                        manifest.remove(item)
                        break

    # Total book duration
    mt = ET.SubElement(metadata, f"{{{ns}}}meta")
    mt.set("property", "media:duration")
    mt.text = ms_to_clock(total_duration_ms)

    mn = ET.SubElement(metadata, f"{{{ns}}}meta")
    mn.set("property", "media:narrator")
    mn.text = voice_name

    mc = ET.SubElement(metadata, f"{{{ns}}}meta")
    mc.set("property", "media:active-class")
    mc.text = "-epub-media-overlay-active"

    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


# ═════════════════════════════════════════════════════════════════════════════
#  Section 7 — Conversion orchestrators
# ═════════════════════════════════════════════════════════════════════════════

def _unique_path(path):
    """Return path, or path_1, path_2, ... if it already exists."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(f"{base}_{n}{ext}"):
        n += 1
    return f"{base}_{n}{ext}"


def convert_epub_to_epub3(input_path, output_path, voice, rate, workers,
                          progress_cb, cancel_event=None,
                          audio_format="audio-24khz-48kbitrate-mono-mp3",
                          bytes_per_ms=6.0,
                          apple_books=False, viewport=None,
                          para_pause_ms=0, sentence_pause_ms=0,
                          bitrate_kbps=48, skip_backmatter=False):
    """Convert EPUB to EPUB3 with Media Overlays."""
    # Always request word boundaries for word-level sync
    need_boundaries = True

    # Validate ffmpeg availability early when Apple Books mode is selected
    if apple_books and not _get_ffmpeg_path():
        progress_cb("ffmpeg not found — downloading automatically...")
        if not _download_ffmpeg(progress_cb):
            progress_cb(
                "ERROR: ffmpeg download failed. Apple Books mode requires ffmpeg.\n"
                "Install ffmpeg manually and add it to PATH."
            )
            return None

    with zipfile.ZipFile(input_path, "r") as zf:
        opf_path = find_opf_path(zf)
        opf_root, manifest, spine, opf_dir = parse_opf(zf, opf_path)

        chapters = [
            (idref, manifest[idref]["href"])
            for idref in spine
            if idref in manifest
            and manifest[idref]["media-type"] in CONTENT_TYPES
        ]
        progress_cb(f"Found {len(chapters)} chapter(s) in spine.")

        modified = {}
        new_files = {}
        ch_items = []
        # Apple Books: track spine replacements and files to exclude
        extra_spine_items = []  # (original_spine_id, [(id, href), ...])
        excluded_files = set()  # original XHTML files replaced by pages

        for ch_idx, (spine_id, href) in enumerate(chapters, 1):
            if cancel_event and cancel_event.is_set():
                progress_cb("Cancelled.")
                return None

            html_zip_path = epub_path(opf_dir, href)
            label = f"Ch {ch_idx:03d}/{len(chapters):03d}"
            progress_cb(f"[{label}]  {href}")

            with zf.open(html_zip_path) as f:
                html_bytes = f.read()

            if skip_backmatter and _is_backmatter(html_bytes, href):
                progress_cb("  -> Back matter detected, skipping TTS.")
                continue

            soup, paragraphs = process_chapter(html_bytes, ch_idx)

            if not paragraphs:
                progress_cb("  -> No paragraphs found, skipping.")
                continue

            progress_cb(f"  {len(paragraphs)} paragraph(s) to synthesize...")

            synth_results = synthesize_paragraphs(
                paragraphs, voice, rate, workers, progress_cb, cancel_event,
                audio_format=audio_format, need_boundaries=need_boundaries,
                sentence_pause_ms=sentence_pause_ms, bitrate_kbps=bitrate_kbps,
            )
            if synth_results is None:
                progress_cb("Cancelled.")
                return None

            # Split results into audio chunks and optional boundary data
            if need_boundaries:
                audio_chunks = [r[0] for r in synth_results]
                all_boundaries = [r[1] for r in synth_results]
            else:
                audio_chunks = synth_results
                all_boundaries = None

            # Append silence after each paragraph (except the last)
            if para_pause_ms > 0 and len(audio_chunks) > 1:
                silence = generate_mp3_silence(para_pause_ms, bitrate_kbps)
                for ci in range(len(audio_chunks) - 1):
                    audio_chunks[ci] = audio_chunks[ci] + silence

            # ── Apple Books: flow text into fixed-layout pages ─────────
            if apple_books and viewport:
                # all_boundaries is always available (forced above)
                pages = _flow_chapter_into_pages(
                    paragraphs, audio_chunks, all_boundaries,
                    viewport, bytes_per_ms, ch_idx,
                )
                progress_cb(f"  Split into {len(pages)} page(s)")

                audio_ext = ".m4a"
                audio_href = f"Audio/ch{ch_idx:03d}{audio_ext}"
                audio_zip  = epub_path(opf_dir, audio_href)
                chapter_audio = b"".join(audio_chunks)
                new_files[audio_zip] = chapter_audio

                total_ch_dur = 0.0
                page_spine_items = []

                for pg_idx, page_fragments in enumerate(pages, 1):
                    pg_id   = f"ch{ch_idx:03d}p{pg_idx:03d}"
                    pg_href = f"Pages/ch{ch_idx:03d}_p{pg_idx:03d}.xhtml"
                    pg_zip  = epub_path(opf_dir, pg_href)

                    pg_xhtml = _render_page_xhtml(
                        page_fragments, viewport, ch_idx, pg_idx,
                    )
                    new_files[pg_zip] = pg_xhtml.encode("utf-8")

                    smil_href = f"SMIL/ch{ch_idx:03d}_p{pg_idx:03d}.smil"
                    smil_zip  = epub_path(opf_dir, smil_href)
                    smil_zip_dir = posixpath.dirname(smil_zip)

                    smil_xml, page_dur = _render_page_smil(
                        page_fragments, pg_zip, audio_zip, smil_zip_dir,
                    )
                    new_files[smil_zip] = smil_xml.encode("utf-8")
                    total_ch_dur += page_dur

                    smil_id = f"overlay-{pg_id}"
                    ch_items.append({
                        "spine_id":    pg_id,
                        "smil_id":     smil_id,
                        "smil_href":   smil_href,
                        "audio_id":    f"audio-ch{ch_idx:03d}",
                        "audio_href":  audio_href,
                        "duration_ms": page_dur,
                        "page_href":   pg_href,
                        "is_page":     True,
                    })
                    page_spine_items.append((pg_id, pg_href))

                extra_spine_items.append((spine_id, page_spine_items))
                excluded_files.add(html_zip_path)
                progress_cb(f"  -> {ms_to_human(int(total_ch_dur))}")

            # ── Standard EPUB3: one SMIL per chapter ────────────────────
            else:
                # Inject word-level spans into HTML for media overlay sync
                span_data = None
                if all_boundaries:
                    span_data = []
                    for pi, (para, bounds) in enumerate(
                            zip(paragraphs, all_boundaries)):
                        spans = inject_word_spans(
                            soup, para, bounds, ch_idx, pi + 1)
                        span_data.append(spans)

                audio_ext = ".mp3"
                audio_href = f"Audio/ch{ch_idx:03d}{audio_ext}"
                smil_href  = f"SMIL/ch{ch_idx:03d}.smil"
                audio_zip  = epub_path(opf_dir, audio_href)
                smil_zip   = epub_path(opf_dir, smil_href)
                smil_zip_dir = posixpath.dirname(smil_zip)

                smil_xml, duration_ms = build_smil(
                    paragraphs, audio_chunks,
                    html_zip_path, audio_zip, smil_zip_dir,
                    bytes_per_ms=bytes_per_ms, span_data=span_data,
                    apple_books=False,
                )

                chapter_audio = b"".join(audio_chunks)

                modified[html_zip_path] = soup.encode("utf-8")
                new_files[audio_zip]    = chapter_audio
                new_files[smil_zip]     = smil_xml.encode("utf-8")

                ch_items.append({
                    "spine_id":    spine_id,
                    "smil_id":     f"overlay-ch{ch_idx:03d}",
                    "smil_href":   smil_href,
                    "audio_id":    f"audio-ch{ch_idx:03d}",
                    "audio_href":  audio_href,
                    "duration_ms": duration_ms,
                    "is_page":     False,
                })

                progress_cb(f"  -> {ms_to_human(duration_ms)}")

        # Apple Books: inject viewport into remaining XHTML files
        # (cover, TOC, etc.) that weren't replaced by split pages
        if apple_books and viewport:
            for name in zf.namelist():
                if name in modified or name in excluded_files:
                    continue
                if not name.endswith((".xhtml", ".html", ".htm")):
                    continue
                raw = zf.read(name)
                s = BeautifulSoup(raw, "lxml")
                head = s.find("head")
                if head:
                    vp = head.find("meta", attrs={"name": "viewport"})
                    if not vp:
                        vp_tag = s.new_tag("meta")
                        vp_tag["name"] = "viewport"
                        vp_tag["content"] = (
                            f"width={viewport[0]}, height={viewport[1]}"
                        )
                        head.append(vp_tag)
                    modified[name] = s.encode("utf-8")

        # Batch-convert all chapter audio to M4A in parallel
        if apple_books:
            audio_keys = [k for k in new_files if k.endswith(".m4a")]
            if audio_keys:
                ffmpeg_workers = min(os.cpu_count() or 4, len(audio_keys), 6)
                progress_cb(
                    f"Converting {len(audio_keys)} chapter(s) to M4A (AAC) "
                    f"using {ffmpeg_workers} workers..."
                )

                def _convert_one(key):
                    return key, mp3_to_m4a(new_files[key])

                with ThreadPoolExecutor(max_workers=ffmpeg_workers) as pool:
                    futures = {pool.submit(_convert_one, k): k for k in audio_keys}
                    done_count = 0
                    for fut in as_completed(futures):
                        if cancel_event and cancel_event.is_set():
                            progress_cb("Cancelled.")
                            return None
                        key, m4a_data = fut.result()
                        new_files[key] = m4a_data
                        done_count += 1
                        progress_cb(
                            f"  M4A {done_count}/{len(audio_keys)} done"
                        )

        total_ms = sum(c["duration_ms"] for c in ch_items)
        modified[opf_path] = patch_opf(
            opf_root, ch_items, total_ms, voice, apple_books=apple_books,
            extra_spine_items=extra_spine_items,
        )

        progress_cb("Writing output EPUB3...")
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as out:
            out.writestr(
                zipfile.ZipInfo("mimetype"),
                "application/epub+zip",
                compress_type=zipfile.ZIP_STORED,
            )
            for name in zf.namelist():
                if name == "mimetype":
                    continue
                if name in excluded_files:
                    continue
                data = modified.get(name, zf.read(name))
                out.writestr(name, data)
            for path, data in new_files.items():
                out.writestr(path, data)

    progress_cb(f"Done!  Total audio: {ms_to_human(total_ms)}")
    progress_cb(f"Output: {output_path}")
    return output_path


def convert_epub_to_mp3(input_path, output_dir, voice, rate, workers,
                        progress_cb, cancel_event=None,
                        audio_format="audio-24khz-48kbitrate-mono-mp3",
                        bytes_per_ms=6.0, para_pause_ms=0, sentence_pause_ms=0,
                        bitrate_kbps=48,
                        skip_backmatter=False):
    """Extract chapter text from EPUB and synthesize per-chapter MP3s."""
    os.makedirs(output_dir, exist_ok=True)

    with zipfile.ZipFile(input_path, "r") as zf:
        opf_path = find_opf_path(zf)
        _, manifest, spine, opf_dir = parse_opf(zf, opf_path)

        chapters = [
            (idref, manifest[idref]["href"])
            for idref in spine
            if idref in manifest
            and manifest[idref]["media-type"] in CONTENT_TYPES
        ]
        progress_cb(f"Found {len(chapters)} chapter(s) in spine.")

        for ch_idx, (spine_id, href) in enumerate(chapters, 1):
            if cancel_event and cancel_event.is_set():
                progress_cb("Cancelled.")
                return None

            html_zip_path = epub_path(opf_dir, href)
            label = f"Ch {ch_idx:03d}/{len(chapters):03d}"
            progress_cb(f"[{label}]  {href}")

            with zf.open(html_zip_path) as f:
                html_bytes = f.read()

            if skip_backmatter and _is_backmatter(html_bytes, href):
                progress_cb("  -> Back matter detected, skipping TTS.")
                continue

            _, paragraphs = process_chapter(html_bytes, ch_idx)

            if not paragraphs:
                progress_cb("  -> No paragraphs found, skipping.")
                continue

            progress_cb(f"  {len(paragraphs)} paragraph(s) to synthesize...")

            audio_chunks = synthesize_paragraphs(
                paragraphs, voice, rate, workers, progress_cb, cancel_event,
                audio_format=audio_format,
                sentence_pause_ms=sentence_pause_ms, bitrate_kbps=bitrate_kbps,
            )
            if audio_chunks is None:
                progress_cb("Cancelled.")
                return None

            # Inject silence between paragraphs
            if para_pause_ms > 0 and len(audio_chunks) > 1:
                silence = generate_mp3_silence(para_pause_ms, bitrate_kbps)
                audio_chunks = [
                    c + silence if i < len(audio_chunks) - 1 else c
                    for i, c in enumerate(audio_chunks)
                ]

            mp3_path = os.path.join(output_dir, f"ch{ch_idx:03d}.mp3")
            with open(mp3_path, "wb") as f:
                f.write(b"".join(audio_chunks))

            duration_ms = bytes_to_ms(sum(len(c) for c in audio_chunks), bytes_per_ms)
            progress_cb(f"  -> {ms_to_human(duration_ms)}  ({mp3_path})")

    progress_cb(f"Done!  MP3s saved to: {output_dir}")
    return output_dir


def convert_text_to_mp3(input_path, output_path, voice, rate, workers,
                        progress_cb, cancel_event=None,
                        audio_format="audio-24khz-48kbitrate-mono-mp3",
                        bitrate_kbps=48):
    """Read a text/markdown file and synthesize to a single MP3."""
    with open(input_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    if not text.strip():
        progress_cb("File is empty, nothing to synthesize.")
        return None

    items = split_text_for_tts(text)
    text_items = [it for it in items if isinstance(it, str)]
    total = len(text_items)

    if total == 0:
        progress_cb("No synthesizable text found.")
        return None

    progress_cb(f"{total} text chunk(s) to synthesize...")

    text_indices = []
    for i, item in enumerate(items):
        if isinstance(item, str):
            text_indices.append((i, item))

    audio_map = {}

    def _do(items_idx, chunk_text):
        if cancel_event and cancel_event.is_set():
            return items_idx, b""
        audio = synthesize_chunk(
            _sanitize_for_ssml(chunk_text), voice, rate,
            audio_format=audio_format,
        )
        return items_idx, audio

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_do, idx, txt): idx
            for idx, txt in text_indices
        }
        done_count = 0
        for fut in as_completed(futures):
            if cancel_event and cancel_event.is_set():
                pool.shutdown(wait=False, cancel_futures=True)
                progress_cb("Cancelled.")
                return None
            items_idx, audio = fut.result()
            audio_map[items_idx] = audio
            done_count += 1
            if done_count % max(1, total // 10) == 0 or done_count == total:
                progress_cb(f"    {done_count}/{total} chunks synthesized")

    with open(output_path, "wb") as out:
        for i, item in enumerate(items):
            if isinstance(item, tuple) and item[0] == _SILENCE:
                out.write(generate_mp3_silence(item[1], bitrate_kbps))
            elif isinstance(item, str):
                out.write(audio_map[i])

    size_kb = os.path.getsize(output_path) // 1024
    progress_cb(f"Done!  {output_path}  ({size_kb} KB)")
    return output_path


# ═════════════════════════════════════════════════════════════════════════════
#  Section 8 — tkinter GUI
# ═════════════════════════════════════════════════════════════════════════════

class ConverterApp:
    def __init__(self, root):
        self.root = root
        self.root.title("EPUB / Text to Audio Converter")
        self.root.minsize(650, 550)

        self.msg_queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker_thread = None

        # label -> short_name mapping for voices
        self.voice_menu = OrderedDict(FALLBACK_VOICES)

        self._build_ui()
        self._poll_queue()
        self._fetch_voices_async()

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # ── File list ─────────────────────────────────────────────────────
        frame_files = ttk.LabelFrame(self.root, text="Files")
        frame_files.pack(fill="x", **pad)

        self.file_listbox = tk.Listbox(frame_files, height=5, selectmode="extended")
        self.file_listbox.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)

        sb = ttk.Scrollbar(frame_files, orient="vertical",
                           command=self.file_listbox.yview)
        sb.pack(side="left", fill="y", pady=4)
        self.file_listbox.config(yscrollcommand=sb.set)

        btn_frame = ttk.Frame(frame_files)
        btn_frame.pack(side="left", fill="y", padx=4, pady=4)
        ttk.Button(btn_frame, text="Browse...", command=self._browse_files).pack(fill="x", pady=2)
        ttk.Button(btn_frame, text="Remove", command=self._remove_selected).pack(fill="x", pady=2)
        ttk.Button(btn_frame, text="Clear", command=self._clear_files).pack(fill="x", pady=2)

        self.files = []  # list of absolute paths

        # ── Settings ──────────────────────────────────────────────────────
        frame_settings = ttk.LabelFrame(self.root, text="Settings")
        frame_settings.pack(fill="x", **pad)

        # Voice
        row = ttk.Frame(frame_settings)
        row.pack(fill="x", padx=4, pady=2)
        ttk.Label(row, text="Voice:").pack(side="left")
        default_label = next(
            (k for k, v in self.voice_menu.items()
             if v == "en-US-AvaMultilingualNeural"),
            list(self.voice_menu.keys())[0],
        )
        self.voice_var = tk.StringVar(value=default_label)
        self.voice_combo = ttk.Combobox(row, textvariable=self.voice_var,
                                         values=list(self.voice_menu.keys()),
                                         width=45)
        self.voice_combo.pack(side="left", padx=(4, 0), fill="x", expand=True)

        # Rate
        row2 = ttk.Frame(frame_settings)
        row2.pack(fill="x", padx=4, pady=2)
        ttk.Label(row2, text="Rate:").pack(side="left")
        self.rate_var = tk.IntVar(value=0)
        self.rate_scale = ttk.Scale(row2, from_=-50, to=50,
                                     variable=self.rate_var, orient="horizontal",
                                     command=self._on_rate_change)
        self.rate_scale.pack(side="left", fill="x", expand=True, padx=(4, 0))
        self.rate_label = ttk.Label(row2, text="+0%", width=6)
        self.rate_label.pack(side="left")

        # Workers
        row3 = ttk.Frame(frame_settings)
        row3.pack(fill="x", padx=4, pady=2)
        ttk.Label(row3, text="Workers:").pack(side="left")
        self.workers_var = tk.IntVar(value=6)
        ttk.Spinbox(row3, from_=1, to=100, textvariable=self.workers_var,
                     width=5).pack(side="left", padx=(4, 0))

        # Target reader
        row_aq = ttk.Frame(frame_settings)
        row_aq.pack(fill="x", padx=4, pady=2)
        ttk.Label(row_aq, text="Target:").pack(side="left")
        self.target_var = tk.StringVar(value=list(TARGET_READERS.keys())[0])
        ttk.Combobox(row_aq, textvariable=self.target_var,
                      values=list(TARGET_READERS.keys()),
                      state="readonly", width=42).pack(side="left", padx=(4, 0))

        # Pause between paragraphs
        row_pause = ttk.Frame(frame_settings)
        row_pause.pack(fill="x", padx=4, pady=2)
        ttk.Label(row_pause, text="Paragraph pause:").pack(side="left")
        self.pause_var = tk.StringVar(value="Short")
        ttk.Combobox(
            row_pause, textvariable=self.pause_var,
            values=list(PAUSE_PRESETS.keys()),
            state="readonly", width=22,
        ).pack(side="left", padx=(4, 0))

        # Pause between sentences
        row_spause = ttk.Frame(frame_settings)
        row_spause.pack(fill="x", padx=4, pady=2)
        ttk.Label(row_spause, text="Sentence pause:").pack(side="left")
        self.sentence_pause_var = tk.StringVar(value="Default")
        ttk.Combobox(
            row_spause, textvariable=self.sentence_pause_var,
            values=list(SENTENCE_PAUSE_PRESETS.keys()),
            state="readonly", width=22,
        ).pack(side="left", padx=(4, 0))

        # Output mode
        row4 = ttk.Frame(frame_settings)
        row4.pack(fill="x", padx=4, pady=2)
        ttk.Label(row4, text="Output:").pack(side="left")
        self.mode_var = tk.StringVar(value="epub3")
        self.rb_epub3 = ttk.Radiobutton(row4, text="EPUB3 with Media Overlay",
                                         variable=self.mode_var, value="epub3")
        self.rb_epub3.pack(side="left", padx=(4, 0))
        ttk.Radiobutton(row4, text="MP3 only",
                         variable=self.mode_var, value="mp3").pack(side="left", padx=(8, 0))

        # Close when done
        # Skip back matter (notes, bibliography, etc.)
        self.skip_backmatter_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame_settings, text="Skip back matter (notes, bibliography, index...)",
                         variable=self.skip_backmatter_var).pack(anchor="w", padx=4, pady=2)

        self.close_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame_settings, text="Close application when done",
                         variable=self.close_var).pack(anchor="w", padx=4, pady=2)

        # ── Action buttons ────────────────────────────────────────────────
        frame_action = ttk.Frame(self.root)
        frame_action.pack(fill="x", **pad)

        self.start_btn = ttk.Button(frame_action, text="Start Conversion",
                                     command=self._start)
        self.start_btn.pack(side="left", fill="x", expand=True, padx=(0, 4))

        self.cancel_btn = ttk.Button(frame_action, text="Cancel",
                                      command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left")

        # ── Progress ──────────────────────────────────────────────────────
        frame_prog = ttk.LabelFrame(self.root, text="Progress")
        frame_prog.pack(fill="both", expand=True, **pad)

        self.log_text = tk.Text(frame_prog, height=10, state="disabled",
                                 wrap="word", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True, padx=4, pady=(4, 0))

        log_sb = ttk.Scrollbar(self.log_text, orient="vertical",
                                command=self.log_text.yview)
        log_sb.pack(side="right", fill="y")
        self.log_text.config(yscrollcommand=log_sb.set)

        self.progress_bar = ttk.Progressbar(frame_prog, mode="determinate")
        self.progress_bar.pack(fill="x", padx=4, pady=4)

    # ── Voice loading ─────────────────────────────────────────────────────

    def _fetch_voices_async(self):
        """Fetch the full voice list from Microsoft in a background thread."""
        def _do():
            menu = fetch_voices()
            if menu:
                self.msg_queue.put(("voices", menu))
        threading.Thread(target=_do, daemon=True).start()

    def _get_selected_voice(self):
        """Resolve the combobox value to an Edge TTS short name."""
        val = self.voice_var.get().strip()
        if val in self.voice_menu:
            return self.voice_menu[val]
        return val

    def _apply_voice_menu(self, menu):
        """Replace the voice dropdown with the full fetched voice list."""
        # Remember what the user currently has selected
        prev_short = self._get_selected_voice()
        self.voice_menu = menu
        labels = list(menu.keys())
        self.voice_combo["values"] = labels
        # Try to re-select the same voice by short name
        new_label = next(
            (k for k, v in menu.items() if v == prev_short),
            labels[0] if labels else "",
        )
        self.voice_var.set(new_label)

    # ── UI callbacks ──────────────────────────────────────────────────────

    def _on_rate_change(self, _=None):
        v = int(float(self.rate_var.get()))
        self.rate_label.config(text=f"{v:+d}%")

    def _browse_files(self):
        paths = filedialog.askopenfilenames(
            title="Select files",
            filetypes=[
                ("Supported files", "*.epub *.txt *.md"),
                ("EPUB", "*.epub"),
                ("Text / Markdown", "*.txt *.md"),
            ],
        )
        for p in paths:
            if p not in self.files:
                self.files.append(p)
                self.file_listbox.insert("end", os.path.basename(p))
        self._update_mode_availability()

    def _remove_selected(self):
        for idx in reversed(self.file_listbox.curselection()):
            self.file_listbox.delete(idx)
            del self.files[idx]
        self._update_mode_availability()

    def _clear_files(self):
        self.file_listbox.delete(0, "end")
        self.files.clear()
        self._update_mode_availability()

    def _update_mode_availability(self):
        has_epub = any(f.lower().endswith(".epub") for f in self.files)
        if has_epub:
            self.rb_epub3.config(state="normal")
        else:
            self.rb_epub3.config(state="disabled")
            self.mode_var.set("mp3")

    def _log(self, msg):
        self.log_text.config(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    # ── Queue-based thread communication ──────────────────────────────────

    def _poll_queue(self):
        while not self.msg_queue.empty():
            try:
                msg_type, data = self.msg_queue.get_nowait()
            except queue.Empty:
                break
            if msg_type == "log":
                self._log(data)
            elif msg_type == "progress":
                self.progress_bar["value"] = data
            elif msg_type == "done":
                self._on_done()
            elif msg_type == "voices":
                self._apply_voice_menu(data)
        self.root.after(100, self._poll_queue)

    def _send(self, msg_type, data=None):
        self.msg_queue.put((msg_type, data))

    # ── Conversion control ────────────────────────────────────────────────

    def _start(self):
        if not self.files:
            self._log("No files selected.")
            return

        self.cancel_event.clear()
        self.start_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.progress_bar["value"] = 0

        self.worker_thread = threading.Thread(target=self._run, daemon=True)
        self.worker_thread.start()

    def _cancel(self):
        self.cancel_event.set()
        self._log("Cancelling... please wait.")
        self.cancel_btn.config(state="disabled")

    def _on_done(self):
        self.start_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        if self.close_var.get():
            self.root.after(500, self.root.destroy)

    # ── Worker thread ─────────────────────────────────────────────────────

    def _run(self):
        voice = self._get_selected_voice()
        rate_val = int(float(self.rate_var.get()))
        rate = f"{rate_val:+d}%"
        workers = self.workers_var.get()
        mode = self.mode_var.get()

        # Resolve pause presets
        pause_label = self.pause_var.get()
        para_pause_ms = PAUSE_PRESETS.get(pause_label, PAUSE_PRESETS["Short"])
        spause_label = self.sentence_pause_var.get()
        sentence_pause_ms = SENTENCE_PAUSE_PRESETS.get(
            spause_label, SENTENCE_PAUSE_PRESETS["Default"]
        )

        # Resolve target reader -> audio format + apple_books flag + viewport
        target_label = self.target_var.get()
        quality_key, apple_books, viewport_key = TARGET_READERS.get(
            target_label, TARGET_READERS[list(TARGET_READERS.keys())[0]]
        )
        audio_format, bytes_per_ms, bitrate_kbps = AUDIO_FORMATS[quality_key]
        viewport = VIEWPORTS.get(viewport_key) if viewport_key else None
        skip_backmatter = self.skip_backmatter_var.get()

        total_files = len(self.files)

        def progress_cb(msg):
            self._send("log", msg)

        for i, filepath in enumerate(self.files):
            if self.cancel_event.is_set():
                self._send("log", "Batch cancelled.")
                break

            self._send("log", f"\n{'=' * 55}")
            self._send("log", f"  File {i+1}/{total_files}: {os.path.basename(filepath)}")
            self._send("log", f"  Voice: {voice}  |  Rate: {rate}  |  Workers: {workers}")
            self._send("log", f"  Target: {target_label}  |  Para pause: {pause_label}  |  Sent pause: {spause_label}")
            self._send("log", f"{'=' * 55}")

            ext = os.path.splitext(filepath)[1].lower()

            try:
                if ext == ".epub":
                    if mode == "epub3":
                        output = _unique_path(
                            filepath.replace(".epub", "_audio.epub")
                        )
                        convert_epub_to_epub3(
                            filepath, output, voice, rate, workers,
                            progress_cb, self.cancel_event,
                            audio_format=audio_format,
                            bytes_per_ms=bytes_per_ms,
                            apple_books=apple_books,
                            viewport=viewport,
                            para_pause_ms=para_pause_ms,
                            sentence_pause_ms=sentence_pause_ms,
                            bitrate_kbps=bitrate_kbps,
                            skip_backmatter=skip_backmatter,
                        )
                    else:
                        output_dir = _unique_path(
                            os.path.splitext(filepath)[0] + "_mp3"
                        )
                        convert_epub_to_mp3(
                            filepath, output_dir, voice, rate, workers,
                            progress_cb, self.cancel_event,
                            audio_format=audio_format,
                            bytes_per_ms=bytes_per_ms,
                            para_pause_ms=para_pause_ms,
                            sentence_pause_ms=sentence_pause_ms,
                            bitrate_kbps=bitrate_kbps,
                            skip_backmatter=skip_backmatter,
                        )
                elif ext in (".txt", ".md"):
                    output = _unique_path(
                        os.path.splitext(filepath)[0] + ".mp3"
                    )
                    convert_text_to_mp3(
                        filepath, output, voice, rate, workers,
                        progress_cb, self.cancel_event,
                        audio_format=audio_format,
                        bitrate_kbps=bitrate_kbps,
                    )
                else:
                    self._send("log", f"  Unsupported file type: {ext}")
            except Exception as e:
                self._send("log", f"  ERROR: {e}")

            self._send("progress", (i + 1) / total_files * 100)

        if not self.cancel_event.is_set():
            self._send("log", "\nAll files processed.")
        self._send("done")


# ═════════════════════════════════════════════════════════════════════════════
#  Entry point
# ═════════════════════════════════════════════════════════════════════════════

def main():
    root = tk.Tk()
    ConverterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
