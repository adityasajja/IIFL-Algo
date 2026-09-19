"""Serve the built web app."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse


logger = logging.getLogger("atr.api")


# ----------------------------------------------------------------------
# Frontend (built React app) — API routes above take precedence.
# ----------------------------------------------------------------------
_WEB_DIST = Path(__file__).resolve().parents[4] / "web" / "dist"


def mount_frontend(app: FastAPI) -> None:
    """Mount the built SPA if present. Call last: its catch-all would swallow later routes."""
    assets = _WEB_DIST / "assets"
    if assets.is_dir():
        from fastapi.staticfiles import StaticFiles

        app.mount("/assets", StaticFiles(directory=assets), name="assets")
    if (_WEB_DIST / "index.html").exists():
        @app.get("/{path:path}", include_in_schema=False, response_model=None)
        def spa(path: str) -> FileResponse | JSONResponse:
            if path.startswith("api/"):
                return JSONResponse({"detail": "not found"}, status_code=404)
            # Resolve before serving: `%2e%2e` decodes to `..` and would otherwise
            # walk out of the dist folder to `.env` or the broker session file.
            root = _WEB_DIST.resolve()
            candidate = (root / path).resolve()
            if path and candidate.is_relative_to(root) and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(_WEB_DIST / "index.html")

