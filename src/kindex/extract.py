"""LLM extraction pipeline — extracts structured knowledge from raw text.

Budget-aware: falls back to keyword extraction when over limit.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .budget import BudgetLedger
from .config import Config
from .llm import _estimate_cost


def _get_client(config: Config):
    # Key resolution has ONE authority: llm.resolve_api_key. This used to do a
    # bare os.environ.get(config.llm.api_key_env), which silently broke every
    # install using the comma-separated fallback syntax that llm.py has always
    # supported — a config of "JMC_OPENAI_API_KEY,OPENAI_API_KEY" looked up an
    # env var of that literal name, found nothing, and fell back to keyword
    # extraction forever. Two resolvers for one fact is how that happened.
    from .llm import get_client, resolve_api_key
    key, tried = resolve_api_key(config)
    # Auto-enable: if the API key exists in the environment, use LLM regardless
    # of config.llm.enabled.  Only skip when there's no key AND not explicitly enabled.
    if not key and not config.llm.enabled:
        return None
    if not key:
        # Explicitly enabled but no key
        import sys
        print(f"Warning: LLM enabled but none of {tried} are set. "
              f"Falling back to keyword extraction.", file=sys.stderr)
        return None
    if config.llm.enabled:
        return get_client(config)
    # Auto-enable path: llm.get_client refuses when `enabled` is false, so
    # build the same provider-aware client against a temporary enabled copy
    # rather than reimplementing provider dispatch here. This module used to
    # hardcode `import anthropic`, which sent an OpenAI key to Anthropic's SDK
    # whenever provider was openai.
    return get_client(config.model_copy(update={
        "llm": config.llm.model_copy(update={"enabled": True})}))


# ── LLM extraction ─────────────────────────────────────────────────────

EXTRACT_PROMPT = """Analyze this text and extract structured knowledge.

TEXT:
{text}

EXISTING GRAPH NODES (for linking, not duplication):
{existing_titles}

Return ONLY valid JSON:
{{
  "concepts": [
    {{"title": "short clear title", "content": "1-3 sentence summary", "domains": ["domain"], "type": "concept"}}
  ],
  "decisions": [
    {{"title": "what was decided", "rationale": "why", "type": "decision"}}
  ],
  "questions": [
    {{"question": "open question text", "context": "what prompted it", "type": "question"}}
  ],
  "connections": [
    {{"from_title": "source node", "to_title": "target node", "type": "relates_to", "why": "reason"}}
  ],
  "bridge_opportunities": [
    {{"concept_a": "node A", "concept_b": "node B", "potential_link": "why they might connect"}}
  ]
}}

Rules:
- Prefer linking to EXISTING nodes over creating new ones
- Only create new concept nodes for genuinely new ideas
- Bridge opportunities are cross-domain connections the user didn't explicitly make
- Keep titles plain and boring — good for retrieval, not for display
- Decisions must have rationale
- Questions must be genuinely open (not rhetorical)"""


def llm_extract(
    text: str,
    existing_titles: list[str],
    config: Config,
    ledger: BudgetLedger,
) -> dict | None:
    """Run LLM extraction pass. Returns structured dict or None if unavailable."""
    if not ledger.can_spend():
        return None

    client = _get_client(config)
    if client is None:
        return None

    titles_str = ", ".join(existing_titles[:100])
    prompt = EXTRACT_PROMPT.format(text=text[:4000], existing_titles=titles_str)

    try:
        response = client.messages.create(
            model=config.llm.model,
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )

        cost = _estimate_cost(
            config.llm.model,
            response.usage.input_tokens,
            response.usage.output_tokens,
        )
        ledger.record(cost, model=config.llm.model, purpose="extract",
                      tokens_in=response.usage.input_tokens,
                      tokens_out=response.usage.output_tokens)

        text_out = response.content[0].text
        # Extract JSON from response
        if "```json" in text_out:
            text_out = text_out.split("```json")[1].split("```")[0]
        elif "```" in text_out:
            text_out = text_out.split("```")[1].split("```")[0]

        return json.loads(text_out)
    except Exception:
        return None


# ── Keyword fallback ───────────────────────────────────────────────────

def keyword_extract(text: str, existing_titles: list[str] | None = None) -> dict:
    """Keyword-based extraction when LLM unavailable.

    Extracts concepts (capitalized phrases, noun phrases, quoted terms),
    decisions, questions, connections, and bridge opportunities.
    """
    if existing_titles is None:
        existing_titles = []
    existing_lower = {t.lower(): t for t in existing_titles}

    concepts = []
    connections = []
    decisions = []
    questions = []
    bridge_opportunities = []
    seen = set()

    # ── Concept extraction ─────────────────────────────────────────────

    # 1. Capitalized multi-word phrases (proper nouns / concepts)
    phrases = re.findall(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b', text)
    for phrase in phrases:
        lower = phrase.lower()
        if lower not in seen and len(phrase) > 5:
            seen.add(lower)
            concepts.append({
                "title": phrase,
                "content": "",
                "domains": [],
                "type": "concept",
            })

    # 2. Noun phrases: adjective+noun or noun+noun patterns (lowercase)
    noun_phrases = re.findall(
        r'\b([a-z]+(?:\s+[a-z]+){1,3})\s+(?:system|pattern|architecture|model|framework|'
        r'algorithm|method|approach|technique|protocol|strategy|mechanism|process|structure|'
        r'design|concept|principle|theory|analysis)\b',
        text, re.IGNORECASE,
    )
    for np_match in noun_phrases:
        # Reconstruct the full noun phrase with the head noun
        full_start = text.lower().find(np_match.lower())
        if full_start >= 0:
            # Find the head noun that follows
            after = text[full_start + len(np_match):].strip()
            head_match = re.match(
                r'(system|pattern|architecture|model|framework|algorithm|method|approach|'
                r'technique|protocol|strategy|mechanism|process|structure|design|concept|'
                r'principle|theory|analysis)\b', after, re.IGNORECASE)
            if head_match:
                full_phrase = f"{np_match} {head_match.group(1)}".strip()
                lower = full_phrase.lower()
                if lower not in seen and len(full_phrase) > 5:
                    seen.add(lower)
                    concepts.append({
                        "title": full_phrase,
                        "content": "",
                        "domains": [],
                        "type": "concept",
                    })

    # 3. Quoted terms
    quoted = re.findall(r'"([^"]{3,50})"', text)
    for q in quoted:
        lower = q.lower()
        if lower not in seen:
            seen.add(lower)
            concepts.append({
                "title": q,
                "content": "",
                "domains": [],
                "type": "concept",
            })

    # 4. "learned that ..." patterns
    learned_patterns = re.findall(
        r'(?:learned that|discovered that|found out that|realized that|'
        r'understood that|noticed that|need to investigate)\s+(.{10,100}?)(?:\.|$)',
        text, re.IGNORECASE,
    )
    for finding in learned_patterns:
        lower = finding.strip().lower()
        if lower not in seen:
            seen.add(lower)
            concepts.append({
                "title": finding.strip()[:60],
                "content": finding.strip(),
                "domains": [],
                "type": "concept",
            })

    # ── Decision extraction ────────────────────────────────────────────

    decision_patterns = re.findall(
        r'(?:decided to|chose to|went with|will use|choosing|opted for|'
        r'decision:\s*|decided that)\s+(.{5,120}?)(?:\.|;|$)',
        text, re.IGNORECASE,
    )
    for dec in decision_patterns:
        # Try to extract rationale from "because" / "since" clauses
        rationale = ""
        rat_match = re.search(r'\b(?:because|since|due to|so that)\s+(.+)', dec, re.IGNORECASE)
        if rat_match:
            rationale = rat_match.group(1).strip()
            title_part = dec[:dec.lower().find(rat_match.group(0).lower())].strip()
        else:
            title_part = dec.strip()
        decisions.append({
            "title": title_part[:60] if title_part else dec.strip()[:60],
            "rationale": rationale,
            "type": "decision",
        })

    # ── Question extraction ────────────────────────────────────────────

    for line in text.split('\n'):
        line = line.strip()
        if line.endswith('?') and len(line) > 10:
            questions.append({
                "question": line,
                "context": "",
                "type": "question",
            })

    # Also extract inline question patterns
    q_patterns = re.findall(
        r'(?:question about|wondering about|curious about|not sure (?:if|whether|how|why))\s+'
        r'(.{5,100}?)(?:\.|;|$)',
        text, re.IGNORECASE,
    )
    for q in q_patterns:
        q_text = q.strip()
        if not q_text.endswith('?'):
            q_text += '?'
        if not any(existing.get("question", "").lower() == q_text.lower() for existing in questions):
            questions.append({
                "question": q_text,
                "context": "",
                "type": "question",
            })

    # ── Connection detection ───────────────────────────────────────────

    conn_patterns = [
        r'(?:similar to|same as|connects to|related to|reminds me of)\s+["\']?(\w[\w\s]{2,30})',
        r'(\w[\w\s]{2,30})\s+(?:is like|is related to|is similar to)',
    ]
    for pattern in conn_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for m in matches:
            connections.append({
                "from_title": "",  # needs resolution
                "to_title": m.strip(),
                "type": "relates_to",
                "why": "detected in text",
            })

    # ── Bridge opportunity detection ───────────────────────────────────

    # 1. Explicit cross-domain patterns: "X is similar to Y", "X reminds me of Y",
    #    "like X but for Y"
    bridge_patterns = [
        # "X is similar to Y" / "X reminds me of Y"
        (r'(\w[\w\s]{2,30}?)\s+(?:is similar to|reminds me of|is like|is analogous to)\s+'
         r'(\w[\w\s]{2,30})'),
        # "like X but for Y"
        r'like\s+(\w[\w\s]{2,30}?)\s+but\s+for\s+(\w[\w\s]{2,30})',
    ]
    for pattern in bridge_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for m in matches:
            a, b = m[0].strip(), m[1].strip()
            if a and b and a.lower() != b.lower():
                bridge_opportunities.append({
                    "concept_a": a,
                    "concept_b": b,
                    "potential_link": f"Cross-domain analogy detected in text",
                })

    # 2. Concepts in the text that match existing graph titles
    all_extracted_titles = [c["title"].lower() for c in concepts]
    text_lower = text.lower()
    matching_existing = []
    for et_lower, et_original in existing_lower.items():
        if et_lower in text_lower and len(et_lower) > 3:
            matching_existing.append(et_original)

    # Bridge: connect newly extracted concepts to matching existing titles
    if matching_existing and concepts:
        for existing_title in matching_existing[:5]:
            for concept in concepts[:5]:
                if concept["title"].lower() != existing_title.lower():
                    bridge_opportunities.append({
                        "concept_a": concept["title"],
                        "concept_b": existing_title,
                        "potential_link": (
                            f"New concept '{concept['title']}' co-occurs with "
                            f"existing node '{existing_title}' in the same text"
                        ),
                    })

    # 3. Cross-paragraph concept bridging: concepts appearing in different paragraphs
    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
    if len(paragraphs) >= 2:
        para_concepts: list[set[str]] = []
        for para in paragraphs:
            para_lower = para.lower()
            found = set()
            for c in concepts:
                if c["title"].lower() in para_lower:
                    found.add(c["title"])
            para_concepts.append(found)

        # Find concepts that appear in different paragraphs (cross-topic bridging)
        for i in range(len(para_concepts)):
            for j in range(i + 1, len(para_concepts)):
                shared = para_concepts[i] & para_concepts[j]
                only_i = para_concepts[i] - shared
                only_j = para_concepts[j] - shared
                for ci in list(only_i)[:2]:
                    for cj in list(only_j)[:2]:
                        bridge_opportunities.append({
                            "concept_a": ci,
                            "concept_b": cj,
                            "potential_link": (
                                "Concepts appear in different sections of the same text, "
                                "suggesting a cross-domain connection"
                            ),
                        })

    return {
        "concepts": concepts[:10],
        "decisions": decisions[:5],
        "questions": questions[:5],
        "connections": connections[:5],
        "bridge_opportunities": bridge_opportunities[:10],
    }


def extract(
    text: str,
    existing_titles: list[str],
    config: Config,
    ledger: BudgetLedger,
) -> dict:
    """Extract knowledge — LLM if available, keyword fallback otherwise."""
    result = llm_extract(text, existing_titles, config, ledger)
    if result is not None:
        return result
    return keyword_extract(text, existing_titles=existing_titles)


# ── Session summarization ─────────────────────────────────────────────

SESSION_SUMMARIZE_PROMPT = """Summarize this Claude Code session concisely in 2-3 sentences.

SESSION TEXT:
{text}

Address these questions in your summary:
- What was the main goal of this session?
- What was accomplished?
- What decisions were made?
- What open questions remain?

Return ONLY the summary text, no formatting or labels."""


def llm_summarize_session(text: str, config: Config, ledger: BudgetLedger) -> str | None:
    """Use LLM to generate a concise summary of a session.

    Returns a 2-3 sentence summary, or None if LLM unavailable.
    """
    if not ledger.can_spend():
        return None

    client = _get_client(config)
    if client is None:
        return None

    prompt = SESSION_SUMMARIZE_PROMPT.format(text=text[:4000])

    try:
        response = client.messages.create(
            model=config.llm.model,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )

        cost = _estimate_cost(
            config.llm.model,
            response.usage.input_tokens,
            response.usage.output_tokens,
        )
        ledger.record(cost, model=config.llm.model, purpose="session-summarize",
                      tokens_in=response.usage.input_tokens,
                      tokens_out=response.usage.output_tokens)

        return response.content[0].text.strip()
    except Exception:
        return None
