"""Run endpoint fault injection in an isolated Python process inside a container.

No model is loaded and no running API state or source file is modified.
"""
import argparse
import json
from pathlib import Path
import subprocess

SCRIPT = r'''
import asyncio, json
from types import SimpleNamespace
import numpy as np
from fastapi import HTTPException
from loguru import logger
logger.remove()
from api.src.routers import development
from api.src.services import tts_service
from api.src.inference.base import AudioChunk
from api.src.inference.kokoro_v1 import KokoroV1
from api.src.structures import CaptionedSpeechRequest, WordTimestamp

async def main():
    results = []
    for failure in ('none', 'after_good_model_chunk', 'second_text_chunk', 'empty_model'):
        calls = []
        service = tts_service.TTSService()
        async def split(*args, **kwargs):
            yield 'first', [1], None
            if failure == 'second_text_chunk':
                yield 'second', [2], None
        async def voices(voice):
            return voice, 'unused.pt'
        async def generate(text, *args, **kwargs):
            calls.append(text)
            if failure == 'second_text_chunk' and text == 'second':
                raise RuntimeError('injected failure on second text chunk')
            if failure == 'empty_model':
                return
            audio = (np.sin(np.arange(24000, dtype=np.float32) / 20) * 0.3).astype(np.float32)
            yield AudioChunk(audio, [WordTimestamp(word=text, start_time=0.0, end_time=0.8)])
            if failure == 'after_good_model_chunk':
                raise RuntimeError('injected failure after successful audio')
        async def get_service():
            return service
        async def validate_voice(voice, svc):
            return voice
        service.model_manager = SimpleNamespace(get_backend=lambda: object.__new__(KokoroV1), generate=generate)
        service._get_voices_path = voices
        tts_service.smart_split = split
        development.get_tts_service = get_service
        development.process_and_validate_voices = validate_voice
        request = CaptionedSpeechRequest(input='first second', voice='af_heart', response_format='pcm', stream=False, return_timestamps=True)
        try:
            response = await development.create_captioned_speech(request, None)
            content = json.loads(response.body)
            result = {'case':failure, 'status':response.status_code, 'audio_b64_length':len(content.get('audio','')), 'calls':calls}
        except HTTPException as exc:
            result = {'case':failure, 'status':exc.status_code, 'detail':exc.detail, 'calls':calls}
        results.append(result)
    print(json.dumps(results))
    assert results[0]['status'] == 200
    assert all(row['status'] == 500 for row in results[1:]), 'A failed request returned success'
asyncio.run(main())
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", default="epub-kokoro-reliable-test")
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("server_failure_results.json"))
    args = parser.parse_args()
    run = subprocess.run(["docker", "exec", "-i", args.container, "/app/.venv/bin/python", "-"],
                         input=SCRIPT, text=True, encoding="utf-8", capture_output=True)
    output = {"container": args.container, "returncode": run.returncode, "stdout": run.stdout, "stderr": run.stderr}
    args.output.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(run.stdout)
    if run.returncode:
        print(run.stderr[-5000:])
    raise SystemExit(run.returncode)


if __name__ == "__main__":
    main()
