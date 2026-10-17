"""Stage 4 changes, explicitly enabled after correctness and PCM validation.

These preserve trimming padding and concatenate PCM once. The comparison form
also handles int16(-32768), which abs(int16) cannot represent safely.
"""
from __future__ import annotations


def patch_audio(source: str) -> str:
    from apply_patch import replace
    before = '''        for X in range(0, len(audio_data)):
            if abs(audio_data[X]) > amplitude_threshold:
                non_silent_index_start = X
                break

        for X in range(len(audio_data) - 1, -1, -1):
            if abs(audio_data[X]) > amplitude_threshold:
                non_silent_index_end = X
                break'''
    after = '''        # Preserve the cheap no-silence path without overflowing int16(-32768).
        if len(audio_data) == 0:
            return 0, 0
        if abs(int(audio_data[0])) > amplitude_threshold:
            non_silent_index_start = 0
        if abs(int(audio_data[-1])) > amplitude_threshold:
            non_silent_index_end = len(audio_data) - 1
        # Bound temporary masks and scan only the quiet edges of long chunks.
        scan_size = 4096
        if non_silent_index_start is None:
            for offset in range(0, len(audio_data), scan_size):
                block = audio_data[offset:offset + scan_size]
                mask = (block > amplitude_threshold) | (block < -amplitude_threshold)
                if np.any(mask):
                    non_silent_index_start = offset + int(np.argmax(mask))
                    break

        if non_silent_index_end is None:
            for end in range(len(audio_data), 0, -scan_size):
                offset = max(0, end - scan_size)
                block = audio_data[offset:end]
                mask = (block > amplitude_threshold) | (block < -amplitude_threshold)
                if np.any(mask):
                    non_silent_index_end = end - 1 - int(np.argmax(mask[::-1]))
                    break'''
    return replace(source, before, after)


def patch_combine(source: str) -> str:
    from apply_patch import replace
    before = '''        output = AudioChunk(
            audio_chunk_list[0].audio, audio_chunk_list[0].word_timestamps
        )

        for audio_chunk in audio_chunk_list[1:]:
            output.audio = np.concatenate(
                (output.audio, audio_chunk.audio), dtype=np.int16
            )
            if output.word_timestamps is not None:
                output.word_timestamps += audio_chunk.word_timestamps

        return output'''
    after = '''        if not audio_chunk_list:
            raise ValueError("Cannot combine an empty audio chunk list")
        # Preserve the single-chunk dtype; multi-chunk paths have always used int16.
        audio = audio_chunk_list[0].audio if len(audio_chunk_list) == 1 else np.concatenate(
            [chunk.audio for chunk in audio_chunk_list], dtype=np.int16
        )
        timestamps = None
        if audio_chunk_list[0].word_timestamps is not None:
            timestamps = []
            for chunk in audio_chunk_list:
                if chunk.word_timestamps is None:
                    raise ValueError("Missing timestamps in one of the audio chunks")
                timestamps.extend(chunk.word_timestamps)
        return AudioChunk(audio, timestamps)'''
    return replace(source, before, after)
