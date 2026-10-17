"""Encode one captured synthetic PCM sample; no change to converter or server."""
import io
import json
from pathlib import Path
import time
import av
import numpy as np

root=Path(__file__).resolve().parent
samples=np.frombuffer((root/'sample_caption_pcm.pcm').read_bytes(),dtype='<i2')
results=[]
for repeat in range(3):
    for bitrate in [128000,48000]:
        buffer=io.BytesIO()
        start=time.perf_counter()
        with av.open(buffer,mode='w',format='mp3') as container:
            stream=container.add_stream('libmp3lame',rate=24000,layout='mono')
            stream.bit_rate=bitrate
            for offset in range(0,len(samples),24000):
                frame=av.AudioFrame.from_ndarray(samples[offset:offset+24000].reshape(1,-1),format='s16',layout='mono')
                frame.sample_rate=24000
                frame.pts=offset
                for packet in stream.encode(frame): container.mux(packet)
            for packet in stream.encode(None): container.mux(packet)
        elapsed=time.perf_counter()-start
        output=buffer.getvalue()
        with av.open(io.BytesIO(output),format='mp3') as check:
            decoded=sum(f.samples for f in check.decode(audio=0))
        row={'repeat':repeat+1,'bitrate':bitrate,'encode_wall_s':elapsed,'output_bytes':len(output),
             'source_samples':len(samples),'decoded_samples':decoded,'seconds':len(samples)/24000}
        results.append(row)
        print(json.dumps(row),flush=True)
        if repeat==0: (root/f'pcm_encoded_{bitrate//1000}.mp3').write_bytes(output)
(root/'encode_benchmark.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
