"""Supplemental adapter surface guards; installed boundary execution is separate."""
import inspect


def test_cli_parses_frozen_prepare_only_command():
    # Capability/public interface CLI paragraph. Red: missing/options-drifted adapter.
    from kindex.cli import build_parser
    args = build_parser().parse_args([
        'context-query', 'auroracable', '--source-bytes', '3000', '--top-k', '3',
        '--client', 'codex', '--include-handoff', '--evidence-session-id',
        '4e11ba81-7edb-4d0f-8999-e59f6f84b5a7', '--handoff-bytes', '5000',
        '--selection', 'explicit_selected', '--evidence-id',
        '51e278ad-60ee-4315-91bd-137fb43f33c6', '--action', 'release_archive',
        '--at', '2030-01-02T12:00:00Z'])
    assert callable(args.func)


def test_mcp_exposes_typed_operation_without_scope_override():
    # Capability/public interface MCP and Client addendum adapter binding.
    from kindex import mcp_server
    parameters = inspect.signature(mcp_server.query_context).parameters
    assert {'query', 'include_handoff', 'evaluation_time', 'evidence_session_id',
            'handoff_selection', 'evidence_ids', 'handoff_max_bytes', 'action'} <= parameters.keys()
    assert not {'client', 'client_scope', 'project', 'project_path', 'db_path',
                'data_dir', 'source_ref', 'ranked_candidates', 'candidates'} & parameters.keys()
    assert all(p.kind != inspect.Parameter.VAR_KEYWORD for p in parameters.values())
