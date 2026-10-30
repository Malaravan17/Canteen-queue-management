from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import sqlite3

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
DATABASE_PATH = Path(os.getenv("SQLITE_DATABASE_PATH", str(BASE_DIR / "smartcanteen.db")))
ADMIN_DEMO_KEY = os.getenv("ADMIN_DEMO_KEY", "change-this-before-deployment")
STATIC_DIR = BASE_DIR / "static"

def now():
    return datetime.now(timezone.utc)


def dt(value):
    """Convert ISO strings stored in SQLite back to aware datetimes."""
    if value is None or isinstance(value, datetime):
        return value
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return value


@contextmanager
def connect():
    con = sqlite3.connect(DATABASE_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 15000")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def row_dict(row):
    if row is None:
        return None
    result = dict(row)
    for key in ("created_at", "updated_at", "ready_at", "completed_at", "paid_at"):
        if key in result and result[key] is not None:
            result[key] = dt(result[key]).isoformat()
    for key in ("supported_categories", "item_names", "items"):
        if key in result and isinstance(result[key], str):
            try:
                result[key] = json.loads(result[key])
            except json.JSONDecodeError:
                pass
    for key in ("is_active", "is_available", "peak_mode", "read", "is_simulated", "low_stock"):
        if key in result and result[key] is not None:
            result[key] = bool(result[key])
    return result


def init_database():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'Snacks',
            price REAL NOT NULL CHECK(price >= 0),
            stock_quantity INTEGER NOT NULL DEFAULT 0 CHECK(stock_quantity >= 0),
            prep_minutes INTEGER NOT NULL DEFAULT 3,
            is_available INTEGER NOT NULL DEFAULT 1,
            image_emoji TEXT NOT NULL DEFAULT '🍽️',
            description TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS counters (
            number INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 0,
            supported_categories TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            peak_mode INTEGER NOT NULL DEFAULT 0,
            auto_open_threshold INTEGER NOT NULL DEFAULT 5,
            event_name TEXT NOT NULL DEFAULT '',
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS combos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            item_names TEXT NOT NULL DEFAULT '[]',
            price REAL NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_name TEXT NOT NULL,
            items TEXT NOT NULL,
            subtotal REAL NOT NULL,
            discount REAL NOT NULL DEFAULT 0,
            total_amount REAL NOT NULL,
            counter_number INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            payment_status TEXT NOT NULL DEFAULT 'pending',
            payment_method TEXT,
            estimated_prep_minutes INTEGER NOT NULL DEFAULT 3,
            estimated_wait_minutes INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            ready_at TEXT,
            completed_at TEXT,
            paid_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at);
        CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
        CREATE INDEX IF NOT EXISTS idx_orders_counter_status ON orders(counter_number, status);
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT NOT NULL,
            amount REAL NOT NULL,
            method TEXT NOT NULL,
            status TEXT NOT NULL,
            is_simulated INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_payments_order_id ON payments(order_id);
        CREATE TABLE IF NOT EXISTS inventory_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT NOT NULL,
            item_name TEXT NOT NULL,
            quantity_change INTEGER NOT NULL,
            reason TEXT NOT NULL,
            order_id TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL,
            read INTEGER NOT NULL DEFAULT 0,
            type TEXT NOT NULL DEFAULT 'general'
        );
        CREATE TABLE IF NOT EXISTS qr_customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            counter_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'waiting',
            created_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY(counter_id) REFERENCES counters(number)
        );
        CREATE INDEX IF NOT EXISTS idx_qr_customers_status ON qr_customers(status);
        CREATE INDEX IF NOT EXISTS idx_qr_customers_counter_status ON qr_customers(counter_id, status);
        """)

        if con.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0:
            created = now().isoformat()
            seed_items = [
                ("Masala Dosa","Breakfast",45,35,5,1,"🥞","Crispy dosa with masala filling"),
                ("Idli (2 pcs)","Breakfast",25,50,2,1,"🍘","Soft idlis with chutney"),
                ("Veg Sandwich","Snacks",40,28,4,1,"🥪","Fresh vegetable sandwich"),
                ("Samosa","Snacks",15,60,1,1,"🥟","Crispy potato samosa"),
                ("Tea","Drinks",12,80,2,1,"☕","Hot tea"),
                ("Fresh Lime Juice","Drinks",25,25,3,1,"🍋","Refreshing lime drink"),
                ("Veg Puff","Snacks",20,40,1,1,"🥐","Flaky vegetable puff"),
            ]
            con.executemany("""INSERT INTO items
                (name,category,price,stock_quantity,prep_minutes,is_available,image_emoji,description,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                [(n,c,p,s,prep,avail,emoji,desc,created) for n,c,p,s,prep,avail,emoji,desc in seed_items])

        if con.execute("SELECT COUNT(*) FROM counters").fetchone()[0] == 0:
            created = now().isoformat()
            categories = json.dumps(["Breakfast", "Snacks", "Drinks"])
            con.executemany("""INSERT INTO counters(number,name,is_active,supported_categories,created_at)
                VALUES (?,?,?,?,?)""",
                [(i, f"Counter {i}", 1 if i == 1 else 0, categories, created) for i in range(1,5)])

        if con.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0:
            con.execute("INSERT INTO settings(id,peak_mode,auto_open_threshold,event_name,updated_at) VALUES (1,0,5,'',?)",
                        (now().isoformat(),))

        if con.execute("SELECT COUNT(*) FROM combos").fetchone()[0] == 0:
            con.executemany("""INSERT INTO combos(name,description,item_names,price,is_active)
                VALUES (?,?,?,?,1)""", [
                ("Break-Time Combo","Samosa + Tea",json.dumps(["Samosa","Tea"]),24),
                ("Snack Combo","Veg Puff + Tea",json.dumps(["Veg Puff","Tea"]),28),
            ])


