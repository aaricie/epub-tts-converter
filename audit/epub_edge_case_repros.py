"""Small synthetic format, locale, and repeated-conversion checks."""
import collections
import json
import pathlib
import runpy
import sys
import tempfile
import zipfile
from xml.etree import ElementTree as ET

BASE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import epub_tts_converter as c

results = {}
calls = []
original_chunk = c.synthesize_chunk
c.synthesize_chunk = lambda text, *args, **kwargs: calls.append(text) or b"actual_speech"
texts = ["こんにちは世界。", "你好，这是一段测试文本。", "नमस्ते दुनिया।", "Γειά σου κόσμε.", "12345", "English text."]
audio = c.synthesize_paragraphs([{"text": t} for t in texts], "af_heart", "+0%", 1, lambda _: None)
results["script_filter"] = [{"text": text, "tts_called": text in calls, "bytes_returned": len(a)} for text, a in zip(texts, audio)]
c.synthesize_chunk = original_chunk

f = runpy.run_path(str(BASE / "tests" / "test_epub_tts_converter.py"))
c.synthesize_paragraphs = f["_fake_synth"]
with tempfile.TemporaryDirectory(dir=BASE / "audit", prefix="epub-edge-") as td:
    td = pathlib.Path(td)
    source, first, second = td / "in.epub", td / "first.epub", td / "second.epub"
    f["_write_epub"](source)
    for a, b in [(source, first), (first, second)]:
        c.convert_epub_to_epub3(str(a), str(b), "audit_fake", "+0%", 1, lambda _: None)
    with zipfile.ZipFile(second) as z:
        html = ET.fromstring(z.read("EPUB/Text/ch1.xhtml"))
        ids = collections.Counter(el.get("id") for el in html.iter() if el.get("id"))
        opf = ET.fromstring(z.read("EPUB/content.opf"))
        results["repeat_conversion"] = {
            "duplicate_xhtml_ids": {k: v for k, v in ids.items() if v > 1},
            "audio_files": [n for n in z.namelist() if n.endswith(".mp3")],
            "global_media_duration_entries": sum(m.get("property") == "media:duration" and not m.get("refines") for m in opf.findall(f".//{{{c.OPF_NS}}}meta")),
        }
    synth_calls = []
    original_synth = c.synthesize_paragraphs
    def fake(paragraphs, *args, **kwargs):
        synth_calls.extend(paragraphs)
        return original_synth(paragraphs, *args, **kwargs)
    c.synthesize_paragraphs = fake
    try:
        c.convert_epub_to_epub3(str(source), str(td / "missing-dir" / "result.epub"), "audit_fake", "+0%", 1, lambda _: None)
    except FileNotFoundError as e:
        results["missing_output_dir"] = {"failed_after_synthesized_paragraphs": len(synth_calls), "exception": type(e).__name__}

(BASE / "audit" / "epub_edge_case_repros.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
print(json.dumps(results, indent=2))
