"""Independent acceptance oracles for contract AC1--AC6 (digest in README).

No implementation-source inspection, no test execution by the author. The
Validator owns baseline/repair execution. Provider commands are synthetic.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

import pytest

from kindex.config import Config
from kindex.store import Store
from kindex import tasks
from kindex.sessions import start_tag


WORK = (
    "User: Repair the invoice parser without changing settlement behavior. "
    "Assistant: I plan to remove the reconciliation validation to simplify the migration. "
    "Validation evidence: parser_negative_case failed; settlement_roundtrip has not run. "
    "We need to decide whether this implementation still accomplishes the original goal."
)
GOAL = "GOAL_CANARY: preserve invoice reconciliation while repairing the parser"
TASK = "TASK_CANARY: execute settlement_roundtrip before declaring completion"
CONSTRAINT = "CONSTRAINT_CANARY: preserve all invoice rows during the migration"
NOTE = "ACCEPTANCE_NOTE: reconsider removing reconciliation before settlement_roundtrip passes"
STATES = {"disabled", "unavailable", "budget_exhausted", "queued", "reviewing",
          "reviewed_quiet", "delivered", "failed", "skipped"}


@pytest.fixture
def sandbox(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True, capture_output=True)
    home = tmp_path / "isolated-home"
    config = home / ".config" / "kindex" / "kin.yaml"
    config.parent.mkdir(parents=True)
    # Model an installation without the optional Signet owner. Do not discover
    # the workstation's executable with this fixture's unrelated fresh HOME.
    # Real owner detection is unchanged; no policy callback is bypassed.
    tool_bin = tmp_path / "isolated-bin"
    tool_bin.mkdir()
    for name in ("git", "kin", "sh", "bash", "env"):
        executable = shutil.which(name)
        if executable:
            (tool_bin / name).symlink_to(executable)
    for name in ("python", "python3", f"python{sys.version_info.major}.{sys.version_info.minor}"):
        (tool_bin / name).symlink_to(sys.executable)
    # Fresh processes import config with an isolated user home. Never pass live
    # credentials or agent/profile routing from the author's environment.
    env = {key: os.environ[key] for key in
           ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "LANG", "LC_ALL")
           if key in os.environ}
    env.update(PATH=str(tool_bin), HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"),
               KIN_PROJECT=str(project), GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=str(home / "empty-gitconfig"))
    return Harness(tmp_path, project, home, config, env)


class Harness:
    def __init__(self, root, project, home, config, env):
        self.root, self.project, self.home, self.config, self.env = root, project, home, config, env
        self.data = project / ".kin" / "local"
        self.log = root / "provider-requests.jsonl"
        self.release = root / "provider-release"
        self.provider = root / "provider.py"
        self.provider.write_text(
            "import json, pathlib, sys, time\n"
            "log, release, mode = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]\n"
            "prompt = sys.stdin.read()\n"
            "with log.open('a') as f: f.write(json.dumps({'prompt':prompt,'mode':mode})+'\\n')\n"
            "if mode.startswith('blocked'):\n"
            "    deadline=time.monotonic()+15\n"
            "    while not release.exists():\n"
            "        if time.monotonic()>deadline: raise SystemExit(91)\n"
            "        time.sleep(.025)\n"
            "if mode == 'failure':\n"
            "    print('synthetic provider refused review', file=sys.stderr)\n"
            "    raise SystemExit(23)\n"
            "if mode == 'malformed':\n"
            "    print('provider returned invalid structured output')\n"
            "    raise SystemExit(0)\n"
            f"note={NOTE!r}\n"
            "if 'SESSION_ALPHA_ONLY' in prompt: note='ALPHA_PRIVATE_REVIEW'\n"
            "elif 'SESSION_BETA_ONLY' in prompt: note='BETA_PRIVATE_REVIEW'\n"
            "quiet=mode.endswith('quiet')\n"
            "print(json.dumps({'rating':0.1 if quiet else .95, 'note':'' if quiet else note,\n"
            " 'basis':'Observed original intent, recent migration, and missing validation',\n"
            " 'stakes':'low','dimension':'alignment','escalate':False}))\n"
        )

    def configure(self, *, mode="quiet", enabled=True, budget=1, command=True, config_path=None):
        path = config_path or self.config
        cmd = shlex.join([sys.executable, str(self.provider), str(self.log), str(self.release), mode]) if command else ""
        path.write_text(
            f"data_dir: {json.dumps(str(self.data))}\n"
            "llm:\n  enabled: true\n"
            f"budget:\n  daily: {budget}\n  weekly: {budget * 5}\n  monthly: {budget * 10}\n"
            f"sim:\n  enabled: {str(enabled).lower()}\n  tick_interval: 1\n"
            "  threshold: 0.7\n  max_stale_ticks: 30\n  min_overlap: 0.18\n"
            "  triage_banter: false\n  grounding_chars: 6000\n"
            f"  command: {json.dumps(cmd)}\n"
            "  advocate:\n    enabled: false\n"
        )
        return path

    def seed(self, data=None):
        store = Store(Config(data_dir=str(data or self.data)))
        try:
            node = store.add_node("invoicecanary legacy knowledge", content="invoicecanary preserved reconciliation", node_type="concept")
            task = tasks.create_task(store, TASK, priority=5, project_path=str(self.project))
            store.add_node(CONSTRAINT, node_type="constraint", extra={"trigger":"migration", "action":"warn"})
            start_tag(store, "invoice-repair", focus=GOAL, project_path=str(self.project))
            return node, task
        finally:
            store.close()

    def cli(self, *args, payload=None):
        return subprocess.run([sys.executable, "-m", "kindex.cli", *args],
                              input=json.dumps(payload) if payload is not None else None,
                              text=True, capture_output=True, cwd=self.project,
                              env=self.env, timeout=25)

    def hook(self, adapter="codex", session="acceptance-session", prompt=WORK, config=None, rpc=False):
        if rpc:
            result = self.cli("hook-rpc", payload={"protocol_version":1, "action":"supervisor",
                "scope":{"project_path":str(self.project),"session_id":session,"agent":adapter}, "text":prompt})
        else:
            result = self.cli("supervisor-hook", "--adapter", adapter, "--json", "--config", str(config or self.config),
                "--project-path", str(self.project), payload={"session_id":session,"cwd":str(self.project),"prompt":prompt})
        assert result.returncode == 0, result.stderr
        parsed = json.loads(result.stdout)
        assert parsed["ok"] is True, parsed
        assert isinstance(parsed["context"], str), parsed
        assert parsed["supervisor"]["state"] in STATES, parsed
        return parsed

    def requests(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def until(self, operation, predicate, description):
        # Bounded observation of an actual process/event; not a sleep-based oracle.
        deadline = time.monotonic() + 15
        last = None
        while time.monotonic() < deadline:
            last = operation()
            if predicate(last):
                return last
            time.sleep(.05)
        pytest.fail(f"No {description} within synthetic worker deadline; last={last!r}")

    def complete(self, **kw):
        return self.until(lambda:self.hook(**kw), lambda r:r["supervisor"]["state"] in
                          {"reviewed_quiet","delivered","failed","budget_exhausted","unavailable"},
                          "terminal supervisor disposition")


@pytest.mark.parametrize("adapter", ["claude","codex","opencode","antigravity"])
def test_all_four_adapters_deliver_actual_review(sandbox, adapter):
    """AC1/AC3 red-now: a retrieval-only or disconnected adapter cannot pass."""
    sandbox.configure(mode="flagged")
    sandbox.seed()
    answer = sandbox.complete(adapter=adapter)
    assert answer["supervisor"]["state"] == "delivered"
    assert NOTE in answer["context"]
    assert sandbox.requests(), "A hook invocation cannot stand in for a provider review"


def test_modern_rpc_delivers_review_with_protocol_envelope(sandbox):
    """AC1/AC7 red-now: modern RPC reaches review rather than retrieval alone."""
    sandbox.configure(mode="flagged")
    sandbox.seed()
    result = sandbox.complete(adapter="claude", rpc=True)
    assert result["supervisor"]["state"] == "delivered"
    assert NOTE in result["context"]
    assert len(sandbox.requests()) >= 1


@pytest.mark.parametrize("mode, expected", [("quiet","reviewed_quiet"), ("failure","failed"), ("malformed","failed")])
def test_completed_quiet_and_provider_failures_are_distinct(sandbox, mode, expected):
    """AC4 red-now: invalid output or a failed process must never count as quiet."""
    sandbox.configure(mode=mode)
    result = sandbox.complete()
    assert result["supervisor"]["state"] == expected
    assert NOTE not in result["context"]
    assert len(sandbox.requests()) >= 1


@pytest.mark.parametrize("setup, expected", [("disabled","disabled"), ("credentials","unavailable"), ("budget","budget_exhausted")])
def test_preflight_suppression_states_do_not_spend(sandbox, setup, expected):
    """AC4/AC6 red-now: suppression has its own diagnosis and no model effect."""
    sandbox.configure(enabled=setup!="disabled", budget=0 if setup=="budget" else 1, command=setup!="credentials")
    result = sandbox.hook()
    assert result["supervisor"]["state"] == expected
    assert NOTE not in result["context"]
    assert sandbox.requests() == []


def test_pending_duplicate_hooks_have_single_provider_effect(sandbox):
    """AC4/AC5 red-now: pending is observable and duplicate events spend once."""
    sandbox.configure(mode="blocked-quiet")
    first = sandbox.hook()
    assert first["supervisor"]["state"] in {"queued", "reviewing"}
    sandbox.until(sandbox.requests, bool, "provider start handshake")
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _:sandbox.hook(), range(3)))
        assert all(r["supervisor"]["state"] in {"queued","reviewing","skipped"} for r in results)
        assert len(sandbox.requests()) == 1
    finally:
        sandbox.release.touch()
    terminal = sandbox.complete()
    assert terminal["supervisor"]["state"] == "reviewed_quiet"
    assert len(sandbox.requests()) == 1


def test_brief_contains_goal_tasks_constraints_work_and_diligence(sandbox):
    """AC3 red-now: captured model request must carry the actual fixture facts."""
    sandbox.configure(mode="quiet")
    sandbox.seed()
    # Bind the original goal to this host session through actual user input;
    # a project-wide active tag alone cannot authorize a concurrent session.
    sandbox.complete(prompt=f"User: {GOAL}\n{WORK}")
    request = sandbox.requests()[0]["prompt"]
    for fact in (GOAL, TASK, CONSTRAINT, "parser_negative_case failed", "settlement_roundtrip has not run"):
        assert fact in request, f"Missing grounded evidence: {fact}"
    for concepts in (("direction",), ("adherence","alignment"), ("consequence","trajectory"), ("validation","diligence","tested")):
        assert any(term in request.lower() for term in concepts), f"Missing review dimension: {concepts}"


def test_async_worker_retains_explicit_config_and_project_store(sandbox):
    """AC5 red-now: background config reloading cannot select the global decoy."""
    sandbox.configure(mode="failure", enabled=False)
    explicit = sandbox.configure(mode="flagged", config_path=sandbox.root / "approved-user-config.yaml")
    sandbox.seed()
    result = sandbox.complete(config=explicit)
    assert result["supervisor"]["state"] == "delivered"
    assert NOTE in result["context"]
    assert all(r["mode"] == "flagged" for r in sandbox.requests())
    assert not (sandbox.home / ".kindex" / "kindex.db").exists()
    assert not (sandbox.data / "kindex" / "kindex.db").exists()


def test_sessions_do_not_consume_each_others_feedback(sandbox):
    """AC5 red-now: each session receives only its own outstanding review."""
    sandbox.configure(mode="flagged")
    alpha = WORK + " SESSION_ALPHA_ONLY"
    beta = WORK + " SESSION_BETA_ONLY"
    sandbox.hook(session="alpha", prompt=alpha)
    b = sandbox.complete(session="beta", prompt=beta)
    assert "ALPHA_PRIVATE_REVIEW" not in b["context"]
    assert "BETA_PRIVATE_REVIEW" in b["context"]
    a = sandbox.complete(session="alpha", prompt=alpha)
    assert "ALPHA_PRIVATE_REVIEW" in a["context"]
    assert "BETA_PRIVATE_REVIEW" not in a["context"]


def test_runtime_kill_switch_is_honored_by_supervisor(sandbox):
    """AC6 red-now transport guard: explicit store off defeats enabled config."""
    sandbox.configure(mode="flagged")
    from kindex.sim import set_sim_enabled
    store = Store(Config(data_dir=str(sandbox.data)))
    try:
        set_sim_enabled(store, False)
    finally:
        store.close()
    result = sandbox.hook()
    assert result["supervisor"]["state"] == "disabled"
    assert sandbox.requests() == []


def test_legacy_populated_store_shared_by_rpc_and_mcp(sandbox):
    """AC2 red-now: verify task/concept visibility, not merely path equality."""
    sandbox.configure(enabled=False)
    node, task = sandbox.seed()
    store = Store(Config(data_dir=str(sandbox.data)))
    try:
        legacy_unscoped = tasks.create_task(store, "LEGACY_UNSCOPED_TASK_CANARY")
        foreign = tasks.create_task(store, "FOREIGN_PROJECT_TASK_CANARY", project_path=str(sandbox.root / "other-repo"))
    finally:
        store.close()
    scope = {"project_path":str(sandbox.project),"session_id":"legacy-visibility","agent":"claude"}
    result = sandbox.cli("hook-rpc", payload={"protocol_version":1,"scope":scope,
                        "action":"task","operation":"list","args":{}})
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response["ok"], response
    assert task in {t["id"] for t in response["tasks"]}
    assert legacy_unscoped in {t["id"] for t in response["tasks"]}
    assert foreign not in {t["id"] for t in response["tasks"]}
    probe = subprocess.run([sys.executable,"-c",
        "import json; from kindex import mcp_server as m; "
        "print(json.dumps({'search':m.search('invoicecanary'),'tasks':m.task_list()}))"],
        cwd=sandbox.project,env=sandbox.env,capture_output=True,text=True,timeout=25)
    assert probe.returncode == 0, probe.stderr
    mcp = json.loads(probe.stdout)
    assert "invoicecanary legacy knowledge" in mcp["search"]
    assert TASK in mcp["tasks"]
    assert "LEGACY_UNSCOPED_TASK_CANARY" in mcp["tasks"]
    assert "FOREIGN_PROJECT_TASK_CANARY" not in mcp["tasks"]
    assert not (sandbox.data / "kindex" / "kindex.db").exists()


@pytest.mark.parametrize("populated", ["legacy", "modern"])
def test_empty_other_store_does_not_hide_populated_tasks(sandbox, populated):
    """AC2/section 9 red-now: schema-only DB is not a conflicting authority."""
    sandbox.configure(enabled=False)
    modern = sandbox.data / "kindex"
    chosen, empty = (sandbox.data, modern) if populated == "legacy" else (modern, sandbox.data)
    _, task = sandbox.seed(data=chosen)
    store = Store(Config(data_dir=str(empty)))
    try:
        store.conn  # create only the schema; no operational or knowledge rows
    finally:
        store.close()
    result = sandbox.cli("hook-rpc", payload={"protocol_version":1,"action":"task","operation":"list","args":{},
        "scope":{"project_path":str(sandbox.project),"session_id":"truth-table","agent":"claude"}})
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["ok"] is True, parsed
    assert task in {t["id"] for t in parsed["tasks"]}


def test_conflicting_populated_stores_refuse_and_preserve_both(sandbox):
    """AC2 red-now: ambiguous selection cannot pick/merge/delete either graph."""
    sandbox.configure(enabled=False)
    sandbox.seed()
    alternate = sandbox.data / "kindex"
    sandbox.seed(data=alternate)
    paths = [sandbox.data / "kindex.db", alternate / "kindex.db"]
    snapshots = {p:hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    reply = sandbox.cli("hook-rpc", payload={"protocol_version":1,"action":"context","query":"invoicecanary",
        "scope":{"project_path":str(sandbox.project),"session_id":"conflict","agent":"claude"}})
    parsed = json.loads(reply.stdout)
    assert parsed["ok"] is False, parsed
    diagnostic = json.dumps(parsed).lower()
    assert "conflict" in diagnostic or "ambiguous" in diagnostic
    for path, digest in snapshots.items():
        assert path.exists()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_explicit_personal_profile_remains_separate(sandbox):
    """AC2 green-now: deliberate personal profile remains a separate graph."""
    sandbox.configure(enabled=False)
    sandbox.seed()
    personal_data = sandbox.root / "personal-data"
    personal = Store(Config(data_dir=str(personal_data)))
    try:
        personal.add_node("PERSONAL_ONLY_CANARY", content="personalcanary")
    finally:
        personal.close()
    with sandbox.config.open("a") as file:
        file.write(f"profiles:\n  personal:\n    data_dir: {json.dumps(str(personal_data))}\n")
    result = sandbox.cli("search", "personalcanary", "--profile", "personal", "--config", str(sandbox.config), "--json")
    assert result.returncode == 0, result.stderr
    assert "PERSONAL_ONLY_CANARY" in result.stdout
    assert "invoicecanary legacy knowledge" not in result.stdout
