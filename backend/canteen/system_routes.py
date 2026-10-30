import sqlite3
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from .database import DATABASE_PATH, STATIC_DIR, connect

router = APIRouter()

@router.get("/")
def home():
    index = STATIC_DIR / "index.html"
    if not index.exists():
        return {"message": "SmartCanteen API is running. Open /docs to test the API. Place your frontend files in the static/ folder to serve the website."}
    return FileResponse(str(index))


@router.get("/api/health")
def health():
    try:
        with connect() as con:
            con.execute("SELECT 1").fetchone()
        return {"status": "ok", "database": "connected", "name": DATABASE_PATH.name}
    except sqlite3.Error as exc:
        raise HTTPException(503, f"SQLite unavailable: {exc}")



