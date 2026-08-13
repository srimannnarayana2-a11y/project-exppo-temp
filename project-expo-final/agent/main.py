"""
Agent backend — main entry point.

  python -m agent.main                    # start server
  python -m agent.main --test "query"     # run a single test query
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import uvicorn


def setup_logging(debug: bool = False):
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s │ %(levelname)-7s │ %(name)-30s │ %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


async def run_test_query(query: str):
    """Run a single query through the full pipeline and print results."""
    from .query import run_query

    print(f"\n{'='*60}")
    print(f"  Query: {query}")
    print(f"{'='*60}\n")

    result = await run_query(query)

    print(f"\n{'─'*60}")
    print(f"  Gate: {result.gate_decision.mode if result.gate_decision else 'N/A'}")
    if result.clarify_decision and result.clarify_decision.should_ask:
        print(f"  Clarify: {result.clarify_decision.question}")
    print(f"  Sources: {len(result.source_urls)}")
    print(f"  Learnings: {len(result.learnings)}")
    print(f"  Time: {result.timing_ms:.0f}ms")
    print(f"  Cached: {result.from_cache}")
    print(f"{'─'*60}\n")
    print(result.answer)
    print(f"\n{'─'*60}")

    if result.source_urls:
        print("  Sources:")
        for url in result.source_urls[:5]:
            print(f"    • {url}")

    print(f"{'='*60}\n")

    # Cleanup
    from .llm.client import get_client
    await get_client().close()


def main():
    parser = argparse.ArgumentParser(description="Agent Research Backend")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=8000, help="Bind port")
    parser.add_argument("--debug", action="store_true", help="Debug mode")
    parser.add_argument("--test", type=str, help="Run a single test query")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on code changes")
    args = parser.parse_args()

    setup_logging(args.debug)

    if args.test:
        asyncio.run(run_test_query(args.test))
        return

    # Import here to avoid circular imports during --test
    from .transport.server import create_app

    app = create_app()

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="debug" if args.debug else "info",
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
