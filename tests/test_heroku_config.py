"""Configuration helper safety with temporary files and mocked cloud APIs.

Never reads the workspace .env or contacts Heroku or Stripe.
"""
import importlib.util
import io
import json
from pathlib import Path
import types
from urllib.error import HTTPError

from dotenv import dotenv_values
import pytest
import stripe


BASE_CONFIG = {
    "STRIPE_SECRET_KEY": "sk_test_fake_deployment_key",
    "STRIPE_CARD_ID": "ic_fake_card",
    "LLM_MODEL": "openai/fake-test-model",
    "LLM_API_KEY": "fake-llm-key-private",
    "COGNEE_SERVICE_URL": "https://fake-memory.invalid",
    "COGNEE_API_KEY": "fake-cognee-key-private",
}


def _write_env(path, values):
    path.write_text("".join(f"{key}={json.dumps(value)}\n" for key, value in values.items()), encoding="utf-8")


@pytest.fixture
def helper(monkeypatch, tmp_path):
    source = Path(__file__).resolve().parents[1] / "scripts" / "configure_heroku.py"
    spec = importlib.util.spec_from_file_location("_test_configure_heroku", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    # source_config consults the module's environ, never the process's real keys.
    monkeypatch.setattr(module, "os", types.SimpleNamespace(environ={}, getenv=lambda key: None))
    _write_env(tmp_path / ".env", BASE_CONFIG)
    monkeypatch.setattr(module, "auth_token", lambda: "fake-heroku-token-private")

    def reject_network(*args, **kwargs):
        raise AssertionError("An unexpected external request was attempted")

    monkeypatch.setattr(module, "urlopen", reject_network)
    monkeypatch.setattr(stripe.WebhookEndpoint, "list", reject_network)
    monkeypatch.setattr(stripe.WebhookEndpoint, "create", reject_network)
    monkeypatch.setattr(stripe, "api_key", None)
    return module


class FakeCloud:
    def __init__(self, helper, monkeypatch, *, existing=None, endpoints=()):
        self.helper = helper
        self.existing = {"DATABASE_URL": "postgresql://demo:fake-db-password@unused.invalid/demo",
                         **(existing or {})}
        self.calls = []
        self.created = []
        self.listed = []
        self.endpoints = list(endpoints)
        monkeypatch.setattr(helper, "heroku_api", self.heroku)
        monkeypatch.setattr(stripe.WebhookEndpoint, "list", self.list_webhooks)
        monkeypatch.setattr(stripe.WebhookEndpoint, "create", self.create_webhook)

    def heroku(self, token, path, method="GET", payload=None):
        assert token == "fake-heroku-token-private"
        self.calls.append((method, path, dict(payload) if payload is not None else None))
        if path.endswith("/config-vars"):
            if method == "PATCH":
                # The local recovery copy must exist before cloud config changes.
                saved = dotenv_values(self.helper.ROOT / ".env.heroku")
                assert saved["DEMO_PASSWORD"] == payload["DEMO_PASSWORD"]
                self.existing.update(payload)
            return dict(self.existing)
        assert method == "GET"
        return {"web_url": "http://treasurer-test.herokuapp.com/"}

    def list_webhooks(self, **kwargs):
        self.listed.append(kwargs)
        return types.SimpleNamespace(auto_paging_iter=lambda: iter(self.endpoints))

    def create_webhook(self, **kwargs):
        self.created.append(kwargs)
        return types.SimpleNamespace(secret="whsec_new_hosted_private")

    @property
    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]


def test_source_uses_allowlist_and_excludes_local_cli_webhook_secret(helper):
    extras = {"HEROKU_API_KEY": "unrelated-heroku-token", "AWS_SECRET_ACCESS_KEY": "unrelated-aws-secret",
              "OPENAI_API_KEY": "unrelated-openai-secret", "DATABASE_URL": "postgresql://wrong-local-db",
              "STRIPE_WEBHOOK_SECRET": "whsec_local_cli", "STRIPE_ISSUING_WEBHOOK_SECRET": "whsec_local_issuing"}
    _write_env(helper.ROOT / ".env", {**BASE_CONFIG, **extras})
    helper.os.environ.update(extras)
    config = helper.source_config()
    assert config["STRIPE_SECRET_KEY"] == BASE_CONFIG["STRIPE_SECRET_KEY"]
    assert set(config) <= set(helper.ALLOWLIST) | {"PYTHONUNBUFFERED"}
    assert not set(extras) & set(config)
    assert config["PYTHONUNBUFFERED"] == "1"
    assert config["MEMORY_CHECK"] == "cognee"


def test_hosted_overrides_are_read_and_environment_keeps_normal_precedence(helper):
    hosted = {"COGNEE_DATASET": "hosted-dataset", "LLM_MODEL": "hosted-model", "DEMO_USERNAME": "judge",
              "STRIPE_WEBHOOK_SECRET": "whsec_hosted_private",
              "STRIPE_ISSUING_WEBHOOK_SECRET": "whsec_hosted_issuing_private"}
    _write_env(helper.ROOT / ".env.heroku", hosted)
    helper.os.environ.update(LLM_MODEL="environment-model", STRIPE_WEBHOOK_SECRET="whsec_local_cli_environment")
    config = helper.source_config()
    assert config["LLM_MODEL"] == "environment-model"
    for key, value in hosted.items():
        if key != "LLM_MODEL":
            assert config[key] == value


def test_card_identity_falls_back_to_local_state_without_copying_other_fields(helper):
    _write_env(helper.ROOT / ".env", {key: value for key, value in BASE_CONFIG.items() if key != "STRIPE_CARD_ID"})
    state_dir = helper.ROOT / "ledger"
    state_dir.mkdir()
    (state_dir / "stripe_state.json").write_text(json.dumps(
        {"card_id": "ic_fallback", "cardholder_id": "ich_fallback", "secret_key": "do-not-copy"}), encoding="utf-8")
    config = helper.source_config()
    assert config["STRIPE_CARD_ID"] == "ic_fallback"
    assert config["STRIPE_CARDHOLDER_ID"] == "ich_fallback"
    assert "secret_key" not in config
    helper.os.environ["STRIPE_CARD_ID"] = "ic_explicit"
    assert helper.source_config()["STRIPE_CARD_ID"] == "ic_explicit"


@pytest.mark.parametrize("change", [{"STRIPE_SECRET_KEY": "sk_live_fake"},
                                     {"STRIPE_SECRET_KEY": "rk_live_fake"},
                                     {"COGNEE_SERVICE_URL": ""}, {"COGNEE_API_KEY": ""}])
def test_invalid_local_config_stops_before_cloud_reads_or_writes(helper, monkeypatch, change):
    _write_env(helper.ROOT / ".env", {**BASE_CONFIG, **change})
    cloud = FakeCloud(helper, monkeypatch)
    token_calls = []
    monkeypatch.setattr(helper, "auth_token", lambda: token_calls.append(1))
    with pytest.raises(ValueError):
        helper.configure("treasurer-test", register_webhook=True)
    assert token_calls == []
    assert cloud.calls == cloud.created == cloud.listed == []
    assert not (helper.ROOT / ".env.heroku").exists()


def test_attached_database_is_required_before_webhook_or_config_writes(helper, monkeypatch):
    cloud = FakeCloud(helper, monkeypatch)
    cloud.existing.pop("DATABASE_URL")
    with pytest.raises(RuntimeError, match="Attach PostgreSQL"):
        helper.configure("treasurer-test", register_webhook=True)
    assert cloud.writes == cloud.created == cloud.listed == []
    assert not (helper.ROOT / ".env.heroku").exists()


def test_config_preserves_attached_database_and_existing_password_without_printing_secrets(helper, monkeypatch, capsys):
    _write_env(helper.ROOT / ".env", {**BASE_CONFIG, "DATABASE_URL": "postgresql://wrong-local-db",
                                       "STRIPE_WEBHOOK_SECRET": "whsec_local_cli"})
    helper.os.environ["DATABASE_URL"] = "postgresql://wrong-environment-db"
    cloud = FakeCloud(helper, monkeypatch, existing={"STRIPE_WEBHOOK_SECRET": "whsec_existing_private",
                                                    "DEMO_PASSWORD": "existing-password-private"})

    def no_new_password(*args):
        raise AssertionError("An existing demo password should be reused")

    monkeypatch.setattr(helper.secrets, "token_urlsafe", no_new_password)
    original_db = cloud.existing["DATABASE_URL"]
    helper.configure("treasurer-test")
    assert len(cloud.writes) == 1
    payload = cloud.writes[0][2]
    assert "DATABASE_URL" not in payload
    assert "STRIPE_WEBHOOK_SECRET" not in payload
    assert cloud.existing["DATABASE_URL"] == original_db
    assert payload["DEMO_PASSWORD"] == "existing-password-private"
    assert dotenv_values(helper.ROOT / ".env.heroku")["DEMO_PASSWORD"] == "existing-password-private"
    output = capsys.readouterr().out
    for secret in (BASE_CONFIG["STRIPE_SECRET_KEY"], BASE_CONFIG["LLM_API_KEY"], BASE_CONFIG["COGNEE_API_KEY"],
                   "existing-password-private", "whsec_existing_private", "fake-heroku-token-private", original_db):
        assert secret not in output
    assert "https://treasurer-test.herokuapp.com/stripe/webhook" in output


def test_existing_webhook_without_signing_secret_never_creates_duplicate(helper, monkeypatch):
    endpoint = types.SimpleNamespace(url="https://treasurer-test.herokuapp.com/stripe/webhook", status="enabled",
                                     enabled_events=list(helper.WEBHOOK_EVENTS))
    cloud = FakeCloud(helper, monkeypatch, endpoints=[endpoint])
    with pytest.raises(RuntimeError, match="no duplicate endpoint"):
        helper.configure("treasurer-test", register_webhook=True)
    assert len(cloud.listed) == 1
    assert cloud.created == cloud.writes == []
    assert not (helper.ROOT / ".env.heroku").exists()


@pytest.mark.parametrize("missing_event", ["issuing_authorization.request", "issuing_authorization.created",
                                           "issuing_transaction.created"])
def test_existing_webhook_missing_required_event_fails_before_any_writes(helper, monkeypatch, missing_event):
    endpoint = types.SimpleNamespace(url="https://treasurer-test.herokuapp.com/stripe/webhook", status="enabled",
                                     enabled_events=[event for event in helper.WEBHOOK_EVENTS if event != missing_event])
    cloud = FakeCloud(helper, monkeypatch, endpoints=[endpoint],
                      existing={"STRIPE_WEBHOOK_SECRET": "whsec_existing_private"})
    with pytest.raises(RuntimeError, match="without all required events"):
        helper.configure("treasurer-test", register_webhook=True)
    assert len(cloud.listed) == 1
    assert cloud.created == cloud.writes == []
    assert not (helper.ROOT / ".env.heroku").exists()


def test_existing_webhook_reuses_hosted_secret_and_preserves_private_overrides(helper, monkeypatch, capsys):
    hosted = {"STRIPE_WEBHOOK_SECRET": "whsec_explicit_hosted_private",
              "STRIPE_ISSUING_WEBHOOK_SECRET": "whsec_explicit_issuing_private",
              "DEMO_PASSWORD": "explicit-password-private", "COGNEE_DATASET": "keep-my-dataset"}
    _write_env(helper.ROOT / ".env.heroku", hosted)
    endpoint = types.SimpleNamespace(url="https://treasurer-test.herokuapp.com/stripe/webhook", status="enabled",
                                     enabled_events=list(helper.WEBHOOK_EVENTS))
    cloud = FakeCloud(helper, monkeypatch, endpoints=[endpoint])
    helper.configure("treasurer-test", register_webhook=True)
    assert cloud.created == []
    assert len(cloud.writes) == 1
    payload = cloud.writes[0][2]
    saved = dotenv_values(helper.ROOT / ".env.heroku")
    for key, value in hosted.items():
        assert payload[key] == saved[key] == value
    output = capsys.readouterr().out
    for key in ("DEMO_PASSWORD", "STRIPE_WEBHOOK_SECRET", "STRIPE_ISSUING_WEBHOOK_SECRET"):
        assert hosted[key] not in output


def test_new_webhook_and_generated_password_are_saved_before_patch_and_not_printed(helper, monkeypatch, capsys):
    _write_env(helper.ROOT / ".env.heroku", {"COGNEE_DATASET": "preserve-this-dataset"})
    cloud = FakeCloud(helper, monkeypatch)
    generator_calls = []

    def new_password(size):
        generator_calls.append(size)
        return "generated-password-private"

    monkeypatch.setattr(helper.secrets, "token_urlsafe", new_password)
    helper.configure("treasurer-test", register_webhook=True)
    assert generator_calls == [24]
    assert len(cloud.created) == len(cloud.writes) == 1
    assert cloud.created[0]["url"] == "https://treasurer-test.herokuapp.com/stripe/webhook"
    assert "issuing_authorization.request" in cloud.created[0]["enabled_events"]
    saved = dotenv_values(helper.ROOT / ".env.heroku")
    assert saved["DEMO_PASSWORD"] == cloud.writes[0][2]["DEMO_PASSWORD"] == "generated-password-private"
    assert saved["STRIPE_WEBHOOK_SECRET"] == cloud.writes[0][2]["STRIPE_WEBHOOK_SECRET"] == "whsec_new_hosted_private"
    assert saved["COGNEE_DATASET"] == "preserve-this-dataset"
    output = capsys.readouterr().out
    assert "generated-password-private" not in output
    assert "whsec_new_hosted_private" not in output


@pytest.mark.parametrize("status", [401, 500])
def test_heroku_errors_never_expose_response_body_or_token(helper, monkeypatch, status):
    def denied(*args, **kwargs):
        raise HTTPError("https://api.heroku.com/apps/fake", status, "private-error-body", {},
                        io.BytesIO(b"secret-api-key-in-response"))

    monkeypatch.setattr(helper, "urlopen", denied)
    with pytest.raises(RuntimeError) as error:
        helper.heroku_api("private-token", "/apps/fake")
    assert "private-token" not in str(error.value)
    assert "private-error-body" not in str(error.value)
    assert "secret-api-key-in-response" not in str(error.value)


def test_cli_masks_unexpected_sdk_exception_details(helper, monkeypatch, capsys):
    def failure(*args, **kwargs):
        raise Exception("Stripe error containing sk_test_do_not_print")

    monkeypatch.setattr(helper, "configure", failure)
    monkeypatch.setattr("sys.argv", ["configure_heroku.py", "--app", "treasurer-test", "--register-webhook"])
    with pytest.raises(SystemExit) as error:
        helper.main()
    assert "sk_test_do_not_print" not in str(error.value)
    assert "Exception" in str(error.value)
    assert "sk_test_do_not_print" not in capsys.readouterr().out
