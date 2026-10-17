"""Fault injection against the reviewed server source without importing its models.

The original source snapshot is audit evidence, so these tests skip in a minimal
application distribution that does not include it. Docker builds independently
check the pinned version and every changed source file.
"""
import ast
import asyncio
import importlib.util
import math
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")
ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "audit/kokoro_runtime_src"
pytestmark = pytest.mark.skipif(not SNAPSHOT.is_dir(), reason="Reviewed Kokoro source snapshot not installed")
sys.path.insert(0, str(ROOT / "kokoro_server_patch"))
import apply_patch as patches
import performance_patch


class NullLogger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def compile_class(source, name, namespace):
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == name)
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, node], type_ignores=[]))
    exec(compile(module, "<reviewed-kokoro-test>", "exec"), namespace)
    return namespace[name]


@pytest.fixture
def server():
    ns = {"np": np, "asyncio": asyncio, "logger": NullLogger(), "os": os,
          "settings": SimpleNamespace(default_voice_code=None),
          "NormalizationOptions": lambda: None, "KokoroV1": type("KokoroV1", (), {})}
    chunk_class = compile_class((SNAPSHOT / "inference/base.py").read_text(), "AudioChunk", ns)
    service_class = compile_class(patches.patch_service((SNAPSHOT / "services/tts_service.py").read_text()), "TTSService", ns)
    service = service_class()
    service.model_manager = SimpleNamespace(get_backend=lambda: ns["KokoroV1"]())
    service._get_voices_path = lambda voice: async_result((voice, "fake.pt"))
    ns["AudioService"] = SimpleNamespace(trim_audio=lambda chunk, *args, **kwargs: chunk)
    return SimpleNamespace(ns=ns, cls=service_class, service=service, chunk=chunk_class)


async def async_result(value):
    return value


async def collect(generator):
    return [chunk async for chunk in generator]


def run(coroutine):
    """These fakes do no I/O; drive them without Windows' socketpair-based loop."""
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    finally:
        coroutine.close()
    raise AssertionError("The fake backend unexpectedly scheduled real asynchronous I/O")


def process(server, output_format=None):
    return server.service._process_chunk("Important text", [1], "af_heart", "fake.pt", 1.0,
                                         object(), output_format=output_format, return_timestamps=True)


def test_verified_source_and_unknown_version_do_not_write(tmp_path):
    changes = patches.build_changes(SNAPSHOT, performance=True)
    assert len(changes) == 5
    for path, data in changes.items():
        assert data != path.read_bytes()
        ast.parse(data)
    for name in patches.UPSTREAM_HASHES:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((SNAPSHOT / name).read_bytes())
    target = tmp_path / "inference/kokoro_v1.py"
    target.write_bytes(target.read_bytes() + b"\n# changed upstream\n")
    first_before = (tmp_path / "services/tts_service.py").read_bytes()
    with pytest.raises(ValueError, match="Refusing unknown"):
        patches.build_changes(tmp_path)
    assert (tmp_path / "services/tts_service.py").read_bytes() == first_before


def test_cache_revision_is_stable_and_changes_with_server_patch_options():
    normal = patches.build_changes(SNAPSHOT)
    faster = patches.build_changes(SNAPSHOT, performance=True)
    route = SNAPSHOT / "routers/development.py"
    assert normal[route] == patches.build_changes(SNAPSHOT)[route]
    assert normal[route] != faster[route]
    assert b'@router.get("/epub-tts/revision")' in normal[route]
    assert patches.UPSTREAM_DIGEST.encode() in normal[route]


@pytest.mark.parametrize("failure", ["before", "after", "empty", "none"])
def test_model_errors_never_finish_a_successful_process_chunk(server, failure):
    async def generate(*args, **kwargs):
        if failure == "after":
            yield server.chunk(np.ones(240, dtype=np.float32), [])
        if failure in ("empty", "none"):
            yield server.chunk(np.array([], dtype=np.int16) if failure == "empty" else None, [])
        raise RuntimeError("injected model failure")
    server.service.model_manager = SimpleNamespace(get_backend=lambda: server.ns["KokoroV1"](), generate=generate)
    with pytest.raises(RuntimeError):
        run(collect(process(server)))


def test_model_yielding_nothing_fails(server):
    async def generate(*args, **kwargs):
        if False:
            yield
    server.service.model_manager = SimpleNamespace(get_backend=lambda: server.ns["KokoroV1"](), generate=generate)
    with pytest.raises(RuntimeError, match="no chunks"):
        run(collect(process(server)))


def test_encoding_error_fails(server):
    async def generate(*args, **kwargs):
        yield server.chunk(np.ones(240, dtype=np.float32), [])
    async def fail(*args, **kwargs):
        raise RuntimeError("injected encoder failure")
    server.service.model_manager = SimpleNamespace(get_backend=lambda: server.ns["KokoroV1"](), generate=generate)
    server.ns["AudioService"].convert_audio = fail
    with pytest.raises(RuntimeError, match="encoder failure"):
        run(collect(process(server, "mp3")))


@pytest.mark.parametrize("failure", ["middle", "pause", "finalize", "empty"])
def test_generate_audio_never_returns_partial_or_empty_success(server, failure):
    async def split(*args, **kwargs):
        if failure == "empty":
            return
        yield "first", [1], None
        yield ("", [], 0.5) if failure == "pause" else ("second", [2], None)
    async def chunk(text, *args, **kwargs):
        if failure == "middle" and text == "second":
            raise RuntimeError("injected middle failure")
        if failure == "finalize" and kwargs.get("is_last"):
            raise RuntimeError("injected finalization failure")
        if kwargs.get("is_last"):
            yield server.chunk(np.array([], dtype=np.int16), [])
        else:
            yield server.chunk(np.ones(240, dtype=np.int16), [])
    class Normalizer:
        def __init__(self):
            pass
    server.ns.update(smart_split=split, AudioNormalizer=Normalizer)
    server.service._process_chunk = chunk
    if failure == "pause":
        def invalid_silence(*args, **kwargs):
            raise RuntimeError("injected silence allocation failure")
        server.ns["np"] = SimpleNamespace(zeros=invalid_silence, int16=np.int16)
    with pytest.raises(RuntimeError):
        run(server.service.generate_audio("first second", "af_heart", object(), return_timestamps=True))


@pytest.fixture
def backend():
    class Base:
        is_loaded = True
    async def no_io(*args, **kwargs):
        return None
    ns = {"np": np, "os": os, "logger": NullLogger(), "BaseModelBackend": Base,
          "settings": SimpleNamespace(default_voice_code=None),
          "model_config": SimpleNamespace(pytorch_gpu=SimpleNamespace(retry_on_oom=True)),
          "paths": SimpleNamespace(load_voice_tensor=no_io, save_voice_tensor=no_io),
          "WordTimestamp": lambda **kwargs: SimpleNamespace(**kwargs)}
    compile_class((SNAPSHOT / "inference/base.py").read_text(), "AudioChunk", ns)
    cls = compile_class(patches.patch_backend((SNAPSHOT / "inference/kokoro_v1.py").read_text()), "KokoroV1", ns)
    engine = object.__new__(cls)
    engine._device = "cpu"
    engine._model = object()
    return engine


@pytest.mark.parametrize("bad_tokens", [[], [SimpleNamespace(text="lost", start_ts=None, end_ts=None)], [SimpleNamespace(text="lost")]])
def test_english_timestamp_loss_is_an_error(backend, bad_tokens):
    good = SimpleNamespace(text="first", start_ts=0.0, end_ts=0.1)
    results = [SimpleNamespace(audio=SimpleNamespace(shape=(240,), numpy=lambda: np.ones(240)), tokens=[good], pred_dur=True),
               SimpleNamespace(audio=SimpleNamespace(shape=(240,), numpy=lambda: np.ones(240)), tokens=bad_tokens, pred_dur=True)]
    backend._get_pipeline = lambda code: lambda *args, **kwargs: iter(results)
    with pytest.raises((RuntimeError, TypeError)):
        run(collect(backend.generate("first lost", ("af_heart", "fake.pt"), return_timestamps=True)))


def test_oom_cannot_recurse_or_duplicate_previous_chunks(backend):
    calls = []
    backend._device = "cuda"
    backend._check_memory = lambda: False
    backend._clear_memory = lambda: calls.append("clear")
    def pipeline(*args, **kwargs):
        calls.append("generate")
        yield SimpleNamespace(audio=SimpleNamespace(shape=(240,), numpy=lambda: np.ones(240)), tokens=[], pred_dur=True)
        raise RuntimeError("CUDA out of memory")
    backend._get_pipeline = lambda code: pipeline
    with pytest.raises(RuntimeError, match="out of memory"):
        run(collect(backend.generate("first lost", ("af_heart", "fake.pt"))))
    assert calls == ["generate", "clear"]


def normalizers():
    settings = SimpleNamespace(gap_trim_ms=30, dynamic_gap_trim_padding_ms=400,
                               dynamic_gap_trim_padding_char_multiplier={".": 1.5, ",": 0.7})
    ns = {"np": np, "math": math, "settings": settings}
    source = (SNAPSHOT / "services/audio.py").read_text()
    original = compile_class(source, "AudioNormalizer", dict(ns))()
    patched = compile_class(performance_patch.patch_audio(source), "AudioNormalizer", dict(ns))()
    return original, patched


def test_chunked_scan_preserves_padding_for_quiet_noisy_and_boundary_inputs():
    original, patched = normalizers()
    rng = np.random.default_rng(20261015)
    arrays = [np.array([], dtype=np.int16), np.zeros(96000, dtype=np.int16),
              np.full(24000, 600, dtype=np.int16), rng.integers(-10000, 10000, 96000, dtype=np.int16)]
    for length in (1, 4095, 4096, 4097, 15000):
        for position in (0, length // 2, length - 1):
            samples = np.zeros(length, dtype=np.int16)
            samples[position] = 600
            arrays.append(samples)
    for samples in arrays:
        for text, speed, last in [("word.", 1.0, False), ("word,", 0.75, False), ("word", 1.5, True)]:
            assert patched.find_first_last_non_silent(samples, text, speed, is_last_chunk=last) == original.find_first_last_non_silent(samples, text, speed, is_last_chunk=last)


def test_int16_minimum_is_audio_not_silence():
    _, patched = normalizers()
    samples = np.zeros(30000, dtype=np.int16)
    samples[16000] = -32768
    assert patched.find_first_last_non_silent(samples, "word", 1.0) == (14800, 24400)


def test_combine_preserves_pcm_and_timestamps_without_mutating_inputs():
    source = performance_patch.patch_combine((SNAPSHOT / "inference/base.py").read_text())
    cls = compile_class(source, "AudioChunk", {"np": np})
    first_stamps = ["first"]
    chunks = [cls(np.arange(240, dtype=np.int16), first_stamps), cls(np.arange(240, 480, dtype=np.int16), ["second"])]
    combined = cls.combine(chunks)
    np.testing.assert_array_equal(combined.audio, np.arange(480, dtype=np.int16))
    assert combined.word_timestamps == ["first", "second"]
    assert first_stamps == ["first"]
    with pytest.raises(ValueError, match="empty"):
        cls.combine([])
