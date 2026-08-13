"""
Retrieval coordination module.

Combines semantic and code retrievers with adaptive switching.
"""

from .coordinator import RetrieverCoordinator, retrieve_adaptive

__all__ = ["RetrieverCoordinator", "retrieve_adaptive"]
