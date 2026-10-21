"""Exercise the full MCP protocol with synthetic state and no provider credentials."""
import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .test_public_boundary import request
from .test_additive_contract import QUERY


def test_stdio_catalog_prepared_read_and_explicit_privacy_refusal(env):
    import kindex
    marker = 'INVENTED_STDIO_SOURCE_PROOF'
    env.source(QUERY + ' ' + marker, title=QUERY)
    before = tuple(env.rw.conn.iterdump())
    home = env.root / 'stdio-home'
    config_root = home / '.config'
    config_path = config_root / 'kindex' / 'kin.yaml'
    config_path.parent.mkdir(parents=True)
    values = env.cfg.model_dump(mode='json')
    values.update(project_dirs=[], embedding={'provider': 'none'},
                  llm={'enabled': False}, sim={'enabled': False},
                  attention={'reinforce_enabled': False})
    config_path.write_text(json.dumps(values))
    process_env = {
        'PATH': os.environ.get('PATH', ''), 'HOME': str(home),
        'XDG_CONFIG_HOME': str(config_root), 'XDG_STATE_HOME': str(home / 'state'),
        'KIN_NO_SCHEDULER_WRITES': '1', 'KIN_PROJECT_PATH': env.project,
        'KIN_CLIENT': 'codex', 'PYTHONPATH': str(Path(kindex.__file__).resolve().parents[1]),
        'PYTHONDONTWRITEBYTECODE': '1',
    }
    params = StdioServerParameters(command=sys.executable,
        args=['-m', 'kindex.mcp_server'], cwd=env.project, env=process_env)
    invented = 'ghp_' + 'INVENTED_NOT_A_REAL_CREDENTIAL_' * 2

    async def cycle():
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                catalog = (await client.list_tools()).tools
                assert sum(tool.name == 'query_context' for tool in catalog) == 1
                response = await client.call_tool('query_context', request())
                assert not response.isError
                text = '\n'.join(item.text for item in response.content if hasattr(item, 'text'))
                value = json.loads(text)
                assert value['status'] == 'prepared'
                assert marker in value['retrieval']['text']
                assert value['handoff']['status'] == 'disabled'
                assert value['delivery'] == value['use'] == 'not_observed'
                refused = await client.call_tool('query_context', request(query=QUERY + ' ' + invented))
                refusal_text = '\n'.join(item.text for item in refused.content if hasattr(item, 'text'))
                assert invented not in refusal_text
                assert refusal_text.strip()
                # The protocol error indicator must not falsely advertise success.
                assert refused.isError is True
    asyncio.run(asyncio.wait_for(cycle(), timeout=30))
    assert tuple(env.rw.conn.iterdump()) == before
