"""Routing queries over a built `.sqlite`: Dijkstra, POI snapping, components,
and the POI-to-POI route network.

**This module exists to stop being rewritten.** `data/BUILD_LOG.md` records that
the on-axis-share metric for build #39 could not be re-run because `gates.py`
and `gates_zeeland.py` "were scratch scripts from an earlier session and are no
longer present in the repo or scratchpad". Every measurement the cleanup work
depends on lives here, in the repo, under test.

The cost function matches what routeiq minimises: `distance * cost_factor`.
Vessel constraints (draft, beam, air draft) are deliberately *not* applied --
cleanup asks "is this stretch of graph ever useful to anyone", and filtering by
one vessel's dimensions would delete water that a shallower boat uses.
"""
import collections
import heapq
import math
import sqlite3
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .graph import RoutingGraph, edge_key

INF = float("inf")


def dijkstra(g: RoutingGraph, source: int,
             targets: Optional[Set[int]] = None) -> Tuple[Dict[int, float], Dict[int, int]]:
    """Single-source shortest paths. Stops early once every target is settled."""
    dist: Dict[int, float] = {source: 0.0}
    parent: Dict[int, int] = {}
    remaining = set(targets) - {source} if targets else None
    pq: List[Tuple[float, int]] = [(0.0, source)]
    settled: Set[int] = set()
    while pq:
        d, u = heapq.heappop(pq)
        if u in settled:
            continue
        settled.add(u)
        if remaining is not None:
            remaining.discard(u)
            if not remaining:
                break
        for v in g.adj.get(u, ()):
            e = g.edges.get(edge_key(u, v))
            if e is None:
                continue
            nd = d + e.weight
            if nd < dist.get(v, INF):
                dist[v] = nd
                parent[v] = u
                heapq.heappush(pq, (nd, v))
    return dist, parent


def path_from_parents(parent: Dict[int, int], source: int, target: int) -> List[int]:
    """`[source, ..., target]`, or `[]` when target was never reached."""
    if target == source:
        return [source]
    if target not in parent:
        return []
    out = [target]
    cur = target
    while cur != source:
        cur = parent[cur]
        out.append(cur)
        if len(out) > 10_000_000:  # pragma: no cover - corrupt parent map
            raise RuntimeError("cycle in parent map")
    out.reverse()
    return out


def shortest_path(g: RoutingGraph, source: int, target: int) -> Tuple[List[int], float]:
    dist, parent = dijkstra(g, source, targets={target})
    return path_from_parents(parent, source, target), dist.get(target, INF)


def components(g: RoutingGraph) -> List[Set[int]]:
    """Connected components, largest first."""
    seen: Set[int] = set()
    out: List[Set[int]] = []
    for start in g.nodes:
        if start in seen:
            continue
        comp = {start}
        seen.add(start)
        stack = [start]
        while stack:
            u = stack.pop()
            for v in g.adj.get(u, ()):
                if v not in seen:
                    seen.add(v)
                    comp.add(v)
                    stack.append(v)
        out.append(comp)
    out.sort(key=len, reverse=True)
    return out


def largest_component_length_fraction(g: RoutingGraph) -> float:
    """Share of total edge length inside the largest component.

    Measured by length, never by node count. `docs/SPEC-GRAPH-DENSITY.md` 6.1:
    the node-count form "sent two investigations chasing a 2.61pp 'regression'
    that does not exist" -- and it is exactly wrong for this work, where the
    whole point is to remove nodes without removing reachable water.
    """
    comps = components(g)
    if not comps:
        return 0.0
    total = g.total_edge_length_m()
    if total <= 0:
        return 0.0
    biggest = comps[0]
    inside = sum(e.distance or 0.0 for (u, v), e in g.edges.items()
                 if u in biggest and v in biggest)
    return inside / total


class NodeIndex:
    """Coarse lat/lon bucket index for snapping arbitrary points to nodes."""

    def __init__(self, g: RoutingGraph, cell_deg: float = 0.01):
        self.cell = cell_deg
        self.buckets: Dict[Tuple[int, int], List[int]] = collections.defaultdict(list)
        for n in g.nodes.values():
            self.buckets[self._key(n.lat, n.lon)].append(n.id)
        self.g = g

    def _key(self, lat: float, lon: float) -> Tuple[int, int]:
        return (int(math.floor(lat / self.cell)), int(math.floor(lon / self.cell)))

    def nearest(self, lat: float, lon: float, max_rings: int = 6) -> Optional[int]:
        """Nearest node, widening the search ring until something is found."""
        klat, klon = self._key(lat, lon)
        for ring in range(max_rings + 1):
            best, best_d = None, INF
            for dla in range(-ring, ring + 1):
                for dlo in range(-ring, ring + 1):
                    if ring and max(abs(dla), abs(dlo)) != ring:
                        continue  # only the new shell
                    for nid in self.buckets.get((klat + dla, klon + dlo), ()):
                        n = self.g.nodes[nid]
                        d = (n.lat - lat) ** 2 + (n.lon - lon) ** 2
                        if d < best_d:
                            best, best_d = nid, d
            if best is not None:
                return best
        return None


def load_pois(db_path: str) -> List[Tuple[float, float, str]]:
    """`(lat, lon, name)` for every POI in the database.

    POIs are the stand-in for "somewhere a boater wants to go". They are not the
    whole story -- anchorages and creeks are not in this table -- so a stretch of
    graph being off every POI-to-POI route makes it a *candidate* for removal,
    never an automatic deletion.
    """
    conn = sqlite3.connect(db_path)
    try:
        return [(r[0], r[1], r[2] or "")
                for r in conn.execute("SELECT lat, lon, name FROM pois")]
    finally:
        conn.close()


def poi_anchor_nodes(g: RoutingGraph, pois: Sequence[Tuple[float, float, str]],
                     index: Optional[NodeIndex] = None) -> Dict[int, str]:
    """Map each POI onto the node it snaps to. Several POIs can share a node."""
    idx = index or NodeIndex(g)
    out: Dict[int, str] = {}
    for lat, lon, name in pois:
        nid = idx.nearest(lat, lon)
        if nid is not None:
            out.setdefault(nid, name)
    return out


def protect_poi_nodes(g: RoutingGraph, db_path: str) -> int:
    """Mark the node each POI snaps to as undeletable, and return how many.

    Without this, simplification quietly destroys snap resolution: a POI sitting
    mid-way along a dead-straight channel is correctly simplified away at both
    ends, and the nearest surviving node ends up kilometres off. Measured on
    build #39, plain Douglas-Peucker at 20 m left "Brewerton Channel Eastern
    Extension" snapping 2,123 m from its charted position -- which is how
    routeiq's `coverage_gap` warning gets born, as a long straight connecting
    leg that is excluded from every constraint check.
    """
    before = len(g.protected)
    idx = NodeIndex(g)
    for lat, lon, _ in load_pois(db_path):
        nid = idx.nearest(lat, lon)
        if nid is not None:
            g.protected.add(nid)
    return len(g.protected) - before


def route_network(g: RoutingGraph, anchors: Iterable[int]
                  ) -> Tuple[Set[int], Set[Tuple[int, int]], int]:
    """Union of shortest paths between every pair of anchors that can reach
    each other. Returns `(nodes_used, edges_used, reachable_pairs)`.

    On build #39 this touched 15.5% of nodes and 12.3% of edges from 262
    anchors -- the measurement the cleanup target is set against.
    """
    anchors = sorted(set(anchors))
    used_n: Set[int] = set()
    used_e: Set[Tuple[int, int]] = set()
    pairs = 0
    aset = set(anchors)
    for s in anchors:
        _, parent = dijkstra(g, s, targets=aset)
        for t in anchors:
            if t <= s:
                continue
            path = path_from_parents(parent, s, t)
            if not path:
                continue
            pairs += 1
            used_n.update(path)
            for a, b in zip(path, path[1:]):
                used_e.add(edge_key(a, b))
    return used_n, used_e, pairs


def reachable_pairs(g: RoutingGraph, anchors: Iterable[int]) -> Set[Tuple[int, int]]:
    """Which anchor pairs can reach each other at all.

    This is the gate that caught builds #11/#12, where 21 named POIs ended up in
    an isolated 8-node island while every headline count still looked healthy.
    """
    anchors = sorted(set(anchors))
    comp_of: Dict[int, int] = {}
    for i, comp in enumerate(components(g)):
        for n in comp:
            comp_of[n] = i
    out: Set[Tuple[int, int]] = set()
    for i, s in enumerate(anchors):
        for t in anchors[i + 1:]:
            if comp_of.get(s, -1) == comp_of.get(t, -2):
                out.add((s, t))
    return out
