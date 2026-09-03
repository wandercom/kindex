"""Tests for hooks module — prime_context, capture_session_end, write_inbox_item, generate_session_directive."""

import datetime
import os
import sys

import pytest

from kindex.config import Config
from kindex.store import Store


@pytest.fixture
def store(tmp_path):
    cfg = Config(data_dir=str(tmp_path))
    s = Store(cfg)
    yield s
    s.close()


@pytest.fixture
def config(tmp_path):
    return Config(data_dir=str(tmp_path))


@pytest.fixture
def ledger(tmp_path):
    from kindex.budget import BudgetLedger
    cfg = Config(data_dir=str(tmp_path))
    return BudgetLedger(cfg.ledger_path, cfg.budget)


class TestPrimeContext:
    def test_prime_context_basic(self, store):
        """Creates nodes, calls prime_context, verifies output contains node titles."""
        from kindex.hooks import prime_context

        store.add_node("Stigmergy Coordination", content="Agents communicate through environment",
                        node_type="concept", node_id="stig")
        store.add_node("Graph Theory Basics", content="Study of nodes and edges",
                        node_type="concept", node_id="graph")
        store.add_edge("stig", "graph", provenance="test")

        output = prime_context(store, topic="stigmergy", max_tokens=750)

        assert "Kindex Context" in output
        # Should contain at least one of the node titles (found via FTS)
        assert "Stigmergy" in output or "Graph Theory" in output

    def test_prime_context_with_ops(self, store):
        """Creates constraints/watches, verifies they appear in output."""
        from kindex.hooks import prime_context

        # Add some searchable content first
        store.add_node("Test Domain Concept", content="Test domain content",
                        node_type="concept", node_id="test-concept")

        # Add operational nodes
        store.add_node("Never deploy on Friday", node_type="constraint",
                        node_id="c1", extra={"trigger": "pre-deploy", "action": "block"})
        store.add_node("Monitor API latency", node_type="watch",
                        node_id="w1", extra={"owner": "alice", "expires": "2026-12-31"})

        output = prime_context(store, topic="test", max_tokens=1500)

        # Operational nodes should appear
        assert "constraint" in output.lower() or "Friday" in output
        assert "watch" in output.lower() or "Monitor" in output or "latency" in output

    def test_prime_context_scopes_nodes_by_adapter(self, store):
        """An Antigravity-scoped directive must not surface when priming Claude."""
        from kindex.hooks import prime_context

        store.add_node("Test Domain Concept", content="Test domain content",
                        node_type="concept", node_id="pc-concept")
        store.add_node(
            "Antigravity PreToolUse hook protocol",
            node_type="directive",
            node_id="ag-dir",
            content="Antigravity PreToolUse stdin is nested camelCase JSON.",
            domains=["antigravity"],
        )

        # The directive's CONTENT (Key concepts / Directives) and its TITLE (the now
        # also-scoped 24h "Recent activity" changelog) must both stay out of a Claude
        # session. The topic matches so it would otherwise surface into Key concepts.
        topic = "Antigravity PreToolUse hook protocol"
        claude_out = prime_context(store, topic=topic, max_tokens=1500, adapter="claude")
        assert "nested camelCase JSON" not in claude_out
        assert "Antigravity PreToolUse hook protocol" not in claude_out

        ag_out = prime_context(store, topic=topic, max_tokens=1500, adapter="antigravity")
        assert "nested camelCase JSON" in ag_out

    def test_prime_context_empty_store(self, store):
        """Prime context on empty store should not crash."""
        from kindex.hooks import prime_context

        output = prime_context(store, topic="anything")
        assert "Kindex Context" in output

    def test_prime_context_respects_token_limit(self, store):
        """Output should stay within approximate token budget."""
        from kindex.hooks import prime_context

        for i in range(20):
            store.add_node(f"Concept {i}", content=f"Detailed content about concept {i} " * 20,
                            node_type="concept")

        output = prime_context(store, topic="concept", max_tokens=200)
        # 200 tokens ~600 chars; output should be roughly in that range
        # Allow some overhead for headers
        assert len(output) < 3000  # generous upper bound for 200 token budget


class TestCaptureSessionEnd:
    def test_capture_session_end(self, store, config, ledger):
        """Provides session text, verifies nodes/edges are created."""
        from kindex.hooks import capture_session_end

        session_text = (
            "We learned that Graph Neural Networks can be applied to knowledge graphs. "
            "We decided to use PyTorch Geometric because it has good documentation. "
            "How does attention mechanism work in transformers? "
            "This is similar to the Self Attention Pattern we discussed earlier."
        )

        count = capture_session_end(store, config, ledger, session_text=session_text)

        # Should have created at least some nodes
        assert count >= 1

        # Check that nodes were actually created in the store
        nodes = store.all_nodes()
        assert len(nodes) >= 1

    def test_capture_session_end_empty(self, store, config, ledger):
        """Empty session text returns 0."""
        from kindex.hooks import capture_session_end

        count = capture_session_end(store, config, ledger, session_text="")
        assert count == 0

    def test_capture_session_end_too_short(self, store, config, ledger):
        """Very short text returns 0."""
        from kindex.hooks import capture_session_end

        count = capture_session_end(store, config, ledger, session_text="hello")
        assert count == 0

    def test_capture_session_end_with_existing_nodes(self, store, config, ledger, monkeypatch):
        """Should link to existing nodes rather than duplicate.

        Extraction is mocked to deterministically return a concept whose title
        exactly matches the pre-existing node. A live LLM would return arbitrary
        title variants ("GNNs", "Graph Neural Networks (GNN)"), making the
        dedup-vs-duplicate outcome nondeterministic — this test exercises the
        dedup logic, not the extractor.
        """
        import kindex.extract as extract_mod
        from kindex.hooks import capture_session_end

        # Pre-populate store
        store.add_node("Graph Neural Networks", content="ML on graphs",
                        node_type="concept", node_id="gnn")

        def fake_extract(text, existing_titles, cfg, led):
            return {
                "concepts": [
                    {"title": "Graph Neural Networks", "content": "ML on graphs",
                     "domains": [], "type": "concept"},
                    {"title": "Bridge Pattern", "content": "connects domains",
                     "domains": [], "type": "concept"},
                ],
                "decisions": [], "questions": [],
                "connections": [], "bridge_opportunities": [],
            }

        monkeypatch.setattr(extract_mod, "extract", fake_extract)

        session_text = (
            "We explored how Graph Neural Networks can improve knowledge graph completion. "
            "The Bridge Pattern connects two unrelated domains effectively. "
            "We decided to use a message passing approach because it handles heterogeneous graphs."
        )

        capture_session_end(store, config, ledger, session_text=session_text)

        # The pre-existing "Graph Neural Networks" should not be duplicated
        gnn_nodes = [n for n in store.all_nodes() if "graph neural" in n["title"].lower()]
        assert len(gnn_nodes) == 1  # should not create a duplicate

    def test_capture_links_nodes_to_same_project_tag(
        self, store, config, ledger, monkeypatch, tmp_path
    ):
        import kindex.extract as extract_mod
        from kindex.hooks import capture_session_end
        from kindex.sessions import get_tag, start_tag

        monkeypatch.chdir(tmp_path)
        start_tag(store, "capture-tag", project_path=str(tmp_path))
        monkeypatch.setattr(
            extract_mod,
            "extract",
            lambda *_args, **_kwargs: {
                "concepts": [{
                    "title": "Captured concept",
                    "content": "Enough detail to retain this concept.",
                    "domains": [],
                    "type": "concept",
                }],
                "decisions": [],
                "questions": [],
                "connections": [],
                "bridge_opportunities": [],
            },
        )

        capture_session_end(
            store,
            config,
            ledger,
            session_text="A sufficiently long session transcript for extraction.",
        )

        tag = get_tag(store, "capture-tag", project_path=str(tmp_path))
        assert len(tag["extra"]["linked_nodes"]) == 1
        captured_id = tag["extra"]["linked_nodes"][0]
        assert any(
            edge["to_id"] == tag["id"]
            for edge in store.edges_from(captured_id)
        )


class TestWriteInboxItem:
    def test_write_inbox_item(self, config):
        """Writes an inbox item, verifies file exists with correct content."""
        from kindex.hooks import write_inbox_item

        path = write_inbox_item(
            config, content="This is a test inbox item.",
            source="test", topic_hint="testing"
        )

        assert path.exists()
        text = path.read_text()
        assert "This is a test inbox item." in text
        assert "source: test" in text
        assert "topic_hint: testing" in text
        assert "processed: false" in text

    def test_write_inbox_item_no_optional(self, config):
        """Inbox item without optional fields."""
        from kindex.hooks import write_inbox_item

        path = write_inbox_item(config, content="Minimal item.")

        assert path.exists()
        text = path.read_text()
        assert "Minimal item." in text
        assert "---" in text

    def test_write_inbox_item_unique_names(self, config):
        """Multiple items should have unique filenames."""
        from kindex.hooks import write_inbox_item

        path1 = write_inbox_item(config, content="Item 1")
        path2 = write_inbox_item(config, content="Item 2")

        assert path1 != path2
        assert path1.exists()
        assert path2.exists()


class TestGenerateSessionDirective:
    def test_generate_session_directive(self, store):
        """Verifies directive contains capture instructions."""
        from kindex.hooks import generate_session_directive

        output = generate_session_directive(store)

        assert "Knowledge Capture" in output
        assert "`add`" in output
        assert "`link`" in output
        assert "concept" in output.lower()
        assert "decision" in output.lower()

    def test_generate_session_directive_with_nodes(self, store):
        """Directive shows graph stats when nodes exist."""
        from kindex.hooks import generate_session_directive

        store.add_node("Test Node A", node_id="a")
        store.add_node("Test Node B", node_id="b")
        store.add_edge("a", "b")

        output = generate_session_directive(store)

        # Should mention current graph stats
        assert "nodes" in output.lower() or "2 nodes" in output

    def test_generate_session_directive_with_suggestions(self, store):
        """Directive mentions pending suggestions if any exist."""
        from kindex.hooks import generate_session_directive

        store.add_node("Alpha", node_id="alpha")
        store.add_node("Beta", node_id="beta")
        store.add_edge("alpha", "beta")
        store.add_suggestion("Alpha", "Beta", reason="test bridge")

        output = generate_session_directive(store)

        assert "suggest" in output.lower()


class TestRemindKindexUsage:
    def test_directive_injected_by_default(self, store):
        """With no config (or default config), the use-kindex directive is injected."""
        from kindex.hooks import prime_context

        output = prime_context(store, topic="anything")
        assert "Session directives" in output
        assert "use kindex MCP tools" in output

    def test_directive_includes_kin_discovery_and_commit_guidance(self, store):
        from kindex.hooks import prime_context

        output = prime_context(store, topic="anything")
        # Discover .kin for touched files (not just cwd root) and commit it with the code
        assert ".kin/" in output
        assert "cwd root" in output
        assert "git add" in output

    def test_directive_can_be_disabled_via_config(self, store, config):
        from kindex.hooks import prime_context

        config.reminders.remind_kindex_usage = False
        output = prime_context(store, topic="anything", config=config)
        assert "Session directives" not in output
        assert "You MUST use kindex MCP tools" not in output

    def test_directive_enabled_via_config(self, store, config):
        from kindex.hooks import prime_context

        config.reminders.remind_kindex_usage = True
        output = prime_context(store, topic="anything", config=config)
        assert "Session directives" in output
        assert ".kin/" in output

    def test_generate_session_directive_includes_kin_guidance(self, store):
        from kindex.hooks import generate_session_directive

        output = generate_session_directive(store)
        assert ".kin/" in output
        assert "repo root" in output


# ── Active collabs (Stage 5: collab injection) ──────────────────────


AGENT = "alice@test"


@pytest.fixture
def collab_config(tmp_path, monkeypatch):
    """Config with a pinned agent identity (env override stripped)."""
    monkeypatch.delenv("KIN_AGENT_ID", raising=False)
    return Config(data_dir=str(tmp_path), agent_id=AGENT)


def _make_collab(store, name="ship-it", agent=AGENT):
    """Create a conversation with `agent` as a member; returns the conv id."""
    from kindex import coordination as coord
    return coord.create_conversation(store, name, created_by=agent)


class TestPrimeCollabs:
    def test_collab_section_present_for_member(self, store, collab_config):
        from kindex import coordination as coord
        from kindex.hooks import prime_context
        from kindex.locks import lock_node

        _make_collab(store)
        coord.post_message(store, "ship-it", "bob@test", "broadcast hello")
        coord.set_inject_message(store, "ship-it", "Use the staging DB only",
                                 "bob@test")
        store.add_node("Deploy Plan", node_type="document", node_id="plan-1")
        coord.attach_resource(store, "ship-it", "plan-1")
        lock_node(store, "plan-1", "bob@test")

        output = prime_context(store, topic="anything", config=collab_config)

        assert "### Active collabs" in output
        assert "**ship-it**" in output
        assert "1 unread" in output
        assert "COLLAB MSG: Use the staging DB only (from bob@test)" in output
        assert "Locked: Deploy Plan (held by bob@test)" in output
        assert "Check the collab: coord_read ship-it" in output

    def test_check_line_unconditional_even_when_quiet_collab(self, store, collab_config):
        """A member collab with no unread/injects still gets the check line."""
        from kindex.hooks import prime_context

        _make_collab(store, name="idle-room")
        output = prime_context(store, topic="anything", config=collab_config)
        assert "### Active collabs" in output
        assert "Check the collab: coord_read idle-room" in output

    def test_collab_section_absent_for_non_member(self, store, collab_config):
        from kindex.hooks import prime_context

        _make_collab(store, agent="someone-else@host")
        output = prime_context(store, topic="anything", config=collab_config)
        assert "### Active collabs" not in output

    def test_collab_section_absent_when_disabled(self, store, collab_config):
        from kindex.hooks import prime_context

        _make_collab(store)
        collab_config.collab.enabled = False
        output = prime_context(store, topic="anything", config=collab_config)
        assert "### Active collabs" not in output

    def test_collab_section_suppressed_when_quiet(self, store, collab_config):
        from kindex.hooks import prime_context

        _make_collab(store)
        collab_config.collab.display = "quiet"
        output = prime_context(store, topic="anything", config=collab_config)
        assert "### Active collabs" not in output

    def test_collab_minimal_one_line_per_collab(self, store, collab_config):
        from kindex import coordination as coord
        from kindex.hooks import prime_context

        _make_collab(store)
        coord.post_message(store, "ship-it", "bob@test", "hi there")
        coord.set_inject_message(store, "ship-it", "standing order", "bob@test")
        collab_config.collab.display = "minimal"

        output = prime_context(store, topic="anything", config=collab_config)

        assert "### Active collabs" in output
        line = next(l for l in output.splitlines() if l.startswith("- ship-it:"))
        assert "1 unread" in line
        assert "coord_read ship-it" in line
        # Minimal mode compresses everything onto the one line
        assert "COLLAB MSG" not in output
        assert "Check the collab:" not in output

    def test_collab_cap_three_plus_more(self, store, collab_config):
        from kindex.hooks import prime_context

        for i in range(5):
            _make_collab(store, name=f"room-{i}")
        output = prime_context(store, topic="anything", config=collab_config)

        shown = [l for l in output.splitlines() if l.startswith("- **room-")]
        assert len(shown) == 3
        assert "- +2 more" in output

    def test_collab_inject_truncated_to_200_chars(self, store, collab_config):
        from kindex import coordination as coord
        from kindex.hooks import prime_context

        _make_collab(store)
        long_text = "x" * 500
        coord.set_inject_message(store, "ship-it", long_text, "bob@test")
        output = prime_context(store, topic="anything", config=collab_config)

        assert "x" * 200 in output
        assert "x" * 201 not in output


class TestPromptCheckCollabLines:
    """In-process tests of the _collab_prompt_lines helper."""

    def _seed(self, store):
        from kindex import coordination as coord
        _make_collab(store)
        coord.post_message(store, "ship-it", "bob@test", "broadcast note")
        coord.post_message(store, "ship-it", "bob@test", "for alice only",
                           to=AGENT)
        coord.post_message(store, "ship-it", "bob@test", "for carol only",
                           to="carol@test")
        coord.set_inject_message(store, "ship-it", "standing instruction",
                                 "bob@test")

    def test_lines_carry_new_messages_and_injects(self, store, collab_config):
        from kindex.cli import _collab_prompt_lines

        self._seed(store)
        lines = _collab_prompt_lines(store, collab_config, "conv-1")
        joined = "\n".join(lines)

        assert "COLLAB UPDATES" in joined
        assert "broadcast note" in joined
        assert "for alice only" in joined
        assert "(to you)" in joined
        assert "for carol only" not in joined  # targeted at someone else
        assert "COLLAB MSG: standing instruction (from bob@test)" in joined
        assert "Check the collab: coord_read ship-it" in joined

    def test_cooldown_suppresses_then_expires(self, store, collab_config):
        from kindex.cli import _collab_prompt_lines

        self._seed(store)
        assert _collab_prompt_lines(store, collab_config, "conv-1")
        # Within the cooldown window: suppressed for the same conversation
        assert _collab_prompt_lines(store, collab_config, "conv-1") == []
        # ...but an unrelated conversation has its own cooldown key
        assert _collab_prompt_lines(store, collab_config, "conv-2")

        # Age the stamp past the window: lines flow again
        cooldown = collab_config.collab.prompt_cooldown_minutes
        old = (datetime.datetime.now()
               - datetime.timedelta(minutes=cooldown + 1)
               ).isoformat(timespec="seconds")
        store.set_meta("collab.prompt_last_injected.conv-1", old)
        assert _collab_prompt_lines(store, collab_config, "conv-1")

    def test_zero_cooldown_never_suppresses(self, store, collab_config):
        from kindex.cli import _collab_prompt_lines

        self._seed(store)
        collab_config.collab.prompt_cooldown_minutes = 0
        assert _collab_prompt_lines(store, collab_config, "conv-1")
        assert _collab_prompt_lines(store, collab_config, "conv-1")

    def test_respects_collab_enabled(self, store, collab_config):
        from kindex.cli import _collab_prompt_lines

        self._seed(store)
        collab_config.collab.enabled = False
        assert _collab_prompt_lines(store, collab_config, "conv-1") == []

    def test_silent_when_not_member(self, store, collab_config):
        from kindex.cli import _collab_prompt_lines

        _make_collab(store, agent="someone-else@host")
        assert _collab_prompt_lines(store, collab_config, "conv-1") == []

    def test_silent_when_nothing_new(self, store, collab_config):
        from kindex import coordination as coord
        from kindex.cli import _collab_prompt_lines

        self._seed(store)
        # Reading the room advances the cursor and clears unread...
        coord.read_messages(store, "ship-it", agent=AGENT)
        coord.clear_inject_messages(store, "ship-it")
        # ...so with no unread and no injects there is nothing to say
        assert _collab_prompt_lines(store, collab_config, "conv-1") == []

    def test_bodies_truncated_to_200_chars(self, store, collab_config):
        from kindex import coordination as coord
        from kindex.cli import _collab_prompt_lines

        _make_collab(store)
        coord.post_message(store, "ship-it", "bob@test", "y" * 500)
        lines = _collab_prompt_lines(store, collab_config, "conv-1")
        joined = "\n".join(lines)
        assert "y" * 200 in joined
        assert "y" * 201 not in joined

    def test_cursor_not_advanced_by_prompt_check(self, store, collab_config):
        from kindex import coordination as coord
        from kindex.cli import _collab_prompt_lines

        self._seed(store)
        collab_config.collab.prompt_cooldown_minutes = 0
        assert _collab_prompt_lines(store, collab_config, "conv-1")
        # The unread messages are still unread (only coord_read marks them)
        collabs = coord.active_collabs_for_agent(store, AGENT)
        assert collabs[0]["unread_count"] > 0


class TestPromptCheckCollabCLI:
    """Subprocess tests: collab lines through the real hook entry points."""

    def _env(self, tmp_path):
        env = dict(os.environ)
        env.pop("KIN_PROFILE", None)
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        env["HOME"] = str(home)
        env["KIN_PROJECT"] = str(tmp_path)
        env["KIN_AGENT_ID"] = AGENT
        return env

    def _run(self, tmp_path, *args, input_text=None):
        import subprocess
        cmd = [sys.executable, "-m", "kindex.cli", *args,
               "--data-dir", str(tmp_path)]
        return subprocess.run(cmd, input=input_text, capture_output=True,
                              text=True, timeout=60, env=self._env(tmp_path),
                              cwd=str(tmp_path))

    def _seed(self, tmp_path):
        from kindex import coordination as coord
        cfg = Config(data_dir=str(tmp_path))
        s = Store(cfg)
        _make_collab(s)
        coord.post_message(s, "ship-it", "bob@test", "broadcast note")
        coord.set_inject_message(s, "ship-it", "standing instruction",
                                 "bob@test")
        s.close()

    def test_prompt_check_emits_collab_lines(self, tmp_path):
        import json
        self._seed(tmp_path)
        r = self._run(tmp_path, "prompt-check",
                      input_text=json.dumps({"session_id": "chat-1",
                                             "prompt": "status"}))
        assert r.returncode == 0, r.stderr
        assert "COLLAB UPDATES" in r.stdout
        assert "broadcast note" in r.stdout
        assert "COLLAB MSG: standing instruction" in r.stdout
        assert "coord_read ship-it" in r.stdout

    def test_prompt_check_codex_envelope_carries_collab_block(self, tmp_path):
        import json
        self._seed(tmp_path)
        r = self._run(tmp_path, "prompt-check", "--adapter", "codex",
                      input_text=json.dumps({"session_id": "chat-1",
                                             "prompt": "status"}))
        assert r.returncode == 0, r.stderr
        payload = json.loads(r.stdout)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        assert "COLLAB UPDATES" in ctx
        assert "broadcast note" in ctx
        assert "coord_read ship-it" in ctx

    def test_prompt_check_antigravity_quiet_uses_antigravity_envelope(self, tmp_path):
        import json
        self._seed(tmp_path)
        cfg_path = tmp_path / "kin.test.yaml"
        cfg_path.write_text("attention:\n  display: quiet\n")
        r = self._run(
            tmp_path,
            "prompt-check",
            "--config",
            str(cfg_path),
            "--adapter",
            "antigravity",
            input_text=json.dumps({"session_id": "chat-1", "prompt": "status"}),
        )
        assert r.returncode == 0, r.stderr
        payload = json.loads(r.stdout)
        assert "injectSteps" in payload
        assert "hookSpecificOutput" not in payload
        assert "COLLAB UPDATES" in payload["injectSteps"][0]["ephemeralMessage"]

    def test_prompt_check_collab_disabled_is_silent(self, tmp_path):
        import json
        self._seed(tmp_path)
        cfg_path = tmp_path / "kin.test.yaml"
        cfg_path.write_text("collab:\n  enabled: false\n")
        r = self._run(tmp_path, "prompt-check", "--config", str(cfg_path),
                      input_text=json.dumps({"session_id": "chat-1",
                                             "prompt": "status"}))
        assert r.returncode == 0, r.stderr
        assert "COLLAB" not in r.stdout
        assert r.stdout.strip() == ""

    def test_prime_codex_envelope_carries_collab_section(self, tmp_path):
        import json
        self._seed(tmp_path)
        r = self._run(tmp_path, "prime", "--for", "hook",
                      "--adapter", "codex", input_text="{}")
        assert r.returncode == 0, r.stderr
        payload = json.loads(r.stdout)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert "### Active collabs" in ctx
        assert "Check the collab: coord_read ship-it" in ctx
