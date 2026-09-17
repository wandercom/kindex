"""Rust support tests for the kindex code adapter.

Covers the rust-specific tree-sitter extractors and an end-to-end ingestion
smoke test on a synthetic 3-file rust project.
"""
from __future__ import annotations

import importlib.util
import textwrap
from pathlib import Path

import pytest

# Skip the module if tree-sitter or the rust grammar isn't installed —
# kindex does not require either at import time.
if (
    importlib.util.find_spec("tree_sitter") is None
    or importlib.util.find_spec("tree_sitter_rust") is None
):
    pytest.skip(
        "tree-sitter / tree-sitter-rust not installed",
        allow_module_level=True,
    )

from kindex.adapters.code import (  # noqa: E402
    _check_ctags,
    _check_treesitter,
    _ts_extract_calls,
    _ts_extract_imports_rust,
)


def _parse(src: str):
    parser = _check_treesitter("Rust")
    assert parser is not None, "tree-sitter Rust parser failed to load"
    source_bytes = src.encode()
    tree = parser.parse(source_bytes)
    return tree, source_bytes


# ── Imports ────────────────────────────────────────────────────────


class TestRustImports:
    def test_simple_use(self):
        tree, src = _parse("use std;\nfn main() {}\n")
        assert ("std", "std") in _ts_extract_imports_rust(tree, src)

    def test_scoped_use(self):
        tree, src = _parse("use std::collections::HashMap;\n")
        imports = _ts_extract_imports_rust(tree, src)
        assert any(
            local == "HashMap" and "std::collections::HashMap" in path
            for local, path in imports
        ), imports

    def test_use_list(self):
        tree, src = _parse("use std::collections::{HashMap, HashSet};\n")
        imports = _ts_extract_imports_rust(tree, src)
        names = {local for local, _ in imports}
        assert "HashMap" in names
        assert "HashSet" in names
        # Both must carry the full scoped path.
        for local, path in imports:
            if local in {"HashMap", "HashSet"}:
                assert path.startswith("std::collections::"), (local, path)

    def test_use_as(self):
        tree, src = _parse("use std::io::Result as IoResult;\n")
        imports = _ts_extract_imports_rust(tree, src)
        assert ("IoResult", "std::io::Result") in imports or any(
            local == "IoResult" for local, _ in imports
        ), imports

    def test_use_glob(self):
        tree, src = _parse("use foo::bar::*;\n")
        imports = _ts_extract_imports_rust(tree, src)
        assert any(local == "*" for local, _ in imports), imports

    def test_use_crate_relative(self):
        tree, src = _parse("use crate::auth::login;\n")
        imports = _ts_extract_imports_rust(tree, src)
        assert any(
            "crate::auth::login" in path for _, path in imports
        ), imports

    def test_no_use_no_imports(self):
        tree, src = _parse("fn add(a: i32, b: i32) -> i32 { a + b }\n")
        assert _ts_extract_imports_rust(tree, src) == []


# ── Calls ──────────────────────────────────────────────────────────


class TestRustCalls:
    def test_simple_call(self):
        tree, src = _parse(textwrap.dedent("""
            fn helper() {}
            fn main() {
                helper();
            }
        """).strip() + "\n")
        calls = _ts_extract_calls(tree, src)
        assert ("main", "helper") in calls, calls

    def test_method_call_in_impl(self):
        # Methods inside `impl Type { ... }` belong to Type's scope.
        tree, src = _parse(textwrap.dedent("""
            struct Counter { n: i32 }

            impl Counter {
                fn bump(&mut self) {
                    self.update(1);
                }
                fn update(&mut self, x: i32) { self.n += x; }
            }
        """).strip() + "\n")
        calls = _ts_extract_calls(tree, src)
        # Counter.bump calls update (self. prefix is stripped).
        assert ("Counter.bump", "update") in calls, calls

    def test_associated_function_call(self):
        # `Type::method()` — Self:: prefix is stripped to bare name.
        tree, src = _parse(textwrap.dedent("""
            struct Foo;
            impl Foo {
                fn new() -> Self { Foo }
                fn build() -> Self { Self::new() }
            }
        """).strip() + "\n")
        calls = _ts_extract_calls(tree, src)
        assert ("Foo.build", "new") in calls, calls

    def test_python_calls_still_work(self):
        # Don't regress python: same helper, different grammar.
        from kindex.adapters.code import _check_treesitter
        py_parser = _check_treesitter("Python")
        if py_parser is None:
            pytest.skip("tree-sitter-python not installed")
        py_src = b"def main():\n    helper()\n\ndef helper():\n    pass\n"
        tree = py_parser.parse(py_src)
        calls = _ts_extract_calls(tree, py_src)
        assert ("main", "helper") in calls


# ── End-to-end ingestion ──────────────────────────────────────────


class TestRustIngestion:
    """Drive the full code adapter on a synthetic rust crate."""

    def _make_crate(self, root: Path) -> None:
        """Create a tiny but realistic rust crate under *root*."""
        src = root / "src"
        src.mkdir()
        (src / "lib.rs").write_text(textwrap.dedent("""
            pub mod auth;
            pub mod store;

            pub use auth::login;
        """).lstrip())
        (src / "auth.rs").write_text(textwrap.dedent("""
            use crate::store::User;

            pub fn login(user: User) -> bool {
                user.is_valid()
            }
        """).lstrip())
        (src / "store.rs").write_text(textwrap.dedent("""
            pub struct User { pub name: String }

            impl User {
                pub fn is_valid(&self) -> bool {
                    !self.name.is_empty()
                }
            }
        """).lstrip())
        # Minimal Cargo.toml so directory is recognizably a crate.
        (root / "Cargo.toml").write_text(textwrap.dedent("""
            [package]
            name = "kindex-test-crate"
            version = "0.0.1"
            edition = "2021"
        """).lstrip())

    def test_ingest_creates_module_nodes(self, tmp_path):
        from kindex.adapters.code import ingest_code
        from kindex.config import Config
        from kindex.store import Store

        crate = tmp_path / "crate"
        crate.mkdir()
        self._make_crate(crate)

        cfg = Config(data_dir=str(tmp_path / "kindex"))
        store = Store(cfg)
        try:
            ingest_code(store, crate)
            # Module nodes use node_type="artifact"; title is the rel path.
            mods = store.all_nodes(node_type="artifact", limit=200)
            mod_titles = {m.get("title", "") for m in mods}
            for expected in ("lib.rs", "auth.rs", "store.rs"):
                assert any(expected in t for t in mod_titles), (
                    f"missing module node for {expected}; got {mod_titles}"
                )
        finally:
            store.close()

    @pytest.mark.skipif(not _check_ctags(),
                        reason="symbol nodes come from universal-ctags, which is not installed")
    def test_ingest_creates_trait_impl_edges(self, tmp_path):
        """`impl Greet for Greeter` should create an `implements` edge.

        ctags does NOT emit `inherits` for Rust impl blocks (verified
        empirically), so kindex relies on the tree-sitter-based
        `_ts_extract_trait_impls_rust` for this — both trait and
        impl must be locally defined for the edge to be creatable.
        """
        from kindex.adapters.code import ingest_code
        from kindex.config import Config
        from kindex.store import Store

        crate = tmp_path / "trait-crate"
        crate.mkdir()
        (crate / "Cargo.toml").write_text(
            "[package]\nname = \"trait-test\"\nversion = \"0.0.1\"\n"
            "edition = \"2021\"\n"
        )
        src = crate / "src"
        src.mkdir()
        (src / "lib.rs").write_text(textwrap.dedent("""
            pub trait Greet {
                fn hello(&self) -> &'static str;
            }

            pub struct Greeter;

            impl Greet for Greeter {
                fn hello(&self) -> &'static str { "hi" }
            }
        """).lstrip())

        cfg = Config(data_dir=str(tmp_path / "kindex"))
        store = Store(cfg)
        try:
            ingest_code(store, crate)
            # Whether the implements edge actually appears depends on
            # whether ctags emits `inherits` for rust impl blocks. If it
            # doesn't, this test will fail and signal that fix #3 needs
            # a tree-sitter-based implementation rather than relying on
            # the generic ctags path.
            mods = store.all_nodes(node_type="artifact", limit=200)
            lib = next(
                (m for m in mods if "lib.rs" in m.get("title", "")), None,
            )
            assert lib, "lib.rs module node missing"

            # Look for *any* implements edge originating from a symbol in
            # this crate. We don't pin a specific from/to id because the
            # exact ctags output for impl blocks varies by version.
            symbols = store.all_nodes(node_type="concept", limit=200)
            crate_symbols = [
                s for s in symbols
                if "trait-test" in s.get("id", "")
                or s.get("id", "").startswith("code-sym-trait")
            ]
            implements_edges_found = False
            for s in crate_symbols:
                for e in store.edges_from(s["id"]):
                    if e.get("type") == "implements":
                        implements_edges_found = True
                        break
                if implements_edges_found:
                    break

            assert implements_edges_found, (
                "expected an implements edge from Greeter → Greet via "
                "tree-sitter trait-impl extraction"
            )
        finally:
            store.close()

    def test_ingest_creates_import_edges(self, tmp_path):
        """auth.rs depends on store.rs via `use crate::store::User`."""
        from kindex.adapters.code import ingest_code
        from kindex.config import Config
        from kindex.store import Store

        crate = tmp_path / "crate"
        crate.mkdir()
        self._make_crate(crate)

        cfg = Config(data_dir=str(tmp_path / "kindex"))
        store = Store(cfg)
        try:
            ingest_code(store, crate)
            mods = store.all_nodes(node_type="artifact", limit=200)
            auth = next(
                (m for m in mods if "auth.rs" in m.get("title", "")), None,
            )
            store_mod = next(
                (m for m in mods if "store.rs" in m.get("title", "")), None,
            )
            assert auth and store_mod, (
                f"module nodes missing; titles={[m.get('title') for m in mods]}"
            )

            edges = store.edges_from(auth["id"])
            depends_on = [
                e for e in edges
                if e.get("type") == "depends_on"
                and e.get("to_id") == store_mod["id"]
            ]
            assert depends_on, (
                f"expected depends_on edge auth.rs → store.rs; got {edges}"
            )
        finally:
            store.close()
