#!/usr/bin/env bash
# Phase 3 live proof: the INTERNAL path (ModelRouter.complete) through the
# gateway engine with the REAL DeepSeek adapter — records an envelope.
set -a
source .env
set +a
BUCKER_MODEL_MODE=live .venv/Scripts/python.exe - <<'PY'
import asyncio, json, tempfile
from pathlib import Path

from bucker.core.blob import BlobStore
from bucker.router.client import ModelRouter, RecordingStore, request_digest

async def main():
    root = Path(tempfile.mkdtemp(prefix="bucker-bridge-live-"))
    router = ModelRouter(
        BlobStore(root / "blobs"),
        mode="live",
        recordings=RecordingStore(root / "recordings"),
    )
    msgs = [{"role": "user", "content": "Reply with exactly: bridge-live-ok"}]
    resp = await router.complete(msgs, purpose="planner", max_tokens=30)
    print("served model:", resp.model)
    print("text:", resp.text.strip()[:60])
    print("cost_usd:", resp.cost_usd, "| cost_unknown:", resp.cost_unknown)
    digest = request_digest(router.model, msgs, 0.0, 30)
    record = router.recordings.get(digest)
    print("recording routing:", json.dumps(record["routing"], indent=1))
    print("recording has envelope:", all(k in record for k in
          ("model", "model_served", "text", "routing", "usage", "raw_ref")))

asyncio.run(main())
PY
