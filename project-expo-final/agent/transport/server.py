"""
FastAPI transport layer — REST + WebSocket + SSE endpoints.

Endpoints:
  POST /query          — simple request/response (non-streaming)
  GET  /query/stream   — SSE streaming (one-way token streaming)
  WS   /ws             — WebSocket bidirectional (thinking + tokens + tools)
  POST /kb/upload      — upload files/folders to knowledge base
  GET  /health         — health check
  GET  /               — API info

No frontend concerns — pure API backend.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

from ..query import run_query, run_query_stream, QueryResult, StreamEvent
from ..llm.client import get_client
from ..knowledge.kb_store import get_kb_store
from ..blocks.semantic.embed import embed_chunks
from ..blocks.semantic.chunk import chunk_text
from ..blocks.semantic.types import Chunk
from ..config.settings import settings
from ..memory.store import get_memory_store
from ..tools.output_renderer import render_output
from ..core.satisfaction import SatisfactionTracker

logger = logging.getLogger(__name__)


# ── Request/Response models ──

class QueryRequest(BaseModel):
    query: str
    session_id: Optional[str] = "default"
    memory_context: Optional[list[str]] = None
    output_format: Optional[str] = "markdown"  # markdown | html | pdf | docx | pptx


class QueryResponse(BaseModel):
    answer: str
    sources: list[str] = []
    timing_ms: float = 0.0
    from_cache: bool = False
    gate_mode: str = ""
    clarify_question: str = ""


# ── App factory ──

def create_app() -> FastAPI:
    app = FastAPI(
        title="Agent Research Backend",
        description="Industry-grade AI research agent with recursive retrieval, "
                    "hypothesis-driven pivoting, and multi-persona critique.",
        version="1.0.0",
    )

    # CORS — allow all origins for dev, restrict in production
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Lifecycle ──

    @app.on_event("startup")
    async def startup():
        logger.info("Agent backend starting...")
        client = get_client()
        key_count = len(client._keys)
        logger.info("NVIDIA NIM: %d API key(s) configured", key_count)
        if settings.brave.api_key:
            logger.info("Brave Search: configured")
        else:
            logger.info("Brave Search: not configured, using DuckDuckGo fallback")

        kb = get_kb_store()
        logger.info("Knowledge Base: %d entries loaded", kb.size)

    @app.on_event("shutdown")
    async def shutdown():
        client = get_client()
        await client.close()
        kb = get_kb_store()
        kb.save()
        logger.info("Agent backend shutdown complete")

    # ── Health check ──

    @app.get("/health")
    async def health():
        return {"status": "ok", "kb_entries": get_kb_store().size}

    @app.get("/")
    async def root():
        search_engine = (
            "brave" if settings.brave.api_key
            else "serpapi" if settings.serpapi.api_key
            else "duckduckgo"
        )
        return {
            "name": "Jarvis Agent Research Backend",
            "version": "2.1.0",
            "endpoints": {
                "query": {
                    "POST /query": "Non-streaming query (JSON body: {query, session_id, output_format})",
                    "GET /query/stream?q=...": "SSE streaming response",
                    "WS /ws": "WebSocket bidirectional (thinking + tokens + tools)",
                },
                "knowledge_base": {
                    "POST /kb/upload": "Smart file upload (auto routes to context or KB)",
                    "POST /kb/github": "Clone + ingest GitHub repo (form: owner, repo)",
                    "POST /kb/drive": "Download + ingest Drive folder (form: folder_id)",
                    "GET /kb/stats": "KB statistics (entries, sources, graph)",
                },
                "github": {
                    "GET /github/repos": "List user's repos (paginated)",
                    "GET /github/tree/{owner}/{repo}?path=&branch=": "Browse repo directory",
                    "POST /github/ingest/{owner}/{repo}": "Clone + ingest entire repo",
                },
                "drive": {
                    "GET /drive/folders?folder_id=root": "Browse Drive folder contents",
                    "GET /auth/google": "Start OAuth2 flow",
                    "GET /auth/google/callback?code=": "OAuth2 callback",
                },
                "tools": {
                    "POST /scrape": "Fetch + summarize a URL (JSON: {url, summarize})",
                    "POST /render": "Markdown → PDF/DOCX/PPTX/HTML (JSON: {markdown, format, title})",
                    "POST /skill": "Execute a Jarvis skill (form: query, output_format)",
                },
                "sessions": {
                    "GET /sessions": "List active sessions",
                    "GET /sessions/{id}/history": "Get session conversation history",
                    "DELETE /sessions/{id}": "Delete a session",
                },
                "status": {
                    "GET /config/status": "Feature availability (what's configured)",
                    "GET /skills": "List available Jarvis skills",
                    "GET /health": "Health check",
                },
            },
            "capabilities": {
                "search_engine": search_engine,
                "kb_entries": get_kb_store().size,
                "active_sessions": get_memory_store().active_sessions,
            },
        }

    # ── POST /render (output format conversion) ──

    class RenderRequest(BaseModel):
        markdown: str
        format: str = "pdf"  # pdf | docx | pptx | html
        title: str = "Report"

    @app.post("/render")
    async def render_endpoint(req: RenderRequest):
        """Convert markdown to PDF/DOCX/PPTX/HTML."""
        from fastapi.responses import Response
        result = render_output(req.markdown, req.format, req.title)
        return Response(
            content=result.content,
            media_type=result.content_type,
            headers={
                "Content-Disposition": f'attachment; filename="{result.filename}"',
            },
        )

    # ── POST /query (non-streaming) ──

    @app.post("/query", response_model=QueryResponse)
    async def query_endpoint(req: QueryRequest):
        # Session memory — tracks corrections, builds effort_bias
        mem_store = get_memory_store()
        session = mem_store.get_or_create(req.session_id or "default")
        session.add_turn("user", req.query)

        # Merge session context with explicit memory_context
        memory_ctx = session.get_context()
        if req.memory_context:
            memory_ctx.extend(req.memory_context)

        # Satisfaction tracker — reward/punishment system
        from ..core.satisfaction import SatisfactionTracker
        satisfaction = getattr(session, '_satisfaction', None)
        if satisfaction is None:
            satisfaction = SatisfactionTracker()
            session._satisfaction = satisfaction

        result = await run_query(
            req.query,
            memory_context=memory_ctx,
            effort_bias=session.effort_bias,
            satisfaction=satisfaction,
        )

        # If clarification is needed
        if result.clarify_decision and result.clarify_decision.should_ask and not result.answer:
            return QueryResponse(
                answer="",
                clarify_question=result.clarify_decision.question,
                gate_mode=result.gate_decision.mode if result.gate_decision else "",
                timing_ms=result.timing_ms,
            )

        # Record answer in session memory (for continuity)
        session.add_turn("assistant", result.answer[:500])

        return QueryResponse(
            answer=result.answer,
            sources=result.source_urls[:10],
            timing_ms=result.timing_ms,
            from_cache=result.from_cache,
            gate_mode=result.gate_decision.mode if result.gate_decision else "",
        )

    # ── POST /scrape (direct URL fetch) ──

    class ScrapeRequest(BaseModel):
        url: str
        summarize: bool = False

    @app.post("/scrape")
    async def scrape_endpoint(req: ScrapeRequest):
        """Direct URL scrape — agent tool for 'scrape this url' queries.

        Returns the raw page content. Optionally summarizes via LLM.
        This is the 'hands' — the agent can directly reach into the web.
        """
        from ..tools.web_fetch import fetch_url

        content = await fetch_url(req.url, max_chars=80_000)
        if not content:
            return JSONResponse({"error": f"Could not fetch {req.url}"}, status_code=400)

        result = {"url": req.url, "content": content, "char_count": len(content)}

        if req.summarize:
            client = get_client()
            messages = [
                {"role": "system", "content": (
                    "Summarize the following web page content concisely. "
                    "Extract the key points, main arguments, and important data. "
                    "Preserve specific numbers, names, and facts."
                )},
                {"role": "user", "content": f"URL: {req.url}\n\nContent:\n{content[:8000]}"},
            ]
            try:
                summary = await client.chat_worker(messages, temperature=0.1)
                result["summary"] = summary
            except Exception as e:
                result["summary_error"] = str(e)

        return result

    @app.get("/query/stream")
    async def stream_endpoint(q: str, memory: Optional[str] = None):
        memory_context = json.loads(memory) if memory else None

        async def event_generator():
            async for event in run_query_stream(q, memory_context=memory_context):
                data = json.dumps({
                    "type": event.type,
                    "data": event.data,
                    "metadata": event.metadata,
                })
                yield f"event: {event.type}\ndata: {data}\n\n"

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ── WebSocket /ws ──

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await ws.accept()
        logger.info("WebSocket connected")

        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    msg = {"query": raw}

                query = msg.get("query", "")
                session_id = msg.get("session_id", "ws_default")
                memory_context = msg.get("memory_context")

                if not query:
                    await ws.send_json({"type": "error", "data": "Empty query"})
                    continue

                # Session memory for WS conversations
                mem_store = get_memory_store()
                session = mem_store.get_or_create(session_id)
                session.add_turn("user", query)

                memory_ctx = session.get_context()
                if memory_context:
                    memory_ctx.extend(memory_context)

                async for event in run_query_stream(
                    query, memory_context=memory_ctx,
                    effort_bias=session.effort_bias,
                ):
                    await ws.send_json({
                        "type": event.type,
                        "data": event.data,
                        "metadata": event.metadata,
                    })

        except WebSocketDisconnect:
            logger.info("WebSocket disconnected")
        except Exception as e:
            logger.error("WebSocket error: %s", e)
            try:
                await ws.send_json({"type": "error", "data": str(e)})
            except Exception:
                pass

    # ── POST /kb/upload (smart routing) ──

    @app.post("/kb/upload")
    async def upload_to_kb(
        file: UploadFile = File(...),
        source_prefix: str = Form("upload"),
        route: str = Form("auto"),  # auto | kb | context
    ):
        """Upload a file with smart routing: context window vs KB.

        auto: small text/images → context, large/PDF/folders → KB
        kb: force KB pipeline
        context: force context window
        """
        content = await file.read()
        filename = file.filename or "unnamed"
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

        # ── Smart routing ──
        is_image = ext in ("png", "jpg", "jpeg", "webp", "gif", "bmp")
        is_doc = ext in ("pdf", "docx", "pptx", "xlsx")
        is_small_text = not is_image and not is_doc and len(content) < 16000

        if route == "context" or (route == "auto" and (is_small_text or is_image)):
            # → Context window (return content for model injection)
            if is_image:
                import base64
                b64 = base64.b64encode(content).decode()
                mime = f"image/{ext}" if ext != "jpg" else "image/jpeg"
                return {
                    "route": "context",
                    "type": "image",
                    "base64": b64,
                    "mime_type": mime,
                    "filename": filename,
                }
            else:
                text = content.decode("utf-8", errors="replace")
                return {
                    "route": "context",
                    "type": "text",
                    "content": text,
                    "filename": filename,
                    "tokens_estimate": len(text) // 4,
                }

        # → KB pipeline
        source_url = f"kb://{source_prefix}/{filename}"
        client = get_client()
        kb = get_kb_store()

        if ext == "pdf":
            from ..knowledge.pdf_ingest import ingest_pdf
            result = await ingest_pdf(pdf_bytes=content)
            chunks = chunk_text(result.markdown, source_url) if result.markdown else []
        elif ext in ("docx", "pptx", "xlsx"):
            from ..knowledge.doc_ingest import ingest_document
            import tempfile, os
            tmp = os.path.join(tempfile.gettempdir(), filename)
            with open(tmp, "wb") as f:
                f.write(content)
            result = await ingest_document(tmp)
            chunks = chunk_text(result.markdown, source_url) if result.markdown else []
        elif is_image:
            from ..knowledge.image_ingest import ingest_image_for_kb
            result = await ingest_image_for_kb(
                image_bytes=content, filename=filename, client=client,
            )
            chunks = [Chunk(text=result.caption, source_url=source_url,
                           title=f"Image: {filename}")] if result.caption else []
        else:
            text = content.decode("utf-8", errors="replace")
            chunks = chunk_text(text, source_url)

        if not chunks:
            return JSONResponse({"error": "No chunks produced"}, status_code=400)

        chunks = await embed_chunks(chunks, client=client)
        kb.add_chunks(chunks)
        kb.rebuild_matrix()
        kb.save()

        # Extract entities for knowledge graph
        try:
            from ..knowledge.graph_store import extract_entities, get_graph_store
            for c in chunks[:5]:  # Extract from top 5 chunks
                triples = await extract_entities(c.text, source_url, client=client)
                if triples:
                    gs = get_graph_store()
                    gs.add_triples(triples)
                    gs.save()
        except Exception:
            pass  # Graph is optional

        return {
            "route": "kb",
            "filename": filename,
            "format": ext,
            "chunks_added": len(chunks),
            "total_kb_entries": kb.size,
        }

    # ── POST /kb/github ──

    @app.post("/kb/github")
    async def ingest_github_repo(
        owner: str = Form(...),
        repo: str = Form(...),
        branch: str = Form(""),
    ):
        """Clone a GitHub repo and ingest into KB."""
        from ..connectors.github import get_github_connector
        from ..knowledge.folder_ingest import ingest_folder

        gh = get_github_connector()
        if not gh.is_configured:
            return JSONResponse(
                {"error": "GITHUB_TOKEN not configured"}, status_code=400,
            )

        clone_dir = await gh.clone_repo(owner, repo, branch=branch)
        if not clone_dir:
            return JSONResponse(
                {"error": f"Failed to clone {owner}/{repo}"}, status_code=500,
            )

        count = await ingest_folder(
            clone_dir, source_prefix=f"github/{owner}/{repo}",
        )

        return {
            "status": "ok",
            "repo": f"{owner}/{repo}",
            "chunks_ingested": count,
        }

    # ── GET /auth/google ──

    @app.get("/auth/google")
    async def google_auth_start():
        """Start Google Drive OAuth2 flow."""
        from ..connectors.google_drive import get_drive_connector
        drive = get_drive_connector()
        if not drive.is_configured:
            return JSONResponse(
                {"error": "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET not configured"},
                status_code=400,
            )
        return {"auth_url": drive.get_auth_url()}

    @app.get("/auth/google/callback")
    async def google_auth_callback(code: str):
        """Handle Google OAuth2 callback."""
        from ..connectors.google_drive import get_drive_connector
        drive = get_drive_connector()
        success = await drive.exchange_code(code)
        if success:
            return {"status": "authenticated"}
        return JSONResponse({"error": "Token exchange failed"}, status_code=400)

    # ── POST /kb/drive ──

    @app.post("/kb/drive")
    async def ingest_drive_folder(folder_id: str = Form(...)):
        """Download a Google Drive folder and ingest into KB."""
        from ..connectors.google_drive import get_drive_connector
        from ..knowledge.folder_ingest import ingest_folder

        drive = get_drive_connector()
        if not drive.is_authenticated:
            return JSONResponse(
                {"error": "Not authenticated. Visit /auth/google first."},
                status_code=401,
            )

        local_dir = await drive.download_folder(folder_id)
        if not local_dir:
            return JSONResponse(
                {"error": "Download failed"}, status_code=500,
            )

        count = await ingest_folder(
            local_dir, source_prefix=f"drive/{folder_id[:8]}",
        )

        return {
            "status": "ok",
            "folder_id": folder_id,
            "chunks_ingested": count,
        }

    # ── POST /skill ──

    @app.post("/skill")
    async def execute_skill_endpoint(
        query: str = Form(...),
        output_format: str = Form("markdown"),
    ):
        """Execute a Jarvis skill by matching query to registered skills."""
        from ..skills.registry import get_skill_registry
        from ..skills.executor import execute_skill

        registry = get_skill_registry()
        match = registry.match(query)

        if not match:
            return JSONResponse(
                {"error": "No skill matched", "available": registry.skill_names},
                status_code=404,
            )

        result = await execute_skill(
            match, query, output_format=output_format,
        )

        response = {
            "skill": match.skill.name,
            "score": match.score,
            "success": result.success,
            "output": result.output,
        }

        if result.rendered_format:
            response["rendered_format"] = result.rendered_format

        if result.error:
            response["error"] = result.error

        return response

    # ═══════════════════════════════════════════════════════════════
    # FRONTEND CONNECTION ENDPOINTS — Browse, Status, Sessions
    # ═══════════════════════════════════════════════════════════════

    # ── GitHub Browse ──

    @app.get("/github/repos")
    async def github_list_repos(page: int = 1, per_page: int = 30):
        """List user's GitHub repos (for frontend repo picker)."""
        from ..connectors.github import get_github_connector
        gh = get_github_connector()
        if not gh.is_configured:
            return JSONResponse(
                {"error": "GITHUB_TOKEN not configured", "configured": False},
                status_code=400,
            )
        repos = await gh.list_repos(per_page=per_page, page=page)
        return {"configured": True, "repos": repos, "page": page}

    @app.get("/github/tree/{owner}/{repo}")
    async def github_browse_tree(
        owner: str, repo: str, path: str = "", branch: str = "",
    ):
        """Browse repo directory tree (for frontend file browser)."""
        from ..connectors.github import get_github_connector
        gh = get_github_connector()
        if not gh.is_configured:
            return JSONResponse({"error": "GITHUB_TOKEN not configured"}, status_code=400)
        tree = await gh.list_tree(owner, repo, path=path, branch=branch)
        return {"owner": owner, "repo": repo, "path": path, "items": tree}

    @app.post("/github/ingest/{owner}/{repo}")
    async def github_ingest_repo(
        owner: str, repo: str, branch: str = "",
    ):
        """Clone + chunk + embed entire repo into KB."""
        from ..connectors.github import get_github_connector
        from ..knowledge.folder_ingest import ingest_folder

        gh = get_github_connector()
        if not gh.is_configured:
            return JSONResponse({"error": "GITHUB_TOKEN not configured"}, status_code=400)

        clone_dir = await gh.clone_repo(owner, repo, branch=branch)
        if not clone_dir:
            return JSONResponse({"error": f"Clone failed: {owner}/{repo}"}, status_code=500)

        count = await ingest_folder(clone_dir, source_prefix=f"github/{owner}/{repo}")
        return {
            "status": "ok",
            "repo": f"{owner}/{repo}",
            "chunks_ingested": count,
            "kb_total": get_kb_store().size,
        }

    # ── Drive Browse ──

    @app.get("/drive/folders")
    async def drive_list_folders(folder_id: str = "root"):
        """List Drive folder contents (for frontend file picker)."""
        from ..connectors.google_drive import get_drive_connector
        drive = get_drive_connector()
        if not drive.is_authenticated:
            return JSONResponse(
                {"error": "Not authenticated", "authenticated": False,
                 "auth_url": drive.get_auth_url() if drive.is_configured else ""},
                status_code=401,
            )
        files = await drive.list_folder(folder_id)
        return {
            "authenticated": True,
            "folder_id": folder_id,
            "items": [
                {
                    "id": f.id,
                    "name": f.name,
                    "type": "folder" if f.is_folder else "file",
                    "mime_type": f.mime_type,
                    "size": f.size,
                }
                for f in files
            ],
        }

    # ── Session Management ──

    @app.get("/sessions")
    async def list_sessions():
        """List active sessions (for frontend session picker)."""
        mem = get_memory_store()
        sessions = []
        for sid, session in mem._sessions.items():
            sessions.append({
                "session_id": sid,
                "turns": len(session.turns),
                "corrections": len(session.corrections),
                "has_summary": bool(session._compacted_summary),
            })
        return {"active_sessions": mem.active_sessions, "sessions": sessions}

    @app.delete("/sessions/{session_id}")
    async def delete_session(session_id: str):
        """Delete a session."""
        mem = get_memory_store()
        mem.delete(session_id)
        return {"status": "deleted", "session_id": session_id}

    @app.get("/sessions/{session_id}/history")
    async def get_session_history(session_id: str, max_turns: int = 20):
        """Get session conversation history (for restoring chat)."""
        mem = get_memory_store()
        session = mem.get(session_id)
        if not session:
            return JSONResponse({"error": "Session not found"}, status_code=404)
        return {
            "session_id": session_id,
            "turns": session.turns[-max_turns:],
            "compacted_summary": session._compacted_summary[:500] if session._compacted_summary else "",
            "corrections": session.corrections[-5:],
            "preferences": session.user_preferences,
        }

    # ── KB Status ──

    @app.get("/kb/stats")
    async def kb_stats():
        """KB statistics for frontend dashboard."""
        kb = get_kb_store()
        stats = {
            "total_entries": kb.size,
            "sources": {},
        }
        # Count by source prefix
        for chunk in getattr(kb, '_chunks', []):
            src = getattr(chunk, 'source_url', '') or ''
            prefix = src.split('/')[0] if '/' in src else src.split(':')[0] if ':' in src else 'unknown'
            stats["sources"][prefix] = stats["sources"].get(prefix, 0) + 1

        # Graph stats
        try:
            from ..knowledge.graph_store import get_graph_store
            gs = get_graph_store()
            stats["graph"] = {
                "entities": gs.entity_count,
                "triples": gs.triple_count,
            }
        except Exception:
            stats["graph"] = {"entities": 0, "triples": 0}

        return stats

    # ── Config / Status (what's configured, what's not) ──

    @app.get("/config/status")
    async def config_status():
        """Frontend checks this to know which features are available."""
        from ..connectors.github import get_github_connector
        from ..connectors.google_drive import get_drive_connector

        gh = get_github_connector()
        drive = get_drive_connector()

        # Skills
        try:
            from ..skills.registry import get_skill_registry
            skills = get_skill_registry().skill_names
        except Exception:
            skills = []

        return {
            "nim": {
                "configured": bool(settings.nim.api_keys),
                "model": settings.nim.chat_model,
                "key_count": len(settings.nim.api_keys),
            },
            "search": {
                "brave": bool(settings.brave.api_key),
                "serpapi": bool(settings.serpapi.api_key),
                "fallback": "duckduckgo",
                "active": (
                    "brave" if settings.brave.api_key
                    else "serpapi" if settings.serpapi.api_key
                    else "duckduckgo"
                ),
            },
            "github": {
                "configured": gh.is_configured,
            },
            "drive": {
                "configured": drive.is_configured,
                "authenticated": drive.is_authenticated,
                "auth_url": drive.get_auth_url() if drive.is_configured and not drive.is_authenticated else "",
            },
            "kb": {
                "entries": get_kb_store().size,
            },
            "skills": skills,
            "memory": {
                "active_sessions": get_memory_store().active_sessions,
            },
        }

    # ── Skills List ──

    @app.get("/skills")
    async def list_skills():
        """List available Jarvis skills for frontend skill picker."""
        try:
            from ..skills.registry import get_skill_registry
            registry = get_skill_registry()
            return {
                "skills": [
                    {
                        "name": s.name,
                        "description": s.description,
                        "triggers": s.triggers,
                    }
                    for s in registry._skills
                ]
            }
        except Exception as e:
            return {"skills": [], "error": str(e)}

    return app
