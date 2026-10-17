"""Offline, bounded audit probes: no network requests or actual TTS.

Run from any directory with the project's Python:
    python audit/client_pipeline_probes.py
Writes client_pipeline_probe_results.json beside this script.
"""
from io import BytesIO
import json
from pathlib import Path
import sys
import threading
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import epub_tts_converter as c


def main():
    out = {}
    seen = []

    def synth(text, *args, **kwargs):
        seen.append(text)
        return b"X" * 160

    with patch.object(c, "synthesize_chunk", synth):
        c._synthesize_with_sentence_pauses(
            "word " * 600, "af_heart", "+0%", "kokoro-mp3", False, 200, 128
        )
    out["single_sentence_request_lengths"] = [len(s) for s in seen]
    seen.clear()
    logs = []

    def partial(text, *args, **kwargs):
        seen.append(text)
        if text.startswith("Missing"):
            raise RuntimeError("simulated failure")
        return b"GOOD"

    with patch.object(c, "synthesize_chunk", partial):
        audio = c.synthesize_paragraphs(
            [{"text": "Good sentence. Missing sentence."}],
            "af_heart", "+0%", 1, logs.append,
            sentence_pause_ms=200, bitrate_kbps=128,
        )
    out["sentence_failure"] = {
        "calls": list(seen), "logs": logs,
        "returned_audio_bytes": len(audio[0]),
        "contains_good": audio[0].startswith(b"GOOD"),
    }
    seen.clear()
    suppressed = ["1945", "\u4f60\u597d\u4e16\u754c", "\u3053\u3093\u306b\u3061\u306f\u4e16\u754c", "\u092f\u0939 \u090f\u0915 \u092a\u0941\u0938\u094d\u0924\u0915 \u0939\u0948", "I."]
    with patch.object(c, "synthesize_chunk", synth):
        results = c.synthesize_paragraphs(
            [{"text": text} for text in suppressed], "af_heart", "+0%",
            1, lambda _: None, bitrate_kbps=128,
        )
    out["suppressed_paragraphs"] = {
        "input_texts": suppressed, "tts_calls": list(seen),
        "audio_lengths": [len(a) for a in results],
    }

    calls = []
    sleeps = []

    def permanent_failure(*args, **kwargs):
        calls.append(1)
        raise HTTPError("http://unused.invalid", 400, "Invalid voice", {}, None)

    c.set_tts_backend("kokoro")
    with patch.object(c, "_kokoro_once", permanent_failure), patch.object(c.time, "sleep", sleeps.append):
        result = c.synthesize_paragraphs(
            [{"text": "Hello world."}], "invalid_voice", "+0%", 1, lambda _: None
        )
    out["permanent_http_400"] = {
        "requests": len(calls), "scheduled_backoff_seconds": sum(sleeps),
        "returned_audio_lengths": [len(a) for a in result],
    }

    cancel = threading.Event()
    started = threading.Event()
    release = threading.Event()
    seen.clear()

    def blocked_first(text, *args, **kwargs):
        seen.append(len(text))
        if len(seen) == 1:
            started.set()
            release.wait(2)
        return b"X"

    with patch.object(c, "synthesize_chunk", blocked_first):
        worker = threading.Thread(target=lambda: c.synthesize_paragraphs(
            [{"text": "word " * 1000}], "af_heart", "+0%", 1,
            lambda _: None, cancel_event=cancel,
        ))
        worker.start()
        if not started.wait(2):
            raise RuntimeError("Audit worker did not start")
        cancel.set()
        worker.join(0.15)
        returned_before_release = not worker.is_alive()
        release.set()
        worker.join(3)
        if worker.is_alive():
            raise RuntimeError("Audit worker did not terminate")
    out["cancel_long_paragraph"] = {
        "returned_before_blocked_request_released": returned_before_release,
        "total_request_lengths": list(seen),
        "requests_started_after_cancel": len(seen) - 1,
    }

    try:
        import soundfile as sf
        out["soundfile_version"] = sf.__version__
        silence = {}
        for bitrate in (48, 96, 128):
            raw = c.generate_mp3_silence(240, bitrate)
            try:
                decoded, sample_rate = sf.read(BytesIO(raw))
                silence[str(bitrate)] = {
                    "samples": len(decoded), "sample_rate": sample_rate,
                    "decoded_ms": len(decoded) * 1000 / sample_rate,
                }
            except Exception as exc:
                silence[str(bitrate)] = {"error": type(exc).__name__}
        corrected = c.generate_mp3_silence(240, 96).replace(
            bytes.fromhex("fff394c0"), bytes.fromhex("fff3a4c0")
        )
        decoded, sample_rate = sf.read(BytesIO(corrected))
        silence["96_corrected_header_in_memory"] = {
            "samples": len(decoded), "sample_rate": sample_rate,
            "decoded_ms": len(decoded) * 1000 / sample_rate,
        }
        out["generated_silence"] = silence
    except ImportError:
        out["generated_silence"] = "Skipped: optional soundfile is unavailable"

    path = Path(__file__).with_name("client_pipeline_probe_results.json")
    path.write_text(json.dumps(out, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(path)
    print(json.dumps(out, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
