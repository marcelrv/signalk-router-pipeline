"""Unit tests for the sagitta-bounded adaptive resampler (SPEC-GRAPH-DENSITY.md §4.1).

`_resample_long_skeleton_edges` used to split every centerline unconditionally at
`max_segment_m` regardless of curvature -- the direct cause of the 90-110m
coastal-centerline edge-length spike covering 48.3% of all such edges (§2.2). This
replaces that cut with a split rule that closes a segment when EITHER its sagitta
(max chord-to-centerline deviation) would exceed a width-coupled tolerance, OR
`max_segment_m` is reached (still a hard backstop -- §4.1, §7).

`max_chord_sagitta_m` defaults to 0.0 (disabled): nothing here changes real build
output until a build explicitly opts in via `--sagitta-cap`.
"""
import math

from pyproj import Geod

from nautical_routing_pipeline import (
    NauticalRoutingPipeline,
    WIDTH_UNKNOWN_M,
    _chain_has_known_width,
    _max_sagitta_m,
    _sagitta_tolerance_m,
)

GEOD = Geod(ellps="WGS84")
ORIGIN_LON, ORIGIN_LAT = 4.10, 51.50  # arbitrary point, Zeeland-ish
STEP_M = 5.0  # roughly pixel_max_m -- the pipeline's medial-axis raster ceiling


def _pipeline():
    # __init__ only assigns attributes (see nautical_routing_pipeline.py) -- no file
    # I/O -- so this is safe to build directly for a unit test with no real GeoJSON.
    return NauticalRoutingPipeline(data_paths={}, db_path=":memory:")


def _geodesic_run(lon0, lat0, bearing_deg, length_m, step_m=STEP_M):
    """Points every step_m along a real geodesic ray -- genuinely straight, so
    _max_sagitta_m (equirectangular about the segment start) measures ~0 on it."""
    n = max(2, int(length_m // step_m) + 1)
    pts = []
    for k in range(n):
        d = min(k * step_m, length_m)
        lon, lat, _ = GEOD.fwd(lon0, lat0, bearing_deg, d)
        pts.append((lon, lat))
    return pts


def _bend_chain(leg_m=300.0, step_m=STEP_M):
    """A right-angle bend: due east, then due north, each leg sampled every step_m.
    The corner sits far off the start->end chord, so any sagitta-bounded resampler
    with a tolerance well under leg_m must close a segment somewhere in the second
    leg rather than spanning a single chord straight across the bend."""
    leg1 = _geodesic_run(ORIGIN_LON, ORIGIN_LAT, 90.0, leg_m, step_m)
    corner_lon, corner_lat = leg1[-1]
    leg2 = _geodesic_run(corner_lon, corner_lat, 0.0, leg_m, step_m)
    return leg1 + leg2[1:]  # corner point is shared, not duplicated


def _legacy_uniform_cut(pipeline, pts, widths, max_segment_m):
    """The exact pre-§4.1 algorithm: accumulate geodesic arc length and cut
    unconditionally at max_segment_m, ignoring curvature entirely."""
    expected = []
    seg_start = 0
    acc = 0.0
    for i in range(1, len(pts)):
        _, _, d = pipeline.geod.inv(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1])
        acc += d
        if acc >= max_segment_m or i == len(pts) - 1:
            expected.append((pts[seg_start:i + 1], widths[seg_start:i + 1]))
            seg_start = i
            acc = 0.0
    return expected


class TestDisabledByDefaultIsUnchanged:
    def test_bend_chain_matches_legacy_uniform_cut(self):
        pipeline = _pipeline()
        pts = _bend_chain()
        widths = [50.0] * len(pts)
        max_segment_m = 100.0

        got = list(pipeline._resample_long_skeleton_edges(pts, widths, max_segment_m))
        expected = _legacy_uniform_cut(pipeline, pts, widths, max_segment_m)

        assert got == expected

    def test_omitting_sagitta_args_matches_explicit_zero_cap(self):
        # 0.0 is the CLI's --sagitta-cap default -- omitting the kwargs entirely
        # (every pre-existing call site before this change) must behave identically.
        pipeline = _pipeline()
        pts = _bend_chain()
        widths = [50.0] * len(pts)

        default_call = list(pipeline._resample_long_skeleton_edges(pts, widths, 100.0))
        explicit_disabled = list(
            pipeline._resample_long_skeleton_edges(pts, widths, 100.0, 0.0, 0.5))

        assert default_call == explicit_disabled


class TestStraightChainCollapses:
    def test_far_fewer_segments_than_todays_uniform_cut(self):
        pipeline = _pipeline()
        pts = _geodesic_run(ORIGIN_LON, ORIGIN_LAT, 45.0, length_m=600.0)
        widths = [400.0] * len(pts)  # wide channel: the cap binds, not the width

        today = list(pipeline._resample_long_skeleton_edges(pts, widths, 100.0))
        relaxed = list(pipeline._resample_long_skeleton_edges(
            pts, widths, max_segment_m=1000.0,
            max_chord_sagitta_m=150.0, sagitta_width_fraction=0.5))

        assert len(today) >= 6  # ~600m / 100m backstop -- today's 90-110m spike
        assert len(relaxed) == 1  # a straight reach collapses to one long edge
        assert relaxed[0][0][0] == pts[0]
        assert relaxed[0][0][-1] == pts[-1]


class TestCurvedChainKeepsDensityAndRespectsTolerance:
    def test_bend_produces_more_segments_than_an_equally_long_straight_run(self):
        pipeline = _pipeline()
        cap, frac, width, max_segment_m = 40.0, 0.5, 200.0, 1000.0

        straight_pts = _geodesic_run(ORIGIN_LON, ORIGIN_LAT, 90.0, length_m=600.0)
        bend_pts = _bend_chain(leg_m=300.0)

        straight_segments = list(pipeline._resample_long_skeleton_edges(
            straight_pts, [width] * len(straight_pts), max_segment_m, cap, frac))
        bend_segments = list(pipeline._resample_long_skeleton_edges(
            bend_pts, [width] * len(bend_pts), max_segment_m, cap, frac))

        assert len(straight_segments) == 1
        assert len(bend_segments) > 1

    def test_every_emitted_segment_sagitta_is_within_its_tolerance(self):
        pipeline = _pipeline()
        cap, frac = 40.0, 0.5
        pts = _bend_chain(leg_m=300.0)
        widths = [200.0] * len(pts)

        segments = list(pipeline._resample_long_skeleton_edges(
            pts, widths, max_segment_m=1000.0,
            max_chord_sagitta_m=cap, sagitta_width_fraction=frac))

        assert len(segments) > 1  # the bend must force at least one early close
        for sub_pts, sub_widths in segments:
            tol = _sagitta_tolerance_m(sub_widths, cap, frac)
            assert _max_sagitta_m(sub_pts) <= tol + 1e-6


class TestWidthCoupledTolerance:
    def test_narrow_channel_gets_more_segments_than_wide_for_the_same_wiggle(self):
        pipeline = _pipeline()
        # A gentle wiggle: a straight run with one point nudged sideways ~3m --
        # enough to trip a 2m (narrow-channel) tolerance but not a 100m (wide) one.
        pts = _geodesic_run(ORIGIN_LON, ORIGIN_LAT, 90.0, length_m=200.0)
        mid = len(pts) // 2
        lon, lat, _ = GEOD.fwd(pts[mid][0], pts[mid][1], 0.0, 3.0)  # nudge north 3m
        pts[mid] = (lon, lat)

        narrow_widths = [4.0] * len(pts)   # tol = min(50, 0.5*4)   = 2m  -> trips
        wide_widths = [400.0] * len(pts)   # tol = min(50, 0.5*400) = 50m -> doesn't

        narrow = list(pipeline._resample_long_skeleton_edges(
            pts, narrow_widths, max_segment_m=1000.0,
            max_chord_sagitta_m=50.0, sagitta_width_fraction=0.5))
        wide = list(pipeline._resample_long_skeleton_edges(
            pts, wide_widths, max_segment_m=1000.0,
            max_chord_sagitta_m=50.0, sagitta_width_fraction=0.5))

        assert len(wide) == 1
        assert len(narrow) > len(wide)

    def test_tolerance_helper_is_capped_by_the_flat_cap_not_only_width(self):
        assert _sagitta_tolerance_m([1000.0, 1000.0], cap_m=75.0, width_fraction=0.5) == 75.0

    def test_tolerance_helper_is_bound_by_width_under_the_cap(self):
        assert _sagitta_tolerance_m([10.0, 10.0], cap_m=75.0, width_fraction=0.5) == 5.0


class TestMissingOrSentinelWidthFallsBackToUniform:
    def test_none_width_disables_relaxation_for_the_whole_chain(self):
        pipeline = _pipeline()
        pts = _bend_chain()
        widths = [50.0] * len(pts)
        widths[len(widths) // 2] = None  # one unmeasured vertex taints the whole chain

        relaxed = list(pipeline._resample_long_skeleton_edges(
            pts, widths, max_segment_m=100.0,
            max_chord_sagitta_m=150.0, sagitta_width_fraction=0.5))
        disabled = list(pipeline._resample_long_skeleton_edges(pts, widths, 100.0))

        assert relaxed == disabled

    def test_999_sentinel_width_disables_relaxation_for_the_whole_chain(self):
        pipeline = _pipeline()
        pts = _bend_chain()
        widths = [50.0] * len(pts)
        widths[0] = WIDTH_UNKNOWN_M

        relaxed = list(pipeline._resample_long_skeleton_edges(
            pts, widths, max_segment_m=100.0,
            max_chord_sagitta_m=150.0, sagitta_width_fraction=0.5))
        disabled = list(pipeline._resample_long_skeleton_edges(pts, widths, 100.0))

        assert relaxed == disabled

    def test_chain_has_known_width_helper(self):
        assert _chain_has_known_width([10.0, 20.0, 30.0])
        assert not _chain_has_known_width([10.0, WIDTH_UNKNOWN_M, 30.0])
        assert not _chain_has_known_width([10.0, None, 30.0])
        assert not _chain_has_known_width([10.0, 0.0, 30.0])
        assert not _chain_has_known_width([10.0, float("nan"), 30.0])
        assert not _chain_has_known_width([])


class TestMaxSegmentBackstopStillCaps:
    def test_backstop_caps_length_even_with_a_generous_sagitta_cap(self):
        pipeline = _pipeline()
        pts = _geodesic_run(ORIGIN_LON, ORIGIN_LAT, 45.0, length_m=500.0)
        widths = [1000.0] * len(pts)  # very wide: the width never binds
        max_segment_m = 40.0

        segments = list(pipeline._resample_long_skeleton_edges(
            pts, widths, max_segment_m=max_segment_m,
            max_chord_sagitta_m=200.0, sagitta_width_fraction=0.5))

        assert len(segments) > 1
        for sub_pts, _ in segments[:-1]:  # exclude the tail, which may be short
            length = sum(
                pipeline.geod.inv(sub_pts[i - 1][0], sub_pts[i - 1][1],
                                   sub_pts[i][0], sub_pts[i][1])[2]
                for i in range(1, len(sub_pts)))
            # The backstop closes on the same hop that crosses max_segment_m, so
            # allow one raster step of slack over the cap (plus float noise).
            assert length < max_segment_m + STEP_M + 1e-6


class TestMaxSagittaMHelper:
    def test_zero_on_a_straight_chord(self):
        # A genuine geodesic straight line still isn't perfectly straight under the
        # equirectangular projection (§4.1 constraint 4 accepts this as negligible
        # over a <=max_segment_m span) -- assert sub-millimetre, not exactly zero.
        pts = _geodesic_run(ORIGIN_LON, ORIGIN_LAT, 90.0, length_m=100.0)
        assert _max_sagitta_m(pts) < 1e-3

    def test_matches_a_known_right_triangle_deviation(self):
        # Local metric plane: (0,0) -> (10,0) -> (10,10). The corner's perpendicular
        # distance from the chord (0,0)->(10,10) is 10/sqrt(2).
        lat0 = 51.5
        m_per_deg_lat = 111320.0
        m_per_deg_lon = 111320.0 * math.cos(math.radians(lat0))
        p0 = (ORIGIN_LON, lat0)
        p1 = (ORIGIN_LON + 10.0 / m_per_deg_lon, lat0)
        p2 = (ORIGIN_LON + 10.0 / m_per_deg_lon, lat0 + 10.0 / m_per_deg_lat)
        got = _max_sagitta_m([p0, p1, p2])
        assert abs(got - 10.0 / math.sqrt(2.0)) < 0.01
