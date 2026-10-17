# Implementation status — October 17, 2026

Follow-up to [REVIEW.md](REVIEW.md). Stages 1–3 were implemented on October 15 (Codex thread "Review Kokoro TTS performance", interrupted by a usage limit before live verification). This pass verified them against the real Kokoro server, measured stage 4, corrected what the measurements contradicted, and upgraded the server.

## Verified live (GTX 1060, `af_heart`, 48 kbps)

| Check | Result |
|---|---|
| Real Kokoro → PCM → chapter MP3 → EPUB (2-chapter fixture with lists, short replies, numbers, a long passage) | SMIL clips continuous; decoded MP3 sample count equals the SMIL timeline exactly; EPUBCheck **0 errors, 0 warnings** |
| Cancel after 4 requests, then re-run | Nothing published on cancel; resume made exactly the 7 remaining requests (no re-synthesis); complete book |
| Packaging of 5 real books (mock audio) | 0 XML errors, duplicate IDs or missing targets, including *Origins of Totalitarianism*, which the October 15 code refused |
| EPUBCheck input → output | Lord of the Rings 0 → 0 (the original converter introduced 8 errors); Ontario 1 → 1 and Kursk 1 → 1 (pre-existing source errors only) |
| Test suite | 183 passed |

## Speed (stage 4) — what the measurements showed

End-to-end conversions of real-book excerpts, fresh checkpoint cache per run, variants interleaved, two runs each ([bench_pipeline.py](implementation/bench_pipeline.py), results in `implementation/bench/`).

| Excerpt | Serial | First batching design (was on by default) | Prefetch | Prefetch + revised batching |
|---|---:|---:|---:|---:|
| Fiction, 30 paragraphs, 9m28s audio — server 0.2.4 | 50.5 s | **72.7 s** | **40.4 s** | 50.3 s |
| History, 10 paragraphs, 7m07s audio — server 0.2.4 | 33.8 s | **54.5 s** | **29.0 s** | 29.4 s |
| Fiction — server 0.9.0 | 46.3 s | — | 38.2 s | 37.0 s |
| History — server 0.9.0 | 33.4 s | — | 28.8 s | 29.4 s |

- **Prefetch** (one request queued beyond the running worker) hides client MP3 encoding and word mapping behind GPU synthesis: 14–20% faster. Wall time is now the server's synthesis time plus about 2 s, so faster encoder settings cannot help; default encoder quality is kept and no audition is needed.
- **The first batching design made real books 44–61% slower.** Kokoro rewrites numbers and abbreviations ("1945" → "nineteen forty-five"), strict word matching rejected the batch, and the combined request was discarded. 81–98% of batches in the history test books contain such words.
- **Revised batching** aligns captions with the source (rewrites inside a paragraph are allowed; only boundary words must match), keeps likely-rewritten openings out of batch boundaries, and cuts in the quietest part of the pause. It no longer wastes requests on history text, but even then it is at most ~3% faster, because Kokoro's cost is per character. It is now **off by default** (GUI checkbox "experimental").
- Server-side silence-scan vectorization (the `kokoro_server_patch` "performance" option) is worth about 1–2% of a typical request; not worth maintaining a custom image for.

## Kokoro server: 0.2.4 → 0.9.0

| | 0.2.4 (June 2025) | 0.9.0 (Sept 2026) |
|---|---|---|
| Runs on the GTX 1060 | yes | yes (default image is CUDA 12.6 / PyTorch 2.8; the `cu128` variant is for RTX 50) |
| Words with a highlight, history excerpt (1,049 words) | ~932 | ~1,068 (essentially all) |
| Cause | one token without timing aborts timestamp extraction for the rest of its chunk (logged as `Failed to process timestamps for chunk`, frequent in the old container's log) | such tokens are skipped individually |
| French word timestamps | no | yes |
| Audio | same model; durations within 0.1% | |

The container was replaced in place: same name (`kokoro-fastapi`, which the LLM proxy manages), same port, no restart policy, image `ghcr.io/remsky/kokoro-fastapi-gpu:v0.9.0`. The old container is kept, stopped, as `kokoro-fastapi-v0.2.4`. Rollback:

```powershell
docker stop kokoro-fastapi; docker rename kokoro-fastapi kokoro-fastapi-v0.9.0; docker rename kokoro-fastapi-v0.2.4 kokoro-fastapi; docker start kokoro-fastapi
```

0.9.0 still returns HTTP 200 with partial audio if an internal chunk fails. The converter now detects that itself (see below), so the `kokoro_server_patch` derivative (0.2.4-only) is optional.

## Other changes in this pass

- **Engine omission check (client side, any server version):** word timestamps are aligned with the text sent; a run of 8+ missing words is an omission only if the timeline leaves no room to have spoken it. Real omissions are retried, then follow the stop/continue setting. Lost timestamps with intact audio (the 0.2.4 bug above, seen live) are correctly *not* flagged.
- **Checkpoint identity on stock servers** uses the server's reported version, so an upgrade does not reuse old audio.
- **Voice menu** reads 0.9.0's graded voice listing (it fell back to 7 hardcoded voices before) and shows grades.
- **OCR markup repair:** attribute names XML cannot represent (words after an unescaped `<` in scanned text) are dropped before validation; visible and narrated text are unchanged.
- Batch progress is logged once per chapter instead of once per batch.

## Left for the user

- Play a converted book in **BookFusion on iOS** (seeking, highlighting, pauses). `implementation/acceptance/bookfusion_check_48kbps.epub` is a small test book made for this.
- Optional cleanup once satisfied with 0.9.0: the stopped `kokoro-fastapi-v0.2.4` container, its image, and the two `epub-kokoro:*` derivative images from October 15.
