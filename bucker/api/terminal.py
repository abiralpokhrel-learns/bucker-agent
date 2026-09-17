from __future__ import annotations

import logging
import os
import platform
import subprocess
import time
import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from bucker.config import settings

logger = logging.getLogger(__name__)

# Try importing pywinpty for better Windows PTY support
try:
    if platform.system() == "Windows":
        import pywinpty

        HAS_PYWINPTY = True
    else:
        HAS_PYWINPTY = False
except ImportError:
    HAS_PYWINPTY = False

# Try importing pty for Unix PTY support
try:
    import fcntl
    import pty
    import struct
    import termios

    HAS_PTY = True
except ImportError:
    HAS_PTY = False


class TerminalSession:
    """
    Manages a single PTY session.
    """

    def __init__(self, shell: str | None = None):
        self.session_id = str(uuid.uuid4())
        self.created_at = time.time()
        self.alive = True

        self.is_windows = platform.system() == "Windows"
        self.shell = shell or (
            "cmd.exe" if self.is_windows else os.environ.get("SHELL", "/bin/bash")
        )

        self.winpty_process = None
        self.subprocess = None
        self.master_fd = None

        self._start_process()

    def _start_process(self):
        try:
            if self.is_windows:
                if HAS_PYWINPTY:
                    self.winpty_process = pywinpty.PTY(80, 24)
                    self.winpty_process.spawn(self.shell)
                    self.pid = self.winpty_process.pid
                else:
                    # Fallback for Windows without pywinpty
                    self.subprocess = subprocess.Popen(
                        self.shell,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                        bufsize=0,
                    )
                    self.pid = self.subprocess.pid
            else:
                if HAS_PTY:
                    pid, master = pty.fork()
                    if pid == 0:
                        # Child process
                        os.execlp(self.shell, self.shell)
                    else:
                        # Parent process
                        self.pid = pid
                        self.master_fd = master

                        # Set non-blocking
                        flags = fcntl.fcntl(self.master_fd, fcntl.F_GETFL)
                        fcntl.fcntl(self.master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
                else:
                    # Fallback for some strange Unix without pty
                    self.subprocess = subprocess.Popen(
                        self.shell,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        bufsize=0,
                    )
                    self.pid = self.subprocess.pid
        except Exception as e:
            logger.error(f"Failed to start terminal process: {e}")
            self.alive = False
            raise

    def write(self, data: bytes) -> None:
        if not self.alive:
            return

        try:
            if self.is_windows:
                if HAS_PYWINPTY and self.winpty_process:
                    self.winpty_process.write(data.decode("utf-8", errors="replace"))
                elif self.subprocess and self.subprocess.stdin:
                    self.subprocess.stdin.write(data)
                    self.subprocess.stdin.flush()
            else:
                if HAS_PTY and self.master_fd is not None:
                    os.write(self.master_fd, data)
                elif self.subprocess and self.subprocess.stdin:
                    self.subprocess.stdin.write(data)
                    self.subprocess.stdin.flush()
        except Exception as e:
            logger.error(f"Error writing to terminal {self.session_id}: {e}")
            self.alive = False

    def read(self) -> bytes:
        if not self.alive:
            return b""

        try:
            if self.is_windows:
                if HAS_PYWINPTY and self.winpty_process:
                    # pywinpty read is blocking, we should be careful.
                    # Using a short timeout or checking if data is available would be better.
                    data = self.winpty_process.read()
                    if not data:
                        self._check_alive()
                    return data.encode("utf-8") if data else b""
                elif self.subprocess and self.subprocess.stdout:
                    # this will block without non-blocking IO setup
                    # fallback only
                    self._check_alive()
                    return b""
            else:
                if HAS_PTY and self.master_fd is not None:
                    try:
                        data = os.read(self.master_fd, 1024)
                        return data
                    except BlockingIOError:
                        self._check_alive()
                        return b""
                elif self.subprocess and self.subprocess.stdout:
                    # Fallback only
                    self._check_alive()
                    return b""
        except OSError:
            self.alive = False
            return b""
        except Exception as e:
            logger.error(f"Error reading from terminal {self.session_id}: {e}")
            self.alive = False
            return b""

        return b""

    def _check_alive(self):
        if self.subprocess:
            if self.subprocess.poll() is not None:
                self.alive = False
        elif self.is_windows and HAS_PYWINPTY and self.winpty_process:
            if not self.winpty_process.isalive():
                self.alive = False
        elif not self.is_windows and HAS_PTY:
            try:
                pid, status = os.waitpid(self.pid, os.WNOHANG)
                if pid == self.pid:
                    self.alive = False
            except ChildProcessError:
                self.alive = False

    def resize(self, cols: int, rows: int) -> None:
        if not self.alive:
            return

        if self.is_windows and HAS_PYWINPTY and self.winpty_process:
            self.winpty_process.set_size(cols, rows)
        elif not self.is_windows and HAS_PTY and self.master_fd is not None:
            try:
                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
            except Exception as e:
                logger.error(f"Failed to resize PTY: {e}")

    def close(self) -> None:
        self.alive = False
        try:
            if self.is_windows:
                if HAS_PYWINPTY and self.winpty_process:
                    del self.winpty_process
                elif self.subprocess:
                    self.subprocess.terminate()
            else:
                if HAS_PTY and self.master_fd is not None:
                    os.close(self.master_fd)
                elif self.subprocess:
                    self.subprocess.terminate()
        except Exception as e:
            logger.error(f"Error closing terminal {self.session_id}: {e}")

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "pid": self.pid,
            "created_at": self.created_at,
            "alive": self.alive,
        }


class TerminalManager:
    """
    Manages terminal sessions, respecting limits.
    """

    def __init__(self):
        self.sessions: dict[str, TerminalSession] = {}
        # Fetch from settings if possible, otherwise default 5
        self.max_sessions = getattr(settings, "max_terminal_sessions", 5)

    def create_session(self, shell: str | None = None) -> str:
        # Clean up dead sessions
        dead_sessions = [sid for sid, s in self.sessions.items() if not s.alive]
        for sid in dead_sessions:
            self.close_session(sid)

        if len(self.sessions) >= self.max_sessions:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Maximum number of terminal sessions ({self.max_sessions}) reached.",
            )

        session = TerminalSession(shell=shell)
        self.sessions[session.session_id] = session
        return session.session_id

    def get_session(self, session_id: str) -> TerminalSession | None:
        return self.sessions.get(session_id)

    def close_session(self, session_id: str) -> None:
        session = self.sessions.pop(session_id, None)
        if session:
            session.close()

    def list_sessions(self) -> list[dict]:
        return [session.to_dict() for session in self.sessions.values()]

    def close_all(self) -> None:
        for session_id in list(self.sessions.keys()):
            self.close_session(session_id)


terminal_manager = TerminalManager()
terminal_router = APIRouter(prefix="/api/terminal/sessions", tags=["terminal"])


class CreateSessionRequest(BaseModel):
    shell: str | None = None


class ResizeRequest(BaseModel):
    cols: int
    rows: int


@terminal_router.post("", response_model=dict)
async def create_session(req: CreateSessionRequest | None = None):
    """Create a new terminal session."""
    shell = req.shell if req else None
    session_id = terminal_manager.create_session(shell=shell)
    session = terminal_manager.get_session(session_id)
    return {"session_id": session_id, "pid": session.pid if session else None}


@terminal_router.get("", response_model=list[dict])
async def list_sessions():
    """List active terminal sessions."""
    return terminal_manager.list_sessions()


@terminal_router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def close_session(session_id: str):
    """Close a terminal session."""
    terminal_manager.close_session(session_id)


@terminal_router.post("/{session_id}/resize", status_code=status.HTTP_200_OK)
async def resize_session(session_id: str, req: ResizeRequest):
    """Resize a PTY terminal."""
    session = terminal_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    session.resize(req.cols, req.rows)
    return {"status": "ok"}
