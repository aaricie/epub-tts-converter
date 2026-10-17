from io import BytesIO
import json
import threading
import zipfile

import pytest

import epub_tts_converter as conv
from test_epub_tts_converter import _write_epub


BACKEND = {'provider': 'kokoro', 'kokoro_url': 'http://local.invalid',
           'model_revision': 'test-model-v1'}


def _book(tmp_path):
    source = tmp_path / 'book.epub'
    _write_epub(source, body='<html xmlns="http://www.w3.org/1999/xhtml">'
                '<head><title>Chapter</title></head><body>'
                '<p>First paragraph.</p><p>Second paragraph.</p></body></html>')
    return source


def _fake_kokoro(calls, *, cancel=None, fail=None):
    def synthesize(text, voice, rate, word_boundary, **kwargs):
        assert kwargs['response_format'] == 'pcm'
        calls.append(text)
        if fail is not None and fail(text):
            raise conv.PassageError('Injected passage failure')
        audio = b'\x10\x00' * 6000  # 250 ms, including an awkward MP3 frame remainder.
        words = text.split()
        bounds = [{'type': 'wordboundary', 'offset': index * 50,
                   'duration': 40, 'text': word} for index, word in enumerate(words)]
        if cancel is not None:
            cancel.set()
        return (audio, bounds) if word_boundary else audio
    return synthesize


def _convert(source, destination, **kwargs):
    return conv.convert_epub_to_epub3(str(source), str(destination), 'af_heart', '+0%',
                                     1, lambda message: None, backend=BACKEND, **kwargs)


def test_pcm_epub_roundtrip_resume_and_parameter_invalidation(tmp_path, monkeypatch):
    av = pytest.importorskip('av')
    calls = []
    monkeypatch.setattr(conv, '_kokoro_once', _fake_kokoro(calls))
    source = _book(tmp_path)
    output = tmp_path / 'audio.epub'
    _convert(source, output, para_pause_ms=125)
    assert len(calls) == 2
    with zipfile.ZipFile(output) as book:
        audio = book.read('EPUB/Audio/ch001.mp3')
        assert book.getinfo('EPUB/Audio/ch001.mp3').compress_type == zipfile.ZIP_STORED
        assert 'clipEnd="0:00:00.625"' in book.read('EPUB/SMIL/ch001.smil').decode()
    with av.open(BytesIO(audio)) as decoded:
        assert sum(frame.samples for frame in decoded.decode(audio=0)) == 15000
    # Only the chapter MP3 is retained; request PCM was pruned after its commit.
    records = list((tmp_path / '.epub_tts_cache').rglob('*.json'))
    assert len(records) == 1
    assert 'chapter_item' in json.loads(records[0].read_text())['metadata']
    _convert(source, tmp_path / 'audio_2.epub', para_pause_ms=125)
    assert len(calls) == 2
    _convert(source, tmp_path / 'audio_3.epub', para_pause_ms=250)
    assert len(calls) == 4


def test_cancel_keeps_existing_output_and_reuses_completed_request(tmp_path, monkeypatch):
    pytest.importorskip('av')
    source = _book(tmp_path)
    destination = tmp_path / 'audio.epub'
    destination.write_bytes(b'existing output')
    calls, cancelled = [], threading.Event()
    monkeypatch.setattr(conv, '_kokoro_once', _fake_kokoro(calls, cancel=cancelled))
    with pytest.raises(conv.ConversionCancelled):
        _convert(source, destination, cancel_event=cancelled)
    assert destination.read_bytes() == b'existing output'
    assert len(calls) == 1
    cancelled.clear()
    monkeypatch.setattr(conv, '_kokoro_once', _fake_kokoro(calls))
    _convert(source, destination, cancel_event=cancelled)
    assert len(calls) == 2  # First paragraph came from the durable request cache.
    assert zipfile.is_zipfile(destination)
    assert not list((tmp_path / '.epub_tts_cache').rglob('build-*'))


def test_partial_chapter_is_not_checkpointed_and_retries_only_missing_passage(tmp_path, monkeypatch):
    pytest.importorskip('av')
    calls, logs = [], []
    source, destination = _book(tmp_path), tmp_path / 'partial.epub'
    monkeypatch.setattr(conv, '_kokoro_once', _fake_kokoro(calls, fail=lambda text: 'Second' in text))
    conv.convert_epub_to_epub3(str(source), str(destination), 'af_heart', '+0%', 1,
        logs.append, backend=dict(BACKEND, continue_on_error=True))
    assert any('WITH WARNINGS' in message for message in logs)
    report = tmp_path / 'partial.epub.warnings.txt'
    assert 'Second paragraph.' in report.read_text()
    records = list((tmp_path / '.epub_tts_cache').rglob('*.json'))
    assert len(records) == 1
    assert 'boundaries' in json.loads(records[0].read_text())['metadata']
    monkeypatch.setattr(conv, '_kokoro_once', _fake_kokoro(calls))
    _convert(source, destination)
    assert calls.count('First paragraph.') == 1
    assert calls.count('Second paragraph.') == 2
    assert not report.exists()
    with zipfile.ZipFile(destination) as book:
        assert 'clipEnd="0:00:00.500"' in book.read('EPUB/SMIL/ch001.smil').decode()


def test_text_pcm_pause_and_cache_resume(tmp_path, monkeypatch):
    av = pytest.importorskip('av')
    calls = []
    source = tmp_path / 'book.txt'
    source.write_text('First paragraph.\n\nSecond paragraph.')
    output = tmp_path / 'book.mp3'
    monkeypatch.setattr(conv, '_kokoro_once', _fake_kokoro(calls))
    conv.convert_text_to_mp3(str(source), str(output), 'af_heart', '+0%', 1,
        lambda _: None, backend=BACKEND, para_pause_ms=125, section_pause_ms=125)
    with av.open(str(output)) as decoded:
        samples = sum(frame.samples for frame in decoded.decode(audio=0))
    assert samples == 15000
    conv.convert_text_to_mp3(str(source), str(tmp_path / 'book_2.mp3'), 'af_heart', '+0%', 1,
        lambda _: None, backend=BACKEND, para_pause_ms=125, section_pause_ms=125)
    assert len(calls) == 2


def test_mp3_chapter_export_uses_same_durable_cache_after_folder_rename(tmp_path, monkeypatch):
    pytest.importorskip('av')
    calls = []
    source = _book(tmp_path)
    monkeypatch.setattr(conv, '_kokoro_once', _fake_kokoro(calls))
    for folder in ['audio', 'audio_2']:
        conv.convert_epub_to_mp3(str(source), str(tmp_path / folder), 'af_heart', '+0%',
                                1, lambda _: None, backend=BACKEND)
        assert (tmp_path / folder / 'ch001.mp3').is_file()
    assert len(calls) == 2


def test_missing_output_directory_fails_before_tts(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(conv, '_kokoro_once', _fake_kokoro(calls))
    with pytest.raises(FileNotFoundError):
        _convert(_book(tmp_path), tmp_path / 'missing' / 'book.epub')
    assert calls == []


@pytest.mark.parametrize('kind', ['text', 'chapters'])
def test_partial_mp3_exports_warn_and_reuse_successful_requests(tmp_path, monkeypatch, kind):
    pytest.importorskip('av')
    calls, logs = [], []
    backend = dict(BACKEND, continue_on_error=True)
    monkeypatch.setattr(conv, '_kokoro_once', _fake_kokoro(calls, fail=lambda text: 'Second' in text))
    if kind == 'text':
        source = tmp_path / 'book.txt'
        source.write_text('First paragraph.\n\nSecond paragraph.')
        output = tmp_path / 'partial.mp3'
        report = tmp_path / 'partial.mp3.warnings.txt'
        def run(config):
            return conv.convert_text_to_mp3(str(source), str(output), 'af_heart', '+0%',
                1, logs.append, backend=config, para_pause_ms=125, section_pause_ms=125)
    else:
        source = _book(tmp_path)
        output = tmp_path / 'chapters'
        report = output / 'warnings.txt'
        def run(config):
            return conv.convert_epub_to_mp3(str(source), str(output), 'af_heart', '+0%',
                                           1, logs.append, backend=config)
    run(backend)
    assert 'Second paragraph.' in report.read_text()
    assert any('WITH WARNINGS' in message for message in logs)
    monkeypatch.setattr(conv, '_kokoro_once', _fake_kokoro(calls))
    run(BACKEND)
    assert calls.count('First paragraph.') == 1
    assert calls.count('Second paragraph.') == 2
    assert not report.exists()


def test_success_cleanup_reuses_then_removes_only_this_runs_checkpoints(tmp_path, monkeypatch):
    pytest.importorskip('av')
    calls = []
    source = _book(tmp_path)
    monkeypatch.setattr(conv, '_kokoro_once', _fake_kokoro(calls))
    _convert(source, tmp_path / 'first.epub')
    cache_root = tmp_path / '.epub_tts_cache'
    assert len(list(cache_root.rglob('*.json'))) == 1
    unrelated = cache_root / 'unrelated.keep'
    unrelated.write_bytes(b'not this conversion')
    conv.convert_epub_to_epub3(str(source), str(tmp_path / 'second.epub'), 'af_heart',
        '+0%', 1, lambda _: None, backend=dict(BACKEND, keep_checkpoints=False))
    assert len(calls) == 2
    assert not list(cache_root.rglob('*.json'))
    assert not list(cache_root.rglob('*.bin'))
    assert unrelated.read_bytes() == b'not this conversion'
