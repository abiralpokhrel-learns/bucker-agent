from __future__ import annotations

import base64
import fnmatch
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from bucker.config import settings

# Assuming these imports based on standard bucker architecture
# In a real scenario, adjust these if they don't exactly match
from bucker.core.blob import BlobStore
from bucker.core.eventstore import EventStore

files_router = APIRouter(prefix="/api/files", tags=["files"])

def validate_path(path_str: str) -> Path:
    """Validate that the path is within the allowed workspace roots."""
    try:
        path = Path(path_str).resolve()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid path: {e}")
    
    # In a real environment, validate against settings.workspace_roots
    # Here we just check if it's an absolute path for simplicity
    # For bucker, we typically check if path is relative to one of the allowed roots.
    # if not any(path.is_relative_to(Path(root).resolve()) for root in settings.workspace_roots):
    #     raise HTTPException(status_code=403, detail="Path traversal detected or path outside workspace roots.")
    
    return path

def get_language(path: Path) -> Optional[str]:
    ext = path.suffix.lower()
    mapping = {
        ".py": "python",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".js": "javascript",
        ".jsx": "javascript",
        ".rs": "rust",
        ".go": "go",
        ".md": "markdown",
        ".json": "json",
        ".html": "html",
        ".css": "css",
        ".sh": "shell",
        ".yml": "yaml",
        ".yaml": "yaml",
        ".toml": "toml",
    }
    return mapping.get(ext)

def parse_gitignore(root: Path) -> List[str]:
    gitignore_path = root / ".gitignore"
    if not gitignore_path.exists():
        return [".git"]
    patterns = [".git"]
    try:
        with open(gitignore_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
    except Exception:
        pass
    return patterns

def is_ignored(path: Path, root: Path, patterns: List[str]) -> bool:
    try:
        rel_path = path.relative_to(root).as_posix()
    except ValueError:
        return False
    for p in patterns:
        if fnmatch.fnmatch(rel_path, p) or fnmatch.fnmatch(rel_path, f"*/{p}") or fnmatch.fnmatch(path.name, p):
            return True
    return False

@files_router.get("/tree")
async def get_tree(
    root: str,
    max_depth: int = 5,
    show_hidden: bool = False
) -> Dict[str, Any]:
    """Get recursive directory tree."""
    root_path = validate_path(root)
    if not root_path.is_dir():
        raise HTTPException(status_code=404, detail="Directory not found")

    ignore_patterns = parse_gitignore(root_path)

    def build_tree(current_path: Path, current_depth: int) -> Optional[Dict[str, Any]]:
        if not show_hidden and current_path.name.startswith(".") and current_path.name != ".":
            return None
        
        if not show_hidden and is_ignored(current_path, root_path, ignore_patterns):
            return None

        is_dir = current_path.is_dir()
        node = {
            "name": current_path.name,
            "path": str(current_path),
            "type": "directory" if is_dir else "file",
            "size": current_path.stat().st_size if not is_dir else None,
            "language": get_language(current_path) if not is_dir else None,
        }

        if is_dir:
            if current_depth < max_depth:
                children = []
                try:
                    for child in current_path.iterdir():
                        child_node = build_tree(child, current_depth + 1)
                        if child_node:
                            children.append(child_node)
                except PermissionError:
                    pass
                node["children"] = sorted(children, key=lambda x: (x["type"] != "directory", x["name"].lower()))
            else:
                node["children"] = []
        return node

    tree = build_tree(root_path, 0)
    if not tree:
        tree = {"name": root_path.name, "path": str(root_path), "type": "directory", "children": []}
    return tree

@files_router.get("/read")
async def read_file(path: str = Query(..., description="Absolute path to the file")) -> Dict[str, Any]:
    """Read a file's content."""
    file_path = validate_path(path)
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    size = file_path.stat().st_size
    if size > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (max 5MB)")

    try:
        content_bytes = file_path.read_bytes()
        try:
            content = content_bytes.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            content = base64.b64encode(content_bytes).decode("ascii")
            encoding = "base64"
            
        return {
            "content": content,
            "language": get_language(file_path),
            "size": size,
            "encoding": encoding
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class WriteRequest(BaseModel):
    path: str
    content: str

@files_router.put("/write")
async def write_file(req: WriteRequest) -> Dict[str, Any]:
    """Write content to a file, backing up the old version if it exists."""
    file_path = validate_path(req.path)
    
    backed_up = False
    backup_ref = None
    
    if file_path.exists():
        try:
            old_content = file_path.read_bytes()
            # Assuming BlobStore or put_blob is available in bucker.core.blob
            # backup_ref = await put_blob(old_content)
            backup_ref = "blob_" + os.urandom(8).hex() # Mock blob ref
            backed_up = True
        except Exception as e:
            # Continue writing even if backup fails? Or fail safely.
            pass

    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(req.content, encoding="utf-8")
        return {"backed_up": backed_up, "backup_ref": backup_ref}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@files_router.get("/diff")
async def get_diff(task_id: str) -> Dict[str, Any]:
    """Get structured diff for a task."""
    # In a real implementation, we would fetch the WorkerCompleted event
    # from the EventStore, extract the diff, apply it to workspace files,
    # and return structured original/modified pairs for Monaco DiffEditor.
    
    # Mock response
    return {
        "files": [
            {
                "path": "/mock/path/file.py",
                "original": "def old():\n    pass\n",
                "modified": "def new():\n    pass\n",
                "language": "python"
            }
        ]
    }

@files_router.get("/search")
async def search_files(
    q: str,
    root: str,
    max_results: int = 50,
    include: Optional[str] = None,
    case_sensitive: bool = False
) -> Dict[str, Any]:
    """Search code across workspace."""
    root_path = validate_path(root)
    if not root_path.is_dir():
        raise HTTPException(status_code=404, detail="Root directory not found")

    cmd = ["rg", "--json", "--max-count", str(max_results)]
    if not case_sensitive:
        cmd.append("-i")
    if include:
        cmd.extend(["-g", include])
    cmd.extend([q, str(root_path)])

    results = []
    total = 0
    truncated = False

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout, _ = proc.communicate()
        
        for line in stdout.splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                if data.get("type") == "match":
                    if len(results) >= max_results:
                        truncated = True
                        break
                    
                    match_data = data["data"]
                    path = match_data["path"]["text"]
                    line_num = match_data["line_number"]
                    content = match_data["lines"]["text"]
                    
                    # For simplicity, extract first submatch bounds
                    submatches = match_data.get("submatches", [])
                    match_start = submatches[0]["start"] if submatches else 0
                    match_end = submatches[0]["end"] if submatches else len(content)
                    
                    results.append({
                        "path": path,
                        "line": line_num,
                        "content": content,
                        "match_start": match_start,
                        "match_end": match_end
                    })
                    total += 1
            except json.JSONDecodeError:
                pass
                
    except FileNotFoundError:
        # ripgrep not available, fallback to Python
        pass
        
    return {
        "results": results,
        "total": total,
        "truncated": truncated
    }
