"""Named `ask:` presets.

`ask.preset: benchmark` is the configuration the published LoCoMo,
LongMemEval-S and BEAM-1M scores were measured with (F25; the reports are in
docs/benchmarks/). It reads more evidence, reasons harder and applies answering
rules written for those benchmarks' questions and judges, so it is not the
default: an agent asking about its own graph of decisions, code and sessions
uses the `agent` defaults in config.AskConfig. Set the preset, and pass the
question's date with `--as-of`, to reproduce a published score.
"""

# Every option whose benchmarked value differs from, or must not drift with,
# the agent defaults. Options set explicitly in user config still win.
BENCHMARK_ASK = {
    # Reasoning: more for counts and dates, and for every answer over
    # conversations between named people or a memory of over 1,000 nodes.
    "hard_effort": "high",
    "dialogue_effort": "xhigh",
    "dialogue_plan": True,
    "large_effort": "xhigh",
    # Budgets and limits.
    "max_input_tokens": 20000,
    "wide_context_tokens": 16000,
    "dialogue_context_tokens": 14000,
    "max_output_tokens": 16000,
    "timeout_seconds": 600.0,
    # A draft that declines is read again from whole excerpts.
    "reread": True,
    # Clocks frozen to the archive, for reproducible reruns of dated benchmarks.
    "fixed_clock": True,
    "archive_clock": True,
    "archival_time": True,
    # Answering rules written for the benchmarks' questions and judges.
    "premise_corrections": False,
    "details": True,
    "recall_only": True,
    "timeline_facets": True,
    "episode_scope": True,
    "dialogue_focus": True,
    "inference_candidates": True,
    "date_anchors": True,
    "count_scope": True,
    "count_readings": True,
    "predicate_counts": True,
    "quantity_readings": True,
    "window_readings": True,
    "relative_focus": True,
    "event_reference": True,
    "subject_scope": True,
    "approximate_state": True,
    "component_updates": True,
    "remainder_quantities": True,
    "recall_candidates": True,
    "disposition_inference": True,
    "digest_directives": True,
    # Conversation readings.
    "exchange_context": True,
    "window_search": True,
    "fact_tiers": 40,
    "list_position": True,
    "ordering_witnesses": True,
    "dialogue_fields": True,
    "episode_endpoints": True,
    "witness_coverage": True,
    "count_witnesses": True,
    "coarse_ordering": True,
    "ordering_occurrences": True,
    "dialogue_episode_bindings": True,
    "dialogue_instance_identity": True,
    "count_example_membership": True,
    # Large-memory (BEAM-1M) readings.
    "large_landmarks": True,
    "large_recall_exchange": True,
    "large_response_contract": True,
    "large_span_witnesses": True,
    "large_claim_witnesses": True,
    "large_summary_coverage": True,
    "large_evidence_pack": True,
    "large_evidence_pack_intents": "aggregation,assistant_recall",
    "large_claim_scan": True,
    "large_summary_methods": True,
}
