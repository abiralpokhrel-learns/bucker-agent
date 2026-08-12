# bucker-agent

```
      ____________________________
     |  BUCKER-AGENT             |
     |  the coding robot that    |
     |  checks its own work      |
     |___________________________|
         \____________________/
          |                  |
          |  (your tasks)    |
          |  ----------------|--------->
         _v__________________v_
        |______________________|
         \____________________/
              THE BUCKET
```

bucker-agent is a **coding robot**. You give it a task, it:

1. makes a plan,
2. writes the code,
3. double-checks its own work,
4. runs tests in a locked-down sandbox,
5. tells you the truth about whether it passed.

Everything is recorded, so you can always see *what happened and why*.
And if it gets stuck, it asks **you** to decide.

```
   ___
  (o o)    <- the verification duck
  | U |       "did you actually run the tests?"
  (___)
```

> **⚠️ LITE MODE RUNS CODE ON YOUR MACHINE.** The quick start below runs
> tasks directly on your computer (no container). Use it for tasks you
> trust. For untrusted code, use the full Docker stack at the bottom of
> this page.

---

## Step 1 - Install (the easy way)

You need **one thing**: Python 3.11, 3.12, or 3.13.

- **Windows**: install Python from [python.org](https://www.python.org/downloads/)
  (tick "Add python.exe to PATH"), or `winget install Python.Python.3.12`.
- **macOS**: `brew install python@3.12` (or download from python.org).
- **Linux**: `sudo apt install python3.12` (Debian/Ubuntu),
  `sudo dnf install python3.12` (Fedora), or
  `sudo pacman -S python` (Arch).

No Docker. No Postgres. No Temporal. No uv. No API keys to start.

---

## Step 2 - Start it

In the project folder:

**Windows** — double-click `start.bat` (or run it from a terminal):

```powershell
start.bat
```

**macOS / Linux**:

```bash
./start.sh
```

The launcher sets up everything (a private environment + dependencies,
which takes a minute the first time), then starts bucker and opens the
dashboard.

---

## Step 3 - Give it your first task

Your browser opens:

```
http://localhost:8123
```

Click **New task** and type something like:

```
create a file called hello.py that prints "hello from the robot"
```

Click create and watch it work. Demo tasks work with **zero API keys**.

You will see:

1. **planning** - the robot decides what to do
2. **working** - it writes the code
3. **critique** - it double-checks its own diff
4. **verifying** - it runs tests
5. **the verdict** - passed, failed, or "needs a human"

```
   ___
  (o o)
  ( v )  quack! "the tests passed"
   \_/
```

---

## Step 4 - What the statuses mean

| Status             | Meaning                                   |
|--------------------|-------------------------------------------|
| pending            | waiting for a worker                      |
| in_progress        | working right now                         |
| completed          | passed - the robot verified its own work  |
| verification_failed| tests failed, trying again                |
| needs_human_review | robot is stuck - it needs YOUR decision   |
| human_approved     | you looked at it and said "yes"           |
| human_rejected     | you looked at it and said "no"            |
| halted             | ran out of budget or time                 |

---

## Step 5 - When the robot asks you

If a task shows **needs_human_review**, open it and click:

- **approve** - accept the result (it becomes `human_approved`)
- **reject** - send it back (it becomes `human_rejected`)

You can add a note explaining why. The robot never forgets - everything
is written to an append-only log.

---

## Step 6 - Real model calls (optional)

Zero-key demo tasks work out of the box. To let the robot actually
write code with a real model, create a `.env` file in the project folder
(see `.env.example`) and add a key:

```
DEEPSEEK_API_KEY=your-key-here
```

or, for free OpenRouter models:

```
OPENROUTER_API_KEY=your-key-here
```

Restart bucker and it will use the model. No key and no model? The robot
still works with the built-in demo tasks and replays.

---

## Step 7 - Useful commands

```bash
# list your recent tasks
uv run python -m bucker.cli tasks --limit 5

# check the whole system is healthy
uv run python -m scripts.doctor

# teach the robot something (it remembers next time)
uv run python -m bucker.cli memory add "the tests run with pytest"

# see what the robot remembers
uv run python -m bucker.cli memory list

# re-schedule tasks that never started (e.g. Temporal was down)
uv run python -m bucker.cli reconcile --dry-run

# back up everything (database + files)
uv run python -m scripts.backup
```

(These need `uv` — `winget install astral-sh.uv`, or `pip install uv`.)

---

## Step 8 - Use it as an API (like OmniRoute)

Your `BUCKER_API_TOKEN` is an API key. Point ANY OpenAI-compatible client
at bucker and it routes to real models (DeepSeek → free local Ollama →
free OpenRouter) automatically:

```bash
curl http://localhost:8123/v1/chat/completions \
  -H "Authorization: Bearer YOUR-B...OKEN" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"say hi"}]}'
```

Every call is tracked and shows up on the dashboard, with its cost.

---

## Running bigger things (full stack, advanced)

The quick start above runs **lite mode**: tasks run in-process, storage
is a local SQLite file, and code runs directly on your machine. That is
fine for trying bucker out.

For the full stack — durable crash-resume (Temporal), the Postgres event
store, and Docker-sandboxed code execution — you need Docker Desktop
(running) and `uv` (`winget install astral-sh.uv`):

```bash
uv sync --extra full
uv run python -m bucker.cli setup
uv run python -m bucker.cli dev
```

One command starts the manager (Temporal), the robot (worker), and the
dashboard (website). It detects what is already running and only starts
what is missing.

Or run everything in containers (Docker only — nothing else needed
locally):

```bash
docker compose up --build -d
```

That builds the database, Temporal, worker, dashboard, and the sandbox
image. Open http://localhost:8123.

**Graphs** - run several tasks in a smart order (some in parallel):

```bash
uv run python -m bucker.cli graph run examples/graph_demo.json
```

**Schedules** - run a task every day at 9am:

```bash
curl -X POST "http://localhost:8123/schedules?schedule_id=daily&cron=0%209%20%2A%20%2A%20%2A&objective=fetch%20the%20weather%20report"
```

**MCP** - let Claude/Cursor/Hermes send tasks to the robot:

```bash
uv sync --extra mcp
```

---

## Troubleshooting

| Problem                          | Fix                                  |
|----------------------------------|--------------------------------------|
| `os error 5` when running uv     | `export UV_PYTHON_INSTALL_DIR="C:\Users\YOURNAME\AppData\Local\uv\python"` |
| Everything returns 503           | run `uv run python -m bucker.cli setup` |
| Task stuck in pending            | is `bucker dev` still running in your terminal?  |
| "free tier exhausted" from OpenAI| wait until midnight UTC, or add credits |
| Need a real API token            | `openssl rand -hex 32` and put it in `.env` |

---

## The whole thing in one picture

```
  YOU               BUCKER
  |  task idea         |
  |------------------->|   plan -> write -> critique -> test
  |                    |       |
  |                    |   needs a human?  -------->
  |<---- dashboard ----|                             |
  |  approve / reject --------------------------------> done
  |<---------------------------------------------------|
  |        (everything recorded, nothing forgotten)
```

```
      ___
     (o o)
     ( v )  quack! now go build something.
      \_/
```

That's it. Give the robot a task, watch it work, and check the dashboard.
Questions or ideas? Open an issue.
