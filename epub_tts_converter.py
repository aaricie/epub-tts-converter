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
import base64
import functools
import hashlib
import json
import os
import posixpath
import queue
import re
import secrets
import ssl
import struct
import threading
import time
import tkinter as tk
import urllib.parse
import uuid
import warnings
import zipfile
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from io import BytesIO
from tkinter import filedialog, ttk
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

# ── Third-party ───────────────────────────────────────────────────────────────
try:
    import websocket
except ImportError:
    raise ImportError(
        "websocket-client is required.\n"
        "Install:  pip install websocket-client"
    )

try:
    from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except ImportError:
    raise ImportError(
        "beautifulsoup4 and lxml are required.\n"
        "Install:  pip install beautifulsoup4 lxml"
    )

try:
    import tkinterdnd2
    HAS_DND = True
except ImportError:
    HAS_DND = False


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


_VOICE_CACHE_TTL = 86400  # 24 hours


def _voice_cache_path():
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return None
    return os.path.join(appdata, "EPUB_TTS_Converter", "voices_cache.json")


def _load_voice_cache():
    path = _voice_cache_path()
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            cache = json.load(f)
        if time.time() - cache.get("_cached_at", 0) < _VOICE_CACHE_TTL:
            return cache.get("voices")
    except Exception:
        pass
    return None


def _save_voice_cache(voices_json):
    path = _voice_cache_path()
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"_cached_at": time.time(), "voices": voices_json}, f)
    except Exception:
        pass


def fetch_voices():
    """Fetch the full voice list from Microsoft. Returns OrderedDict or None."""
    cached = _load_voice_cache()
    if cached:
        return _build_voice_menu(cached)

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
            _save_voice_cache(data)
            return _build_voice_menu(data)
    except Exception:
        pass
    return None


# ═════════════════════════════════════════════════════════════════════════════
#  Section 2 — Edge TTS token generation
# ═════════════════════════════════════════════════════════════════════════════

_clock_skew = 0.0
_clock_skew_lock = threading.Lock()


def _get_unix_timestamp():
    return datetime.now(timezone.utc).timestamp() + _clock_skew


# Candidate skews to walk through on repeated 403s.  The local clock may be
# either behind or ahead of the server, so alternate directions.
_SKEW_STEPS = [300.0, -300.0, 600.0, -600.0, 900.0, -900.0,
               1200.0, -1200.0, 1500.0, -1500.0]


def _advance_clock_skew():
    global _clock_skew
    with _clock_skew_lock:
        try:
            idx = _SKEW_STEPS.index(_clock_skew)
        except ValueError:
            idx = -1
        _clock_skew = _SKEW_STEPS[(idx + 1) % len(_SKEW_STEPS)]


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
    m = re.match(r"^([a-z]{2,})-([A-Z]{2,})-(.+)$", short_name)
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


def _voice_locale(short_name):
    m = re.match(r"^([a-z]{2,})-([A-Z]{2,})", short_name)
    return f"{m.group(1)}-{m.group(2)}" if m else "en-US"


def make_ssml(voice, text, rate="+0%", volume="+0%", pitch="+0Hz"):
    v = normalise_voice(voice)
    lang = _voice_locale(voice)
    return (
        "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis'"
        f" xml:lang='{lang}'>"
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


# ═════════════════════════════════════════════════════════════════════════════
#  Section 3b — TTS provider selection (Edge online  /  Kokoro local)
# ═════════════════════════════════════════════════════════════════════════════

# Kokoro-FastAPI returns CBR 128 kbps / 24 kHz mono MP3, so the byte->ms timing
# the SMIL builder relies on works exactly like Edge's, just with a different
# bytes-per-ms constant.
KOKORO_DEFAULT_URL  = "http://localhost:8880"
KOKORO_BYTES_PER_MS = 16.0   # 128 kbps
KOKORO_BITRATE_KBPS = 128

# Largest text (in characters) sent to the TTS engine in one request.  Long
# paragraphs are split into pieces below this size before synthesis.  This keeps
# Edge requests under its ~10-minute-per-request ceiling (which otherwise causes
# 60s timeouts) and keeps Kokoro's GPU memory bounded (the caching allocator's
# high-water mark tracks the largest request).
MAX_TTS_CHARS = 1500

# Process-wide backend selection, set by the GUI before a conversion starts via
# set_tts_backend().  Kept as module state so the existing call chain (which
# threads voice/rate/etc. through many functions) does not need a new argument
# at every layer.
_tts_backend = {"provider": "edge", "kokoro_url": KOKORO_DEFAULT_URL}


def set_tts_backend(provider, kokoro_url=None):
    """Select the active TTS provider: "edge" or "kokoro"."""
    _tts_backend["provider"] = provider if provider in ("edge", "kokoro") else "edge"
    if kokoro_url:
        _tts_backend["kokoro_url"] = kokoro_url.rstrip("/")


def get_tts_provider():
    return _tts_backend["provider"]


def _rate_to_speed(rate):
    """Map an Edge-style rate string ("+0%", "-25%", "+50%") to a Kokoro speed.

    Edge expresses rate as a percentage delta; Kokoro uses a multiplier where
    1.0 is normal.  The GUI slider is limited to +/-50%, so clamp accordingly.
    """
    m = re.match(r"^([+-]?\d+)\s*%?$", str(rate).strip())
    pct = int(m.group(1)) if m else 0
    return max(0.5, min(2.0, 1.0 + pct / 100.0))


def _resolve_bytes_per_ms(audio_format):
    """Bytes-per-ms for the active provider/format (for byte-based duration)."""
    if _tts_backend["provider"] == "kokoro":
        return KOKORO_BYTES_PER_MS
    for _, (fmt, bpm, _kbps) in AUDIO_FORMATS.items():
        if fmt == audio_format:
            return bpm
    return 6.0

# ═════════════════════════════════════════════════════════════════════════════
#  Section 4 — Text sanitizer & splitter
# ═════════════════════════════════════════════════════════════════════════════

_SSML_DROP_CHARS = set("\u200b\u200c\u200d\u200e\u200f\ufeff\u00ad\u2060")
_SSML_REPLACEMENTS = {
    "\u2018": "'", "\u2019": "'",
    "\u201c": '"', "\u201d": '"',
    "\u00ab": '"', "\u00bb": '"',
    "\u2039": "'", "\u203a": "'",
    "\u2014": " - ", "\u2013": " - ",
    "\u2010": "-", "\u2011": "-",
    "\u2012": "-", "\u2015": " - ",
    "\u2026": "...",
    "\u00b2": "2", "\u00b3": "3",
    "\u2032": "'", "\u2033": '"',
    "\u00a0": " ",
}
_SSML_SPACE_RE = re.compile(r'[\u2000-\u200a\u202f\u205f]')
_SSML_CONTROL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]')


def _sanitize_for_ssml_with_map(text):
    """Return sanitized text and a per-character map back to *text* indexes."""
    out = []
    mapping = []

    for idx, ch in enumerate(text):
        if ch in _SSML_DROP_CHARS or _SSML_CONTROL_RE.match(ch):
            continue
        repl = _SSML_REPLACEMENTS.get(ch)
        if repl is None:
            repl = " " if _SSML_SPACE_RE.match(ch) else ch
        for repl_ch in repl:
            out.append(repl_ch)
            mapping.append(idx)

    collapsed = []
    collapsed_map = []
    prev_space = False
    for ch, src_idx in zip(out, mapping):
        if ch == " ":
            if prev_space:
                continue
            prev_space = True
        else:
            prev_space = False
        collapsed.append(ch)
        collapsed_map.append(src_idx)

    return "".join(collapsed), collapsed_map


def _sanitize_for_ssml(text):
    return _sanitize_for_ssml_with_map(text)[0]


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
    words = s.split()
    return (len(alpha) >= 3 and alpha == alpha.upper()
            and len(s) < 80 and len(words) <= 10)


def _clean_header(line):
    return re.sub(r'^#{1,4}\s+', '', line.strip())


def _split_long_body(text, max_chars):
    parts = []

    def _emit(chunk):
        # Hard-split on word (then character) boundaries so no emitted part
        # ever exceeds max_chars, even for a single run-on sentence.
        while len(chunk) > max_chars:
            split_at = chunk.rfind(' ', 0, max_chars)
            if split_at <= 0:
                split_at = max_chars
            parts.append(chunk[:split_at].strip())
            chunk = chunk[split_at:].strip()
        if chunk:
            parts.append(chunk)

    sentences = re.split(r'(?<=[.!?])\s+', text)
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
            _emit(current)
            current = sent
    if current:
        _emit(current)
    return parts


@functools.lru_cache(maxsize=32)
def generate_mp3_silence(duration_ms, bitrate_kbps=48):
    """Generate silent MP3 data matching Edge TTS output format.

    Format: MPEG-2 Layer III, 24 kHz, mono.
    Frame size = 72 * bitrate / samplerate (MPEG-2, not MPEG-1's 144).
    This ensures bytes_to_ms(len(silence), bytes_per_ms) == actual duration.
    """
    # MPEG-2 Layer III headers for supported bitrates at 24 kHz mono
    _HEADERS = {
        48:  b'\xff\xf3\x64\xc0',  # 48 kbps
        96:  b'\xff\xf3\x94\xc0',  # 96 kbps
        128: b'\xff\xf3\xc4\xc0',  # 128 kbps (Kokoro output rate)
    }
    header = _HEADERS.get(bitrate_kbps, _HEADERS[48])
    frame_size = (72 * bitrate_kbps * 1000) // 24000
    frame = header + b'\x00' * (frame_size - len(header))
    # Each frame = 576 samples at 24 kHz = 24 ms
    ms_per_frame = 24.0
    num_frames = max(1, round(duration_ms / ms_per_frame))
    return frame * num_frames


def split_text_for_tts(text, max_chars=1500,
                       para_pause_ms=None, section_pause_ms=None):
    if not text or not text.strip():
        return []

    para_pause = para_pause_ms if para_pause_ms is not None else SILENCE_PARA_MS
    section_pause = section_pause_ms if section_pause_ms is not None else SILENCE_SECTION_MS

    text = _sanitize_for_ssml(text)
    text = text.replace('\r\n', '\n').replace('\r', '\n')

    # A "paragraph" is a blank-line-separated block; hard-wrapped lines inside
    # a block are merged so line breaks in wrapped text files don't cause
    # pauses mid-sentence.  Header lines split their block.
    segments = []
    for block in re.split(r'\n\s*\n', text):
        block = block.strip()
        if not block:
            continue
        body_lines = []
        for line in block.split('\n'):
            line = line.strip()
            if not line:
                continue
            if _is_header(line):
                if body_lines:
                    segments.append(('body', ' '.join(body_lines)))
                    body_lines = []
                segments.append(('header', _clean_header(line)))
            else:
                body_lines.append(re.sub(r'\s+', ' ', line))
        if body_lines:
            segments.append(('body', ' '.join(body_lines)))

    if not segments:
        return []

    items = []

    def _append_pause(ms):
        if ms > 0 and items and not isinstance(items[-1], tuple):
            items.append((_SILENCE, ms))

    for stype, stext in segments:
        if stype == 'header':
            _append_pause(section_pause)
            items.append(stext.rstrip('.') + '.')
            _append_pause(para_pause)
            continue

        _append_pause(para_pause)
        if len(stext) > max_chars:
            items.extend(_split_long_body(stext, max_chars))
        elif (para_pause <= 0 and items and isinstance(items[-1], str)
              and len(items[-1]) + 1 + len(stext) <= max_chars):
            # No pause requested: merge small paragraphs into one TTS request.
            items[-1] = items[-1] + " " + stext
        else:
            items.append(stext)

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
            if _tts_backend["provider"] == "kokoro":
                return _kokoro_once(text, voice, rate, word_boundary)
            # A skew that produced a working token is kept for later requests.
            return _synthesize_once(text, voice, rate, volume, pitch,
                                    audio_format, word_boundary)
        except Exception as e:
            last_error = e
            err_str = str(e)
            if "403" in err_str and attempt < retries:
                _advance_clock_skew()
            if attempt < retries:
                if "No audio received" in err_str:
                    time.sleep(3 + attempt * 4)
                else:
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
    if not done.wait(timeout=60):
        ws.close()
        raise TimeoutError("Edge TTS synthesis timed out after 60 seconds")

    if error_box[0]:
        raise ConnectionError(f"Edge TTS: {error_box[0]}")
    result = audio_data.getvalue()
    if not result:
        raise RuntimeError("No audio received")
    if word_boundary:
        return result, boundaries
    return result


# ═════════════════════════════════════════════════════════════════════════════
#  Section 5a — Kokoro (local) synthesis
# ═════════════════════════════════════════════════════════════════════════════

def _strip_id3v2(mp3):
    """Drop a leading ID3v2 tag if present.

    ffmpeg-encoded MP3s (Kokoro) may start with an ID3 tag; those bytes play
    as nothing but would be counted by the byte->ms duration estimate,
    drifting SMIL timings when chunks are concatenated.
    """
    if mp3[:3] == b"ID3" and len(mp3) >= 10:
        size = ((mp3[6] & 0x7F) << 21 | (mp3[7] & 0x7F) << 14
                | (mp3[8] & 0x7F) << 7 | (mp3[9] & 0x7F))
        return mp3[10 + size:]
    return mp3


def _kokoro_once(text, voice, rate, word_boundary):
    """Synthesize one chunk via a local Kokoro-FastAPI server.

    Mirrors _synthesize_once's contract: returns raw MP3 bytes, or
    (mp3_bytes, boundaries) when word_boundary is True.  Word timestamps come
    from the /dev/captioned_speech endpoint and are converted to the same
    boundary shape Edge produces so inject_word_spans/build_smil are unchanged.
    """
    base = _tts_backend["kokoro_url"]
    speed = _rate_to_speed(rate)

    # Work around Kokoro-FastAPI issue #249: a run of 3+ hyphens makes the
    # captioned endpoint silently drop every timestamp after it.  Collapsing to
    # a single spaced dash keeps the narration natural and the timing intact.
    payload_text = re.sub(r"-{3,}", " - ", text)

    path = "/dev/captioned_speech" if word_boundary else "/v1/audio/speech"
    payload = {
        "model": "kokoro",
        "input": payload_text,
        "voice": voice,
        "response_format": "mp3",
        "speed": speed,
        "stream": False,
    }
    if word_boundary:
        payload["return_timestamps"] = True

    req = Request(
        f"{base}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(req, timeout=120) as resp:
        body = resp.read()

    if not word_boundary:
        if not body:
            raise RuntimeError("No audio received")
        return _strip_id3v2(body)

    meta = json.loads(body.decode("utf-8"))
    audio = _strip_id3v2(base64.b64decode(meta.get("audio") or ""))
    if not audio:
        raise RuntimeError("No audio received")

    boundaries = []
    for w in meta.get("timestamps") or []:
        word = str(w.get("word", "")).strip()
        if not word or not re.search(r"\w", word):
            continue  # skip pure-punctuation tokens, matching Edge behaviour
        start_ms = float(w.get("start_time", 0.0)) * 1000.0
        end_ms = float(w.get("end_time", w.get("start_time", 0.0))) * 1000.0
        boundaries.append({
            "type": "wordboundary",
            "offset": start_ms,
            "duration": max(0.0, end_ms - start_ms),
            "text": word,
        })
    return audio, boundaries


def fetch_kokoro_voices(base_url=None):
    """Return OrderedDict 'label -> voice_id' from a Kokoro server, or None."""
    base = (base_url or _tts_backend["kokoro_url"]).rstrip("/")
    _LANG = {"a": "US English", "b": "British", "e": "Spanish", "f": "French",
             "h": "Hindi", "i": "Italian", "j": "Japanese", "p": "Portuguese",
             "z": "Chinese"}
    _GENDER = {"f": "Female", "m": "Male"}
    try:
        req = Request(f"{base}/v1/audio/voices",
                      headers={"Accept": "application/json"})
        with urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        voices = data.get("voices") if isinstance(data, dict) else data
        if not voices:
            return None
        menu = OrderedDict()
        for vid in sorted(voices):
            lang = _LANG.get(vid[:1], "")
            gender = _GENDER.get(vid[1:2], "")
            tag = ", ".join(p for p in (lang, gender) if p)
            menu[f"{vid}  ({tag})" if tag else vid] = vid
        return menu
    except Exception:
        return None


# Used when the Kokoro server can't be reached at startup.
KOKORO_FALLBACK_VOICES = OrderedDict([
    ("af_heart  (US English, Female)",   "af_heart"),
    ("af_bella  (US English, Female)",   "af_bella"),
    ("af_nicole  (US English, Female)",  "af_nicole"),
    ("am_michael  (US English, Male)",   "am_michael"),
    ("am_puck  (US English, Male)",      "am_puck"),
    ("bf_emma  (British, Female)",       "bf_emma"),
    ("bm_george  (British, Male)",       "bm_george"),
])


# ═════════════════════════════════════════════════════════════════════════════
#  Section 5b — EPUB processing
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
    href = href.split("#", 1)[0]
    href = urllib.parse.unquote(href)
    path = posixpath.join(opf_dir, href) if opf_dir else href
    return posixpath.normpath(path)


def resolve_spine_chapters(manifest, spine, opf_dir):
    """Resolve spine itemrefs to a deduplicated list of (idref, href) chapters.

    Itemrefs that are missing from the manifest or are not document content are
    skipped.  Critically, entries that resolve to a file already seen are also
    skipped: some EPUBs (especially PDF/MOBI conversions from sources like
    Anna's Archive) repeat the same itemref dozens of times — e.g. one chapter
    file referenced 14x — which would otherwise make us synthesize the same
    text over and over, inflating both runtime and output size many-fold.

    Returns (chapters, skipped_duplicate_count).
    """
    chapters = []
    seen_files = set()
    skipped = 0
    for idref in spine:
        item = manifest.get(idref)
        if item is None or item["media-type"] not in CONTENT_TYPES:
            continue
        file_key = epub_path(opf_dir, item["href"])
        if file_key in seen_files:
            skipped += 1
            continue
        seen_files.add(file_key)
        chapters.append((idref, item["href"]))
    return chapters, skipped


def opf_href_from_zip_path(opf_dir: str, zip_path: str) -> str:
    href = posixpath.relpath(zip_path, opf_dir) if opf_dir else zip_path
    return urllib.parse.quote(href, safe="/#%:@?&=+$,;~!*'()")


def _generate_nav_xhtml(chapters_info):
    """Generate a minimal EPUB3 nav document.

    chapters_info: list of {"href": str, "title": str} dicts.
    Returns UTF-8 bytes of nav.xhtml.
    """
    items = []
    for ch in chapters_info:
        href = escape(ch["href"])
        title = escape(ch["title"])
        items.append(f'        <li><a href="{href}">{title}</a></li>')
    li_block = "\n".join(items)
    xhtml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"'
        ' xmlns:epub="http://www.idpf.org/2007/ops">\n'
        '<head><title>Table of Contents</title></head>\n'
        '<body>\n'
        '  <nav epub:type="toc" id="toc">\n'
        '    <h1>Table of Contents</h1>\n'
        '    <ol>\n'
        f'{li_block}\n'
        '    </ol>\n'
        '  </nav>\n'
        '</body>\n'
        '</html>\n'
    )
    return xhtml.encode("utf-8")


def _nav_info_from_ncx(zf: zipfile.ZipFile, manifest, opf_dir: str):
    ncx_href = None
    for item in manifest.values():
        if item["media-type"] == "application/x-dtbncx+xml":
            ncx_href = item["href"]
            break
    if not ncx_href:
        return []

    ncx_zip_path = epub_path(opf_dir, ncx_href)
    try:
        root = ET.fromstring(zf.read(ncx_zip_path))
    except Exception:
        return []

    ncx_dir = posixpath.dirname(ncx_zip_path)
    chapters = []
    for nav_point in root.findall(f".//{{{NCX_NS}}}navPoint"):
        text_el = nav_point.find(f".//{{{NCX_NS}}}navLabel/{{{NCX_NS}}}text")
        content_el = nav_point.find(f"{{{NCX_NS}}}content")
        if content_el is None:
            continue
        src = content_el.get("src", "")
        if not src:
            continue
        src_path = urllib.parse.unquote(src.split("#", 1)[0])
        zip_path = posixpath.normpath(posixpath.join(ncx_dir, src_path))
        href = opf_href_from_zip_path(opf_dir, zip_path)
        title = "".join(text_el.itertext()).strip() if text_el is not None else href
        chapters.append({"href": href, "title": title or href})
    return chapters


# Based on W3C EPUB 3 Structural Semantics Vocabulary 1.1
# https://www.w3.org/TR/epub-ssv-11/

_BACKMATTER_EPUB_TYPES = {
    "endnotes", "footnotes", "rearnotes", "notes",
    "bibliography", "biblioentry",
    "appendix", "index", "glossary", "colophon",
    "credits",
}

_BACKMATTER_FILENAME_RE = re.compile(
    r'(?:notes?|endnotes?|footnotes?|bibliograph\w*|appendix|glossary'
    r'|references?|refere|referencenote'
    r'|colophon|index|errata|credits?'
    r'|list.?(?:cited|illus|tables?|figures?|maps?|charts?|abbreviat)'
    r'|works?.?cited|sources|permissions)'
    r'(?:[a-z]?\d*)?(?:[_.\s\-]|$)',
    re.IGNORECASE,
)

_BACKMATTER_HEADING_RE = re.compile(
    r'^\s*(?:notes?|endnotes?|footnotes?|bibliography|references?'
    r'|reference\s+notes?'
    r'|appendix|ind(?:ex|ices)|glossary|colophon|credits?|errata'
    r'|works?\s+cited|further\s+reading|sources?|permissions?'
    r'|list\s+of\s+(?:illustrations?|figures?|tables?|maps?'
    r'|charts?|plates?|abbreviat\w*|contrib\w*)'
    r'|(?:list\s+of\s+)?tables?\s+and\s+illustrations?'
    r'|abbreviations?\s*(?:and\s+\w+\s+terms)?'
    r'|acronyms?|comparative\s+ranks?'
    r'|selected\s+bibliograph\w*'
    r'|suggestions?\s+for\s+further\s+reading'
    r'|(?:photo|image|picture)\s+credits?)\s*\d*\s*$',
    re.IGNORECASE,
)

_NOT_BACKMATTER_HEADING_RE = re.compile(
    r'^\s*(?:epilogue|afterword|foreword|preface|introduction'
    r'|prologue|conclusion|postscript|summary|overview)',
    re.IGNORECASE,
)

_BACKMATTER_EPUBTYPE_RE = re.compile(
    r'epub:type\s*=\s*["\']([^"\']*(?:'
    + "|".join(_BACKMATTER_EPUB_TYPES)
    + r')[^"\']*)["\']',
    re.IGNORECASE,
)


def _is_backmatter(html_bytes: bytes, href: str) -> bool:
    """Heuristic detection of back matter sections (notes, bibliography, etc.).

    Epilogues, afterwords, and other narrative content that happens to live
    in the EPUB "backmatter" structural region are NOT flagged.
    """
    basename = posixpath.basename(href).lower()
    stripped = re.sub(r'^[\d_]+', '', basename)

    if _NOT_BACKMATTER_HEADING_RE.match(stripped):
        return False

    if _BACKMATTER_FILENAME_RE.match(stripped):
        return True

    html_str = html_bytes[:16384].decode("utf-8", errors="replace")
    if _BACKMATTER_EPUBTYPE_RE.search(html_str):
        return True

    soup = BeautifulSoup(html_bytes[:16384], "lxml")

    for sec in soup.find_all(["section", "body"], limit=3):
        sec_title = sec.get("title", "")
        if sec_title and _BACKMATTER_HEADING_RE.match(sec_title.strip()):
            return True

    for h in soup.find_all(["h1", "h2", "h3"], limit=2):
        heading_text = h.get_text(strip=True)
        if _NOT_BACKMATTER_HEADING_RE.match(heading_text):
            return False
        if _BACKMATTER_HEADING_RE.match(heading_text):
            return True

    return False


_FOOTNOTE_FILE_RE = re.compile(
    r'(?:n?fn\d|footnote|endnote)', re.IGNORECASE,
)


def _is_footnote_file(href: str) -> bool:
    """Detect individual footnote/endnote files (e.g. ch01fn3.html)."""
    basename = posixpath.basename(href).lower()
    return bool(_FOOTNOTE_FILE_RE.search(basename))


_FRONTMATTER_EPUB_TYPES = {
    "cover", "titlepage", "title-page", "copyright-page",
    "halftitlepage", "dedication", "seriespage",
    "imprint", "imprimatur", "contributors", "other-credits",
    "toc", "toc-brief", "landmarks",
    "loa", "loi", "lot", "lov", "page-list",
}

_FRONTMATTER_FILENAME_RE = re.compile(
    r'^(?:cover|titlepage|title|copyright|cop(?:y|yright)?'
    r'|dedication|toc|contents'
    r'|halftitle|half.title|frontmatter|seriespage|imprint'
    r'|contributors|nav|praise|about.the|also.?by|cr)(?:[_.\s\-\d]|$)',
    re.IGNORECASE,
)

_FRONTMATTER_HEADING_RE = re.compile(
    r'^\s*(?:table\s+of\s+contents|contents|copyright'
    r'|also\s+by\s*\w*|books?\s+by|other\s+books'
    r'|about\s+the\s+(?:author|book|publisher|translator)'
    r'|praise\s+for|praise)\s*$',
    re.IGNORECASE,
)

_NOT_FRONTMATTER_FILENAME_RE = re.compile(
    r'^(?:introduction|preface|foreword|prologue|part\d*|chapter|ch\d'
    r'|acknowledgments?)',
    re.IGNORECASE,
)

_FRONTMATTER_ANYWHERE_RE = re.compile(
    r'(?:^|[_\-])(?:copyright\w*|copy[_.\-\d]|cop[_.\-\d]'
    r'|about[_.\-]|also.?by|praise|cr[_.\-\d])',
    re.IGNORECASE,
)


def _is_frontmatter(html_bytes: bytes, href: str) -> bool:
    """Detect front matter (cover, title page, copyright, TOC, dedication).

    Content sections (introduction, preface, foreword) that happen to be
    inside an EPUB 'frontmatter' structural container are NOT flagged.
    """
    basename = posixpath.basename(href).lower()
    stripped = re.sub(r'^[\d_]+', '', basename)

    if _FRONTMATTER_ANYWHERE_RE.search(basename):
        return True

    if _NOT_FRONTMATTER_FILENAME_RE.match(stripped):
        return False

    if _FRONTMATTER_FILENAME_RE.match(stripped):
        return True

    html_str = html_bytes[:8192].decode("utf-8", errors="replace")
    for etype in _FRONTMATTER_EPUB_TYPES:
        if re.search(
            r'epub:type\s*=\s*["\'][^"\']*\b' + etype + r'\b[^"\']*["\']',
            html_str, re.IGNORECASE,
        ):
            return True

    soup = BeautifulSoup(html_bytes[:8192], "lxml")

    nav = soup.find("nav", attrs={"epub:type": re.compile(r"\btoc\b")})
    if nav:
        return True

    links = soup.find_all("a")
    all_text = soup.get_text(strip=True)
    if links and all_text:
        link_text = sum(len(a.get_text(strip=True)) for a in links)
        if link_text > len(all_text) * 0.7 and len(links) > 10:
            return True

    for h in soup.find_all(["h1", "h2", "h3"], limit=2):
        if _FRONTMATTER_HEADING_RE.match(h.get_text(strip=True)):
            return True

    return False


_VOID_ELEMENTS_RE = re.compile(
    rb'<(meta|link|br|hr|img|input|col|area|base|source|embed|track|wbr)'
    rb'(\s[^>]*?)?(?<!/)\s*>',
)


def _to_xhtml_bytes(soup):
    """Encode a BeautifulSoup tree as XHTML-compliant UTF-8 bytes."""
    raw = soup.encode("utf-8", formatter="minimal")
    return _VOID_ELEMENTS_RE.sub(rb'<\1\2/>', raw)


def _is_tts_skipped_tag(tag):
    epub_type = tag.get("epub:type", "")
    if re.search(r"(?:^|\s)(?:noteref|pagebreak)(?:\s|$)", epub_type):
        return True
    if tag.name == "sup":
        sup_text = tag.get_text(strip=True)
        return (
            len(sup_text) <= 3
            and re.match(r'^[\d*\u2020\u2021\u00a7]+$', sup_text)
        )
    if tag.name == "a":
        a_text = tag.get_text(strip=True)
        return re.match(r'^\d{1,4}$', a_text) and tag.get("href", "").startswith("#")
    return False


def _node_is_tts_skipped(node, root):
    parent = getattr(node, "parent", None)
    while parent is not None:
        if _is_tts_skipped_tag(parent):
            return True
        if parent is root:
            return False
        parent = getattr(parent, "parent", None)
    return False


def _append_mapped_text(out, mapping, text, text_map):
    for ch, loc in zip(text, text_map):
        if ch.isspace():
            if out and out[-1] == " ":
                continue
            out.append(" ")
            mapping.append(loc)
        else:
            out.append(ch)
            mapping.append(loc)


def _display_text_and_map(tag):
    """Return visible TTS text plus a map back to the original text nodes."""
    from bs4 import NavigableString

    out = []
    mapping = []
    for node in tag.descendants:
        if getattr(node, "name", None) == "br":
            _append_mapped_text(out, mapping, " ", [None])
            continue
        if not isinstance(node, NavigableString):
            continue
        if _node_is_tts_skipped(node, tag):
            continue
        raw = str(node)
        raw_map = [(node, i) for i in range(len(raw))]
        _append_mapped_text(out, mapping, raw, raw_map)

    while out and out[0] == " ":
        out.pop(0)
        mapping.pop(0)
    while out and out[-1] == " ":
        out.pop()
        mapping.pop()
    return "".join(out), mapping


def _strip_footnote_refs(tag):
    """Return text from *tag* with footnote/citation markers and page breaks removed."""
    return _display_text_and_map(tag)[0]


_PRIMARY_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p"}
_FALLBACK_TAGS = {"div", "blockquote", "li", "section", "article", "dd", "dt"}


def _paragraph_record(tag, ch_idx, counter):
    display_text, display_map = _display_text_and_map(tag)
    if len(display_text) < 5:
        return None
    tts_text, tts_map = _sanitize_for_ssml_with_map(display_text)
    pid = f"mo_ch{ch_idx:03d}p{counter:04d}"
    if not tag.get("id"):
        tag["id"] = pid
    return {
        "id": tag["id"],
        "text": display_text,
        "display_text": display_text,
        "display_map": display_map,
        "tts_text": tts_text,
        "tts_map": tts_map,
        "tag": tag,
    }


def _has_direct_text(tag):
    """True if *tag* contains meaningful text not already inside a child block."""
    from bs4 import NavigableString
    for child in tag.children:
        if isinstance(child, NavigableString) and child.strip():
            return True
    return False


def process_chapter(html_bytes: bytes, ch_idx: int):
    """Parse a chapter, inject IDs on paragraphs, extract text.

    Returns (soup, paragraphs) where paragraphs is a list of
    {"id": str, "text": str, "tag": BeautifulSoup Tag (for word/sentence injection)}.

    First pass collects standard block elements (h1-h6, p).  If that yields
    nothing, a second pass looks for content in div/blockquote/li/section/etc.,
    filtering out purely structural wrappers.
    """
    soup = BeautifulSoup(html_bytes, "lxml")
    paragraphs = []
    counter = 1
    seen_tags = set()

    for tag in soup.find_all(list(_PRIMARY_TAGS)):
        para = _paragraph_record(tag, ch_idx, counter)
        if para is None:
            continue
        paragraphs.append(para)
        seen_tags.add(id(tag))
        counter += 1

    if not paragraphs:
        for tag in soup.find_all(list(_FALLBACK_TAGS)):
            # find_all is document order, so an accepted ancestor (e.g. a div
            # with direct text AND nested blocks) already covers this tag's
            # text; recording it again would narrate it twice.
            if any(id(anc) in seen_tags for anc in tag.parents):
                continue
            if not _has_direct_text(tag):
                child_blocks = tag.find_all(
                    list(_PRIMARY_TAGS | _FALLBACK_TAGS), recursive=False)
                if child_blocks:
                    continue
            para = _paragraph_record(tag, ch_idx, counter)
            if para is None:
                continue
            paragraphs.append(para)
            seen_tags.add(id(tag))
            counter += 1

    return soup, paragraphs


def inject_word_spans(soup, para, boundaries, ch_idx, para_idx):
    """Map Edge word-boundary metadata back onto the source XHTML.

    Edge receives sanitized text, so its word metadata is aligned against the
    sanitized TTS string first.  Mapped words wrap the original visible text.
    Tokens that cannot be shown safely still get empty sync markers so their
    audio clips stay represented in the SMIL overlay.
    """
    from bs4 import NavigableString

    def _find_token(haystack, token, start):
        candidates = []
        for candidate in (token, _sanitize_for_ssml(token)):
            if candidate and candidate not in candidates:
                candidates.append(candidate)
        for candidate in candidates:
            idx = haystack.find(candidate, start)
            if idx >= 0:
                return idx, idx + len(candidate)
        lower = haystack.lower()
        for candidate in candidates:
            idx = lower.find(candidate.lower(), start)
            if idx >= 0:
                return idx, idx + len(candidate)
        return None, None

    def _display_index_for_tts_pos(pos):
        if not tts_map:
            return None
        pos = min(max(pos, 0), len(tts_map) - 1)
        for idx in range(pos, len(tts_map)):
            if tts_map[idx] is not None:
                return tts_map[idx]
        for idx in range(pos, -1, -1):
            if tts_map[idx] is not None:
                return tts_map[idx]
        return None

    def _marker_record(sid, wb, tts_pos):
        return {
            "id": sid,
            "offset": wb["offset"],
            "duration": wb["duration"],
            "marker": True,
            "display_pos": _display_index_for_tts_pos(tts_pos),
        }

    def _add_node_op(loc, op):
        if loc is None:
            tail_markers.append(op["id"])
            return
        node, offset = loc
        key = id(node)
        if key not in node_ops:
            node_ops[key] = {"node": node, "ops": []}
            node_order.append(key)
        op["offset"] = offset
        node_ops[key]["ops"].append(op)

    def _location_for_display_index(display_idx):
        if display_idx is None or not display_map:
            return None
        display_idx = min(max(display_idx, 0), len(display_map) - 1)
        for idx in range(display_idx, len(display_map)):
            if display_map[idx] is not None:
                return display_map[idx]
        for idx in range(display_idx, -1, -1):
            if display_map[idx] is not None:
                return display_map[idx]
        return None

    tag = para["tag"]
    original = para.get("display_text", para["text"])
    display_map = para.get("display_map")
    tts_text = para.get("tts_text")
    tts_map = para.get("tts_map")
    if tts_text is None or tts_map is None:
        tts_text, tts_map = _sanitize_for_ssml_with_map(original)

    word_bounds = [b for b in boundaries if b["type"] == "wordboundary" and b["text"]]

    if not word_bounds:
        return []

    if display_map is None:
        _, display_map = _display_text_and_map(tag)

    span_records = []
    node_ops = {}
    node_order = []
    tail_markers = []
    search_pos = 0

    for wi, wb in enumerate(word_bounds):
        sid = f"mo_ch{ch_idx:03d}p{para_idx:04d}w{wi+1:04d}"
        idx, word_end = _find_token(tts_text, wb["text"], search_pos)
        if idx is None:
            record = _marker_record(sid, wb, search_pos)
            span_records.append(record)
            continue

        while word_end < len(tts_text) and tts_text[word_end] in ".,;:!?\"')]-\u2019\u201d":
            word_end += 1

        mapped = [m for m in tts_map[idx:word_end] if m is not None]
        if not mapped:
            record = _marker_record(sid, wb, idx)
            span_records.append(record)
            search_pos = word_end
            continue

        display_start = min(mapped)
        display_end = max(mapped) + 1
        locs = [
            display_map[i]
            for i in range(display_start, min(display_end, len(display_map)))
            if display_map[i] is not None
        ]
        if not locs:
            record = _marker_record(sid, wb, idx)
            span_records.append(record)
            search_pos = word_end
            continue

        nodes = {id(node) for node, _ in locs}
        if len(nodes) == 1:
            node = locs[0][0]
            offsets = [offset for _, offset in locs]
            record = {
                "id": sid,
                "offset": wb["offset"],
                "duration": wb["duration"],
                "marker": False,
                "node": node,
                "start": min(offsets),
                "end": max(offsets) + 1,
            }
        else:
            record = _marker_record(sid, wb, idx)

        span_records.append(record)
        search_pos = word_end

    for record in span_records:
        if record.get("marker"):
            loc = _location_for_display_index(record.get("display_pos"))
            _add_node_op(loc, {"kind": "marker", "id": record["id"]})
        else:
            key = id(record["node"])
            if key not in node_ops:
                node_ops[key] = {"node": record["node"], "ops": []}
                node_order.append(key)
            node_ops[key]["ops"].append({
                "kind": "wrap",
                "id": record["id"],
                "start": record["start"],
                "end": record["end"],
            })

    for key in reversed(node_order):
        node = node_ops[key]["node"]
        raw = str(node)
        ops = node_ops[key]["ops"]
        ops.sort(key=lambda op: (
            op.get("start", op.get("offset", 0)),
            0 if op["kind"] == "marker" else 1,
        ))
        fragments = []
        pos = 0

        for op in ops:
            if op["kind"] == "marker":
                offset = min(max(op["offset"], 0), len(raw))
                if offset > pos:
                    fragments.append(NavigableString(raw[pos:offset]))
                    pos = offset
                span_el = soup.new_tag("span", id=op["id"])
                fragments.append(span_el)
                continue

            start = min(max(op["start"], 0), len(raw))
            end = min(max(op["end"], start), len(raw))
            if start < pos or start == end:
                # Overlapping or empty wrap: fall back to an empty sync marker
                # so the SMIL par for this word still has an XHTML target.
                fragments.append(soup.new_tag("span", id=op["id"]))
                continue
            if start > pos:
                fragments.append(NavigableString(raw[pos:start]))
            span_el = soup.new_tag("span", id=op["id"])
            span_el.string = raw[start:end]
            fragments.append(span_el)
            pos = end

        if pos < len(raw):
            fragments.append(NavigableString(raw[pos:]))

        if fragments:
            node.replace_with(fragments[0])
            prev = fragments[0]
            for frag in fragments[1:]:
                prev.insert_after(frag)
                prev = frag

    for marker_id in tail_markers:
        tag.append(soup.new_tag("span", id=marker_id))

    return [{
        "id": record["id"],
        "offset": record["offset"],
        "duration": record["duration"],
    } for record in span_records]



_ABBREVIATIONS = {
    "Dr", "Mr", "Mrs", "Ms", "Prof", "Rev", "Jr", "Sr", "St",
    "Gen", "Col", "Maj", "Lt", "Sgt", "Cpl", "Pvt", "Capt", "Cmdr", "Adm",
    "Gov", "Pres", "Spc", "Cdr",
    "Corp", "Inc", "Ltd", "Dept", "Assn", "Bros", "Co",
    "approx", "dept", "est", "govt", "misc",
    "Jan", "Feb", "Mar", "Apr", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    "Ave", "Blvd", "Rd",
    "Vol", "Ch", "Fig", "No", "Nos",
}

_ABBREV_RE = re.compile(
    r'\b(' + '|'.join(re.escape(a) for a in sorted(_ABBREVIATIONS, key=len, reverse=True))
    + r')\.\s',
)

_LATIN_ABBREV_RE = re.compile(
    r'\b(e\.g|i\.e|etc|vs|al|cf|pp|ca|c)\.\s',
    re.IGNORECASE,
)

_PLACEHOLDER = "\x00"


def _split_into_sentences(text):
    """Split text into sentences at .!? boundaries, protecting abbreviations."""
    protected = _ABBREV_RE.sub(lambda m: m.group(1) + "." + _PLACEHOLDER, text)
    protected = _LATIN_ABBREV_RE.sub(
        lambda m: m.group(1) + "." + _PLACEHOLDER, protected)

    parts = re.split(r'(?<=[.!?])\s+(?=[A-Z"\u201c(])', protected)
    if len(parts) <= 1 and len(text) > 200:
        parts = re.split(r'(?<=[.!?\u3002\uff01\uff1f])\s+', protected)

    return [s.replace(_PLACEHOLDER, " ").strip()
            for s in parts if s and s.strip()]


def _synthesize_long(text, voice, rate, audio_format, need_boundaries):
    """Synthesize text that may exceed MAX_TTS_CHARS by splitting into pieces.

    Pieces are cut on sentence boundaries (falling back to word/character
    boundaries) so each request stays small — keeping Edge under its
    per-request audio ceiling and Kokoro's GPU memory bounded.  Audio is
    concatenated and word-boundary offsets shifted to be relative to the
    combined clip, matching synthesize_chunk's return contract.
    """
    pieces = _split_long_body(text, MAX_TTS_CHARS)
    if len(pieces) <= 1:
        return synthesize_chunk(
            text, voice, rate,
            audio_format=audio_format, word_boundary=need_boundaries,
        )

    bytes_per_ms = _resolve_bytes_per_ms(audio_format)
    combined_audio = b""
    combined_bounds = []
    cursor_ms = 0.0
    for piece in pieces:
        result = synthesize_chunk(
            piece, voice, rate,
            audio_format=audio_format, word_boundary=need_boundaries,
        )
        if need_boundaries:
            audio_bytes, bounds = result
        else:
            audio_bytes, bounds = result, []
        for b in bounds:
            combined_bounds.append({
                "type": b["type"],
                "offset": b["offset"] + cursor_ms,
                "duration": b["duration"],
                "text": b["text"],
            })
        combined_audio += audio_bytes
        cursor_ms += bytes_to_ms(len(audio_bytes), bytes_per_ms)

    if need_boundaries:
        return combined_audio, combined_bounds
    return combined_audio


def _synthesize_with_sentence_pauses(text, voice, rate, audio_format,
                                     need_boundaries, sentence_pause_ms,
                                     bitrate_kbps, progress_cb=None):
    """Split paragraph into sentences, synthesize each, add silence between.

    Returns same format as synthesize_chunk: bytes or (bytes, boundaries).
    Word boundary offsets are adjusted to be relative to the combined audio.
    """
    sentences = _split_into_sentences(text)
    if len(sentences) <= 1:
        return synthesize_chunk(
            text, voice, rate,
            audio_format=audio_format, word_boundary=need_boundaries,
        )

    bytes_per_ms = _resolve_bytes_per_ms(audio_format)

    silence = generate_mp3_silence(sentence_pause_ms, bitrate_kbps)

    combined_audio = b""
    combined_bounds = []
    cursor_ms = 0.0

    for si, sent in enumerate(sentences):
        try:
            if len(sent) > MAX_TTS_CHARS:
                result = _synthesize_long(
                    sent, voice, rate, audio_format, need_boundaries)
            else:
                result = synthesize_chunk(
                    sent, voice, rate,
                    audio_format=audio_format, word_boundary=need_boundaries,
                )
        except Exception as e:
            if progress_cb:
                progress_cb(f"    WARNING: sentence {si+1}/{len(sentences)} failed: {e}")
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
                          cancel_event=None, pause_event=None,
                          audio_format="audio-24khz-48kbitrate-mono-mp3",
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
        if pause_event:
            pause_event.wait()
        if cancel_event and cancel_event.is_set():
            if need_boundaries:
                return idx, (b"", [])
            return idx, b""

        if not re.search(r'[a-zA-ZÀ-ɏЀ-ӿ]{2,}', text):
            silence = generate_mp3_silence(SILENCE_SECTION_MS, bitrate_kbps)
            if need_boundaries:
                return idx, (silence, [])
            return idx, silence

        if sentence_pause_ms > 0:
            return idx, _synthesize_with_sentence_pauses(
                text, voice, rate, audio_format, need_boundaries,
                sentence_pause_ms, bitrate_kbps, progress_cb,
            )

        if len(text) > MAX_TTS_CHARS:
            return idx, _synthesize_long(
                text, voice, rate, audio_format, need_boundaries)

        result = synthesize_chunk(
            text, voice, rate,
            audio_format=audio_format, word_boundary=need_boundaries,
        )
        return idx, result

    failed = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_do, i, p.get("tts_text", p["text"])): i
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
                progress_cb(f"    WARNING: paragraph {idx+1} failed ({e})")
                if need_boundaries:
                    result = (b"", [])
                else:
                    result = b""
            results[idx] = result
            done_count += 1
            if done_count % max(1, total // 10) == 0 or done_count == total:
                progress_cb(f"    {done_count}/{total} paragraphs synthesized")

    if failed:
        progress_cb(f"    Retrying {len(failed)} failed paragraph(s)...")
        still_failed = []
        for idx in failed:
            if cancel_event and cancel_event.is_set():
                return None
            if pause_event:
                pause_event.wait()
            time.sleep(2)
            text = paragraphs[idx].get("tts_text", paragraphs[idx]["text"])
            try:
                _, result = _do(idx, text)
                results[idx] = result
                progress_cb(f"    Retry OK: paragraph {idx+1}")
                continue
            except Exception:
                pass

            sentences = _split_into_sentences(text)
            if len(sentences) <= 1:
                still_failed.append(idx)
                progress_cb(f"    Retry failed: paragraph {idx+1}, skipping")
                continue

            progress_cb(
                f"    Paragraph {idx+1}: retrying as {len(sentences)} sentences...")
            parts = []
            skipped_sents = 0
            for si, sent in enumerate(sentences):
                if not re.search(r'[a-zA-ZÀ-ɏЀ-ӿ]{2,}', sent):
                    continue
                try:
                    audio = synthesize_chunk(
                        sent, voice, rate,
                        audio_format=audio_format,
                        word_boundary=need_boundaries,
                    )
                    parts.append(audio)
                except Exception:
                    skipped_sents += 1

            if parts:
                if need_boundaries:
                    bpm = _resolve_bytes_per_ms(audio_format)
                    combined = b""
                    combined_bounds = []
                    cursor_ms = 0.0
                    for p in parts:
                        if isinstance(p, tuple):
                            audio_bytes, bounds = p
                        else:
                            audio_bytes, bounds = p, []
                        for b in bounds:
                            combined_bounds.append({
                                "type": b["type"],
                                "offset": b["offset"] + cursor_ms,
                                "duration": b["duration"],
                                "text": b["text"],
                            })
                        combined += audio_bytes
                        cursor_ms += bytes_to_ms(len(audio_bytes), bpm)
                    results[idx] = (combined, combined_bounds)
                else:
                    results[idx] = b"".join(parts)
                msg = f"    Recovered paragraph {idx+1}"
                if skipped_sents:
                    msg += f" ({skipped_sents} sentence(s) dropped)"
                progress_cb(msg)
            else:
                still_failed.append(idx)
                progress_cb(f"    Retry failed: paragraph {idx+1}, skipping")

        if still_failed:
            progress_cb(
                f"    {len(still_failed)} paragraph(s) failed after retry and were skipped")

    return [results[i] for i in range(total)]


def build_smil(paragraphs, audio_chunks,
               html_zip_path, audio_zip_path, smil_zip_dir,
               bytes_per_ms=6.0, span_data=None):
    """Generate SMIL XML.

    span_data: if provided, a list (one per paragraph) of span record lists.
        Each span record has {"id", "offset", "duration"}.  When present,
        SMIL entries are generated per-span instead of per-paragraph.
    """
    html_rel  = urllib.parse.quote(
        posixpath.relpath(html_zip_path, smil_zip_dir),
        safe="/#%:@?&=+$,;~!*'()",
    )
    audio_rel = urllib.parse.quote(
        posixpath.relpath(audio_zip_path, smil_zip_dir),
        safe="/#%:@?&=+$,;~!*'()",
    )

    indent = "      "

    pars   = []
    cursor = 0.0
    par_counter = 0

    for i, (para, audio) in enumerate(zip(paragraphs, audio_chunks)):
        para_dur = bytes_to_ms(len(audio), bytes_per_ms)

        # A paragraph whose synthesis failed contributes no audio; emitting a
        # par with clipBegin == clipEnd trips up some readers, so skip it.
        if not audio:
            continue

        if span_data and i < len(span_data) and span_data[i]:
            for sp in span_data[i]:
                par_counter += 1
                begin = ms_to_clock(cursor + sp["offset"])
                end   = ms_to_clock(cursor + sp["offset"] + sp["duration"])
                text_src = quoteattr(f"{html_rel}#{sp['id']}")
                audio_src = quoteattr(audio_rel)
                pars.append(
                    f'{indent}<par id="par{par_counter:04d}">\n'
                    f'{indent}  <text src={text_src}/>\n'
                    f'{indent}  <audio src={audio_src}'
                    f' clipBegin="{begin}"'
                    f' clipEnd="{end}"/>\n'
                    f'{indent}</par>'
                )
        else:
            par_counter += 1
            begin = ms_to_clock(cursor)
            end   = ms_to_clock(cursor + para_dur)
            text_src = quoteattr(f"{html_rel}#{para['id']}")
            audio_src = quoteattr(audio_rel)
            pars.append(
                f'{indent}<par id="par{par_counter:04d}">\n'
                f'{indent}  <text src={text_src}/>\n'
                f'{indent}  <audio src={audio_src}'
                f' clipBegin="{begin}"'
                f' clipEnd="{end}"/>\n'
                f'{indent}</par>'
            )

        cursor += para_dur

    seq_textref = quoteattr(html_rel)

    smil = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<smil xmlns="http://www.w3.org/ns/SMIL"\n'
        '      xmlns:epub="http://www.idpf.org/2007/ops"\n'
        '      version="3.0">\n'
        '  <body>\n'
        f'    <seq id="seq001" epub:textref={seq_textref}>\n'
        + "\n".join(pars) + "\n"
        + '    </seq>\n'
          '  </body>\n'
          '</smil>\n'
    )
    return smil, cursor


def _unique_manifest_id(manifest_el, base_id, ns):
    """Return base_id if unique in manifest, otherwise append a numeric suffix."""
    existing = {item.get("id") for item in manifest_el.findall(f"{{{ns}}}item")}
    if base_id not in existing:
        return base_id
    n = 1
    while f"{base_id}-{n}" in existing:
        n += 1
    return f"{base_id}-{n}"


def patch_opf(root, chapter_items, total_duration_ms, voice_name, nav_href=None):
    ns = OPF_NS
    root.set("version", "3.0")
    manifest = root.find(f"{{{ns}}}manifest")
    metadata = root.find(f"{{{ns}}}metadata")

    for ch in chapter_items:
        smil_id = _unique_manifest_id(manifest, ch["smil_id"], ns)
        audio_id = _unique_manifest_id(manifest, ch["audio_id"], ns)

        for item in manifest.findall(f"{{{ns}}}item"):
            if item.get("id") == ch["spine_id"]:
                item.set("media-overlay", smil_id)
                break

        si = ET.SubElement(manifest, f"{{{ns}}}item")
        si.set("id", smil_id)
        si.set("href", ch["smil_href"])
        si.set("media-type", "application/smil+xml")

        ai = ET.SubElement(manifest, f"{{{ns}}}item")
        ai.set("id", audio_id)
        ai.set("href", ch["audio_href"])
        ai.set("media-type", "audio/mpeg")

        m = ET.SubElement(metadata, f"{{{ns}}}meta")
        m.set("property", "media:duration")
        m.set("refines", f"#{smil_id}")
        m.text = ms_to_clock(ch["duration_ms"])

    mod = next(
        (el for el in metadata.findall(f"{{{ns}}}meta")
         if el.get("property") == "dcterms:modified"),
        None,
    )
    if mod is None:
        mod = ET.SubElement(metadata, f"{{{ns}}}meta")
        mod.set("property", "dcterms:modified")
    mod.text = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    mt = ET.SubElement(metadata, f"{{{ns}}}meta")
    mt.set("property", "media:duration")
    mt.text = ms_to_clock(total_duration_ms)

    mn = ET.SubElement(metadata, f"{{{ns}}}meta")
    mn.set("property", "media:narrator")
    mn.text = voice_name

    mc = ET.SubElement(metadata, f"{{{ns}}}meta")
    mc.set("property", "media:active-class")
    mc.text = "-epub-media-overlay-active"

    if nav_href:
        existing_nav = None
        for item in manifest.findall(f"{{{ns}}}item"):
            props = item.get("properties", "")
            if "nav" in props.split():
                existing_nav = item
                break
        if existing_nav is None:
            ni = ET.SubElement(manifest, f"{{{ns}}}item")
            ni.set("id", _unique_manifest_id(manifest, "nav-doc", ns))
            ni.set("href", nav_href)
            ni.set("media-type", "application/xhtml+xml")
            ni.set("properties", "nav")

    return ET.tostring(root, encoding="utf-8", xml_declaration=True)

# Standards-blessed font obfuscation algorithms — NOT DRM.  Any other
# encryption algorithm in encryption.xml means real content encryption.
_FONT_OBFUSCATION_ALGORITHMS = {
    "http://www.idpf.org/2008/embedding",
    "http://ns.adobe.com/pdf/enc#RC",
}


def _check_drm(zf: zipfile.ZipFile, progress_cb):
    """Return True if the EPUB appears to be DRM-encrypted."""
    if "META-INF/encryption.xml" not in zf.namelist():
        return False
    try:
        root = ET.fromstring(zf.read("META-INF/encryption.xml"))
    except Exception:
        return False
    enc_ns = "{http://www.w3.org/2001/04/xmlenc#}"
    algorithms = {
        m.get("Algorithm", "")
        for m in root.iter(f"{enc_ns}EncryptionMethod")
    }
    if algorithms - _FONT_OBFUSCATION_ALGORITHMS:
        progress_cb(
            "ERROR: This EPUB appears to be DRM-protected.\n"
            "       Remove DRM before converting."
        )
        return True
    return False


def _unique_path(path):
    """Return path, or path_1, path_2, ... if it already exists."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(f"{base}_{n}{ext}"):
        n += 1
    return f"{base}_{n}{ext}"


def _unique_zip_href(opf_dir, href, existing_names):
    """Return href, adjusted so its zip path collides with nothing in the EPUB."""
    if epub_path(opf_dir, href) not in existing_names:
        return href
    base, ext = posixpath.splitext(href)
    n = 1
    while epub_path(opf_dir, f"{base}_{n}{ext}") in existing_names:
        n += 1
    return f"{base}_{n}{ext}"


def convert_epub_to_epub3(input_path, output_path, voice, rate, workers,
                          progress_cb, cancel_event=None, pause_event=None,
                          audio_format="audio-24khz-48kbitrate-mono-mp3",
                          bytes_per_ms=6.0,
                          para_pause_ms=0, sentence_pause_ms=0,
                          bitrate_kbps=48, skip_backmatter=False,
                          skip_frontmatter=False,
                          progress_pct_cb=None):
    """Convert EPUB to EPUB3 with MP3 media overlays."""
    need_boundaries = True

    with zipfile.ZipFile(input_path, "r") as zf:
        if _check_drm(zf, progress_cb):
            return None

        opf_path = find_opf_path(zf)
        opf_root, manifest, spine, opf_dir = parse_opf(zf, opf_path)

        chapters, skipped_dupes = resolve_spine_chapters(manifest, spine, opf_dir)
        if skipped_dupes:
            progress_cb(
                f"Found {len(chapters)} unique chapter file(s) in spine "
                f"({skipped_dupes} duplicate spine reference(s) skipped)."
            )
        else:
            progress_cb(f"Found {len(chapters)} chapter(s) in spine.")

        modified = {}
        new_files = {}
        ch_items = []
        chapter_nav_info = []
        existing_names = set(zf.namelist())

        for ch_idx, (spine_id, href) in enumerate(chapters, 1):
            if cancel_event and cancel_event.is_set():
                progress_cb("Cancelled.")
                return None

            html_zip_path = epub_path(opf_dir, href)
            label = f"Ch {ch_idx:03d}/{len(chapters):03d}"
            progress_cb(f"[{label}]  {href}")

            with zf.open(html_zip_path) as f:
                html_bytes = f.read()

            if _is_footnote_file(href):
                progress_cb("  -> Footnote file, skipping.")
                continue

            if skip_backmatter and _is_backmatter(html_bytes, href):
                progress_cb("  -> Back matter detected, skipping TTS.")
                chapter_nav_info.append(
                    {"href": href, "title": f"Chapter {ch_idx}"})
                continue

            if skip_frontmatter and _is_frontmatter(html_bytes, href):
                progress_cb("  -> Front matter detected, skipping TTS.")
                chapter_nav_info.append(
                    {"href": href, "title": f"Chapter {ch_idx}"})
                continue

            soup, paragraphs = process_chapter(html_bytes, ch_idx)

            if not paragraphs:
                progress_cb("  -> No paragraphs found, skipping.")
                chapter_nav_info.append({"href": href, "title": f"Chapter {ch_idx}"})
                continue

            title = paragraphs[0]["text"][:80]
            chapter_nav_info.append({"href": href, "title": title, "ch_idx": ch_idx})

            progress_cb(f"  {len(paragraphs)} paragraph(s) to synthesize...")

            synth_results = synthesize_paragraphs(
                paragraphs, voice, rate, workers, progress_cb, cancel_event,
                pause_event=pause_event,
                audio_format=audio_format, need_boundaries=need_boundaries,
                sentence_pause_ms=sentence_pause_ms, bitrate_kbps=bitrate_kbps,
            )
            if synth_results is None:
                progress_cb("Cancelled.")
                return None

            audio_chunks = [r[0] for r in synth_results]
            all_boundaries = [r[1] for r in synth_results]

            if para_pause_ms > 0 and len(audio_chunks) > 1:
                silence = generate_mp3_silence(para_pause_ms, bitrate_kbps)
                for ci in range(len(audio_chunks) - 1):
                    audio_chunks[ci] = audio_chunks[ci] + silence

            span_data = []
            for pi, (para, bounds) in enumerate(zip(paragraphs, all_boundaries)):
                spans = inject_word_spans(soup, para, bounds, ch_idx, pi + 1)
                span_data.append(spans)

            audio_href = _unique_zip_href(
                opf_dir, f"Audio/ch{ch_idx:03d}.mp3", existing_names)
            smil_href = _unique_zip_href(
                opf_dir, f"SMIL/ch{ch_idx:03d}.smil", existing_names)
            audio_zip = epub_path(opf_dir, audio_href)
            smil_zip = epub_path(opf_dir, smil_href)
            existing_names.update((audio_zip, smil_zip))
            smil_zip_dir = posixpath.dirname(smil_zip)

            smil_xml, duration_ms = build_smil(
                paragraphs, audio_chunks,
                html_zip_path, audio_zip, smil_zip_dir,
                bytes_per_ms=bytes_per_ms, span_data=span_data,
            )

            modified[html_zip_path] = _to_xhtml_bytes(soup)
            new_files[audio_zip] = b"".join(audio_chunks)
            new_files[smil_zip] = smil_xml.encode("utf-8")

            ch_items.append({
                "spine_id": spine_id,
                "smil_id": f"overlay-ch{ch_idx:03d}",
                "smil_href": smil_href,
                "audio_id": f"audio-ch{ch_idx:03d}",
                "audio_href": audio_href,
                "duration_ms": duration_ms,
            })

            progress_cb(f"  -> {ms_to_human(duration_ms)}")

            ch_chars = sum(len(p["text"]) for p in paragraphs)
            if progress_pct_cb:
                progress_pct_cb(ch_chars)

        has_nav = any(
            "nav" in item.get("properties", "").split()
            for item in opf_root.findall(
                f".//{{{OPF_NS}}}manifest/{{{OPF_NS}}}item")
        )
        nav_rel_href = None
        if not has_nav:
            nav_rel_href = _unique_zip_href(opf_dir, "nav.xhtml", existing_names)
            nav_info = _nav_info_from_ncx(zf, manifest, opf_dir) or chapter_nav_info
            nav_bytes = _generate_nav_xhtml(nav_info)
            new_files[epub_path(opf_dir, nav_rel_href)] = nav_bytes

        total_ms = sum(c["duration_ms"] for c in ch_items)
        modified[opf_path] = patch_opf(
            opf_root, ch_items, total_ms, voice, nav_href=nav_rel_href,
        )

        progress_cb("Writing output EPUB3...")
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as out:
            out.writestr(
                zipfile.ZipInfo("mimetype"),
                "application/epub+zip",
                compress_type=zipfile.ZIP_STORED,
            )
            for name in zf.namelist():
                # new_files entries win over any same-named (unmanifested)
                # file in the source; writing both would duplicate the entry.
                if name == "mimetype" or name in new_files:
                    continue
                data = modified.get(name, zf.read(name))
                out.writestr(name, data)
            for path, data in new_files.items():
                # MP3 audio is already compressed; DEFLATE-ing it again only
                # burns CPU for ~0 size gain (very noticeable on large
                # audiobooks).  Keep DEFLATE for the text-based SMIL/nav files.
                ctype = (zipfile.ZIP_STORED if path.lower().endswith(".mp3")
                         else zipfile.ZIP_DEFLATED)
                out.writestr(path, data, compress_type=ctype)

    progress_cb(f"Done!  Total audio: {ms_to_human(total_ms)}")
    progress_cb(f"Output: {output_path}")
    return output_path

def convert_epub_to_mp3(input_path, output_dir, voice, rate, workers,
                        progress_cb, cancel_event=None, pause_event=None,
                        audio_format="audio-24khz-48kbitrate-mono-mp3",
                        bytes_per_ms=6.0, para_pause_ms=0, sentence_pause_ms=0,
                        bitrate_kbps=48,
                        skip_backmatter=False, skip_frontmatter=False,
                        progress_pct_cb=None):
    """Extract chapter text from EPUB and synthesize per-chapter MP3s."""
    os.makedirs(output_dir, exist_ok=True)

    with zipfile.ZipFile(input_path, "r") as zf:
        if _check_drm(zf, progress_cb):
            return None

        opf_path = find_opf_path(zf)
        _, manifest, spine, opf_dir = parse_opf(zf, opf_path)

        chapters, skipped_dupes = resolve_spine_chapters(manifest, spine, opf_dir)
        if skipped_dupes:
            progress_cb(
                f"Found {len(chapters)} unique chapter file(s) in spine "
                f"({skipped_dupes} duplicate spine reference(s) skipped)."
            )
        else:
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

            if _is_footnote_file(href):
                progress_cb("  -> Footnote file, skipping.")
                continue

            if skip_backmatter and _is_backmatter(html_bytes, href):
                progress_cb("  -> Back matter detected, skipping TTS.")
                continue

            if skip_frontmatter and _is_frontmatter(html_bytes, href):
                progress_cb("  -> Front matter detected, skipping TTS.")
                continue

            _, paragraphs = process_chapter(html_bytes, ch_idx)

            if not paragraphs:
                progress_cb("  -> No paragraphs found, skipping.")
                continue

            progress_cb(f"  {len(paragraphs)} paragraph(s) to synthesize...")

            audio_chunks = synthesize_paragraphs(
                paragraphs, voice, rate, workers, progress_cb, cancel_event,
                pause_event=pause_event,
                audio_format=audio_format,
                sentence_pause_ms=sentence_pause_ms, bitrate_kbps=bitrate_kbps,
            )
            if audio_chunks is None:
                progress_cb("Cancelled.")
                return None

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

            ch_chars = sum(len(p["text"]) for p in paragraphs)
            if progress_pct_cb:
                progress_pct_cb(ch_chars)

    progress_cb(f"Done!  MP3s saved to: {output_dir}")
    return output_dir


def convert_text_to_mp3(input_path, output_path, voice, rate, workers,
                        progress_cb, cancel_event=None,
                        audio_format="audio-24khz-48kbitrate-mono-mp3",
                        bitrate_kbps=48,
                        para_pause_ms=None, section_pause_ms=None,
                        sentence_pause_ms=0, progress_pct_cb=None):
    """Read a text/markdown file and synthesize to a single MP3."""
    with open(input_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    if not text.strip():
        progress_cb("File is empty, nothing to synthesize.")
        return None

    items = split_text_for_tts(text, para_pause_ms=para_pause_ms,
                               section_pause_ms=section_pause_ms)
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
        if sentence_pause_ms > 0:
            audio = _synthesize_with_sentence_pauses(
                chunk_text, voice, rate, audio_format, False,
                sentence_pause_ms, bitrate_kbps, progress_cb,
            )
        else:
            audio = synthesize_chunk(
                chunk_text, voice, rate,
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
            if progress_pct_cb:
                progress_pct_cb(len(items[items_idx]))
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
        self.root.minsize(650, 720)

        self.msg_queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.worker_thread = None
        self._last_output_dir = None
        self._close_countdown = 0

        # TTS provider state.  Each provider keeps its own label->voice map;
        # self.voice_menu always points at the active provider's map.
        self._provider = "edge"
        self._voice_menus = {
            "edge":   OrderedDict(FALLBACK_VOICES),
            "kokoro": OrderedDict(KOKORO_FALLBACK_VOICES),
        }
        self.voice_menu = self._voice_menus["edge"]

        self._build_ui()
        self._poll_queue()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
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

        if HAS_DND:
            self.file_listbox.drop_target_register(tkinterdnd2.DND_FILES)
            self.file_listbox.dnd_bind("<<Drop>>", self._on_drop)

        # ── Settings ──────────────────────────────────────────────────────
        frame_settings = ttk.LabelFrame(self.root, text="Settings")
        frame_settings.pack(fill="x", **pad)

        # TTS engine / provider
        row_provider = ttk.Frame(frame_settings)
        row_provider.pack(fill="x", padx=4, pady=2)
        ttk.Label(row_provider, text="Engine:").pack(side="left")
        self._provider_labels = OrderedDict([
            ("Edge TTS (online)",  "edge"),
            ("Kokoro (local)",     "kokoro"),
        ])
        self.provider_var = tk.StringVar(value="Edge TTS (online)")
        self.provider_combo = ttk.Combobox(
            row_provider, textvariable=self.provider_var,
            values=list(self._provider_labels.keys()),
            state="readonly", width=20)
        self.provider_combo.pack(side="left", padx=(4, 0))
        self.provider_combo.bind("<<ComboboxSelected>>", self._on_provider_change)
        self.kokoro_url_label = ttk.Label(row_provider, text="Kokoro URL:")
        self.kokoro_url_var = tk.StringVar(value=KOKORO_DEFAULT_URL)
        self.kokoro_url_entry = ttk.Entry(
            row_provider, textvariable=self.kokoro_url_var, width=22)
        # Shown only when Kokoro is selected (see _on_provider_change).

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
                                         state="readonly", width=40)
        self.voice_combo.pack(side="left", padx=(4, 0), fill="x", expand=True)
        self.preview_btn = ttk.Button(row, text="Test", width=5,
                                       command=self._preview_voice)
        self.preview_btn.pack(side="left", padx=(4, 0))

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
        self.workers_var = tk.IntVar(value=4)
        ttk.Spinbox(row3, from_=1, to=12, textvariable=self.workers_var,
                     width=5).pack(side="left", padx=(4, 0))

        # Audio quality
        row_aq = ttk.Frame(frame_settings)
        row_aq.pack(fill="x", padx=4, pady=2)
        ttk.Label(row_aq, text="Quality:").pack(side="left")
        self.quality_var = tk.StringVar(value=list(AUDIO_FORMATS.keys())[0])
        ttk.Combobox(row_aq, textvariable=self.quality_var,
                      values=list(AUDIO_FORMATS.keys()),
                      state="readonly", width=28).pack(side="left", padx=(4, 0))

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

        # Output directory
        row_out = ttk.Frame(frame_settings)
        row_out.pack(fill="x", padx=4, pady=2)
        ttk.Label(row_out, text="Save to:").pack(side="left")
        self.output_dir_var = tk.StringVar(value="")
        self.output_dir_entry = ttk.Entry(
            row_out, textvariable=self.output_dir_var, width=35)
        self.output_dir_entry.pack(side="left", padx=(4, 0), fill="x", expand=True)
        ttk.Button(row_out, text="Browse...",
                   command=self._browse_output_dir, width=8).pack(side="left", padx=(4, 0))
        ttk.Label(row_out, text="(blank = same as input)",
                  foreground="gray").pack(side="left", padx=(4, 0))

        self.skip_frontmatter_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame_settings, text="Skip front matter (cover, title, copyright, TOC...)",
                         variable=self.skip_frontmatter_var).pack(anchor="w", padx=4, pady=2)

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

        self.pause_btn = ttk.Button(frame_action, text="Pause",
                                     command=self._pause, state="disabled")
        self.pause_btn.pack(side="left", padx=(0, 4))

        self.cancel_btn = ttk.Button(frame_action, text="Cancel",
                                      command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left")

        # ── Progress ──────────────────────────────────────────────────────
        self.frame_prog = ttk.LabelFrame(self.root, text="Progress")
        frame_prog = self.frame_prog
        frame_prog.pack(fill="both", expand=True, **pad)
        frame_prog.pack_propagate(False)

        self.log_text = tk.Text(frame_prog, height=1, state="disabled",
                                 wrap="word", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True, padx=4, pady=(4, 0))

        log_sb = ttk.Scrollbar(self.log_text, orient="vertical",
                                command=self.log_text.yview)
        log_sb.pack(side="right", fill="y")
        self.log_text.config(yscrollcommand=log_sb.set)

        self.progress_bar = ttk.Progressbar(frame_prog, mode="determinate")
        self.progress_bar.pack(fill="x", padx=4, pady=4)

        bottom_row = ttk.Frame(frame_prog)
        bottom_row.pack(fill="x", padx=4, pady=(0, 4))
        self.open_folder_btn = ttk.Button(
            bottom_row, text="Open Output Folder",
            command=self._open_output_folder, state="disabled")
        self.open_folder_btn.pack(side="left")
        ttk.Button(bottom_row, text="Save Log",
                   command=self._save_log).pack(side="right")

    # ── Voice loading ─────────────────────────────────────────────────────

    def _fetch_voices_async(self, provider=None):
        """Fetch the voice list for a provider in a background thread."""
        provider = provider or self._provider
        kokoro_url = self.kokoro_url_var.get().strip()

        def _do():
            menu = (fetch_kokoro_voices(kokoro_url) if provider == "kokoro"
                    else fetch_voices())
            if menu:
                self.msg_queue.put(("voices", (provider, menu)))
        threading.Thread(target=_do, daemon=True).start()

    def _get_selected_voice(self):
        """Resolve the combobox value to a provider voice id/short name."""
        val = self.voice_var.get().strip()
        if val in self.voice_menu:
            return self.voice_menu[val]
        return val

    def _apply_voice_menu(self, provider, menu):
        """Cache a fetched voice list; refresh the dropdown if it's active."""
        self._voice_menus[provider] = menu
        if provider != self._provider:
            return
        prev_short = self._get_selected_voice()
        self.voice_menu = menu
        labels = list(menu.keys())
        self.voice_combo["values"] = labels
        new_label = next(
            (k for k, v in menu.items() if v == prev_short),
            labels[0] if labels else "",
        )
        self.voice_var.set(new_label)

    def _default_voice_label(self, provider, menu):
        """Pick a sensible default voice label for a provider."""
        wanted = "af_heart" if provider == "kokoro" else "en-US-AvaMultilingualNeural"
        labels = list(menu.keys())
        return next((k for k, v in menu.items() if v == wanted),
                    labels[0] if labels else "")

    _WORKER_DEFAULTS = {"edge": 4, "kokoro": 1}

    def _on_provider_change(self, _=None):
        provider = self._provider_labels.get(self.provider_var.get(), "edge")
        prev_provider = self._provider
        self._provider = provider
        menu = self._voice_menus[provider]
        self.voice_menu = menu
        self.voice_combo["values"] = list(menu.keys())
        self.voice_var.set(self._default_voice_label(provider, menu))

        if provider == "kokoro":
            # Show the URL field; Kokoro is GPU-serialised so 1 worker is best.
            self.kokoro_url_label.pack(side="left", padx=(8, 0))
            self.kokoro_url_entry.pack(side="left", padx=(4, 0))
        else:
            self.kokoro_url_label.pack_forget()
            self.kokoro_url_entry.pack_forget()

        # Apply the new provider's default worker count, but leave a value the
        # user customized alone.
        if self.workers_var.get() == self._WORKER_DEFAULTS.get(prev_provider, 4):
            self.workers_var.set(self._WORKER_DEFAULTS[provider])

        # Refresh from the live server/service for the newly selected provider.
        self._fetch_voices_async(provider)

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
                parent = os.path.basename(os.path.dirname(p))
                display = f"{parent}/{os.path.basename(p)}" if parent else os.path.basename(p)
                self.file_listbox.insert("end", display)
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

    def _on_drop(self, event):
        raw = event.data
        paths = []
        if raw.startswith("{"):
            paths = [p.strip("{}") for p in re.findall(r'\{[^}]+\}|[^\s]+', raw)]
        else:
            paths = raw.split()
        for p in paths:
            p = p.strip()
            if not p:
                continue
            ext = os.path.splitext(p)[1].lower()
            if ext in (".epub", ".txt", ".md") and p not in self.files:
                self.files.append(p)
                parent = os.path.basename(os.path.dirname(p))
                display = f"{parent}/{os.path.basename(p)}" if parent else os.path.basename(p)
                self.file_listbox.insert("end", display)
        self._update_mode_availability()

    def _browse_output_dir(self):
        d = filedialog.askdirectory(title="Select output directory")
        if d:
            self.output_dir_var.set(d)

    def _open_output_folder(self):
        if self._last_output_dir and os.path.isdir(self._last_output_dir):
            os.startfile(self._last_output_dir)
        elif self._last_output_dir:
            os.startfile(os.path.dirname(self._last_output_dir))

    def _save_log(self):
        path = filedialog.asksaveasfilename(
            title="Save log", defaultextension=".log",
            filetypes=[("Log files", "*.log"), ("Text files", "*.txt")])
        if path:
            self.log_text.config(state="normal")
            content = self.log_text.get("1.0", "end")
            self.log_text.config(state="disabled")
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)

    def _preview_voice(self):
        if self.worker_thread and self.worker_thread.is_alive():
            self._log("Preview is unavailable while a conversion is running.")
            return
        voice = self._get_selected_voice()
        provider = self._provider
        kokoro_url = self.kokoro_url_var.get().strip() or KOKORO_DEFAULT_URL
        self.preview_btn.config(state="disabled")
        self._log(f"Testing voice: {voice}...")

        def _do():
            try:
                import ctypes
                import tempfile
                set_tts_backend(provider, kokoro_url)
                audio = synthesize_chunk(
                    "This is a sample of the selected voice.",
                    voice, "+0%")
                tmp = tempfile.NamedTemporaryFile(
                    suffix=".mp3", delete=False, dir=os.environ.get("TEMP"))
                tmp.write(audio)
                tmp.close()
                winmm = ctypes.windll.winmm
                winmm.mciSendStringW(
                    f'open "{tmp.name}" type mpegvideo alias tts_preview',
                    None, 0, None)
                winmm.mciSendStringW("play tts_preview wait", None, 0, None)
                winmm.mciSendStringW("close tts_preview", None, 0, None)
                os.unlink(tmp.name)
            except Exception as e:
                self._send("log", f"  Preview failed: {e}")
            finally:
                self.msg_queue.put(("preview_done", None))

        threading.Thread(target=_do, daemon=True).start()

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
            elif msg_type == "eta":
                self.frame_prog.config(text=data)
            elif msg_type == "done":
                self._on_done()
            elif msg_type == "voices":
                self._apply_voice_menu(*data)
            elif msg_type == "preview_done":
                self.preview_btn.config(state="normal")
        self.root.after(50, self._poll_queue)

    def _send(self, msg_type, data=None):
        self.msg_queue.put((msg_type, data))

    # ── Conversion control ────────────────────────────────────────────────

    def _start(self):
        if not self.files:
            self._log("No files selected.")
            return

        self.cancel_event.clear()
        self.pause_event.set()
        self.start_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.pause_btn.config(state="normal", text="Pause")
        # Preview shares the process-wide TTS backend; switching it mid-run
        # would reroute the conversion, so it is locked out while running.
        self.preview_btn.config(state="disabled")
        self.progress_bar["value"] = 0

        # Tk variables are not thread-safe: snapshot every setting on the
        # main thread and hand the worker plain values only.
        cfg = {
            "files": list(self.files),
            "voice": self._get_selected_voice(),
            "rate": f"{int(float(self.rate_var.get())):+d}%",
            "workers": self.workers_var.get(),
            "mode": self.mode_var.get(),
            "pause_label": self.pause_var.get(),
            "spause_label": self.sentence_pause_var.get(),
            "quality_key": self.quality_var.get(),
            "provider": self._provider,
            "kokoro_url": self.kokoro_url_var.get().strip() or KOKORO_DEFAULT_URL,
            "skip_backmatter": self.skip_backmatter_var.get(),
            "skip_frontmatter": self.skip_frontmatter_var.get(),
            "out_base_dir": self.output_dir_var.get().strip(),
        }

        self.worker_thread = threading.Thread(
            target=self._run, args=(cfg,), daemon=True)
        self.worker_thread.start()

    def _pause(self):
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.pause_btn.config(text="Resume")
            self._log("Paused.")
        else:
            self.pause_event.set()
            self.pause_btn.config(text="Pause")
            self._log("Resumed.")

    def _cancel(self):
        self.cancel_event.set()
        self.pause_event.set()
        self._log("Cancelling... please wait.")
        self.cancel_btn.config(state="disabled")
        self.pause_btn.config(state="disabled")

    def _on_close(self):
        if self.worker_thread and self.worker_thread.is_alive():
            self.cancel_event.set()
            self.pause_event.set()
            self.worker_thread.join(timeout=3)
        self.root.destroy()

    def _on_done(self):
        self.start_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        self.pause_btn.config(state="disabled", text="Pause")
        self.preview_btn.config(state="normal")
        self.frame_prog.config(text="Progress")
        if self._last_output_dir:
            self.open_folder_btn.config(state="normal")
        if self.close_var.get():
            self._close_countdown = 5
            self._tick_close()

    def _tick_close(self):
        if self._close_countdown <= 0:
            self.root.destroy()
            return
        self._log(f"Closing in {self._close_countdown}s...")
        self._close_countdown -= 1
        self.root.after(1000, self._tick_close)

    # ── Worker thread ─────────────────────────────────────────────────────

    def _prescan_files(self, files, skip_backmatter, skip_frontmatter):
        """Quick scan of all files to count total characters for accurate ETA.

        Returns a list of character counts (one per file).  Character count
        is the best proxy for synthesis time since TTS duration is roughly
        proportional to text length, unlike paragraph count which varies
        wildly (a 50-char paragraph ≠ a 2000-char paragraph).
        """
        counts = []
        for filepath in files:
            ext = os.path.splitext(filepath)[1].lower()
            n = 0
            try:
                if ext == ".epub":
                    with zipfile.ZipFile(filepath, "r") as zf:
                        opf_path = find_opf_path(zf)
                        _, manifest, spine, opf_dir = parse_opf(zf, opf_path)
                        # Same dedup as the conversion itself, or the total is
                        # inflated for duplicate-spine books and the ETA lies.
                        chapters, _ = resolve_spine_chapters(
                            manifest, spine, opf_dir)
                        for _idref, href in chapters:
                            if _is_footnote_file(href):
                                continue
                            hp = epub_path(opf_dir, href)
                            data = zf.read(hp)
                            if skip_backmatter and _is_backmatter(data, href):
                                continue
                            if skip_frontmatter and _is_frontmatter(data, href):
                                continue
                            _, paras = process_chapter(data, 0)
                            n += sum(len(p["text"]) for p in paras)
                elif ext in (".txt", ".md"):
                    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                        text = f.read()
                    items = split_text_for_tts(text)
                    n = sum(len(it) for it in items if isinstance(it, str))
            except Exception:
                n = 100
            counts.append(max(n, 1))
        return counts

    def _run(self, cfg):
        files = cfg["files"]
        voice = cfg["voice"]
        rate = cfg["rate"]
        workers = cfg["workers"]
        mode = cfg["mode"]

        # Resolve pause presets
        pause_label = cfg["pause_label"]
        para_pause_ms = PAUSE_PRESETS.get(pause_label, PAUSE_PRESETS["Short"])
        spause_label = cfg["spause_label"]
        sentence_pause_ms = SENTENCE_PAUSE_PRESETS.get(
            spause_label, SENTENCE_PAUSE_PRESETS["Default"]
        )

        quality_key = cfg["quality_key"]
        audio_format, bytes_per_ms, bitrate_kbps = AUDIO_FORMATS.get(
            quality_key, AUDIO_FORMATS[list(AUDIO_FORMATS.keys())[0]]
        )

        # Select the TTS backend for this run (process-wide).
        provider = cfg["provider"]
        kokoro_url = cfg["kokoro_url"]
        set_tts_backend(provider, kokoro_url)
        if provider == "kokoro":
            # Kokoro emits CBR 128 kbps / 24 kHz mono MP3 regardless of the Edge
            # "quality" choice, so pin the timing/silence constants to match.
            audio_format = "kokoro-mp3"
            bytes_per_ms = KOKORO_BYTES_PER_MS
            bitrate_kbps = KOKORO_BITRATE_KBPS
            quality_key = f"Kokoro 128 kbps ({kokoro_url})"

        skip_backmatter = cfg["skip_backmatter"]
        skip_frontmatter = cfg["skip_frontmatter"]

        total_files = len(files)
        batch_start = time.time()

        self._send("eta", "Progress — scanning files...")
        char_counts = self._prescan_files(
            files, skip_backmatter, skip_frontmatter)
        total_chars = sum(char_counts)

        self._send("log",
                   f"Pre-scan: {total_chars:,} characters across {total_files} file(s)")

        def progress_cb(msg):
            self._send("log", msg)

        chars_done = [0]

        def _format_eta(remaining):
            if remaining >= 3600:
                return f"{int(remaining // 3600)}h {int((remaining % 3600) // 60)}m"
            if remaining >= 60:
                return f"{int(remaining // 60)}m {int(remaining % 60)}s"
            return f"{int(remaining)}s"

        def _update_eta(done_delta=0):
            chars_done[0] += done_delta
            if chars_done[0] <= 0 or total_chars <= 0:
                return
            elapsed = time.time() - batch_start
            frac = chars_done[0] / total_chars
            remaining = elapsed * (1.0 / frac - 1.0)
            pct = int(frac * 100)
            self._send("progress", pct)
            self._send("eta",
                       f"Progress — {pct}% — ~{_format_eta(remaining)} remaining")

        for i, filepath in enumerate(files):
            if self.cancel_event.is_set():
                self._send("log", "Batch cancelled.")
                break

            self._send("log", f"\n{'=' * 55}")
            self._send("log", f"  File {i+1}/{total_files}: {os.path.basename(filepath)}")
            self._send("log", f"  Voice: {voice}  |  Rate: {rate}  |  Workers: {workers}")
            self._send("log", f"  Quality: {quality_key}  |  Para pause: {pause_label}  |  Sent pause: {spause_label}")
            self._send("log", f"{'=' * 55}")

            ext = os.path.splitext(filepath)[1].lower()

            out_base_dir = cfg["out_base_dir"]

            def _pct_cb(ch_chars):
                _update_eta(ch_chars)

            try:
                if ext == ".epub":
                    if mode == "epub3":
                        out_name = os.path.splitext(os.path.basename(filepath))[0] + "_audio.epub"
                        if out_base_dir:
                            output = _unique_path(os.path.join(out_base_dir, out_name))
                        else:
                            output = _unique_path(
                                os.path.splitext(filepath)[0] + "_audio.epub")

                        convert_epub_to_epub3(
                            filepath, output, voice, rate, workers,
                            progress_cb, self.cancel_event,
                            pause_event=self.pause_event,
                            audio_format=audio_format,
                            bytes_per_ms=bytes_per_ms,
                            para_pause_ms=para_pause_ms,
                            sentence_pause_ms=sentence_pause_ms,
                            bitrate_kbps=bitrate_kbps,
                            skip_backmatter=skip_backmatter,
                            skip_frontmatter=skip_frontmatter,
                            progress_pct_cb=_pct_cb,
                        )
                        self._last_output_dir = os.path.dirname(output)
                    else:
                        dir_name = os.path.splitext(os.path.basename(filepath))[0] + "_mp3"
                        if out_base_dir:
                            out_dir = _unique_path(os.path.join(out_base_dir, dir_name))
                        else:
                            out_dir = _unique_path(
                                os.path.splitext(filepath)[0] + "_mp3")

                        convert_epub_to_mp3(
                            filepath, out_dir, voice, rate, workers,
                            progress_cb, self.cancel_event,
                            pause_event=self.pause_event,
                            audio_format=audio_format,
                            bytes_per_ms=bytes_per_ms,
                            para_pause_ms=para_pause_ms,
                            sentence_pause_ms=sentence_pause_ms,
                            bitrate_kbps=bitrate_kbps,
                            skip_backmatter=skip_backmatter,
                            skip_frontmatter=skip_frontmatter,
                            progress_pct_cb=_pct_cb,
                        )
                        self._last_output_dir = out_dir
                elif ext in (".txt", ".md"):
                    out_name = os.path.splitext(os.path.basename(filepath))[0] + ".mp3"
                    if out_base_dir:
                        output = _unique_path(os.path.join(out_base_dir, out_name))
                    else:
                        output = _unique_path(
                            os.path.splitext(filepath)[0] + ".mp3")
                    convert_text_to_mp3(
                        filepath, output, voice, rate, workers,
                        progress_cb, self.cancel_event,
                        audio_format=audio_format,
                        bitrate_kbps=bitrate_kbps,
                        para_pause_ms=para_pause_ms,
                        sentence_pause_ms=sentence_pause_ms,
                        progress_pct_cb=_pct_cb,
                    )
                    self._last_output_dir = os.path.dirname(output)
                else:
                    self._send("log", f"  Unsupported file type: {ext}")
            except Exception as e:
                self._send("log", f"  ERROR: {e}")


        if not self.cancel_event.is_set():
            elapsed = time.time() - batch_start
            if elapsed >= 3600:
                t_str = f"{int(elapsed // 3600)}h {int((elapsed % 3600) // 60)}m"
            else:
                t_str = f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
            self._send("log", f"\nAll files processed in {t_str}.")
        self._send("done")


# ═════════════════════════════════════════════════════════════════════════════
#  Entry point
# ═════════════════════════════════════════════════════════════════════════════

def main():
    if HAS_DND:
        root = tkinterdnd2.Tk()
    else:
        root = tk.Tk()
    ConverterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
