# Kokoro client and synthesis pipeline audit

Read-only implementation review, 2026-10-15. No implementation or existing tests were changed. All reproduction calls below replace synthesis with mocks; no TTS server or external service was called. MP3 silence was decoded locally with soundfile 0.13.1. See `client_pipeline_probes.py` and `client_pipeline_probe_results.json` for repeatable evidence.

Line references are to `epub_tts_converter.py` as reviewed.

## Confirmed correctness and control issues

### 0. Word clips leave substantial audio gaps out of the media overlay (P1 interoperability)

At 1912–1913, each SMIL audio clip covers only a span's offset through offset + duration. Inter-word gaps and inserted sentence/paragraph silence are not assigned to either neighboring clip. The paragraph cursor does advance by the full encoded paragraph size (1939), so the next paragraph jumps past those unreferenced gaps.

Measured by parsing all 57 SMIL files in the supplied original EPUB, with no audio decoding: 392,776 clips; summed first-to-last chapter timelines of 151,296.233 seconds; sum of individual clip durations 141,888.413 seconds. Positive gaps between successive clips total **9,414.532 seconds (2 h 36 min 54.532 s)**; overlaps total 6.712 seconds. There are 30,771 gaps of at least 100 ms, 3,344 of at least 300 ms, and a largest gap of 21.205 seconds. These numbers exclude any gap before a chapter's first clip and after its last clip. They do not establish that every gap is silence: alignment omissions can also leave spoken material unreferenced. Results and a repeatable metadata-only script are in `client_smil_gap_measurements.json` and `client_smil_gap_probe.py`.

The [W3C EPUB reading-system specification](https://www.w3.org/TR/epub-rs-33/#sec-rsconf-rendering-audio) defines sequential children and playback from each clip's beginning through its end. Thus a reader following the declared clips does not automatically play the gap. Actual audible behavior depends on the reader: continuous-play implementations can preserve nearly contiguous audio despite these gaps. The standards group's [playback-continuation discussion](https://github.com/w3c/epub-specs/issues/2944) explicitly describes that workaround and its tension with the specification. No target-reader playback test was performed here.

Suggested direction: make playback clips cover the intended continuous narration, for example extending each word clip to the next start and preserving paragraph/chapter tails. Keep highlight precision separate from whether silence/audio is included in playback. Also verify metadata durations against the sum of the actual declared clips. This issue is distinct from MP3 bitrate and encoder delay; recompressing audio does not repair missing SMIL coverage.

### 1. Failed sentences can disappear while conversion reports success (P1)

At lines 1690–1693, `_synthesize_with_sentence_pauses` catches a failed sentence, logs a warning and continues. The function then returns partial audio as a successful paragraph. Consequently `synthesize_paragraphs` never adds this paragraph to its retry list (1783–1793), and completion/progress accounting treats it as finished. A paragraph with every sentence failing can return an empty audio result without a paragraph failure. Outside sentence-pause mode, exhausted paragraph retries also eventually return missing audio (1870–1874), and the converter still logs `Done!` (2258 / 2348).

Reproduction: a mocked two-sentence paragraph, with the second sentence failing, produced audio containing only the first sentence and logs `WARNING: sentence 2/2 failed` followed by `1/1 paragraphs synthesized`. There was no paragraph retry. This is particularly costly for long books because discovering omissions later means regenerating work.

Suggested direction: return explicit success/failure metadata; distinguish completed and incomplete output; retain a list of failed segment IDs and support retry/resume. Sentence failure should not be swallowed as paragraph success. The existing warning is useful, but it is not a reliable completeness result.

### 2. Cancellation continues generating additional requests (P2)

Cancellation is checked at paragraph entry (1744) and when a future completes (1780), but not between pieces in `_synthesize_long` (1633–1650), between sentences (1680), or during retry/backoff (613–629). `as_completed` itself does not wake for cancellation. Furthermore, `pool.shutdown(wait=False, cancel_futures=True)` inside a `with ThreadPoolExecutor(...)` does not prevent the context manager from waiting for running workers when it exits (1773–1782; same pattern 2399–2409).

Reproduction: a 5,000-character mocked paragraph split into 1,499 / 1,499 / 1,499 / 499-character requests. Cancellation was set while the first request was blocked. The conversion did not return, and after releasing the first request all **three remaining requests were still started**.

Kokoro calls have a 120-second socket timeout (783) and up to five attempts (613); the existing retry sleeps total 16 seconds per `synthesize_chunk`. This is not a guaranteed maximum elapsed time, since read timeouts are not whole-request deadlines. Pausing also acts only at paragraph boundaries. Text/Markdown conversion does not accept or inspect a pause event at all (2352–2423), despite the common Pause button.

Suggested direction: thread a cancellation token through every split/retry loop; use interruptible backoff; bounded scheduling and explicit executor lifetime; check pause/cancel between requests. An already running server computation requires separate cancellation support, but the client can immediately stop starting subsequent requests.

### 3. Sentence-pause mode bypasses the 1,500-character cap for one long sentence (P2)

The early return at 1665–1670 sends a paragraph directly to `synthesize_chunk` when the sentence splitter returns one sentence. This occurs before the length guard at 1682, even though callers select this function before their own `_synthesize_long` branch (1755–1763).

Reproduction: a single 3,000-character sentence was sent as one 3,000-character request with sentence pause enabled. This contradicts the stated memory/time-limit protection at 338–343. Normal paragraph mode correctly goes through the long-text splitter.

Suggested direction: route every synthesis through the same bounded-request helper, including the single-sentence fast path and sentence retry fallback at 1830.

### 4. The paragraph filter drops numbers and supported non-Latin languages (P2)

The check at 1749 requires at least two consecutive Latin/extended-Latin/Cyrillic characters, otherwise substituting section silence. The retry path repeats this at 1827. This suppresses meaningful chapter headings/dates such as `1945`, short text such as `I.`, and complete Chinese, Japanese, Hindi, Greek or Arabic paragraphs. Kokoro voice discovery explicitly recognizes Chinese, Japanese and Hindi (815–817).

Reproduction: `1945`, Chinese `你好世界`, Japanese `こんにちは世界`, Hindi `यह एक पुस्तक है`, and `I.` made **zero synthesis calls** and each returned 13,440 bytes of 128 kbps silence (840 ms after frame rounding).

Suggested direction: distinguish punctuation-only separators from Unicode letters/numbers, using Unicode-aware tests; keep section-break detection separate from deciding whether a paragraph is speakable.

### 5. Generated 96 kbps silence is malformed (P2; Edge quality mode)

At 522 the header is `ff f3 94 c0`, whose MPEG-2 Layer III bitrate index identifies 80 kbps. The frame body is nevertheless created for 96 kbps (526), so frame sizes and header disagree. The corresponding 96 kbps header is `ff f3 a4 c0`.

Local decoder verification: 240 ms of 48 and 128 kbps generated silence each decoded to 5,760 samples at 24 kHz. The 96 kbps version failed with `LibsndfileError` and MPEG-header/resynchronization diagnostics. Replacing only the header in memory decoded to exactly 5,760 samples / 240 ms. No source edit was made.

Impact: the “Higher quality (96 kbps)” option can inject invalid frames at paragraph/sentence/section pauses. The present 128 kbps Kokoro path is not affected by this specific header bug.

## Confirmed resource and throughput concerns

### 6. EPUB mode retains all audiobook audio in RAM and saves only at the end (high impact for large books)

`new_files` starts at 2117; every completed chapter is joined into a new bytes object and retained there (2201); the EPUB is only opened for writing after all chapters have been synthesized (2236–2256). During chapter assembly, `synth_results` retains original audio, `audio_chunks` may contain copied audio with pauses appended (2172–2178), and the join adds another chapter-sized allocation. Long/sentence helpers also repeatedly copy immutable bytes with `+=` (1649, 1709–1714, 1857).

This architecture is confirmed by code; no large-memory stress test was run. Audio alone costs 57.6 MB per hour at 128 kbps versus 21.6 MB per hour at 48 kbps, before Python/XML/intermediate allocations. On a memory-constrained system, storing a long book in memory can cause paging or allocation failure. Cancellation/crash before final ZIP creation loses all completed EPUB audio work; there is no checkpoint cache in this flow.

Suggested direction: persist completed chapter audio and timestamp metadata to a workspace/cache directory; stream them into a temporary output archive; finalize atomically. A cache keyed by source text, engine/model/voice/speed and encoding settings could support crash recovery and avoid repeating unchanged synthesis. Prefer byte lists plus join or buffered files where repeated `+=` could become large. Disk buffering and resumability are the substantial changes; small Python concatenation optimizations are secondary.

### 7. Permanent failures are retried as if transient (P2 efficiency)

The generic catch in `synthesize_chunk` (620–629) retries every exception, including invalid input/voice HTTP 400 responses. `synthesize_paragraphs` then runs the same paragraph again (1798–1814), with another complete five-attempt cycle. Only after that does it fall back or skip.

Reproduction: an immediate HTTP 400 invalid-voice error caused **10 identical requests and 34 seconds of scheduled backoff** for a one-sentence paragraph, with empty audio returned afterward. Sleeps were mocked, so the test did not actually wait 34 seconds.

Suggested direction: classify errors; fail fast on invalid configuration/authentication/input, retry transient errors with appropriate limits, and stop a run early when a shared server/voice problem affects the whole book. Avoid broad paragraph retries redoing already successful pieces.

### 8. Kokoro ignores the selected quality setting and assumes a fixed output format

The GUI exposes 48/96 kbps quality (2552–2559), but `_run` forces 128 kbps for Kokoro (3033–3039). `_kokoro_once` sends `response_format: mp3` but no bitrate selection (767–774). `bytes_to_ms` is simple bytes/rate (870–871), with hardcoded 16 bytes/ms for Kokoro (335, 376–377). Silence generation and split/SMIL offset arithmetic depend on matching this fixed format.

This explains why choosing “Compact (48 kbps)” currently does not make Kokoro output compact. Changing only the server encoder to 48 kbps would make client timing incorrect until the provider audio format and silence parameters are updated together. Changing only the constant would not compress audio. Parse or validate returned MP3 properties, or carry explicit sample durations from a stable encoding pipeline; a provider configuration object would be clearer than process-global provider state plus independent bitrate parameters.

The first 8 KiB of the supplied original `OEBPS/Audio/ch002.mp3` begins directly with MPEG frames and contains no Info/Xing header. The compressed copy begins with ID3 and Info at offset 57. Therefore a retained leading Info/Xing-frame bug was **not established for the current original Kokoro output**; MP3 encoder-delay/timeline behavior still merits separate examination when changing the encoding flow.

## Optimization opportunities requiring measurement

- Sentence pauses make one request per sentence instead of one request per bounded paragraph (1665–1689). This increases HTTP, base64/JSON and encoding work and can reduce cross-sentence prosody. Default sentence pause is zero, so this is an optional-mode cost rather than a default regression.
- EPUB synthesis makes at least one request per non-separator paragraph. Batching short paragraphs could reduce per-request overhead, but must preserve boundaries, intentional pauses and failure recovery. Benchmark on the actual engine before choosing a larger request size.
- Kokoro defaults to one worker (2705–2717), which is sensible for a serialized GPU engine; do not assume increasing workers improves throughput. Extra workers can instead queue requests until client timeouts. Verify actual server serialization and benchmark a small representative set.
- `urlopen` is invoked independently per request and does not provide an explicit reusable client/session. Connection reuse may trim overhead but is unlikely to dominate local inference; measure before treating it as a major gain.

## Test coverage limits

The existing Kokoro tests validate dispatch, payloads, timestamp shape and leading ID3 removal using fake `MP3DATA` / `RAWMP3` bytes (tests lines 414–478). The 128 kbps silence test checks frame prefix and byte length, not decoding. There are no direct regression tests for cancellation, sentence-level failure, the long-sentence pause path, permanent retry classification, or Unicode/numeric paragraph handling. The attached offline probe script covers these paths without touching the real engine and can inform targeted regression tests if fixes are approved.
