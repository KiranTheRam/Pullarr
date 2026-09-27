import httpx
import pytest
from fastapi import FastAPI

from pullarr.frontend import install_frontend


@pytest.fixture
def public_app(tmp_path):
    root = tmp_path / "static"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<html>Pullarr</html>")
    (root / "icon.svg").write_text("<svg></svg>")
    (root / "assets" / "app.js").write_text("console.log('public')")
    (tmp_path / "private.txt").write_text("outside sentinel")
    sibling = tmp_path / "static-private"
    sibling.mkdir()
    (sibling / "private.txt").write_text("outside sentinel")
    (root / "escape.txt").symlink_to(tmp_path / "private.txt")
    (root / "escape-dir").symlink_to(sibling, target_is_directory=True)
    (root / "assets" / "escape.txt").symlink_to(tmp_path / "private.txt")
    (root / "alias.svg").symlink_to(root / "icon.svg")
    (root / "loop").symlink_to(root / "loop")
    app = FastAPI()
    install_frontend(app, root)
    return app, root


@pytest.mark.parametrize("path", [
    "/..%2Fprivate.txt", "/%2e%2e/private.txt", "/nested/..%2F..%2Fprivate.txt",
    "/..%2Fstatic-private%2Fprivate.txt", "/escape.txt", "/escape-dir/private.txt",
    "/assets/escape.txt", "/loop", "/%00",
])
async def test_outside_or_invalid_paths_are_not_served(public_app, path):
    app, _ = public_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.get(path)
    assert response.status_code == 404
    assert "outside sentinel" not in response.text


@pytest.mark.parametrize("path", ["/", "/series/42", "/settings", "/unknown", "/%252e%252e%252fprivate.txt"])
async def test_spa_navigation_preserves_fallback(public_app, path):
    app, _ = public_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.get(path)
    assert response.status_code == 200
    assert response.text == "<html>Pullarr</html>"
    assert "no-store" in response.headers["cache-control"]


async def test_files_assets_and_safe_symlink(public_app):
    app, _ = public_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        for path in ["/icon.svg", "/alias.svg"]:
            response = await client.get(path)
            assert response.status_code == 200
            assert response.text == "<svg></svg>"
            assert "image/svg+xml" in response.headers["content-type"]
        assert (await client.get("/assets/app.js")).text == "console.log('public')"
        assert (await client.get("/assets/missing.js")).status_code == 404


async def test_index_symlink_and_absolute_paths_cannot_escape(public_app):
    app, root = public_app
    (root / "index.html").unlink()
    (root / "index.html").symlink_to(root.parent / "private.txt")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        for path in ["/", "/settings", "/%2F" + str(root.parent / "private.txt").lstrip("/")]:
            response = await client.get(path)
            assert response.status_code == 404
            assert "outside sentinel" not in response.text
