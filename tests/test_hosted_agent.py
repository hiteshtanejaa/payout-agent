"""Hosted agent behavior with temporary files and fully fake external modules.

Never reads the workspace .env, inbox or ledger, and never contacts Stripe,
Cognee, an LLM or PostgreSQL. Run with `python -m pytest tests -q`.
"""
import importlib.util
import io
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
REQUEST = "merchant: Figma\ncategory: computer_software_stores\namount: 120\nreason: Team seat\n"


def _load_module(name, source):
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def hosted_agent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "policy.yaml").write_text((ROOT / "policy.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake_agent_tests")
    for name in ("DATABASE_URL", "STRIPE_CARD_ID", "STRIPE_CARDHOLDER_ID", "DYNO"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = Mock(return_value=False)
    fake_memory = types.ModuleType("memory")
    fake_memory.HAVE_COGNEE = False
    fake_memory.remember_decision_sync = Mock()
    fake_recall = types.ModuleType("recall")
    fake_recall.check = Mock(return_value={"status": "clear", "source": "fake", "notes": []})
    fake_extractor = types.ModuleType("extractor")
    fake_extractor.extract = Mock(side_effect=AssertionError("Unexpected LLM extraction"))
    fake_stripe = types.ModuleType("stripe")
    auth = {"id": "iauth_fake_test", "approved": True, "request_history": [], "metadata": {}}
    auth_object = types.SimpleNamespace(to_dict=lambda: dict(auth))
    helpers = types.SimpleNamespace(create=Mock(return_value=auth_object), capture=Mock())
    fake_stripe.issuing = types.SimpleNamespace(Authorization=types.SimpleNamespace(TestHelpers=helpers))
    client_authorizations = types.SimpleNamespace(create=Mock(return_value=auth_object), capture=Mock())
    client = types.SimpleNamespace(test_helpers=types.SimpleNamespace(
        issuing=types.SimpleNamespace(authorizations=client_authorizations)))
    fake_stripe.StripeClient = Mock(return_value=client)

    for name, module in {"dotenv": fake_dotenv, "memory": fake_memory, "recall": fake_recall,
                         "extractor": fake_extractor, "stripe": fake_stripe}.items():
        monkeypatch.setitem(sys.modules, name, module)
    test_ledger = _load_module("_hosted_agent_test_ledger", ROOT / "ledger.py")
    monkeypatch.setitem(sys.modules, "ledger", test_ledger)
    return _load_module("_hosted_agent_under_test", ROOT / "agent.py")


def _request_file():
    path = Path("inbox/request.txt")
    path.parent.mkdir(exist_ok=True)
    path.write_text(REQUEST, encoding="utf-8")
    return path


def test_hosted_card_identity_does_not_need_local_state(hosted_agent, monkeypatch):
    monkeypatch.setenv("STRIPE_CARD_ID", "ic_fake_hosted")
    monkeypatch.setenv("STRIPE_CARDHOLDER_ID", "ich_fake_hosted")
    assert not Path(hosted_agent.STATE_PATH).exists()
    assert hosted_agent.state() == {"card_id": "ic_fake_hosted", "cardholder_id": "ich_fake_hosted"}


@pytest.mark.parametrize("key", ["sk_live_fake", "rk_live_fake", ""])
def test_non_test_keys_stop_before_any_stripe_call(hosted_agent, key):
    hosted_agent.stripe.api_key = key
    with pytest.raises(RuntimeError, match="test-mode"):
        hosted_agent.create_test_authorization(amount=12000, currency="gbp", card="ic_fake",
                                               merchant_name="Figma", category="computer_software_stores")
    hosted_agent.stripe.issuing.Authorization.TestHelpers.create.assert_not_called()
    hosted_agent.stripe.issuing.Authorization.TestHelpers.capture.assert_not_called()
    hosted_agent.stripe.StripeClient.assert_not_called()


@pytest.mark.parametrize("sdk", ["legacy", "client"])
def test_retries_use_stable_intent_and_capture_idempotency(hosted_agent, monkeypatch, sdk):
    monkeypatch.setenv("STRIPE_CARD_ID", "ic_fake_hosted")
    if sdk == "legacy":
        authorizations = hosted_agent.stripe.issuing.Authorization.TestHelpers
    else:
        authorizations = hosted_agent.stripe.StripeClient.return_value.test_helpers.issuing.authorizations
        monkeypatch.delattr(hosted_agent.stripe.issuing.Authorization, "TestHelpers")
    intent = hosted_agent.ledger.new_intent(merchant="Figma", amount=12000, currency="gbp",
                                             category="computer_software_stores", status="cleared")
    for _ in range(2):
        assert hosted_agent.execute(intent["id"])["id"] == "iauth_fake_test"
    assert authorizations.create.call_count == authorizations.capture.call_count == 2
    assert authorizations.create.call_args_list[0] == authorizations.create.call_args_list[1]
    assert authorizations.capture.call_args_list[0] == authorizations.capture.call_args_list[1]
    if sdk == "legacy":
        create_params = authorizations.create.call_args.kwargs
        capture_options = authorizations.capture.call_args.kwargs
        create_options = create_params
        hosted_agent.stripe.StripeClient.assert_not_called()
    else:
        create_params = authorizations.create.call_args.args[0]
        create_options = authorizations.create.call_args.kwargs["options"]
        capture_options = authorizations.capture.call_args.kwargs["options"]
        assert hosted_agent.stripe.StripeClient.call_count == 4
    assert create_params["card"] == "ic_fake_hosted"
    assert create_params["amount"] == 12000
    assert create_options["idempotency_key"] == "treasurer-intent-" + intent["id"]
    assert capture_options["idempotency_key"] == "treasurer-capture-iauth_fake_test"
    assert authorizations.capture.call_args.args == ("iauth_fake_test",)
    assert hosted_agent.ledger.get_intent(intent["id"])["authorization_id"] == "iauth_fake_test"


def test_restored_inbox_file_is_excluded_by_persisted_receipt(hosted_agent, monkeypatch):
    path = _request_file()
    handler = Mock()
    monkeypatch.setattr(hosted_agent, "handle_intent", handler)
    hosted_agent.inbox(interactive=False)
    assert not path.exists()
    assert Path("inbox/processed/request.txt").exists()
    # A Heroku restart restores the original file from its deployed slug.
    path.write_text(REQUEST, encoding="utf-8")
    assert hosted_agent.inbox_files() == []
    hosted_agent.inbox(interactive=False)
    assert len(hosted_agent.ledger.list_intents()) == len(hosted_agent.ledger.decisions()) == 1
    assert handler.call_count == 1
    # A changed request with the same filename remains eligible for processing.
    path.write_text(REQUEST.replace("amount: 120", "amount: 130"), encoding="utf-8")
    assert hosted_agent.inbox_files() == [str(path)]


def test_receipt_is_saved_before_interrupted_execution_and_survives_restart(hosted_agent, monkeypatch):
    path = _request_file()
    fingerprint = hosted_agent.inbox_fingerprint(path)

    def interrupted(*args, **kwargs):
        assert hosted_agent.ledger.inbox_processed(path.name, fingerprint)
        raise InterruptedError("Fake interruption during payment")

    monkeypatch.setattr(hosted_agent, "handle_intent", interrupted)
    with pytest.raises(InterruptedError):
        hosted_agent.inbox(interactive=False)
    assert path.exists()
    assert len(hosted_agent.ledger.list_intents()) == 1
    # Reload storage as a restarted process would, using the same persisted files.
    monkeypatch.setattr(hosted_agent, "ledger", _load_module("_restarted_test_ledger", ROOT / "ledger.py"))
    handler = Mock()
    monkeypatch.setattr(hosted_agent, "handle_intent", handler)
    hosted_agent.inbox(interactive=False)
    handler.assert_not_called()
    assert len(hosted_agent.ledger.list_intents()) == len(hosted_agent.ledger.decisions()) == 1


def test_failed_extraction_is_recorded_once_and_never_proposes_payment(hosted_agent, monkeypatch):
    path = _request_file()
    path.write_text("An incomplete free-text purchase email.", encoding="utf-8")
    original = path.read_text(encoding="utf-8")
    hosted_agent.extractor.extract.side_effect = ValueError("Fake incomplete request")
    handler = Mock()
    monkeypatch.setattr(hosted_agent, "handle_intent", handler)
    hosted_agent.inbox(interactive=False)
    path.write_text(original, encoding="utf-8")
    hosted_agent.inbox(interactive=False)
    assert hosted_agent.ledger.list_intents() == []
    records = hosted_agent.ledger.decisions()
    assert len(records) == 1
    assert records[0]["stage"] == "extraction"
    assert records[0]["decision"] == "decline"
    hosted_agent.extractor.extract.assert_called_once()
    hosted_agent.memory.remember_decision_sync.assert_not_called()
    handler.assert_not_called()
