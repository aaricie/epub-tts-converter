"""Small deterministic reproductions for review only, with synthetic audio."""
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
for name, html in {
    "mixed_p_div_li": '<html><body><p>First paragraph.</p><div>Second paragraph in a div.</div><ul><li>List information.</li></ul></body></html>',
    "short_dialogue": '<html><body><p>Yes.</p><p>No.</p><p>Go!</p><p>Longer sentence.</p></body></html>',
    "comment": '<html><body><p>Hello <!-- editorial note -->world.</p></body></html>',
    "fallback_block_spacing": '<html><body><div>First line<div>Second line</div></div></body></html>',
    "inline_note_section": '<html xmlns:epub="http://www.idpf.org/2007/ops"><body><h1>Chapter One</h1><p>Narrative text.</p><aside epub:type="footnote"><p>Footnote text.</p></aside></body></html>',
}.items():
    raw = html.encode()
    soup, paragraphs = c.process_chapter(raw, 1)
    results[name] = {"tts_texts": [p["text"] for p in paragraphs], "backmatter": c._is_backmatter(raw, "section001.xhtml")}

f = runpy.run_path(str(BASE / "tests" / "test_epub_tts_converter.py"))
c.synthesize_paragraphs = f["_fake_synth"]
with tempfile.TemporaryDirectory(dir=BASE / "audit", prefix="epub-integrity-") as td:
    td = pathlib.Path(td)
    source, dest = td / "in.epub", td / "out.epub"
    f["_write_epub"](source, body='<html xmlns="http://www.w3.org/1999/xhtml"><body><p id="intro">First paragraph.</p><p id="section-two">Second paragraph.</p></body></html>', ncx=True)
    with zipfile.ZipFile(source, "r") as z:
        files = {name: z.read(name) for name in z.namelist()}
    ncx = ET.fromstring(files["EPUB/toc.ncx"])
    content = ncx.find(f".//{{{c.NCX_NS}}}content")
    content.set("src", "Text/ch1.xhtml#section-two")
    files["EPUB/toc.ncx"] = ET.tostring(ncx)
    opf = ET.fromstring(files["EPUB/content.opf"])
    spine = opf.find(f"{{{c.OPF_NS}}}spine")
    ET.SubElement(spine, f"{{{c.OPF_NS}}}itemref", idref="ch1")
    files["EPUB/content.opf"] = ET.tostring(opf)
    with zipfile.ZipFile(source, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    logs = []
    c.convert_epub_to_epub3(str(source), str(dest), "af_heart", "+0%", 1, logs.append)
    with zipfile.ZipFile(dest) as z:
        opf = ET.fromstring(z.read("EPUB/content.opf"))
        nav = ET.fromstring(z.read("EPUB/nav.xhtml"))
        results["ncx_fragment"] = {"input_target": "Text/ch1.xhtml#section-two", "output_target": nav.find(".//{http://www.w3.org/1999/xhtml}a").get("href")}
        results["duplicate_spine"] = {"log": logs[0], "output_itemrefs": [x.get("idref") for x in opf.findall(f".//{{{c.OPF_NS}}}itemref")]}

(BASE / "audit" / "epub_integrity_repros.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
print(json.dumps(results, indent=2))
