import sqlite3
from typing import Optional
from fastapi import APIRouter, Header, HTTPException
from .database import connect, now
from .schemas import QRScanInput
from .services import QR_COUNTER_CAPACITY, require_admin, qr_maybe_activate_next_counter, qr_waiting_count

router = APIRouter()

@router.post("/api/scan")
def scan_qr(payload: QRScanInput):
    customer_name = payload.name.strip() or "Customer"
    timestamp = now().isoformat()
    try:
        with connect() as con:
            con.execute("BEGIN IMMEDIATE")
            qr_maybe_activate_next_counter(con)
            active = con.execute(
                "SELECT number FROM counters WHERE is_active=1 ORDER BY number"
            ).fetchall()
            if not active:
                raise HTTPException(503, "No counters are open. Please contact a worker.")

            # Route to the active counter with the shortest live QR queue.
            loads = []
            for row in active:
                number = int(row["number"])
                waiting = qr_waiting_count(con, number)
                if waiting < QR_COUNTER_CAPACITY:
                    loads.append((waiting, number))
            if not loads:
                raise HTTPException(503, "All counters are at QR capacity. Please wait for a counter to become available.")
            _, assigned_counter = min(loads)

            cur = con.execute(
                """INSERT INTO qr_customers(name,counter_id,status,created_at)
                   VALUES (?,?, 'waiting', ?)""",
                (customer_name, assigned_counter, timestamp),
            )
            customer_id = int(cur.lastrowid)
            qr_maybe_activate_next_counter(con)
            customer = con.execute(
                "SELECT id,name,counter_id,status,created_at FROM qr_customers WHERE id=?",
                (customer_id,),
            ).fetchone()
        return {"customer": dict(customer)}
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise HTTPException(500, f"Could not assign a counter: {exc}")


@router.get("/api/state")
def qr_demo_state():
    with connect() as con:
        counter_rows = con.execute(
            "SELECT number,is_active FROM counters ORDER BY number"
        ).fetchall()
        counters = []
        for row in counter_rows:
            number = int(row["number"])
            waiting = qr_waiting_count(con, number)
            assigned_total = int(con.execute(
                "SELECT COUNT(*) FROM qr_customers WHERE counter_id=?", (number,)
            ).fetchone()[0])
            completed = int(con.execute(
                "SELECT COUNT(*) FROM qr_customers WHERE counter_id=? AND status='completed'",
                (number,),
            ).fetchone()[0])
            staff_notified = con.execute(
                "SELECT 1 FROM alerts WHERE type='qr_counter' AND message LIKE ? LIMIT 1",
                (f"%Counter {number}%",),
            ).fetchone() is not None
            counters.append({
                "id": number,
                "active": bool(row["is_active"]),
                "waiting": waiting,
                "capacity": QR_COUNTER_CAPACITY,
                "available_slots": max(0, QR_COUNTER_CAPACITY - waiting),
                "utilization_percent": min(100, round(waiting / QR_COUNTER_CAPACITY * 100)),
                "assigned_total": assigned_total,
                "completed": completed,
                "staff_notified": staff_notified,
            })

        customers = [dict(row) for row in con.execute(
            """SELECT id,name,counter_id,status,created_at
               FROM qr_customers ORDER BY id DESC LIMIT 50"""
        ).fetchall()]
        alert_rows = con.execute(
            """SELECT id,message,created_at,read FROM alerts
               WHERE type='qr_counter' ORDER BY id DESC LIMIT 30"""
        ).fetchall()
        alerts = [dict(row) for row in alert_rows]
        total_scans = int(con.execute(
            "SELECT COUNT(*) FROM qr_customers"
        ).fetchone()[0])

    return {
        "total_scans": total_scans,
        "total_counters": 4,
        "counters": counters,
        "customers": customers,
        "alerts": alerts,
    }


@router.post("/api/customers/{customer_id}/complete")
def complete_qr_customer(customer_id: int):
    timestamp = now().isoformat()
    with connect() as con:
        row = con.execute(
            "SELECT * FROM qr_customers WHERE id=?", (customer_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Customer ticket not found")
        if row["status"] == "waiting":
            con.execute(
                "UPDATE qr_customers SET status='completed',completed_at=? WHERE id=?",
                (timestamp, customer_id),
            )
        updated = con.execute(
            "SELECT id,name,counter_id,status,created_at,completed_at FROM qr_customers WHERE id=?",
            (customer_id,),
        ).fetchone()
    return {"customer": dict(updated)}


@router.post("/api/alerts/{alert_id}/read")
def mark_qr_alert_read(alert_id: str, x_admin_key: Optional[str] = Header(default=None)):
    # The QR manager page uses this endpoint to acknowledge worker alerts.
    # The existing alert table is shared with the ordering features.
    require_admin(x_admin_key)
    try:
        alert_number = int(alert_id)
    except ValueError:
        raise HTTPException(404, "Alert not found")
    with connect() as con:
        cur = con.execute(
            "UPDATE alerts SET read=1 WHERE id=? AND type='qr_counter'",
            (alert_number,),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "QR counter alert not found")
        row = con.execute(
            "SELECT id,message,created_at,read FROM alerts WHERE id=?",
            (alert_number,),
        ).fetchone()
    return dict(row)


@router.post("/api/reset")
def reset_qr_demo():
    timestamp = now().isoformat()
    with connect() as con:
        con.execute("DELETE FROM qr_customers")
        con.execute("DELETE FROM alerts WHERE type='qr_counter'")
        con.execute("UPDATE counters SET is_active=CASE WHEN number=1 THEN 1 ELSE 0 END,updated_at=?",
                    (timestamp,))
        # SQLite AUTOINCREMENT is reset only for this demo table.
        con.execute("DELETE FROM sqlite_sequence WHERE name='qr_customers'")
    return {"status": "ok", "message": "QR demo reset. Only Counter 1 is active."}

