"""Cross-platform file primitives: no-follow opens and exclusive advisory locks.

POSIX keeps ``O_NOFOLLOW`` and ``flock``. Windows has neither, so:

* ``open_nofollow`` refuses a symlink or junction reparse point at the
  final component, checked before opening and again against the opened handle,
  so a link swapped in between the two checks is refused too. The error is the
  same ``ELOOP`` that ``O_NOFOLLOW`` raises on Linux.
* ``lock`` takes a one-byte ``msvcrt`` lock far past any data the file holds.
  Windows byte-range locks are mandatory; locking byte 0 would block ordinary
  reads and writes of a lock file that also carries data.

Like ``flock``, the lock is per open descriptor, exclusive, and released when
the descriptor is closed or the process exits.

``fsync_directory`` makes a rename durable on POSIX. Windows cannot open a
directory with ``os.open``; NTFS journals the rename itself, so it is a no-op.

``home`` and ``expanduser`` resolve ``~`` like ``Path.home`` and
``os.path.expanduser`` on POSIX. On Windows, Python ignores ``HOME`` and reads
``USERPROFILE``; these honor ``HOME`` first, as git does, so Git Bash/MSYS
users (and anything that isolates a run by setting ``HOME``) get the same home
on every platform. An MSYS-style ``HOME=/c/Users/me`` is read as ``C:/Users/me``.

``null_device`` maps the conventional ``/dev/null`` (e.g. ``--config /dev/null``
for "no config") to Windows' ``nul`` when a program, not a shell, passes it.

``kill_tree`` is ``killpg(pid, SIGKILL)`` for a child started in its own
session; Windows has no process groups, so it kills the process tree with
``taskkill /T /F``. Both raise ``ProcessLookupError`` when nothing is left.

``essential_environment`` is what a minimal child environment must still carry:
nothing on POSIX; on Windows the system (never per-user) variables without
which a process cannot start its socket layer or find its shell. A closed
environment stays closed: no user profile, app-data or temp directories.

``posix_shell`` is the ``sh`` a shell command line runs under. Commands in the
config (``sim.command``, reminder actions) are POSIX shell, split with
``shlex``; on Windows they run under Git for Windows' ``sh.exe`` (the one
beside the ``git`` on PATH, else from its install record), never a bare
``bash``/``sh`` from PATH, which can be WSL's. None means ``cmd.exe``.
"""

from __future__ import annotations

import errno
import os
import re
import shutil
import signal
import stat
import time
from pathlib import Path, PurePath

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

__all__ = ["open_nofollow", "lock", "unlock", "fsync_directory", "home", "expanduser", "null_device", "kill_tree", "essential_environment", "posix_shell", "shell_can_run"]

#: The Windows branches below test this flag, not ``os.name``, so tests can
#: exercise them on POSIX without patching ``os.name`` (which makes pathlib
#: build WindowsPath objects that POSIX cannot instantiate).
WINDOWS = os.name == "nt"

_LOCK_OFFSET = 0x7FFFFFFF  # one byte past any realistic lock-file payload
_RETRY_SECONDS = 0.01


# Reparse points that redirect the path: symbolic links and junctions. Cloud
# placeholders (OneDrive) and deduplicated files are reparse points too, but
# they hold the file's own data and must still open.
_LINK_REPARSE_TAGS = (0xA000000C, 0xA0000003)  # IO_REPARSE_TAG_SYMLINK, IO_REPARSE_TAG_MOUNT_POINT


def _is_link(info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return (bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
            and getattr(info, "st_reparse_tag", 0) in _LINK_REPARSE_TAGS)


def _refuse(path) -> OSError:
    return OSError(errno.ELOOP, "Refusing to follow a symbolic link", str(path))


def open_nofollow(path, flags: int, mode: int = 0o777) -> int:
    """``os.open(path, flags | O_NOFOLLOW, mode)`` that also refuses links on Windows."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        return os.open(path, flags | nofollow, mode)
    try:
        if _is_link(os.lstat(path)):
            raise _refuse(path)
    except FileNotFoundError:
        pass
    fd = os.open(path, flags | getattr(os, "O_BINARY", 0), mode)
    try:
        current = os.lstat(path)
        if _is_link(current) or not os.path.samestat(current, os.fstat(fd)):
            raise _refuse(path)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _locking(fd: int, mode: int) -> None:
    position = os.lseek(fd, 0, os.SEEK_CUR)
    os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
    try:
        msvcrt.locking(fd, mode, 1)
    finally:
        os.lseek(fd, position, os.SEEK_SET)


def lock(fd: int, *, blocking: bool = True) -> None:
    """Exclusive lock on ``fd``. Non-blocking raises ``BlockingIOError`` while another holder has it."""
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return
    while True:
        try:
            _locking(fd, msvcrt.LK_NBLCK)
            return
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EDEADLK, errno.EAGAIN):
                raise
            if not blocking:
                raise BlockingIOError(errno.EAGAIN, "Lock is held by another descriptor") from None
            time.sleep(_RETRY_SECONDS)


def unlock(fd: int) -> None:
    """Release a lock taken with ``lock``. Closing the descriptor also releases it."""
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    _locking(fd, msvcrt.LK_UNLCK)


def fsync_directory(path) -> None:
    """Flush a directory entry (e.g. after ``os.replace``) where the platform supports it."""
    if WINDOWS:
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _windows_home() -> str | None:
    value = os.environ.get("HOME") if WINDOWS else None
    if not value:
        return None
    msys = re.match(r"^/([A-Za-z])(?=/|$)", value)
    return f"{msys.group(1).upper()}:{value[2:] or '/'}" if msys else value


def home() -> Path:
    """``Path.home()``, except that on Windows a set ``HOME`` wins over ``USERPROFILE``."""
    value = _windows_home()
    return Path(value) if value else Path.home()


def expanduser(path):
    """``os.path.expanduser`` that follows ``home()``; returns the same type it was given."""
    text = os.fspath(path)
    value = _windows_home()
    if value and (text == "~" or text[:2] in ("~/", "~\\")):
        text = value + text[1:]
    else:
        text = os.path.expanduser(text)
    return type(path)(text) if isinstance(path, PurePath) else text


def null_device(path):
    """``os.devnull`` for ``/dev/null`` on Windows; any other path, and every path on POSIX, unchanged."""
    if WINDOWS and os.fspath(path).replace("\\", "/") == "/dev/null":
        return type(path)(os.devnull) if isinstance(path, PurePath) else os.devnull
    return path


def kill_tree(pid: int) -> None:
    """Kill ``pid`` and its descendants (its process group on POSIX)."""
    if not WINDOWS:
        os.killpg(pid, signal.SIGKILL)
        return
    # Imported here: config imports this module, and a test that stands in a
    # fake msvcrt would otherwise make subprocess import _winapi on POSIX.
    import subprocess

    result = subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                            capture_output=True, stdin=subprocess.DEVNULL)
    if result.returncode == 128:  # no such process
        raise ProcessLookupError(errno.ESRCH, "No such process", pid)
    if result.returncode:
        raise PermissionError(errno.EPERM, result.stderr.decode(errors="replace").strip() or "taskkill failed", pid)


_WINDOWS_ESSENTIAL = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT")


def essential_environment() -> dict[str, str]:
    """The variables a minimal child environment must keep on this platform (none on POSIX)."""
    if not WINDOWS:
        return {}
    return {key: os.environ[key] for key in _WINDOWS_ESSENTIAL if key in os.environ}


def _git_install_roots() -> list[Path]:
    roots = []
    git = shutil.which("git")
    if git:
        # Git\cmd\git.exe, Git\mingw64\bin\git.exe or Git\ucrt64\bin\git.exe
        roots.extend(list(Path(git).resolve().parents)[1:3])
    try:
        import winreg
    except ImportError:  # POSIX
        return roots
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\GitForWindows") as key:
                roots.append(Path(winreg.QueryValueEx(key, "InstallPath")[0]))
        except OSError:
            continue
    return roots


def shell_can_run(program: str) -> bool:
    """Whether a shell command line can start ``program``: ``shutil.which``,
    except that on Windows Git's sh also runs a script given by path (its
    shebang), which ``which`` refuses for lacking a PATHEXT extension."""
    if shutil.which(program):
        return True
    return (WINDOWS and os.path.dirname(program) != "" and os.path.isfile(program)
            and posix_shell() is not None)


def posix_shell() -> str | None:
    """``/bin/sh`` on POSIX; Git for Windows' ``sh.exe`` on Windows, or None when Git is absent."""
    if not WINDOWS:
        return "/bin/sh"
    for root in _git_install_roots():
        candidate = root / "usr" / "bin" / "sh.exe"
        if candidate.is_file():
            return str(candidate)
    return None
