"""End-to-end test of `review_region.py`'s CLI, against a tiny synthetic
database and the offline mock backend -- proves the whole prepare/answer/ops
pipeline wires together correctly, without touching real chart data or a
network."""
import json
import os
import sqlite3

import pytest

import review_region


def _make_db(path):
    conn = sqlite3.connect(path)
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
        CREATE TABLE navmesh_regions (id INTEGER PRIMARY KEY,
            boundary_geometry TEXT, vertices TEXT, triangles TEXT,
            triangle_adjacency TEXT, boundary_node_ids TEXT);
    """)
    # A ring (no dead ends of its own) plus one real dead-end stub off it.
    ring = [(1, 52.00, 4.00), (2, 52.00, 4.01), (3, 52.00, 4.02),
           (4, 52.00, 4.03), (5, 52.01, 4.02), (6, 52.01, 4.01)]
    conn.executemany("INSERT INTO nodes VALUES (?,?,?,10,1,0,1,2)", ring)
    conn.executemany("INSERT INTO nodes VALUES (?,?,?,10,1,0,1,2)",
                     [(10, 52.005, 4.021), (11, 52.010, 4.022)])
    row = (100.0, 10.0, 5.0, 99.0, 200.0, 1.0, 500.0, 0, 0, 0, 0, 0, 1, 2, None, 0, None)
    ids = [n for n, _, _ in ring]
    pairs = list(zip(ids, ids[1:] + ids[:1])) + [(3, 10), (10, 11)]
    for a, b in pairs:
        for s, t in ((a, b), (b, a)):
            conn.execute(f"INSERT INTO edges VALUES ({s},{t}," + ",".join("?" * 17) + ")", row)
    conn.execute("INSERT INTO pois VALUES (1,'Ring Marina',0,'{}',52.0,4.02,1,1,2)")
    conn.commit()
    conn.close()


def _make_geojson(input_dir):
    input_dir.mkdir()
    fc = {"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {},
        "geometry": {"type": "Polygon", "coordinates": [[
            [3.9, 51.9], [4.2, 51.9], [4.2, 52.2], [3.9, 52.2], [3.9, 51.9]]]}}]}
    (input_dir / "land_polygons.geojson").write_text(json.dumps(fc))


def test_prepare_only_writes_tiles_and_stops(tmp_path):
    db = tmp_path / "g.sqlite"
    _make_db(db)
    input_dir = tmp_path / "geo"
    _make_geojson(input_dir)
    out_dir = tmp_path / "tiles"

    rc = review_region.main(["--db", str(db), "--input-dir", str(input_dir),
                             "--out-dir", str(out_dir), "--prepare-only"])
    assert rc == 0
    tile_dirs = review_region._find_tile_dirs(str(out_dir))
    assert len(tile_dirs) == 1
    assert not os.path.exists(os.path.join(tile_dirs[0], "answer.json"))


def test_mock_backend_end_to_end_writes_ops(tmp_path):
    db = tmp_path / "g.sqlite"
    _make_db(db)
    input_dir = tmp_path / "geo"
    _make_geojson(input_dir)
    out_dir = tmp_path / "tiles"
    ops_out = tmp_path / "ai.ops.jsonl"

    rc = review_region.main(["--db", str(db), "--input-dir", str(input_dir),
                             "--out-dir", str(out_dir), "--backend", "mock",
                             "--ops-out", str(ops_out)])
    assert rc == 0
    tile_dirs = review_region._find_tile_dirs(str(out_dir))
    assert all(os.path.exists(os.path.join(d, "answer.json")) for d in tile_dirs)
    assert ops_out.exists()


def test_reusing_out_dir_ignores_a_stale_tile_from_an_earlier_run(tmp_path):
    """A stale tile directory left in --out-dir by an earlier, differently-
    scoped run (a different --bbox or --sample-tiles) must not be answered or
    included in --ops-out -- only this run's own tiles. `_find_tile_dirs`
    globs the whole directory and would previously pick up anything with a
    context.json, mixing an out-of-scope answer into a fresh run's output."""
    db = tmp_path / "g.sqlite"
    _make_db(db)
    input_dir = tmp_path / "geo"
    _make_geojson(input_dir)
    out_dir = tmp_path / "tiles"

    # A stale tile directory, as if left over from an earlier run with a
    # different scope: its own context.json/manifest.json/answer.json,
    # naming a node this run's own database doesn't even have -- so if it
    # were picked up, it would show up as an extra drop op.
    stale_dir = out_dir / "t0_stale_9999"
    stale_dir.mkdir(parents=True)
    (stale_dir / "context.json").write_text(json.dumps({
        "tile_id": "t0_stale_9999", "bbox": [0, 0, 1, 1],
        "candidates": [{"n": 1, "kind": "dead_end_stub", "candidate_id": "stub:999999"}],
    }))
    (stale_dir / "manifest.json").write_text(json.dumps({
        # Two nodes: dead_end_stub drops nodes[:-1] (everything but the
        # junction end), so this must have more than one node to produce a
        # real op -- a single-node list would drop nothing regardless.
        "1": {"candidate_id": "stub:999999", "kind": "dead_end_stub",
              "nodes": [999999, 999998]},
    }))
    (stale_dir / "answer.json").write_text(json.dumps({
        "1": {"verdict": "drop", "why": "stale, out-of-scope answer"},
    }))

    ops_out = tmp_path / "ai.ops.jsonl"
    rc = review_region.main(["--db", str(db), "--input-dir", str(input_dir),
                             "--out-dir", str(out_dir), "--backend", "mock",
                             "--ops-out", str(ops_out)])
    assert rc == 0

    from graph_cleanup import ops as ops_mod
    written_ops = list(ops_mod.read_ops(str(ops_out)))
    assert all(op.node != 999999 for op in written_ops), \
        "the stale tile's answer must not leak into this run's ops"

    # The stale directory itself is untouched by this run.
    assert json.loads((stale_dir / "answer.json").read_text())["1"]["why"] == \
        "stale, out-of-scope answer"


def test_bbox_filters_candidates(tmp_path, capsys):
    db = tmp_path / "g.sqlite"
    _make_db(db)
    input_dir = tmp_path / "geo"
    _make_geojson(input_dir)
    out_dir = tmp_path / "tiles"

    rc = review_region.main(["--db", str(db), "--input-dir", str(input_dir),
                             "--out-dir", str(out_dir), "--prepare-only",
                             "--bbox", "170,80,171,81"])  # far from any candidate
    assert rc == 0
    out = capsys.readouterr().out
    assert "restricts to 0/" in out
    assert "nothing to review" in out
