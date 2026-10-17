"""Bounded, synthetic-text benchmarks of the user's already-running local API."""
import argparse
import base64
import concurrent.futures
import io
import json
from pathlib import Path
import time
import urllib.request
import av

OUT = Path(__file__).resolve().parent
TEXTS = [
    "The old library stood beside a quiet square. Every morning, the caretaker opened the shutters and arranged the returned books on a wooden table. Visitors came to read, study, and exchange stories about the town. By noon, sunlight reached the farthest shelves, and the room felt warm and welcoming.",
    "Our experiment compares several ways of producing spoken audio. We use a small collection of ordinary paragraphs, keep the voice and reading speed the same, and measure the time needed to finish every request. This makes the results easier to compare, although a complete book will contain much more varied material.",
    "Beyond the village, a narrow path led through the forest. The travelers followed it until they reached a bridge over the river. There they stopped for lunch and watched the water moving around the stones. When the rain finally passed, they packed their bags and continued toward the mountains.",
    "A useful computer program should report its progress honestly. If an operation fails, the user needs to know what remains unfinished. Saving completed work can make a long task easier to recover after an interruption. Clear messages and reliable output are often more valuable than a complicated collection of options.",
]

def call(text, fmt="mp3", caption=True):
    payload = {"model":"kokoro", "input":text, "voice":"af_heart", "speed":1.0,
               "response_format":fmt, "stream":False}
    if caption:
        payload["return_timestamps"] = True
    path = "/dev/captioned_speech" if caption else "/v1/audio/speech"
    req = urllib.request.Request("http://localhost:8880" + path,
        data=json.dumps(payload).encode(), headers={"Content-Type":"application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=120) as response:
        raw = response.read()
    elapsed = time.perf_counter() - t0
    metadata = json.loads(raw) if caption else {}
    audio = base64.b64decode(metadata["audio"]) if caption else raw
    if fmt == "pcm":
        samples, sr, bit_rate = len(audio)//2, 24000, 384000
    else:
        with av.open(io.BytesIO(audio)) as c:
            s=c.streams.audio[0]
            sr=s.sample_rate
            bit_rate=s.bit_rate
            samples=sum(f.samples for f in c.decode(s))
    stamps=metadata.get("timestamps") or []
    result={"elapsed_s":elapsed, "audio_s":samples/sr, "samples":samples,
            "sample_rate":sr, "bit_rate":bit_rate, "audio_bytes":len(audio),
            "response_bytes":len(raw), "timestamp_count":len(stamps),
            "last_timestamp":stamps[-1] if stamps else None}
    return result,audio,metadata

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--phase",choices=["workers","formats","batching"],default="workers")
    args=parser.parse_args()
    rows=[]
    def save(row):
        rows.append(row)
        (OUT / ("benchmark_"+args.phase+".json")).write_text(json.dumps(rows,indent=2),encoding="utf-8")
        print(json.dumps(row),flush=True)
    if args.phase=="workers":
        warm=call("This is a short warm up for the audio performance measurement.")[0]
        save({"case":"warmup",**warm})
        for repeat,workers in enumerate([1,2,4,4,2,1]):
            start=time.perf_counter()
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                results=list(pool.map(lambda t:call(t)[0],TEXTS))
            elapsed=time.perf_counter()-start
            seconds=sum(x["audio_s"] for x in results)
            save({"case":"parallel", "run":repeat+1, "workers":workers,
                  "wall_s":elapsed, "audio_s":seconds, "realtime_multiple":seconds/elapsed,
                  "requests":results})
    elif args.phase=="formats":
        for caption,fmt in [(True,"mp3"),(True,"pcm"),(False,"mp3"),(False,"pcm"),
                            (False,"pcm"),(False,"mp3"),(True,"pcm"),(True,"mp3")]:
            result,audio,meta=call(TEXTS[1],fmt,caption)
            name=f"sample_{'caption' if caption else 'plain'}_{fmt}"
            (OUT/(name+"."+fmt)).write_bytes(audio)
            if meta:
                meta.pop("audio",None)
                (OUT/(name+".json")).write_text(json.dumps(meta,indent=2),encoding="utf-8")
            save({"case":"format", "format":fmt, "caption":caption,**result})
    else:
        for mode in ["separate","batched","batched","separate"]:
            start=time.perf_counter()
            texts=TEXTS if mode=="separate" else ["\n\n".join(TEXTS)]
            results=[call(t)[0] for t in texts]
            elapsed=time.perf_counter()-start
            save({"case":mode,"wall_s":elapsed,"audio_s":sum(x['audio_s'] for x in results),"requests":results})

if __name__=="__main__":
    main()
