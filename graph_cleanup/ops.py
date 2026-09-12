"""The cleanup operation record and its `ops.jsonl` file format
(`docs/SPEC-GRAPH-CLEANUP.md`).

Every pass -- the deterministic simplifier, the model adjudicating candidates,
the model tracing routes -- emits operations rather than geometry. That gives
one append-only, diffable, replayable artifact per region instead of a pile of
derived databases, and it means a decision can be attributed, filtered by
author or confidence, and reverted without re-running anything.

Operations are deliberately few and small:

| `op`          | names        | effect |
|---------------|--------------|--------|
| `drop_node`   | `node`       | remove the node and every edge on it |
| `splice_node` | `node`       | remove a degree-2 node, join its neighbours |
| `drop_edge`   | `u`, `v`     | remove one undirected edge |
| `move_node`   | `node`, `lat`, `lon` | reposition, re-measuring incident edges |

Applying is **order-dependent and forgiving**: an op whose target has already
gone, or no longer has the shape the op requires (a `splice_node` on something
that is no longer degree 2), is skipped and counted, not an error. Replaying a
file onto an already-cleaned database is therefore a no-op rather than a
corruption, which is what lets ops accumulate across sessions and survive a
region rebuild.
"""
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional

from .graph import RoutingGraph

DROP_NODE = "drop_node"
SPLICE_NODE = "splice_node"
DROP_EDGE = "drop_edge"
MOVE_NODE = "move_node"

VALID_OPS = (DROP_NODE, SPLICE_NODE, DROP_EDGE, MOVE_NODE)


@dataclass
class Op:
    """One cleanup decision.

    `author` is free text but conventionally `<kind>:<name>`, e.g. `det:dp20`,
    `det:navmesh_ring`, `ai:claude-sonnet-5`, `ai:qwen-vl-32b`. Deterministic
    passes use confidence 1.0; a model's confidence comes from its verdict.
    """

    op: str
    reason: str
    author: str
    confidence: float = 1.0
    node: Optional[int] = None
    u: Optional[int] = None
    v: Optional[int] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    tile: Optional[str] = None

    def __post_init__(self):
        if self.op not in VALID_OPS:
            raise ValueError(f"unknown op {self.op!r}, expected one of {VALID_OPS}")
        if self.op in (DROP_NODE, SPLICE_NODE) and self.node is None:
            raise ValueError(f"{self.op} requires 'node'")
        if self.op == DROP_EDGE and (self.u is None or self.v is None):
            raise ValueError("drop_edge requires 'u' and 'v'")
        if self.op == MOVE_NODE and (self.node is None or self.lat is None or self.lon is None):
            raise ValueError("move_node requires 'node', 'lat' and 'lon'")

    def to_json(self) -> str:
        return json.dumps({k: v for k, v in asdict(self).items() if v is not None},
                          sort_keys=True)

    @classmethod
    def from_json(cls, line: str) -> "Op":
        return cls(**json.loads(line))


@dataclass
class ApplyResult:
    """What `apply` actually did, for the BUILD_LOG entry and the gates."""

    applied: int = 0
    skipped: int = 0
    by_op: Dict[str, int] = field(default_factory=dict)
    by_author: Dict[str, int] = field(default_factory=dict)
    skipped_reasons: Dict[str, int] = field(default_factory=dict)
    nodes_before: int = 0
    nodes_after: int = 0
    edges_before: int = 0
    edges_after: int = 0

    def summary(self) -> str:
        dn = self.nodes_before - self.nodes_after
        de = self.edges_before - self.edges_after
        pn = 100.0 * dn / self.nodes_before if self.nodes_before else 0.0
        pe = 100.0 * de / self.edges_before if self.edges_before else 0.0
        return (
            f"applied {self.applied} ops ({self.skipped} skipped); "
            f"nodes {self.nodes_before} -> {self.nodes_after} (-{dn}, -{pn:.1f}%); "
            f"edges {self.edges_before} -> {self.edges_after} (-{de}, -{pe:.1f}%)"
        )


def write_ops(path: str, ops: Iterable[Op], append: bool = True) -> int:
    n = 0
    with open(path, "a" if append else "w", encoding="utf-8") as fh:
        for op in ops:
            fh.write(op.to_json() + "\n")
            n += 1
    return n


def read_ops(path: str) -> Iterator[Op]:
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                yield Op.from_json(line)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"{path}:{lineno}: {exc}") from exc


def apply(g: RoutingGraph, ops: Iterable[Op],
          min_confidence: float = 0.0,
          authors: Optional[List[str]] = None) -> ApplyResult:
    """Apply ops to the graph in file order.

    `authors`, when given, is a list of author prefixes to include -- so a run
    can replay only the deterministic passes, or only one model's verdicts,
    without editing the file.
    """
    res = ApplyResult(nodes_before=len(g.nodes), edges_before=len(g.edges))

    def skip(why: str):
        res.skipped += 1
        res.skipped_reasons[why] = res.skipped_reasons.get(why, 0) + 1

    for op in ops:
        if op.confidence < min_confidence:
            skip("below_min_confidence")
            continue
        if authors and not any(op.author.startswith(a) for a in authors):
            skip("author_filtered")
            continue

        if op.op == DROP_NODE:
            ok = g.remove_node(op.node)
            if not ok:
                skip("node_missing")
                continue
        elif op.op == SPLICE_NODE:
            if op.node not in g.nodes:
                skip("node_missing")
                continue
            if not g.splice_out(op.node):
                skip("not_spliceable")
                continue
        elif op.op == DROP_EDGE:
            if not g.remove_edge(op.u, op.v):
                skip("edge_missing")
                continue
        elif op.op == MOVE_NODE:
            if not g.move_node(op.node, op.lat, op.lon):
                skip("node_missing")
                continue

        res.applied += 1
        res.by_op[op.op] = res.by_op.get(op.op, 0) + 1
        res.by_author[op.author] = res.by_author.get(op.author, 0) + 1

    res.nodes_after = len(g.nodes)
    res.edges_after = len(g.edges)
    return res


def provenance_rows(ops: Iterable[Op], reviewer: str) -> List[Dict[str, Any]]:
    """Ops rendered for the `override_provenance` table, which the schema has
    carried since Phase 3 (`nautical_routing_pipeline.py`) with no rows in it.

    `reviewer` is NOT NULL in that table by design -- tier 5 in the README is
    "Human/AI-curated override, *after human sign-off*" -- so the caller has to
    name who signed off on the batch.
    """
    rows = []
    for op in ops:
        if op.op == DROP_EDGE:
            entity_type, entity_ref = "edge", f"{op.u}:{op.v}"
        else:
            entity_type, entity_ref = "node", str(op.node)
        rows.append({
            "entity_type": entity_type,
            "entity_ref": entity_ref,
            "reason": f"{op.op}: {op.reason}",
            "evidence": op.tile or "",
            "contributor": op.author,
            "reviewer": reviewer,
        })
    return rows
