"""Run-v8 acceptance oracles for the process-local config-root seam.

Authority: product specification ``58916cd0...`` R1.1--R1.5 and architecture
``de100e45...`` Amendment 1.  ``red_now`` tests assert behavior absent from the
0.30.1 base.  The unmarked R1.3 test protects byte-identical unbound behavior.

The module is imported before every binding is made.  That is intentional: the
defect is specifically a cache resolved at import time.  All paths used by the
tests are below ``tmp_path``; no process-start environment override is used as
the seam.
"""
from __future__ import annotations

import builtins
import io
import os
import re
import subprocess
from pathlib import Path

import pytest

import kindex.config as config_mod
from kindex.store import Store


@pytest.fixture(autouse=True)
def _release_config_binding_after_each_test():
    """Guarantee a failed oracle cannot poison the next process-local test."""
    yield
    release = getattr(config_mod, "unbind_root", None)
    if callable(release):
        release()


def _seam():
    """Return the four Amendment-1 callables, failing with the API contract."""
    names = ("bind_root", "unbind_root", "active_root", "bound_root")
    missing = [name for name in names if not callable(getattr(config_mod, name, None))]
    assert not missing, (
        "R1 config seam is missing the Amendment-1 API: " + ", ".join(missing)
    )
    return tuple(getattr(config_mod, name) for name in names)


def _resolved(path: str | os.PathLike) -> Path:
    return Path(path).expanduser().resolve()


def _assert_under(path: str | os.PathLike, root: Path) -> None:
    actual = _resolved(path)
    expected_root = root.resolve()
    assert actual.is_relative_to(expected_root), (
        f"resolved path escaped active root: {actual} is not beneath {expected_root}"
    )


def _import_mcp():
    try:
        import kindex.mcp_server as mcp_mod
    except (ImportError, SystemExit) as exc:
        pytest.skip(f"authoritative MCP extra is unavailable: {exc}")
    return mcp_mod


def _clear_kin_env(monkeypatch) -> None:
    for key in list(os.environ):
        if key.startswith("KIN_"):
            monkeypatch.delenv(key, raising=False)


@pytest.mark.red_now
def test_r1_1_post_import_binding_resolves_every_derived_path_under_root(tmp_path):
    """R1.1: a post-import bind controls data and derived path resolution.

    Reversion: removing the seam, consulting the old cache before the binding,
    or letting a derived log path retain its unbound base turns this red.
    Reachability/non-degeneracy: ``kindex.config`` was imported at collection,
    the root does not exist before the call, and ``load_config`` is invoked only
    after binding; both ``data_path`` and an independently derived scheduler
    path must resolve beneath it.  The assertions distinguish a cosmetic
    ``active_root`` variable from a seam actually consulted by resolution.
    Base evidence: 0.30.1 fails on the missing Amendment-1 callables, exactly
    the post-import injectable-seam defect named by R1.1.
    """
    bind_root, unbind_root, active_root, _ = _seam()
    root = tmp_path / "bound-root"
    assert not root.exists()

    bind_root(root)
    try:
        assert active_root() == root.resolve()
        cfg = config_mod.load_config()
        _assert_under(cfg.data_dir, root)
        _assert_under(cfg.data_path, root)
        _assert_under(cfg.scheduler_log_path, root)
    finally:
        unbind_root()


@pytest.mark.red_now
def test_r1_2_mcp_store_accessor_honors_binding_made_after_import(
        tmp_path, monkeypatch):
    """R1.2: the MCP accessor opens its Store below a later binding.

    Reversion: restoring the MCP module's cached config/store path makes the DB
    path point at the unbound HOME and turns this red.  Reachability/non-
    degeneracy: MCP is imported under a controlled temporary HOME *before* the
    bind, both module singletons are cleared, ``_get_store`` constructs a real
    Store, and ``search`` exercises that Store.  Inspecting the resulting
    ``db_path`` proves the accessor ran; this cannot pass through a no-op call.
    Base evidence: 0.30.1 fails because the declared seam API is absent, the
    exact isolation defect behind the R1.2 descope.
    """
    _clear_kin_env(monkeypatch)
    home = tmp_path / "controlled-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    mcp_mod = _import_mcp()  # deliberately before bind_root
    bind_root, unbind_root, _, _ = _seam()
    root = tmp_path / "mcp-bound"
    opened = None

    bind_root(root)
    try:
        monkeypatch.setattr(mcp_mod, "_store", None)
        monkeypatch.setattr(mcp_mod, "_config", None)
        opened, opened_config = mcp_mod._get_store()
        _assert_under(opened.db_path, root)
        _assert_under(opened_config.data_path, root)
        out = mcp_mod.search("v8-mcp-binding-no-match")
        assert isinstance(out, str)
        assert mcp_mod._store is opened
        _assert_under(mcp_mod._store.db_path, root)
    finally:
        if opened is not None:
            opened.close()
        monkeypatch.setattr(mcp_mod, "_store", None)
        monkeypatch.setattr(mcp_mod, "_config", None)
        unbind_root()


def test_r1_3_unbound_resolution_precedence_and_defaults_are_unchanged(
        tmp_path, monkeypatch):
    """R1.3: unused seam leaves layering, precedence, and defaults intact.

    Reversion: inserting the seam as a new unbound precedence tier, changing
    project-over-global order, or changing legacy defaults turns this green-now
    guard red.  Reachability/non-degeneracy: global and project files contain
    conflicting ``data_dir`` values, so the local-wins assertion has two real
    inputs; a separate empty-config/neutral-project resolution reaches the
    defaults branch.  The two assertions discriminate both precedence and
    defaults.  Base evidence: 0.30.1 passes this guard with project-local wins,
    ``~/.kindex`` and legacy profile state, as R1.3 requires on both sides.
    """
    _clear_kin_env(monkeypatch)
    if callable(getattr(config_mod, "active_root", None)):
        assert config_mod.active_root() is None

    global_cfg = tmp_path / "global.yaml"
    global_cfg.write_text(f"data_dir: {tmp_path / 'global-data'}\n")
    monkeypatch.setattr(config_mod, "_GLOBAL_PATHS", [global_cfg])
    project = tmp_path / "project"
    (project / ".kin").mkdir(parents=True)
    local_data = tmp_path / "local-data"
    (project / ".kin" / "config").write_text(f"data_dir: {local_data}\n")

    layered = config_mod.load_config(project_path=project)
    assert layered.data_dir == str(local_data), (
        "project config must continue to override the global config layer"
    )

    empty_cfg = tmp_path / "empty.yaml"
    empty_cfg.write_text("")
    monkeypatch.setattr(config_mod, "_GLOBAL_PATHS", [empty_cfg])
    neutral = tmp_path / "neutral"
    neutral.mkdir()
    defaults = config_mod.load_config(project_path=neutral)
    assert defaults.data_dir == "~/.kindex"
    assert defaults.active_profile is None
    assert defaults.profile_source == "legacy"


@pytest.mark.red_now
def test_r1_4_bound_context_releases_and_restores_unbound_resolution(
        tmp_path, monkeypatch):
    """R1.4: leaving ``bound_root`` restores the exact unbound resolution.

    Reversion: omitting release-time cache invalidation leaves the bound path in
    the second ``load_config`` and turns this red.  Reachability/non-degeneracy:
    the unbound baseline is an explicit config whose data path differs from the
    bound root; resolution is performed before, during, and after the context,
    and the middle assertion proves binding actually took effect.  Thus equal
    before/after values cannot arise from an unused seam.  Base evidence:
    0.30.1 fails on the absent context-manager API, for R1.4's reversible-seam
    reason rather than an unrelated config parse failure.
    """
    _clear_kin_env(monkeypatch)
    _, _, active_root, bound_root = _seam()
    project = tmp_path / "neutral"
    project.mkdir()
    explicit = tmp_path / "explicit.yaml"
    default_data = tmp_path / "unbound-data"
    explicit.write_text(f"data_dir: {default_data}\n")

    before = config_mod.load_config(config_path=explicit, project_path=project)
    assert _resolved(before.data_path) == default_data.resolve()

    root = tmp_path / "temporary-binding"
    with bound_root(root) as yielded:
        assert yielded == root.resolve()
        assert active_root() == root.resolve()
        during = config_mod.load_config(config_path=explicit, project_path=project)
        _assert_under(during.data_path, root)

    assert active_root() is None
    after = config_mod.load_config(config_path=explicit, project_path=project)
    assert _resolved(after.data_path) == default_data.resolve()


@pytest.mark.red_now
def test_r1_4_next_test_starts_unbound_and_unbind_is_idempotent(tmp_path):
    """R1.4: a later test in the same interpreter sees no leaked binding.

    Reversion: a process-global binding that survives the preceding test, or an
    ``unbind_root`` implementation that errors when already clear, turns this
    red.  Reachability/non-degeneracy: this is a distinct pytest item in the
    same session; it checks state before mutating it, then performs a real
    bind/release and a second no-op release.  The precondition discriminates
    cross-test leakage and is not hidden by fixture cleanup.  Base evidence:
    0.30.1 fails on the absent seam API, the R1.4 process-reversibility gap.
    """
    bind_root, unbind_root, active_root, _ = _seam()
    assert active_root() is None, "a prior test leaked its config binding"
    bind_root(tmp_path / "one-shot")
    assert active_root() == (tmp_path / "one-shot").resolve()
    unbind_root()
    unbind_root()  # specified no-op
    assert active_root() is None


@pytest.mark.red_now
def test_r1_4_nested_context_restores_prior_binding_and_direct_rebind_errors(
        tmp_path):
    """Amendment 1/R1.4: context nesting restores, direct rebind rejects.

    Reversion: permitting direct overwrite, clearing the outer binding on inner
    exit, or failing to release after an exception turns this red.
    Reachability/non-degeneracy: two distinct roots are active in sequence; the
    RuntimeError must name the first, the inner context must expose the second,
    and a deliberate body exception exercises ``finally`` cleanup.  These
    state transitions distinguish restoration from unconditional clearing.
    Base evidence: 0.30.1 fails because none of the contracted lifecycle API
    exists, exactly the reversible/error-semantics requirement.
    """
    bind_root, unbind_root, active_root, bound_root = _seam()
    outer = (tmp_path / "outer").resolve()
    inner = (tmp_path / "inner").resolve()

    bind_root(outer)
    try:
        with pytest.raises(RuntimeError, match=re.escape(str(outer))):
            bind_root(inner)
        assert active_root() == outer
        with bound_root(inner) as yielded:
            assert yielded == inner
            assert active_root() == inner
        assert active_root() == outer
    finally:
        unbind_root()

    class BodyFailure(Exception):
        pass

    with pytest.raises(BodyFailure):
        with bound_root(outer):
            raise BodyFailure("exercise exceptional context exit")
    assert active_root() is None


@pytest.mark.red_now
def test_r1_5_binding_contains_config_store_search_and_mcp_access(
        tmp_path, monkeypatch):
    """R1.5 CRITICAL: bound activity neither reads nor creates outside root.

    Reversion: consulting HOME/project config, retaining a cached MCP Store, or
    deriving any DB path from the canary's escape value turns this red.
    Reachability/non-degeneracy: valid canaries at both normal config locations
    point to a distinct escape directory; guarded file-open functions fail on
    any attempted read.  A real Store is opened, a node is added, direct FTS
    finds it, MCP search finds the same title, and both DB paths are asserted
    under the binding.  That proves all named code paths ran and the search had
    a survivor, while before/after outside-tree snapshots and canary mtimes
    discriminate writes.  Base evidence: 0.30.1 fails on the absent seam API,
    the exact isolation failure that previously exposed a live graph.
    """
    _clear_kin_env(monkeypatch)
    home = tmp_path / "home"
    global_canary = home / ".config" / "kindex" / "kin.yaml"
    global_canary.parent.mkdir(parents=True)
    project = tmp_path / "project"
    project_canary = project / ".kin" / "config"
    project_canary.parent.mkdir(parents=True)
    escape = tmp_path / "must-not-exist"
    canary_text = f"data_dir: {escape}\ncanary: do-not-read\n"
    global_canary.write_text(canary_text)
    project_canary.write_text(canary_text)
    # The module computes its global-path list at import time.  Point that
    # declared resolution surface at this temporary outside-root canary so a
    # regression that consults unbound globals is observed without touching a
    # real user graph.
    monkeypatch.setattr(config_mod, "_GLOBAL_PATHS", [global_canary])
    canaries = {global_canary.resolve(), project_canary.resolve()}
    mtimes = {p: p.stat().st_mtime_ns for p in canaries}
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project)

    mcp_mod = _import_mcp()  # controlled HOME, deliberately pre-bind
    bind_root, unbind_root, active_root, _ = _seam()
    root = tmp_path / "only-allowed-root"
    default_data = home / ".kindex"
    assert not default_data.exists()
    outside_before = {
        p.relative_to(tmp_path)
        for p in tmp_path.rglob("*")
        if p != root and not p.is_relative_to(root)
    }

    real_builtin_open = builtins.open
    real_io_open = io.open
    real_os_open = os.open

    def forbidden_config_path(file) -> bool:
        if isinstance(file, int):
            return False
        path = _resolved(file)
        if path.is_relative_to(root.resolve()):
            return False
        return (
            path.name in {"kin.yaml", "conv.yaml"}
            or (path.name == "config" and path.parent.name == ".kin")
        )

    def guarded_builtin_open(file, *args, **kwargs):
        if forbidden_config_path(file) or (
                not isinstance(file, int) and _resolved(file) in canaries
        ):
            pytest.fail(f"bound resolution read outside-root config: {file}")
        return real_builtin_open(file, *args, **kwargs)

    def guarded_io_open(file, *args, **kwargs):
        if forbidden_config_path(file) or (
                not isinstance(file, int) and _resolved(file) in canaries
        ):
            pytest.fail(f"bound resolution read outside-root config: {file}")
        return real_io_open(file, *args, **kwargs)

    def guarded_os_open(file, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is None and (
                forbidden_config_path(file)
                or (not isinstance(file, int) and _resolved(file) in canaries)
        ):
            pytest.fail(f"bound resolution opened outside-root config: {file}")
        return real_os_open(file, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(builtins, "open", guarded_builtin_open)
    monkeypatch.setattr(io, "open", guarded_io_open)
    monkeypatch.setattr(os, "open", guarded_os_open)

    direct_store = None
    mcp_store = None
    bind_root(root)
    try:
        assert active_root() == root.resolve()
        cfg = config_mod.load_config()
        _assert_under(cfg.data_dir, root)
        _assert_under(cfg.data_path, root)
        direct_store = Store(cfg)
        direct_store.add_node(
            "V8 containment canary node",
            content="v8containmenttoken",
            node_id="v8-contained",
        )
        direct_ids = {
            row["id"] for row in direct_store.fts_search("v8containmenttoken")
        }
        assert direct_ids == {"v8-contained"}, (
            f"direct search reachability control failed: {direct_ids}"
        )
        _assert_under(direct_store.db_path, root)

        monkeypatch.setattr(mcp_mod, "_store", None)
        monkeypatch.setattr(mcp_mod, "_config", None)
        out = mcp_mod.search("v8containmenttoken")
        assert "V8 containment canary node" in out, (
            f"MCP search did not reach the bound fixture: {out!r}"
        )
        mcp_store = mcp_mod._store
        assert mcp_store is not None
        _assert_under(mcp_store.db_path, root)
    finally:
        if mcp_store is not None and mcp_store is not direct_store:
            mcp_store.close()
        if direct_store is not None:
            direct_store.close()
        monkeypatch.setattr(mcp_mod, "_store", None)
        monkeypatch.setattr(mcp_mod, "_config", None)
        unbind_root()

    assert active_root() is None
    assert not escape.exists(), "outside-root data_dir from canary was honored"
    assert not default_data.exists(), "default HOME data directory was created"
    for path, before_mtime in mtimes.items():
        assert path.stat().st_mtime_ns == before_mtime, (
            f"outside-root canary mtime changed: {path}"
        )
    outside_after = {
        p.relative_to(tmp_path)
        for p in tmp_path.rglob("*")
        if p != root and not p.is_relative_to(root)
    }
    assert outside_after == outside_before, (
        f"binding created paths outside root: {outside_after - outside_before}"
    )


@pytest.mark.red_now
def test_r1_5_relative_parent_paths_cannot_escape_binding(tmp_path, monkeypatch):
    """R1.5: relative ``../`` and ``../../`` inputs remain contained.

    Reversion: resolving configured relative parent paths before applying the
    active root turns this red.  Reachability/non-degeneracy: two distinct
    config files request ``../escaped`` and ``../../escaped``; each is resolved
    while a real binding is active and each result is checked directly beneath
    the root.  The fixture targets traversal syntax specifically; absolute and
    tilde paths would not exercise this escape shape.
    """
    bind_root, unbind_root, active_root, _ = _seam()
    root = tmp_path / "bound" / "nested"
    root.mkdir(parents=True)
    for index, value in enumerate(("../escaped", "../../escaped")):
        config_path = tmp_path / f"relative-{index}.yaml"
        config_path.write_text(f"data_dir: {value}\n")
        bind_root(root)
        try:
            assert active_root() == root.resolve()
            cfg = config_mod.load_config(config_path=config_path)
            _assert_under(cfg.data_dir, root)
            _assert_under(cfg.data_path, root)
        finally:
            unbind_root()


@pytest.mark.red_now
def test_r1_5_binding_contains_project_resolver_env_input(tmp_path, monkeypatch):
    """R1.5: ``KIN_PROJECT`` cannot redirect resolution outside the root.

    Reversion: consulting ``KIN_PROJECT`` before the active binding turns this
    red.  Reachability/non-degeneracy: the environment points at a real
    outside project containing a config file, while the bound root is distinct;
    ``load_config`` resolves under the binding and the direct path assertions
    detect any environment escape.  Unlike the broad containment fixture, this
    deliberately retains ``KIN_PROJECT`` instead of stripping it.
    """
    bind_root, unbind_root, active_root, _ = _seam()
    outside = tmp_path / "outside-project"
    (outside / ".kin").mkdir(parents=True)
    (outside / ".kin" / "config").write_text(
        f"data_dir: {tmp_path / 'outside-data'}\n"
    )
    monkeypatch.setenv("KIN_PROJECT", str(outside))
    root = tmp_path / "bound-project"
    bind_root(root)
    try:
        assert active_root() == root.resolve()
        cfg = config_mod.load_config()
        _assert_under(cfg.data_dir, root)
        _assert_under(cfg.data_path, root)
    finally:
        unbind_root()


@pytest.mark.red_now
def test_r1_5_binding_contains_explicit_project_path_input(tmp_path):
    """R1.5: an explicit outside ``project_path`` cannot escape the binding.

    Reversion: honoring the explicit resolver argument ahead of the active
    binding turns this red.  Reachability/non-degeneracy: a real outside
    project config requests an outside data directory, and the call passes
    that project explicitly while bound; the resulting data and derived path
    assertions prove the argument was exercised but contained.
    """
    bind_root, unbind_root, active_root, _ = _seam()
    outside = tmp_path / "explicit-outside"
    (outside / ".kin").mkdir(parents=True)
    (outside / ".kin" / "config").write_text(
        f"data_dir: {tmp_path / 'explicit-outside-data'}\n"
    )
    root = tmp_path / "explicit-bound"
    bind_root(root)
    try:
        assert active_root() == root.resolve()
        cfg = config_mod.load_config(project_path=outside)
        _assert_under(cfg.data_dir, root)
        _assert_under(cfg.data_path, root)
    finally:
        unbind_root()


@pytest.mark.red_now
def test_r1_5_binding_contains_upward_git_project_root_walk(tmp_path, monkeypatch):
    """R1.5: an upward git-root walk cannot cross the active binding.

    Reversion: walking from a bound nested directory to the enclosing checkout
    root turns this red.  Reachability/non-degeneracy: a real temporary git
    repository is initialized, the binding is placed inside its ``.kin``
    subtree, and the resolver is called from that nested directory.  The
    returned project root is asserted directly beneath the binding, exercising
    the surrounding-filesystem shape that other containment fixtures lack.
    """
    bind_root, unbind_root, active_root, _ = _seam()
    repo = tmp_path / "checkout"
    nested = repo / "bound" / "nested"
    nested.mkdir(parents=True)
    subprocess.run(
        ["git", "init", str(repo)], check=True, capture_output=True,
    )
    monkeypatch.chdir(nested)
    bind_root(nested)
    try:
        assert active_root() == nested.resolve()
        resolved = config_mod.resolve_project_root()
        _assert_under(resolved, nested)
    finally:
        unbind_root()


@pytest.mark.red_now
def test_r1_5_git_probe_does_not_start_outside_binding(tmp_path, monkeypatch):
    """R1.5: the git filesystem probe itself must stay under the binding.

    Reversion: launching ``git`` with an outside checkout/root as its working
    directory turns this red even if the resolver later clamps its return value.
    Reachability/non-degeneracy: a real temporary checkout contains the bound
    nested directory, and the resolver is invoked while ``KIN_PROJECT`` points
    at that nested directory.  The subprocess probe is wrapped at the declared
    Python boundary; any explicit ``cwd`` outside the binding fails immediately,
    and the observed probe list must remain empty.  This covers the prohibited
    read itself rather than trusting only the returned path or probe start.
    """
    bind_root, unbind_root, active_root, _ = _seam()
    repo = tmp_path / "probe-checkout"
    nested = repo / "bound" / "nested"
    nested.mkdir(parents=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    monkeypatch.setenv("KIN_PROJECT", str(nested))
    root = nested
    observed = []
    real_run = subprocess.run

    def guarded_run(*args, **kwargs):
        observed.append((args, kwargs))
        cwd = kwargs.get("cwd")
        if cwd is not None:
            assert Path(cwd).resolve().is_relative_to(root.resolve()), (
                f"git/config probe started outside active binding: {cwd}"
            )
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)
    bind_root(root)
    try:
        assert active_root() == root.resolve()
        config_mod.resolve_project_root()
    finally:
        unbind_root()
    assert observed == [], (
        f"bound project resolution must not launch an upward git probe: {observed}"
    )


@pytest.mark.red_now
def test_r1_5_binding_contains_config_profile_and_mcp_writes(tmp_path, monkeypatch):
    """R1.5 write direction: bound surfaces cannot mutate outside paths.

    Reversion: any global-config/profile writer or MCP reminder scheduler that
    bypasses the active binding turns this red.  Reachability/non-degeneracy:
    the CLI's real ``config set --global`` and ``profile create`` paths and the
    MCP ``remind_create`` tool all execute while bound.  A complete path/mtime
    snapshot of the surrounding temporary tree is taken before and after; the
    outside-root comparison is set-based and therefore catches unpredicted
    files, modifications, and deletions rather than allowlisting filenames.
    """
    import sys

    import kindex.cli as cli_mod
    import kindex.mcp_server as mcp_mod

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "bound-write-root"
    root.mkdir()
    before = {
        path.relative_to(tmp_path): path.stat().st_mtime_ns
        for path in tmp_path.rglob("*")
        if not path.is_relative_to(root)
    }

    def invoke_cli(*argv):
        monkeypatch.setattr(sys, "argv", ["kin", *argv])
        return cli_mod.main()

    bind_root, unbind_root, active_root, _ = _seam()
    bind_root(root)
    try:
        assert active_root() == root.resolve()
        invoke_cli("config", "set", "llm.enabled", "false", "--global")
        profile_data = root / "profile-data"
        invoke_cli(
            "profile", "create", "v8-write-probe", "--default",
            "--data-dir", str(profile_data),
        )
        monkeypatch.setattr(mcp_mod, "_store", None)
        monkeypatch.setattr(mcp_mod, "_config", None)
        result = mcp_mod.remind_create("v8 write containment", "2030-01-01T00:00:00")
        assert isinstance(result, str)
    finally:
        monkeypatch.setattr(mcp_mod, "_store", None)
        monkeypatch.setattr(mcp_mod, "_config", None)
        unbind_root()

    after = {
        path.relative_to(tmp_path): path.stat().st_mtime_ns
        for path in tmp_path.rglob("*")
        if not path.is_relative_to(root)
    }
    modified = {p for p in set(before) & set(after) if before[p] != after[p]}
    assert after == before, (
        f"bound write surfaces changed outside-root paths: "
        f"added={set(after) - set(before)}, removed={set(before) - set(after)}, "
        f"modified={modified}"
    )


@pytest.mark.red_now
def test_r1_5_binding_rejects_existing_target_config_symlinks(tmp_path, monkeypatch):
    """R1.5: bound config reads/writes cannot follow symlinks outside root.

    Existing targets are intentional: dangling links only exercise error
    handling, while these links prove that a successful read/write cannot
    escape through either config file or its parent directory.
    """
    import builtins
    import io
    import sys

    import kindex.cli as cli_mod

    outside = tmp_path / "outside"
    outside.mkdir()
    outside_global = outside / "global.yaml"
    outside_project = outside / "project-config"
    outside_global.write_text("data_dir: /outside/global\nknown: sentinel\n")
    outside_project.write_text("data_dir: /outside/project\nknown: sentinel\n")
    global_content = outside_global.read_text()
    project_content = outside_project.read_text()
    global_mtime = outside_global.stat().st_mtime_ns
    project_mtime = outside_project.stat().st_mtime_ns
    root = tmp_path / "bound"
    (root / ".config" / "kindex").mkdir(parents=True)
    project = root / "project"
    (project / ".kin").mkdir(parents=True)
    (root / ".config" / "kindex" / "kin.yaml").symlink_to(outside_global)
    (project / ".kin" / "config").symlink_to(outside_project)
    monkeypatch.setattr(config_mod, "_GLOBAL_PATHS", [root / ".config" / "kindex" / "kin.yaml"])
    monkeypatch.chdir(project)

    real_open, real_io_open = builtins.open, io.open
    def deny_outside(file, *args, **kwargs):
        if not isinstance(file, int) and Path(file).resolve() in {outside_global, outside_project}:
            raise AssertionError(f"bound config followed symlink read: {file}")
        return real_open(file, *args, **kwargs)
    def deny_outside_io(file, *args, **kwargs):
        if not isinstance(file, int) and Path(file).resolve() in {outside_global, outside_project}:
            raise AssertionError(f"bound config followed symlink read: {file}")
        return real_io_open(file, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", deny_outside)
    monkeypatch.setattr(io, "open", deny_outside_io)

    bind_root, unbind_root, _, _ = _seam()
    bind_root(root)
    try:
        cfg = config_mod.load_config(project_path=project)
        _assert_under(cfg.data_dir, root)
        monkeypatch.setattr(sys, "argv", [
            "kin", "config", "set", "symlink.probe", "blocked", "--global",
        ])
        cli_mod.main()
    finally:
        unbind_root()
    monkeypatch.setattr(builtins, "open", real_open)
    monkeypatch.setattr(io, "open", real_io_open)
    assert outside_global.read_text() == global_content
    assert outside_project.read_text() == project_content
    assert outside_global.stat().st_mtime_ns == global_mtime
    assert outside_project.stat().st_mtime_ns == project_mtime


@pytest.mark.red_now
def test_r1_5_bound_config_directory_symlink_cannot_create_outside(tmp_path, monkeypatch):
    """R1.5: creating a bound config directory must not follow an outside link."""
    import sys

    import kindex.cli as cli_mod

    outside = tmp_path / "outside-dir"
    outside.mkdir()
    root = tmp_path / "bound-dir"
    (root / ".config").mkdir(parents=True)
    (root / ".config" / "kindex").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(config_mod, "_GLOBAL_PATHS", [root / ".config" / "kindex" / "kin.yaml"])
    bind_root, unbind_root, _, _ = _seam()
    bind_root(root)
    try:
        monkeypatch.setattr(sys, "argv", [
            "kin", "config", "set", "directory.probe", "blocked", "--global",
        ])
        cli_mod.main()
    finally:
        unbind_root()
    assert list(outside.iterdir()) == [], "bound config creation escaped via symlink"


@pytest.mark.red_now
def test_r1_5_scheduler_installers_do_not_escape_environment(tmp_path, monkeypatch):
    """R1.5: cron/launchd setup cannot write or execute outside a binding."""
    import kindex.setup as setup_mod

    root = tmp_path / "bound"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    before = {p.relative_to(tmp_path): p.stat().st_mtime_ns for p in tmp_path.rglob("*")}
    calls = []
    real_run = subprocess.run
    def guarded_run(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError(f"scheduler installer executed outside-bound subprocess: {args}")
    monkeypatch.setattr(subprocess, "run", guarded_run)
    bind_root, unbind_root, _, _ = _seam()
    bind_root(root)
    try:
        cfg = config_mod.load_config()
        setup_mod.install_crontab(cfg)
        setup_mod.install_launchd(cfg)
    finally:
        unbind_root()
        monkeypatch.setattr(subprocess, "run", real_run)
    after = {p.relative_to(tmp_path): p.stat().st_mtime_ns for p in tmp_path.rglob("*")}
    assert after == before, f"scheduler setup changed outside-bound paths: {after ^ before}"
    assert not calls, f"scheduler setup launched an uncontained subprocess: {calls}"


def _outside_snapshot(tmp_path, root):
    return {p.relative_to(tmp_path): p.stat().st_mtime_ns
            for p in tmp_path.rglob("*") if not p.is_relative_to(root)}


def test_r1_5_unusable_config_parent_falls_back_inside_root(tmp_path, monkeypatch):
    """R1.5: a bound ``.config`` that is a regular file makes the global
    config directory uncreatable. The writers used to fall back to the real
    HOME's kin.yaml; they must fall back to the bound root's instead."""
    import sys

    import kindex.cli as cli_mod

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "bound-blocked"
    root.mkdir()
    (root / ".config").write_text("not a directory")
    before = _outside_snapshot(tmp_path, root)
    bind_root, unbind_root, _, _ = _seam()
    bind_root(root)
    try:
        monkeypatch.setattr(sys, "argv", ["kin", "config", "set", "llm.enabled", "false", "--global"])
        cli_mod.main()
        monkeypatch.setattr(sys, "argv", ["kin", "profile", "create", "blocked-parent",
                                          "--data-dir", str(root / "profile-data")])
        cli_mod.main()
    finally:
        unbind_root()
    assert _outside_snapshot(tmp_path, root) == before, "bound config write escaped to HOME"
    assert "blocked-parent" in (root / "kin.yaml").read_text()


def test_r1_5_profile_create_explicit_config_is_anchored_in_root(tmp_path, monkeypatch):
    """R1.5: ``kin profile create --config <outside path>`` resolves the path
    beneath the bound root, as ``kin config set --config`` already does."""
    import sys

    import kindex.cli as cli_mod

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "bound-explicit"
    root.mkdir()
    outside = tmp_path / "outside" / "kin.yaml"
    before = _outside_snapshot(tmp_path, root)
    bind_root, unbind_root, _, _ = _seam()
    bind_root(root)
    try:
        monkeypatch.setattr(sys, "argv", ["kin", "profile", "create", "explicit-probe",
                                          "--data-dir", str(root / "d"), "--config", str(outside)])
        cli_mod.main()
    finally:
        unbind_root()
    assert not outside.exists()
    assert _outside_snapshot(tmp_path, root) == before, "explicit --config wrote outside the root"


def test_r1_5_bound_identity_never_probes_git(tmp_path, monkeypatch):
    """R1.5: under a binding, the current user is not read from a git
    config outside the root (the real HOME's or an enclosing repo's)."""
    outer = tmp_path / "outer"
    outer.mkdir()
    subprocess.run(["git", "init", "-q", str(outer)], check=True)
    subprocess.run(["git", "-C", str(outer), "config", "user.name", "OUTSIDE-BINDING"], check=True)
    root = outer / "bound"
    root.mkdir()
    calls = []
    real_run = subprocess.run
    def spy(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("args"))
        return real_run(*args, **kwargs)
    monkeypatch.setattr(subprocess, "run", spy)
    bind_root, unbind_root, _, _ = _seam()
    bind_root(root)
    try:
        user = config_mod.load_config().current_user
    finally:
        unbind_root()
    assert user.lower() != "outside-binding"
    assert not [c for c in calls if c and c[0] == "git"], f"git probed under a binding: {calls}"


def test_profile_create_succeeds_on_a_bare_home(tmp_path, monkeypatch):
    """A fresh HOME with no ~/.config/kindex: ``kin profile create`` creates
    the directory and the file and exits 0 (the cli_home fixture pre-creates
    the directory, which hid a crash here)."""
    import sys

    import kindex.cli as cli_mod

    home = tmp_path / "bare-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    target = home / ".config" / "kindex" / "kin.yaml"
    monkeypatch.setattr(config_mod, "_GLOBAL_PATHS", [target])
    monkeypatch.setattr(sys, "argv", ["kin", "profile", "create", "fresh",
                                      "--data-dir", str(tmp_path / "fresh-data")])
    try:
        cli_mod.main()
    except SystemExit as exc:
        assert exc.code in (0, None), f"profile create exited {exc.code}"
    assert "fresh" in target.read_text()


def test_r1_5_symlinked_root_fallback_is_refused(tmp_path, monkeypatch):
    """R1.5: when the bound config parent is unusable and ``<root>/kin.yaml``
    is a symlink out of the root, the write is refused, not followed."""
    import sys

    import kindex.cli as cli_mod

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "bound-linked"
    root.mkdir()
    (root / ".config").write_text("not a directory")
    outside = tmp_path / "outside-kin.yaml"
    outside.write_text("untouched: true\n")
    (root / "kin.yaml").symlink_to(outside)
    bind_root, unbind_root, _, _ = _seam()
    bind_root(root)
    try:
        monkeypatch.setattr(sys, "argv", ["kin", "config", "set", "llm.enabled", "false", "--global"])
        with pytest.raises(SystemExit) as exc:
            cli_mod.main()
    finally:
        unbind_root()
    assert exc.value.code == 1
    assert outside.read_text() == "untouched: true\n"
