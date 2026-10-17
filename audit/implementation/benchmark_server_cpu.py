"""Isolated CPU benchmarks of stage 4 against the reviewed server snapshot."""
import ast
import json
import math
from pathlib import Path
import statistics
import sys
import time
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "kokoro_server_patch"))
from performance_patch import patch_audio, patch_combine


def extract(source, name, ns):
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == name)
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    tree = ast.fix_missing_locations(ast.Module(body=[future, node], type_ignores=[]))
    exec(compile(tree, "<server-cpu-benchmark>", "exec"), ns)
    return ns[name]


def measured(fn, repetitions=7):
    fn()
    times = []
    for _ in range(repetitions):
        start = time.perf_counter()
        result = fn()
        times.append(time.perf_counter() - start)
    return result, {"median_s": statistics.median(times), "samples_s": times}


def main():
    config_source = (ROOT / "audit/kokoro_runtime_src/core/config.py").read_text()
    wanted = {"gap_trim_ms", "dynamic_gap_trim_padding_ms", "dynamic_gap_trim_padding_char_multiplier"}
    values = {n.target.id: ast.literal_eval(n.value) for n in ast.walk(ast.parse(config_source))
              if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id in wanted}
    ns = {"np": np, "math": math, "settings": SimpleNamespace(**values)}
    source = (ROOT / "audit/kokoro_runtime_src/services/audio.py").read_text()
    before = extract(source, "AudioNormalizer", dict(ns))()
    after = extract(patch_audio(source), "AudioNormalizer", dict(ns))()
    pcm = np.frombuffer((Path(__file__).parent / "pcm_contract/english.pcm").read_bytes(), dtype="<i2")
    inputs = {"real_18s": pcm, "real_10s": pcm[:240000],
              "quiet_edges_40s": np.pad(pcm[:240000], (360000, 360000)),
              "non_silent_10s": np.full(240000, 3000, dtype=np.int16),
              "all_silence_10s": np.zeros(240000, dtype=np.int16)}
    rows = {}
    for name, samples in inputs.items():
        old_result, old_time = measured(lambda: before.find_first_last_non_silent(samples, "Example.", 1.0))
        new_result, new_time = measured(lambda: after.find_first_last_non_silent(samples, "Example.", 1.0))
        assert old_result == new_result
        rows[name] = {"bounds": old_result, "before": old_time, "after": new_time}
    source = (ROOT / "audit/kokoro_runtime_src/inference/base.py").read_text()
    old_cls = extract(source, "AudioChunk", {"np": np})
    new_cls = extract(patch_combine(source), "AudioChunk", {"np": np})
    blocks = [np.full(24000, i, dtype=np.int16) for i in range(100)]
    old_result, old_time = measured(lambda: old_cls.combine([old_cls(x, []) for x in blocks]))
    new_result, new_time = measured(lambda: new_cls.combine([new_cls(x, []) for x in blocks]))
    np.testing.assert_array_equal(old_result.audio, new_result.audio)
    rows["combine_100_one_second_chunks"] = {"samples": len(old_result.audio), "before": old_time, "after": new_time}
    target = Path(__file__).with_name("server_cpu_benchmark.json")
    target.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(json.dumps({name: {"before_ms": row["before"]["median_s"]*1000,
                             "after_ms": row["after"]["median_s"]*1000} for name, row in rows.items()}, indent=2))


if __name__ == "__main__":
    main()
