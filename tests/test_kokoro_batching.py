import math

import pytest

from kokoro_batching import ParagraphBatch, plan_paragraph_batches, split_pcm_batch


def _bound(word, start, end):
    return {'type': 'wordboundary', 'text': word, 'offset': start, 'duration': end - start}


def _pcm(milliseconds=1000, silent=()):
    # Deliberately non-silent distinct bytes: conservation must not be an
    # accidental equality caused by zero-valued silence everywhere. Explicit
    # silent spans make the quietest cut position deterministic.
    data = bytearray((bytes(range(256)) * ((milliseconds * 48 + 255) // 256))[:milliseconds * 48])
    for start, end in silent:
        data[start * 48:end * 48] = bytes((end - start) * 48)
    return bytes(data)


def test_planner_limits_payload_including_separator_and_keeps_indices():
    paragraphs = [{'text': text} for text in ['one', 'two', 'three', 'four', 'five']]
    batches = plan_paragraph_batches(paragraphs, max_chars=10, max_paragraphs=2)
    assert [batch.indices for batch in batches] == [(0, 1), (2,), (3, 4)]
    assert [batch.combined_text for batch in batches] == ['one\n\ntwo', 'three', 'four\n\nfive']
    assert all(len(batch.combined_text) <= 10 for batch in batches)


def test_planner_isolates_silence_oversized_and_empty_passages():
    batches = plan_paragraph_batches([{'text': text} for text in ['a', '***', 'bbbbbb', '', 'c', 'd']],
                                     max_chars=5)
    assert [batch.indices for batch in batches] == [(0,), (1,), (2,), (3,), (4, 5)]
    assert batches[-1].texts == ('c', 'd')


def test_planner_uses_sanitized_tts_text_and_maximum_four_default():
    batches = plan_paragraph_batches([{'text': 'visible', 'tts_text': 'spoken'}] * 5)
    assert [len(batch.indices) for batch in batches] == [4, 1]
    assert batches[0].combined_text == 'spoken\n\nspoken\n\nspoken\n\nspoken'


def test_split_keeps_every_sample_and_cuts_in_the_quietest_part_of_the_pause():
    batch = ParagraphBatch((0, 1), ('Hello world.', 'Second paragraph.'))
    bounds = [_bound('Hello', 50, 150), _bound('world.', 180, 300),
              _bound('Second', 600, 750), _bound('paragraph.', 780, 950)]
    pcm = _pcm(silent=[(400, 500)])
    pieces = split_pcm_batch(batch, pcm, bounds)
    assert [len(piece[0]) for piece in pieces] == [450 * 48, 550 * 48]
    assert b''.join(piece[0] for piece in pieces) == pcm
    assert pieces[0][1] == bounds[:2]
    assert pieces[1][1] == [_bound('Second', 150, 300), _bound('paragraph.', 330, 500)]
    assert bounds[2]['offset'] == 600  # No mutation of the cached response.


def test_cut_avoids_quiet_audio_outside_the_pause():
    batch = ParagraphBatch((0, 1), ('Hello world.', 'Second paragraph.'))
    bounds = [_bound('Hello', 50, 150), _bound('world.', 180, 300),
              _bound('Second', 600, 750), _bound('paragraph.', 780, 950)]
    # Silence inside a word must not attract the cut; the pause itself is louder.
    pieces = split_pcm_batch(batch, _pcm(silent=[(650, 700)]), bounds)
    assert 300 * 48 <= len(pieces[0][0]) <= 600 * 48


def test_repeated_words_are_allocated_by_order_and_count():
    batch = ParagraphBatch((3, 4, 5), ('Go go.', 'Go.', 'Go go.'))
    bounds = [_bound('Go', start, start + 50) for start in [10, 100, 300, 500, 700]]
    pieces = split_pcm_batch(batch, _pcm(silent=[(200, 250), (400, 450)]), bounds)
    assert [len(words) for _, words in pieces] == [2, 1, 2]
    assert [len(data) // 48 for data, _ in pieces] == [225, 200, 575]


@pytest.mark.parametrize('text, captions', [
    ('$12.', ['twelve', 'dollars']),
    ('On 12/10/2026.', ['On', 'October', 'twelfth', 'twenty', 'twenty', 'six']),
    ('First sentence.', ['First']),
    ('First sentence.', ['First', 'sentence', 'extra']),
])
def test_normalized_missing_or_extra_vocabulary_requires_individual_fallback(text, captions):
    batch = ParagraphBatch((0, 1), (text, 'Done.'))
    words = captions + ['Done']
    bounds = [_bound(word, index * 100, index * 100 + 50) for index, word in enumerate(words)]
    assert split_pcm_batch(batch, _pcm(), bounds) is None


def test_rewritten_words_inside_a_paragraph_keep_the_batch():
    batch = ParagraphBatch((0, 1), ("In 1945 it ended. Can't go.", 'Then peace came.'))
    words = ['In', 'nineteen', 'forty-five', 'it', 'ended', 'Cannot', 'go', 'Then', 'peace', 'came']
    bounds = [_bound(word, index * 90, index * 90 + 60) for index, word in enumerate(words)]
    pieces = split_pcm_batch(batch, _pcm(), bounds)
    assert [[bound['text'] for bound in group] for _, group in pieces] == [words[:7], words[7:]]


def test_planner_does_not_place_a_batch_boundary_before_rewritten_openings():
    texts = ['Hello there.', '1945 was a year.', 'Mr. Smith left.', 'No. 5 is here.',
             'NATO met.', 'Good news came.', 'No.', 'Yes.']
    batches = plan_paragraph_batches([{'text': text} for text in texts])
    assert [batch.indices for batch in batches] == [(0,), (1,), (2,), (3,), (4, 5, 6, 7)]


def test_planner_needs_a_second_verbatim_word_after_a_rewritten_ending():
    batches = plan_paragraph_batches([{'text': text} for text in ['It cost $12.', 'Done.', 'Done that.']])
    assert [batch.indices for batch in batches] == [(0,), (1, 2)]


def test_punctuation_case_and_canonical_unicode_do_not_create_false_mismatch():
    batch = ParagraphBatch((0, 1), ('“CAFÉ”—well-known.', "I'm here!"))
    bounds = [_bound('cafe\u0301', 0, 50), _bound('well-known', 60, 200),
              _bound('I’m', 500, 600), _bound('here.', 700, 900)]
    assert split_pcm_batch(batch, _pcm(), bounds) is not None


def test_caption_cannot_straddle_paragraph_boundary_even_with_matching_vocabulary():
    batch = ParagraphBatch((0, 1), ('First.', 'Second.'))
    assert split_pcm_batch(batch, _pcm(), [_bound('First Second', 0, 900)]) is None


def test_multiword_caption_inside_single_paragraph_is_accepted():
    batch = ParagraphBatch((0, 1), ('First two.', 'Second.'))
    pieces = split_pcm_batch(batch, _pcm(), [_bound('First two', 0, 400), _bound('Second', 500, 900)])
    assert [len(words) for _, words in pieces] == [1, 1]


@pytest.mark.parametrize('bad', [
    _bound('Second', -1, 500),
    _bound('Second', 500, 400),
    _bound('Second', 500, 500),
    _bound('Second', 500, 1100),
    _bound('Second', math.nan, 900),
    _bound('Second', 500, math.inf),
    _bound('Second', 0, 50),
])
def test_invalid_or_unsplittable_timings_fall_back(bad):
    batch = ParagraphBatch((0, 1), ('First.', 'Second.'))
    assert split_pcm_batch(batch, _pcm(), [_bound('First', 0, 200), bad]) is None


def test_overlapping_words_across_paragraph_boundary_require_fallback():
    batch = ParagraphBatch((0, 1), ('First.', 'Second.'))
    assert split_pcm_batch(batch, _pcm(), [_bound('First', 0, 600), _bound('Second', 500, 900)]) is None


def test_fractional_millisecond_timing_keeps_whole_samples():
    batch = ParagraphBatch((0, 1), ('First.', 'Second.'))
    pcm = _pcm(silent=[(380, 420)])
    pieces = split_pcm_batch(batch, pcm, [_bound('First', 0, 300), _bound('Second', 500.02, 900)])
    assert b''.join(piece[0] for piece in pieces) == pcm
    assert all(len(piece[0]) % 2 == 0 for piece in pieces)
    assert pieces[1][1][0]['offset'] == pytest.approx(100.02)
    assert pieces[1][1][0]['duration'] == pytest.approx(399.98)


def test_empty_and_partial_pcm_cannot_be_published():
    batch = ParagraphBatch((0, 1), ('First.', 'Second.'))
    bounds = [_bound('First', 0, 200), _bound('Second', 500, 900)]
    assert split_pcm_batch(batch, b'', bounds) is None
    assert split_pcm_batch(batch, _pcm()[:-1], bounds) is None
