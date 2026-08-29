import base64
import json
import zipfile
from xml.etree import ElementTree as ET

import pytest

import epub_tts_converter as conv


@pytest.fixture(autouse=True)
def _reset_backend():
    """Ensure the global TTS provider never leaks between tests."""
    yield
    conv.set_tts_backend("edge")


class _FakeResp:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_urlopen(captured, body):
    def _open(req, timeout=None):
        captured["url"] = req.full_url
        captured["data"] = req.data
        return _FakeResp(body)
    return _open


OPF_NS = {"opf": conv.OPF_NS}
XHTML_NS = {"x": "http://www.w3.org/1999/xhtml"}


def _write_epub(path, *, href="Text/ch1.xhtml", body=None,
                extra_manifest="", extra_files=None, nav=False, ncx=False):
    body = body or (
        '<html xmlns="http://www.w3.org/1999/xhtml">'
        '<head><title>Ch</title></head>'
        '<body><p>Hello <em>world</em>.</p></body></html>'
    )
    extra_files = extra_files or {}
    nav_item = (
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
        'properties="nav"/>'
        if nav else ""
    )
    ncx_item = (
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
        if ncx else ""
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip",
                    compress_type=zipfile.ZIP_STORED)
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?>'
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
            'version="1.0"><rootfiles><rootfile full-path="EPUB/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        zf.writestr(
            "EPUB/content.opf",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" '
            'unique-identifier="id">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:identifier id="id">x</dc:identifier>'
            '<dc:title>T</dc:title><dc:language>en</dc:language></metadata>'
            '<manifest>'
            f'<item id="ch1" href="{href}" media-type="application/xhtml+xml"/>'
            f'{extra_manifest}{nav_item}{ncx_item}'
            '</manifest><spine toc="ncx"><itemref idref="ch1"/></spine>'
            '</package>',
        )
        zf.writestr(f"EPUB/{conv.epub_path('', href)}", body)
        if nav:
            zf.writestr(
                "EPUB/nav.xhtml",
                '<html xmlns="http://www.w3.org/1999/xhtml" '
                'xmlns:epub="http://www.idpf.org/2007/ops"><body>'
                '<nav epub:type="toc"><ol><li><a href="Text/ch1.xhtml">'
                'Existing</a></li></ol></nav></body></html>',
            )
        if ncx:
            zf.writestr(
                "EPUB/toc.ncx",
                '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/">'
                '<navMap><navPoint id="n1"><navLabel><text>NCX Title</text>'
                '</navLabel><content src="Text/ch1.xhtml"/></navPoint>'
                '</navMap></ncx>',
            )
        for name, data in extra_files.items():
            zf.writestr(name, data)


def _fake_synth(paragraphs, *args, **kwargs):
    out = []
    for para in paragraphs:
        words = [w.strip(".,!?") for w in para.get("tts_text", para["text"]).split()]
        offset = 0
        bounds = []
        for word in words:
            if not word:
                continue
            bounds.append({
                "type": "wordboundary",
                "offset": offset,
                "duration": 100,
                "text": word,
            })
            offset += 120
        out.append((b"X" * 600, bounds))
    return out


def _convert(monkeypatch, tmp_path, *, href="Text/ch1.xhtml", body=None,
             extra_manifest="", extra_files=None, nav=False, ncx=False):
    monkeypatch.setattr(conv, "synthesize_paragraphs", _fake_synth)
    inp = tmp_path / "in.epub"
    out = tmp_path / "out.epub"
    _write_epub(inp, href=href, body=body, extra_manifest=extra_manifest,
                extra_files=extra_files, nav=nav, ncx=ncx)
    logs = []
    result = conv.convert_epub_to_epub3(
        str(inp), str(out), "en-US-AvaMultilingualNeural", "+0%", 1, logs.append
    )
    assert result == str(out)
    return out


def _opf_from(epub_path):
    with zipfile.ZipFile(epub_path) as zf:
        return ET.fromstring(zf.read("EPUB/content.opf"))


def test_word_level_output_and_mimetype(monkeypatch, tmp_path):
    out = _convert(monkeypatch, tmp_path, nav=True)
    with zipfile.ZipFile(out) as zf:
        assert zf.namelist()[0] == "mimetype"
        assert zf.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        html = zf.read("EPUB/Text/ch1.xhtml").decode("utf-8")
        smil = zf.read("EPUB/SMIL/ch001.smil").decode("utf-8")

    assert 'id="mo_ch001p0001w0001"' in html
    assert 'Text/ch1.xhtml#mo_ch001p0001w0001' in smil
    assert 'media-overlay="overlay-ch001"' in ET.tostring(
        _opf_from(out), encoding="unicode"
    )


def test_unmapped_word_metadata_keeps_dense_sync_marker(monkeypatch, tmp_path):
    def bad_synth(paragraphs, *args, **kwargs):
        return [(b"X" * 600, [
            {
                "type": "wordboundary",
                "offset": 0,
                "duration": 100,
                "text": "Hello",
            },
            {
                "type": "wordboundary",
                "offset": 120,
                "duration": 100,
                "text": "missingword",
            },
            {
                "type": "wordboundary",
                "offset": 240,
                "duration": 100,
                "text": "world",
            },
        ])]

    monkeypatch.setattr(conv, "synthesize_paragraphs", bad_synth)
    inp = tmp_path / "in.epub"
    out = tmp_path / "out.epub"
    _write_epub(inp)
    conv.convert_epub_to_epub3(
        str(inp), str(out), "en-US-AvaMultilingualNeural", "+0%", 1, lambda _: None
    )

    with zipfile.ZipFile(out) as zf:
        html = zf.read("EPUB/Text/ch1.xhtml").decode("utf-8")
        smil = zf.read("EPUB/SMIL/ch001.smil").decode("utf-8")

    assert "<em>" in html
    assert "world" in html
    assert "missingword" not in html
    assert "Text/ch1.xhtml#mo_ch001p0001w0001" in smil
    assert "Text/ch1.xhtml#mo_ch001p0001w0002" in smil
    assert "Text/ch1.xhtml#mo_ch001p0001w0003" in smil


def test_split_inline_word_gets_sync_marker_without_flattening():
    soup = conv.BeautifulSoup(
        '<html><body><p id="p1">Hello <span>wor</span><span>ld</span>.</p></body></html>',
        "lxml",
    )
    tag = soup.find("p")
    display_text, display_map = conv._display_text_and_map(tag)
    tts_text, tts_map = conv._sanitize_for_ssml_with_map(display_text)
    para = {
        "id": "p1",
        "text": display_text,
        "display_text": display_text,
        "display_map": display_map,
        "tts_text": tts_text,
        "tts_map": tts_map,
        "tag": tag,
    }
    spans = conv.inject_word_spans(
        soup,
        para,
        [
            {"type": "wordboundary", "offset": 0, "duration": 100, "text": "Hello"},
            {"type": "wordboundary", "offset": 120, "duration": 100, "text": "world"},
        ],
        1,
        1,
    )

    assert spans == [
        {"id": "mo_ch001p0001w0001", "offset": 0, "duration": 100},
        {"id": "mo_ch001p0001w0002", "offset": 120, "duration": 100},
    ]
    assert '<span id="mo_ch001p0001w0001">Hello</span>' in str(tag)
    assert 'id="mo_ch001p0001w0002"' in str(tag)
    assert "wor" in str(tag)
    assert "ld" in str(tag)
    assert tag.get_text() == "Hello world."


def test_inline_small_caps_are_joined_for_tts_text():
    soup, paragraphs = conv.process_chapter(
        b'<html><body><p>E<span class="small">NGLAND</span> and '
        b'G<span class="small">ERMANY</span>.</p></body></html>',
        1,
    )

    assert paragraphs[0]["display_text"] == "ENGLAND and GERMANY."
    assert paragraphs[0]["tts_text"] == "ENGLAND and GERMANY."


def test_inline_formatting_keeps_source_spaces_for_tts_text():
    soup, paragraphs = conv.process_chapter(
        b'<html><body><p>Hello <em>world</em>.<br/>Next line.</p></body></html>',
        1,
    )

    assert paragraphs[0]["display_text"] == "Hello world. Next line."
    assert paragraphs[0]["tts_text"] == "Hello world. Next line."


def test_empty_index_anchor_preserves_real_source_space():
    soup, paragraphs = conv.process_chapter(
        b'<html><body><p>Alpha <a class="hlink" id="i1"/>Beta and '
        b'Gamma<a class="hlink" id="i2"/> Delta.</p></body></html>',
        1,
    )

    assert paragraphs[0]["display_text"] == "Alpha Beta and Gamma Delta."


def test_sanitized_punctuation_maps_to_visible_text(monkeypatch, tmp_path):
    body = (
        '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        '<p>“Hello”—she said…</p>'
        '</body></html>'
    )
    out = _convert(monkeypatch, tmp_path, body=body, nav=True)
    with zipfile.ZipFile(out) as zf:
        html = zf.read("EPUB/Text/ch1.xhtml").decode("utf-8")

    assert "“" in html
    assert "”" in html
    assert "—" in html
    assert "…" in html
    assert '"Hello"' not in html


def test_citation_text_stays_visible_but_skipped_for_tts(monkeypatch, tmp_path):
    body = (
        '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        '<p>Claim<a href="#note1">123</a> stays.</p>'
        '</body></html>'
    )
    out = _convert(monkeypatch, tmp_path, body=body, nav=True)
    with zipfile.ZipFile(out) as zf:
        html = zf.read("EPUB/Text/ch1.xhtml").decode("utf-8")
        smil = zf.read("EPUB/SMIL/ch001.smil").decode("utf-8")

    assert '<a href="#note1">123</a>' in html
    assert "123" not in smil
    assert "Text/ch1.xhtml#mo_ch001p0001w0001" in smil
    assert "Text/ch1.xhtml#mo_ch001p0001w0002" in smil


def test_empty_inline_anchor_survives_word_wrapping(monkeypatch, tmp_path):
    body = (
        '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        '<p>Hello <a class="hlink" id="idx"></a>world.</p>'
        '</body></html>'
    )
    out = _convert(monkeypatch, tmp_path, body=body, nav=True)
    with zipfile.ZipFile(out) as zf:
        html = zf.read("EPUB/Text/ch1.xhtml").decode("utf-8")

    assert 'id="idx"' in html
    assert "mo_ch001p0001w0001" in html
    assert "mo_ch001p0001w0002" in html


def test_smil_id_collision_uses_final_id(monkeypatch, tmp_path):
    out = _convert(
        monkeypatch,
        tmp_path,
        extra_manifest=(
            '<item id="overlay-ch001" href="SMIL/existing.smil" '
            'media-type="application/smil+xml"/>'
        ),
        extra_files={"EPUB/SMIL/existing.smil": "<smil/>"},
    )
    opf = _opf_from(out)
    ch = opf.find(".//opf:manifest/opf:item[@id='ch1']", OPF_NS)
    assert ch.get("media-overlay") == "overlay-ch001-1"
    duration = opf.find(
        ".//opf:metadata/opf:meta[@property='media:duration']"
        "[@refines='#overlay-ch001-1']",
        OPF_NS,
    )
    assert duration is not None


def test_percent_encoded_href_resolves(monkeypatch, tmp_path):
    out = _convert(monkeypatch, tmp_path, href="Text/chapter%201.xhtml", nav=True)
    with zipfile.ZipFile(out) as zf:
        assert "EPUB/Text/chapter 1.xhtml" in zf.namelist()
        assert "EPUB/SMIL/ch001.smil" in zf.namelist()


def test_missing_nav_is_generated_from_ncx(monkeypatch, tmp_path):
    out = _convert(monkeypatch, tmp_path, ncx=True)
    with zipfile.ZipFile(out) as zf:
        nav = zf.read("EPUB/nav.xhtml").decode("utf-8")
    assert "NCX Title" in nav
    assert 'href="Text/ch1.xhtml"' in nav


def test_resolve_spine_chapters_dedupes_repeated_itemref():
    # Same itemref repeated (e.g. PDF/MOBI conversions) must be synthesized once.
    manifest = {
        "c1": {"href": "ch1.xhtml", "media-type": "application/xhtml+xml"},
        "c2": {"href": "ch2.xhtml", "media-type": "application/xhtml+xml"},
    }
    spine = ["c1", "c1", "c1", "c2", "c1", "c2"]
    chapters, skipped = conv.resolve_spine_chapters(manifest, spine, "")
    assert chapters == [("c1", "ch1.xhtml"), ("c2", "ch2.xhtml")]
    assert skipped == 4


def test_resolve_spine_chapters_dedupes_distinct_ids_same_file():
    # Distinct ids resolving to the same file (ignoring fragments) collapse too.
    manifest = {
        "a": {"href": "ch1.xhtml", "media-type": "application/xhtml+xml"},
        "b": {"href": "ch1.xhtml#sec2", "media-type": "application/xhtml+xml"},
        "img": {"href": "fig1.png", "media-type": "image/png"},
        "c": {"href": "ch2.xhtml", "media-type": "application/xhtml+xml"},
    }
    spine = ["a", "b", "img", "missing", "c"]
    chapters, skipped = conv.resolve_spine_chapters(manifest, spine, "")
    # Non-content (img), unknown (missing) ignored; same-file dup collapsed.
    assert chapters == [("a", "ch1.xhtml"), ("c", "ch2.xhtml")]
    assert skipped == 1


def test_resolve_spine_chapters_noop_for_unique_spine():
    manifest = {
        "a": {"href": "ch1.xhtml", "media-type": "application/xhtml+xml"},
        "b": {"href": "ch2.xhtml", "media-type": "application/xhtml+xml"},
    }
    chapters, skipped = conv.resolve_spine_chapters(manifest, ["a", "b"], "")
    assert chapters == [("a", "ch1.xhtml"), ("b", "ch2.xhtml")]
    assert skipped == 0


# ── Kokoro provider ───────────────────────────────────────────────────────────

def test_rate_to_speed_maps_and_clamps():
    assert conv._rate_to_speed("+0%") == 1.0
    assert conv._rate_to_speed("-25%") == 0.75
    assert conv._rate_to_speed("+50%") == 1.5
    assert conv._rate_to_speed("+999%") == 2.0   # clamped
    assert conv._rate_to_speed("-999%") == 0.5   # clamped


def test_set_tts_backend_validates():
    conv.set_tts_backend("kokoro", "http://host:8880/")
    assert conv.get_tts_provider() == "kokoro"
    assert conv._tts_backend["kokoro_url"] == "http://host:8880"  # trailing / stripped
    conv.set_tts_backend("nonsense")
    assert conv.get_tts_provider() == "edge"  # unknown falls back to edge


def test_kokoro_once_converts_timestamps_to_boundaries(monkeypatch):
    conv.set_tts_backend("kokoro", "http://localhost:8880")
    body = json.dumps({
        "audio": base64.b64encode(b"MP3DATA").decode(),
        "audio_format": "mp3",
        "timestamps": [
            {"word": "Hello", "start_time": 0.0, "end_time": 0.5},
            {"word": ",",     "start_time": 0.5, "end_time": 0.6},
            {"word": "world", "start_time": 0.6, "end_time": 1.0},
        ],
    }).encode()
    cap = {}
    monkeypatch.setattr(conv, "urlopen", _fake_urlopen(cap, body))

    audio, bounds = conv._kokoro_once("Hello, world", "af_heart", "+0%", True)
    assert audio == b"MP3DATA"
    assert "captioned_speech" in cap["url"]
    # punctuation-only tokens dropped; seconds converted to ms
    assert [b["text"] for b in bounds] == ["Hello", "world"]
    assert bounds[0] == {"type": "wordboundary", "offset": 0.0,
                         "duration": 500.0, "text": "Hello"}
    assert bounds[1]["offset"] == 600.0 and bounds[1]["duration"] == 400.0


def test_kokoro_once_plain_audio_uses_speech_endpoint(monkeypatch):
    conv.set_tts_backend("kokoro")
    cap = {}
    monkeypatch.setattr(conv, "urlopen", _fake_urlopen(cap, b"RAWMP3"))
    out = conv._kokoro_once("hi there", "af_heart", "+0%", False)
    assert out == b"RAWMP3"
    assert cap["url"].endswith("/v1/audio/speech")
    sent = json.loads(cap["data"].decode())
    assert sent["voice"] == "af_heart" and sent["stream"] is False


def test_kokoro_collapses_triple_hyphen_for_issue_249(monkeypatch):
    conv.set_tts_backend("kokoro")
    body = json.dumps({"audio": base64.b64encode(b"X").decode(),
                       "timestamps": []}).encode()
    cap = {}
    monkeypatch.setattr(conv, "urlopen", _fake_urlopen(cap, body))
    conv._kokoro_once("part one --- part two ----- part three", "af_heart", "+0%", True)
    sent = json.loads(cap["data"].decode())["input"]
    assert "---" not in sent


def test_synthesize_chunk_dispatches_to_kokoro(monkeypatch):
    conv.set_tts_backend("kokoro")
    monkeypatch.setattr(conv, "_kokoro_once",
                        lambda text, voice, rate, wb: b"FROM_KOKORO")
    assert conv.synthesize_chunk("hi", "af_heart", "+0%") == b"FROM_KOKORO"


def test_generate_silence_supports_kokoro_128k():
    s = conv.generate_mp3_silence(120, 128)
    assert s[:2] == b"\xff\xf3"          # MPEG-2 Layer III frame sync
    assert len(s) % 384 == 0             # 128 kbps / 24 kHz frame size


def test_strip_id3v2_removes_leading_tag():
    frames = b"\xff\xf3\xc4\xc0" + b"\x00" * 380
    tag_body = b"\x00" * 20
    id3 = b"ID3\x04\x00\x00" + bytes([0, 0, 0, len(tag_body)]) + tag_body
    assert conv._strip_id3v2(id3 + frames) == frames
    assert conv._strip_id3v2(frames) == frames  # untouched without tag


# ── Regression tests for review fixes ─────────────────────────────────────────

def test_split_long_body_enforces_max_for_middle_sentence():
    long_sent = ("word " * 500).strip()  # ~2500 chars, no inner punctuation
    text = "Short one. " + long_sent + ". Short two."
    parts = conv._split_long_body(text, 1500)
    assert all(len(p) <= 1500 for p in parts)
    assert " ".join(parts).count("word") == 500


def _epub_with_encryption(path, algorithm):
    _write_epub(path)
    enc = (
        '<?xml version="1.0"?>'
        '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
        'xmlns:enc="http://www.w3.org/2001/04/xmlenc#">'
        f'<enc:EncryptedData><enc:EncryptionMethod Algorithm="{algorithm}"/>'
        '<enc:CipherData><enc:CipherReference URI="Fonts/x.otf"/></enc:CipherData>'
        '</enc:EncryptedData></encryption>'
    )
    with zipfile.ZipFile(path, "a") as zf:
        zf.writestr("META-INF/encryption.xml", enc)


def test_font_obfuscation_is_not_flagged_as_drm(tmp_path):
    p = tmp_path / "fonts.epub"
    _epub_with_encryption(p, "http://www.idpf.org/2008/embedding")
    logs = []
    with zipfile.ZipFile(p) as zf:
        assert conv._check_drm(zf, logs.append) is False
    assert logs == []


def test_real_encryption_is_flagged_as_drm(tmp_path):
    p = tmp_path / "drm.epub"
    _epub_with_encryption(p, "http://www.w3.org/2001/04/xmlenc#aes128-cbc")
    logs = []
    with zipfile.ZipFile(p) as zf:
        assert conv._check_drm(zf, logs.append) is True
    assert any("DRM" in m for m in logs)


def test_text_split_inserts_pause_between_paragraphs():
    text = "Para one is here.\n\nPara two is here."
    items = conv.split_text_for_tts(text, para_pause_ms=450)
    assert items == [
        "Para one is here.",
        (conv._SILENCE, 450),
        "Para two is here.",
    ]


def test_text_split_merges_paragraphs_when_pause_is_zero():
    text = "Para one is here.\n\nPara two is here."
    items = conv.split_text_for_tts(text, para_pause_ms=0, section_pause_ms=0)
    assert items == ["Para one is here. Para two is here."]


def test_text_split_hard_wrapped_lines_join_without_pause():
    text = "A sentence that was\nhard-wrapped mid flow.\n\nNext para."
    items = conv.split_text_for_tts(text, para_pause_ms=450)
    assert items[0] == "A sentence that was hard-wrapped mid flow."
    assert items[1] == (conv._SILENCE, 450)


def test_build_smil_skips_empty_audio_paragraphs():
    paragraphs = [{"id": "p1"}, {"id": "p2"}]
    smil, total = conv.build_smil(
        paragraphs, [b"", b"X" * 600],
        "Text/ch1.xhtml", "Audio/ch1.mp3", "SMIL",
    )
    assert "#p1" not in smil
    assert "#p2" in smil
    assert total == conv.bytes_to_ms(600)


def test_prescan_counts_deduped_spine(monkeypatch, tmp_path):
    # A spine that repeats the same chapter must be counted once, like the
    # conversion itself does.
    inp = tmp_path / "dup.epub"
    _write_epub(inp)
    # Rewrite with a duplicated spine entry.
    import re as _re
    with zipfile.ZipFile(inp) as zf:
        names = {n: zf.read(n) for n in zf.namelist()}
    names["EPUB/content.opf"] = _re.sub(
        rb'<itemref idref="ch1"/>',
        b'<itemref idref="ch1"/><itemref idref="ch1"/>',
        names["EPUB/content.opf"],
    )
    with zipfile.ZipFile(inp, "w") as zf:
        for n, d in names.items():
            zf.writestr(n, d)

    class _App:
        pass

    counts = conv.ConverterApp._prescan_files(
        _App(), [str(inp)], False, False)
    _, paras = conv.process_chapter(names["EPUB/Text/ch1.xhtml"], 0)
    assert counts == [sum(len(p["text"]) for p in paras)]

