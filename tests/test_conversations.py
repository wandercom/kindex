"""Tests for conversation ingest and digests (kindex.conversations)."""

import json
from types import SimpleNamespace

import pytest

from kindex import conversations as conv
from kindex.config import Config, LLMConfig
from kindex.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(Config(data_dir=str(tmp_path)))
    yield s
    s.close()


MESSAGES = [
    {"role": "user", "content": "I bought a red kayak for $450."},
    {"role": "assistant", "content": "Nice! " + "Paddle safely. " * 300},
    {"role": "user", "content": "Always give distances in kilometres."},
]


def test_pack_keeps_whole_messages_and_splits_long_ones():
    chunks = conv.pack(["a" * 10, "b" * 25, "c" * 5], limit=20)
    assert chunks == ["a" * 10, "b" * 20, "b" * 5 + "\n" + "c" * 5]
    assert all(len(c) <= 20 for c in chunks)


def test_ingest_is_lossless_dated_linked_and_idempotent(store):
    ids = conv.ingest_conversation(store, "c1", MESSAGES, "2024/03/10 (Sun) 09:00")
    assert len(ids) == 3  # the 4,500-character reply is split across two nodes
    text = "\n".join(store.get_node(i)["content"] for i in ids)
    for m in MESSAGES:
        assert m["content"].strip() in text.replace("\n", "")
    first = store.get_node(ids[0])
    assert first["prov_when"] == "2024/03/10 (Sun) 09:00"
    assert first["type"] == "document"
    assert [e["to_id"] for e in store.edges_from(ids[0])] == [ids[1]]
    assert conv.ingest_conversation(store, "c1", MESSAGES, "2024/03/10 (Sun) 09:00") == []


def test_ingest_directory_reads_json_and_jsonl(store, tmp_path):
    d = tmp_path / "convs"
    d.mkdir()
    (d / "a.json").write_text(json.dumps({"id": "a", "date": "2024-01-01", "messages": MESSAGES[:1]}))
    (d / "b.jsonl").write_text(json.dumps({"id": "b", "messages": MESSAGES[2:]}) + "\n")
    assert conv.ingest_directory(store, d) == 2


def test_conversations_adapter_is_discovered():
    from kindex.adapters.registry import discover

    assert "conversations" in discover()


def _fake_client(payload):
    calls = []

    def create(**kw):
        calls.append(kw)
        usage = SimpleNamespace(input_tokens=1, output_tokens=1, cache_creation_input_tokens=0,
                                cache_read_input_tokens=0)
        return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(payload))], usage=usage)

    return SimpleNamespace(messages=SimpleNamespace(create=create)), calls


def test_digest_records_directives_once_and_a_linked_summary(store, tmp_path, monkeypatch):
    from kindex import llm

    client, calls = _fake_client({"directives": ["Always give distances in kilometres."],
                                  "summary": "The user bought a red kayak for $450 on 10 March 2024."})
    monkeypatch.setattr(llm, "get_client", lambda config, **kw: client)
    cfg = Config(data_dir=str(tmp_path), llm=LLMConfig(enabled=True, provider="openai", model="m"))
    monkeypatch.setattr(conv, "SUMMARY_MIN_TOKENS", 0)
    ids = conv.ingest_conversation(store, "c1", MESSAGES, "2024-03-10")
    assert conv.backfill_digests(store, cfg) == 1
    directives = store.all_nodes(node_type="directive")
    assert [d["title"] for d in directives] == ["Always give distances in kilometres."]
    summary = [n for n in store.all_nodes(node_type="document") if n["id"].startswith("convsum-")][0]
    assert "$450" in summary["content"] and summary["prov_when"] == "2024-03-10"
    assert {e["to_id"] for e in store.edges_from(summary["id"])} == set(ids)
    assert calls[0]["json_schema"]["name"] == "conversation_digest"
    # Already digested: no second call, no duplicate directive.
    assert conv.backfill_digests(store, cfg) == 0
    assert len(calls) == 1


def test_short_conversations_get_directives_but_no_summary(store, tmp_path, monkeypatch):
    from kindex import llm

    client, calls = _fake_client({"directives": [], "summary": "should not be stored"})
    monkeypatch.setattr(llm, "get_client", lambda config, **kw: client)
    cfg = Config(data_dir=str(tmp_path), llm=LLMConfig(enabled=True, provider="openai", model="m"))
    conv.ingest_conversation(store, "c1", MESSAGES[:1], "2024-03-10")
    assert conv.backfill_digests(store, cfg) == 1
    assert calls[0]["json_schema"]["schema"]["required"] == ["directives"]
    assert not [n for n in store.all_nodes(node_type="document") if n["id"].startswith("convsum-")]


def test_user_messages_keeps_only_the_users_side():
    text = "user: line one\ncontinues here\nassistant: a long reply\nuser: second"
    assert conv.user_messages(text) == "user: line one\ncontinues here\nuser: second"
    assert conv.user_messages("Caroline: hi\nMelanie: hello") == ""


def test_facts_become_dated_nodes_when_enabled(store, tmp_path, monkeypatch):
    from kindex import llm
    from kindex.config import ConversationsConfig

    client, calls = _fake_client({"directives": [], "facts": [
        {"date": "2024-03-09", "subject": "user", "text": "The user bought a red kayak for $450 on the Saturday before 2024-03-10."}]})
    monkeypatch.setattr(llm, "get_client", lambda config, **kw: client)
    cfg = Config(data_dir=str(tmp_path), llm=LLMConfig(enabled=True, provider="openai", model="m"),
                 conversations=ConversationsConfig(facts=True))
    conv.ingest_conversation(store, "c1", MESSAGES[:2], "2024-03-10")
    conv.backfill_digests(store, cfg)
    prompt = calls[0]["messages"][0]["content"]
    assert "- facts:" in prompt and "assistant: Nice!" in prompt  # full text, not the user's side only
    facts = [n for n in store.all_nodes(node_type="document") if n["id"].startswith("convfact-")]
    assert len(facts) == 1 and facts[0]["extra"]["fact_date"] == "2024-03-09"


def test_facts_are_off_by_default(store, tmp_path, monkeypatch):
    from kindex import llm

    client, calls = _fake_client({"directives": []})
    monkeypatch.setattr(llm, "get_client", lambda config, **kw: client)
    cfg = Config(data_dir=str(tmp_path), llm=LLMConfig(enabled=True, provider="openai", model="m"))
    conv.ingest_conversation(store, "c1", MESSAGES[:1], "2024-03-10")
    conv.backfill_digests(store, cfg)
    assert "- facts:" not in calls[0]["messages"][0]["content"]


# ── Re-ingest reconciles a conversation with what was stored before ──

def _chunks(store, cid):
    nodes = [n for n in store.all_nodes(node_type="document", limit=10_000)
             if (n.get("extra") or {}).get("conversation_id") == cid and "kind" not in (n.get("extra") or {})]
    return sorted(nodes, key=lambda n: n["extra"]["position"])


def _linked_in_order(store, nodes):
    return all(any(e["to_id"] == b["id"] for e in store.edges_from(a["id"])) for a, b in zip(nodes, nodes[1:]))


def test_an_append_inside_the_last_chunk_is_stored(store):
    conv.ingest_conversation(store, "c1", MESSAGES[:1], "2024-03-10")
    changed = conv.ingest_conversation(store, "c1", MESSAGES[:1] + [{"role": "user", "content": "And a paddle."}],
                                       "2024-03-10")
    assert changed == [conv.chunk_id("c1", 0)]
    assert "And a paddle." in store.get_node(changed[0])["content"]


def test_an_append_across_a_chunk_boundary_is_stored_and_linked(store):
    conv.ingest_conversation(store, "c1", MESSAGES[:1], "2024-03-10")
    conv.ingest_conversation(store, "c1", MESSAGES, "2024-03-10")
    chunks = _chunks(store, "c1")
    assert len(chunks) == 3 and _linked_in_order(store, chunks)
    assert "kilometres" in chunks[-1]["content"]


def test_an_edit_replaces_only_the_changed_chunk(store):
    conv.ingest_conversation(store, "c1", MESSAGES, "2024-03-10")
    edited = [{"role": "user", "content": "I bought a blue kayak for $450."}] + MESSAGES[1:]
    assert conv.ingest_conversation(store, "c1", edited, "2024-03-10") == [conv.chunk_id("c1", 0)]
    assert "blue kayak" in _chunks(store, "c1")[0]["content"]
    assert _linked_in_order(store, _chunks(store, "c1"))


def test_a_shorter_conversation_drops_its_old_tail(store):
    conv.ingest_conversation(store, "c1", MESSAGES, "2024-03-10")
    conv.ingest_conversation(store, "c1", MESSAGES[:1], "2024-03-10")
    assert len(_chunks(store, "c1")) == 1


def test_an_interrupted_ingest_is_completed_next_time(store, monkeypatch):
    real = store.add_node
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real(*a, **kw)

    monkeypatch.setattr(store, "add_node", flaky)
    with pytest.raises(OSError):
        conv.ingest_conversation(store, "c1", MESSAGES, "2024-03-10")
    monkeypatch.setattr(store, "add_node", real)
    assert len(_chunks(store, "c1")) == 1
    conv.ingest_conversation(store, "c1", MESSAGES, "2024-03-10")
    chunks = _chunks(store, "c1")
    assert len(chunks) == 3 and _linked_in_order(store, chunks)


# ── Digests: grouping, status and revisions ──

def _cfg(tmp_path, **conversations):
    from kindex.config import ConversationsConfig

    return Config(data_dir=str(tmp_path), llm=LLMConfig(enabled=True, provider="openai", model="m"),
                  conversations=ConversationsConfig(**conversations))


def test_conversations_sharing_a_file_are_digested_separately(store, tmp_path, monkeypatch):
    from kindex import llm

    client, calls = _fake_client({"directives": []})
    monkeypatch.setattr(llm, "get_client", lambda config, **kw: client)
    d = tmp_path / "convs"
    d.mkdir()
    (d / "export.jsonl").write_text(
        json.dumps({"date": "2024-01-01", "messages": [{"role": "user", "content": "January kayak"}]}) + "\n"
        + json.dumps({"date": "2024-02-01", "messages": [{"role": "user", "content": "February canoe"}]}) + "\n")
    conv.ingest_directory(store, d)
    assert conv.backfill_digests(store, _cfg(tmp_path)) == 2
    prompts = sorted(c["messages"][0]["content"] for c in calls)
    assert "on 2024-01-01" in prompts[0] and "January kayak" in prompts[0] and "February" not in prompts[0]
    assert "on 2024-02-01" in prompts[1] and "February canoe" in prompts[1] and "January" not in prompts[1]
    for cid in ("export#0", "export#1"):
        assert _chunks(store, cid)[0]["prov_source"] == str(d / "export.jsonl")


def test_a_failed_digest_is_retried(store, tmp_path, monkeypatch):
    from kindex import llm

    def broken(**kw):
        raise RuntimeError("OpenAI API error 500")

    monkeypatch.setattr(llm, "get_client", lambda config, **kw: SimpleNamespace(
        messages=SimpleNamespace(create=broken)))
    conv.ingest_conversation(store, "c1", MESSAGES[:1], "2024-03-10")
    assert conv.backfill_digests(store, _cfg(tmp_path)) == 0
    client, calls = _fake_client({"directives": ["Always give distances in kilometres."]})
    monkeypatch.setattr(llm, "get_client", lambda config, **kw: client)
    assert conv.backfill_digests(store, _cfg(tmp_path)) == 1
    assert len(store.all_nodes(node_type="directive")) == 1


def test_an_unparseable_digest_is_retried(store, tmp_path, monkeypatch):
    from kindex import llm

    client, calls = _fake_client("not an object")
    monkeypatch.setattr(llm, "get_client", lambda config, **kw: client)
    conv.ingest_conversation(store, "c1", MESSAGES[:1], "2024-03-10")
    assert conv.backfill_digests(store, _cfg(tmp_path)) == 0
    assert conv.backfill_digests(store, _cfg(tmp_path)) == 0
    assert len(calls) == 2


def test_a_spent_budget_records_nothing(store, tmp_path, monkeypatch):
    from kindex import llm

    client, calls = _fake_client({"directives": []})
    monkeypatch.setattr(llm, "get_client", lambda config, **kw: client)
    conv.ingest_conversation(store, "c1", MESSAGES[:1], "2024-03-10")
    spent = SimpleNamespace(can_spend=lambda: False)
    assert conv.backfill_digests(store, _cfg(tmp_path), spent) == 0
    assert calls == []
    assert conv.backfill_digests(store, _cfg(tmp_path)) == 1


def test_a_changed_conversation_is_digested_again(store, tmp_path, monkeypatch):
    from kindex import llm

    client, calls = _fake_client({"directives": ["Always give distances in kilometres."]})
    monkeypatch.setattr(llm, "get_client", lambda config, **kw: client)
    conv.ingest_conversation(store, "c1", MESSAGES[:1], "2024-03-10")
    assert conv.backfill_digests(store, _cfg(tmp_path)) == 1
    conv.ingest_conversation(store, "c1", MESSAGES[:1] + MESSAGES[2:], "2024-03-10")
    assert conv.backfill_digests(store, _cfg(tmp_path)) == 1
    assert "kilometres" in calls[-1]["messages"][0]["content"]
    assert len(store.all_nodes(node_type="directive")) == 1
    assert conv.backfill_digests(store, _cfg(tmp_path)) == 0


def test_turning_on_facts_digests_again(store, tmp_path, monkeypatch):
    from kindex import llm

    client, calls = _fake_client({"directives": [], "facts": []})
    monkeypatch.setattr(llm, "get_client", lambda config, **kw: client)
    conv.ingest_conversation(store, "c1", MESSAGES[:1], "2024-03-10")
    assert conv.backfill_digests(store, _cfg(tmp_path)) == 1
    assert conv.backfill_digests(store, _cfg(tmp_path, facts=True)) == 1
    assert "- facts:" in calls[-1]["messages"][0]["content"]


def test_a_record_from_before_revisions_is_honoured(store, tmp_path, monkeypatch):
    from kindex import llm

    client, calls = _fake_client({"directives": []})
    monkeypatch.setattr(llm, "get_client", lambda config, **kw: client)
    conv.ingest_conversation(store, "c1", MESSAGES[:1], "2024-03-10")
    store.set_meta("conversation_digests", json.dumps(["c1"]))
    assert conv.backfill_digests(store, _cfg(tmp_path)) == 0 and calls == []


# ── Retention: expiry and retraction reach everything derived ──

def test_retraction_removes_the_conversation_and_what_only_it_gave(store, tmp_path, monkeypatch):
    from kindex import llm

    client, _ = _fake_client({"directives": ["Always give distances in kilometres."],
                              "summary": "A kayak purchase."})
    monkeypatch.setattr(llm, "get_client", lambda config, **kw: client)
    monkeypatch.setattr(conv, "SUMMARY_MIN_TOKENS", 0)
    d = tmp_path / "convs"
    d.mkdir()
    (d / "a.json").write_text(json.dumps([{"id": "a", "date": "2024-01-01", "messages": MESSAGES},
                                          {"id": "b", "date": "2024-01-02", "messages": MESSAGES[2:]}]))
    conv.ingest_directory(store, d)
    assert conv.backfill_digests(store, _cfg(tmp_path)) == 2
    (d / "a.json").write_text(json.dumps([{"id": "a", "retracted": True},
                                          {"id": "b", "date": "2024-01-02", "messages": MESSAGES[2:]}]))
    conv.ingest_directory(store, d)
    assert _chunks(store, "a") == []
    assert not [n for n in store.all_nodes(node_type="document", limit=1000)
                if (n.get("extra") or {}).get("conversation_id") == "a"]
    directive = store.all_nodes(node_type="directive")
    assert len(directive) == 1 and directive[0]["extra"]["conversations"] == {"b": None}
    (d / "a.json").write_text(json.dumps([{"id": "b", "retracted": True}]))
    conv.ingest_directory(store, d)
    assert store.all_nodes(node_type="directive") == []
    assert "a" not in json.loads(store.get_meta("conversation_digests"))


def test_expiry_reaches_chunks_and_everything_derived(store, tmp_path, monkeypatch):
    from kindex import llm
    from kindex.store import node_expired

    client, _ = _fake_client({"directives": ["Always give distances in kilometres."], "summary": "Kayak."})
    monkeypatch.setattr(llm, "get_client", lambda config, **kw: client)
    monkeypatch.setattr(conv, "SUMMARY_MIN_TOKENS", 0)
    conv.ingest_conversation(store, "old", MESSAGES, "2024-01-01", expires="2024-06-01")
    conv.backfill_digests(store, _cfg(tmp_path))
    derived = [n for n in store.all_nodes(limit=1000)
               if (n.get("extra") or {}).get("conversation_id") == "old" or n["type"] == "directive"]
    assert len(derived) == 5  # three chunks, a summary, a directive
    assert all(node_expired(n, today="2025-01-01") for n in derived)
    # Another conversation giving the same directive, without an expiry, keeps it live.
    conv.ingest_conversation(store, "new", MESSAGES[2:], "2024-02-01")
    conv.backfill_digests(store, _cfg(tmp_path))
    directive = store.all_nodes(node_type="directive")[0]
    assert not node_expired(directive, today="2025-01-01")
