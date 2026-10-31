"""Cross-platform no-follow opens and advisory locks (kindex.platform_io)."""

import ast
import contextlib
import errno
import os
import stat
import sys
import types
from pathlib import Path

import pytest

from kindex import platform_io


def _link_or_fake(monkeypatch, link, target):
    """A real symlink where the platform allows it; otherwise lstat reports one."""
    try:
        os.symlink(target, link)
        return
    except (OSError, NotImplementedError):
        pass
    link.write_bytes(b"")
    real_lstat = os.lstat

    def fake_lstat(path, *args, **kwargs):
        info = real_lstat(path, *args, **kwargs)
        if os.fspath(path) == os.fspath(link):
            return os.stat_result((stat.S_IFLNK | 0o777,) + tuple(info)[1:])
        return info

    monkeypatch.setattr(os, "lstat", fake_lstat)


def test_open_nofollow_creates_and_reopens_regular_file(tmp_path):
    path = tmp_path / "plain.lock"
    fd = platform_io.open_nofollow(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.write(fd, b"data")
    finally:
        os.close(fd)
    fd = platform_io.open_nofollow(path, os.O_RDONLY)
    try:
        assert os.read(fd, 4) == b"data"
    finally:
        os.close(fd)


def test_open_nofollow_refuses_final_component_link(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.write_bytes(b"secret")
    link = tmp_path / "link"
    _link_or_fake(monkeypatch, link, target)
    with pytest.raises(OSError) as raised:
        platform_io.open_nofollow(link, os.O_RDWR | os.O_APPEND)
    assert raised.value.errno == errno.ELOOP
    assert target.read_bytes() == b"secret"


def test_open_nofollow_refuses_link_swapped_in_after_the_first_check(tmp_path, monkeypatch):
    if getattr(os, "O_NOFOLLOW", 0):
        # The kernel checks atomically; the race only exists for the Windows path.
        monkeypatch.delattr(os, "O_NOFOLLOW")
    path = tmp_path / "raced"
    path.write_bytes(b"")
    real_lstat = os.lstat
    calls = []
    opened = []
    real_open = os.open

    def lstat_after_swap(target, *args, **kwargs):
        info = real_lstat(target, *args, **kwargs)
        calls.append(target)
        if len(calls) > 1:
            return os.stat_result((stat.S_IFLNK | 0o777,) + tuple(info)[1:])
        return info

    def recording_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(os, "lstat", lstat_after_swap)
    monkeypatch.setattr(os, "open", recording_open)
    with pytest.raises(OSError) as raised:
        platform_io.open_nofollow(path, os.O_RDWR)
    assert raised.value.errno == errno.ELOOP
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])  # the descriptor of the refused open was closed


@pytest.mark.parametrize("tag, is_link", [
    (0xA000000C, True),   # IO_REPARSE_TAG_SYMLINK
    (0xA0000003, True),   # IO_REPARSE_TAG_MOUNT_POINT (junction)
    (0x9000001A, False),  # IO_REPARSE_TAG_CLOUD (OneDrive placeholder)
    (0x80000013, False),  # IO_REPARSE_TAG_DEDUP
])
def test_only_redirecting_reparse_points_count_as_links(tag, is_link):
    info = types.SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_reparse_tag=tag,
                                 st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
    assert platform_io._is_link(info) is is_link


def test_lock_is_exclusive_between_descriptors_and_released_by_unlock(tmp_path):
    path = tmp_path / "x.lock"
    first = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    second = os.open(path, os.O_RDWR)
    try:
        platform_io.lock(first)
        with pytest.raises(BlockingIOError):
            platform_io.lock(second, blocking=False)
        platform_io.unlock(first)
        platform_io.lock(second, blocking=False)
        platform_io.unlock(second)
    finally:
        os.close(first)
        os.close(second)


def test_lock_is_released_when_the_descriptor_closes(tmp_path):
    path = tmp_path / "x.lock"
    first = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    second = os.open(path, os.O_RDWR)
    try:
        platform_io.lock(first, blocking=False)
        os.close(first)
        first = None
        platform_io.lock(second, blocking=False)
    finally:
        if first is not None:
            os.close(first)
        os.close(second)


def test_held_lock_does_not_block_reading_or_writing_the_file(tmp_path):
    path = tmp_path / "ledger"
    holder = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    writer = os.open(path, os.O_RDWR | os.O_APPEND | getattr(os, "O_BINARY", 0))
    try:
        platform_io.lock(holder)
        assert os.write(writer, b"line\n") == 5
        assert path.read_bytes() == b"line\n"
    finally:
        os.close(holder)
        os.close(writer)


def test_fsync_directory_after_replace(tmp_path):
    temporary = tmp_path / "state.tmp"
    temporary.write_bytes(b"{}")
    os.replace(temporary, tmp_path / "state.json")
    platform_io.fsync_directory(tmp_path)
    assert (tmp_path / "state.json").read_bytes() == b"{}"
    assert not temporary.exists()


def test_home_follows_HOME_even_where_python_reads_USERPROFILE(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "profile"))
    assert platform_io.home() == tmp_path / "home"
    assert platform_io.expanduser("~/.kindex") == str(tmp_path / "home") + "/.kindex"
    assert platform_io.expanduser(Path("~") / ".kindex") == tmp_path / "home" / ".kindex"
    assert isinstance(platform_io.expanduser(Path("~")), Path)
    assert isinstance(platform_io.expanduser("~"), str)


def test_paths_without_a_leading_tilde_are_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert platform_io.expanduser("relative/~/x") == "relative/~/x"
    assert platform_io.expanduser(Path("a") / "b") == Path("a") / "b"


def test_windows_reads_an_msys_HOME(monkeypatch):
    monkeypatch.setattr(platform_io, "WINDOWS", True)
    monkeypatch.setenv("HOME", "/c/Users/me")
    assert platform_io.home() == Path("C:/Users/me")
    assert platform_io.expanduser("~/.kindex") == "C:/Users/me/.kindex"
    monkeypatch.setenv("HOME", "/d")
    assert platform_io.home() == Path("D:/")


def test_unset_HOME_falls_back_to_the_platform_home(monkeypatch):
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setattr(platform_io, "WINDOWS", True)
    assert platform_io.home() == Path.home()


def test_kindex_resolves_home_only_through_platform_io():
    """Path.home() and expanduser ignore HOME on Windows; every caller goes through platform_io."""
    package = Path(platform_io.__file__).parent
    offenders = []
    for path in sorted(package.rglob("*.py")):
        if path.name == "platform_io.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            func = node.func
            if func.attr == "expanduser" and not (isinstance(func.value, ast.Name) and func.value.id == "platform_io"):
                offenders.append(f"{path.relative_to(package)}:{node.lineno} expanduser")
            elif func.attr == "home" and isinstance(func.value, ast.Name) and func.value.id == "Path":
                offenders.append(f"{path.relative_to(package)}:{node.lineno} Path.home()")
    assert offenders == []


def test_null_device_maps_dev_null_on_windows_only(monkeypatch):
    monkeypatch.setattr(platform_io, "WINDOWS", True)
    assert platform_io.null_device("/dev/null") == os.devnull
    assert platform_io.null_device(Path("/dev/null")) == Path(os.devnull)
    assert platform_io.null_device("config.yaml") == "config.yaml"
    monkeypatch.setattr(platform_io, "WINDOWS", False)
    assert platform_io.null_device("/dev/null") == "/dev/null"


class _Registry:
    """A stand-in ``winreg`` holding at most a GitForWindows InstallPath."""
    HKEY_LOCAL_MACHINE, HKEY_CURRENT_USER = "HKLM", "HKCU"

    def __init__(self, install_path=None):
        self.install_path = install_path

    def OpenKey(self, hive, name):
        if self.install_path is None or hive != self.HKEY_LOCAL_MACHINE or name != r"SOFTWARE\GitForWindows":
            raise FileNotFoundError(name)
        return contextlib.nullcontext(name)

    def QueryValueEx(self, key, value):
        assert value == "InstallPath"
        return str(self.install_path), 1


def _git_tree(root, *tools):
    for tool in tools:
        (root / tool).parent.mkdir(parents=True, exist_ok=True)
        (root / tool).write_bytes(b"")
    return root


def test_posix_shell_is_bin_sh_on_posix(monkeypatch):
    monkeypatch.setattr(platform_io, "WINDOWS", False)
    assert platform_io.posix_shell() == "/bin/sh"


@pytest.mark.parametrize("git", ["cmd/git.exe", "mingw64/bin/git.exe", "ucrt64/bin/git.exe"])
def test_posix_shell_is_the_sh_of_the_git_on_path(tmp_path, monkeypatch, git):
    install = _git_tree(tmp_path / "Git", git, "usr/bin/sh.exe")
    monkeypatch.setattr(platform_io, "WINDOWS", True)
    monkeypatch.setattr(platform_io.shutil, "which", lambda name: str(install / git) if name == "git" else None)
    monkeypatch.setitem(sys.modules, "winreg", _Registry())
    assert platform_io.posix_shell() == str((install / "usr/bin/sh.exe").resolve())


def test_posix_shell_falls_back_to_the_git_install_record(tmp_path, monkeypatch):
    install = _git_tree(tmp_path / "Git", "usr/bin/sh.exe")
    monkeypatch.setattr(platform_io, "WINDOWS", True)
    monkeypatch.setattr(platform_io.shutil, "which", lambda name: None)
    monkeypatch.setitem(sys.modules, "winreg", _Registry(install))
    assert platform_io.posix_shell() == str(install / "usr/bin/sh.exe")


def test_shell_can_run_a_script_by_path_that_which_refuses_on_windows(tmp_path, monkeypatch):
    script = tmp_path / "provider"
    script.write_text("#!/bin/sh\ncat\n")
    monkeypatch.setattr(platform_io.shutil, "which", lambda name: None)
    monkeypatch.setattr(platform_io, "posix_shell", lambda: "sh.exe")
    monkeypatch.setattr(platform_io, "WINDOWS", True)
    assert platform_io.shell_can_run(str(script))
    assert not platform_io.shell_can_run("provider")  # a bare name still needs PATH
    assert not platform_io.shell_can_run(str(tmp_path / "missing"))
    monkeypatch.setattr(platform_io, "posix_shell", lambda: None)
    assert not platform_io.shell_can_run(str(script))  # cmd.exe cannot run it
    monkeypatch.setattr(platform_io, "WINDOWS", False)
    monkeypatch.setattr(platform_io, "posix_shell", lambda: "/bin/sh")
    assert not platform_io.shell_can_run(str(script))  # POSIX keeps which's verdict


def test_posix_shell_never_takes_a_bare_sh_or_bash_from_path(tmp_path, monkeypatch):
    # WSL puts bash.exe in System32; only Git's own sh.exe qualifies.
    loose = _git_tree(tmp_path / "System32", "bash.exe", "sh.exe")
    monkeypatch.setattr(platform_io, "WINDOWS", True)
    monkeypatch.setattr(platform_io.shutil, "which", lambda name: None if name == "git" else str(loose / f"{name}.exe"))
    monkeypatch.setitem(sys.modules, "winreg", _Registry())
    assert platform_io.posix_shell() is None
