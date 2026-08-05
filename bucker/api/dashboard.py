"""Server-rendered dashboard pages (BUILD_PLAN step 33).

Pure functions: every one takes data and returns an HTML string. No I/O,
no database access, no app imports — the routes in ``bucker.api.app`` do the
querying and call these. That keeps the pages testable with plain function
calls and lets the API tests keep patching module globals.

No React, no frontend framework, no build step. One HTML page answers the
debugging question: what happened, why, how long, how much.
"""

from __future__ import annotations

import html
from typing import Any

# ------------------------------------------------------------------- theme --

CSS = """
:root {
  --bg: #0d1117; --panel: #161b22; --panel2: #1c2333;
  --border: #2d333b; --text: #e6edf3; --muted: #8b949e;
  --blue: #58a6ff; --green: #3fb950; --red: #f85149; --amber: #d29922;
  --purple: #bc8cff; --cyan: #39c5cf; --orange: #f0883e; --pink: #f778ba;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
       font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
a { color: var(--blue); text-decoration: none; }
a:hover { text-decoration: underline; }
.wrap { max-width: 1080px; margin: 0 auto; padding: 24px 20px 60px; }
header.top { display: flex; align-items: baseline; gap: 14px;
             border-bottom: 1px solid var(--border); padding-bottom: 14px;
             margin-bottom: 22px; }
header.top h1 { margin: 0; font-size: 20px; letter-spacing: -0.02em; }
header.top .tag { color: var(--muted); font-size: 12.5px; }
header.top .spacer { flex: 1; }
.badge { display: inline-block; padding: 2px 9px; border-radius: 999px;
         font-size: 11.5px; font-weight: 600; letter-spacing: 0.03em;
         border: 1px solid var(--border); text-transform: uppercase; }
.badge.completed { color: var(--green); border-color: rgba(63,185,80,.45);
                   background: rgba(63,185,80,.08); }
.badge.failed, .badge.verification_failed { color: var(--red);
                   border-color: rgba(248,81,73,.45); background: rgba(248,81,73,.08); }
.badge.in_progress, .badge.pending { color: var(--blue);
                   border-color: rgba(88,166,255,.45); background: rgba(88,166,255,.08); }
.badge.halted { color: var(--orange); border-color: rgba(240,136,62,.45);
                background: rgba(240,136,62,.08); }
.badge.needs_human_review { color: var(--purple);
                   border-color: rgba(188,140,255,.45); background: rgba(188,140,255,.08); }
.badge.unknown { color: var(--muted); }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
         gap: 12px; margin-bottom: 26px; }
.card { background: var(--panel); border: 1px solid var(--border);
        border-radius: 10px; padding: 14px 16px; }
.card .k { color: var(--muted); font-size: 11.5px; text-transform: uppercase;
           letter-spacing: 0.06em; }
.card .v { font-size: 24px; font-weight: 650; margin-top: 4px;
           font-variant-numeric: tabular-nums; }
.card .v.small { font-size: 17px; }
.panel { background: var(--panel); border: 1px solid var(--border);
         border-radius: 10px; padding: 18px 20px; margin-bottom: 22px; }
.panel h2 { margin: 0 0 14px; font-size: 15px; letter-spacing: 0.02em;
            color: var(--text); }
.panel h2 .hint { color: var(--muted); font-weight: 400; font-size: 12px; }
table { width: 100%; border-collapse: collapse; }
th { text-align: left; color: var(--muted); font-size: 11.5px;
     text-transform: uppercase; letter-spacing: 0.05em; padding: 6px 10px;
     border-bottom: 1px solid var(--border); }
td { padding: 9px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
tr:last-child td { border-bottom: none; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.mono { font-family: ui-monospace, "Cascadia Code", Consolas, monospace;
        font-size: 12.5px; }
.muted { color: var(--muted); }
.meta { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
        gap: 10px 18px; }
.meta dt { color: var(--muted); font-size: 11.5px; text-transform: uppercase;
           letter-spacing: 0.05em; }
.meta dd { margin: 2px 0 0; font-size: 13.5px; word-break: break-word; }
/* timeline */
.timeline { list-style: none; margin: 0; padding: 0; }
.timeline li { position: relative; padding: 7px 0 7px 26px;
               border-left: 2px solid var(--border); margin-left: 7px; }
.timeline li .dot { position: absolute; left: -7px; top: 11px; width: 12px;
                    height: 12px; border-radius: 50%; background: var(--muted);
                    border: 2px solid var(--bg); }
.timeline li .when { color: var(--muted); font-size: 11.5px; }
.timeline li .what { font-weight: 600; font-size: 13px; }
.timeline li .detail { color: var(--muted); font-size: 12.5px; margin-top: 1px; }
.timeline li .ref { font-size: 11px; }
/* bars */
.bar-row { display: flex; align-items: center; gap: 10px; margin: 6px 0; }
.bar-row .label { width: 150px; color: var(--muted); font-size: 12.5px;
                  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bar-row .track { flex: 1; height: 12px; background: var(--panel2);
                  border-radius: 6px; overflow: hidden; }
.bar-row .fill { height: 100%; border-radius: 6px; background: var(--blue); }
.bar-row .val { width: 90px; text-align: right; font-size: 12px;
                font-variant-numeric: tabular-nums; }
/* buttons & forms */
button, .btn { background: var(--panel2); color: var(--text);
               border: 1px solid var(--border); border-radius: 8px;
               padding: 8px 16px; font-size: 13px; cursor: pointer; }
button:hover, .btn:hover { border-color: var(--blue); text-decoration: none; }
button.primary { background: #1f6feb; border-color: #1f6feb; font-weight: 600; }
button.primary:hover { background: #388bfd; }
button:disabled { opacity: .5; cursor: wait; }
input[type=text], textarea, select { background: var(--panel2); color: var(--text);
    border: 1px solid var(--border); border-radius: 8px; padding: 8px 12px;
    font: inherit; width: 100%; }
label.fld { display: block; margin-bottom: 12px; }
label.fld span { display: block; color: var(--muted); font-size: 11.5px;
                 text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 4px; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
@media (max-width: 720px) { .grid2 { grid-template-columns: 1fr; } }
pre { background: var(--panel2); border: 1px solid var(--border);
      border-radius: 8px; padding: 12px 14px; overflow: auto;
      font-size: 12.5px; max-height: 420px; }
.alert { border-radius: 8px; padding: 12px 16px; margin: 14px 0; font-size: 13px; }
.alert.err { background: rgba(248,81,73,.08); border: 1px solid rgba(248,81,73,.4); }
.alert.ok { background: rgba(63,185,80,.08); border: 1px solid rgba(63,185,80,.4); }
/* landing hero */
.hero { background: linear-gradient(180deg, var(--panel2), var(--panel));
        border: 1px solid var(--border); border-radius: 12px;
        padding: 18px 20px; margin-bottom: 22px; }
.hero h2 { margin: 0 0 6px; font-size: 15px; }
.hero p { margin: 0; color: var(--muted); font-size: 13px; }
.steps { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
         gap: 10px; margin-top: 12px; }
.steps .step { background: var(--panel); border: 1px solid var(--border);
               border-radius: 8px; padding: 10px 12px; font-size: 12.5px;
               color: var(--muted); }
.steps .step b { display: block; color: var(--text); font-size: 13px; margin-bottom: 2px; }
/* verdict banner */
.banner { border-radius: 10px; padding: 14px 18px; margin-bottom: 20px;
          font-size: 15px; font-weight: 650; display: flex; align-items: center;
          gap: 10px; }
.banner.passed { background: rgba(63,185,80,.1); border: 1px solid rgba(63,185,80,.5);
                 color: var(--green); }
.banner.failed { background: rgba(248,81,73,.1); border: 1px solid rgba(248,81,73,.5);
                 color: var(--red); }
.banner .sub { font-weight: 400; font-size: 12.5px; color: var(--muted); }
/* form sections */
.sec-title { margin: 22px 0 10px; font-size: 11.5px; text-transform: uppercase;
             letter-spacing: 0.08em; color: var(--muted);
             border-top: 1px solid var(--border); padding-top: 14px; }
.sec-title:first-of-type { border-top: none; margin-top: 4px; padding-top: 0; }
.check { display: flex; align-items: flex-start; gap: 8px; font-size: 13px;
         color: var(--muted); margin: 4px 0 16px; }
.check input { width: auto; margin-top: 3px; }
.tmpl-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
             gap: 10px; margin: 8px 0 18px; }
.tmpl { display: flex; flex-direction: column; gap: 6px; text-align: left; cursor: pointer;
        background: rgba(255,255,255,.03); border: 1px solid var(--border);
        border-radius: 8px; padding: 12px; font: inherit; color: inherit; }
.tmpl:hover { border-color: rgba(112,170,255,.6); background: rgba(112,170,255,.06); }
.tmpl b { font-size: 14px; }
.tmpl span { font-size: 12px; color: var(--muted); line-height: 1.4; }
.tmpl code { font-size: 11px; color: #7ee0a3; }
tr.cfg td { background: rgba(126,224,163,.06); }
/* empty state */
.cta { text-align: center; padding: 34px 20px; }
.cta .big { font-size: 16px; font-weight: 600; margin-bottom: 6px; }
/* system page status rows */
.status-row { display: flex; align-items: center; gap: 10px; padding: 8px 0;
              border-bottom: 1px solid var(--border); font-size: 13px; }
.status-row:last-child { border-bottom: none; }
.status-row .label { width: 150px; color: var(--muted); text-transform: capitalize; }
.status-row .detail { color: var(--muted); font-size: 12.5px; }
.pill { display: inline-block; padding: 1px 8px; border-radius: 6px;
        font-size: 11px; background: var(--panel2); border: 1px solid var(--border);
        color: var(--muted); }
footer { margin-top: 30px; color: var(--muted); font-size: 12px;
         border-top: 1px solid var(--border); padding-top: 14px; }
"""

#: status -> badge css class
_STATUS_CLASS = {
    "completed": "completed", "failed": "failed", "verification_failed": "verification_failed",
    "in_progress": "in_progress", "pending": "pending", "halted": "halted",
    "needs_human_review": "needs_human_review",
}

#: event type -> (dot color, short label)
_EVENT_STYLE = {
    "TaskCreated": ("#58a6ff", "created"),
    "TaskStarted": ("#58a6ff", "started"),
    "TaskCompleted": ("#3fb950", "completed"),
    "TaskFailed": ("#f85149", "failed"),
    "PlanRequested": ("#bc8cff", "plan requested"),
    "PlanGenerated": ("#bc8cff", "plan generated"),
    "SchemaValidationFailed": ("#f85149", "schema invalid"),
    "StepStarted": ("#58a6ff", "step started"),
    "StepCompleted": ("#3fb950", "step completed"),
    "ToolCallCompleted": ("#39c5cf", "tool call"),
    "ModelCallCompleted": ("#bc8cff", "model call"),
    "ModelCallFailed": ("#f85149", "model call failed"),
    "WorkerCompleted": ("#39c5cf", "worker done"),
    "VerificationRequested": ("#d29922", "verification"),
    "VerificationPassed": ("#3fb950", "verified"),
    "VerificationFailed": ("#f85149", "verification failed"),
    "RetryScheduled": ("#d29922", "retry scheduled"),
    "NeedsHumanReview": ("#bc8cff", "needs human"),
    "BudgetExceeded": ("#f0883e", "budget exceeded"),
    "DeadlineExceeded": ("#f0883e", "deadline exceeded"),
    "CorrectionApplied": ("#f778ba", "correction"),
    "RedactionApplied": ("#f778ba", "redaction"),
}

#: event types that carry a per-call cost in their payload
_COST_EVENTS = {"ModelCallCompleted", "WorkerCompleted"}


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _badge(status: str) -> str:
    cls = _STATUS_CLASS.get(status, "unknown")
    return f'<span class="badge {cls}">{_esc(status.replace("_", " "))}</span>'


def _money(value: Any) -> str:
    try:
        return f"${float(value):,.4f}"
    except (TypeError, ValueError):
        return "—"


def _short_id(task_id: str) -> str:
    return task_id[:8] if len(task_id) > 8 else task_id


def _fmt_tokens(value: Any) -> str:
    """Human-friendly token count: 12.4k, 1.2M, 340."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _fmt_time(iso: str | None) -> str:
    if not iso:
        return "—"
    t = iso[:19].replace("T", " ")
    return t


def _page(title: str, body: str, *, extra_js: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)} · bucker-agent</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🛡️</text></svg>">
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
{body}
<footer>bucker-agent — every task is an append-only event stream; state is a replay of history. · <a href="/docs">api docs</a></footer>
</div>
{extra_js}
</body>
</html>"""


# ------------------------------------------------------------------ index --

def render_index(stats: dict, tasks: list[dict]) -> str:
    """Landing page: aggregate numbers, cost by type, tasks per day, recent list."""
    by_status = stats.get("by_status", {})
    status_order = (
        "in_progress", "pending", "completed", "verification_failed",
        "failed", "halted", "needs_human_review",
    )
    status_cells = "".join(
        f'<div class="card"><div class="k">{_esc(s.replace("_", " "))}</div>'
        f'<div class="v small">{by_status.get(s, 0)}</div></div>'
        for s in status_order
    )

    cost_rows = "".join(
        f'<div class="bar-row"><span class="label">{_esc(k)}</span>'
        f'<span class="track"><span class="fill" style="width:{pct:.1f}%"></span></span>'
        f'<span class="val">{_money(v)}</span></div>'
        for k, v, pct in stats.get("cost_by_type", [])
    ) or '<div class="muted">no spend recorded yet</div>'

    day_rows = "".join(
        f'<div class="bar-row"><span class="label">{_esc(d)}</span>'
        f'<span class="track"><span class="fill" style="width:{pct:.1f}%"></span></span>'
        f'<span class="val">{n} task{"s" if n != 1 else ""}</span></div>'
        for d, n, pct in stats.get("per_day", [])
    ) or '<div class="muted">no tasks yet</div>'

    rows = "".join(
        f"<tr>"
        f'<td class="mono"><a href="/tasks/{_esc(t["id"])}/dashboard">{_short_id(t["id"])}</a></td>'
        f"<td>{_esc(t.get('task_type', ''))}</td>"
        f"<td>{_badge(t.get('status', ''))}</td>"
        f'<td>{_esc(t.get("objective", ""))[:80]}</td>'
        f'<td class="num">{_fmt_tokens(t.get("total_tokens", 0))}</td>'
        f'<td class="num">{_money(t.get("cost_usd", 0))}</td>'
        f'<td class="num">{_fmt_time(t.get("created_at", ""))}</td>'
        f"</tr>"
        for t in tasks
    ) or '<tr><td colspan="7" class="muted">no tasks yet — POST /tasks to create one</td></tr>'

    if stats.get("total", 0) == 0:
        empty_cta = (
            '<div class="panel cta"><div class="big">no tasks yet</div>'
            '<div class="muted">Create your first task — it runs the real pipeline: '
            "planner → worker → verifier.</div>"
            '<p style="margin-top:14px"><a class="btn primary" href="/tasks/new">'
            "+ new task</a></p></div>"
        )
    else:
        empty_cta = ""

    body = f"""
<header class="top">
  <h1>bucker-agent</h1>
  <span class="tag">nothing is trusted until it's verified</span>
  <span class="spacer"></span>
  <a class="btn" href="/usage">usage</a>
  <a class="btn" href="/system">system</a>
  <a class="btn" href="/tasks/new">+ new task</a>
  <a class="btn" href="/docs">api</a>
</header>

<div class="hero">
  <h2>plan → work → verify</h2>
  <p>Every task is an append-only event stream. A planner turns your goal into a
  typed contract, a worker does the work in a network-isolated container, and a
  verifier — not the model — decides whether it's correct.</p>
  <div class="steps">
    <div class="step"><b>1 · Plan</b>fuzzy goal → typed contract with budget, deadline and verifier</div>
    <div class="step"><b>2 · Work</b>a model edits files in an isolated container; the diff is applied</div>
    <div class="step"><b>3 · Verify</b>tests decide; every step is recorded and replayable</div>
  </div>
</div>

{empty_cta}

<div class="cards">
  <div class="card"><div class="k">Total tasks</div><div class="v">{stats.get("total", 0)}</div></div>
  <div class="card"><div class="k">Success rate</div><div class="v">{stats.get("success_rate", 0):.0%}</div></div>
  <div class="card"><div class="k">Total spend</div><div class="v">{_money(stats.get("total_cost", 0))}</div></div>
  <div class="card"><div class="k">Total tokens</div><div class="v small">{_fmt_tokens(stats.get("total_tokens", 0))}</div></div>
  <div class="card"><div class="k">Avg / task</div><div class="v small">{_money(stats.get("avg_cost", 0))}</div></div>
  {status_cells}
</div>

<div class="grid2">
  <div class="panel"><h2>Cost by task type</h2>{cost_rows}</div>
  <div class="panel"><h2>Tasks per day <span class="hint">last 7 days</span></h2>{day_rows}</div>
</div>

<div class="panel">
  <h2>Recent tasks</h2>
  <table>
    <thead><tr><th>id</th><th>type</th><th>status</th><th>objective</th>
      <th class="num">tokens</th><th class="num">cost</th><th class="num">created</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>
"""
    return _page("Overview", body)


# ------------------------------------------------------------- system page --

def _status_badge(check: dict) -> str:
    """up/down/unknown badge for one health check row."""
    if check.get("ok") is True:
        return '<span class="badge completed">up</span>'
    if check.get("ok") is False:
        return '<span class="badge failed">down</span>'
    return '<span class="badge unknown">unknown</span>'


def render_system_page(status: dict) -> str:
    """Control center: model chain, providers, infrastructure, platform.

    Pure function of the status dict the API computes. Secrets are never
    rendered — provider checks report key *shape*, never the key.
    """
    model = status.get("model", {})
    fallbacks = model.get("fallbacks") or []
    fb_html = ", ".join(_esc(m) for m in fallbacks) or \
        '<span class="muted">none — no fallback configured</span>'

    degraded_html = ""
    if status.get("degraded"):
        degraded_html = (
            '<div class="alert err"><b>DEGRADED MODE</b> — the database pool '
            "failed at startup. Data routes answer 503. Fix Postgres "
            "(docker compose up -d) and run "
            "<code>uv run python -m bucker.cli migrate</code>, then restart "
            "the API.</div>"
        )

    providers = status.get("providers", {})
    provider_rows = "".join(
        f'<div class="status-row"><span class="label">{_esc(name)}</span>'
        f'{_status_badge(info)}'
        f'<span class="detail">{_esc(info.get("detail", ""))}</span></div>'
        for name, info in sorted(providers.items())
    ) or '<div class="muted">no external providers configured</div>'

    infra = status.get("infra", {})
    infra_rows = "".join(
        f'<div class="status-row"><span class="label">{_esc(name)}</span>'
        f'{_status_badge(info)}'
        f'<span class="detail">{_esc(info.get("detail", ""))}</span></div>'
        for name, info in infra.items()
    ) or '<div class="muted">no infrastructure checks</div>'

    platform = status.get("platform", {})
    verifiers = platform.get("verifiers") or []
    pills = "".join(f'<span class="pill" style="margin:2px 4px 2px 0">{_esc(v)}</span>'
                    for v in verifiers) or '<span class="muted">none registered</span>'
    recordings = platform.get("recordings")
    tasks = platform.get("tasks")

    body = f"""
<header class="top">
  <h1>system</h1>
  <span class="tag">control center</span>
  <span class="spacer"></span>
  <a class="btn" href="/">overview</a>
  <a class="btn" href="/usage">usage</a>
  <a class="btn" href="/models-page">models</a>
  <a class="btn" href="/memory-page">memory</a>
  <a class="btn" href="/skills-page">skills</a>
  <a class="btn" href="/schedules-page">schedules</a>
  <a class="btn" href="/system">system</a>
  <a class="btn" href="/tasks/new">+ new task</a>
</header>

{degraded_html}

<div class="grid2">
  <div class="panel"><h2>Model</h2>
  <dl class="meta">
    <dt>Primary</dt><dd class="mono">{_esc(model.get("primary", "—"))}</dd>
    <dt>Fallbacks</dt><dd class="mono">{fb_html}</dd>
    <dt>Mode</dt><dd>{_esc(model.get("mode", "—"))}</dd>
    <dt>Planner tokens</dt><dd>{model.get("max_tokens_planner", "—")}</dd>
    <dt>Worker tokens</dt><dd>{model.get("max_tokens_worker", "—")}</dd>
  </dl></div>

  <div class="panel"><h2>Providers <span class="hint">shape only — keys are never shown</span></h2>
  {provider_rows}</div>
</div>

<div class="panel"><h2>Infrastructure</h2>{infra_rows}</div>

<div class="grid2">
  <div class="panel"><h2>Verifiers</h2>{pills}</div>
  <div class="panel"><h2>Storage</h2>
  <dl class="meta">
    <dt>Recordings</dt><dd>{recordings if recordings is not None else "—"}</dd>
    <dt>Tasks in DB</dt><dd>{tasks if tasks is not None else "—"}</dd>
  </dl></div>
</div>
"""
    return _page("System", body)


# --------------------------------------------------------- schedules page --


def render_schedules_page(
    schedules: list,
    *,
    templates: list | None = None,
    temporal_ok: bool = True,
) -> str:
    """Recurring tasks: what runs, when, and a form to add one.

    Schedules live in Temporal (the durable source of truth); this page
    renders them + the creation form (template + cron). The create/delete
    buttons hit the JSON API from the browser.
    """
    templates = templates or []

    if not temporal_ok:
        rows = ('<div class="alert err"><b>Temporal is not reachable</b> — '
                "schedules are stored in Temporal, so they cannot be listed "
                "or created until it is running "
                "(<code>temporal server start-dev</code>).</div>")
    elif not schedules:
        rows = '<div class="muted">no schedules yet — create one below</div>'
    else:
        rows = "".join(
            f'<div class="status-row"><span class="label mono">{_esc(s["schedule_id"])}</span>'
            f'<span class="detail">{"paused" if s.get("paused") else "active"}</span>'
            f'<span class="detail">'
            f'<button class="mini" onclick="delSchedule(\'{_esc(s["schedule_id"])}\')">delete</button>'
            f"</span></div>"
            for s in schedules
        )

    template_opts = "".join(
        f'<option value="{_esc(t["id"])}">{_esc(t["name"])} — {_esc(t.get("default_budget_usd") or "default")} USD</option>'
        for t in templates
    )
    if not template_opts:
        template_opts = '<option value="">(no templates registered)</option>'

    body = """
<header class="top">
  <h1>schedules</h1>
  <span class="tag">recurring verified tasks</span>
  <span class="spacer"></span>
  <a class="btn" href="/">overview</a>
  <a class="btn" href="/tasks/new">+ new task</a>
</header>

%(rows)s

<div class="panel">
  <h2>Create a schedule</h2>
  <form id="sf" onsubmit="createSchedule(event)">
    <div class="grid2">
      <label class="fld"><span>Schedule id <span class="muted">— stable, e.g. nightly-bench</span></span>
        <input name="schedule_id" required minlength="3" placeholder="nightly-bench"></label>
      <label class="fld"><span>Cron <span class="muted">— 5 fields, e.g. 0 9 * * 1-5</span></span>
        <input name="cron" required value="0 9 * * *" placeholder="0 9 * * *"></label>
      <label class="fld"><span>Template</span>
        <select name="template">%(template_opts)s</select></label>
      <label class="fld"><span>Objective override <span class="muted">— empty uses the template's</span></span>
        <input name="objective" placeholder="(optional)"></label>
      <label class="fld"><span>Budget (USD) <span class="muted">— per run</span></span>
        <input type="number" name="budget_usd" step="0.01" min="0" placeholder="template default"></label>
      <label class="fld"><span>Deadline (minutes) <span class="muted">— per run</span></span>
        <input type="number" name="deadline_minutes" min="1" placeholder="template default"></label>
    </div>
    <button type="submit" class="primary">Create schedule</button>
  </form>
  <div id="sout"></div>
</div>

<script>
async function createSchedule(ev) {
  ev.preventDefault();
  const params = new URLSearchParams(new FormData(ev.target));
  const out = document.getElementById('sout');
  try {
    const resp = await fetch('/schedules?' + params.toString(), { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) {
      out.innerHTML = '<div class="alert err">' + (data.detail || resp.status) + '</div>';
      return;
    }
    out.innerHTML = '<div class="alert ok">schedule <b>' + data.schedule_id + '</b> created on ' + data.cron + '</div>';
    setTimeout(() => location.reload(), 800);
  } catch (err) {
    out.innerHTML = '<div class="alert err">' + err + '</div>';
  }
}
async function delSchedule(id) {
  const out = document.getElementById('sout');
  try {
    const resp = await fetch('/schedules/' + id, { method: 'DELETE' });
    if (resp.ok) { location.reload(); }
    else { const d = await resp.json(); out.innerHTML = '<div class="alert err">' + (d.detail || resp.status) + '</div>'; }
  } catch (err) {
    out.innerHTML = '<div class="alert err">' + err + '</div>';
  }
}
</script>
""" % {"rows": rows, "template_opts": template_opts}
    return _page("Schedules", body)


# -------------------------------------------------------------- models page --


def render_models_page(
    catalog: list,
    *,
    configured_chain: list | None = None,
    providers: dict | None = None,
    suggested_chain: list | None = None,
) -> str:
    """Browse the model catalog: free vs paid vs local, what's configured.

    The OmniRoute-inspired "which API am I using" surface: tiers as
    badges, the configured chain with live provider health, and the
    setup wizard's suggestion. Pure function of the data the API passes.
    """
    configured_chain = configured_chain or []
    providers = providers or {}
    suggested_chain = suggested_chain or []
    configured_ids = {m["id"] for m in configured_chain}

    tier_badge = {
        "local": '<span class="pill" style="color:#7ee0a3;border-color:rgba(126,224,163,.4)">local</span>',
        "free": '<span class="pill" style="color:#ffd479;border-color:rgba(255,212,121,.4)">free</span>',
        "paid": '<span class="pill" style="color:#ff9e9e;border-color:rgba(255,158,158,.4)">paid</span>',
    }
    rows = "".join(
        f'<tr class="{"cfg" if m.id in configured_ids else ""}">'
        f"<td>{tier_badge.get(m.tier, m.tier)}</td>"
        f'<td class="mono">{_esc(m.id)}</td>'
        f'<td>{_esc(m.name)}</td>'
        f'<td class="num">{m.context:,}</td>'
        f'<td class="muted">{_esc(m.notes)}</td>'
        f'<td>{"<b>configured</b>" if m.id in configured_ids else ""}</td>'
        f"</tr>"
        for m in catalog
    )

    chain_html = "".join(
        f'<div class="status-row"><span class="label mono">{_esc(c["id"])}</span>'
        f'{tier_badge.get(c["tier"], c["tier"])}'
        f'<span class="detail">{_esc(c["provider"])}</span></div>'
        for c in configured_chain
    ) or '<div class="muted">no models configured</div>'

    prov_rows = "".join(
        f'<div class="status-row"><span class="label">{_esc(name)}</span>'
        f'{_status_badge(info)}'
        f'<span class="detail">{_esc(info.get("detail", ""))}</span></div>'
        for name, info in sorted(providers.items())
    ) or '<div class="muted">no providers configured</div>'

    suggest_html = (
        '<div class="status-row"><span class="label">suggested free-first chain</span>'
        f'<span class="detail mono">{" → ".join(_esc(m) for m in suggested_chain)}</span></div>'
        if suggested_chain else ""
    )

    body = f"""
<header class="top">
  <h1>models</h1>
  <span class="tag">free · paid · local</span>
  <span class="spacer"></span>
  <a class="btn" href="/">overview</a>
  <a class="btn" href="/usage">usage</a>
  <a class="btn" href="/schedules-page">schedules</a>
</header>

<div class="grid2">
  <div class="panel"><h2>Configured chain</h2>{chain_html}</div>
  <div class="panel"><h2>Providers <span class="hint">key shape only</span></h2>{prov_rows}</div>
</div>

<div class="panel">{suggest_html}</div>

<div class="panel">
  <h2>Model catalog</h2>
  <table>
    <thead><tr><th>tier</th><th>model id</th><th>name</th>
      <th class="num">context</th><th>notes</th><th></th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>
"""
    return _page("Models", body)


# -------------------------------------------------------------- skills page --


def render_skills_page(skills: list) -> str:
    """Procedural memory: what the worker knows how to do, viewable/editable.

    Skills are the harness's learned procedures (Hermes-style SKILL.md).
    The page lists them with a create form that hits the JSON API.
    """
    if skills:
        rows = "".join(
            f'<div class="status-row"><span class="label mono">{_esc(s["name"])}</span>'
            f'<span class="detail">{_esc(s["description"])}</span></div>'
            for s in skills
        )
    else:
        rows = ('<div class="muted">no skills yet — skills are procedures the '
                "worker follows when an objective matches. Create one below "
                "or with <code>bucker skills new</code>.</div>")

    body = """
<header class="top">
  <h1>skills</h1>
  <span class="tag">procedural memory</span>
  <span class="spacer"></span>
  <a class="btn" href="/">overview</a>
  <a class="btn" href="/memory-page">memory</a>
</header>

<div class="panel"><h2>Skills</h2>%(rows)s</div>

<div class="panel">
  <h2>Add a skill</h2>
  <form id="sf" onsubmit="createSkill(event)">
    <label class="fld"><span>Name <span class="muted">— slug, e.g. fix-failing-tests</span></span>
      <input name="name" required minlength="3"></label>
    <label class="fld"><span>Description <span class="muted">— when the worker should use it</span></span>
      <input name="description" required minlength="3"></label>
    <label class="fld"><span>Procedure <span class="muted">— steps, one per line</span></span>
      <textarea name="procedure" rows="6" required minlength="3"></textarea></label>
    <button type="submit" class="primary">Create skill</button>
  </form>
  <div id="sout"></div>
</div>

<script>
async function createSkill(ev) {
  ev.preventDefault();
  const params = new URLSearchParams(new FormData(ev.target));
  const out = document.getElementById('sout');
  try {
    const resp = await fetch('/skills?' + params.toString(), { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) {
      out.innerHTML = '<div class="alert err">' + (data.detail || resp.status) + '</div>';
      return;
    }
    out.innerHTML = '<div class="alert ok">skill <b>' + data.skill.name + '</b> created</div>';
    setTimeout(() => location.reload(), 800);
  } catch (err) {
    out.innerHTML = '<div class="alert err">' + err + '</div>';
  }
}
</script>
""" % {"rows": rows}
    return _page("Skills", body)


# -------------------------------------------------------------- memory page --


def render_memory_page(facts: list) -> str:
    """Semantic memory: durable facts, viewable and searchable.

    The harness's long-term memory (Hermes-style): facts persist across
    sessions, injected into planner/worker context when relevant.
    """
    if facts:
        rows = "".join(
            f'<div class="status-row"><span class="label mono">{_esc(f["id"][:8])}</span>'
            f'<span class="detail">{_esc(f["text"])}</span>'
            f'<span class="detail muted">{_esc(f["source"])}</span></div>'
            for f in facts
        )
    else:
        rows = ('<div class="muted">no facts yet — add them with '
                "<code>bucker memory add \"&lt;durable fact&gt;\"</code> "
                "or the form below.</div>")

    body = """
<header class="top">
  <h1>memory</h1>
  <span class="tag">semantic memory</span>
  <span class="spacer"></span>
  <a class="btn" href="/">overview</a>
  <a class="btn" href="/skills-page">skills</a>
</header>

<div class="panel"><h2>Facts</h2>%(rows)s</div>

<div class="panel">
  <h2>Add a fact</h2>
  <form id="mf" onsubmit="createFact(event)">
    <label class="fld"><span>Fact <span class="muted">— one durable truth, e.g. 'tests run with pytest'</span></span>
      <input name="text" required minlength="1"></label>
    <button type="submit" class="primary">Store fact</button>
  </form>
  <div id="mout"></div>
</div>

<script>
async function createFact(ev) {
  ev.preventDefault();
  const params = new URLSearchParams(new FormData(ev.target));
  const out = document.getElementById('mout');
  try {
    const resp = await fetch('/memory?' + params.toString(), { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) {
      out.innerHTML = '<div class="alert err">' + (data.detail || resp.status) + '</div>';
      return;
    }
    out.innerHTML = '<div class="alert ok">stored fact ' + data.fact_id.slice(0, 8) + '</div>';
    setTimeout(() => location.reload(), 800);
  } catch (err) {
    out.innerHTML = '<div class="alert err">' + err + '</div>';
  }
}
</script>
""" % {"rows": rows}
    return _page("Memory", body)


# ------------------------------------------------------------- usage page --

def render_usage_page(usage: dict) -> str:
    """Token + cost usage: which model burned what, day by day.

    Pure function of the usage dict the API computes. This is the page that
    answers "which API am I using and how many tokens have I used".
    """
    total_tokens = usage.get("total_tokens", 0)
    total_cost = usage.get("total_cost", 0)
    total_calls = usage.get("total_calls", 0)
    week_tokens = usage.get("week_tokens", 0)
    week_cost = usage.get("week_cost", 0)

    model_rows = usage.get("by_model", []) or []
    model_table = "".join(
        f"<tr>"
        f'<td class="mono">{_esc(m["model"])}</td>'
        f'<td class="num">{m["calls"]}</td>'
        f'<td><div class="bar-row" style="margin:0"><span class="track">'
        f'<span class="fill" style="width:{m["pct"]:.1f}%"></span></span></div></td>'
        f'<td class="num">{_fmt_tokens(m["tokens"])}</td>'
        f'<td class="num muted">{_fmt_tokens(m["prompt_tokens"])} / '
        f'{_fmt_tokens(m["completion_tokens"])}</td>'
        f'<td class="num">{_money(m["cost_usd"])}</td>'
        f"</tr>"
        for m in model_rows
    ) or '<tr><td colspan="6" class="muted">no model calls recorded yet — run a task</td></tr>'

    purpose_rows = usage.get("by_purpose", []) or []
    purpose_html = "".join(
        f'<div class="status-row"><span class="label">{_esc(p["purpose"])}</span>'
        f'<span class="detail">{p["calls"]} call(s)</span>'
        f'<span style="flex:1"></span>'
        f'<span class="detail">{_fmt_tokens(p["tokens"])} tokens</span>'
        f'<span class="detail muted">{_money(p["cost_usd"])}</span></div>'
        for p in purpose_rows
    ) or '<div class="muted">no usage yet</div>'

    day_rows = usage.get("per_day", []) or []
    day_html = "".join(
        f'<div class="bar-row"><span class="label">{_esc(d["day"])}</span>'
        f'<span class="track"><span class="fill" style="width:{d["pct"]:.1f}%"></span></span>'
        f'<span class="val">{_fmt_tokens(d["tokens"])} · {_money(d["cost_usd"])}</span></div>'
        for d in day_rows
    ) or '<div class="muted">no usage in the last 7 days</div>'

    body = f"""
<header class="top">
  <h1>usage</h1>
  <span class="tag">which model, how many tokens, what it cost</span>
  <span class="spacer"></span>
  <a class="btn" href="/">overview</a>
  <a class="btn" href="/system">system</a>
  <a class="btn" href="/api/usage">json</a>
</header>

<div class="cards">
  <div class="card"><div class="k">Total tokens</div><div class="v small">{_fmt_tokens(total_tokens)}</div></div>
  <div class="card"><div class="k">Model calls</div><div class="v">{total_calls}</div></div>
  <div class="card"><div class="k">Total cost</div><div class="v small">{_money(total_cost)}</div></div>
  <div class="card"><div class="k">Tokens · last 7 days</div><div class="v small">{_fmt_tokens(week_tokens)} <span class="muted" style="font-size:12px">· {_money(week_cost)}</span></div></div>
</div>

<div class="panel">
  <h2>Tokens by model <span class="hint">which API is doing the work</span></h2>
  <table>
    <thead><tr><th>model</th><th class="num">calls</th><th>share</th>
      <th class="num">tokens</th><th class="num">prompt / completion</th>
      <th class="num">cost</th></tr></thead>
    <tbody>{model_table}</tbody>
  </table>
</div>

<div class="grid2">
  <div class="panel"><h2>By pipeline stage</h2>{purpose_html}</div>
  <div class="panel"><h2>Tokens per day <span class="hint">last 7 days</span></h2>{day_html}</div>
</div>
"""
    return _page("Usage", body)


# ------------------------------------------------------------- task page --

def _event_row(e: dict) -> str:
    etype = e.get("event_type", "?")
    color, label = _EVENT_STYLE.get(etype, ("#8b949e", etype))
    payload = e.get("payload") or {}
    detail_parts: list[str] = []

    if etype == "ModelCallCompleted":
        detail_parts.append(f"model={_esc(payload.get('model', '?'))}")
        detail_parts.append(_money(payload.get("cost_usd", 0)))
        if payload.get("latency_ms") is not None:
            detail_parts.append(f"{payload.get('latency_ms')}ms")
        if payload.get("from_recording"):
            detail_parts.append('<span class="pill">recording</span>')
    elif etype == "ModelCallFailed":
        detail_parts.append(f"model={_esc(payload.get('model', '?'))}")
        detail_parts.append(_esc(str(payload.get("error", "")))[:160])
    elif etype == "ToolCallCompleted":
        detail_parts.append(f"tool={_esc(payload.get('tool', '?'))}")
        detail_parts.append(f"exit={payload.get('exit_code', '?')}")
        if payload.get("secrets_redacted"):
            detail_parts.append(
                f'<span class="pill" style="color:var(--amber)">'
                f'{payload.get("secrets_redacted")} secret(s) redacted</span>'
            )
    elif etype in ("VerificationPassed", "VerificationFailed"):
        detail_parts.append(f"verifier={_esc(payload.get('verifier', '?'))}")
        detail_parts.append(f"{payload.get('duration_ms', '?')}ms")
    elif etype == "VerificationRequested":
        detail_parts.append(f"verifier={_esc(payload.get('verifier', '?'))}")
    elif etype == "RetryScheduled":
        detail_parts.append(f"attempt {payload.get('attempt', '?')} failed → retry")
        detail_parts.append(_esc(str(payload.get("reason", "")))[:120])
    elif etype in ("TaskFailed", "NeedsHumanReview", "BudgetExceeded", "DeadlineExceeded"):
        detail_parts.append(_esc(str(payload.get("reason", "")))[:200])
    elif etype == "PlanGenerated":
        detail_parts.append(f"{payload.get('attempts', '?')} attempt(s)")
        if payload.get("repaired"):
            detail_parts.append('<span class="pill" style="color:var(--amber)">repaired</span>')
    elif etype == "SchemaValidationFailed":
        detail_parts.append(_esc("; ".join(payload.get("errors", []) or []))[:160])

    detail = f'<div class="detail">{" · ".join(detail_parts)}</div>' if detail_parts else ""
    ref = ""
    if e.get("tool_output_ref"):
        ref = (f'<div class="ref muted">blob {_esc(e["tool_output_ref"])[:48]}</div>')

    return (
        f'<li><span class="dot" style="background:{color}"></span>'
        f'<span class="when">{_fmt_time(e.get("created_at", ""))} · #{e.get("id", "?")}</span>'
        f'<div class="what">{_esc(label)}</div>{detail}{ref}</li>'
    )


def _verdict_banner(status: str) -> str:
    """A loud PASSED/FAILED strip for terminal states."""
    if status == "completed":
        return ('<div class="banner passed">✔ PASSED '
                '<span class="sub">verification passed — the task is done</span></div>')
    if status in ("failed", "verification_failed"):
        return ('<div class="banner failed">✘ FAILED '
                '<span class="sub">verification failed — the timeline below shows why</span></div>')
    if status == "needs_human_review":
        return ('<div class="banner" style="border-color:rgba(188,140,255,.5);'
                'color:var(--purple);background:rgba(188,140,255,.1)">✋ NEEDS HUMAN REVIEW '
                '<span class="sub">retries exhausted — the verifier output below is the '
                'evidence a human should read</span></div>')
    return ""


def _task_control_bar(task_id: str, status: str) -> str:
    """Control buttons for one task: re-run (finished) or cancel (active).

    Both are POSTs driven by fetch, so no form reloads and no navigation.
    A re-run is a brand-new task with the same objective — the original
    event stream is never mutated.
    """
    terminal = status in (
        "completed", "failed", "verification_failed", "halted", "needs_human_review",
    )
    active = status in ("pending", "in_progress")

    buttons = ""
    if status == "needs_human_review":
        # Human-in-the-loop: the verifier never passed, so the human is
        # the judge. Approve/reject are append-only reviews.
        buttons += (
            '<button class="primary" onclick="reviewTask(true)" '
            'id="approve-btn">approve</button> '
            '<button onclick="reviewTask(false)" id="reject-btn" '
            'style="border-color:var(--red);color:var(--red)">reject</button> '
            '<input id="review-note" placeholder="note (why)" '
            'style="width:220px;margin-left:6px"> '
        )
    if terminal:
        buttons += (
            '<button class="primary" onclick="rerunTask()" id="rerun-btn">'
            "re-run this task</button> "
        )
    if active:
        buttons += (
            '<button onclick="cancelTask()" id="cancel-btn" '
            'style="border-color:var(--red);color:var(--red)">cancel</button> '
        )
    if not buttons:
        return ""

    js = f"""
<script>
async function reviewTask(approved) {{
  const note = (document.getElementById('review-note') || {{}}).value || '';
  const url = '/tasks/{_esc(task_id)}/' + (approved ? 'approve' : 'reject')
    + (note ? '?note=' + encodeURIComponent(note) : '');
  const resp = await fetch(url, {{ method: 'POST' }});
  const data = await resp.json().catch(() => ({{}}));
  document.getElementById('ctl-out').innerHTML =
    '<div class="alert ' + (resp.ok ? 'ok' : 'err') + '">'
    + (data.detail || data.status || resp.status) + '</div>';
  if (resp.ok) setTimeout(() => location.reload(), 600);
}}
async function _post(url, btnId, done) {{
  const btn = document.getElementById(btnId);
  btn.disabled = true;
  try {{
    const resp = await fetch(url, {{ method: 'POST' }});
    const data = await resp.json().catch(() => ({{}}));
    done(data, resp.ok);
  }} catch (err) {{
    document.getElementById('ctl-out').innerHTML =
      '<div class="alert err">' + err + '</div>';
  }} finally {{
    btn.disabled = false;
  }}
}}
async function rerunTask() {{
  await _post('/tasks/{_esc(task_id)}/rerun', 'rerun-btn', (data, ok) => {{
    const out = document.getElementById('ctl-out');
    if (!ok) {{ out.innerHTML = '<div class="alert err">' + (data.detail || 'failed') + '</div>'; return; }}
    out.innerHTML = '<div class="alert ok">re-run created — ' +
      '<a href="/tasks/' + data.task_id + '/dashboard">task ' + data.task_id + '</a></div>';
  }});
}}
async function cancelTask() {{
  await _post('/tasks/{_esc(task_id)}/cancel', 'cancel-btn', (data, ok) => {{
    const out = document.getElementById('ctl-out');
    if (!ok) {{ out.innerHTML = '<div class="alert err">' + (data.detail || 'failed') + '</div>'; return; }}
    out.innerHTML = '<div class="alert ok">cancelled — the workflow was terminated</div>';
    setTimeout(function () {{ location.reload(); }}, 1500);
  }});
}}
</script>
"""

    return (
        f'<div class="panel" style="display:flex;align-items:center;gap:10px;'
        f'padding:12px 18px"><span class="muted" style="font-size:12.5px">control:</span>'
        f"{buttons}<span style='flex:1'></span>"
        f'<a class="btn" href="/tasks/{_esc(task_id)}/replay">replay</a></div>'
        f'<div id="ctl-out"></div>{js}'
    )


def render_task_dashboard(
    task_id: str,
    state: dict,
    events: list[dict],
    *,
    verifier_output: str = "",
    failed_model_calls: list[str] | None = None,
) -> str:
    """One task: meta, plan, and the event timeline.

    ``verifier_output`` is the FULL diagnostics blob from the last
    verification — the same text a retry would feed back to the worker — so
    a failed task shows exactly why, not a truncated summary.
    """
    failed_model_calls = failed_model_calls or []
    status = state.get("status", "unknown")
    refresh = ""
    if status in ("pending", "in_progress"):
        refresh = """
<script>
setTimeout(function () { location.reload(); }, 4000);
</script>"""

    plan = state.get("plan")
    plan_html = ""
    if plan:
        files = ", ".join(plan.get("files", []) or []) or "—"
        plan_html = f"""
<div class="panel"><h2>Plan <span class="hint">typed contract from the planner</span></h2>
<dl class="meta">
  <dt>Task type</dt><dd>{_esc(plan.get('task_type', '—'))}</dd>
  <dt>Verifier</dt><dd>{_esc(plan.get('verifier', '—'))}</dd>
  <dt>Files</dt><dd class="mono">{_esc(files)}</dd>
  <dt>Budget</dt><dd>{_money(plan.get('budget_usd'))}</dd>
  <dt>Deadline</dt><dd>{_esc(plan.get('deadline_minutes', '—'))} min</dd>
  <dt>Objective</dt><dd>{_esc(plan.get('objective', '—'))}</dd>
</dl></div>"""

    timeline = "".join(_event_row(e) for e in events) or \
        '<li class="muted" style="padding-left:26px">no events yet</li>'

    last_verification = state.get("last_verification")
    verify_html = ""
    if last_verification:
        verdict = "passed" if last_verification.get("passed") else "failed"
        cls = "ok" if last_verification.get("passed") else "err"
        verify_html = (
            f'<div class="alert {cls}">last verification <b>{verdict}</b> — '
            f'{_esc(str(last_verification.get("diagnostics", "")))[:400]}</div>'
        )

    # The full verifier output — the retry prompt. Show it whenever the task
    # did not pass; it is the single most useful debugging surface here.
    verifier_panel = ""
    if verifier_output and not last_verification.get("passed"):
        verifier_panel = (
            '<div class="panel"><h2>Verifier output '
            '<span class="hint">what a retry would feed back to the worker</span></h2>'
            f"<pre>{_esc(verifier_output)}</pre></div>"
        )

    model_fail_html = ""
    if failed_model_calls:
        rows = "".join(
            f'<div class="status-row"><span class="label">model call failed</span>'
            f'<span class="detail">{_esc(err)}</span></div>'
            for err in failed_model_calls
        )
        model_fail_html = (
            '<div class="panel"><h2>Failed model calls</h2>' + rows + "</div>"
        )

    body = f"""
<header class="top">
  <h1 class="mono" style="font-size:16px">task {_esc(task_id)}</h1>
  <span class="tag">{_badge(status)}</span>
  <span class="spacer"></span>
  <a class="btn" href="/">overview</a>
  <a class="btn" href="/usage">usage</a>
  <a class="btn" href="/system">system</a>
  <a class="btn" href="/tasks/{_esc(task_id)}/events">events json</a>
  <a class="btn" href="/tasks/{_esc(task_id)}/trajectory?format=md">trajectory</a>
  <a class="btn" href="/tasks/{_esc(task_id)}/replay">replay</a>
</header>

{_verdict_banner(status)}

{_task_control_bar(task_id, status)}

{verify_html}

{model_fail_html}

{verifier_panel}

<div class="panel"><h2>Task</h2>
<dl class="meta">
  <dt>Status</dt><dd>{_badge(status)}</dd>
  <dt>Objective</dt><dd>{_esc(state.get('objective', '—'))}</dd>
  <dt>Task type</dt><dd>{_esc(state.get('task_type', '—'))}</dd>
  <dt>Verifier</dt><dd>{_esc(state.get('verifier', '—'))}</dd>
  <dt>Cost</dt><dd>{_money(state.get('cost_usd', 0))}</dd>
  <dt>Attempts</dt><dd>{state.get('attempts', 0)}</dd>
  <dt>Events</dt><dd>{len(events)}</dd>
  <dt>Halted reason</dt><dd>{_esc(state.get('halted_reason') or '—')}</dd>
  <dt>Budget</dt><dd>{_money(state.get('budget_usd'))}</dd>
</dl></div>

{plan_html}

<div class="panel"><h2>Event stream <span class="hint">the audit trail — state is a replay of this</span></h2>
<ul class="timeline">{timeline}</ul></div>
"""
    return _page(f"Task {task_id}", body, extra_js=refresh)


# ------------------------------------------------------------- replay page --

def render_replay_page(task_id: str) -> str:
    """Replay page: a button that calls the replay API and renders the result.

    The API endpoint is POST (it runs a re-execution); the page drives it with
    fetch so no form reload is involved.
    """
    body = f"""
<header class="top">
  <h1 class="mono" style="font-size:16px">replay · {_esc(task_id[:16])}…</h1>
  <span class="spacer"></span>
  <a class="btn" href="/tasks/{_esc(task_id)}/dashboard">back to task</a>
</header>

<div class="panel">
  <h2>Deterministic replay</h2>
  <p class="muted">Re-runs the whole pipeline — planner, worker, verifier —
  answering every model call from stored recordings. No live provider, no cost,
  no nondeterminism. The result must match the original verification outcome.</p>
  <button id="run" class="primary" onclick="runReplay()">Run replay</button>
  <div id="out"></div>
</div>

<script>
async function runReplay() {{
  const btn = document.getElementById('run');
  const out = document.getElementById('out');
  btn.disabled = true; btn.textContent = 'replaying…';
  out.innerHTML = '<div class="alert">replaying from stored recordings…</div>';
  try {{
    const resp = await fetch('/tasks/{_esc(task_id)}/replay', {{ method: 'POST' }});
    const data = await resp.json();
    if (!resp.ok) {{
      out.innerHTML = '<div class="alert err">' + (data.detail || resp.status) + '</div>';
      return;
    }}
    const ok = data.match;
    out.innerHTML =
      '<div class="alert ' + (ok ? 'ok' : 'err') + '">' +
      '<b>' + (ok ? 'MATCH' : 'MISMATCH') + '</b> — original ' +
      (data.original_passed ? 'PASSED' : 'FAILED') + ' vs replay ' +
      (data.replayed_passed ? 'PASSED' : 'FAILED') + '</div>' +
      '<pre>' + (data.diagnostics || '') + '</pre>';
  }} catch (err) {{
    out.innerHTML = '<div class="alert err">' + err + '</div>';
  }} finally {{
    btn.disabled = false; btn.textContent = 'Run replay again';
  }}
}}
</script>
"""
    return _page(f"Replay {task_id}", body)


# ------------------------------------------------------------- new task page --

def render_new_task_page(templates: list | None = None) -> str:
    """Form that POSTs to the JSON API from the browser.

    Templates render as clickable cards that prefill the form (objective,
    type, and sensible limits) — one click to start a common job.
    """
    templates = templates or []
    card_html = ""
    if templates:
        cards = []
        for t in templates:
            tid = t["id"]
            cards.append(
                '<button type="button" class="tmpl" '
                f'onclick="applyTemplate(\'{tid}\')">'
                f'<b>{_esc(t["name"])}</b>'
                f'<span>{_esc(t["description"])}</span>'
                f"<code>{_esc(t.get('default_budget_usd') or 'default')} USD · "
                f"{_esc(str(t.get('default_deadline_minutes') or 'default'))} min</code>"
                f"</button>"
            )
        card_html = (
            '<div class="sec-title">Start from a template '
            '<span class="hint">click a card to fill the form</span></div>'
            '<div class="tmpl-grid">' + "".join(cards) + "</div>"
        )

    tpl_json = __import__("json").dumps(templates)
    body = """
<header class="top">
  <h1>new task</h1>
  <span class="spacer"></span>
  <a class="btn" href="/">overview</a>
  <a class="btn" href="/schedules-page">schedules</a>
</header>

<div class="panel">
  <h2>Create a task</h2>
  %(card_html)s
  <form id="f" onsubmit="submitTask(event)">
    <div class="sec-title">The task</div>
    <label class="fld"><span>Objective</span>
      <textarea name="objective" rows="4" required minlength="8"
        placeholder="e.g. Add a subtract function to calc.py so the test suite passes"></textarea></label>
    <div class="grid2">
      <label class="fld"><span>Task type</span>
        <select name="task_type">
          <option value="code_change" selected>code_change — planner → worker → verifier</option>
          <option value="demo">demo — 5 fake steps, noop verifier</option>
        </select></label>
      <label class="fld"><span>Verifier <span class="muted">(demo tasks only — the planner picks for code)</span></span>
        <select name="verifier">
          <option value="noop">noop</option>
          <option value="python_test_runner">python_test_runner</option>
          <option value="citation_checker">citation_checker</option>
        </select></label>
    </div>
    <div class="sec-title">Limits</div>
    <div class="grid2">
      <label class="fld"><span>Budget (USD) <span class="muted">— platform default if empty</span></span>
        <input type="number" name="budget_usd" step="0.01" min="0" placeholder="platform default"></label>
      <label class="fld"><span>Deadline (minutes) <span class="muted">— platform default if empty</span></span>
        <input type="number" name="deadline_minutes" min="1" placeholder="platform default"></label>
    </div>
    <div class="sec-title">On failure</div>
    <label class="fld" style="max-width:220px"><span>Max retries</span>
      <input type="number" name="max_retries" min="0" max="5" value="2"></label>
    <div class="check">
      <input type="checkbox" name="adaptive" value="on" id="adaptive">
      <label for="adaptive" style="cursor:pointer">Adaptive retries (M3) — on repeated failure, switch model / chunk the objective / ask for clarification instead of re-prompting with the same strategy</label>
    </div>
    <button type="submit" class="primary">Create task</button>
  </form>
  <div id="out"></div>
</div>

<script>
const TEMPLATES = %(tpl_json)s;

function applyTemplate(id) {
  const t = TEMPLATES.find(x => x.id === id);
  if (!t) return;
  const form = document.getElementById('f');
  form.objective.value = t.objective || '';
  form.task_type.value = t.task_type || 'code_change';
  if (t.default_budget_usd) form.budget_usd.value = t.default_budget_usd;
  if (t.default_deadline_minutes) form.deadline_minutes.value = t.default_deadline_minutes;
  if (t.default_max_retries != null) form.max_retries.value = t.default_max_retries;
  document.getElementById('out').innerHTML =
    '<div class="alert ok">template <b>' + t.name + '</b> loaded — review and submit</div>';
}

async function submitTask(ev) {
  ev.preventDefault();
  const form = ev.target;
  const params = new URLSearchParams(new FormData(form));
  const out = document.getElementById('out');
  try {
    const resp = await fetch('/tasks?' + params.toString(), { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) {
      out.innerHTML = '<div class="alert err">' + (data.detail || resp.status) + '</div>';
      return;
    }
    out.innerHTML = '<div class="alert ok">created — ' +
      '<a href="/tasks/' + data.task_id + '/dashboard">task ' + data.task_id + '</a>' +
      (data.workflow_id ? ' · workflow ' + data.workflow_id : '') + '</div>';
    form.reset();
  } catch (err) {
    out.innerHTML = '<div class="alert err">' + err + '</div>';
  }
}
</script>
""" % {"card_html": card_html, "tpl_json": tpl_json}
    return _page("New task", body)
