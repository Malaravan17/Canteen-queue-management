from typing import Optional
from fastapi import APIRouter, Header, HTTPException
from .database import connect, now, row_dict
from .schemas import ItemInput, StockInput, CounterInput
from .services import active_queue_for_counter, queue_estimate, require_admin

router = APIRouter()

@router.get("/api/items")
def list_items(available_only: bool = False):
    sql = "SELECT * FROM items"
    if available_only:
        sql += " WHERE is_available=1 AND stock_quantity>0"
    sql += " ORDER BY category,name"
    with connect() as con:
        rows = con.execute(sql).fetchall()
    return [row_dict(r) for r in rows]


@router.post("/api/items")
def create_item(payload: ItemInput, x_admin_key: Optional[str] = Header(default=None)):
    require_admin(x_admin_key)
    created = now().isoformat()
    with connect() as con:
        cur = con.execute("""INSERT INTO items
            (name,category,price,stock_quantity,prep_minutes,is_available,image_emoji,description,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (payload.name.strip(), payload.category, payload.price, payload.stock_quantity, payload.prep_minutes,
             int(payload.is_available), payload.image_emoji, payload.description, created, created))
        row = con.execute("SELECT * FROM items WHERE id=?", (cur.lastrowid,)).fetchone()
    return row_dict(row)


@router.patch("/api/items/{item_id}/stock")
def update_stock(item_id: str, payload: StockInput, x_admin_key: Optional[str] = Header(default=None)):
    require_admin(x_admin_key)
    try:
        iid = int(item_id)
    except ValueError:
        raise HTTPException(404, "Item not found")
    with connect() as con:
        item = con.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
        if not item:
            raise HTTPException(404, "Item not found")
        con.execute("UPDATE items SET stock_quantity=?,updated_at=? WHERE id=?", (payload.stock_quantity, now().isoformat(), iid))
        con.execute("""INSERT INTO inventory_logs(item_id,item_name,quantity_change,reason,created_at)
            VALUES (?,?,?,?,?)""", (str(iid), item["name"], payload.stock_quantity, "Admin stock update (new total)", now().isoformat()))
        row = con.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
    return row_dict(row)


@router.get("/api/counters")
def list_counters():
    with connect() as con:
        rows = con.execute("SELECT * FROM counters ORDER BY number").fetchall()
        today_start = now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        output = []
        for raw in rows:
            c = row_dict(raw)
            queue = active_queue_for_counter(c["number"])
            c.update({
                "waiting": len([o for o in queue if o["status"] in ["queued", "preparing"]]),
                "queue_length": len(queue),
                "estimated_wait_minutes": queue_estimate(c["number"]),
                "completed_today": con.execute("""SELECT COUNT(*) FROM orders
                    WHERE counter_number=? AND status='completed' AND created_at>=?""", (c["number"], today_start)).fetchone()[0],
            })
            output.append(c)
    return output


@router.patch("/api/counters/{counter_number}")
def set_counter(counter_number: int, payload: CounterInput, x_admin_key: Optional[str] = Header(default=None)):
    require_admin(x_admin_key)
    if counter_number < 1 or counter_number > 4:
        raise HTTPException(400, "There are 4 configured counters.")
    with connect() as con:
        existing = con.execute("SELECT * FROM counters WHERE number=?", (counter_number,)).fetchone()
        if not existing:
            raise HTTPException(404, "Counter not found")
        if not payload.is_active and con.execute("SELECT COUNT(*) FROM counters WHERE is_active=1").fetchone()[0] <= 1:
            raise HTTPException(409, "At least one counter must remain open.")
        active_orders = con.execute("SELECT COUNT(*) FROM orders WHERE counter_number=? AND status IN ('queued','preparing','ready')", (counter_number,)).fetchone()[0]
        if not payload.is_active and active_orders:
            raise HTTPException(409, "This counter still has active orders. Complete or reassign them first.")
        waiting_qr_customers = con.execute("SELECT COUNT(*) FROM qr_customers WHERE counter_id=? AND status='waiting'", (counter_number,)).fetchone()[0]
        if not payload.is_active and waiting_qr_customers:
            raise HTTPException(409, "This counter still has waiting QR customers. Complete their tickets first.")
        con.execute("UPDATE counters SET is_active=?,updated_at=? WHERE number=?", (int(payload.is_active), now().isoformat(), counter_number))
        con.execute("INSERT INTO alerts(message,created_at,read,type) VALUES (?,?,0,'counter')",
                    (f"Counter {counter_number} {'opened' if payload.is_active else 'closed'} by admin.", now().isoformat()))
        row = con.execute("SELECT * FROM counters WHERE number=?", (counter_number,)).fetchone()
    return row_dict(row)


@router.get("/api/combos")
def list_combos():
    with connect() as con:
        rows = con.execute("SELECT * FROM combos WHERE is_active=1 ORDER BY id").fetchall()
    return [row_dict(r) for r in rows]


@router.patch("/api/combos/{combo_id}/toggle")
def toggle_combo(combo_id: str, x_admin_key: Optional[str] = Header(default=None)):
    require_admin(x_admin_key)
    try:
        cid = int(combo_id)
    except ValueError:
        raise HTTPException(404, "Combo not found")
    with connect() as con:
        combo = con.execute("SELECT * FROM combos WHERE id=?", (cid,)).fetchone()
        if not combo:
            raise HTTPException(404, "Combo not found")
        con.execute("UPDATE combos SET is_active=? WHERE id=?", (0 if combo["is_active"] else 1, cid))
        return row_dict(con.execute("SELECT * FROM combos WHERE id=?", (cid,)).fetchone())

