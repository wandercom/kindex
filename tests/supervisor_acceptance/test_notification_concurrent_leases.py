"""Independent regression: concurrent checkers cannot duplicate an in-flight alert.

Oracle: parent-ratified 15-second transport lease boundary and N1-N5. The two
synthetic native calls are deliberately slow; no product code was inspected.
"""
import json
from pathlib import Path
import subprocess
import sys
import time

from test_notifications import box, inbox
from test_health import issues


FAKE_NATIVE = r'''
import fcntl, json, pathlib, sys, time
root = pathlib.Path(CONTROL_ROOT)

def publish(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value))
    temporary.replace(path)

with (root / 'lock').open('a+') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    state_path = root / 'state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {'count':0,'active':[]}
    number = state['count'] + 1
    stamp = {'number':number, 'monotonic':time.monotonic(), 'wall':time.time(), 'argv':sys.argv[1:]}
    if state['active']:
        publish(root / ('overlap-' + str(number) + '.json'), {'new':number,'already_active':state['active'][:]})
    state['count'] = number
    state['active'].append(number)
    publish(state_path, state)
    publish(root / ('entered-' + str(number) + '.json'), stamp)
    fcntl.flock(lock, fcntl.LOCK_UN)

exit_code = 0
if number <= 2:
    deadline = time.monotonic() + 9.5
    release = root / ('release-' + str(number))
    while not release.exists():
        if time.monotonic() >= deadline:
            exit_code = 72
            break
        time.sleep(0.01)

with (root / 'lock').open('a+') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    state = json.loads((root / 'state.json').read_text())
    state['active'].remove(number)
    publish(root / 'state.json', state)
    publish(root / ('finished-' + str(number) + '.json'), {'monotonic':time.monotonic(),'exit_code':exit_code})
    fcntl.flock(lock, fcntl.LOCK_UN)
raise SystemExit(exit_code)
'''


def wait_for_file(path, deadline, description):
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists(), "Fixture did not observe " + description
    return json.loads(path.read_text())


def wait_until(boundary, deadline):
    while time.monotonic() < boundary and time.monotonic() < deadline:
        time.sleep(0.02)
    assert time.monotonic() >= boundary, "Fixture deadline expired before the specified lease boundary"


def test_two_checkers_do_not_overlap_submission_of_the_same_occurrence(box):
    """N3 lease regression: later claims need a fresh lease, not check-start expiry.

    First provider call lasts about nine seconds. While the second call remains
    active, another checker starts more than fifteen seconds after the first
    provider entry. Mutation: create every lease from the old checker start time.
    """
    control = box.root / "native-barriers"
    control.mkdir(mode=0o700)
    box.desktop_command.write_text("#!" + sys.executable + "\n" + FAKE_NATIVE.replace("CONTROL_ROOT", repr(str(control))))
    box.desktop_command.chmod(0o700)
    box.failures()
    other_project = box.root / "second-project"
    other_project.mkdir()
    other_scope = {"project_path": str(other_project), "agent": "claude", "session_id": "lease-second-session"}
    box.record("activity", -60, scope=other_scope, active=True, source="explicit")
    box.record("hook", -50, scope=other_scope)
    box.record("use", -40, scope=other_scope, tool="search", initiator="agent", outcome="success")
    for number in range(3):
        box.record("review", -30 + number, scope=other_scope, state="failed")
    prime = box.check(notify=False)
    assert len(issues(prime, "review_failures")) == 2

    command = [sys.executable, "-m", "kindex.supervisor_health", "check", "--notify", "--json"]
    processes = []
    deadline = time.monotonic() + 40
    try:
        first = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, env=box.env)
        processes.append(first)
        first_entry = wait_for_file(control / "entered-1.json", min(deadline, time.monotonic() + 5), "first native-call entry")
        wait_until(first_entry["monotonic"] + 9.0, deadline)
        (control / "release-1").touch()
        second_entry = wait_for_file(control / "entered-2.json", min(deadline, time.monotonic() + 3), "second sequential native-call entry")
        wait_until(first_entry["monotonic"] + 15.4, deadline)
        assert not (control / "finished-2.json").exists(), "Fixture second provider call ended before the lease-crossing probe"
        second = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, env=box.env)
        processes.append(second)
        # Hold the second call through the lease-crossing observation, still below
        # the native provider's ten-second timeout. Elapsed time stages the race;
        # observed overlap and final receipts, not timing, determine the verdict.
        wait_until(second_entry["monotonic"] + 9.0, deadline)
        (control / "release-2").touch()
        reports = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=max(0.1, deadline - time.monotonic()))
            assert process.returncode in (0, 1), stderr
            reports.append(json.loads(stdout))
        assert all(len(issues(report, "review_failures")) == 2 for report in reports)
        assert not list(control.glob("overlap-*.json")), "Two checkers submitted the same two-issue workload concurrently"
        state = json.loads((control / "state.json").read_text())
        assert state["count"] == 2
        assert state["active"] == []
        for number in (1, 2):
            assert json.loads((control / f"finished-{number}.json").read_text())["exit_code"] == 0
        history = inbox(box)
        assert len(history["alerts"]) == 2
        assert all(alert["transports"]["desktop"]["accepted_at"] is not None for alert in history["alerts"])
        assert box.mail() == []
    finally:
        for number in (1, 2):
            (control / f"release-{number}").touch()
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.communicate(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=1)
