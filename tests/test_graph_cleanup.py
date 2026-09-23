"""Unit tests for post-build graph cleanup (docs/SPEC-GRAPH-CLEANUP.md).

Covers the three things that can silently corrupt a shipped database:
splicing must never make an edge look safer than what it replaced, nothing in
`RoutingGraph.protected` may be deleted or moved, and the gates in
`graph_cleanup/validate.py` must fail when reachability or POI snapping
regresses -- the failure mode that got past every headline count in builds
#11/#12 (`data/BUILD_LOG.md`).

All fixtures are synthetic geometry -- no real chart data.
"""
import json
import math
import sqlite3

import pytest

from graph_cleanup import RoutingGraph, iter_chains, ops as ops_mod
from graph_cleanup.graph import EdgeRec, NodeRec, edge_key
from graph_cleanup import simplify, trace, validate


# --------------------------------------------------------------------- helpers

def _edge(**kw):
    base = dict(distance=100.0, min_depth=10.0, drval1=5.0, max_air_draft=99.0,
                min_width=200.0, cost_factor=1.0, distance_to_land=500.0,
                edge_type_id=0, traffic_mode=0, crosses_land=0, crosses_obstacle=0,
                edge_kind_id=0, source_tier=1, source_id=2, width_profile=None,
                requires_lock=0, lock_id=None)
    base.update(kw)
    return EdgeRec(**base)


def _line_graph(coords, **edge_kw):
    """Path graph through `coords` ([(lat, lon), ...]), node ids 1..n."""
    g = RoutingGraph()
    for i, (lat, lon) in enumerate(coords, start=1):
        g.nodes[i] = NodeRec(id=i, lat=lat, lon=lon, node_depth=10.0, region_id=1,
                             node_kind_id=0, source_tier=1, source_id=2)
        g.adj[i] = set()
    for a in range(1, len(coords)):
        b = a + 1
        g.edges[edge_key(a, b)] = _edge(**edge_kw)
        g.adj[a].add(b)
        g.adj[b].add(a)
    for (u, v) in list(g.edges):
        g.edges[edge_key(u, v)].distance = g.edge_length_m(u, v)
    return g


def _straight(n=12, step=0.001):
    return [(52.0, 4.0 + i * step) for i in range(n)]


# ----------------------------------------------------------------- graph basics

def test_load_drops_self_loops_and_dedups_directions(tmp_path):
    db = tmp_path / "g.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE nodes (id INTEGER PRIMARY KEY, lat REAL, lon REAL,
            node_depth REAL, region_id INTEGER, node_kind_id INTEGER,
            source_tier INTEGER, source_id INTEGER);
        CREATE TABLE edges (source INTEGER, target INTEGER, distance REAL,
            min_depth REAL, drval1 REAL, max_air_draft REAL, min_width REAL,
            cost_factor REAL, distance_to_land REAL, edge_type_id INTEGER,
            traffic_mode INTEGER, crosses_land INTEGER, crosses_obstacle INTEGER,
            edge_kind_id INTEGER, source_tier INTEGER, source_id INTEGER,
            width_profile TEXT, requires_lock INTEGER, lock_id INTEGER);
        CREATE TABLE navmesh_regions (id INTEGER PRIMARY KEY, boundary_node_ids TEXT);
    """)
    for i in (1, 2):
        conn.execute("INSERT INTO nodes VALUES (?,52.0,4.0,10,1,0,1,2)", (i,))
    row = (100.0, 10.0, 5.0, 99.0, 200.0, 1.0, 500.0, 0, 0, 0, 0, 0, 1, 2, None, 0, None)
    conn.execute("INSERT INTO edges VALUES (1,2," + ",".join("?" * 17) + ")", row)
    conn.execute("INSERT INTO edges VALUES (2,1," + ",".join("?" * 17) + ")", row)
    conn.execute("INSERT INTO edges VALUES (1,1," + ",".join("?" * 17) + ")", row)
    conn.execute("INSERT INTO navmesh_regions VALUES (1, ?)", (json.dumps([2]),))
    conn.commit()
    conn.close()

    g = RoutingGraph.load(str(db))
    assert len(g.edges) == 1, "both directions of one edge must collapse to one record"
    assert g.dropped_self_loops == 1
    assert g.protected == {2}, "navmesh seam nodes must load as protected"


def test_save_round_trips_both_directions(tmp_path):
    db = tmp_path / "g.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE nodes (id INTEGER PRIMARY KEY, lat REAL, lon REAL,
            node_depth REAL, region_id INTEGER, node_kind_id INTEGER,
            source_tier INTEGER, source_id INTEGER);
        CREATE TABLE edges (source INTEGER, target INTEGER, distance REAL,
            min_depth REAL, drval1 REAL, max_air_draft REAL, min_width REAL,
            cost_factor REAL, distance_to_land REAL, edge_type_id INTEGER,
            traffic_mode INTEGER, crosses_land INTEGER, crosses_obstacle INTEGER,
            edge_kind_id INTEGER, source_tier INTEGER, source_id INTEGER,
            width_profile TEXT, requires_lock INTEGER, lock_id INTEGER);
    """)
    conn.executemany("INSERT INTO nodes VALUES (?,?,?,10,1,0,1,2)",
                     [(1, 52.0, 4.0), (2, 52.0, 4.001), (3, 52.0, 4.002)])
    row = (100.0, 10.0, 5.0, 99.0, 200.0, 1.0, 500.0, 0, 0, 0, 0, 0, 1, 2, None, 0, None)
    for a, b in ((1, 2), (2, 1), (2, 3), (3, 2)):
        conn.execute(f"INSERT INTO edges VALUES ({a},{b}," + ",".join("?" * 17) + ")", row)
    conn.commit()
    conn.close()

    g = RoutingGraph.load(str(db))
    out = tmp_path / "out.sqlite"
    g.save(str(out))
    conn = sqlite3.connect(out)
    assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 3
    conn.close()


# ---------------------------------------------------------------------- splice

def test_splice_inherits_worst_attributes():
    g = _line_graph(_straight(3))
    g.edges[edge_key(1, 2)] = _edge(min_depth=12.0, cost_factor=1.0, source_tier=1,
                                    min_width=300.0, crosses_land=0)
    g.edges[edge_key(2, 3)] = _edge(min_depth=3.5, cost_factor=1.8, source_tier=4,
                                    min_width=60.0, crosses_land=1)
    e1_weight = g.edge(1, 2).weight
    e2_weight = g.edge(2, 3).weight
    assert g.splice_out(2)
    merged = g.edge(1, 3)
    assert merged.min_depth == 3.5, "shallowest depth must survive"
    assert merged.cost_factor >= 1.8, "most expensive cost factor must survive as a floor"
    assert merged.source_tier == 4, "least trustworthy tier must survive"
    assert merged.min_width == 60.0
    assert merged.crosses_land == 1, "a land crossing must not be smoothed away"
    assert merged.weight >= e1_weight + e2_weight - 1e-6, \
        "a spliced chain must never look cheaper than the two edges it replaces"


def test_splice_on_a_curved_chain_never_produces_a_cheaper_shortcut():
    """The straight chord a-b is shorter than the path a-node-b it replaces
    whenever the spliced-out node sits off the direct line -- exactly the
    common case for this project's own wobbly coastal_water chains. Taking
    only the higher of the two cost factors is not enough on its own to keep
    splice_out's documented promise that a merge can never look cheaper than
    what it replaces; the cost factor must be scaled up to compensate for the
    corner cut, on top of the existing worst-of-the-two floor."""
    g = RoutingGraph()
    # A right-angle bend: 1 -> 2 -> 3, with node 2 well off the direct 1-3
    # line, so distance(1,3) is meaningfully shorter than distance(1,2) +
    # distance(2,3) (Pythagoras: chord ~1.41x a leg, vs. two legs ~2x).
    g.nodes[1] = NodeRec(id=1, lat=52.000, lon=4.000, node_depth=10.0, region_id=1,
                         node_kind_id=0, source_tier=1, source_id=2)
    g.nodes[2] = NodeRec(id=2, lat=52.000, lon=4.001, node_depth=10.0, region_id=1,
                         node_kind_id=0, source_tier=1, source_id=2)
    g.nodes[3] = NodeRec(id=3, lat=52.001, lon=4.001, node_depth=10.0, region_id=1,
                         node_kind_id=0, source_tier=1, source_id=2)
    g.adj = {1: {2}, 2: {1, 3}, 3: {2}}
    g.edges[edge_key(1, 2)] = _edge(cost_factor=1.0, distance=g.edge_length_m(1, 2))
    g.edges[edge_key(2, 3)] = _edge(cost_factor=1.0, distance=g.edge_length_m(2, 3))
    e1_weight = g.edge(1, 2).weight
    e2_weight = g.edge(2, 3).weight
    straight_chord_m = g.edge_length_m(1, 3)
    assert straight_chord_m < g.edge(1, 2).distance + g.edge(2, 3).distance, \
        "fixture sanity check: the chord must actually be shorter than the path"

    assert g.splice_out(2)
    merged = g.edge(1, 3)
    assert merged.weight >= e1_weight + e2_weight - 1e-6
    # The un-compensated cost factor (max of the two inputs, both 1.0) would
    # have produced a materially cheaper shortcut on this fixture -- confirm
    # the fix actually engaged, not just that the floor happened to be enough.
    assert merged.cost_factor > 1.0


def test_splice_refuses_when_neighbours_already_joined():
    g = _line_graph(_straight(3))
    g.edges[edge_key(1, 3)] = _edge()
    g.adj[1].add(3)
    g.adj[3].add(1)
    assert not g.splice_out(2), "splicing a triangle would silently drop a path"
    assert 2 in g.nodes


def test_splice_recomputes_distance():
    g = _line_graph(_straight(3))
    before = g.edge(1, 2).distance + g.edge(2, 3).distance
    g.splice_out(2)
    assert g.edge(1, 3).distance == pytest.approx(before, rel=1e-3)


# ---------------------------------------------------- protected-node enforcement
#
# `simplify.py`'s own op generation already skips protected nodes, but that is
# not the only way an op reaches these mutation methods: `ops.apply()`'s
# `--replay` path (a real, used path -- e.g. applying an AI-reviewed
# ops.jsonl) calls remove_node/splice_out/move_node directly. The invariant
# must hold at that layer too, or a malformed/hand-edited ops file could
# silently remove or move a navmesh seam or POI-snapped node.

def test_remove_node_refuses_a_protected_node():
    g = _line_graph(_straight(3))
    g.protected = {2}
    assert not g.remove_node(2)
    assert 2 in g.nodes


def test_splice_out_refuses_a_protected_node():
    g = _line_graph(_straight(3))
    g.protected = {2}
    assert not g.splice_out(2)
    assert 2 in g.nodes
    assert g.degree(2) == 2


def test_move_node_refuses_a_protected_node():
    g = _line_graph(_straight(3))
    g.protected = {2}
    original_lat, original_lon = g.nodes[2].lat, g.nodes[2].lon
    assert not g.move_node(2, 52.5, 5.5)
    assert (g.nodes[2].lat, g.nodes[2].lon) == (original_lat, original_lon)


# -------------------------------------------------------------------- simplify

def test_contract_removes_collinear_interior():
    g = _line_graph(_straight(12))
    ops = simplify.contract_chains(g, tolerance_m=20.0, max_spacing_m=0)
    assert len(ops) == 10, "every interior node of a straight chain is redundant"
    assert all(o.op == ops_mod.SPLICE_NODE for o in ops)
    ops_mod.apply(g, ops)
    assert set(g.nodes) == {1, 12}


def test_contract_keeps_a_real_corner():
    coords = [(52.0, 4.0), (52.0, 4.001), (52.0, 4.002), (52.002, 4.002), (52.004, 4.002)]
    g = _line_graph(coords)
    ops = simplify.contract_chains(g, tolerance_m=5.0, max_spacing_m=0)
    kept = set(range(1, 6)) - {o.node for o in ops}
    assert 3 in kept, "the corner vertex carries the shape and must survive"


def test_contract_never_touches_protected_nodes():
    g = _line_graph(_straight(12))
    g.protected = {5, 6}
    ops = simplify.contract_chains(g, tolerance_m=20.0, max_spacing_m=0)
    assert {5, 6}.isdisjoint({o.node for o in ops})


def test_max_spacing_restores_snap_resolution():
    # ~7.7 km of dead-straight chain: shape needs nothing, snapping needs density.
    g = _line_graph([(52.0, 4.0 + i * 0.001) for i in range(120)])
    loose = simplify.contract_chains(g, tolerance_m=20.0, max_spacing_m=0)
    dense = simplify.contract_chains(g, tolerance_m=20.0, max_spacing_m=500.0)
    assert len(loose) == 118
    assert len(dense) < len(loose), "the spacing cap must keep intermediate nodes"
    ops_mod.apply(g, dense)
    for (u, v) in g.edges:
        assert g.edge_length_m(u, v) <= 600.0, "no gap may exceed the cap by much"


def test_tolerance_is_capped_by_charted_width():
    narrow = _line_graph(_straight(6), min_width=40.0)      # half-width 20 m
    wide = _line_graph(_straight(6), min_width=2000.0)
    chain = next(iter_chains(narrow))
    tol_n = simplify._effective_tolerance(narrow, chain, 20.0, 0.25)
    tol_w = simplify._effective_tolerance(wide, next(iter_chains(wide)), 20.0, 0.25)
    assert tol_n == pytest.approx(5.0), "a 40 m channel caps the tolerance at 5 m"
    assert tol_w == 20.0, "a wide channel takes the requested tolerance"


def test_tolerance_cap_has_no_one_metre_floor_on_a_very_narrow_channel():
    """A charted half-width * width_fraction below 1 m must stand on its own --
    a floor here would let the tolerance (and the smoothing budget, same
    pattern) exceed the documented fraction of the charted width for exactly
    the narrowest, most safety-sensitive channels."""
    very_narrow = _line_graph(_straight(6), min_width=2.0)  # half-width 1 m
    chain = next(iter_chains(very_narrow))
    tol = simplify._effective_tolerance(very_narrow, chain, 20.0, 0.25)
    assert tol == pytest.approx(0.25), "no floor: 1m half-width * 0.25 fraction"


def test_unknown_width_sentinel_is_not_treated_as_a_wide_channel():
    g = _line_graph(_straight(6), min_width=simplify.WIDTH_UNKNOWN)
    assert simplify._chain_half_width(g, next(iter_chains(g))) is None


def test_smoothing_needs_a_charted_width():
    zig = [(52.0, 4.0), (52.0005, 4.001), (52.0, 4.002), (52.0005, 4.003),
           (52.0, 4.004), (52.0005, 4.005), (52.0, 4.006)]
    unknown = _line_graph(zig, min_width=simplify.WIDTH_UNKNOWN)
    known = _line_graph(zig, min_width=400.0)
    assert simplify.smooth_chains(unknown) == [], \
        "without a width there is no proof the smoothed line stays in water"
    assert simplify.smooth_chains(known), "a charted width lets smoothing proceed"


def test_smoothing_respects_the_width_budget():
    zig = [(52.0, 4.0), (52.002, 4.001), (52.0, 4.002), (52.002, 4.003),
           (52.0, 4.004), (52.002, 4.005), (52.0, 4.006)]
    g = _line_graph(zig, min_width=100.0)      # half-width 50 m, budget 25 m
    for op in simplify.smooth_chains(g, width_fraction=0.5):
        before = g.nodes[op.node]
        moved = math.dist(
            simplify._local_xy(before.lat, before.lon, 52.0),
            simplify._local_xy(op.lat, op.lon, 52.0))
        assert moved <= 25.0 + 1e-6, "displacement must stay inside the channel"


def test_smoothing_skips_protected_nodes():
    zig = [(52.0, 4.0), (52.002, 4.001), (52.0, 4.002), (52.002, 4.003),
           (52.0, 4.004), (52.002, 4.005), (52.0, 4.006)]
    g = _line_graph(zig, min_width=400.0)
    g.protected = {3, 4}
    assert {3, 4}.isdisjoint({o.node for o in simplify.smooth_chains(g)})


def test_redundant_edge_removal_keeps_the_only_link():
    g = _line_graph(_straight(4))
    ops = simplify.drop_redundant_edges(g)
    assert ops == [], "a path graph has no redundant edge to remove"
    assert len(g.edges) == 3


def test_redundant_edge_removal_drops_a_parallel_chord():
    g = _line_graph(_straight(4))
    g.edges[edge_key(1, 4)] = _edge(distance=g.edge_length_m(1, 4), cost_factor=1.0)
    g.adj[1].add(4)
    g.adj[4].add(1)
    ops = simplify.drop_redundant_edges(g, slack=1.5)
    assert any({o.u, o.v} == {1, 4} for o in ops), \
        "a chord the long way round already covers is redundant"


def test_redundant_edge_removal_spares_locks():
    g = _line_graph(_straight(4))
    g.edges[edge_key(1, 4)] = _edge(distance=g.edge_length_m(1, 4), requires_lock=1)
    g.adj[1].add(4)
    g.adj[4].add(1)
    assert simplify.drop_redundant_edges(g, slack=1.5) == []


# ------------------------------------------------------------------------- ops

def test_ops_round_trip(tmp_path):
    path = tmp_path / "ops.jsonl"
    given = [ops_mod.Op(op=ops_mod.SPLICE_NODE, node=7, reason="r", author="det:dp20"),
             ops_mod.Op(op=ops_mod.DROP_EDGE, u=1, v=2, reason="r", author="ai:x",
                        confidence=0.6)]
    ops_mod.write_ops(str(path), given, append=False)
    back = list(ops_mod.read_ops(str(path)))
    assert [o.to_json() for o in back] == [o.to_json() for o in given]


def test_op_validates_its_own_arguments():
    with pytest.raises(ValueError):
        ops_mod.Op(op="explode", reason="r", author="a")
    with pytest.raises(ValueError):
        ops_mod.Op(op=ops_mod.DROP_EDGE, u=1, reason="r", author="a")
    with pytest.raises(ValueError):
        ops_mod.Op(op=ops_mod.MOVE_NODE, node=1, lat=52.0, reason="r", author="a")


def test_replaying_ops_twice_is_a_no_op():
    g = _line_graph(_straight(6))
    ops = simplify.contract_chains(g, tolerance_m=20.0, max_spacing_m=0)
    first = ops_mod.apply(g, ops)
    second = ops_mod.apply(g, ops)
    assert first.applied == 4
    assert second.applied == 0, "a replayed file must not corrupt an applied graph"
    assert second.skipped == 4


def test_apply_filters_by_author_and_confidence():
    g = _line_graph(_straight(6))
    ops = [ops_mod.Op(op=ops_mod.SPLICE_NODE, node=2, reason="r", author="det:dp20"),
           ops_mod.Op(op=ops_mod.SPLICE_NODE, node=3, reason="r", author="ai:m",
                      confidence=0.3)]
    res = ops_mod.apply(g, ops, min_confidence=0.5, authors=["det:"])
    assert res.applied == 1
    assert res.skipped_reasons == {"below_min_confidence": 1}


def test_provenance_rows_require_a_reviewer():
    rows = ops_mod.provenance_rows(
        [ops_mod.Op(op=ops_mod.DROP_EDGE, u=1, v=2, reason="no boat goes here",
                    author="ai:claude-sonnet-5", tile="tile_0042")], reviewer="marcel")
    assert rows[0]["entity_type"] == "edge"
    assert rows[0]["entity_ref"] == "1:2"
    assert rows[0]["reviewer"] == "marcel"
    assert rows[0]["contributor"] == "ai:claude-sonnet-5"


# ----------------------------------------------------------------------- gates

def _db_with_pois(tmp_path, coords, pois):
    db = tmp_path / "g.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE nodes (id INTEGER PRIMARY KEY, lat REAL, lon REAL,
            node_depth REAL, region_id INTEGER, node_kind_id INTEGER,
            source_tier INTEGER, source_id INTEGER);
        CREATE TABLE edges (source INTEGER, target INTEGER, distance REAL,
            min_depth REAL, drval1 REAL, max_air_draft REAL, min_width REAL,
            cost_factor REAL, distance_to_land REAL, edge_type_id INTEGER,
            traffic_mode INTEGER, crosses_land INTEGER, crosses_obstacle INTEGER,
            edge_kind_id INTEGER, source_tier INTEGER, source_id INTEGER,
            width_profile TEXT, requires_lock INTEGER, lock_id INTEGER);
        CREATE TABLE pois (id INTEGER PRIMARY KEY, name TEXT, type_id INTEGER,
            properties TEXT, lat REAL, lon REAL, region_id INTEGER,
            source_tier INTEGER, source_id INTEGER);
    """)
    conn.executemany("INSERT INTO nodes VALUES (?,?,?,10,1,0,1,2)",
                     [(i, la, lo) for i, (la, lo) in enumerate(coords, start=1)])
    row = (100.0, 10.0, 5.0, 99.0, 200.0, 1.0, 500.0, 0, 0, 0, 0, 0, 1, 2, None, 0, None)
    for a in range(1, len(coords)):
        for s, t in ((a, a + 1), (a + 1, a)):
            conn.execute(f"INSERT INTO edges VALUES ({s},{t}," + ",".join("?" * 17) + ")", row)
    conn.executemany("INSERT INTO pois VALUES (?,?,0,'{}',?,?,1,1,2)",
                     [(i, n, la, lo) for i, (n, la, lo) in enumerate(pois, start=1)])
    conn.commit()
    conn.close()
    return str(db)


def test_gates_pass_on_a_clean_simplification(tmp_path):
    coords = _straight(12)
    db = _db_with_pois(tmp_path, coords, [("start", 52.0, 4.0), ("end", 52.0, 4.011)])
    g = RoutingGraph.load(db)
    base = validate.Baseline.measure(g, db)
    ops_mod.apply(g, simplify.contract_chains(g, tolerance_m=20.0, max_spacing_m=0))
    report = validate.check(g, base)
    assert report.passed, str(report)


def _add_edge(g, a, b):
    """Add one extra undirected edge between two existing nodes (graph is
    undirected in memory: one EdgeRec per edge_key)."""
    from dataclasses import replace
    tmpl = next(iter(g.edges.values()))
    g.edges[edge_key(a, b)] = replace(tmpl)
    g.adj[a].add(b)
    g.adj[b].add(a)


def _counts_gate(report):
    return next(gate for gate in report.gates if gate.name == "counts")


def test_counts_gate_default_fails_on_edge_growth(tmp_path):
    coords = _straight(12)
    db = _db_with_pois(tmp_path, coords, [("start", 52.0, 4.0), ("end", 52.0, 4.011)])
    g = RoutingGraph.load(db)
    base = validate.Baseline.measure(g, db)
    _add_edge(g, 1, 12)
    assert len(g.edges) == base.edges + 1
    assert not _counts_gate(validate.check(g, base)).passed


def test_counts_gate_opt_in_passes_within_bound_and_fails_beyond(tmp_path):
    coords = _straight(12)
    db = _db_with_pois(tmp_path, coords, [("start", 52.0, 4.0), ("end", 52.0, 4.011)])
    g = RoutingGraph.load(db)
    base = validate.Baseline.measure(g, db)
    _add_edge(g, 1, 12)
    _add_edge(g, 2, 11)                            # +2 edges
    assert _counts_gate(validate.check(g, base, max_edge_growth=2)).passed
    assert _counts_gate(validate.check(g, base, max_edge_growth=5)).passed
    assert not _counts_gate(validate.check(g, base, max_edge_growth=1)).passed


def test_counts_gate_opt_in_never_tolerates_node_growth(tmp_path):
    coords = _straight(12)
    db = _db_with_pois(tmp_path, coords, [("start", 52.0, 4.0), ("end", 52.0, 4.011)])
    g = RoutingGraph.load(db)
    base = validate.Baseline.measure(g, db)
    from dataclasses import replace
    g.nodes[13] = replace(g.nodes[12], id=13)
    g.adj[13] = set()
    assert not _counts_gate(validate.check(g, base, max_edge_growth=100)).passed


def test_counts_gate_shrink_passes_with_and_without_opt_in(tmp_path):
    coords = _straight(12)
    db = _db_with_pois(tmp_path, coords, [("start", 52.0, 4.0), ("end", 52.0, 4.011)])
    g = RoutingGraph.load(db)
    base = validate.Baseline.measure(g, db)
    ops_mod.apply(g, simplify.contract_chains(g, tolerance_m=20.0, max_spacing_m=0))
    assert len(g.edges) < base.edges
    assert _counts_gate(validate.check(g, base)).passed
    assert _counts_gate(validate.check(g, base, max_edge_growth=10)).passed


def test_negative_edge_growth_bound_is_rejected(tmp_path):
    coords = _straight(12)
    db = _db_with_pois(tmp_path, coords, [("start", 52.0, 4.0), ("end", 52.0, 4.011)])
    g = RoutingGraph.load(db)
    base = validate.Baseline.measure(g, db)
    with pytest.raises(ValueError):
        validate.check(g, base, max_edge_growth=-1)


def test_apply_cleanup_cli_rejects_negative_edge_growth_early(tmp_path, capsys):
    """A negative --max-edge-growth is a usage error (argparse exit 2) raised
    before the DB is even loaded, not a ValueError traceback after the cleanup ran."""
    import apply_cleanup
    with pytest.raises(SystemExit) as exc:
        apply_cleanup.main(["--db", str(tmp_path / "does_not_exist.sqlite"),
                            "--ops", str(tmp_path / "ops.jsonl"), "--dry-run",
                            "--max-edge-growth", "-1"])
    assert exc.value.code == 2
    assert "--max-edge-growth must be >= 0" in capsys.readouterr().err


def test_reachability_gate_catches_a_severed_graph(tmp_path):
    coords = _straight(12)
    db = _db_with_pois(tmp_path, coords, [("start", 52.0, 4.0), ("end", 52.0, 4.011)])
    g = RoutingGraph.load(db)
    base = validate.Baseline.measure(g, db)
    g.remove_node(6)          # cut the line in half, as builds #11/#12 did
    report = validate.check(g, base)
    assert not report.passed
    assert any(gate.name == "poi_pair_reachability" for gate in report.failures())


def test_snap_drift_gate_catches_lost_resolution(tmp_path):
    # A POI in the middle of a long straight run, which simplification alone
    # would strand kilometres from the nearest surviving node.
    coords = [(52.0, 4.0 + i * 0.001) for i in range(60)]
    db = _db_with_pois(tmp_path, coords, [("start", 52.0, 4.0),
                                          ("middle", 52.0, 4.030),
                                          ("end", 52.0, 4.059)])
    g = RoutingGraph.load(db)
    base = validate.Baseline.measure(g, db)
    ops_mod.apply(g, simplify.contract_chains(g, tolerance_m=20.0, max_spacing_m=0))
    report = validate.check(g, base)
    assert not report.passed
    assert any(gate.name == "poi_snap_drift" for gate in report.failures())


def test_poi_protection_prevents_the_drift(tmp_path):
    coords = [(52.0, 4.0 + i * 0.001) for i in range(60)]
    db = _db_with_pois(tmp_path, coords, [("start", 52.0, 4.0),
                                          ("middle", 52.0, 4.030),
                                          ("end", 52.0, 4.059)])
    g = RoutingGraph.load(db)
    base = validate.Baseline.measure(g, db)
    assert trace.protect_poi_nodes(g, db) == 3
    ops_mod.apply(g, simplify.contract_chains(g, tolerance_m=20.0, max_spacing_m=0))
    assert validate.check(g, base).passed


def test_connectivity_is_measured_by_length_not_node_count():
    # Two components: a long line and a short stub. Removing interior nodes of
    # the long line must not move the length fraction, though it moves any
    # node-count ratio a lot -- SPEC-GRAPH-DENSITY.md 6.1.
    g = _line_graph(_straight(20))
    for i, (lat, lon) in enumerate([(53.0, 5.0), (53.0, 5.0005)], start=100):
        g.nodes[i] = NodeRec(id=i, lat=lat, lon=lon, node_depth=10.0, region_id=1,
                             node_kind_id=0, source_tier=1, source_id=2)
        g.adj[i] = set()
    g.edges[edge_key(100, 101)] = _edge(distance=g.edge_length_m(100, 101))
    g.adj[100].add(101)
    g.adj[101].add(100)
    before = trace.largest_component_length_fraction(g)
    ops_mod.apply(g, simplify.contract_chains(g, tolerance_m=20.0, max_spacing_m=0))
    assert trace.largest_component_length_fraction(g) == pytest.approx(before, abs=1e-6)


# ----------------------------------------------------------------------- trace

def test_shortest_path_and_components():
    g = _line_graph(_straight(5))
    path, cost = trace.shortest_path(g, 1, 5)
    assert path == [1, 2, 3, 4, 5]
    assert cost == pytest.approx(sum(g.edge(a, b).weight for a, b in zip(path, path[1:])))
    g.remove_node(3)
    assert trace.shortest_path(g, 1, 5)[0] == []
    assert len(trace.components(g)) == 2


def test_cost_factor_is_part_of_the_weight():
    g = _line_graph(_straight(3))
    g.edges[edge_key(1, 2)].cost_factor = 2.0
    assert g.edge(1, 2).weight == pytest.approx(g.edge(1, 2).distance * 2.0)
