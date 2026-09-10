# Doctor FTS integrity (issue #21)

`nodes_fts` is an external-content FTS5 table backed by `nodes`. A plain
`COUNT(*) FROM nodes_fts` counts the backing rows, not the indexed postings.
Since v12, `Store.stats()["nodes"]` means semantic nodes and excludes sessions;
comparing those counts both falsely flags healthy session history and misses
real index corruption in semantic-only graphs.

Doctor uses FTS5 `integrity-check` with `rank=1` to compare actual postings with
all backing text. The index population remains all stored nodes, including
sessions and retired nodes. Search still excludes archived/superseded results
by default and restores them with `include_archived`.

The check's INSERT syntax opens a write transaction, so its savepoint is rolled
back and released without committing a caller's pending work. SQLite interrupts
and some I/O errors can roll back the entire transaction themselves; cleanup
must not mask the original error by referring to a discarded savepoint.
A rebuild is checked again within the repair transaction before commit and
before reporting `FIXED`. The repair invocation retains doctor's existing
issue-history convention; the subsequent doctor invocation reports healthy.

Regression coverage lives in `tests/test_doctor_fts.py` and
`tests/test_doctor_fts_transactions.py`: mixed v12 populations, missing/extra
postings, equal-count swaps, stale text, full indexed fields and long content,
trigger maintenance, transaction/lock cleanup, interruption, and repair rollback.
The check scans the full index and canonical text and briefly requires a writer
lock. It does not validate trigger definitions or change schema/search policy.
