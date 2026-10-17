import hashlib
import json
from pathlib import Path
import struct

import pytest

import audio_pipeline as pipeline


def _cache(tmp_path, *, context=None, name="book.epub"):
    book = tmp_path / name
    if not book.exists():
        book.write_bytes(b"The unchanged source book")
    return pipeline.RequestCache(tmp_path / "cache", book, context or {
        "provider": "kokoro", "endpoint": "http://localhost:8880",
        "model_contract": "kokoro-v1-pcm-contract-1", "voice": "af_heart",
        "rate": 1, "normalization": "client-v1", "format": pipeline.PCM_FORMAT,
    })


def test_cache_durable_resume_and_canonical_request(tmp_path):
    cache = _cache(tmp_path)
    request = {"text": "Hello!", "rate": 1}
    metadata = {"words": [{"text": "Hello!", "start": 0, "end": 0.5}]}
    original = cache.put(request, b"\x01\x00" * 12, metadata)
    assert original.size_bytes == 24
    resumed = _cache(tmp_path).get({"rate": 1, "text": "Hello!"})
    assert resumed == original
    assert resumed.path.read_bytes() == b"\x01\x00" * 12
    assert resumed.metadata == metadata


def test_cache_context_and_book_changes_invalidate(tmp_path):
    request = {"text": "Hello!"}
    cache = _cache(tmp_path)
    cache.put(request, b"audio", {})
    for setting, value in [("voice", "am_adam"), ("rate", 1.2),
                           ("model_contract", "updated"), ("endpoint", "http://other:8880"),
                           ("normalization", "client-v2"), ("format", "mp3")]:
        changed = dict(cache.context, **{setting: value})
        assert _cache(tmp_path, context=changed).get(request) is None
    (tmp_path / "book.epub").write_bytes(b"An edited source book")
    assert _cache(tmp_path).get(request) is None


def test_cache_is_independent_of_input_filename_and_output_name(tmp_path):
    request = {"text": "Hello!"}
    cache = _cache(tmp_path)
    stored = cache.put(request, b"audio", {})
    assert _cache(tmp_path, name="renamed.epub").get(request) == stored


@pytest.mark.parametrize("corruption", ["same_size_audio", "truncated_audio", "metadata", "record_json", "record_missing"])
def test_cache_rejects_corruption(tmp_path, corruption):
    cache = _cache(tmp_path)
    request = {"text": "Hello!"}
    stored = cache.put(request, b"audio", {"duration_ms": 100})
    record_path = stored.path.with_suffix(".json")
    if corruption == "same_size_audio":
        stored.path.write_bytes(b"wrong")
    elif corruption == "truncated_audio":
        stored.path.write_bytes(b"a")
    elif corruption == "metadata":
        record = json.loads(record_path.read_text())
        record["metadata"]["duration_ms"] = 200
        record_path.write_text(json.dumps(record))
    elif corruption == "record_json":
        record_path.write_bytes(b'{"schema":')
    else:
        record_path.unlink()
    assert cache.get(request) is None


def test_cache_file_copy_and_discard_preserves_other_entries(tmp_path):
    cache = _cache(tmp_path)
    source = tmp_path / "source.pcm"
    source.write_bytes(b"\0\0" * 25000)
    one = cache.put({"text": "one"}, source, {})
    two = cache.put({"text": "two"}, b"audio", {})
    cache.discard({"text": "one"})
    assert not one.path.exists()
    assert cache.get({"text": "one"}) is None
    assert cache.get({"text": "two"}) == two
    assert source.exists()


def test_cache_precomputed_fingerprint_does_not_reread_book(tmp_path, monkeypatch):
    book = tmp_path / "book.epub"
    book.write_bytes(b"book")
    fingerprint = pipeline.file_fingerprint(book)
    assert fingerprint == {"sha256": hashlib.sha256(b"book").hexdigest(), "size_bytes": 4}
    def no_read(*args, **kwargs):
        raise AssertionError("Book must only be hashed once")
    monkeypatch.setattr(pipeline, "file_fingerprint", no_read)
    cache = pipeline.RequestCache(tmp_path / "cache", book, {}, input_fingerprint=fingerprint)
    assert cache.input_fingerprint == fingerprint


def test_failed_cache_write_is_not_a_checkpoint(tmp_path):
    cache = _cache(tmp_path)
    request = {"text": "Hello!"}
    calls = 0
    def cancel():
        nonlocal calls
        calls += 1
        return calls == 2
    cache.cancel_check = cancel
    with pytest.raises(pipeline.AudioCancelled):
        cache.put(request, bytes(2 * pipeline._IO_CHUNK), {})
    cache.cancel_check = None
    assert cache.get(request) is None
    assert list(cache.directory.iterdir()) == []


def test_atomic_output_keeps_original_when_producer_fails(tmp_path):
    output = tmp_path / "book.epub"
    output.write_bytes(b"existing complete book")
    with pytest.raises(RuntimeError, match="incomplete"):
        with pipeline.atomic_output(output) as temporary:
            temporary.write_bytes(b"partial new book")
            raise RuntimeError("incomplete")
    assert output.read_bytes() == b"existing complete book"
    assert list(tmp_path.iterdir()) == [output]
    with pipeline.atomic_output(output) as temporary:
        temporary.write_bytes(b"new complete book")
    assert output.read_bytes() == b"new complete book"


def test_preflight_missing_directory_does_not_start_output(tmp_path):
    with pytest.raises(FileNotFoundError, match="Output directory does not exist"):
        pipeline.preflight_output(tmp_path / "missing" / "book.epub")
    with pytest.raises(IsADirectoryError):
        pipeline.preflight_output(tmp_path)


def _pcm(count):
    # Deterministic audible waveform; avoids an additional NumPy dependency.
    return b"".join(struct.pack("<h", 10000 if index % 100 < 50 else -10000)
                    for index in range(count))


@pytest.mark.parametrize("bitrate", pipeline.SUPPORTED_BITRATES)
def test_encode_chapter_exact_samples_and_bitrate(tmp_path, bitrate):
    av = pytest.importorskip("av")
    count = 24001
    target = tmp_path / f"chapter-{bitrate}.mp3"
    with pipeline.PCMChapterWriter(target, bitrate_kbps=bitrate) as writer:
        assert writer.append_pcm(_pcm(count)) == count
        result = writer.finish()
    assert result.path == target
    assert result.sample_count == count
    assert result.duration_ms == count * 1000 / 24000
    assert result.segment_durations_ms == (count * 1000 / 24000,)
    with av.open(str(target)) as decoded:
        assert decoded.streams.audio[0].bit_rate == bitrate * 1000
        assert sum(frame.samples for frame in decoded.decode(audio=0)) == count
    # Gapless metadata is necessary for exact PCM duration after MP3 decoding.
    assert b"Info" in target.read_bytes()[:2048] or b"Xing" in target.read_bytes()[:2048]
    assert list(tmp_path.iterdir()) == [target]


def test_encode_multiple_segments_and_fractional_pause(tmp_path):
    av = pytest.importorskip("av")
    first = tmp_path / "first.pcm"
    first.write_bytes(_pcm(12001))
    target = tmp_path / "chapter.mp3"
    with pipeline.PCMChapterWriter(target) as writer:
        writer.append_pcm(first)
        assert writer.append_silence(123.5) == 2964
        writer.append_pcm(_pcm(502))
        assert writer.append_silence(0) == 0
        result = writer.finish()
    assert result.sample_count == 12001 + 2964 + 502
    assert result.segment_durations_ms == (12001 * 1000 / 24000, 502 * 1000 / 24000)
    with av.open(str(target)) as decoded:
        assert sum(frame.samples for frame in decoded.decode(audio=0)) == result.sample_count


@pytest.mark.parametrize("count", [47, 576, 577, 578, 622, 623, 1152, 24193])
def test_lame_padding_split_over_two_packets_preserves_samples(tmp_path, count):
    pytest.importorskip("av")
    with pipeline.PCMChapterWriter(tmp_path / "boundary.mp3") as writer:
        writer.append_pcm(_pcm(count))
        assert writer.finish().sample_count == count


def test_pathologically_short_pcm_is_rejected_without_changing_existing_file(tmp_path):
    pytest.importorskip("av")
    target = tmp_path / "boundary.mp3"
    target.write_bytes(b"old chapter")
    with pipeline.PCMChapterWriter(target) as writer:
        writer.append_pcm(_pcm(1))
        with pytest.raises(RuntimeError, match="duration changed"):
            writer.finish()
    assert target.read_bytes() == b"old chapter"


@pytest.mark.parametrize("bad_pcm", [b"", b"\0", b"\0\0\0"])
def test_invalid_pcm_never_replaces_target(tmp_path, bad_pcm):
    pytest.importorskip("av")
    target = tmp_path / "chapter.mp3"
    target.write_bytes(b"old chapter")
    writer = pipeline.PCMChapterWriter(target)
    with pytest.raises(ValueError, match="PCM audio"):
        writer.append_pcm(bad_pcm)
    with pytest.raises(RuntimeError, match="aborted"):
        writer.finish()
    assert target.read_bytes() == b"old chapter"
    assert list(tmp_path.iterdir()) == [target]


def test_cancel_checks_inside_large_pcm_and_preserves_original(tmp_path):
    pytest.importorskip("av")
    target = tmp_path / "chapter.mp3"
    target.write_bytes(b"old chapter")
    calls = 0
    def cancel():
        nonlocal calls
        calls += 1
        return calls == 3
    writer = pipeline.PCMChapterWriter(target, cancel_check=cancel)
    with pytest.raises(pipeline.AudioCancelled):
        writer.append_pcm(bytes(10 * pipeline._PCM_CHUNK))
    assert writer.sample_count == pipeline.SAMPLE_RATE
    assert target.read_bytes() == b"old chapter"
    assert list(tmp_path.iterdir()) == [target]


def test_failed_sample_count_validation_keeps_original(tmp_path, monkeypatch):
    pytest.importorskip("av")
    target = tmp_path / "chapter.mp3"
    target.write_bytes(b"old chapter")
    writer = pipeline.PCMChapterWriter(target)
    writer.append_pcm(_pcm(2400))
    # Simulate unexpected encoder padding/sample loss after all inputs accepted.
    writer.sample_count += 1
    with pytest.raises(RuntimeError, match="duration changed"):
        writer.finish()
    assert target.read_bytes() == b"old chapter"
    assert list(tmp_path.iterdir()) == [target]


def test_unfinished_context_cleans_only_own_temp_file(tmp_path):
    pytest.importorskip("av")
    unrelated = tmp_path / ".another-conversion.tmp.mp3"
    unrelated.write_bytes(b"unrelated")
    with pipeline.PCMChapterWriter(tmp_path / "chapter.mp3") as writer:
        writer.append_pcm(_pcm(100))
    assert list(tmp_path.iterdir()) == [unrelated]


def test_missing_pyav_is_actionable_and_does_not_create_output(tmp_path, monkeypatch):
    def unavailable(name):
        raise ImportError("No module named av")
    monkeypatch.setattr(pipeline.importlib, "import_module", unavailable)
    with pytest.raises(RuntimeError, match="python -m pip install"):
        pipeline.PCMChapterWriter(tmp_path / "chapter.mp3")
    assert list(tmp_path.iterdir()) == []


def test_unsupported_bitrate_rejected_before_output(tmp_path):
    with pytest.raises(ValueError, match="bitrate"):
        pipeline.PCMChapterWriter(tmp_path / "chapter.mp3", bitrate_kbps=32)
    assert list(tmp_path.iterdir()) == []


def test_optional_faster_encoder_setting_preserves_sample_count(tmp_path):
    pytest.importorskip("av")
    with pipeline.PCMChapterWriter(tmp_path / "fast.mp3", compression_level=7) as writer:
        assert writer._stream.codec_context.options["compression_level"] == "7"
        writer.append_pcm(_pcm(24193))
        result = writer.finish()
    assert result.sample_count == 24193


@pytest.mark.parametrize("level", [-1, 10, 1.5, True, "7"])
def test_invalid_encoder_compression_level_rejected(tmp_path, level):
    with pytest.raises(ValueError, match="compression_level"):
        pipeline.PCMChapterWriter(tmp_path / "invalid.mp3", compression_level=level)
    assert list(tmp_path.iterdir()) == []
