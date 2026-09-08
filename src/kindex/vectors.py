"""Optional vector embedding support via sqlite-vec.

Enables semantic similarity search when installed:
    pip install kindex[vectors]          # just sqlite-vec; API-based providers work out of the box
    pip install sentence-transformers    # opt-in for local embeddings (pulls torch + sklearn)

Supports multiple embedding providers:
    - voyage: Voyage AI Embeddings API (requires VOYAGE_API_KEY) — recommended default
    - openai: OpenAI Embeddings API (requires OPENAI_API_KEY)
    - gemini: Google Gemini Embeddings API (requires GEMINI_API_KEY)
    - local: sentence-transformers (requires separate pip install sentence-transformers)

Falls back gracefully to FTS5 when the configured provider is unavailable.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .privacy import redact, redact_text, safe_error
from .privacy import redacting_print as print

if TYPE_CHECKING:
    from .config import Config, EmbeddingConfig
    from .store import Store

_VEC_AVAILABLE = None
_MODEL = None

# Provider defaults: model, dimensions, api_key_env
PROVIDER_DEFAULTS = {
    "local": {"model": "all-MiniLM-L6-v2", "dimensions": 384, "api_key_env": ""},
    "openai": {"model": "text-embedding-3-small", "dimensions": 1536, "api_key_env": "OPENAI_API_KEY"},
    "gemini": {"model": "gemini-embedding-001", "dimensions": 3072, "api_key_env": "GEMINI_API_KEY"},
    "voyage": {"model": "voyage-context-4", "dimensions": 1024, "api_key_env": "VOYAGE_API_KEY"},
}

EMBED_QUEUE_META = "embed.queue"
EMBEDDING_PRICE_PER_MILLION = {
    ("voyage", "voyage-context-4"): 0.12,
    ("voyage", "voyage-context-3"): 0.18,
    ("voyage", "voyage-3.5"): 0.06,
    ("voyage", "voyage-3-large"): 0.18,
}


def _resolve_embedding_config(config: Config | None) -> tuple[str, str, int, str]:
    """Resolve provider, model, dimensions, api_key_env from config.

    Returns (provider, model, dimensions, api_key_env).
    """
    if config is None:
        defaults = PROVIDER_DEFAULTS["voyage"]
        return "voyage", defaults["model"], defaults["dimensions"], defaults["api_key_env"]

    ec = config.embedding
    provider = ec.provider
    defaults = PROVIDER_DEFAULTS.get(provider, PROVIDER_DEFAULTS["local"])
    model = ec.model or defaults["model"]
    dims = ec.dimensions or defaults["dimensions"]
    api_key_env = ec.api_key_env or defaults["api_key_env"]
    return provider, model, dims, api_key_env


def _embedding_options(config: Config | None) -> dict:
    """Return embedding tuning options with defaults for old configs."""
    ec = getattr(config, "embedding", None) if config is not None else None
    return {
        "strategy": (getattr(ec, "strategy", "") or "auto").lower(),
        "chunk_chars": max(1, int(getattr(ec, "chunk_chars", 6000) or 6000)),
        "chunk_overlap_chars": max(0, int(getattr(ec, "chunk_overlap_chars", 600) or 0)),
        "max_group_chunks": max(1, int(getattr(ec, "max_group_chunks", 20) or 20)),
        "reindex_max_jobs": max(1, int(getattr(ec, "reindex_max_jobs", 200) or 200)),
        "reindex_max_queue": max(1, int(getattr(ec, "reindex_max_queue", 100000) or 100000)),
        "drain_time_budget": max(1, int(getattr(ec, "drain_time_budget", 120) or 120)),
    }


def contextual_embeddings_supported(config: Config | None) -> bool:
    """True when the configured provider/model can embed grouped chunks."""
    provider, model, _, _ = _resolve_embedding_config(config)
    return provider == "voyage" and _is_voyage_context_model(model)


def embedding_strategy(config: Config | None) -> str:
    """Return the effective document embedding strategy.

    ``auto`` enables contextual chunk groups only for models that support them.
    Explicit ``contextual`` is also provider-gated so unsupported providers fall
    back to ``single`` instead of breaking existing users.
    """
    requested = _embedding_options(config)["strategy"]
    if requested in {"contextual", "contextual_chunks", "chunked"}:
        return "contextual" if contextual_embeddings_supported(config) else "single"
    if requested == "single":
        return "single"
    return "contextual" if contextual_embeddings_supported(config) else "single"


def embedding_fingerprint(config: Config | None) -> str:
    """Stable fingerprint for deciding whether existing vectors are stale."""
    provider, model, dims, _ = _resolve_embedding_config(config)
    opts = _embedding_options(config)
    strategy = embedding_strategy(config)
    payload = {
        "provider": provider,
        "model": model,
        "dimensions": dims,
        "strategy": strategy,
    }
    if strategy == "contextual":
        payload.update({
            "chunk_chars": opts["chunk_chars"],
            "chunk_overlap_chars": opts["chunk_overlap_chars"],
            "max_group_chunks": opts["max_group_chunks"],
        })
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _check_vec() -> bool:
    """Check if sqlite-vec extension is available."""
    global _VEC_AVAILABLE
    if _VEC_AVAILABLE is not None:
        return _VEC_AVAILABLE
    try:
        import sqlite_vec  # noqa: F401
        _VEC_AVAILABLE = True
    except ImportError:
        _VEC_AVAILABLE = False
    return _VEC_AVAILABLE


def _get_model(model_name: str = "all-MiniLM-L6-v2"):
    """Lazy-load the sentence transformer model."""
    global _MODEL
    if _MODEL is not None and getattr(_MODEL, '_kindex_model_name', None) == model_name:
        return _MODEL
    try:
        from sentence_transformers import SentenceTransformer
        _MODEL = SentenceTransformer(model_name, local_files_only=True)
        _MODEL._kindex_model_name = model_name
        return _MODEL
    except ImportError:
        print("Warning: sentence-transformers not installed. "
              "Install with: pip install sentence-transformers", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Warning: local embedding model unavailable: {safe_error(e)}", file=sys.stderr)
        return None


def _embed_local(text: str, model_name: str) -> list[float] | None:
    """Embed text using local sentence-transformers."""
    model = _get_model(model_name)
    if model is None:
        return None
    embedding = model.encode(text, normalize_embeddings=True)
    return embedding.tolist()


def _embed_openai(text: str, model: str, dimensions: int, api_key_env: str) -> list[float] | None:
    """Embed text using OpenAI Embeddings API."""
    api_key = os.environ.get(api_key_env)
    if not api_key:
        print(f"Warning: {api_key_env} not set. Cannot embed text.", file=sys.stderr)
        return None

    body = {"input": text, "model": model}
    if dimensions:
        body["dimensions"] = dimensions

    try:
        req = urllib.request.Request(
            "https://api.openai.com/v1/embeddings",
            data=json.dumps(redact(body)).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            return result["data"][0]["embedding"]
    except Exception as e:
        print(f"OpenAI embedding error: {safe_error(e)}", file=sys.stderr)
        return None


def _embed_gemini(text: str, model: str, dimensions: int, api_key_env: str) -> list[float] | None:
    """Embed text using Google Gemini Embeddings API."""
    api_key = os.environ.get(api_key_env)
    if not api_key:
        print(f"Warning: {api_key_env} not set. Cannot embed text.", file=sys.stderr)
        return None

    body = {
        "model": f"models/{model}",
        "content": {"parts": [{"text": text}]},
    }
    if dimensions:
        body["outputDimensionality"] = dimensions

    try:
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent",
            data=json.dumps(redact(body)).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            if "embedding" in result and "values" in result["embedding"]:
                return result["embedding"]["values"]
            return None
    except Exception as e:
        print(f"Gemini embedding error: {safe_error(e)}", file=sys.stderr)
        return None


def _is_voyage_context_model(model: str) -> bool:
    """Return True for Voyage contextualized chunk embedding models."""
    return model.startswith("voyage-context-")


def _embed_voyage_context_chunks(
    chunks: list[str],
    model: str,
    dimensions: int,
    api_key_env: str,
    *,
    input_type: str = "document",
) -> list[list[float]] | None:
    """Embed a query or document chunk group with Voyage's contextual endpoint."""
    api_key = os.environ.get(api_key_env)
    if not api_key:
        print(f"Warning: {api_key_env} not set. Cannot embed text.", file=sys.stderr)
        return None
    if not chunks:
        return []

    body: dict[str, object] = {
        "inputs": chunks if input_type == "query" else [chunks],
        "model": model,
        "input_type": input_type,
    }
    if dimensions:
        body["output_dimension"] = dimensions

    try:
        req = urllib.request.Request(
            "https://api.voyageai.com/v1/contextualizedembeddings",
            data=json.dumps(redact(body)).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            if input_type == "query":
                return [item["data"][0]["embedding"] for item in result["data"]]
            items = sorted(result["data"][0]["data"], key=lambda item: item.get("index", 0))
            return [item["embedding"] for item in items]
    except Exception as e:
        print(f"Voyage embedding error: {safe_error(e)}", file=sys.stderr)
        return None


def _embed_voyage(
    text: str,
    model: str,
    dimensions: int,
    api_key_env: str,
    input_type: str = "document",
) -> list[float] | None:
    """Embed text using Voyage AI Embeddings API.

    Voyage ships as a pure-HTTP API with no native dependencies. Context models
    use Voyage's contextualized endpoint; standard Voyage models keep using the
    regular embeddings endpoint for backward-compatible custom configs.
    """
    api_key = os.environ.get(api_key_env)
    if not api_key:
        print(f"Warning: {api_key_env} not set. Cannot embed text.", file=sys.stderr)
        return None

    if _is_voyage_context_model(model):
        embeddings = _embed_voyage_context_chunks(
            [text], model, dimensions, api_key_env, input_type=input_type
        )
        if not embeddings:
            return None
        return embeddings[0]

    body = {
        "input": [text],
        "model": model,
        "input_type": input_type,
    }

    try:
        req = urllib.request.Request(
            "https://api.voyageai.com/v1/embeddings",
            data=json.dumps(redact(body)).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            return result["data"][0]["embedding"]
    except Exception as e:
        print(f"Voyage embedding error: {safe_error(e)}", file=sys.stderr)
        return None


_EMBED_DISPATCH = {
    "local": lambda text, model, dims, key_env, input_type: _embed_local(text, model),
    "openai": lambda text, model, dims, key_env, input_type: _embed_openai(text, model, dims, key_env),
    "gemini": lambda text, model, dims, key_env, input_type: _embed_gemini(text, model, dims, key_env),
    "voyage": _embed_voyage,
}


def is_available() -> bool:
    """Check if vector search is available."""
    return _check_vec()


def embed_text(
    text: str,
    config: Config | None = None,
    *,
    input_type: str = "document",
) -> list[float] | None:
    """Embed a text string into a vector using the configured provider."""
    provider, model, dims, api_key_env = _resolve_embedding_config(config)
    fn = _EMBED_DISPATCH.get(provider)
    if fn is None:
        print(f"Warning: unknown embedding provider '{provider}'. "
              f"Supported: {', '.join(PROVIDER_DEFAULTS)}", file=sys.stderr)
        return None
    return fn(redact_text(text), model, dims, api_key_env, input_type)


def _get_embedding_dim(config: Config | None) -> int:
    """Get the embedding dimension for the configured provider."""
    _, _, dims, _ = _resolve_embedding_config(config)
    return dims


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _estimate_tokens(text: str) -> int:
    # Cheap, provider-independent estimate. Good enough for reindex planning.
    return max(1, math.ceil(len(text) / 4))


def _embedding_text_for_node(node: dict) -> str:
    return redact_text(f"{node.get('title') or ''} {node.get('content') or ''}".strip())


def _chunk_text(text: str, *, chunk_chars: int, overlap_chars: int) -> list[str]:
    """Split text into stable overlapping chunks using character offsets."""
    text = redact_text(text)
    if not text:
        return []
    chunk_chars = max(1, chunk_chars)
    overlap_chars = min(max(0, overlap_chars), chunk_chars - 1)
    if len(text) <= chunk_chars:
        return [text]
    step = max(1, chunk_chars - overlap_chars)
    chunks = []
    start = 0
    while start < len(text):
        chunks.append(text[start:start + chunk_chars])
        if start + chunk_chars >= len(text):
            break
        start += step
    return chunks


def _chunk_vector_id(node_id: str, index: int, count: int) -> str:
    return node_id if count == 1 else f"{node_id}#{index:04d}"


def _current_price_per_million(config: Config | None) -> float | None:
    provider, model, _, _ = _resolve_embedding_config(config)
    return EMBEDDING_PRICE_PER_MILLION.get((provider, model))


def _ensure_vector_meta_table(store: Store) -> None:
    store.conn.execute("""
        CREATE TABLE IF NOT EXISTS node_vector_meta (
            vector_id TEXT PRIMARY KEY,
            node_id TEXT NOT NULL,
            chunk_index INTEGER NOT NULL DEFAULT 0,
            chunk_count INTEGER NOT NULL DEFAULT 1,
            text_hash TEXT NOT NULL DEFAULT '',
            fingerprint TEXT NOT NULL DEFAULT '',
            strategy TEXT NOT NULL DEFAULT 'single',
            token_estimate INTEGER NOT NULL DEFAULT 0,
            text_preview TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    store.conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_node_vector_meta_node ON node_vector_meta(node_id)"
    )
    store.conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_node_vector_meta_fingerprint "
        "ON node_vector_meta(fingerprint)"
    )


def ensure_vec_table(store: Store) -> bool:
    """Create the vector table if sqlite-vec is available.

    Handles provider/model/strategy changes by recreating the table.
    """
    if not _check_vec():
        return False

    dim = _get_embedding_dim(store.config)
    fingerprint = embedding_fingerprint(store.config)

    try:
        import sqlite_vec
        store.conn.enable_load_extension(True)
        sqlite_vec.load(store.conn)
        store.conn.enable_load_extension(False)

        # Check if dimension changed (provider switch)
        store.conn.execute(
            "CREATE TABLE IF NOT EXISTS vec_meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        row = store.conn.execute(
            "SELECT value FROM vec_meta WHERE key = 'embedding_dim'"
        ).fetchone()
        stored_dim = int(row[0]) if row else None
        fp_row = store.conn.execute(
            "SELECT value FROM vec_meta WHERE key = 'embedding_fingerprint'"
        ).fetchone()
        stored_fingerprint = fp_row[0] if fp_row else None
        table_exists = store.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='node_vectors'"
        ).fetchone() is not None

        if ((stored_dim and stored_dim != dim)
                or (table_exists and stored_fingerprint != fingerprint)):
            reason = f"dimension changed ({stored_dim} -> {dim})"
            if table_exists and stored_fingerprint != fingerprint:
                reason = "embedding provider/model/strategy changed"
            print(f"{reason}. Recreating vector table.", file=sys.stderr)
            store.conn.execute("DROP TABLE IF EXISTS node_vectors")
            store.conn.execute("DROP TABLE IF EXISTS node_vector_meta")

        store.conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS node_vectors USING vec0(
                node_id TEXT PRIMARY KEY,
                embedding float[{dim}]
            )
        """)
        _ensure_vector_meta_table(store)
        store.conn.execute(
            "INSERT OR REPLACE INTO vec_meta (key, value) VALUES ('embedding_dim', ?)",
            (str(dim),),
        )
        store.conn.execute(
            "INSERT OR REPLACE INTO vec_meta (key, value) "
            "VALUES ('embedding_fingerprint', ?)",
            (fingerprint,),
        )
        store.conn.commit()
        return True
    except Exception as e:
        print(f"Warning: Could not initialize vector table: {e}", file=sys.stderr)
        return False


def embed_document_chunks(text: str, config: Config | None = None) -> list[dict] | None:
    """Embed document text and return chunk records ready for storage."""
    if not text:
        return []
    strategy = embedding_strategy(config)
    text_hash = _hash_text(text)
    if strategy != "contextual":
        embedding = embed_text(text, config, input_type="document")
        if embedding is None:
            return None
        return [{
            "index": 0,
            "text": text,
            "embedding": embedding,
            "text_hash": text_hash,
            "token_estimate": _estimate_tokens(text),
        }]

    provider, model, dims, api_key_env = _resolve_embedding_config(config)
    if provider != "voyage" or not _is_voyage_context_model(model):
        embedding = embed_text(text, config, input_type="document")
        if embedding is None:
            return None
        return [{
            "index": 0,
            "text": text,
            "embedding": embedding,
            "text_hash": text_hash,
            "token_estimate": _estimate_tokens(text),
        }]

    opts = _embedding_options(config)
    chunks = _chunk_text(
        text,
        chunk_chars=opts["chunk_chars"],
        overlap_chars=opts["chunk_overlap_chars"],
    )
    records: list[dict] = []
    for offset in range(0, len(chunks), opts["max_group_chunks"]):
        group = chunks[offset:offset + opts["max_group_chunks"]]
        embeddings = _embed_voyage_context_chunks(
            group, model, dims, api_key_env, input_type="document"
        )
        if embeddings is None or len(embeddings) != len(group):
            return None
        for i, (chunk_text, embedding) in enumerate(zip(group, embeddings)):
            records.append({
                "index": offset + i,
                "text": chunk_text,
                "embedding": embedding,
                "text_hash": text_hash,
                "token_estimate": _estimate_tokens(chunk_text),
            })
    return records


def upsert_embedding(store: Store, node_id: str, text: str) -> bool:
    """Compute and store embeddings for a node."""
    if not ensure_vec_table(store):
        return False

    chunks = embed_document_chunks(text, store.config)
    if not chunks:
        return False

    try:
        fingerprint = embedding_fingerprint(store.config)
        strategy = embedding_strategy(store.config)
        now = _now_iso()
        count = len(chunks)
        delete_embedding(store, node_id)
        _ensure_vector_meta_table(store)
        for chunk in chunks:
            index = int(chunk["index"])
            vector_id = _chunk_vector_id(node_id, index, count)
            store.conn.execute(
                "INSERT OR REPLACE INTO node_vectors (node_id, embedding) VALUES (?, ?)",
                (vector_id, _serialize_vec(chunk["embedding"])),
            )
            store.conn.execute(
                """INSERT OR REPLACE INTO node_vector_meta
                   (vector_id, node_id, chunk_index, chunk_count, text_hash,
                    fingerprint, strategy, token_estimate, text_preview, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    vector_id, node_id, index, count, chunk.get("text_hash") or "",
                    fingerprint, strategy, int(chunk.get("token_estimate") or 0),
                    (chunk.get("text") or "")[:240], now,
                ),
            )
        store.conn.commit()
        return True
    except Exception:
        return False


def enqueue_embedding(store: Store, node_id: str, *, max_queue: int = 100000,
                      commit: bool = True) -> bool:
    """Queue a node for (re)embedding by the daemon. Cheap: one small SQLite
    write, no model load, no network — safe on the add/edit/supersede hot path.

    Deferring keeps a slow embedding provider (a remote API can stall up to its
    HTTP timeout per call) off the agent's critical path. The daemon drains the
    queue in cron via ``drain_embedding_queue``. Deduped by node_id; FIFO.
    """
    if not node_id:
        return False
    try:
        raw = store.get_meta(EMBED_QUEUE_META)
        queue = json.loads(raw) if raw else []
        if not isinstance(queue, list):
            queue = []
    except Exception:
        queue = []
    # Dedup and move to the tail so the newest edit wins ordering.
    queue = [n for n in queue if n != node_id]
    queue.append(node_id)
    try:
        value = json.dumps(queue[-max_queue:])
        if commit:
            store.set_meta(EMBED_QUEUE_META, value)
        else:
            # The task service owns the surrounding node + receipt transaction.
            from .privacy import redact_serialized
            store.conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)",
                               (EMBED_QUEUE_META, redact_serialized(value)))
        return True
    except Exception:
        return False


def enqueue_embeddings(store: Store, node_ids: list[str], *, max_queue: int = 100000) -> int:
    """Queue many node IDs for re-embedding, deduping and preserving order."""
    added = 0
    for node_id in node_ids:
        if enqueue_embedding(store, node_id, max_queue=max_queue):
            added += 1
    return added


def _embedding_queue_len(store: Store) -> int:
    try:
        raw = store.get_meta(EMBED_QUEUE_META)
        queue = json.loads(raw) if raw else []
        return len(queue) if isinstance(queue, list) else 0
    except Exception:
        return 0


def drain_embedding_queue(store: Store, config: Config | None = None, *,
                          max_jobs: int | None = None,
                          time_budget: float | None = None) -> dict:
    """Embed queued nodes. This is where the (possibly networked) embedding cost
    lives — runs in cron, off the agent's path. Idempotent per node.

    Each node is re-fetched fresh so the embedding reflects its current text.
    Bounded to ``max_jobs`` attempts AND ``time_budget`` wall-clock seconds per
    drain; unattempted nodes and transient failures are carried to the next cron
    (failures re-queued at the tail so a persistently failing node can't starve
    newer ones). The time budget keeps a slow/stalling provider from consuming
    the whole cron interval — each queued node can cost multiple HTTP calls at
    up to 30s apiece, so a large backlog against a degraded provider would
    otherwise run for hours.
    """
    opts = _embedding_options(config or store.config)
    if max_jobs is None:
        max_jobs = opts["reindex_max_jobs"]
    if time_budget is None:
        time_budget = opts["drain_time_budget"]
    try:
        raw = store.get_meta(EMBED_QUEUE_META)
        queue = json.loads(raw) if raw else []
        if not isinstance(queue, list):
            queue = []
    except Exception:
        queue = []
    if not queue:
        return {"status": "empty", "embedded": 0, "pending": 0}
    if not is_available():
        # Backend not installed/usable; leave the (bounded) queue intact in case
        # it becomes available later.
        return {"status": "unavailable", "embedded": 0, "pending": len(queue)}

    # De-dup preserving order; drop falsy ids.
    deduped = list(dict.fromkeys(n for n in queue if n))
    remaining: list[str] = []
    embedded = 0
    attempts = 0
    deadline = time.monotonic() + float(time_budget)
    for i, node_id in enumerate(deduped):
        if attempts >= max_jobs or time.monotonic() >= deadline:
            remaining.extend(deduped[i:])  # carry the rest to the next cron
            break
        node = store.get_node(node_id)
        if not node or node.get("status") == "superseded":
            continue  # node is gone — drop from the queue
        text = _embedding_text_for_node(node)
        if not text:
            continue  # nothing to embed — drop
        attempts += 1
        try:
            ok = upsert_embedding(store, node_id, text)
        except Exception:
            ok = False  # provider raised — treat as transient
        if ok:
            embedded += 1
        else:
            remaining.append(node_id)  # transient (e.g. provider down) — retry
    try:
        store.set_meta(EMBED_QUEUE_META, json.dumps(remaining))
    except Exception:
        pass
    return {"status": "ok", "embedded": embedded, "pending": len(remaining)}


def _node_embedding_fresh(store: Store, node: dict, fingerprint: str) -> bool:
    text = _embedding_text_for_node(node)
    if not text:
        return True
    text_hash = _hash_text(text)
    try:
        rows = store.conn.execute(
            """SELECT text_hash, fingerprint
               FROM node_vector_meta
               WHERE node_id = ?""",
            (node["id"],),
        ).fetchall()
    except Exception:
        rows = []
    if not rows:
        return False
    return all(row["text_hash"] == text_hash and row["fingerprint"] == fingerprint
               for row in rows)


def _normalize_project_path(path: str | None) -> str | None:
    if not path:
        return None
    p = Path(path).expanduser()
    if p.name == "config" and p.parent.name == ".kin":
        p = p.parent.parent
    elif p.name == ".kin":
        p = p.parent
    try:
        return str(p.resolve())
    except OSError:
        return str(p)


def select_reindex_nodes(
    store: Store,
    *,
    tags: list[str] | None = None,
    node_type: str | None = None,
    status: str | None = None,
    since: str | None = None,
    project_path: str | None = None,
    stale: bool = False,
    limit: int | None = None,
) -> list[dict]:
    """Select nodes for embedding maintenance using operator-friendly filters."""
    q = "SELECT * FROM nodes WHERE status != 'superseded'"
    params: list = []
    if status:
        q += " AND status = ?"
        params.append(status)
    if node_type:
        q += " AND type = ?"
        params.append(node_type)
    if since:
        q += " AND updated_at >= ?"
        params.append(since)
    if tags:
        for tag in tags:
            q += " AND domains LIKE ?"
            params.append(f'%"{tag}"%')
    project_prefix = _normalize_project_path(project_path)
    if project_prefix:
        q += " AND (prov_source LIKE ? OR extra LIKE ?)"
        params.extend([f"{project_prefix}%", f"%{project_prefix}%"])
    q += " ORDER BY weight DESC, updated_at DESC"
    if limit:
        q += " LIMIT ?"
        params.append(limit)

    rows = store.conn.execute(q, params).fetchall()
    nodes = [store._row_to_dict(row) for row in rows]
    if stale:
        fingerprint = embedding_fingerprint(store.config)
        nodes = [node for node in nodes if not _node_embedding_fresh(store, node, fingerprint)]
    return nodes


def estimate_reindex_cost(nodes: list[dict], config: Config | None = None) -> dict:
    """Estimate text volume, token volume, and known provider cost."""
    strategy = embedding_strategy(config)
    opts = _embedding_options(config)
    text_bytes = 0
    token_estimate = 0
    chunk_count = 0
    for node in nodes:
        text = _embedding_text_for_node(node)
        text_bytes += len(text.encode("utf-8"))
        if not text:
            continue
        if strategy == "contextual":
            chunks = _chunk_text(
                text,
                chunk_chars=opts["chunk_chars"],
                overlap_chars=opts["chunk_overlap_chars"],
            )
            chunk_count += len(chunks)
            token_estimate += sum(_estimate_tokens(chunk) for chunk in chunks)
        else:
            chunk_count += 1
            token_estimate += _estimate_tokens(text)
    price = _current_price_per_million(config)
    return {
        "nodes": len(nodes),
        "chunks": chunk_count,
        "text_bytes": text_bytes,
        "estimated_tokens": token_estimate,
        "estimated_cost_usd": (token_estimate / 1_000_000 * price
                               if price is not None else None),
        "price_per_million_tokens": price,
        "strategy": strategy,
        "fingerprint": embedding_fingerprint(config),
    }


def plan_embedding_reindex(store: Store, **filters) -> dict:
    nodes = select_reindex_nodes(store, **filters)
    plan = estimate_reindex_cost(nodes, store.config)
    plan["queue_pending"] = _embedding_queue_len(store)
    return plan


def enqueue_reindex(store: Store, **filters) -> dict:
    max_queue = int(filters.pop("max_queue", 0) or _embedding_options(store.config)["reindex_max_queue"])
    nodes = select_reindex_nodes(store, **filters)
    added = enqueue_embeddings(store, [node["id"] for node in nodes], max_queue=max_queue)
    plan = estimate_reindex_cost(nodes, store.config)
    plan["enqueued"] = added
    plan["queue_pending"] = _embedding_queue_len(store)
    return plan


def reindex_now(store: Store, *, verbose: bool = False, **filters) -> dict:
    """Synchronously reindex selected nodes."""
    nodes = select_reindex_nodes(store, **filters)
    plan = estimate_reindex_cost(nodes, store.config)
    if not ensure_vec_table(store):
        plan.update({"status": "unavailable", "embedded": 0})
        return plan
    embedded = 0
    failed = 0
    for node in nodes:
        text = _embedding_text_for_node(node)
        if not text:
            continue
        if upsert_embedding(store, node["id"], text):
            embedded += 1
            if verbose:
                print(f"  Embedded: {node['title']}")
        else:
            failed += 1
    plan.update({
        "status": "ok",
        "embedded": embedded,
        "failed": failed,
        "queue_pending": _embedding_queue_len(store),
    })
    return plan


def embedding_status(store: Store) -> dict:
    """Return current embedding configuration and queue/index status."""
    provider, model, dims, api_key_env = _resolve_embedding_config(store.config)
    status = {
        "provider": provider,
        "model": model,
        "dimensions": dims,
        "api_key_env": api_key_env,
        "strategy": embedding_strategy(store.config),
        "contextual_supported": contextual_embeddings_supported(store.config),
        "fingerprint": embedding_fingerprint(store.config),
        "queue_pending": _embedding_queue_len(store),
        "vector_rows": None,
        "indexed_nodes": None,
    }
    try:
        status["vector_rows"] = store.conn.execute(
            "SELECT COUNT(*) FROM node_vector_meta"
        ).fetchone()[0]
        status["indexed_nodes"] = store.conn.execute(
            "SELECT COUNT(DISTINCT node_id) FROM node_vector_meta"
        ).fetchone()[0]
    except Exception:
        pass
    return status


def delete_embedding(store: Store, node_id: str) -> bool:
    """Remove a node's stored embedding (best-effort).

    Used when a node is deleted or superseded so vector search stops
    surfacing its stale text. Returns True if a row was deleted; False
    when no row existed or the vector table is unavailable.
    """
    try:
        vector_ids = [node_id]
        try:
            rows = store.conn.execute(
                "SELECT vector_id FROM node_vector_meta WHERE node_id = ?",
                (node_id,),
            ).fetchall()
            vector_ids.extend(row["vector_id"] for row in rows)
        except Exception:
            pass
        deleted = 0
        for vector_id in dict.fromkeys(vector_ids):
            cur = store.conn.execute(
                "DELETE FROM node_vectors WHERE node_id = ?", (vector_id,)
            )
            deleted += cur.rowcount
        try:
            cur = store.conn.execute(
                "DELETE FROM node_vector_meta WHERE node_id = ?", (node_id,)
            )
            deleted += cur.rowcount
        except Exception:
            pass
        store.conn.commit()
        return deleted > 0
    except Exception:
        return False


def _vector_row_node(store: Store, vector_id: str) -> tuple[str, int | None]:
    try:
        row = store.conn.execute(
            "SELECT node_id, chunk_index FROM node_vector_meta WHERE vector_id = ?",
            (vector_id,),
        ).fetchone()
        if row:
            return row["node_id"], row["chunk_index"]
    except Exception:
        pass
    return vector_id, None


def vector_search(store: Store, query: str, top_k: int = 10,
                  min_similarity: float | None = None) -> list[dict]:
    """Search for similar nodes using vector similarity.

    `min_similarity`, when given, drops rows whose cosine similarity falls
    below it. Without a floor this returns the top-k nearest neighbours for any
    query however nonsensical, which is how a near-null query used to pull real
    nodes into an agent's context — the floor is the only thing that lets the
    graph say it knows nothing. The floor is supplied by the caller from a
    calibration record; this function does not invent one.
    """
    if not ensure_vec_table(store):
        return []

    embedding = embed_text(query, store.config, input_type="query")
    if embedding is None:
        return []

    try:
        rows = store.conn.execute(
            """SELECT node_id, distance
               FROM node_vectors
               WHERE embedding MATCH ?
               ORDER BY distance
               LIMIT ?""",
            (_serialize_vec(embedding), max(top_k * 8, top_k)),
        ).fetchall()

        best: dict[str, dict] = {}
        for row in rows:
            vector_id = row[0]
            node_id, chunk_index = _vector_row_node(store, vector_id)
            distance = row[1]
            existing = best.get(node_id)
            if existing is None or distance < existing["distance"]:
                best[node_id] = {
                    "distance": distance,
                    "chunk_index": chunk_index,
                    "vector_id": vector_id,
                }

        from .grounding import similarity_from_distance

        results = []
        for node_id, match in sorted(best.items(), key=lambda item: item[1]["distance"]):
            similarity = similarity_from_distance(match["distance"])
            if min_similarity is not None and similarity < min_similarity:
                # Rows arrive best-first, so the first row under the floor
                # means every remaining row is too.
                break
            node = store.get_node(node_id)
            # Skip superseded nodes — their embeddings are deleted on
            # supersede now, but rows from older DBs may linger.
            if node and node.get("status") != "superseded":
                node["vec_distance"] = match["distance"]
                node["vec_similarity"] = similarity
                if match["chunk_index"] is not None:
                    node["vec_chunk_index"] = match["chunk_index"]
                results.append(node)
                if len(results) >= top_k:
                    break
        return results
    except Exception:
        return []


def _serialize_vec(embedding: list[float]) -> bytes:
    """Serialize a float list to bytes for sqlite-vec."""
    import struct
    return struct.pack(f"{len(embedding)}f", *embedding)


def index_all_nodes(store: Store, verbose: bool = False) -> int:
    """Index all active nodes for vector similarity search."""
    if not ensure_vec_table(store):
        provider = "unknown"
        try:
            provider, _, _, _ = _resolve_embedding_config(store.config)
        except Exception:
            pass
        if provider == "local":
            print("Vector search not available. Install: pip install sqlite-vec sentence-transformers",
                  file=sys.stderr)
        else:
            print("Vector search not available. Install: pip install sqlite-vec",
                  file=sys.stderr)
        return 0

    nodes = select_reindex_nodes(store, status="active")
    count = 0
    for node in nodes:
        text = _embedding_text_for_node(node)
        if upsert_embedding(store, node["id"], text):
            count += 1
            if verbose:
                print(f"  Embedded: {node['title']}")

    return count
