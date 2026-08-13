"""
Session Store — Conversation Persistence & Restoration

From Jarvis's `sessionManager.ts`:
  - Saves conversation history to disk (.agent/session.json)
  - Allows "continue" after crash or restart
  - Auto-summarizes old turns when context fills up
  - Restores full context on next run

Industry pattern:
  - Enable persistent conversations across server restarts
  - Prevent loss of multi-turn reasoning on server crash
  - Compress old context automatically
  - Users can "load session" to resume interrupted work

Usage:
    from agent.memory.session_store import SessionStore
    
    store = SessionStore()
    history = store.load()
    if history:
        print(f"Resumed {len(history)} messages")
    else:
        print("New session")
    
    # ... do work ...
    
    store.save(updated_history)
    store.clear()  # optional: clear on logout
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)


# ─── Config ───────────────────────────────────────────────────────────────────

# Default session directory
DEFAULT_SESSION_DIR = ".agent"
DEFAULT_SESSION_FILE = "session.json"

# Versioning for session format (for future migrations)
SESSION_VERSION = 1


# ─── Session Metadata ─────────────────────────────────────────────────────────

@dataclass
class SessionMetadata:
    """Metadata about a saved session."""
    session_id: str          # UUID or timestamp-based ID
    created_at: str          # ISO 8601 datetime
    last_updated_at: str     # ISO 8601 datetime
    message_count: int       # Total messages in session
    version: int = SESSION_VERSION
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ─── Session Store ────────────────────────────────────────────────────────────

class SessionStore:
    """
    Persistent session storage.
    
    Manages conversation history on disk with:
      - Automatic save/load
      - Metadata tracking
      - History validation
      - Directory auto-creation
    """
    
    def __init__(
        self,
        session_dir: Optional[str] = None,
        session_file: str = DEFAULT_SESSION_FILE,
        cwd: Optional[str] = None,
    ):
        """
        Initialize session store.
        
        Args:
            session_dir: Directory to store sessions (default: .agent)
            session_file: Session filename (default: session.json)
            cwd: Working directory (default: current directory)
        """
        self.cwd = Path(cwd or ".")
        self.session_dir = self.cwd / (session_dir or DEFAULT_SESSION_DIR)
        self.session_path = self.session_dir / session_file
        
        # Create directory if needed
        self.session_dir.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"SessionStore initialized: {self.session_path}")
    
    def save(
        self,
        history: List[Dict[str, Any]],
        session_id: Optional[str] = None,
    ) -> None:
        """
        Save conversation history to disk.
        
        Args:
            history: Conversation history (list of message dicts)
            session_id: Session ID (auto-generated if not provided)
        """
        if not session_id:
            session_id = datetime.now().isoformat()
        
        now = datetime.now().isoformat()
        
        # Create session object
        session_data = {
            "metadata": {
                "session_id": session_id,
                "created_at": now,
                "last_updated_at": now,
                "message_count": len(history),
                "version": SESSION_VERSION,
            },
            "history": history,
        }
        
        try:
            # Write to temporary file first (atomic write)
            temp_path = self.session_path.with_suffix(".tmp")
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(session_data, f, indent=2)
            
            # Atomic rename
            temp_path.replace(self.session_path)
            
            logger.info(
                f"Saved session: {len(history)} messages to {self.session_path}"
            )
        except Exception as e:
            logger.error(f"Failed to save session: {e}")
            raise
    
    def load(self) -> Optional[List[Dict[str, Any]]]:
        """
        Load conversation history from disk.
        
        Returns:
            Conversation history or None if not found
        """
        if not self.session_path.exists():
            logger.info(f"No session file found at {self.session_path}")
            return None
        
        try:
            with open(self.session_path, "r", encoding="utf-8") as f:
                session_data = json.load(f)
            
            history = session_data.get("history", [])
            metadata = session_data.get("metadata", {})
            
            # Validate history
            valid, error = self._validate_history(history)
            if not valid:
                logger.warning(f"Session validation failed: {error}. Starting fresh.")
                return None
            
            logger.info(
                f"Loaded session: {len(history)} messages "
                f"(session_id={metadata.get('session_id', 'unknown')})"
            )
            
            return history
        except Exception as e:
            logger.error(f"Failed to load session: {e}")
            return None
    
    def clear(self) -> None:
        """Delete the session file."""
        try:
            if self.session_path.exists():
                self.session_path.unlink()
                logger.info(f"Cleared session: {self.session_path}")
        except Exception as e:
            logger.error(f"Failed to clear session: {e}")
            raise
    
    def exists(self) -> bool:
        """Check if a session exists."""
        return self.session_path.exists()
    
    def get_metadata(self) -> Optional[SessionMetadata]:
        """Get session metadata without loading full history."""
        if not self.session_path.exists():
            return None
        
        try:
            with open(self.session_path, "r", encoding="utf-8") as f:
                session_data = json.load(f)
            
            meta_dict = session_data.get("metadata", {})
            return SessionMetadata(
                session_id=meta_dict.get("session_id", "unknown"),
                created_at=meta_dict.get("created_at", ""),
                last_updated_at=meta_dict.get("last_updated_at", ""),
                message_count=meta_dict.get("message_count", 0),
                version=meta_dict.get("version", SESSION_VERSION),
            )
        except Exception as e:
            logger.warning(f"Failed to read metadata: {e}")
            return None
    
    def list_sessions(self) -> List[str]:
        """List all session files in session directory."""
        try:
            return [
                f.name for f in self.session_dir.glob("*.json")
                if f.name != ".gitkeep"
            ]
        except Exception as e:
            logger.warning(f"Failed to list sessions: {e}")
            return []
    
    # Private helper methods
    
    @staticmethod
    def _validate_history(history: Any) -> tuple[bool, str]:
        """
        Validate conversation history format.
        
        Returns:
            (is_valid, error_message)
        """
        if not isinstance(history, list):
            return False, "History must be a list"
        
        if not history:
            return False, "History cannot be empty"
        
        # Check first message is system
        if history[0].get("role") != "system":
            return False, "First message must have role='system'"
        
        # Check all messages have required fields
        for i, msg in enumerate(history):
            if not isinstance(msg, dict):
                return False, f"Message {i} must be a dict"
            if "role" not in msg or "content" not in msg:
                return False, f"Message {i} missing 'role' or 'content'"
            
            valid_roles = {"system", "user", "assistant", "tool"}
            if msg["role"] not in valid_roles:
                return False, f"Message {i} has invalid role: {msg['role']}"
        
        return True, ""


# ─── Global Session Store Instance ────────────────────────────────────────────

_GLOBAL_STORE: Optional[SessionStore] = None


def get_session_store(
    session_dir: Optional[str] = None,
    cwd: Optional[str] = None,
) -> SessionStore:
    """
    Get or create global session store.
    
    Args:
        session_dir: Custom session directory
        cwd: Custom working directory
    
    Returns:
        SessionStore instance
    """
    global _GLOBAL_STORE
    if _GLOBAL_STORE is None:
        _GLOBAL_STORE = SessionStore(session_dir=session_dir, cwd=cwd)
    return _GLOBAL_STORE


def save_session(history: List[Dict[str, Any]]) -> None:
    """Save conversation to global session store."""
    store = get_session_store()
    store.save(history)


def load_session() -> Optional[List[Dict[str, Any]]]:
    """Load conversation from global session store."""
    store = get_session_store()
    return store.load()


def clear_session() -> None:
    """Clear the global session."""
    store = get_session_store()
    store.clear()


def session_exists() -> bool:
    """Check if a session exists."""
    store = get_session_store()
    return store.exists()


def get_session_metadata() -> Optional[SessionMetadata]:
    """Get metadata of the current session."""
    store = get_session_store()
    return store.get_metadata()
