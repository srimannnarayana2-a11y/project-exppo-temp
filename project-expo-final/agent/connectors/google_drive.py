"""
Google Drive connector — OAuth2 user flow for personal Drive access.

Flow:
  1. User visits /auth/google → redirects to Google consent screen
  2. Google redirects back with auth code
  3. Exchange code for access_token + refresh_token
  4. Store tokens, auto-refresh on expiry
  5. Use access_token with Drive API v3 REST endpoints

All API calls async via aiohttp (not the sync google-api-python-client).
Rate limited via asyncio.Semaphore.
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

# Rate limit: max 5 concurrent Drive API calls
_SEMAPHORE = asyncio.Semaphore(5)


@dataclass
class DriveFile:
    """One file from Google Drive."""
    id: str
    name: str
    mime_type: str = ""
    size: int = 0
    is_folder: bool = False
    parent_id: str = ""


@dataclass
class DriveTokens:
    """OAuth2 tokens for Drive access."""
    access_token: str = ""
    refresh_token: str = ""
    expires_at: float = 0.0
    token_type: str = "Bearer"

    @property
    def is_expired(self) -> bool:
        import time
        return time.time() >= self.expires_at - 60  # 60s buffer


class GoogleDriveConnector:
    """Async Google Drive client with OAuth2 user flow."""

    def __init__(
        self,
        client_id: str = "",
        client_secret: str = "",
        redirect_uri: str = "http://localhost:8000/auth/google/callback",
    ):
        self._client_id = client_id or os.getenv("GOOGLE_CLIENT_ID", "")
        self._client_secret = client_secret or os.getenv("GOOGLE_CLIENT_SECRET", "")
        self._redirect_uri = redirect_uri
        self._tokens: Optional[DriveTokens] = None
        self._session: Optional[aiohttp.ClientSession] = None

        # Try loading persisted tokens
        self._load_tokens()

    @property
    def is_configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    @property
    def is_authenticated(self) -> bool:
        return self._tokens is not None and bool(self._tokens.access_token)

    # ── OAuth2 Flow ──

    def get_auth_url(self, state: str = "") -> str:
        """Generate the Google OAuth2 consent URL."""
        from urllib.parse import urlencode
        params = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "scope": "https://www.googleapis.com/auth/drive.readonly",
            "access_type": "offline",
            "prompt": "consent",
        }
        if state:
            params["state"] = state
        return f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"

    async def exchange_code(self, code: str) -> bool:
        """Exchange auth code for tokens."""
        async with aiohttp.ClientSession() as session:
            data = {
                "code": code,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": self._redirect_uri,
                "grant_type": "authorization_code",
            }
            async with session.post(
                "https://oauth2.googleapis.com/token",
                data=data,
            ) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    logger.warning("Token exchange failed: %s", error)
                    return False

                result = await resp.json()
                import time
                self._tokens = DriveTokens(
                    access_token=result["access_token"],
                    refresh_token=result.get("refresh_token", ""),
                    expires_at=time.time() + result.get("expires_in", 3600),
                )
                self._save_tokens()
                return True

    async def _refresh_if_needed(self):
        """Auto-refresh access token if expired."""
        if not self._tokens or not self._tokens.is_expired:
            return
        if not self._tokens.refresh_token:
            logger.warning("No refresh token, cannot auto-refresh")
            return

        async with aiohttp.ClientSession() as session:
            data = {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._tokens.refresh_token,
                "grant_type": "refresh_token",
            }
            async with session.post(
                "https://oauth2.googleapis.com/token",
                data=data,
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    import time
                    self._tokens.access_token = result["access_token"]
                    self._tokens.expires_at = time.time() + result.get("expires_in", 3600)
                    self._save_tokens()
                else:
                    logger.warning("Token refresh failed: %d", resp.status)

    # ── Drive API ──

    async def _get_session(self) -> aiohttp.ClientSession:
        await self._refresh_if_needed()
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "Authorization": f"Bearer {self._tokens.access_token}" if self._tokens else "",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def list_folder(self, folder_id: str = "root") -> list[DriveFile]:
        """List files in a Drive folder."""
        async with _SEMAPHORE:
            session = await self._get_session()
            url = "https://www.googleapis.com/drive/v3/files"
            params = {
                "q": f"'{folder_id}' in parents and trashed=false",
                "fields": "files(id,name,mimeType,size,parents)",
                "pageSize": "100",
            }

            try:
                async with session.get(url, params=params) as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json()

                    files = []
                    for f in data.get("files", []):
                        files.append(DriveFile(
                            id=f["id"],
                            name=f["name"],
                            mime_type=f.get("mimeType", ""),
                            size=int(f.get("size", 0)),
                            is_folder=f.get("mimeType") == "application/vnd.google-apps.folder",
                            parent_id=folder_id,
                        ))
                    return files
            except Exception as e:
                logger.warning("Drive list_folder failed: %s", e)
                return []

    async def download_file(self, file_id: str) -> bytes:
        """Download a file's content as bytes."""
        async with _SEMAPHORE:
            session = await self._get_session()
            url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"

            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return b""
                    return await resp.read()
            except Exception as e:
                logger.warning("Drive download failed: %s", e)
                return b""

    async def download_folder(
        self, folder_id: str, target_dir: str = "",
    ) -> str:
        """Download entire folder recursively to local disk.

        Returns path to the local directory.
        """
        if not target_dir:
            target_dir = os.path.join(tempfile.gettempdir(), f"drive_{folder_id[:8]}")
        os.makedirs(target_dir, exist_ok=True)

        files = await self.list_folder(folder_id)

        tasks = []
        for f in files:
            if f.is_folder:
                # Recurse into subfolder
                sub_dir = os.path.join(target_dir, f.name)
                tasks.append(self.download_folder(f.id, sub_dir))
            else:
                # Download file
                tasks.append(self._download_single(f, target_dir))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        return target_dir

    async def _download_single(self, file: DriveFile, target_dir: str):
        """Download a single file to target_dir."""
        content = await self.download_file(file.id)
        if content:
            path = os.path.join(target_dir, file.name)
            with open(path, "wb") as f:
                f.write(content)

    # ── Token persistence ──

    def _save_tokens(self):
        """Persist tokens to disk."""
        if not self._tokens:
            return
        token_dir = getattr(settings, 'kb_dir', '.kb_data')
        os.makedirs(token_dir, exist_ok=True)
        path = os.path.join(token_dir, "drive_tokens.json")
        with open(path, "w") as f:
            json.dump({
                "access_token": self._tokens.access_token,
                "refresh_token": self._tokens.refresh_token,
                "expires_at": self._tokens.expires_at,
            }, f)

    def _load_tokens(self):
        """Load persisted tokens."""
        token_dir = getattr(settings, 'kb_dir', '.kb_data')
        path = os.path.join(token_dir, "drive_tokens.json")
        if not os.path.exists(path):
            return
        try:
            with open(path) as f:
                data = json.load(f)
            self._tokens = DriveTokens(
                access_token=data.get("access_token", ""),
                refresh_token=data.get("refresh_token", ""),
                expires_at=data.get("expires_at", 0),
            )
        except Exception:
            pass


# ── Module singleton ──

_connector: Optional[GoogleDriveConnector] = None


def get_drive_connector() -> GoogleDriveConnector:
    global _connector
    if _connector is None:
        _connector = GoogleDriveConnector()
    return _connector
