#!/usr/bin/env python3
"""Refuse a release tag that does not name the committed project version.

The tag used to be rendered into ``pyproject.toml`` at build time, so a tag
ahead of the committed version published a wheel whose own ``__version__``,
registry metadata and changelog still named the previous release. The
committed surfaces are the release; the tag must agree with them.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path


def check_release_version(tag: str, pyproject_path: Path) -> str:
    """Return the version a ``vX.Y.Z`` tag names, if it is the project's."""
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
        raise ValueError(f"Release tag must use the vX.Y.Z format, got {tag!r}")
    version = tag[1:]
    committed = tomllib.loads(pyproject_path.read_text())["project"]["version"]
    if committed != version:
        raise ValueError(
            f"Release tag {tag} does not name the committed project version "
            f"{committed}; bump every release surface (scripts/sync-version.sh) "
            "and tag that commit"
        )
    return version


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: check-release-version.py vX.Y.Z path/to/pyproject.toml")
    try:
        print(check_release_version(sys.argv[1], Path(sys.argv[2])))
    except ValueError as error:
        raise SystemExit(str(error)) from None
