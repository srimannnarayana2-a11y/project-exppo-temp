"""
GitHub connector — PAT-based private repo access.

Uses fine-grained Personal Access Tokens (simplest for project).
Auth: Bearer token in Authorization header.
Clone: https://x-access-token:<PAT>@github.com/owner/repo.git
API: https://api.github.com with Accept: application/vnd.github+json

All operations are async via aiohttp.
Git clone uses asyncio.create_subprocess_exec.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

from ..config.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class RepoFile:
    """One file from a GitHub repo."""
    path: str
    content: str = ""
    size: int = 0
    sha: str = ""


@dataclass
class RepoInfo:
    """Repo metadata."""
    full_name: str
    description: str = ""
    default_branch: str = "main"
    stars: int = 0
    language: str = ""
    topics: list[str] = field(default_factory=list)


class GitHubConnector:
    """Async GitHub API client with PAT auth."""

    def __init__(self, token: str = ""):
        self._token = token or getattr(settings, 'github_token', '') or os.getenv("GITHUB_TOKEN", "")
        self._base_url = "https://api.github.com"
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def is_configured(self) -> bool:
        return bool(self._token)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ── API methods ──

    async def get_repo(self, owner: str, repo: str) -> Optional[RepoInfo]:
        """Get repo metadata."""
        session = await self._get_session()
        url = f"{self._base_url}/repos/{owner}/{repo}"

        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning("GitHub API %d for %s/%s", resp.status, owner, repo)
                    return None
                data = await resp.json()
                return RepoInfo(
                    full_name=data.get("full_name", f"{owner}/{repo}"),
                    description=data.get("description", ""),
                    default_branch=data.get("default_branch", "main"),
                    stars=data.get("stargazers_count", 0),
                    language=data.get("language", ""),
                    topics=data.get("topics", []),
                )
        except Exception as e:
            logger.warning("GitHub get_repo failed: %s", e)
            return None

    async def list_repos(
        self, per_page: int = 30, page: int = 1,
        sort: str = "updated",
    ) -> list[dict]:
        """List authenticated user's repos (personal + accessible).

        Returns lightweight dicts for frontend: {name, full_name, description, private, language, updated_at}
        """
        session = await self._get_session()
        url = f"{self._base_url}/user/repos"
        params = {
            "per_page": str(per_page),
            "page": str(page),
            "sort": sort,
            "direction": "desc",
            "type": "all",  # all repos the user has access to
        }

        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.warning("GitHub list_repos %d", resp.status)
                    return []
                data = await resp.json()
                return [
                    {
                        "name": r.get("name", ""),
                        "full_name": r.get("full_name", ""),
                        "description": r.get("description", "") or "",
                        "private": r.get("private", False),
                        "language": r.get("language", "") or "",
                        "updated_at": r.get("updated_at", ""),
                        "default_branch": r.get("default_branch", "main"),
                        "stars": r.get("stargazers_count", 0),
                    }
                    for r in data
                ]
        except Exception as e:
            logger.warning("GitHub list_repos failed: %s", e)
            return []

    async def list_tree(
        self, owner: str, repo: str, path: str = "", branch: str = "",
    ) -> list[dict]:
        """List directory contents at a path (for frontend file browser).

        Returns: [{name, path, type('file'|'dir'), size}]
        """
        if not branch:
            info = await self.get_repo(owner, repo)
            branch = info.default_branch if info else "main"

        session = await self._get_session()
        url = f"{self._base_url}/repos/{owner}/{repo}/contents/{path}"
        params = {"ref": branch}

        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                if not isinstance(data, list):
                    data = [data]
                return [
                    {
                        "name": item.get("name", ""),
                        "path": item.get("path", ""),
                        "type": item.get("type", "file"),
                        "size": item.get("size", 0),
                    }
                    for item in data
                ]
        except Exception as e:
            logger.warning("GitHub list_tree failed: %s", e)
            return []

    async def list_files(
        self, owner: str, repo: str, branch: str = "",
    ) -> list[str]:
        """List all files in a repo (via Git tree API, recursive)."""
        if not branch:
            info = await self.get_repo(owner, repo)
            branch = info.default_branch if info else "main"

        session = await self._get_session()
        url = f"{self._base_url}/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"

        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return [
                    item["path"] for item in data.get("tree", [])
                    if item.get("type") == "blob"
                ]
        except Exception as e:
            logger.warning("GitHub list_files failed: %s", e)
            return []

    async def get_file_content(
        self, owner: str, repo: str, path: str, branch: str = "",
    ) -> str:
        """Get raw file content from a repo."""
        if not branch:
            info = await self.get_repo(owner, repo)
            branch = info.default_branch if info else "main"

        session = await self._get_session()
        url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"

        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return ""
                return await resp.text()
        except Exception as e:
            logger.warning("GitHub get_file failed: %s", e)
            return ""

    async def clone_repo(
        self, owner: str, repo: str,
        target_dir: str = "",
        branch: str = "",
    ) -> str:
        """Clone a repo to local disk. Returns clone directory path.

        Uses PAT in the clone URL for private repo access:
        https://x-access-token:<PAT>@github.com/owner/repo.git
        """
        if not target_dir:
            target_dir = os.path.join(tempfile.gettempdir(), f"github_{owner}_{repo}")

        if os.path.exists(target_dir):
            # Already cloned — pull instead
            logger.info("Repo already cloned, pulling: %s", target_dir)
            try:
                proc = await asyncio.create_subprocess_exec(
                    "git", "pull",
                    cwd=target_dir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.wait()
            except Exception as e:
                logger.warning("Git pull failed: %s", e)
            return target_dir

        clone_url = f"https://x-access-token:{self._token}@github.com/{owner}/{repo}.git"
        cmd = ["git", "clone", "--depth", "1"]
        if branch:
            cmd.extend(["--branch", branch])
        cmd.extend([clone_url, target_dir])

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)

            if proc.returncode != 0:
                error = stderr.decode(errors="replace")
                logger.warning("Git clone failed: %s", error)
                return ""

            logger.info("Cloned %s/%s to %s", owner, repo, target_dir)
            return target_dir
        except asyncio.TimeoutError:
            logger.warning("Git clone timed out for %s/%s", owner, repo)
            return ""
        except Exception as e:
            logger.warning("Git clone error: %s", e)
            return ""


# ── Module singleton ──

_connector: Optional[GitHubConnector] = None


def get_github_connector() -> GitHubConnector:
    global _connector
    if _connector is None:
        _connector = GitHubConnector()
    return _connector
