# Bucker

[![CI](https://github.com/abiralpokhrel-learns/bucker-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/abiralpokhrel-learns/bucker-agent/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

**An AI coding workspace with a separate, verifiable task-execution platform.**

Bucker Desktop brings your editor, project files, AI assistant, and provider settings
into one application. The CLI/API platform adds test-gated execution, budgets,
event history, and replay for structured tasks.

> **Developer preview.** Desktop and Lite tools run on your machine. Use version
> control, review edits, and work only with projects you trust.

## Desktop app

- **Coding workspace:** Monaco editor, explorer, tabs, and a VS Code-inspired dark theme.
- **Bucker assistant:** integrated agent panel, tool activity, and permission prompts.
- **Provider setup:** key instructions, model search, and OS-encrypted key storage.
- **Conveniences:** command palette, sidebar toggle, word wrap, and minimap.
- **Clearer errors:** rejected keys, quotas, rate limits, and timeouts have distinct messages; generation budgets are longer.

| Shortcut | Action |
|---|---|
| `Ctrl+S` | Save file |
| `Ctrl+B` | Toggle sidebar |
| `Ctrl+Shift+P` | Command palette |

### Windows package

The x64 installer bundles Python, the agent harness, and the local gateway.
No separate Python or agent installation is required.

**Download pending:** the installer is built and its packaged payload passed smoke
tests, but the GitHub upload has not completed. No public installer asset is
available yet. Check [Releases](https://github.com/abiralpokhrel-learns/bucker-agent/releases)
for availability; the source-code ZIP is not an installer.

Once installed: **open a folder → Providers → add your key → connect the agent**.
Editing works offline; cloud AI needs internet and your own provider account.
Git, Node, compilers, and other project tools are not bundled.

The desktop catalog includes **OpenRouter, Gemini, Groq, Mistral, SambaNova, and
Hugging Face**. OpenRouter choices are restricted to `:free` models. Quotas and
availability vary. Expanded OpenRouter/OpenCode options are not included yet.
See [provider notes](desktop/PROVIDERS.md) and [desktop source setup](desktop/README.md).

## Try the task platform

The browser dashboard is separate from the desktop app. Lite mode needs
**Python 3.11–3.13**, but no Docker, Postgres, or Temporal.

```bash
git clone https://github.com/abiralpokhrel-learns/bucker-agent.git
cd bucker-agent
```

| System | Start command |
|---|---|
| Windows | `.\start.bat` |
| macOS / Linux | `./start.sh` |

Open **http://localhost:8123**. Demo tasks need no model key; AI code tasks need a
configured provider. See the [usage guide](docs/USAGE.md).

For the full **Postgres + Temporal + Docker sandbox** stack, start Docker and run:

```bash
uv sync --extra full
uv run python -m bucker.cli dev
```

## What the platform adds

- **Verified tasks:** plan → work → review → test, with bounded retries and human escalation.
- **Durable history:** append-only events, stored responses, and deterministic replay.
- **Execution controls:** budgets, deadlines, provider routing, and fallback.
- **Automation:** task graphs, schedules, memory/skills, REST API, Python SDK, and MCP.

Not all platform features are exposed in the desktop preview. Strict verified
execution through MCP is not yet connected to the desktop agent loop.

## Verification and limits

CI covers tests, Lite startup, the Windows launcher, and crash/resume recovery.
Desktop smoke tests cover editor save/restart and the bundled agent/gateway handshake.

- The installer is unsigned; Windows or organizational policy may block it. Do not bypass security policy.
- Clean-machine installation and a complete live-provider desktop task remain unverified.
- Unsaved edits can be lost on shutdown. Automatic updates are not implemented.
- Desktop and Lite mode are **not security sandboxes**; container isolation belongs to the full-stack pipeline.
- No published SWE-bench results yet. This is a prototype, not a production reliability guarantee.

```bash
uv sync --extra dev --extra full
uv run python -m ruff check .
uv run python -m pytest tests/
```

Database-dependent tests require `BUCKER_TEST_DATABASE_URL`; otherwise they skip.

## Documentation

[Usage](docs/USAGE.md) · [Desktop](desktop/README.md) · [Providers](desktop/PROVIDERS.md) ·
[Roadmap](BUILD_PLAN.md) · [Operations](docs/OPERATIONS.md) ·
[Deployment](docs/DEPLOYMENT.md) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)

## Credits and license

Bucker is [Apache-2.0](LICENSE). The desktop harness uses
[Hermes Agent](https://github.com/NousResearch/hermes-agent); its MIT license and
third-party notices are retained. [OmniRoute](https://github.com/diegosouzapw/OmniRoute)
is an upstream reference checkout, not the desktop runtime gateway.
