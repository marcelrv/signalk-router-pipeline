"""Review backends: anything that can answer one prepared tile.

`docs/SPEC-GRAPH-CLEANUP.md` §6 explains why the production path is a flat
batch of self-contained work items rather than an agentic/MCP loop -- small
local models are unreliable at multi-turn tool use, and Sonnet's answers need
to be directly comparable, decision-by-decision, to whatever runs after it.
Every backend implements the same `answer_tile(tile_dir) -> dict` shape so
`graph_cleanup/runner.py` doesn't need to know which one it's driving.
"""
from .base import Backend, BackendError
from .mock import MockBackend

__all__ = ["Backend", "BackendError", "MockBackend"]
