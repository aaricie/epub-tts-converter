"""Durable TTS checkpoints and bounded-memory, sample-accurate chapter encoding.

Cache callers must include *every* synthesis-affecting setting in ``context`` or
the request: provider endpoint and model/version contract, voice, rate, text,
normalization version and requested format. Encoding-only settings belong in a
completed-chapter key, so changing bitrate does not invalidate unfinished PCM.
PCM throughout this module is 24 kHz, mono, signed 16-bit little endian.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Callable, Mapping


SAMPLE_RATE = 24000
SAMPLE_BYTES = 2
PCM_FORMAT = "pcm_s16le_24000_mono"
SUPPORTED_BITRATES = (48, 64, 96, 128)
_IO_CHUNK = 1024 * 1024
_PCM_CHUNK = SAMPLE_RATE * SAMPLE_BYTES  # One second, regardless of chapter size.
_CACHE_SCHEMA = 1


class AudioCancelled(RuntimeError):
    """Raised when a cancellation callback returns a truthy value."""


def _check_cancel(callback):
    if callback is not None and callback():
        raise AudioCancelled("Audio processing was cancelled.")


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def file_fingerprint(path, cancel_check=None) -> dict:
    """Hash a file with bounded reads; reject a source changed during hashing."""
    path = Path(path)
    before = path.stat()
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while True:
            _check_cancel(cancel_check)
            chunk = source.read(_IO_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or size != before.st_size:
        raise OSError(f"File changed while it was being read: {path}")
    return {"sha256": digest.hexdigest(), "size_bytes": size}


def preflight_output(destination) -> Path:
    """Fail before synthesis if a sibling temporary output cannot be created.

    The parent must exist. This never truncates an existing destination.
    Final replacement can still fail if permissions or file locks change later.
    """
    destination = Path(destination).absolute()
    if not destination.parent.is_dir():
        raise FileNotFoundError(f"Output directory does not exist: {destination.parent}")
    if destination.exists() and not destination.is_file():
        raise IsADirectoryError(f"Output is not a regular file: {destination}")
    if destination.exists() and not os.access(destination, os.W_OK):
        raise PermissionError(f"Output is not writable: {destination}")
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".preflight",
                                     dir=destination.parent)
    os.close(fd)
    Path(temporary).unlink()
    return destination


def _temporary_path(destination: Path) -> Path:
    fd, name = tempfile.mkstemp(prefix=f".{destination.stem}.",
                                suffix=f".tmp{destination.suffix}", dir=destination.parent)
    os.close(fd)
    return Path(name)


def _publish(temporary: Path, destination: Path):
    # Windows' _commit requires a writable file descriptor.
    with temporary.open("rb+") as finished:
        os.fsync(finished.fileno())
    os.replace(temporary, destination)


@contextmanager
def atomic_output(destination):
    """Yield a unique sibling path; replace destination only after success.

    A caller must raise if its conversion or validation is incomplete. On any
    exception only this context's temporary file is removed, never the existing
    destination or another conversion's temporary file.
    """
    destination = preflight_output(destination)
    temporary = _temporary_path(destination)
    try:
        yield temporary
        _publish(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class CachedSegment:
    path: Path
    metadata: dict
    size_bytes: int


class RequestCache:
    """Content-addressed, validated checkpoints scoped to a book and context.

    ``input_fingerprint`` can reuse a previously computed ``file_fingerprint``.
    Metadata is the commit record: a missing, truncated or mismatched record is
    a cache miss. Audio and metadata are both hashed, including length. The
    cache remains reusable after cancellation and does not depend on the final
    output filename. No file bytes are retained by this class.
    """

    def __init__(self, root, input_path, context: Mapping, *,
                 input_fingerprint=None, cancel_check=None):
        self.cancel_check = cancel_check
        self.input_fingerprint = dict(input_fingerprint or file_fingerprint(input_path, cancel_check))
        # Round-trip to take a stable, independent copy and reject non-JSON data.
        self.context = json.loads(_canonical(dict(context)))
        self.context_key = _digest(self.context)
        self.directory = (Path(root).absolute() / self.input_fingerprint["sha256"] /
                          self.context_key)
        self.directory.mkdir(parents=True, exist_ok=True)

    def key(self, request: Mapping) -> str:
        return _digest({"schema": _CACHE_SCHEMA, "context": self.context_key,
                        "input": self.input_fingerprint, "request": request})

    def _paths(self, request):
        key = self.key(request)
        return key, self.directory / f"{key}.bin", self.directory / f"{key}.json"

    def get(self, request: Mapping) -> CachedSegment | None:
        key, audio_path, record_path = self._paths(request)
        _check_cancel(self.cancel_check)
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
            checksum = record.pop("record_sha256")
            if (checksum != _digest(record) or record["schema"] != _CACHE_SCHEMA or
                    record["key"] != key or record["context"] != self.context_key or
                    record["input"] != self.input_fingerprint or
                    not isinstance(record["metadata"], dict)):
                return None
            fingerprint = file_fingerprint(audio_path, self.cancel_check)
            if fingerprint != record["audio"]:
                return None
            return CachedSegment(audio_path, record["metadata"], fingerprint["size_bytes"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def put(self, request: Mapping, audio: bytes | Path, metadata: Mapping) -> CachedSegment:
        """Persist bytes or copy a staged file, then atomically commit metadata."""
        key, audio_path, record_path = self._paths(request)
        metadata = json.loads(_canonical(dict(metadata)))
        digest = hashlib.sha256()
        size = 0
        with atomic_output(audio_path) as temporary:
            with temporary.open("wb") as target:
                if isinstance(audio, (bytes, bytearray, memoryview)):
                    view = memoryview(audio)
                    for offset in range(0, len(view), _IO_CHUNK):
                        _check_cancel(self.cancel_check)
                        chunk = view[offset:offset + _IO_CHUNK]
                        target.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                else:
                    with Path(audio).open("rb") as source:
                        while True:
                            _check_cancel(self.cancel_check)
                            chunk = source.read(_IO_CHUNK)
                            if not chunk:
                                break
                            target.write(chunk)
                            digest.update(chunk)
                            size += len(chunk)
                target.flush()
            _check_cancel(self.cancel_check)
        record = {"schema": _CACHE_SCHEMA, "key": key, "context": self.context_key,
                  "input": self.input_fingerprint,
                  "audio": {"sha256": digest.hexdigest(), "size_bytes": size},
                  "metadata": metadata}
        record["record_sha256"] = _digest(record)
        with atomic_output(record_path) as temporary:
            temporary.write_bytes(_canonical(record))
            _check_cancel(self.cancel_check)
        return CachedSegment(audio_path, metadata, size)

    def discard(self, request: Mapping):
        """Prune an unfinished PCM segment after its chapter is checkpointed."""
        _, audio_path, record_path = self._paths(request)
        # Remove the commit record first, so interrupted cleanup is just a miss.
        record_path.unlink(missing_ok=True)
        audio_path.unlink(missing_ok=True)


def require_audio_encoder():
    """Check the optional binary dependency and codec before making TTS calls."""
    try:
        av = importlib.import_module("av")
        av.codec.Codec("libmp3lame", "w")
        if not all(hasattr(av.Packet, method) for method in ("get_sidedata", "set_sidedata")):
            raise ImportError("PyAV 17 or later is required for exact MP3 padding metadata")
    except (ImportError, OSError, ValueError) as error:
        raise RuntimeError("Kokoro chapter encoding requires PyAV with libmp3lame. "
                           "Install the project requirements (python -m pip install -r requirements.txt) "
                           'or install PyAV with python -m pip install "av>=17".') from error
    return av


@dataclass(frozen=True)
class StagedChapter:
    path: Path
    duration_ms: float
    sample_count: int
    segment_durations_ms: tuple[float, ...]


class PCMChapterWriter:
    """Encode a chapter once while streaming bounded PCM chunks into PyAV.

    ``append_pcm`` returns its exact sample count and records one speech segment
    duration. ``append_silence`` returns its sample count but does not add a
    speech segment. Total duration includes all pauses. ``finish`` decodes the
    MP3 to verify sample count and format before publishing the staged file.
    Existing target files survive failures, cancellation and failed validation.
    """

    def __init__(self, target_path, bitrate_kbps=48, cancel_check: Callable | None = None,
                 *, compression_level=None):
        if bitrate_kbps not in SUPPORTED_BITRATES:
            raise ValueError(f"MP3 bitrate must be one of {SUPPORTED_BITRATES} kbps.")
        if compression_level is not None and (type(compression_level) is not int or
                                               not 0 <= compression_level <= 9):
            raise ValueError("MP3 compression_level must be an integer from 0 to 9, or None.")
        self.av = require_audio_encoder()
        self.path = preflight_output(target_path)
        self.cancel_check = cancel_check
        self.sample_count = 0
        self.segment_durations_ms = []
        self._finished = False
        self._closed = False
        self._container = None
        self._pending_packet = None
        self._trailing_discard = 0
        self._temporary = _temporary_path(self.path)
        try:
            _check_cancel(self.cancel_check)
            self._container = self.av.open(str(self._temporary), mode="w", format="mp3",
                                           options={"write_xing": "1"})
            self._stream = self._container.add_stream("libmp3lame", rate=SAMPLE_RATE, layout="mono")
            self._stream.bit_rate = int(bitrate_kbps * 1000)
            if compression_level is not None:
                self._stream.codec_context.options = {"compression_level": str(compression_level)}
        except BaseException:
            self.abort()
            raise

    def _ensure_open(self):
        if self._closed:
            raise RuntimeError("Chapter writer has already finished or was aborted.")

    def _append_chunk(self, chunk):
        _check_cancel(self.cancel_check)
        if len(chunk) % SAMPLE_BYTES:
            raise ValueError("PCM must contain complete signed 16-bit samples (even byte length).")
        if not chunk:
            return
        count = len(chunk) // SAMPLE_BYTES
        frame = self.av.AudioFrame(format="s16", layout="mono", samples=count)
        frame.planes[0].update(chunk)
        frame.sample_rate = SAMPLE_RATE
        frame.time_base = Fraction(1, SAMPLE_RATE)
        frame.pts = self.sample_count
        for packet in self._stream.encode(frame):
            self._queue_packet(packet)
        self.sample_count += count

    def _queue_packet(self, packet):
        """Carry all trailing skip samples on the final packet.

        FFmpeg 8's MP3 muxer retains only the last packet's discard count. LAME
        sometimes spreads padding over two packets, losing up to 46 samples at
        24 kHz. Consolidating that side data also works with newer muxers which
        sum discard counts. Only one compressed packet is buffered; no samples
        are added, removed or re-encoded. The muxer still writes the Xing tag.
        See FFmpeg libavformat/mp3enc.c mp3_write_audio_packet (8.0 vs master).
        """
        side = packet.get_sidedata("skip_samples")
        discard = int.from_bytes(bytes(side)[4:8], "little") if side else 0
        if self._pending_packet is not None:
            pending_side = self._pending_packet.get_sidedata("skip_samples")
            if pending_side:
                data = bytearray(pending_side)
                data[4:8] = bytes(4)
                pending_side.update(data)
                self._pending_packet.set_sidedata(pending_side)
            self._container.mux(self._pending_packet)
        self._trailing_discard = self._trailing_discard + discard if discard else 0
        self._pending_packet = packet

    def _mux_last_packet(self):
        if self._pending_packet is not None:
            side = self._pending_packet.get_sidedata("skip_samples")
            if side and self._trailing_discard:
                data = bytearray(side)
                data[4:8] = self._trailing_discard.to_bytes(4, "little")
                side.update(data)
                self._pending_packet.set_sidedata(side)
            self._container.mux(self._pending_packet)
            self._pending_packet = None

    def append_pcm(self, pcm: bytes | Path) -> int:
        self._ensure_open()
        start = self.sample_count
        try:
            if isinstance(pcm, (bytes, bytearray, memoryview)):
                view = memoryview(pcm)
                if not len(view) or len(view) % SAMPLE_BYTES:
                    raise ValueError("PCM audio must be nonempty and contain complete signed 16-bit samples.")
                for offset in range(0, len(view), _PCM_CHUNK):
                    self._append_chunk(view[offset:offset + _PCM_CHUNK])
            else:
                source_path = Path(pcm)
                expected_size = source_path.stat().st_size
                if not expected_size or expected_size % SAMPLE_BYTES:
                    raise ValueError("PCM audio must be nonempty and contain complete signed 16-bit samples.")
                with source_path.open("rb") as source:
                    while True:
                        chunk = source.read(_PCM_CHUNK)
                        if not chunk:
                            break
                        self._append_chunk(chunk)
                if (self.sample_count - start) * SAMPLE_BYTES != expected_size:
                    raise OSError(f"PCM source changed during encoding: {source_path}")
            count = self.sample_count - start
            self.segment_durations_ms.append(count * 1000 / SAMPLE_RATE)
            return count
        except BaseException:
            self.abort()
            raise

    def append_silence(self, duration_ms: float) -> int:
        self._ensure_open()
        try:
            if not math.isfinite(duration_ms) or duration_ms < 0:
                raise ValueError("Pause duration must be a finite nonnegative number of milliseconds.")
            count = round(duration_ms * SAMPLE_RATE / 1000)
            remaining = count * SAMPLE_BYTES
            silence = bytes(_PCM_CHUNK)
            while remaining:
                length = min(remaining, len(silence))
                self._append_chunk(silence[:length])
                remaining -= length
            return count
        except BaseException:
            self.abort()
            raise

    def finish(self) -> StagedChapter:
        self._ensure_open()
        try:
            _check_cancel(self.cancel_check)
            if not self.sample_count:
                raise ValueError("Cannot encode an empty chapter.")
            for packet in self._stream.encode(None):
                _check_cancel(self.cancel_check)
                self._queue_packet(packet)
            self._mux_last_packet()
            self._container.close()
            self._container = None
            decoded_samples = 0
            with self.av.open(str(self._temporary), mode="r", format="mp3") as check:
                for frame in check.decode(audio=0):
                    _check_cancel(self.cancel_check)
                    if frame.sample_rate != SAMPLE_RATE or len(frame.layout.channels) != 1:
                        raise RuntimeError("Encoded MP3 has an unexpected sample rate or channel count.")
                    decoded_samples += frame.samples
            if decoded_samples != self.sample_count:
                raise RuntimeError(f"Encoded MP3 duration changed: expected {self.sample_count} "
                                   f"samples, decoded {decoded_samples}. Chapter was not published.")
            _check_cancel(self.cancel_check)
            _publish(self._temporary, self.path)
            self._finished = True
            self._closed = True
            return StagedChapter(self.path, self.sample_count * 1000 / SAMPLE_RATE,
                                 self.sample_count, tuple(self.segment_durations_ms))
        except BaseException:
            self.abort()
            raise

    def abort(self):
        """Close the encoder and remove only this writer's temporary file."""
        self._closed = True
        try:
            if self._container is not None:
                self._container.close()
        finally:
            self._container = None
            self._temporary.unlink(missing_ok=True)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if not self._finished:
            self.abort()
        return False
