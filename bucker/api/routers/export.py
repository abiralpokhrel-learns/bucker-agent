from __future__ import annotations

from io import StringIO
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse, StreamingResponse

from bucker.config import settings
from bucker.core.eventstore import EventStore, create_pool

export_router = APIRouter(prefix="/api/export", tags=["export"])

async def get_store() -> EventStore:
    pool = await create_pool(settings.database_url)
    return EventStore(pool)

@export_router.get("/task/{task_id}/report", response_class=HTMLResponse)
async def export_task_report(task_id: str, store: EventStore = Depends(get_store)):
    # Basic skeleton for HTML report
    return HTMLResponse(content=f"<html><body><h1>Report for {task_id}</h1></body></html>")

@export_router.get("/tasks/csv", response_class=StreamingResponse)
async def export_tasks_csv(
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
    store: EventStore = Depends(get_store)
):
    # Dummy CSV export
    output = StringIO()
    output.write("id,type,status,objective,cost_usd,tokens,created_at,completed_at\n")
    output.seek(0)
    
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=tasks.csv"}
    )

@export_router.get("/task/{task_id}/markdown")
async def export_task_markdown(task_id: str, store: EventStore = Depends(get_store)):
    return StreamingResponse(
        iter([f"# Task {task_id} Trajectory\n\nExported content."]),
        media_type="text/markdown",
        headers={"Content-Disposition": f"attachment; filename=task_{task_id}.md"}
    )
