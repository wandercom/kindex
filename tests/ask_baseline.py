"""`kin ask` option sets shared by the answer tests.

F25 is the benchmarked configuration (`ask.preset: benchmark`); the defaults
are for agents. Tests of one reading rule start from the pre-F25 baseline,
where every F25 option is off, and turn on what they test, so each option's own
effect stays isolated.
"""

from kindex.ask_presets import BENCHMARK_ASK

# The benchmarked `ask:` configuration (F25), as measured.
F25_ASK = dict(BENCHMARK_ASK)

# The F25 options at their previous (off) values.
PRE_F25_ASK = {
    'hard_effort': '',
    'dialogue_effort': '',
    'dialogue_plan': False,
    'reread': False,
    'max_input_tokens': 0,
    'details': False,
    'recall_only': False,
    'timeline_facets': False,
    'episode_scope': False,
    'dialogue_focus': False,
    'inference_candidates': False,
    'date_anchors': False,
    'count_scope': False,
    'count_readings': False,
    'predicate_counts': False,
    'quantity_readings': False,
    'window_readings': False,
    'relative_focus': False,
    'event_reference': False,
    'subject_scope': False,
    'approximate_state': False,
    'exchange_context': False,
    'window_search': False,
    'fact_tiers': 0,
    'dialogue_context_tokens': 0,
    'list_position': False,
    'component_updates': False,
    'remainder_quantities': False,
    'recall_candidates': False,
    'archival_time': False,
    'disposition_inference': False,
    'large_effort': '',
    'large_landmarks': False,
    'large_recall_exchange': False,
    'large_response_contract': False,
    'ordering_witnesses': False,
    'dialogue_fields': False,
    'episode_endpoints': False,
    'fixed_clock': False,
    'large_span_witnesses': False,
    'large_claim_witnesses': False,
    'large_summary_coverage': False,
    'witness_coverage': False,
    'count_witnesses': False,
    'coarse_ordering': False,
    'ordering_occurrences': False,
    'large_evidence_pack': False,
    'large_evidence_pack_intents': '',
    'archive_clock': False,
    'dialogue_episode_bindings': False,
    'dialogue_instance_identity': False,
    'count_example_membership': False,
    'large_claim_scan': False,
    'large_summary_methods': False,
    # Defaults the agent preset changed, at the values these tests were written against.
    'wide_context_tokens': 16000,
    'max_output_tokens': 16000,
    'timeout_seconds': 600.0,
    'premise_corrections': False,
    'digest_directives': True,
}
