"""ComicInfo.xml helpers (Anansi/ComicRack schema, as read by Komga and
Kavita). GetComics files arrive as finished CBR/CBZ archives, so instead of
packing pages we inject/refresh ComicInfo.xml into CBZ (zip) files on import;
CBR (rar) can't be rewritten without rar tooling and is left untouched."""

import shutil
import zipfile
from pathlib import Path
from xml.dom import minidom
from xml.etree.ElementTree import Element, SubElement, tostring


def build_comicinfo(
    series: str,
    number: float | str | None = None,
    volume: int | None = None,
    title: str = "",
    summary: str = "",
    publisher: str = "",
    year: int | None = None,
    web: str = "",
    page_count: int | None = None,
) -> str:
    root = Element("ComicInfo")
    root.set("xmlns:xsi", "http://www.w3.org/2001/XMLSchema-instance")
    root.set("xmlns:xsd", "http://www.w3.org/2001/XMLSchema")

    def add(tag: str, value) -> None:
        if value is None or value == "":
            return
        SubElement(root, tag).text = str(value)

    add("Series", series)
    if number is not None:
        if isinstance(number, str):
            add("Number", number)
        else:
            add("Number", int(number) if float(number).is_integer() else number)
    add("Volume", volume)
    add("Title", title)
    add("Summary", summary)
    add("Publisher", publisher)
    add("Year", year)
    add("Web", web)
    add("PageCount", page_count)
    rough = tostring(root, encoding="unicode")
    return minidom.parseString(rough).toprettyxml(indent="  ")


def has_comicinfo(archive: Path) -> bool:
    try:
        with zipfile.ZipFile(archive) as zf:
            return any(n.lower() == "comicinfo.xml" for n in zf.namelist())
    except (zipfile.BadZipFile, OSError):
        return False


def inject_comicinfo(archive: Path, comicinfo_xml: str) -> bool:
    """Add ComicInfo.xml to a CBZ that lacks one. Returns True when written.
    No-ops (False) for non-zip archives or when one is already present."""
    archive = Path(archive)
    if archive.suffix.lower() not in (".cbz", ".zip") or not zipfile.is_zipfile(archive):
        return False
    if has_comicinfo(archive):
        return False
    tmp = archive.with_suffix(archive.suffix + ".partial")
    shutil.copy2(archive, tmp)
    try:
        with zipfile.ZipFile(tmp, "a", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("ComicInfo.xml", comicinfo_xml)
        tmp.replace(archive)
        return True
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def write_cbz(dest: Path, pages: list[bytes], comicinfo_xml: str) -> Path:
    """Writes ordered page images + ComicInfo.xml into a .cbz at dest (used
    when packing a folder of loose images)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".cbz.partial")
    width = max(3, len(str(len(pages))))
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("ComicInfo.xml", comicinfo_xml)
        for i, data in enumerate(pages, start=1):
            zf.writestr(f"{i:0{width}d}{guess_extension(data)}", data)
    tmp.rename(dest)
    return dest


EXT_BY_SIGNATURE = {
    b"\xff\xd8\xff": ".jpg",
    b"\x89PNG": ".png",
    b"GIF8": ".gif",
    b"RIFF": ".webp",
}


def guess_extension(data: bytes, fallback: str = ".jpg") -> str:
    for sig, ext in EXT_BY_SIGNATURE.items():
        if data.startswith(sig):
            return ext
    return fallback
