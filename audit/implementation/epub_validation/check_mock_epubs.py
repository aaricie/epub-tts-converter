"""Run representative full packaging with fake audio; check XML and targets."""
import collections
import json
import pathlib
import re
import sys
import time
import urllib.parse
import zipfile
from xml.etree import ElementTree as ET

BASE = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BASE))
import epub_tts_converter as c

OUTPUT = BASE / "audit/implementation/epub_validation/artifacts/outputs"
OUTPUT.mkdir(parents=True, exist_ok=True)
AUDIO = c.generate_mp3_silence(1000)


def fake_synth(paragraphs, *args, **kwargs):
    # One word marker per paragraph keeps validation light. No server requests.
    for p in paragraphs:
        yield AUDIO, [{"type": "wordboundary", "offset": 0, "duration": 200,
                       "text": p["tts_text"].split()[0]}]


c.iter_synthesized_paragraphs = fake_synth
results = []
for stem in ["L'Ontario", "Kursk", "The Lord", "The Wages", "The-Origins"]:
    p = next((BASE / "test books").glob(stem + "*.epub"))
    output = OUTPUT / p.name
    started = time.perf_counter()
    logs = []
    try:
        c.convert_epub_to_epub3(str(p), str(output), "audit_fake_audio", "+0%", 1,
                              logs.append, skip_frontmatter=True, skip_backmatter=True,
                              backend={"provider": "edge", "kokoro_url": "unused"})
    except Exception as error:
        result = {"name": p.name, "seconds": round(time.perf_counter()-started, 3),
                  "conversion_error": f"{type(error).__name__}: {error}", "output_exists": output.exists()}
        results.append(result)
        (BASE / "audit/implementation/epub_validation/mock_results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(json.dumps(result), flush=True)
        continue
    result = {"name": p.name, "seconds": round(time.perf_counter() - started, 3),
              "output_bytes": output.stat().st_size, "xml_errors": [], "source_xml_errors": [],
              "duplicate_ids": [], "missing_targets": [], "wrong_xhtml_namespace": [],
              "smil_documents": 0, "smil_text_targets": 0}
    with zipfile.ZipFile(output) as z, zipfile.ZipFile(p) as source:
        opf = c.find_opf_path(z)
        root, manifest, spine, directory = c.parse_opf(z, opf)
        tree_cache = {}
        for item in manifest.values():
            if item["media-type"] not in c.CONTENT_TYPES | {"application/smil+xml"}:
                continue
            name = c.epub_path(directory, item["href"])
            try:
                tree = ET.fromstring(z.read(name))
                tree_cache[name] = tree
            except Exception as e:
                result["xml_errors"].append({"file": name, "error": str(e)})
                try:
                    ET.fromstring(source.read(name))
                except Exception as se:
                    result["source_xml_errors"].append({"file": name, "error": str(se)})
                continue
            ids = [el.get("id") for el in tree.iter() if el.get("id")]
            duplicates = [k for k, v in collections.Counter(ids).items() if v > 1]
            if duplicates:
                result["duplicate_ids"].append({"file": name, "ids": duplicates[:20]})
            if item["media-type"] in c.CONTENT_TYPES and tree.tag != "{http://www.w3.org/1999/xhtml}html":
                result["wrong_xhtml_namespace"].append({"file": name, "root": tree.tag})
        for name, tree in tree_cache.items():
            if tree.tag != "{http://www.w3.org/ns/SMIL}smil":
                continue
            result["smil_documents"] += 1
            for element in tree.iter():
                if element.tag not in {"{http://www.w3.org/ns/SMIL}text", "{http://www.w3.org/ns/SMIL}audio"}:
                    continue
                src = element.get("src")
                target, _, fragment = src.partition("#")
                target = c.epub_path(str(pathlib.PurePosixPath(name).parent), target)
                if target not in z.namelist():
                    result["missing_targets"].append({"file": name, "src": src, "reason": "file absent"})
                elif fragment:
                    result["smil_text_targets"] += 1
                    target_tree = tree_cache.get(target)
                    if target_tree is not None and not any(el.get("id") == urllib.parse.unquote(fragment) for el in target_tree.iter()):
                        result["missing_targets"].append({"file": name, "src": src, "reason": "id absent"})
    results.append(result)
    (BASE / "audit/implementation/epub_validation/mock_results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(result), flush=True)
