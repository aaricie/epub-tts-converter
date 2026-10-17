# EPUB parsing, packaging, and UI audit

Read-only application review performed 2026-10-15. No application source or input EPUB was changed. Audit scripts, reports, and synthetic outputs were added only under `audit/`.

## Scope and checks

- Scanned all **23 EPUB files** in `test books/`, resolving the real spine and running the current extraction and front/back matter classifiers on every chapter.
- Checked text coverage outside selected paragraphs, excluding explicit noteref/pagebreak markers, comments, scripts, and styles from the coverage calculation. A coverage difference alone is not necessarily a bug: some uncovered material is inline footnotes, bibliographic tables, or intentionally optional appendices.
- Performed five complete conversions with fake audio: Ontario, Kursk, Lord of the Rings, Wages of Destruction, and Origins of Totalitarianism. These exercises used one synthetic word boundary per paragraph to limit CPU and memory, not real Kokoro speech or every-word alignment.
- Inspected **682 generated SMIL documents and 17,920 SMIL text targets**. No missing generated target files or IDs, duplicate XHTML IDs, or lost XHTML root namespaces occurred on the first conversion of the well-formed inputs. Six already-malformed source pages in Origins remain malformed in output.
- Ran minimal deterministic reproductions for extraction omissions, short paragraphs, HTML comments, fallback whitespace, NCX fragments, duplicate spines, non-Latin text filtering, repeated conversion, and missing output directory handling.
- Parent review ran the existing **31 tests**, all passing. The additional checks here expose paths that existing tests do not cover.

The initial checks were XML/reference validation. A subsequent official EPUBCheck 5.4.0 run covered three original/output pairs; its verified findings are appended below. A fake-audio package is not a usable audiobook and does not validate audio timing or reader playback.

## Highest priority confirmed findings

### 1. Mixed paragraph/list chapters silently lose real prose

**Location:** `epub_tts_converter.py:1330` and `:1338` (`process_chapter`).

The primary pass selects only `h1`–`h6` and `p`. The `div`, `li`, `blockquote`, `section`, etc. fallback runs only when the entire chapter has no selected primary blocks. A chapter containing normal paragraphs plus plain list items narrates the paragraphs and drops the list items. `td`/`th`/`figcaption` are not in either selection set.

**Real-book evidence:**

| Input | Visible characters outside selected content in chapters retained by current skip settings | Interpretation |
|---|---:|---|
| L'Ontario a moto | 18,423 | Predominantly list-based travel advice; verified meaningful narrative omissions |
| Nord-Est des Etats-Unis a moto | 15,409 | Predominantly itinerary/tips lists; verified meaningful narrative omissions |
| Lord of the Rings | 39,372 | Mostly chronology and explanatory tables in appendices; include/exclude should be explicit |
| On the Psychology of Military Incompetence | 17,882 | Mostly inline `aside` footnotes; do not treat this count as lost main narrative |
| Wages of Destruction | 285,932 | Mostly notes in `div` blocks; illustrates inconsistent note detection, not 285k missing characters of main prose |

The Ontario guide loses roughly 7.3% of the selected-plus-uncovered visible character count in those chapters; Nord-Est roughly 5.8%. Minimal fixture `<p>First paragraph.</p><div>Second paragraph in a div.</div><ul><li>List information.</li></ul>` returns only the first paragraph.

**Suggested direction:** traverse mixed block content in document order with ancestor/descendant deduplication; define treatment of lists, tables, captions, and footnotes explicitly. Add real-format regression fixtures. This is more important than micro-optimizing the parser.

### 2. Generated EPUB3 navigation discards section destinations

**Location:** `epub_tts_converter.py:1012`–`:1016` (`_nav_info_from_ncx`).

Every NCX `content src` is split at `#` and the fragment is never restored. Generated navigation points to the beginning of the HTML file, even when the original link targets a later section. The generator also flattens the original NCX hierarchy.

**Verified fixture:** `Text/ch1.xhtml#section-two` becomes `Text/ch1.xhtml`.

**Real impact:** seven supplied books lack an existing EPUB3 nav and have NCX navPoint fragment links. They include **54 Ontario** and **95 Nord-Est** links, where many sections share a small number of HTML files. Other affected inputs: Speer (32), Germany and the Second World War (151), Hitler's Wehrmacht (15), Occupied Economies (32), To Lose a Battle (270). Counts are specifically navPoint/content entries, not NCX page-list entries. Some fragments simply identify a file's opening heading, but later sections and footnotes are genuinely redirected.

**Suggested direction:** preserve encoded fragments when resolving the path and reproduce the nested NCX tree.

### 3. Reprocessing an existing narrated EPUB duplicates audio and word IDs

**Locations:** `epub_tts_converter.py:1454`, `:1983`–`:2027`, `:2179`–`:2202`, `:2243`–`:2249`.

Existing `mo_ch...w...` spans stay in the document and the next run generates the same IDs again. New audio receives a collision-safe name while old audio and SMIL remain in the archive. OPF global media duration, narrator, and active-class metadata are appended again.

**Verified two-pass minimal conversion:** two copies of each generated word ID, both `Audio/ch001.mp3` and `Audio/ch001_1.mp3`, and two unrefined `media:duration` entries. The second pass succeeds without warning. This is directly relevant to file-size concerns if a user accidentally feeds an `_audio.epub` back into the converter.

**Suggested direction:** detect existing media overlays before synthesis and provide a deliberate replace/preserve policy. Replacement should remove only the converter's managed resources/spans and reset its metadata, while preserving source EPUB links and other content.

## Further confirmed content and behavior issues

### Short genuine passages are discarded

**Location:** `epub_tts_converter.py:1289`.

Every block with fewer than five characters is dropped. `Yes.`, `No.`, and `Go!` all disappear in a fixture. Real LOTR examples include `June`, `July`, `NOTE`, and `MAPS`. This may have been intended to suppress ornaments, but length is an unreliable proxy for non-speech. Test alphanumeric content instead, with explicit ornament handling.

### Non-Latin scripts and numeric paragraphs become silence without calling TTS

**Locations:** `epub_tts_converter.py:1749` and `:1827`.

The allowed-letter regex requires two adjacent Latin/Cyrillic characters. Mocked synthesis proves Japanese, Chinese, Hindi, Greek, and `12345` never reach the engine; each receives a 5,040-byte silence segment under default settings. The English control reaches the engine normally. This is independent of whether a particular selected voice supports the text. Use Unicode-aware content detection rather than a hard-coded script whitelist. Single-letter meaningful text and numeric labels deserve an explicit policy too.

### HTML comments are spoken

**Location:** `epub_tts_converter.py:1261`–`:1268`.

BeautifulSoup `Comment` is a `NavigableString`, so `<p>Hello <!-- editorial note -->world.</p>` produces `Hello editorial note world.`. Explicitly exclude comments and non-rendered nodes before building the text/map. No prevalence claim is made for the real books.

### Fallback block boundaries can join words

**Location:** `epub_tts_converter.py:1256`–`:1269`.

Only `br` inserts a separator. `<div>First line<div>Second line</div></div>` produces `First lineSecond line`. Separators should reflect block boundaries while preserving joins across ordinary inline markup (existing small-caps tests correctly require inline joins).

### Duplicate spine entries are only removed from the synthesis work list

**Locations:** `epub_tts_converter.py:924`–`:949`, `:1969` onward.

The original OPF spine is not repaired. A fixture with two `itemref idref="ch1"` entries logs that one duplicate was skipped, writes audio only once, but still outputs two spine references. A reader following the spine can revisit the same narrated chapter. None of the 23 current test books had duplicate resolved spine documents, so this is a supported-input edge case rather than observed impact in this set.

### Plain-text and Markdown Pause does not pause

**Locations:** `epub_tts_converter.py:2352`–`:2425`, `:3144`–`:3155`.

The GUI exposes Pause for all modes, but `convert_text_to_mp3` has no `pause_event` argument and its tasks never wait on it. EPUB synthesis does. Pass the event through to plain text and check it between chunks; also decide how cancellation should interrupt in-flight requests. This finding is established by code, not a live GUI run.

### A missing output folder fails after all synthesis

**Locations:** `epub_tts_converter.py:2237`, `:2419`, `:2909`, `:3078` onward.

The output-directory field is editable, but EPUB3/text output is not created or checked before synthesis. Minimal reproduction confirms the synth function runs before `FileNotFoundError` is raised for a missing directory. The MP3-chapter path does create its directory. Validate/create the requested directory and perform a small writability probe before expensive work; use a temporary output and atomic completion for long conversions.

### Preview completion can re-enable backend switching during a run

**Locations:** `epub_tts_converter.py:2807`–`:2841`, `:2875`–`:2876`, `:2894`.

Start remains available while a preview is running. If conversion begins during that preview, Start disables Test, but the preview's eventual `preview_done` unconditionally enables Test again. A subsequent Test uses `set_tts_backend` and can change the process-wide backend while the conversion is active. This is a conditional race identified from the control flow, not reproduced against a real GUI/audio device. Make Test availability depend on both preview and conversion state; better, pass an immutable provider configuration into synthesis.

## Input quality and skip-policy observations

- Origins of Totalitarianism has six malformed OCR-derived HTML pages: `page_121`, `267`, `368`, `394`, `430`, `457`. Invalid markup already exists in the input and survives conversion; the application currently still logs successful completion. Treat this as an input-validation gap, not new corruption introduced by the converter. A preflight report could warn about unparseable output and suspected OCR text loss before spending hours on TTS.
- The whole-file front/back matter classification is heuristic and inconsistent for notes in generically named split files or `aside`/table structures. For example, Wages' notes split across `part0035*` and `part0036` are classified as ordinary content, but much note text is accidentally skipped because of the mixed-block bug. Fixing extraction without an explicit note policy would begin narrating this material. This should be a conscious product decision.
- Whole-file semantic checks search early HTML bytes. Embedded `epub:type="footnotes"` or `epub:type="toc"` regions can therefore classify the containing file, not just the section. A monolithic EPUB with mixed semantic regions needs section-aware handling. This is a code-level edge case, not demonstrated as a current-book omission.
- The full mock conversions preserved XHTML namespaces and generated target references in the well-formed examples. Official EPUBCheck subsequently confirmed that merely setting package version to 3.0 leaves incompatible EPUB2 metadata and XHTML constructs; see the specific migration errors below.

## Performance opportunities in this part of the code

1. **Stream completed chapters to a temporary output/staging area.** `new_files[audio_zip] = b"".join(audio_chunks)` at line 2201 retains every completed chapter's audio until final writing at line 2237. Peak memory scales with the entire audiobook, plus current-chapter copies. This is likely far more valuable on a small machine than parser micro-optimizations. Staging also enables durable resume after failure.
2. **Avoid full mapping structures for an ETA scan.** `_prescan_files` calls `process_chapter` at line 2995 solely to count characters, creating per-character mappings and paragraph IDs that are discarded. Conversion parses the chapter again. Reuse a lightweight extraction/preflight plan or count-only traversal, ensuring it follows the same inclusion rules. My scanner also performs extra diagnostics and is not a clean benchmark of the application's pre-scan.
3. **Do not eagerly read replaced ZIP entries.** `modified.get(name, zf.read(name))` at line 2248 evaluates `zf.read` even for known replacements. Use an explicit branch. This is a small, straightforward reduction in decompression and allocation, not expected to transform synthesis speed.
4. **Keep the existing stored-MP3 optimization.** New MP3 ZIP entries already use `ZIP_STORED` at lines 2253–2255. Recommending this as a new optimization would be incorrect. Existing source MP3s are still copied with default compression if reprocessing a narrated input; removing/replacing obsolete audio is the better first fix.
5. **Validate output and source before synthesis.** This prevents wasted runs rather than making the neural engine faster. The missing-folder fixture and malformed OCR inputs provide concrete examples.

Audio-specific duration, concatenation, pause coverage, bitrate, and engine throughput are being reviewed separately. In particular, `build_smil` emits only each boundary's offset/duration, leaving gaps between clips; the inserted paragraph silence is included in the audio cursor but not necessarily played by a media-overlay reader. Reader behavior and real clip coverage need the audio-side findings before a final fix is chosen.

## Artifacts

- `epub_integrity_scan.py` / `.json`: per-book/chapter coverage and XML scan for all 23 inputs.
- `epub_integrity_repros.py` / `.json`: extraction/nav/duplicate-spine fixtures.
- `epub_edge_case_repros.py` / `.json`: script filter, reconversion, and missing-output-folder fixtures.
- `epub_nav_scan.json`: corrected NCX navPoint fragment counts.
- `epub_mock_conversion_check.py` / `.json`: five complete mock conversions and invariant checks.
- `mock_epub_outputs/`: five synthetic packages were produced for inspection; these intentionally contain fake audio and must not be mistaken for finished audiobooks. They may be removed after validation to avoid retaining approximately 174 MB of redundant data. `epub_mock_conversion_check.py` regenerates them from the unchanged source books.

Suggested implementation order: prevent content omissions and fix navigation; make output streaming/resumable and preflight inputs/destination; define note/table policy; repair reprocessing behavior; then address UI consistency and minor parser/ZIP optimizations. Coordinate this with the audio/48 kbps work rather than changing all concerns in one patch.

## Official EPUBCheck 5.4.0 follow-up

Ran the official local validator on original and mock-converted Ontario, Kursk, and LOTR packages, one JVM at a time with a 512 MB Java heap. The six completed checks took approximately 88 seconds total according to the validator. The first attempt inside the execution sandbox did not start Java, including a separate `java -version` probe; these timeouts are an execution-environment limitation and are not book-performance measurements. Retrying outside that sandbox completed normally.

| Book | Original errors / warnings | Converted errors / warnings | New conversion/migration errors |
|---|---:|---:|---:|
| Ontario | 1 / 0 | 6 / 0 | 5 |
| Kursk | 1 / 0 | 1 / 0 | 0 |
| Lord of the Rings | 0 / 0 | 8 / 0 | 8 |

These are EPUBCheck's reported error counts; none of the six runs had fatal errors. This is stronger evidence than XML parsing alone: the LOTR original passes, while its converted version fails.

**Verified migration defect, `patch_opf` at line 1971:** the converter switches to EPUB3 without migrating EPUB2 metadata and presentation attributes. Ontario gains four OPF errors for legacy `file-as`, `role`, and two `scheme` attributes, plus an XHTML error for retained `img width="61%"`. Its pre-existing empty identifier remains a separate input defect. LOTR gains four OPF errors (`event`, `file-as`, `role`, and a second `dc:date`) and four XHTML errors (`td valign`, `table width`, non-integer image `height`, and non-integer image `width`). All eight were absent when validating the original EPUB2 package. A real EPUB2-to-EPUB3 migration should convert these constructs to EPUB3 metadata refinements/CSS rather than only changing the version string. This should join the higher-priority packaging work.

**Pre-existing defect, not introduced:** Kursk's NCX identifier `9781912866793` disagrees with its OPF identifier `5747224`; the same NCX-001 error appears before and after conversion. No new diagnostics were introduced for this EPUB3 source.

The parent's separate tiny real-Kokoro fixture also receives **CSS-030** because `patch_opf` declares a media-overlay active styling class but the content document has no CSS. This does not occur in the three book pairs, which already contain CSS. Preserve this as a separate minimal-input regression case.

Detailed validator JSON and stdout logs: `epubcheck_{ontario,kursk,lotr}_{input,output}.{json,log}`. Compact counts/timings: `epubcheck_pairs_summary.json`. Reproduction: `epubcheck_pairs.py` (regenerate mock packages first if cleaned up).
