import httpx
import pytest
import respx

from pullarr.download.ddl import download_release, filename_from_response
from pullarr.sources.base import DDLSource


class FakeSource(DDLSource):
    name = "fake"

    def __init__(self, links: list[str]) -> None:
        self._links = links
        self.client = httpx.AsyncClient(follow_redirects=True)

    async def search_series(self, query):
        return []

    async def list_releases(self, external_id):
        return []

    async def search_releases(self, query):
        return []

    async def resolve_downloads(self, release_external_id):
        return self._links


class TestFilenameFromResponse:
    def _resp(self, url: str, headers: dict | None = None) -> httpx.Response:
        req = httpx.Request("GET", url)
        return httpx.Response(200, request=req, headers=headers or {})

    def test_from_content_disposition(self):
        resp = self._resp("https://x/dl", {
            "Content-Disposition": 'attachment; filename="Batman 015.cbr"'})
        assert filename_from_response(resp, "fb") == "Batman 015.cbr"

    def test_from_url_path_percent_decoded(self):
        resp = self._resp(
            "https://fs1.comicfiles.ru/2025.12.10/Absolute%20Batman%20015%20%282026%29.cbr"
        )
        assert filename_from_response(resp, "fb") == "Absolute Batman 015 (2026).cbr"

    def test_fallback(self):
        resp = self._resp("https://x/")
        assert filename_from_response(resp, "part1") == "part1"


@pytest.mark.asyncio
@respx.mock
async def test_download_release_streams_files(tmp_path):
    payload = b"comic-bytes" * 100
    respx.get("https://getcomics.org/dls/abc").respond(
        302, headers={"Location": "https://fs1.comicfiles.ru/Batman%20015.cbr"}
    )
    respx.get("https://fs1.comicfiles.ru/Batman%20015.cbr").respond(
        200, content=payload, headers={"Content-Length": str(len(payload))}
    )
    source = FakeSource(["https://getcomics.org/dls/abc"])
    progress = []

    payload_dir = await download_release(
        source, "https://getcomics.org/post", tmp_path,
        progress_cb=lambda done, total: progress.append((done, total)),
    )

    files = list(payload_dir.iterdir())
    assert [f.name for f in files] == ["Batman 015.cbr"]
    assert files[0].read_bytes() == payload
    assert not list(payload_dir.glob("*.partial"))
    assert progress[-1] == (len(payload), len(payload))


@pytest.mark.asyncio
@respx.mock
async def test_download_release_cleans_partials_on_failure(tmp_path):
    respx.get("https://fs1.comicfiles.ru/x.cbr").respond(500)
    source = FakeSource(["https://fs1.comicfiles.ru/x.cbr"])

    with pytest.raises(httpx.HTTPStatusError):
        await download_release(source, "https://getcomics.org/post", tmp_path)

    assert not list(tmp_path.rglob("*.partial"))
