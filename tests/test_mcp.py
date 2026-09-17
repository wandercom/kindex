"""Tests for Kindex MCP server tool functions."""

import json
import os

import pytest

mcp = pytest.importorskip("mcp", reason="mcp not installed")


@pytest.fixture
def mcp_store(tmp_path):
    """Set up a Store + Config for MCP tool testing."""
    from kindex.config import Config
    from kindex.store import Store

    # An explicit config: load_config() read the developer's global config
    # and the repository's own .kin from the test's working directory.
    cfg = Config(data_dir=str(tmp_path))
    store = Store(cfg)

    # Add test data
    id1 = store.add_node(title="Stigmergy", content="Coordination through environmental traces",
                         node_type="concept", domains=["biology", "ai"],
                         prov_activity="test")
    id2 = store.add_node(title="Python", content="Expert-level programming skill",
                         node_type="skill", domains=["engineering"],
                         prov_activity="test")
    store.add_node(title="Never break the API contract",
                   content="All public endpoints must maintain backward compatibility",
                   node_type="constraint", prov_activity="test",
                   extra={"trigger": "pre-deploy", "action": "block"})
    store.add_edge(id1, id2, edge_type="relates_to", weight=0.6)

    yield store, cfg
    store.close()


@pytest.fixture
def patch_store(mcp_store, monkeypatch):
    """Patch the MCP server's _get_store to use test store."""
    store, cfg = mcp_store
    import kindex.mcp_server as mcp_mod
    monkeypatch.setattr(mcp_mod, "_store", store)
    monkeypatch.setattr(mcp_mod, "_config", cfg)
    return store, cfg


class TestMCPSearch:
    def test_search_finds_results(self, patch_store):
        from kindex.mcp_server import search
        result = search("stigmergy")
        assert "Stigmergy" in result
        assert "stigmergy" in result.lower()

    def test_search_no_results(self, patch_store):
        from kindex.mcp_server import search
        result = search("zzzznonexistent")
        assert "No results" in result or "0 results" in result or "Found" in result


class TestMCPAdd:
    def test_add_creates_node(self, patch_store):
        from kindex.mcp_server import add
        result = add("Graph theory is about vertices and edges", node_type="concept")
        assert "Created node" in result

    def test_add_with_type(self, patch_store):
        from kindex.mcp_server import add
        result = add("Should we use Redis?", node_type="question")
        assert "Created node" in result
        assert "question" in result


class TestMCPContext:
    def test_context_with_topic(self, patch_store):
        from kindex.mcp_server import context
        result = context(topic="stigmergy", level="abridged")
        assert "Kindex" in result or "stigmergy" in result.lower()

    def test_context_empty_topic(self, patch_store):
        from kindex.mcp_server import context
        result = context()
        # Should return something (recent nodes fallback)
        assert isinstance(result, str)


class TestMCPShow:
    def test_show_by_title(self, patch_store):
        from kindex.mcp_server import show
        result = show("Stigmergy")
        assert "Stigmergy" in result
        assert "concept" in result

    def test_show_not_found(self, patch_store):
        from kindex.mcp_server import show
        result = show("nonexistent-node-id")
        assert "not found" in result.lower()


class TestMCPLink:
    def test_link_nodes(self, patch_store):
        from kindex.mcp_server import add, link
        add("Machine Learning fundamentals", node_type="concept")
        result = link("Python", "Machine Learning fundamentals",
                       relationship="implements", weight=0.8)
        assert "Linked" in result

    def test_link_not_found(self, patch_store):
        from kindex.mcp_server import link
        result = link("nonexistent", "also-nonexistent")
        assert "not found" in result.lower()


class TestMCPListNodes:
    def test_list_all(self, patch_store):
        from kindex.mcp_server import list_nodes
        result = list_nodes()
        assert "node(s)" in result
        assert "Stigmergy" in result

    def test_list_by_type(self, patch_store):
        from kindex.mcp_server import list_nodes
        result = list_nodes(node_type="skill")
        assert "Python" in result

    def test_list_empty(self, patch_store):
        from kindex.mcp_server import list_nodes
        result = list_nodes(node_type="nonexistent-type")
        assert "No nodes" in result


class TestMCPStatus:
    def test_status_returns_stats(self, patch_store):
        from kindex.mcp_server import status
        result = status()
        assert "Nodes:" in result
        assert "Edges:" in result


class TestMCPAsk:
    def test_ask_procedural(self, patch_store):
        from kindex.mcp_server import ask
        result = ask("How do I use stigmergy?")
        assert "procedural" in result.lower()

    def test_ask_factual(self, patch_store):
        from kindex.mcp_server import ask
        result = ask("What is Python?")
        assert "factual" in result.lower()

    def test_ask_decision(self, patch_store):
        from kindex.mcp_server import ask
        result = ask("Should I use stigmergy vs direct communication?")
        assert "decision" in result.lower()


class TestMCPSuggest:
    def test_suggest_empty(self, patch_store):
        from kindex.mcp_server import suggest
        result = suggest()
        assert "No pending" in result or "suggestion" in result.lower()


class TestMCPLearn:
    def test_learn_extracts(self, patch_store):
        from kindex.mcp_server import learn
        result = learn("We decided to use Redis for caching because of its speed. "
                       "This connects to our Distributed Systems architecture.")
        assert "Extracted" in result

    def test_learn_creates_no_orphans(self, patch_store):
        """Regression: mcp-learn must not create unlinked title-only concepts.

        Every concept the extractor surfaces with real content gets grounded to
        a source node, so the orphan count must not grow after a learn() call.
        """
        from kindex.mcp_server import learn
        store, _ = patch_store
        before = len(store.orphans())
        learn("We discovered that the cache layer leaks memory under load. "
              "This relates to our Distributed Systems architecture and the "
              "garbage collection tuning we did last quarter.")
        after = len(store.orphans())
        assert after <= before, f"learn() created orphans: {before} -> {after}"

    def test_learn_rejects_low_information_concepts(self, patch_store):
        """Title-only concepts (no content, no domains) must be rejected."""
        from kindex.mcp_server import _is_substantive_concept
        assert not _is_substantive_concept({"title": "Distributed Systems"})
        assert not _is_substantive_concept({"title": "Distributed Systems",
                                            "content": "", "domains": []})
        assert not _is_substantive_concept({"title": "x", "content": "real"})
        assert _is_substantive_concept({"title": "Cache leak",
                                        "content": "leaks under load"})
        assert _is_substantive_concept({"title": "Cache leak", "domains": ["perf"]})


class TestMCPGraphStats:
    def test_graph_stats(self, patch_store):
        from kindex.mcp_server import graph_stats
        result = graph_stats()
        assert "Nodes:" in result
        assert "Density:" in result


class TestMCPChangelog:
    def test_changelog(self, patch_store):
        from kindex.mcp_server import changelog
        result = changelog(days=30)
        assert isinstance(result, str)


class TestMCPResources:
    def test_resource_status(self, patch_store):
        from kindex.mcp_server import resource_status
        result = resource_status()
        data = json.loads(result)
        assert "nodes" in data

    def test_resource_node(self, patch_store):
        from kindex.mcp_server import resource_node
        result = resource_node("Stigmergy")
        assert "Stigmergy" in result

    def test_resource_recent(self, patch_store):
        from kindex.mcp_server import resource_recent
        result = resource_recent()
        assert isinstance(result, str)

    def test_resource_orphans(self, patch_store):
        from kindex.mcp_server import resource_orphans
        result = resource_orphans()
        assert isinstance(result, str)


class TestMCPTags:
    def test_add_with_tags(self, patch_store):
        from kindex.mcp_server import add
        result = add("Tagged concept for testing", tags="python,ml")
        assert "Created node" in result
        store, _ = patch_store
        node = store.get_node_by_title("Tagged concept for testing")
        assert "python" in node["domains"]
        assert "ml" in node["domains"]

    def test_add_tags_supplement_domains(self, patch_store):
        from kindex.mcp_server import add
        result = add("Dual tagged item", tags="user-tag", domains="auto-tag")
        assert "Created node" in result
        store, _ = patch_store
        node = store.get_node_by_title("Dual tagged item")
        assert "user-tag" in node["domains"]
        assert "auto-tag" in node["domains"]

    def test_search_with_tags_filter(self, patch_store):
        from kindex.mcp_server import search
        result = search("coordination", tags="biology")
        assert "Stigmergy" in result

    def test_search_with_tags_excludes_non_matching(self, patch_store):
        from kindex.mcp_server import search
        result = search("coordination", tags="nonexistent")
        assert "No results" in result

    def test_list_nodes_with_tags(self, patch_store):
        from kindex.mcp_server import list_nodes
        result = list_nodes(tags="biology")
        assert "Stigmergy" in result

    def test_list_nodes_default_limit(self, patch_store):
        import inspect
        from kindex.mcp_server import list_nodes
        sig = inspect.signature(list_nodes)
        assert sig.parameters["limit"].default == 100


@pytest.fixture
def agent_env(monkeypatch):
    """Deterministic agent identity for collab tools."""
    monkeypatch.setenv("KIN_AGENT_ID", "mcp-agent")
    return "mcp-agent"


class TestMCPCoordLegacy:
    """First coverage of the pre-existing coord_* tools."""

    def test_start_post_read_list_end(self, patch_store, agent_env):
        from kindex.mcp_server import (
            coord_end,
            coord_list,
            coord_post,
            coord_read,
            coord_start,
        )

        result = coord_start("Build Plan", agent="agent-a")
        assert "Started coordination conversation" in result

        result = coord_post("build-plan", agent="agent-a", message="claimed parser")
        assert "Posted coordination message #1" in result

        result = coord_read("build-plan")
        assert "claimed parser" in result
        assert "agent-a" in result

        result = coord_list()
        assert "build-plan" in result

        result = coord_end("build-plan", summary="done")
        assert "Ended coordination conversation" in result
        assert "build-plan" not in coord_list()

    def test_post_to_missing_conversation_errors(self, patch_store, agent_env):
        from kindex.mcp_server import coord_post
        result = coord_post("ghost", message="anyone there?")
        assert "Could not post" in result

    def test_read_missing_conversation_errors(self, patch_store, agent_env):
        from kindex.mcp_server import coord_read
        result = coord_read("ghost")
        assert "Could not read" in result


class TestMCPCoordCollab:
    def test_start_defaults_creator_to_resolved_agent(self, patch_store, agent_env):
        from kindex.coordination import get_conversation
        from kindex.mcp_server import coord_start

        store, _ = patch_store
        coord_start("Crew")
        extra = get_conversation(store, "crew")["extra"]
        assert extra["created_by"] == "mcp-agent"
        assert [m["agent"] for m in extra["members"]] == ["mcp-agent"]

    def test_join_idempotent_and_errors(self, patch_store, agent_env):
        from kindex.coordination import get_conversation
        from kindex.mcp_server import coord_join, coord_start

        store, _ = patch_store
        coord_start("Crew", agent="agent-a")
        assert "Joined crew as mcp-agent" in coord_join("crew")
        assert "Joined crew as mcp-agent" in coord_join("crew")
        members = get_conversation(store, "crew")["extra"]["members"]
        assert [m["agent"] for m in members] == ["agent-a", "mcp-agent"]

        assert "Could not join" in coord_join("ghost")

    def test_read_advances_default_agent_cursor(self, patch_store, agent_env):
        from kindex.coordination import active_collabs_for_agent
        from kindex.mcp_server import coord_join, coord_post, coord_read, coord_start

        store, _ = patch_store
        coord_start("Crew", agent="agent-a")
        coord_join("crew")  # mcp-agent joins
        coord_post("crew", agent="agent-a", message="news")
        assert active_collabs_for_agent(store, "mcp-agent")[0]["unread_count"] == 1

        coord_read("crew")
        assert active_collabs_for_agent(store, "mcp-agent")[0]["unread_count"] == 0

    def test_post_targeted_message(self, patch_store, agent_env):
        from kindex.coordination import read_messages
        from kindex.mcp_server import coord_post, coord_start

        store, _ = patch_store
        coord_start("Crew")
        coord_post("crew", message="for b", to="agent-b")
        msg = read_messages(store, "crew")["messages"][0]
        assert msg["author"] == "mcp-agent"
        assert msg["to"] == "agent-b"

    def test_attach_by_id_title_and_missing(self, patch_store, agent_env):
        from kindex.coordination import get_conversation
        from kindex.mcp_server import coord_attach, coord_start

        store, _ = patch_store
        coord_start("Crew")
        result = coord_attach("crew", "Stigmergy")  # by title
        assert "Attached" in result
        resources = get_conversation(store, "crew")["extra"]["resources"]
        assert len(resources) == 1

        # by id, idempotent
        coord_attach("crew", resources[0])
        assert get_conversation(store, "crew")["extra"]["resources"] == resources

        assert "Could not attach" in coord_attach("crew", "no-such-node-xyz")

    def test_inject_set_list_clear(self, patch_store, agent_env):
        from kindex.mcp_server import coord_inject, coord_start

        coord_start("Crew")
        result = coord_inject("crew", action="set", text="branch frozen")
        assert "Set inject message #1" in result
        result = coord_inject("crew", action="set", text="b: rebase", to="agent-b")
        assert "#2 " in result or "#2" in result

        listing = coord_inject("crew", action="list")
        assert "branch frozen" in listing
        assert "-> agent-b" in listing

        assert "Cleared 1" in coord_inject("crew", action="clear", message_id=1)
        assert "Cleared 1" in coord_inject("crew", action="clear")
        assert "No inject messages" in coord_inject("crew", action="list")

    def test_inject_unknown_action(self, patch_store, agent_env):
        from kindex.mcp_server import coord_inject, coord_start
        coord_start("Crew")
        assert "Unknown inject action" in coord_inject("crew", action="bogus")


class TestMCPLocks:
    def test_acquire_conflict_force_release(self, patch_store, agent_env, monkeypatch):
        from kindex.mcp_server import lock_acquire, lock_release

        result = lock_acquire("Stigmergy", ttl_minutes=30, note="editing")
        assert "Locked Stigmergy" in result
        assert "mcp-agent" in result

        # Another agent cannot take or release it without force
        monkeypatch.setenv("KIN_AGENT_ID", "other-agent")
        assert "Could not lock node" in lock_acquire("Stigmergy")
        assert "Could not unlock node" in lock_release("Stigmergy")
        assert "Locked Stigmergy" in lock_acquire("Stigmergy", force=True)

        # New holder releases cleanly
        assert "Unlocked Stigmergy" in lock_release("Stigmergy")
        assert "No lock on" in lock_release("Stigmergy")

    def test_lock_missing_node(self, patch_store, agent_env):
        from kindex.mcp_server import lock_acquire, lock_release
        assert "Node not found" in lock_acquire("no-such-node-xyz")
        assert "Node not found" in lock_release("no-such-node-xyz")


class TestMCPTaskClaimDefaults:
    def test_task_claim_defaults_agent(self, patch_store, agent_env):
        from kindex.mcp_server import task_add, task_claim

        store, _ = patch_store
        task_add("Ship the parser")
        task_id = store.all_nodes(node_type="task", limit=10)[0]["id"]

        result = task_claim(task_id)
        assert "mcp-agent" in result
        claim = (store.get_node(task_id).get("extra") or {}).get("claim")
        assert claim["agent"] == "mcp-agent"


class TestMCPEdit:
    def test_edit_editable_by_title(self, patch_store, agent_env):
        from kindex.mcp_server import edit
        store, _ = patch_store
        result = edit("Stigmergy", content="Updated body")
        assert result.startswith("Edited")
        assert "content" in result
        assert store.get_node_by_title("Stigmergy")["content"] == "Updated body"

    def test_edit_additive_refused(self, patch_store, agent_env):
        from kindex.mcp_server import edit
        result = edit("Never break the API contract", content="rewrite history")
        assert result.startswith("Error")
        assert "additive" in result
        assert "supersede" in result

    def test_edit_additive_append_ok(self, patch_store, agent_env):
        from kindex.mcp_server import edit
        store, _ = patch_store
        result = edit("Never break the API contract", append="Reaffirmed for v2")
        assert result.startswith("Edited")
        content = store.get_node_by_title("Never break the API contract")["content"]
        assert "[addendum" in content
        assert "mcp-agent" in content
        assert "Reaffirmed for v2" in content

    def test_edit_managed_refused(self, patch_store, agent_env):
        from kindex.mcp_server import edit
        store, _ = patch_store
        store.add_node("Some task", node_type="task",
                       extra={"task_status": "open"})
        result = edit("Some task", content="nope")
        assert result.startswith("Error")
        assert "managed" in result

    def test_edit_requires_a_field(self, patch_store, agent_env):
        from kindex.mcp_server import edit
        result = edit("Stigmergy")
        assert result.startswith("Error")
        assert "at least one field" in result

    def test_edit_not_found(self, patch_store, agent_env):
        from kindex.mcp_server import edit
        assert "Node not found" in edit("no-such-node-xyz", content="x")

    def test_edit_tags_and_expires(self, patch_store, agent_env):
        from kindex.mcp_server import edit
        store, _ = patch_store
        result = edit("Stigmergy", add_tags="swarm, emergence",
                      remove_tags="biology", expires="2099-01-01")
        assert result.startswith("Edited")
        node = store.get_node_by_title("Stigmergy")
        assert "swarm" in node["domains"]
        assert "emergence" in node["domains"]
        assert "biology" not in node["domains"]
        assert node["extra"]["expires"] == "2099-01-01"


class TestMCPSupersede:
    def test_supersede_creates_replacement(self, patch_store, agent_env):
        from kindex.mcp_server import supersede
        store, _ = patch_store
        result = supersede("Never break the API contract",
                           "All public endpoints stay compatible across majors",
                           reason="tightened wording")
        assert "Superseded" in result
        new_id = result.rsplit(" ", 1)[1]

        old = store.get_node_by_title("Never break the API contract")
        assert old["status"] == "superseded"
        assert old["extra"]["superseded_by"] == new_id

        new = store.get_node(new_id)
        assert new["status"] == "active"
        assert new["type"] == "constraint"
        assert new["extra"]["supersedes"] == old["id"]
        assert new["extra"]["supersede_reason"] == "tightened wording"

    def test_supersede_empty_text_errors(self, patch_store, agent_env):
        from kindex.mcp_server import supersede
        result = supersede("Stigmergy", "   ")
        assert result.startswith("Error")

    def test_supersede_not_found(self, patch_store, agent_env):
        from kindex.mcp_server import supersede
        assert "Node not found" in supersede("no-such-node-xyz", "text")

    def test_supersede_managed_refused(self, patch_store, agent_env):
        from kindex.mcp_server import supersede
        store, _ = patch_store
        tid = store.add_node("A live task", node_type="task",
                             extra={"task_status": "open"})
        result = supersede(tid, "replacement text")
        assert result.startswith("Error")
        assert "managed" in result
        assert store.get_node(tid)["status"] == "active"  # untouched

    def test_double_supersede_names_successor(self, patch_store, agent_env):
        from kindex.mcp_server import supersede
        store, _ = patch_store
        nid = store.add_node("Twice target", node_type="concept")
        first = supersede(nid, "first replacement")
        assert "Superseded" in first
        new_id = first.rsplit(" ", 1)[1]

        second = supersede(nid, "second replacement")
        assert second.startswith("Error")
        assert new_id in second  # error names the successor
        assert store.get_node(nid)["extra"]["superseded_by"] == new_id


class TestMCPToolRegistry:
    def test_tag_update_registered_reinforce_private(self):
        import kindex.mcp_server as mcp_mod
        names = {t.name for t in mcp_mod.mcp._tool_manager.list_tools()}
        assert "tag_update" in names
        assert "_reinforce_on_end" not in names
        assert "reinforce_on_end" not in names
        assert "edit" in names
        assert "supersede" in names

    def test_tag_end_flow_still_reinforces(self, patch_store):
        """_reinforce_on_end stays functional as the private helper inside
        tag_update's end action (never raises, returns '')."""
        from kindex.mcp_server import tag_start, tag_update
        tag_start("registry-regression", focus="check end flow")
        result = tag_update(name="registry-regression", action="end",
                            summary="all done")
        assert result.startswith("Completed: registry-regression")


class TestMCPChangelogDiffs:
    def test_changelog_renders_diffs(self, patch_store, agent_env):
        from kindex.mcp_server import changelog
        store, _ = patch_store
        nid = store.get_node_by_title("Stigmergy")["id"]
        store.edit_node(nid, actor="mcp-agent", content="Changed content here")

        out = changelog(days=1)
        assert "edit_node" in out
        assert "Stigmergy" in out
        assert "content:" in out
        assert "-> Changed content here" in out


# ── Server instructions cover the new tools and doctrine (idx 21) ─────


class TestMCPInstructions:
    def test_instructions_name_every_new_tool(self):
        import kindex.mcp_server as mcp_mod
        text = mcp_mod.mcp.instructions or ""
        for name in ("edit", "supersede", "coord_join", "coord_attach",
                     "coord_inject", "lock_acquire", "lock_release"):
            assert name in text, f"instructions omit `{name}`"

    def test_instructions_teach_edit_dont_readd(self):
        import kindex.mcp_server as mcp_mod
        text = mcp_mod.mcp.instructions or ""
        assert "edit, don't re-add" in text
        # search line steers toward edit/supersede when a node exists
        assert "prefer `edit`/`supersede` over `add`" in text
        # lock semantics: edit refuses foreign locks, TTL expiry never blocks
        assert "refuses foreign" in text
        assert "never blocks" in text


# ── graph_merge policy/lock/extra/pheromone (idx 27) ──────────────────


class TestMCPGraphMergeSelf:
    def test_a_node_cannot_be_merged_into_itself(self, patch_store):
        from kindex.mcp_server import graph_merge

        store, _ = patch_store
        node = store.add_node(title="Solo", content="x", node_id="solo")
        result = graph_merge(node, node)
        assert result.startswith("Error"), result
        assert store.get_node("solo")["status"] == "active"


class TestMCPPrimeHeader:
    def test_the_header_counts_the_graph(self, patch_store):
        from kindex.mcp_server import prime

        store, _ = patch_store
        header = prime("Stigmergy").split("\n\n", 2)[1]
        stats = store.stats()
        assert header == f"Graph: {stats['nodes']} nodes, {stats['edges']} edges"
        assert stats["nodes"] > 0


class TestMCPGraphMergePolicy:
    def test_refuses_managed_types_and_preserves_collab_state(
            self, patch_store, agent_env):
        """Repro: merging two coordination nodes wiped members/messages."""
        from kindex.coordination import create_conversation, get_conversation
        from kindex.mcp_server import graph_merge

        store, _ = patch_store
        a = create_conversation(store, "sprint-14-planning", created_by="agent-a")
        b = create_conversation(store, "sprint-15-planning", created_by="agent-b")

        result = graph_merge(a, b)
        assert result.startswith("Error")
        assert "managed" in result
        for name in ("sprint-14-planning", "sprint-15-planning"):
            conv = get_conversation(store, name)
            assert conv is not None
            assert conv["extra"]["coord_status"] == "active"
            assert "members" in conv["extra"]

    def test_refuses_additive_without_force(self, patch_store, agent_env):
        from kindex.mcp_server import graph_merge
        store, _ = patch_store
        a = store.add_node("Rule alpha one", node_type="decision", content="A")
        b = store.add_node("Rule alpha two", node_type="decision", content="B")

        result = graph_merge(a, b)
        assert result.startswith("Error")
        assert "supersede" in result
        assert store.get_node(a)["status"] == "active"

        forced = graph_merge(a, b, force=True)
        assert "Merged" in forced
        assert store.get_node(a)["status"] == "archived"

    def test_refuses_foreign_lock_without_force(self, patch_store, agent_env):
        from kindex.locks import lock_node
        from kindex.mcp_server import graph_merge

        store, _ = patch_store
        a = store.add_node("Lockmerge source", node_type="concept", content="S")
        b = store.add_node("Lockmerge target", node_type="concept", content="T")
        lock_node(store, a, "alice@elsewhere", ttl_minutes=60)

        result = graph_merge(a, b)
        assert result.startswith("Error")
        assert "alice@elsewhere" in result
        assert store.get_node(a)["status"] == "active"
        assert store.get_node(a)["extra"]["lock"]["agent"] == "alice@elsewhere"

        forced = graph_merge(a, b, force=True)
        assert "Merged" in forced

    def test_merge_preserves_source_extra_and_migrates_pheromone(
            self, patch_store, agent_env):
        from kindex.mcp_server import graph_merge

        store, _ = patch_store
        a = store.add_node("Phero source xx", node_type="concept", content="S",
                           extra={"custom_key": "keepme"})
        b = store.add_node("Phero target xx", node_type="concept", content="T")
        store.deposit_pheromone(a, amount=2.0)

        result = graph_merge(a, b)
        assert "Merged" in result

        extra = store.get_node(a)["extra"]
        assert extra["custom_key"] == "keepme"  # not wholesale-replaced
        assert extra["merged_into"] == b

        rows = store.conn.execute(
            "SELECT node_id FROM injection_pheromone WHERE node_id IN (?, ?)",
            (a, b)).fetchall()
        assert [r["node_id"] for r in rows] == [b]  # trail followed the merge


# ── supersede lock error names a remedy that exists (idx 19) ──────────


class TestMCPSupersedeLockMessage:
    def test_locked_supersede_does_not_advertise_force(self, patch_store, agent_env):
        from kindex.locks import lock_node
        from kindex.mcp_server import supersede

        store, _ = patch_store
        nid = store.add_node("Locked decision xyz", node_type="decision",
                             content="original")
        lock_node(store, nid, "other@host", ttl_minutes=60)

        result = supersede(nid, "replacement text")
        assert result.startswith("Error")
        assert "other@host" in result
        assert "pass force to override" not in result  # surface has no force
        assert "release the lock first" in result
        assert "lock_release" in result

    def test_locked_edit_still_advertises_force(self, patch_store, agent_env):
        """edit DOES expose force on both surfaces — its hint is unchanged."""
        from kindex.locks import lock_node
        from kindex.mcp_server import edit

        store, _ = patch_store
        nid = store.add_node("Locked concept xyz", node_type="concept",
                             content="original")
        lock_node(store, nid, "other@host", ttl_minutes=60)

        result = edit(nid, content="new content")
        assert result.startswith("Error")
        assert "pass force to override" in result


class TestMCPAddTypes:
    def test_add_refuses_a_type_it_does_not_own(self, patch_store):
        from kindex.mcp_server import add

        store, _ = patch_store
        before = store.stats()["stored_nodes"]
        for node_type in ("task", "session", "made-up"):
            result = add(f"Typed as {node_type}", node_type=node_type)
            assert result.startswith("Error"), result
        assert store.stats()["stored_nodes"] == before
        assert not add("Never skip the migration review", node_type="constraint").startswith("Error")


def test_health_outcome_recognises_every_refusal_form():
    from kindex.mcp_server import _health_outcome

    for failed in ("Error: bad", "Node not found: x", "Task not found: t",
                   "Unknown action: y", '{"ok": false, "error": {}}',
                   {"ok": False}, {"error": {"code": "x"}}):
        assert _health_outcome(failed) == "failed", failed
    for fine in ("Added node x", '{"imported": 1}', {"ok": True}, "", None):
        assert _health_outcome(fine) == "success", fine


def test_caller_limits_are_clamped(patch_store, monkeypatch):
    import kindex.mcp_server as mcp_mod

    store, _ = patch_store
    monkeypatch.setattr(mcp_mod, "MAX_TOOL_ROWS", 2)
    for n in range(5):
        store.add_node(title=f"Orphan {n}", content="alone", node_id=f"orphan-{n}")
    listed = mcp_mod.list_nodes(limit=1000)
    assert listed.startswith("2 node(s)"), listed
    orphans = mcp_mod.resource_orphans()
    assert "more (use graph_heal or list_nodes)" in orphans
    assert mcp_mod._bounded(-5) == 1 and mcp_mod._bounded("x") == 2


def test_invalidate_honours_locks_and_names_the_server_actor(patch_store, monkeypatch):
    import json as _json

    import kindex.mcp_server as mcp_mod
    from kindex.locks import lock_node

    store, _ = patch_store
    monkeypatch.setattr(mcp_mod, "_default_agent", lambda agent="": "agent-self")
    node = store.add_node(title="Retry budget", content="three", node_id="budget")
    lock_node(store, node, "agent-other")
    refused = mcp_mod.invalidate(node, invalidated_by="agent-other", disposition_code="stale")
    assert isinstance(refused, str) and refused.startswith("Error"), refused
    assert store.get_node(node).get("invalid_at") in (None, "")

    mcp_mod.invalidate(node, invalidated_by="agent-other", disposition_code="stale", force=True)
    entry = store.activity_since("1970-01-01", action="invalidate_node")[0]
    details = entry["details"] if isinstance(entry["details"], dict) else _json.loads(entry["details"])
    assert entry["actor"] == "agent-self"
    assert details["asserted_by"] == "agent-other"


def test_resources_answer_memory_unavailable_and_status_shows_drift(patch_store, monkeypatch):
    import kindex.mcp_server as mcp_mod

    store, _ = patch_store
    store.set_meta("schema_version", "99")
    assert "Schema drift: the database is v99" in mcp_mod.status()

    def broken():
        raise mcp_mod.MemoryUnavailableError(RuntimeError("locked"))

    monkeypatch.setattr(mcp_mod, "_get_store", broken)
    assert mcp_mod.resource_orphans().startswith("Error: memory unavailable")


def test_search_is_scoped_to_the_client(patch_store, monkeypatch):
    import kindex.mcp_server as mcp_mod

    store, _ = patch_store
    store.add_node(title="Codex-only rollout note", content="rollout steps",
                   node_id="codex-note", tags=["client:codex"])
    monkeypatch.setenv("KIN_CLIENT", "claude")
    assert "Codex-only rollout note" not in mcp_mod.search("rollout note")
    monkeypatch.setenv("KIN_CLIENT", "codex")
    assert "Codex-only rollout note" in mcp_mod.search("rollout note")


def test_the_project_identity_prefers_the_declared_path(monkeypatch, tmp_path):
    import kindex.mcp_server as mcp_mod

    monkeypatch.delenv("KIN_PROJECT_PATH", raising=False)
    monkeypatch.delenv("KIN_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert mcp_mod._mcp_project_path() == str(tmp_path) or mcp_mod._mcp_project_path().endswith(tmp_path.name)
    monkeypatch.setenv("KIN_PROJECT", "/srv/project")
    assert mcp_mod._mcp_project_path() == "/srv/project"
    monkeypatch.setenv("KIN_PROJECT_PATH", "/srv/declared")
    assert mcp_mod._mcp_project_path() == "/srv/declared"
