"""Runaway-merge guards for the dream cycle.

The defect these cover: `merge_nodes` appends source content into the target
with no cap, and `content_overlap` compares only the first 500 chars — where
machine-generated files are identical. Minified symbols, a handler class
defined in twenty files, a vendored LICENSE, and generated Prisma schemas are
all mutually similar by construction, so every one of those merges is a false
positive that grows the target. Unguarded this produced a 35 MB "concept" node
of merged minified Astro output, and five such nodes held 86% of the live
graph's content by bytes.
"""

from __future__ import annotations

import logging

import pytest

from kindex.config import Config
from kindex.dream import (
    MAX_MERGE_ABSORPTIONS,
    MAX_MERGE_RESULT_CHARS,
    MERGE_MARKER,
    MERGE_REFUSAL_COUNTER,
    merge_nodes,
)
from kindex.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(Config(data_dir=str(tmp_path)))
    yield s
    s.close()


def test_ordinary_merge_still_works(store):
    """The guards must not break the case the feature exists for."""
    target = store.add_node("Widget cache", content="The widget cache is warm.")
    source = store.add_node("Widget cache", content="It is invalidated wholesale.")

    assert merge_nodes(store, source, target) is True
    merged = store.get_node(target)
    assert "invalidated wholesale" in merged["content"]
    assert store.get_node(source)["status"] == "archived"
    assert store.get_meta(MERGE_REFUSAL_COUNTER) is None


def test_merge_refused_when_result_would_exceed_size_cap(store):
    target = store.add_node("Bundle", content="x" * (MAX_MERGE_RESULT_CHARS - 10))
    source = store.add_node("Bundle", content="y" * 100)

    assert merge_nodes(store, source, target) is False
    assert store.get_meta(MERGE_REFUSAL_COUNTER) == "1"


def test_refused_merge_leaves_both_nodes_untouched(store):
    """A refusal must not half-apply: no appended content, no archived source.

    The guards run before any mutation precisely so a refusal is a no-op.
    """
    target_content = "x" * (MAX_MERGE_RESULT_CHARS - 10)
    target = store.add_node("Bundle", content=target_content)
    source = store.add_node("Bundle", content="y" * 100)

    merge_nodes(store, source, target)

    assert store.get_node(target)["content"] == target_content
    assert store.get_node(source)["status"] == "active"
    assert store.get_node(source)["content"] == "y" * 100
    # Edges must not have been moved either.
    assert store.edges_from(target) == []


def test_merge_refused_after_absorption_cap(store):
    """A heavily-absorbed target is evidence of a generated-content cluster."""
    absorbed = "\n".join(f"{MERGE_MARKER} thing {i}]\nbody"
                         for i in range(MAX_MERGE_ABSORPTIONS))
    target = store.add_node("Handler", content=absorbed)
    source = store.add_node("Handler", content="one more definition")

    assert merge_nodes(store, source, target) is False
    assert store.get_node(source)["status"] == "active"


def test_absorption_cap_allows_the_last_permitted_merge(store):
    """Off-by-one guard: at cap-1 absorptions the merge must still succeed."""
    absorbed = "\n".join(f"{MERGE_MARKER} thing {i}]\nbody"
                         for i in range(MAX_MERGE_ABSORPTIONS - 1))
    target = store.add_node("Handler", content=absorbed)
    source = store.add_node("Handler", content="one more definition")

    assert merge_nodes(store, source, target) is True


def test_repeated_merges_cannot_grow_a_node_without_bound(store):
    """The end-to-end property: the live 35 MB node could not happen now.

    Simulates the minified-symbol case — many mutually-similar sources merged
    into one target, one after another.
    """
    target = store.add_node("class Ha", content="class Ha (bundle.js:1)\n" + "z" * 5000)
    for i in range(200):
        source = store.add_node(f"class H{i}", content=f"class H{i} (bundle.js:1)\n" + "z" * 5000)
        merge_nodes(store, source, target)

    grown = store.get_node(target)
    assert len(grown["content"]) <= MAX_MERGE_RESULT_CHARS
    # And the condition was reported, not silently tolerated.
    assert int(store.get_meta(MERGE_REFUSAL_COUNTER)) > 0


def test_refusal_is_logged(store, caplog):
    target = store.add_node("Bundle", content="x" * (MAX_MERGE_RESULT_CHARS - 10))
    source = store.add_node("Bundle", content="y" * 100)

    with caplog.at_level(logging.WARNING, logger="kindex.dream"):
        merge_nodes(store, source, target)

    assert "merge refused" in caplog.text


def test_missing_nodes_do_not_count_as_refusals(store):
    """A nonexistent node is a caller error, not a runaway-merge signal.

    Counting it would pollute the metric `kin doctor` reads.
    """
    target = store.add_node("Real", content="body")
    assert merge_nodes(store, "does-not-exist", target) is False
    assert store.get_meta(MERGE_REFUSAL_COUNTER) is None
