# Bucker-Agent v2.0 — Comprehensive Desktop IDE & Platform Implementation Plan

## Executive Summary & Vision

**Bucker-Agent** is an append-only, durable execution and verification platform for autonomous AI coding agents. Its core motto is:
> *"Nothing trusted until verified, nothing lost on crash, nothing overspends silently."*

The v2.0 evolution transitions bucker-agent from an operational CLI and server-rendered HTML dashboard into a **full-fledged, developer-first Agentic Desktop IDE** (inspired by Cursor, Windsurf, and VS Code), while preserving its core verification guarantees, deterministic replay capabilities, and dual-mode execution (Zero-dependency Lite mode and Enterprise Full stack).

---

## 1. System Architecture

```
+-------------------------------------------------------------------------+
|                      ELECTRON DESKTOP SHELL                             |
|  - Window management, native menus, tray icon, notifications            |
|  - Process Supervisor: auto-boots & monitors Python FastAPI backend     |
|  - node-pty subprocess bridge for native terminal                       |
+-------------------------------------------------------------------------+
                                   |
                +------------------+------------------+
                |                                     | (Local IPC / PTY)
                v                                     v
+-------------------------------+   +-------------------------------------+
|      REACT IDE FRONTEND       |   |     PYTHON FASTAPI BACKEND (8123)   |
| (Vite + TS + Tailwind)        |   | - Event-Sourced Kernel (EventStore) |
| - Monaco Editor & Diff Viewer |<->| - In-Memory Lite / Postgres Pool    |
| - xterm.js Terminal           |WS | - WebSocket Streaming (/ws/*)       |
| - Agent Cockpit & Chat        |   | - File Browser & Search APIs        |
| - Task Timeline & Human Review|   | - PTY & Terminal Server             |
| - State Management (Zustand)  |   | - Multi-language Verifiers          |
+-------------------------------+   +-------------------------------------+
                                                      |
                                    +-----------------+-----------------+
                                    |                                   |
                                    v                                   v
                      +---------------------------+       +---------------------------+
                      |   LITE ENGINE (Local)     |       |   FULL ENGINE (Enterprise)|
                      | - SQLite Event Log        |       | - Postgres Append-Only DB |
                      | - In-Process Async Runner |       | - Temporal Orchestration  |
                      | - Subprocess Scratch Env  |       | - Docker Sandbox Isolated |
                      +---------------------------+       +---------------------------+
```

---

## 2. Technology Stack

| Layer | Component | Technology | Rationale |
| :--- | :--- | :--- | :--- |
| **Desktop Shell** | Native Wrapper | **Electron** | Standard for AI IDEs (VS Code, Cursor). Direct node-pty terminal integration and Chromium engine. |
| **Packaging** | Python Bundler | **PyInstaller (`--onedir`)** | Packs FastAPI backend into standalone directory for zero-Python runtime client installs. |
| **Frontend UI** | Framework | **React 19 + TypeScript + Vite** | Fast HMR, strong typing, modern build toolchain. |
| **Design System**| UI Components | **Tailwind CSS + shadcn/ui** | High-density dark IDE theme, accessible headless components. |
| **Code Editor**  | Code & Diff | **Monaco Editor** (`@monaco-editor/react`) | VS Code's editor engine; rich syntax highlighting, multi-cursor, side-by-side diffing. |
| **Terminal**     | Emulator | **xterm.js v5 + WebLinks addon** | Hardware-accelerated terminal emulator, seamlessly pairs with node-pty / websocket PTY. |
| **State**        | Client Store | **Zustand** | Minimal boilerplate, reactive state for editor tabs, task trees, and chat sessions. |
| **Backend API**  | Server | **FastAPI + Uvicorn** | High-performance asynchronous REST & WebSocket framework. |
| **Execution**    | State Sourcing | **EventStore + Snapshots** | Append-only event stream; state is a pure fold of history. |
| **Inference**    | Gateway | **RouterEngine (OpenRouter/DeepSeek)** | Policy routing (cost, latency, priority), fallback chains, and circuit breakers. |

---

## 3. Directory & File Manifest

### Backend Structure (`bucker/`)
- `bucker/config.py`: Centralized configuration settings (frozen dataclass from `.env`).
- `bucker/api/app.py`: FastAPI application mounting all REST routes, middleware, and CORS.
- `bucker/api/ws.py`: WebSocket server (`/ws/tasks/{id}`, `/ws/agent/{id}`, `/ws/terminal/{id}`).
- `bucker/api/files.py`: Workspace file tree, search, diff, and safe write APIs.
- `bucker/api/terminal.py`: Cross-platform PTY manager (Windows process group + Unix pty).
- `bucker/api/chat.py`: Interactive AI agent chat sessions, streaming tokens, task-from-chat generation.
- `bucker/api/onboarding.py`: Environment diagnostic discovery (Git, Node, Docker, API keys).
- `bucker/api/routers/`: Modular route split:
  - `tasks.py`: Task lifecycle, dispatch, cancel, approval, rerun.
  - `schedules.py`: Cron workflows and recurring schedules.
  - `memory.py`: Semantic memory (facts) and procedural skills (SKILL.md).
  - `system.py`: Platform telemetry, model health, token quotas.
  - `export.py`: Trajectory export in HTML, CSV, and Markdown.
- `bucker/core/projects.py`: Multi-project workspace management.
- `bucker/core/git_ops.py`: Asynchronous Git operations (status, diff, branch, stash/pop, commit).
- `bucker/core/search.py`: Ripgrep (`rg`) and `fd` code search with regex fallbacks.
- `bucker/security/scan.py`: Real-time secret and credential detection/redaction scanner.
- `bucker/verifiers/`:
  - `python_test_runner.py`: Pytest runner inside sandbox.
  - `typescript_verifier.py`: `tsc --noEmit` + Jest/Vitest JSON verifier.
  - `rust_verifier.py`: `cargo test` + Clippy JSON verifier.
  - `go_verifier.py`: `go test ./...` + `go vet` verifier.
  - `lint_verifier.py`: Polyglot linter verifier (Ruff, ESLint, Clippy, GoVet).

### Frontend Structure (`bucker/frontend/`)
```
bucker/frontend/
├── package.json
├── vite.config.ts
├── tsconfig.json
├── src/
│   ├── main.tsx
│   ├── App.tsx
│   ├── api/
│   │   ├── client.ts
│   │   ├── ws.ts
│   │   └── types.ts
│   ├── components/
│   │   ├── layout/
│   │   │   ├── Sidebar.tsx
│   │   │   ├── TopBar.tsx
│   │   │   ├── BottomPanel.tsx
│   │   │   └── PanelManager.tsx
│   │   ├── editor/
│   │   │   ├── CodeEditor.tsx
│   │   │   ├── DiffViewer.tsx
│   │   │   ├── EditorTabs.tsx
│   │   │   └── FileTree.tsx
│   │   ├── terminal/
│   │   │   ├── Terminal.tsx
│   │   │   └── TerminalTabs.tsx
│   │   ├── chat/
│   │   │   ├── ChatPanel.tsx
│   │   │   ├── ChatMessage.tsx
│   │   │   └── ReasoningSteps.tsx
│   │   ├── tasks/
│   │   │   ├── TaskList.tsx
│   │   │   ├── TaskDetail.tsx
│   │   │   ├── EventTimeline.tsx
│   │   │   └── ApprovalPanel.tsx
│   │   └── dashboard/
│   │       ├── Overview.tsx
│   │       ├── UsagePage.tsx
│   │       ├── ModelsPage.tsx
│   │       └── SystemPage.tsx
│   └── store/
│       ├── taskStore.ts
│       ├── editorStore.ts
│       ├── chatStore.ts
│       └── settingsStore.ts
```

### Desktop Shell Structure (`desktop/`)
```
desktop/
├── package.json
├── electron-builder.yml
├── src/
│   ├── main/
│   │   ├── index.ts        (Window creation & app lifecycle)
│   │   ├── backend.ts      (FastAPI process launcher & health supervisor)
│   │   ├── tray.ts         (System tray icon & quick commands)
│   │   └── terminal.ts     (node-pty IPC handlers)
│   └── preload/
│       └── index.ts        (Safe contextBridge API)
```

---

## 4. Detailed Component Specifications

### 4.1 Real-Time Streaming & WebSockets (`bucker/api/ws.py`)
- **`/ws/tasks/{task_id}`**: Subscribes the client directly to task event emissions. When connected, pushes historical events, then pushes every newly appended `Event` as JSON. Automatically sends a terminal verdict message when task enters `completed`, `failed`, or `needs_human_review`.
- **`/ws/agent/{session_id}`**: Handles streaming AI reasoning steps, markdown token deltas (`chunk`), tool calls, and human approvals.
- **`/ws/terminal/{session_id}`**: Direct bidirectional raw text stream bridging xterm.js keystrokes to the underlying PTY subprocess.

### 4.2 Workspace & Safe File Operations (`bucker/api/files.py`)
- **`GET /api/files/tree`**: Traverses project root respecting `.gitignore` using standard fnmatch patterns. Emits tree nodes with file types, sizes, and programming language classifications.
- **`GET /api/files/read` & `PUT /api/files/write`**: Safe file read/write. Writes automatically snapshot the target file into `BlobStore` before modifying content, guaranteeing instant rollback/undo capability.
- **`GET /api/files/diff`**: Reads a task's diff, calculates original file content vs modified content, and serves clean side-by-side data structures ready for Monaco's `DiffEditor`.

### 4.3 Multi-Language Verification Subsystem
Every task is guarded by an automated verifier that runs inside the execution sandbox.
1. **Python (`PythonTestRunner`)**: Executes `pytest -q --tb=short` and parses test summaries.
2. **TypeScript/JavaScript (`TypeScriptVerifier`)**: Detects `tsconfig.json` or `package.json`. Executes `tsc --noEmit` for typing errors, followed by Jest/Vitest in JSON output format.
3. **Rust (`RustVerifier`)**: Detects `Cargo.toml`. Executes `cargo test --message-format=json` and captures compiler errors and test results. Runs `cargo clippy` in non-blocking mode.
4. **Go (`GoVerifier`)**: Detects `go.mod`. Executes `go test ./... -json` and parses package-level failures, followed by `go vet ./...`.
5. **Polyglot Linter (`LintVerifier`)**: Auto-selects Ruff, ESLint, or Clippy based on modified file extensions and repository markers.

### 4.4 Agent Interactive Chat Panel (`bucker/api/chat.py`)
- AI conversational partner connected to the repository context.
- Can be invoked with contextual tags (`@file:path`, `@task:id`).
- When a code solution is reached during a chat, the user can click **"Create Bucker Task"**, which takes the chat's proposed diff or plan and converts it into a typed `TaskCreated` contract executed and verified through the durable pipeline.

### 4.5 Secret & Credential Guard (`bucker/security/scan.py`)
- Integrated regex engine targeting AWS keys, GitHub PATs, OpenAI/Anthropic/DeepSeek API keys, Slack tokens, and high-entropy private keys.
- Operates in two directions:
  1. Prevents exposing secrets in workspace views passed to LLMs.
  2. Redacts accidentally emitted keys in agent diffs and event logs before database persistence.

---

## 5. Execution Roadmap

### Phase 1: Backend Foundation & Routing Refactor
- [x] Create `bucker/api/ws.py` (WebSocket manager & endpoints)
- [x] Create `bucker/api/terminal.py` (PTY manager)
- [x] Create `bucker/api/files.py` (File tree, read/write, diff)
- [x] Create `bucker/core/git_ops.py` (Async Git wrapper)
- [x] Create `bucker/core/search.py` (Ripgrep/FD wrapper)
- [x] Create `bucker/api/chat.py` (Chat session store & endpoints)
- [x] Create `bucker/core/projects.py` (Project workspace entity)
- [x] Create `bucker/api/onboarding.py` (System diagnosis API)
- [x] Create `bucker/verifiers/{typescript,rust,go,lint}_verifier.py`
- [x] Create `bucker/security/scan.py` (Secret scanner)
- [x] Create `bucker/api/routers/export.py` (CSV/Report export)
- [ ] Refactor `bucker/api/app.py` to register all new routers with clean CORS & auth
- [ ] Validate unit tests across all new backend modules

### Phase 2: React IDE Frontend Development
- [ ] Initialize `bucker/frontend` with Vite + React 19 + TypeScript + Tailwind
- [ ] Implement Monaco Editor integration with multi-tab support
- [ ] Implement Monaco DiffViewer for task review
- [ ] Implement xterm.js terminal panel with WebSocket connection
- [ ] Build Left Sidebar (File Explorer + Project Tree + Task List)
- [ ] Build Right Dock (Interactive AI Chat + Reasoning Trace)
- [ ] Build Task Dashboard (Live Event Timeline, Spend Meter, Human Approval Banner)
- [ ] Implement Dark/Light theme system and keyboard shortcuts (`Ctrl+P`, `Ctrl+\``, `Ctrl+B`)

### Phase 3: Electron Desktop Packaging
- [ ] Initialize `desktop/` Electron project
- [ ] Implement process supervisor for `python -m uvicorn bucker.api.app:app`
- [ ] Add system tray support and OS native notifications on task completion
- [ ] Write cross-platform packaging script (`package-all.ps1` / `electron-builder.yml`)
- [ ] Bundle Python backend with PyInstaller for zero-dependency distribution

### Phase 4: Verification, Benchmarking & Polish
- [ ] Test full end-to-end task loop: Chat -> Create Task -> Worker Diff -> Sandbox Verify -> Monaco Diff Review -> Human Approval -> Git Commit
- [ ] Run test suite across all verifiers
- [ ] Verify Lite mode (SQLite) and Full mode (Docker/Postgres) parity
- [ ] Update documentation and quickstart guides

---

## 6. Verification & Quality Gates

1. **Backend Test Suite**:
   ```bash
   pytest tests/
   ```
2. **Frontend Type Check & Build**:
   ```bash
   cd bucker/frontend
   pnpm run type-check
   pnpm run build
   ```
3. **End-to-End Desktop Verification**:
   - Start the desktop application.
   - Confirm backend starts and passes `/health/live`.
   - Open a workspace folder.
   - Run a terminal command inside the embedded xterm.js window.
   - Submit a code change task via Chat or Task form.
   - Verify that Monaco displays the live diff and the verifier passes or flags errors cleanly.
