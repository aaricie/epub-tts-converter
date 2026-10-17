import base64
import json
import threading
import time
from types import SimpleNamespace
from urllib.error import HTTPError
from xml.etree import ElementTree as ET

import pytest

import epub_tts_converter as conv


ENGLISH = {"provider": "kokoro", "kokoro_url": "http://test"}


def test_permanent_error_is_not_retried(monkeypatch):
    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        raise HTTPError("http://test", 400, "invalid voice", {}, None)
    monkeypatch.setattr(conv, "_kokoro_once", fail)
    with pytest.raises(conv.SynthesisError, match="400"):
        conv.synthesize_chunk("Hello", "bad", backend=ENGLISH)
    assert len(calls) == 1


def test_retry_can_be_cancelled_without_another_request(monkeypatch):
    cancel = threading.Event()
    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        cancel.set()
        raise ConnectionError("temporary")
    monkeypatch.setattr(conv, "_kokoro_once", fail)
    with pytest.raises(conv.ConversionCancelled):
        conv.synthesize_chunk("Hello", "af_heart", backend=ENGLISH, cancel_event=cancel)
    assert len(calls) == 1


def test_transient_errors_retry_then_succeed(monkeypatch):
    attempts = []
    def synth(*args, **kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise HTTPError("http://test", 503, "busy", {}, None)
        return b"audio"
    monkeypatch.setattr(conv, "_kokoro_once", synth)
    monkeypatch.setattr(conv, "_interruptible_delay", lambda *a: None)
    assert conv.synthesize_chunk("Hello", "af_heart", backend=ENGLISH) == b"audio"
    assert len(attempts) == 3


def test_long_sentence_is_split_even_with_sentence_pause(monkeypatch):
    calls = []
    monkeypatch.setattr(conv, "synthesize_chunk", lambda text, *a, **k: calls.append(text) or b"\0\0" * 2400)
    text = "word " * 1200
    conv._synthesize_with_sentence_pauses(text, "af_heart", "+0%", "kokoro-pcm", False, 200, 48)
    assert len(calls) > 1
    assert all(len(text) <= conv.MAX_TTS_CHARS for text in calls)
    assert sum(text.count("word") for text in calls) == 1200


def test_sentence_failure_does_not_publish_partial_success(monkeypatch):
    def synth(text, *args, **kwargs):
        if text.startswith("Second"):
            raise conv.PassageError("failed sentence")
        return b"\0\0" * 2400
    monkeypatch.setattr(conv, "synthesize_chunk", synth)
    with pytest.raises(conv.PassageError):
        conv._synthesize_with_sentence_pauses("First sentence. Second sentence.", "af_heart",
            "+0%", "kokoro-pcm", False, 200, 48)


def test_continue_policy_records_failed_sentence_and_keeps_later_audio(monkeypatch):
    calls, logs = [], []
    def synth(text, *args, **kwargs):
        calls.append(text)
        if text.startswith("Second"):
            raise conv.PassageError("failed sentence")
        return b"\0\0" * 2400
    monkeypatch.setattr(conv, "synthesize_chunk", synth)
    cache = SimpleNamespace(failures=[])
    audio = conv._synthesize_with_sentence_pauses("First sentence. Second sentence. Third sentence.",
        "af_heart", "+0%", "kokoro-pcm", False, 200, 48, logs.append,
        backend=dict(ENGLISH, continue_on_error=True), cache=cache)
    assert len(calls) == 3
    assert cache.failures == [{"text": "Second sentence.", "error": "failed sentence"}]
    assert any("WARNING:" in line for line in logs)
    assert len(audio) >= 9600


def test_continue_policy_does_not_mask_configuration_error(monkeypatch):
    def fail(*a, **k):
        raise conv.SynthesisError("No timestamp support")
    monkeypatch.setattr(conv, "synthesize_chunk", fail)
    with pytest.raises(conv.SynthesisError):
        conv._synthesize_long("Hello", "ff_siwis", "+0%", "kokoro-pcm", True,
                              backend=dict(ENGLISH, continue_on_error=True))


def test_unicode_numbers_and_short_answers_are_synthesized(monkeypatch):
    seen = []
    monkeypatch.setattr(conv, "synthesize_chunk", lambda text, *a, **k: seen.append(text) or b"audio")
    paragraphs = [{"text": text} for text in ("1", "No.", "日本語", "हिन्दी", "Oui.")]
    assert len(conv.synthesize_paragraphs(paragraphs, "voice", "+0%", 1, None)) == 5
    assert seen == [p["text"] for p in paragraphs]


def test_scheduler_is_bounded_ordered_and_cancellation_stops_new_work(monkeypatch):
    gate = threading.Event()
    started = threading.Event()
    seen = []
    def synth(text, *args, **kwargs):
        seen.append(text)
        started.set()
        gate.wait(2)
        conv._check_control(kwargs["cancel_event"])
        return text.encode()
    monkeypatch.setattr(conv, "synthesize_chunk", synth)
    cancel = threading.Event()
    output = []
    def consume():
        try:
            output.extend(conv.iter_synthesized_paragraphs([{"text": str(i)} for i in range(100)],
                "voice", "+0%", 2, None, cancel_event=cancel))
        except conv.ConversionCancelled:
            output.append("cancelled")
    thread = threading.Thread(target=consume)
    thread.start()
    assert started.wait(1)
    cancel.set()
    thread.join(1)
    gate.set()
    assert not thread.is_alive()
    assert output == ["cancelled"]
    assert len(seen) <= 2


def test_single_worker_prefetches_next_request_while_consumer_works(monkeypatch):
    lock = threading.Lock()
    running, peak, started = [0], [0], []
    def synth(text, *args, **kwargs):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
            started.append(text)
        time.sleep(0.05)
        with lock:
            running[0] -= 1
        return text.encode()
    monkeypatch.setattr(conv, "synthesize_chunk", synth)
    results = conv.iter_synthesized_paragraphs(
        [{"text": text} for text in ("One", "Two", "Three")], "voice", "+0%", 1, None)
    assert next(results) == b"One"
    # The consumer still holds the first result; the engine already has the next one.
    deadline = time.monotonic() + 2
    while "Two" not in started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert started == ["One", "Two"]  # Bounded: one running request plus one queued.
    assert list(results) == [b"Two", b"Three"]
    assert peak[0] == 1


def test_pause_can_be_cancelled_without_synthesis(monkeypatch):
    cancel, pause = threading.Event(), threading.Event()
    cancel.set()
    with pytest.raises(conv.ConversionCancelled):
        list(conv.iter_synthesized_paragraphs([{"text": "Hello"}], "voice", "+0%", 1, None,
             cancel_event=cancel, pause_event=pause))


def test_backend_is_frozen_during_request(monkeypatch):
    calls = []
    def synth(text, voice, rate, wb, **kwargs):
        calls.append(kwargs["base_url"])
        conv.set_tts_backend("edge")
        return b"audio"
    monkeypatch.setattr(conv, "_kokoro_once", synth)
    conv.synthesize_paragraphs([{"text": "One"}, {"text": "Two"}], "af_heart", "+0%", 1,
                              None, backend=ENGLISH)
    assert calls == ["http://test", "http://test"]


def test_pcm_word_times_clamp_small_edges_and_reject_missing(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return json.dumps(payload).encode()
    payload = {"audio": base64.b64encode(b"\0\0" * 24000).decode(),
               "timestamps": [{"word": "Hello", "start_time": -.035, "end_time": .3},
                              {"word": "world", "start_time": .5, "end_time": 1.035}]}
    monkeypatch.setattr(conv, "urlopen", lambda *a, **k: Response())
    audio, boundaries = conv._kokoro_once("Hello world", "af_heart", "+0%", True, response_format="pcm")
    assert boundaries[0]["offset"] == 0
    assert boundaries[-1]["duration"] == 500
    payload["timestamps"] = []
    with pytest.raises(conv.SynthesisError, match="without word timestamps"):
        conv._kokoro_once("Bonjour", "ff_siwis", "+0%", True, response_format="pcm")


def _words(*words, start=0.0):
    return [{"type": "wordboundary", "text": word, "offset": start + index * 300.0, "duration": 250.0}
            for index, word in enumerate(words)]


def test_caption_coverage_accepts_normalization_and_unpronounceable_scripts():
    conv._check_caption_coverage("In 1945, Dr. Smith left.",
                                 _words("In", "nineteen", "forty-five", "Doctor", "Smith", "left"), 1800)
    # An English voice cannot speak a Cyrillic quotation; that is not an engine failure.
    conv._check_caption_coverage(
        "He wrote: Мы не знали, что будет дальше, и никто не мог нам сказать. Then he left.",
        _words("He", "wrote", "Then", "he", "left"), 1500)


LOST = ("The first sentence is spoken. The second sentence was lost inside the engine "
        "after a failure. The last sentence is spoken.")


def test_caption_coverage_detects_a_dropped_engine_chunk():
    # The engine closes the gap: no room is left for the eleven missing words.
    spoken = _words("The", "first", "sentence", "is", "spoken", "The", "last", "sentence", "is", "spoken")
    with pytest.raises(conv.IncompleteResponse, match="omits 11 words"):
        conv._check_caption_coverage(LOST, spoken, 3000)


def test_lost_timestamps_with_intact_audio_are_not_an_omission():
    # Kokoro 0.2.x can drop a chunk's timestamps but keep its audio: the
    # timeline still has room for the uncaptioned words (here 4 s for 11).
    spoken = (_words("The", "first", "sentence", "is", "spoken") +
              _words("The", "last", "sentence", "is", "spoken", start=5500))
    conv._check_caption_coverage(LOST, spoken, 7000)
    # The same at the end of a response: the audio continues after the last caption.
    conv._check_caption_coverage(LOST, _words("The", "first", "sentence", "is", "spoken"), 6000)


def test_engine_omission_is_retried_then_left_to_the_failure_policy(monkeypatch):
    attempts = []
    def synth(*args, **kwargs):
        attempts.append(1)
        raise conv.IncompleteResponse("Kokoro's audio omits 11 words of the passage")
    monkeypatch.setattr(conv, "_kokoro_once", synth)
    monkeypatch.setattr(conv, "_interruptible_delay", lambda *a: None)
    with pytest.raises(conv.PassageError, match="omits 11 words"):
        conv.synthesize_chunk("Hello", "af_heart", backend=ENGLISH)
    assert len(attempts) == 5


def test_stock_kokoro_is_identified_by_its_api_version(monkeypatch):
    class Response:
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return self.body
    def fake_urlopen(request, timeout=None):
        if request.full_url.endswith("/epub-tts/revision"):
            raise HTTPError(request.full_url, 404, "Not Found", {}, None)
        return Response(json.dumps({"info": {"version": "0.9.0"}}).encode())
    monkeypatch.setattr(conv, "urlopen", fake_urlopen)
    logs = []
    backend = conv._resolve_conversion_backend(dict(ENGLISH), logs.append)
    assert backend["model_revision"] == "kokoro-fastapi-0.9.0-pcm-v2"
    assert "0.9.0" in logs[0]


@pytest.mark.parametrize("listing", [
    {"voices": ["bf_emma", "af_heart"]},  # Kokoro-FastAPI 0.2.x
    {"voices": [{"id": "bf_emma", "name": "bf_emma", "overall_grade": "B-"},
                {"id": "af_heart", "name": "af_heart", "overall_grade": "A"}],
     "default_voice": "af_heart"},        # 0.8.2+
])
def test_kokoro_voice_menu_accepts_old_and_graded_listings(monkeypatch, listing):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return json.dumps(listing).encode()
    monkeypatch.setattr(conv, "urlopen", lambda *a, **k: Response())
    menu = conv.fetch_kokoro_voices("http://test")
    assert list(menu.values()) == ["af_heart", "bf_emma"]
    graded = isinstance(listing["voices"][0], dict)
    assert next(iter(menu)) == ("af_heart  (US English, Female, grade A)" if graded
                                else "af_heart  (US English, Female)")


def test_smil_has_no_gaps_and_covers_pause_and_tail():
    smil, duration = conv.build_smil([{"id": "p1"}, {"id": "p2"}], None,
        "Text/ch.xhtml", "Audio/ch.mp3", "SMIL", durations_ms=[1000, 800],
        span_data=[[{"id": "w1", "offset": 35, "duration": 100},
                    {"id": "w2", "offset": 400, "duration": 100}], []])
    audio = ET.fromstring(smil).findall(".//{http://www.w3.org/ns/SMIL}audio")
    assert duration == 1800
    assert audio[0].get("clipBegin") == conv.ms_to_clock(0)
    assert audio[-1].get("clipEnd") == conv.ms_to_clock(1800)
    assert all(left.get("clipEnd") == right.get("clipBegin") for left, right in zip(audio, audio[1:]))


@pytest.mark.parametrize("bitrate,header", [(48, 0x64), (64, 0x84), (96, 0xA4), (128, 0xC4)])
def test_silence_header_matches_bitrate(bitrate, header):
    audio = conv.generate_mp3_silence(100, bitrate)
    assert audio[2] == header
    assert len(audio) % (bitrate * 3) == 0


def test_empty_silence_has_no_extra_frame():
    assert conv.generate_mp3_silence(0, 48) == b""
