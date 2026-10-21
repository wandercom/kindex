"""The _run_process contract with commands that run on every platform.

test_reminder_action_safety.py states the same contract in POSIX sh; these
use the running interpreter instead of a shell, so Windows exercises it too.
"""

import os
import shlex
import subprocess
import sys
import time

from kindex import platform_io
from kindex.actions import _run_process


def _alive(pid: int) -> bool:
    if os.name == "nt":
        listed = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                                capture_output=True, text=True).stdout
        return f'"{pid}"' in listed
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _gone_within(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return not _alive(pid)


def _spawner(pidfile, *, then: str) -> str:
    return (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(child.pid))\n"
        f"{then}\n"
    )


def test_a_timed_out_run_takes_its_children_with_it(tmp_path):
    pidfile = tmp_path / "child.pid"
    started = time.monotonic()
    returncode, _, _ = _run_process([sys.executable, "-c", _spawner(pidfile, then="time.sleep(60)")], timeout=3)
    assert returncode is None
    assert time.monotonic() - started < 20
    assert _gone_within(int(pidfile.read_text()), 10)


def test_a_shell_command_line_is_posix_shell():
    # The config's commands are split with shlex; a quoted Windows path must run too.
    words = ["a b", "$HOME", "*", "@words", "C:\\\\Users\\\\me 'q' \"dq\"\nnext"]
    command = shlex.join([sys.executable, "-c", "import sys; print(repr(sys.argv[1:]))", *words]) + " | cat"
    returncode, stdout, _ = _run_process(command, shell=True, timeout=30)
    assert returncode == 0
    assert stdout.strip() == repr(words)


def test_a_shell_command_line_keeps_its_exit_status():
    returncode, stdout, _ = _run_process("echo one; exit 7; echo two", shell=True, timeout=30)
    assert returncode == 7
    assert stdout.strip() == "one"


def test_a_timed_out_shell_command_takes_its_children_with_it(tmp_path):
    pidfile = tmp_path / "child.pid"
    command = shlex.join([sys.executable, "-c", _spawner(pidfile, then="time.sleep(60)")])
    returncode, _, _ = _run_process(command + "; true", shell=True, timeout=3)
    assert returncode is None
    assert _gone_within(int(pidfile.read_text()), 10)


def test_a_command_that_leaves_a_background_child_finishes(tmp_path):
    pidfile = tmp_path / "child.pid"
    started = time.monotonic()
    returncode, stdout, _ = _run_process(
        [sys.executable, "-c", _spawner(pidfile, then="print('started', flush=True)")], timeout=30, grace=1)
    try:
        assert returncode == 0
        assert "started" in stdout
        assert time.monotonic() - started < 15
    finally:
        try:
            platform_io.kill_tree(int(pidfile.read_text()))
        except (ProcessLookupError, PermissionError):
            pass


def test_a_large_prompt_does_not_deadlock_a_chatty_child():
    script = ("import sys; sys.stdout.write('x' * 200000); sys.stdout.flush(); "
              "data = sys.stdin.read(); print(len(data))")
    started = time.monotonic()
    returncode, stdout, _ = _run_process([sys.executable, "-c", script], input_text="y" * 300000, timeout=30)
    assert returncode == 0
    assert stdout.endswith("300000\n") or stdout.endswith("300000\r\n")
    assert time.monotonic() - started < 15


def test_max_bytes_keeps_the_head_and_still_drains_the_rest():
    script = "import sys; sys.stdout.write('a' * 100 + 'b' * 1000000); sys.stdout.flush()"
    returncode, stdout, _ = _run_process([sys.executable, "-c", script], timeout=30, max_bytes=100)
    assert returncode == 0
    assert stdout == "a" * 100


def test_undecodable_output_is_not_a_failure():
    script = "import sys; sys.stdout.buffer.write(b'done\\xff\\n'); sys.stdout.flush()"
    returncode, stdout, _ = _run_process([sys.executable, "-c", script], timeout=30)
    assert returncode == 0
    assert stdout.startswith("done�")
