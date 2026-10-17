"""Small sequential request benchmark; use an explicit IPv4 loopback URL."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import statistics
import time
import urllib.request

from verify_pcm_contract import PARAGRAPHS


def call(url, text):
    payload = {"input": text, "voice": "af_heart", "speed": 1.0, "model": "kokoro",
               "stream": False, "response_format": "pcm", "return_timestamps": True}
    req = urllib.request.Request(url + "/dev/captioned_speech", json.dumps(payload).encode(),
                                 {"Content-Type": "application/json"})
    start = time.perf_counter()
    with urllib.request.urlopen(req, timeout=120) as response:
        data = json.load(response)
    audio = base64.b64decode(data["audio"])
    elapsed = time.perf_counter() - start
    words = [t["word"] for t in data["timestamps"]]
    return {"elapsed_s": elapsed, "audio_s": len(audio)/48000,
            "audio_bytes": len(audio), "audio_sha256": hashlib.sha256(audio).hexdigest(),
            "words": words, "timestamps": data["timestamps"]}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8881")
    p.add_argument("--variant", required=True)
    p.add_argument("--repeats", type=int, default=4)
    args = p.parse_args()
    call(args.url, "Warm up the model and connection before recording measurements.")
    rows = []
    for repeat in range(args.repeats):
        cases = [("paragraph", PARAGRAPHS[0]), ("four_paragraphs", "\n\n".join(PARAGRAPHS))]
        if repeat % 2:
            cases.reverse()
        for case, text in cases:
            result = call(args.url, text)
            rows.append({"case": case, "repeat": repeat, **result})
            Path(__file__).with_name("server_benchmark_"+args.variant+".json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
            print(json.dumps({"variant": args.variant, "case": case, "repeat": repeat,
                              "elapsed_s": result["elapsed_s"], "audio_s": result["audio_s"]}), flush=True)
    print(json.dumps({case:statistics.median(r["elapsed_s"] for r in rows if r["case"]==case)
                      for case in ("paragraph", "four_paragraphs")}))


if __name__ == "__main__":
    main()
