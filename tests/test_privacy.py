"""Synthetic credential checks across actual Kindex persistence and egress."""

import hashlib
import io
import json
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from kindex.config import Config, degraded_ledger_path, record_degraded
from kindex.privacy import POLICY_VERSION, REDACTED, protect_logger, redact, redact_text
from kindex.store import CandidateStateError, Store

CANARY = "sk-ant-api03-INVALID-SYNTHETIC-CREDENTIAL-" + "x" * 32
DIGEST = hashlib.sha256(b"public-evidence").hexdigest()


@pytest.fixture
def graph(tmp_path):
    cfg = Config(data_dir=str(tmp_path / "data"))
    store = Store(cfg)
    yield cfg, store
    store.close()


@pytest.mark.parametrize("text", [
    CANARY,
    "ghp_" + "x" * 36,
    "github_pat_" + "x" * 45,
    "Authorization: Bearer deliberately-short",
    "Authorization: Basic YTpzZWNyZXQ=",
    "Cookie: session=short; other=another",
    "postgres://person:deliberately-short@example.invalid/db",
    "https://example.invalid/?access_token=short&limit=10",
    "https://example.invalid/?token=short&limit=10",
    "ANTHROPIC_API_KEY=short",
    'password="short"',
    '{"client_secret":"short"}',
    "-----BEGIN PRIVATE KEY-----\nsynthetic-key-data\n-----END PRIVATE KEY-----",
])
def test_supported_credentials_are_redacted_idempotently(text):
    cleaned = redact_text(text)
    assert cleaned != text
    assert REDACTED in cleaned
    assert redact_text(cleaned) == cleaned


def test_structured_sensitive_fields_preserve_ids_types_and_evidence():
    original = {
        "id": "4c6b646d-57a5-4bf1-afb4-205f44736234", "source_digest": DIGEST,
        "review_token": DIGEST, "count": 4, "enabled": True,
        "api_key_env": "ANTHROPIC_API_KEY", "contact": "engineer@example.invalid",
        "address": "192.0.2.1", "long_symbol": "ordinary_symbol_" * 5,
        "data": [{"password": "short", "private_key": "short\nmultiline"}],
    }
    cleaned = redact(original)
    assert cleaned["data"] == [{"password": REDACTED, "private_key": REDACTED}]
    assert original["data"][0]["password"] == "short"
    assert {k: v for k, v in cleaned.items() if k != "data"} == {
        k: v for k, v in original.items() if k != "data"
    }
    assert redact(cleaned) == cleaned


def test_operational_placeholders_are_not_replaced():
    assert redact_text('API_KEY="$API_KEY"') == 'API_KEY="$API_KEY"'
    assert redact({"api_key": "${KEY_FROM_ENV}"}) == {"api_key": "${KEY_FROM_ENV}"}
    assert redact_text(DIGEST) == DIGEST
    assert redact_text("Basic facts about bearer instruments") == "Basic facts about bearer instruments"


@pytest.mark.parametrize("value", ["<actual-secret>", "[redacted actual-secret]", "${BROKEN", "$BROKEN}", 123456])
def test_sensitive_fields_cannot_disguise_secrets_as_placeholders(value):
    assert redact({"password": value}) == {"password": REDACTED}


def test_escaped_assignment_quotes_and_sensitive_tuple():
    assert redact_text(r'password="prefix\"secret-suffix"') == 'password="[REDACTED]"'
    assert redact_text(r"password='prefix\'secret-suffix'") == "password='[REDACTED]'"
    assert redact({"password": ("short", "other")}) == {"password": (REDACTED, REDACTED)}


def test_credentials_in_object_keys_are_redacted_or_collision_rejected():
    assert redact({CANARY: "metadata"}) == {REDACTED: "metadata"}
    assert redact({"credentials": {CANARY: "value"}}) == {"credentials": {REDACTED: REDACTED}}
    with pytest.raises(ValueError, match="merge object fields"):
        redact({CANARY: "one", "ghp_" + "y" * 36: "two"})


def test_redaction_marker_cannot_hide_an_assignment_suffix():
    assert redact_text("password=[REDACTED]secret-suffix") == "password=[REDACTED]"
    assert redact_text("API key is [REDACTED]secret-suffix") == "API key is [REDACTED]"


def test_long_scheme_like_symbol_does_not_trigger_quadratic_url_matching():
    # A subprocess timeout bounds the regression itself; the former regex
    # tried parsing a scheme at every letter and took ~26 s for this input.
    subprocess.run([sys.executable, "-c",
        "from kindex.privacy import redact_text; value='ordinary-'+'A'*200000; "
        "assert redact_text(value)==value"], check=True, timeout=5, capture_output=True)


def test_legacy_context_is_redacted_before_snippet_truncation(graph, monkeypatch):
    from kindex.hooks import prime_context

    cfg, store = graph
    prefix = "Public words " * 8
    node = {"id": "synthetic", "title": "Visible concept", "content": prefix + CANARY}
    monkeypatch.setattr("kindex.retrieve.hybrid_search", lambda *a, **kw: [node])
    output = prime_context(store, topic="visible", config=cfg)
    assert "sk-ant-api" not in output
    assert node["content"] == prefix + CANARY  # read projection, not history rewrite


def test_redacted_referent_projection_keeps_digest_without_claiming_a_changed_location():
    referent = {"url": "https://example.invalid/?token=short", "digest_scope": "url", "content_digest": DIGEST}
    assert redact(referent) == {"url_redacted": True, "digest_scope": "url", "content_digest": DIGEST}


def test_inline_authorization_header_is_redacted():
    assert "short" not in redact_text("curl -H 'Authorization: Bearer short' https://example.invalid")


def test_print_guard_retains_jsonl_framing_and_joins_arguments(capsys):
    from kindex.privacy import redacting_print

    redacting_print(json.dumps({"token": "short"}))
    redacting_print(json.dumps({"token": "another"}))
    lines = capsys.readouterr().out.splitlines()
    assert [json.loads(line) for line in lines] == [{"token": REDACTED}, {"token": REDACTED}]
    redacting_print("Authorization:", "Bearer", "short")
    assert "short" not in capsys.readouterr().out


def test_reminder_command_refuses_literal_secrets_instead_of_breaking_execution(graph):
    _, store = graph
    with pytest.raises(ValueError, match="environment variables"):
        store.add_reminder("test", "2026-09-07", extra={"action_command": "curl -H 'Authorization: Bearer " + CANARY + "' https://example.invalid"})
    assert not store.list_reminders()


def test_node_edit_extra_logs_and_wal_never_receive_supported_canary(graph):
    cfg, store = graph
    node_id = store.add_node(CANARY, content=CANARY, extra={"password": "short"})
    store.edit_node(node_id, content=f"Updated {CANARY}", actor=CANARY)
    store.atomic_extra_update(node_id, lambda extra: extra.update({"output": CANARY}))
    store.add_suggestion("A", "B", reason=CANARY)
    store.add_edge(node_id, node_id, provenance=CANARY)
    successor = store.supersede_node(node_id, "Successor " + CANARY, reason=CANARY)
    assert CANARY not in json.dumps(store.get_node(successor["id"]))
    assert CANARY not in json.dumps(store.recent_activity())
    assert store.get_node(node_id)["extra"]["password"] == REDACTED
    assert not store.fts_search(CANARY)
    for path in cfg.data_path.glob("kindex.db*"):
        assert CANARY.encode() not in path.read_bytes(), path.name


def test_credential_bearing_node_id_is_rejected_not_silently_rekeyed(graph):
    _, store = graph
    with pytest.raises(ValueError, match="IDs must not contain"):
        store.add_node("Synthetic", node_id=CANARY)
    assert not store.all_nodes()


def test_store_json_default_and_json_error_fields_do_not_bypass_sanitization(graph):
    from kindex.privacy import safe_error

    _, store = graph
    node_id = store.add_node("Path evidence", extra={"path": Path("/tmp") / CANARY})
    assert CANARY not in store.get_node(node_id)["extra"]["path"]
    assert "short" not in safe_error(ValueError(json.dumps({"token": "short"})))


def test_capture_is_redacted_before_digest_review_and_atomic_promotion(graph):
    _, store = graph
    candidate_id = store.add_capture_candidate(
        title="Candidate " + CANARY, content=CANARY, source_digest=DIGEST,
    )
    candidate = store.get_capture_candidate(candidate_id)
    payload = {k: candidate[k] for k in ("title", "content", "node_type", "domains", "connections")}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert candidate["payload_digest"] == hashlib.sha256(encoded.encode()).hexdigest()
    assert CANARY not in encoded
    duplicate = store.add_capture_candidate(
        title="Candidate " + CANARY, content=CANARY, source_digest=DIGEST,
    )
    assert duplicate == candidate_id
    accepted = store.accept_capture_candidate(
        candidate_id, review_token=store.candidate_review_token(candidate_id),
        reviewed_by="reviewer", prov_method="synthetic-test",
    )
    assert accepted["status"] == "accepted"
    node = store.get_node(accepted["created_node_id"])
    assert node["content"] == payload["content"]
    assert node["extra"]["payload_digest"] == candidate["payload_digest"]
    assert node["extra"]["redaction_policy"] == POLICY_VERSION


def test_legacy_candidate_is_not_rewritten_under_a_review_token(graph):
    _, store = graph
    candidate_id = store.add_capture_candidate(title="Candidate", content="safe content", source_digest=DIGEST)
    # Simulate an existing pre-policy row. No live database is touched.
    store.conn.execute("UPDATE capture_candidates SET content=? WHERE id=?", (CANARY, candidate_id))
    store.conn.commit()
    token = store.candidate_review_token(candidate_id)
    with pytest.raises(CandidateStateError, match="restage"):
        store.accept_capture_candidate(candidate_id, review_token=token,
                                       reviewed_by="reviewer", prov_method="test")
    assert store.get_capture_candidate(candidate_id)["content"] == CANARY
    assert store.get_capture_candidate(candidate_id)["status"] == "pending"
    assert not store.all_nodes()


def test_inbox_queues_degraded_ledger_and_reminders(graph):
    from kindex.attention import ATTENTION_QUEUE_META, enqueue_attention_review
    from kindex.hooks import write_inbox_item
    from kindex.reinforce import REINFORCE_QUEUE_META, enqueue_reinforce
    from kindex.actions import _update_action_status

    cfg, store = graph
    path = write_inbox_item(cfg, CANARY, source=CANARY, topic_hint=CANARY)
    assert CANARY not in path.read_text()
    record_degraded("test", ValueError(CANARY), config=cfg)
    assert CANARY not in degraded_ledger_path(cfg).read_text()
    assert REDACTED in degraded_ledger_path(cfg).read_text()
    enqueue_attention_review(store, cfg, {"job_id": "test", "conversation_id": "test", "snippet": CANARY})
    enqueue_reinforce(store, "test", trace=CANARY)
    assert CANARY not in store.get_meta(ATTENTION_QUEUE_META)
    assert CANARY not in store.get_meta(REINFORCE_QUEUE_META)
    rid = store.add_reminder("test", "2026-09-07", body=CANARY)
    _update_action_status(store, rid, store.get_reminder(rid), "completed", CANARY)
    assert CANARY not in json.dumps(store.get_reminder(rid))


def test_new_archive_preserves_sanitized_bytes_and_old_sources_are_not_rewritten(graph):
    from kindex.archive import archive_nodes, search_archives

    cfg, store = graph
    node_id = store.add_node("Archived", CANARY)
    assert archive_nodes(cfg, store, [node_id]) == 1
    assert CANARY not in json.dumps(search_archives(cfg, "Archived"))
    legacy = store.add_node("Legacy", "safe")
    store.conn.execute("UPDATE nodes SET content=? WHERE id=?", (CANARY, legacy))
    store.conn.commit()
    with pytest.raises(ValueError, match="credential remediation"):
        archive_nodes(cfg, store, [legacy])
    assert store.get_node(legacy)["content"] == CANARY
    assert not search_archives(cfg, "Legacy")


def test_refuse_secret_bearing_referent_without_changing_its_identity(graph):
    _, store = graph
    referent = {"url": "https://example.invalid/?access_token=short", "content_digest": DIGEST}
    with pytest.raises(ValueError, match="remove them"):
        store.add_node("Bound", referent=referent)
    assert not store.all_nodes()


def test_public_export_and_index_guard_legacy_titles_preserve_evidence(graph, monkeypatch, capsys, tmp_path):
    from kindex.cli import cmd_export
    from kindex.ingest import write_kin_index

    _, store = graph
    node_id = store.add_node("legacy export", DIGEST, audience="public")
    store.conn.execute("UPDATE nodes SET title=? WHERE id=?", (CANARY, node_id))
    store.conn.commit()
    monkeypatch.setattr("kindex.cli._store", lambda args: store)
    monkeypatch.setattr(store, "close", lambda: None)
    cmd_export(SimpleNamespace(export_kind="graph", audience="public", format="json"))
    output = capsys.readouterr().out
    assert CANARY not in output
    assert DIGEST in output
    index = write_kin_index(store, tmp_path)
    assert CANARY not in index.read_text()


def test_real_repo_index_requires_shareable_audience_or_explicit_private_opt_in(graph, tmp_path):
    from kindex.ingest import write_kin_index

    _, store = graph
    repo = tmp_path / "project"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    public_id = "code-mod-project-" + "a" * 12
    private_id = "code-mod-project-" + "b" * 12
    store.add_node("Public", node_id=public_id, audience="public")
    store.add_node("Private", node_id=private_id, audience="private")
    path = write_kin_index(store, repo)
    assert [node["id"] for node in json.loads(path.read_text())["nodes"]] == [public_id]
    (repo / ".kin" / "config").write_text("audience: private\n")
    path = write_kin_index(store, repo)
    assert {node["id"] for node in json.loads(path.read_text())["nodes"]} == {public_id, private_id}


def test_transcript_and_mocked_extraction_provider_are_sanitized_before_truncation(graph, monkeypatch, tmp_path):
    from kindex.extract import llm_extract
    from kindex.ingest import _extract_session_text

    cfg, _ = graph
    text = "Prefix " + CANARY + " " + "x" * 4100
    path = tmp_path / "transcript.jsonl"
    path.write_text(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": text}}) + "\n")
    assert CANARY not in _extract_session_text(path)
    response = SimpleNamespace(content=[SimpleNamespace(text=json.dumps({"concepts": [{"title": "test", "content": CANARY}]}))],
                               usage=SimpleNamespace(input_tokens=1, output_tokens=1))
    create = Mock(return_value=response)
    monkeypatch.setattr("kindex.extract._get_client", lambda cfg: SimpleNamespace(messages=SimpleNamespace(create=create)))
    ledger = SimpleNamespace(can_spend=lambda: True, record=lambda *args, **kwargs: None)
    result = llm_extract(text, [CANARY], cfg, ledger)
    assert CANARY not in json.dumps(create.call_args.kwargs)
    assert CANARY not in json.dumps(result)


def test_get_client_guards_sdk_prompt_without_redacting_operational_api_key(graph, monkeypatch):
    from kindex.llm import get_client

    cfg, _ = graph
    cfg.llm.enabled = True
    cfg.llm.provider = "openai"
    cfg.llm.api_key_env = "SYNTHETIC_TEST_API_KEY"
    monkeypatch.setenv("SYNTHETIC_TEST_API_KEY", CANARY)
    requests = []
    def urlopen(request, **kwargs):
        requests.append(request)
        return io.BytesIO(b'{"output_text":"ok","usage":{}}')
    monkeypatch.setattr("kindex.llm.urllib.request.urlopen", urlopen)
    get_client(cfg).messages.create(model="test-model", max_tokens=1,
                                    messages=[{"role": "user", "content": CANARY}])
    assert CANARY.encode() not in requests[0].data
    assert requests[0].get_header("Authorization") == "Bearer " + CANARY


def test_embedding_provider_body_and_error_output_are_sanitized(monkeypatch, capsys):
    from kindex.vectors import _embed_openai

    monkeypatch.setenv("SYNTHETIC_EMBED_KEY", "synthetic-auth-only")
    requests = []
    def urlopen(request, **kwargs):
        requests.append(request)
        raise RuntimeError(CANARY)
    monkeypatch.setattr("kindex.vectors.urllib.request.urlopen", urlopen)
    assert _embed_openai(CANARY, "test-model", 2, "SYNTHETIC_EMBED_KEY") is None
    assert CANARY.encode() not in requests[0].data
    assert CANARY not in capsys.readouterr().err


def test_logger_sanitizes_interpolation_and_traceback():
    logger = protect_logger(logging.getLogger("kindex.privacy-test"))
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger.addHandler(handler)
    try:
        try:
            raise RuntimeError(CANARY)
        except RuntimeError:
            logger.exception("Provider failed: %s", CANARY)
        assert CANARY not in stream.getvalue()
        assert REDACTED in stream.getvalue()
    finally:
        logger.removeHandler(handler)


def test_logger_sanitizes_structured_message_and_formatter_extras():
    logger = protect_logger(logging.getLogger("kindex.privacy-extra-test"))
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s %(password)s"))
    logger.addHandler(handler)
    try:
        logger.warning(json.dumps({"token": "short"}), extra={"password": "another"})
        assert "short" not in stream.getvalue()
        assert "another" not in stream.getvalue()
        assert REDACTED in stream.getvalue()
    finally:
        logger.removeHandler(handler)
