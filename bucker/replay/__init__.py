"""Deterministic replay (BUILD_PLAN step 23).

Re-runs a task answering every model/tool call from stored outputs, never live.
Determinism here is record-and-replay, not a claim that LLMs are deterministic.
Reports match/mismatch against the original verification outcome.
"""
