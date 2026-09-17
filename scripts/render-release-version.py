#!/usr/bin/env python3
"""Render the package version from a release tag into ``pyproject.toml``."""

from __future__ import annotations

import re
import sys
from pathlib import Path


def render_release_version(tag: str, pyproject_path: Path) -> str:
    """Replace the project version with the version encoded by a ``v`` tag."""
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
        raise ValueError(f"Release tag must use the vX.Y.Z format, got {tag!r}")

    version = tag[1:]
    pyproject = pyproject_path.read_text()
    rendered, replacements = re.subn(
        r'^version = "[^"]+"$',
        f'version = "{version}"',
        pyproject,
        count=1,
        flags=re.MULTILINE,
    )
    if replacements != 1:
        raise ValueError(f"Could not find exactly one project version in {pyproject_path}")

    pyproject_path.write_text(rendered)
    return version


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: render-release-version.py vX.Y.Z path/to/pyproject.toml")

    print(render_release_version(sys.argv[1], Path(sys.argv[2])))
