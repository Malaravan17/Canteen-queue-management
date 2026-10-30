"""SmartCanteen AI - FastAPI + SQLite demo backend.
Run from this folder: uvicorn main:app --reload
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from canteen.database import STATIC_DIR, init_database, DATABASE_PATH
from canteen import admin_routes, catalog_routes, order_routes, qr_routes, system_routes, wastage_routes, chatbot_routes


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_database()
    from canteen.database import DATABASE_PATH as current_database_path
    print(f"SQLite connected: {current_database_path}")
    yield


app = FastAPI(title="SmartCanteen AI API", version="1.0.0", lifespan=lifespan)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

for route_module in (system_routes, qr_routes, catalog_routes, order_routes, admin_routes, wastage_routes, chatbot_routes):
    app.include_router(route_module.router)
