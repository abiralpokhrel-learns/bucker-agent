# bucker-agent Security Policy

This document describes bucker-agent's trust model, names the boundaries the
project treats as load-bearing, and defines the scope for vulnerability
reports.

## 1. Reporting a Vulnerability

Report privately via [GitHub Security Advisories](https://github.com/abiralpokhrel-learns/bucker-agent/security/advisories/new)
Do **not** open public issues
for security vulnerabilities. There is no bug bounty program.

A useful report includes:

- A concise description and severity assessment.
- The affected component, identified by file path and line range
  (e.g. `bucker/sandbox/runtime.py:120-145`).
- Environment details (OS, Python version, Docker version, commit SHA).
- A reproduction against `main`.
- A statement of which trust boundary in §2 is crossed.

Reports that demonstrate limits of an in-process heuristic that this policy
does not treat as a boundary are out of scope for the private channel — but
they are still welcome as regular issues or pull requests.

## 2. Trust Model

bucker-agent executes untrusted model output inside containers. The layered
posture below is ordered by how load-bearing each layer is.

### 2.1 Definitions

- **Sandbox.** The Docker container (`bucker-sandbox:latest`) where worker
  code runs. It is created per task, runs with `--network none`, and is
  destroyed when the task ends.
- **Event store.** The Postgres `events` table. The `bucker_app` role has
  `INSERT` and `SELECT` only — `UPDATE`/`DELETE` are revoked at the database
  permission level, not by convention.
- **Recorder.** The router's blob archive: every model request and response
  is stored verbatim and content-addressed before anything downstream sees
  it. Replay reads only this archive.
- **Verifier.** The domain-specific objective check (e.g. running the
  project's own tests inside the sandbox). The verifier, never the model,
  decides whether a task passed.
- **API surface.** The FastAPI app (`bucker.api.app`) and the dashboard it
  renders.

### 2.2 The Boundaries

1. **Sandbox isolation (highest).** An adversarial model must not escape the
   container. The load-bearing properties: `--network none` (no
   exfiltration, no callbacks), a non-root user inside the container, no
   host volumes beyond the task workspace, and per-task containers.
   A report that demonstrates container escape, host filesystem access, or
   network access from inside a default sandbox is in scope regardless of
   severity.
2. **Append-only event store.** No code path should be able to rewrite
   history. A report demonstrating a way to modify or delete rows in
   `events` (as `bucker_app`, or via the application's own code paths)
   is in scope.
3. **Secret handling.** Provider keys live in `.env`, which is git-ignored.
   The dashboard reports key *shape* (prefix, length) on the system page,
   never the value. A report demonstrating a key value being rendered,
   logged, or recorded is in scope.
4. **Verifier independence.** The verifier must never call a model (a test
   asserts the verifier package does not import the router). A report
   demonstrating "model grading itself" is in scope.

### 2.3 Out of Scope

- Prompt-injection of the *user's own* agent loop (bucker does not run the
  agent loop; it executes under it).
- A weak local model producing incorrect code — that is a model-quality
  issue, and the verifier exists precisely to catch it.
- Cost abuse of a model the operator deliberately configured with a token
  ceiling (ceilings are a guardrail, not a security boundary).

## 3. Coordinated Disclosure

We ask for a 90-day embargo from the initial report before public
disclosure, and we will credit reporters in the changelog unless they ask
otherwise. We aim to acknowledge reports within 3 business days.
