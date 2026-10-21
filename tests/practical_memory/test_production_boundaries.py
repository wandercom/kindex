"""Production failures absent from the research campaign's small graph fixtures."""
import copy
import sqlite3
from unittest import mock

from . import reference_support as s


class ProductionBoundaries(s.Fixture):
    def test_unopened_writer_does_not_recreate_database_removed_after_validation(self):
        """Interpose at sqlite's public open seam, after existence validation."""
        assertion = self.assertion()
        path = self.store.db_path
        before = self.database()
        self.store.close()
        selected = s.Store(self.config.model_copy(deep=True), migrate=False, manage_project_storage=False)
        moved = path.with_name(path.name + ".moved-original")
        recreated = path.with_name(path.name + ".unexpected-created")
        connect = sqlite3.connect
        raced = False

        def race(database, *args, **kwargs):
            nonlocal raced
            # The qualification read happens first; move only at writable open.
            if not raced and "mode=ro" not in str(database):
                path.rename(moved)
                raced = True
            return connect(database, *args, **kwargs)

        try:
            with mock.patch.object(sqlite3, "connect", side_effect=race):
                with self.assertRaises(s.api.ReferenceError) as raised:
                    s.api.record_correction(selected, assertion, context=self.writer)
                self.assertEqual(raised.exception.code, "unavailable")
            self.assertTrue(raced, "Fixture did not reach the writable-open race")
            self.assertFalse(path.exists(), "Registry writer recreated the missing database")
        finally:
            selected.close()
            # Preserve every synthetic database, even the erroneous empty file.
            # Restore the original so ordinary teardown cannot initialize one.
            if moved.exists():
                if path.exists():
                    path.rename(recreated)
                moved.rename(path)
            self.store = s.Store(self.config, manage_project_storage=False)
        self.assertEqual(self.database(), before)

    def test_incoming_budget_is_global_and_fails_closed_on_overflow(self):
        """Two individually small stars must not evade the request-wide limit."""
        seeds = [self.old, self.new]
        for seed in seeds:
            self.store.conn.execute("UPDATE nodes SET prov_when=? WHERE id=?", ("2026-10-01T00:00:00Z", seed))
        candidates = []
        for index in range(1100):
            key = self.add("incoming-%04d" % index, "Synthetic later note.")
            candidates.append(key)
            self.store.conn.execute("UPDATE nodes SET prov_when=? WHERE id=?", ("2026-10-19T00:00:00Z", key))
            self.store.add_edge(key, seeds[index % 2], edge_type="relates_to", bidirectional=False)
        self.store.conn.commit()
        request = self.expand_request(refs=[self.ref(key) for key in seeds], mode="direct", optional_limit=1, optional_budget_bytes=32768)
        before = self.database()
        reader = self.reader()
        try:
            legacy = s.api.expand_references(reader, request, context=self.context)
            self.assertEqual(legacy["optional_text"], "", "Incoming links are off by default")
            with self.assertRaises(s.api.ReferenceError) as raised:
                s.api.expand_references(reader, request, context=self.context, incoming_later=True)
            self.assertEqual(raised.exception.code, "unavailable")
            self.assertFalse(reader.conn.in_transaction)
        finally:
            reader.close()
        self.assertEqual(self.database(), before)
