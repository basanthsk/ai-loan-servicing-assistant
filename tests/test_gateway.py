"""Gateway end to end (offline): auth, login, WebSocket chat, account API, fault switch guard."""

from conftest import FakeChain, wire_hub
from fastapi.testclient import TestClient


def make_client(system_apps, chain):
    from gateway import app as gw

    client = TestClient(gw.app)
    client.__enter__()
    a = gw.state["assistant"]
    wire_hub(a.hub, a.settings, system_apps)
    a.orchestrator.chain = chain
    a.orchestrator.decider.llm.chain = chain
    return client


def test_requests_without_token_are_refused(system_apps):
    client = make_client(system_apps, FakeChain([]))
    try:
        assert client.get("/api/account").status_code == 401
        assert (
            client.get("/api/account", headers={"Authorization": "Bearer nonsense"}).status_code
            == 401
        )
    finally:
        client.__exit__(None, None, None)


def test_login_chat_and_account(system_apps):
    client = make_client(system_apps, FakeChain([], decisions={"next EMI": "loan_next_emi"}))
    try:
        login = client.post("/api/login", json={"customer_id": "C1001"}).json()
        assert login["name"] == "Priya Sharma"
        token = login["token"]

        with client.websocket_connect(f"/ws?token={token}") as ws:
            assert ws.receive_json()["type"] == "ready"
            ws.send_json({"type": "message", "text": "What's my next EMI?"})
            events = []
            while True:
                e = ws.receive_json()
                events.append(e)
                if e["type"] == "done":
                    break
        assert events[0] == {"type": "user", "text": "What's my next EMI?", "channel": "text"}
        assert "₹12,926" in events[-1]["text"]
        assert events[-1]["trace"]["spans"]

        acct = client.get("/api/account", headers={"Authorization": f"Bearer {token}"}).json()
        assert {lo["loan_id"] for lo in acct["loans"]} == {"L-1042", "L-1043"}
    finally:
        client.__exit__(None, None, None)


def test_websocket_rejects_bad_token(system_apps):
    client = make_client(system_apps, FakeChain([]))
    try:
        with client.websocket_connect("/ws?token=bad") as ws:
            assert ws.receive_json()["type"] == "error"
    finally:
        client.__exit__(None, None, None)
