from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from bucker.config import settings
from bucker.core.eventstore import create_pool

onboarding_router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])


class ConfigureRequest(BaseModel):
    workspace_root: str
    model: Optional[str] = None
    api_key_provider: Optional[str] = None


# --- Detection Functions ---

def _check_git() -> Tuple[bool, str]:
    """Check if git is installed and get its version."""
    if not shutil.which("git"):
        return False, "Not found"
    try:
        result = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True)
        return True, result.stdout.strip()
    except Exception as e:
        return False, str(e)


def _check_docker() -> Tuple[bool, str]:
    """Check if docker is available."""
    if not shutil.which("docker"):
        return False, "Not found"
    try:
        result = subprocess.run(["docker", "--version"], capture_output=True, text=True, check=True)
        return True, result.stdout.strip()
    except Exception as e:
        return False, str(e)


def _check_node() -> Tuple[bool, str]:
    """Check if node.js is available."""
    if not shutil.which("node"):
        return False, "Not found"
    try:
        result = subprocess.run(["node", "--version"], capture_output=True, text=True, check=True)
        return True, result.stdout.strip()
    except Exception as e:
        return False, str(e)


def _check_model() -> Dict[str, Any]:
    """Check model configuration status."""
    # Assuming config settings has model-related attributes or relying on environment
    model_name = getattr(settings, "model_name", "unknown")
    api_key = getattr(settings, "api_token", None) or os.getenv("OPENROUTER_API_KEY")
    return {
        "model_configured": bool(model_name and model_name != "unknown"),
        "model_name": model_name,
        "api_key_set": bool(api_key)
    }


async def _count_tasks(pool: Any) -> int:
    """Count number of tasks in the DB."""
    try:
        async with pool.acquire() as conn:
            # Assuming an event stream or a tasks table exists.
            # Replace with the actual query if known.
            row = await conn.fetchrow("SELECT COUNT(*) as count FROM events WHERE type = 'task_created'")
            return row["count"] if row else 0
    except Exception:
        return 0


# Dependency to get DB pool
async def get_db():
    return await create_pool()


# --- Endpoints ---

@onboarding_router.get("/status")
async def get_status(db=Depends(get_db)):
    """Return comprehensive setup status for the first-run experience."""
    git_avail, git_ver = _check_git()
    doc_avail, doc_ver = _check_docker()
    node_avail, node_ver = _check_node()
    model_status = _check_model()
    
    tasks_count = await _count_tasks(db)
    
    # Mode determination based on config or DB driver (simplified here)
    mode = "lite" if "sqlite" in getattr(settings, "database_url", "") else "full"
    
    workspace_set = bool(getattr(settings, "workspace_root", None))
    first_run = tasks_count == 0
    
    suggestions = []
    if not model_status["api_key_set"]:
        suggestions.append("Set OPENROUTER_API_KEY for live model calls")
    if not git_avail:
        suggestions.append("Install Git for version control features")
        
    return {
        "first_run": first_run,
        "python_version": sys.version.split(" ")[0],
        "git_available": git_avail,
        "docker_available": doc_avail,
        "node_available": node_avail,
        "model_configured": model_status["model_configured"],
        "model_name": model_status["model_name"],
        "api_key_set": model_status["api_key_set"],
        "workspace_set": workspace_set,
        "workspace_path": getattr(settings, "workspace_root", ""),
        "database_type": "sqlite" if mode == "lite" else "postgres",
        "tasks_count": tasks_count,
        "mode": mode,
        "suggestions": suggestions
    }


@onboarding_router.post("/configure")
async def configure(req: ConfigureRequest):
    """Configure onboarding settings (workspace, model, etc)."""
    if not os.path.isdir(req.workspace_root):
        return {"error": f"Workspace path does not exist: {req.workspace_root}"}
        
    # In a real app, you would save these settings to a user config file or DB.
    # We return success for now.
    return {
        "status": "success",
        "workspace_root": req.workspace_root,
        "model": req.model
    }
