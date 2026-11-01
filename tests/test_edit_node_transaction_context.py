"""Transaction ownership and access-time behavior at the public edit boundary."""

import sqlite3
from contextlib import closing

import pytest

from kindex.config import Config
from kindex.store import Store


def test_rejected_nested_edit_preserves_callers_pending_transaction(tmp_path):
    with closing(Store(Config(data_dir=str(tmp_path)))) as editor:
        target = editor.add_node("Target", content="original", queue_embedding=False)
        other = editor.add_node("Other", content="before", queue_embedding=False)
        reader = sqlite3.connect(editor.db_path)
        try:
            editor.conn.execute(
                "UPDATE nodes SET content = 'pending' WHERE id = ?", (other,)
            )
            with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
                editor.edit_node(target, append="must not apply")
            assert editor.conn.in_transaction
            assert (
                editor.conn.execute(
                    "SELECT content FROM nodes WHERE id = ?", (other,)
                ).fetchone()[0]
                == "pending"
            )
            assert (
                reader.execute(
                    "SELECT content FROM nodes WHERE id = ?", (other,)
                ).fetchone()[0]
                == "before"
            )
            editor.conn.commit()
            assert (
                reader.execute(
                    "SELECT content FROM nodes WHERE id = ?", (other,)
                ).fetchone()[0]
                == "pending"
            )
            assert (
                reader.execute(
                    "SELECT content FROM nodes WHERE id = ?", (target,)
                ).fetchone()[0]
                == "original"
            )
        finally:
            reader.close()


@pytest.mark.parametrize("title", ["Renamed", "Target"])
def test_direct_edit_refreshes_stale_access_within_its_transaction(tmp_path, title):
    with closing(Store(Config(data_dir=str(tmp_path)))) as editor:
        node_id = editor.add_node("Target", content="original", queue_embedding=False)
        editor.conn.execute(
            "UPDATE nodes SET last_accessed = ? WHERE id = ?",
            ("2000-01-01T00:00:00", node_id),
        )
        editor.conn.commit()
        before = editor.peek_node(node_id)
        returned = editor.edit_node(node_id, title=title)
        with sqlite3.connect(editor.db_path) as reader:
            accessed = reader.execute(
                "SELECT last_accessed FROM nodes WHERE id = ?", (node_id,)
            ).fetchone()[0]
            audits = reader.execute(
                "SELECT COUNT(*) FROM activity_log WHERE target_id = ? AND action = 'edit_node'",
                (node_id,),
            ).fetchone()[0]
        assert accessed > before["last_accessed"]
        assert returned["last_accessed"] == accessed
        assert audits == (1 if title != "Target" else 0)
        if title == "Target":
            assert returned["updated_at"] == before["updated_at"]


def test_failed_edit_does_not_commit_access_refresh(tmp_path):
    with closing(Store(Config(data_dir=str(tmp_path)))) as editor:
        node_id = editor.add_node("Target", content="original", queue_embedding=False)
        editor.conn.execute(
            "UPDATE nodes SET last_accessed = ? WHERE id = ?",
            ("2000-01-01T00:00:00", node_id),
        )
        editor.conn.execute(
            "CREATE TRIGGER reject_edit_audit BEFORE INSERT ON activity_log WHEN NEW.action = 'edit_node' BEGIN SELECT RAISE(ABORT, 'audit rejected'); END"
        )
        editor.conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="audit rejected"):
            editor.edit_node(node_id, title="Must roll back")
        assert not editor.conn.in_transaction
        with sqlite3.connect(editor.db_path) as reader:
            assert reader.execute(
                "SELECT title, last_accessed FROM nodes WHERE id = ?", (node_id,)
            ).fetchone() == ("Target", "2000-01-01T00:00:00")
