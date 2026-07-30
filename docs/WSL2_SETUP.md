# Moving bucker-agent development into WSL2

**Why:** every environment problem in this project so far has been Windows-specific
— OneDrive syncing `.venv`, uv trampolines breaking when the folder moved, Smart
App Control blocking `pytest.exe`, and the venv refusing to execute Python at
all. None of them were bugs in the code.

The stack is Linux-native throughout: Docker, Postgres, Temporal, the sandbox
containers, and a CI job that runs on Ubuntu. Docker Desktop is already running
a WSL2 Linux VM underneath. Developing inside that VM removes the whole class of
problem *and* makes your machine match CI, so "passes locally, fails in CI"
stops happening.

Budget half a day. It pays back within the first week.

---

## 1. Install WSL2

In **PowerShell as Administrator**:

```powershell
wsl --install -d Ubuntu-24.04
```

Reboot when prompted. On first launch Ubuntu asks for a username and password —
these are local to the Linux VM and unrelated to your Windows account. The
password is what `sudo` will ask for.

Verify you got version 2 (not 1 — version 1 has poor filesystem performance and
no proper Docker integration):

```powershell
wsl -l -v
```

`VERSION` must read `2`. If it says 1: `wsl --set-version Ubuntu-24.04 2`.

---

## 2. Point Docker Desktop at it

Docker Desktop → **Settings → Resources → WSL Integration** → enable the toggle
for `Ubuntu-24.04` → **Apply & Restart**.

Then, **inside the Ubuntu terminal**, confirm the daemon is reachable:

```bash
docker run --rm hello-world
```

If that works, containers you start from WSL are the same Docker Desktop you
already use — no second installation, no duplicated images.

---

## 3. Install the toolchain inside Ubuntu

```bash
sudo apt update && sudo apt install -y build-essential git curl

# uv (manages Python versions too — no system Python needed)
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc

# Temporal CLI
curl -sSf https://temporal.download/cli.sh | sh
echo 'export PATH="$HOME/.temporalio/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc

uv --version && temporal --version
```

Note both installers are the plain `curl | sh` form that failed on Windows.
They work here because this is the platform they were written for.

---

## 4. Clone the repo — do NOT copy it from `C:\`

Your code is already on GitHub, so cloning is cleaner than copying: no stale
`.venv`, no `__pycache__`, no line-ending surprises.

```bash
mkdir -p ~/projects && cd ~/projects
git clone https://github.com/abiralpokhrel-learns/bucker-agent.git
cd bucker-agent
```

> **The one rule that matters most in this whole document:** keep the repo inside
> the Linux filesystem (`~/projects/...`), **never** under `/mnt/c/...`.
> Cross-boundary file access is roughly an order of magnitude slower and
> reintroduces Windows permission semantics — which is the exact thing you are
> leaving behind. A `pytest` run that takes 2 seconds in `~` can take 30+ under
> `/mnt/c`.

**Before you clone, check for uncommitted work on the Windows side.** The Ollama
changes may not be committed yet:

```powershell
cd "C:\abiralprojects\bucker agent"
git status
```

Anything uncommitted needs committing and pushing first, or it will not come
across. Keep the Windows folder around until WSL is verified working, then
delete it so there is only one copy.

---

## 5. Recreate `.env`

`.env` is gitignored, so it did not come with the clone. That is correct
behaviour, not a problem.

```bash
cp .env.example .env
nano .env
```

Set these two lines (free model — no credit required, and much stronger than a
local 7B):

```
BUCKER_MODEL=openrouter/nvidia/nemotron-3-super-120b-a12b:free
OPENROUTER_API_KEY=<your rotated key>
```

Save with `Ctrl+O`, `Enter`, then exit with `Ctrl+X`.

Note: `nano` will not silently append `.txt` the way Notepad does, so the file
is simply named what you told it.

---

## 6. Install and verify

```bash
uv sync --extra dev --extra llm
uv run python -m pytest -q
```

Expect **238 passed, 3 skipped**. If you get that, the whole codebase is
verified on the new machine before you change anything else.

The 3 skips are the two Docker integration tests and the append-only grant test.
Once Docker is running from WSL, those two Docker tests should actually execute
— so you may see **240 passed, 1 skipped**, which is better.

---

## 7. Infrastructure and the smoke run

```bash
docker compose up -d                                       # Postgres
docker build -f Dockerfile.sandbox -t bucker-sandbox:latest .
uv run python -m bucker.cli migrate

# separate terminal
temporal server start-dev

# back in the first terminal
uv run python -m tests.crash_test          # M1 again, on the new machine
uv run python -m scripts.smoke_run --live  # the first real pipeline run
```

Re-running the crash test here is worth the two minutes: it re-proves M1 on a
second, different platform, which is a stronger claim than proving it once.

---

## Working here day to day

- **Editor:** VS Code with the *WSL* extension. Run `code .` from the Ubuntu
  terminal and it opens connected to Linux — extensions, terminal, and debugger
  all run inside the VM.
- **Reaching the files from Windows:** `\\wsl$\Ubuntu-24.04\home\<you>\projects\bucker-agent`
  in Explorer. Fine for occasional browsing; do not run builds through it.
- **`localhost` is shared**, so the Temporal UI at `http://localhost:8233` opens
  in your Windows browser exactly as before.
- **Everything is `python -m`** anyway (see README), so nothing about the
  commands changes.

## What this fixes permanently

| Problem hit on Windows | Status in WSL2 |
|---|---|
| Smart App Control blocking `pytest.exe` | Gone — no unsigned Windows shims |
| uv trampolines breaking when the folder moved | Gone — venvs are relocatable here |
| OneDrive syncing `.venv` | Gone — outside OneDrive entirely |
| `.venv` "Access is denied" | Gone — normal Unix permissions |
| Notepad appending `.txt` to `.env` | Gone — `nano`/`vim` do not do this |
| Local environment differing from CI | Gone — both are Ubuntu now |

## Rollback

The Windows folder is untouched until you delete it. If WSL2 turns out not to
suit you, nothing is lost — the source of truth is GitHub either way.
