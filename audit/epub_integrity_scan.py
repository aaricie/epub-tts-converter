"""Read-only EPUB audit helper; does not modify application or input books."""
import collections
import json
import pathlib
import sys
import time
import zipfile
from xml.etree import ElementTree as ET

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import epub_tts_converter as c
from bs4 import NavigableString, Comment

BASE = pathlib.Path(__file__).resolve().parents[1]


def scan_book(path):
    started = time.perf_counter()
    out = {"name": path.name, "input_bytes": path.stat().st_size, "chapters": []}
    with zipfile.ZipFile(path) as z:
        opf = c.find_opf_path(z)
        root, manifest, spine, d = c.parse_opf(z, opf)
        chapters, duplicates = c.resolve_spine_chapters(manifest, spine, d)
        out.update(spine_items=len(spine), unique_chapters=len(chapters), duplicate_spine_items=duplicates)
        for i, (spine_id, href) in enumerate(chapters, 1):
            raw = z.read(c.epub_path(d, href))
            soup, paras = c.process_chapter(raw, i)
            selected = {id(p["tag"]) for p in paras}
            uncovered = collections.Counter()
            examples = []
            root_tag = soup.body or soup
            for n in root_tag.descendants:
                if not isinstance(n, NavigableString) or isinstance(n, Comment) or not str(n).strip():
                    continue
                if any(a.name in {"script", "style"} for a in n.parents):
                    continue
                if c._node_is_tts_skipped(n, root_tag):
                    continue
                if any(id(a) in selected for a in n.parents):
                    continue
                val = str(n).strip()
                uncovered[n.parent.name] += len(val)
                if len(examples) < 5:
                    examples.append({"tag": n.parent.name, "text": val[:85]})
            xml_error = None
            try:
                ET.fromstring(c._to_xhtml_bytes(soup))
            except ET.ParseError as e:
                xml_error = str(e)
            out["chapters"].append({
                "href": href, "paras": len(paras), "selected_chars": sum(len(p["text"]) for p in paras),
                "frontmatter": c._is_frontmatter(raw, href), "backmatter": c._is_backmatter(raw, href),
                "footnote_file": c._is_footnote_file(href), "uncovered_chars": sum(uncovered.values()),
                "uncovered_tags": dict(uncovered), "examples": examples, "xml_error": xml_error,
            })
    out["seconds"] = round(time.perf_counter() - started, 3)
    return out


if __name__ == "__main__":
    results = []
    for path in sorted((BASE / "test books").glob("*.epub")):
        try:
            b = scan_book(path)
            results.append(b)
            kept = [ch for ch in b["chapters"] if not(ch["frontmatter"] or ch["backmatter"] or ch["footnote_file"])]
            print(json.dumps({"book": b["name"], "seconds": b["seconds"], "chapters": b["unique_chapters"],
                "duplicate_spine": b["duplicate_spine_items"], "narrated_chars": sum(ch["selected_chars"] for ch in kept),
                "uncovered_chars_kept": sum(ch["uncovered_chars"] for ch in kept),
                "invalid_xml_chapters": sum(bool(ch["xml_error"]) for ch in b["chapters"])}), flush=True)
        except Exception as e:
            results.append({"name": path.name, "error": repr(e)})
            print(json.dumps(results[-1]), flush=True)
        (BASE / "audit" / "epub_integrity_scan.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
