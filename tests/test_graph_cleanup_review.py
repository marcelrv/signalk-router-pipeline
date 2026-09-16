"""Unit tests for the AI-review pipeline: candidate generation, tiling,
tile preparation, and turning a backend's answers back into ops
(`docs/SPEC-GRAPH-CLEANUP.md` §6).

Every backend call in these tests goes through `MockBackend`
(`graph_cleanup/backends/mock.py`) -- deterministic and offline, so the harness
is fully tested without an API key. All fixtures are synthetic geometry -- no
real chart data.
"""
import json
import os
import sqlite3

import pytest

from graph_cleanup import candidates as C
from graph_cleanup import prepare, runner, tiles as T
from graph_cleanup.backends.mock import MockBackend
from graph_cleanup.graph import RoutingGraph, NodeRec, EdgeRec, edge_key
from graph_cleanup.ops import DROP_NODE


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


def _graph_with_stub_and_component():
    """A main *ring* (1-2-3-4-5-6-1, no dead ends of its own -- real networks
    don't dead-end at an arbitrary interior point, and an open line would),
    with one genuine dead-end stub off node 3 (3-10-11), plus a small
    disconnected *triangle* (20-21-22, likewise no dead ends of its own) far
    away, so `find_dead_end_stubs` and `find_small_components` each have
    exactly one unambiguous thing to find."""
    g = RoutingGraph()
    ring = [(1, 52.00, 4.00), (2, 52.00, 4.01), (3, 52.00, 4.02),
           (4, 52.00, 4.03), (5, 52.01, 4.02), (6, 52.01, 4.01)]
    for nid, lat, lon in ring:
        _add_node(g, nid, lat, lon)
    ids = [n for n, _, _ in ring]
    for a, b in zip(ids, ids[1:] + ids[:1]):
        _add_edge(g, a, b)
    _add_node(g, 10, 52.005, 4.021)
    _add_node(g, 11, 52.010, 4.022)
    _add_edge(g, 3, 10)
    _add_edge(g, 10, 11)
    for nid, lat, lon in [(20, 53.0, 5.0), (21, 53.001, 5.001), (22, 53.002, 5.0)]:
        _add_node(g, nid, lat, lon)
    for a, b in ((20, 21), (21, 22), (22, 20)):
        _add_edge(g, a, b)
    for (u, v) in list(g.edges):
        g.edges[edge_key(u, v)].distance = g.edge_length_m(u, v)
    return g


def _sqlite_for(g, tmp_path, pois=()):
    """A minimal .sqlite the trace/candidate modules can read POIs from."""
    db = tmp_path / "g.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE pois (id INTEGER PRIMARY KEY, name TEXT, type_id INTEGER,
            properties TEXT, lat REAL, lon REAL, region_id INTEGER,
            source_tier INTEGER, source_id INTEGER);
    """)
    conn.executemany("INSERT INTO pois VALUES (?,?,0,'{}',?,?,1,1,2)",
                     [(i, n, la, lo) for i, (n, la, lo) in enumerate(pois, start=1)])
    conn.commit()
    conn.close()
    return str(db)


# --------------------------------------------------------------- candidates

def test_find_dead_end_stubs_finds_the_stub_not_the_main_line():
    g = _graph_with_stub_and_component()
    stubs = C.find_dead_end_stubs(g)
    assert len(stubs) == 1
    assert stubs[0].kind == C.DEAD_END_STUB
    assert stubs[0].anchor == 11
    assert set(stubs[0].nodes) == {11, 10, 3}
    assert stubs[0].facts["n_nodes"] == 3


def test_find_dead_end_stubs_respects_max_length():
    g = _graph_with_stub_and_component()
    long_stub_len = C._stub_length_m(g, [11, 10, 3])
    assert C.find_dead_end_stubs(g, max_length_m=long_stub_len - 1) == []
    assert len(C.find_dead_end_stubs(g, max_length_m=long_stub_len + 1)) == 1


def test_find_dead_end_stubs_reports_nearest_poi(tmp_path):
    g = _graph_with_stub_and_component()
    pois = [(52.010, 4.0225, "Test Marina")]  # (lat, lon, name) -- matches trace.load_pois
    stubs = C.find_dead_end_stubs(g, pois=pois)
    assert stubs[0].facts["nearest_poi"] == "Test Marina"
    assert stubs[0].facts["nearest_poi_m"] < 100


def test_find_small_components_excludes_the_largest():
    g = _graph_with_stub_and_component()
    comps = C.find_small_components(g, max_component_size=10)
    assert len(comps) == 1
    assert comps[0].kind == C.SMALL_COMPONENT
    assert set(comps[0].nodes) == {20, 21, 22}


def test_find_small_components_respects_size_bounds():
    g = _graph_with_stub_and_component()
    assert C.find_small_components(g, max_component_size=2) == []
    assert C.find_small_components(g, min_component_size=4) == []


def test_find_all_combines_both_kinds(tmp_path):
    g = _graph_with_stub_and_component()
    db = _sqlite_for(g, tmp_path)
    cands = C.find_all(g, db_path=db, max_component_size=10)
    kinds = {c.kind for c in cands}
    assert kinds == {C.DEAD_END_STUB, C.SMALL_COMPONENT}


def test_find_all_fixes_the_nearest_poi_blind_spot_with_lateral_marks(tmp_path):
    """`nearest_poi_m` measured against the `pois` table alone is blind to
    named daybeacons/buoys (`docs/SPEC-GRAPH-CLEANUP.md` §7, found twice in
    real Pass B review): a stub can sit right next to a named lateral mark and
    still report a multi-km `nearest_poi_m` because the nearest *POI-table*
    entry is a distant marina. `input_dir` should close that gap."""
    g = _graph_with_stub_and_component()
    # No POIs anywhere near the stub (tip is node 11, 52.010, 4.022) -- the
    # only POI is far away, same shape as the real blind spot.
    db = _sqlite_for(g, tmp_path, pois=[("Distant Marina", 0.0, 0.0)])
    (tmp_path / "lateral_marks_points.geojson").write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"OBJNAM": "Test Creek Daybeacon 4"},
            "geometry": {"type": "Point", "coordinates": [4.0221, 52.0101]},
        }],
    }))

    without_marks = C.find_all(g, db_path=db, max_component_size=10)
    stub_before = next(c for c in without_marks if c.kind == C.DEAD_END_STUB)
    assert stub_before.facts["nearest_poi"] == "Distant Marina"
    assert stub_before.facts["nearest_poi_m"] > 100_000

    with_marks = C.find_all(g, db_path=db, input_dir=str(tmp_path),
                            max_component_size=10)
    stub_after = next(c for c in with_marks if c.kind == C.DEAD_END_STUB)
    assert stub_after.facts["nearest_poi"] == "Test Creek Daybeacon 4"
    assert stub_after.facts["nearest_poi_m"] < 100


# --------------------------------------------------------------------- tiles

def test_build_tiles_buckets_far_apart_candidates_separately():
    g = _graph_with_stub_and_component()
    cands = C.find_dead_end_stubs(g) + C.find_small_components(g, max_component_size=10)
    result = T.build_tiles(g, cands, tile_m=6000.0)
    assert len(result) == 2, "the stub and the component are ~150km apart"
    total = sum(len(t.candidates) for t in result)
    assert total == len(cands)


def test_build_tiles_empty_input():
    g = _graph_with_stub_and_component()
    assert T.build_tiles(g, []) == []


def test_tile_numbered_is_stable_and_one_based():
    g = _graph_with_stub_and_component()
    cands = C.find_dead_end_stubs(g)
    tile = T.Tile(id="t", bbox=(0, 0, 1, 1), candidates=cands)
    numbered = tile.numbered()
    assert set(numbered.keys()) == set(range(1, len(cands) + 1))


def test_build_tiles_splits_a_crowded_cell():
    g = RoutingGraph()
    # Many short stubs hanging off one shared ring, all close together --
    # forces every candidate's anchor into the same nominal grid cell.
    ring_ids = list(range(1000, 1000 + 40))
    for i, nid in enumerate(ring_ids):
        _add_node(g, nid, 52.0, 4.0 + i * 0.0002)
    for a, b in zip(ring_ids, ring_ids[1:] + ring_ids[:1]):
        _add_edge(g, a, b)
    for i, junction in enumerate(ring_ids):
        stub_id = 5000 + i
        _add_node(g, stub_id, 52.0002, 4.0 + i * 0.0002)
        _add_edge(g, junction, stub_id)
    for (u, v) in list(g.edges):
        g.edges[edge_key(u, v)].distance = g.edge_length_m(u, v)
    cands = C.find_dead_end_stubs(g, max_length_m=1e9)
    tiles = T.build_tiles(g, cands, tile_m=6000.0, max_per_tile=10)
    assert all(len(t.candidates) <= 10 for t in tiles)
    assert sum(len(t.candidates) for t in tiles) == len(cands)


def test_build_tiles_bbox_covers_a_stub_extending_past_the_anchor_cell():
    """A candidate is bucketed by its anchor (the free tip) alone, but the
    stub's junction end (the opposite end of the chain) can sit well outside
    that cell -- real candidates in this project run past 1 km. The tile's
    bbox must cover every node in the candidate, not just its cell + padding,
    or the far end/context a reviewer needs can fall outside the image."""
    g = RoutingGraph()
    # Compute real cell size at this latitude rather than guessing offsets in
    # degrees -- the tip sits mid-cell, the junction end a bit over two full
    # cells away, which no amount of padding on the tip's own cell can cover.
    lat_mid = 52.0
    _, lon_step = T._cell_size_deg(lat_mid, tile_m=6000.0)
    base_lon = 10 * lon_step
    tip_lon = base_lon + lon_step / 2.0
    junction_lon = base_lon + 2.5 * lon_step
    mid_lon = (tip_lon + junction_lon) / 2.0
    # Two short branches (4, 5) off node 3 make it a genuine degree-3
    # junction, not just the chain's other dead end.
    _add_node(g, 1, lat_mid, tip_lon)          # tip / anchor
    _add_node(g, 2, lat_mid, mid_lon)
    _add_node(g, 3, lat_mid, junction_lon)     # junction end, 2+ cells from the tip
    _add_node(g, 4, lat_mid + 0.0005, junction_lon + 0.0005)
    _add_node(g, 5, lat_mid + 0.0005, junction_lon - 0.0005)
    _add_edge(g, 1, 2)
    _add_edge(g, 2, 3)
    _add_edge(g, 3, 4)
    _add_edge(g, 3, 5)
    for (u, v) in list(g.edges):
        g.edges[edge_key(u, v)].distance = g.edge_length_m(u, v)

    cands = C.find_dead_end_stubs(g, max_length_m=1e9)
    stub = next(c for c in cands if c.anchor == 1)
    assert stub.nodes[-1] == 3, "fixture sanity check: the junction end is node 3"

    tiles = T.build_tiles(g, [stub], tile_m=6000.0)
    assert len(tiles) == 1
    min_lon, min_lat, max_lon, max_lat = tiles[0].bbox
    junction = g.nodes[3]
    assert min_lon <= junction.lon <= max_lon, \
        "the junction end must not fall outside the tile's bbox"
    assert min_lat <= junction.lat <= max_lat


# ----------------------------------------------------------------- prepare

def _fake_geojson_dir(tmp_path):
    input_dir = tmp_path / "geo"
    input_dir.mkdir()
    fc = {"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {},
        "geometry": {"type": "Polygon", "coordinates": [[
            [3.9, 51.9], [4.2, 51.9], [4.2, 53.2], [3.9, 53.2], [3.9, 51.9]]]}}]}
    (input_dir / "land_polygons.geojson").write_text(json.dumps(fc))
    return str(input_dir)


def test_write_tile_produces_all_four_files(tmp_path):
    g = _graph_with_stub_and_component()
    cands = C.find_dead_end_stubs(g) + C.find_small_components(g, max_component_size=10)
    tile = T.build_tiles(g, cands, tile_m=6000.0)[0]
    out_dir = tmp_path / "tile_out"
    prepared = prepare.write_tile(tile, g, str(out_dir), input_dir=_fake_geojson_dir(tmp_path))
    assert prepared.n_candidates >= 1
    for name in ("chart.png", "candidates.png", "context.json", "prompt.txt",
                "manifest.json"):
        assert (out_dir / name).exists(), f"missing {name}"


def test_write_tile_requires_input_dir(tmp_path):
    g = _graph_with_stub_and_component()
    cands = C.find_dead_end_stubs(g)
    tile = T.Tile(id="t", bbox=(3.9, 51.9, 4.2, 52.2), candidates=cands)
    with pytest.raises(ValueError):
        prepare.write_tile(tile, g, str(tmp_path / "x"), input_dir=None)


def test_context_json_omits_nodes_but_manifest_keeps_them(tmp_path):
    g = _graph_with_stub_and_component()
    cands = C.find_dead_end_stubs(g)
    tile = T.Tile(id="t", bbox=(3.9, 51.9, 4.2, 52.2), candidates=cands)
    out_dir = tmp_path / "tile_out"
    prepare.write_tile(tile, g, str(out_dir), input_dir=_fake_geojson_dir(tmp_path))
    context = json.loads((out_dir / "context.json").read_text())
    assert "nodes" not in context["candidates"][0], \
        "raw node ids are noise to a reviewer and must not be in the prompt payload"
    manifest = json.loads((out_dir / "manifest.json").read_text())
    assert manifest["1"]["nodes"] == cands[0].nodes


# ------------------------------------------------------------------- runner

def _prepared_tile(tmp_path, g, cands, tile_id="t1"):
    tile = T.Tile(id=tile_id, bbox=(3.9, 51.9, 4.2, 53.3), candidates=cands)
    out_dir = tmp_path / tile_id
    prepare.write_tile(tile, g, str(out_dir), input_dir=_fake_geojson_dir(tmp_path))
    return str(out_dir)


def test_run_tile_with_mock_backend_produces_valid_verdicts(tmp_path):
    g = _graph_with_stub_and_component()
    cands = C.find_dead_end_stubs(g) + C.find_small_components(g, max_component_size=10)
    tile_dir = _prepared_tile(tmp_path, g, cands)
    verdicts = runner.run_tile(MockBackend(), tile_dir)
    assert verdicts is not None
    assert set(verdicts.keys()) == {str(n) for n in range(1, len(cands) + 1)}
    for entry in verdicts.values():
        assert entry["verdict"] in runner.VALID_VERDICTS


def test_run_all_writes_answer_json_and_resumes(tmp_path):
    g = _graph_with_stub_and_component()
    cands = C.find_dead_end_stubs(g) + C.find_small_components(g, max_component_size=10)
    tile_dir = _prepared_tile(tmp_path, g, cands)
    stats1 = runner.run_all(MockBackend(), [tile_dir])
    assert stats1.answered == 1
    assert os.path.exists(os.path.join(tile_dir, "answer.json"))

    stats2 = runner.run_all(MockBackend(), [tile_dir])  # resume=True default
    assert stats2.skipped_existing == 1
    assert stats2.answered == 0


def test_validate_rejects_unknown_candidate_numbers():
    context = {"candidates": [{"n": 1, "kind": "dead_end_stub"}]}
    with pytest.raises(ValueError):
        runner._validate('{"1": {"verdict": "keep"}, "99": {"verdict": "drop"}}',
                         context)


def test_validate_rejects_bad_verdict():
    context = {"candidates": [{"n": 1, "kind": "dead_end_stub"}]}
    with pytest.raises(ValueError):
        runner._validate('{"1": {"verdict": "maybe"}}', context)


def test_validate_rejects_a_truncated_answer_missing_candidates():
    """A response answering only some of a tile's candidates must not be
    accepted as this tile's final answer -- otherwise a truncated response
    marks the tile "answered" and resume permanently skips the omitted
    candidates, never actually reviewing them."""
    context = {"candidates": [{"n": 1, "kind": "dead_end_stub"},
                              {"n": 2, "kind": "dead_end_stub"}]}
    with pytest.raises(ValueError, match="omits"):
        runner._validate('{"1": {"verdict": "keep"}}', context)


def test_validate_accepts_an_answer_covering_every_candidate():
    context = {"candidates": [{"n": 1, "kind": "dead_end_stub"},
                              {"n": 2, "kind": "dead_end_stub"}]}
    result = runner._validate(
        '{"1": {"verdict": "keep"}, "2": {"verdict": "drop"}}', context)
    assert set(result.keys()) == {"1", "2"}


def test_answers_to_ops_only_emits_drops(tmp_path):
    g = _graph_with_stub_and_component()
    cands = C.find_dead_end_stubs(g) + C.find_small_components(g, max_component_size=10)
    tile_dir = _prepared_tile(tmp_path, g, cands)
    runner.run_all(MockBackend(stub_keep_max_m=0.0), [tile_dir])  # forces drop on the stub
    result_ops = runner.answers_to_ops([tile_dir], author="ai:mock")
    assert result_ops, "expected at least one drop"
    assert all(op.op == DROP_NODE for op in result_ops)
    assert all(op.author == "ai:mock" for op in result_ops)


def test_answers_to_ops_stub_drop_excludes_the_junction(tmp_path):
    g = _graph_with_stub_and_component()
    # A distant POI so the mock backend's rule ("drop" past stub_keep_max_m)
    # actually has a nearest_poi_m to compare against -- with no POIs the mock
    # answers "unsure" (correctly: it has nothing to judge from), which would
    # never reach the "drop" path this test means to exercise.
    stub = C.find_dead_end_stubs(g, pois=[(0.0, 0.0, "Far Away")])[0]  # [11, 10, 3]
    tile_dir = _prepared_tile(tmp_path, g, [stub], tile_id="stub_only")
    runner.run_all(MockBackend(stub_keep_max_m=0.0), [tile_dir])
    result_ops = runner.answers_to_ops([tile_dir], author="ai:mock")
    dropped = {op.node for op in result_ops}
    assert dropped == {10, 11}
    assert 3 not in dropped, "the junction node must never be dropped by a stub verdict"


def test_answers_to_ops_component_drop_includes_every_node(tmp_path):
    g = _graph_with_stub_and_component()
    comp = C.find_small_components(g, max_component_size=10)[0]  # {20, 21, 22}
    tile_dir = _prepared_tile(tmp_path, g, [comp], tile_id="comp_only")
    runner.run_all(MockBackend(component_keep_min_nodes=100), [tile_dir])  # forces drop
    result_ops = runner.answers_to_ops([tile_dir], author="ai:mock")
    dropped = {op.node for op in result_ops}
    assert dropped == {20, 21, 22}


def test_answers_to_ops_applies_cleanly_to_the_graph(tmp_path):
    from graph_cleanup import ops as ops_mod

    g = _graph_with_stub_and_component()
    comp = C.find_small_components(g, max_component_size=10)[0]
    tile_dir = _prepared_tile(tmp_path, g, [comp], tile_id="comp_only")
    runner.run_all(MockBackend(component_keep_min_nodes=100), [tile_dir])
    result_ops = runner.answers_to_ops([tile_dir], author="ai:mock")
    res = ops_mod.apply(g, result_ops)
    assert res.applied == 3
    assert {20, 21, 22}.isdisjoint(g.nodes)
