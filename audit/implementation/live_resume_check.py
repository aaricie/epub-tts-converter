"""Cancel a real conversion part-way, then resume it and count engine requests.

    python audit/implementation/live_resume_check.py --url http://127.0.0.1:8880
"""
import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import epub_tts_converter as conv  # noqa: E402

SOURCE = ROOT / "audit" / "implementation" / "bench" / "excerpt_history.epub"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8880")
    parser.add_argument("--cancel-after", type=int, default=4)
    args = parser.parse_args()
    if not SOURCE.exists():
        sys.exit("Run bench_pipeline.py first to create the excerpt.")
    work = Path(tempfile.mkdtemp(prefix="resume-"))
    output = work / "book_audio.epub"
    backend = {"provider": "kokoro", "kokoro_url": args.url, "batch_paragraphs": False}
    calls = []
    original = conv._kokoro_once

    def counted(text, *a, **k):
        calls.append(text)
        return original(text, *a, **k)
    conv._kokoro_once = counted
    try:
        cancel = threading.Event()

        def progress(message):
            if f"{args.cancel_after}/" in message and "synthesized" in message:
                cancel.set()
        try:
            conv.convert_epub_to_epub3(str(SOURCE), str(output), "af_heart", "+0%", 1, progress,
                                       cancel_event=cancel, para_pause_ms=350, bitrate_kbps=48,
                                       backend=backend)
            first = "completed (cancel came too late)"
        except conv.ConversionCancelled:
            first = "cancelled"
        first_calls = len(calls)
        published_after_cancel = output.exists()
        logs = []
        conv.convert_epub_to_epub3(str(SOURCE), str(output), "af_heart", "+0%", 1, logs.append,
                                   para_pause_ms=350, bitrate_kbps=48, backend=backend)
        with zipfile.ZipFile(output) as book:
            smil = book.read("EPUB/SMIL/ch001.smil").decode()
        report = {"first_run": first, "requests_before_cancel": first_calls,
                  "output_published_after_cancel": published_after_cancel,
                  "requests_on_resume": len(calls) - first_calls,
                  "paragraphs": 10, "clips": smil.count("<par "),
                  "resume_summary": [line for line in logs if line.startswith("Done!")]}
        print(json.dumps(report, indent=2))
    finally:
        conv._kokoro_once = original
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
