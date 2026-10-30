from typing import Optional
from fastapi import HTTPException
from .database import ADMIN_DEMO_KEY, connect, now, row_dict

QR_COUNTER_CAPACITY = 10

def require_admin(x_admin_key: Optional[str]):
    if ADMIN_DEMO_KEY != "change-this-before-deployment" and x_admin_key != ADMIN_DEMO_KEY:
        raise HTTPException(401, "Admin key missing or invalid")


def get_order_by_id(order_id):
    try:
        oid = int(order_id)
    except (TypeError, ValueError):
        raise HTTPException(404, "Order not found")
    with connect() as con:
        row = con.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not row:
        raise HTTPException(404, "Order not found")
    return row_dict(row)


def active_queue_for_counter(number):
    with connect() as con:
        rows = con.execute("""SELECT * FROM orders
            WHERE counter_number=? AND status IN ('queued','preparing','ready')
            ORDER BY created_at ASC""", (number,)).fetchall()
    return [row_dict(r) for r in rows]


def order_work_minutes(order):
    return max(1, int(order.get("estimated_prep_minutes", 3)))


def queue_estimate(counter_number, new_prep=0):
    orders = active_queue_for_counter(counter_number)
    remaining = sum(order_work_minutes(o) for o in orders if o.get("status") in ["queued", "preparing"])
    return max(0, remaining) + max(0, int(new_prep))


def choose_counter(items, order_prep):
    categories = {x["category"] for x in items}
    with connect() as con:
        rows = con.execute("SELECT * FROM counters WHERE is_active=1 ORDER BY number").fetchall()
    active = [row_dict(r) for r in rows]
    eligible = [c for c in active if categories.issubset(set(c.get("supported_categories", [])))]
    if not eligible:
        eligible = active
    if not eligible:
        raise HTTPException(503, "No counters are open. Please try again shortly.")
    return min(eligible, key=lambda c: (queue_estimate(c["number"], order_prep), c["number"]))


def qr_waiting_count(con, counter_number=None):
    if counter_number is None:
        row = con.execute(
            "SELECT COUNT(*) FROM qr_customers WHERE status='waiting'"
        ).fetchone()
    else:
        row = con.execute(
            "SELECT COUNT(*) FROM qr_customers WHERE status='waiting' AND counter_id=?",
            (counter_number,),
        ).fetchone()
    return int(row[0])


def qr_maybe_activate_next_counter(con):
    active_numbers = [int(row["number"]) for row in con.execute(
        "SELECT number FROM counters WHERE is_active=1"
    ).fetchall()]
    active_count = len(active_numbers)
    waiting = sum(qr_waiting_count(con, number) for number in active_numbers)
    next_counter = con.execute(
        "SELECT number FROM counters WHERE is_active=0 ORDER BY number LIMIT 1"
    ).fetchone()

    # Each active counter can hold ten waiting QR customers. Open the next
    # counter as soon as all currently active capacity is occupied.
    if next_counter and active_count < 4 and waiting >= active_count * QR_COUNTER_CAPACITY:
        number = int(next_counter["number"])
        timestamp = now().isoformat()
        con.execute(
            "UPDATE counters SET is_active=1,updated_at=? WHERE number=?",
            (timestamp, number),
        )
        con.execute(
            "INSERT INTO alerts(message,created_at,read,type) VALUES (?,?,0,'qr_counter')",
            (f"QR queue reached {waiting} students. Counter {number} opened automatically (capacity: {QR_COUNTER_CAPACITY} per counter).", timestamp),
        )
        return number
    return None