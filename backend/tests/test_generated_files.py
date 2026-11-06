"""Regression coverage for metadata and archive names from untrusted metadata."""

import errno
import inspect
import zipfile
from pathlib import Path
from xml.etree.ElementTree import fromstring

import httpx
import pytest

from pullarr.download import cbz
from pullarr.download.ddl import filename_from_response
from pullarr.filename import parse_filename
from pullarr.library import naming
from pullarr.util import sanitize_filename


@pytest.mark.parametrize("failure", ["write", "replace"])
def test_failed_cbz_write_removes_partial_and_preserves_destination(tmp_path, monkeypatch, failure):
    dest = tmp_path / "comic.cbz"
    dest.write_bytes(b"existing archive")

    def fail(*args, **kwargs):
        raise OSError(errno.ENOSPC, "simulated full disk")

    if failure == "write":
        monkeypatch.setattr(zipfile.ZipFile, "writestr", fail)
    else:
        monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError, match="simulated full disk"):
        cbz.write_cbz(dest, [b"image"], cbz.build_comicinfo("Series"))
    assert dest.read_bytes() == b"existing archive"
    assert list(tmp_path.iterdir()) == [dest]


@pytest.mark.parametrize("operation", [cbz.inject_comicinfo, cbz.upsert_comicinfo])
def test_metadata_write_failure_leaves_archive_intact(tmp_path, monkeypatch, operation):
    dest = tmp_path / "comic.cbz"
    with zipfile.ZipFile(dest, "w") as archive:
        archive.writestr("001.jpg", b"image")
    original = dest.read_bytes()

    def fail(*args, **kwargs):
        raise RuntimeError("simulated archive writer failure")

    monkeypatch.setattr(zipfile.ZipFile, "writestr", fail)
    with pytest.raises(RuntimeError, match="simulated archive writer failure"):
        operation(dest, cbz.build_comicinfo("Series"))
    assert dest.read_bytes() == original
    assert list(tmp_path.iterdir()) == [dest]


def test_metadata_copy_failure_cleans_partial(tmp_path, monkeypatch):
    dest = tmp_path / "comic.cbz"
    with zipfile.ZipFile(dest, "w") as archive:
        archive.writestr("001.jpg", b"image")
    original = dest.read_bytes()

    def fail_copy(source, target):
        target.write_bytes(b"incomplete copy")
        raise OSError("copy failed")

    monkeypatch.setattr(cbz.shutil, "copy2", fail_copy)
    with pytest.raises(OSError, match="copy failed"):
        cbz.inject_comicinfo(dest, cbz.build_comicinfo("Series"))
    assert dest.read_bytes() == original
    assert list(tmp_path.iterdir()) == [dest]


def test_all_comicinfo_text_fields_strip_illegal_xml_and_keep_unicode():
    illegal = "\x00\x01\x08\x0b\x0c\x0e\x1f\ud800\udfff\ufffe\uffff"
    good = "A\tB\nC\rD 漫画 😀 & < > \ue000\U0010ffff"
    fields = {key: good + illegal for key, param in inspect.signature(cbz.build_comicinfo).parameters.items()
              if isinstance(param.default, str)}
    xml = cbz.build_comicinfo(good + illegal, **fields)
    root = fromstring(xml)
    # XML parsers normalize CR into LF. XML escaping and legal text survive.
    assert all(child.text == good.replace("\r", "\n") for child in root)


@pytest.mark.parametrize(("name", "expected"), [
    (".Batman//Hidden", "BatmanHidden"), ("Why ...?", "Why"),
    (" ... ", "Unknown"), ("CON", "CON_"), ("aux.cbz", "aux_.cbz"),
    ("NUL .zip", "NUL_ .zip"), ("COM1", "COM1_"), ("LPT9", "LPT9_"),
    ("COM¹", "COM¹_"), ("Conan", "Conan"), ("COM10", "COM10"),
    ("Nul #001", "Nul #001"),
])
def test_safe_component_edge_cases(name, expected):
    assert sanitize_filename(name) == expected


@pytest.mark.parametrize("char", ["a", "é", "長", "😀"])
def test_shortened_components_keep_full_unicode_and_distinct_series(char):
    prefix = char * 300
    names = [sanitize_filename(prefix + ending) for ending in (" One", " Two")]
    assert names[0] != names[1]
    for name in names:
        assert len(name.encode()) <= 200
        assert name == name.encode().decode()
        assert not name.startswith(".") and not name.endswith((" ", "."))
        assert parse_filename(name).issue_number is None
    assert sanitize_filename(prefix + " One") == names[0]


TEMPLATES = [naming.DEFAULT_TEMPLATE, "{series} ({year}) #{issue:03d} - {title}",
             "{title} {series} #{issue:03d}", "{series} " + "x" * 250 + " #{issue:03d}"]


@pytest.mark.parametrize("char", ["a", "é", "長", "😀"])
@pytest.mark.parametrize("template", TEMPLATES)
def test_long_filenames_keep_issue_identity_and_fit_temporary_names(char, template):
    for length in (0, 50, 199, 300):
        series = char * length
        names = [naming.issue_filename(template, series, number, char * 300, 2026, ext=".cbr")
                 for number in (1, 2, 12.25, 1000.5)]
        assert len(set(names)) == len(names)
        for number, name in zip((1, 2, 12.25, 1000.5), names):
            assert name.endswith(".cbr")
            assert len((name + ".metadata-partial").encode()) <= 255
            assert parse_filename(Path(name).stem).issue_number == number


@pytest.mark.parametrize("label", ["1AU", "78.BEY", "Annual 1", "12.25"])
def test_long_names_preserve_string_issue_labels(label):
    name = naming.issue_filename(naming.DEFAULT_TEMPLATE, "長" * 300, label)
    rendered = label if label != "12.25" else "012.25"
    assert name.endswith(f"#{rendered}.cbz")


def test_long_series_ranges_volumes_and_reboots_stay_distinct():
    series = "A" * 300
    names = [naming.volume_filename(series, v) for v in (1, 2)]
    assert len(set(names)) == 2
    assert [parse_filename(Path(n).stem).volume_number for n in names] == [1, 2]
    issue_range = naming.range_filename(series, 12.25, 15)
    volume_range = naming.volume_range_filename(series, 3, 7)
    assert parse_filename(Path(issue_range).stem).issue == (12.25, 15)
    assert parse_filename(Path(volume_range).stem).volume == (3, 7)
    for name in [*names, issue_range, volume_range]:
        assert len((name + ".metadata-partial").encode()) <= 255
    assert naming.series_folder(series, 2016) != naming.series_folder(series, 2026)
    assert naming.issue_filename(naming.DEFAULT_TEMPLATE, series + "1", 1) != naming.issue_filename(
        naming.DEFAULT_TEMPLATE, series + "2", 1)


def test_narrow_custom_template_keeps_series_distinct_and_warns_once(caplog):
    naming._warn_long_template.cache_clear()
    template = "x" * 220 + "{series} #{issue:03d}"
    titles = ["A" * 300 + end for end in ("1", "2")]
    names = [naming.issue_filename(template, series, 1) for series in titles]
    assert len(set(names)) == 2
    assert names == [naming.issue_filename(naming.DEFAULT_TEMPLATE, series, 1) for series in titles]
    assert sum("too long" in record.message for record in caplog.records) == 1


def test_long_title_padding_does_not_discard_title():
    template = "{series} #{issue:03d} - {title}"
    base = naming.issue_filename(template, "S" * 200, 1, "Title" * 30)
    assert " - Title" in base
    assert base == naming.issue_filename(template, "S" * 200, 1, "Title" * 30 + " ?" * 300)


@pytest.mark.parametrize("ext", [".cbz", ".cbr", ".cb7", ".zip", ".rar", ".7z", ".CBZ"])
def test_download_names_keep_archive_extension_when_shortened(ext):
    from urllib.parse import quote
    response = httpx.Response(200, request=httpx.Request("GET", "https://example.com/file"), headers={
        "Content-Disposition": "attachment; filename*=UTF-8''" + quote("長" * 300 + ext),
    })
    name = filename_from_response(response, "fallback")
    assert name.endswith(ext)
    assert len((name + ".partial").encode()) <= 255
    assert name != filename_from_response(httpx.Response(200, request=response.request, headers={
        "Content-Disposition": "attachment; filename*=UTF-8''" + quote("長" * 300 + "2" + ext),
    }), "fallback")


def test_long_generated_name_can_be_packed_and_metadata_refreshed(tmp_path):
    name = naming.issue_filename("{series} #{issue:03d} - {title}", "A" * 300, 12.25, "B" * 300)
    dest = tmp_path / naming.series_folder("A" * 300, 2026) / name
    cbz.write_cbz(dest, [b"\x89PNG\r\nimage"], cbz.build_comicinfo("Series", number=12.25))
    assert cbz.upsert_comicinfo(dest, cbz.build_comicinfo("Series", number=12.25, title="Updated"))
    assert list(dest.parent.iterdir()) == [dest]
    with zipfile.ZipFile(dest) as archive:
        assert archive.read("001.png") == b"\x89PNG\r\nimage"
        assert fromstring(archive.read("ComicInfo.xml")).findtext("Title") == "Updated"


def test_import_keeps_long_issue_names_and_shared_series_prefixes_separate(tmp_path):
    from pullarr.library.importer import import_payload
    from pullarr.models import Issue, Series

    root = tmp_path / "library"
    root.mkdir()
    placed = []
    for series_id, ending in enumerate((" One", " Two"), start=1):
        payload = tmp_path / f"payload-{series_id}"
        payload.mkdir()
        for number in (1, 2):
            with zipfile.ZipFile(payload / f"Series #{number:03d}.cbz", "w") as archive:
                archive.writestr("001.png", b"\x89PNG\r\n\x1a\n" + bytes([series_id, number]))
        series = Series(id=series_id, title="長" * 300 + ending, folder_name="", year=2026)
        issues = [Issue(id=series_id * 10 + number, series_id=series_id, number=float(number),
                        title="Title" * 100) for number in (1, 2)]
        imported = import_payload(payload, series, issues, root, "{series} {title} #{issue:03d}")
        assert len(imported) == 2
        for result in imported:
            placed.append(result.dest)
            with zipfile.ZipFile(result.dest) as archive:
                assert archive.read("001.png").endswith(bytes([series_id, int(result.issue.number)]))
    assert len(set(placed)) == 4
    assert len({dest.parent for dest in placed}) == 2
    assert not list(root.rglob("*.importing")) and not list(root.rglob("*.metadata-partial"))


@pytest.mark.parametrize("char,extension", [("x", ".cbz"), ("長", ".zip")])
def test_long_unmatched_import_names_fit_and_remain_distinct(tmp_path, char, extension):
    from pullarr.library.importer import import_payload
    from pullarr.models import Series

    root = tmp_path / "library"
    root.mkdir()
    payload = tmp_path / "payload"
    payload.mkdir()
    # Both source names fit one filesystem component. Their full generated
    # destination names used to exceed it after adding the series prefix.
    stems = [char * (240 // len(char.encode())) + ending for ending in ("One", "Two")]
    for index, stem in enumerate(stems):
        with zipfile.ZipFile(payload / f"{stem}{extension}", "w") as archive:
            archive.writestr("001.png", b"page" + bytes([index]))
    series = Series(title="A moderately long series title", year=2026, folder_name="Series")
    imported = import_payload(payload, series, [], root, naming.DEFAULT_TEMPLATE, move=True)
    assert len(imported) == 2
    assert len({item.dest.name for item in imported}) == 2
    for index, item in enumerate(imported):
        assert item.status == "unmatched" and not item.covered
        assert item.dest.suffix == ".cbz"
        assert len((item.dest.name + ".metadata-partial").encode()) <= 255
        assert item.dest.name == naming.unmatched_filename(series.title, stems[index], series.year)
        with zipfile.ZipFile(item.dest) as archive:
            assert archive.read("001.png") == b"page" + bytes([index])
            assert "ComicInfo.xml" in archive.namelist()
    assert not list(payload.iterdir())
    assert not list(root.rglob("*.importing")) and not list(root.rglob("*.metadata-partial"))
