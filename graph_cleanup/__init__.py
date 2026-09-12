"""Post-build routing-graph cleanup (`docs/SPEC-GRAPH-CLEANUP.md`).

Three passes, in order, all of them emitting `Op` records into one append-only
`ops.jsonl` per region rather than writing derived databases:

* `simplify` -- deterministic, no model. Chain contraction, navmesh ring
  contraction, redundant-edge removal, corridor smoothing. Everything it emits
  is geometrically provable.
* the model adjudicating what the simplifier could not prove (keep/drop over
  numbered candidates), and the model tracing the route most boats take.
* `validate` -- the five gates from `docs/SPEC-GRAPH-DENSITY.md`, run before any
  op is allowed to land.

`trace` holds the routing measurements the whole thing is judged by, in the repo
and under test, so they never have to be rewritten as scratch scripts again.
"""
from .graph import RoutingGraph, NodeRec, EdgeRec, edge_key, iter_chains
from .ops import Op, ApplyResult, apply, read_ops, write_ops

__all__ = [
    "RoutingGraph", "NodeRec", "EdgeRec", "edge_key", "iter_chains",
    "Op", "ApplyResult", "apply", "read_ops", "write_ops",
]
