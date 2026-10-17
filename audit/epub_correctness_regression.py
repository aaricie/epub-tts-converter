"""Exercise all supplied books without TTS or writing converted EPUBs."""
import collections
import json
import pathlib
import sys
import time
import zipfile
from xml.etree import ElementTree as ET

BASE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import epub_tts_converter as c
from epub_migration import prepare_epub_for_conversion


def xml_error(data):
    try:
        ET.fromstring(data)
    except ET.ParseError as exc:
        return str(exc)
    return None


def scan(path):
    start = time.perf_counter()
    result = {'book': path.name, 'chapters': [], 'problems': []}
    with zipfile.ZipFile(path) as zf:
        root, manifest, spine, opf_dir = c.parse_opf(zf, c.find_opf_path(zf))
        # Inspect known malformed source chapters too. Production conversion uses
        # the default strict preflight and rejects them before requesting audio.
        removed, migrated = prepare_epub_for_conversion(zf, root, opf_dir, validate=False)
        result['removed_resources'] = len(removed)
        result['migrated_documents'] = len(migrated)
        for index, (_, href) in enumerate(c.resolve_spine_chapters(manifest, spine, opf_dir)[0], 1):
            source = zf.read(c.epub_path(opf_dir, href))
            data = migrated[c.epub_path(opf_dir, href)]
            soup, paragraphs = c.process_chapter(data, index)
            original_visible_text = (soup.body or soup).get_text()
            original_ids = collections.Counter(t['id'] for t in soup.find_all(id=True))
            chars = sum(len(p['text']) for p in paragraphs)
            if chars != c.count_chapter_characters(data):
                result['problems'].append([href, 'count-only extraction differs'])
            seen_positions = set()
            for paragraph_index, para in enumerate(paragraphs, 1):
                if len(para['text']) != len(para['display_map']):
                    result['problems'].append([href, 'display-map length mismatch'])
                for loc in para['display_map']:
                    if loc is None:
                        continue
                    key = (id(loc[0]), loc[1])
                    if key in seen_positions:
                        result['problems'].append([href, 'source text narrated twice'])
                        break
                    seen_positions.add(key)
                # Force either a mapped word or a safe marker on inline splits.
                token = para['tts_text'].split()[0]
                c.inject_word_spans(soup, para, [{'type': 'wordboundary', 'offset': 0,
                                    'duration': 100, 'text': token}], index, paragraph_index)
            if (soup.body or soup).get_text() != original_visible_text:
                result['problems'].append([href, 'word wrapping changed visible text'])
            final_ids = collections.Counter(t['id'] for t in soup.find_all(id=True))
            if any(count > max(1, original_ids[ident]) for ident, count in final_ids.items()):
                result['problems'].append([href, 'introduced duplicate XHTML ID'])
            original_error = xml_error(source)
            output_error = xml_error(c._to_xhtml_bytes(soup))
            if output_error and not original_error:
                result['problems'].append([href, 'introduced malformed XML: ' + output_error])
            result['chapters'].append({'href': href, 'paragraphs': len(paragraphs),
                                       'characters': chars, 'source_xml_error': original_error,
                                       'result_xml_error': output_error})
        result['ncx_top_level_items'] = len(c._nav_info_from_ncx(zf, manifest, opf_dir))
    result['seconds'] = round(time.perf_counter() - start, 3)
    return result


if __name__ == '__main__':
    results = []
    for path in sorted((BASE / 'test books').glob('*.epub')):
        try:
            result = scan(path)
        except Exception as exc:
            result = {'book': path.name, 'problems': [repr(exc)]}
        results.append(result)
        print(json.dumps({'book': result['book'], 'chapters': len(result.get('chapters', [])),
                          'problems': result['problems'], 'seconds': result.get('seconds')},
                         ensure_ascii=True), flush=True)
        (BASE / 'audit' / 'epub_correctness_regression.json').write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    sys.exit(any(result['problems'] for result in results))
