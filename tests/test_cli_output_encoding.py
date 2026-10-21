"""The CLI never dies on a character its output encoding cannot carry.

A Windows pipe takes the ANSI code page (cp1252), which has no ✓/✗; a strict
ASCII stream stands in for it on every platform.
"""

import os
import subprocess
import sys


def test_an_unencodable_character_is_replaced_not_fatal():
    script = (
        "import sys\n"
        "from kindex import cli\n"
        "sys.argv = ['kin', '--help']\n"
        "try:\n"
        "    cli.main()\n"
        "except SystemExit:\n"
        "    pass\n"
        "print('\\u2717 done')\n"
    )
    env = dict(os.environ, PYTHONIOENCODING="ascii:strict")
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, env=env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == b"? done"
