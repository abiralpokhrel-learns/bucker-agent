from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from bucker.config import settings
from bucker.core.eventstore import create_pool
from bucker.router.client import ModelRouter

chat_router = APIRouter(prefix="/api/chat", tags=["chat"])

# --- Models ---


class ChatMessage(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    role: str  # "user" | "assistant" | "system"
    content: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] | None = None


class ChatSession(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    title: str
    messages: list[ChatMessage] = Field(default_factory=list)
    project_root: str | None = None


class CreateSessionRequest(BaseModel):
    title: str | None = None
    project_root: str | None = None


class SendMessageRequest(BaseModel):
    content: str
    context: dict[str, Any] | None = None


class ApplySuggestionRequest(BaseModel):
    message_id: str
    objective: str | None = None


class SendMessageResponse(BaseModel):
    message_id: str
    status: str


# --- Storage Setup ---
async def init_chat_schema(pool: Any = None):
    if pool is None:
        pool = await create_pool(settings.database_url)
    async with pool.acquire() as conn:
        query = """
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id VARCHAR(50) PRIMARY KEY,
            title VARCHAR(255),
            created_at TIMESTAMP,
            project_root TEXT,
            messages_json TEXT
        )
        """
        await conn.execute(query)


# Dependency to get DB pool
_chat_pool: Any = None


async def get_db():
    global _chat_pool
    try:
        from bucker.api.app import _pool

        if _pool is not None:
            return _pool
    except ImportError:
        pass
    if _chat_pool is None:
        _chat_pool = await create_pool(settings.database_url)
    return _chat_pool


# --- Endpoints ---


@chat_router.post("/sessions", response_model=ChatSession)
async def create_session(req: CreateSessionRequest, db=Depends(get_db)):
    """Create a new chat session."""
    session = ChatSession(
        title=req.title or "New Chat",
        project_root=req.project_root,
    )

    async with db.acquire() as conn:
        query = """
        INSERT INTO chat_sessions (id, title, created_at, project_root, messages_json)
        VALUES ($1, $2, $3, $4, $5)
        """
        await conn.execute(
            query,
            session.id,
            session.title,
            session.created_at,
            session.project_root,
            json.dumps([m.model_dump(mode="json") for m in session.messages]),
        )
    return session


@chat_router.get("/sessions", response_model=list[ChatSession])
async def list_sessions(limit: int = 50, offset: int = 0, db=Depends(get_db)):
    """List chat sessions, most recent first."""
    async with db.acquire() as conn:
        query = "SELECT * FROM chat_sessions ORDER BY created_at DESC LIMIT $1 OFFSET $2"
        rows = await conn.fetch(query, limit, offset)

    sessions = []
    for row in rows:
        messages = [ChatMessage(**m) for m in json.loads(row["messages_json"])]
        sessions.append(
            ChatSession(
                id=row["id"],
                title=row["title"],
                created_at=row["created_at"],
                project_root=row["project_root"],
                messages=messages,
            )
        )
    return sessions


@chat_router.get("/sessions/{session_id}", response_model=ChatSession)
async def get_session(session_id: str, db=Depends(get_db)):
    """Get a chat session with full history."""
    async with db.acquire() as conn:
        query = "SELECT * FROM chat_sessions WHERE id = $1"
        row = await conn.fetchrow(query, session_id)

    if not row:
        raise HTTPException(status_code=404, detail="Session not found")

    messages = [ChatMessage(**m) for m in json.loads(row["messages_json"])]
    return ChatSession(
        id=row["id"],
        title=row["title"],
        created_at=row["created_at"],
        project_root=row["project_root"],
        messages=messages,
    )


@chat_router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, db=Depends(get_db)):
    """Delete a chat session."""
    async with db.acquire() as conn:
        query = "DELETE FROM chat_sessions WHERE id = $1"
        result = await conn.execute(query, session_id)
    return {"status": "deleted" if result != "DELETE 0" else "not_found"}


@chat_router.post("/sessions/{session_id}/messages", response_model=SendMessageResponse)
async def send_message(session_id: str, req: SendMessageRequest, db=Depends(get_db)):
    """
    Send a message to a session.
    The actual processing and streaming happens async via websockets.
    """
    # Verify session exists
    session = await get_session(session_id, db)

    user_msg = ChatMessage(role="user", content=req.content, metadata=req.context)
    session.messages.append(user_msg)

    # Save the updated session
    async with db.acquire() as conn:
        query = "UPDATE chat_sessions SET messages_json = $1 WHERE id = $2"
        await conn.execute(
            query, json.dumps([m.model_dump(mode="json") for m in session.messages]), session_id
        )

    # Kick off background processing
    asyncio.create_task(_process_chat_message(session_id, user_msg, session, req.context, db))

    return SendMessageResponse(message_id=user_msg.id, status="processing")


@chat_router.post("/sessions/{session_id}/apply")
async def apply_suggestion(session_id: str, req: ApplySuggestionRequest, db=Depends(get_db)):
    """Create a bucker task from a chat message suggestion."""
    session = await get_session(session_id, db)
    target_msg = next((m for m in session.messages if m.id == req.message_id), None)
    if not target_msg:
        raise HTTPException(status_code=404, detail="Message not found")

    # In a real implementation, we would create a task in the Temporal/Bucker system
    # using the objective and the message context.
    task_id = uuid4().hex[:12]
    # create_bucker_task(objective=req.objective, context=target_msg.content)

    return {"task_id": task_id, "status": "created"}


# --- Internal Processing ---


async def _process_chat_message(
    session_id: str,
    user_msg: ChatMessage,
    session: ChatSession,
    context: dict[str, Any] | None,
    db,
):
    """
    Process the user message, query the LLM, stream back, and save the result.
    """
    try:
        # Build System Prompt
        sys_prompt = (
            "You are an AI assistant integrated into the IDE via bucker-agent. "
            "You can explain code, suggest fixes, create bucker tasks, "
            "explain task results, and search the codebase. "
        )
        if session.project_root:
            sys_prompt += f"\nCurrent project context: {session.project_root}"

        messages = [{"role": "system", "content": sys_prompt}]

        # Inject file or task context if provided
        if context:
            if file_path := context.get("file"):
                # Ideally read file here
                messages.append({"role": "system", "content": f"Context File: {file_path}"})
            if task_id := context.get("task_id"):
                messages.append({"role": "system", "content": f"Context Task: {task_id}"})

        # Append session history
        for m in session.messages:
            messages.append({"role": m.role, "content": m.content})

        # Call model
        router = ModelRouter()

        # In a real implementation, you would handle token streaming via a websocket manager
        # ws_manager.stream(f"ws/agent/{session_id}", chunk)
        response_content = await router.complete(messages, purpose="chat")

        # Save assistant response
        assistant_msg = ChatMessage(role="assistant", content=response_content)
        session.messages.append(assistant_msg)

        async with db.acquire() as conn:
            query = "UPDATE chat_sessions SET messages_json = $1 WHERE id = $2"
            await conn.execute(
                query,
                json.dumps([m.model_dump(mode="json") for m in session.messages]),
                session_id,
            )

    except Exception as e:
        # Broadcast error via WS in real system
        print(f"Error processing chat message: {e}")
