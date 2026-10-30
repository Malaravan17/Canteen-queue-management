from datetime import timedelta
from typing import Optional
from fastapi import APIRouter, Header, HTTPException
from .database import connect, now, row_dict
from .schemas import SettingsInput, ClearOperationalDataInput
from .services import QR_COUNTER_CAPACITY, require_admin

router = APIRouter()


@router.delete("/api/admin/operational-data")
def clear_operational_data(payload: ClearOperationalDataInput, x_admin_key: Optional[str] = Header(default=None)):
    """Clear operational history while preserving menu, stock, counter and settings configuration."""
    require_admin(x_admin_key)
    if payload.confirmation != "CLEAR DATA":
        raise HTTPException(400, "Type CLEAR DATA to confirm this action.")
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        # Delete child/transaction tables before their parent records.
        for table in ("payments", "order_tracking_secrets", "orders", "inventory_logs",
                      "daily_wastage_records", "qr_customers", "alerts"):
            con.execute(f"DELETE FROM {table}")
        for table in ("payments", "orders", "inventory_logs", "daily_wastage_records", "qr_customers", "alerts"):
            con.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))
    return {"status": "ok", "message": "Operational history cleared. Menu, current stock, counters and settings were preserved."}

@router.get("/api/settings")
def get_settings():
    with connect() as con:
        row = con.execute("SELECT * FROM settings WHERE id=1").fetchone()
    return row_dict(row) or {"peak_mode": False, "auto_open_threshold": 5, "event_name": ""}


@router.patch("/api/settings")
def update_settings(payload: SettingsInput, x_admin_key: Optional[str] = Header(default=None)):
    require_admin(x_admin_key)
    with connect() as con:
        con.execute("""UPDATE settings SET peak_mode=?,auto_open_threshold=?,event_name=?,updated_at=? WHERE id=1""",
                    (int(payload.peak_mode), payload.auto_open_threshold, payload.event_name, now().isoformat()))
        row = con.execute("SELECT * FROM settings WHERE id=1").fetchone()
    return row_dict(row)


@router.get("/api/analytics")
def analytics():
    start = now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    week_ago = (now() - timedelta(days=7)).isoformat()
    with connect() as con:
        order_rows = con.execute("SELECT * FROM orders WHERE created_at>=?", (start,)).fetchall()
        all_items = [row_dict(r) for r in con.execute("SELECT * FROM items").fetchall()]
        all_counters = [row_dict(r) for r in con.execute("SELECT * FROM counters ORDER BY number").fetchall()]
        past_rows = [row_dict(r) for r in con.execute("SELECT * FROM orders WHERE created_at>=?", (week_ago,)).fetchall()]
        qr_scans_today = int(con.execute("SELECT COUNT(*) FROM qr_customers WHERE created_at>=?", (start,)).fetchone()[0])
        total_qr_scans = int(con.execute("SELECT COUNT(*) FROM qr_customers").fetchone()[0])
        qr_waiting_by_counter = {
            int(row["counter_id"]): int(row["waiting"])
            for row in con.execute("""SELECT counter_id,COUNT(*) AS waiting FROM qr_customers
                WHERE status='waiting' GROUP BY counter_id""").fetchall()
        }
    orders = [row_dict(r) for r in order_rows]
    completed = [o for o in orders if o.get("status") == "completed"]
    paid = [o for o in orders if o.get("payment_status") == "paid"]
    sold = {}
    for order in orders:
        if order.get("status") == "cancelled":
            continue
        for line in order.get("items", []):
            sold[line["name"]] = sold.get(line["name"], 0) + line["quantity"]
    top = sorted([{"name": k, "quantity": v} for k, v in sold.items()], key=lambda x: x["quantity"], reverse=True)[:5]
    item_stats = []
    for item in all_items:
        qty = sold.get(item["name"], 0)
        recent = 0
        for order in past_rows:
            if order.get("status") == "cancelled":
                continue
            recent += sum(line["quantity"] for line in order.get("items", []) if line["item_id"] == str(item["id"]))
        predicted = max(qty, round(recent / 7 * 1.2))
        item_stats.append({
            "item_id": str(item["id"]), "name": item["name"], "stock_quantity": item["stock_quantity"],
            "sold_today": qty, "predicted_next_day_demand": predicted,
            "recommended_restock": max(0, predicted - item["stock_quantity"]),
            "low_stock": item["stock_quantity"] <= max(5, predicted * 0.2),
        })
    counter_stats = []
    for counter in all_counters:
        counter_orders = [o for o in completed if o.get("counter_number") == counter["number"]]
        qr_waiting = qr_waiting_by_counter.get(counter["number"], 0)
        counter_stats.append({
            "counter_number": counter["number"], "name": counter["name"], "is_active": counter["is_active"],
            "completed_orders": len(counter_orders),
            "qr_waiting": qr_waiting,
            "qr_capacity": QR_COUNTER_CAPACITY if counter["is_active"] else 0,
            "qr_available_slots": max(0, QR_COUNTER_CAPACITY - qr_waiting) if counter["is_active"] else 0,
            "qr_utilization_percent": min(100, round(qr_waiting / QR_COUNTER_CAPACITY * 100)) if counter["is_active"] else 0,
        })
    active_counter_count = sum(1 for counter in counter_stats if counter["is_active"])
    total_qr_waiting = sum(counter["qr_waiting"] for counter in counter_stats)
    idle_counter_count = sum(1 for counter in counter_stats if counter["is_active"] and counter["qr_waiting"] == 0)
    active_qr_capacity = active_counter_count * QR_COUNTER_CAPACITY
    inactive_counter_available = any(not counter["is_active"] for counter in counter_stats)
    if total_qr_waiting == 0:
        queue_insight = "No QR students are waiting. Open counters are ready to serve incoming students."
    elif idle_counter_count:
        queue_insight = f"{idle_counter_count} active counter(s) have no QR queue while {total_qr_waiting} student(s) wait. QR scans are routed to the shortest active queue; direct students to an idle counter to spread demand."
    elif inactive_counter_available and active_qr_capacity and total_qr_waiting >= active_qr_capacity:
        queue_insight = f"All {active_qr_capacity} QR places at active counters are occupied. The next counter opens automatically as QR demand reaches capacity."
    else:
        queue_insight = f"{total_qr_waiting} student(s) are waiting across {active_counter_count} active counter(s). Each counter holds up to {QR_COUNTER_CAPACITY} waiting QR students; the next counter opens automatically when active capacity fills."
    return {
        "orders_today": len(orders), "completed_today": len(completed),
        "paid_sales_today": round(sum(float(o.get("total_amount", 0)) for o in paid), 2),
        "total_qr_scans": total_qr_scans,
        "qr_scans_today": qr_scans_today,
        "total_qr_waiting": total_qr_waiting,
        "active_counter_count": active_counter_count,
        "idle_counter_count": idle_counter_count,
        "active_qr_capacity": active_qr_capacity,
        "queue_insight": queue_insight,
        "top_items": top, "stock_recommendations": item_stats, "counters": counter_stats,
        "peak_hours_note": "QR scans are assigned to the active counter with the shortest live queue. A new counter opens automatically when all current QR capacity is occupied.",
    }


@router.get("/api/alerts")
def list_alerts():
    with connect() as con:
        rows = con.execute("SELECT * FROM alerts ORDER BY created_at DESC LIMIT 30").fetchall()
    return [row_dict(row) for row in rows]
