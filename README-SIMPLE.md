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

You need:

- **Docker Desktop** (running) - for the sandbox and the database
- **Python 3.12** (installed)
- **uv** (Python package manager)

Then, in the project folder:

```bash
uv sync
```

This installs everything. Done.

---

## Step 2 - Configure

Copy the example config to your own file:

```bash
cp .env.example .env
```

Open `.env` and add the keys you have:

```
BUCKER_API_TOKEN=make-up-a-secret-token
DEEPSEEK_API_KEY=your-key-here
OPENROUTER_API_KEY=your-key-here
```

No keys? No problem - the robot still works with a **free local model**
(Ollama). Install [Ollama](https://ollama.com) and pull a model:

```bash
ollama pull qwen2.5-coder:7b
```

---

## Step 3 - Start the database

```bash
docker compose up -d
uv run python -m bucker.cli migrate
```

That's it - your database is ready.

---

## Step 4 - Start the engine

Open **three** terminal windows.

**Terminal 1 - the clock** (Temporal keeps track of everything):

```bash
temporal server start-dev
```

**Terminal 2 - the worker** (this is the robot itself):

```bash
BUCKER_MODEL_MODE=live uv run python -m bucker.worker
```

**Terminal 3 - the dashboard** (this is the website):

```bash
uv run uvicorn bucker.api.app:app --port 8123
```

---

## Step 5 - Give it your first task

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

## Step 6 - What the statuses mean

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

## Step 7 - When the robot asks you

If a task shows **needs_human_review**, open it and click:

- **approve** - accept the result (it becomes `human_approved`)
- **reject** - send it back (it becomes `human_rejected`)

You can add a note explaining why. The robot never forgets - everything
is written to an append-only log.

---

## Step 8 - Useful commands

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

## Step 9 - Running bigger things

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
| Everything returns 503           | start the database: `docker compose up -d` |
| Task stuck in pending            | is the worker running? (Terminal 2)  |
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
