"""End-to-end conversion benchmark on excerpts of real books.

Each run converts a small single-chapter EPUB (real paragraphs, plain markup)
with a fresh checkpoint cache, so every request reaches the engine. Variants
are interleaved to spread drift (thermal, background load) across all of them.

    python audit/implementation/bench_pipeline.py --url http://127.0.0.1:8880 --reps 2
"""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import re
import shutil
import statistics
import sys
import tempfile
import time
import zipfile
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(1, str(Path(__file__).resolve().parent))
import audio_pipeline  # noqa: E402
import epub_tts_converter as conv  # noqa: E402
import kokoro_batching  # noqa: E402
import legacy_kokoro_batching as legacy  # noqa: E402

_CURRENT = (kokoro_batching.plan_paragraph_batches, kokoro_batching.split_pcm_batch)


@contextmanager
def use_batching_module(module):
    """The converter imports the planner at call time; swap it for a run."""
    kokoro_batching.plan_paragraph_batches = module.plan_paragraph_batches
    kokoro_batching.split_pcm_batch = module.split_pcm_batch
    try:
        yield
    finally:
        kokoro_batching.plan_paragraph_batches, kokoro_batching.split_pcm_batch = _CURRENT

BOOKS = ROOT / "test books"
OUT = ROOT / "audit" / "implementation" / "bench"
EXCERPTS = {
    # name: (book file, spine href, first paragraph, paragraph count)
    "fiction": ("The Lord of the Rings - J. R. R. Tolkien.epub", "Text/LordoftheRings_chap-1.html", 10, 30),
    "history": ("Suicide in Nazi Germany.epub", "xhtml/ch01.html", 3, 10),
}
VARIANTS = {
    # name: (lookahead, batching: None, "legacy" or "current")
    "serial": (0, None),
    "serial+legacy-batch": (0, "legacy"),
    "prefetch": (1, None),
    "prefetch+batch": (1, "current"),
}


def make_excerpt(name):
    book, href, first, count = EXCERPTS[name]
    path = OUT / f"excerpt_{name}.epub"
    if path.exists():
        return path
    with zipfile.ZipFile(BOOKS / book) as source:
        opf = conv.find_opf_path(source)
        _, _, _, opf_dir = conv.parse_opf(source, opf)
        _, paragraphs = conv.process_chapter(source.read(conv.epub_path(opf_dir, href)), 1)
    texts = [p["text"] for p in paragraphs[first:first + count]]
    body = "".join(f"<p>{escape(text)}</p>" for text in texts)
    OUT.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml",
                         '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
                         '<rootfiles><rootfile full-path="EPUB/book.opf" media-type="application/oebps-package+xml"/>'
                         '</rootfiles></container>')
        archive.writestr("EPUB/book.opf",
                         '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
                         '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                         f'<dc:identifier id="id">urn:uuid:bench-{name}</dc:identifier>'
                         f'<dc:title>Benchmark excerpt: {name}</dc:title><dc:language>en</dc:language>'
                         '<meta property="dcterms:modified">2026-10-17T00:00:00Z</meta></metadata>'
                         '<manifest><item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
                         '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/></manifest>'
                         '<spine><itemref idref="ch1"/></spine></package>')
        archive.writestr("EPUB/nav.xhtml",
                         '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
                         '<head><title>Contents</title></head><body><nav epub:type="toc"><ol>'
                         '<li><a href="ch1.xhtml">Excerpt</a></li></ol></nav></body></html>')
        archive.writestr("EPUB/ch1.xhtml",
                         '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Excerpt</title></head>'
                         f"<body>{body}</body></html>")
    return path


class Probe:
    """Wrap engine calls and client-side work to see where the time goes."""

    def __init__(self):
        self.server_s = self.encode_s = self.spans_s = 0.0
        self.requests = 0
        self._patches = []

    def _wrap(self, owner, name, field):
        original = getattr(owner, name)

        def timed(*args, **kwargs):
            started = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                setattr(self, field, getattr(self, field) + time.perf_counter() - started)
                if field == "server_s":
                    self.requests += 1
        setattr(owner, name, timed)
        self._patches.append((owner, name, original))

    def __enter__(self):
        self._wrap(conv, "_kokoro_once", "server_s")
        self._wrap(audio_pipeline.PCMChapterWriter, "append_pcm", "encode_s")
        self._wrap(conv, "inject_word_spans", "spans_s")
        return self

    def __exit__(self, *exc):
        for owner, name, original in reversed(self._patches):
            setattr(owner, name, original)


def run_once(source, url, variant, voice):
    lookahead, batching = VARIANTS[variant]
    conv.SYNTHESIS_LOOKAHEAD = lookahead
    planner = legacy if batching == "legacy" else kokoro_batching
    work = Path(tempfile.mkdtemp(prefix="bench-"))
    try:
        output = work / "out.epub"
        logs = []
        with Probe() as probe, use_batching_module(planner):
            started = time.perf_counter()
            conv.convert_epub_to_epub3(
                str(source), str(output), voice, "+0%", 1, logs.append,
                para_pause_ms=350, bitrate_kbps=48,
                backend={"provider": "kokoro", "kokoro_url": url,
                         "batch_paragraphs": batching is not None})
            wall = time.perf_counter() - started
        with zipfile.ZipFile(output) as book:
            smil = book.read("EPUB/SMIL/ch001.smil").decode()
        total = [line for line in logs if line.startswith("Done!")]
        batched = fallback = 0
        for line in logs:
            # Per-batch lines (first design) or one summary per chapter (current).
            if "could not be mapped" in line:
                fallback += 1
            elif match := re.search(r"Batched \d+ short paragraphs into (\d+) requests"
                                    r"(?:; (\d+) batch)?", line):
                batched += int(match.group(1))
                fallback += int(match.group(2) or 0)
            elif "Batched" in line:
                batched += 1
        return {"variant": variant, "wall_s": wall, "server_s": probe.server_s,
                "encode_s": probe.encode_s, "spans_s": probe.spans_s,
                "requests": probe.requests, "batches": batched, "batch_fallbacks": fallback,
                "clips": smil.count("<par "), "summary": total[-1] if total else ""}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8880")
    parser.add_argument("--reps", type=int, default=2)
    parser.add_argument("--voice", default="af_heart")
    parser.add_argument("--excerpts", nargs="*", default=list(EXCERPTS))
    parser.add_argument("--variants", nargs="*", default=list(VARIANTS))
    parser.add_argument("--label", default="baseline")
    args = parser.parse_args()
    rows = []
    for name in args.excerpts:
        source = make_excerpt(name)
        # Warm the engine (model load, voice cache) outside the measurements.
        conv.synthesize_chunk("Warm up.", args.voice, audio_format="kokoro-pcm", word_boundary=True,
                              backend={"provider": "kokoro", "kokoro_url": args.url})
        for rep in range(args.reps):
            order = args.variants if rep % 2 == 0 else list(reversed(args.variants))
            for variant in order:
                row = run_once(source, args.url, variant, args.voice)
                row.update(excerpt=name, rep=rep)
                rows.append(row)
                print(json.dumps(row), flush=True)
    summary = {}
    for name in args.excerpts:
        for variant in args.variants:
            walls = [r["wall_s"] for r in rows if r["excerpt"] == name and r["variant"] == variant]
            if walls:
                summary[f"{name}/{variant}"] = round(statistics.median(walls), 2)
    print(json.dumps(summary, indent=2))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"results_{args.label}.json").write_text(json.dumps({"rows": rows, "median_wall_s": summary}, indent=2))


if __name__ == "__main__":
    main()
