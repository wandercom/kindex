"""Synthetic contracts ported from the frozen practical-memory campaign."""
from pathlib import Path


def make_store(run_root, name='fixture', *, read_only=False):
    """Open only a plan-registered synthetic store. Never load ambient config."""
    root = Path(run_root).resolve()
    if not name or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789_-' for c in name):
        raise ValueError('Use a simple lowercase synthetic store name')
    from kindex.config import Config
    from kindex.store import Store

    inputs = root / 'empty-inputs' / name
    config = Config(
        data_dir=str(root / 'stores' / name), user='v6-fixture', agent_id='v6-fixture-agent',
        project_dirs=[], profiles={}, secret_mask=None,
        llm={'enabled': False}, attention={'enabled': False}, sim={'enabled': False},
        **{key: str(inputs / key) for key in (
            'claude_dir', 'codex_dir', 'gemini_dir', 'antigravity_dir',
            'antigravity_cli_dir', 'opencode_dir', 'cursor_dir', 'muse_dir')},
    )
    return Store(config, manage_project_storage=False, read_only=read_only)
