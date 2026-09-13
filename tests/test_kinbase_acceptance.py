"""Independent acceptance tests grounded in kinbase-compatibility.md and shipping model/crypto/corpus.
Synthetic signed inputs only. No production data or implementation imports during authorship.
"""
from __future__ import annotations
import base64
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from kindex.config import Config
from kindex.store import Store

KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
PUBLIC = KEY.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()

# All test fixtures use the integer/string subset of RFC 8785. UTF-16 key
# order is implemented explicitly, independently of the production reader.
def canonical(value):
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + canonical(value[k]) for k in sorted(value, key=lambda k:k.encode("utf-16-be"))) + "}"
    if isinstance(value, list):
        return "[" + ",".join(canonical(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

def fact(key="policy", **updates):
    result = dict(schema="kinbase-event/1", event_id="event-"+key, fact_id="fact-"+key,
        logical_key=key, store_kind="codebase", authority_id="test-owner", authority_scope="repo:test",
        repository_id="test-repository", atom_kind="decision", scope="test-scope",
        statement="Choose fenced writes for transaction safety", evidence_refs=["https://example.invalid/tickets/42"],
        asserted_at="2026-01-01T00:00:00Z", effective_from="2026-01-01T00:00:00Z",
        disposition="accepted", distortion=dict(trigger="dependent decision",loss_if_absent=5000,rationale="safe writes"),
        parents=[], supersedes=[], redundancy_with=[], complements=[], company_refs=[],
        authority_snapshot_cursor="0", confidence=8000, unresolved_uncertainty=None,
        standing="ratified", provenance="human", governs_paths=["src/**"],
        anchors=[dict(path="src/write.py",line_start=10,line_end=14,revision="abc123")])
    result.update(updates)
    return result

def unknown(key="policy", **updates):
    result = fact(key, schema="kinbase-unknown/1", event_id="question-"+key,
        fact_id="unknown-"+key, atom_kind="unknown", statement="Who decides?", question="Who owns the transaction safety ruling?",
        decision_blocked="Choose write behavior", owner_role="transaction-maintainer", owner_identity="unassigned",
        closure_evidence=["Recorded maintainer ruling"],status="UNKNOWN_OWNER_UNRESOLVED",
        response_due_at="2026-12-01T00:00:00Z",expiry_policy="retain",standing="present",provenance="unknown")
    result.update(updates)
    return result

def sign(document, *, message_type=None, signature_encoding="hex", omit_signer=False):
    document = dict(document, signer=PUBLIC)
    signed = {k:v for k,v in document.items() if k != "signature" and (k != "signer" or not omit_signer)}
    message_type = message_type or ("unknown-event" if document["schema"] == "kinbase-unknown/1" else "fact-event")
    payload = b"kinbase-sig/1\x00" + message_type.encode() + b"\x00" + canonical(signed).encode()
    signature = KEY.sign(hashlib.sha256(payload).digest())
    document["signature"] = signature.hex() if signature_encoding == "hex" else base64.b64encode(signature).decode()
    return document

def write_doc(repo, doc, *, path_digest=None):
    content = canonical(doc).encode()
    digest = path_digest or hashlib.sha256(content).hexdigest()
    path = repo / ".kin/events" / digest[:2] / digest[2:4] / (digest[4:] + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path

def sync(store, repo, **kwargs):
    return importlib.import_module("kindex.kinbase").sync_kinbase(store, repo, **kwargs)

def rows(store):
    return [store.get_node(r[0]) for r in store.conn.execute("SELECT id FROM nodes")]

def by_content(store, text):
    return [r for r in rows(store) if r["content"] == text]

def source_snapshot(repo):
    return {str(p.relative_to(repo)):hashlib.sha256(p.read_bytes()).hexdigest() for p in repo.rglob("*") if p.is_file()}

@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.endswith("API_KEY") or key.startswith("KINBASE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setattr("kindex.vectors.is_available", lambda:False)

@pytest.fixture
def store(tmp_path):
    result = Store(Config(data_dir=str(tmp_path / "destination")))
    yield result
    result.close()

@pytest.fixture
def repo(tmp_path):
    result = tmp_path / "source-repository"
    (result / ".kin/events").mkdir(parents=True)
    (result / ".kin/kinbase.toml").write_text('repository_id = "test-repository"\n')
    return result

@pytest.mark.parametrize("encoding",["hex","base64"])
def test_signed_raw_mapping_preservation_readonly_idempotence(store,repo,encoding):
    document = sign(fact(),signature_encoding=encoding)
    write_doc(repo,document)
    before = source_snapshot(repo)
    first = sync(store,repo,mode="raw")
    assert first["mode"] == "raw" and first["imported"] == 1 and first["quarantined"] == 0
    imported = by_content(store, document["statement"])
    assert len(imported) == 1
    node = imported[0]
    assert node["standing"] == "ratified"
    assert node["type"] == "decision"
    assert "test-scope" in node["domains"]
    all_fields = json.dumps(node,ensure_ascii=False)
    for value in ["policy","src/write.py","src/**","abc123","https://example.invalid/tickets/42","human",PUBLIC]:
        assert value in all_fields, value
    second = sync(store,repo,mode="raw")
    assert second["imported"] == 0 and second["unchanged"] == 1
    assert len(by_content(store,document["statement"])) == 1
    assert source_snapshot(repo) == before

@pytest.mark.parametrize("provenance,expected",[("human","authoritative"),("human_review","prevalent"),("transcript","prevalent"),("ai_generated","present"),("bot","present"),("unknown","present")])
def test_raw_provenance_clamps_claimed_authority(store,repo,provenance,expected):
    doc = fact(provenance,standing="authoritative",provenance=provenance)
    write_doc(repo,sign(doc))
    sync(store,repo,mode="raw")
    assert by_content(store,doc["statement"])[0]["standing"] == expected

def test_missing_provenance_is_unknown_and_capped(store,repo):
    doc = fact(standing="authoritative")
    del doc["provenance"]
    write_doc(repo,sign(doc))
    sync(store,repo,mode="raw")
    assert by_content(store,doc["statement"])[0]["standing"] == "present"

@pytest.mark.parametrize("damage",["wrong_path","bad_signature","omitted_signer","wrong_message_domain","tampered_bytes"])
def test_invalid_event_quarantined_without_source_repair(store,repo,damage):
    doc = sign(fact(),omit_signer=damage=="omitted_signer",message_type="unknown-event" if damage=="wrong_message_domain" else None)
    if damage == "bad_signature":
        doc["signature"] = "00"*64
    path = write_doc(repo,doc,path_digest="ab"*32 if damage=="wrong_path" else None)
    if damage == "tampered_bytes":
        path.write_bytes(path.read_bytes().replace(b"fenced", b"unsafe"))
    before = source_snapshot(repo)
    report = sync(store,repo,mode="raw")
    assert report["quarantined"] == 1 and report["imported"] == 0
    assert not by_content(store,doc["statement"])
    assert source_snapshot(repo) == before

def test_jcs_uses_utf16_sorting_and_preserves_unicode(store,repo):
    # Non-BMP sorts before U+E000 under JCS/UTF-16 but after it in Python
    # codepoint sorting. Extra signed fields must not be stripped to verify.
    doc = fact(statement="Unicode policy café and 🚀", **{"😀":"first", "\ue000":"second"})
    write_doc(repo,sign(doc))
    report = sync(store,repo,mode="raw")
    assert report["imported"] == 1 and report["quarantined"] == 0

@pytest.mark.parametrize("damage",["remove","tamper"])
def test_resync_retires_missing_or_corrupt_previous_import(store,repo,damage):
    doc = fact()
    path = write_doc(repo,sign(doc))
    sync(store,repo,mode="raw")
    if damage == "remove": path.unlink()
    else: path.write_bytes(path.read_bytes().replace(b"fenced", b"unsafe"))
    sync(store,repo,mode="raw")
    from kindex.retrieve import hybrid_search
    assert not [r for r in hybrid_search(store,"transaction safety",top_k=20) if r["content"] == doc["statement"]]


def test_raw_signature_is_not_governance_or_trusted_answer(store,repo):
    doc = fact(standing="authoritative")
    write_doc(repo,sign(doc))
    sync(store,repo,mode="raw")
    from kindex.retrieve import hybrid_search
    trusted = hybrid_search(store,"transaction safety",trusted_only=True,top_k=10)
    assert not [r for r in trusted if r["content"] == doc["statement"]]

def test_one_ratified_fact_beats_256_repetitions_with_top_one(store,repo):
    winner = fact("write-policy",statement="Transaction safety requires fencing; approved by the responsible maintainer.")
    write_doc(repo,sign(winner))
    for i in range(256):
        write_doc(repo,sign(fact("observation-"+str(i), statement="Transaction safety transaction safety transaction safety",standing="present",provenance="ai_generated",asserted_at="2026-09-09T00:00:00Z")))
    sync(store,repo,mode="raw")
    from kindex.retrieve import hybrid_search
    for ranking in ("ensemble","rrf"):
        hits=hybrid_search(store,"transaction safety",top_k=1,expand_graph=False,ranking=ranking)
        assert len(hits) == 1 and hits[0]["content"] == winner["statement"]

def test_associated_unresolved_unknown_visible_with_fact_at_top_one(store,repo):
    doc=fact()
    question=unknown()
    write_doc(repo,sign(doc))
    write_doc(repo,sign(question))
    report=sync(store,repo,mode="raw")
    assert report["imported"] == 2
    from kindex.retrieve import hybrid_search, format_context_block
    hits=hybrid_search(store,"transaction safety",top_k=1)
    assert hits[0]["content"] == doc["statement"]
    context=format_context_block(store,hits,query="transaction safety",level="full",max_tokens_approx=4000)
    for expected in [question["question"],"UNKNOWN_OWNER_UNRESOLVED","transaction-maintainer","unassigned"]:
        assert expected in context

def test_unknown_requires_own_signature_domain(store,repo):
    write_doc(repo,sign(unknown(),message_type="fact-event"))
    report=sync(store,repo,mode="raw")
    assert report["imported"] == 0 and report["quarantined"] == 1

def test_native_standing_defaults_and_reopen(store,tmp_path):
    old=store.add_node("Old ordinary knowledge", "Legacy native statement")
    ruling=store.add_node("A ruling","Explicit native standing",standing="ratified")
    assert store.get_node(old)["standing"] == "unruled"
    assert store.get_node(ruling)["standing"] == "ratified"
    store.close()
    assert store.get_node(old)["standing"] == "unruled"
    assert store.get_node(ruling)["standing"] == "ratified"

# A tiny fake Kinbase executable emits the shipping `explain` shape, after
# testing argv. The source repository still supplies signed event keys.
def executable(tmp_path,payloads, *, fail=False):
    # Shipping corpus::explain always includes the reproducible reduction
    # envelope. Keep fixtures concise while preserving that wire contract.
    payloads = {key: dict(value) for key, value in payloads.items()}
    for key, value in payloads.items():
        value.setdefault("logical_key", key)
        value.setdefault("as_of", "2026-09-10T00:00:00Z")
        value.setdefault("as_of_source", "explicit")
        value.setdefault("ambient_clock_read", False)
        value.setdefault("reducer_version", "kinbase-reducer/2")
        value.setdefault("authority_cursor", "0")
        value.setdefault("state", "current" if value.get("current") is not None and value["current"].get("status") == "current" else "unknown")
        value.setdefault("observed_state", value["state"])
        value.setdefault("projection_state", "projected" if value.get("trusted") else "withheld")
    path=tmp_path / "kinbase-fixture"
    program = "#!"+sys.executable+"\nimport sys,json\n"
    program += "assert sys.argv[1] == 'explain', sys.argv\nassert '--repo' in sys.argv and '--json' in sys.argv\n"
    if fail:
        program += "print('authority lookup failed',file=sys.stderr)\nsys.exit(7)\n"
    else:
        program += "payloads="+repr(payloads)+"\nprint(json.dumps(payloads[sys.argv[2]]))\n"
    path.write_text(program)
    path.chmod(0o700)
    return str(path)

def current(doc):
    result={k:v for k,v in doc.items() if k not in ("schema","signature","signer","parents","asserted_at")}
    result.update(status="current",trust="trusted",stale_reasons=[],support_event_ids=[doc["event_id"]],independent_support_count=1,criticality="advisory",claimed_standing=doc["standing"])
    return result

def test_reduced_uses_explain_for_all_local_keys_and_current_only(store,repo,tmp_path):
    accepted=fact("admitted",statement="Reduced current answer")
    withheld=fact("withheld",statement="Raw conflicted answer must not leak")
    for doc in (accepted,withheld): write_doc(repo,sign(doc))
    payloads={"admitted":dict(logical_key="admitted",current=current(accepted),unknowns=[],trusted=True,as_of="2026-09-10T00:00:00Z",reducer_version="kinbase-reducer/2"),
        "withheld":dict(logical_key="withheld",current=None,unknowns=[dict(unknown_id="unresolved-key",logical_key="withheld",question="Who owns this scope?",owner_role="maintainer",owner_identity="unassigned",status="UNKNOWN_OWNER_UNRESOLVED",scope="test-scope")],trusted=False)}
    binary=executable(tmp_path,payloads)
    before=source_snapshot(repo)
    report=sync(store,repo,mode="reduced",binary=binary)
    assert report["mode"] == "reduced"
    assert by_content(store,accepted["statement"])
    from kindex.retrieve import hybrid_search
    assert any(n["content"] == accepted["statement"] for n in hybrid_search(store,"Reduced current answer",top_k=10))
    assert not by_content(store,withheld["statement"])
    assert "Who owns this scope?" in json.dumps(rows(store))
    assert source_snapshot(repo) == before

@pytest.mark.parametrize("mode",["reduced","auto"])
def test_reduction_failure_is_explicit_and_does_not_fallback_to_raw(store,repo,tmp_path,mode):
    doc=fact()
    write_doc(repo,sign(doc))
    binary=executable(tmp_path,{},fail=True)
    with pytest.raises(Exception):
        sync(store,repo,mode=mode,binary=binary)
    assert not by_content(store,doc["statement"])

def test_auto_missing_binary_uses_raw(store,repo,tmp_path):
    doc=fact()
    write_doc(repo,sign(doc))
    report=sync(store,repo,mode="auto",binary=str(tmp_path/"not-installed"))
    assert report["mode"] == "raw" and report["imported"] == 1

def cli(tmp_path,*args):
    import subprocess
    env=dict(os.environ)
    env["PYTHONPATH"]=os.pathsep.join(sys.path)
    return subprocess.run([sys.executable,"-m","kindex.cli",*map(str,args)],cwd=tmp_path,env=env,text=True,capture_output=True,timeout=30)

def test_cli_raw_sync_accepts_explicit_destination_and_emits_json(repo,tmp_path):
    doc=fact()
    write_doc(repo,sign(doc))
    data=tmp_path/"cli-destination"
    before=source_snapshot(repo)
    result=cli(tmp_path,"kinbase","sync","--repo",repo,"--mode","raw","--data-dir",data,"--json")
    assert result.returncode == 0, result.stderr
    payload=json.loads(result.stdout)
    assert payload["mode"] == "raw" and payload["imported"] == 1
    destination=Store(Config(data_dir=str(data)))
    try:
        assert by_content(destination,doc["statement"])
    finally:
        destination.close()
    assert source_snapshot(repo) == before

def test_native_standing_survives_json_export_import(store,tmp_path):
    node_id=store.add_node("Native ruling","Keep the ratified precedence",standing="ratified")
    store.close()
    exported=cli(tmp_path,"export","--audience","private","--format","json","--data-dir",store.config.data_path)
    assert exported.returncode == 0, exported.stderr
    path=tmp_path/"export.json"
    path.write_text(exported.stdout)
    destination=tmp_path/"roundtrip-destination"
    imported=cli(tmp_path,"import",path,"--data-dir",destination)
    assert imported.returncode == 0, imported.stderr
    reloaded=Store(Config(data_dir=str(destination)))
    try:
        assert reloaded.get_node(node_id)["standing"] == "ratified"
    finally:
        reloaded.close()

def test_existing_schema_v12_upgrades_default_without_losing_knowledge(tmp_path):
    import sqlite3
    data=tmp_path/"legacy-destination"
    data.mkdir()
    db=sqlite3.connect(data/"kindex.db")
    db.executescript(Path(__file__).with_name("legacy_schema_v12.sql").read_text())
    db.execute("INSERT INTO meta(key,value) VALUES('schema_version','12')")
    db.execute("INSERT INTO nodes(id,type,title,content) VALUES('legacy','concept','Legacy statement','Preserve all existing knowledge')")
    db.commit()
    db.close()
    upgraded=Store(Config(data_dir=str(data)))
    try:
        node=upgraded.get_node("legacy")
        assert node["content"] == "Preserve all existing knowledge"
        assert node["standing"] == "unruled"
        new_id=upgraded.add_node("Migrated ruling",standing="ratified")
        assert upgraded.get_node(new_id)["standing"] == "ratified"
    finally:
        upgraded.close()

def test_mcp_sync_routes_explicit_repository_and_parity(store,repo,monkeypatch):
    mcp=importlib.import_module("kindex.mcp_server")
    monkeypatch.setattr(mcp,"_get_store",lambda:(store,store.config))
    doc=fact()
    write_doc(repo,sign(doc))
    result=mcp.kinbase_sync(str(repo),mode="raw")
    if isinstance(result,str):
        # MCP traditionally returns human-readable text; JSON is also valid.
        assert "1" in result and "raw" in result.lower()
    else:
        assert result["imported"] == 1 and result["mode"] == "raw"
    assert by_content(store,doc["statement"])[0]["standing"] == "ratified"

@pytest.mark.parametrize("schema",["kinbase-event/99","kinbase-repo-certificate/1","unknown"])
def test_unsupported_schema_not_admitted_as_fact(store,repo,schema):
    doc=fact(schema=schema)
    write_doc(repo,sign(doc))
    result=sync(store,repo,mode="raw")
    assert result["imported"] == 0 and result["quarantined"] == 1
    assert not by_content(store,doc["statement"])

def test_missing_event_directory_is_error_without_retiring_existing_import(store,repo):
    doc=fact()
    write_doc(repo,sign(doc))
    sync(store,repo,mode="raw")
    events=repo/".kin/events"
    events.rename(repo/".kin/events-offline")
    with pytest.raises(Exception):
        sync(store,repo,mode="raw")
    from kindex.retrieve import hybrid_search
    assert any(n["content"] == doc["statement"] for n in hybrid_search(store,"transaction safety",top_k=10))

def test_symlink_event_store_refused_without_following_external_data(store,repo,tmp_path):
    external=tmp_path/"external"
    write_doc(external,sign(fact()))
    (repo/".kin/events").rmdir()
    (repo/".kin/events").symlink_to(external/".kin/events",target_is_directory=True)
    before=source_snapshot(external)
    with pytest.raises(Exception):
        sync(store,repo,mode="raw")
    assert not by_content(store,fact()["statement"])
    assert source_snapshot(external) == before

def test_switch_raw_to_reduced_removes_raw_answer_when_reducer_withholds(store,repo,tmp_path):
    doc=fact()
    write_doc(repo,sign(doc))
    sync(store,repo,mode="raw")
    payload={"policy":dict(logical_key="policy",current=None,unknowns=[],trusted=False)}
    sync(store,repo,mode="reduced",binary=executable(tmp_path,payload))
    from kindex.retrieve import hybrid_search
    assert not any(n["content"] == doc["statement"] for n in hybrid_search(store,"transaction safety",top_k=10))

def test_reduced_refresh_current_null_retires_previous_answer(store,repo,tmp_path):
    doc=fact()
    write_doc(repo,sign(doc))
    payload={"policy":dict(logical_key="policy",current=current(doc),unknowns=[],trusted=True)}
    binary=executable(tmp_path,payload)
    sync(store,repo,mode="reduced",binary=binary)
    assert by_content(store,doc["statement"])
    executable(tmp_path,{"policy":dict(logical_key="policy",current=None,unknowns=[],trusted=False)})
    sync(store,repo,mode="reduced",binary=binary)
    from kindex.retrieve import hybrid_search
    assert not any(n["content"] == doc["statement"] for n in hybrid_search(store,"transaction safety",top_k=10))

@pytest.mark.parametrize("trust",["excluded","withheld"])
def test_reduced_withheld_evidence_is_retained_but_not_active_guidance(store,repo,tmp_path,trust):
    doc=fact("withheld-evidence",statement="Withheld policy evidence needs authority review")
    write_doc(repo,sign(doc))
    reduced=current(doc)
    reduced["trust"]=trust
    reduced["stale_reasons"]=["REVOCATION_STALE"]
    payload={doc["logical_key"]:dict(current=reduced,unknowns=[],trusted=False,projection_state="withheld")}
    sync(store,repo,mode="reduced",binary=executable(tmp_path,payload))
    evidence=by_content(store,doc["statement"])
    assert evidence, "Keep explain's withheld evidence available for inspection"
    serialized=json.dumps(evidence)
    assert trust in serialized and "REVOCATION_STALE" in serialized
    from kindex.retrieve import hybrid_search
    assert not any(n["content"] == doc["statement"] for n in hybrid_search(store,"Withheld policy evidence",top_k=10))
    assert not any(n["content"] == doc["statement"] for n in hybrid_search(store,"Withheld policy evidence",top_k=10,trusted_only=True))


def test_unsigned_repo_publication_preserves_kinbase_provenance_or_refuses(store,repo,tmp_path):
    # Explicit shareable audience does not authorize stripping the signature
    # provenance/standing/clamp metadata out of imported source evidence.
    doc=sign(fact())
    write_doc(repo,doc)
    sync(store,repo,mode="raw")
    node=by_content(store,doc["statement"])[0]
    store.conn.execute("UPDATE nodes SET audience='team' WHERE id=?",(node["id"],))
    store.conn.commit()
    target=tmp_path/"publication-target"
    (target/".kin").mkdir(parents=True)
    from kindex.repo_memory import publish
    try:
        publish(store,target,[node["id"]])
    except (ValueError,RuntimeError):
        assert not (target/".kin/knowledge.json").exists()
        return
    published=(target/".kin/knowledge.json").read_text()
    for expected in ["ratified",PUBLIC,doc["signature"],"kinbase", "human"]:
        assert expected in published, "Unsigned transport dropped Kinbase source provenance: "+expected

@pytest.mark.parametrize("ranking",["ensemble","rrf"])
def test_expired_high_standing_cannot_starve_live_relevant_answer(store,repo,ranking):
    for i in range(3):
        expired=fact("expired-"+str(i),statement="Fencing transaction safety expired ruling "+str(i),standing="authoritative",effective_until="2026-02-01T00:00:00Z")
        write_doc(repo,sign(expired))
    live=fact("live-lower-standing",statement="Fencing transaction safety live observed behavior",standing="present",provenance="ai_generated")
    write_doc(repo,sign(live))
    sync(store,repo,mode="raw")
    from kindex.retrieve import hybrid_search
    results=hybrid_search(store,"Fencing transaction safety",top_k=1,expand_graph=False,ranking=ranking,evaluation_time="2026-09-10T00:00:00Z")
    assert len(results) == 1 and results[0]["content"] == live["statement"]

@pytest.mark.parametrize("ranking",["ensemble","rrf"])
def test_unverified_high_standing_imports_cannot_starve_verified_native_answer(store,repo,ranking):
    for i in range(3):
        write_doc(repo,sign(fact("unverified-"+str(i),statement="Fencing transaction safety raw ruling "+str(i),standing="authoritative")))
    sync(store,repo,mode="raw")
    native=store.add_node("Fencing transaction safety", "Fencing transaction safety inspected native answer")
    store.verify_node(native,verified_by="human-reviewer",prov_method="direct-source-review",verified_at="2026-09-01T00:00:00Z",valid_at="2026-09-01T00:00:00Z")
    from kindex.retrieve import hybrid_search
    results=hybrid_search(store,"Fencing transaction safety",top_k=1,expand_graph=False,ranking=ranking,trusted_only=True,evaluation_time="2026-09-10T00:00:00Z")
    assert len(results) == 1 and results[0]["id"] == native


def test_org_export_scrubs_nested_kinbase_contacts_and_marks_signature_scope(store,repo,tmp_path):
    email="synthetic-owner@example.invalid"
    # Keep title derivation free of contacts so this exercises full statement
    # and nested metadata rather than a title-only or top-level scrub.
    statement="Company policy " + ("signed source context " * 12) + "Contact " + email + " for a ruling."
    assert statement.index(email) > 160
    document=sign(fact("company-contact-policy",store_kind="company",statement=statement,
        owner_identity=email,attributes={"owner":{"owner_identity":email,"contacts":[email]},email:"contact lookup key"}))
    write_doc(repo,document)
    sync(store,repo,mode="raw")
    imported=by_content(store,statement)
    # Imported stores may already redact recognized contacts, while export
    # must ensure no nesting retains them regardless of capture behavior.
    if not imported:
        imported=[n for n in rows(store) if "Company policy" in n["content"]]
    assert imported
    store.close()
    output=cli(tmp_path,"export","--audience","org","--format","json","--data-dir",store.config.data_path)
    assert output.returncode == 0,output.stderr
    payload=json.loads(output.stdout)
    serialized=json.dumps(payload,ensure_ascii=False)
    assert "Company policy" in serialized, "An org export should contain the company evidence"
    assert email not in serialized, "Sharing must scrub nested source documents as well as display text"
    # A scrubbed copy no longer has the exact signed bytes. Preserve the
    # signature record and explicitly label redaction/original-byte scope;
    # do not represent the shared sanitized object itself as signed.
    assert document["signature"] in serialized
    lowered=serialized.lower()
    assert "redact" in lowered, "Export must disclose source content was sanitized"
    assert "original" in lowered, "Verification must remain scoped to the original signed source"


def test_shared_roundtrip_preserves_source_identity_without_leaking_paths(store,repo,tmp_path):
    source_a=repo
    source_b=tmp_path/"other-private-source-repository"
    (source_b/".kin/events").mkdir(parents=True)
    (source_b/".kin/kinbase.toml").write_text('repository_id = "other-test-repository"\n')
    alpha=fact("shared-logical-key",store_kind="company",statement="Alphaowned routing follows the ratified alpha ruling")
    beta=fact("shared-logical-key",store_kind="company",statement="Betaowned routing follows the ratified beta ruling")
    question=unknown("shared-logical-key",store_kind="company",question="Which alpha owner should settle the contested routing choice?")
    write_doc(source_a,sign(alpha))
    write_doc(source_a,sign(question))
    write_doc(source_b,sign(beta))
    sync(store,source_a,mode="raw")
    sync(store,source_b,mode="raw")
    store.close()
    exported=cli(tmp_path,"export","--audience","org","--format","json","--data-dir",store.config.data_path)
    assert exported.returncode == 0,exported.stderr
    payload=json.loads(exported.stdout)
    serialized=json.dumps(payload,ensure_ascii=False)
    assert str(source_a) not in serialized and str(source_b) not in serialized
    assert source_a.name not in serialized and source_b.name not in serialized
    shared=tmp_path/"shared-roundtrip.json"
    shared.write_text(exported.stdout)
    destination=tmp_path/"shared-roundtrip-destination"
    imported=cli(tmp_path,"import",shared,"--data-dir",destination)
    assert imported.returncode == 0,imported.stderr
    restored=Store(Config(data_dir=str(destination)))
    try:
        from kindex.retrieve import hybrid_search,format_context_block
        a_hits=hybrid_search(restored,"Alphaowned routing",top_k=1,expand_graph=False)
        b_hits=hybrid_search(restored,"Betaowned routing",top_k=1,expand_graph=False)
        assert len(a_hits) == 1 and a_hits[0]["content"] == alpha["statement"]
        assert len(b_hits) == 1 and b_hits[0]["content"] == beta["statement"]
        a_context=format_context_block(restored,a_hits,query="Alphaowned routing",level="full",max_tokens_approx=4000)
        b_context=format_context_block(restored,b_hits,query="Betaowned routing",level="full",max_tokens_approx=4000)
        assert question["question"] in a_context
        assert question["question"] not in b_context
    finally:
        restored.close()
