"""Synthetic unit tests for derive_channel_axes.py (docs/SPEC-CHANNEL-AXES.md).

All fixtures are synthetic geometry in a local metric frame (or a tiny WGS84 patch
near Zeeland for the end-to-end run) -- no real chart data.
"""
import json
import os

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point, box

import derive_channel_axes as dca
from derive_channel_axes import (
    Anchor,
    CATLAM_PORT,
    CATLAM_PREF_STARBOARD,
    CATLAM_STARBOARD,
    ChannelAxisDeriver,
    Mark,
    Params,
    build_corridor,
    corridor_centerline,
    dedupe_marks,
    center_chain,
    default_half_width_m,
    load_marks,
    order_marks_spatial,
    parse_mark_name,
    polygon_skeleton,
    shoal_normal,
    split_anchors,
    subtract_coverage,
    SPATIAL_KEY,
)


# ----------------------------------------------------------------------------- names

class TestParseMarkName:
    def test_us_buoy(self):
        assert parse_mark_name("Potomac River Channel Buoy 15") == ("potomac river channel", 15, "", "Potomac River Channel")

    def test_us_lighted_buoy_and_suffix(self):
        assert parse_mark_name("Potomac River Channel Lighted Buoy 14A")[:3] == ("potomac river channel", 14, "A")

    def test_us_ice_buoy_same_channel(self):
        assert parse_mark_name("Elk River Channel Lighted Ice Buoy 23")[0] == "elk river channel"

    def test_us_daybeacon_with_letter_suffix(self):
        assert parse_mark_name("Wicomico River Daybeacon 2W")[:3] == ("wicomico river", 2, "W")

    def test_entrance_light_joins_bare_channel(self):
        assert parse_mark_name("Wicomico River Entrance Light 1W")[0] == "wicomico river"

    def test_eu_abbreviation(self):
        assert parse_mark_name("O 12")[:3] == ("o", 12, "")
        assert parse_mark_name("ZOL 5")[:3] == ("zol", 5, "")
        assert parse_mark_name("MAAS-1")[:3] == ("maas", 1, "")

    def test_bare_number_names_form_the_unnamed_key(self):
        assert parse_mark_name("27")[:3] == (dca.BARE_NUMBER_KEY, 27, "")
        assert parse_mark_name("25 C")[:3] == (dca.BARE_NUMBER_KEY, 25, "C")
        assert parse_mark_name("KTG") is None

    def test_unparseable(self):
        assert parse_mark_name("Wicomico River Junction Buoy PW") is None
        assert parse_mark_name(None) is None
        assert parse_mark_name("") is None


# ----------------------------------------------------------------------------- polygons

class TestPolygonSkeleton:
    def test_straight_corridor_gives_one_axis_along_the_middle(self):
        poly = box(0, 0, 2000, 100)
        lines = polygon_skeleton(poly, step_m=10.0, prune_m=150.0, reach_m=350.0)
        assert len(lines) == 1
        line = lines[0]
        assert line.length == pytest.approx(2000, abs=15)
        ys = [c[1] for c in line.coords]
        assert max(abs(y - 50.0) for y in ys) < 3.0
        # ends were extended to the polygon boundary
        assert min(Point(line.coords[0]).distance(poly.boundary),
                   Point(line.coords[-1]).distance(poly.boundary)) < 1.0

    def test_l_shaped_channel_keeps_both_legs(self):
        poly = box(0, 0, 1500, 100).union(box(1400, 0, 1500, 1200))
        lines = polygon_skeleton(poly, step_m=10.0, prune_m=150.0, reach_m=350.0)
        total = sum(ln.length for ln in lines)
        # ~1450 (x-leg to the corner) + ~1150 (y-leg) minus a little at the corner
        assert 2300 < total < 2750
        assert all(poly.buffer(1.0).covers(ln) for ln in lines)

    def test_short_component_is_rejected_as_a_whole(self, tmp_path):
        # a 150 m fairway stub (below --min-length-m 200) yields no axis but a reason
        utm = "EPSG:32631"
        x0, y0 = 550000.0, 5700000.0
        d = tmp_path / "stub"
        d.mkdir()
        gpd.GeoDataFrame(geometry=[box(x0, y0, x0 + 3000, y0 + 3000)], crs=utm).to_crs("EPSG:4326").to_file(
            d / "coastal_water_polygons.geojson", driver="GeoJSON")
        gpd.GeoDataFrame({"OBJNAM": ["Stub"], "src_objl": ["FAIRWY"]},
                         geometry=[box(x0 + 1000, y0 + 1000, x0 + 1150, y0 + 1030)], crs=utm
                         ).to_crs("EPSG:4326").to_file(d / "fairways_polygons.geojson", driver="GeoJSON")
        ChannelAxisDeriver(str(d), str(d), Params()).run()
        axes = gpd.read_file(d / dca.OUTPUT_AXES)
        rejected = gpd.read_file(d / dca.OUTPUT_REJECTED)
        assert len(axes) == 0
        assert list(rejected["reason"]) == ["too_short"]

    def test_side_branch_survives_pruning(self):
        poly = box(0, 0, 2000, 100).union(box(950, 0, 1050, 800))
        lines = polygon_skeleton(poly, step_m=10.0, prune_m=150.0, reach_m=350.0)
        # three legs meeting at a junction: west, east, and the 800 m branch
        assert len(lines) == 3
        assert max(ln.length for ln in lines) > 700


# ----------------------------------------------------------------------------- marks

def _mark(x, y, catlam, num, key="test channel", kind="buoy", cscl=12000, suf=""):
    return Mark(f"Test Channel Buoy {num}{suf}", key, num, suf, catlam, kind, cscl, Point(x, y))


class TestMarkHelpers:
    def test_shoal_normal_port_is_left_of_buoyage_direction(self):
        # travelling +x, a port-hand mark sits to the left (+y); its shoal is further +y
        assert shoal_normal((1.0, 0.0), CATLAM_PORT) == (0.0, 1.0)
        assert shoal_normal((1.0, 0.0), CATLAM_STARBOARD) == (0.0, -1.0)

    def test_dedupe_keeps_largest_scale_copy(self):
        a = _mark(0, 0, CATLAM_PORT, 1, cscl=40000)
        b = _mark(5, 5, CATLAM_PORT, 1, cscl=12000)      # same aid, harbour-scale cell
        c = _mark(900, 0, CATLAM_PORT, 1, cscl=12000)    # same number, far away: distinct
        out = dedupe_marks([a, b, c])
        assert len(out) == 2
        assert b in out and c in out

    def test_center_chain_opposite_pairs_sit_on_the_centre(self):
        seq = []
        for i in range(4):
            seq.append(_mark(i * 300, 100, CATLAM_PORT, 2 * i + 1))
            seq.append(_mark(i * 300, -100, CATLAM_STARBOARD, 2 * i + 2))
        seq.sort(key=lambda m: m.num)
        anchors = center_chain(seq, default_half_width_m(seq))
        assert len(anchors) == 7 and all(a.is_gate for a in anchors)
        assert all(abs(a.pt.y) < 1e-6 for a in anchors)
        assert all(a.half_width == pytest.approx(100.0, abs=1.0) for a in anchors)

    def test_center_chain_single_sided_is_offset_into_the_channel(self):
        seq = [_mark(i * 300, 100, CATLAM_PORT, 2 * i + 1) for i in range(4)]
        anchors = center_chain(seq, default_half_width_m(seq))
        assert len(anchors) == 3 and not any(a.is_gate for a in anchors)
        # port marks, travelling +x: channel is to the right (-y); default half width 75 m
        assert all(a.pt.y == pytest.approx(25.0) for a in anchors)

    def test_center_chain_alternating_wide_river_is_straight(self):
        # marks 3 km apart along, alternating sides 2 km across (Potomac-like)
        seq = [_mark(i * 3000, 1000 if i % 2 == 0 else -1000, CATLAM_STARBOARD if i % 2 == 0 else CATLAM_PORT,
                     i + 10) for i in range(6)]
        anchors = center_chain(seq, default_half_width_m(seq))
        assert all(abs(a.pt.y) < 1e-6 for a in anchors)
        assert all(a.half_width == pytest.approx(1000.0, abs=1.0) for a in anchors)

    def test_split_at_gap_and_turn(self):
        pts = [Point(0, 0), Point(300, 0), Point(600, 0), Point(9000, 0), Point(9300, 0), Point(9000, -50)]
        anchors = [Anchor(p, [], False) for p in pts]
        chains = split_anchors(anchors, max_gap_m=5000.0, max_turn_deg=120.0)
        # gap between 600 and 9000 splits; the ~170-degree reversal at (9300,0) splits
        # again, the turning anchor being shared by both chains
        assert [len(c) for c in chains] == [3, 2, 2]


# ----------------------------------------------------------------------------- corridors

def _straight_chain(port_y=100.0, n=6, spacing=500.0):
    seq = [_mark(i * spacing, port_y, CATLAM_PORT, 2 * i + 1) for i in range(n)]
    return center_chain(seq, default_half_width_m(seq))


def _r_fn(half_width):
    return float(min(max(1.5 * half_width + 50.0, 100.0), 1500.0))


class TestCorridor:
    def test_single_sided_chain_axis_passes_on_channel_side(self):
        water = box(-200, -400, 3000, 400)
        anchors = _straight_chain()
        corridor, naive, radii = build_corridor(anchors, water, _r_fn, wall_buffer_m=30.0)
        assert corridor is not None
        line, reason = corridor_centerline(corridor, anchors, naive, radii, hug=1.0)
        assert reason is None and line is not None
        assert water.covers(line)
        # port marks at y=100 travelling +x -> shoal at y>100, channel at y<100
        ys = [c[1] for c in line.coords]
        assert max(ys) < 100.0
        assert line.length > 1800

    def test_wrong_side_water_is_walled_off(self):
        # water only on the shoal side of the marks: no valid channel side -> disconnected
        water = box(-200, 100, 3000, 400)
        anchors = _straight_chain()
        corridor, naive, radii = build_corridor(anchors, water, _r_fn, wall_buffer_m=30.0)
        if corridor is not None:
            line, reason = corridor_centerline(corridor, anchors, naive, radii, hug=1.0)
            assert line is None and reason == "corridor_disconnected"

    def test_bent_creek_axis_stays_in_water(self):
        # 120 m wide L-shaped creek; beacons on the outer bank; the naive chain would cut the corner
        # travelling +x then turning left to +y: port-hand beacons sit on the north bank
        # of the first leg and on the west bank of the second leg
        creek = box(0, 0, 1000, 120).union(box(880, 0, 1000, 1000))
        seq = [
            _mark(100, 118, CATLAM_PORT, 1, kind="beacon"),
            _mark(500, 118, CATLAM_PORT, 3, kind="beacon"),
            _mark(882, 118, CATLAM_PORT, 5, kind="beacon"),   # inner corner
            _mark(882, 500, CATLAM_PORT, 7, kind="beacon"),
            _mark(882, 900, CATLAM_PORT, 9, kind="beacon"),
        ]
        anchors = center_chain(seq, default_half_width_m(seq))
        chains = split_anchors(anchors, max_gap_m=5000.0, max_turn_deg=120.0)
        assert len(chains) == 1
        corridor, naive, radii = build_corridor(chains[0], creek, _r_fn, wall_buffer_m=10.0)
        line, reason = corridor_centerline(corridor, chains[0], naive, radii, hug=1.0)
        assert reason is None
        assert creek.buffer(0.5).covers(line)
        # the beacons stand on the bank; the axis runs down the middle of the creek
        mids = [line.interpolate(f, normalized=True) for f in (0.2, 0.4, 0.6, 0.8)]
        assert min(m.distance(creek.exterior) for m in mids) > 35.0


class TestDedup:
    def test_subtract_coverage_removes_covered_part(self):
        axis = LineString([(0, 0), (3000, 0)])
        cov = [LineString([(1000, 20), (2000, 20)])]
        parts, removed = subtract_coverage(axis, cov, buffer_m=50.0, min_len_m=200.0)
        assert len(parts) == 2
        assert removed == pytest.approx(1100, abs=15)   # round buffer caps trim a little less

    def test_fully_covered_axis_dropped(self):
        axis = LineString([(0, 0), (1000, 0)])
        parts, removed = subtract_coverage(axis, [axis], buffer_m=50.0, min_len_m=200.0)
        assert parts == [] and removed == pytest.approx(1000)


# ----------------------------------------------------------------------------- end to end

def _to_wgs(gdf_m):
    return gdf_m.to_crs("EPSG:4326")


@pytest.fixture
def synthetic_dir(tmp_path):
    """A 6 km x 3 km water rectangle in UTM 31N near Zeeland with a fairway polygon,
    a buoy chain (incl. an 'Ice' duplicate and a coarse-cell duplicate), land, DEPARE."""
    utm = "EPSG:32631"
    x0, y0 = 550000.0, 5700000.0
    water = box(x0, y0, x0 + 6000, y0 + 3000)
    land = box(x0, y0 + 3000, x0 + 6000, y0 + 3500)
    depare = [
        (2.0, box(x0, y0, x0 + 6000, y0 + 3000)),
        (5.0, box(x0, y0 + 800, x0 + 6000, y0 + 2200)),
    ]
    fairway = box(x0 + 200, y0 + 2400, x0 + 2200, y0 + 2500)   # 2 km x 100 m strip
    marks = []
    for i in range(8):
        x = x0 + 2500 + i * 400
        marks.append(("Test Channel Buoy %d" % (2 * i + 1), CATLAM_PORT, 12000, Point(x, y0 + 1650)))
        marks.append(("Test Channel Buoy %d" % (2 * i + 2), CATLAM_STARBOARD, 12000, Point(x, y0 + 1350)))
    marks.append(("Test Channel Lighted Ice Buoy 3", CATLAM_PORT, 12000, Point(x0 + 2900 + 5, y0 + 1650)))
    marks.append(("Test Channel Buoy 5", CATLAM_PORT, 90000, Point(x0 + 3300 + 20, y0 + 1650 - 10)))
    marks.append(("Lonely Buoy 1", CATLAM_PORT, 12000, Point(x0 + 500, y0 + 500)))
    marks.append(("Lonely Buoy 3", CATLAM_PORT, 12000, Point(x0 + 700, y0 + 500)))   # 2 marks: rejected, recorded
    d = tmp_path / "layers"
    d.mkdir()
    _to_wgs(gpd.GeoDataFrame({"DRVAL1": [2.0]}, geometry=[water], crs=utm)).to_file(d / "coastal_water_polygons.geojson", driver="GeoJSON")
    _to_wgs(gpd.GeoDataFrame(geometry=[land], crs=utm)).to_file(d / "land_polygons.geojson", driver="GeoJSON")
    _to_wgs(gpd.GeoDataFrame({"DRVAL1": [v for v, _ in depare]}, geometry=[g for _, g in depare], crs=utm)).to_file(d / "depare_polygons.geojson", driver="GeoJSON")
    _to_wgs(gpd.GeoDataFrame({"OBJNAM": ["Test Fairway"], "src_objl": ["FAIRWY"], "src_cscl": [12000]},
                             geometry=[fairway], crs=utm)).to_file(d / "fairways_polygons.geojson", driver="GeoJSON")
    _to_wgs(gpd.GeoDataFrame({"OBJNAM": [m[0] for m in marks], "CATLAM": [m[1] for m in marks],
                              "src_objl": ["BOYLAT"] * len(marks), "src_cscl": [m[2] for m in marks]},
                             geometry=[m[3] for m in marks], crs=utm)).to_file(d / "lateral_marks_points.geojson", driver="GeoJSON")
    return d


class TestEndToEnd:
    def test_run_writes_axes_rejected_and_stats(self, synthetic_dir):
        out = synthetic_dir / "out"
        ChannelAxisDeriver(str(synthetic_dir), str(out), Params()).run()
        axes = gpd.read_file(out / dca.OUTPUT_AXES)
        assert set(axes["axis_kind"]) == {"polygon_centerline", "mark_chain"}
        poly_axes = axes[axes["axis_kind"] == "polygon_centerline"]
        chain_axes = axes[axes["axis_kind"] == "mark_chain"]
        assert len(poly_axes) == 1 and 1900 < poly_axes["length_m"].iloc[0] < 2100
        assert len(chain_axes) == 1
        row = chain_axes.iloc[0]
        assert row["n_marks"] == 16           # ice + coarse-cell duplicates removed
        assert row["n_gates"] == 15          # every consecutive pair is opposite-hand
        assert row["confidence"] >= 0.8
        assert row["depth_checked"]
        assert row["tier"] == 3
        # gates sit at y0+1500, i.e. squarely inside the 5 m DEPARE band
        assert row["depth_median_m"] == pytest.approx(5.0)
        rejected = gpd.read_file(out / dca.OUTPUT_REJECTED)
        assert list(rejected["reason"]) == ["too_few_marks"]
        assert rejected["channel_name"].iloc[0] == "Lonely"
        stats = json.load(open(out / dca.OUTPUT_STATS))
        assert stats["output"]["axes"] == 2
        assert stats["marks"]["deduped"] == 18
        assert stats["parity_convention"].startswith("odd=port")

    def test_min_confidence_filters_output(self, synthetic_dir):
        out = synthetic_dir / "out2"
        ChannelAxisDeriver(str(synthetic_dir), str(out), Params(min_confidence=0.85)).run()
        axes = gpd.read_file(out / dca.OUTPUT_AXES)
        assert set(axes["axis_kind"]) == {"polygon_centerline"}

    def test_missing_layers_still_run(self, tmp_path, synthetic_dir):
        # only the fairway polygon and water: tier 3 has nothing, output still valid
        d = tmp_path / "poly_only"
        d.mkdir()
        for f in ("coastal_water_polygons.geojson", "fairways_polygons.geojson"):
            (d / f).write_bytes((synthetic_dir / f).read_bytes())
        ChannelAxisDeriver(str(d), str(d), Params()).run()
        axes = gpd.read_file(d / dca.OUTPUT_AXES)
        assert list(axes["axis_kind"]) == ["polygon_centerline"]
        assert os.path.exists(d / dca.OUTPUT_REJECTED)


# ----------------------------------------------------------------------------- spatial fallback

def _mark_chain_dir(tmp_path, subdir, names):
    """8 alternating-hand mark pairs, 400 m apart, water only (no fairway/depare, so
    tier 2 yields nothing and the chain-level confidence isolates the tier 3 path)."""
    utm = "EPSG:32631"
    x0, y0 = 550000.0, 5700000.0
    water = box(x0, y0, x0 + 6000, y0 + 3000)
    marks = []
    for i in range(8):
        x = x0 + 2500 + i * 400
        marks.append((names[2 * i], CATLAM_PORT, Point(x, y0 + 1650)))
        marks.append((names[2 * i + 1], CATLAM_STARBOARD, Point(x, y0 + 1350)))
    d = tmp_path / subdir
    d.mkdir()
    _to_wgs(gpd.GeoDataFrame({"DRVAL1": [2.0]}, geometry=[water], crs=utm)).to_file(
        d / "coastal_water_polygons.geojson", driver="GeoJSON")
    _to_wgs(gpd.GeoDataFrame({"OBJNAM": [m[0] for m in marks], "CATLAM": [m[1] for m in marks],
                              "src_objl": ["BOYLAT"] * len(marks), "src_cscl": [12000] * len(marks)},
                             geometry=[m[2] for m in marks], crs=utm)).to_file(
        d / "lateral_marks_points.geojson", driver="GeoJSON")
    return d


# No trailing digits anywhere -- guaranteed to fail both _US_MARK_RE (needs a keyword
# + number) and _EU_MARK_RE (needs a mandatory trailing number), unlike "O 12" or "Q1".
_GARBAGE_NAMES = ["Radar Reflector", "Racon Site", "Historic Wreck", "Sunken Barge",
                  "Old Piling", "Rock Ledge", "Reef Marker", "Shoal Post",
                  "Ruin Stake", "Ballast Heap", "Ghost Light", "Broken Spar",
                  "Mud Bank Post", "Weed Bed Stub", "Timber Snag", "Coral Head"]
_BARE_NAMES = [str(n) for n in range(1, 17)]  # odd=port, even=starboard, per the loop above


class TestSpatialChainingFallback:
    def test_unparseable_names_are_chained_not_dropped(self, tmp_path):
        d = _mark_chain_dir(tmp_path, "spatial", _GARBAGE_NAMES)
        ChannelAxisDeriver(str(d), str(d), Params()).run()
        axes = gpd.read_file(d / dca.OUTPUT_AXES)
        assert list(axes["axis_kind"]) == ["mark_chain"]
        assert axes.iloc[0]["n_marks"] == 16
        stats = json.load(open(d / dca.OUTPUT_STATS))
        assert stats["marks"]["unparsed_name"] == 16
        assert stats["marks"]["spatial_fallback"] == 16

    def test_spatial_chain_gets_a_distinct_channel_key(self, tmp_path):
        d = _mark_chain_dir(tmp_path, "spatial_key", _GARBAGE_NAMES)
        ChannelAxisDeriver(str(d), str(d), Params()).run()
        axes = gpd.read_file(d / dca.OUTPUT_AXES)
        assert axes.iloc[0]["channel_name"] == SPATIAL_KEY
        assert SPATIAL_KEY != dca.BARE_NUMBER_KEY

    def test_spatial_chain_confidence_lower_than_bare_number_chain(self, tmp_path):
        spatial_dir = _mark_chain_dir(tmp_path, "spatial_conf", _GARBAGE_NAMES)
        bare_dir = _mark_chain_dir(tmp_path, "bare_conf", _BARE_NAMES)
        ChannelAxisDeriver(str(spatial_dir), str(spatial_dir), Params()).run()
        ChannelAxisDeriver(str(bare_dir), str(bare_dir), Params()).run()
        spatial_conf = gpd.read_file(spatial_dir / dca.OUTPUT_AXES).iloc[0]["confidence"]
        bare_conf = gpd.read_file(bare_dir / dca.OUTPUT_AXES).iloc[0]["confidence"]
        assert spatial_conf < bare_conf

    def test_order_marks_spatial_walks_nearest_neighbour(self):
        # SPATIAL_KEY marks have no (number, suffix) to sort by; ordering must come
        # from geometry alone, via nearest-neighbour chaining from a PCA-picked end.
        pts = [(0, 0), (1200, 5), (400, -5), (800, 0)]
        marks = [_mark(x, y, CATLAM_PORT, 0, key=SPATIAL_KEY) for x, y in pts]
        ordered = order_marks_spatial(marks)
        xs = [m.pt.x for m in ordered]
        assert xs == sorted(xs)

    def test_safe_water_marks_are_excluded_from_the_spatial_fallback(self):
        # safe-water marks carry no CATLAM/hand -- they aren't lateral-channel
        # evidence and must not enter SPATIAL_KEY chaining, only lateral marks
        # (buoy/beacon) with an unparseable name should.
        lateral = gpd.GeoDataFrame(
            {"OBJNAM": ["Radar Reflector"], "CATLAM": [CATLAM_PORT], "src_objl": ["BOYLAT"], "src_cscl": [12000]},
            geometry=[Point(0, 0)])
        safe = gpd.GeoDataFrame(
            {"OBJNAM": ["Historic Wreck"], "src_objl": ["BOYSAW"], "src_cscl": [12000]},
            geometry=[Point(10, 10)])
        marks, unparsed, unnamed = load_marks(lateral, safe)
        assert unparsed == 2  # both names failed parsing and are counted
        assert unnamed == 0
        assert [m.name for m in marks] == ["Radar Reflector"]  # only the lateral one is chainable
        assert marks[0].key == SPATIAL_KEY

    def test_marks_without_a_usable_catlam_are_excluded_from_the_spatial_fallback(self):
        # a lateral-kind mark with a missing or junction-only CATLAM (not plain
        # port/starboard) carries no hand evidence -- same rationale as excluding
        # safe-water marks -- and must not be retained under SPATIAL_KEY either.
        lateral = gpd.GeoDataFrame(
            {"OBJNAM": ["Radar Reflector", "Racon Site", "Historic Wreck"],
             "CATLAM": [None, CATLAM_PREF_STARBOARD, CATLAM_PORT],
             "src_objl": ["BOYLAT"] * 3, "src_cscl": [12000] * 3},
            geometry=[Point(0, 0), Point(1, 1), Point(2, 2)])
        safe = gpd.GeoDataFrame(geometry=[])
        marks, unparsed, unnamed = load_marks(lateral, safe)
        assert unparsed == 3  # all three names failed parsing and are counted
        assert [m.name for m in marks] == ["Historic Wreck"]  # only the port/starboard one chains

    def test_dedupe_keeps_distinct_named_spatial_marks_close_together(self):
        # all SPATIAL_KEY marks share the placeholder (SPATIAL_KEY, 0, "") identity;
        # dedupe must not collapse two different, unrelated aids just because
        # they happen to sit within tol_m of each other (e.g. opposite banks of
        # a narrow channel) -- only genuine same-name duplicates should merge.
        a = Mark("Rock Ledge", SPATIAL_KEY, 0, "", CATLAM_PORT, "buoy", 12000, Point(0, 0))
        b = Mark("Reef Marker", SPATIAL_KEY, 0, "", CATLAM_STARBOARD, "buoy", 12000, Point(30, 0))
        out = dedupe_marks([a, b], tol_m=60.0)
        assert len(out) == 2

    def test_dedupe_keeps_opposite_hand_marks_distinct_even_with_the_same_name(self):
        # the generic descriptive names this fallback exists for can recur on
        # both banks of the same channel; a shared name alone must not collapse
        # two marks of opposite hand into one.
        a = Mark("Radar Reflector", SPATIAL_KEY, 0, "", CATLAM_PORT, "buoy", 12000, Point(0, 0))
        b = Mark("Radar Reflector", SPATIAL_KEY, 0, "", CATLAM_STARBOARD, "buoy", 12000, Point(30, 0))
        out = dedupe_marks([a, b], tol_m=60.0)
        assert len(out) == 2

    def test_dedupe_still_merges_same_named_spatial_marks(self):
        # the same real aid charted twice (overlapping cell coverage) shares a name
        # and must still collapse to one, same as the parsed-name path does.
        a = Mark("Rock Ledge", SPATIAL_KEY, 0, "", CATLAM_PORT, "buoy", 40000, Point(0, 0))
        b = Mark("Rock Ledge", SPATIAL_KEY, 0, "", CATLAM_PORT, "buoy", 12000, Point(5, 5))
        out = dedupe_marks([a, b], tol_m=60.0)
        assert out == [b]  # smaller src_cscl (larger-scale cell) wins

    def test_reliable_direction_false_skips_the_same_hand_offset(self):
        # a walk order with no relation to true buoyage direction must not guess
        # a side: same-hand pairs stay at the raw midpoint instead of being offset.
        seq = [_mark(i * 300, 100, CATLAM_PORT, 2 * i + 1) for i in range(4)]
        anchors = center_chain(seq, default_half_width_m(seq), reliable_direction=False)
        assert all(a.pt.y == pytest.approx(100.0) for a in anchors)

    def test_reliable_direction_false_skips_the_shoal_wall(self):
        # mirrors TestCorridor.test_wrong_side_water_is_walled_off: that test's
        # water sits only on the "wrong" (shoal) side of a reliable chain, and the
        # wall there cuts it off entirely, leaving no valid corridor. With an
        # unreliable direction, no wall is cut, so a route through that same
        # water becomes possible again.
        water = box(-200, 100, 3000, 400)
        anchors = _straight_chain()
        reliable, _, _ = build_corridor(anchors, water, _r_fn, wall_buffer_m=30.0)
        unreliable, naive, radii = build_corridor(anchors, water, _r_fn, wall_buffer_m=30.0,
                                                   reliable_direction=False)
        assert unreliable is not None
        if reliable is not None:
            assert unreliable.area > reliable.area
        line, reason = corridor_centerline(unreliable, anchors, naive, radii, hug=1.0)
        assert reason is None and line is not None

    def test_all_same_hand_spatial_chain_is_rejected_not_emitted_off_centre(self, tmp_path):
        # no opposite-hand pair anywhere in this cluster -> reliable_direction=False
        # leaves every anchor on the raw (port-only) mark line itself, which is a
        # channel edge, not a centre; must be rejected, not emitted as an axis.
        utm = "EPSG:32631"
        x0, y0 = 550000.0, 5700000.0
        water = box(x0, y0, x0 + 6000, y0 + 300)
        pts = [Point(x0 + 2500 + i * 400, y0 + 150) for i in range(4)]
        d = tmp_path / "spatial_no_gates"
        d.mkdir()
        _to_wgs(gpd.GeoDataFrame({"DRVAL1": [2.0]}, geometry=[water], crs=utm)).to_file(
            d / "coastal_water_polygons.geojson", driver="GeoJSON")
        _to_wgs(gpd.GeoDataFrame({"OBJNAM": _GARBAGE_NAMES[:4], "CATLAM": [CATLAM_PORT] * 4,
                                  "src_objl": ["BOYLAT"] * 4, "src_cscl": [12000] * 4},
                                 geometry=pts, crs=utm)).to_file(
            d / "lateral_marks_points.geojson", driver="GeoJSON")
        ChannelAxisDeriver(str(d), str(d), Params()).run()
        axes = gpd.read_file(d / dca.OUTPUT_AXES)
        assert len(axes) == 0
        stats = json.load(open(d / dca.OUTPUT_STATS))
        assert stats["tier3"]["reasons"].get("no_centre_evidence") == 1
        assert stats["tier3"]["rejected"] == 1  # n_rej must count this rejection too

    def test_single_gate_at_one_end_is_not_enough_centre_evidence(self, tmp_path):
        # a walk that follows one bank for most of its length and only crosses to
        # the other bank once, at the far end, has exactly one gate -- not zero --
        # but the rest of the chain would still be edge-hugging same-hand anchors.
        # A single gate must not be enough; gates need to be the majority.
        utm = "EPSG:32631"
        x0, y0 = 550000.0, 5700000.0
        water = box(x0, y0, x0 + 6000, y0 + 300)
        # 6 port marks along one bank, then 1 starboard mark at the far end: the
        # nearest-neighbour walk follows the port bank in order and only gates once,
        # at the final port->starboard step.
        pts = [(Point(x0 + 2500 + i * 400, y0 + 150), CATLAM_PORT) for i in range(6)]
        pts.append((Point(x0 + 2500 + 6 * 400, y0 + 200), CATLAM_STARBOARD))
        d = tmp_path / "spatial_one_gate"
        d.mkdir()
        _to_wgs(gpd.GeoDataFrame({"DRVAL1": [2.0]}, geometry=[water], crs=utm)).to_file(
            d / "coastal_water_polygons.geojson", driver="GeoJSON")
        _to_wgs(gpd.GeoDataFrame({"OBJNAM": _GARBAGE_NAMES[:7], "CATLAM": [c for _, c in pts],
                                  "src_objl": ["BOYLAT"] * 7, "src_cscl": [12000] * 7},
                                 geometry=[p for p, _ in pts], crs=utm)).to_file(
            d / "lateral_marks_points.geojson", driver="GeoJSON")
        ChannelAxisDeriver(str(d), str(d), Params()).run()
        axes = gpd.read_file(d / dca.OUTPUT_AXES)
        assert len(axes) == 0
        stats = json.load(open(d / dca.OUTPUT_STATS))
        assert stats["tier3"]["reasons"].get("no_centre_evidence") == 1

    def test_parsed_and_bare_number_paths_unaffected(self, synthetic_dir):
        # regression: the existing named-channel path in the shared fixture (which
        # also contains no unparseable names) is untouched by the new SPATIAL_KEY branch
        out = synthetic_dir / "out_regress"
        ChannelAxisDeriver(str(synthetic_dir), str(out), Params()).run()
        axes = gpd.read_file(out / dca.OUTPUT_AXES)
        chain_axes = axes[axes["axis_kind"] == "mark_chain"]
        assert len(chain_axes) == 1
        assert chain_axes.iloc[0]["channel_name"] == "Test Channel"
        assert chain_axes.iloc[0]["confidence"] >= 0.8
        stats = json.load(open(out / dca.OUTPUT_STATS))
        assert stats["marks"]["unparsed_name"] == 0
        assert stats["marks"]["spatial_fallback"] == 0


class TestCli:
    def test_rejects_bad_params(self):
        with pytest.raises(SystemExit):
            dca.main(["--input-dir", "/nonexistent", "--max-mark-gap-m", "-1"])
        with pytest.raises(SystemExit):
            dca.main(["--input-dir", "/nonexistent", "--corridor-min-r-m", "2000", "--corridor-max-r-m", "100"])
        with pytest.raises(SystemExit):
            dca.main(["--input-dir", "/nonexistent", "--min-confidence", "1.5"])
