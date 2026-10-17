import io
import zipfile
from xml.etree import ElementTree as ET

import pytest

import epub_tts_converter as conv
from epub_migration import MANAGED_META, migrate_opf, prepare_epub_for_conversion


def _chapter(body):
    return ('<html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Title</title>'
            '</head><body>' + body + '</body></html>').encode()


def test_mixed_blocks_preserve_order_and_do_not_repeat_descendants():
    source = _chapter('<p>First.</p><div>Before<em> inline</em><div>Inner.</div>'
                      'After.</div><ul><li>A.</li><li><p>B.</p>Tail.</li></ul>'
                      '<table><tr><th>Year</th><td>1945</td></tr></table>'
                      '<figure><figcaption>Caption.</figcaption></figure>')
    soup, paragraphs = conv.process_chapter(source, 1)
    assert [p['text'] for p in paragraphs] == [
        'First.', 'Before inline', 'Inner.', 'After.', 'A.', 'B.', 'Tail.',
        'Year', '1945', 'Caption.']
    assert len({p['id'] for p in paragraphs}) == len(paragraphs)
    assert conv.count_chapter_characters(source) == sum(len(p['text']) for p in paragraphs)
    assert soup.em.get_text() == ' inline'
    # Every mapped source position is used at most once.
    locations = [(id(n), offset) for p in paragraphs for loc in p['display_map']
                 if loc is not None for n, offset in [loc]]
    assert len(locations) == len(set(locations))


def test_short_numeric_unicode_and_inline_join_are_spoken():
    _, paragraphs = conv.process_chapter(_chapter(
        '<p>Yes.</p><p>No.</p><p>A</p><p>123</p><p>日本語</p><p>हिन्दी</p>'
        '<p>E<span>NGLAND</span>.</p><p>***</p>'), 1)
    assert [p['text'] for p in paragraphs] == ['Yes.', 'No.', 'A', '123', '日本語', 'हिन्दी', 'ENGLAND.']


def test_notes_comments_and_hidden_regions_stay_visible_but_are_not_spoken():
    body = ('<p>Hello <!-- do not speak -->world<a epub:type="noteref" href="#note">1</a>.</p>'
            '<aside epub:type="footnote" id="note"><p>Footnote.</p></aside>'
            '<section role="doc-endnotes"><p>Endnotes.</p></section>'
            '<p hidden="hidden">Hidden.</p><div style="display: none"><p>Invisible.</p></div>'
            '<script>bad()</script><p>Final.</p>')
    soup, paragraphs = conv.process_chapter(_chapter(body), 1)
    assert [p['text'] for p in paragraphs] == ['Hello world.', 'Final.']
    assert soup.find(id='note').get_text() == 'Footnote.'
    assert 'do not speak' in str(soup)


def test_display_text_map_uses_block_boundaries():
    soup = conv.BeautifulSoup('<div>Before<div>Inside</div>After</div>', 'lxml')
    text, mapping = conv._display_text_and_map(soup.div)
    assert text == 'Before Inside After'
    assert len(text) == len(mapping)
    assert mapping[6] is None and mapping[13] is None


def test_legacy_note_backlinks_are_explicitly_excluded():
    source = _chapter('<p>Story<a id="Ref-en1" href="notes.html#en1">1</a>.</p>'
                      '<div class="end"><a id="en1" href="story.html#Ref-en1">1</a>Note.</div>'
                      '<div class="end">The end of the story.</div>')
    soup, paragraphs = conv.process_chapter(source, 1)
    assert [p['text'] for p in paragraphs] == ['Story.', 'The end of the story.']
    assert 'Note.' in soup.get_text()


def test_authored_id_collision_keeps_generated_targets_unique():
    soup, paragraphs = conv.process_chapter(_chapter(
        '<p id="mo_ch001p0001w0001">Hello.</p><p>Second.</p>'), 1)
    records = conv.inject_word_spans(soup, paragraphs[0], [
        {'type': 'wordboundary', 'offset': 0, 'duration': 100, 'text': 'Hello'}], 1, 1)
    assert records[0]['id'] != 'mo_ch001p0001w0001'
    ids = [tag['id'] for tag in soup.find_all(id=True)]
    assert len(ids) == len(set(ids))


def test_ncx_keeps_encoded_fragments_and_nested_destinations():
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.writestr('EPUB/TOC/book.ncx', '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/">'
                    '<navMap><navPoint><navLabel><text>Parent</text></navLabel>'
                    '<content src="../Text/ch%201.xhtml#part%202"/>'
                    '<navPoint><navLabel><text>Child</text></navLabel>'
                    '<content src="../Text/ch%201.xhtml#child"/></navPoint>'
                    '</navPoint></navMap></ncx>')
    with zipfile.ZipFile(archive) as zf:
        info = conv._nav_info_from_ncx(zf, {'ncx': {'href': 'TOC/book.ncx',
                                         'media-type': 'application/x-dtbncx+xml'}}, 'EPUB')
    assert info[0]['href'] == 'Text/ch%201.xhtml#part%202'
    assert info[0]['children'][0]['href'].endswith('#child')
    nav = ET.fromstring(conv._generate_nav_xhtml(info))
    ns = {'x': 'http://www.w3.org/1999/xhtml'}
    assert nav.find('.//x:li/x:ol/x:li/x:a', ns).text == 'Child'


def _opf(extra_metadata='', extra_manifest='', refs='<itemref idref="ch"/>'):
    return ET.fromstring('<package xmlns="http://www.idpf.org/2007/opf" version="2.0"'
                         ' unique-identifier="book"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"'
                         ' xmlns:opf="http://www.idpf.org/2007/opf">'
                         '<dc:identifier id="book" opf:scheme="ISBN">123</dc:identifier>'
                         '<dc:title>Title</dc:title><dc:language>en</dc:language>'
                         + extra_metadata + '</metadata><manifest>'
                         '<item id="ch" href="Text/ch.xhtml" media-type="application/xhtml+xml"/>'
                         + extra_manifest + '</manifest><spine>' + refs + '</spine></package>')


def test_opf_migrates_metadata_and_deduplicates_resolved_spine():
    root = _opf('<dc:creator opf:file-as="Doe, Jane" opf:role="aut">Jane Doe</dc:creator>'
                '<dc:date opf:event="publication">2000</dc:date>'
                '<dc:date opf:event="creation">1999</dc:date>',
                '<item id="alias" href="Text/../Text/ch.xhtml#part" media-type="application/xhtml+xml"/>',
                '<itemref idref="ch"/><itemref idref="ch"/><itemref idref="alias"/>')
    migrate_opf(root)
    ns = {'o': conv.OPF_NS, 'dc': 'http://purl.org/dc/elements/1.1/'}
    assert len(root.findall('o:spine/o:itemref', ns)) == 1
    assert len(root.findall('o:metadata/dc:date', ns)) == 1
    assert root.find(".//o:meta[@property='file-as']", ns).text == 'Doe, Jane'
    assert root.find(".//o:meta[@property='role']", ns).get('scheme') == 'marc:relators'
    assert root.find(".//o:meta[@property='identifier-type']", ns).text == 'ISBN'
    assert root.find(".//o:meta[@property='dcterms:created']", ns).text == '1999'
    before = ET.tostring(root)
    migrate_opf(root)
    assert ET.tostring(root) == before


def test_html_presentation_migration_preserves_css_precedence_and_adds_highlight():
    soup, _ = conv.process_chapter(_chapter('<p>Hello.</p><table width="80%">'
        '<tr><td valign="top" style="vertical-align:middle">A.</td></tr></table>'
        '<img src="a.jpg" width="61%" height="200"/>'), 1)
    assert soup.table.get('width') is None
    assert 'width:80%' in soup.table['style']
    assert soup.td.get('valign') is None
    assert soup.td['style'].endswith('vertical-align:middle')
    assert soup.img.get('width') is None and soup.img['height'] == '200'
    assert '.-epub-media-overlay-active' in soup.style.decode_contents()


def test_semantic_subsections_do_not_skip_entire_mixed_book():
    source = _chapter('<nav epub:type="toc"><p>Contents.</p></nav><section><h1>Chapter</h1>'
                      '<p>Story.</p></section><section epub:type="endnotes"><p>Notes.</p></section>')
    assert not conv._is_frontmatter(source, 'book.xhtml')
    assert not conv._is_backmatter(source, 'book.xhtml')


def _narrated_archive(author_overlay=False, shared_audio=False):
    overlay = 'author-overlay' if author_overlay else 'overlay-ch001'
    root = _opf(extra_manifest=(f'<item id="{overlay}" href="SMIL/ch001.smil" media-type="application/smil+xml"/>'
                  '<item id="audio-ch001" href="Audio/ch001.mp3" media-type="audio/mpeg"/>'
                  '<item id="unlinked" href="SMIL/author.smil" media-type="application/smil+xml"/>'))
    root.find(f'{{{conv.OPF_NS}}}manifest/{{{conv.OPF_NS}}}item').set('media-overlay', overlay)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.writestr('EPUB/Text/ch.xhtml', _chapter('<p id="mo_ch001p0001">'
                    '<span id="mo_ch001p0001w0001">Hello.</span><em> Still styled.</em></p>'))
        zf.writestr('EPUB/SMIL/ch001.smil', '<smil xmlns="http://www.w3.org/ns/SMIL"><body><seq><par>'
                    '<text src="../Text/ch.xhtml#mo_ch001p0001w0001"/>'
                    '<audio src="../Audio/ch001.mp3"/></par></seq></body></smil>')
        zf.writestr('EPUB/Audio/ch001.mp3', b'audio')
        zf.writestr('EPUB/SMIL/author.smil', '<smil xmlns="http://www.w3.org/ns/SMIL">'
                    + ('<audio src="../Audio/ch001.mp3"/>' if shared_audio else '') + '</smil>')
    archive.seek(0)
    return archive, root


def test_reprocess_removes_only_proven_converter_resources_and_word_wrappers():
    archive, root = _narrated_archive()
    with zipfile.ZipFile(archive) as zf:
        removed, replacements = prepare_epub_for_conversion(zf, root, 'EPUB')
    assert removed == {'EPUB/Audio/ch001.mp3', 'EPUB/SMIL/ch001.smil'}
    assert b'mo_ch001p0001w0001' not in replacements['EPUB/Text/ch.xhtml']
    assert b'<em> Still styled.</em>' in replacements['EPUB/Text/ch.xhtml']
    assert root.find(f".//{{{conv.OPF_NS}}}item[@id='unlinked']") is not None
    assert root.find(f".//{{{conv.OPF_NS}}}item[@id='ch']").get('media-overlay') is None


def test_new_ownership_marker_preserves_authored_converter_looking_span_id():
    archive, root = _narrated_archive()
    metadata = root.find(f'{{{conv.OPF_NS}}}metadata')
    ET.SubElement(metadata, f'{{{conv.OPF_NS}}}meta', {
        'name': MANAGED_META, 'content': 'overlay-ch001'})
    with zipfile.ZipFile(archive) as zf:
        _, replacements = prepare_epub_for_conversion(zf, root, 'EPUB')
    # This source span predates the converter and has no data-epub-tts marker.
    assert b'id="mo_ch001p0001w0001"' in replacements['EPUB/Text/ch.xhtml']


def test_malformed_xhtml_is_rejected_during_preflight_with_document_path():
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.writestr('EPUB/Text/ch.xhtml', _chapter('<p><unknown:tag>Story.</unknown:tag></p>'))
    with zipfile.ZipFile(archive) as zf, pytest.raises(ValueError, match='EPUB/Text/ch.xhtml'):
        prepare_epub_for_conversion(zf, _opf(), 'EPUB')


def test_ocr_markup_debris_is_repaired_without_changing_visible_text():
    # An OCR'd "<" turns the following words into attributes, which XML cannot hold.
    source = _chapter('<p xml:lang="en" epub:type="z3998:fiction" class="x">See op. cit. '
                      '<l ...="" s.j.:="" all="">orders of</l> the society.</p>')
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.writestr('EPUB/Text/ch.xhtml', source)
    with zipfile.ZipFile(archive) as zf:
        _, replacements = prepare_epub_for_conversion(zf, _opf(), 'EPUB')
    document = ET.fromstring(replacements['EPUB/Text/ch.xhtml'])
    paragraph = document.find('.//{http://www.w3.org/1999/xhtml}p')
    assert ''.join(paragraph.itertext()) == 'See op. cit. orders of the society.'
    assert paragraph.get('{http://www.w3.org/XML/1998/namespace}lang') == 'en'
    assert paragraph.get('{http://www.idpf.org/2007/ops}type') == 'z3998:fiction'
    assert paragraph.get('class') == 'x'
    assert paragraph.find('{http://www.w3.org/1999/xhtml}l').get('all') == ''


@pytest.mark.parametrize('kwargs', [{'author_overlay': True}, {'shared_audio': True}])
def test_reprocess_refuses_unowned_or_shared_overlay(kwargs):
    archive, root = _narrated_archive(**kwargs)
    with zipfile.ZipFile(archive) as zf, pytest.raises(ValueError):
        prepare_epub_for_conversion(zf, root, 'EPUB')


def test_patch_opf_tracks_final_collision_safe_overlay_ids():
    root = _opf(extra_manifest='<item id="overlay-ch001" href="author.smil" media-type="application/smil+xml"/>')
    conv.patch_opf(root, [{'spine_id': 'ch', 'smil_id': 'overlay-ch001', 'smil_href': 'SMIL/ch001.smil',
                          'audio_id': 'audio-ch001', 'audio_href': 'Audio/ch001.mp3', 'duration_ms': 1000}], 1000, 'voice')
    marker = root.find(f".//{{{conv.OPF_NS}}}meta[@name='{MANAGED_META}']")
    assert marker.get('content') == 'overlay-ch001-1'


def test_patch_opf_does_not_overwrite_refined_source_modified_date():
    root = _opf('<meta property="dcterms:modified" refines="#book">2000-01-01T00:00:00Z</meta>')
    conv.patch_opf(root, [], 0, 'voice')
    modified = root.findall(f".//{{{conv.OPF_NS}}}meta[@property='dcterms:modified']")
    assert len(modified) == 2
    assert modified[0].text == '2000-01-01T00:00:00Z'
    assert modified[1].get('refines') is None
