#!/usr/bin/env python3
"""Times `kin ask`, MCP `ask`, conversation ingest and `kin digest`, for two
Kindex source trees on one LoCoMo conversation.

The conversation's sessions are written as conversation files and stored with
the new tree's `kin ingest conversations` (main has no equivalent); each tree
then works on its own copy of that store. MCP `ask` is timed in-process with no
model call (the new tool makes none unless answer=True). `kin ask` runs as a
command per question with the configured model, as a user would run it, so
its time includes interpreter start-up and the provider's latency. Search is
full-text only (no embedding provider is configured), the same for both.

  git worktree add ../kindex-main origin/main
  python scripts/bench_ask_speed.py --old-src ../kindex-main/src --new-src src \\
      --locomo locomo10.json --questions 20 --key-env OPENAI_API_KEY --out speed.json

The report gives the platform, the Python version, both trees' commits, the
workload's SHA-256, per-question timing distributions, and the model tokens
each side recorded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MCP_DRIVER = r"""
import json, sys, time
from kindex.config import Config
from kindex.store import Store
import kindex.mcp_server as server
data_dir, questions = sys.argv[1], json.load(open(sys.argv[2]))
config = Config(data_dir=data_dir)
store = Store(config)
server._store, server._config = store, config
server.ask(questions[0])  # warm caches and imports
times, sizes = [], []
for question in questions:
    t0 = time.perf_counter()
    out = server.ask(question)
    times.append(time.perf_counter() - t0)
    sizes.append(len(out))
print(json.dumps({"seconds": times, "chars": sizes}))
"""


def summary(values: list[float], digits: int = 3) -> dict:
    values = sorted(values)
    p90 = values[min(len(values) - 1, round(0.9 * (len(values) - 1)))]
    return {"n": len(values), "min": round(values[0], digits), "median": round(statistics.median(values), digits),
            "p90": round(p90, digits), "max": round(values[-1], digits)}


def revision(src: Path) -> str:
    out = subprocess.run(["git", "-C", str(src), "rev-parse", "--short", "HEAD"], capture_output=True, text=True)
    return out.stdout.strip() or "unknown"


def conversation_files(locomo: Path, index: int, out: Path) -> tuple[list[dict], str]:
    item = json.loads(locomo.read_text())[index]
    conv = item["conversation"]
    out.mkdir(parents=True)
    written = []
    n = 1
    while f"session_{n}" in conv:
        record = {"id": f"session_{n}", "date": conv.get(f"session_{n}_date_time"),
                  "messages": [{"role": "user", "name": turn["speaker"], "content": turn["text"]}
                               for turn in conv[f"session_{n}"]]}
        (out / f"session_{n:03d}.json").write_text(json.dumps(record))
        written.append(record)
        n += 1
    questions = [qa["question"] for qa in item["qa"] if qa.get("category") != 5]
    digest = hashlib.sha256(json.dumps(written, sort_keys=True).encode()).hexdigest()
    return questions, digest


def kin(src: Path, args: list[str], env: dict) -> tuple[float, subprocess.CompletedProcess]:
    env = {**env, "PYTHONPATH": str(src)}
    t0 = time.perf_counter()
    done = subprocess.run([sys.executable, "-m", "kindex.cli", *args], env=env, capture_output=True, text=True)
    return time.perf_counter() - t0, done


def ledger_tokens(data_dir: Path) -> dict:
    import yaml

    totals = {"calls": 0, "tokens_in": 0, "tokens_out": 0}
    path = data_dir / "budget.yaml"
    if not path.exists():
        return totals

    def walk(value):
        if isinstance(value, dict):
            if "tokens_in" in value:
                totals["calls"] += 1
                totals["tokens_in"] += int(value.get("tokens_in") or 0)
                totals["tokens_out"] += int(value.get("tokens_out") or 0)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(yaml.safe_load(path.read_text()))
    return totals


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--old-src", type=Path, required=True)
    ap.add_argument("--new-src", type=Path, required=True)
    ap.add_argument("--locomo", type=Path, required=True)
    ap.add_argument("--conversation", type=int, default=0)
    ap.add_argument("--questions", type=int, default=20)
    ap.add_argument("--model", default="gpt-6-luna")
    ap.add_argument("--key-env", default="OPENAI_API_KEY", help="environment variable holding the OpenAI key")
    ap.add_argument("--no-llm", action="store_true", help="skip `kin ask` and `kin digest`")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    old, new = a.old_src.resolve(), a.new_src.resolve()
    report: dict = {
        "machine": {"system": platform.system(), "release": platform.release(), "machine": platform.machine(),
                    "cores": os.cpu_count(), "python": platform.python_version()},
        "commits": {"old": revision(old), "new": revision(new)},
    }
    with tempfile.TemporaryDirectory(prefix="kindex-speed-") as tmp:
        root = Path(tmp)
        questions, workload = conversation_files(a.locomo, a.conversation, root / "conversations")
        questions = questions[: a.questions]
        (root / "questions.json").write_text(json.dumps(questions))
        report["workload"] = {"locomo_conversation": a.conversation, "sha256": workload,
                              "sessions": len(list((root / "conversations").iterdir())),
                              "questions": len(questions),
                              "questions_sha256": hashlib.sha256(json.dumps(questions).encode()).hexdigest()}
        config = root / "kin.yaml"
        config.write_text(json.dumps({
            "llm": {"enabled": not a.no_llm, "provider": "openai", "model": a.model, "api_key_env": a.key_env},
            # Kindex costs a model it has no price for at the highest known rate;
            # the cap must not cut the measurement short.
            "budget": {"daily": 10000, "weekly": 10000, "monthly": 10000},
        }))
        env = {k: v for k, v in os.environ.items() if not k.startswith("KIN")}
        env["HOME"] = str(root / "home")
        env["XDG_CONFIG_HOME"] = str(root / "home" / ".config")

        store = root / "store"
        seconds, done = kin(new, ["ingest", "conversations", "--directory", str(root / "conversations"),
                                  "--data-dir", str(store), "--config", str(config)], env)
        if done.returncode:
            raise SystemExit(f"ingest failed: {done.stderr[-2000:]}")
        report["ingest_new"] = {"seconds": round(seconds, 3)}
        copies = {}
        for name in ("old", "new"):
            copies[name] = root / f"store-{name}"
            shutil.copytree(store, copies[name])

        report["mcp_ask"] = {}
        for name, src in (("old", old), ("new", new)):
            done = subprocess.run([sys.executable, "-c", MCP_DRIVER, str(copies[name]), str(root / "questions.json")],
                                  env={**env, "PYTHONPATH": str(src)}, capture_output=True, text=True)
            if done.returncode:
                raise SystemExit(f"MCP ask ({name}) failed: {done.stderr[-2000:]}")
            measured = json.loads(done.stdout.strip().splitlines()[-1])
            report["mcp_ask"][name] = {"seconds": summary(measured["seconds"], 4),
                                       "output_chars": summary(measured["chars"], 0)}

        if not a.no_llm:
            report["kin_ask"] = {}
            # Alternate the trees per question so provider drift hits both alike.
            times: dict[str, list[float]] = {"old": [], "new": []}
            empty = {"old": 0, "new": 0}
            for question in questions:
                for name, src in (("old", old), ("new", new)):
                    seconds, done = kin(src, ["ask", "--data-dir", str(copies[name]), "--config", str(config),
                                              "--", question], env)
                    times[name].append(seconds)
                    empty[name] += not done.stdout.strip()
            for name in ("old", "new"):
                report["kin_ask"][name] = {"seconds": summary(times[name], 2), "empty_answers": empty[name],
                                           "model": ledger_tokens(copies[name])}
            seconds, done = kin(new, ["digest", "--data-dir", str(copies["new"]), "--config", str(config)], env)
            report["digest_new"] = {"seconds": round(seconds, 2), "output": done.stdout.strip()[-200:]}
    text = json.dumps(report, indent=2)
    print(text)
    if a.out:
        a.out.write_text(text + "\n")


if __name__ == "__main__":
    main()
