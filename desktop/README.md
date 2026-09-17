# Bucker Desktop — developer preview

## Architecture preserved

Electron + React/Monaco → Hermes ACP subprocess → authenticated loopback inference gateway → user-connected providers.
Hermes owns the agent loop. `bucker/desktop_gateway` reuses Bucker's RouterEngine, registry, circuit breaker and provider adapter. The full FastAPI task application is not started. Existing CLI/API/MCP remain separate. Optional strict verified execution via MCP is still a product integration TODO, not enabled by this preview.

## Windows installer

Download the installer and SHA256 checksums from
[GitHub Releases](https://github.com/abiralpokhrel-learns/bucker-agent/releases).
Generated packages are release assets, not source files; `desktop/release/` is
ignored by Git and remains available locally after builds.

`release/Bucker-Desktop-2.0.0-Setup.exe` is a single x64 NSIS installer containing Electron, the renderer, Python 3.12.10, Hermes 0.20.5 sources and 71 Python packages, and the standalone gateway. No user credentials or profiles are included. Enter your provider key after launch. Internet is needed for model calls; project-specific compilers, Git/Bash, Node and other toolchains are not included.

This unsigned developer-preview installer was built successfully, but Device Guard blocked installation on the test host. Do not disable organizational policy to install it; administrator approval/signing is needed. The unpacked packaged payload was used for acceptance testing, not a clean installed system.

## Run from source

Prerequisites: Node/npm, this repository's Python environment, and an installed Hermes with ACP support (`hermes acp --check`). These prerequisites apply to source development; the installer bundles Python and Hermes.

From `desktop/`:

    npm install
    npm --prefix ../bucker/frontend install
    npm --prefix ../bucker/frontend run build
    npm run build:main
    npm start

Open a folder. Providers are under the gear button. Keys are entered by the user and persisted with Electron safeStorage. Use free-only provider accounts; Bucker cannot inspect or enforce an external account's billing configuration. Connect the agent after setting up providers. Local agent tools execute on the host, not inside a Docker sandbox.

Do not use this preview on an irreplaceable workspace. Use version control and review changes. Desktop editor file access is workspace-scoped, but this is NOT a security sandbox for Hermes tools.

## Verification performed

- TypeScript main/preload build and Vite frontend build.
- Node transport, workspace, environment, and session bridge tests.
- Real Electron editor acceptance: open folder, browse file, edit Monaco, save, read disk back. Provider cards also checked. Native folder selection is stubbed to a temporary fixture; editor and IPC are real.
- Installed Hermes 0.20.5 initializes using ACP v1 in an isolated home; no model request.
- Scripted local stdio ACP peer: updates, plan, permission response, prompt completion. This is a fixture, NOT real Hermes inference or an Electron agent-loop test. Filename `tests/e2e-agent.cjs` is historical.
- Gateway plus existing gateway regression suite: 64 passed, 1 skipped (2 deprecation warnings).
- Real loopback gateway health/auth/model discovery tested; deliberately invalid upstream credential returns a sanitized failure. No successful authenticated provider inference claimed.

Reproduce:

    npm run build:main
    node --test tests/acp.test.cjs tests/workspace.test.cjs tests/runtime.test.cjs tests/session.test.cjs
    node tests/e2e-editor.cjs
    node tests/e2e-agent.cjs
    node tests/acp-live.cjs

From repository root:

    .venv/Scripts/python.exe -m pytest tests/test_desktop_gateway.py tests/test_gateway_engine.py tests/test_gateway.py -o addopts='' -q

## Release blockers / honest limits

- No live provider task has completed the full desktop → Hermes → gateway → provider → edit/test path. Account signup, quotas, all model IDs and multi-turn tool compatibility require authenticated checks.
- Installer and bundled runtime exist. Trusted code signing, updater, actual installer execution on this policy-restricted host, and clean-machine acceptance remain unverified.
- Catalog is a documentation-checked snapshot, not a live capability/price checker. No authoritative quota balance. Some providers require metadata the generic adapter may not retain. Free accounts have severe limits; no unlimited usage promise.
- Full diff review, working preview, robust event replay after renderer reload, session list, optional strict mode, provider replacement/removal UX and lifecycle recovery remain incomplete.
- App shutdown can lose unsaved editor changes; tab-close and folder-change guards exist, but crash/shutdown recovery does not.
- Full independent security audit and Linux/macOS acceptance remain outstanding.
- Large Monaco bundle warning remains.
- Full repository run: 771 passed, 33 skipped, 0 failed (2 deprecation warnings). DB-dependent tests skip when the test DSN is absent from the initial test environment; an explicitly configured but unreachable database still fails.
- Fixed during this work: `tests/test_verifiers.py` expectations updated for the four new builtin verifiers; `tests/test_migrations_upgrade.py` now reuses conftest's frozen `TEST_DSN` instead of re-reading `os.environ` at import, which previously made skips depend on test collection order.
- Focused Python lint passed. Repository `git diff --check` reports trailing whitespace in the already-modified `bucker/verifiers/python_test_runner.py:249`; that unrelated file was not changed by this desktop work.

`BUCKER_DESKTOP_HOME` allows an isolated desktop data directory for testing. It does not change the user's normal Hermes profile.

See `PROVIDERS.md` for source links and entitlement distinctions. No upstream license/notice files were removed. A distributor must bundle the pinned Hermes version's MIT license and third-party notices with the eventual installer.
