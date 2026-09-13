"""Two independent regressions: file-URL privacy and modern-plugin session reset.

Only parent-ratified public contracts are used. The TypeScript module is imported
at Validator test execution, never read or rewritten by the independent Tester.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess

from test_kinbase_transfer_identity import (
    ALPHA, BETA, QUESTION, EVIDENCE_URL, SEED, all_strings, cli, run_python, transfer,
)


FILE_URL = "file:///Users/private-url-owner/notes"
LOCALHOST_FILE_URL = "file://localhost/private-url-owner/notes"


def test_org_export_scrubs_file_urls_in_prose_nested_metadata_and_identity_keys(transfer):
    """Ratified privacy: local file URLs are private even when URL-shaped.

    Mutation: preserve every URL scheme as web evidence, or collapse sanitized
    file-shaped logical identities into one generic key.
    """
    source = transfer["root"] / "file-url-source"

    def metadata(key, source_suffix):
        return {"repo": "synthetic-file-url-repository", "logical_key": key,
                "source_identity": key + "#" + source_suffix,
                "mode": "raw", "provenance": "external",
                "evidence": {"nested": [{"local": FILE_URL,
                                           "localhost": LOCALHOST_FILE_URL,
                                           "https": EVIDENCE_URL}]}}

    nodes = [
        {"id": ALPHA, "title": "Synthetic URL alpha fact", "type": "concept",
         "content": "Local notes: " + FILE_URL + ". Evidence: " + EVIDENCE_URL,
         "kinbase": metadata(FILE_URL, "fact-alpha")},
        {"id": BETA, "title": "Synthetic URL beta fact", "type": "concept",
         "content": "Local notes: " + LOCALHOST_FILE_URL + ". Evidence: " + EVIDENCE_URL,
         "kinbase": metadata(LOCALHOST_FILE_URL, "fact-beta")},
        {"id": QUESTION, "title": "Synthetic URL alpha question", "type": "question",
         "content": "Which synthetic URL alpha check remains unresolved?",
         "kinbase": metadata(FILE_URL, "question-alpha")},
    ]
    links = run_python(transfer, SEED, {"data_dir": str(source), "nodes": nodes, "facts": [ALPHA, BETA]})
    assert links[ALPHA] == [QUESTION]
    assert links[BETA] == []
    exported = json.loads(cli(transfer, "export", "--audience", "org", "--format", "json",
                              "--data-dir", str(source), "--config", "/dev/null"))
    rows = {row["id"]: row for row in exported}
    assert {ALPHA, BETA, QUESTION} <= set(rows)
    strings = list(all_strings(exported))
    assert not any("private-url-owner" in value for value in strings)
    assert not any(FILE_URL in value or LOCALHOST_FILE_URL in value for value in strings)
    for identity in (ALPHA, BETA, QUESTION):
        assert EVIDENCE_URL in set(all_strings(rows[identity]["extra"]["kinbase"]))
    alpha = rows[ALPHA]["extra"]["kinbase"]
    beta = rows[BETA]["extra"]["kinbase"]
    question = rows[QUESTION]["extra"]["kinbase"]
    assert alpha["logical_key"] != beta["logical_key"]
    assert alpha["logical_key"] == question["logical_key"]
    assert alpha["source_identity"] != beta["source_identity"]


MODERN_DRIVER = r'''
import { registerHooks } from 'node:module';
import { pathToFileURL } from 'node:url';

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === './runtime' && context.parentURL) {
      return nextResolve(new URL('./runtime.ts', context.parentURL).href, context);
    }
    return nextResolve(specifier, context);
  },
});

const callbacks = new Map();
const calls = [];
let phase = 'registration';
let currentSession = fixture.firstSession;
const on = (name, callback) => {
  const list = callbacks.get(name) ?? [];
  list.push(callback);
  callbacks.set(name, list);
};
const $ = {
  session: {
    cwd: async () => fixture.project,
    id: async () => currentSession,
  },
  tool: {
    register: (...args) => ({tool: typeof args[0] === 'string' ? args[0] : 'synthetic-tool'}),
  },
  ui: {status: () => {}},
  process: {
    run: async (argv, options = {}) => {
      const request = options.stdin ? JSON.parse(options.stdin) : null;
      calls.push({phase, argv, request});
      return {exitCode: 0, stdout: JSON.stringify({
        ok: true, policy_owner: 'kindex', context: 'synthetic',
        open_tasks: 0, retrieved: 0, supervisor: {state: 'quiet'},
      })};
    },
  },
};
const {register} = await import(pathToFileURL(fixture.module).href);
await register(on);
async function fire(name, event) {
  const handlers = callbacks.get(name);
  if (!handlers || handlers.length === 0) throw new Error('Public hook not registered: ' + name);
  let current = event;
  for (const handler of handlers) {
    const result = await handler($, current, async (nextEvent = current) => nextEvent);
    if (result !== undefined) current = result;
  }
  return current;
}

phase = 'first-start';
await fire('session.start', {});
phase = 'first-prompt';
await fire('prompt.submit', {text: fixture.oldText});
phase = 'first-context';
await fire('prompt.context', {blocks: []});
currentSession = fixture.secondSession;
phase = 'second-start';
await fire('session.start', {});
phase = 'second-before-prompt';
await fire('prompt.context', {blocks: []});
phase = 'second-prompt';
await fire('prompt.submit', {text: fixture.newText});
phase = 'second-context';
await fire('prompt.context', {blocks: []});
process.stdout.write(JSON.stringify({calls}));
'''


def test_modern_plugin_second_session_does_not_inherit_first_goal_or_work(tmp_path):
    """Ratified session isolation: register() can service two session.start events.

    Mutation: retain originalGoal/recentWork across session.start or route the new
    prompt's supervisor window under the previous session ID.
    """
    module = Path.cwd() / "src/kindex/claude_modern/hooks/kindex.ts"
    assert module.is_file(), "Validator must invoke this test from the candidate repository root"
    home = tmp_path / "isolated-home"
    home.mkdir()
    project = tmp_path / "synthetic-project"
    project.mkdir()
    names = ("PATH", "PYTHONPATH", "PYTHONHOME", "TMPDIR", "SYSTEMROOT", "LANG", "LC_ALL", "TZ")
    env = {name: os.environ[name] for name in names if name in os.environ}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"),
               XDG_DATA_HOME=str(home / ".local/share"), CODEX_HOME=str(home / ".codex"),
               CLAUDE_CONFIG_DIR=str(home / ".claude"), CURSOR_CONFIG_DIR=str(home / ".cursor"),
               KIN_HEALTH_DIR=str(tmp_path / "isolated-health"))
    node = shutil.which("node", path=env.get("PATH"))
    assert node is not None, "Node >=24 is a required fixture prerequisite; this test is not skipped"
    version = subprocess.run([node, "--version"], capture_output=True, text=True, env=env)
    assert version.returncode == 0, version.stderr
    assert int(version.stdout.strip().lstrip("v").split(".")[0]) >= 24, "Node >=24 is required for built-in TypeScript stripping"
    old_goal = "OLD_GOAL_CANARY preserve legacy invoice history"
    old_work = "OLD_WORK_CANARY investigating the discarded parser branch"
    new_goal = "NEW_GOAL_CANARY repair reservation validation"
    new_work = "NEW_WORK_CANARY verify the corrected reservation fixture"
    fixture = {"module": str(module), "project": str(project),
               "firstSession": "modern-session-alpha", "secondSession": "modern-session-beta",
               "oldText": old_goal + "\n" + old_work, "newText": new_goal + "\n" + new_work}
    driver = "const fixture = " + json.dumps(fixture) + ";\n" + MODERN_DRIVER
    result = subprocess.run([node, "--input-type=module"], input=driver, text=True,
                            capture_output=True, env=env, cwd=project, timeout=20)
    assert result.returncode == 0, result.stderr
    calls = json.loads(result.stdout)["calls"]
    supervisors = [call for call in calls if isinstance(call["request"], dict)
                   and call["request"].get("action") == "supervisor"]
    old_calls = [call for call in supervisors if call["phase"].startswith("first-")]
    assert old_calls, "Fixture must first populate and exercise the old supervisor window"
    assert any(old_goal in call["request"].get("text", "") and old_work in call["request"].get("text", "") for call in old_calls)
    before_prompt = [call for call in supervisors if call["phase"] == "second-before-prompt"]
    assert all(old_goal not in json.dumps(call["request"]) and old_work not in json.dumps(call["request"]) for call in before_prompt)
    new_calls = [call for call in supervisors if call["phase"] in ("second-prompt", "second-context")]
    assert new_calls, "Second prompt must reach the real plugin's supervisor RPC path"
    for call in new_calls:
        request = call["request"]
        assert request["scope"]["session_id"] == fixture["secondSession"]
        assert new_goal in request["text"]
        assert new_work in request["text"]
        assert old_goal not in json.dumps(request)
        assert old_work not in json.dumps(request)
