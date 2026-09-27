"""Serve public files and SPA routes within the frontend's filesystem root."""

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


def install_frontend(app: FastAPI, directory: Path) -> None:
    root = directory.resolve()
    app.mount("/assets", StaticFiles(directory=root / "assets"), name="assets")

    def public_path(path: str) -> Path:
        try:
            candidate = (root / path).resolve()
            if not candidate.is_relative_to(root):
                raise ValueError("Outside public directory")
            return candidate
        except (OSError, RuntimeError, ValueError):
            raise HTTPException(404, "Not found") from None

    @app.get("/{full_path:path}")
    async def spa(full_path: str):
        candidate = public_path(full_path)
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        index = public_path("index.html")
        if not index.is_file():
            raise HTTPException(404, "Not found")
        return FileResponse(
            index,
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )
