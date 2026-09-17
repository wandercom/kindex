#!/usr/bin/env bash
# Pre-commit hook: sync version, tool/command/test counts across all files.
# Install: ln -sf ../../scripts/sync-version.sh .git/hooks/pre-commit

set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"

# ── Gather facts ──────────────────────────────────────────────────────
VERSION=$(grep '^version' "$ROOT/pyproject.toml" | head -1 | sed 's/.*"\(.*\)"/\1/')
# Tools are registered through the _tool guard, which wraps mcp.tool();
# a bare @mcp.tool is still counted if one reappears. `grep -c` prints the
# count and exits 1 when it is zero, so `|| echo 0` used to print a second
# line and the docs sed then failed on it: `|| true` keeps the single count.
MCP_TOOLS=$(grep -cE '^@(_tool|mcp\.tool)\b' "$ROOT/src/kindex/mcp_server.py" || true)
# No published surface shows a test or CLI command count any more; collecting
# the whole suite on every commit to fill one cost seconds for nothing.

# In-place edit that works with BSD and GNU sed alike.
sed_inplace() {
    local pattern="$1" file="$2" tmp
    tmp="$(mktemp "${file}.XXXXXX")"
    sed -E "$pattern" "$file" > "$tmp" && mv "$tmp" "$file"
}

if [ -z "$VERSION" ]; then
    echo "ERROR: Could not read version from pyproject.toml"
    exit 1
fi

echo "sync-version: v${VERSION} | ${MCP_TOOLS} MCP tools"

CHANGED=0

# ── README.md ─────────────────────────────────────────────────────────
if [ -f "$ROOT/README.md" ]; then
    sed_inplace "s/version-[0-9]+\.[0-9]+\.[0-9]+/version-${VERSION}/g" "$ROOT/README.md"
    sed_inplace "s/\[v[0-9]+\.[0-9]+\.[0-9]+\]/[v${VERSION}]/g" "$ROOT/README.md"
    if ! git diff --quiet "$ROOT/README.md"; then
        git add "$ROOT/README.md"
        CHANGED=1
    fi
fi

# ── docs/index.html ──────────────────────────────────────────────────
if [ -f "$ROOT/docs/index.html" ]; then
    # Version badges
    sed_inplace "s/v[0-9]+\.[0-9]+\.[0-9]+/v${VERSION}/g" "$ROOT/docs/index.html"
    # MCP tools count
    sed_inplace "s/[0-9]+ MCP Tools/${MCP_TOOLS} MCP Tools/g" "$ROOT/docs/index.html"
    if ! git diff --quiet "$ROOT/docs/index.html"; then
        git add "$ROOT/docs/index.html"
        CHANGED=1
    fi
fi

# ── Version surfaces derived from pyproject ───────────────────────────
# tests/test_release_metadata.py requires every one of these to carry the
# same version; a release that bumped only pyproject failed CI at publish.
sync_version_in() {
    local file="$1" pattern="$2"
    if [ -f "$ROOT/$file" ]; then
        sed_inplace "$pattern" "$ROOT/$file"
        if ! git diff --quiet "$ROOT/$file"; then
            git add "$ROOT/$file"
            CHANGED=1
        fi
    fi
}
sync_version_in "src/kindex/__init__.py" \
    "s/^__version__ = \"[0-9]+\.[0-9]+\.[0-9]+\"/__version__ = \"${VERSION}\"/"
for json_file in server.json docs/.well-known/mcp/server-card.json \
        .claude-plugin/plugin.json src/kindex/claude_modern/.claude-plugin/plugin.json; do
    sync_version_in "$json_file" \
        "s/\"version\": \"[0-9]+\.[0-9]+\.[0-9]+\"/\"version\": \"${VERSION}\"/g"
done
# The changelog entry is written by hand; say so before CI does.
if ! grep -qE "^## \[${VERSION}\]" "$ROOT/CHANGELOG.md"; then
    echo "sync-version: CHANGELOG.md has no '## [${VERSION}]' entry; tests/test_release_metadata.py fails until it does" >&2
fi

if [ "$CHANGED" -eq 1 ]; then
    echo "sync-version: staged updated files"
fi
