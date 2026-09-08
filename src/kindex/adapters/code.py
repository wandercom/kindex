"""Code structure adapter — ingest repository topology via ctags, cscope, tree-sitter.

Extracts the structural skeleton and dependency graph of a codebase:
- Tier 1: Module nodes (one per source file) with structural summaries
- Tier 2: Symbol nodes (one per class/interface/type) with method signatures
- Edges: imports (depends_on), inheritance (implements), containment (context_of)

Tools degrade gracefully:
- Tier A: module indexing plus ctags symbols when available
- Tier B: ctags + cscope (C/C++ cross-references)
- Tier C: ctags/tree-sitter (AST-based call graphs, import resolution)
"""

from __future__ import annotations

from ..privacy import redacting_print as print

import hashlib
import json
import re
import subprocess
import tempfile
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .base import AdapterMeta, AdapterOption, IngestResult

if TYPE_CHECKING:
    from ..store import Store


# ── Tool detection ──────────────────────────────────────────────────

def _check_ctags() -> bool:
    """Check for Universal Ctags (not Exuberant)."""
    try:
        r = subprocess.run(
            ["ctags", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        return "Universal Ctags" in r.stdout
    except Exception:
        return False


def _check_cscope() -> bool:
    try:
        r = subprocess.run(
            ["cscope", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0 or "cscope" in (r.stdout + r.stderr).lower()
    except Exception:
        return False


def _check_treesitter(language: str) -> object | None:
    """Try to load a tree-sitter parser for the given language.

    Returns the parser object or None.
    """
    # Map ctags language names to tree-sitter grammar packages
    grammar_map = {
        "Python": "tree_sitter_python",
        "JavaScript": "tree_sitter_javascript",
        "TypeScript": "tree_sitter_typescript",
        "Rust": "tree_sitter_rust",
        "Go": "tree_sitter_go",
        "C": "tree_sitter_c",
        "C++": "tree_sitter_cpp",
        "Java": "tree_sitter_java",
        "Ruby": "tree_sitter_ruby",
    }
    pkg = grammar_map.get(language)
    if not pkg:
        return None
    try:
        import importlib
        import tree_sitter as ts  # type: ignore[import-untyped]
        grammar_mod = importlib.import_module(pkg)
        lang = ts.Language(grammar_mod.language())
        parser = ts.Parser(lang)
        return parser
    except Exception:
        return None


# ── Repo / file detection ──────────────────────────────────────────

def _detect_repo(path: Path) -> tuple[Path, str] | None:
    """Find git repo root and derive a slug. Returns (root, slug) or None."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
            cwd=str(path),
        )
        if r.returncode == 0:
            root = Path(r.stdout.strip())
            slug = root.name.lower().replace(" ", "-")
            return root, slug
    except Exception:
        pass
    return None


def _git_ls_files(repo_root: Path) -> list[Path]:
    """Get tracked files via git ls-files."""
    try:
        r = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            capture_output=True, text=True, timeout=30,
            cwd=str(repo_root),
        )
        if r.returncode == 0:
            return [repo_root / f for f in r.stdout.strip().split("\n") if f]
    except Exception:
        pass
    return []


# Source file extensions worth indexing even when ctags has no parser for them.
_LANGUAGE_BY_EXTENSION = {
    ".py": "Python",
    ".js": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".jsx": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".mts": "TypeScript",
    ".cts": "TypeScript",
    ".astro": "Astro",
    ".svelte": "Svelte",
    ".vue": "Vue",
    ".rs": "Rust",
    ".go": "Go",
    ".c": "C",
    ".h": "C",
    ".cpp": "C++",
    ".hpp": "C++",
    ".cc": "C++",
    ".cxx": "C++",
    ".hh": "C++",
    ".java": "Java",
    ".rb": "Ruby",
    ".php": "PHP",
    ".cs": "C#",
    ".swift": "Swift",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".scala": "Scala",
    ".lua": "Lua",
    ".zig": "Zig",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".erl": "Erlang",
    ".hrl": "Erlang",
    ".hs": "Haskell",
    ".ml": "OCaml",
    ".mli": "OCaml",
    ".r": "R",
    ".sh": "Shell",
    ".bash": "Shell",
    ".zsh": "Shell",
    ".vim": "Vim",
    ".el": "Emacs Lisp",
}

_CODE_EXTENSIONS = set(_LANGUAGE_BY_EXTENSION)

# Unity serialized-asset extensions — opt-in via code_ingest.unity in
# .kin/config or `kin ingest code --unity` (issue #17). These are YAML
# when the project uses text serialization, binary otherwise; content is
# never parsed here, only sniffed (see _sniff_serialization).
_UNITY_LANGUAGE_BY_EXTENSION = {
    ".unity": "Unity Scene",
    ".prefab": "Unity Prefab",
    ".asset": "Unity Asset",
    ".mat": "Unity Material",
    ".controller": "Unity Animator",
    ".anim": "Unity Animation",
}

# Unity build/cache output that must never be indexed. fnmatch's '*'
# crosses path separators, so the '*/' variants catch nested Unity
# projects (monorepo layouts) without matching e.g. Assets/MyLibrary/.
_UNITY_EXCLUDES = [
    "Library/*", "*/Library/*",
    "Temp/*", "*/Temp/*",
    "Logs/*", "*/Logs/*",
    "obj/*", "*/obj/*",
    "Build/*", "*/Build/*",
    "UserSettings/*", "*/UserSettings/*",
]

# GUID line in a .meta sidecar, e.g. "guid: 0123456789abcdef0123456789abcdef"
_UNITY_GUID_RE = re.compile(r"^guid:\s*([0-9a-fA-F]{32})\s*$", re.MULTILINE)

# Universal Ctags supports TypeScript but does not map `.tsx` by default.
_CTAGS_EXTENSION_MAPS = [
    "--map-TypeScript=+.tsx",
    "--map-TypeScript=+.mts",
    "--map-TypeScript=+.cts",
]

_DEFAULT_EXCLUDES = [
    "test_*", "*_test.*", "*_test_*", "tests/*", "test/*",
    "vendor/*", "third_party/*", "node_modules/*", "__pycache__/*",
    ".git/*", "dist/*", "build/*", "*.min.js", "*.min.css",
]


def _walk_files(root: Path, exclude: list[str],
                extensions: set[str] | None = None) -> list[Path]:
    """Walk directory tree, filtering by code extensions and exclude patterns."""
    extensions = extensions or _CODE_EXTENSIONS
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in extensions:
            continue
        rel = str(path.relative_to(root))
        if any(fnmatch(rel, pat) for pat in exclude):
            continue
        # Skip hidden directories
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        files.append(path)
    return files


def _get_file_list(directory: Path, repo_root: Path | None,
                   exclude: list[str],
                   extensions: set[str] | None = None) -> list[Path]:
    """Get list of source files, preferring git ls-files."""
    extensions = extensions or _CODE_EXTENSIONS
    if repo_root:
        all_files = _git_ls_files(repo_root)
        if all_files:
            # Filter to code files within the target directory, apply excludes
            result = []
            for f in all_files:
                if not f.suffix.lower() in extensions:
                    continue
                try:
                    rel = str(f.relative_to(directory))
                except ValueError:
                    continue  # outside target directory
                if any(fnmatch(rel, pat) for pat in exclude):
                    continue
                result.append(f)
            return sorted(result)
    return _walk_files(directory, exclude, extensions)


# ── ctags extraction (Tier A) ──────────────────────────────────────

def _parse_ctags_output(output: str | None) -> list[dict]:
    """Parse any valid JSON tags from ctags output."""
    if not output:
        return []
    tags = []
    for line in output.strip().split("\n"):
        if not line:
            continue
        try:
            tag = json.loads(line)
            if tag.get("_type") == "tag":
                tags.append(tag)
        except json.JSONDecodeError:
            continue
    return tags


def _run_ctags_once(files: list[Path], repo_root: Path) -> tuple[list[dict], bool]:
    """Run ctags once, preserving usable output even when the command fails."""
    if not files:
        return [], False
    # Write file list to a temp file to avoid arg length limits
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        for path in files:
            f.write(str(path) + "\n")
        listfile = f.name
    try:
        r = subprocess.run(
            [
                "ctags",
                "--output-format=json",
                "--fields=+nKSlri",
                *_CTAGS_EXTENSION_MAPS,
                "-f", "-",
                "-L", listfile,
            ],
            capture_output=True, text=True, timeout=120,
            cwd=str(repo_root),
        )
        return _parse_ctags_output(r.stdout), r.returncode != 0
    except Exception:
        return [], False
    finally:
        Path(listfile).unlink(missing_ok=True)


def _canonical_path(path: str | Path, repo_root: Path | None = None) -> Path:
    """Normalize a source path, resolving relative ctags paths from repo root."""
    normalized = Path(path)
    if not normalized.is_absolute() and repo_root:
        normalized = repo_root / normalized
    return normalized.resolve()


def _tag_key(tag: dict) -> str:
    """Build a stable dedupe key for ctags output."""
    return json.dumps(tag, sort_keys=True, separators=(",", ":"))


def _run_ctags(files: list[Path], repo_root: Path) -> list[dict]:
    """Run ctags, preserving partial output and isolating failed files."""
    tags, had_failure = _run_ctags_once(files, repo_root)
    if had_failure and len(files) > 1:
        tagged_paths = {
            _canonical_path(tag["path"], repo_root)
            for tag in tags
            if tag.get("path")
        }
        for path in files:
            if _canonical_path(path, repo_root) in tagged_paths:
                continue
            retry_tags, _ = _run_ctags_once([path], repo_root)
            tags.extend(retry_tags)

    deduped: dict[str, dict] = {}
    for tag in tags:
        deduped.setdefault(_tag_key(tag), tag)
    return list(deduped.values())


def _group_by_file(
    tags: list[dict],
    files: list[Path] | None = None,
    repo_root: Path | None = None,
) -> dict[str, list[dict]]:
    """Group tags by file path, retaining source files with no tags."""
    if files is None:
        grouped: dict[str, list[dict]] = {}
        for tag in tags:
            path = tag.get("path", "")
            if path:
                grouped.setdefault(path, []).append(tag)
        return grouped

    grouped: dict[str, list[dict]] = {}
    known_paths: dict[Path, str] = {}
    if files:
        for path in files:
            key = str(_canonical_path(path, repo_root))
            grouped[key] = []
            known_paths[_canonical_path(path, repo_root)] = key

    for tag in tags:
        path = tag.get("path", "")
        if path:
            normalized = _canonical_path(path, repo_root)
            key = known_paths.get(normalized)
            if key:
                grouped[key].append(tag)
    return grouped


# ctags kind codes that become Tier 2 symbol nodes
_CLASS_KINDS = {"class", "interface", "struct", "enum", "trait", "union", "type"}


def _is_public(tag: dict) -> bool:
    """Heuristic: is this symbol public/exported?"""
    name = tag.get("name", "")
    # Python private convention
    if name.startswith("_") and not name.startswith("__"):
        return False
    # Access field if ctags provides it
    access = tag.get("access", "")
    if access and access in ("private", "protected"):
        return False
    return True


def _infer_language(path: Path, tags: list[dict],
                    lang_by_ext: dict[str, str] | None = None) -> str:
    """Prefer ctags language, then use the source extension as fallback."""
    for tag in tags:
        if tag.get("language"):
            return tag["language"]
    return (lang_by_ext or _LANGUAGE_BY_EXTENSION).get(path.suffix.lower(), "Unknown")


def _sniff_serialization(path: Path) -> str:
    """Classify an asset file as text or binary serialization.

    Unity text serialization always starts with a "%YAML 1.1" header;
    binary assets contain NUL bytes early. Reads at most 64 bytes.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(64)
    except OSError:
        return "unreadable"
    if head.startswith(b"%YAML"):
        return "text"
    return "binary" if b"\x00" in head else "text"


def _unity_guid(path: Path) -> str:
    """Read the asset GUID from a Unity .meta sidecar, if present.

    Unity assets reference each other by GUID, not file name, so the
    GUID is what lets models resolve cross-asset references.
    """
    meta = path.with_name(path.name + ".meta")
    try:
        if not meta.is_file():
            return ""
        match = _UNITY_GUID_RE.search(meta.read_text(errors="replace")[:2048])
    except OSError:
        return ""
    return match.group(1).lower() if match else ""


def _count_lines(path: Path) -> int:
    """Count source lines without requiring text decoding.

    Streams in chunks so huge files (e.g. multi-MB Unity scenes) never
    get slurped into memory.
    """
    total = 0
    last = b""
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                total += chunk.count(b"\n")
                last = chunk
    except OSError:
        return 0
    if not last:
        return 0
    return total + (not last.endswith(b"\n"))


def _build_module_content(
    rel_path: str,
    tags: list[dict],
    *,
    language: str = "Unknown",
    line_count: int = 0,
) -> str:
    """Build structural summary for a module node."""
    # Count lines from the last tag's line number when no file count is known.
    max_line = line_count or max((t.get("line", 0) for t in tags), default=0)

    # Collect classes
    classes = []
    for t in tags:
        if t.get("kind") in _CLASS_KINDS and _is_public(t):
            inherits = t.get("inherits", "")
            if inherits and inherits is not False:
                classes.append(f"- {t['name']}({inherits})")
            else:
                classes.append(f"- {t['name']}")

    # Collect top-level functions (no scope = not a method)
    functions = []
    for t in tags:
        if t.get("kind") == "function" and not t.get("scope") and _is_public(t):
            sig = t.get("signature", "()")
            typeref = t.get("typeref", "")
            ret = ""
            if typeref and typeref.startswith("typename:"):
                ret = f" -> {typeref.split(':', 1)[1]}"
            functions.append(f"- {t['name']}{sig}{ret}")

    # Collect imports
    imports = []
    for t in tags:
        if t.get("kind") in ("namespace", "import"):
            nameref = t.get("nameref", "")
            if nameref and isinstance(nameref, str):
                imports.append(nameref.replace("module:", ""))
            else:
                imports.append(t["name"])

    parts = [f"Language: {language} | Path: {rel_path} | ~{max_line} lines"]
    if classes:
        parts.append("\n## Classes\n" + "\n".join(classes))
    if functions:
        parts.append("\n## Functions\n" + "\n".join(functions))
    if imports:
        parts.append("\n## Imports\n" + ", ".join(imports))

    return "\n".join(parts)


def _build_class_content(class_tag: dict, member_tags: list[dict],
                         rel_path: str) -> str:
    """Build content for a class/type symbol node."""
    name = class_tag["name"]
    line = class_tag.get("line", "?")
    inherits = class_tag.get("inherits", "")

    header = f"class {name}"
    if inherits and inherits is not False:
        header += f"({inherits})"
    header += f" ({rel_path}:{line})"

    # Public methods
    methods = []
    for m in member_tags:
        if m.get("kind") == "member" and m.get("scope") == name and _is_public(m):
            mname = m["name"]
            if mname == "__init__":
                continue  # skip constructor noise
            sig = m.get("signature", "()")
            typeref = m.get("typeref", "")
            ret = ""
            if typeref and isinstance(typeref, str) and typeref.startswith("typename:"):
                ret = f" -> {typeref.split(':', 1)[1]}"
            methods.append(f"- {mname}{sig}{ret}")

    parts = [header]
    if methods:
        parts.append("\n## Methods\n" + "\n".join(methods))

    return "\n".join(parts)


def _extract_inheritance(tags: list[dict]) -> list[tuple[str, str, str]]:
    """Extract (child_qualified, parent_name, file_path) from ctags inherits field."""
    results = []
    for t in tags:
        inherits = t.get("inherits")
        if inherits and isinstance(inherits, str):
            path = t.get("path", "")
            scope = t.get("scope", "")
            child = f"{path}:{scope}.{t['name']}" if scope else f"{path}:{t['name']}"
            # inherits can be comma-separated for multiple parents
            for parent in inherits.split(","):
                parent = parent.strip()
                if parent:
                    results.append((child, parent, path))
    return results


# ── cscope extraction (Tier B) ─────────────────────────────────────

def _build_cscope_db(files: list[Path], repo_root: Path) -> Path | None:
    """Build cscope cross-reference database in a temp directory.

    Returns the temp directory path or None on failure.
    """
    # Filter to C/C++/ObjC files
    c_extensions = {".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".hh", ".m", ".mm"}
    c_files = [f for f in files if f.suffix.lower() in c_extensions]
    if not c_files:
        return None

    tmpdir = Path(tempfile.mkdtemp(prefix="kindex-cscope-"))
    namefile = tmpdir / "cscope.files"
    namefile.write_text("\n".join(str(f) for f in c_files) + "\n")

    try:
        r = subprocess.run(
            ["cscope", "-b", "-q", "-i", str(namefile)],
            capture_output=True, text=True, timeout=120,
            cwd=str(tmpdir),
        )
        if r.returncode != 0:
            return None
        return tmpdir
    except Exception:
        return None


def _cscope_query(cscope_dir: Path, query_type: int,
                  symbol: str) -> list[dict]:
    """Run a cscope line-mode query.

    query_type: 0=symbol, 2=functions calling, 3=functions called by, 8=includes
    Returns list of {file, function, line, text}.
    """
    try:
        r = subprocess.run(
            ["cscope", "-d", "-L", f"-{query_type}", symbol,
             "-f", str(cscope_dir / "cscope.out")],
            capture_output=True, text=True, timeout=30,
            cwd=str(cscope_dir),
        )
        if r.returncode != 0:
            return []
        results = []
        for line in r.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split(None, 3)
            if len(parts) >= 4:
                results.append({
                    "file": parts[0],
                    "function": parts[1],
                    "line": parts[2],
                    "text": parts[3],
                })
        return results
    except Exception:
        return []


def _extract_cscope_includes(cscope_dir: Path,
                             repo_root: Path) -> list[tuple[str, str]]:
    """Extract #include relationships: (includer_relpath, included_relpath)."""
    results = []
    # Query type 8 = files including this file
    # But we need to iterate over files — instead query for common headers
    # Actually, cscope -L -8 "pattern" finds #include lines matching pattern
    # More practical: parse cscope.out directly for includes
    try:
        r = subprocess.run(
            ["cscope", "-d", "-L", "-8", ".*",
             "-f", str(cscope_dir / "cscope.out")],
            capture_output=True, text=True, timeout=30,
            cwd=str(cscope_dir),
        )
        if r.returncode != 0:
            return results
        for line in r.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split(None, 3)
            if len(parts) >= 4:
                includer = parts[0]
                # Extract included file from the #include text
                text = parts[3]
                included = ""
                if '"' in text:
                    # #include "foo.h"
                    start = text.index('"') + 1
                    end = text.index('"', start)
                    included = text[start:end]
                if included:
                    try:
                        includer_rel = str(Path(includer).relative_to(repo_root))
                        results.append((includer_rel, included))
                    except ValueError:
                        results.append((includer, included))
    except Exception:
        pass
    return results


def _extract_cscope_calls(cscope_dir: Path, symbols: list[str],
                          repo_root: Path) -> list[tuple[str, str]]:
    """Extract caller->callee relationships for given symbols.

    Returns (caller_name, callee_name) pairs.
    """
    results = []
    for sym in symbols[:100]:  # cap to avoid excessive queries
        # Query type 3: functions called by sym
        callees = _cscope_query(cscope_dir, 3, sym)
        for c in callees:
            callee_func = c["function"]
            if callee_func != "<global>" and callee_func != sym:
                results.append((sym, callee_func))
    return results


# ── tree-sitter extraction (Tier C) ────────────────────────────────

def _ts_extract_imports_python(tree: Any, source: bytes) -> list[tuple[str, str]]:
    """Extract Python imports from tree-sitter AST.

    Returns (local_name, module_path) pairs.
    """
    results = []
    root = tree.root_node

    def _walk(node: Any) -> None:
        if node.type == "import_statement":
            # import foo, import foo.bar
            for child in node.children:
                if child.type == "dotted_name":
                    mod = source[child.start_byte:child.end_byte].decode()
                    results.append((mod.split(".")[-1], mod))
                elif child.type == "aliased_import":
                    name_node = child.child_by_field_name("name")
                    alias_node = child.child_by_field_name("alias")
                    if name_node:
                        mod = source[name_node.start_byte:name_node.end_byte].decode()
                        local = mod
                        if alias_node:
                            local = source[alias_node.start_byte:alias_node.end_byte].decode()
                        results.append((local, mod))
        elif node.type == "import_from_statement":
            # from foo import bar
            mod_node = None
            for child in node.children:
                if child.type == "dotted_name" or child.type == "relative_import":
                    mod_node = child
                    break
            if mod_node:
                mod = source[mod_node.start_byte:mod_node.end_byte].decode()
                for child in node.children:
                    if child.type == "dotted_name" and child != mod_node:
                        name = source[child.start_byte:child.end_byte].decode()
                        results.append((name, f"{mod}.{name}"))
                    elif child.type == "aliased_import":
                        name_node = child.child_by_field_name("name")
                        if name_node:
                            name = source[name_node.start_byte:name_node.end_byte].decode()
                            results.append((name, f"{mod}.{name}"))
        else:
            for child in node.children:
                _walk(child)

    _walk(root)
    return results


def _ts_extract_imports_rust(tree: Any, source: bytes) -> list[tuple[str, str]]:
    """Extract Rust imports from tree-sitter AST.

    Parses ``use`` declarations and returns ``(local_name, module_path)``
    pairs, using ``::`` separators for module paths to mirror Rust's own
    syntax. Handles:

      - ``use foo;``                     → ("foo", "foo")
      - ``use foo::bar;``                → ("bar", "foo::bar")
      - ``use foo::{bar, baz};``         → ("bar", "foo::bar"), ("baz", "foo::baz")
      - ``use foo::bar as b;``           → ("b", "foo::bar")
      - ``use foo::*;``                  → ("*", "foo")  (glob)
      - ``use crate::foo;``              → ("foo", "crate::foo")

    Module-level only — does not descend into ``mod {}`` blocks.
    """
    results: list[tuple[str, str]] = []
    root = tree.root_node

    def _text(n: Any) -> str:
        return source[n.start_byte:n.end_byte].decode(errors="replace")

    def _expand_use_tree(node: Any, prefix: str) -> None:
        """Expand a ``use`` tree node into (local_name, full_path) pairs.

        ``prefix`` is the dotted-but-Rust-style scope already accumulated
        (e.g. "foo::bar" when processing the contents of a use_list).
        """
        kind = node.type
        if kind == "identifier":
            name = _text(node)
            full = f"{prefix}::{name}" if prefix else name
            results.append((name, full))
        elif kind == "scoped_identifier":
            full = _text(node)
            full = f"{prefix}::{full}" if prefix else full
            local = full.rsplit("::", 1)[-1]
            results.append((local, full))
        elif kind == "scoped_use_list":
            # foo::{bar, baz}  — first child is the scope, then the use_list
            scope_node = node.child_by_field_name("path")
            list_node = node.child_by_field_name("list")
            scope = _text(scope_node) if scope_node else ""
            new_prefix = f"{prefix}::{scope}" if prefix and scope else (scope or prefix)
            if list_node:
                for child in list_node.children:
                    if child.type not in (",", "{", "}"):
                        _expand_use_tree(child, new_prefix)
        elif kind == "use_list":
            for child in node.children:
                if child.type not in (",", "{", "}"):
                    _expand_use_tree(child, prefix)
        elif kind == "use_as_clause":
            path_node = node.child_by_field_name("path")
            alias_node = node.child_by_field_name("alias")
            if path_node:
                full = _text(path_node)
                full = f"{prefix}::{full}" if prefix else full
                local = _text(alias_node) if alias_node else full.rsplit("::", 1)[-1]
                results.append((local, full))
        elif kind == "use_wildcard":
            # foo::* — the glob import. Record under the scope path.
            for child in node.children:
                if child.type in ("identifier", "scoped_identifier"):
                    base = _text(child)
                    full = f"{prefix}::{base}" if prefix else base
                    results.append(("*", full))
                    return
            if prefix:
                results.append(("*", prefix))

    def _walk(node: Any) -> None:
        if node.type == "use_declaration":
            # First non-trivial child after the `use` keyword is the tree.
            for child in node.children:
                if child.type in (
                    "identifier", "scoped_identifier", "scoped_use_list",
                    "use_list", "use_as_clause", "use_wildcard",
                ):
                    _expand_use_tree(child, "")
                    break
        else:
            for child in node.children:
                _walk(child)

    _walk(root)
    return results


def _ts_extract_trait_impls_rust(tree: Any, source: bytes) -> list[tuple[str, str]]:
    """Extract Rust trait implementations from tree-sitter AST.

    Returns ``(implementing_type, trait_name)`` pairs for every
    ``impl Trait for Type`` block. Inherent impls (``impl Type {}``)
    are skipped — they have no trait, so they produce no edge.

    Universal-ctags does not surface trait names in its ``inherits``
    field for Rust impl blocks (verified empirically), so this is the
    only path for Rust trait-impl edges in the graph.
    """
    results: list[tuple[str, str]] = []
    root = tree.root_node

    def _text(n: Any) -> str:
        return source[n.start_byte:n.end_byte].decode(errors="replace")

    def _walk(node: Any) -> None:
        if node.type == "impl_item":
            trait_node = node.child_by_field_name("trait")
            type_node = node.child_by_field_name("type")
            if trait_node and type_node:
                trait_name = _text(trait_node)
                impl_type = _text(type_node)
                # Strip generic parameters: `Display<T>` → `Display`,
                # `Vec<u8>` → `Vec`. Keeps the symbol-name match simple.
                trait_name = trait_name.split("<", 1)[0].split("::")[-1]
                impl_type = impl_type.split("<", 1)[0].split("::")[-1]
                if trait_name and impl_type:
                    results.append((impl_type, trait_name))
        for child in node.children:
            _walk(child)

    _walk(root)
    return results


def _ts_extract_calls(tree: Any, source: bytes) -> list[tuple[str, str]]:
    """Extract function calls from tree-sitter AST.

    Returns ``(containing_function, called_function)`` pairs. Handles
    Python (``call`` / ``function_definition`` / ``class_definition``)
    and Rust (``call_expression`` / ``function_item`` / ``impl_item``).
    """
    results = []
    root = tree.root_node

    # Node-type aliases differ across grammars. Treat both flavors uniformly.
    CALL_TYPES = {"call", "call_expression"}
    FN_TYPES = {"function_definition", "method_definition", "function_item"}
    BODY_TYPES = {"block"}  # function_item carries its body in `block` too

    def _find_calls_in_function(func_node: Any, func_name: str) -> None:
        """Walk a function body and find call expressions."""
        for child in func_node.children:
            if child.type in BODY_TYPES:
                _walk_for_calls(child, func_name)

    def _walk_for_calls(node: Any, func_name: str) -> None:
        if node.type in CALL_TYPES:
            callee = node.child_by_field_name("function")
            if callee:
                callee_name = source[callee.start_byte:callee.end_byte].decode(
                    errors="replace",
                )
                # Strip self./Self:: prefixes — they're scope, not name.
                if callee_name.startswith("self."):
                    callee_name = callee_name[5:]
                elif callee_name.startswith("Self::"):
                    callee_name = callee_name[6:]
                # Only keep simple names; skip complex generic expressions.
                if callee_name and len(callee_name) < 80:
                    results.append((func_name, callee_name))
        for child in node.children:
            _walk_for_calls(child, func_name)

    def _walk_top(node: Any, scope: str = "") -> None:
        if node.type in FN_TYPES:
            name_node = node.child_by_field_name("name")
            if name_node:
                name = source[name_node.start_byte:name_node.end_byte].decode(
                    errors="replace",
                )
                qualified = f"{scope}.{name}" if scope else name
                _find_calls_in_function(node, qualified)
        elif node.type == "class_definition":
            name_node = node.child_by_field_name("name")
            cls_name = ""
            if name_node:
                cls_name = source[name_node.start_byte:name_node.end_byte].decode(
                    errors="replace",
                )
            for child in node.children:
                _walk_top(child, cls_name)
        elif node.type == "impl_item":
            # Rust: methods inside `impl Type { ... }` belong to Type's scope.
            type_node = node.child_by_field_name("type")
            impl_scope = ""
            if type_node:
                impl_scope = source[
                    type_node.start_byte:type_node.end_byte
                ].decode(errors="replace")
            for child in node.children:
                _walk_top(child, impl_scope)
        else:
            for child in node.children:
                _walk_top(child, scope)

    _walk_top(root)
    return results


# ── Node ID helpers ────────────────────────────────────────────────

def _module_id(repo_slug: str, rel_path: str) -> str:
    h = hashlib.sha256(rel_path.encode()).hexdigest()[:12]
    return f"code-mod-{repo_slug}-{h}"


def _symbol_id(repo_slug: str, qualified_name: str) -> str:
    h = hashlib.sha256(qualified_name.encode()).hexdigest()[:12]
    return f"code-sym-{repo_slug}-{h}"


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


# ── Main ingestion ─────────────────────────────────────────────────

def _link_to_project(store: "Store", repo_slug: str,
                     node_ids: list[str]) -> None:
    """Link code nodes to their project node if one exists."""
    # Try to find project node by slug
    project = store.get_node_by_title(repo_slug)
    if not project:
        nodes = store.all_nodes(node_type="project", limit=200)
        for p in nodes:
            extra = p.get("extra") or {}
            path = extra.get("path", "")
            title = p.get("title", "").lower()
            if repo_slug in path.lower() or repo_slug in title:
                project = p
                break
    if not project:
        return
    pid = project["id"]
    for nid in node_ids:
        try:
            store.add_edge(
                nid, pid,
                edge_type="context_of",
                weight=0.5,
                provenance="code-ingest",
                bidirectional=False,
            )
        except Exception:
            pass


def ingest_code(
    store: "Store",
    directory: str | Path,
    *,
    limit: int | None = None,
    verbose: bool = False,
    exclude: list[str] | None = None,
    unity: bool = False,
    extra_extensions: dict[str, str] | None = None,
) -> IngestResult:
    """Ingest code structure from a directory into the knowledge graph.

    Analysis is fully local (ctags/cscope/tree-sitter, no LLM calls), so
    limit defaults to unlimited. None, 0, or negative all mean unlimited;
    a positive limit caps created+updated nodes and reports truncation.

    unity opts in to Unity serialized assets (.unity/.prefab/.asset/...)
    with .meta GUID attachment; extra_extensions maps additional
    extensions to language labels (".shader" -> "Unity Shader").
    """
    directory = Path(directory).resolve()
    if not directory.is_dir():
        return IngestResult(errors=[f"Not a directory: {directory}"])

    if limit is not None and limit <= 0:
        limit = None

    exclude_patterns = list(exclude) if exclude else list(_DEFAULT_EXCLUDES)

    lang_by_ext = dict(_LANGUAGE_BY_EXTENSION)
    if unity:
        lang_by_ext.update(_UNITY_LANGUAGE_BY_EXTENSION)
        exclude_patterns.extend(_UNITY_EXCLUDES)
    if extra_extensions:
        lang_by_ext.update({
            (ext if ext.startswith(".") else f".{ext}").lower(): label
            for ext, label in extra_extensions.items()
        })
    extensions = set(lang_by_ext)

    # Detect repo
    repo_info = _detect_repo(directory)
    repo_root = repo_info[0] if repo_info else None
    repo_slug = repo_info[1] if repo_info else directory.name.lower().replace(" ", "-")

    # Detect available tools
    has_cscope = _check_cscope()
    # tree-sitter checked per-language later

    # Get file list
    files = _get_file_list(directory, repo_root, exclude_patterns, extensions)
    if verbose:
        print(f"  Found {len(files)} source files in {directory}")

    if not files:
        return IngestResult()

    # Run ctags on code files only — asset files added via unity/
    # extra_extensions have no ctags parser, and feeding them in would
    # make the per-file retry after a failed batch O(assets).
    effective_root = repo_root or directory
    ctags_files = [f for f in files if f.suffix.lower() in _CODE_EXTENSIONS]
    tags = _run_ctags(ctags_files, effective_root) if ctags_files else []
    if verbose:
        print(f"  ctags produced {len(tags)} tags")

    if not tags and verbose:
        print("  ctags produced no tags; continuing with module fallback")

    grouped = _group_by_file(tags, files, effective_root)

    created = 0
    updated = 0
    skipped = 0
    errors: list[str] = []
    warnings: list[str] = []
    truncated = False
    processed_files = 0
    all_node_ids: list[str] = []

    # Maps for edge creation: qualified_name -> node_id
    symbol_node_ids: dict[str, str] = {}
    module_node_ids: dict[str, str] = {}  # rel_path -> node_id

    # Phase 1: Create module and symbol nodes from ctags
    for abs_path_str, file_tags in grouped.items():
        abs_path = Path(abs_path_str)
        try:
            rel_path = str(abs_path.relative_to(effective_root))
        except ValueError:
            rel_path = abs_path_str

        # --- Module node (Tier 1) ---
        mod_id = _module_id(repo_slug, rel_path)
        module_node_ids[rel_path] = mod_id

        # Check if file changed (incremental). With unity, the GUID lives
        # in a .meta sidecar outside the content hash, so a changed or
        # newly-present GUID must also invalidate the skip.
        existing = store.get_node(mod_id)
        if existing:
            old_extra = existing.get("extra") or {}
            old_hash = old_extra.get("file_hash", "")
            try:
                current_hash = _file_hash(abs_path)
            except OSError:
                skipped += 1
                continue
            guid_unchanged = (
                not unity
                or old_extra.get("unity_guid", "") == _unity_guid(abs_path)
            )
            if old_hash == current_hash and guid_unchanged:
                skipped += 1
                # Still record symbol IDs for edge creation
                for t in file_tags:
                    if t.get("kind") in _CLASS_KINDS and _is_public(t):
                        scope = t.get("scope", "")
                        qname = f"{rel_path}:{scope}.{t['name']}" if scope else f"{rel_path}:{t['name']}"
                        sym_id = _symbol_id(repo_slug, qname)
                        symbol_node_ids[qname] = sym_id
                        symbol_node_ids[t["name"]] = sym_id  # short name too
                continue

        # Only files that actually mint/update nodes consume the budget;
        # checking here (after the unchanged-skip) avoids a false
        # truncation warning when everything left would have been skipped.
        if limit is not None and created + updated >= limit:
            truncated = True
            break
        processed_files += 1

        try:
            current_hash = _file_hash(abs_path)
        except OSError:
            errors.append(f"Cannot read: {rel_path}")
            continue

        # Determine language from ctags when possible, then from extension.
        language = _infer_language(abs_path, file_tags, lang_by_ext)
        language_source = (
            "ctags"
            if any(tag.get("language") for tag in file_tags)
            else "extension"
        )
        # Asset files from unity/extra_extensions may be binary; sniff
        # before reading (_count_lines slurps the whole file).
        serialization = ""
        if abs_path.suffix.lower() not in _CODE_EXTENSIONS:
            serialization = _sniff_serialization(abs_path)
        line_count = 0 if serialization == "binary" else _count_lines(abs_path)

        content = _build_module_content(
            rel_path,
            file_tags,
            language=language,
            line_count=line_count,
        )

        # Count classes and functions
        class_count = sum(1 for t in file_tags if t.get("kind") in _CLASS_KINDS)
        func_count = sum(1 for t in file_tags
                         if t.get("kind") == "function" and not t.get("scope"))

        extra = {
            "file_hash": current_hash,
            "relative_path": rel_path,
            "language": language,
            "language_source": language_source,
            "ctags_tag_count": len(file_tags),
            "line_count": line_count,
            "class_count": class_count,
            "function_count": func_count,
            "tool_tier": "A",
            "repo_root": str(effective_root),
        }
        if serialization:
            extra["serialization"] = serialization
        if unity:
            guid = _unity_guid(abs_path)
            if guid:
                extra["unity_guid"] = guid

        title = rel_path  # Use relative path as title — most useful for search

        if existing:
            store.update_node(mod_id, content=content, extra=extra)
            updated += 1
        else:
            store.add_node(
                title=title,
                content=content,
                node_id=mod_id,
                node_type="artifact",
                domains=["code", language.lower()],
                prov_source=str(abs_path),
                prov_activity="code-ingest",
                extra=extra,
            )
            created += 1
        all_node_ids.append(mod_id)

        if verbose:
            print(f"  {'Updated' if existing else 'Created'} module: {rel_path}")

        # --- Symbol nodes (Tier 2) — classes/interfaces/types ---
        for t in file_tags:
            if t.get("kind") not in _CLASS_KINDS:
                continue
            if not _is_public(t):
                continue
            # Budget check after the filters, so tags that would never
            # mint a node cannot trigger a false truncation warning.
            if limit is not None and created + updated >= limit:
                truncated = True
                break

            scope = t.get("scope", "")
            qname = f"{rel_path}:{scope}.{t['name']}" if scope else f"{rel_path}:{t['name']}"
            sym_id = _symbol_id(repo_slug, qname)
            symbol_node_ids[qname] = sym_id
            symbol_node_ids[t["name"]] = sym_id  # short name for edge matching

            sym_existing = store.get_node(sym_id)

            # Collect members of this class
            members = [m for m in file_tags
                       if m.get("scope") == t["name"]
                       and m.get("scopeKind") in ("class", "struct", "enum")]
            public_methods = [m["name"] for m in members
                              if m.get("kind") == "member" and _is_public(m)
                              and m["name"] != "__init__"]

            sym_content = _build_class_content(t, members, rel_path)
            inherits_raw = t.get("inherits", "")
            inherits_list = []
            if inherits_raw and isinstance(inherits_raw, str):
                inherits_list = [p.strip() for p in inherits_raw.split(",") if p.strip()]

            sym_extra = {
                "kind": t["kind"],
                "language": language,
                "relative_path": rel_path,
                "line": t.get("line", 0),
                "qualified_name": qname,
                "inherits": inherits_list,
                "public_methods": public_methods,
                "repo_root": str(effective_root),
            }

            sym_title = t["name"]
            # Add scope for disambiguation
            if scope:
                sym_title = f"{scope}.{t['name']}"

            if sym_existing:
                store.update_node(sym_id, content=sym_content, extra=sym_extra)
                updated += 1
            else:
                store.add_node(
                    title=sym_title,
                    content=sym_content,
                    node_id=sym_id,
                    node_type="concept",
                    domains=["code", language.lower()],
                    prov_source=f"{abs_path}:{t.get('line', 0)}",
                    prov_activity="code-ingest",
                    extra=sym_extra,
                )
                created += 1
            all_node_ids.append(sym_id)

            # Edge: symbol -> module (context_of)
            try:
                store.add_edge(
                    sym_id, mod_id,
                    edge_type="context_of",
                    weight=0.6,
                    provenance="code-ingest: defined in",
                    bidirectional=False,
                )
            except Exception:
                pass

            if verbose:
                print(f"    {'Updated' if sym_existing else 'Created'} class: {sym_title}")

    # Phase 2: Create edges from inheritance
    for child_qname, parent_name, _file_path in _extract_inheritance(tags):
        child_id = symbol_node_ids.get(child_qname)
        parent_id = symbol_node_ids.get(parent_name)
        if child_id and parent_id:
            try:
                store.add_edge(
                    child_id, parent_id,
                    edge_type="implements",
                    weight=0.7,
                    provenance="code-ingest: inherits",
                )
            except Exception:
                pass

    # Phase 3: Import edges (module -> module)
    for file_tags_list in grouped.values():
        for t in file_tags_list:
            if t.get("kind") not in ("namespace", "import"):
                continue
            nameref = t.get("nameref", "")
            if not nameref or not isinstance(nameref, str):
                continue
            imported_module = nameref.replace("module:", "")
            # Find the source file's module node
            src_path = t.get("path", "")
            try:
                src_rel = str(Path(src_path).relative_to(effective_root))
            except (ValueError, TypeError):
                continue
            src_mod_id = module_node_ids.get(src_rel)
            if not src_mod_id:
                continue
            # Try to find target module node by matching imported name to rel_paths
            target_mod_id = None
            for rp, mid in module_node_ids.items():
                stem = Path(rp).stem
                if stem == imported_module or rp.replace("/", ".").replace(".py", "") == imported_module:
                    target_mod_id = mid
                    break
            if target_mod_id and target_mod_id != src_mod_id:
                try:
                    store.add_edge(
                        src_mod_id, target_mod_id,
                        edge_type="depends_on",
                        weight=0.5,
                        provenance=f"code-ingest: imports {imported_module}",
                        bidirectional=False,
                    )
                except Exception:
                    pass

    # Phase 4: cscope edges (Tier B) — C/C++ only
    if has_cscope:
        cscope_dir = _build_cscope_db(files, effective_root)
        if cscope_dir:
            try:
                # Include edges
                includes = _extract_cscope_includes(cscope_dir, effective_root)
                for includer_rel, included in includes:
                    src_id = module_node_ids.get(includer_rel)
                    # Try to find the included file
                    tgt_id = None
                    for rp, mid in module_node_ids.items():
                        if rp.endswith(included) or Path(rp).name == Path(included).name:
                            tgt_id = mid
                            break
                    if src_id and tgt_id and src_id != tgt_id:
                        try:
                            store.add_edge(
                                src_id, tgt_id,
                                edge_type="depends_on",
                                weight=0.5,
                                provenance="code-ingest: #include",
                                bidirectional=False,
                            )
                        except Exception:
                            pass

                # Call graph edges
                class_symbols = [name for name in symbol_node_ids
                                 if ":" not in name]  # short names only
                calls = _extract_cscope_calls(cscope_dir, class_symbols, effective_root)
                for caller, callee in calls:
                    caller_id = symbol_node_ids.get(caller)
                    callee_id = symbol_node_ids.get(callee)
                    if caller_id and callee_id and caller_id != callee_id:
                        try:
                            store.add_edge(
                                caller_id, callee_id,
                                edge_type="relates_to",
                                weight=0.4,
                                provenance="code-ingest: calls (cscope)",
                            )
                        except Exception:
                            pass

                if verbose:
                    print(f"  cscope: {len(includes)} includes, {len(calls)} call edges")
            finally:
                # Cleanup temp cscope files
                import shutil
                shutil.rmtree(cscope_dir, ignore_errors=True)

            # Update tool tier in module extras
            for mod_id in module_node_ids.values():
                node = store.get_node(mod_id)
                if node:
                    extra = node.get("extra") or {}
                    lang = extra.get("language", "")
                    if lang in ("C", "C++", "Objective-C"):
                        extra["tool_tier"] = "B"
                        store.update_node(mod_id, extra=extra)

    # Phase 5: tree-sitter edges (Tier C)
    # Group files by language, try to load parser for each
    lang_files: dict[str, list[tuple[str, Path]]] = {}
    for abs_path_str, file_tags in grouped.items():
        abs_path = Path(abs_path_str)
        language = _infer_language(abs_path, file_tags, lang_by_ext)
        if language != "Unknown":
            try:
                rel_path = str(abs_path.relative_to(effective_root))
            except ValueError:
                continue
            lang_files.setdefault(language, []).append((rel_path, abs_path))

    for language, file_list in lang_files.items():
        parser = _check_treesitter(language)
        if not parser:
            continue

        for rel_path, abs_path in file_list:
            try:
                source = abs_path.read_bytes()
                tree = parser.parse(source)
            except Exception:
                continue

            mod_id = module_node_ids.get(rel_path)
            if not mod_id:
                continue

            # Import edges from tree-sitter (more accurate than ctags).
            # Resolution is per-language because the path syntax differs:
            # Python uses dotted ("foo.bar"), Rust uses scoped ("foo::bar"
            # plus crate-relative "crate::*", "super::*", "self::*").
            if language == "Python":
                ts_imports = _ts_extract_imports_python(tree, source)
                for local_name, mod_path in ts_imports:
                    target_id = None
                    mod_parts = mod_path.replace(".", "/")
                    for rp, mid in module_node_ids.items():
                        if rp.replace(".py", "").endswith(mod_parts) or Path(rp).stem == local_name:
                            target_id = mid
                            break
                    if target_id and target_id != mod_id:
                        try:
                            store.add_edge(
                                mod_id, target_id,
                                edge_type="depends_on",
                                weight=0.5,
                                provenance=f"code-ingest: imports {mod_path} (tree-sitter)",
                                bidirectional=False,
                            )
                        except Exception:
                            pass
            elif language == "Rust":
                # Trait impl edges (impl Trait for Type → implements).
                # ctags doesn't surface this in `inherits` for Rust, so
                # tree-sitter is the only source.
                for impl_type, trait_name in _ts_extract_trait_impls_rust(
                    tree, source,
                ):
                    # Match the symbol nodes by suffix on qualified name —
                    # ctags reports symbols as "<rel_path>:<scope>.<name>"
                    # so we match on the trailing ".<name>" or ":<name>".
                    impl_id = None
                    trait_id = None
                    for qname, sid in symbol_node_ids.items():
                        leaf = qname.rsplit(".", 1)[-1].rsplit(":", 1)[-1]
                        if leaf == impl_type and impl_id is None:
                            impl_id = sid
                        if leaf == trait_name and trait_id is None:
                            trait_id = sid
                    if impl_id and trait_id and impl_id != trait_id:
                        try:
                            store.add_edge(
                                impl_id, trait_id,
                                edge_type="implements",
                                weight=0.7,
                                provenance=(
                                    f"code-ingest: impl {trait_name} for "
                                    f"{impl_type} (tree-sitter)"
                                ),
                                bidirectional=False,
                            )
                        except Exception:
                            pass

                ts_imports = _ts_extract_imports_rust(tree, source)
                for local_name, mod_path in ts_imports:
                    # Strip crate-relative prefixes so the suffix match works:
                    # crate::foo::bar  → foo::bar  (crate root corresponds to src/)
                    # self::foo        → foo      (relative to current file's dir)
                    # super::foo       → foo      (parent module — best-effort)
                    norm_path = mod_path
                    for prefix in ("crate::", "self::", "super::"):
                        if norm_path.startswith(prefix):
                            norm_path = norm_path[len(prefix):]
                            break
                    mod_parts = norm_path.replace("::", "/")
                    target_id = None
                    for rp, mid in module_node_ids.items():
                        rp_stem = rp.replace(".rs", "")
                        # Match either the full path tail or a leaf name when
                        # the import resolves to a single symbol (use foo::Bar).
                        if rp_stem.endswith(mod_parts) or Path(rp).stem == local_name:
                            target_id = mid
                            break
                        # Module hierarchies: `use foo::bar;` may import
                        # symbols from foo.rs; match the parent path too.
                        parent = mod_parts.rsplit("/", 1)[0] if "/" in mod_parts else mod_parts
                        if parent and rp_stem.endswith(parent):
                            target_id = mid
                            break
                    if target_id and target_id != mod_id:
                        try:
                            store.add_edge(
                                mod_id, target_id,
                                edge_type="depends_on",
                                weight=0.5,
                                provenance=f"code-ingest: imports {mod_path} (tree-sitter)",
                                bidirectional=False,
                            )
                        except Exception:
                            pass

            # Call graph edges from tree-sitter
            calls = _ts_extract_calls(tree, source)
            for caller, callee in calls:
                caller_id = symbol_node_ids.get(caller)
                callee_id = symbol_node_ids.get(callee)
                if caller_id and callee_id and caller_id != callee_id:
                    try:
                        store.add_edge(
                            caller_id, callee_id,
                            edge_type="relates_to",
                            weight=0.4,
                            provenance="code-ingest: calls (tree-sitter)",
                        )
                    except Exception:
                        pass

            # Update tool tier
            node = store.get_node(mod_id)
            if node:
                extra = node.get("extra") or {}
                if extra.get("tool_tier", "A") == "A":
                    extra["tool_tier"] = "C"
                    store.update_node(mod_id, extra=extra)

        if verbose:
            print(f"  tree-sitter: processed {len(file_list)} {language} files")

    # Link all code nodes to project
    _link_to_project(store, repo_slug, all_node_ids)

    if truncated:
        msg = (f"limit {limit} reached after {processed_files} of "
               f"{len(grouped)} files; pass --limit 0 to ingest everything")
        if verbose:
            print(f"  Warning: {msg}")
        warnings.append(msg)

    return IngestResult(created=created, updated=updated, skipped=skipped,
                        errors=errors, warnings=warnings)


# ── Adapter protocol wrapper ───────────────────────────────────────

def _target_code_ingest(directory: str):
    """Read the TARGET repo's own .kin/config code_ingest section.

    The project-scoped Unity opt-in should follow the directory being
    ingested, not whatever project the caller's cwd resolves to. Reads
    only the target's git-tracked .kin/config (checked at the directory
    itself, then at its git root) — no global config layering, so tests
    and cross-project ingests stay hermetic. Returns None when absent.
    """
    try:
        import yaml

        from ..config import CodeIngestConfig

        d = Path(directory).resolve()
        repo = _detect_repo(d)
        candidates = [d] + ([repo[0]] if repo else [])
        for root in dict.fromkeys(candidates):
            f = Path(root) / ".kin" / "config"
            if f.is_file():
                data = yaml.safe_load(f.read_text()) or {}
                section = data.get("code_ingest") if isinstance(data, dict) else None
                if isinstance(section, dict):
                    return CodeIngestConfig(**section)
    except Exception:
        pass
    return None


class CodeAdapter:
    meta = AdapterMeta(
        name="code",
        description="Ingest code structure via ctags, cscope, and tree-sitter",
        options=[
            AdapterOption("directory", "Repository or directory to analyze", required=True),
            AdapterOption("exclude", "Comma-separated exclude glob patterns"),
            AdapterOption("unity", "Include Unity asset files (.unity/.prefab/.asset) "
                                   "and .meta GUIDs"),
        ],
    )

    def is_available(self) -> bool:
        return _check_ctags()

    def ingest(self, store: "Store", *, limit: int | None = None, since: str | None = None,
               verbose: bool = False, **kwargs: Any) -> IngestResult:
        directory = kwargs.get("directory")
        if not directory:
            return IngestResult(errors=["--directory is required"])

        exclude = None
        exclude_str = kwargs.get("exclude", "")
        if exclude_str:
            exclude = [p.strip() for p in exclude_str.split(",") if p.strip()]

        # Unity opt-in: explicit --unity flag wins; otherwise the TARGET
        # directory's own .kin/config code_ingest section (the project
        # being ingested, not the cwd), falling back to the loaded config.
        code_ingest = (_target_code_ingest(directory)
                       or getattr(kwargs.get("_config"), "code_ingest", None))
        unity = kwargs.get("unity")
        if unity is None:
            unity = bool(code_ingest and code_ingest.unity)
        extra_extensions = dict(code_ingest.include_extensions) if code_ingest else None

        return ingest_code(
            store, directory,
            limit=limit, verbose=verbose, exclude=exclude,
            unity=unity, extra_extensions=extra_extensions,
        )


adapter = CodeAdapter()
