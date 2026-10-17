"""Conservative EPUB2 migration and replacement of this converter's overlays.

Author resources are never identified by filename alone. Existing overlays must
have the converter's manifest/SMIL signature before replacement is allowed.
"""
import posixpath
import re
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

OPF = "http://www.idpf.org/2007/opf"
DC = "http://purl.org/dc/elements/1.1/"
SMIL = "http://www.w3.org/ns/SMIL"
MANAGED_META = "epub-tts-converter:managed-overlays"
GENERATED_ID = re.compile(r"mo_ch\d+p\d+(?:w\d+)?(?:-\d+)?\Z")


def _path(directory, href):
    return posixpath.normpath(posixpath.join(directory, unquote(urlsplit(href).path)))


def _unique_id(root, base):
    ids = {el.get("id") for el in root.iter() if el.get("id")}
    value, number = base, 1
    while value in ids:
        value = f"{base}-{number}"
        number += 1
    return value


def migrate_opf(root):
    """Migrate legacy metadata, preserving names, identifiers and date values."""
    metadata = root.find(f"{{{OPF}}}metadata")
    if metadata is None:
        raise ValueError("EPUB package has no metadata element")
    for el in list(metadata):
        if not el.tag.startswith(f"{{{DC}}}"):
            continue
        local = el.tag.split("}")[-1]
        legacy = {}
        for key in list(el.attrib):
            name = key.split("}")[-1]
            if name in {"file-as", "role", "scheme", "event"}:
                legacy[name] = el.attrib.pop(key)
        for prop in ("file-as", "role"):
            if prop not in legacy:
                continue
            if not el.get("id"):
                el.set("id", _unique_id(root, f"legacy-{local}"))
            meta = ET.SubElement(metadata, f"{{{OPF}}}meta", {
                "refines": "#" + el.get("id"), "property": prop})
            if prop == "role" and re.fullmatch(r"[a-z]{3}", legacy[prop]):
                meta.set("scheme", "marc:relators")
            meta.text = legacy[prop]
        if "scheme" in legacy:
            if not el.get("id"):
                el.set("id", _unique_id(root, f"legacy-{local}"))
            if local == "identifier":
                meta = ET.SubElement(metadata, f"{{{OPF}}}meta", {
                    "refines": "#" + el.get("id"), "property": "identifier-type"})
                meta.text = legacy["scheme"]
            else:
                # Preserve uncommon legacy schemes without inventing an EPUB
                # vocabulary property that a reading system cannot interpret.
                ET.SubElement(metadata, f"{{{OPF}}}meta", {
                    "name": f"epub2:{local}-scheme:{el.get('id')}",
                    "content": legacy["scheme"]})
        if local == "date" and legacy.get("event") not in (None, "publication"):
            event = legacy["event"]
            el.tag = f"{{{OPF}}}meta"
            el.set("property", {"creation": "dcterms:created",
                                "modification": "dcterms:modified"}.get(event, "dcterms:date"))
            # A source modification date is not the new package modification
            # timestamp. Preserve it as legacy metadata with its event label.
            if event == "modification" or event not in {"creation"}:
                el.attrib.pop("property", None)
                el.set("name", f"epub2:date:{event}")
                el.set("content", el.text or "")
                el.text = None
    dates = metadata.findall(f"{{{DC}}}date")
    for el in dates[1:]:
        el.tag = f"{{{OPF}}}meta"
        el.set("property", "dcterms:date")

    manifest = root.find(f"{{{OPF}}}manifest")
    spine = root.find(f"{{{OPF}}}spine")
    if manifest is not None and spine is not None:
        paths = {el.get("id"): _path("", el.get("href", "")) for el in manifest}
        seen = set()
        for ref in list(spine):
            if ref.tag != f"{{{OPF}}}itemref":
                continue
            key = paths.get(ref.get("idref"), ref.get("idref"))
            if key in seen:
                spine.remove(ref)
            else:
                seen.add(key)
    root.set("version", "3.0")


_XML_NAME = re.compile(r"[^\W\d][\w.\-]*\Z")


def _drop_unrepresentable_attributes(soup):
    """Remove attribute names that XML cannot represent.

    OCR'd books often contain an unescaped "<" in running text; HTML parsers
    then read the following words as attributes ('s.j.:=""', '...=""').
    Reading systems never display those words, so dropping them leaves the
    visible and narrated text unchanged while making the document well-formed.
    """
    allowed = {"xml", "xmlns", "epub"}
    allowed.update(name.split(":", 1)[1] for tag in soup.find_all(True)
                   for name in tag.attrs if name.startswith("xmlns:"))
    allowed = {prefix for prefix in allowed if _XML_NAME.match(prefix)}
    for tag in soup.find_all(True):
        for name in list(tag.attrs):
            prefix, colon, local = name.rpartition(":")
            if not _XML_NAME.match(local) or (colon and prefix not in allowed):
                del tag[name]


def migrate_xhtml_soup(soup, *, highlight=False):
    """Replace obsolete presentation attributes with equivalent inline CSS."""
    _drop_unrepresentable_attributes(soup)
    for tag in soup.find_all(True):
        css = []
        for attr, prop in (("valign", "vertical-align"), ("bgcolor", "background-color")):
            if attr in tag.attrs:
                css.append(f"{prop}:{tag.attrs.pop(attr)}")
        if "align" in tag.attrs:
            value = tag.attrs.pop("align")
            if tag.name in {"img", "table"} and value in {"left", "right"}:
                css.append(f"float:{value}")
            elif tag.name == "table" and value == "center":
                css.extend(("margin-left:auto", "margin-right:auto"))
            else:
                css.append(f"text-align:{value}")
        for attr in ("width", "height"):
            if attr not in tag.attrs:
                continue
            value = str(tag[attr]).strip()
            valid_integer = bool(re.fullmatch(r"\d+", value))
            if tag.name in {"img", "video", "canvas", "iframe", "embed", "object", "input"} and valid_integer:
                continue
            if tag.name in {"img", "table", "td", "th", "col", "colgroup", "hr", "iframe", "object", "embed"}:
                del tag[attr]
                css.append(f"{attr}:{value + 'px' if valid_integer else value}")
        if "nowrap" in tag.attrs:
            del tag["nowrap"]
            css.append("white-space:nowrap")
        for attr, props in (("hspace", ("margin-left", "margin-right")),
                            ("vspace", ("margin-top", "margin-bottom"))):
            if attr in tag.attrs:
                value = str(tag.attrs.pop(attr))
                value = value + "px" if value.isdigit() else value
                css.extend(f"{prop}:{value}" for prop in props)
        if css:
            # Original inline CSS has greater specificity than legacy HTML
            # presentation hints, so keep it last when migrating those hints.
            tag["style"] = ";".join(css) + ";" + tag.get("style", "")
    if soup.html and not soup.html.get("xmlns"):
        soup.html["xmlns"] = "http://www.w3.org/1999/xhtml"
    if highlight and soup.html:
        head = soup.head
        if head is None:
            head = soup.new_tag("head")
            soup.html.insert(0, head)
        if not any(".-epub-media-overlay-active" in s.decode_contents() for s in head.find_all("style")):
            style = soup.new_tag("style", type="text/css")
            style.string = ".-epub-media-overlay-active { background-color: #ffeb7f; color: #000; }"
            head.append(style)


def prepare_epub_for_conversion(zf, root, opf_dir, *, validate=True):
    """Return (removed ZIP paths, migrated XHTML replacements), mutating OPF.

    Unsupported author media overlays fail before synthesis. An unlinked SMIL
    manifest item, even with a converter-looking ID, is always preserved.
    """
    manifest = root.find(f"{{{OPF}}}manifest")
    metadata = root.find(f"{{{OPF}}}metadata")
    items = {item.get("id"): item for item in manifest}
    linked = [item for item in manifest if item.get("media-overlay")]
    managed_ids = set()
    for meta in metadata.findall(f"{{{OPF}}}meta"):
        if meta.get("name") == MANAGED_META:
            managed_ids.update(meta.get("content", "").split())
    removed, managed_targets, cleanup_ids = set(), {}, set()
    marked_chapters = set()
    removals = []
    for chapter in linked:
        overlay_id = chapter.get("media-overlay")
        overlay = items.get(overlay_id)
        if overlay is None:
            raise ValueError(f"Missing media overlay {overlay_id!r}")
        overlay_path = _path(opf_dir, overlay.get("href", ""))
        chapter_path = _path(opf_dir, chapter.get("href", ""))
        signature = bool(re.fullmatch(r"overlay-ch\d+(?:-\d+)?", overlay_id))
        signature = signature and bool(re.search(r"(?:^|/)SMIL/ch\d+(?:_\d+)?\.smil$", overlay_path))
        try:
            smil = ET.fromstring(zf.read(overlay_path))
        except (KeyError, ET.ParseError):
            signature = False
            smil = ET.Element("invalid")
        targets = []
        audio_paths = set()
        for node in smil.findall(f".//{{{SMIL}}}text"):
            src = node.get("src", "")
            parts = urlsplit(src)
            target_path = _path(posixpath.dirname(overlay_path), src)
            fragment = unquote(parts.fragment)
            if target_path != chapter_path or not fragment or (
                    overlay_id not in managed_ids and not GENERATED_ID.fullmatch(fragment)):
                signature = False
            targets.append(fragment)
        for node in smil.findall(f".//{{{SMIL}}}audio"):
            audio_paths.add(_path(posixpath.dirname(overlay_path), node.get("src", "")))
        if not targets or len(audio_paths) != 1:
            signature = False
        audio_items = [item for item in manifest if _path(opf_dir, item.get("href", "")) in audio_paths]
        if (len(audio_items) != 1 or audio_items[0].get("media-type") != "audio/mpeg"
                or not re.fullmatch(r"audio-ch\d+(?:-\d+)?", audio_items[0].get("id", ""))):
            signature = False
        # Explicit ownership supports paragraph IDs authored by the source book.
        # Legacy output remains deliberately stricter (only generated targets).
        if not signature:
            raise ValueError(f"Existing media overlay {overlay_id!r} is not safely replaceable; "
                             "use the original book without narration")
        managed_targets.setdefault(chapter_path, set()).update(targets)
        if overlay_id in managed_ids:
            marked_chapters.add(chapter_path)
        cleanup_ids.add(overlay_id)
        removals.extend([overlay, *audio_items])
        removed.update({overlay_path, *audio_paths})
        chapter.attrib.pop("media-overlay", None)
    # Never remove audio that is also used by an unrelated SMIL resource.
    for item in manifest:
        if item in removals or item.get("media-type") != "application/smil+xml":
            continue
        path = _path(opf_dir, item.get("href", ""))
        try:
            other = ET.fromstring(zf.read(path))
        except (KeyError, ET.ParseError):
            continue
        for node in other.findall(f".//{{{SMIL}}}audio"):
            if _path(posixpath.dirname(path), node.get("src", "")) in removed:
                raise ValueError("Converter audio is also referenced by an author media overlay")

    replacements = {}
    for item in list(manifest):
        if item.get("media-type") not in {"application/xhtml+xml", "text/html"}:
            continue
        path = _path(opf_dir, item.get("href", ""))
        data = zf.read(path)
        soup = BeautifulSoup(data, "lxml")
        for audio in soup.find_all(["audio", "source"]):
            if _path(posixpath.dirname(path), audio.get("src", "")) in removed:
                raise ValueError("Converter audio is also embedded in author content")
        for ident in managed_targets.get(path, set()):
            if not GENERATED_ID.fullmatch(ident):
                continue
            for span in soup.find_all("span", id=ident):
                if path in marked_chapters and span.get("data-epub-tts") not in {"word", "anchor"}:
                    continue
                span.unwrap()
        migrate_xhtml_soup(soup)
        raw = soup.encode("utf-8", formatter="minimal")
        replacements[path] = re.sub(
            rb'<(meta|link|br|hr|img|input|col|area|base|source|embed|track|wbr)'
            rb'(\s[^>]*?)?(?<!/)\s*>', rb'<\1\2/>', raw)
        if validate:
            try:
                ET.fromstring(replacements[path])
            except ET.ParseError as error:
                raise ValueError(f"Invalid XHTML in {path!r}: {error}. "
                                 "Repair this source document before generating narration.") from error
    for item in removals:
        if item in list(manifest):
            manifest.remove(item)
    for meta in list(metadata):
        if meta.get("name") == MANAGED_META or meta.get("refines", "").lstrip("#") in cleanup_ids:
            metadata.remove(meta)
        elif linked and not meta.get("refines") and meta.get("property") in {
                "media:duration", "media:narrator", "media:active-class"}:
            metadata.remove(meta)
    migrate_opf(root)
    return removed, replacements
