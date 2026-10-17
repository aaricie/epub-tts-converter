"""Read-only comparison of the two user-supplied narrated EPUB archives."""
import hashlib
import io
import json
from pathlib import Path
import time
import zipfile
from xml.etree import ElementTree as ET
import av

ROOT=Path(__file__).resolve().parent
DOWNLOADS=Path('C:/Users/Aricie/Downloads')
PATHS=[DOWNLOADS/'Peter; Longerich - Hitler_audio.epub',
       DOWNLOADS/'Peter; Longerich - Hitler_audio-48kbps.epub']

def main():
    result={"archives":[],"non_audio_comparison":{},"sample_tracks":[]}
    with zipfile.ZipFile(PATHS[0]) as original,zipfile.ZipFile(PATHS[1]) as small:
        for path,z in zip(PATHS,[original,small]):
            mp3=[i for i in z.infolist() if i.filename.endswith('.mp3')]
            smil=[i for i in z.infolist() if i.filename.endswith('.smil')]
            result['archives'].append({"path":str(path),"bytes":path.stat().st_size,
                "entries":len(z.infolist()),"mp3_tracks":len(mp3),"smil_files":len(smil),
                "mp3_uncompressed_bytes":sum(i.file_size for i in mp3),
                "mp3_zip_bytes":sum(i.compress_size for i in mp3),
                "mp3_compression_types":sorted(set(i.compress_type for i in mp3))})
        names0=set(original.namelist());names1=set(small.namelist())
        changed=[];unchanged=0
        for n in sorted(names0 & names1):
            if n.endswith('.mp3'): continue
            if hashlib.sha256(original.read(n)).digest()!=hashlib.sha256(small.read(n)).digest():changed.append(n)
            else: unchanged+=1
        result['non_audio_comparison']={"only_original":sorted(names0-names1),"only_small":sorted(names1-names0),
                                       "changed":changed,"identical_count":unchanged}
        tracks=sorted([i for i in original.infolist() if i.filename.endswith('.mp3')],key=lambda i:i.file_size)
        chosen=[tracks[0],tracks[len(tracks)//2],tracks[-1]]
        for track in chosen:
            pair={"name":track.filename,"versions":[]}
            for z in [original,small]:
                start=time.perf_counter()
                with z.open(track.filename) as source,av.open(source,format='mp3') as c:
                    s=c.streams.audio[0]
                    sr=s.sample_rate; bitrate=s.bit_rate
                    samples=sum(f.samples for f in c.decode(s))
                pair['versions'].append({"bytes":z.getinfo(track.filename).file_size,
                    "sample_rate":sr,"bit_rate":bitrate,"samples":samples,"seconds":samples/sr,
                    "decode_wall_s":time.perf_counter()-start})
            result['sample_tracks'].append(pair)
            print(json.dumps(pair),flush=True)
        result['savings_bytes']=PATHS[0].stat().st_size-PATHS[1].stat().st_size
        result['savings_pct']=100*result['savings_bytes']/PATHS[0].stat().st_size
    (ROOT/'audio_epub_comparison.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2),flush=True)
if __name__=='__main__': main()
