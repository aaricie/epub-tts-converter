"""Conservative paragraph batching for captioned Kokoro PCM requests.

Planning is independent of synthesis. A caller must split and validate the
*whole* response before publishing any of it. ``None`` from split_pcm_batch
means retry those paragraphs individually; never guess at timing. The original
request can remain checkpointed for diagnosis/reuse.

Kokoro normalizes text before captioning it ("1945" becomes "nineteen
forty-five", "Dr." becomes "Doctor"). Those rewrites are allowed *inside* a
paragraph: captions are aligned with the source words, and only the words at
each paragraph boundary must match verbatim. The planner keeps paragraphs whose
opening words are likely to be rewritten out of batch boundaries, so that a
batch is rarely synthesized only to be discarded.
"""
from array import array
from dataclasses import dataclass
from difflib import SequenceMatcher
import math
import re
import sys
import unicodedata
from typing import Mapping, Sequence


def _lexical_tokens(text):
    # Match Unicode letters/digits rather than an English-only whitelist.
    # Apostrophes/dashes are separators on both sides.
    return re.findall(r"[^\W_]+", unicodedata.normalize("NFC", text).casefold())


# Words the engine is likely to expand or spell out. "No." is only a number
# abbreviation when a numeral follows; as a reply it is read verbatim.
_EXPANDED_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "st", "mt", "jr", "sr", "prof", "gen", "col", "lt",
    "capt", "sgt", "gov", "rev", "hon", "fr", "vs", "etc", "cf", "vol", "pp", "fig"}
_WORD = re.compile(r"[^\W_]+")


def _literal_words(text):
    """Leading words that the engine should caption verbatim, up to two."""
    literal = []
    for match in _WORD.finditer(text):
        word, rest = match.group(), text[match.end():]
        folded = word.casefold()
        if (not word.isalpha() or (len(word) > 1 and word.isupper())
                or (rest.startswith(".") and (folded in _EXPANDED_ABBREVIATIONS or (
                    folded == "no" and re.match(r"\.\s*\d", rest))))):
            break  # Numerals ("1945"), acronyms ("NATO"), abbreviations ("Dr.").
        literal.append(word)
        if len(literal) == 2:
            break
    return literal


def literal_opening(text):
    """True when the engine should caption a paragraph's first word verbatim."""
    return bool(_literal_words(text))


def _literal_ending(text):
    words = _WORD.findall(text)
    return bool(words) and _literal_words(words[-1]) == [words[-1]]


def _can_join(previous_text, text):
    """A batch boundary needs two verbatim words on either side of it.

    The next paragraph must open verbatim; its second word or the previous
    paragraph's last word anchors the alignment (see _caption_cuts).
    """
    opening = _literal_words(text)
    return bool(opening) and (len(opening) == 2 or _literal_ending(previous_text))


@dataclass(frozen=True)
class ParagraphBatch:
    indices: tuple[int, ...]
    texts: tuple[str, ...]

    @property
    def combined_text(self):
        return "\n\n".join(self.texts)


def plan_paragraph_batches(paragraphs: Sequence[Mapping], *, max_chars=1500,
                           max_paragraphs=4) -> list[ParagraphBatch]:
    """Group adjacent short paragraphs within a character/paragraph budget.

    Empty, punctuation-only and oversized paragraphs are standalone batches;
    the normal individual-paragraph path retains responsibility for those. A
    paragraph whose opening is likely to be rewritten starts a new batch.
    Indices are zero-based positions in the original sequence.
    """
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
        if (len(indices) >= max_paragraphs or characters + added > max_chars
                or (indices and not _can_join(texts[-1], text))):
            flush()
            added = len(text)
        indices.append(index)
        texts.append(text)
        characters += added
    flush()
    return batches


def _caption_cuts(source_tokens, caption_tokens):
    """Caption token index where each paragraph after the first begins, or None.

    A boundary is accepted only where the alignment is unambiguous: the next
    paragraph's first word is matched verbatim, inside a run of at least two
    matched words or directly after the previous paragraph's matched last
    word, and nothing unmatched from the captions sits on the boundary.
    """
    flat = [token for tokens in source_tokens for token in tokens]
    starts, position = [], 0
    for tokens in source_tokens:
        starts.append(position)
        position += len(tokens)
    opcodes = SequenceMatcher(None, flat, caption_tokens, autojunk=False).get_opcodes()
    cuts = []
    for paragraph, start in enumerate(starts[1:], 1):
        found = None
        for number, (tag, i1, i2, j1, j2) in enumerate(opcodes):
            if i1 <= start < i2:
                found = number
                break
        if found is None:
            return None
        tag, i1, i2, j1, j2 = opcodes[found]
        if tag != "equal" or (start == i1 and i2 - i1 < 2):
            return None
        if start == i1:
            previous = opcodes[found - 1] if found else None
            if previous is not None:
                ptag, pi1, pi2, _, _ = previous
                # Unmatched caption words here could belong to either paragraph.
                if ptag == "insert" or pi1 < starts[paragraph - 1] or pi2 != start:
                    return None
        cuts.append(j1 + start - i1)
    return cuts


def _quietest_sample(pcm, low, high, window):
    """Centre of the quietest ``window`` within [low, high), nearest the middle."""
    if high - low <= window:
        return (low + high) // 2
    samples = array("h")
    samples.frombytes(bytes(pcm[low * 2:high * 2]))
    if sys.byteorder == "big":
        samples.byteswap()
    middle = (high - low) / 2
    energy = sum(abs(value) for value in samples[:window])
    best = (energy, abs(window / 2 - middle), window // 2)
    for end in range(window, len(samples)):
        energy += abs(samples[end]) - abs(samples[end - window])
        centre = end - window + 1 + window // 2
        candidate = (energy, abs(centre - middle), centre)
        if candidate < best:
            best = candidate
    return low + best[2]


def split_pcm_batch(batch: ParagraphBatch, pcm: bytes, boundaries: Sequence[Mapping],
                    *, sample_rate=24000):
    """Return ``[(PCM bytes, paragraph-relative word boundaries), ...]`` or None.

    PCM is mono signed 16-bit little endian. Caption words are aligned with the
    source words; every caption belongs wholly to one paragraph, and each cut
    falls in the quietest part of the pause between the previous paragraph's
    last spoken word and the next paragraph's first. Every PCM sample, including
    natural pauses and leading/trailing silence, belongs to exactly one piece.
    The caller adds configured paragraph pauses *after* these pieces.
    """
    if not isinstance(sample_rate, int) or sample_rate <= 0:
        raise ValueError("PCM sample rate must be a positive integer.")
    if not isinstance(pcm, (bytes, bytearray, memoryview)):
        raise TypeError("PCM must be bytes-like.")
    if not pcm or len(pcm) % 2 or not batch.texts or len(batch.texts) != len(batch.indices):
        return None
    source_tokens = [_lexical_tokens(text) for text in batch.texts]
    if any(not tokens for tokens in source_tokens):
        return None
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
    cuts = _caption_cuts(source_tokens, caption_tokens)
    if cuts is None:
        return None

    # Allocate caption records by token position. Repeated words and
    # paragraphs therefore cannot accidentally select an earlier hit.
    limits = cuts + [len(caption_tokens)]
    groups, record_cursor, token_start = [], 0, 0
    for limit in limits:
        group = []
        while record_cursor < len(records) and token_ranges[record_cursor][0] < limit:
            start_token, end_token = token_ranges[record_cursor]
            if start_token < token_start or end_token > limit:
                return None  # One caption crosses a paragraph boundary.
            group.append(records[record_cursor])
            record_cursor += 1
        if not group:
            return None
        groups.append(group)
        token_start = limit
    if record_cursor != len(records):
        return None

    sample_count = len(pcm) // 2
    window = max(1, sample_rate // 200)  # 5 ms
    sample_cuts = [0]
    for previous, group in zip(groups, groups[1:]):
        previous_end = max(r["offset"] + r["duration"] for r in previous)
        next_start = group[0]["offset"]
        low = math.ceil(previous_end * sample_rate / 1000)
        high = math.floor(next_start * sample_rate / 1000)
        if high < low:
            return None  # Overlapping words leave no pause to cut in.
        cut = _quietest_sample(pcm, low, high, window)
        if not sample_cuts[-1] < cut < sample_count:
            return None
        sample_cuts.append(cut)
    sample_cuts.append(sample_count)
    result = []
    for group, start_sample, end_sample in zip(groups, sample_cuts, sample_cuts[1:]):
        start_ms = start_sample * 1000.0 / sample_rate
        end_ms = end_sample * 1000.0 / sample_rate
        shifted = []
        for boundary in group:
            original_start = boundary["offset"]
            original_end = original_start + boundary["duration"]
            # The cut lies between the groups' words; anything outside its own
            # piece by more than one sample of rounding is inconsistent timing.
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
