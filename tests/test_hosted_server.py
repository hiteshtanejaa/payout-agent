"""Hosted-demo boundaries, jobs, and signed Stripe delivery retries.

All credentials are fake, dotenv loading is disabled, memory is stubbed, and
the ledger is temporary. These tests never contact Stripe, Cognee, or a DB.
Run with: python -m pytest tests/test_hosted_server.py -q
"""
import base64
import hashlib
import hmac
import importlib
import io
import json
import sys
import threading
import time
import types
from pathlib import Path

import dotenv
import pytest
import stripe
from fastapi.testclient import TestClient


@pytest.fixture
def server_module(monkeypatch, tmp_path):
    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    # _Tee writes to the original console; a UTF-8-capable sink keeps Windows
    # pytest capture independent from the user's cp1252 terminal encoding.
    monkeypatch.setattr(sys, "__stdout__", io.StringIO())
    for name in ("DYNO", "DATABASE_URL", "COGNEE_SERVICE_URL", "COGNEE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_hosted_server_tests")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_regular_test")
    monkeypatch.setenv("STRIPE_ISSUING_WEBHOOK_SECRET", "whsec_issuing_test")
    monkeypatch.setenv("STRIPE_CARD_ID", "ic_hosted_demo_test")
    monkeypatch.setenv("DEMO_USERNAME", "demo")
    monkeypatch.setenv("DEMO_PASSWORD", "fake-demo-password")
    monkeypatch.setenv("MEMORY_CHECK", "file")

    fake_memory = types.ModuleType("memory")
    fake_memory.HAVE_COGNEE = False
    fake_memory.remember_decision_sync = lambda rec: None
    monkeypatch.setitem(sys.modules, "memory", fake_memory)
    for name in ("server", "agent", "ledger"):
        monkeypatch.delitem(sys.modules, name, raising=False)

    def no_stripe_network(*args, **kwargs):
        raise AssertionError("A test unexpectedly attempted a Stripe API call")

    monkeypatch.setattr(stripe.issuing.Card, "retrieve", no_stripe_network)
    monkeypatch.setattr(stripe, "api_key", "sk_test_hosted_server_tests")
    module = importlib.import_module("server")
    module._CARD_CACHE.update(id="ic_hosted_demo_test", last4="4242", status="active")
    try:
        yield module
    finally:
        module._JOB_EXECUTOR.shutdown(wait=True, cancel_futures=True)


@pytest.fixture
def client(server_module):
    with TestClient(server_module.app) as test_client:
        yield test_client


def _login():
    token = base64.b64encode(b"demo:fake-demo-password").decode("ascii")
    return {"Authorization": "Basic " + token}


def _signed_event(event_type, obj, *, event_id="evt_test", secret="whsec_regular_test", live=False):
    timestamp = int(time.time())
    event = {"id": event_id, "object": "event", "created": timestamp,
             "type": event_type, "livemode": live, "data": {"object": obj}}
    payload = json.dumps(event, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), str(timestamp).encode("ascii") + b"." + payload,
                         hashlib.sha256).hexdigest()
    return payload, {"Stripe-Signature": f"t={timestamp},v1={signature}",
                     "Content-Type": "application/json"}


def _authorization(merchant="Google Ads", amount=35000):
    return {"id": "iauth_hosted_test", "object": "issuing.authorization", "amount": 0,
            "currency": "gbp", "approved": False,
            "card": {"id": "ic_hosted_demo_test", "object": "issuing.card"},
            "pending_request": {"amount": amount, "currency": "gbp"},
            "merchant_data": {"name": merchant, "category": "advertising_services"}}


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/", None),
    ("GET", "/api/state", None),
    ("GET", "/api/inbox", None),
    ("GET", "/api/jobs/job_missing", None),
    ("GET", "/decisions", None),
    ("GET", "/intents", None),
    ("GET", "/docs", None),
    ("POST", "/api/inbox", {}),
    ("POST", "/api/why", {"question": "Why did you pay Google Ads?"}),
    ("POST", "/api/intents/int_missing/approve", {"approver": "sarthak"}),
])
def test_dashboard_and_actions_require_login(client, method, path, body):
    response = client.request(method, path, json=body)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic ")
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("authorization", ["Bearer fake", "Basic !!!", "Basic /w==",
                                               "Basic ZGVtbzp3cm9uZw=="])
def test_malformed_or_wrong_credentials_are_rejected(client, authorization):
    assert client.get("/api/state", headers={"Authorization": authorization}).status_code == 401


def test_health_and_signed_webhook_do_not_require_browser_login(client):
    assert client.get("/health").json()["test_mode"] is True
    payload, headers = _signed_event("issuing_card.updated", {"id": "ic_test", "object": "issuing.card"})
    assert client.post("/stripe/webhook", content=payload, headers=headers).status_code == 200
    assert client.post("/stripe/webhook", json={}).status_code == 400
    response = client.get("/api/state", headers=_login())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/", headers=_login()).status_code == 200


@pytest.mark.parametrize("origin", ["https://evil.example", "https://testserver", "null"])
def test_authenticated_cross_origin_mutations_are_rejected(client, server_module, monkeypatch, origin):
    calls = []
    monkeypatch.setattr(server_module.agent, "inbox", lambda *args, **kwargs: calls.append(1))
    response = client.post("/api/inbox", json={}, headers={**_login(), "Origin": origin})
    assert response.status_code == 403
    assert calls == []


def test_same_origin_action_is_allowed(client, server_module, monkeypatch):
    calls = []
    monkeypatch.setattr(server_module.agent, "inbox", lambda *args, **kwargs: calls.append(1))
    response = client.post("/api/inbox", json={}, headers={**_login(), "Origin": "http://testserver"})
    assert response.status_code == 202
    server_module._JOB_EXECUTOR.submit(lambda: None).result(timeout=3)
    assert calls == [1]


@pytest.mark.parametrize("missing", ["DATABASE_URL", "DEMO_PASSWORD", "STRIPE_CARD_ID"])
def test_hosted_startup_fails_before_storage_when_required_config_missing(server_module, monkeypatch, missing):
    monkeypatch.setenv("DYNO", "web.1")
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused.invalid/demo")
    monkeypatch.setenv("DEMO_PASSWORD", "fake-demo-password")
    monkeypatch.setenv("STRIPE_CARD_ID", "ic_hosted_demo_test")
    monkeypatch.delenv(missing)
    initialized = []
    monkeypatch.setattr(server_module.ledger, "ensure_storage", lambda: initialized.append(True))
    with pytest.raises(RuntimeError, match=missing):
        with TestClient(server_module.app):
            pass
    assert initialized == []


@pytest.mark.parametrize("key", ["sk_live_fake", "rk_live_fake", ""])
def test_startup_rejects_non_test_stripe_keys(server_module, monkeypatch, key):
    monkeypatch.setattr(server_module.stripe, "api_key", key)
    with pytest.raises(RuntimeError, match="test-mode"):
        with TestClient(server_module.app):
            pass


def test_hosted_cognee_requires_remote_service_and_package(server_module, monkeypatch):
    monkeypatch.setenv("DYNO", "web.1")
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused.invalid/demo")
    monkeypatch.setenv("MEMORY_CHECK", "cognee")
    monkeypatch.setattr(server_module.memory, "HAVE_COGNEE", True)
    with pytest.raises(RuntimeError, match="COGNEE_SERVICE_URL"):
        server_module.validate_hosted_config()
    monkeypatch.setenv("COGNEE_SERVICE_URL", "https://unused.invalid")
    monkeypatch.setattr(server_module.memory, "HAVE_COGNEE", False)
    with pytest.raises(RuntimeError, match="Cognee package"):
        server_module.validate_hosted_config()
    monkeypatch.setattr(server_module.memory, "HAVE_COGNEE", True)
    server_module.validate_hosted_config()
    monkeypatch.setenv("MEMORY_CHECK", "file")
    monkeypatch.delenv("COGNEE_SERVICE_URL")
    server_module.validate_hosted_config()


@pytest.mark.parametrize("action", ["inbox", "why", "approve"])
def test_actions_return_202_support_polling_and_reject_overlapping_jobs(client, server_module, monkeypatch, action):
    started, release = threading.Event(), threading.Event()
    calls, captures = [], []
    expected = {"decision": "approve", "failed_clause": None}

    def blocked(*args, **kwargs):
        calls.append((args, kwargs))
        started.set()
        if not release.wait(timeout=5):
            raise RuntimeError("Test did not release its worker")
        print("finished test action")
        return (expected, {}) if action == "approve" else expected

    if action == "inbox":
        monkeypatch.setattr(server_module.agent, "inbox", blocked)
        path, body = "/api/inbox", {"files": ["sample.txt"]}
    elif action == "why":
        monkeypatch.setattr(server_module, "_answer_why", blocked)
        path, body = "/api/why", {"question": "Why did you pay?"}
    else:
        monkeypatch.setattr(server_module.agent, "approve", blocked)
        monkeypatch.setattr(server_module.agent, "execute", lambda iid: captures.append(iid))
        path, body = "/api/intents/int_test/approve", {"approver": " sarthak "}

    try:
        response = client.post(path, json=body, headers=_login())
        assert response.status_code == 202
        assert started.wait(timeout=2)
        job_id = response.json()["job_id"]
        running = client.get(f"/api/jobs/{job_id}", headers=_login())
        assert running.status_code == 200
        assert running.json()["status"] == "running"
        assert client.get("/api/state", headers=_login()).json()["busy"] is True
        conflict = client.post(path, json=body, headers=_login())
        assert conflict.status_code == 409
        assert len(calls) == 1
    finally:
        release.set()
        server_module._JOB_EXECUTOR.submit(lambda: None).result(timeout=3)

    finished = client.get(f"/api/jobs/{job_id}", headers=_login()).json()
    assert finished["status"] == "complete"
    assert finished["result"] == expected
    assert "finished test action" in finished["log"]
    assert client.get("/api/state", headers=_login()).json()["busy"] is False
    assert len(calls) == 1
    if action == "approve":
        assert calls[0][0] == ("int_test", "sarthak")
        assert captures == ["int_test"]
    assert client.get("/api/jobs/job_missing", headers=_login()).status_code == 404


def test_authorization_retry_replays_identical_response_without_double_counting(client, server_module):
    approval = {"approver": "sarthak", "token": "apr_fake", "ts": server_module.ledger.now()}
    intent = server_module.ledger.new_intent(merchant="Google Ads", amount=35000, currency="gbp",
                                             category="advertising_services", status="cleared", approval=approval)
    payload, headers = _signed_event("issuing_authorization.request", _authorization(), event_id="evt_auth_retry")
    first = client.post("/stripe/webhook", content=payload, headers=headers)
    assert first.status_code == 200
    assert first.json()["approved"] is True
    assert first.json()["metadata"]["approver"] == "sarthak"
    assert first.json()["metadata"]["intent_id"] == intent["id"]
    assert first.headers["stripe-version"] == server_module.API_VERSION
    assert server_module.ledger.get_intent(intent["id"])["status"] == "executed"
    second = client.post("/stripe/webhook", content=payload, headers=headers)
    assert second.status_code == 200
    assert second.json() == first.json()
    assert second.headers["stripe-version"] == first.headers["stripe-version"]
    assert len(server_module.ledger.decisions()) == 1
    assert server_module.ledger.month_spent() == 35000


def test_capture_webhook_retry_records_one_capture(client, server_module):
    capture = {"id": "ipi_hosted_test", "object": "issuing.transaction", "amount": -35000,
               "currency": "gbp", "authorization": "iauth_hosted_test", "card": "ic_hosted_demo_test",
               "merchant_data": {"name": "Google Ads", "category": "advertising_services"}}
    payload, headers = _signed_event("issuing_transaction.created", capture, event_id="evt_capture_retry")
    for _ in range(2):
        response = client.post("/stripe/webhook", content=payload, headers=headers)
        assert response.status_code == 200
    decisions = server_module.ledger.decisions()
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "captured"
    assert decisions[0]["amount"] == 35000
    assert decisions[0]["stripe"]["authorization_id"] == "iauth_hosted_test"
    assert server_module.ledger.month_spent() == 0


def test_second_signing_secret_is_accepted(client, server_module):
    payload, headers = _signed_event("issuing_authorization.request", _authorization(amount=10000),
                                    event_id="evt_second_secret", secret="whsec_issuing_test")
    response = client.post("/stripe/webhook", content=payload, headers=headers)
    assert response.status_code == 200
    assert response.json()["approved"] is False
    assert response.json()["metadata"]["failed_clause"] == "C5"
    assert len(server_module.ledger.decisions()) == 1


@pytest.mark.parametrize("live,secret", [(True, "whsec_regular_test"), (False, "whsec_wrong_test")])
def test_live_mode_or_invalid_signature_cannot_mutate_ledger(client, server_module, live, secret):
    payload, headers = _signed_event("issuing_authorization.request", _authorization(amount=10000),
                                    event_id="evt_rejected", live=live, secret=secret)
    response = client.post("/stripe/webhook", content=payload, headers=headers)
    assert response.status_code == 400
    assert server_module.ledger.decisions() == []
    assert server_module.ledger.month_spent() == 0
