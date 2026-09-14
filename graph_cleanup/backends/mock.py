"""A deterministic, offline stand-in for a real reviewer.

**Not a review of any kind.** It exists to let `graph_cleanup/runner.py` and
the prepare/apply pipeline be tested end-to-end without an API key or network
access, and to let `run_review.py --backend mock` do a full dry run of a real
region's tiles to sanity-check tile counts, sizes, and the harness itself
before spending real money on `--backend claude`. Its rule (`keep` unless the
nearest named place is implausibly far, or a component is very small) is
chosen to produce a mix of verdicts on real data, not to approximate what a
real reviewer would say.
"""
import json
import os
from typing import Optional

from .base import read_context

DEFAULT_STUB_KEEP_MAX_M = 800.0
DEFAULT_COMPONENT_KEEP_MIN_NODES = 4


class MockBackend:
    def __init__(self, stub_keep_max_m: float = DEFAULT_STUB_KEEP_MAX_M,
                component_keep_min_nodes: int = DEFAULT_COMPONENT_KEEP_MIN_NODES):
        self.stub_keep_max_m = stub_keep_max_m
        self.component_keep_min_nodes = component_keep_min_nodes

    def answer_tile(self, tile_dir: str) -> str:
        context = read_context(tile_dir)
        out = {}
        for c in context["candidates"]:
            n = str(c["n"])
            if c["kind"] == "dead_end_stub":
                dist = c.get("nearest_poi_m")
                if dist is None:
                    out[n] = {"verdict": "unsure", "why": "mock: no POI distance"}
                elif dist <= self.stub_keep_max_m:
                    out[n] = {"verdict": "keep",
                             "why": f"mock: {dist:.0f}m from {c.get('nearest_poi')}"}
                else:
                    out[n] = {"verdict": "drop",
                             "why": f"mock: {dist:.0f}m from nearest named place"}
            elif c["kind"] == "small_component":
                if c.get("n_nodes", 0) >= self.component_keep_min_nodes:
                    out[n] = {"verdict": "keep", "why": "mock: large enough to be real"}
                else:
                    out[n] = {"verdict": "drop", "why": "mock: too small to judge"}
            else:
                out[n] = {"verdict": "unsure", "why": "mock: unknown candidate kind"}
        return json.dumps(out)
