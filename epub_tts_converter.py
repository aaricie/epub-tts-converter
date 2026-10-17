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
import math
import os
import posixpath
import queue
import re
import secrets
import ssl
import struct
import tempfile
import threading
import time
import tkinter as tk
import urllib.parse
import uuid
import warnings
import zipfile
from collections import OrderedDict
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait, as_completed
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from tkinter import filedialog, ttk
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
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

# Kokoro conversion uses 24 kHz PCM; MP3 constants remain for voice previews.
KOKORO_DEFAULT_URL  = "http://127.0.0.1:8880"
KOKORO_BYTES_PER_MS = 16.0   # 128 kbps
KOKORO_BITRATE_KBPS = 128
KOKORO_AUDIO_FORMATS = {
    label: ("kokoro-pcm", 48.0, bitrate)
    for label, bitrate in (("Compact (48 kbps)", 48), ("Standard (64 kbps)", 64),
                          ("Higher quality (96 kbps)", 96), ("Original size (128 kbps)", 128))
}

# Requests queued beyond the running workers.  One keeps a single-worker Kokoro
# GPU busy while the converter encodes and maps the previous paragraph.
SYNTHESIS_LOOKAHEAD = 1

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
    """Duration clock selected by the actual format, independent of GUI state."""
    if audio_format == "kokoro-pcm":
        return 48.0  # 24,000 signed 16-bit mono samples per second
    if audio_format == "kokoro-mp3":
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
        64:  b'\xff\xf3\x84\xc0',  # 64 kbps
        96:  b'\xff\xf3\xa4\xc0',  # 96 kbps
        128: b'\xff\xf3\xc4\xc0',  # 128 kbps (Kokoro output rate)
    }
    if bitrate_kbps not in _HEADERS:
        raise ValueError(f"Unsupported silence bitrate: {bitrate_kbps}")
    if duration_ms <= 0:
        return b""
    header = _HEADERS[bitrate_kbps]
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

class ConversionCancelled(RuntimeError):
    """The requested conversion was cancelled, without publishing a partial result."""


class SynthesisError(RuntimeError):
    """A passage could not be synthesized completely."""


class PassageError(SynthesisError):
    """A request failed after retries; the explicit continue policy may omit it."""


class IncompleteResponse(ConnectionError):
    """The engine reported success but its audio does not cover the text.

    Kokoro-FastAPI logs an internal chunk failure and returns the remaining
    audio with HTTP 200. Retried like any transient failure; persistent
    omissions become a PassageError for the stop/continue policy.
    """


def _check_control(cancel_event=None, pause_event=None):
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise ConversionCancelled("Conversion cancelled; checkpoints were retained.")
        if pause_event is None or pause_event.wait(timeout=0.1):
            return


def _interruptible_delay(seconds, cancel_event=None, pause_event=None):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        _check_control(cancel_event, pause_event)
        remaining = max(0, min(0.1, deadline - time.monotonic()))
        if cancel_event is not None:
            cancel_event.wait(remaining)
        else:
            time.sleep(remaining)
    _check_control(cancel_event, pause_event)


class _CombinedCancel:
    def __init__(self, *events):
        self.events = [event for event in events if event is not None]

    def is_set(self):
        return any(event.is_set() for event in self.events)

    def wait(self, timeout):
        deadline = time.monotonic() + timeout
        while not self.is_set() and time.monotonic() < deadline:
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        return self.is_set()


_request_locks = {}
_request_locks_guard = threading.Lock()


@contextmanager
def _request_lock(key, cancel_event, pause_event):
    """Serialize identical checkpoints, including a request finishing after cancel."""
    with _request_locks_guard:
        entry = _request_locks.setdefault(key, [threading.Lock(), 0])
        entry[1] += 1
    acquired = False
    try:
        while not acquired:
            _check_control(cancel_event, pause_event)
            acquired = entry[0].acquire(timeout=0.1)
        yield
    finally:
        if acquired:
            entry[0].release()
        with _request_locks_guard:
            entry[1] -= 1
            if not entry[1]:
                del _request_locks[key]


def synthesize_chunk(text, voice, rate="+0%", volume="+0%", pitch="+0Hz",
                     retries=4, audio_format="audio-24khz-48kbitrate-mono-mp3",
                     word_boundary=False, *, backend=None, cancel_event=None,
                     pause_event=None, cache=None):
    backend = dict(_tts_backend if backend is None else backend)
    args = (text, voice, rate, volume, pitch, retries, audio_format, word_boundary)
    kwargs = dict(backend=backend, cancel_event=cancel_event, pause_event=pause_event, cache=cache)
    if cache is None:
        return _synthesize_chunk_impl(*args, **kwargs)
    identity = (str(cache.directory), text, voice, rate, volume, pitch, audio_format, word_boundary,
                json.dumps({k: v for k, v in backend.items() if k not in ("continue_on_error", "keep_checkpoints", "batch_paragraphs")}, sort_keys=True))
    with _request_lock(identity, cancel_event, pause_event):
        return _synthesize_chunk_impl(*args, **kwargs)


def _synthesize_chunk_impl(text, voice, rate="+0%", volume="+0%", pitch="+0Hz",
                     retries=4, audio_format="audio-24khz-48kbitrate-mono-mp3",
                     word_boundary=False, *, backend=None, cancel_event=None,
                     pause_event=None, cache=None):
    """Synthesize one bounded request, retry transient failures, and checkpoint it."""
    backend = dict(_tts_backend if backend is None else backend)
    request = dict(kind="request", version=2, text=text, voice=voice, rate=rate,
                   volume=volume, pitch=pitch, audio_format=audio_format,
                   word_boundary=word_boundary,
                   backend={key: value for key, value in backend.items()
                            if key not in ("continue_on_error", "keep_checkpoints", "batch_paragraphs")})
    _check_control(cancel_event, pause_event)
    if cache is not None:
        if hasattr(cache, "used_requests"):
            cache.used_requests.append(request)
        cached = cache.get(request)
        if cached is not None:
            audio = cached.path.read_bytes()
            bounds = cached.metadata.get("boundaries", [])
            return (audio, bounds) if word_boundary else audio
    for attempt in range(retries + 1):
        _check_control(cancel_event, pause_event)
        try:
            if backend["provider"] == "kokoro":
                result = _kokoro_once(text, voice, rate, word_boundary,
                                      base_url=backend["kokoro_url"],
                                      response_format="pcm" if audio_format == "kokoro-pcm" else "mp3")
            else:
                result = _synthesize_once(text, voice, rate, volume, pitch,
                                          audio_format, word_boundary)
            # A completed in-flight request is useful on resume, even if cancelled.
            audio, bounds = result if word_boundary else (result, [])
            if not audio:
                raise PassageError("The engine returned no audio.")
            if cache is not None:
                cache.put(request, audio, {"boundaries": bounds})
            _check_control(cancel_event, pause_event)
            return result
        except (ConversionCancelled, ValueError, SynthesisError):
            raise
        except Exception as error:
            if not isinstance(error, (HTTPError, URLError, TimeoutError, ConnectionError,
                                      OSError, websocket.WebSocketException)):
                raise
            if isinstance(error, HTTPError) and error.code not in (408, 429) and error.code < 500:
                raise SynthesisError(f"TTS rejected the request (HTTP {error.code}): {error.reason}") from error
            if attempt >= retries:
                raise PassageError(f"TTS failed after {attempt + 1} attempts: {error}") from error
            if backend["provider"] == "edge" and "403" in str(error):
                _advance_clock_skew()
            delay = min(15, 1 + attempt * 2)
            if isinstance(error, HTTPError):
                try:
                    delay = min(60, max(delay, float(error.headers.get("Retry-After", delay))))
                except (ValueError, TypeError, AttributeError):
                    pass
            _interruptible_delay(delay, cancel_event, pause_event)


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
    received_turn_end = threading.Event()
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
                received_turn_end.set()
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
    if not received_turn_end.is_set():
        raise ConnectionError("Edge TTS disconnected before completing the passage")
    result = audio_data.getvalue()
    if not result:
        raise RuntimeError("No audio received")
    if word_boundary and any(char.isalnum() for char in text) and not boundaries:
        raise ConnectionError("Edge TTS completed without word timestamps")
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


def _kokoro_once(text, voice, rate, word_boundary, *, base_url=None,
                 response_format="mp3"):
    """Read Kokoro PCM and real word times; reject unsupported/malformed captions."""
    base = (base_url or _tts_backend["kokoro_url"]).rstrip("/")
    payload_text = re.sub(r"-{3,}", " - ", text)
    path = "/dev/captioned_speech" if word_boundary else "/v1/audio/speech"
    payload = {"model": "kokoro", "input": payload_text, "voice": voice,
               "response_format": response_format, "speed": _rate_to_speed(rate),
               "stream": False}
    if word_boundary:
        payload["return_timestamps"] = True
    req = Request(f"{base}{path}", data=json.dumps(payload).encode("utf-8"),
                  headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=120) as resp:
        body = resp.read()
    meta = json.loads(body.decode("utf-8")) if word_boundary else {}
    audio = base64.b64decode(meta.get("audio") or "", validate=True) if word_boundary else body
    if not audio:
        raise IncompleteResponse("Kokoro returned no audio.")
    if response_format == "pcm":
        if len(audio) % 2:
            raise SynthesisError("Kokoro returned incomplete 16-bit PCM samples.")
        duration_ms = len(audio) / 48.0
    else:
        audio = _strip_id3v2(audio)
        duration_ms = len(audio) / KOKORO_BYTES_PER_MS
    if not word_boundary:
        return audio
    boundaries = []
    previous = 0.0
    for word_record in meta.get("timestamps") or []:
        word = str(word_record.get("word", "")).strip()
        if not any(char.isalnum() for char in word):
            continue  # Kokoro punctuation records can overlap internal chunks.
        start_ms = float(word_record.get("start_time", 0)) * 1000
        end_ms = float(word_record.get("end_time", word_record.get("start_time", 0))) * 1000
        if not all(math.isfinite(value) for value in (start_ms, end_ms)):
            raise SynthesisError("Kokoro returned non-finite word timestamps.")
        # Current Kokoro trimming yields small edge errors; never hide large drift.
        if (start_ms < -100 or end_ms > duration_ms + 100 or
                end_ms < start_ms or (boundaries and start_ms < previous - 1) or
                start_ms >= duration_ms or end_ms <= 0):
            raise SynthesisError(f"Kokoro word timing falls outside its audio: {word!r}.")
        start_ms = max(previous, 0.0, start_ms)
        end_ms = min(duration_ms, end_ms)
        boundaries.append({"type": "wordboundary", "offset": start_ms,
                           "duration": max(0, end_ms - start_ms), "text": word})
        previous = start_ms
    if any(char.isalnum() for char in text) and not boundaries:
        raise SynthesisError("Kokoro returned audio without word timestamps. "
                             "Use an English Kokoro voice for synchronized EPUBs, "
                             "or select Edge TTS for this language.")
    _check_caption_coverage(payload_text, boundaries, duration_ms)
    return audio, boundaries


# A dropped engine chunk removes whole sentences; normalization only rewrites
# a few words ("1945" -> "nineteen forty-five").
MISSING_WORDS_LIMIT = 8
# Far below natural speech even at the fastest rate the GUI offers.
MIN_MS_PER_WORD = 120
_LATIN_TOKEN = re.compile(r"[0-9a-zÀ-ɏ]+\Z")


def _check_caption_coverage(text, boundaries, duration_ms):
    """Raise IncompleteResponse when the audio itself skips a run of source words.

    A run of uncaptioned words is only an omission when the timeline leaves
    no room to have spoken it. Kokoro drops a failed chunk's audio and closes
    the gap, but it can also lose the timestamps of a chunk whose audio is
    intact (0.2.x aborts timestamp extraction at a token without timing);
    those words simply get no highlight. Only Latin-script words count: an
    English voice cannot speak a Cyrillic or Greek quotation.
    """
    from difflib import SequenceMatcher
    from kokoro_batching import _lexical_tokens

    source = [token for token in _lexical_tokens(text) if _LATIN_TOKEN.match(token)]
    spoken, owners = [], []
    for index, boundary in enumerate(boundaries):
        for token in _lexical_tokens(boundary["text"]):
            if _LATIN_TOKEN.match(token):
                spoken.append(token)
                owners.append(index)
    matcher = SequenceMatcher(None, source, spoken, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        missing = (i2 - i1) - (j2 - j1)
        if tag not in ("delete", "replace") or missing < MISSING_WORDS_LIMIT:
            continue
        # Repeated words ("the") make the exact edge of the run ambiguous, so
        # look for the largest pause within a few captions either side.
        low, high = max(0, j1 - 3), min(len(owners), j2 + 3)
        window = []
        for k in range(low, high):
            if not window or window[-1] is not boundaries[owners[k]]:
                window.append(boundaries[owners[k]])
        spans = [(record["offset"], record["offset"] + record["duration"]) for record in window]
        gaps = [later[0] - earlier[1] for earlier, later in zip(spans, spans[1:])]
        if low == 0:
            gaps.append(spans[0][0] if spans else duration_ms)
        if high == len(owners):
            gaps.append(duration_ms - (spans[-1][1] if spans else 0.0))
        if max(gaps, default=duration_ms) >= missing * MIN_MS_PER_WORD:
            continue  # Audio is there; only its timestamps are missing.
        words = " ".join(source[i1:i2])
        raise IncompleteResponse(
            f"Kokoro's audio omits {i2 - i1} words of the passage: {words[:120]!r}")


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
        # 0.2.x lists names; 0.8.2+ lists {"id": ..., "overall_grade": ...}.
        entries = []
        for voice in voices:
            if isinstance(voice, dict):
                vid, grade = voice.get("id") or voice.get("name"), voice.get("overall_grade")
            else:
                vid, grade = voice, None
            if isinstance(vid, str) and vid:
                entries.append((vid, grade if isinstance(grade, str) else None))
        menu = OrderedDict()
        for vid, grade in sorted(entries):
            parts = [p for p in (_LANG.get(vid[:1], ""), _GENDER.get(vid[1:2], "")) if p]
            if grade:
                parts.append(f"grade {grade}")
            tag = ", ".join(parts)
            menu[f"{vid}  ({tag})" if tag else vid] = vid
        return menu or None
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
    def render(items):
        result = []
        for ch in items:
            children = ch.get("children", [])
            nested = "<ol>" + render(children) + "</ol>" if children else ""
            result.append(f'<li><a href={quoteattr(ch["href"])}>'
                          f'{escape(ch["title"])}</a>{nested}</li>')
        return "\n".join(result)
    li_block = render(chapters_info)
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
    def read_points(parent):
        chapters = []
        for nav_point in parent.findall(f"{{{NCX_NS}}}navPoint"):
            children = read_points(nav_point)
            text_el = nav_point.find(f"{{{NCX_NS}}}navLabel/{{{NCX_NS}}}text")
            content_el = nav_point.find(f"{{{NCX_NS}}}content")
            src = content_el.get("src", "") if content_el is not None else ""
            if not src:
                chapters.extend(children)
                continue
            parts = urllib.parse.urlsplit(src)
            if parts.scheme or parts.netloc:
                href = src
            else:
                zip_path = epub_path(ncx_dir, parts.path)
                href = opf_href_from_zip_path(opf_dir, zip_path)
                if parts.query:
                    href += "?" + parts.query
                if parts.fragment:
                    href += "#" + parts.fragment
            title = "".join(text_el.itertext()).strip() if text_el is not None else href
            chapters.append({"href": href, "title": title or href, "children": children})
        return chapters
    nav_map = root.find(f"{{{NCX_NS}}}navMap")
    return read_points(nav_map) if nav_map is not None else []


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

    soup = BeautifulSoup(html_bytes, "lxml")
    if _document_has_semantics(soup, _BACKMATTER_EPUB_TYPES):
        return True

    for sec in soup.find_all(["section", "body"], limit=3):
        sec_title = sec.get("title", "")
        if sec_title and _BACKMATTER_HEADING_RE.match(sec_title.strip()):
            return True

    for h in soup.find_all(["h1", "h2", "h3"], limit=1):
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

    soup = BeautifulSoup(html_bytes, "lxml")
    if _document_has_semantics(soup, _FRONTMATTER_EPUB_TYPES):
        return True

    links = soup.find_all("a")
    all_text = soup.get_text(strip=True)
    if links and all_text:
        link_text = sum(len(a.get_text(strip=True)) for a in links)
        if link_text > len(all_text) * 0.7 and len(links) > 10:
            return True

    for h in soup.find_all(["h1", "h2", "h3"], limit=1):
        if _FRONTMATTER_HEADING_RE.match(h.get_text(strip=True)):
            return True

    return False


def _document_has_semantics(soup, semantics):
    """A subsection must not classify a whole mixed-content document."""
    from bs4 import Comment, NavigableString
    body = soup.body or soup
    for node in [body, *body.find_all(attrs={"epub:type": True})]:
        if not set(node.get("epub:type", "").split()) & semantics:
            continue
        if node is body:
            return True
        outside_text = any(
            isinstance(text, NavigableString) and not isinstance(text, Comment)
            and str(text).strip() and not any(parent is node for parent in text.parents)
            and not _node_is_tts_skipped(text, body)
            for text in body.descendants
        )
        if not outside_text:
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
    if tag.name in {"head", "script", "style", "template", "noscript"}:
        return True
    if tag.has_attr("hidden") or tag.get("aria-hidden", "").lower() == "true":
        return True
    if re.search(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\b",
                 tag.get("style", ""), re.I):
        return True
    epub_type = tag.get("epub:type", "")
    if set(epub_type.split()) & {"noteref", "annoref", "pagebreak", "footnote",
                                  "endnote", "rearnote", "footnotes", "endnotes",
                                  "rearnotes", "notes"}:
        return True
    if set(tag.get("role", "").split()) & {"doc-noteref", "doc-footnote",
                                            "doc-endnote", "doc-endnotes",
                                            "doc-pagebreak"}:
        return True
    classes = set(tag.get("class", []))
    if classes & {"footnote", "footnotes", "endnote", "endnotes", "rearnote", "rearnotes"}:
        return True
    if "end" in classes and tag.name in {"div", "li", "aside", "p"}:
        # Older Calibre books (including Wages of Destruction) use div.end
        # with en123/Ref-en123 backlinks instead of EPUB note semantics.
        # Class "end" alone is not sufficient evidence to exclude prose.
        backlink = tag.find("a", href=True)
        if backlink and re.fullmatch(r"(?:en|fn)\d+", backlink.get("id", ""), re.I):
            target = urllib.parse.urlsplit(backlink["href"]).fragment
            if target.lower() == "ref-" + backlink["id"].lower():
                return True
    if tag.name == "sup":
        sup_text = tag.get_text(strip=True)
        return (
            len(sup_text) <= 3
            and re.match(r'^[\d*\u2020\u2021\u00a7]+$', sup_text)
        )
    if tag.name == "a":
        a_text = tag.get_text(strip=True)
        href = tag.get("href", "")
        note_target = re.fullmatch(r"(?:en|fn)\d+", urllib.parse.urlsplit(href).fragment, re.I)
        return re.match(r'^\d{1,4}$', a_text) and (href.startswith("#") or note_target)
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
    from bs4 import NavigableString, Comment, Declaration, Doctype, ProcessingInstruction

    out = []
    mapping = []
    def walk(node):
        if isinstance(node, (Comment, Declaration, Doctype, ProcessingInstruction)):
            return
        if isinstance(node, NavigableString):
            raw = str(node)
            _append_mapped_text(out, mapping, raw, [(node, i) for i in range(len(raw))])
            return
        if _is_tts_skipped_tag(node):
            return
        block = node.name in _BLOCK_TAGS or node.name == "br"
        if block:
            _append_mapped_text(out, mapping, " ", [None])
        for child in node.children:
            walk(child)
        if block:
            _append_mapped_text(out, mapping, " ", [None])
    if not _node_is_tts_skipped(tag, tag):
        walk(tag)

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
_BLOCK_TAGS = _PRIMARY_TAGS | _FALLBACK_TAGS | {
    "body", "ul", "ol", "dl", "table", "thead", "tbody", "tfoot", "tr",
    "td", "th", "caption", "figcaption", "figure", "aside", "header", "footer",
    "nav", "main", "address", "pre", "hr",
}


def _unique_xhtml_id(soup, base):
    ids = soup.__dict__.get("_tts_reserved_ids")
    if ids is None:
        ids = {tag["id"] for tag in soup.find_all(id=True)}
        soup.__dict__["_tts_reserved_ids"] = ids
    candidate = base
    suffix = 1
    while candidate in ids:
        candidate = f"{base}-{suffix}"
        suffix += 1
    ids.add(candidate)
    return candidate


def _paragraph_record(tag, ch_idx, counter):
    display_text, display_map = _display_text_and_map(tag)
    if not any(ch.isalnum() for ch in display_text):
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


def process_chapter(html_bytes: bytes, ch_idx: int, *, build_maps=True):
    """Parse a chapter, inject IDs on paragraphs, extract text.

    Returns (soup, paragraphs) where paragraphs is a list of
    {"id": str, "text": str, "tag": BeautifulSoup Tag (for word/sentence injection)}.

    Mixed blocks are visited in document order. Every visible text node is
    consumed once, including text before/after nested blocks. Semantic notes
    remain in the document but are deliberately excluded from narration.
    build_maps=False is a lightweight character-counting path for pre-scans.
    """
    from bs4 import NavigableString, Comment, Declaration, Doctype, ProcessingInstruction
    from epub_migration import migrate_xhtml_soup

    soup = BeautifulSoup(html_bytes, "lxml")
    migrate_xhtml_soup(soup, highlight=build_maps)
    paragraphs = []
    out, mapping = [], []
    owner = None
    used_owners = set()

    def flush():
        nonlocal out, mapping
        text = "".join(out).strip()
        if text and any(ch.isalnum() for ch in text):
            if not build_maps:
                paragraphs.append({"text": text})
            else:
                left = 1 if out and out[0] == " " else 0
                text_map = mapping[left:left + len(text)]
                pid = _unique_xhtml_id(soup, f"mo_ch{ch_idx:03d}p{len(paragraphs)+1:04d}")
                if id(owner) not in used_owners:
                    if not owner.get("id"):
                        owner["id"] = pid
                    pid = owner["id"]
                    used_owners.add(id(owner))
                else:
                    anchor = soup.new_tag("span", id=pid)
                    anchor["data-epub-tts"] = "anchor"
                    first = next((loc for loc in text_map if loc is not None), None)
                    if first:
                        first[0].insert_before(anchor)
                    else:
                        owner.append(anchor)
                tts_text, tts_map = _sanitize_for_ssml_with_map(text)
                paragraphs.append({"id": pid, "text": text, "display_text": text,
                                   "display_map": text_map, "tts_text": tts_text,
                                   "tts_map": tts_map, "tag": owner})
        out, mapping = [], []

    def walk(node, block_owner):
        nonlocal owner
        if isinstance(node, (Comment, Declaration, Doctype, ProcessingInstruction)):
            return
        if isinstance(node, NavigableString):
            raw = str(node)
            if build_maps:
                _append_mapped_text(out, mapping, raw, [(node, i) for i in range(len(raw))])
            else:
                for char in raw:
                    char = " " if char.isspace() else char
                    if char != " " or not out or out[-1] != " ":
                        out.append(char)
            return
        if _is_tts_skipped_tag(node):
            return
        if node.name == "br":
            if not out or out[-1] != " ":
                out.append(" ")
                if build_maps:
                    mapping.append(None)
            return
        is_block = node.name in _BLOCK_TAGS
        if is_block:
            flush()
            owner = node
        for child in list(node.children):
            walk(child, node if is_block else block_owner)
        if is_block:
            flush()
            owner = block_owner

    body = soup.body or soup
    owner = body
    walk(body, body)
    flush()

    return soup, paragraphs


def count_chapter_characters(html_bytes):
    return sum(len(p["text"]) for p in process_chapter(html_bytes, 0, build_maps=False)[1])


def inject_word_spans(soup, para, boundaries, ch_idx, para_idx):
    """Map Edge word-boundary metadata back onto the source XHTML.

    Edge receives sanitized text, so its word metadata is aligned against the
    sanitized TTS string first.  Mapped words wrap the original visible text.
    Tokens that cannot be shown safely still get empty sync markers so their
    audio clips stay represented in the SMIL overlay.
    """
    from bs4 import NavigableString

    def _word_span(sid):
        span = soup.new_tag("span", id=sid)
        span["data-epub-tts"] = "word"
        return span

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
        sid = _unique_xhtml_id(soup, f"mo_ch{ch_idx:03d}p{para_idx:04d}w{wi+1:04d}")
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
                span_el = _word_span(op["id"])
                fragments.append(span_el)
                continue

            start = min(max(op["start"], 0), len(raw))
            end = min(max(op["end"], start), len(raw))
            if start < pos or start == end:
                # Overlapping or empty wrap: fall back to an empty sync marker
                # so the SMIL par for this word still has an XHTML target.
                fragments.append(_word_span(op["id"]))
                continue
            if start > pos:
                fragments.append(NavigableString(raw[pos:start]))
            span_el = _word_span(op["id"])
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
        tag.append(_word_span(marker_id))

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


def _audio_silence(duration_ms, bitrate_kbps, audio_format):
    if audio_format == "kokoro-pcm":
        return bytes(round(duration_ms * 24) * 2)
    return generate_mp3_silence(duration_ms, bitrate_kbps)


def _synthesize_long(text, voice, rate, audio_format, need_boundaries,
                     *, backend=None, cancel_event=None, pause_event=None, cache=None,
                     progress_cb=None):
    """Split every oversized request, keeping offsets on the selected audio clock."""
    chunks = bytearray()
    boundaries = []
    bytes_per_ms = _resolve_bytes_per_ms(audio_format)
    for piece in _split_long_body(text, MAX_TTS_CHARS):
        _check_control(cancel_event, pause_event)
        try:
            result = synthesize_chunk(piece, voice, rate, audio_format=audio_format,
                                      word_boundary=need_boundaries, backend=backend,
                                      cancel_event=cancel_event, pause_event=pause_event,
                                      cache=cache)
        except PassageError as error:
            if not (backend or {}).get("continue_on_error", False):
                raise
            warning = {"text": piece, "error": str(error)}
            if cache is not None:
                if not hasattr(cache, "failures"):
                    cache.failures = []
                cache.failures.append(warning)
            if progress_cb:
                progress_cb(f"    WARNING: omitted passage {piece[:100]!r}: {error}")
            continue
        audio, bounds = result if need_boundaries else (result, [])
        cursor_ms = len(chunks) / bytes_per_ms
        boundaries.extend(dict(boundary, offset=boundary["offset"] + cursor_ms)
                          for boundary in bounds)
        chunks.extend(audio)
    if not chunks and not (backend or {}).get("continue_on_error", False):
        raise SynthesisError("A nonempty passage produced no audio.")
    return (bytes(chunks), boundaries) if need_boundaries else bytes(chunks)


def _synthesize_with_sentence_pauses(text, voice, rate, audio_format,
                                     need_boundaries, sentence_pause_ms,
                                     bitrate_kbps, progress_cb=None, *, backend=None,
                                     cancel_event=None, pause_event=None, cache=None):
    """Add requested pauses; any failed sentence fails the entire passage."""
    sentences = _split_into_sentences(text)
    chunks = bytearray()
    boundaries = []
    bytes_per_ms = _resolve_bytes_per_ms(audio_format)
    silence = _audio_silence(sentence_pause_ms, bitrate_kbps, audio_format)
    for index, sentence in enumerate(sentences):
        _check_control(cancel_event, pause_event)
        result = _synthesize_long(sentence, voice, rate, audio_format, need_boundaries,
                                  backend=backend, cancel_event=cancel_event,
                                  pause_event=pause_event, cache=cache, progress_cb=progress_cb)
        audio, bounds = result if need_boundaries else (result, [])
        if not audio:
            continue
        if chunks:
            chunks.extend(silence)
        cursor_ms = len(chunks) / bytes_per_ms
        boundaries.extend(dict(boundary, offset=boundary["offset"] + cursor_ms)
                          for boundary in bounds)
        chunks.extend(audio)
    return (bytes(chunks), boundaries) if need_boundaries else bytes(chunks)


def iter_synthesized_paragraphs(paragraphs, voice, rate, workers, progress_cb,
                                cancel_event=None, pause_event=None,
                                audio_format="audio-24khz-48kbitrate-mono-mp3",
                                need_boundaries=False, sentence_pause_ms=0,
                                bitrate_kbps=48, *, backend=None, cache=None):
    """Yield in document order with ``workers`` requests running at once.

    At most ``workers + SYNTHESIS_LOOKAHEAD`` results are outstanding, which
    bounds memory. HTTP already in progress may finish after cancellation, but
    no new requests or retries are issued. Completed requests remain available
    in the cache.
    """
    backend = dict(_tts_backend if backend is None else backend)
    workers = max(1, int(workers))
    stopped = threading.Event()
    combined_cancel = _CombinedCancel(cancel_event, stopped)

    def synthesize(paragraph):
        _check_control(combined_cancel, pause_event)
        text = paragraph.get("tts_text", paragraph["text"])
        if not any(char.isalnum() for char in text):
            silence = _audio_silence(SILENCE_SECTION_MS, bitrate_kbps, audio_format)
            return (silence, []) if need_boundaries else silence
        kwargs = dict(backend=backend, cancel_event=combined_cancel,
                      pause_event=pause_event, cache=cache)
        if sentence_pause_ms > 0:
            return _synthesize_with_sentence_pauses(
                text, voice, rate, audio_format, need_boundaries,
                sentence_pause_ms, bitrate_kbps, progress_cb, **kwargs)
        return _synthesize_long(text, voice, rate, audio_format, need_boundaries,
                                progress_cb=progress_cb, **kwargs)

    # English Kokoro voices share the captioning pipeline that the batch
    # splitter validates; other languages keep one request per paragraph.
    use_batching = (backend["provider"] == "kokoro" and audio_format == "kokoro-pcm"
                    and voice[:1] in ("a", "b") and backend.get("batch_paragraphs", False)
                    and sentence_pause_ms == 0)
    if use_batching:
        from kokoro_batching import plan_paragraph_batches, split_pcm_batch
        batches = plan_paragraph_batches(paragraphs, max_chars=MAX_TTS_CHARS)
        groups = [batch.indices for batch in batches]
    else:
        batches = []
        groups = [(index,) for index in range(len(paragraphs))]

    batch_stats = {"requests": 0, "paragraphs": 0, "fallbacks": 0}
    stats_lock = threading.Lock()

    def synthesize_group(group_index):
        indices = groups[group_index]
        if use_batching and len(indices) > 1:
            batch = batches[group_index]
            try:
                # One retry: a failed batch falls back to single requests,
                # which retry fully on their own.
                audio, boundaries = synthesize_chunk(batch.combined_text, voice, rate,
                    retries=1, audio_format=audio_format, word_boundary=True, backend=backend,
                    cancel_event=combined_cancel, pause_event=pause_event, cache=cache)
                split = split_pcm_batch(batch, audio, boundaries)
            except SynthesisError:
                # Includes timing the client rejects; single requests still
                # report a genuine configuration error on their own.
                split = None
            with stats_lock:
                if split is not None:
                    batch_stats["requests"] += 1
                    batch_stats["paragraphs"] += len(indices)
                else:
                    batch_stats["fallbacks"] += 1
            if split is not None:
                return split if need_boundaries else [audio for audio, _ in split]
        return [synthesize(paragraphs[index]) for index in indices]

    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tts")
    pending = {}
    next_index, completed = 0, 0
    # Queue SYNTHESIS_LOOKAHEAD groups beyond the running ones, so the engine
    # starts the next request while this thread encodes and maps the previous
    # result. Only ``workers`` requests ever run at once.
    outstanding = workers + max(0, SYNTHESIS_LOOKAHEAD)
    try:
        for index in range(len(groups)):
            _check_control(combined_cancel, pause_event)
            while next_index < min(len(groups), index + outstanding):
                pending[next_index] = pool.submit(synthesize_group, next_index)
                next_index += 1
            future = pending[index]
            while not future.done():
                _check_control(combined_cancel, pause_event)
                for other in pending.values():
                    if other.done() and not other.cancelled() and other.exception():
                        other.result()
                wait([future], timeout=0.1)
            try:
                results = future.result()
            except ConversionCancelled:
                raise
            except Exception as error:
                raise SynthesisError(f"Paragraph {groups[index][0] + 1}/{len(paragraphs)} failed: {error}") from error
            del pending[index]
            for result in results:
                _check_control(combined_cancel, pause_event)
                completed += 1
                if progress_cb and (completed % max(1, len(paragraphs) // 10) == 0 or completed == len(paragraphs)):
                    progress_cb(f"    {completed}/{len(paragraphs)} paragraphs synthesized")
                yield result
        if progress_cb and (batch_stats["requests"] or batch_stats["fallbacks"]):
            summary = (f"    Batched {batch_stats['paragraphs']} short paragraphs into "
                       f"{batch_stats['requests']} requests")
            if batch_stats["fallbacks"]:
                summary += f"; {batch_stats['fallbacks']} batch(es) fell back to single requests"
            progress_cb(summary)
    finally:
        stopped.set()
        pool.shutdown(wait=False, cancel_futures=True)


def synthesize_paragraphs(paragraphs, voice, rate, workers, progress_cb,
                          cancel_event=None, pause_event=None,
                          audio_format="audio-24khz-48kbitrate-mono-mp3",
                          need_boundaries=False, sentence_pause_ms=0,
                          bitrate_kbps=48, *, backend=None, cache=None):
    """Compatibility wrapper; conversions consume the bounded iterator directly."""
    return list(iter_synthesized_paragraphs(
        paragraphs, voice, rate, workers, progress_cb, cancel_event, pause_event,
        audio_format, need_boundaries, sentence_pause_ms, bitrate_kbps,
        backend=backend, cache=cache))


def build_smil(paragraphs, audio_chunks,
               html_zip_path, audio_zip_path, smil_zip_dir,
               bytes_per_ms=6.0, span_data=None, durations_ms=None):
    """Generate continuous SMIL intervals on the chapter audio clock.

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

    if durations_ms is None:
        durations_ms = [bytes_to_ms(len(audio), bytes_per_ms) for audio in audio_chunks]
    if len(durations_ms) != len(paragraphs):
        raise ValueError("Every paragraph must have a measured audio duration.")
    for i, (para, para_dur) in enumerate(zip(paragraphs, durations_ms)):
        if para_dur == 0:
            continue  # Explicit continue-on-failure mode omitted this paragraph.
        if not math.isfinite(para_dur) or para_dur < 0:
            raise ValueError(f"Paragraph {i + 1} has no complete audio.")
        spans = span_data[i] if span_data and i < len(span_data) else []
        spans = spans or [{"id": para["id"], "offset": 0.0}]
        starts = [0.0]
        for span in spans[1:]:
            offset = float(span["offset"])
            if not math.isfinite(offset) or offset < starts[-1] or offset > para_dur:
                raise ValueError(f"Paragraph {i + 1} has invalid synchronization offsets.")
            starts.append(offset)
        ends = starts[1:] + [para_dur]
        for span, start, end in zip(spans, starts, ends):
            if end <= start:
                continue
            par_counter += 1
            text_src = quoteattr(f"{html_rel}#{span['id']}")
            audio_src = quoteattr(audio_rel)
            pars.append(
                f'{indent}<par id="par{par_counter:04d}">\n'
                f'{indent}  <text src={text_src}/>\n'
                f'{indent}  <audio src={audio_src}'
                f' clipBegin="{ms_to_clock(cursor + start)}"'
                f' clipEnd="{ms_to_clock(cursor + end)}"/>\n'
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
    from epub_migration import MANAGED_META, migrate_opf

    ns = OPF_NS
    migrate_opf(root)
    manifest = root.find(f"{{{ns}}}manifest")
    metadata = root.find(f"{{{ns}}}metadata")
    managed_overlays = []

    for old in list(metadata):
        if (old.get("name") == MANAGED_META or
                (not old.get("refines") and old.get("property") in {
                    "media:duration", "media:narrator", "media:active-class"})):
            metadata.remove(old)

    for ch in chapter_items:
        smil_id = _unique_manifest_id(manifest, ch["smil_id"], ns)
        audio_id = _unique_manifest_id(manifest, ch["audio_id"], ns)
        managed_overlays.append(smil_id)

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
         if el.get("property") == "dcterms:modified" and not el.get("refines")),
        None,
    )
    if mod is None:
        mod = ET.SubElement(metadata, f"{{{ns}}}meta")
        mod.set("property", "dcterms:modified")
    mod.text = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for duplicate in list(metadata):
        if (duplicate is not mod and duplicate.get("property") == "dcterms:modified"
                and not duplicate.get("refines")):
            metadata.remove(duplicate)

    mt = ET.SubElement(metadata, f"{{{ns}}}meta")
    mt.set("property", "media:duration")
    mt.text = ms_to_clock(total_duration_ms)

    mn = ET.SubElement(metadata, f"{{{ns}}}meta")
    mn.set("property", "media:narrator")
    mn.text = voice_name

    mc = ET.SubElement(metadata, f"{{{ns}}}meta")
    mc.set("property", "media:active-class")
    mc.text = "-epub-media-overlay-active"

    if managed_overlays:
        ET.SubElement(metadata, f"{{{ns}}}meta", {
            "name": MANAGED_META, "content": " ".join(managed_overlays)})

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


def _resolve_conversion_backend(backend, progress_cb):
    """Capture engine identity before consulting persistent audio checkpoints."""
    backend = dict(_tts_backend if backend is None else backend)
    if backend["provider"] != "kokoro" or backend.get("model_revision"):
        return backend
    endpoint = backend["kokoro_url"].rstrip("/")
    try:
        with urlopen(Request(endpoint + "/epub-tts/revision"), timeout=5) as response:
            revision = json.loads(response.read()).get("revision")
        if not isinstance(revision, str) or not revision.strip():
            raise SynthesisError("Kokoro returned an invalid engine revision.")
        backend["model_revision"] = revision
    except HTTPError as error:
        if error.code != 404:
            raise SynthesisError(f"Cannot check Kokoro engine identity: HTTP {error.code}") from error
        version = _kokoro_api_version(endpoint)
        backend["model_revision"] = f"kokoro-fastapi-{version or 'unversioned'}-pcm-v2"
        progress_cb(f"  Kokoro-FastAPI version: {version or 'unknown'}. Checkpoints are kept per "
                    "version; clear .epub_tts_cache after replacing model or voice files by hand.")
    except (OSError, URLError) as error:
        raise SynthesisError(f"Cannot reach Kokoro at {endpoint}: {error}") from error
    return backend


def _kokoro_api_version(endpoint):
    """Release reported by a stock server's OpenAPI document (0.2.x reports 1.0.0)."""
    try:
        with urlopen(Request(endpoint + "/openapi.json"), timeout=5) as response:
            version = json.loads(response.read()).get("info", {}).get("version")
    except (OSError, URLError, ValueError, AttributeError):
        return None
    return version.strip() if isinstance(version, str) and version.strip() else None


def _conversion_cache(input_path, output_parent, backend):
    from audio_pipeline import RequestCache, require_audio_encoder
    if backend['provider'] == 'kokoro':
        require_audio_encoder()
    cache = RequestCache(Path(output_parent) / '.epub_tts_cache', input_path, {
        'backend': {key: value for key, value in backend.items()
                    if key not in {'continue_on_error', 'keep_checkpoints'}},
        'pipeline_version': 4,
        'pcm_contract': 'signed16le-mono-24000', 'normalization_version': 2,
    })
    cache.used_requests = []
    cache.completed_requests = []
    cache.failures = []
    return cache


def _chapter_cache_request(ch_idx, href, html_bytes, voice, rate, audio_format,
                           bitrate_kbps, para_pause_ms, sentence_pause_ms, **extra):
    return dict(kind='chapter', version=4, chapter=ch_idx, href=href,
                source_sha256=hashlib.sha256(html_bytes).hexdigest(),
                voice=voice, rate=rate, audio_format=audio_format,
                bitrate_kbps=bitrate_kbps, para_pause_ms=para_pause_ms,
                sentence_pause_ms=sentence_pause_ms, **extra)


def _prune_chapter_requests(cache):
    # Only call after the chapter audio and its metadata are committed together.
    # Duplicate request keys are harmless and common for short repeated text.
    for request in cache.used_requests:
        cache.discard(request)
    cache.used_requests.clear()


def _finish_conversion_cache(cache, backend, progress_cb):
    """Reclaim only this successful run's checkpoints when explicitly selected."""
    if cache.failures or backend.get('keep_checkpoints', True):
        return
    for request in cache.completed_requests + cache.used_requests:
        try:
            cache.discard(request)
        except OSError as error:
            progress_cb(f'WARNING: Output is complete, but checkpoint cleanup failed: {error}')


def _copy_stream(source, destination, cancel_event=None, pause_event=None):
    while True:
        _check_control(cancel_event, pause_event)
        chunk = source.read(1024 * 1024)
        if not chunk:
            break
        destination.write(chunk)


def _write_conversion_warnings(path, failures):
    """Persist an explicit record whenever the user chose partial narration."""
    from audio_pipeline import atomic_output
    path = Path(path)
    heading = 'EPUB TTS conversion warnings\n'
    if failures:
        lines = [heading, '\nThe user enabled continuing after synthesis failures.\n',
                 'The following passages could not be narrated:\n']
        for index, failure in enumerate(failures, 1):
            lines.append(f"\n{index}. {failure.get('error', 'Unknown synthesis error')}\n")
            lines.append(f"Text: {failure.get('text', '')}\n")
        with atomic_output(path) as temporary:
            temporary.write_text(''.join(lines), encoding='utf-8')
    elif path.is_file():
        # Clear only a report that this application wrote for the same output.
        with path.open('r', encoding='utf-8', errors='replace') as previous:
            managed = previous.readline() == heading
        if managed:
            path.unlink()


def _stage_paragraph_audio(paragraphs, stage_path, voice, rate, workers,
                           progress_cb, cancel_event, pause_event, audio_format,
                           bytes_per_ms, bitrate_kbps, sentence_pause_ms,
                           pauses_after, backend, cache, *, need_boundaries=False,
                           on_boundaries=None, leading_pause_ms=0,
                           progress_pct_cb=None):
    """Consume ordered results into one disk chapter, never an audio list."""
    from contextlib import ExitStack
    from audio_pipeline import PCMChapterWriter, atomic_output
    pcm = backend['provider'] == 'kokoro'
    durations_ms = []
    cache.used_requests.clear()
    with ExitStack() as cleanup:
        if pcm:
            writer = cleanup.enter_context(PCMChapterWriter(
                stage_path, bitrate_kbps,
                cancel_check=lambda: _check_control(cancel_event, pause_event)))
            append_audio = lambda data: writer.append_pcm(data) * 1000 / 24000
            append_pause = lambda ms: writer.append_silence(ms) * 1000 / 24000
        else:
            temporary = cleanup.enter_context(atomic_output(stage_path))
            stream = cleanup.enter_context(open(temporary, 'wb'))
            def append_audio(data):
                _check_control(cancel_event, pause_event)
                if not data:
                    raise SynthesisError('The engine returned no audio.')
                stream.write(data)
                return bytes_to_ms(len(data), bytes_per_ms)
            def append_pause(ms):
                return append_audio(generate_mp3_silence(ms, bitrate_kbps)) if ms > 0 else 0
        total_ms = append_pause(leading_pause_ms) if leading_pause_ms else 0
        results = iter_synthesized_paragraphs(
            paragraphs, voice, rate, workers, progress_cb, cancel_event,
            pause_event=pause_event, audio_format=audio_format,
            need_boundaries=need_boundaries, sentence_pause_ms=sentence_pause_ms,
            bitrate_kbps=bitrate_kbps, backend=backend, cache=cache)
        try:
            for index, result in enumerate(results):
                if index >= len(paragraphs):
                    raise SynthesisError('The engine returned too many paragraph results.')
                _check_control(cancel_event, pause_event)
                audio, boundaries = result if need_boundaries else (result, [])
                failed_empty = not audio and backend.get('continue_on_error', False)
                duration = 0 if failed_empty else append_audio(audio)
                del audio, result
                if not failed_empty and pauses_after[index] > 0:
                    duration += append_pause(pauses_after[index])
                durations_ms.append(duration)
                total_ms += duration
                if on_boundaries is not None:
                    on_boundaries(index, boundaries)
                if progress_pct_cb:
                    progress_pct_cb(len(paragraphs[index]['text']))
            if len(durations_ms) != len(paragraphs):
                raise SynthesisError('The engine returned incomplete paragraph results.')
            _check_control(cancel_event, pause_event)
            if pcm:
                if writer.sample_count:
                    total_ms = writer.finish().duration_ms
                elif backend.get('continue_on_error', False):
                    writer.abort()
                    # The caller omits an empty chapter from the output entirely.
                    Path(stage_path).write_bytes(b'')
                else:
                    raise SynthesisError('The chapter contains no audio.')
        finally:
            if hasattr(results, 'close'):
                results.close()
    return durations_ms, total_ms


def _safe_output_path(input_path, output_path):
    from audio_pipeline import preflight_output
    if Path(input_path).resolve() == Path(output_path).resolve():
        raise ValueError('Choose an output path different from the input book.')
    return preflight_output(output_path)


def convert_epub_to_epub3(input_path, output_path, voice, rate, workers,
                          progress_cb, cancel_event=None, pause_event=None,
                          audio_format="audio-24khz-48kbitrate-mono-mp3",
                          bytes_per_ms=6.0,
                          para_pause_ms=0, sentence_pause_ms=0,
                          bitrate_kbps=48, skip_backmatter=False,
                          skip_frontmatter=False,
                          progress_pct_cb=None, backend=None):
    """Build an EPUB atomically from durable, disk-staged chapter audio."""
    from audio_pipeline import atomic_output
    from epub_migration import prepare_epub_for_conversion
    destination = _safe_output_path(input_path, output_path)
    backend = _resolve_conversion_backend(backend, progress_cb)
    if backend['provider'] == 'kokoro':
        audio_format = 'kokoro-pcm'
    _check_control(cancel_event, pause_event)
    cache = _conversion_cache(input_path, destination.parent, backend)
    with zipfile.ZipFile(input_path, 'r') as zf, tempfile.TemporaryDirectory(
            prefix='build-', dir=cache.directory) as staging:
        if _check_drm(zf, progress_cb):
            raise SynthesisError('This EPUB is DRM-protected and cannot be converted.')
        opf_path = find_opf_path(zf)
        opf_root, manifest, spine, opf_dir = parse_opf(zf, opf_path)
        removed_names, modified = prepare_epub_for_conversion(zf, opf_root, opf_dir)
        chapters, skipped_dupes = resolve_spine_chapters(manifest, spine, opf_dir)
        progress_cb(f'Found {len(chapters)} unique chapter file(s) in spine' +
                    (f' ({skipped_dupes} duplicate reference(s) skipped).' if skipped_dupes else '.'))
        new_files, audio_files, ch_items, chapter_nav_info = {}, {}, [], []
        existing_names = set(zf.namelist()) - removed_names
        for ch_idx, (spine_id, href) in enumerate(chapters, 1):
            _check_control(cancel_event, pause_event)
            html_zip_path = epub_path(opf_dir, href)
            progress_cb(f'[Ch {ch_idx:03d}/{len(chapters):03d}]  {href}')
            html_bytes = modified[html_zip_path] if html_zip_path in modified else zf.read(html_zip_path)
            if _is_footnote_file(href):
                progress_cb('  -> Footnote file, skipping.')
                continue
            if ((skip_backmatter and _is_backmatter(html_bytes, href)) or
                    (skip_frontmatter and _is_frontmatter(html_bytes, href))):
                progress_cb('  -> Front/back matter detected, skipping TTS.')
                chapter_nav_info.append({'href': href, 'title': f'Chapter {ch_idx}'})
                continue
            soup, paragraphs = process_chapter(html_bytes, ch_idx)
            if not paragraphs:
                progress_cb('  -> No paragraphs found, skipping.')
                chapter_nav_info.append({'href': href, 'title': f'Chapter {ch_idx}'})
                continue
            chapter_nav_info.append({'href': href, 'title': paragraphs[0]['text'][:80], 'ch_idx': ch_idx})
            audio_href = _unique_zip_href(opf_dir, f'Audio/ch{ch_idx:03d}.mp3', existing_names)
            smil_href = _unique_zip_href(opf_dir, f'SMIL/ch{ch_idx:03d}.smil', existing_names)
            audio_zip, smil_zip = epub_path(opf_dir, audio_href), epub_path(opf_dir, smil_href)
            existing_names.update((audio_zip, smil_zip))
            request = _chapter_cache_request(ch_idx, href, html_bytes, voice, rate,
                audio_format, bitrate_kbps, para_pause_ms, sentence_pause_ms,
                output='epub3', audio_zip=audio_zip, smil_zip=smil_zip,
                bytes_per_ms=bytes_per_ms, spine_id=spine_id)
            cache.completed_requests.append(request)
            checkpoint = cache.get(request)
            if checkpoint is not None:
                data = checkpoint.metadata
                progress_cb('  -> Reusing completed chapter checkpoint.')
                if progress_pct_cb:
                    progress_pct_cb(sum(len(p['text']) for p in paragraphs))
            else:
                progress_cb(f'  {len(paragraphs)} paragraph(s) to synthesize...')
                stage_path = Path(staging) / f'ch{ch_idx:03d}.mp3'
                failures_before = len(cache.failures)
                span_data = []
                def on_boundaries(index, bounds):
                    span_data.append(inject_word_spans(soup, paragraphs[index], bounds, ch_idx, index + 1))
                durations_ms, _ = _stage_paragraph_audio(
                    paragraphs, stage_path, voice, rate, workers, progress_cb,
                    cancel_event, pause_event, audio_format, bytes_per_ms, bitrate_kbps,
                    sentence_pause_ms, [para_pause_ms] * (len(paragraphs) - 1) + [0],
                    backend, cache, need_boundaries=True, on_boundaries=on_boundaries,
                    progress_pct_cb=progress_pct_cb)
                if not any(durations_ms):
                    progress_cb('  -> WARNING: no audio succeeded in this chapter; narration omitted.')
                    stage_path.unlink(missing_ok=True)
                    continue
                smil_xml, duration_ms = build_smil(
                    paragraphs, None, html_zip_path, audio_zip, posixpath.dirname(smil_zip),
                    bytes_per_ms=bytes_per_ms, span_data=span_data, durations_ms=durations_ms)
                ch_item = dict(spine_id=spine_id, smil_id=f'overlay-ch{ch_idx:03d}',
                    smil_href=smil_href, audio_id=f'audio-ch{ch_idx:03d}',
                    audio_href=audio_href, duration_ms=duration_ms)
                data = {'xhtml_base64': base64.b64encode(_to_xhtml_bytes(soup)).decode('ascii'),
                        'smil': smil_xml, 'chapter_item': ch_item}
                if len(cache.failures) == failures_before:
                    checkpoint = cache.put(request, stage_path, data)
                    _prune_chapter_requests(cache)
                    stage_path.unlink()
                else:
                    # Explicit partial output is never mistaken for a completed
                    # chapter on the next run. Successful requests remain cached.
                    from audio_pipeline import CachedSegment
                    checkpoint = CachedSegment(stage_path, data, stage_path.stat().st_size)
            modified[html_zip_path] = base64.b64decode(data['xhtml_base64'], validate=True)
            new_files[smil_zip] = data['smil'].encode('utf-8')
            audio_files[audio_zip] = checkpoint.path
            ch_items.append(data['chapter_item'])
            progress_cb(f"  -> {ms_to_human(data['chapter_item']['duration_ms'])}")
        has_nav = any('nav' in item.get('properties', '').split()
                      for item in opf_root.findall(f'.//{{{OPF_NS}}}manifest/{{{OPF_NS}}}item'))
        nav_rel_href = None
        if not has_nav:
            nav_rel_href = _unique_zip_href(opf_dir, 'nav.xhtml', existing_names)
            nav_info = _nav_info_from_ncx(zf, manifest, opf_dir) or chapter_nav_info
            new_files[epub_path(opf_dir, nav_rel_href)] = _generate_nav_xhtml(nav_info)
        total_ms = sum(item['duration_ms'] for item in ch_items)
        modified[opf_path] = patch_opf(opf_root, ch_items, total_ms, voice, nav_href=nav_rel_href)
        _check_control(cancel_event, pause_event)
        progress_cb('Writing output EPUB3...')
        if cache.failures:
            _write_conversion_warnings(str(destination) + '.warnings.txt', cache.failures)
        with atomic_output(destination) as temporary:
            with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as out:
                out.writestr(zipfile.ZipInfo('mimetype'), 'application/epub+zip',
                             compress_type=zipfile.ZIP_STORED)
                for item in zf.infolist():
                    name = item.filename
                    if name == 'mimetype' or name in removed_names or name in new_files or name in audio_files:
                        continue
                    _check_control(cancel_event, pause_event)
                    if name in modified:
                        out.writestr(name, modified[name])
                    else:
                        with zf.open(item) as source, out.open(item, 'w', force_zip64=True) as target:
                            _copy_stream(source, target, cancel_event, pause_event)
                for name, data in new_files.items():
                    _check_control(cancel_event, pause_event)
                    out.writestr(name, data)
                for name, path in audio_files.items():
                    info = zipfile.ZipInfo(name)
                    info.compress_type = zipfile.ZIP_STORED
                    with path.open('rb') as source, out.open(info, 'w', force_zip64=True) as target:
                        _copy_stream(source, target, cancel_event, pause_event)
            _check_control(cancel_event, pause_event)
    _write_conversion_warnings(str(destination) + '.warnings.txt', cache.failures)
    _finish_conversion_cache(cache, backend, progress_cb)
    progress_cb(f"{'Completed WITH WARNINGS' if cache.failures else 'Done!'}  Total audio: {ms_to_human(total_ms)}")
    progress_cb(f'Output: {output_path}')
    return output_path


def convert_epub_to_mp3(input_path, output_dir, voice, rate, workers,
                        progress_cb, cancel_event=None, pause_event=None,
                        audio_format="audio-24khz-48kbitrate-mono-mp3",
                        bytes_per_ms=6.0, para_pause_ms=0, sentence_pause_ms=0,
                        bitrate_kbps=48, skip_backmatter=False, skip_frontmatter=False,
                        progress_pct_cb=None, backend=None):
    """Export complete chapters atomically, retaining durable request checkpoints."""
    from audio_pipeline import atomic_output, preflight_output
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    preflight_output(Path(output_dir) / 'ch001.mp3')
    backend = _resolve_conversion_backend(backend, progress_cb)
    if backend['provider'] == 'kokoro':
        audio_format = 'kokoro-pcm'
    _check_control(cancel_event, pause_event)
    cache = _conversion_cache(input_path, Path(output_dir).parent, backend)
    with zipfile.ZipFile(input_path, 'r') as zf, tempfile.TemporaryDirectory(
            prefix='build-', dir=cache.directory) as staging:
        if _check_drm(zf, progress_cb):
            raise SynthesisError('This EPUB is DRM-protected and cannot be converted.')
        opf_path = find_opf_path(zf)
        _, manifest, spine, opf_dir = parse_opf(zf, opf_path)
        chapters, skipped_dupes = resolve_spine_chapters(manifest, spine, opf_dir)
        progress_cb(f'Found {len(chapters)} unique chapter file(s) in spine' +
                    (f' ({skipped_dupes} duplicate reference(s) skipped).' if skipped_dupes else '.'))
        for ch_idx, (_, href) in enumerate(chapters, 1):
            _check_control(cancel_event, pause_event)
            progress_cb(f'[Ch {ch_idx:03d}/{len(chapters):03d}]  {href}')
            html_bytes = zf.read(epub_path(opf_dir, href))
            if (_is_footnote_file(href) or
                    (skip_backmatter and _is_backmatter(html_bytes, href)) or
                    (skip_frontmatter and _is_frontmatter(html_bytes, href))):
                progress_cb('  -> Skipping footnotes/front/back matter.')
                continue
            _, paragraphs = process_chapter(html_bytes, ch_idx)
            if not paragraphs:
                progress_cb('  -> No paragraphs found, skipping.')
                continue
            request = _chapter_cache_request(ch_idx, href, html_bytes, voice, rate,
                audio_format, bitrate_kbps, para_pause_ms, sentence_pause_ms,
                output='mp3', bytes_per_ms=bytes_per_ms)
            cache.completed_requests.append(request)
            checkpoint = cache.get(request)
            if checkpoint is None:
                stage_path = Path(staging) / f'ch{ch_idx:03d}.mp3'
                failures_before = len(cache.failures)
                _, duration_ms = _stage_paragraph_audio(
                    paragraphs, stage_path, voice, rate, workers, progress_cb,
                    cancel_event, pause_event, audio_format, bytes_per_ms, bitrate_kbps,
                    sentence_pause_ms, [para_pause_ms] * (len(paragraphs) - 1) + [0],
                    backend, cache, progress_pct_cb=progress_pct_cb)
                if duration_ms <= 0:
                    progress_cb('  -> WARNING: no audio succeeded in this chapter; MP3 omitted.')
                    stage_path.unlink(missing_ok=True)
                    continue
                if len(cache.failures) == failures_before:
                    checkpoint = cache.put(request, stage_path, {'duration_ms': duration_ms})
                    _prune_chapter_requests(cache)
                    stage_path.unlink()
                else:
                    from audio_pipeline import CachedSegment
                    checkpoint = CachedSegment(stage_path, {'duration_ms': duration_ms}, stage_path.stat().st_size)
            else:
                progress_cb('  -> Reusing completed chapter checkpoint.')
                if progress_pct_cb:
                    progress_pct_cb(sum(len(p['text']) for p in paragraphs))
            mp3_path = Path(output_dir) / f'ch{ch_idx:03d}.mp3'
            _safe_output_path(input_path, mp3_path)
            if cache.failures:
                _write_conversion_warnings(Path(output_dir) / 'warnings.txt', cache.failures)
            with atomic_output(mp3_path) as temporary:
                with checkpoint.path.open('rb') as source, temporary.open('wb') as target:
                    _copy_stream(source, target, cancel_event, pause_event)
                _check_control(cancel_event, pause_event)
            progress_cb(f"  -> {ms_to_human(checkpoint.metadata['duration_ms'])}  ({mp3_path})")
    _write_conversion_warnings(Path(output_dir) / 'warnings.txt', cache.failures)
    _finish_conversion_cache(cache, backend, progress_cb)
    progress_cb(f"{'Completed WITH WARNINGS' if cache.failures else 'Done!'}  MP3s saved to: {output_dir}")
    return output_dir


def convert_text_to_mp3(input_path, output_path, voice, rate, workers,
                        progress_cb, cancel_event=None,
                        audio_format="audio-24khz-48kbitrate-mono-mp3",
                        bitrate_kbps=48, para_pause_ms=None, section_pause_ms=None,
                        sentence_pause_ms=0, progress_pct_cb=None,
                        pause_event=None, backend=None):
    """Stream text/Markdown narration, with pause/cancel and atomic output."""
    from audio_pipeline import atomic_output
    destination = _safe_output_path(input_path, output_path)
    backend = _resolve_conversion_backend(backend, progress_cb)
    if backend['provider'] == 'kokoro':
        audio_format = 'kokoro-pcm'
    _check_control(cancel_event, pause_event)
    with open(input_path, 'r', encoding='utf-8', errors='replace') as source:
        text = source.read()
    if not text.strip():
        progress_cb('File is empty, nothing to synthesize.')
        return None
    items = split_text_for_tts(text, para_pause_ms=para_pause_ms, section_pause_ms=section_pause_ms)
    paragraphs, pauses_after, leading_pause_ms = [], [], 0
    for item in items:
        if isinstance(item, str):
            paragraphs.append({'text': item})
            pauses_after.append(0)
        elif isinstance(item, tuple) and item[0] == _SILENCE:
            if pauses_after:
                pauses_after[-1] += item[1]
            else:
                leading_pause_ms += item[1]
    if not paragraphs:
        progress_cb('No synthesizable text found.')
        return None
    cache = _conversion_cache(input_path, destination.parent, backend)
    request = _chapter_cache_request(1, 'text', text.encode('utf-8'), voice, rate,
        audio_format, bitrate_kbps, para_pause_ms, sentence_pause_ms,
        output='text-mp3', section_pause_ms=section_pause_ms)
    cache.completed_requests.append(request)
    checkpoint = cache.get(request)
    if checkpoint is None:
        progress_cb(f'{len(paragraphs)} text chunk(s) to synthesize...')
        with tempfile.TemporaryDirectory(prefix='build-', dir=cache.directory) as staging:
            stage_path = Path(staging) / 'text.mp3'
            _, duration_ms = _stage_paragraph_audio(
                paragraphs, stage_path, voice, rate, workers, progress_cb,
                cancel_event, pause_event, audio_format, bitrate_kbps / 8, bitrate_kbps,
                sentence_pause_ms, pauses_after, backend, cache,
                leading_pause_ms=leading_pause_ms, progress_pct_cb=progress_pct_cb)
            if duration_ms <= 0:
                _write_conversion_warnings(str(destination) + '.warnings.txt', cache.failures)
                raise SynthesisError('No text could be narrated. No empty MP3 was published.')
            if not cache.failures:
                checkpoint = cache.put(request, stage_path, {'duration_ms': duration_ms})
                _prune_chapter_requests(cache)
            else:
                # Publish while the partial staged file still exists, retaining
                # successful request checkpoints for a later retry.
                _write_conversion_warnings(str(destination) + '.warnings.txt', cache.failures)
                with atomic_output(destination) as temporary:
                    with stage_path.open('rb') as source, temporary.open('wb') as target:
                        _copy_stream(source, target, cancel_event, pause_event)
                    _check_control(cancel_event, pause_event)
                progress_cb(f'Completed WITH WARNINGS  {output_path}')
                return output_path
    else:
        progress_cb('Reusing completed audio checkpoint.')
        if progress_pct_cb:
            progress_pct_cb(sum(len(p['text']) for p in paragraphs))
    with atomic_output(destination) as temporary:
        with checkpoint.path.open('rb') as source, temporary.open('wb') as target:
            _copy_stream(source, target, cancel_event, pause_event)
        _check_control(cancel_event, pause_event)
    _write_conversion_warnings(str(destination) + '.warnings.txt', cache.failures)
    _finish_conversion_cache(cache, backend, progress_cb)
    size_kb = destination.stat().st_size // 1024
    progress_cb(f'Done!  {output_path}  ({size_kb} KB)')
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
        self.quality_combo = ttk.Combobox(row_aq, textvariable=self.quality_var,
                      values=list(AUDIO_FORMATS.keys()), state="readonly", width=28)
        self.quality_combo.pack(side="left", padx=(4, 0))

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

        # Measured at most ~3% faster once requests are prefetched, so opt-in.
        self.batch_paragraphs_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame_settings, text="Batch short Kokoro paragraphs (experimental)",
                         variable=self.batch_paragraphs_var).pack(anchor="w", padx=4, pady=2)
        self.keep_checkpoints_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame_settings, text="Keep checkpoints after successful conversion",
                         variable=self.keep_checkpoints_var).pack(anchor="w", padx=4, pady=2)
        self.continue_on_error_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame_settings, text="Continue after failed passages (save warnings)",
                         variable=self.continue_on_error_var).pack(anchor="w", padx=4, pady=2)

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
        formats = KOKORO_AUDIO_FORMATS if provider == "kokoro" else AUDIO_FORMATS
        self.quality_combo["values"] = list(formats)
        if self.quality_var.get() not in formats:
            self.quality_var.set(next(iter(formats)))

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
                audio = synthesize_chunk(
                    "This is a sample of the selected voice.",
                    voice, "+0%", backend={"provider": provider, "kokoro_url": kokoro_url})
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
                if not self.worker_thread or not self.worker_thread.is_alive():
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
        # Keep preview inference from competing with a running conversion.
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
            "continue_on_error": self.continue_on_error_var.get(),
            "batch_paragraphs": self.batch_paragraphs_var.get(),
            "keep_checkpoints": self.keep_checkpoints_var.get(),
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
        if self.close_var.get() and getattr(self, "_batch_success", False):
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
                            n += count_chapter_characters(data)
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
        provider = cfg["provider"]
        kokoro_url = cfg["kokoro_url"]
        backend = {"provider": provider, "kokoro_url": kokoro_url,
                   "continue_on_error": cfg.get("continue_on_error", False),
                   "batch_paragraphs": cfg.get("batch_paragraphs", False),
                   "keep_checkpoints": cfg.get("keep_checkpoints", False)}
        formats = KOKORO_AUDIO_FORMATS if provider == "kokoro" else AUDIO_FORMATS
        audio_format, bytes_per_ms, bitrate_kbps = formats.get(quality_key, next(iter(formats.values())))
        self._batch_success = False
        completed, failed = 0, 0
        warning_count = [0]

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
            if "WARNING:" in msg:
                warning_count[0] += 1
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
                            backend=backend,
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
                            backend=backend,
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
                        backend=backend, pause_event=self.pause_event,
                    )
                    self._last_output_dir = os.path.dirname(output)
                else:
                    raise ValueError(f"Unsupported file type: {ext}")
                completed += 1
            except ConversionCancelled:
                self._send("log", "Cancelled. Completed audio checkpoints were retained for retry.")
                break
            except Exception as e:
                failed += 1
                self._send("log", f"  ERROR: {e}")


        if not self.cancel_event.is_set():
            elapsed = time.time() - batch_start
            if elapsed >= 3600:
                t_str = f"{int(elapsed // 3600)}h {int((elapsed % 3600) // 60)}m"
            else:
                t_str = f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
            self._send("log", f"\nFinished in {t_str}: {completed} completed, {failed} failed, "
                       f"{warning_count[0]} warning(s).")
        self._batch_success = completed == total_files and failed == 0 and not warning_count[0] and not self.cancel_event.is_set()
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
