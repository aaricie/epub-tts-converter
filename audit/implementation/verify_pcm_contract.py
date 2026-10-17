"""Bounded sequential live contract checks against the installed Kokoro server.

Synthetic public-domain test sentences only; raw responses remain audit evidence.
This checks structure and timelines, not perceptual alignment or pronunciation.
"""
from __future__ import annotations

import argparse
import array
import base64
import difflib
import json
import math
from pathlib import Path
import re
import time
import urllib.error
import urllib.request


OUT = Path(__file__).resolve().parent / "pcm_contract"
PARAGRAPHS = [
    "The old library stood beside a quiet square. Every morning, the caretaker opened the shutters and arranged the returned books on a wooden table. Visitors came to read, study, and exchange stories about the town. By noon, sunlight reached the farthest shelves, and the room felt warm and welcoming.",
    "Our experiment compares several ways of producing spoken audio. We use a small collection of ordinary paragraphs, keep the voice and reading speed the same, and measure the time needed to finish every request. This makes the results easier to compare, although a complete book will contain much more varied material.",
    "Beyond the village, a narrow path led through the forest. The travelers followed it until they reached a bridge over the river. There they stopped for lunch and watched the water moving around the stones. When the rain finally passed, they packed their bags and continued toward the mountains.",
    "A useful computer program should report its progress honestly. If an operation fails, the user needs to know what remains unfinished. Saving completed work can make a long task easier to recover after an interruption. Clear messages and reliable output are often more valuable than a complicated collection of options.",
]
CASES = [
    ("short_yes", "Yes.", "af_heart", 1.0),
    ("french", "Bonjour. Le train arrivera demain matin. Nous avons acheté trois livres pour les enfants.", "ff_siwis", 1.0),
    ("english", PARAGRAPHS[0], "af_heart", 1.0),
    ("british", PARAGRAPHS[0], "bf_emma", 1.0),
    ("short_no", "No.", "af_heart", 1.0),
    ("numbers", "In 1945, Dr. Smith paid $14.50 for 3 books. On March 12th, the temperature was -4.5 degrees.", "af_heart", 1.0),
    ("bare_year", "1945", "af_heart", 1.0),
    ("abbreviations", "Mr. Green met Mrs. Brown and Dr. Wilson at 4 p.m. They discussed the U.S.A., NATO, and e.g. several other abbreviations.", "af_heart", 1.0),
    ("hyphens", "A well-known author wrote a twenty-one-page book. Mother-in-law, state-of-the-art, co-operation, and re-enter are hyphenated expressions.", "af_heart", 1.0),
    ("newlines", "First line ends here.\nSecond line starts here.\n\nThird paragraph contains a final sentence.", "af_heart", 1.0),
    ("punctuation", "\"Hello!\" she said. \"Really?\" He paused... Then answered: yes; absolutely. (The room was quiet.) Finally—silence.", "af_heart", 1.0),
    ("no_final_punctuation", "Please retain every word including the final word ending", "af_heart", 1.0),
    ("slow", PARAGRAPHS[2], "af_heart", 0.75),
    ("fast", PARAGRAPHS[2], "af_heart", 1.5),
    ("multiparagraph", "\n\n".join(PARAGRAPHS), "af_heart", 1.0),
    ("long_single_sentence", "A person walking along the road noticed " + ", ".join(["the bright windows beside the silent garden and the old stone wall"] * 30) + " and finally reached the destination.", "af_heart", 1.0),
    ("french_slow", "En 1945, le docteur Martin avait acheté vingt et un livres. Aujourd'hui, ils coûtent 14,50 euros.", "ff_siwis", 0.75),
    ("invalid_voice", "This request should fail before generating audio.", "not_a_real_voice", 1.0),
]


def tokens(text):
    return [x.casefold() for x in re.findall(r"[^\W_]+(?:['’][^\W_]+)*", text)]


def inspect(meta, audio, source):
    duration = len(audio) / 48000
    stamps = meta.get("timestamps") or []
    finite = all(isinstance(t.get(k), (int, float)) and math.isfinite(t[k])
                 for t in stamps for k in ("start_time", "end_time"))
    lexical = [t for t in stamps if any(c.isalnum() for c in t.get("word", ""))]
    source_tokens = tokens(source)
    response_tokens = tokens(" ".join(t.get("word", "") for t in stamps))
    changes = []
    for tag, a, b, c, d in difflib.SequenceMatcher(None, source_tokens, response_tokens, autojunk=False).get_opcodes():
        if tag != "equal":
            changes.append({"kind": tag, "source": source_tokens[a:b], "caption": response_tokens[c:d]})
    pcm = array.array("h", audio)
    significant = [i for i, value in enumerate(pcm) if abs(value) > 200]
    report = {
        "audio_format": meta.get("audio_format"), "audio_bytes": len(audio),
        "even_bytes": len(audio) % 2 == 0, "assumed_sample_rate": 24000,
        "duration_s": duration, "peak": max(map(abs, pcm), default=0),
        "first_significant_s": significant[0] / 24000 if significant else None,
        "last_significant_s": significant[-1] / 24000 if significant else None,
        "timestamp_count": len(stamps), "lexical_timestamp_count": len(lexical),
        "finite": finite, "source_token_count": len(source_tokens),
        "caption_token_count": len(response_tokens), "token_changes": changes,
        "first_timestamp": stamps[0] if stamps else None,
        "last_timestamp": stamps[-1] if stamps else None,
        "last_lexical_timestamp": lexical[-1] if lexical else None,
    }
    if stamps and finite:
        report.update({
            "min_start_s": min(t["start_time"] for t in stamps),
            "max_end_s": max(t["end_time"] for t in stamps),
            "max_end_overshoot_s": max(t["end_time"] for t in stamps) - duration,
            "lexical_end_overshoot_s": max(t["end_time"] for t in lexical) - duration if lexical else None,
            "reversed_spans": [i for i,t in enumerate(stamps) if t["end_time"] < t["start_time"]],
            "backward_starts": [i for i in range(1,len(stamps)) if stamps[i]["start_time"] < stamps[i-1]["start_time"]],
            "overlap_count": sum(stamps[i]["start_time"] < stamps[i-1]["end_time"]-1e-6 for i in range(1,len(stamps))),
            "lexical_wholly_outside": [t for t in lexical if t["start_time"] >= duration or t["end_time"] <= 0],
        })
    return report


def contract_errors(row):
    """Fixture expectations for this pinned installation, not a generic aligner."""
    if row["case"] == "invalid_voice":
        return [] if row.get("status") == 400 else ["invalid voice did not fail with HTTP 400"]
    errors = []
    if row.get("status") != 200 or row.get("error"):
        return ["unexpected endpoint failure"]
    for key in ("even_bytes", "finite"):
        if not row.get(key):
            errors.append(key)
    if row.get("audio_format") != "audio/pcm" or row.get("peak", 0) <= 0:
        errors.append("missing PCM audio")
    if row["voice"].startswith("ff_"):
        return errors  # Missing French captions are a documented capability limit.
    if not row.get("lexical_timestamp_count"):
        return errors + ["English lexical timestamps missing"]
    if row.get("reversed_spans") or row.get("lexical_wholly_outside"):
        errors.append("invalid lexical ranges")
    if row.get("min_start_s", 0) < -0.25 or row.get("lexical_end_overshoot_s", 0) > 0.35:
        errors.append("unexpectedly large boundary error")
    expected = row["source"]
    if row["case"] == "numbers":
        expected = expected.replace("1945", "nineteen forty-five").replace("Dr.", "Doctor").replace("$14.50", "fourteen dollars and fifty cents").replace("3 books", "three books").replace("-4.5", "minus four point five")
    elif row["case"] == "bare_year":
        expected = "nineteen forty-five"
    elif row["case"] == "abbreviations":
        expected = expected.replace("Mr.", "Mister").replace("Dr.", "Doctor").replace("4 p.m.", "four p.m.")
    caption_path = row["caption_path"]
    stamps = json.loads(Path(caption_path).read_text(encoding="utf-8"))["timestamps"]
    lexical = [t for t in stamps if any(c.isalnum() for c in t.get("word", ""))]
    if any(lexical[i]["start_time"] < lexical[i-1]["start_time"] for i in range(1, len(lexical))):
        errors.append("lexical starts move backward")
    if tokens(expected) != tokens(" ".join(t["word"] for t in stamps)):
        errors.append("normalized lexical source coverage differs")
    return errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["smoke", "full"], default="full")
    parser.add_argument("--url", default="http://localhost:8880")
    parser.add_argument("--output", type=Path, default=OUT)
    parser.add_argument("--only", help="Comma-separated case names for a smaller verification run")
    args = parser.parse_args()
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(args.url + "/v1/audio/voices", timeout=10) as response:
        voices = json.load(response)
    (out / "voices.json").write_text(json.dumps(voices, indent=2), encoding="utf-8")
    rows = []
    cases = CASES[:3] if args.phase == "smoke" else CASES
    if args.only:
        requested = set(args.only.split(","))
        cases = [case for case in cases if case[0] in requested]
        if {case[0] for case in cases} != requested:
            raise ValueError("Unknown or excluded case name in --only")
    for name, text, voice, speed in cases:
        payload = {"model": "kokoro", "input": text, "voice": voice, "speed": speed,
                   "response_format": "pcm", "stream": False, "return_timestamps": True}
        req = urllib.request.Request(args.url + "/dev/captioned_speech", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        row = {"case": name, "voice": voice, "speed": speed, "source": text}
        start = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                meta = json.load(response)
                row["status"] = response.status
            audio = base64.b64decode(meta.pop("audio"), validate=True)
            (out / (name + ".pcm")).write_bytes(audio)
            (out / (name + ".json")).write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            row.update(inspect(meta, audio, text))
            row["caption_path"] = str((out / (name + ".json")).resolve())
        except urllib.error.HTTPError as exc:
            row.update(status=exc.code, error=exc.read().decode())
        except Exception as exc:
            row.update(error=repr(exc))
        row["elapsed_s"] = time.perf_counter() - start
        row["contract_errors"] = contract_errors(row)
        rows.append(row)
        (out / (args.phase + "_results.json")).write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({key: value for key, value in row.items() if key not in ("source", "token_changes")}), flush=True)
    if any(row["contract_errors"] for row in rows):
        raise SystemExit("PCM contract verification failed; inspect the saved results")


if __name__ == "__main__":
    main()
