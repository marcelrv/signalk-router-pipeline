"""Unit tests for `graph_cleanup/render.py`.

The important thing this module gets wrong once already, on real data, and
these tests exist to keep it fixed:

* a mixed Polygon/Point GeoJSON layer (`caution_areas_polygons`,
  `obstructions_points` in the real MD data) must never let its Point subset
  fall through to matplotlib's default colour cycle -- that produced
  unstyled, misleading dots over open water in the first render of build #40's
  before/after comparison.
* a navmesh region's boundary ring must render *with* its interior tinted,
  or it reads as an isolated, disconnected artifact -- `edge_kind_id ==
  EDGE_KIND_NAVMESH_BOUNDARY` is only ever the perimeter
  (`nautical_routing_pipeline.build_navmesh_region`); the interior
  triangulation lives in `navmesh_regions.vertices`/`triangles` and is walked
  by the funnel algorithm at query time, never flattened into `edges` rows.

All fixtures are synthetic -- no real chart data. Assertions are necessarily
shallow (file exists, non-trivial size, no exception) since the output is a
raster image; the geometry-correctness assertions belong to
`_load_navmesh_regions`/`_in_bbox`, tested directly below.
"""
import json
import sqlite3

import pytest

from graph_cleanup import render
from graph_cleanup.graph import RoutingGraph, NodeRec, EdgeRec, edge_key


def _edge(**kw):
    base = dict(distance=100.0, min_depth=10.0, drval1=5.0, max_air_draft=99.0,
                min_width=200.0, cost_factor=1.0, distance_to_land=500.0,
                edge_type_id=0, traffic_mode=0, crosses_land=0, crosses_obstacle=0,
                edge_kind_id=0, source_tier=1, source_id=2, width_profile=None,
                requires_lock=0, lock_id=None)
    base.update(kw)
    return EdgeRec(**base)


def _small_graph():
    g = RoutingGraph()
    coords = {1: (52.0, 4.0), 2: (52.001, 4.001), 3: (52.002, 4.002)}
    for i, (lat, lon) in coords.items():
        g.nodes[i] = NodeRec(id=i, lat=lat, lon=lon, node_depth=10.0, region_id=1,
                             node_kind_id=0, source_tier=1, source_id=2)
        g.adj[i] = set()
    for a, b in ((1, 2), (2, 3)):
        g.edges[edge_key(a, b)] = _edge(source_id=15)  # channel_axes styling
        g.adj[a].add(b)
        g.adj[b].add(a)
    return g


def _bbox_around(coords, pad=0.01):
    lats = [c[0] for c in coords]
    lons = [c[1] for c in coords]
    return (min(lons) - pad, min(lats) - pad, max(lons) + pad, max(lats) + pad)


# --------------------------------------------------------------------- basics

def test_render_graph_alone_produces_a_file(tmp_path):
    g = _small_graph()
    out = tmp_path / "tile.png"
    bbox = _bbox_around([(la, lo) for la, lo in
                         [(n.lat, n.lon) for n in g.nodes.values()]])
    render.render_tile(bbox, str(out), graph=g)
    assert out.exists()
    assert out.stat().st_size > 1000, "an empty/near-empty PNG suggests nothing drew"


def test_render_requires_something_to_draw(tmp_path):
    with pytest.raises(ValueError):
        render.render_tile((4.0, 52.0, 4.01, 52.01), str(tmp_path / "x.png"))


def test_numbered_nodes_do_not_crash_when_out_of_bbox(tmp_path):
    g = _small_graph()
    bbox = _bbox_around([(52.0, 4.0)], pad=0.0005)  # excludes nodes 2 and 3
    out = tmp_path / "tile.png"
    render.render_tile(bbox, str(out), graph=g, numbered_nodes={1: 1, 3: 2})
    assert out.exists()


def test_in_bbox():
    bbox = (4.0, 52.0, 4.1, 52.1)
    assert render._in_bbox(52.05, 4.05, bbox)
    assert not render._in_bbox(53.0, 4.05, bbox)
    assert not render._in_bbox(52.05, 5.0, bbox)


def test_segment_intersects_bbox_with_an_endpoint_inside():
    bbox = (4.0, 52.0, 4.1, 52.1)
    assert render._segment_intersects_bbox(52.05, 4.05, 60.0, 10.0, bbox)


def test_segment_intersects_bbox_crossing_straight_through_with_both_ends_outside():
    """A long edge (a simplified/contracted chain, or a removed stub whose far
    end sits well outside a review tile) can span clean across the box with
    neither endpoint inside it -- the old endpoints-only check dropped this
    from the image entirely."""
    bbox = (4.0, 52.0, 4.1, 52.1)
    assert render._segment_intersects_bbox(52.05, 3.9, 52.05, 4.2, bbox)


def test_segment_intersects_bbox_entirely_outside_is_still_excluded():
    bbox = (4.0, 52.0, 4.1, 52.1)
    assert not render._segment_intersects_bbox(60.0, 10.0, 61.0, 11.0, bbox)


def test_segment_intersects_bbox_passing_near_but_missing_the_box():
    """A segment whose bounding box overlaps the tile's but that doesn't
    actually cross into it (passes just outside a corner) must not intersect."""
    bbox = (4.0, 52.0, 4.1, 52.1)
    assert not render._segment_intersects_bbox(51.9, 3.9, 52.0 - 1e-6, 4.0 - 1e-6, bbox)


def test_scale_bar_length_is_a_round_number():
    bbox = (4.0, 52.0, 4.1, 52.1)  # roughly 6.8 km wide at this latitude
    length = render._scale_bar_length_m(bbox)
    assert length in (10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 2500,
                      5000, 10000, 20000)


# --------------------------------------------------------- mixed-geometry fix

def _write_mixed_layer(path, bbox_center=(4.0, 52.0)):
    """A layer with both a Polygon and a Point feature, mirroring
    `caution_areas_polygons`/`obstructions_points` in the real data."""
    lon, lat = bbox_center
    fc = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {},
             "geometry": {"type": "Polygon", "coordinates": [[
                 [lon - 0.01, lat - 0.01], [lon + 0.01, lat - 0.01],
                 [lon + 0.01, lat + 0.01], [lon - 0.01, lat + 0.01],
                 [lon - 0.01, lat - 0.01]]]}},
            {"type": "Feature", "properties": {},
             "geometry": {"type": "Point", "coordinates": [lon, lat]}},
        ],
    }
    path.write_text(json.dumps(fc))


def test_load_layer_caches_the_whole_file_across_calls(tmp_path):
    """Two different bboxes over the same layer must read the file from disk
    only once -- this is the fix for the ~14s/tile render cost measured in
    docs/SPEC-GRAPH-CLEANUP.md 6 (a bbox-filtered read still scans the whole
    file every time; caching the whole GeoDataFrame and slicing in memory
    doesn't)."""
    render.clear_layer_cache()
    layer_path = tmp_path / "land_polygons.geojson"
    _write_mixed_layer(layer_path, bbox_center=(4.0, 52.0))
    a = render._load_layer(str(tmp_path), "land_polygons", (3.9, 51.9, 4.1, 52.1))
    cache_size_after_first = len(render._LAYER_CACHE)
    b = render._load_layer(str(tmp_path), "land_polygons", (3.95, 51.95, 4.05, 52.05))
    assert cache_size_after_first == len(render._LAYER_CACHE) == 1, \
        "a second call with a different bbox must not add another cache entry"
    assert a is not None and b is not None


def test_load_layer_cx_slice_matches_bbox_read(tmp_path):
    """The cached whole-file + `.cx[]` slice must select the same features a
    bbox-filtered `read_file` would (verified against real MD data before
    trusting this in review_region.py; here as a permanent regression guard
    on synthetic data)."""
    render.clear_layer_cache()
    layer_path = tmp_path / "land_polygons.geojson"
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"id": i},
         "geometry": {"type": "Point", "coordinates": [4.0 + i * 0.5, 52.0]}}
        for i in range(6)  # spread across a wide lon range, only some in bbox
    ]}
    layer_path.write_text(json.dumps(fc))
    bbox = (3.9, 51.9, 4.6, 52.1)  # should catch i=0 (4.0) and i=1 (4.5) only
    gdf = render._load_layer(str(tmp_path), "land_polygons", bbox)
    assert sorted(gdf["id"].tolist()) == [0, 1]


def test_load_layer_can_isolate_geometry_type(tmp_path):
    import geopandas as gpd

    layer_path = tmp_path / "caution_areas_polygons.geojson"
    _write_mixed_layer(layer_path)
    bbox = (3.9, 51.9, 4.1, 52.1)

    polys = render._load_layer(str(tmp_path), "caution_areas_polygons", bbox,
                               geom_types=("Polygon", "MultiPolygon"))
    points = render._load_layer(str(tmp_path), "caution_areas_polygons", bbox,
                                geom_types=("Point",))
    assert len(polys) == 1 and polys.geometry.iloc[0].geom_type == "Polygon"
    assert len(points) == 1 and points.geometry.iloc[0].geom_type == "Point"


def test_context_layers_never_draw_the_point_subset(tmp_path):
    """A polygon-styled context layer must not silently include Points --
    that is exactly the bug that produced unstyled dots over open water."""
    input_dir = tmp_path / "geo"
    input_dir.mkdir()
    for name, _, _, _, _ in render.CONTEXT_LAYERS:
        if name == "caution_areas_polygons":
            _write_mixed_layer(input_dir / f"{name}.geojson")
    bbox = (3.9, 51.9, 4.1, 52.1)
    gdf = render._load_layer(str(input_dir), "caution_areas_polygons", bbox,
                             geom_types=("Polygon", "MultiPolygon"))
    assert all(t in ("Polygon", "MultiPolygon") for t in gdf.geometry.geom_type)


def test_render_with_mixed_geometry_context_does_not_crash(tmp_path):
    input_dir = tmp_path / "geo"
    input_dir.mkdir()
    _write_mixed_layer(input_dir / "caution_areas_polygons.geojson")
    _write_mixed_layer(input_dir / "obstructions_points.geojson")
    g = _small_graph()
    bbox = (3.9, 51.9, 4.1, 52.1)
    out = tmp_path / "tile.png"
    render.render_tile(bbox, str(out), graph=g, input_dir=str(input_dir))
    assert out.exists()


# ----------------------------------------------------------- navmesh regions

def _db_with_navmesh_region(path, poly_coords):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE navmesh_regions (id INTEGER PRIMARY KEY,
            boundary_geometry TEXT, vertices TEXT, triangles TEXT,
            triangle_adjacency TEXT, boundary_node_ids TEXT);
    """)
    vertices = [[lat, lon] for lon, lat in poly_coords[:-1]]  # [lat, lon] order
    boundary = {"type": "Polygon", "coordinates": [poly_coords]}
    conn.execute(
        "INSERT INTO navmesh_regions VALUES (1, ?, ?, '[]', '[]', '[]')",
        (json.dumps(boundary), json.dumps(vertices)))
    conn.commit()
    conn.close()


def test_load_navmesh_regions_filters_by_bbox(tmp_path):
    db = tmp_path / "g.sqlite"
    inside = [[4.0, 52.0], [4.01, 52.0], [4.01, 52.01], [4.0, 52.01], [4.0, 52.0]]
    _db_with_navmesh_region(db, inside)

    hit = render._load_navmesh_regions(str(db), (3.9, 51.9, 4.1, 52.1))
    miss = render._load_navmesh_regions(str(db), (10.0, 10.0, 10.1, 10.1))
    assert len(hit) == 1
    assert len(miss) == 0


def test_load_navmesh_regions_uses_lat_lon_vertex_order(tmp_path):
    """`vertices` is stored [lat, lon] -- a bbox test on the wrong axis would
    silently drop or wrongly keep every region."""
    db = tmp_path / "g.sqlite"
    # A region whose lat values (52.x) and lon values (4.x) are clearly
    # distinguishable: only a bbox test using the right axis finds it.
    coords = [[4.0, 52.0], [4.01, 52.0], [4.01, 52.01], [4.0, 52.01], [4.0, 52.0]]
    _db_with_navmesh_region(db, coords)
    found = render._load_navmesh_regions(str(db), (3.95, 51.95, 4.05, 52.05))
    assert len(found) == 1


def test_render_diff_marks_removed_edges_and_nodes(tmp_path):
    """The core promise of render_diff: an edge/node present in `before` but
    gone from `after` must be drawn (as `removed`), and the counts in the
    title/legend must match what was actually removed -- not just "it doesn't
    crash". Two synthetic databases sharing one node in common (so removal is
    unambiguous) and an isolated dropped node with no incident edge."""
    def make_db(path, nodes, edges):
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
            CREATE TABLE navmesh_regions (id INTEGER PRIMARY KEY,
                boundary_geometry TEXT, vertices TEXT, triangles TEXT,
                triangle_adjacency TEXT, boundary_node_ids TEXT);
        """)
        conn.executemany("INSERT INTO nodes VALUES (?,?,?,10,1,0,1,2)", nodes)
        row = (100.0, 10.0, 5.0, 99.0, 200.0, 1.0, 500.0, 0, 0, 0, 0, 0, 1, 2, None, 0, None)
        for a, b in edges:
            for s, t in ((a, b), (b, a)):
                conn.execute(f"INSERT INTO edges VALUES ({s},{t}," + ",".join("?" * 17) + ")", row)
        conn.commit()
        conn.close()

    before_db = tmp_path / "before.sqlite"
    after_db = tmp_path / "after.sqlite"
    # before: a 4-node chain (1-2-3) plus an isolated dead node 4
    make_db(before_db, [(1, 52.0, 4.0), (2, 52.0, 4.001), (3, 52.0, 4.002),
                        (4, 52.01, 4.01)], [(1, 2), (2, 3)])
    # after: only the first edge survives; node 3 and isolated node 4 are gone
    make_db(after_db, [(1, 52.0, 4.0), (2, 52.0, 4.001)], [(1, 2)])

    from graph_cleanup.graph import RoutingGraph, edge_key

    g_before, g_after = RoutingGraph.load(str(before_db)), RoutingGraph.load(str(after_db))
    removed_edges, removed_nodes = render.diff_removed(g_before, g_after)
    assert set(removed_edges) == {edge_key(2, 3)}
    # Node 3 is both an endpoint of the removed edge *and* reported as a removed
    # node -- deliberate, not a double-count bug: on a multi-node dropped stub
    # (the common real case) this puts a dot at every vanished vertex along the
    # dashed line, not just at its far end, which is what made the real Coltons
    # Point render legible. Node 4 has no incident edge at all and must still
    # show up here, or an isolated drop_node would be invisible in the diff.
    assert set(removed_nodes) == {3, 4}
    assert 2 not in removed_nodes, "node 2 survives (kept edge 1-2) and must not appear"

    out = tmp_path / "diff.png"
    bbox = (3.9, 51.9, 4.05, 52.05)
    render.render_diff(bbox, str(out), str(before_db), str(after_db))
    assert out.exists()
    assert out.stat().st_size > 1000


def test_render_diff_draws_the_same_chart_context_as_render_tile(tmp_path, monkeypatch):
    """render_diff used to draw only CONTEXT_LAYERS (polygons), omitting the
    buoys/lights/marks and navmesh-region shading render_tile includes -- a
    reviewer judging a removal from the diff alone was missing exactly the
    hazard/navigation context they need. Both renderers must now go through
    the same `_draw_chart_context` helper, with real navmesh-region context
    (the surviving/`after` graph's own source_db) and the input_dir passed
    through."""
    conn = sqlite3.connect
    def make_db(path):
        c = conn(path)
        c.executescript("""
            CREATE TABLE nodes (id INTEGER PRIMARY KEY, lat REAL, lon REAL,
                node_depth REAL, region_id INTEGER, node_kind_id INTEGER,
                source_tier INTEGER, source_id INTEGER);
            CREATE TABLE edges (source INTEGER, target INTEGER, distance REAL,
                min_depth REAL, drval1 REAL, max_air_draft REAL, min_width REAL,
                cost_factor REAL, distance_to_land REAL, edge_type_id INTEGER,
                traffic_mode INTEGER, crosses_land INTEGER, crosses_obstacle INTEGER,
                edge_kind_id INTEGER, source_tier INTEGER, source_id INTEGER,
                width_profile TEXT, requires_lock INTEGER, lock_id INTEGER);
            CREATE TABLE navmesh_regions (id INTEGER PRIMARY KEY,
                boundary_geometry TEXT, vertices TEXT, triangles TEXT,
                triangle_adjacency TEXT, boundary_node_ids TEXT);
        """)
        c.executemany("INSERT INTO nodes VALUES (?,?,?,10,1,0,1,2)",
                      [(1, 52.0, 4.0), (2, 52.0, 4.001)])
        row = (100.0, 10.0, 5.0, 99.0, 200.0, 1.0, 500.0, 0, 0, 0, 0, 0, 1, 2, None, 0, None)
        for s, t in ((1, 2), (2, 1)):
            c.execute(f"INSERT INTO edges VALUES ({s},{t}," + ",".join("?" * 17) + ")", row)
        c.commit()
        c.close()

    before_db, after_db = tmp_path / "before.sqlite", tmp_path / "after.sqlite"
    make_db(before_db)
    make_db(after_db)

    calls = []
    real = render._draw_chart_context

    def spy(ax, bbox, cfg, legend_handles, input_dir=None, source_db=None):
        calls.append({"input_dir": input_dir, "source_db": source_db})
        return real(ax, bbox, cfg, legend_handles, input_dir=input_dir, source_db=source_db)

    monkeypatch.setattr(render, "_draw_chart_context", spy)

    out = tmp_path / "diff.png"
    bbox = (3.9, 51.9, 4.05, 52.05)
    render.render_diff(bbox, str(out), str(before_db), str(after_db), input_dir="/tmp/nonexistent")

    assert len(calls) == 1
    assert calls[0]["input_dir"] == "/tmp/nonexistent"
    assert calls[0]["source_db"] == str(after_db), \
        "navmesh-region context must come from the surviving (after) graph"


def test_render_with_navmesh_region_does_not_crash(tmp_path):
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
        CREATE TABLE navmesh_regions (id INTEGER PRIMARY KEY,
            boundary_geometry TEXT, vertices TEXT, triangles TEXT,
            triangle_adjacency TEXT, boundary_node_ids TEXT);
    """)
    conn.executemany("INSERT INTO nodes VALUES (?,?,?,10,1,1,1,NULL)",
                     [(1, 52.0, 4.0), (2, 52.01, 4.01)])
    row = (100.0, 10.0, 5.0, 99.0, 200.0, 1.0, 500.0, 0, 0, 0, 0, 1, 1, None, None, 0, None)
    for s, t in ((1, 2), (2, 1)):
        conn.execute(f"INSERT INTO edges VALUES ({s},{t}," + ",".join("?" * 17) + ")", row)
    poly = [[3.99, 51.99], [4.02, 51.99], [4.02, 52.02], [3.99, 52.02], [3.99, 51.99]]
    conn.execute("INSERT INTO navmesh_regions VALUES (1, ?, ?, '[]', '[]', '[]')",
                (json.dumps({"type": "Polygon", "coordinates": [poly]}),
                 json.dumps([[la, lo] for lo, la in poly[:-1]])))
    conn.commit()
    conn.close()

    g = RoutingGraph.load(str(db))
    out = tmp_path / "tile.png"
    render.render_tile((3.95, 51.95, 4.05, 52.05), str(out), graph=g)
    assert out.exists()
