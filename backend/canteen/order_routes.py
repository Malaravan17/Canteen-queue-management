import json
import secrets
from typing import Optional
from fastapi import APIRouter, Header, HTTPException
from .database import connect, now, row_dict
from .schemas import OrderInput, StatusInput, PaymentInput
from .services import get_order_by_id, get_order_with_tracking_token, hash_order_tracking_token, require_admin

router = APIRouter()

@router.post("/api/orders")
def create_order(payload: OrderInput):
    if not payload.items:
        raise HTTPException(400, "Add at least one item to your cart.")
    quantities = {}
    tracking_token = secrets.token_urlsafe(32)
    for line in payload.items:
        try:
            iid = int(line.item_id)
        except ValueError:
            raise HTTPException(400, "Invalid item in cart.")
        quantities[iid] = quantities.get(iid, 0) + line.quantity

    reserved = []
    try:
        with connect() as con:
            con.execute("BEGIN IMMEDIATE")
            item_snapshots = []
            for item_id, quantity in quantities.items():
                item = con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
                if not item or not item["is_available"] or item["stock_quantity"] < quantity:
                    raise HTTPException(409, "An item is out of stock or the requested quantity is no longer available. Refresh the menu.")
                con.execute("UPDATE items SET stock_quantity=stock_quantity-?,updated_at=? WHERE id=?",
                            (quantity, now().isoformat(), item_id))
                reserved.append((item_id, quantity, item["name"]))
                item_snapshots.append({
                    "item_id": str(item_id), "name": item["name"], "category": item["category"],
                    "quantity": quantity, "unit_price": float(item["price"]),
                    "line_total": round(float(item["price"]) * quantity, 2),
                    "prep_minutes": int(item["prep_minutes"]),
                })

            combo_discount = 0
            if payload.combo_id:
                try:
                    combo_id = int(payload.combo_id)
                except ValueError:
                    combo_id = -1
                combo = con.execute("SELECT * FROM combos WHERE id=? AND is_active=1", (combo_id,)).fetchone()
                if combo:
                    names = {i["name"] for i in item_snapshots}
                    combo_names = set(json.loads(combo["item_names"]))
                    if combo_names.issubset(names):
                        eligible_total = sum(i["unit_price"] for i in item_snapshots if i["name"] in combo_names)
                        combo_discount = max(0, round(eligible_total - float(combo["price"]), 2))

            subtotal = round(sum(i["line_total"] for i in item_snapshots), 2)
            total = max(0, round(subtotal - combo_discount, 2))
            prep = max(i["prep_minutes"] for i in item_snapshots) + max(0, len(item_snapshots) - 1)

            categories = {i["category"] for i in item_snapshots}
            counter_rows = con.execute("SELECT * FROM counters WHERE is_active=1 ORDER BY number").fetchall()
            active = [row_dict(r) for r in counter_rows]
            eligible = [c for c in active if categories.issubset(set(c["supported_categories"]))]
            if not eligible:
                eligible = active
            if not eligible:
                raise HTTPException(503, "No counters are open. Please try again shortly.")

            def estimated_for(number):
                orders = con.execute("""SELECT estimated_prep_minutes,status FROM orders
                    WHERE counter_number=? AND status IN ('queued','preparing','ready')""", (number,)).fetchall()
                return sum(max(1, int(o["estimated_prep_minutes"])) for o in orders if o["status"] in ("queued", "preparing"))
            counter = min(eligible, key=lambda c: (estimated_for(c["number"]) + prep, c["number"]))
            created = now().isoformat()
            cur = con.execute("""INSERT INTO orders
                (customer_name,items,subtotal,discount,total_amount,counter_number,status,payment_status,payment_method,
                 estimated_prep_minutes,estimated_wait_minutes,created_at,updated_at,ready_at,completed_at,paid_at)
                VALUES (?,?,?,?,?,?,'queued','pending',NULL,?,?,?,?,NULL,NULL,NULL)""",
                (payload.customer_name.strip() or "Student", json.dumps(item_snapshots), subtotal, combo_discount, total,
                 counter["number"], prep, estimated_for(counter["number"]) + prep, created, created))
            order_id = cur.lastrowid
            con.execute("INSERT INTO order_tracking_secrets(order_id,token_hash,created_at) VALUES (?,?,?)",
                        (order_id, hash_order_tracking_token(tracking_token), created))
            for item_id, quantity, item_name in reserved:
                con.execute("""INSERT INTO inventory_logs(item_id,item_name,quantity_change,reason,order_id,created_at)
                    VALUES (?,?,?,?,?,?)""",
                    (str(item_id), item_name, -quantity, f"Reserved for order {order_id}", str(order_id), created))

        created_order = get_order_by_id(order_id)
        created_order["tracking_token"] = tracking_token
        return created_order
    except HTTPException:
        # A failed transaction rolls back stock updates when the connection context exits.
        raise
    except Exception as exc:
        raise HTTPException(500, f"Could not create order: {exc}")


@router.get("/api/orders")
def list_orders(status: Optional[str] = None, limit: int = 100):
    limit = max(1, min(limit, 300))
    with connect() as con:
        if status:
            rows = con.execute("SELECT * FROM orders WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit)).fetchall()
        else:
            rows = con.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [row_dict(r) for r in rows]


@router.get("/api/orders/{order_id}")
def read_order(order_id: str, x_order_tracking_token: Optional[str] = Header(default=None)):
    return get_order_with_tracking_token(order_id, x_order_tracking_token or "")


@router.patch("/api/orders/{order_id}/status")
def update_order_status(order_id: str, payload: StatusInput, x_admin_key: Optional[str] = Header(default=None)):
    require_admin(x_admin_key)
    allowed = {"queued", "preparing", "ready", "completed", "cancelled"}
    if payload.status not in allowed:
        raise HTTPException(400, f"Status must be one of {', '.join(sorted(allowed))}.")
    order = get_order_by_id(order_id)
    old = order["status"]
    if old in ["completed", "cancelled"] and payload.status != old:
        raise HTTPException(409, "A completed or cancelled order cannot be reopened.")
    updates = {"status": payload.status, "updated_at": now().isoformat()}
    if payload.status == "ready":
        updates["ready_at"] = now().isoformat()
    if payload.status == "completed":
        if order.get("payment_status") != "paid":
            raise HTTPException(409, "Record successful payment before completing handover.")
        updates["completed_at"] = now().isoformat()
    if payload.status == "cancelled":
        if order.get("payment_status") == "paid":
            raise HTTPException(409, "Paid orders need a refund workflow before cancellation.")
        with connect() as con:
            con.execute("BEGIN IMMEDIATE")
            for line in order["items"]:
                con.execute("UPDATE items SET stock_quantity=stock_quantity+?,updated_at=? WHERE id=?",
                            (line["quantity"], now().isoformat(), int(line["item_id"])))
                con.execute("""INSERT INTO inventory_logs(item_id,item_name,quantity_change,reason,order_id,created_at)
                    VALUES (?,?,?,?,?,?)""",
                    (line["item_id"], line["name"], line["quantity"], f"Restored from cancelled order {order_id}", order_id, now().isoformat()))
            con.execute("UPDATE orders SET status=?,updated_at=? WHERE id=?", (payload.status, updates["updated_at"], int(order_id)))
        return get_order_by_id(order_id)
    with connect() as con:
        con.execute("""UPDATE orders SET status=?,updated_at=?,ready_at=COALESCE(?,ready_at),
            completed_at=COALESCE(?,completed_at) WHERE id=?""",
            (updates["status"], updates["updated_at"], updates.get("ready_at"), updates.get("completed_at"), int(order_id)))
    return get_order_by_id(order_id)


@router.post("/api/orders/{order_id}/payment")
def pay_order(order_id: str, payload: PaymentInput):
    if payload.method not in ["cash", "online"]:
        raise HTTPException(400, "Payment method must be cash or online.")
    order = get_order_by_id(order_id)
    if order["status"] in ["cancelled", "completed"]:
        raise HTTPException(409, "This order can no longer be paid.")
    if order.get("payment_status") == "paid":
        return {"order": order, "payment": "already_paid"}
    if payload.method == "online" and not payload.simulate_success:
        with connect() as con:
            con.execute("INSERT INTO payments(order_id,amount,method,status,is_simulated,created_at) VALUES (?,?,?,'failed',1,?)",
                        (order_id, order["total_amount"], "online", now().isoformat()))
        raise HTTPException(402, "Demo online payment failed. You can retry or choose cash.")
    paid_at = now().isoformat()
    with connect() as con:
        con.execute("INSERT INTO payments(order_id,amount,method,status,is_simulated,created_at) VALUES (?,?,?,'paid',?,?)",
                    (order_id, order["total_amount"], payload.method, int(payload.method == "online"), paid_at))
        con.execute("""UPDATE orders SET payment_status='paid',payment_method=?,paid_at=?,updated_at=? WHERE id=?""",
                    (payload.method, paid_at, paid_at, int(order_id)))
    return {"order": get_order_by_id(order_id), "payment": "paid", "simulated": payload.method == "online"}

