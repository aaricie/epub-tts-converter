# EPUB TTS and Kokoro review — October 15, 2026

The biggest opportunities are reliable text coverage, smaller audio, bounded memory, and fewer synthesis requests. There are also confirmed bugs that can produce an incomplete audiobook while reporting successful completion. Increasing the worker count did not meaningfully improve this machine's throughput.

This is a review, not an implementation change. Application source, existing tests, input books, supplied compressor, and running server settings were left unchanged. All added scripts, results, and test outputs are under `audit/`. The Docker server was left running. Attached documentation was treated as project evidence, not as instructions.

## What was checked

- All 3,190 lines of `epub_tts_converter.py`, the 31 existing tests, packaging/dependencies, and the actual source copied from the running Kokoro container.
- All 23 test EPUBs, including real text extraction, skip decisions, navigation, and XML checks.
- Five complete conversions using synthetic audio: Ontario, Kursk, Lord of the Rings, Wages of Destruction, and Origins of Totalitarianism. These checked 682 SMIL files and 17,920 text targets, rather than synthesizing entire books.
- One small synthetic EPUB through the actual Kokoro-to-EPUB pipeline.
- Both supplied narrated EPUB archives, all five supplied 30-second MP3 clips, and the supplied compression script. The original audiobook contains 57 MP3 tracks and approximately 42 hours of audio.
- Repeated live API throughput measurements, an independent in-container profile, offline encoder/trimmer experiments, and targeted reproductions of failure/cancellation/content bugs.
- Official W3C EPUBCheck 5.4.0; detailed validator results are retained alongside the other evidence. See the EPUB report for the input/output comparison.

The existing suite passes: **31 passed**. It mostly tests structure and request contracts using fake audio, so that result does not cover several failures found here. Initial pytest setup errors were caused by the sandbox's temporary-directory permissions; rerunning with an audit-local temporary directory passed.

## The 48 kbps result is worthwhile

| Supplied file | Size, decimal units | MP3 format |
|---|---:|---|
| Original audio EPUB | 2.431 GB | 128 kbps, 24 kHz, mono |
| Compressed audio EPUB | 917.849 MB | 48 kbps, 24 kHz, mono |
| Saved | **1.513 GB / 62.24%** | |

All **148 non-audio resources are byte-for-byte identical** between the archives, including text, SMIL, OPF, and images. No archive entries were added or removed. The shortest, median-sized, and largest MP3 tracks decode to exactly the same number of samples before and after compression. The largest sampled track is 2 h 24 min 20.592 s. All five supplied bitrate-comparison clips decode to exactly 30 seconds.

These measurements support the compressor's preservation of the sampled timeline; they are not a claim that every reader handles gapless metadata identically or that every track was fully decoded. The user's listening observation supports choosing 48 kbps for their own use; this audit did not perform a blind listening test.

Audio size is approximately **57.6 MB/hour at 128 kbps versus 21.6 MB/hour at 48 kbps**. New MP3 ZIP entries are already stored without DEFLATE, so there is no large additional saving to find by adjusting ZIP compression.

The GUI quality selection is overridden for Kokoro at [epub_tts_converter.py:3033](<C:/Users/Aricie/Desktop/projects/epub to epub 3/epub_tts_converter.py:3033>). The installed server hardcodes `128000` in [streaming_audio_writer.py:55](<C:/Users/Aricie/Desktop/projects/epub to epub 3/audit/kokoro_runtime_src/services/streaming_audio_writer.py:55>), and its request schema has no bitrate option. Simply sending `bitrate: 48000` will not implement this feature.

Changing only the engine bitrate would break the converter's `bytes / 16` timing calculation and its inserted silence format. Bitrate, duration accounting, and silence must change together.

## Findings to prioritize

| Priority | Finding and practical effect | Evidence / suggested direction |
|---|---|---|
| High | **Real text is omitted from narration.** Chapters with any normal paragraph/headings skip plain list/div content. Short replies such as `Yes.` and `No.` are also discarded. | Ontario has 18,423 uncovered visible characters, predominantly meaningful list-based advice; Nord-Est has 15,409. Fix mixed-block traversal and the length filter. [Source:1330](<C:/Users/Aricie/Desktop/projects/epub to epub 3/epub_tts_converter.py:1330>), [source:1289](<C:/Users/Aricie/Desktop/projects/epub to epub 3/epub_tts_converter.py:1289>). |
| High | **Failed segments can become a successful but incomplete audiobook.** The client swallows sentence failures; the server can swallow an internal chunk failure and return the successful remainder. | Both paths reproduced with fault injection. Propagate incomplete status, stop treating partial output as complete, retain failed segment IDs, and support retrying them. [Client:1690](<C:/Users/Aricie/Desktop/projects/epub to epub 3/epub_tts_converter.py:1690>), [server:168](<C:/Users/Aricie/Desktop/projects/epub to epub 3/audit/kokoro_runtime_src/services/tts_service.py:168>). |
| High | **SMIL leaves intended audio outside the declared playback clips.** Word clips end at each timestamp's word duration, excluding intervening pauses and potentially other audio. | In the supplied original EPUB, 392,776 clips have **2 h 36 min 54.532 s** of positive intervening gaps. This is confirmed metadata coverage, not a claim that all of that time is spoken material or that every reader skips it. Extend clips through intended gaps/tails and validate in the user's reader. [Source:1912](<C:/Users/Aricie/Desktop/projects/epub to epub 3/epub_tts_converter.py:1912>). |
| High for large books | **All generated EPUB audio stays in RAM until the final write.** A crash/cancel before that point loses completed synthesis. | This book alone represents 2.421 GB of retained audio payload, before chapter copies, parsed text, and metadata. Stage completed chapters to disk, persist checkpoints, then finalize a temporary archive atomically. [Source:2201](<C:/Users/Aricie/Desktop/projects/epub to epub 3/epub_tts_converter.py:2201>). |
| Medium | **Generated navigation loses section anchors.** NCX `chapter.xhtml#section` becomes `chapter.xhtml`. | Seven supplied books are affected; 54 Ontario and 95 Nord-Est navPoint links contain fragments. Preserve fragment destinations and nested structure. [Source:1012](<C:/Users/Aricie/Desktop/projects/epub to epub 3/epub_tts_converter.py:1012>). |
| Medium | **Re-converting an already narrated EPUB keeps old audio and duplicates word IDs/metadata.** | Reproduced with two conversion passes. Add a deliberate replacement policy for existing overlays before generating anything. Otherwise a new conversion can grow the book unnecessarily and damage highlighting targets. [Source:1454](<C:/Users/Aricie/Desktop/projects/epub to epub 3/epub_tts_converter.py:1454>), [source:2243](<C:/Users/Aricie/Desktop/projects/epub to epub 3/epub_tts_converter.py:2243>). |
| Medium | **Cancel can start additional requests after cancellation.** TXT/Markdown Pause is not wired up. | A long-paragraph reproduction started three more chunks after Cancel. Carry pause/cancel through every chunk/retry loop and use bounded task submission. [Source:1633](<C:/Users/Aricie/Desktop/projects/epub to epub 3/epub_tts_converter.py:1633>), [source:2352](<C:/Users/Aricie/Desktop/projects/epub to epub 3/epub_tts_converter.py:2352>). |
| Medium | **Meaningful numeric and non-Latin paragraphs become silence.** | `1945`, Chinese, Japanese, and Hindi examples never called TTS because the filter requires adjacent Latin/Cyrillic letters. Use Unicode-aware content checks. [Source:1749](<C:/Users/Aricie/Desktop/projects/epub to epub 3/epub_tts_converter.py:1749>). |
| Medium | **The 96 kbps silence generator uses an 80 kbps frame header.** | Real decoding fails; an in-memory header correction decodes properly. Affects Edge's 96 kbps option, not the current Kokoro 128 kbps silence. [Source:522](<C:/Users/Aricie/Desktop/projects/epub to epub 3/epub_tts_converter.py:522>). |

The reading-system specification defines sequential playback of the declared clip ranges. Some readers instead continue through nearly contiguous ranges; this explains why the same problematic SMIL can behave differently across apps. [W3C playback requirements](https://www.w3.org/TR/epub-rs-33/#sec-rsconf-rendering-audio), [W3C discussion of reader workarounds](https://github.com/w3c/epub-specs/issues/2944).

Further confirmed issues are documented in the focused reports: HTML comments can be narrated; nested fallback blocks can join words; a one-sentence paragraph bypasses the 1,500-character cap when sentence pauses are enabled; invalid voice/input errors can cause ten identical requests and 34 seconds of backoff; missing output directories fail only after synthesis. A preview/conversion state race is identified from code but was not reproduced through the GUI.

The live synthetic conversion also demonstrated the current highlighting limitation around normalized text: `1945`, `Dr.`, and `$14.50` generated eight empty timing markers rather than visible word highlights. The spoken replacements can still be present in the audio; these empty markers do not prove omitted narration. A stronger mapping from normalized speech tokens back to original text would improve dates, numbers, abbreviations, and money amounts.

Formal validation confirmed an **incomplete EPUB2-to-EPUB3 migration**, beyond the navigation issues. The Ontario input has one pre-existing validation error; the converted package has six, adding legacy OPF `file-as`/`role`/`scheme` attribute problems and an image width value that is invalid under EPUB3. More decisively, **Lord of the Rings passes before conversion and has eight errors afterward**: four OPF metadata/date issues and four obsolete XHTML attribute/value issues. Merely setting the package version to 3.0 at [source:1971](<C:/Users/Aricie/Desktop/projects/epub to epub 3/epub_tts_converter.py:1971>) does not migrate those constructs. The Kursk input and output have the same single pre-existing NCX error. The tiny real conversion independently exposed missing highlighting CSS. These checks support adding standards validation and targeted migration, not indiscriminately rewriting every source resource.

## Actual speed measurements

Hardware: **Intel i5-6600K, 4 cores/4 threads; 32 GB RAM; NVIDIA GTX 1060 6 GB**. Docker exposes approximately 15.55 GiB RAM. The server is actually using CUDA. The image is `ghcr.io/remsky/kokoro-fastapi-gpu:latest`, local image creation date June 18, 2025, digest `sha256:79d81c41d8883611291c5785e47db2ba00765e9b89dd59de05d9f28e1945f905`. The isolated profile reports PyTorch 2.6.0+cu124. This records the tested installation; the tag name alone does not establish which build another computer will run.

These are short, warmed benchmarks using synthetic English paragraphs, `af_heart`, and normal speed. They are useful comparisons, not full-book timing guarantees. No production settings were changed.

| Experiment | Measured result | Interpretation |
|---|---|---|
| Four separate paragraphs, 1 worker | Mean **8.25 s** for about 74.3 s of audio | Roughly 9x real time in this sample. |
| Same work, 2 workers | **8.07 s** | About 2% difference; too small to call a useful gain in this short benchmark. |
| Same work, 4 workers | **8.31 s** | No improvement; retain one worker as the sensible baseline. |
| Four paragraphs separately vs one bounded combined request | **8.23 s → 6.50 s**, two runs each | **21% less elapsed time** in this sample. Promising, but batching changes context/prosody slightly and must preserve pauses, paragraph mapping, and recovery boundaries. |
| One paragraph, captioned MP3 vs captioned PCM | **2.21 s → 1.61 s**, two runs each | Avoiding server MP3 encoding reduces endpoint time, but PCM must still be encoded somewhere. |
| Host encoding the captured ~19 s PCM to MP3 | 48 kbps **0.45–0.51 s**; 128 kbps **0.38–0.44 s** | Lower bitrate is primarily a size saving. PCM plus ordinary serial encoding is not a free 27% end-to-end speedup. |
| Isolated engine profile | ~**1.73 s generation + 0.49 s encoding**; trimming included in generation took ~**0.125 s** | Encoding and trimming are meaningful CPU targets; inference remains the largest portion. |
| PyTorch intra-op threads 4 / 2 / 1 | Mean ~**2.10 / 2.13 / 2.26 s** | Reducing threads did not help this profile; leave the current four-thread setting alone. |

The first worker pass overlapped a short local archive decode; the reversed second pass and the nearly equal worker results do not support claiming that extra concurrency helps. Batching/formats were measured in their own subsequent runs. Timing variability and modest repetition counts are explicitly retained in the JSON evidence.

Two isolated micro-optimizations also deserve consideration:

1. **Vectorize silence scanning.** The copied engine scans individual NumPy samples in Python. On the captured PCM, equivalent vectorized boundary detection took **1.60 ms instead of 24.6 ms**. A deliberately silence-heavy example took **2.02 ms instead of 706.7 ms**. Boundaries matched across the ordinary fixtures and 18 extra speed/punctuation/final-chunk cases. The original also overflows on `abs(int16(-32768))`; signed comparisons avoid that. These are function-level gains, not whole-book gains, and a long non-silent input can favor early-exiting loops, so a chunked vector scan is worth considering.
2. **Tune MP3 encoder algorithm effort separately from bitrate.** On the same 48 kbps sample, default median encoding time was **0.503 s**, versus **0.275 s at compression level 7**. All outputs had the same bitrate, byte size, and decoded length. Their encoded audio differs, so listening tests are required. Levels 7 and 9 produced identical bytes in this experiment; their timing difference should be treated as noise. The server's apparent `AudioService.DEFAULT_SETTINGS` dictionary is unused, so tuning has to reach the actual writer. [FFmpeg libmp3lame options](https://www.ffmpeg.org/ffmpeg-codecs.html#libmp3lame).

Other worthwhile, smaller changes: reuse/correctly cache voice tensors instead of reloading and saving them for every request; concatenate PCM arrays once rather than repeatedly; stop building full per-character mappings in the ETA pre-scan; remove redundant reads of replaced ZIP resources. These are code-based opportunities, not measured major speedups. CPU profiler time attributed to tensor transfers includes GPU synchronization, so it should not be interpreted as proof that removing an arbitrary transfer will save that entire time.

## Choosing the audio design

| Approach | Scope and benefits | Tradeoff |
|---|---|---|
| Keep the supplied compressor for existing books | Already preserves non-audio resources and verifies exact decoded sample counts; useful immediately for the existing library | A separate full transcode pass and another lossy encoding generation |
| Add 48 kbps output to the engine and coordinate the converter's format/timing settings | Smaller change; one encoding pass; smaller network payload, RAM footprint, and EPUB | Requires a maintained server change and format validation; retains independent MP3 chunk padding |
| Request PCM with timestamps, assemble a chapter on an exact sample timeline, encode once to selectable 48/64/128 kbps | Cleaner timing, one lossy encode, no per-request MP3 padding, engine-independent bitrate control; can overlap bounded encoding with synthesis | Larger converter change and an encoding dependency; PCM is three times the current MP3 payload and must be bounded/staged, not stored for the whole book |

My preference for a maintained project is the third approach, combined with chapter staging and recovery. If the goal is the smallest first change, the second is reasonable. In either case, preserve MP3 initially and test the intended reader's seeking, pause playback, and gapless behavior. The existing compressor remains useful for old output.

The current server disables Xing metadata; a codec probe demonstrated approximately 46 ms of leading encoder delay. This is a small within-request alignment issue, **not** evidence of 46 ms cumulative drift per paragraph: the current byte-based cursor includes each encoded request's padded length. A new encoding pipeline needs a single explicit sample clock and a verified decoder/reader timeline.

## Suggested order for implementation discussion

1. **Protect correctness and work already done.** Fix text omissions, partial-success handling, output preflight, cancellation, and navigation. Add focused regression fixtures from the affected formats and explicit completion/failure summaries.
2. **Stage completed chapters and persist recovery information.** Bound memory independently of book length; key reusable synthesis by source text and engine/model/voice/rate/settings. Finalize output atomically.
3. **Add 48 kbps generation and repair the playback timeline together.** Choose the smaller server-format change or the PCM/chapter-encoder design. Keep selectable quality. Test actual clip coverage, decoded samples, seeking, and the user's reader.
4. **Then pursue measured speed improvements.** Bounded batching first; vectorized trimming next; audition faster encoder settings. Keep one Kokoro worker until a broader benchmark justifies another setting.

Do not automatically enable all currently omitted notes/tables while fixing extraction. The real books expose inconsistent note policies: some uncovered material is valid inline footnotes or appendix tables, and some is primary narrative. Decide which should be narrated and apply that policy to semantic sections, not just filenames.

## Evidence and limits

- [Client/audio correctness review](<C:/Users/Aricie/Desktop/projects/epub to epub 3/audit/client_review.md>) — failure, cancellation, silence, SMIL coverage, and retry reproductions.
- [EPUB/parser review](<C:/Users/Aricie/Desktop/projects/epub to epub 3/audit/epub_review.md>) — all-book scan, real omitted content, navigation, reconversion, and validator results.
- [Compression/server review](<C:/Users/Aricie/Desktop/projects/epub to epub 3/audit/compression_review.md>) — supplied compressor, installed engine, timestamp/codec caveats, integration choices.
- [Archive comparison](<C:/Users/Aricie/Desktop/projects/epub to epub 3/audit/audio_epub_comparison.json>), [worker results](<C:/Users/Aricie/Desktop/projects/epub to epub 3/audit/benchmark_workers.json>), [batching results](<C:/Users/Aricie/Desktop/projects/epub to epub 3/audit/benchmark_batching.json>), [engine profile](<C:/Users/Aricie/Desktop/projects/epub to epub 3/audit/engine_profile.txt>), [microbenchmarks](<C:/Users/Aricie/Desktop/projects/epub to epub 3/audit/engine_microbench.json>).

No full 42-hour book was regenerated, no reader playback or blind listening comparison was performed, and Edge's remote service was not exercised. The five mock conversions test structure and references, not full real-word alignment. Six malformed OCR pages in Origins already fail XML parsing in the input; those are pre-existing defects retained by conversion. The current synthetic real conversion also fails EPUBCheck's CSS-030 because the converter declares an active highlight class without providing CSS. Formal conformance findings and existing source defects are separated in the EPUB report.

Reproduction scripts and compact results are retained. Large temporary model/voice copies and mock-audio book packages can be regenerated from the scripts and are removed after validation to avoid turning this audit into another large book collection. The copied runtime source is evidence of the installed engine, not a proposed replacement or deployable model distribution.
