"""Small offline CPU-only codec and trimmer audit; modifies no runtime source."""
import ast
import copy
import hashlib
import io
import json
import math
from pathlib import Path
import random
import statistics
import time
from types import SimpleNamespace
import warnings

import av
import numpy as np

ROOT = Path(__file__).resolve().parent


def encode(samples, level):
    buffer = io.BytesIO()
    options = {} if level is None else {"compression_level": str(level)}
    start = time.perf_counter()
    with av.open(buffer, mode="w", format="mp3", options={"write_xing": "0"}) as container:
        stream = container.add_stream("libmp3lame", rate=24000, layout="mono", options=options)
        stream.bit_rate = 48000
        for offset in range(0, len(samples), 24000):
            frame = av.AudioFrame.from_ndarray(samples[offset:offset + 24000].reshape(1, -1), format="s16", layout="mono")
            frame.sample_rate = 24000
            frame.pts = offset
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
        unconsumed_options = dict(stream.codec_context.options)
    wall = time.perf_counter() - start
    data = buffer.getvalue()
    with av.open(io.BytesIO(data), format="mp3") as container:
        stream = container.streams.audio[0]
        bitrate = stream.bit_rate
        rate = stream.sample_rate
        decoded = sum(frame.samples for frame in container.decode(audio=0))
    return {
        "level": "default" if level is None else level,
        "wall_s": wall,
        "bytes": len(data),
        "decoded_samples": decoded,
        "decoded_sample_rate": rate,
        "detected_bitrate": bitrate,
        "unconsumed_codec_options": unconsumed_options,
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def extract_normalizers():
    source = (ROOT / "kokoro_runtime_src/services/audio.py").read_text(encoding="utf-8")
    original = next(node for node in ast.parse(source).body if isinstance(node, ast.ClassDef) and node.name == "AudioNormalizer")
    vector = copy.deepcopy(original)
    vector.name = "VectorizedAudioNormalizer"
    method = next(node for node in vector.body if isinstance(node, ast.FunctionDef) and node.name == "find_first_last_non_silent")
    scan = ast.parse("""
mask = (audio_data > amplitude_threshold) | (audio_data < -amplitude_threshold)
if np.any(mask):
    non_silent_index_start = int(np.argmax(mask))
    non_silent_index_end = len(audio_data) - 1 - int(np.argmax(mask[::-1]))
else:
    non_silent_index_start, non_silent_index_end = None, None
""").body
    method.body = [node for node in method.body if not 80 <= node.lineno <= 90]
    insertion = next(i for i, node in enumerate(method.body) if node.lineno > 90)
    method.body[insertion:insertion] = scan
    config = ast.parse((ROOT / "kokoro_runtime_src/core/config.py").read_text(encoding="utf-8"))
    wanted = {"gap_trim_ms", "dynamic_gap_trim_padding_ms", "dynamic_gap_trim_padding_char_multiplier"}
    values = {
        node.target.id: ast.literal_eval(node.value)
        for node in ast.walk(config)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id in wanted
    }
    namespace = {"np": np, "math": math, "settings": SimpleNamespace(**values)}
    for node in (original, vector):
        module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
        exec(compile(module, "<isolated-normalizer-audit>", "exec"), namespace)
    return namespace["AudioNormalizer"](), namespace["VectorizedAudioNormalizer"](), values


def timed_trim(normalizer, data, text="Sentence.", speed=1.0, last=False):
    start = time.perf_counter()
    bounds = normalizer.find_first_last_non_silent(data, text, speed, is_last_chunk=last)
    return bounds, time.perf_counter() - start


def main():
    samples = np.frombuffer((ROOT / "sample_caption_pcm.pcm").read_bytes(), dtype="<i2")
    result = {"pyav_version": av.__version__, "numpy_version": np.__version__,
              "source_samples": len(samples), "source_seconds": len(samples) / 24000,
              "codec": "libmp3lame", "bitrate": 48000, "write_xing": False}
    levels = [None, 2, 5, 7, 9]
    encode(samples[:24000], None)  # Excluded warmup.
    encodes = []
    for repeat in range(3):
        order = list(levels)
        random.Random(repeat + 312).shuffle(order)
        for level in order:
            row = encode(samples, level)
            row["repeat"] = repeat + 1
            encodes.append(row)
            print("encode", row["level"], round(row["wall_s"], 4), flush=True)
    result["encoding_runs"] = encodes
    encoding_summary = {}
    for level in levels:
        key = "default" if level is None else level
        runs = [row for row in encodes if row["level"] == key]
        encoding_summary[str(key)] = {
            "median_wall_s": statistics.median(row["wall_s"] for row in runs),
            "min_wall_s": min(row["wall_s"] for row in runs),
            "max_wall_s": max(row["wall_s"] for row in runs),
            "output_bytes": sorted(set(row["bytes"] for row in runs)),
            "decoded_samples": sorted(set(row["decoded_samples"] for row in runs)),
            "detected_bitrates": sorted(set(row["detected_bitrate"] for row in runs)),
        }
    result["encoding_summary"] = encoding_summary
    assert all(row["detected_bitrate"] == 48000 for row in encodes)
    assert len({row["decoded_samples"] for row in encodes}) == 1
    assert all(not row["unconsumed_codec_options"] for row in encodes)

    original, vector, config = extract_normalizers()
    result["normalizer_config_defaults"] = config
    datasets = {
        "captured_trimmed_pcm": samples,
        "captured_pcm_with_5s_silence_each_end": np.pad(samples, (120000, 120000)),
        "10s_all_silence": np.zeros(240000, dtype=np.int16),
        "19s_no_edge_silence": np.full(len(samples), 1000, dtype=np.int16),
    }
    trim_runs = []
    for name, data in datasets.items():
        for repeat in range(3):
            old_bounds, old_time = timed_trim(original, data)
            new_bounds, new_time = timed_trim(vector, data)
            assert old_bounds == new_bounds
            trim_runs.append({"dataset": name, "repeat": repeat + 1, "samples": len(data),
                              "bounds": list(old_bounds), "original_wall_s": old_time,
                              "vector_wall_s": new_time})
            print("trim", name, round(old_time, 4), round(new_time, 6), flush=True)
    result["trimming_runs"] = trim_runs
    result["trimming_summary"] = {
        name: {
            "median_original_wall_s": statistics.median(row["original_wall_s"] for row in trim_runs if row["dataset"] == name),
            "median_vector_wall_s": statistics.median(row["vector_wall_s"] for row in trim_runs if row["dataset"] == name),
        } for name in datasets
    }
    agreements = 0
    for speed in (0.5, 1.0, 1.5):
        for text in ("", "Sentence.", "Comma,"):
            for last in (False, True):
                expected, _ = timed_trim(original, samples, text, speed, last)
                actual, _ = timed_trim(vector, samples, text, speed, last)
                assert actual == expected
                agreements += 1
    result["additional_boundary_agreement_cases"] = agreements
    overflow = np.zeros(50000, dtype=np.int16)
    overflow[5000] = -32768
    overflow[20000] = -32768
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        old_bounds, _ = timed_trim(original, overflow)
    new_bounds, _ = timed_trim(vector, overflow)
    result["signed_int16_overflow_probe"] = {
        "input": "50000 zeros, with sample 5000 and 20000 set to -32768",
        "original_bounds": list(old_bounds), "vector_bounds": list(new_bounds),
        "warnings": sorted(set(str(w.message) for w in caught)),
    }
    output = ROOT / "engine_microbench.json"
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(output, flush=True)
    print(json.dumps({key: result[key] for key in ("encoding_summary", "trimming_summary", "signed_int16_overflow_probe")}, indent=2))


if __name__ == "__main__":
    main()
