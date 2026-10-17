"""Apply reviewed changes to the exact Kokoro FastAPI build tested here.

No fuzzy matching: every input file is checked before any file is changed.
Run in a derivative image, never against an unreviewed upstream version.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path


UPSTREAM_DIGEST = "ghcr.io/remsky/kokoro-fastapi-gpu@sha256:79d81c41d8883611291c5785e47db2ba00765e9b89dd59de05d9f28e1945f905"
UPSTREAM_HASHES = {
    "services/tts_service.py": "9502b38acddf5fc4aa7f2568b2dcdc6c005caa68c7804c08e63ff5067c61da2f",
    "inference/kokoro_v1.py": "1d5057192cbdfdcf663cd853b14894f1e39bd09b9c2a60007634542f661da3fa",
    "services/audio.py": "6e612febccac4ac55ecd6fa2fac3f0d6229245679b8ce3954590ec10d035cb27",
    "inference/base.py": "388a0ba333f261f62e97c7e9ca16f7747c7ba1e0ac4848cb076e4615cbdff881",
    "routers/development.py": "b7ce00303949f72fa8a4e881a53e96c3dc0d5ccfc35b236141804ee5270e022f",
}


def replace(source: str, before: str, after: str, count: int = 1) -> str:
    if source.count(before) != count:
        raise ValueError(f"Upstream patch context changed: expected {count} matches for {before!r}")
    return source.replace(before, after)


def patch_service(source: str) -> str:
    source = replace(source,
        '\n                                logger.error(f"Failed to convert audio: {str(e)}")',
        '\n                                logger.error(f"Failed to convert audio: {str(e)}")\n                                raise')
    source = replace(source,
        '\n                            logger.error(f"Failed to convert audio: {str(e)}")',
        '\n                            logger.error(f"Failed to convert audio: {str(e)}")\n                            raise')
    source = replace(source,
        '                logger.error(f"Failed to process tokens: {str(e)}")',
        '                logger.error(f"Failed to process tokens: {str(e)}")\n                raise')
    source = replace(source,
        '                        chunk_data.audio*=volume_multiplier',
        '                        if chunk_data.audio is None or len(chunk_data.audio) == 0:\n'
        '                            raise RuntimeError("Model returned no audio for a requested text chunk")\n'
        '                        chunk_data.audio*=volume_multiplier')
    source = replace(source,
        '                        chunk_index += 1\n                else:',
        '                        chunk_index += 1\n'
        '                    if chunk_index == 0:\n'
        '                        raise RuntimeError("Model returned no chunks for requested text")\n'
        '                else:')
    source = replace(source,
        '                        logger.error("Model generated None for audio chunk")\n                        return',
        '                        raise RuntimeError("Model generated None for audio chunk")')
    source = replace(source,
        '                        logger.error("Model generated empty audio chunk")\n                        return',
        '                        raise RuntimeError("Model generated empty audio chunk")')
    source = replace(source,
        '                        logger.error(f"Failed to process pause chunk: {str(e)}")\n                        continue',
        '                        logger.error(f"Failed to process pause chunk: {str(e)}")\n                        raise')
    source = replace(source,
        '                            else:\n                                logger.warning(\n'
        '                                    f"No audio generated for chunk: \'{chunk_text[:100]}...\'"\n                                )',
        '                            else:\n                                raise RuntimeError(\n'
        '                                    f"No audio generated for chunk: \'{chunk_text[:100]}...\'"\n                                )')
    source = replace(source,
        '                            f"Failed to process audio for chunk: \'{chunk_text[:100]}...\'. Error: {str(e)}"\n'
        '                        )\n                        continue',
        '                            f"Failed to process audio for chunk: \'{chunk_text[:100]}...\'. Error: {str(e)}"\n'
        '                        )\n                        raise')
    source = replace(source,
        '                    logger.error(f"Failed to finalize audio stream: {str(e)}")',
        '                    logger.error(f"Failed to finalize audio stream: {str(e)}")\n                    raise')
    source = replace(source,
        '            combined_audio_data = AudioChunk.combine(audio_data_chunks)',
        '            if not audio_data_chunks:\n'
        '                raise RuntimeError("No audio generated for the requested text")\n'
        '            combined_audio_data = AudioChunk.combine(audio_data_chunks)')
    return source


def patch_backend(source: str) -> str:
    source = replace(source,
        '                                        continue\n                                    if not token.text',
        '                                        raise RuntimeError("Incomplete word timestamp metadata")\n                                    if not token.text')
    source = replace(source,
        '                                    f"Failed to process timestamps for chunk: {e}"\n                                )',
        '                                    f"Failed to process timestamps for chunk: {e}"\n                                )\n                                raise')
    source = replace(source,
        '                    yield AudioChunk(\n                        result.audio.numpy(), word_timestamps=word_timestamps',
        '                    if return_timestamps and pipeline_lang_code in ("a", "b") and not word_timestamps:\n'
        '                        raise RuntimeError("English word timestamps were requested but not generated")\n'
        '                    yield AudioChunk(\n                        result.audio.numpy(), word_timestamps=word_timestamps')
    source = replace(source,
        '                    logger.warning("No audio in chunk")',
        '                    raise RuntimeError("No audio in model chunk")', count=2)
    source = replace(source,
        '                async for chunk in self.generate(text, voice, speed, lang_code):\n                    yield chunk',
        '                # The caller retries the entire non-streaming request. Retrying here\n'
        '                # can duplicate chunks already yielded and recurse indefinitely.')
    source = replace(source,
        '                async for chunk in self.generate_from_tokens(\n'
        '                    tokens, voice, speed, lang_code\n                ):\n                    yield chunk',
        '                # Propagate OOM; callers must retry the whole request.')
    return source


def build_changes(root: Path, performance: bool = False) -> dict[Path, bytes]:
    names = ["services/tts_service.py", "inference/kokoro_v1.py", "routers/development.py"]
    if performance:
        names += ["services/audio.py", "inference/base.py"]
    originals = {}
    for name in names:
        path = root / name
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != UPSTREAM_HASHES[name]:
            raise ValueError(f"Refusing unknown or already modified upstream source: {path}")
        originals[name] = data.decode("utf-8")
    changed = {
        "services/tts_service.py": patch_service(originals["services/tts_service.py"]),
        "inference/kokoro_v1.py": patch_backend(originals["inference/kokoro_v1.py"]),
    }
    if performance:
        from performance_patch import patch_audio, patch_combine
        changed["services/audio.py"] = patch_audio(originals["services/audio.py"])
        changed["inference/base.py"] = patch_combine(originals["inference/base.py"])
    code_hash = hashlib.sha256(json.dumps(changed, sort_keys=True).encode("utf-8")).hexdigest()
    revision = f"{UPSTREAM_DIGEST}/epub-tts-v1/{code_hash}/perf-{int(performance)}"
    changed["routers/development.py"] = originals["routers/development.py"] + (
        '\n\n@router.get("/epub-tts/revision")\n'
        'async def epub_tts_revision():\n'
        '    """Stable cache identity for this pinned, unmodified derivative image."""\n'
        f'    return {{"revision": {revision!r}, "captioned_pcm": True, "fail_fast": True, '
        f'"performance": {performance!r}}}\n'
    )
    for name, source in changed.items():
        ast.parse(source, filename=name)
    return {root / name: source.encode("utf-8") for name, source in changed.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/app/api/src"))
    parser.add_argument("--performance", action="store_true", help="Also apply separately tested stage 4 CPU optimizations")
    parser.add_argument("--check", action="store_true", help="Validate source and patch contexts without writing")
    args = parser.parse_args()
    changes = build_changes(args.root, args.performance)
    for path, data in changes.items():
        if not args.check:
            path.write_bytes(data)
        print(f"{'checked' if args.check else 'patched'} {path} {hashlib.sha256(data).hexdigest()}")


if __name__ == "__main__":
    main()
