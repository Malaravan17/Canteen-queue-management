"""Controlled database-context retrieval, order verification, and chat sessions."""
from __future__ import annotations

from collections import Counter
import hmac
import json
import re
import threading
import time
import uuid

from .database import connect
from .gemini_service import generate_reply
from .services import hash_order_tracking_token

SESSION_TTL_SECONDS = 30 * 60
MAX_SESSIONS = 1000
MAX_HISTORY_MESSAGES = 12
_sessions: dict[str, dict] = {}
_session_lock = threading.Lock()

ORDER_TERMS = re.compile(r"\b(track|tracking|status|my order|assigned counter|my counter|pickup status)\b", re.I)
MENU_TERMS = re.compile(r"\b(menu|food|items?|available|availability|price|cost|stock|recommend|breakfast|lunch|snacks?|drinks?|desserts?|tea|dosa|idli|samosa|sandwich|puff|juice)\b", re.I)
POPULAR_TERMS = re.compile(r"\b(popular|best[ -]?selling|top seller|most sold|favourites?)\b", re.I)
FOLLOWUP_TERMS = re.compile(r"\b(it|they|those|that|them|how much|what about|and the|and what)\b", re.I)


def _json_list(value):
    try:
        decoded = json.loads(value or "[]")
        return decoded if isinstance(decoded, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def _get_session(session_id: str | None) -> tuple[str, list[dict]]:
    current_time = time.monotonic()
    with _session_lock:
        expired = [key for key, val in _sessions.items() if current_time - val["updated"] > SESSION_TTL_SECONDS]
        for key in expired:
            _sessions.pop(key, None)
        session_id = session_id or str(uuid.uuid4())
        session = _sessions.get(session_id)
        if session is None:
            if len(_sessions) >= MAX_SESSIONS:
                oldest = min(_sessions, key=lambda key: _sessions[key]["updated"])
                _sessions.pop(oldest, None)
            session = {"history": [], "updated": current_time}
            _sessions[session_id] = session
        session["updated"] = current_time
        return session_id, list(session["history"])


def _save_session(session_id: str, history: list[dict]) -> None:
    with _session_lock:
        if session_id in _sessions:
            _sessions[session_id] = {"history": history[-MAX_HISTORY_MESSAGES:], "updated": time.monotonic()}


def _safe_order(order_id: str, token: str) -> dict | None:
    try:
        numeric_id = int(order_id)
    except (TypeError, ValueError):
        return None
    with connect() as con:
        row = con.execute("""SELECT o.id,o.items,o.status,o.counter_number,o.estimated_wait_minutes,
            o.created_at,c.name AS counter_name,c.is_active AS counter_active,s.token_hash
            FROM orders o LEFT JOIN counters c ON c.number=o.counter_number
            JOIN order_tracking_secrets s ON s.order_id=o.id WHERE o.id=?""", (numeric_id,)).fetchone()
    if not row or not hmac.compare_digest(hash_order_tracking_token(token), row["token_hash"]):
        return None
    try:
        lines = json.loads(row["items"])
    except (TypeError, json.JSONDecodeError):
        lines = []
    # Never send customer name, payment data, or the tracking secret to Gemini.
    return {
        "verified": True,
        "order_id": row["id"],
        "items": [{"name": line.get("name"), "quantity": line.get("quantity")} for line in lines if isinstance(line, dict)],
        "status": row["status"],
        "counter": {"number": row["counter_number"], "name": row["counter_name"], "active": bool(row["counter_active"])},
        "estimated_wait_minutes": row["estimated_wait_minutes"],
        "created_at": row["created_at"],
    }


def _menu_context(con, query: str) -> list[dict]:
    lower = query.casefold()
    all_names = con.execute("SELECT name FROM items ORDER BY name").fetchall()
    mentioned = [row["name"] for row in all_names if row["name"].casefold() in lower]
    categories = con.execute("SELECT DISTINCT category FROM items").fetchall()
    category = next((row["category"] for row in categories if row["category"].casefold() in lower), None)
    if mentioned:
        placeholders = ",".join("?" for _ in mentioned)
        rows = con.execute(f"""SELECT name,category,price,stock_quantity,is_available,description
            FROM items WHERE name IN ({placeholders}) ORDER BY name""", tuple(mentioned)).fetchall()
    elif category:
        rows = con.execute("""SELECT name,category,price,stock_quantity,is_available,description
            FROM items WHERE category=? ORDER BY name LIMIT 30""", (category,)).fetchall()
    else:
        rows = con.execute("""SELECT name,category,price,stock_quantity,is_available,description
            FROM items WHERE is_available=1 AND stock_quantity>0 ORDER BY category,name LIMIT 30""").fetchall()
    return [{
        "name": row["name"], "category": row["category"], "price_inr": row["price"],
        "stock_quantity": row["stock_quantity"],
        "can_order_now": bool(row["is_available"] and row["stock_quantity"] > 0),
        "description": row["description"],
    } for row in rows]


def _popular_context(con) -> list[dict]:
    rows = con.execute("""SELECT items FROM orders WHERE status!='cancelled'
        ORDER BY created_at DESC LIMIT 500""").fetchall()
    totals = Counter()
    for row in rows:
        try:
            lines = json.loads(row["items"])
        except (TypeError, json.JSONDecodeError):
            continue
        for line in lines:
            if isinstance(line, dict) and line.get("name"):
                totals[str(line["name"])] += max(0, int(line.get("quantity", 0)))
    return [{"item_name": name, "quantity_ordered": qty} for name, qty in totals.most_common(5)]


def retrieve_context(message: str, history: list[dict], order_id: str | None = None, tracking_token: str | None = None) -> tuple[dict, bool]:
    history_text = " ".join(entry["text"] for entry in history[-6:])
    use_followup_context = bool(history and FOLLOWUP_TERMS.search(message))
    query = f"{history_text} {message}" if use_followup_context else message
    context: dict = {}
    explicit_order_id = bool(re.search(r"\border\s*(?:#|number|id)?\s*\d+\b", message, re.I))
    order_question = bool(ORDER_TERMS.search(message)) or explicit_order_id or bool(order_id and tracking_token and re.search(r"\b(it|that order|that one)\b", message, re.I))
    verified_order = _safe_order(order_id, tracking_token) if order_question and order_id and tracking_token else None

    with connect() as con:
        if MENU_TERMS.search(message) or (use_followup_context and MENU_TERMS.search(history_text)):
            context["menu_items"] = _menu_context(con, query)
        if POPULAR_TERMS.search(message) or (use_followup_context and POPULAR_TERMS.search(history_text)):
            context["popular_items_from_recent_order_history"] = _popular_context(con)
        if "counter" in query.casefold() and not verified_order:
            rows = con.execute("SELECT number,name,is_active,supported_categories FROM counters ORDER BY number").fetchall()
            context["counters"] = [{"number": row["number"], "name": row["name"],
                "active": bool(row["is_active"]), "supported_categories": _json_list(row["supported_categories"])} for row in rows]
        if any(term in query.casefold() for term in ("wait time", "waiting time", "queue wait")) and not order_question:
            rows = con.execute("""SELECT c.number,c.name,c.is_active,COUNT(o.id) AS queued_orders,
                COALESCE(SUM(CASE WHEN o.status IN ('queued','preparing') THEN o.estimated_prep_minutes ELSE 0 END),0) AS estimated_queue_minutes
                FROM counters c LEFT JOIN orders o ON o.counter_number=c.number
                AND o.status IN ('queued','preparing','ready') GROUP BY c.number,c.name,c.is_active ORDER BY c.number""").fetchall()
            context["live_counter_queue_summary"] = [{"counter": row["name"], "active": bool(row["is_active"]),
                "queued_orders": row["queued_orders"], "estimated_queue_minutes": row["estimated_queue_minutes"]} for row in rows]

    if order_question:
        context["order_lookup"] = verified_order or {
            "verified": False,
            "note": "Order-specific information is unavailable unless both the matching numeric order ID and its private tracking token are verified. Older orders without a token cannot be verified by this assistant.",
        }
    guidance = {}
    if any(term in query.casefold() for term in ("how do i place", "how to order", "place an order", "how can i order")):
        guidance["ordering"] = "Browse the student menu, add items to the cart, continue to checkout, enter a name, choose an available payment option, and submit the order."
    if any(term in query.casefold() for term in ("how do i track", "how to track", "track my order")):
        guidance["order_tracking"] = "Use the order number and private tracking code shown after checkout in the Track your order form. Do not share the tracking code."
    if guidance:
        context["app_guidance"] = guidance
    return context, bool(order_question and verified_order)


def chat(message: str, session_id: str | None, order_id: str | None = None, tracking_token: str | None = None) -> dict:
    session_id, history = _get_session(session_id)
    safe_message = message.replace(tracking_token, "[private tracking code redacted]") if tracking_token else message
    context, is_verified_order_turn = retrieve_context(safe_message, history, order_id, tracking_token)
    reply = generate_reply(safe_message, history, context)
    # A private order reply is never retained in session history. The owner must
    # provide the token again for each order-specific follow-up.
    if not is_verified_order_turn:
        _save_session(session_id, history + [
            {"role": "user", "text": safe_message},
            {"role": "model", "text": reply},
        ])
    return {"reply": reply, "session_id": session_id, "order_verified": is_verified_order_turn}
