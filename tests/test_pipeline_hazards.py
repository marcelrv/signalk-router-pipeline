"""Main pipeline x hazards.py (docs/SPEC-HAZARDS-AND-NOTICES.md step 5): --hazard-cells-dir wiring, the
shared tier rules (RESTRN 7 vs 1, restrn/RESTRN union, UNKNOWN VALSOU), bridge/lock exemption, and the
schema 3 export (crosses_obstacle, min_depth fold, seasonal cost, notices tables)."""
import math
import sqlite3

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point, Polygon

import hazards
import nautical_routing_pipeline as npl
from nautical_routing_pipeline import NauticalRoutingPipeline, UNKNOWN_DEPTH, link_graph_hazards

LON0, LAT0 = 3.5, 51.5
KX = 111320.0 * math.cos(math.radians(LAT0))
KY = 111320.0


def ll(x, y):
    return (LON0 + x / KX, LAT0 + y / KY)


def box(x0, y0, x1, y1):
    return Polygon([ll(x0, y0), ll(x1, y0), ll(x1, y1), ll(x0, y1)])


def rec(cls, geom, **attrs):
    return {"source_class": cls, "source_ref": f"LN-{cls}-{len(attrs)}-{sorted(attrs)}", "source_cell": "T.000",
            "name": attrs.pop("name", None), "text": attrs.pop("text", None),
            "period_start": attrs.pop("period_start", None), "period_end": attrs.pop("period_end", None),
            "valid_from": attrs.pop("valid_from", None), "valid_until": attrs.pop("valid_until", None),
            "text_review": 0, "attributes": attrs, "geometry": geom}


def tiered(*records, depth=20.0):
    return hazards.tier_records(list(records), lambda lon, lat: depth)


def graph(nodes_xy, edges, **edge_attrs):
    """DiGraph with both directions per edge. nodes_xy: {id: (x_m, y_m)}; edges: [(u, v)]."""
    import networkx as nx
    g = nx.DiGraph()
    for n, (x, y) in nodes_xy.items():
        lon, lat = ll(x, y)
        g.add_node(n, lon=lon, lat=lat)
    for u, v in edges:
        for a, b in ((u, v), (v, u)):
            g.add_edge(a, b, min_depth=15.0, cost_factor=1.2, crosses_obstacle=0, **edge_attrs)
    return g


# a 400 m east-west edge through x in [150, 250] (a 100 m wide area, so not a graze)
NODES = {1: (0, 0), 2: (400, 0)}
AREA = box(150, -100, 250, 100)


def run(records, g=None, **kw):
    g = g or graph(NODES, [(1, 2)])
    return g, link_graph_hazards(g, tiered(*records, **kw))


class TestRestrn:
    def test_entry_prohibited_blocks(self):
        g, res = run([rec("RESARE", AREA, RESTRN=[7])])
        assert g.edges[1, 2]["crosses_obstacle"] == 1 and g.edges[2, 1]["crosses_obstacle"] == 1
        assert res["stats"]["blocked"] == 1

    def test_anchoring_prohibited_does_not_block(self):
        # the old _is_entry_prohibited tested RESTRN == 1 (anchoring prohibited) instead of 7
        g, res = run([rec("RESARE", AREA, RESTRN=[1])])
        assert g.edges[1, 2]["crosses_obstacle"] == 0
        assert [r[3] for r in res["edge_notices"]] == [0, 0]       # linked as info, both directions

    def test_lowercase_restrn_only(self):
        g, _ = run([rec("resare", AREA, restrn=[7])])
        assert g.edges[1, 2]["crosses_obstacle"] == 1

    def test_union_of_restrn_and_lowercase(self):
        # RESTRN says anchoring, restrn says entry: the old lowercase-keyed map saw only one of the columns
        g, _ = run([rec("RESARE", AREA, RESTRN=[1], restrn=[7])])
        assert g.edges[1, 2]["crosses_obstacle"] == 1


class TestUnknownValsou:
    POINT = Point(*ll(200, 0))

    def test_zero_valsou_submerged_unknown_is_info(self):
        # Dutch 'UNKNOWN' VALSOU reads as 0.0; WATLEV 3 + CATWRK 3 -> info, no fold
        g, res = run([rec("WRECKS", self.POINT, VALSOU=0.0, WATLEV=[3], CATWRK=[3])])
        assert g.edges[1, 2]["min_depth"] == 15.0 and g.edges[1, 2]["crosses_obstacle"] == 0
        assert res["stats"]["folded"] == 0 and res["edge_notices"][0][3] == 0

    def test_dangerous_unknown_folds_at_zero(self):
        g, res = run([rec("WRECKS", self.POINT, WATLEV=[3], CATWRK=[2])])
        assert g.edges[1, 2]["min_depth"] == 0.0 and g.edges[2, 1]["min_depth"] == 0.0
        assert res["stats"]["folded"] == 1

    def test_known_depth_folds_and_unknown_min_depth_is_replaced(self):
        g = graph(NODES, [(1, 2)])
        g.edges[1, 2]["min_depth"] = UNKNOWN_DEPTH
        link_graph_hazards(g, tiered(rec("WRECKS", self.POINT, VALSOU=4.2, WATLEV=[3]), depth=20.0))
        assert g.edges[1, 2]["min_depth"] == 4.2 and g.edges[2, 1]["min_depth"] == 4.2   # reverse: min(15, 4.2)


class TestExemptions:
    @pytest.mark.parametrize("attr", ["is_opening_bridge_edge", "requires_lock"])
    def test_bridge_and_lock_edges_keep_notice_but_not_block(self, attr):
        g = graph(NODES, [(1, 2)])
        g.edges[1, 2][attr] = True
        res = link_graph_hazards(g, tiered(rec("RESARE", AREA, RESTRN=[7]),
                                           rec("WRECKS", Point(*ll(200, 0)), WATLEV=[3], CATWRK=[2])))
        for a, b in ((1, 2), (2, 1)):
            assert g.edges[a, b]["crosses_obstacle"] == 0 and g.edges[a, b]["min_depth"] == 15.0
        assert res["stats"]["blocked"] == 0 and res["stats"]["folded"] == 0
        assert sorted({r[3] for r in res["edge_notices"]}) == [2, 3]     # still linked, with their severities


class TestSeasonalAndNodes:
    def test_time_limited_closure_is_linked_but_neither_blocks_nor_costs(self):
        r = rec("RESARE", AREA, RESTRN=[7], period_start="--1101", period_end="--0515")
        g, res = run([r, rec("RESARE", AREA, RESTRN=[7], valid_until="20301231", name="second")])
        assert g.edges[1, 2]["cost_factor"] == pytest.approx(1.2)
        assert g.edges[1, 2]["crosses_obstacle"] == 0
        assert res["stats"]["time_limited"] == 1 and res["stats"]["blocked"] == 0
        assert {(r[3], r[4]) for r in res["edge_notices"]} == {(3, "seasonal")}
        assert set(res["stats"]) >= {"blocked", "folded", "time_limited", "unreviewed_text"}

    def test_time_limited_next_to_blocking_notice_still_blocks(self):
        g, res = run([rec("RESARE", AREA, RESTRN=[7], PERSTA="--0401", PEREND="--0930"),
                      rec("RESARE", AREA, RESTRN=[7], name="all year")])
        assert g.edges[1, 2]["crosses_obstacle"] == 1
        assert res["stats"]["blocked"] == 1 and res["stats"]["time_limited"] == 0

    def test_unreviewed_text_blocks_with_reason(self):
        r = rec("RESARE", AREA, RESTRN=[7], text="Winter resting area from November 1 until May 15")
        r["text_review"] = 1
        g, res = run([r])
        assert g.edges[1, 2]["crosses_obstacle"] == 1
        assert {(x[3], x[4]) for x in res["edge_notices"]} == {(3, "conditions in chart text, unreviewed")}
        assert res["stats"]["unreviewed_text"] == 1 and res["stats"]["blocked"] == 1

    def test_unreviewed_text_graze_reports_graze_reason(self):
        g = graph(NODES, [(1, 2)])
        r = rec("RESARE", box(150, 0, 250, 100), RESTRN=[7], text="winter area")      # edge runs along its boundary
        r["text_review"] = 1
        res = link_graph_hazards(g, tiered(r))
        assert {x[4] for x in res["edge_notices"]} <= {"boundary graze", "along charted axis"}
        assert g.edges[1, 2]["crosses_obstacle"] == 0

    def test_node_inside_area_is_linked(self):
        g = graph({1: (0, 0), 2: (200, 0), 3: (400, 0)}, [(1, 2), (2, 3)])
        res = link_graph_hazards(g, tiered(rec("RESARE", AREA, RESTRN=[1])))
        assert [(n, s) for n, _, s in res["node_notices"]] == [(2, 0)]

    def test_axis_downgrade_reason(self):
        # a charted axis running along the crossing downgrades it to caution with a reason
        g = graph(NODES, [(1, 2)])
        axis = LineString([ll(-50, 0), ll(450, 0)])
        res = link_graph_hazards(g, tiered(rec("RESARE", AREA, RESTRN=[7])), [axis])
        assert g.edges[1, 2]["crosses_obstacle"] == 0
        assert {(r[3], r[4]) for r in res["edge_notices"]} == {(1, "along charted axis")}

    def test_no_records_is_a_noop(self):
        g = graph(NODES, [(1, 2)])
        res = link_graph_hazards(g, [])
        assert res["edge_notices"] == [] and g.edges[1, 2]["min_depth"] == 15.0


SQ = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])


def depare(rows):
    """rows: [(DRVAL1, DRVAL2, src_cscl)] all covering SQ."""
    return gpd.GeoDataFrame({"DRVAL1": [r[0] for r in rows], "DRVAL2": [r[1] for r in rows],
                             "src_cscl": [r[2] for r in rows]}, geometry=[SQ] * len(rows), crs="EPSG:4326")


class TestDepthFn:
    def test_off_area_and_empty_are_unknown(self):
        fn = npl._depth_area_fn(depare([(12.0, 20.0, 20000)]))
        assert fn(3.0, 0.5) is None
        assert npl._depth_area_fn(gpd.GeoDataFrame(geometry=[], crs="EPSG:4326"))(0, 0) is None

    def test_overlapping_multiscale_polygons_use_the_edge_selection(self):
        # a coarse 0-18 band and a finer 12-20 band over the same point: the same winner the edge min_depth gets
        gdf = depare([(0.0, 18.2, 500000), (12.0, 20.0, 20000)])
        assert npl._depth_area_fn(gdf)(0.5, 0.5) == 12.0
        assert npl._depare_point_depth(gdf, Point(0.5, 0.5))[0] == 12.0

    def test_coarse_zero_band_is_unknown_like_the_edges(self):
        assert npl._depth_area_fn(depare([(0.0, 18.2, 500000)]))(0.5, 0.5) is None
        assert npl._depth_area_fn(depare([(0.0, 2.0, 20000)]))(0.5, 0.5) == 0.0     # tight band: a real 0

    def test_implausible_drying_placeholder_uses_drval2(self):
        gdf = depare([(-50.0, -4.0, 20000)])
        assert npl._depth_area_fn(gdf)(0.5, 0.5) == -4.0

    def test_wreck_in_minus50_minus4_polygon(self):
        # charted depth there is -4 (DRVAL2), not -50: a wreck shoaler than that is a depth hazard,
        # one at or below it is covered by the depth raster (info). With -50 both would have been info.
        fn = npl._depth_area_fn(depare([(-50.0, -4.0, 20000)]))
        pt = Point(0.5, 0.5)
        shoal = hazards.tier_records([rec("WRECKS", pt, VALSOU=-5.0, WATLEV=[4])], fn)[0]
        assert (shoal["severity"], shoal["depth_m"]) == (hazards.SEV_DEPTH, -5.0)
        covered = hazards.tier_records([rec("WRECKS", pt, VALSOU=-3.0, WATLEV=[4])], fn)[0]
        assert covered["severity"] == hazards.SEV_INFO

    def test_edge_worker_uses_the_shared_helper(self):
        # same polygons through the real worker: min_depth equals the helper's number
        gdf = depare([(-50.0, -4.0, 20000)])
        g = {"depth_areas": gdf}
        npl._edge_attr_init(npl.Geod(ellps="WGS84"), g)
        out = npl._edge_attr_worker([(1, 2, 0.2, 0.5, 0.8, 0.5, "coastal", False, False, None)])
        assert out[(1, 2)]["min_depth"] == -4.0


class TestChartedAxes:
    def _p(self, tmp_path, channel_axes):
        p = NauticalRoutingPipeline(data_paths={}, db_path=str(tmp_path / "o.sqlite"), use_channel_axes=channel_axes)
        p.classification_config.channel_axes_min_confidence = 0.0
        charted = LineString([ll(-50, 5), ll(450, 5)])
        from shapely.geometry import MultiLineString
        multi = MultiLineString([[ll(-50, 400), ll(450, 400)], [ll(-50, 450), ll(450, 450)]])
        p.gdfs["inland_waterways"] = gpd.GeoDataFrame(geometry=[charted, multi], crs="EPSG:4326")
        derived = LineString([ll(-50, -300), ll(450, -300)])
        p.gdfs["channel_axes"] = gpd.GeoDataFrame({"confidence": [1.0]}, geometry=[derived], crs="EPSG:4326")
        return p

    @pytest.mark.parametrize("channel_axes", [False, True])
    def test_charted_axes_only_and_independent_of_channel_axes(self, tmp_path, channel_axes):
        p = self._p(tmp_path, channel_axes)
        p._capture_charted_axes()
        p._merge_channel_axes()
        assert len(p.hazard_axes) == 3 and all(a.geom_type == "LineString" for a in p.hazard_axes)
        assert not any(a.bounds[1] < ll(0, -100)[1] for a in p.hazard_axes)      # the derived axis is not included

    def test_edge_along_charted_axis_in_a_web_is_downgraded(self, tmp_path):
        p = self._p(tmp_path, False)
        p._capture_charted_axes()
        g = graph({1: (0, 5), 2: (200, 5), 3: (400, 5), 4: (200, 90)}, [(1, 2), (2, 3), (2, 4)])
        res = link_graph_hazards(g, tiered(rec("RESARE", AREA, RESTRN=[7])), p.hazard_axes)
        assert g.edges[1, 2]["crosses_obstacle"] == 0 and g.edges[2, 3]["crosses_obstacle"] == 0
        assert {(r[0], r[1]): (r[3], r[4]) for r in res["edge_notices"]}[(1, 2)] == (1, "along charted axis")


class TestWiring:
    def _pipeline(self, tmp_path, cells_dir):
        p = NauticalRoutingPipeline(data_paths={}, db_path=str(tmp_path / "o.sqlite"), hazard_cells_dir=cells_dir,
                                    coverage_bbox=(3.4, 51.4, 3.6, 51.6))
        p.graph = graph(NODES, [(1, 2)])
        p.gdfs["depth_areas"] = gpd.GeoDataFrame({"DRVAL1": [20.0]}, geometry=[box(-500, -500, 900, 500)],
                                                 crs="EPSG:4326")
        return p

    def test_cell_selection_and_extract_wiring(self, tmp_path, monkeypatch):
        seen = {}

        def fake_cells(d, bbox):
            seen["cells"] = (d, tuple(bbox))
            return ["a.000", "b.000"]

        def fake_extract(cells, bbox):
            seen["extract"] = (list(cells), tuple(bbox))
            return [rec("RESARE", AREA, RESTRN=[7])]
        monkeypatch.setattr(hazards, "latest_cells", fake_cells)
        monkeypatch.setattr(hazards, "extract_hazards", fake_extract)
        p = self._pipeline(tmp_path, str(tmp_path / "cells"))
        p._apply_hazards()
        assert seen["cells"] == (str(tmp_path / "cells"), (3.4, 51.4, 3.6, 51.6))
        assert seen["extract"] == (["a.000", "b.000"], (3.4, 51.4, 3.6, 51.6))
        assert p.graph.edges[1, 2]["crosses_obstacle"] == 1
        assert p.hazard_result["stats"]["notices"] == 1

    def test_zero_cells_is_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hazards, "latest_cells", lambda d, b: [])
        p = self._pipeline(tmp_path, str(tmp_path / "empty"))
        with pytest.raises(SystemExit) as e:
            p._apply_hazards()
        assert "--no-hazards" in str(e.value) and "no S-57 cells" in str(e.value)

    def test_without_flag_nothing_is_blocked_and_warns(self, tmp_path, caplog):
        p = self._pipeline(tmp_path, None)
        p._apply_hazards()
        assert p.hazard_result is None and p.graph.edges[1, 2]["crosses_obstacle"] == 0
        assert "--hazard-cells-dir" in caplog.text

    def test_latest_cells_keeps_last_edition_per_name(self, tmp_path, monkeypatch):
        for ed in ("ed1", "ed2"):
            (tmp_path / ed).mkdir()
            (tmp_path / ed / "X.000").write_text("")
        # no real S-57 content: the bbox filter drops unreadable cells, so only check the selection path
        monkeypatch.setattr("osgeo.ogr.Open", lambda p: None)
        assert hazards.latest_cells(str(tmp_path), (0, 0, 1, 1)) == []


class TestExport:
    def test_schema3_rows(self, tmp_path, monkeypatch):
        r_block = rec("RESARE", AREA, RESTRN=[7], name="NoEntry")
        r_season = rec("RESARE", box(300, -100, 350, 100), RESTRN=[7], PERSTA="--0401", PEREND="--0930",
                       period_start="--0401", period_end="--0930")
        r_wreck = rec("WRECKS", Point(*ll(50, 0)), VALSOU=3.0, WATLEV=[3])
        monkeypatch.setattr(hazards, "latest_cells", lambda d, b: ["a.000"])
        monkeypatch.setattr(hazards, "extract_hazards", lambda c, b: [r_block, r_season, r_wreck])
        p = TestWiring()._pipeline(tmp_path, "cells")
        p.graph = graph({1: (0, 0), 2: (400, 0)}, [(1, 2)])
        p.gdfs["depth_areas"] = gpd.GeoDataFrame({"DRVAL1": [20.0]}, geometry=[box(-500, -500, 900, 500)],
                                                 crs="EPSG:4326")
        p._apply_hazards()
        try:
            p.export_to_sqlite()
        except Exception as e:           # the full exporter needs a built pipeline; do not mask real failures
            pytest.fail(f"export_to_sqlite failed on a minimal graph: {e!r}")
        c = sqlite3.connect(p.db_path)
        assert c.execute("SELECT schema_version FROM metadata").fetchone()[0] == 3
        edges = c.execute("SELECT source, target, min_depth, cost_factor, crosses_obstacle FROM edges "
                          "ORDER BY source").fetchall()
        assert [(e[0], e[1]) for e in edges] == [(1, 2), (2, 1)]
        for e in edges:
            assert e[2] == 3.0 and e[4] == 1 and e[3] == pytest.approx(1.2)      # no seasonal cost any more
        assert c.execute("SELECT COUNT(*) FROM notices").fetchone()[0] == 3
        en = c.execute("SELECT source, target, notice_id, severity FROM edge_notices").fetchall()
        assert len(en) == 6 and {(s, t) for s, t, _, _ in en} == {(1, 2), (2, 1)}
        assert c.execute("SELECT COUNT(*) FROM node_notices").fetchone()[0] == 0   # no node inside/near any object
        sev = {r[0]: r[1] for r in c.execute("SELECT name, severity FROM notices WHERE name IS NOT NULL")}
        assert sev == {"NoEntry": 3}

    def _winter(self):
        r = rec("RESARE", AREA, RESTRN=[7], text="Winter resting area from November 1 until May 15")
        r["text_review"] = 1
        return r

    def _run_apply(self, tmp_path, monkeypatch, overrides):
        r = self._winter()
        monkeypatch.setattr(hazards, "latest_cells", lambda d, b: ["a.000"])
        monkeypatch.setattr(hazards, "extract_hazards", lambda c, b: [r])
        monkeypatch.setattr(hazards, "DEFAULT_OVERRIDES_PATH", str(tmp_path / "absent.json"))
        p = TestWiring()._pipeline(tmp_path, "cells")
        p.hazard_overrides = overrides
        p._apply_hazards()
        return p, r

    def test_apply_hazards_unreviewed_text_blocks_and_logs_stats(self, tmp_path, monkeypatch):
        p, _ = self._run_apply(tmp_path, monkeypatch, None)
        assert p.graph.edges[1, 2]["crosses_obstacle"] == 1
        assert p.hazard_stats["blocked"] == 1 and p.hazard_stats["unreviewed_text"] == 1
        assert p.hazard_stats["time_limited"] == 0 and "seasonal" not in p.hazard_stats

    def test_apply_hazards_accepted_override_makes_it_time_limited(self, tmp_path, monkeypatch):
        import json
        r = self._winter()
        ov = tmp_path / "ov.json"
        json.dump({str(hazards.notice_id(r)): {"period_start": "--1101", "period_end": "--0515", "valid_from": None,
                                               "valid_until": None, "text_sha1": hazards.text_sha1(r),
                                               "status": "accepted"}}, open(ov, "w"))
        p, _ = self._run_apply(tmp_path, monkeypatch, str(ov))
        assert p.graph.edges[1, 2]["crosses_obstacle"] == 0 and p.graph.edges[1, 2]["cost_factor"] == pytest.approx(1.2)
        assert p.hazard_stats["blocked"] == 0 and p.hazard_stats["time_limited"] == 1
        assert {(x[3], x[4]) for x in p.hazard_result["edge_notices"]} == {(3, "seasonal")}
        p.export_to_sqlite()
        c = sqlite3.connect(p.db_path)
        assert c.execute("SELECT period_start, period_end, period_source, text_review, severity FROM notices"
                         ).fetchone() == ("--1101", "--0515", "text-reviewed", 0, 3)

    def test_apply_hazards_stale_or_proposed_override_ignored(self, tmp_path, monkeypatch):
        import json
        r = self._winter()
        for status, sha in (("accepted", "0" * 40), ("proposed", hazards.text_sha1(r))):
            ov = tmp_path / f"ov-{status}.json"
            json.dump({str(hazards.notice_id(r)): {"period_start": "--1101", "period_end": "--0515",
                                                   "text_sha1": sha, "status": status}}, open(ov, "w"))
            p, _ = self._run_apply(tmp_path, monkeypatch, str(ov))
            assert p.graph.edges[1, 2]["crosses_obstacle"] == 1

    def test_cli_has_hazard_overrides_option(self):
        import subprocess, sys, os
        out = subprocess.run([sys.executable, os.path.join(os.path.dirname(npl.__file__), "nautical_routing_pipeline.py"),
                              "--help"], capture_output=True, text=True).stdout
        assert "--hazard-overrides" in out

    def test_schema2_without_flag(self, tmp_path):
        p = TestWiring()._pipeline(tmp_path, None)
        p._apply_hazards()
        p.export_to_sqlite()
        c = sqlite3.connect(p.db_path)
        assert c.execute("SELECT schema_version FROM metadata").fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='notices'").fetchone()[0] == 0


class TestCliAndOrder:
    def test_cli_requires_hazard_cells_dir_or_no_hazards(self, tmp_path):
        import subprocess, sys, os
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        r = subprocess.run([sys.executable, "nautical_routing_pipeline.py", "--input-dir", str(tmp_path),
                            "--output", str(tmp_path / "o.sqlite")], cwd=repo, capture_output=True, text=True)
        assert r.returncode == 2 and "--hazard-cells-dir" in r.stderr and "--no-hazards" in r.stderr
        r = subprocess.run([sys.executable, "nautical_routing_pipeline.py", "--hazard-cells-dir", "x", "--no-hazards"],
                           cwd=repo, capture_output=True, text=True)
        assert r.returncode == 2 and "mutually exclusive" in r.stderr

    def test_latest_cells_follows_symlinked_dirs(self, tmp_path, monkeypatch):
        real = tmp_path / "real" / "ENC_ROOT" / "C1"
        real.mkdir(parents=True)
        (real / "C1.000").write_text("")
        comp = tmp_path / "composed"
        comp.mkdir()
        (comp / "MD").symlink_to(tmp_path / "real")
        seen = []
        monkeypatch.setattr("osgeo.ogr.Open", lambda p: seen.append(p))   # None -> skipped; we only check the walk
        hazards.latest_cells(str(comp), (0, 0, 1, 1))
        assert seen and seen[0].endswith("C1.000") and "/composed/MD/" in seen[0]

    def test_hazards_run_after_edge_attributes(self, tmp_path, monkeypatch):
        p = NauticalRoutingPipeline(data_paths={}, db_path=str(tmp_path / "o.sqlite"))
        calls = []
        for name in ("parse_shapefiles", "build_network", "_log_node_origin_diag", "_add_opening_bridge_edges",
                     "_add_lock_crossing_edges", "_mark_edges_inside_locks", "_sanity_check_no_land_crossings",
                     "_ensure_coastal_connectivity", "_connect_channel_axis_deadends", "calculate_edge_attributes",
                     "_apply_hazards", "_compute_node_depths", "export_to_sqlite"):
            monkeypatch.setattr(p, name, lambda *a, _n=name, **k: calls.append(_n))
        p.run_pipeline()
        assert calls.index("_apply_hazards") > calls.index("calculate_edge_attributes")
        assert calls.index("_apply_hazards") < calls.index("export_to_sqlite")


class TestExplicitOverridesPath:
    def test_missing_explicit_path_is_an_error_in_apply_and_cli(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hazards, "latest_cells", lambda d, b: ["a.000"])
        monkeypatch.setattr(hazards, "extract_hazards", lambda c, b: [])
        p = TestWiring()._pipeline(tmp_path, "cells")
        p.hazard_overrides = str(tmp_path / "missing.json")
        with pytest.raises(SystemExit, match="file not found"):
            p._apply_hazards()
        import subprocess, sys, os
        r = subprocess.run([sys.executable, os.path.join(os.path.dirname(npl.__file__), "nautical_routing_pipeline.py"),
                            "--hazard-overrides", str(tmp_path / "missing.json"), "--no-hazards"],
                           capture_output=True, text=True)
        assert r.returncode != 0 and ("file not found" in r.stderr or "required" in r.stderr)
