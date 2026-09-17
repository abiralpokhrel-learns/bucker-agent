from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, HTTPException, status
from starlette.websockets import WebSocketState

from bucker.config import settings
from bucker.core.eventstore import EventStore, create_pool
from bucker.api.terminal import TerminalManager, terminal_manager

logger = logging.getLogger(__name__)

ws_router = APIRouter()

class ConnectionManager:
    """
    Manages active WebSocket connections for tasks, agents, and terminals.
    Provides methods to broadcast messages and clean up on disconnects.
    """
    def __init__(self):
        self.task_connections: Dict[str, List[WebSocket]] = {}
        self.agent_connections: Dict[str, List[WebSocket]] = {}
        self.terminal_connections: Dict[str, List[WebSocket]] = {}

    async def connect_task(self, websocket: WebSocket, task_id: str):
        await websocket.accept()
        if task_id not in self.task_connections:
            self.task_connections[task_id] = []
        self.task_connections[task_id].append(websocket)

    def disconnect_task(self, websocket: WebSocket, task_id: str):
        if task_id in self.task_connections:
            if websocket in self.task_connections[task_id]:
                self.task_connections[task_id].remove(websocket)
            if not self.task_connections[task_id]:
                del self.task_connections[task_id]

    async def broadcast_to_task(self, task_id: str, event: dict):
        if task_id in self.task_connections:
            for connection in self.task_connections[task_id]:
                if connection.client_state == WebSocketState.CONNECTED:
                    try:
                        await connection.send_json(event)
                    except Exception as e:
                        logger.error(f"Error broadcasting to task {task_id}: {e}")

    async def connect_agent(self, websocket: WebSocket, session_id: str):
        await websocket.accept()
        if session_id not in self.agent_connections:
            self.agent_connections[session_id] = []
        self.agent_connections[session_id].append(websocket)

    def disconnect_agent(self, websocket: WebSocket, session_id: str):
        if session_id in self.agent_connections:
            if websocket in self.agent_connections[session_id]:
                self.agent_connections[session_id].remove(websocket)
            if not self.agent_connections[session_id]:
                del self.agent_connections[session_id]

    async def connect_terminal(self, websocket: WebSocket, session_id: str):
        await websocket.accept()
        if session_id not in self.terminal_connections:
            self.terminal_connections[session_id] = []
        self.terminal_connections[session_id].append(websocket)

    def disconnect_terminal(self, websocket: WebSocket, session_id: str):
        if session_id in self.terminal_connections:
            if websocket in self.terminal_connections[session_id]:
                self.terminal_connections[session_id].remove(websocket)
            if not self.terminal_connections[session_id]:
                del self.terminal_connections[session_id]

manager = ConnectionManager()


async def verify_token(websocket: WebSocket, token: Optional[str]):
    """Verify the API token provided in the query string."""
    if settings.api_token:
        if token != settings.api_token:
            # Dev bypass for localhost
            client_host = websocket.client.host if websocket.client else ""
            if client_host not in ("127.0.0.1", "localhost", "::1"):
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return False
    return True


def _get_app_store() -> EventStore | None:
    try:
        from bucker.api.app import _get_store
        return _get_store()
    except Exception:
        return None


@ws_router.websocket("/ws/tasks/{task_id}")
async def task_events_ws(websocket: WebSocket, task_id: str, token: Optional[str] = Query(None)):
    """
    Stream task events in real-time. Polls the event store every 500ms for new events.
    Closes automatically when the task reaches a terminal status.
    """
    if not await verify_token(websocket, token):
        return

    await manager.connect_task(websocket, task_id)
    
    try:
        from uuid import UUID
        task_uuid = UUID(task_id)
    except ValueError:
        await websocket.send_json({"error": "Invalid task UUID"})
        await websocket.close(code=status.WS_1003_UNSUPPORTED_DATA)
        manager.disconnect_task(websocket, task_id)
        return

    try:
        last_event_id = 0
        terminal_statuses = {"completed", "failed", "cancelled", "halted", "needs_human_review"}
        
        while True:
            store = _get_app_store()
            if store is not None:
                try:
                    events = await store.read_stream(task_uuid, after_id=last_event_id)
                    for ev in events:
                        ev_dict = {
                            "id": ev.id,
                            "task_id": str(ev.task_id),
                            "event_type": ev.event_type,
                            "payload": ev.payload,
                            "created_at": ev.created_at.isoformat() if ev.created_at else None,
                        }
                        await websocket.send_json({"type": "event", "event": ev_dict})
                        last_event_id = ev.id
                        
                        # Terminal event check
                        status_val = ev.payload.get("status") if isinstance(ev.payload, dict) else None
                        if status_val in terminal_statuses:
                            await websocket.send_json({"type": "terminal", "status": status_val})
                            await asyncio.sleep(0.5)
                            await websocket.close()
                            return
                except Exception as store_err:
                    logger.debug(f"Event store read exception: {store_err}")

            await asyncio.sleep(0.5)
            
            # Simple ping to keep alive
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.send_json({"type": "ping"})
            
    except WebSocketDisconnect:
        manager.disconnect_task(websocket, task_id)
    except Exception as e:
        logger.error(f"WebSocket error in task {task_id}: {e}")
        manager.disconnect_task(websocket, task_id)


@ws_router.websocket("/ws/agent/{session_id}")
async def agent_chat_ws(websocket: WebSocket, session_id: str, token: Optional[str] = Query(None)):
    """
    Bidirectional agent chat channel.
    Receives user messages and sends back thought, tool_call, diff, token, or done events.
    """
    if not await verify_token(websocket, token):
        return

    await manager.connect_agent(websocket, session_id)
    
    async def heartbeat():
        try:
            while True:
                await asyncio.sleep(30)
                if websocket.client_state == WebSocketState.CONNECTED:
                    await websocket.send_json({"type": "ping"})
        except Exception:
            pass

    heartbeat_task = asyncio.create_task(heartbeat())
    
    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "user_message":
                    # Echo for now, normally would route to model
                    await websocket.send_json({
                        "type": "thought",
                        "payload": f"Received: {msg.get('content')}"
                    })
                elif msg.get("type") == "pong":
                    pass # heartbeat response
            except json.JSONDecodeError:
                await websocket.send_json({"error": "Invalid JSON"})
                
    except WebSocketDisconnect:
        manager.disconnect_agent(websocket, session_id)
        heartbeat_task.cancel()
    except Exception as e:
        logger.error(f"Agent WS error {session_id}: {e}")
        manager.disconnect_agent(websocket, session_id)
        heartbeat_task.cancel()


@ws_router.websocket("/ws/terminal/{session_id}")
async def terminal_ws(websocket: WebSocket, session_id: str, token: Optional[str] = Query(None)):
    """
    PTY bridge for the terminal. Receives raw bytes (keystrokes) and sends back stdout/stderr.
    """
    if not await verify_token(websocket, token):
        return

    session = terminal_manager.get_session(session_id)
    if not session:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await manager.connect_terminal(websocket, session_id)
    
    async def read_from_pty():
        try:
            while True:
                # Read from PTY (in a real app this should be async or run in executor to avoid blocking)
                # But for this simple implementation, we assume non-blocking read
                data = await asyncio.to_thread(session.read)
                if data:
                    if websocket.client_state == WebSocketState.CONNECTED:
                        await websocket.send_bytes(data)
                else:
                    if not session.alive:
                        break
                await asyncio.sleep(0.01)
        except Exception as e:
            logger.error(f"Error reading from PTY: {e}")

    pty_reader_task = asyncio.create_task(read_from_pty())
    
    try:
        while True:
            data = await websocket.receive_bytes()
            # Write to PTY
            await asyncio.to_thread(session.write, data)
    except WebSocketDisconnect:
        manager.disconnect_terminal(websocket, session_id)
        pty_reader_task.cancel()
    except Exception as e:
        logger.error(f"Terminal WS error {session_id}: {e}")
        manager.disconnect_terminal(websocket, session_id)
        pty_reader_task.cancel()
