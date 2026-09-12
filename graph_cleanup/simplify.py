"""Pass A of the cleanup: deterministic simplification, no model
(`docs/SPEC-GRAPH-CLEANUP.md`).

Everything here is provable, so it runs first and unattended. What is left over
-- "does this dead end lead anywhere a boater wants to go", "which of these
parallel lines is the real route" -- is judgement, and goes to a model in Pass B.

Measured on build #39 (`data/BUILD_LOG.md`), which is what the tolerances below
are set against:

* 24,830 of 62,904 nodes are degree-2 chain interiors. Douglas-Peucker at 20 m
  removes 26.4% of *all* nodes.
* The over-density and the wobble are different layers and need different
  treatment. `channel_axes` and `inland_waterways` are dead straight -- median
  turn 0.5 deg and 0.2 deg at 76 m and 119 m spacing -- so they are pure
  redundancy that Douglas-Peucker removes exactly. The `coastal_water`
  medial-axis skeleton turns a median of 35.7 deg with 63% of its nodes over
  20 deg at 56 m spacing: those vertices are genuinely off-line, so simplifying
  will not touch them and `smooth_chains` handles them separately.
* 33.7% of edges are on no shortest-path tree from 60 spread origins.

Two invariants hold across every pass here:

1. **Nothing in `RoutingGraph.protected` is deleted or moved** -- those are the
   navmesh seam nodes routeiq enters a navmesh region through.
2. **No point moves further than the pass's tolerance**, and for the skeleton
   that tolerance is bounded by the channel's own charted half-width, so a
   simplified line cannot leave water the original was in the middle of.
"""
import math
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .graph import RoutingGraph, edge_key, iter_chains
from .ops import DROP_EDGE, SPLICE_NODE, Op

# `min_width` uses 999.0 as "unknown" (nautical_routing_pipeline's default).
# On build #39 that is 100% of channel_axes/inland_waterways edges and 24% of
# coastal_water ones, so anything keyed off width has to treat it as no data
# rather than as a 999 m wide channel.
WIDTH_UNKNOWN = 999.0

# Fraction of the local channel half-width a vertex may be displaced by. The
# medial axis is equidistant from both banks, so a point on it has at least
# half_width of clearance; staying inside that fraction of it is what makes
# simplification and smoothing provably stay in water without loading any
# polygons. `min_width` is the *narrowest* width along the whole chain, so every
# node on it is budgeted by its tightest point -- conservative by construction.
#
# Simplification is held tighter than smoothing because its error is a chord
# deviation that compounds along a run of removed vertices, whereas smoothing
# displaces each node once and independently. At 0.5 a smoothed node still keeps
# half its original clearance, and measured on build #39 it takes the median
# turn on reachable skeleton chains from 36.5 deg to 16.4 deg (0.25 only reaches
# 29.2 deg).
SIMPLIFY_WIDTH_FRACTION = 0.25
SMOOTH_WIDTH_FRACTION = 0.5

DEFAULT_WIDTH_FRACTION = SIMPLIFY_WIDTH_FRACTION


def _local_xy(lat: float, lon: float, lat0: float) -> Tuple[float, float]:
    """Equirectangular metres about `lat0`. Good to well under a metre over the
    few-kilometre span of one chain, which is all it is ever used for."""
    return (math.radians(lon) * 6378137.0 * math.cos(math.radians(lat0)),
            math.radians(lat) * 6378137.0)


def _perp_distance(p: Tuple[float, float], a: Tuple[float, float],
                   b: Tuple[float, float]) -> float:
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    seg = math.hypot(dx, dy)
    if seg == 0.0:
        return math.hypot(px - ax, py - ay)
    return abs(dy * px - dx * py + bx * ay - by * ax) / seg


def douglas_peucker_keep(points: Sequence[Tuple[float, float]],
                         tolerance_m: float) -> List[int]:
    """Indices of the points Douglas-Peucker keeps, sorted, endpoints included."""
    n = len(points)
    if n <= 2:
        return list(range(n))
    keep = {0, n - 1}
    stack = [(0, n - 1)]
    while stack:
        lo, hi = stack.pop()
        if hi - lo < 2:
            continue
        worst, worst_i = -1.0, -1
        for i in range(lo + 1, hi):
            d = _perp_distance(points[i], points[lo], points[hi])
            if d > worst:
                worst, worst_i = d, i
        if worst > tolerance_m:
            keep.add(worst_i)
            stack.append((lo, worst_i))
            stack.append((worst_i, hi))
    return sorted(keep)


def _chain_half_width(g: RoutingGraph, chain: Sequence[int]) -> Optional[float]:
    """Narrowest known half-width along a chain, or None if nothing is charted."""
    widths = []
    for a, b in zip(chain, chain[1:]):
        e = g.edge(a, b)
        if e and e.min_width is not None and e.min_width < WIDTH_UNKNOWN:
            widths.append(e.min_width)
    return min(widths) / 2.0 if widths else None


def _effective_tolerance(g: RoutingGraph, chain: Sequence[int], tolerance_m: float,
                         width_fraction: float) -> float:
    """The requested tolerance, capped by the channel's own half-width.

    Where no width is charted the requested tolerance stands on its own -- it is
    already small relative to any navigable channel -- but where the chart says
    the channel is narrow, that wins.
    """
    half = _chain_half_width(g, chain)
    if half is None:
        return tolerance_m
    return min(tolerance_m, max(1.0, half * width_fraction))


def _enforce_max_spacing(pts: Sequence[Tuple[float, float]], keep: List[int],
                         max_spacing_m: float) -> List[int]:
    """Put vertices back until no kept-to-kept gap exceeds `max_spacing_m`.

    Douglas-Peucker is right about shape and wrong about resolution: on a
    dead-straight 4 km channel it correctly removes every interior vertex, and
    a click (or a POI) in the middle then has nothing within 2 km to snap to.
    Routing needs shape; snapping needs density. This restores the minimum
    density, by repeatedly keeping the vertex nearest the middle of each
    too-long gap.
    """
    if max_spacing_m <= 0 or len(keep) < 2:
        return keep
    keepset = set(keep)
    queue = list(zip(keep, keep[1:]))
    while queue:
        lo, hi = queue.pop()
        if hi - lo < 2:
            continue
        span = math.dist(pts[lo], pts[hi])
        if span <= max_spacing_m:
            continue
        mid_x = (pts[lo][0] + pts[hi][0]) / 2.0
        mid_y = (pts[lo][1] + pts[hi][1]) / 2.0
        best_i = min(range(lo + 1, hi),
                     key=lambda i: math.dist(pts[i], (mid_x, mid_y)))
        keepset.add(best_i)
        queue.append((lo, best_i))
        queue.append((best_i, hi))
    return sorted(keepset)


def contract_chains(g: RoutingGraph, tolerance_m: float = 20.0,
                    width_fraction: float = DEFAULT_WIDTH_FRACTION,
                    max_spacing_m: float = 500.0,
                    author: Optional[str] = None) -> List[Op]:
    """Douglas-Peucker every degree-2 chain; emit a `splice_node` per dropped node.

    Splicing rather than dropping is what keeps this safe: the two edges either
    side of a removed node become one edge carrying the *worst* attribute of the
    pair, so a simplified chain can never look deeper, wider, cheaper or more
    trustworthy than the vertices it replaced.

    `max_spacing_m` bounds how sparse a simplified run may get, so a boater can
    still click anywhere on a long straight channel and snap to something near
    by. Set it to 0 to simplify on shape alone.
    """
    author = author or f"det:dp{tolerance_m:g}"
    ops: List[Op] = []
    for chain in iter_chains(g):
        tol = _effective_tolerance(g, chain, tolerance_m, width_fraction)
        lat0 = g.nodes[chain[0]].lat
        pts = [_local_xy(g.nodes[n].lat, g.nodes[n].lon, lat0) for n in chain]
        keep = _enforce_max_spacing(pts, douglas_peucker_keep(pts, tol), max_spacing_m)
        keepset = set(keep)
        for i, node in enumerate(chain):
            if i in keepset or i in (0, len(chain) - 1):
                continue
            if node in g.protected:
                continue
            ops.append(Op(op=SPLICE_NODE, node=node, author=author,
                          confidence=1.0,
                          reason=f"collinear within {tol:.1f}m of the simplified chain"))
    return ops


def smooth_chains(g: RoutingGraph, strength: float = 0.5, passes: int = 2,
                  width_fraction: float = SMOOTH_WIDTH_FRACTION,
                  min_turn_deg: float = 20.0, min_interior: int = 3,
                  author: str = "det:smooth") -> List[Op]:
    """Take the wobble out of medial-axis chains, bounded by the channel width.

    This is the one pass that moves geometry instead of only removing it.

    **Its reach is small and the measurement says so.** 63% of `coastal_water`
    skeleton nodes turn more than 20 deg, but the skeleton is a mesh, not a set
    of lines: 22,095 of the 44,236 nodes touching a skeleton edge are degree-3
    or more, and of its 5,832 degree-2 chains, 3,675 hold a single interior
    node. Only ~6,600 interior nodes sit in a chain long enough
    (`min_interior`) for smoothing toward a neighbour midpoint to mean anything.
    The rest of the wobble is *between junctions*, where no chain pass can reach
    it -- that is Pass B/C's problem, or `--skeleton-boundary-simplify-m`'s at
    generation time.

    Chebyshev-style smoothing toward the midpoint of the neighbours, with each
    node's total displacement clamped to `width_fraction` of the charted local
    half-width. **Chains with no charted width are left alone** -- without a
    width there is no proof the smoothed line stays in water, and a plausible
    guess is exactly what this whole effort is trying to stop doing.
    """
    ops: List[Op] = []
    for chain in iter_chains(g):
        if len(chain) - 2 < min_interior:
            continue
        half = _chain_half_width(g, chain)
        if half is None:
            continue
        budget = max(1.0, half * width_fraction)
        lat0 = g.nodes[chain[0]].lat
        orig = [_local_xy(g.nodes[n].lat, g.nodes[n].lon, lat0) for n in chain]
        if max(_turn_angles(orig), default=0.0) < min_turn_deg:
            continue
        cur = list(orig)
        for _ in range(passes):
            nxt = list(cur)
            for i in range(1, len(cur) - 1):
                mx = (cur[i - 1][0] + cur[i + 1][0]) / 2.0
                my = (cur[i - 1][1] + cur[i + 1][1]) / 2.0
                nxt[i] = (cur[i][0] + (mx - cur[i][0]) * strength,
                          cur[i][1] + (my - cur[i][1]) * strength)
            cur = nxt
        for i in range(1, len(chain) - 1):
            node = chain[i]
            if node in g.protected:
                continue
            dx, dy = cur[i][0] - orig[i][0], cur[i][1] - orig[i][1]
            moved = math.hypot(dx, dy)
            if moved < 1.0:
                continue
            if moved > budget:  # clamp back onto the budget circle
                dx, dy = dx * budget / moved, dy * budget / moved
                moved = budget
            rec = g.nodes[node]
            lat = rec.lat + math.degrees(dy / 6378137.0)
            lon = rec.lon + math.degrees(dx / (6378137.0 * math.cos(math.radians(lat0))))
            ops.append(Op(op="move_node", node=node, lat=round(lat, 5),
                          lon=round(lon, 5), author=author, confidence=1.0,
                          reason=f"smoothed {moved:.1f}m, within {budget:.1f}m of "
                                 f"the charted half-width"))
    return ops


def _turn_angles(points: Sequence[Tuple[float, float]]) -> List[float]:
    out = []
    for i in range(1, len(points) - 1):
        (ax, ay), (bx, by), (cx, cy) = points[i - 1], points[i], points[i + 1]
        v1, v2 = (bx - ax, by - ay), (cx - bx, cy - by)
        n1, n2 = math.hypot(*v1), math.hypot(*v2)
        if n1 == 0 or n2 == 0:
            continue
        cos = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
        out.append(math.degrees(math.acos(cos)))
    return out


def drop_redundant_edges(g: RoutingGraph, slack: float = 1.05,
                         max_detour_m: float = 2000.0,
                         author: str = "det:redundant") -> List[Op]:
    """Remove edges that no route needs.

    An edge is redundant when its endpoints are already joined by another path
    costing no more than `slack` times the edge's own cost -- taking it can never
    save a route more than that margin, so nothing that used it loses anything
    meaningful. Checked against the graph as it is mutated, so a chain of
    removals can never eat the last connection between two places.

    Bridges, locks and any edge whose removal would isolate a node are left
    alone regardless: those carry meaning beyond their geometry.
    """
    ops: List[Op] = []
    # Longest (most expensive) first: those are the chords across a bay that a
    # shorter way round already covers, and removing them early means the cheap
    # local edges are re-tested against a graph that no longer has them.
    ordered = sorted(g.edges.items(), key=lambda kv: -(kv[1].weight))
    for (u, v), e in ordered:
        if edge_key(u, v) not in g.edges:
            continue  # already removed this pass
        if e.requires_lock or e.lock_id is not None:
            continue
        if g.degree(u) <= 1 or g.degree(v) <= 1:
            continue
        limit = e.weight * slack
        if limit > max_detour_m:
            continue
        g.remove_edge(u, v)
        alt = _bounded_cost(g, u, v, limit)
        if alt is None:
            g.edges[edge_key(u, v)] = e  # put it back, it was load-bearing
            g.adj[u].add(v)
            g.adj[v].add(u)
            continue
        ops.append(Op(op=DROP_EDGE, u=u, v=v, author=author, confidence=1.0,
                      reason=f"alternative path costs {alt:.0f} vs {e.weight:.0f}, "
                             f"within {slack:g}x"))
    return ops


def _bounded_cost(g: RoutingGraph, source: int, target: int,
                  limit: float) -> Optional[float]:
    """Cheapest path cost from source to target, or None if it exceeds `limit`.

    Abandons the search as soon as everything reachable costs more than the
    limit, which is what keeps this affordable across 82k edges.
    """
    import heapq

    dist: Dict[int, float] = {source: 0.0}
    pq: List[Tuple[float, int]] = [(0.0, source)]
    while pq:
        d, u = heapq.heappop(pq)
        if d > limit:
            return None
        if u == target:
            return d
        if d > dist.get(u, math.inf):
            continue
        for w in g.adj.get(u, ()):
            e = g.edges.get(edge_key(u, w))
            if e is None:
                continue
            nd = d + e.weight
            if nd <= limit and nd < dist.get(w, math.inf):
                dist[w] = nd
                heapq.heappush(pq, (nd, w))
    return None


def run_all(g: RoutingGraph, tolerance_m: float = 20.0,
            smooth: bool = True, redundant: bool = True) -> Dict[str, List[Op]]:
    """Every deterministic pass, in the order they must run.

    Order matters. Smoothing reads turn angles, so it runs *before* contraction
    removes the vertices that make a chain look wobbly; redundant-edge removal
    runs last, on the already-simplified graph, so it is deciding about the edges
    that will actually ship.

    Each pass is emitted against a throwaway copy of the graph state the previous
    passes produced, so the returned ops replay in this same order onto a clean
    database.
    """
    from . import ops as ops_mod

    out: Dict[str, List[Op]] = {}
    if smooth:
        out["smooth"] = smooth_chains(g)
        ops_mod.apply(g, out["smooth"])
    out["contract"] = contract_chains(g, tolerance_m=tolerance_m)
    ops_mod.apply(g, out["contract"])
    if redundant:
        out["redundant"] = drop_redundant_edges(g)
    return out


def flatten(passes: Dict[str, List[Op]]) -> List[Op]:
    """Ops from `run_all` in replay order."""
    order = ("smooth", "contract", "redundant")
    return [op for name in order for op in passes.get(name, [])]
