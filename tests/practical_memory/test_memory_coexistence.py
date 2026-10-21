"""Independent coexistence contract for both additive session memory registers."""
import copy

from . import test_memory_contract as core
from .native_fixture import make_store


class MemoryCoexistence(core.PracticalMemoryContract):
    def test_retain_persist_and_ack_preserve_both_registers_on_one_native_session(self):
        from kindex import evidence
        from kindex.sessions import start_tag

        core.Path(self.project).mkdir(parents=True, exist_ok=True)
        session = start_tag(self.store, "combined-memory-session", focus="Preserve both memory formats",
                            project_path=self.project)
        self.scope["session_id"] = session
        native_session_extra = copy.deepcopy(self.store.peek_node(session)["extra"])
        source_before = copy.deepcopy(self.store.peek_node(self.node))

        def retain(node, operation):
            snapshot = evidence.source_snapshot(self.store, node, project_path=self.project)
            return evidence.retain_evidence(self.store, session_id=session, project_path=self.project,
                source_ref=snapshot["source_ref"], expected_source_sha256=snapshot["source_sha256"],
                expected_metadata_sha256=snapshot["metadata_sha256"], start_byte=0,
                end_byte=snapshot["source_byte_length"], operation_id=operation)

        retained_first = retain(self.node, "coexist-retain-one")
        retained_register = copy.deepcopy(self.store.peek_node(session)["extra"]["retained_evidence_v2"])
        persist_request = self.persist_request(operation_id="coexist-persist-one")
        persisted = core.api.persist(self.store, persist_request)
        extra = self.store.peek_node(session)["extra"]
        self.assertEqual(extra["retained_evidence_v2"], retained_register)
        practical_register = copy.deepcopy(extra["practical_memory_v1"])

        second = self.add_source("second-coexist-source", "Quartz also keeps exact source evidence across sessions.")
        retained_second = retain(second, "coexist-retain-two")
        extra = self.store.peek_node(session)["extra"]
        self.assertEqual(extra["practical_memory_v1"], practical_register)
        self.assertNotEqual(extra["retained_evidence_v2"], retained_register)
        retained_register = copy.deepcopy(extra["retained_evidence_v2"])

        prepared = persist_request["prepared"]
        ack_request = {"scope": copy.deepcopy(self.scope), "evaluation_time": core.NOW,
            "operation_id": "coexist-ack-one", "projection_sha256": prepared["projection_sha256"],
            "host_receipt": "synthetic-local-host-observation", "payload_sha256": prepared["text_sha256"]}
        acknowledged = core.api.ack(self.store, ack_request)
        extra = self.store.peek_node(session)["extra"]
        self.assertEqual(extra["retained_evidence_v2"], retained_register)
        self.assertNotEqual(extra["practical_memory_v1"], practical_register)
        for key, value in native_session_extra.items():
            self.assertEqual(extra[key], value, "Memory writes clobbered unrelated native session metadata")
        saved_extra = copy.deepcopy(extra)

        self.store.close()
        self.store = make_store(self.run_root, name="fixture")
        self.assertEqual(self.store.peek_node(session)["extra"], saved_extra)
        self.assertEqual(self.store.peek_node(self.node), source_before)
        before_replay = self.snapshot()
        self.assertEqual(retain(self.node, "coexist-retain-one")["receipt"], retained_first["receipt"])
        self.assertEqual(retain(second, "coexist-retain-two")["receipt"], retained_second["receipt"])
        self.assertEqual(core.api.persist(self.store, persist_request), persisted)
        self.assertEqual(core.api.ack(self.store, ack_request), acknowledged)
        self.assertEqual(self.snapshot(), before_replay, "Exact operation replays rewrote shared session state")
        inspected = evidence.inspect_evidence(self.store, session_id=session, project_path=self.project,
                                              evaluation_time=core.NOW)
        self.assertEqual(len(inspected["entries"]), 2)


# Reuse the fixture helpers without rerunning its inherited contract methods.
for _name in core.PracticalMemoryContract.__dict__:
    if _name.startswith("test_") and _name not in MemoryCoexistence.__dict__:
        setattr(MemoryCoexistence, _name, None)
