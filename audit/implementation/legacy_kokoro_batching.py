"""Benchmark-only copy of the first (strict) batching design, 2026-10-15.

Kept so bench_pipeline.py can compare it with kokoro_batching.py. Not used by
the converter. Strict token equality across the whole batch; cuts at the next
paragraph's first word timestamp.
"""
from dataclasses import dataclass
import math
import re
import unicodedata
from typing import Mapping, Sequence


def _lexical_tokens(text):
    return re.findall(r"[^\W_]+", unicodedata.normalize("NFC", text).casefold())


@dataclass(frozen=True)
class ParagraphBatch:
    indices: tuple[int, ...]
    texts: tuple[str, ...]

    @property
    def combined_text(self):
        return "\n\n".join(self.texts)


def plan_paragraph_batches(paragraphs: Sequence[Mapping], *, max_chars=1500,
                           max_paragraphs=4) -> list[ParagraphBatch]:
    if max_chars < 1 or max_paragraphs < 1:
        raise ValueError("Batch character and paragraph limits must be positive.")
    batches, indices, texts = [], [], []
    characters = 0

    def flush():
        nonlocal indices, texts, characters
        if indices:
            batches.append(ParagraphBatch(tuple(indices), tuple(texts)))
        indices, texts, characters = [], [], 0

    for index, paragraph in enumerate(paragraphs):
        text = paragraph.get("tts_text", paragraph.get("text", ""))
        if not isinstance(text, str):
            raise TypeError("Paragraph TTS text must be a string.")
        if not _lexical_tokens(text) or len(text) > max_chars:
            flush()
            batches.append(ParagraphBatch((index,), (text,)))
            continue
        added = len(text) + (2 if indices else 0)
        if len(indices) >= max_paragraphs or characters + added > max_chars:
            flush()
            added = len(text)
        indices.append(index)
        texts.append(text)
        characters += added
    flush()
    return batches


def split_pcm_batch(batch: ParagraphBatch, pcm: bytes, boundaries: Sequence[Mapping],
                    *, sample_rate=24000):
    if not isinstance(sample_rate, int) or sample_rate <= 0:
        raise ValueError("PCM sample rate must be a positive integer.")
    if not isinstance(pcm, (bytes, bytearray, memoryview)):
        raise TypeError("PCM must be bytes-like.")
    if not pcm or len(pcm) % 2 or not batch.texts or len(batch.texts) != len(batch.indices):
        return None
    source_tokens = [_lexical_tokens(text) for text in batch.texts]
    if any(not tokens for tokens in source_tokens):
        return None
    expected = [token for tokens in source_tokens for token in tokens]
    duration_ms = len(pcm) * 500.0 / sample_rate
    sample_ms = 1000.0 / sample_rate
    records, caption_tokens, token_ranges = [], [], []
    last_start = -1.0
    for boundary in boundaries:
        if boundary.get("type") != "wordboundary":
            continue
        text = boundary.get("text", "")
        if not isinstance(text, str):
            return None
        tokens = _lexical_tokens(text)
        if not tokens:
            continue
        try:
            start = float(boundary["offset"])
            duration = float(boundary["duration"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        end = start + duration
        if (not all(math.isfinite(value) for value in (start, duration, end))
                or start < 0 or duration <= 0 or start < last_start
                or start >= duration_ms or end > duration_ms + sample_ms):
            return None
        token_ranges.append((len(caption_tokens), len(caption_tokens) + len(tokens)))
        caption_tokens.extend(tokens)
        records.append(dict(boundary, offset=start, duration=duration))
        last_start = start
    if caption_tokens != expected:
        return None
    groups, token_cursor, record_cursor = [], 0, 0
    for tokens in source_tokens:
        token_end = token_cursor + len(tokens)
        group = []
        while record_cursor < len(records) and token_ranges[record_cursor][0] < token_end:
            start_token, end_token = token_ranges[record_cursor]
            if start_token < token_cursor or end_token > token_end:
                return None
            group.append(records[record_cursor])
            record_cursor += 1
        if not group:
            return None
        groups.append(group)
        token_cursor = token_end
    if record_cursor != len(records):
        return None
    sample_count = len(pcm) // 2
    cuts = [0]
    for group in groups[1:]:
        cut = round(group[0]["offset"] * sample_rate / 1000)
        if not cuts[-1] < cut < sample_count:
            return None
        cuts.append(cut)
    cuts.append(sample_count)
    result = []
    for group, start_sample, end_sample in zip(groups, cuts, cuts[1:]):
        start_ms = start_sample * 1000.0 / sample_rate
        end_ms = end_sample * 1000.0 / sample_rate
        shifted = []
        for boundary in group:
            original_start = boundary["offset"]
            original_end = original_start + boundary["duration"]
            if original_start < start_ms - sample_ms or original_end > end_ms + sample_ms:
                return None
            relative_start = max(0.0, original_start - start_ms)
            relative_end = min(end_ms, original_end) - start_ms
            if relative_end <= relative_start:
                return None
            shifted.append(dict(boundary, offset=relative_start,
                                duration=relative_end - relative_start))
        result.append((bytes(pcm[start_sample * 2:end_sample * 2]), shifted))
    return result
