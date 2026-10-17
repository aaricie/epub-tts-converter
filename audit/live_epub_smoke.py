"""One tiny real Kokoro->EPUB conversion, using only synthetic text."""
import io
import json
from pathlib import Path
import sys
import time
import zipfile
from xml.etree import ElementTree as ET
import av
from bs4 import BeautifulSoup

root=Path(__file__).resolve().parent
sys.path.insert(0,str(root.parent))
import epub_tts_converter as conv

input_path=root/'synthetic_input.epub';output_path=root/'synthetic_kokoro_output.epub'
html='''<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Audit</title></head><body>
<h1>A small example</h1>
<p>The library opened in 1945. Dr. Green paid $14.50 for three books.</p>
<p>At the desk, a reader said, "Yes." Then everyone returned to their work.</p>
<p>No.</p><ul><li>This list item should also be read aloud.</li></ul>
</body></html>'''
with zipfile.ZipFile(input_path,'w') as z:
    z.writestr('mimetype','application/epub+zip',compress_type=zipfile.ZIP_STORED)
    z.writestr('META-INF/container.xml','''<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>''')
    z.writestr('content.opf','''<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="book-id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="book-id">audit-synthetic</dc:identifier><dc:title>Audit</dc:title><dc:language>en</dc:language></metadata><manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="chapter"/></spine></package>''')
    z.writestr('chapter.xhtml',html)
logs=[]
conv.set_tts_backend('kokoro')
start=time.perf_counter()
conv.convert_epub_to_epub3(str(input_path),str(output_path),'af_heart','+0%',1,logs.append,
                          audio_format='kokoro-mp3',bytes_per_ms=16,bitrate_kbps=128,
                          para_pause_ms=450)
elapsed=time.perf_counter()-start
with zipfile.ZipFile(output_path) as z:
    soup=BeautifulSoup(z.read('chapter.xhtml'),'lxml')
    spans=soup.find_all('span',id=lambda s:s and s.startswith('mo_'))
    empty=[s['id'] for s in spans if not s.get_text()]
    audio=z.read('Audio/ch001.mp3')
    with av.open(io.BytesIO(audio)) as c:
        stream=c.streams.audio[0]
        samples=sum(f.samples for f in c.decode(stream));duration=samples/stream.sample_rate
    def seconds(clock):
        parts=clock.split(':');return float(parts[0])*3600+float(parts[1])*60+float(parts[2])
    smil=ET.fromstring(z.read('SMIL/ch001.smil'))
    clips=[(seconds(a.get('clipBegin')),seconds(a.get('clipEnd'))) for a in smil.iter('{http://www.w3.org/ns/SMIL}audio')]
    result={'wall_s':elapsed,'decoded_audio_s':duration,'byte_inferred_audio_s':len(audio)/16000,
            'clip_duration_sum_s':sum(e-b for b,e in clips),'clips':len(clips),
            'empty_highlight_markers':len(empty),'empty_ids':empty,'narrated_text_from_spans':' '.join(s.get_text() for s in spans),
            'original_visible_text':BeautifulSoup(html,'lxml').body.get_text(' ',strip=True),
            'logs':logs}
(root/'live_epub_smoke.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
print(json.dumps(result,indent=2))
