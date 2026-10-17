"""Independent one-off profile, leaving the running API and its settings alone."""
import json
from pathlib import Path
import subprocess
from benchmark_kokoro import TEXTS

script = '''
import asyncio, cProfile, io, json, pstats, sys, time
import numpy as np
import torch
from loguru import logger
logger.remove()
logger.add(sys.stderr,level="WARNING")
from api.src.inference.model_manager import get_manager
from api.src.inference.voice_manager import get_manager as get_voice_manager
from api.src.services.tts_service import TTSService
from api.src.services.streaming_audio_writer import StreamingAudioWriter
from api.src.services.audio import AudioService
from api.src.inference.base import AudioChunk

TEXT = __TEXT__
async def main():
    manager=await get_manager()
    voices=await get_voice_manager()
    await manager.initialize_with_warmup(voices)
    service=await TTSService.create()
    async def run():
        writer=StreamingAudioWriter('mp3',24000)
        t0=time.perf_counter()
        chunk=await service.generate_audio(TEXT,'af_heart',writer,return_timestamps=True)
        t1=time.perf_counter()
        result=await AudioService.convert_audio(chunk,'mp3',writer,trim_audio=False)
        final=await AudioService.convert_audio(AudioChunk(np.array([],dtype=np.int16)),'mp3',writer,is_last_chunk=True)
        t2=time.perf_counter()
        writer.close()
        return {'generation_s':t1-t0,'encode_s':t2-t1,'total_s':t2-t0,'audio_s':len(chunk.audio)/24000,'timestamps':len(chunk.word_timestamps)}
    print('VERSIONS',json.dumps({'torch':torch.__version__,'cuda':torch.version.cuda,'threads':torch.get_num_threads(),'interop':torch.get_num_interop_threads(),'gpu':torch.cuda.get_device_name()}),flush=True)
    await run()
    for threads in [4,2,1,1,2,4]:
        torch.set_num_threads(threads)
        print('THREADS',threads,json.dumps(await run()),flush=True)
    prof=cProfile.Profile()
    prof.enable()
    row=await run()
    prof.disable()
    print('PROFILE_RESULT',json.dumps(row),flush=True)
    stats=pstats.Stats(prof).strip_dirs().sort_stats('cumulative')
    stats.print_stats(45)
    stats.sort_stats('tottime').print_stats(30)
    manager.unload_all()
asyncio.run(main())
'''.replace('__TEXT__',repr(TEXTS[1]))

result=subprocess.run(['docker','exec','-i','kokoro-fastapi','/app/.venv/bin/python','-'],
                      input=script,text=True,encoding='utf-8',capture_output=True)
out=Path(__file__).resolve().parent/'engine_profile.txt'
out.write_text(result.stdout+'\nSTDERR\n'+result.stderr,encoding='utf-8')
print(result.stdout)
print(result.stderr[-3500:])
raise SystemExit(result.returncode)
