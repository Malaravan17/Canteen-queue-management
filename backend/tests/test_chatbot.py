from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import canteen.database as database
import canteen.chatbot_service as chatbot_service
from canteen.gemini_service import GeminiUnavailable, generate_reply
from main import app


@pytest.fixture
def client_and_context(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "canteen.db")
    captured = []

    def fake_reply(message, history, context):
        captured.append({"message": message, "history": list(history), "context": context})
        return "Verified test reply."

    monkeypatch.setattr(chatbot_service, "generate_reply", fake_reply)
    with TestClient(app) as client:
        yield client, captured
    with chatbot_service._session_lock:
        chatbot_service._sessions.clear()


def post_chat(client, message, **extra):
    return client.post("/api/chat", json={"message": message, "session_id": "test_session_123", **extra})


def test_menu_price_stock_and_followup_context(client_and_context):
    client, captured = client_and_context
    response = post_chat(client, "What is the price of Masala Dosa?")
    assert response.status_code == 200
    assert captured[-1]["context"]["menu_items"][0]["price_inr"] == 45
    assert captured[-1]["context"]["menu_items"][0]["can_order_now"] is True

    followup = post_chat(client, "Is it in stock?")
    assert followup.status_code == 200
    assert any(row["name"] == "Masala Dosa" for row in captured[-1]["context"]["menu_items"])
    assert "test_session_123" == response.json()["session_id"] == followup.json()["session_id"]


def test_available_food_and_unrelated_question_context(client_and_context):
    client, captured = client_and_context
    assert post_chat(client, "Show available food").status_code == 200
    names = [row["name"] for row in captured[-1]["context"]["menu_items"]]
    assert "Tea" in names
    assert post_chat(client, "Can you solve this unrelated algebra problem?").status_code == 200
    assert captured[-1]["context"] == {}


def test_order_lookup_requires_matching_private_token(client_and_context):
    client, captured = client_and_context
    item_id = client.get("/api/items").json()[0]["id"]
    placed = client.post("/api/orders", json={"customer_name":"Private Student", "items":[{"item_id":str(item_id),"quantity":1}]})
    assert placed.status_code == 200
    order = placed.json()
    token = order["tracking_token"]

    no_secret = post_chat(client, f"What is order {order['id']} status?")
    assert no_secret.status_code == 200
    assert captured[-1]["context"]["order_lookup"]["verified"] is False
    assert "Private Student" not in str(captured[-1]["context"])

    wrong_secret = post_chat(client, "Where is my order?", order_id=str(order["id"]), tracking_token="x" * 32)
    assert wrong_secret.status_code == 200
    assert captured[-1]["context"]["order_lookup"]["verified"] is False

    verified = post_chat(client, "Where is my order and which counter?", order_id=str(order["id"]), tracking_token=token)
    assert verified.status_code == 200
    safe_order = captured[-1]["context"]["order_lookup"]
    assert safe_order["verified"] is True
    assert "customer_name" not in safe_order and "payment_status" not in safe_order
    assert "tracking_token" not in safe_order and "token_hash" not in str(safe_order)
    token_in_message = post_chat(client, f"Where is my order? {token}", order_id=str(order["id"]), tracking_token=token)
    assert token_in_message.status_code == 200
    assert token not in captured[-1]["message"]
    # The public order endpoint is also protected with the same credential.
    assert client.get(f"/api/orders/{order['id']}").status_code == 404
    assert client.get(f"/api/orders/{order['id']}", headers={"X-Order-Tracking-Token":token}).status_code == 200


def test_popularity_uses_order_history_not_model_claims(client_and_context):
    client, captured = client_and_context
    item = client.get("/api/items").json()[0]
    placed = client.post("/api/orders", json={"customer_name":"Student", "items":[{"item_id":str(item["id"]),"quantity":2}]})
    assert placed.status_code == 200
    response = post_chat(client, "What are today's most popular items?")
    assert response.status_code == 200
    assert captured[-1]["context"]["popular_items_from_recent_order_history"][0] == {"item_name":item["name"],"quantity_ordered":2}


def test_chat_api_rejects_empty_and_oversized_messages(client_and_context):
    client, _ = client_and_context
    assert client.post("/api/chat", json={"message":"   "}).status_code == 422
    assert client.post("/api/chat", json={"message":"x" * 1001}).status_code == 422


def test_completed_order_releases_counter_for_next_load_based_allocation(client_and_context):
    client, _ = client_and_context
    item_id = client.get("/api/items").json()[0]["id"]
    payload = {"customer_name":"Student", "items":[{"item_id":str(item_id),"quantity":1}]}
    first = client.post("/api/orders", json=payload).json()
    assert first["counter_number"] == 1
    assert client.patch("/api/counters/2", json={"is_active":True}).status_code == 200
    second = client.post("/api/orders", json=payload).json()
    assert second["counter_number"] == 2

    # With equal active workloads the existing formula breaks ties by counter number.
    third_while_busy = client.post("/api/orders", json=payload).json()
    assert third_while_busy["counter_number"] == 1

    assert client.post(f"/api/orders/{second['id']}/payment", json={"method":"cash"}).status_code == 200
    completed = client.patch(f"/api/orders/{second['id']}/status", json={"status":"completed"})
    assert completed.status_code == 200
    # Completed orders no longer count toward the live load, so the same formula selects counter 2.
    next_order = client.post("/api/orders", json=payload).json()
    assert next_order["counter_number"] == 2


def test_manager_can_clear_operational_history_without_removing_menu_or_stock(client_and_context):
    client, _ = client_and_context
    item = client.get("/api/items").json()[0]
    order = client.post("/api/orders", json={"customer_name":"Student", "items":[{"item_id":str(item["id"]),"quantity":1}]})
    assert order.status_code == 200
    assert client.post("/api/admin/operational-data", json={"confirmation":"CLEAR DATA"}).status_code == 405
    assert client.request("DELETE", "/api/admin/operational-data", json={"confirmation":"wrong"}).status_code == 400
    response = client.request("DELETE", "/api/admin/operational-data", json={"confirmation":"CLEAR DATA"})
    assert response.status_code == 200
    assert client.get("/api/orders").json() == []
    assert any(row["id"] == item["id"] for row in client.get("/api/items").json())
    with database.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM inventory_logs").fetchone()[0] == 0


def test_missing_api_key_returns_safe_configuration_error(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(GeminiUnavailable) as error:
        generate_reply("hello", [], {})
    assert error.value.status_code == 503
    assert "not configured" in str(error.value)


def test_gemini_sdk_call_uses_configured_model_and_system_context(monkeypatch):
    from google import genai
    calls = []

    class FakeModels:
        def generate_content(self, **kwargs):
            calls.append(kwargs)
            return type("Result", (), {"text":"Hello from Gemini."})()

    class FakeClient:
        def __init__(self, **kwargs):
            self.models = FakeModels()

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test-flash")
    monkeypatch.setattr(genai, "Client", FakeClient)
    reply = generate_reply("Show the menu", [], {"menu_items":[{"name":"Tea","price_inr":12}]})
    assert reply == "Hello from Gemini."
    assert calls[0]["model"] == "gemini-test-flash"
    assert "VERIFIED_DATABASE_CONTEXT" in calls[0]["contents"][-1].parts[0].text
    assert calls[0]["config"].system_instruction


@pytest.mark.parametrize("failure,expected_status,expected_text", [
    (TimeoutError("read timed out"), 504, "too long"),
    (type("RateError", (Exception,), {"code":429})("resource exhausted"), 503, "quota or rate limit"),
    (type("ModelError", (Exception,), {"code":404})("model unavailable"), 503, "model was not found"),
    (type("AuthError", (Exception,), {"code":403})("permission denied"), 503, "gemini rejected this request"),
])
def test_gemini_timeout_and_rate_limit_are_friendly(monkeypatch, failure, expected_status, expected_text):
    from google import genai

    class FakeModels:
        def generate_content(self, **kwargs):
            raise failure

    class FakeClient:
        def __init__(self, **kwargs):
            self.models = FakeModels()

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(genai, "Client", FakeClient)
    with pytest.raises(GeminiUnavailable) as error:
        generate_reply("hello", [], {})
    assert error.value.status_code == expected_status
    assert expected_text in str(error.value).lower()

