"""Unit tests for hazards.py (synthetic records, no raw S-57 cells needed)."""
import json
import math

from shapely.geometry import LineString, Point, Polygon

import hazards
from hazards import classify, link_edges, link_nodes, notice_id, notice_row

LON0, LAT0 = 3.5, 51.5
KX = 111320.0 * math.cos(math.radians(LAT0))
KY = 111320.0


def ll(x, y):
    """Local metres -> lon/lat."""
    return (LON0 + x / KX, LAT0 + y / KY)


def box(x0, y0, x1, y1):
    return Polygon([ll(x0, y0), ll(x1, y0), ll(x1, y1), ll(x0, y1)])


def line(*pts):
    return LineString([ll(x, y) for x, y in pts])


def rec(cls, geom=None, **attrs):
    return {"source_class": cls, "source_ref": "LNAM1", "source_cell": "X.000",
            "name": attrs.pop("name", None), "text": attrs.pop("text", None),
            "period_start": None, "period_end": None, "attributes": attrs,
            "geometry": geom or Point(*ll(0, 0))}


class TestAreaTiers:
    def test_entry_prohibited_all_year(self):
        assert classify(rec("RESARE", RESTRN=[7]), None)[0] == 3

    def test_entry_prohibited_seasonal(self):
        # time-limited prohibited areas stay severity 3 (the writers neither block nor re-cost them)
        assert classify(rec("RESARE", RESTRN=[7], PERSTA="--0401", PEREND="--0930"), None) == \
            (3, None, "entry prohibited, seasonal")
        assert classify(rec("RESARE", RESTRN=[7], DATSTA="20261101"), None)[0] == 3

    def test_entry_restricted(self):
        assert classify(rec("RESARE", RESTRN=[8]), None)[0] == 1

    def test_anchoring_prohibited_is_info(self):
        assert classify(rec("RESARE", RESTRN=[1]), None)[0] == 0

    def test_lowercase_restrn_only(self):
        assert classify(rec("RESARE", restrn="['7']"), None)[0] == 3
        assert classify(rec("RESARE", restrn=8), None)[0] == 1

    def test_union_of_both_columns(self):
        assert classify(rec("RESARE", RESTRN=[1], restrn=[7]), None)[0] == 3

    def test_kite_area_info(self):
        assert classify(rec("RESARE", CATREA=[26]), None)[0] == 0

    def test_mariculture_without_restriction_info(self):
        assert classify(rec("MARCUL", CATMFA=3), None)[0] == 0

    def test_caution_area(self):
        assert classify(rec("CTNARE"), None)[0] == 1

    def test_hulk(self):
        assert classify(rec("HULKES"), None)[0] == 3

    def test_phrase_fallback_without_restrn(self):
        assert classify(rec("RESARE", text="Entry prohibited"), None)[0] == 3
        assert classify(rec("RESARE", name="Toegang verboden"), None)[0] == 3

    def test_phrase_ignored_when_restrn_present(self):
        assert classify(rec("RESARE", RESTRN=[1], text="entry prohibited"), None)[0] == 0


class TestPointTiers:
    def test_known_valsou_shoaler_than_surroundings(self):
        assert classify(rec("WRECKS", VALSOU=3.2, WATLEV=3), 10.0)[:2] == (2, 3.2)

    def test_known_valsou_not_shoaler(self):
        assert classify(rec("WRECKS", VALSOU=12.0, WATLEV=3), 10.0)[0] == 0
        assert classify(rec("WRECKS", VALSOU=10.0, WATLEV=3), 10.0)[0] == 0

    def test_known_valsou_unknown_surroundings_is_depth(self):
        assert classify(rec("OBSTRN", VALSOU=2.0), None)[:2] == (2, 2.0)

    def test_valsou_zero_with_watlev3_is_unknown(self):
        sev, depth, _ = classify(rec("WRECKS", VALSOU=0.0, WATLEV=3, CATWRK=3), 20.0)
        assert (sev, depth) == (0, None)

    def test_unknown_distributed_remains_submerged(self):
        assert classify(rec("WRECKS", CATWRK=3, WATLEV=3), 20.0)[0] == 0

    def test_unknown_dangerous(self):
        assert classify(rec("WRECKS", CATWRK=2), 20.0)[:2] == (2, 0.0)
        assert classify(rec("OBSTRN", EXPSOU=2), 20.0)[:2] == (2, 0.0)

    def test_unknown_non_dangerous(self):
        assert classify(rec("WRECKS", CATWRK=1), 20.0)[0] == 0
        assert classify(rec("OBSTRN", EXPSOU=1, WATLEV=2), 20.0)[0] == 0

    def test_mast_showing(self):
        assert classify(rec("WRECKS", CATWRK=5, VALSOU=5.0), 20.0)[0] == 3

    def test_no_valsou_awash(self):
        assert classify(rec("UWTROC", WATLEV=2), 20.0)[0] == 3

    def test_no_valsou_no_watlev(self):
        assert classify(rec("WRECKS"), 20.0)[0] == 3

    def test_watlev_list_parsed(self):
        assert classify(rec("WRECKS", WATLEV=[3]), 20.0)[0] == 0
        assert classify(rec("WRECKS", WATLEV="['3']"), 20.0)[0] == 0

    def test_tier_records_uses_depth_fn(self):
        r = rec("WRECKS", VALSOU=3.0, WATLEV=3)
        hazards.tier_records([r], lambda lon, lat: 10.0)
        assert (r["severity"], r["depth_m"]) == (2, 3.0)
        hazards.tier_records([r], lambda lon, lat: "land")
        assert r["severity"] == 2  # unknown surroundings -> conservative


class TestLinkEdges:
    def area(self):
        return rec("RESARE", box(0, 0, 400, 200), RESTRN=[7])

    def test_corner_graze_downgraded(self):
        # enters the west side 18 m deep, mid-height
        links = link_edges([self.area()], [("e", line((-50, 100), (18, 100)))])
        assert len(links) == 1
        assert links[0].severity == 1 and links[0].downgraded

    def test_deep_crossing_stays_prohibited(self):
        links = link_edges([self.area()], [("e", line((-50, 100), (450, 100)))])
        assert links[0].severity == 3 and not links[0].downgraded

    def test_deep_crossing_along_charted_axis(self):
        edge = line((-50, 105), (450, 105))
        axis = line((-100, 100), (500, 100))
        links = link_edges([self.area()], [("e", edge)], axes=[axis])
        assert links[0].severity == 1 and links[0].downgraded

    def test_axis_not_along_edge_does_not_downgrade(self):
        edge = line((-50, 150), (450, 150))
        axis = line((-100, 20), (500, 20))
        links = link_edges([self.area()], [("e", edge)], axes=[axis])
        assert links[0].severity == 3

    def test_disjoint_edge_not_linked(self):
        assert link_edges([self.area()], [("e", line((-50, 300), (450, 300)))]) == []

    def test_point_radius(self):
        w = rec("WRECKS", Point(*ll(100, 25)), VALSOU=2.0)
        w2 = rec("WRECKS", Point(*ll(100, 40)), VALSOU=2.0)
        hazards.tier_records([w, w2], lambda lon, lat: 10.0)
        links = link_edges([w, w2], [("e", line((0, 0), (200, 0)))])
        assert [(l.index, l.severity) for l in links] == [(0, 2)]

    def test_non_restriction_prohibited_not_downgraded(self):
        hulk = rec("HULKES", box(0, 0, 400, 200))
        links = link_edges([hulk], [("e", line((-50, 100), (18, 100)))])
        assert links[0].severity == 3

    def test_many_edges_fast(self):
        recs = [rec("WRECKS", Point(*ll(i * 7 % 5000, i * 13 % 5000)), VALSOU=1.0) for i in range(2000)]
        edges = [(i, line((i % 100 * 50, i // 100 * 50), (i % 100 * 50 + 40, i // 100 * 50))) for i in range(10000)]
        import time
        t = time.time()
        link_edges(recs, edges)
        assert time.time() - t < 20


class TestChains:
    def sev(self, area, edges, axes=None):
        return {l.edge_key: l.severity for l in link_edges([area], edges, axes)}

    def test_fork_outside_area_is_not_one_crossing(self):
        area = rec("RESARE", box(0, 0, 400, 200), RESTRN=[7])
        g = ("G", line((-60, 100), (18, 100)))
        d = ("D", line((-60, 100), (450, 180)))
        assert self.sev(area, [g]) == {"G": 1}
        assert self.sev(area, [g, d]) == {"G": 1, "D": 3}

    def test_lines_meeting_inside_form_one_crossing(self):
        area = rec("RESARE", box(0, 0, 50, 200), RESTRN=[7])
        a = ("A", line((-50, 100), (25, 100)))
        b = ("B", line((100, 100), (25, 100)))
        assert self.sev(area, [a]) == {"A": 1} and self.sev(area, [b]) == {"B": 1}
        assert self.sev(area, [a, b]) == {"A": 3, "B": 3}

    def test_lines_meeting_inside_within_snap_distance_chain(self):
        area = rec("RESARE", box(0, 0, 400, 200), RESTRN=[7])
        em = [shapely_proj(line((-50, 100), (25, 100))), shapely_proj(line((100, 100), (29, 103)))]
        assert hazards._chains([0, 1], em, shapely_proj(area["geometry"])) == [[0, 1]]
        far = [shapely_proj(line((-50, 100), (25, 100))), shapely_proj(line((100, 100), (45, 100)))]
        assert len(hazards._chains([0, 1], far, shapely_proj(area["geometry"]))) == 2

    def test_gap_inside_area_is_bridged(self):
        # 50 m strip: two lines meeting 4 m apart inside it are one full crossing -> prohibited.
        area = rec("RESARE", box(0, 0, 50, 200), RESTRN=[7])
        a = ("A", line((-50, 100), (25, 100)))
        b = ("B", line((100, 100), (25, 104)))
        assert self.sev(area, [a, b]) == {"A": 3, "B": 3}

    def test_multi_segment_graze_stays_caution(self):
        area = rec("RESARE", box(0, 0, 400, 200), RESTRN=[7])
        pts = [(-50, 100), (-5, 100), (8, 100), (14, 100), (20, 100)]
        edges = [(i, line(a, b)) for i, (a, b) in enumerate(zip(pts[:-1], pts[1:]))]
        got = self.sev(area, edges)
        assert got == {1: 1, 2: 1, 3: 1}

    def test_reentering_crossing_is_two_chains(self):
        area = rec("RESARE", box(0, 0, 400, 200), RESTRN=[7])
        pts = [(-50, 100), (20, 100), (20, 260), (300, 100), (450, 100)]
        edges = [(i, line(a, b)) for i, (a, b) in enumerate(zip(pts[:-1], pts[1:]))]
        assert self.sev(area, edges) == {0: 1, 1: 1, 2: 3, 3: 3}

    def test_graze_along_axis_stays_caution(self):
        area = rec("RESARE", box(0, 0, 400, 200), RESTRN=[7])
        links = link_edges([area], [("e", line((-50, 100), (18, 100)))], axes=[line((-100, 100), (500, 100))])
        assert links[0].severity == 1 and links[0].downgraded


def shapely_proj(geom):
    """lon/lat -> local metres, the same frame as ll()."""
    import shapely
    return shapely.transform(geom, lambda c: __import__("numpy").column_stack(
        [(c[:, 0] - LON0) * KX, (c[:, 1] - LAT0) * KY]))


class TestLinkNodes:
    def test_area_contains_and_point_radius(self):
        area = rec("RESARE", box(0, 0, 100, 100), RESTRN=[1])
        wreck = rec("WRECKS", Point(*ll(500, 500)), VALSOU=1.0)
        nodes = [("in", ll(50, 50)), ("out", ll(150, 50)), ("near", ll(510, 500)), ("far", ll(560, 500))]
        got = {(l.node_key, l.index) for l in link_nodes([area, wreck], nodes)}
        assert got == {("in", 0), ("near", 1)}


class TestNotices:
    def test_notice_id_stable_and_distinct(self):
        a = rec("RESARE", box(0, 0, 100, 100), RESTRN=[7])
        assert notice_id(a) == notice_id(dict(a))
        assert notice_id(a) != notice_id(dict(a, source_ref="LNAM2"))
        assert notice_id(a) != notice_id(dict(a, source_class="MARCUL"))
        # geometry edits between editions keep the id when an LNAM exists
        assert notice_id(a) == notice_id(rec("RESARE", box(0, 0, 100, 101), RESTRN=[7]))

    def test_notice_id_without_lnam_uses_geometry(self):
        a = dict(rec("RESARE", box(0, 0, 100, 100)), source_ref=None)
        b = dict(rec("RESARE", box(0, 0, 100, 101)), source_ref=None)
        assert notice_id(a) != notice_id(b)
        assert 0 <= notice_id(a) < 16 ** 13

    def test_notice_row_json(self):
        a = rec("RESARE", box(0, 0, 100, 100), RESTRN=[7], name="Natura")
        row = notice_row(a, 3, None)
        json.dumps(row)
        assert row["id"] == notice_id(a) and row["severity"] == 3 and row["depth_m"] is None
        assert json.loads(row["attributes"])["RESTRN"] == [7]
        assert json.loads(row["geometry"])["type"] == "Polygon"
        assert row["min_lat"] < row["max_lat"] and row["min_lon"] < row["max_lon"]

    def test_ddl_mentions_all_tables(self):
        for t in ("notices", "edge_notices", "node_notices"):
            assert f"CREATE TABLE {t}" in hazards.NOTICES_DDL


class TestParsing:
    def test_adjacent_string_literals_not_concatenated(self):
        assert hazards._ints("['8' '27']") == [8, 27]
        assert hazards._ints("['5' '7']") == [5, 7]

    def test_nested_string_lists(self):
        assert hazards._ints(["['5' '7']"]) == [5, 7]
        assert hazards._ints("[\"['5' '7']\"]") == [5, 7]

    def test_scalars(self):
        assert hazards._ints(7) == [7] and hazards._ints(7.0) == [7]
        assert hazards._ints("7,8") == [7, 8] and hazards._ints(None) == []

    def test_restrn_7_in_string_list_is_prohibited(self):
        assert classify(rec("RESARE", RESTRN="['5' '7']"), None)[0] == 3


class TestMoreTiers:
    def test_restrn_7_and_8(self):
        assert classify(rec("RESARE", RESTRN=[7, 8]), None)[0] == 3

    def test_restrn_8_with_period_still_caution(self):
        assert classify(rec("RESARE", RESTRN=[8], PERSTA="--0401"), None)[0] == 1

    def test_lowercase_period_key_is_seasonal(self):
        assert classify(rec("RESARE", RESTRN=[7], persta="--0401"), None)[2] == "entry prohibited, seasonal"

    def test_expsou(self):
        assert classify(rec("OBSTRN", EXPSOU=1), 5.0)[0] == 0
        assert classify(rec("OBSTRN", EXPSOU=2), 5.0)[:2] == (2, 0.0)

    def test_catwrk1_with_any_watlev_is_info_when_unknown_depth(self):
        # spec: unknown depth + CATWRK 1 -> info, checked before WATLEV
        assert classify(rec("WRECKS", CATWRK=1, WATLEV=2), 5.0)[0] == 0

    def test_land_and_negative_surroundings(self):
        assert classify(rec("WRECKS", VALSOU=2.0, WATLEV=3), None)[:2] == (2, 2.0)
        assert classify(rec("WRECKS", VALSOU=1.0, WATLEV=3), -0.5)[0] == 0   # dries around it
        assert classify(rec("WRECKS", VALSOU=-1.0, WATLEV=3), -0.5)[0] == 2

    def test_wreck_polygon_and_uwtroc(self):
        assert classify(rec("WRECKS", box(0, 0, 20, 20), VALSOU=3.0), 10.0)[:2] == (2, 3.0)
        assert classify(rec("UWTROC", WATLEV=4, VALSOU=1.2), 6.0)[:2] == (2, 1.2)
        assert classify(rec("UWTROC", WATLEV=3), 6.0)[0] == 0

    def test_tier_records_numpy_depth(self):
        import numpy as np
        r = rec("WRECKS", VALSOU=3.0, WATLEV=3)
        hazards.tier_records([r], lambda lon, lat: np.float32(10.0))
        assert r["severity"] == 2
        hazards.tier_records([r], lambda lon, lat: np.float64(2.0))
        assert r["severity"] == 0


class TestChainGraze:
    def strip(self):
        return rec("RESARE", box(0, 0, 50, 1000), RESTRN=[7])

    def test_full_crossing_of_narrow_strip_is_prohibited(self):
        links = link_edges([self.strip()], [("e", line((-20, 500), (70, 500)))])
        assert links[0].severity == 3

    def test_densified_crossing_is_one_chain(self):
        edges = [(i, line((-20 + 30 * i, 500), (10 + 30 * i, 500))) for i in range(3)]
        links = link_edges([self.strip()], edges)
        assert len(links) == 3 and all(l.severity == 3 for l in links)

    def test_corner_graze_of_big_area(self):
        a = rec("RESARE", box(0, 0, 1000, 1000), RESTRN=[7])
        links = link_edges([a], [("e", line((-50, 18), (18, 18)))])
        assert links[0].severity == 1

    def test_run_inside_boundary_for_600m(self):
        a = rec("RESARE", box(0, 0, 1000, 1000), RESTRN=[7])
        edge = line((-30, 20), (600, 20))   # enters at x=0, 20 m inside the south boundary
        assert link_edges([a], [("e", edge)])[0].severity == 1

    def test_chain_ending_inside_is_not_a_cut(self):
        a = rec("RESARE", box(0, 0, 1000, 1000), RESTRN=[7])
        assert link_edges([a], [("e", line((-30, 500), (20, 500)))])[0].severity == 1

    def test_multipolygon(self):
        from shapely.geometry import MultiPolygon
        mp = MultiPolygon([box(0, 0, 50, 1000), box(500, 0, 1500, 1000)])
        a = rec("RESARE", mp, RESTRN=[7])
        links = link_edges([a], [("cross", line((-20, 500), (70, 500))), ("graze", line((480, 18), (518, 18)))])
        got = {l.edge_key: l.severity for l in links}
        assert got == {"cross": 3, "graze": 1}

    def test_polygon_with_hole(self):
        outer = [ll(0, 0), ll(1000, 0), ll(1000, 1000), ll(0, 1000)]
        hole = [ll(300, 300), ll(700, 300), ll(700, 700), ll(300, 700)]
        a = rec("RESARE", Polygon(outer, [hole]), RESTRN=[7])
        # a deep run through the ring, passing the hole: prohibited
        deep = link_edges([a], [("e", line((-30, 150), (1030, 150)))])
        assert deep[0].severity == 3
        # crossing the hole edge-to-edge is not a graze either
        assert link_edges([a], [("e", line((250, 500), (750, 500)))])[0].severity == 3

    def test_axis_rule_on_chain(self):
        a = rec("RESARE", box(0, 0, 400, 200), RESTRN=[7])
        edges = [(i, line((-50 + 125 * i, 105), (75 + 125 * i, 105))) for i in range(4)]
        axis = line((-100, 100), (600, 100))
        assert all(l.severity == 1 for l in link_edges([a], edges, axes=[axis]))
        assert all(l.severity == 3 for l in link_edges([a], edges))


class TestNoticeRowGeometry:
    def test_bbox_from_original_and_simplified_geometry(self):
        pts = [ll(x, 50 + 2 * (x % 20 > 10)) for x in range(0, 500, 5)]
        poly = Polygon(pts + [ll(500, 0), ll(0, 0)])
        row = notice_row(rec("RESARE", poly, RESTRN=[7]), 3, None)
        assert row["min_lon"] == poly.bounds[0] and row["max_lat"] == poly.bounds[3]
        n_orig = len(poly.exterior.coords)
        n_simp = len(json.loads(row["geometry"])["coordinates"][0])
        assert n_simp < n_orig

    def test_tiny_polygon_not_emptied(self):
        tiny = box(0, 0, 0.01, 0.01)   # 1 cm: set_precision(1e-6) would collapse it
        row = notice_row(rec("RESARE", tiny, RESTRN=[7]), 3, None)
        assert json.loads(row["geometry"])["coordinates"]


class _Geom:
    def __init__(self, g): self.g = g
    def ExportToIsoWkb(self): return bytearray(__import__("shapely").to_wkb(self.g))


class _Feat:
    def __init__(self, g, items): self.g, self._i = g, items
    def GetGeometryRef(self): return _Geom(self.g)
    def items(self): return dict(self._i)


class _Layer:
    def __init__(self, feats, cscl=None): self.feats, self.cscl, self.i = feats, cscl, 0
    def SetSpatialFilterRect(self, *a): pass
    def ResetReading(self): self.i = 0
    def GetNextFeature(self):
        if self.i >= len(self.feats):
            return None
        self.i += 1
        return self.feats[self.i - 1]
    def GetField(self, name): return self.cscl


class _DS:
    def __init__(self, layers): self.layers = layers
    def GetLayerByName(self, n): return self.layers.get(n)


class TestExtractHazards:
    def run(self, monkeypatch, cells):
        from osgeo import ogr
        monkeypatch.setattr(ogr, "Open", lambda p: cells[p])
        return hazards.extract_hazards(list(cells), (3.0, 51.0, 4.0, 52.0))

    def ds(self, cscl, feats, cls="WRECKS"):
        dsid = _Layer([_Feat(Point(0, 0), {})], cscl)
        return _DS({"DSID": dsid, cls: _Layer(feats)})

    def test_neighbouring_wrecks_survive_and_copies_merge(self, monkeypatch):
        p1, p2 = Point(*ll(0, 0)), Point(*ll(0.5, 0))
        silvio = {"LNAM": "A1", "OBJNAM": "Silvio", "CATWRK": 2, "VALSOU": 16.7, "WATLEV": 3}
        other = {"LNAM": "A2", "CATWRK": 1, "VALSOU": 0.0, "WATLEV": 3}
        big = self.ds(5000, [_Feat(p1, silvio), _Feat(p2, other)])
        small = self.ds(40000, [_Feat(p1, dict(silvio, LNAM="B9", RCID=7)), _Feat(p2, dict(other, LNAM="B8"))])
        recs = self.run(monkeypatch, {"small.000": small, "big.000": big})
        assert len(recs) == 2
        assert {r["source_cell"] for r in recs} == {"big.000"}
        unknown = [r for r in recs if r["source_ref"] == "A2"][0]
        assert "VALSOU" not in unknown["attributes"]       # UNKNOWN 0.0 -> dropped
        assert classify(unknown, 10.0)[0] == 0

    def test_same_lnam_other_cell_merges(self, monkeypatch):
        a = {"LNAM": "A1", "CATWRK": 2, "VALSOU": 3.0}
        b = {"LNAM": "A1", "CATWRK": 2, "VALSOU": 3.5}
        recs = self.run(monkeypatch, {"x.000": self.ds(5000, [_Feat(Point(*ll(0, 0)), a)]),
                                      "y.000": self.ds(9000, [_Feat(Point(*ll(3, 0)), b)])})
        assert len(recs) == 1 and recs[0]["attributes"]["VALSOU"] == 3.0

    def test_object_split_over_neighbouring_cells_is_joined(self, monkeypatch):
        # Same LNAM, pieces ~2 km apart (cell halves N/S): one record, one notice_id, union geometry.
        a = {"LNAM": "S1", "RESTRN": ["7"]}
        north = Polygon([ll(0, 1000), ll(500, 1000), ll(500, 2000), ll(0, 2000)])
        south = Polygon([ll(0, -1000), ll(500, -1000), ll(500, 0), ll(0, 0)])
        recs = self.run(monkeypatch, {"n.000": self.ds(22000, [_Feat(north, a)], cls="RESARE"),
                                      "s.000": self.ds(22000, [_Feat(south, a)], cls="RESARE")})
        assert len(recs) == 1
        r = recs[0]
        assert r["geometry"].contains(north.centroid) and r["geometry"].contains(south.centroid)
        assert set(r["source_cell"].split(",")) == {"n.000", "s.000"}

    def test_lowercase_resare_and_text(self, monkeypatch):
        f = _Feat(box(0, 0, 100, 100), {"LNAM": "R1", "restrn": ["7"], "INFORM": "a", "NINFOM": "b",
                                        "PERSTA": "--0401", "NaNattr": float("nan")})
        recs = self.run(monkeypatch, {"c.000": self.ds(5000, [f], cls="resare")})
        r = recs[0]
        assert r["source_class"] == "RESARE" and r["text"] == "a | b" and r["period_start"] == "--0401"
        assert "NaNattr" not in r["attributes"]


class TestAxisPerEdge:
    """A mesh graph: the edges inside an area join into one web, so the chain as a whole never runs along an axis."""

    def web(self):
        a = rec("RESARE", box(0, 0, 400, 200), RESTRN=[7])
        edges = [("A1", line((-50, 105), (200, 105))), ("A2", line((200, 105), (450, 105))),
                 ("B", line((200, 105), (200, 190)))]
        return a, edges, [line((-100, 100), (500, 100))]

    def sev(self, links):
        return {l.edge_key: l.severity for l in links}

    def test_default_web_stays_blocked(self):
        a, edges, axes = self.web()
        assert self.sev(link_edges([a], edges, axes=axes)) == {"A1": 3, "A2": 3, "B": 3}

    def test_per_edge_downgrades_only_the_edges_along_the_axis(self):
        a, edges, axes = self.web()
        links = link_edges([a], edges, axes=axes, axis_per_edge=True)
        assert self.sev(links) == {"A1": 1, "A2": 1, "B": 3}

    def test_per_edge_without_axes_changes_nothing(self):
        a, edges, _ = self.web()
        assert self.sev(link_edges([a], edges, axis_per_edge=True)) == {"A1": 3, "A2": 3, "B": 3}

    def test_per_edge_graze_still_per_chain(self):
        a = rec("RESARE", box(0, 0, 400, 200), RESTRN=[7])
        edges = [("g", line((-50, 100), (18, 100)))]
        assert link_edges([a], edges, axis_per_edge=True)[0].severity == 1

    def test_reasons_with_option(self):
        a, edges, axes = self.web()
        links = link_edges([a], edges, axes=axes, axis_per_edge=True)
        reasons = hazards.downgrade_reasons([a], edges, links, axes, axis_per_edge=True)
        assert reasons == {("A1", 0): "along charted axis", ("A2", 0): "along charted axis"}
        # a corner graze keeps its own reason
        g = [("g", line((-50, 100), (18, 100)))]
        gl = link_edges([a], g, axes=axes, axis_per_edge=True)
        assert hazards.downgrade_reasons([a], g, gl, axes, axis_per_edge=True) == {("g", 0): "boundary graze"}


# --------------------------------------------------------------------------
# time-aware notices: period vs validity, time-limited tier, text detector, overrides
# --------------------------------------------------------------------------

import logging  # noqa: E402

import pytest  # noqa: E402

WINTER = "Winter Resting/Foraging area from November 1 until May 15"


def made(cls="RESARE", geom=None, **items):
    return hazards._make_record(cls, items, "X.000", 20000, geom or Point(*ll(0, 0)))


def timed_rec(**kw):
    r = rec("RESARE", RESTRN=[7])
    r.update({"period_start": None, "period_end": None, "valid_from": None, "valid_until": None})
    r.update(kw)
    return r


class TestPeriodValiditySplit:
    def test_persta_goes_to_period_datsta_to_validity(self):
        r = made(RESTRN=[7], PERSTA="--1101", PEREND="--0515", DATSTA="20220101", DATEND="20301231")
        assert (r["period_start"], r["period_end"]) == ("--1101", "--0515")
        assert (r["valid_from"], r["valid_until"]) == ("20220101", "20301231")
        assert r["period_source"] == "chart" and r["text_review"] == 0

    def test_datend_alone_does_not_become_a_period(self):
        r = made("CTNARE", INFORM="Krammersluizen: renovations", DATEND="20220425")
        assert r["period_start"] is None and r["period_end"] is None
        assert r["valid_until"] == "20220425" and r["period_source"] == "chart"

    def test_persta_with_year_stays_raw(self):
        assert made(RESTRN=[7], PERSTA="20261101")["period_start"] == "20261101"

    def test_no_time_info(self):
        r = made(RESTRN=[7])
        assert r["period_source"] is None and not any(r[k] for k in ("period_start", "valid_from"))

    def test_notice_row_carries_all_time_fields(self):
        r = made(RESTRN=[7], PERSTA="--1101", PEREND="--0515", DATEND="20301231")
        row = notice_row(r, 3, None)
        assert (row["period_start"], row["period_end"], row["valid_from"], row["valid_until"],
                row["period_source"], row["text_review"]) == ("--1101", "--0515", None, "20301231", "chart", 0)

    def test_ddl_has_the_columns(self):
        import sqlite3
        c = sqlite3.connect(":memory:")
        c.executescript(hazards.NOTICES_DDL)
        cols = {r[1] for r in c.execute("PRAGMA table_info(notices)")}
        assert {"period_start", "period_end", "valid_from", "valid_until", "period_source", "text_review"} <= cols
        assert "ACTIVE" in hazards.NOTICES_DDL and "wraps over New Year" in hazards.NOTICES_DDL


class TestTimeLimitedTier:
    def test_prohibited_with_period_or_validity_is_time_limited_sev3(self):
        for kw in ({"period_start": "--1101"}, {"period_end": "--0515"}, {"valid_until": "20301231"}):
            r = timed_rec(**kw)
            assert hazards.is_time_limited(r)
            assert classify(r, None) == (3, None, "entry prohibited, seasonal")
            assert not hazards.blocks(r, 3)
            assert hazards.edge_reason(r, 3) == "seasonal"

    def test_all_year_prohibited_blocks(self):
        r = timed_rec()
        assert not hazards.is_time_limited(r) and hazards.blocks(r, 3) and hazards.edge_reason(r, 3) is None

    def test_period_on_non_prohibited_is_not_time_limited(self):
        r = rec("RESARE", RESTRN=[8], PERSTA="--0401")
        assert not hazards.is_time_limited(r) and classify(r, None)[0] == 1

    def test_downgrade_reason_wins(self):
        r = timed_rec(period_start="--1101")
        assert hazards.edge_reason(r, 1, "boundary graze") == "boundary graze"

    def test_time_limited_gets_graze_downgrade_like_any_prohibited_area(self):
        r = timed_rec(period_start="--1101", period_end="--0515", geometry=box(0, 0, 200, 100))
        r["severity"] = 3
        links = link_edges([r], [("e", line((-100, 99), (300, 99)))])
        assert links and links[0].downgraded and links[0].severity == 1


class TestUnreviewedText:
    def test_text_review_set_only_without_time_attributes(self):
        assert made(RESTRN=[7], INFORM=WINTER)["text_review"] == 1
        assert made(RESTRN=[7], INFORM=WINTER, PERSTA="--1101")["text_review"] == 0
        assert made(RESTRN=[7], INFORM="Passage prohibited. Very dangerous tidal streams")["text_review"] == 0

    def test_unreviewed_prohibited_stays_blocked_with_reason(self):
        r = made(RESTRN=[7], INFORM=WINTER)
        assert classify(r, None)[0] == 3 and hazards.blocks(r, 3)
        assert hazards.edge_reason(r, 3) == "conditions in chart text, unreviewed"
        assert hazards.edge_reason(r, 1, "along charted axis") == "along charted axis"

    def test_not_flagged_for_wrecks(self):
        assert made("WRECKS", INFORM="Wreck during winter storm 1990", WATLEV=1)["text_review"] == 0


class TestTextMentionsTime:
    @pytest.mark.parametrize("text", [
        WINTER, "Winter resting area", "Zomerseizoen", "Seasonal closure", "Tijdelijk gesloten", "Temporary buoy",
        "Closed during the breeding season", "gesloten van 1 november tot 15 mei", "Closed until further notice",
        "15 mei", "1 November", "01-11", "1/11", "from 1 Nov to 15 Mar", "van 1 april tot 1 oktober",
        "Rustgebied voor vogels", "Foraging area", "closed in May", "May 15", "Sep.", "dec",
    ])
    def test_positive(self, text):
        assert hazards.text_mentions_time({"name": None, "text": text}), text

    @pytest.mark.parametrize("text", [
        "Passage prohibited. Very dangerous tidal streams", "Mussel seed installation | Mosselzaadinvanginstallatie",
        "Vessels may not enter", "Bollen van het Nieuwe Zand", "least depth 5.5 m", "Massagoedhaven (d= 360 m)", "",
        "Natura 2000", "Entry prohibited",
    ])
    def test_negative(self, text):
        assert not hazards.text_mentions_time({"name": None, "text": text}), text

    def test_name_is_searched_too(self):
        assert hazards.text_mentions_time({"name": "Winter area", "text": None})
        assert not hazards.text_mentions_time({"name": None, "text": None})


class TestDates:
    @pytest.mark.parametrize("v", ["--1101", "--0515", "--11", "---15", "20220425", "202204", "2022", None, ""])
    def test_valid(self, v):
        assert hazards.validate_s57_date(v) == (v or None)

    @pytest.mark.parametrize("v", ["--1301", "--1132", "11011", "2022-04-25", "--110", "20221301", "20220432", "abc", 5])
    def test_invalid(self, v):
        with pytest.raises(ValueError):
            hazards.validate_s57_date(v)


class TestOverrides:
    def entry(self, r, **kw):
        e = {"period_start": "--1101", "period_end": "--0515", "valid_from": None, "valid_until": None, "note": "n",
             "text_sha1": hazards.text_sha1(r), "status": "accepted", "proposed_by": "llm", "reviewed_by": "m",
             "reviewed_at": "t"}
        e.update(kw)
        return {str(notice_id(r)): e}

    def test_accepted_override_makes_it_time_limited(self):
        r = made(RESTRN=[7], INFORM=WINTER)
        assert hazards.apply_overrides([r], self.entry(r)) == 1
        assert (r["period_start"], r["period_end"], r["period_source"], r["text_review"]) == \
            ("--1101", "--0515", "text-reviewed", 0)
        assert hazards.is_time_limited(r) and classify(r, None)[2] == "entry prohibited, seasonal"

    def test_applies_before_tiering(self):
        r = made(RESTRN=[7], INFORM=WINTER)
        hazards.apply_overrides([r], self.entry(r))
        hazards.tier_records([r], lambda lon, lat: None)
        assert r["severity"] == 3 and r["reason"] == "entry prohibited, seasonal"

    def test_retiers_an_already_tiered_record(self):
        r = made(RESTRN=[7], INFORM=WINTER)
        hazards.tier_records([r], None)
        assert r["reason"] == "entry/passage prohibited"
        hazards.apply_overrides([r], self.entry(r))
        assert r["reason"] == "entry prohibited, seasonal"

    def test_all_null_accepted_means_reviewed_permanent(self):
        r = made(RESTRN=[7], INFORM=WINTER)
        hazards.apply_overrides([r], self.entry(r, period_start=None, period_end=None))
        assert r["text_review"] == 0 and r["period_source"] is None and not hazards.is_time_limited(r)

    def test_stale_sha1_ignored_with_one_warning(self, caplog):
        r = made(RESTRN=[7], INFORM=WINTER)
        ov = self.entry(r, text_sha1="0" * 40)
        with caplog.at_level(logging.WARNING, logger="hazards"):
            n = hazards.apply_overrides([r, dict(r)], ov)
        assert n == 0 and r["text_review"] == 1 and r["period_start"] is None
        assert len([x for x in caplog.records if "stale" in x.message]) == 1

    def test_missing_sha1_is_stale(self):
        r = made(RESTRN=[7], INFORM=WINTER)
        ov = self.entry(r)
        ov[str(notice_id(r))].pop("text_sha1")
        assert hazards.apply_overrides([r], ov) == 0

    @pytest.mark.parametrize("status", ["proposed", "rejected", None, "weird"])
    def test_only_accepted_applies(self, status):
        r = made(RESTRN=[7], INFORM=WINTER)
        assert hazards.apply_overrides([r], self.entry(r, status=status)) == 0
        assert r["text_review"] == 1 and r["period_start"] is None and r["period_source"] is None

    def test_bad_date_in_accepted_entry_ignored(self, caplog):
        r = made(RESTRN=[7], INFORM=WINTER)
        with caplog.at_level(logging.WARNING, logger="hazards"):
            assert hazards.apply_overrides([r], self.entry(r, period_start="--1301")) == 0
        assert r["text_review"] == 1

    def test_load_and_save_roundtrip(self, tmp_path):
        p = str(tmp_path / "sub" / "o.json")
        assert hazards.load_overrides(p) == {} and hazards.load_overrides(None) == {}
        hazards.save_overrides(p, {"1": {"status": "proposed"}})
        assert hazards.load_overrides(p) == {"1": {"status": "proposed"}}


# --------------------------------------------------------------------------
# review fixes: valid_from alone, provenance, id lookup, text_review scope, explicit overrides path, detector
# --------------------------------------------------------------------------

class TestValidFromAlone:
    def test_valid_from_only_stays_blocked(self):
        r = made(RESTRN=[7], DATSTA="20220425")
        assert r["valid_from"] == "20220425" and r["period_source"] == "chart"
        assert not hazards.is_time_limited(r) and hazards.blocks(r, 3)
        assert classify(r, None) == (3, None, "entry/passage prohibited")
        assert hazards.edge_reason(r, 3) is None
        assert not hazards.is_time_limited(timed_rec(valid_from="20220425"))

    def test_valid_from_with_an_end_or_period_is_time_limited(self):
        assert hazards.is_time_limited(made(RESTRN=[7], DATSTA="20220425", DATEND="20301231"))
        assert hazards.is_time_limited(made(RESTRN=[7], DATSTA="20220425", PERSTA="--1101", PEREND="--0515"))

    def test_text_review_still_flagged_with_valid_from_only(self):
        assert made(RESTRN=[7], DATSTA="20220425", INFORM=WINTER)["text_review"] == 1


class TestOverrideProvenance:
    def test_never_overwrites_chart(self):
        r = made(RESTRN=[7], PERSTA="--0401", PEREND="--0930", INFORM=WINTER)
        ov = {str(notice_id(r)): {"period_start": "--1101", "period_end": "--0515", "text_sha1": hazards.text_sha1(r),
                                  "status": "accepted"}}
        hazards.apply_overrides([r], ov)
        assert r["period_source"] == "chart"

    def test_all_null_keeps_source_and_clears_review(self):
        r = made(RESTRN=[7], INFORM=WINTER)
        ov = {str(notice_id(r)): {"text_sha1": hazards.text_sha1(r), "status": "accepted"}}
        hazards.apply_overrides([r], ov)
        assert r["period_source"] is None and r["text_review"] == 0

    def test_lookup_by_stored_id_without_lnam(self):
        r = made(RESTRN=[7], INFORM=WINTER, geom=box(0, 0, 100, 100))
        assert r["source_ref"] is None
        stored = notice_id(r)
        r["geometry"] = box(0, 0, 100.0001, 100)          # simplified geometry: the hash would differ
        assert notice_id(r) != stored
        r["id"] = stored
        ov = {str(stored): {"period_start": "--1101", "period_end": "--0515", "text_sha1": hazards.text_sha1(r),
                            "status": "accepted"}}
        assert hazards.apply_overrides([r], ov) == 1 and r["period_source"] == "text-reviewed"


class TestTextReviewScope:
    def test_only_prohibited_areas(self):
        assert made(RESTRN=[7], INFORM=WINTER)["text_review"] == 1
        assert made(RESTRN=[8], INFORM=WINTER)["text_review"] == 0
        assert made("CTNARE", INFORM=WINTER)["text_review"] == 0
        assert made(RESTRN=[1], INFORM=WINTER)["text_review"] == 0


class TestResolveOverrides:
    def test_explicit_missing_path_is_an_error(self, tmp_path):
        with pytest.raises(SystemExit, match="file not found"):
            hazards.resolve_overrides(str(tmp_path / "nope.json"))

    def test_default_absent_is_silent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hazards, "DEFAULT_OVERRIDES_PATH", str(tmp_path / "absent.json"))
        assert hazards.resolve_overrides(None) == {}

    def test_default_present_is_loaded_and_explicit_wins(self, tmp_path, monkeypatch):
        d, e = tmp_path / "d.json", tmp_path / "e.json"
        d.write_text('{"1": {}}')
        e.write_text('{"2": {}}')
        monkeypatch.setattr(hazards, "DEFAULT_OVERRIDES_PATH", str(d))
        assert hazards.resolve_overrides(None) == {"1": {}} and hazards.resolve_overrides(str(e)) == {"2": {}}


class TestDetectorAndDatesExtra:
    @pytest.mark.parametrize("text", ["Foerageergebied", "Fourageergebied vogels", "Gesloten tijdens het broedseizoen",
                                      "gedurende de winter", "15 jan", "jan 15", "from Jan 15"])
    def test_positive(self, text):
        assert hazards.text_mentions_time({"name": None, "text": text}), text

    def test_bare_jan_is_a_name(self):
        assert not hazards.text_mentions_time({"name": "Jan de Wit haven", "text": "Jan"})

    @pytest.mark.parametrize("v", ["20230229", "20220431", "--0230", "--0431", "00000101", "0000", "--0132"])
    def test_invalid(self, v):
        with pytest.raises(ValueError):
            hazards.validate_s57_date(v)

    @pytest.mark.parametrize("v", ["20240229", "--0229", "20221231", "---31", "0001"])
    def test_valid(self, v):
        assert hazards.validate_s57_date(v) == v
