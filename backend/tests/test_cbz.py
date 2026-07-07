import zipfile
from xml.etree.ElementTree import fromstring

from pullarr.download.cbz import (
    build_comicinfo,
    guess_extension,
    has_comicinfo,
    inject_comicinfo,
    write_cbz,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 8


class TestGuessExtension:
    def test_png(self):
        assert guess_extension(PNG) == ".png"

    def test_jpg(self):
        assert guess_extension(JPG) == ".jpg"

    def test_fallback(self):
        assert guess_extension(b"unknown-format") == ".jpg"


class TestBuildComicinfo:
    def test_fields(self):
        xml = build_comicinfo(
            "Absolute Batman", number=2, title="The Zoo Part 2",
            summary="Bruce.", publisher="DC Comics", year=2024,
            web="https://example.com", page_count=20,
        )
        root = fromstring(xml)
        assert root.tag == "ComicInfo"
        get = lambda tag: root.findtext(tag)
        assert get("Series") == "Absolute Batman"
        assert get("Number") == "2"
        assert get("Title") == "The Zoo Part 2"
        assert get("Summary") == "Bruce."
        assert get("Publisher") == "DC Comics"
        assert get("Year") == "2024"
        assert get("PageCount") == "20"

    def test_fractional_number_kept(self):
        root = fromstring(build_comicinfo("X", number=10.5))
        assert root.findtext("Number") == "10.5"

    def test_empty_fields_omitted(self):
        root = fromstring(build_comicinfo("X"))
        assert root.find("Number") is None
        assert root.find("Volume") is None
        assert root.find("Summary") is None
        assert root.find("Publisher") is None


class TestInjectComicinfo:
    def _cbz(self, path):
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("001.png", PNG)

    def test_injects_when_missing(self, tmp_path):
        cbz = tmp_path / "x.cbz"
        self._cbz(cbz)
        assert not has_comicinfo(cbz)

        assert inject_comicinfo(cbz, build_comicinfo("X", number=1)) is True

        assert has_comicinfo(cbz)
        with zipfile.ZipFile(cbz) as zf:  # original content intact
            assert zf.read("001.png") == PNG
        assert not list(tmp_path.glob("*.partial"))

    def test_noop_when_present(self, tmp_path):
        cbz = tmp_path / "x.cbz"
        with zipfile.ZipFile(cbz, "w") as zf:
            zf.writestr("ComicInfo.xml", "<ComicInfo/>")
        assert inject_comicinfo(cbz, "<ComicInfo/>") is False

    def test_noop_for_cbr(self, tmp_path):
        cbr = tmp_path / "x.cbr"
        cbr.write_bytes(b"Rar!\x1a\x07\x00fake")
        assert inject_comicinfo(cbr, "<ComicInfo/>") is False


class TestWriteCbz:
    def test_roundtrip(self, tmp_path):
        dest = tmp_path / "sub" / "out.cbz"
        result = write_cbz(dest, [PNG, JPG, PNG], build_comicinfo("X", number=1))
        assert result == dest
        assert dest.exists()
        assert not dest.with_suffix(".cbz.partial").exists()
        with zipfile.ZipFile(dest) as zf:
            names = zf.namelist()
            assert names[0] == "ComicInfo.xml"
            assert names[1:] == ["001.png", "002.jpg", "003.png"]
            assert zf.read("002.jpg") == JPG
