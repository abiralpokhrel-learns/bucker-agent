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

---

## Step 1 - Install

You need just TWO things:

- **Docker Desktop** (running) - for the sandbox and the database
- **uv** (Python package manager - install with `winget install astral-sh.uv`)

Then, in the project folder:

```bash
uv sync
```

This installs everything. Done.

---

## Step 2 - One-command setup

The setup command checks your machine, creates your private config
file (.env) with a generated security token, starts the database, and
gets everything ready:

```bash
uv run python -m bucker.cli setup
```

That's it. If you have model API keys (DeepSeek / OpenRouter), open
`.env` and paste them in. No keys? The robot still works with a free
local model - install [Ollama](https://ollama.com) and run:

```bash
ollama pull qwen2.5-coder:7b
```

---

## Step 3 - Start the whole stack with ONE command

No more three terminals. One command starts everything - the manager
(Temporal), the robot (worker), and the dashboard (website):

```bash
uv run python -m bucker.cli dev
```

It detects what is already running and only starts what is missing.
The first time, it may start Temporal for you automatically.

---

## Step 4 - Give it your first task

Open your browser:

```
http://localhost:8123
```

Click **New task** and type something like:

```
create a file called hello.py that prints "hello from the robot"
```

Click create and watch it work.

You will see:

1. **planning** - the robot decides what to do
2. **working** - it writes the code
3. **critique** - it double-checks its own diff
4. **verifying** - it runs tests in the sandbox
5. **the verdict** - passed, failed, or "needs a human"

```
   ___
  (o o)
  ( v )  quack! "the tests passed"
   \_/
```

---

## Step 5 - What the statuses mean

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

## Step 6 - When the robot asks you

If a task shows **needs_human_review**, open it and click:

- **approve** - accept the result (it becomes `human_approved`)
- **reject** - send it back (it becomes `human_rejected`)

You can add a note explaining why. The robot never forgets - everything
is written to an append-only log.

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

---

## Step 8 - Running bigger things

- **Graphs** - run several tasks in a smart order (some in parallel):

```bash
uv run python -m bucker.cli graph run examples/graph_demo.json
```

- **Schedules** - run a task every day at 9am:

```bash
curl -X POST "http://localhost:8123/schedules?schedule_id=daily&cron=0%209%20%2A%20%2A%20%2A&objective=fetch%20the%20weather%20report"
```

- **MCP** - let Claude/Cursor/Hermes send tasks to the robot:

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
