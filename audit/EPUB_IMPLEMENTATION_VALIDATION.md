# EPUB correctness implementation validation

2026-10-15. Input EPUBs were read without modification. This report covers extraction, markup and metadata; audio engine validation is separate.

- Mixed blocks now narrate in document order: paragraphs, headings, list items, table cells, captions, and direct prose surrounding nested blocks. Each source character is mapped once. Short replies, digits and Unicode text are retained. Inline formatting and small caps stay intact.
- Semantic note regions, citation markers, comments, scripts/styles, explicit hidden content, and narrowly identified legacy note backlinks are excluded from narration while remaining in the book. Wages of Destruction's three note documents retain 812 narrated characters of headings/introduction, rather than accidentally adding hundreds of thousands of note characters after the mixed-block fix.
- NCX navigation preserves encoded fragments and nested lists. Generated paragraph/word IDs avoid existing authored IDs. Duplicated resolved spine entries are removed from the resulting package.
- EPUB2 creator roles/file-as values and identifier schemes become EPUB3 refinements; date events are preserved without duplicate publication dates. Deprecated presentation attributes become CSS, with existing inline styles retaining precedence. Narrated documents include a highlight stylesheet.
- Reprocessing requires a linked overlay with the converter's manifest/SMIL signature. New output also records ownership in metadata and generated spans. Referenced converter resources are replaced; unrelated author SMIL and XHTML formatting are preserved. Shared or unknown author overlays produce a clear preflight error.
- Serialized XHTML is validated before any synthesis. Original defects that the parser can repair are allowed. Documents that remain malformed are rejected with their path and XML error.

## Verification

`python -m pytest tests/test_epub_correctness.py -q --basetemp=.pytest_epub_correctness6 --tb=short`: **17 passed**. Existing parser/packaging tests also passed before the concurrent synthesis-interface changes; Kokoro-specific mock updates are managed with the audio implementation.

`python audit/epub_correctness_regression.py`: **23 books, 4,593 spine documents, 82,601 narrated blocks**, approximately **232 seconds** in this run. No newly malformed XML, introduced duplicate IDs, repeated mapped source positions, count-only/full extraction differences, or changed visible body text after synchronization-span injection were found. Full details are in `epub_correctness_regression.json`.

The library scan deliberately disables production's strict XML preflight so it can inspect every source document. The same **six pre-existing malformed pages** remain in Origins of Totalitarianism: 121, 267, 368, 394, 430 and 457. Production conversion now rejects this book before synthesis until those source pages are repaired. These are not new migration errors.

This scan does not replace EPUBCheck or reader playback testing. EPUBCheck comparisons and end-to-end audio checks belong to the integration validation.
