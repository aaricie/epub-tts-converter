# Offline codec and silence-trimming microbenchmarks

2026-10-15. Ran on the Windows host with PyAV 17.0.1 against the saved 18.970-second, mono, 24 kHz PCM sample (`sample_caption_pcm.pcm`). No TTS requests, GPU work, or implementation edits. Reproducible script: `engine_microbench.py`; raw runs: `engine_microbench.json`.

## MP3 encoding

Three runs per setting, shuffled order, excluded one-second warmup. Encoder: `libmp3lame`, fixed 48 kbps. `write_xing=0` matches the inspected server writer. Encoding time includes container setup, frame conversion/muxing and flush/close, but excludes decoding verification.

| compression_level | Median wall time | Range |
| --- | ---: | ---: |
| Unspecified default | 0.503 s | 0.387–0.509 s |
| 2 | 0.751 s | 0.670–0.799 s |
| 5 | 0.408 s | 0.202–0.520 s |
| 7 | 0.275 s | 0.251–0.310 s |
| 9 | 0.239 s | 0.124–0.275 s |

Every result was 114,236 bytes, detected as 48,000 bps / 24 kHz, and decoded to 456,768 samples. The source has 455,282 samples: the extra 1,486 samples (61.917 ms) are consistent across settings, with Xing/gapless metadata disabled. This test validates consistency of encoding settings and decoded sample counts; it does **not** establish sample-accurate timestamp alignment. All requested codec options were consumed, and each level produced a deterministic hash across repeats.

Levels 7 and 9 produced **identical files for this sample**; their measured time difference should be treated as run variability, not proof that 9 outperforms 7. The meaningful result is that these faster settings merit evaluation, while level 2 was slower than the unspecified default. The level-5 timing overlaps default substantially. These are stage timings on one sample and one host, not measured whole-book or end-to-end TTS improvements. The different encoder decisions need listening tests before adoption; no quality-transparency claim is made.

## Silence trimming

Extracted `AudioNormalizer` from the saved runtime source using Python AST. A second isolated copy replaced only the two scalar Python scan loops in `find_first_last_non_silent` (audio.py:80–90) with NumPy threshold comparisons and first/last `argmax`. All punctuation, speed, padding and all-silent behavior remained the same. Config defaults came from the saved config AST (410 ms dynamic padding, 50 ms initial padding).

| Input | Current median | Vectorized median |
| --- | ---: | ---: |
| Captured PCM | 24.574 ms | 1.597 ms |
| Captured PCM plus 5 s silence at both ends | 706.739 ms | 2.024 ms |
| 10 s entirely silent | 1,255.824 ms | 0.676 ms |
| 19 s with no edge silence | 0.036 ms | 1.394 ms |

Bounds matched for every measured input and 18 additional combinations of speed, punctuation and final-chunk status. The captured sample is already trimmed, so this is not a substitute for rerunning the engine profile on raw inference output. The vectorized version scans the full array and allocates a boolean mask; the original can finish immediately when both edges are loud, as the final row demonstrates. A blockwise vector scan from the edges could combine their strengths if that tradeoff matters.

An additional signed-integer edge case exposed a correctness bug: `abs(np.int16(-32768))` overflows, remains negative, and raises a warning. For 50,000 otherwise silent samples with full-negative peaks at indices 5,000 and 20,000, the original classified the whole array as silent and returned `(0, 50000)`; the safe vector comparison returned `(3800, 28640)`, preserving the configured padding. No `-32768` samples occur in the captured test PCM. A future optimization should use signed comparisons or widen before absolute value; applying `np.abs` directly to an int16 array would retain the same overflow defect.
