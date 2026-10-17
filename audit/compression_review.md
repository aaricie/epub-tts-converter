# Audio compression and Kokoro encoding review

Reviewed on October 15, 2026. This is an audit, with no converter, engine, or supplied-file changes. The README and script in the supplied folder were treated as evidence, not instructions to execute. Live synthesis and performance benchmarks were handled separately by the main audit. This review added only reproducible probes and their results in `audit/`.

## Main conclusion

48 kbps MP3 is a sensible output option for this project, consistent with the user's listening comparison. It reduces the audio payload by approximately 62.5% relative to 128 kbps while retaining the existing codec and sample rate. The current server really does hardcode 128 kbps. However, changing only that server constant would break the converter's timing calculations: the server output format, silence generation, and duration accounting have to change together.

For existing books, the supplied compressor is substantially more careful than a simple bitrate conversion command. For newly generated books, producing the desired bitrate during the initial encoding avoids a second lossy encoding pass. A PCM-to-one-MP3-per-chapter design also provides a clean sample timeline, but it is a larger implementation change and is not an established speed improvement. The main audit's short host measurements put encoding 19 seconds of PCM to 48 kbps at approximately 0.45–0.51 seconds versus 0.38–0.44 seconds at 128 kbps; lower bitrate must not be presented as faster neural synthesis or free encoding.

## Measured supplied clips

All five MP3 clips decode to exactly 720,000 samples at 24,000 Hz, mono: exactly 30 seconds each. The reported codec bitrates match their filenames. These are decoded-sample checks, not a claim of perceptually identical sound.

| Clip bitrate | File bytes | Decoded seconds |
|---|---:|---:|
| 128 kbps | 481,248 | 30.000 |
| 64 kbps | 240,671 | 30.000 |
| 48 kbps | 180,575 | 30.000 |
| 32 kbps | 120,479 | 30.000 |
| 16 kbps | 60,383 | 30.000 |

At the nominal bitrates, one hour is 57.6 MB at 128 kbps and 21.6 MB at 48 kbps, before small container metadata overhead. A 50-hour narration is consequently about 2.88 GB versus 1.08 GB of audio. The user has already supplied the relevant subjective observation that 48 kbps sounds sufficient to them; no additional subjective listening conclusion was inferred from these measurements.

Reproduction: [compression_review_probe.py](<C:/Users/Aricie/Desktop/projects/epub to epub 3/audit/compression_review_probe.py>) and [compression_review_probe.json](<C:/Users/Aricie/Desktop/projects/epub to epub 3/audit/compression_review_probe.json>). The probes use host Python 3.12.10 and PyAV 17.0.1, with no real TTS requests.

## Confirmed findings

**1. The 128 kbps assumption is coupled to timing in several places.** The copied runtime's [streaming_audio_writer.py](<C:/Users/Aricie/Desktop/projects/epub to epub 3/audit/kokoro_runtime_src/services/streaming_audio_writer.py:55>) assigns `self.stream.bit_rate = 128000` to MP3, AAC, and Opus. Neither the runtime speech request nor captioned-speech request schema exposes a bitrate field. In the converter, [lines 331–336](<C:/Users/Aricie/Desktop/projects/epub to epub 3/epub_tts_converter.py:331>) hardcode 16 bytes/ms and 128 kbps; [the duration resolver](<C:/Users/Aricie/Desktop/projects/epub to epub 3/epub_tts_converter.py:374>) always returns that constant for Kokoro; [conversion startup](<C:/Users/Aricie/Desktop/projects/epub to epub 3/epub_tts_converter.py:3033>) overrides the selected quality. The generated silence also uses a bitrate-specific MP3 header at [line 519](<C:/Users/Aricie/Desktop/projects/epub to epub 3/epub_tts_converter.py:519>).

If the engine alone starts returning 48 kbps, 1 second of 48 kbps audio is estimated as 0.375 seconds by the existing 16-bytes/ms calculation, and inserted silence would retain the old bitrate. This is an integration hazard, not a defect in the current consistent 128 kbps setup. An implementation needs an explicit bitrate contract or actual sample/frame durations, plus matching silence. Sending an unsupported `bitrate` JSON key is not an implementation of that contract.

**2. MP3 encoding adds an unaccounted start delay to the word timestamps.** The runtime [writer line 39](<C:/Users/Aricie/Desktop/projects/epub to epub 3/audit/kokoro_runtime_src/services/streaming_audio_writer.py:39>) disables the Xing header. The captioned endpoint [lines 316–349](<C:/Users/Aricie/Desktop/projects/epub to epub 3/audit/kokoro_runtime_src/routers/development.py:316>) generates PCM with timestamps, encodes it to MP3, and returns the original PCM timestamps without an encoder-delay adjustment. The converter [lines 796–808](<C:/Users/Aricie/Desktop/projects/epub to epub 3/epub_tts_converter.py:796>) converts those timestamps to milliseconds without further correction.

An in-memory synthetic probe using the same PyAV MP3/write_xing=0 approach encoded a 24,000-sample sine wave. At both 128 and 48 kbps it decoded to 25,344 samples, and the first sample above the chosen signal threshold appeared at sample 1,106, approximately 46.08 ms. The ID3-stripped byte duration was 1.056 seconds, exactly matching the padded decoded length. This proves the encoder behavior with the host's PyAV build; it is not a direct measurement of the precise onset of every word from the live server.

The likely visible effect is a small systematic lead of highlighting over the audio inside each independently encoded request. It is **not** evidence of a 46 ms cumulative drift per paragraph: the converter advances between requests using their full encoded length, including padding. Whole-track recompression preserves these existing delays and does not repair original timestamp alignment. For a PCM/chapter encoder design, preserve the same PCM clock in both SMIL and final MP3 gapless metadata, then verify it in the user's actual reader.

**3. The server can return partial narration after an internal error.** Runtime [tts_service.py lines 168–169](<C:/Users/Aricie/Desktop/projects/epub to epub 3/audit/kokoro_runtime_src/services/tts_service.py:168>) logs processing exceptions without re-raising. The conversion branches also swallow errors at [lines 118–119](<C:/Users/Aricie/Desktop/projects/epub to epub 3/audit/kokoro_runtime_src/services/tts_service.py:118>). The generation path can therefore collect successful chunks and return them even if a later internal chunk failed. A client retry that is triggered only by an HTTP error cannot reliably recover this case.

This was reproduced without inference by compiling the real `_process_chunk` method from the copied runtime and substituting a fake backend that yields one valid 24,000-sample chunk, then raises an exception on the second chunk. The method yielded the first chunk, completed without raising to its caller, and only logged `Failed to process tokens`. The probe output records this explicitly. This is a correctness priority above small encoding optimizations: incomplete narration should fail visibly, or carry an explicit incomplete status the client refuses to publish.

**4. Long requests cause avoidable copying, but the current client cap limits the impact.** Runtime [AudioChunk.combine](<C:/Users/Aricie/Desktop/projects/epub to epub 3/audit/kokoro_runtime_src/inference/base.py:24>) concatenates the growing PCM array again for each subsequent chunk. Total copying grows quadratically with the number of internal chunks; one `np.concatenate` over the list would be linear. The nonstream caption endpoint uses this path. The converter's 1,500-character request cap bounds its usual impact, so this is not a measured dominant bottleneck and should not outrank the reliability issues or the main audit's batching results.

**5. Some apparent server encoder settings are inactive.** Runtime [AudioService.DEFAULT_SETTINGS](<C:/Users/Aricie/Desktop/projects/epub to epub 3/audit/kokoro_runtime_src/services/audio.py:122>) contains fields called `compression_level` and `bitrate_mode`, but the encoding path does not use that dictionary. Actual setup lives in `StreamingAudioWriter`. Editing this dictionary alone will not lower the bitrate or tune the encoder. FFmpeg distinguishes the target bitrate from LAME's algorithm-quality/speed setting: `compression_level` values 0–9 run from slowest/highest algorithm quality to fastest/lowest algorithm quality. That setting should be benchmarked as an independent tradeoff, not confused with file bitrate. [FFmpeg libmp3lame documentation](https://www.ffmpeg.org/ffmpeg-codecs.html#libmp3lame).

**6. Existing tests do not validate actual encoded audio.** The Kokoro request tests use placeholder bytes such as `MP3DATA` and `RAWMP3` at [test lines 414–446](<C:/Users/Aricie/Desktop/projects/epub to epub 3/tests/test_epub_tts_converter.py:414>). The 128 kbps silence test at [line 467](<C:/Users/Aricie/Desktop/projects/epub to epub 3/tests/test_epub_tts_converter.py:467>) checks the prefix and divisibility of its byte count, not a decoder's sample timeline. Those tests are useful for request dispatch but cannot catch an unexpected server bitrate, encoder delay, or audio format mismatch. Any codec/timing change needs at least a real decode/sample-count check and a short mixed speech/silence concatenation check.

## Supplied compressor assessment

The supplied [compress_epub_audio.py](<C:/Users/Aricie/Downloads/temp audio/compress_epub_audio.py>) has useful safeguards:

- [Lines 113–129](<C:/Users/Aricie/Downloads/temp audio/compress_epub_audio.py:113>) scan audio packets to measure full-track bitrate, including VBR, rather than trusting file size or ID3 tags.
- [Lines 132–196](<C:/Users/Aricie/Downloads/temp audio/compress_epub_audio.py:132>) preserve the sample rate/channel layout, check source timestamp continuity, encode whole tracks, and fully decode the result to verify exactly the original decoded sample count. The Xing/delay metadata is intentionally enabled here.
- [Lines 209–212](<C:/Users/Aricie/Downloads/temp audio/compress_epub_audio.py:209>) copy tracks already at or below the target bitrate unchanged, avoiding unnecessary repeated lossy conversion.
- [Lines 218–251](<C:/Users/Aricie/Downloads/temp audio/compress_epub_audio.py:218>) use separate worker processes and start larger tracks first. This is a suitable coarse-grained parallel unit for CPU MP3 encoding.
- [Lines 283–319](<C:/Users/Aricie/Downloads/temp audio/compress_epub_audio.py:283>) preserve non-audio resource contents, store newly compressed MP3s without redundant ZIP DEFLATE, verify the completed archive, and publish it without overwriting an existing output.

The compressor preserves the source's decoded timeline; it does not establish that the source SMIL was correct. Reader behavior remains relevant because the new MP3s rely on honoring their encoder-delay metadata, as its README already states. It also performs several deliberate passes: extraction, bitrate scan, decode/encode, output decode, archive assembly, and archive verification. These cost time and I/O but provide different guarantees. Integrating the desired bitrate at initial generation eliminates the entire later conversion pass; removing the integrity checks to advertise a faster compressor would be a different tradeoff.

For generic third-party MP3s, embedded MP3 artwork is not explicitly copied as an attached-picture stream: the code copies container text metadata but creates only one output audio stream. This does not remove the EPUB's separate cover image, and no such artwork-loss problem was established for the supplied Kokoro clips. It is a limitation to document if the compressor is marketed as a general MP3 metadata-preserving tool.

## Practical implementation choices for discussion

| Choice | Benefits | Costs and required checks |
|---|---|---|
| Keep 128 kbps generation; run the existing 48 kbps compressor | Smallest change; works with existing books; robust whole-track sample verification | A second lossy encode; extra CPU, disk traffic, temporary space, and a separate step |
| Add a real server bitrate setting and request 48 kbps MP3 | One encode; smaller payload and in-memory audio; limited client changes | Must update client duration/silence together and validate server output; must persist server changes in image/config; retains per-request MP3 delay/padding behavior |
| Request PCM with timestamps, assemble each chapter on a sample timeline, encode once to 48 kbps | One lossy encode; bitrate-independent timing; avoids per-request MP3 boundary padding; encoder work could overlap subsequent inference | Larger client change; raw 24 kHz mono int16 is 48,000 bytes/s, three times the current 128 kbps payload; stage to disk or a bounded buffer; encoding is not free; verify gapless playback and seeking in the target reader |

The third option is the cleanest timing design, not a promised speed win. The second is a smaller scope if the immediate goal is reduced size. The first remains useful for the already-generated library.

Do not select VBR while retaining bytes-per-millisecond duration estimates. VBR requires real frame/sample accounting. Retaining MP3 also minimizes the migration scope; Opus is an option to investigate only with corresponding container, manifest, timing, and reader tests. Current EPUB 3.3 does include Ogg Opus among its core media types, so it should not be described as invalid EPUB audio. [W3C EPUB 3.3 core media types](https://www.w3.org/TR/epub-33/#sec-core-media-types).

## External source cross-check

The current upstream [speech request schemas](https://github.com/remsky/Kokoro-FastAPI/blob/master/api/src/structures/schemas.py) and [audio writer](https://github.com/remsky/Kokoro-FastAPI/blob/master/api/src/services/streaming_audio_writer.py) were inspected as primary sources. They likewise expose output-format selection rather than a request bitrate and retain a hardcoded 128,000-bit/s writer setting. The installed runtime was reviewed independently from its copied files; current upstream code was not assumed to match every installed behavior. [FFmpeg's MP3 muxer documentation](https://ffmpeg.org/ffmpeg-formats.html#mp3) describes the Xing header and attached-picture handling relevant to the compressor.
