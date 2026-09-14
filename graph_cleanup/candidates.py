"""What Pass A could not prove safe, turned into numbered review candidates
(`docs/SPEC-GRAPH-CLEANUP.md` §6).

Pass A only removes what is provably redundant -- a collinear vertex, an edge
with a cheap-enough alternative. Everything left over needs judgement: *does
this dead end lead anywhere a boater wants to go*, *is this disconnected patch
of graph real water or an artifact*. This module finds those spots and packages
each one as a `Candidate` -- an id, a kind, the nodes/edges involved, an anchor
point to hang a number on, and the measured facts a reviewer (human or model)
needs to judge it without having to read a coordinate.

Two kinds for the first pass, chosen because both render unambiguously with a
single numbered marker and both map directly onto the `missing_axis`/`drop`/
`keep` framing in the review prompt:

* **`dead_end_stub`** -- walk a degree-1 node inward to the first junction.
  7,368 of these exist on the cleaned MD graph (build #40). Most are legitimate
  (a marina entrance, a creek), some are medial-axis artifacts into a marsh.
* **`small_component`** -- a connected component other than the largest, small
  enough that "is this real" is one sensible question. On the cleaned MD graph
  the second-largest component alone is 7,818 nodes -- almost certainly a real,
  large, unstitched water body, not a candidate for a single keep/drop verdict.
  `max_component_size` exists specifically to leave those alone: bulk-judging a
  component that size from one tile would be exactly the "one code bug becomes
  a hundred data patches" mistake `docs/SPEC-GRAPH-CLEANUP.md` §3 warns against.

Not attempted here: the ~34% of edges that are merely *unused* rather than
provably redundant (`docs/SPEC-GRAPH-CLEANUP.md` §7). A useful "is this a
duplicate of that other path" candidate needs a *pairing* between the unused
edge and whichever kept edge serves the same journey, and getting that pairing
wrong produces a confusing tile rather than a useful one. Left as documented
future work rather than shipped half-considered.
"""
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .graph import RoutingGraph, edge_key, geodesic_m
from . import trace

DEAD_END_STUB = "dead_end_stub"
SMALL_COMPONENT = "small_component"


@dataclass
class Candidate:
    id: str                    # stable across runs: "stub:<terminal_node>" / "comp:<anchor_node>"
    kind: str
    nodes: List[int]           # every node involved, for rendering
    anchor: int                # the node the tile number is drawn on
    facts: Dict[str, object] = field(default_factory=dict)


def _walk_stub(g: RoutingGraph, start: int) -> List[int]:
    """From a degree-1 node, inward to the first junction (degree != 2), or
    until the path dead-ends into another degree-1 node (an isolated dangling
    edge with no junction at all)."""
    path = [start]
    prev, cur = None, start
    while True:
        nbs = [n for n in g.adj.get(cur, ()) if n != prev]
        if not nbs:
            break
        nxt = nbs[0]
        path.append(nxt)
        if g.degree(nxt) != 2:
            break
        prev, cur = cur, nxt
    return path


def _stub_length_m(g: RoutingGraph, path: Sequence[int]) -> float:
    return sum(g.edge_length_m(a, b) for a, b in zip(path, path[1:]))


def _stub_min_depth(g: RoutingGraph, path: Sequence[int]) -> Optional[float]:
    depths = [g.edge(a, b).min_depth for a, b in zip(path, path[1:])
             if g.edge(a, b) and g.edge(a, b).min_depth is not None]
    return min(depths) if depths else None


def find_dead_end_stubs(g: RoutingGraph, max_length_m: float = 3000.0,
                        pois: Optional[Sequence[Tuple[float, float, str]]] = None,
                        exclude_nodes: Optional[Set[int]] = None
                        ) -> List[Candidate]:
    """One candidate per maximal degree-1 chain, capped at `max_length_m`.

    A dead end longer than the cap is treated as a real, charted stub (a long
    approach channel, say) rather than a stray fragment, and is left alone --
    generating a candidate for it would just be asking "keep or drop this
    obviously-real thing", wasting a review on a question with only one sane
    answer.

    `exclude_nodes` skips a stub whose entire walk lies inside it -- `find_all`
    passes every node already covered by a `small_component` candidate, so a
    tiny disconnected fragment that happens to be a dangling line (not a loop)
    is judged once, as one component, rather than twice: once as a component
    and again as one or two separate dead-end stubs for the exact same nodes.
    """
    out: List[Candidate] = []
    seen: Set[int] = set()
    exclude_nodes = exclude_nodes or set()
    dead_ends = [n for n in g.adj if len(g.adj[n]) == 1]
    for start in dead_ends:
        if start in seen:
            continue
        path = _walk_stub(g, start)
        seen.update(path)
        if all(n in exclude_nodes for n in path):
            continue
        length = _stub_length_m(g, path)
        if length > max_length_m:
            continue
        # `path` runs [tip, ..., junction] (`_walk_stub` starts at the degree-1
        # node and walks inward) -- `tip` is the free end where the question
        # "does this lead anywhere" actually needs to be judged, so it anchors
        # the candidate and is what nearest-POI distance is measured from. The
        # junction stays last so `runner._drop_ops`'s `nodes[:-1]` excludes it.
        tip = path[0]
        facts: Dict[str, object] = {
            "n_nodes": len(path),
            "length_m": round(length, 1),
            "min_depth_m": _stub_min_depth(g, path),
            "protected": any(n in g.protected for n in path),
        }
        if pois is not None:
            near_name, near_dist = _nearest_poi(g.nodes[tip], pois)
            facts["nearest_poi"] = near_name
            facts["nearest_poi_m"] = round(near_dist, 0) if near_dist is not None else None
        out.append(Candidate(id=f"stub:{tip}", kind=DEAD_END_STUB,
                             nodes=path, anchor=tip, facts=facts))
    return out


def _nearest_poi(node, pois: Sequence[Tuple[float, float, str]]
                 ) -> Tuple[Optional[str], Optional[float]]:
    best_name, best_d = None, None
    for lat, lon, name in pois:
        d = geodesic_m(node.lat, node.lon, lat, lon)
        if best_d is None or d < best_d:
            best_name, best_d = name, d
    return best_name, best_d


def find_small_components(g: RoutingGraph, max_component_size: int = 30,
                          min_component_size: int = 2) -> List[Candidate]:
    """One candidate per connected component small enough to judge whole,
    excluding the largest component (that one is the graph, not a candidate).

    The anchor is the component's node nearest its own centroid, so the number
    lands inside the cluster rather than on an arbitrary member.
    """
    comps = trace.components(g)
    out: List[Candidate] = []
    for comp in comps[1:]:  # skip the largest
        if not (min_component_size <= len(comp) <= max_component_size):
            continue
        nodes = list(comp)
        clat = sum(g.nodes[n].lat for n in nodes) / len(nodes)
        clon = sum(g.nodes[n].lon for n in nodes) / len(nodes)
        anchor = min(nodes, key=lambda n: (g.nodes[n].lat - clat) ** 2 +
                                          (g.nodes[n].lon - clon) ** 2)
        total_len = sum(g.edge(u, v).distance or 0.0
                        for (u, v) in g.edges if u in comp and v in comp)
        out.append(Candidate(
            id=f"comp:{anchor}", kind=SMALL_COMPONENT, nodes=nodes, anchor=anchor,
            facts={"n_nodes": len(nodes), "total_length_m": round(total_len, 1)}))
    return out


def find_all(g: RoutingGraph, db_path: Optional[str] = None,
             max_stub_length_m: float = 3000.0,
             max_component_size: int = 30) -> List[Candidate]:
    """Every candidate kind this module knows about, in one list.

    Runs `find_small_components` first so its node set can exclude redundant
    stub candidates within those same components (see `find_dead_end_stubs`).
    """
    pois = trace.load_pois(db_path) if db_path else None
    components = find_small_components(g, max_component_size=max_component_size)
    component_nodes = {n for c in components for n in c.nodes}
    stubs = find_dead_end_stubs(g, max_length_m=max_stub_length_m, pois=pois,
                                exclude_nodes=component_nodes)
    return stubs + components


def anchor_latlon(g: RoutingGraph, c: Candidate) -> Tuple[float, float]:
    n = g.nodes[c.anchor]
    return n.lat, n.lon
