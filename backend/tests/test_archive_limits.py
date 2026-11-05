"""Hostile containers must be bounded without changing ordinary comic payloads."""
import io
import os
import stat
import struct
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from xml.etree.ElementTree import ParseError

import pytest

from pullarr.download import archive as limits
from pullarr.download import cbz
from pullarr.library import importer
from pullarr.library.matcher import _comicinfo_identity
from pullarr.models import Issue, Series


def comic(path, pages=None, method=zipfile.ZIP_DEFLATED, metadata=None):
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in pages or [("001.png", b"page")]:
            archive.writestr(name, data, compress_type=method)
        if metadata is not None:
            archive.writestr("ComicInfo.xml", metadata)
    return path


def patch_eocd(path, offset, fmt, value):
    data = bytearray(path.read_bytes())
    struct.pack_into(fmt, data, data.rindex(b"PK\x05\x06") + offset, value)
    path.write_bytes(data)


@pytest.mark.parametrize("offset,fmt,value", [
    (12, "<I", limits.MAX_DIRECTORY_BYTES + 1),
    (10, "<H", limits.MAX_ARCHIVE_ENTRIES + 1),
])
def test_directory_rejected_before_zipfile_allocates(tmp_path, monkeypatch, offset, fmt, value):
    path = comic(tmp_path / "comic.cbz", metadata="<ComicInfo/>")
    patch_eocd(path, offset, fmt, value)
    def never_allocate(*args, **kwargs):
        raise AssertionError("ZipFile allocated an unchecked central directory")
    monkeypatch.setattr(zipfile, "ZipFile", never_allocate)
    with pytest.raises(limits.ImportValidationError, match="directory"):
        with limits.open_zip(path):
            pass
    assert cbz.read_comicinfo_bytes(path) is None


def test_actual_entry_count_is_checked_when_eocd_understates_it(tmp_path, monkeypatch):
    path = comic(tmp_path / "comic.cbz", [(f"{n}.png", b"page") for n in range(3)])
    patch_eocd(path, 10, "<H", 1)
    monkeypatch.setattr(limits, "MAX_ARCHIVE_ENTRIES", 2)
    with pytest.raises(limits.ImportValidationError, match="too many entries"):
        limits.validate_archive(path)


def test_zip64_and_unicode_subdirectories_remain_supported(tmp_path, monkeypatch):
    monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 1)
    path = comic(tmp_path / "comic.cbz", [("日本語/001.png", b"page")])
    limits.validate_archive(path)
    assert cbz.upsert_comicinfo(path, cbz.build_comicinfo("日本語"))
    with zipfile.ZipFile(path) as archive:
        assert archive.read("日本語/001.png") == b"page"


@pytest.mark.parametrize("operation", [limits.validate_archive, importer._content_hash, importer._nested_archive_members,
                                       lambda p: cbz.upsert_comicinfo(p, "<ComicInfo/>")])
def test_member_size_limit_precedes_decompression(tmp_path, monkeypatch, operation):
    path = comic(tmp_path / "comic.cbz", [("001.png", b"12345")])
    original = path.read_bytes()
    monkeypatch.setattr(limits, "MAX_MEMBER_BYTES", 4)
    def never_decode(*args, **kwargs):
        raise AssertionError("Unsafe member was decoded")
    monkeypatch.setattr(zipfile.ZipFile, "open", never_decode)
    with pytest.raises(limits.ImportValidationError, match="member"):
        operation(path)
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.metadata-partial"))


def test_per_archive_expansion_limit_precedes_decompression(tmp_path, monkeypatch):
    path = comic(tmp_path / "comic.cbz", [("001.png", b"1234"), ("002.png", b"5678")])
    monkeypatch.setattr(limits, "expansion_limit", lambda _: 7)
    with pytest.raises(limits.ImportValidationError, match="expanded size"):
        limits.validate_archive(path)


@pytest.mark.parametrize("name", ["../001.png", "/001.png", "C:/001.png", "..\\001.png", "."])
def test_unsafe_member_paths_rejected(tmp_path, name):
    path = comic(tmp_path / "comic.cbz", [(name, b"page")])
    with pytest.raises(limits.ImportValidationError, match="unsafe member path"):
        limits.validate_archive(path)


def test_zip_symlinks_rejected_before_import(tmp_path):
    path = tmp_path / "comic.cbz"
    link = zipfile.ZipInfo("001.png")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(link, "/outside")
    with pytest.raises(limits.ImportValidationError, match="links"):
        limits.validate_archive(path)


@pytest.mark.parametrize("method", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_all_existing_zip_codecs_survive_validation_hash_and_rewrite(tmp_path, method):
    payload = b"image page" * 10000
    path = comic(tmp_path / "comic.cbz", [("001.png", payload)], method=method)
    limits.validate_archive(path)
    before = importer._content_hash(path)
    assert cbz.upsert_comicinfo(path, cbz.build_comicinfo("Comic", number=1))
    assert importer._content_hash(path) == before
    with zipfile.ZipFile(path) as archive:
        assert archive.read("001.png") == payload
        assert archive.getinfo("001.png").compress_type == method


def test_rewrite_and_hash_stream_pages_and_keep_duplicate_members(tmp_path, monkeypatch):
    with pytest.warns(UserWarning, match="Duplicate name"):
        path = comic(tmp_path / "comic.cbz", [("001.png", b"first" * 500000), ("001.png", b"second" * 500000)])
    def never_read_whole(*args, **kwargs):
        raise AssertionError("A full archive member was read into memory")
    original_read = zipfile.ZipExtFile.read
    def bounded_read(self, n=-1):
        assert 0 <= n <= max(limits.COPY_CHUNK, cbz.MAX_COMICINFO_BYTES + 1)
        return original_read(self, n)
    monkeypatch.setattr(zipfile.ZipFile, "read", never_read_whole)
    monkeypatch.setattr(zipfile.ZipExtFile, "read", bounded_read)
    limits.validate_archive(path)
    before = importer._content_hash(path)
    with pytest.warns(UserWarning, match="Duplicate name"):
        assert cbz.upsert_comicinfo(path, "<ComicInfo><Series>New</Series></ComicInfo>")
    assert importer._content_hash(path) == before
    with zipfile.ZipFile(path) as archive:
        pages = [i for i in archive.infolist() if i.filename == "001.png"]
        assert len(pages) == 2
        assert pages[0].CRC != pages[1].CRC


def test_rewrite_failure_mid_stream_preserves_original_and_removes_partial(tmp_path, monkeypatch):
    path = comic(tmp_path / "comic.cbz")
    original = path.read_bytes()
    def fail_after_one_block(*args):
        output = args[3]
        output.write(b"partial")
        raise limits.ImportValidationError("size changed while reading")
    monkeypatch.setattr(cbz, "copy_zip_member", fail_after_one_block)
    with pytest.raises(limits.ImportValidationError):
        cbz.upsert_comicinfo(path, "<ComicInfo/>")
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.metadata-partial"))


def test_rewrite_can_drop_oversized_metadata_without_decoding_it(tmp_path):
    path = comic(tmp_path / "comic.cbz", metadata=b"x" * (cbz.MAX_COMICINFO_BYTES + 1))
    assert cbz.read_comicinfo(path) is None
    assert cbz.upsert_comicinfo(path, "<ComicInfo><Series>New</Series></ComicInfo>")
    assert "New" in cbz.read_comicinfo(path)


@pytest.mark.parametrize("xml", [
    '<!DOCTYPE ComicInfo [<!ENTITY x "expanded">]><ComicInfo><Series>&x;</Series></ComicInfo>',
    "<ComicInfo>" + "<Page>" * 65 + "</Page>" * 65 + "</ComicInfo>",
    "<ComicInfo>" + "<Page/>" * 10001 + "</ComicInfo>",
])
def test_untrusted_xml_structure_is_ignored_then_safely_replaced(tmp_path, xml):
    path = comic(tmp_path / "comic.cbz", metadata=xml)
    assert _comicinfo_identity(path, False) == ("", "", None)
    with pytest.raises(ParseError):
        cbz.parse_comicinfo(xml)
    assert cbz.upsert_comicinfo(path, "<ComicInfo><Series>New</Series></ComicInfo>")
    assert _comicinfo_identity(path, False) == ("New", "", None)


@pytest.mark.parametrize("operation", [cbz.inject_comicinfo, cbz.upsert_comicinfo])
def test_generated_metadata_cannot_exceed_read_limit(tmp_path, operation):
    path = comic(tmp_path / "comic.cbz")
    original = path.read_bytes()
    with pytest.raises((limits.ImportValidationError, ParseError)):
        operation(path, "<ComicInfo>" + "x" * cbz.MAX_COMICINFO_BYTES + "</ComicInfo>")
    assert path.read_bytes() == original


def test_nested_validation_uses_one_cumulative_expansion_budget(tmp_path):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("001.png", b"page" * 100)
    content = data.getvalue()
    path = comic(tmp_path / "pack.zip", [("one.cbz", content), ("two.cbz", content)])
    # Each comic is within its budget; the combined pack is not.
    budget = limits.ArchiveBudget(limit=len(content) * 2 + 799)
    with pytest.raises(limits.ImportValidationError, match="expanded size"):
        limits.validate_archive(path, allow_pack=True, _budget=budget)


def test_nested_extract_uses_checked_member_and_cumulative_budget(tmp_path):
    path = comic(tmp_path / "pack.zip", [("sub/one.cbz", b"a" * 10), ("two.cbz", b"b" * 10)])
    members = importer._nested_archive_members(path)
    with pytest.raises(limits.ImportValidationError, match="expanded size"):
        importer._extract_nested_archives(path, members, tmp_path / "expanded", limits.ArchiveBudget(limit=19))
    assert not (tmp_path / "one.cbz").exists()


def fake_decoder(tmp_path, body):
    decoder = tmp_path / "decoder"
    decoder.write_text(f"#!{sys.executable}\n" + body)
    decoder.chmod(0o700)
    return str(decoder)


def test_nonzip_decoded_output_is_limited(tmp_path, monkeypatch):
    decoder = fake_decoder(tmp_path, 'import sys\nsys.stdout.buffer.write(b"001.png\\n" if sys.argv[1] == "-tf" else b"x" * 101)\n')
    monkeypatch.setattr(limits.shutil, "which", lambda _: decoder)
    monkeypatch.setattr(limits, "expansion_limit", lambda _: 100)
    path = tmp_path / "comic.cbr"
    path.write_bytes(b"Rar!\x1a\x07\x00")
    with pytest.raises(limits.ImportValidationError, match="decoded output"):
        limits.validate_archive(path)


def test_nonzip_listing_is_limited(tmp_path, monkeypatch):
    decoder = fake_decoder(tmp_path, 'print("001.png\\n" * 100)\n')
    monkeypatch.setattr(limits.shutil, "which", lambda _: decoder)
    monkeypatch.setattr(limits, "MAX_DIRECTORY_BYTES", 100)
    path = tmp_path / "comic.cbr"
    path.write_bytes(b"Rar!\x1a\x07\x00")
    with pytest.raises(limits.ImportValidationError, match="decoded output"):
        limits.validate_archive(path)


def test_stalled_decoder_deadline_kills_and_reaps_process(tmp_path):
    pidfile = tmp_path / "pid"
    decoder = fake_decoder(tmp_path, f'import os,time\nopen({str(pidfile)!r},"w").write(str(os.getpid()))\ntime.sleep(60)\n')
    started = time.monotonic()
    budget = limits.ArchiveBudget(deadline=started + 0.3)
    with pytest.raises(limits.ImportValidationError, match="time limit"):
        list(limits._decoder_blocks("list", tmp_path / "unused", decoder, budget, 100))
    assert time.monotonic() - started < 5
    if pidfile.exists():
        with pytest.raises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux hard address-space limit")
def test_decoder_worker_enforces_hard_memory_and_cpu_limits(tmp_path):
    decoder = fake_decoder(tmp_path, '''import resource
assert resource.getrlimit(resource.RLIMIT_AS) == (1024**3, 1024**3)
assert resource.getrlimit(resource.RLIMIT_CPU) == (120, 120)
print("001.png")
''')
    result = list(limits._decoder_blocks("list", tmp_path / "unused", decoder, limits.ArchiveBudget(), 100))
    assert b"".join(result) == b"001.png\n"


def test_limit_failure_during_move_import_keeps_source_and_library_empty(tmp_path, monkeypatch):
    source = comic(tmp_path / "Series #1.cbz", [("001.png", b"oversize")])
    library = tmp_path / "library"
    library.mkdir()
    series = Series(id=1, title="Series", alt_titles="", folder_name="Series")
    issue = Issue(id=1, number=1, display_number="1", downloaded=False)
    monkeypatch.setattr(limits, "MAX_MEMBER_BYTES", 4)
    with pytest.raises(limits.ImportValidationError):
        importer.import_payload(source, series, [issue], library, "{series} #{number:03}", move=True)
    assert source.is_file()
    assert not list(library.rglob("*.cbz"))
    assert not issue.downloaded


@pytest.mark.parametrize("operation", [limits.validate_archive, importer._content_hash,
                                       lambda p: cbz.inject_comicinfo(p, "<ComicInfo/>")])
def test_container_size_is_checked_before_copy_or_hash(tmp_path, monkeypatch, operation):
    path = comic(tmp_path / "comic.cbz")
    original = path.read_bytes()
    monkeypatch.setattr(limits, "MAX_CONTAINER_BYTES", path.stat().st_size - 1)
    with pytest.raises(limits.ImportValidationError, match="container"):
        operation(path)
    assert path.read_bytes() == original


def test_inject_cannot_create_an_archive_beyond_directory_entry_limit(tmp_path, monkeypatch):
    path = comic(tmp_path / "comic.cbz")
    original = path.read_bytes()
    monkeypatch.setattr(limits, "MAX_ARCHIVE_ENTRIES", 1)
    with pytest.raises(limits.ImportValidationError):
        cbz.inject_comicinfo(path, "<ComicInfo/>")
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.partial"))


def test_comicinfo_also_bounds_compressed_work_before_opening_member(tmp_path, monkeypatch):
    path = comic(tmp_path / "comic.cbz", metadata="<ComicInfo/>")
    data = bytearray(path.read_bytes())
    central = data.rindex(b"PK\x01\x02")
    struct.pack_into("<I", data, central + 20, cbz.MAX_COMICINFO_BYTES + 65537)
    path.write_bytes(data)
    def never_decode(*args, **kwargs):
        raise AssertionError("Oversized compressed metadata was decoded")
    monkeypatch.setattr(zipfile.ZipFile, "open", never_decode)
    assert cbz.read_comicinfo(path) is None
