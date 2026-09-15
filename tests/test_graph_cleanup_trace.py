"""Unit tests for `graph_cleanup/trace.py`.

trace.py's own docstring says "Every measurement the cleanup work depends on
lives here, in the repo, under test" -- but had no dedicated test file of its
own. Covers two real bugs found by code review and fixed:

1. `largest_component_length_fraction` picked the node-count-largest
   component (`comps[0]`) rather than the one with the greatest total edge
   length, contradicting its own docstring and this repo's explicit rule
   (`docs/SPEC-GRAPH-DENSITY.md` 6.1) that connectivity must be measured by
   length, never node count -- the exact metric `validate.py`'s
   `largest_component_by_length` gate relies on.
2. `NodeIndex.nearest()` returned as soon as the first non-empty search ring
   had any candidate, without checking whether a closer node existed in the
   next ring out -- a classic bucket-search bug that can return the wrong
   node for a query point near a cell boundary. Feeds POI snapping, used by
   `validate.py`'s `poi_pair_reachability`/`poi_snap_drift` gates.

All fixtures are synthetic geometry -- no real chart data.
"""
import pytest

from graph_cleanup.graph import EdgeRec, NodeRec, RoutingGraph, edge_key
from graph_cleanup import trace


def _edge(**kw):
    base = dict(distance=100.0, min_depth=10.0, drval1=5.0, max_air_draft=99.0,
                min_width=200.0, cost_factor=1.0, distance_to_land=500.0,
                edge_type_id=0, traffic_mode=0, crosses_land=0, crosses_obstacle=0,
                edge_kind_id=0, source_tier=1, source_id=2, width_profile=None,
                requires_lock=0, lock_id=None)
    base.update(kw)
    return EdgeRec(**base)


def _add_node(g, nid, lat, lon):
    g.nodes[nid] = NodeRec(id=nid, lat=lat, lon=lon, node_depth=10.0, region_id=1,
                           node_kind_id=0, source_tier=1, source_id=2)
    g.adj.setdefault(nid, set())


def _add_edge(g, a, b, **kw):
    g.edges[edge_key(a, b)] = _edge(**kw)
    g.adj[a].add(b)
    g.adj[b].add(a)


# ---------------------------------------------- largest_component_length_fraction

def test_picks_the_component_with_the_greatest_edge_length_not_node_count():
    g = RoutingGraph()
    # Component A: many nodes (a 30-node ring), but every edge is tiny --
    # would win a node-count comparison.
    ring_ids = list(range(1, 31))
    for i, nid in enumerate(ring_ids):
        _add_node(g, nid, 10.0, i * 0.00001)  # ~1.1 m apart
    for a, b in zip(ring_ids, ring_ids[1:] + ring_ids[:1]):
        _add_edge(g, a, b, distance=1.1)

    # Component B: two nodes, one very long edge -- far more total length,
    # despite losing on node count 2-to-30.
    _add_node(g, 100, 20.0, 0.0)
    _add_node(g, 101, 20.1, 0.0)
    _add_edge(g, 100, 101, distance=100_000.0)

    comps = trace.components(g)
    assert len(comps[0]) == 30, "fixture sanity check: A is the node-count leader"

    total = 30 * 1.1 + 100_000.0
    frac = trace.largest_component_length_fraction(g)
    assert frac == pytest.approx(100_000.0 / total, rel=1e-9)


def test_single_component_is_the_whole_graph():
    g = RoutingGraph()
    _add_node(g, 1, 0.0, 0.0)
    _add_node(g, 2, 0.0, 0.001)
    _add_edge(g, 1, 2, distance=111.0)
    assert trace.largest_component_length_fraction(g) == 1.0


def test_empty_graph_has_zero_fraction():
    assert trace.largest_component_length_fraction(RoutingGraph()) == 0.0


# ------------------------------------------------------------------- NodeIndex

def test_nearest_finds_a_closer_node_across_a_cell_boundary():
    g = RoutingGraph()
    cell = 0.01
    # Node A sits in the query's own cell, but far from the query point
    # within it. Node B sits just across the cell boundary, genuinely closer
    # in real distance -- the bug returned A because it was found in ring 0
    # and the search stopped without ever looking at B's cell.
    query_lat, query_lon = 0.0099, 0.0099  # right at the edge of cell (0, 0)
    _add_node(g, 1, 0.0001, 0.0001)   # "A": same cell, far corner
    _add_node(g, 2, 0.0101, 0.0101)   # "B": next cell, just across the edge
    idx = trace.NodeIndex(g, cell_deg=cell)

    def d2(nid):
        n = g.nodes[nid]
        return (n.lat - query_lat) ** 2 + (n.lon - query_lon) ** 2

    assert d2(2) < d2(1), "fixture sanity check: B is the true nearest node"
    assert idx.nearest(query_lat, query_lon) == 2


def test_nearest_returns_none_when_nothing_within_max_rings():
    g = RoutingGraph()
    _add_node(g, 1, 50.0, 50.0)
    idx = trace.NodeIndex(g, cell_deg=0.01)
    assert idx.nearest(0.0, 0.0, max_rings=2) is None


def test_nearest_returns_the_only_candidate_in_an_empty_neighbourhood():
    g = RoutingGraph()
    _add_node(g, 1, 0.0, 0.0)
    idx = trace.NodeIndex(g, cell_deg=0.01)
    assert idx.nearest(0.0002, 0.0002) == 1
