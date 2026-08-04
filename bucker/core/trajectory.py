"""Trajectory export: the full trace of one run, in one file.

The LLM-ops piece: a task's event stream is the durable audit trail, but
debugging needs the *trajectory* — every model call with tokens, every
tool call, every verdict, in order, human-readable. This module renders a
task's event stream into:

  * a dict (the API / JSON export),
  * markdown (readable anywhere),
  * JSONL (one event per line — grep-able, machine-parseable).

Nothing here mutates state: the trajectory is a projection of the
append-only event store.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from bucker.core.eventstore import EventStore

#: Event types that carry model-call detail worth surfacing in the trace.
_MODEL_CALL_EVENTS = ("ModelCallCompleted", "ModelCallFailed")
_TOOL_EVENTS = ("ToolCallCompleted", "SandboxCommandCompleted")


def _summarize_payload(event_type: str, payload: dict) -> dict:
    """A stable, small projection of an event payload for the trace."""
    if event_type in _MODEL_CALL_EVENTS:
        return {
            "purpose": payload.get("purpose"),
            "model": payload.get("model"),
            "cost_usd": payload.get("cost_usd"),
            "latency_ms": payload.get("latency_ms"),
            "error": payload.get("error"),
        }
    if event_type in _TOOL_EVENTS:
        return {
            "tool": payload.get("tool"),
            "exit_code": payload.get("exit_code"),
            "secrets_redacted": payload.get("secrets_redacted"),
        }
    if event_type == "VerificationPassed" or event_type == "VerificationFailed":
        return {
            "verifier": payload.get("verifier"),
            "attempt": payload.get("attempt"),
            "duration_ms": payload.get("duration_ms"),
            "details": payload.get("details"),
        }
    if event_type == "CritiqueCompleted":
        return {
            "attempt": payload.get("attempt"),
            "verdict": payload.get("verdict"),
            "issues": payload.get("issues"),
            "repaired": payload.get("repaired"),
        }
    return dict(payload)


async def export_trajectory(
    task_id: UUID,
    store: EventStore,
    *,
    include_payloads: bool = True,
) -> dict[str, Any]:
    """The full trajectory of one task as a dict."""
    events = await store.read_stream(task_id)
    if not events:
        return {"task_id": str(task_id), "events": [], "summary": {}}

    trace_events = []
    for e in events:
        entry: dict[str, Any] = {
            "id": e.id,
            "event_type": e.event_type,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        if include_payloads:
            entry["payload"] = _summarize_payload(e.event_type, e.payload)
        trace_events.append(entry)

    summary = {
        "task_id": str(task_id),
        "events": len(events),
        "model_calls": sum(
            1 for e in events if e.event_type == "ModelCallCompleted"
        ),
        "failed_model_calls": sum(
            1 for e in events if e.event_type == "ModelCallFailed"
        ),
        "verifications": sum(
            1 for e in events
            if e.event_type in ("VerificationPassed", "VerificationFailed")
        ),
    }
    return {"task_id": str(task_id), "events": trace_events, "summary": summary}


def trajectory_to_markdown(trajectory: dict[str, Any]) -> str:
    """A readable trace: one line per event, model calls with tokens."""
    lines = [
        f"# Trajectory: {trajectory['task_id']}",
        "",
        f"- events: {trajectory['summary']['events']}",
        f"- model calls: {trajectory['summary']['model_calls']}",
        f"- failed model calls: {trajectory['summary']['failed_model_calls']}",
        f"- verifications: {trajectory['summary']['verifications']}",
        "",
        "| id | time | event | detail |",
        "|----|------|-------|--------|",
    ]
    for e in trajectory["events"]:
        payload = e.get("payload") or {}
        detail = ""
        if e["event_type"] in _MODEL_CALL_EVENTS:
            detail = (
                f"{payload.get('purpose', '')} {payload.get('model', '')} "
                f"${payload.get('cost_usd', 0)}"
                + (f" ERR: {payload.get('error', '')[:60]}" if payload.get("error") else "")
            )
        elif e["event_type"] in _TOOL_EVENTS:
            detail = f"{payload.get('tool', '')} exit={payload.get('exit_code')}"
        elif e["event_type"] in ("VerificationPassed", "VerificationFailed"):
            verdict = "PASSED" if e["event_type"] == "VerificationPassed" else "FAILED"
            detail = (f"{verdict} via {payload.get('verifier', '')} "
                      f"attempt {payload.get('attempt')}")
        elif e["event_type"] == "CritiqueCompleted":
            detail = (f"critic={payload.get('verdict')} issues="
                      f"{len(payload.get('issues') or [])} "
                      f"repaired={payload.get('repaired')}")
        else:
            detail = json.dumps(payload)[:100]
        lines.append(
            f"| {e['id']} | {e['created_at'] or ''} | {e['event_type']} | "
            f"{detail.replace('|', '/')} |"
        )
    return "\n".join(lines) + "\n"


def trajectory_to_jsonl(trajectory: dict[str, Any]) -> str:
    """One JSON object per line — grep-able, machine-parseable."""
    return "".join(
        json.dumps(e, default=str) + "\n" for e in trajectory["events"]
    )
