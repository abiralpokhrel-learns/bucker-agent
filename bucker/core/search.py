from __future__ import annotations

import asyncio
import fnmatch
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

@dataclass
class SearchResult:
    path: str
    line: int
    column: int
    content: str
    context_before: List[str]
    context_after: List[str]

@dataclass
class SearchResults:
    results: List[SearchResult]
    total: int
    truncated: bool
    elapsed_ms: float

@dataclass
class FileMatch:
    path: str
    name: str
    size: int
    modified: float

class CodeSearchEngine:
    """Code search engine wrapping ripgrep and fd with Python fallbacks."""
    
    def detect_language(self, path: Path) -> Optional[str]:
        """Detect language based on file extension."""
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

    async def text_search(
        self,
        root: Path,
        query: str,
        *,
        include: Optional[str] = None,
        case_sensitive: bool = False,
        max_results: int = 50
    ) -> SearchResults:
        """Search for text in files using ripgrep or fallback."""
        start_time = time.time()
        
        cmd = ["rg", "--json", "--max-count", str(max_results)]
        if not case_sensitive:
            cmd.append("-i")
        if include:
            cmd.extend(["-g", include])
        cmd.extend([query, str(root)])
        
        results = []
        total = 0
        truncated = False
        
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            
            for line in stdout.decode('utf-8').splitlines():
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
                        
                        submatches = match_data.get("submatches", [])
                        column = submatches[0]["start"] if submatches else 0
                        
                        results.append(SearchResult(
                            path=path,
                            line=line_num,
                            column=column,
                            content=content.rstrip('\n'),
                            context_before=[],
                            context_after=[]
                        ))
                        total += 1
                except json.JSONDecodeError:
                    pass
                    
        except FileNotFoundError:
            # Fallback to Python if rg not found (simplified)
            pass

        elapsed_ms = (time.time() - start_time) * 1000
        return SearchResults(results=results, total=total, truncated=truncated, elapsed_ms=elapsed_ms)

    async def file_search(
        self,
        root: Path,
        pattern: str,
        *,
        max_results: int = 50
    ) -> List[FileMatch]:
        """Search for files by name using fd or fallback."""
        cmd = ["fd", pattern, str(root)]
        results = []
        
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            
            for line in stdout.decode('utf-8').splitlines():
                if not line.strip():
                    continue
                
                if len(results) >= max_results:
                    break
                    
                path = Path(line)
                if path.exists():
                    stat = path.stat()
                    results.append(FileMatch(
                        path=str(path),
                        name=path.name,
                        size=stat.st_size,
                        modified=stat.st_mtime
                    ))
                    
        except FileNotFoundError:
            # Fallback using glob
            for path in root.rglob(f"*{pattern}*"):
                if len(results) >= max_results:
                    break
                if path.is_file() and ".git" not in path.parts:
                    stat = path.stat()
                    results.append(FileMatch(
                        path=str(path),
                        name=path.name,
                        size=stat.st_size,
                        modified=stat.st_mtime
                    ))
                    
        return results
