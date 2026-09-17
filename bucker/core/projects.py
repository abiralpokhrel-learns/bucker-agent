from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

@dataclass
class Project:
    id: str
    name: str
    root: Path
    created_at: datetime
    settings: Dict[str, Any]
    last_accessed: datetime


class ProjectStore:
    """Project management for multi-workspace support."""
    
    def __init__(self, pool: Any):
        """Initialize with a database pool (asyncpg or sqlite)."""
        self.pool = pool

    async def init_schema(self) -> None:
        """Create the projects table if it does not exist."""
        query = """
        CREATE TABLE IF NOT EXISTS projects (
            id VARCHAR(50) PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            root TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL,
            settings_json TEXT NOT NULL,
            last_accessed TIMESTAMP NOT NULL
        )
        """
        async with self.pool.acquire() as conn:
            await conn.execute(query)

    async def create(self, name: str, root: Path, settings: Optional[Dict[str, Any]] = None) -> Project:
        """Create a new project."""
        now = datetime.now(timezone.utc)
        project = Project(
            id=uuid4().hex[:12],
            name=name,
            root=root,
            created_at=now,
            settings=settings or {},
            last_accessed=now
        )
        
        query = """
        INSERT INTO projects (id, name, root, created_at, settings_json, last_accessed)
        VALUES ($1, $2, $3, $4, $5, $6)
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                query,
                project.id,
                project.name,
                str(project.root),
                project.created_at,
                json.dumps(project.settings),
                project.last_accessed
            )
        return project

    async def get(self, project_id: str) -> Optional[Project]:
        """Get a project by ID."""
        query = "SELECT * FROM projects WHERE id = $1"
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, project_id)
            
        if not row:
            return None
            
        return Project(
            id=row["id"],
            name=row["name"],
            root=Path(row["root"]),
            created_at=row["created_at"],
            settings=json.loads(row["settings_json"]),
            last_accessed=row["last_accessed"]
        )

    async def list_all(self) -> List[Project]:
        """List all projects."""
        query = "SELECT * FROM projects ORDER BY last_accessed DESC"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query)
            
        projects = []
        for row in rows:
            projects.append(Project(
                id=row["id"],
                name=row["name"],
                root=Path(row["root"]),
                created_at=row["created_at"],
                settings=json.loads(row["settings_json"]),
                last_accessed=row["last_accessed"]
            ))
        return projects

    async def update_settings(self, project_id: str, settings: Dict[str, Any]) -> None:
        """Update a project's settings."""
        query = "UPDATE projects SET settings_json = $1 WHERE id = $2"
        async with self.pool.acquire() as conn:
            await conn.execute(query, json.dumps(settings), project_id)

    async def delete(self, project_id: str) -> None:
        """Delete a project."""
        query = "DELETE FROM projects WHERE id = $1"
        async with self.pool.acquire() as conn:
            await conn.execute(query, project_id)

    async def touch(self, project_id: str) -> None:
        """Update the last_accessed timestamp for a project."""
        now = datetime.now(timezone.utc)
        query = "UPDATE projects SET last_accessed = $1 WHERE id = $2"
        async with self.pool.acquire() as conn:
            await conn.execute(query, now, project_id)

    async def get_by_root(self, root: Path) -> Optional[Project]:
        """Find a project by its workspace path."""
        query = "SELECT * FROM projects WHERE root = $1"
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, str(root))
            
        if not row:
            return None
            
        return Project(
            id=row["id"],
            name=row["name"],
            root=Path(row["root"]),
            created_at=row["created_at"],
            settings=json.loads(row["settings_json"]),
            last_accessed=row["last_accessed"]
        )
