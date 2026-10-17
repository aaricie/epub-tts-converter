"""Small real Kokoro -> chapter MP3 -> EPUB acceptance fixture for BookFusion."""
import argparse
import io
import json
from pathlib import Path
import sys
import time
import zipfile
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import av
import epub_tts_converter as conv


def create_book(path):
    chapters = [
        '<h1 id="chapter-one">A walk by the river</h1>'
        '<p>The morning was quiet. A light breeze moved through the trees, and the river reflected the pale sky.</p>'
        '<p>"Yes," she said. "We have time to walk a little farther." He closed his notebook and followed her along the path.</p>'
        '<ul><li>Look for the small wooden bridge.</li><li>Turn left where the path meets the water.</li></ul>'
        '<p>At the bridge, they stopped. The sound of the current was gentle, but steady. They watched a leaf drift downstream.</p>'
        '<div>This paragraph lives directly inside a division. It should be read after the list, without losing any words.</div>'
        '<p>No.</p><p>That was not the end of the conversation.</p>',
        '<h1 id="chapter-two">Numbers, pauses, and a long passage</h1>'
        '<p>In 1945, Dr. Smith bought a notebook for $14.50. The receipt was dated December 3rd. These words test spoken normalization.</p>'
        '<p>First, a short pause. Next, a longer thought: the timing should follow the voice through the spaces between words, '
        'and through the silence between paragraphs.</p>'
        '<p>' + ' '.join([
            'The path continued beyond the bridge, passing a field where the grass was still wet with morning dew.',
            'A cyclist rang a bell as she passed, and the two walkers stepped aside with a wave.',
            'They talked about ordinary things: the weather, the changing season, and the books they planned to read.',
            'Neither was in a hurry, so they took the longer route beneath the old trees.',
            'Near the bend in the river, the sound of traffic faded and birds could be heard in the branches.',
            'One of them pointed toward a little house on the opposite bank, half hidden by the leaves.',
            'The other remembered visiting it years before, when the garden had been full of bright yellow flowers.',
            'They paused again to look across the water, then resumed their walk in comfortable silence.',
            'A cloud passed over the sun, softening the light on the surface of the river.',
            'The breeze grew a little stronger and sent a ripple through the tall grass beside the path.',
            'At last they reached a bench, where they sat down to rest and share the lunch they had brought.',
            'It was a simple morning, and that was exactly what they had hoped for.',
            'When they returned home, they would remember the bridge and the quiet sound of moving water.',
            'For now, they stayed where they were, watching the light change above the trees.',
            'The final words of this longer passage should remain audible, even when playback reaches the end of the chapter.'
        ]) + '</p><p>This is the final sentence. Let the full audio tail play.</p>',
    ]
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        archive.writestr('META-INF/container.xml', '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="EPUB/book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        archive.writestr('EPUB/book.opf', '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="bookid">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="bookid">urn:uuid:ac007783-403c-4fc5-a909-240e2b28f3aa</dc:identifier>'
            '<dc:title>BookFusion synchronization check</dc:title><dc:language>en</dc:language><dc:creator>EPUB TTS test fixture</dc:creator></metadata>'
            '<manifest><item id="ch1" href="Text/ch1.xhtml" media-type="application/xhtml+xml"/><item id="ch2" href="Text/ch2.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/></manifest><spine toc="ncx"><itemref idref="ch1"/><itemref idref="ch2"/></spine></package>')
        archive.writestr('EPUB/toc.ncx', '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head><meta name="dtb:uid" content="urn:uuid:ac007783-403c-4fc5-a909-240e2b28f3aa"/></head>'
            '<docTitle><text>BookFusion synchronization check</text></docTitle><navMap>'
            '<navPoint id="n1" playOrder="1"><navLabel><text>A walk by the river</text></navLabel><content src="Text/ch1.xhtml#chapter-one"/></navPoint>'
            '<navPoint id="n2" playOrder="2"><navLabel><text>Numbers and pauses</text></navLabel><content src="Text/ch2.xhtml#chapter-two"/></navPoint></navMap></ncx>')
        for index, chapter in enumerate(chapters, 1):
            archive.writestr(f'EPUB/Text/ch{index}.xhtml', '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter '+str(index)+'</title></head><body>'+chapter+'</body></html>')


def clock_ms(value):
    hours, minutes, seconds = value.split(':')
    return (int(hours)*3600 + int(minutes)*60 + float(seconds))*1000


def check_book(path):
    result = []
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        for name in archive.namelist():
            if not name.endswith('.smil'):
                continue
            root = ET.fromstring(archive.read(name))
            audio = root.findall('.//{http://www.w3.org/ns/SMIL}audio')
            assert clock_ms(audio[0].get('clipBegin')) == 0
            assert all(a.get('clipEnd') == b.get('clipBegin') for a,b in zip(audio,audio[1:]))
            audio_path = conv.epub_path(str(Path(name).parent).replace('\\','/'), audio[0].get('src'))
            data = archive.read(audio_path)
            with av.open(io.BytesIO(data)) as container:
                stream = container.streams.audio[0]
                samples = sum(frame.samples for frame in container.decode(audio=0))
                rate = stream.codec_context.sample_rate
            duration = samples * 1000 / rate
            assert abs(duration-clock_ms(audio[-1].get('clipEnd'))) < 1
            result.append(dict(smil=name, mp3=audio_path, clips=len(audio), bytes=len(data), samples=samples, sample_rate=rate, duration_ms=duration))
    return result


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--url',default='http://localhost:8881')
    parser.add_argument('--batch',action='store_true')
    args=parser.parse_args()
    directory=ROOT/'audit'/'implementation'/'acceptance'
    directory.mkdir(parents=True,exist_ok=True)
    source=directory/'bookfusion_check_source.epub'
    output=directory/('bookfusion_check_batched_48kbps.epub' if args.batch else 'bookfusion_check_48kbps.epub')
    if not source.exists(): create_book(source)
    started=time.perf_counter()
    conv.convert_epub_to_epub3(str(source),str(output),'af_heart','+0%',1,
        lambda message: print(message,flush=True),para_pause_ms=350,bitrate_kbps=48,
        backend={'provider':'kokoro','kokoro_url':args.url,'batch_paragraphs':args.batch})
    report={'elapsed_seconds':time.perf_counter()-started,'output':str(output),'chapters':check_book(output)}
    (directory/('acceptance_batch.json' if args.batch else 'acceptance.json')).write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))
