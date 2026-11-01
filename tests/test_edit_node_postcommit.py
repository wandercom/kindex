"""A committed append must not be reported as failed by return-value bookkeeping."""

import sqlite3

from kindex.config import Config
from kindex.store import Store


def test_append_returns_after_competing_writer_takes_postcommit_lock(tmp_path):
    editor = Store(Config(data_dir=str(tmp_path)), sqlite_timeout=0.05)
    rival = sqlite3.connect(editor.db_path, timeout=0.05, isolation_level=None)
    reader = sqlite3.connect(editor.db_path, timeout=0.05, isolation_level=None)
    observed = {"audit": False, "fired": False, "error": None}
    marker = "append survives post-commit contention"
    try:
        node_id = editor.add_node(
            "Post-commit append", content="seed", node_type="concept",
            queue_embedding=False,
        )
        editor.conn.execute(
            "UPDATE nodes SET last_accessed = ? WHERE id = ?",
            ("2000-01-01T00:00:00", node_id),
        )
        editor.conn.commit()

        def lock_after_commit(statement):
            sql = " ".join(statement.upper().split())
            if sql.startswith("--") or observed["fired"]:
                return
            if not observed["audit"]:
                observed["audit"] = (
                    "INSERT INTO ACTIVITY_LOG" in sql and "EDIT_NODE" in sql
                )
                return
            # Trace callbacks precede execution. The first statement after
            # the edit's COMMIT sees no open transaction on the editing handle.
            if not editor.conn.in_transaction:
                observed["fired"] = True
                try:
                    rival.execute("BEGIN IMMEDIATE")
                except sqlite3.Error as exc:
                    # SQLite swallows callback exceptions: assert reachability below.
                    observed["error"] = str(exc)

        editor.conn.set_trace_callback(lock_after_commit)
        result = editor.edit_node(node_id, append=marker)
        editor.conn.set_trace_callback(None)

        assert observed == {"audit": True, "fired": True, "error": None}
        assert rival.in_transaction, "the competing lock must span the return"
        assert not editor.conn.in_transaction
        content = reader.execute(
            "SELECT content FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()[0]
        assert content.count(marker) == 1
        assert result["content"] == content
        assert reader.execute(
            "SELECT COUNT(*) FROM activity_log WHERE target_id = ? "
            "AND action = 'edit_node'", (node_id,),
        ).fetchone()[0] == 1
    finally:
        editor.conn.set_trace_callback(None)
        rival.close()
        reader.close()
        editor.close()
