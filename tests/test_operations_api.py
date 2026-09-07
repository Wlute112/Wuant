from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import asyncio
import json
import uuid

from fastapi import FastAPI
import pytest

from quant.api import operations, jobs_routes
from quant.ops.state import OperationsStore

TOKEN = "test-operator-token-with-at-least-32-characters"
JOB = "paper_test"
TARGET = f"strategy:{JOB}"


class ASGIClient:
    """Exercise FastAPI routing/auth/validation without an optional HTTP client."""
    def __init__(self, app, *, client=("127.0.0.1", 50000)):
        self.app, self.client, self.headers = app, client, {}

    def close(self):
        pass

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)

    def request(self, method, path, *, headers=None, json=None):
        import json as serializer
        body = serializer.dumps(json).encode() if json is not None else b""
        merged = {**self.headers, **(headers or {}), "Content-Type": "application/json"}
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
                 "query_string": b"", "root_path": "", "server": ("localhost", 8000),
                 "client": self.client, "headers": [(key.lower().encode(), value.encode()) for key, value in merged.items()]}
        messages = []
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}
        async def send(message):
            messages.append(message)
        asyncio.run(self.app(scope, receive, send))
        payload = b"".join(message.get("body", b"") for message in messages)
        return SimpleNamespace(status_code=messages[0]["status"], json=lambda: serializer.loads(payload))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_CONTROL_TOKEN", TOKEN)
    monkeypatch.setenv("QUANT_CONTROL_OPERATOR", "test-operator")
    monkeypatch.setattr(operations, "JOBS_DIR", tmp_path)
    job = {"id": JOB, "kind": "paper", "status": "running"}
    monkeypatch.setattr(jobs_routes, "manager", SimpleNamespace(get=lambda key: job if key == JOB else None))
    store = OperationsStore(str(tmp_path / "operations.sqlite3"))
    store.heartbeat(TARGET, "strategy", status="FROZEN", details={"resume_blockers": []})
    app = FastAPI()
    app.include_router(operations.router)
    client = ASGIClient(app)
    client.headers["Authorization"] = f"Bearer {TOKEN}"
    yield client, store, job
    client.close()
    store.close()


def body(action="FREEZE_ENTRIES"):
    return {"action": action, "reason": "Operator safety test", "request_id": str(uuid.uuid4()),
            "confirmation": f"{action} {TARGET}"}


def test_corporate_action_record_review_retry_and_cancel(setup):
    from quant.data.instrument_identity import EquityIdentity
    from quant.ops.corporate_actions import CorporateActionRegistry

    client, store, _ = setup
    registry = CorporateActionRegistry(store)
    account = "IB-DU1"
    registry.reconcile(account, [EquityIdentity(123, "ABC", "NYSE", "COMMON")],
                       broker_flat=True, now=datetime.now(timezone.utc))
    store.set_state(f"equity-identities:{TARGET}", {"account_id": account})
    path = f"/api/operations/{JOB}/corporate-actions"
    request = {"confirmation": f"RECORD CORPORATE ACTION {JOB}", "event": {
        "event_id": "split-1", "kind": "SPLIT", "con_id": 123,
        "effective_at": "2026-09-01T13:30:00Z", "split_ratio": "2",
        "source_reference": "issuer notice split-1"}}
    assert client.post(path, json=request, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post(path, json={**request, "confirmation": "wrong"}).status_code == 422
    first = client.post(path, json=request)
    assert first.status_code == 202
    assert client.post(path, json=request).json()["digest"] == first.json()["digest"]
    state = client.get(f"/api/operations/{JOB}").json()["corporate_actions"]
    assert state["identities"][0]["con_id"] == 123
    assert state["events"][0]["status"] == "PENDING"
    cancelled = client.post(path + "/split-1/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["events"][0]["status"] == "CANCELLED"
    assert store.verify_audit_chain()[0]


def test_corporate_actions_require_qualified_session(setup):
    client, _, _ = setup
    assert client.post(f"/api/operations/{JOB}/corporate-actions/unknown/cancel").status_code == 409


def test_authentication_authorization_and_origin_are_fail_closed(setup, monkeypatch):
    client, store, job = setup
    path = f"/api/operations/{JOB}"
    assert client.get(path).status_code == 200
    assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get(path, headers={"Origin": "https://evil.example"}).status_code == 403
    monkeypatch.delenv("QUANT_CONTROL_TOKEN")
    assert client.get(path).status_code == 503
    monkeypatch.setenv("QUANT_CONTROL_TOKEN", TOKEN)
    job["kind"] = "live"
    assert client.post(path + "/commands", json=body()).status_code == 403
    assert store.commands_for_target(TARGET) == []


def test_remote_peer_cannot_use_local_controls(setup):
    client, _, _ = setup
    remote = ASGIClient(client.app, client=("203.0.113.7", 123))
    assert remote.get(f"/api/operations/{JOB}", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 403


def test_request_audit_retry_and_confirmation(setup):
    client, store, _ = setup
    request = body("FLATTEN")
    path = f"/api/operations/{JOB}/commands"
    assert client.post(path, json={**request, "confirmation": "FLATTEN strategy:wrong"}).status_code == 422
    first = client.post(path, json=request)
    assert first.status_code == 202
    command = first.json()
    assert command["target"] == TARGET and command["status"] == "PENDING"
    assert command["payload"] == {"operator": "test-operator", "source": "dashboard"}
    assert TOKEN not in str(asdict(store.audit_events()[0]))
    assert client.post(path, json=request).json()["command_id"] == command["command_id"]
    claimed = store.claim_commands(TARGET, TARGET)
    assert len(claimed) == 1
    store.complete_command(command["command_id"], TARGET, success=True, result={"broker_flat_confirmed": True})
    assert client.post(path, json=request).json()["status"] == "COMPLETED"
    assert client.post(path, json={**request, "reason": "Different reason"}).status_code == 409
    assert store.verify_audit_chain()[0]


def test_resume_rejects_missing_stale_future_unknown_and_pending_evidence(setup):
    client, store, job = setup
    path = f"/api/operations/{JOB}/commands"
    for delta, details in [(11, {"resume_blockers": []}), (-10, {"resume_blockers": []}),
                           (0, {}), (0, {"resume_blockers": ["UNCERTAIN"]})]:
        store.heartbeat(TARGET, "strategy", details=details,
                        observed_at=datetime.now(timezone.utc) - timedelta(seconds=delta))
        assert client.post(path, json=body("RESUME_ENTRIES")).status_code == 409
    store.heartbeat(TARGET, "strategy", status="FROZEN", details={"resume_blockers": []})
    assert client.post(path, json=body("RESUME_ENTRIES")).status_code == 202
    assert client.post(path, json=body("RESUME_ENTRIES")).status_code == 409
    job["status"] = "cancelled"
    assert client.post(path, json=body()).status_code == 409


def test_only_unclaimed_commands_can_be_cancelled(setup):
    client, store, _ = setup
    path = f"/api/operations/{JOB}/commands"
    first = client.post(path, json=body()).json()
    assert client.post(f"{path}/{first['command_id']}/cancel").json()["status"] == "CANCELLED"
    assert store.claim_commands(TARGET, TARGET) == []
    second = client.post(path, json=body()).json()
    store.claim_commands(TARGET, TARGET)
    assert client.post(f"{path}/{second['command_id']}/cancel").status_code == 409


def test_command_and_audit_are_atomic_on_audit_failure(tmp_path, monkeypatch):
    store = OperationsStore(str(tmp_path / "ops.sqlite3"))
    def fail(*args, **kwargs):
        raise RuntimeError("disk failure")
    monkeypatch.setattr(store, "append_event", fail)
    with pytest.raises(RuntimeError):
        store.request_command(TARGET, "KILL", "test")
    assert store.commands_for_target(TARGET) == []
    store.close()


def test_concurrent_duplicate_requests_create_one_command_and_audit(tmp_path):
    path = str(tmp_path / "ops.sqlite3")
    store = OperationsStore(path)
    identifier = uuid.uuid4().hex
    def submit(_):
        worker = OperationsStore(path)
        try:
            return worker.request_command(TARGET, "FREEZE_ENTRIES", "test", command_id=identifier).command_id
        finally:
            worker.close()
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert set(pool.map(submit, range(8))) == {identifier}
    assert len(store.commands_for_target(TARGET)) == 1
    assert len(store.audit_events()) == 1
    assert store.verify_audit_chain()[0]
    store.close()
