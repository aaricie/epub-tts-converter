import threading
from types import SimpleNamespace

import pytest

import epub_tts_converter as conv
from audio_pipeline import RequestCache


BACKEND = {'provider': 'kokoro', 'kokoro_url': 'http://test.invalid',
           'batch_paragraphs': True, 'model_revision': 'test'}
PARAGRAPHS = [{'text': 'Hello world.'}, {'text': 'Second paragraph.'}]
COMBINED = 'Hello world.\n\nSecond paragraph.'
# One second of non-silent PCM with a silent stretch inside the paragraph pause.
PCM = bytearray((bytes(range(256)) * 188)[:48000])
PCM[400 * 48:500 * 48] = bytes(100 * 48)
PCM = bytes(PCM)
BOUNDS = [
    {'type': 'wordboundary', 'text': 'Hello', 'offset': 50, 'duration': 100},
    {'type': 'wordboundary', 'text': 'world.', 'offset': 180, 'duration': 120},
    {'type': 'wordboundary', 'text': 'Second', 'offset': 600, 'duration': 150},
    {'type': 'wordboundary', 'text': 'paragraph.', 'offset': 780, 'duration': 170},
]


def _iterate(paragraphs=PARAGRAPHS, *, workers=1, need_boundaries=True,
             backend=BACKEND, **kwargs):
    return conv.iter_synthesized_paragraphs(paragraphs, 'af_heart', '+0%', workers, None,
        audio_format='kokoro-pcm', need_boundaries=need_boundaries, backend=backend, **kwargs)


def _cache(tmp_path):
    source = tmp_path / 'source.txt'
    source.write_text(COMBINED)
    cache = RequestCache(tmp_path / 'cache', source, BACKEND)
    cache.used_requests, cache.failures = [], []
    return cache


def test_accepted_batch_uses_one_request_and_keeps_all_pcm(monkeypatch):
    calls = []
    def engine(text, voice, rate, word_boundary, **kwargs):
        calls.append((text, word_boundary, kwargs['response_format']))
        return PCM, BOUNDS
    monkeypatch.setattr(conv, '_kokoro_once', engine)
    results = list(_iterate())
    assert calls == [(COMBINED, True, 'pcm')]
    assert b''.join(audio for audio, _ in results) == PCM
    assert [len(audio) for audio, _ in results] == [21600, 26400]
    assert results[1][1][0]['offset'] == 150


def test_batching_logs_one_summary_per_chapter(monkeypatch):
    def engine(text, voice, rate, word_boundary, **kwargs):
        if '\n\n' in text and '1945' in text:
            raise conv.PassageError('injected failure')  # The second batch falls back.
        if '\n\n' in text:
            return PCM, BOUNDS
        return b'\x01\x02' * 2400, [dict(BOUNDS[0], text=text.split()[0], offset=0, duration=90)]
    monkeypatch.setattr(conv, '_kokoro_once', engine)
    monkeypatch.setattr(conv, '_interruptible_delay', lambda *a: None)
    logs = []
    # A rewritten opening ("1945") starts the second batch.
    paragraphs = PARAGRAPHS + [{'text': '1945 came.'}, {'text': 'Second try.'}, {'text': 'Hello again.'}]
    list(conv.iter_synthesized_paragraphs(paragraphs, 'af_heart', '+0%', 1, logs.append,
        audio_format='kokoro-pcm', need_boundaries=True,
        backend=dict(BACKEND, continue_on_error=True)))
    assert [line for line in logs if 'Batched' in line] == [
        '    Batched 2 short paragraphs into 1 requests; 1 batch(es) fell back to single requests']


def test_mp3_only_batch_still_splits_using_captions_but_yields_only_audio(monkeypatch):
    def engine(text, voice, rate, word_boundary, **kwargs):
        assert word_boundary
        return PCM, BOUNDS
    monkeypatch.setattr(conv, '_kokoro_once', engine)
    pieces = list(_iterate(need_boundaries=False))
    assert all(isinstance(piece, bytes) for piece in pieces)
    assert b''.join(pieces) == PCM


def test_boundary_caption_mismatch_falls_back_before_yielding_any_batched_audio(monkeypatch):
    calls = []
    def engine(text, voice, rate, word_boundary, **kwargs):
        calls.append(text)
        if '\n\n' in text:
            # The next paragraph's opening word is not captioned verbatim.
            return PCM, [BOUNDS[0], BOUNDS[1], dict(BOUNDS[2], text='Sekond'), BOUNDS[3]]
        # Distinct PCM makes accidental use of the discarded batch detectable.
        return (b'\x11\x22' * 2400, [dict(BOUNDS[0], text=text, offset=0, duration=90)])
    monkeypatch.setattr(conv, '_kokoro_once', engine)
    results = list(_iterate())
    assert calls == [COMBINED, 'Hello world.', 'Second paragraph.']
    assert [audio for audio, _ in results] == [b'\x11\x22' * 2400] * 2


def test_failed_batch_retries_individually_and_continue_policy_only_records_failed_passage(monkeypatch):
    calls = []
    cache = SimpleNamespace(failures=[])
    # Mock at request boundary here so the failure tracker can be minimal.
    def request(text, *args, **kwargs):
        calls.append(text)
        if '\n\n' in text or text.startswith('Hello'):
            raise conv.PassageError('injected failure')
        return b'\x01\x02' * 2400, [dict(BOUNDS[2], offset=0, duration=90)]
    monkeypatch.setattr(conv, 'synthesize_chunk', request)
    results = list(_iterate(backend=dict(BACKEND, continue_on_error=True), cache=cache))
    assert calls == [COMBINED, 'Hello world.', 'Second paragraph.']
    assert results[0] == (b'', [])
    assert results[1][0] == b'\x01\x02' * 2400
    assert cache.failures == [{'text': 'Hello world.', 'error': 'injected failure'}]


def test_batch_configuration_failure_is_not_hidden_by_continue_policy(monkeypatch):
    calls = []
    def engine(text, *args, **kwargs):
        calls.append(text)
        raise conv.SynthesisError('Unsupported caption contract')
    monkeypatch.setattr(conv, '_kokoro_once', engine)
    with pytest.raises(conv.SynthesisError, match='Unsupported caption contract'):
        list(_iterate(backend=dict(BACKEND, continue_on_error=True)))
    # The batch falls back once; the single request reports the real error.
    assert calls == [COMBINED, 'Hello world.']


@pytest.mark.parametrize('voice,sentence_pause', [('ff_siwis', 0), ('jf_alpha', 0), ('af_heart', 100)])
def test_non_english_voices_and_explicit_sentence_pauses_use_individual_requests(monkeypatch, voice, sentence_pause):
    calls = []
    def engine(text, voice, rate, word_boundary, **kwargs):
        calls.append((text, word_boundary))
        assert not word_boundary
        return b'\x01\x02' * 2400
    monkeypatch.setattr(conv, '_kokoro_once', engine)
    results = list(conv.iter_synthesized_paragraphs(PARAGRAPHS, voice, '+0%', 1, None,
        audio_format='kokoro-pcm', need_boundaries=False, sentence_pause_ms=sentence_pause,
        backend=BACKEND))
    assert calls == [('Hello world.', False), ('Second paragraph.', False)]
    assert len(results) == 2


def test_british_english_voices_batch_like_american_ones(monkeypatch):
    calls = []
    def engine(text, voice, rate, word_boundary, **kwargs):
        calls.append(text)
        return PCM, BOUNDS
    monkeypatch.setattr(conv, '_kokoro_once', engine)
    results = list(conv.iter_synthesized_paragraphs(PARAGRAPHS, 'bf_emma', '+0%', 1, None,
        audio_format='kokoro-pcm', need_boundaries=True, backend=BACKEND))
    assert calls == [COMBINED]
    assert b''.join(audio for audio, _ in results) == PCM


def test_parallel_identical_batches_share_one_inflight_request_and_cached_response(tmp_path, monkeypatch):
    cache = _cache(tmp_path)
    started, release = threading.Event(), threading.Event()
    calls, output, errors = [], [], []
    four = PARAGRAPHS * 2
    eight = four * 2
    combined = '\n\n'.join(p['text'] for p in four)
    def engine(text, voice, rate, word_boundary, **kwargs):
        calls.append(text)
        started.set()
        assert release.wait(3)
        bounds = BOUNDS + [dict(bound, offset=bound['offset'] + 1000) for bound in BOUNDS]
        return PCM * 2, bounds
    monkeypatch.setattr(conv, '_kokoro_once', engine)
    def consume():
        try:
            output.extend(_iterate(eight, workers=2, cache=cache))
        except BaseException as error:
            errors.append(error)
    worker = threading.Thread(target=consume)
    worker.start()
    try:
        assert started.wait(2)
        release.set()
        worker.join(4)
        assert not worker.is_alive()
        assert not errors
        assert calls == [combined]
        assert len(output) == 8
        assert b''.join(audio for audio, _ in output) == PCM * 4
    finally:
        release.set()
        worker.join(4)


def test_cancel_during_batch_does_not_launch_fallback_or_later_batches(monkeypatch):
    cancel, started, release, engine_finished = (threading.Event() for _ in range(4))
    calls, errors, output = [], [], []
    def engine(text, voice, rate, word_boundary, **kwargs):
        calls.append(text)
        started.set()
        assert release.wait(3)
        engine_finished.set()
        return PCM, BOUNDS
    monkeypatch.setattr(conv, '_kokoro_once', engine)
    def consume():
        try:
            output.extend(_iterate(PARAGRAPHS * 6, workers=1, cancel_event=cancel))
        except BaseException as error:
            errors.append(error)
    worker = threading.Thread(target=consume)
    worker.start()
    try:
        assert started.wait(2)
        cancel.set()
        worker.join(2)
        assert not worker.is_alive()
        assert len(calls) == 1
        assert output == []
        assert len(errors) == 1 and isinstance(errors[0], conv.ConversionCancelled)
    finally:
        release.set()
        assert engine_finished.wait(2)
        worker.join(3)


def test_staged_batch_preserves_pcm_duration_and_adds_configured_pause_exactly(tmp_path, monkeypatch):
    av = pytest.importorskip('av')
    cache = _cache(tmp_path)
    monkeypatch.setattr(conv, '_kokoro_once', lambda *args, **kwargs: (PCM, BOUNDS))
    received_bounds = []
    stage = tmp_path / 'chapter.mp3'
    durations, total = conv._stage_paragraph_audio(PARAGRAPHS, stage, 'af_heart', '+0%', 1,
        None, None, None, 'kokoro-pcm', 48.0, 48, 0, [125, 0], BACKEND, cache,
        need_boundaries=True, on_boundaries=lambda index, bounds: received_bounds.append(bounds))
    assert durations == [575, 550]
    assert total == 1125
    assert received_bounds[1][0]['offset'] == 150
    with av.open(str(stage)) as decoder:
        assert sum(frame.samples for frame in decoder.decode(audio=0)) == 27000
