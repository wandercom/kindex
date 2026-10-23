"""Kindex text files have an explicit UTF-8 encoding on every platform."""

import ast
from pathlib import Path

import pytest

import kindex
from kindex.config import Config, read_degraded_events
from kindex.daemon import _process_inbox
from kindex.hooks import write_inbox_item
from kindex.ingest import resolve_kin_chain, scan_kin_files
from kindex.scheduling import SchedulerState, StoreReport, _read_state, _write_state
from kindex.store import Store


def _text_mode(call: ast.Call, name: str) -> bool:
    func = call.func
    module = (func.value.id if isinstance(func, ast.Attribute)
              and isinstance(func.value, ast.Name) else None)
    if name == "open" and module in {"os", "tarfile", "zipfile", "webbrowser"}:
        return False  # descriptor, archive, or URL APIs rather than text streams

    if name in {"NamedTemporaryFile", "TemporaryFile"}:
        index, default = 0, "w+b"
    elif name == "open" and module in {"gzip", "bz2", "lzma"}:
        index, default = 1, "rb"
    elif (name == "open" and isinstance(func, ast.Attribute)
          and module not in {"io", "codecs"}):
        index, default = 0, "r"  # Path.open(mode)
    else:
        index, default = 1, "r"  # open(path, mode), os.fdopen(fd, mode)

    mode = next((k.value for k in call.keywords if k.arg == "mode"),
                call.args[index] if len(call.args) > index else ast.Constant(default))
    return not (isinstance(mode, ast.Constant) and isinstance(mode.value, str)
                and "b" in mode.value)


def _missing_encoding(source: str) -> list[str]:
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name not in {"read_text", "write_text", "open", "fdopen",
                        "NamedTemporaryFile", "TemporaryFile"}:
            continue
        if any(k.arg == "encoding" and isinstance(k.value, ast.Constant)
               and k.value.value == "utf-8" for k in node.keywords):
            continue
        if name not in {"read_text", "write_text"} and not _text_mode(node, name):
            continue
        offenders.append(f"{node.lineno} {name}")
    return offenders


def test_kindex_names_the_encoding_of_every_text_file():
    package = Path(kindex.__file__).parent
    offenders = [f"{path.relative_to(package)}:{item}"
                 for path in sorted(package.rglob("*.py"))
                 for item in _missing_encoding(path.read_text(encoding="utf-8"))]
    assert offenders == []


@pytest.mark.parametrize("call", [
    'os.fdopen(fd, "w")',
    'path.open("w")',
    'tempfile.NamedTemporaryFile(mode="w")',
    'gzip.open(path, "wt")',
    'bz2.open(path, "wt")',
    'lzma.open(path, "wt")',
    'io.open(path, "w")',
    'codecs.open(path, "w")',
    'open(path, "w", **options)',
    'open(path, "w", encoding="locale")',
])
def test_guard_catches_other_text_writer_apis(call):
    assert _missing_encoding(call)


@pytest.mark.parametrize("call", [
    'os.open(path, flags)',
    'os.fdopen(fd, "wb")',
    'tempfile.NamedTemporaryFile()',
    'gzip.open(path)',
])
def test_guard_allows_binary_and_non_text_apis(call):
    assert _missing_encoding(call) == []


def test_inbox_writer_emits_utf8(tmp_path):
    config = Config(data_dir=str(tmp_path))
    item = write_inbox_item(config, "Café booking", source="José")
    raw = item.read_bytes()
    assert "Café booking".encode("utf-8") in raw
    assert "José".encode("utf-8") in raw
    assert raw.decode("utf-8")


def test_scheduler_state_round_trips_utf8(tmp_path):
    path = tmp_path / "scheduler-state.json"
    key = "C:/Users/José/.kindex/db.sqlite"
    state = SchedulerState(stores={key: StoreReport(interval=60, at=1)}, applied=60)
    _write_state(path, state)
    assert key.encode("utf-8") in path.read_bytes()
    assert _read_state(path) == state


def test_legacy_inbox_item_remains_unprocessed(tmp_path):
    config = Config(data_dir=str(tmp_path))
    config.inbox_dir.mkdir(parents=True)
    item = config.inbox_dir / "legacy.md"
    raw = "---\nsource: José\n---\n\nCafé booking\n".encode("cp1252")
    item.write_bytes(raw)
    store = Store(config)
    try:
        results = {}
        assert _process_inbox(config, store, results=results) == 0
        assert results["inbox_decode_errors"] == 1
        assert "left unprocessed" in results["errors"]["inbox"]
        assert item.read_bytes() == raw
        assert not (config.inbox_dir / ".processed" / item.name).exists()
    finally:
        store.close()


def test_multiple_legacy_inbox_items_report_every_path(tmp_path):
    config = Config(data_dir=str(tmp_path))
    config.inbox_dir.mkdir(parents=True)
    items = [config.inbox_dir / name for name in ("first.md", "second.md")]
    for item in items:
        item.write_bytes("Café booking request".encode("cp1252"))
    store = Store(config)
    try:
        results = {}
        assert _process_inbox(config, store, results=results) == 0
        assert results["inbox_decode_errors"] == 2
        for item in items:
            assert str(item) in results["errors"]["inbox"]
            assert item.read_bytes() == "Café booking request".encode("cp1252")
    finally:
        store.close()


def test_legacy_scheduler_state_is_not_reset(tmp_path):
    path = tmp_path / "scheduler-state.json"
    raw = SchedulerState(
        stores={"C:/Users/José/db.sqlite": StoreReport(interval=60, at=1)},
        applied=60,
    ).model_dump_json().encode("cp1252")
    path.write_bytes(raw)
    with pytest.raises(ValueError, match="not UTF-8"):
        _read_state(path)
    assert path.read_bytes() == raw


def test_legacy_project_config_is_reported_and_preserved(tmp_path, capsys):
    projects = tmp_path / "projects"
    kin_dir = projects / "legacy" / ".kin"
    kin_dir.mkdir(parents=True)
    config_file = kin_dir / "config"
    raw = "name: Café\n".encode("cp1252")
    config_file.write_bytes(raw)
    config = Config(data_dir=str(tmp_path / "data"), project_dirs=[str(projects)])
    store = Store(config)
    try:
        assert scan_kin_files(config, store) == 0
    finally:
        store.close()
    assert "not UTF-8" not in capsys.readouterr().err
    assert any("not UTF-8" in event["msg"]
               for event in read_degraded_events(config))
    store = Store(config)
    try:
        assert scan_kin_files(config, store, verbose=True) == 0
    finally:
        store.close()
    assert "not UTF-8" in capsys.readouterr().err
    assert config_file.read_bytes() == raw


def test_direct_kin_chain_reports_legacy_config_without_modifying_it(tmp_path):
    config_file = tmp_path / ".kin" / "config"
    config_file.parent.mkdir()
    raw = "name: Café\n".encode("cp1252")
    config_file.write_bytes(raw)
    with pytest.raises(UnicodeError, match="Back it up and convert a copy"):
        resolve_kin_chain(config_file)
    assert config_file.read_bytes() == raw
