"""Run independent boundary cases without ambient state, network, or scheduler writes."""
import os
from pathlib import Path
import runpy
import sys
import tempfile

# Product code loads only after all ambient user configuration/credentials are gone.
candidate_src = os.environ.get("CANDIDATE_SRC", str(Path(__file__).parents[1] / "src"))
isolated_home = Path(tempfile.mkdtemp(prefix="kindex-boundary-isolated-", dir="/tmp"))
os.environ.clear()
os.environ.update({"HOME": str(isolated_home), "PATH": "/opt/homebrew/bin:/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "CANDIDATE_SRC": candidate_src, "KINDEX_BOUNDARY_ISOLATED_HOME": str(isolated_home), "KIN_NO_SCHEDULER_WRITES": "1"})
for label in ("CONFIG", "DATA", "STATE"):
    directory = isolated_home / label.lower()
    directory.mkdir()
    os.environ["XDG_" + label + "_HOME"] = str(directory)

# Audit denial is an execution guard, not a substitute for any product boundary.
# No credentials are inherited by the sterile launcher. Git is local-fixture-only.
def deny_network(event, args):
    if event.startswith('socket.') or event in ('os.system', 'os.posix_spawn', 'os.posix_spawnp'):
        raise RuntimeError('Independent test forbids network/process escape: ' + event)
    if event == 'subprocess.Popen':
        executable, argv = args[:2]
        if Path(str(executable)).name != 'git':
            raise RuntimeError('Independent test forbids external process: ' + str(executable))
        git_args = list(argv[1:])
        while git_args and git_args[0].startswith('-'):
            option = git_args.pop(0)
            if option == '-C' and git_args:
                git_args.pop(0)
            elif option != '--literal-pathspecs':
                raise RuntimeError('Independent test forbids git option: ' + option)
        if not git_args or git_args[0] not in ('init', 'rev-parse', 'ls-files', 'clone', 'worktree'):
            raise RuntimeError('Independent test forbids git action: ' + str(argv))
        if git_args[0] == 'clone' and not Path(git_args[-2]).is_dir():
            raise RuntimeError('Independent test permits local clone only')
        if git_args[0] == 'worktree' and git_args[1] != 'add':
            raise RuntimeError('Independent test permits worktree add only')
sys.dont_write_bytecode = True
sys.addaudithook(deny_network)
sys.path.insert(0, os.environ['CANDIDATE_SRC'])
sys.path.insert(0, str(Path(__file__).parent))
if len(sys.argv) == 1:
    sys.argv.extend(['-v', 'test_established_projects'])
runpy.run_module('unittest', run_name='__main__')
