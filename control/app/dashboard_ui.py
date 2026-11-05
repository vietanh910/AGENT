"""Serve the repository-owned dashboard without a separate frontend runtime."""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter(include_in_schema=False)
ROOT = Path(__file__).resolve().parent / "static" / "dashboard"


@router.get("/dashboard")
@router.get("/dashboard/")
def dashboard_page():
    return FileResponse(ROOT / "index.html", media_type="text/html", headers={
        "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
        "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"})


@router.get("/dashboard/assets/{name}")
def dashboard_asset(name: str):
    if name not in {"dashboard.css", "dashboard.js"}:
        raise HTTPException(404)
    return FileResponse(ROOT / name)
