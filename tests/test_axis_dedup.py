"""Unit tests for width-coupled axis dedup (SPEC-GRAPH-DENSITY.md §4.3).

`build_skeleton_network` used to rasterize and skeletonize `coastal_water` with no
awareness that `_build_inland_network` had already ingested the SAME channel from an
authoritative `inland_waterways` axis line (wtwaxs/RECTRC/NAVLNE) -- generating a
redundant medial-axis "twin" a few metres off the real centerline (the Krammersluizen
screenshot that motivated this spec section). `_axis_dedup_suppression_mask` carves
water pixels within a width-coupled tolerance of a nearby axis line out of the mask
BEFORE skeletonizing, so the twin is never generated (not generated-then-pruned).

`axis_dedup_cap_m` defaults to 0.0 (disabled): nothing here changes real build output
until a build explicitly opts in via `--axis-dedup-cap`, exactly like `--sagitta-cap`.
"""
import numpy as np
import geopandas as gpd
from rasterio.transform import from_origin
from shapely.geometry import LineString, Point, box

from nautical_routing_pipeline import (
    ClassificationConfig,
    NauticalRoutingPipeline,
    _candidates_by_bounds_static,
    _lonlat_margin_deg,
    WATERWAY_CONNECTOR_MAX_M,
)

# A UTM zone-31N patch near Zeeland (3.7-3.75E, 51.44-51.45N) -- estimate_utm_crs()
# on this extent returns exactly EPSG:32631, confirmed directly against pyproj.
UTM_CRS = "EPSG:32631"
PX_M = 5.0
ROWS, COLS = 100, 200          # 500m (N-S) x 1000m (E-W) raster at 5m pixels
EASTING0 = 550000.0            # west edge
NORTHING0_TOP = 5700250.0      # north (top-row) edge


def _transform(px=PX_M):
    return from_origin(EASTING0, NORTHING0_TOP, px, px)


def _pipeline(axis_dedup_cap_m=50.0, axis_dedup_fraction=0.5, axis_dedup_floor_m=5.0):
    # __init__ only assigns attributes (no file I/O) -- safe to build directly, same
    # pattern as tests/test_sagitta_resample.py's _pipeline().
    p = NauticalRoutingPipeline(data_paths={}, db_path=":memory:")
    p.classification_config = ClassificationConfig(
        axis_dedup_cap_m=axis_dedup_cap_m,
        axis_dedup_fraction=axis_dedup_fraction,
        axis_dedup_floor_m=axis_dedup_floor_m,
    )
    # Normally set at the top of build_network(); tests that call node-creation
    # methods (_get_or_create_node, build_navmesh_region, ...) directly, without
    # going through build_network itself, need it initialized the same way.
    p.coords_to_node = {}
    return p


def _channel_mask(row_lo, row_hi, rows=ROWS, cols=COLS):
    """A straight east-west channel spanning the full raster width, rows [row_lo, row_hi)."""
    mask = np.zeros((rows, cols), dtype=bool)
    mask[row_lo:row_hi, :] = True
    return mask


def _axis_line_wgs84(transform, row, col_lo=0, col_hi=COLS):
    """A straight east-west line at pixel row `row`, spanning [col_lo, col_hi)."""
    x0, y = transform * (col_lo + 0.5, row + 0.5)
    x1, _ = transform * (col_hi - 0.5, row + 0.5)
    line_utm = LineString([(x0, y), (x1, y)])
    return gpd.GeoSeries([line_utm], crs=UTM_CRS).to_crs("EPSG:4326").iloc[0]


def _polygon_wgs84_covering_raster(transform, rows=ROWS, cols=COLS):
    x0, y0 = transform * (0, 0)
    x1, y1 = transform * (cols, rows)
    poly_utm = box(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
    return gpd.GeoSeries([poly_utm], crs=UTM_CRS).to_crs("EPSG:4326").iloc[0]


def _inland_gdf(*lines_wgs84):
    return gpd.GeoDataFrame(geometry=list(lines_wgs84), crs="EPSG:4326")


class TestCarvesWhenEnabled:
    def test_pixels_near_a_nearby_axis_line_are_suppressed(self):
        # A 200m-wide channel (rows 30:70) with an axis line running right down its
        # middle (row 49) -- the textbook "generated twin" case the spec targets.
        transform = _transform()
        mask = _channel_mask(30, 70)
        pipeline = _pipeline(axis_dedup_cap_m=50.0)
        pipeline.gdfs["inland_waterways"] = _inland_gdf(_axis_line_wgs84(transform, 49))
        polygon = _polygon_wgs84_covering_raster(transform)

        suppress, _ = pipeline._axis_dedup_suppression_mask(mask, transform, UTM_CRS, PX_M, polygon)

        assert suppress.shape == mask.shape
        assert suppress.dtype == bool
        # Right on the axis: suppressed.
        assert suppress[49, 100]
        # A channel-interior pixel close to the axis (well within any plausible
        # tolerance): suppressed.
        assert suppress[45, 100]
        # Far from the axis line, near the channel's own bank (~100m away, beyond the
        # cap=50m): NOT suppressed.
        assert not suppress[31, 100]
        assert not suppress[68, 100]

    def test_carved_mask_has_fewer_water_pixels_than_the_original(self):
        transform = _transform()
        mask = _channel_mask(30, 70)
        pipeline = _pipeline(axis_dedup_cap_m=50.0)
        pipeline.gdfs["inland_waterways"] = _inland_gdf(_axis_line_wgs84(transform, 49))
        polygon = _polygon_wgs84_covering_raster(transform)

        suppress, _ = pipeline._axis_dedup_suppression_mask(mask, transform, UTM_CRS, PX_M, polygon)
        carved = mask & ~suppress

        assert int(carved.sum()) < int(mask.sum())
        assert int(carved.sum()) > 0  # bank strips survive -- not everything is wiped out


class TestToleranceScalesWithLocalWidth:
    def test_narrow_channel_gets_a_tighter_tolerance_than_a_wide_one(self):
        # Wide channel: 200m tall (rows 30:70) -> away from its own banks, local width
        # stays large enough that fraction*width exceeds the 50m cap, so the cap binds:
        # tol = 50m uniformly through most of the channel's interior.
        wide_transform = _transform()
        wide_mask = _channel_mask(30, 70)
        wide_pipeline = _pipeline(axis_dedup_cap_m=50.0, axis_dedup_fraction=0.5, axis_dedup_floor_m=5.0)
        wide_pipeline.gdfs["inland_waterways"] = _inland_gdf(_axis_line_wgs84(wide_transform, 49))
        wide_polygon = _polygon_wgs84_covering_raster(wide_transform)
        wide_suppress, _ = wide_pipeline._axis_dedup_suppression_mask(
            wide_mask, wide_transform, UTM_CRS, PX_M, wide_polygon)

        # Narrow channel: 40m tall (rows 46:54) -> local width near the centre tops
        # out around 40m, so fraction*width (~20m) stays under the cap -- the
        # width-coupled fraction binds instead of the flat 50m cap.
        narrow_transform = _transform()
        narrow_mask = _channel_mask(46, 54)
        narrow_pipeline = _pipeline(axis_dedup_cap_m=50.0, axis_dedup_fraction=0.5, axis_dedup_floor_m=5.0)
        narrow_pipeline.gdfs["inland_waterways"] = _inland_gdf(_axis_line_wgs84(narrow_transform, 49))
        narrow_polygon = _polygon_wgs84_covering_raster(narrow_transform)
        narrow_suppress, _ = narrow_pipeline._axis_dedup_suppression_mask(
            narrow_mask, narrow_transform, UTM_CRS, PX_M, narrow_polygon)

        # Right next to the axis, both are suppressed regardless of width.
        assert wide_suppress[48, 100] and narrow_suppress[48, 100]
        # Far from the axis (20 rows = 100m, well past even the 50m cap): neither is.
        assert not wide_suppress[29, 100] and not narrow_suppress[29, 100]
        # The actual comparison the width coupling predicts: total suppression reach
        # (rows suppressed in a column through the axis) is strictly larger for the
        # wide (cap-bound, 50m) channel than for the narrow (fraction-bound, ~20m) one.
        wide_reach = int(wide_suppress[:, 100].sum())
        narrow_reach = int(narrow_suppress[:, 100].sum())
        assert wide_reach > narrow_reach


class TestFloorIsRespected:
    def test_never_suppresses_below_the_5m_floor(self):
        # A very narrow channel where fraction*width falls BELOW the 5m floor at the
        # band's own edge rows if the floor weren't applied -- use 1m pixels so a
        # 6m-tall band (rows 47:53) is representable. width_est at rows 47/52 (the
        # band's top/bottom edge, closest to the water/land boundary) is ~2m ->
        # fraction*width = 1m, but the floor must clip that up to 5m. Both rows
        # checked are ON the water mask -- CodeRabbit PR #14 review finding 2 fixed
        # `_axis_dedup_suppression_mask` to only ever return water pixels, so an
        # off-mask assertion can no longer observe this (see the off-mask test below).
        px = 1.0
        transform = _transform(px=px)
        mask = _channel_mask(47, 53, rows=ROWS, cols=COLS)  # 6 rows tall @ 1m px = 6m
        pipeline = _pipeline(axis_dedup_cap_m=50.0, axis_dedup_fraction=0.5, axis_dedup_floor_m=5.0)
        pipeline.gdfs["inland_waterways"] = _inland_gdf(_axis_line_wgs84(transform, 49, col_hi=COLS))
        polygon = _polygon_wgs84_covering_raster(transform)

        suppress, _ = pipeline._axis_dedup_suppression_mask(mask, transform, UTM_CRS, px, polygon)

        # Row 47 (band's top edge, on-mask): axis_dist=2m from the row-49 axis line.
        # Without the floor, tol there would be fraction*width=1m (2m > 1m -> NOT
        # suppressed); with the floor, tol=5m (2m < 5m -> suppressed).
        assert suppress[47, 100]
        # Row 52 (band's bottom edge, on-mask): axis_dist=3m, same floor-dependent story.
        assert suppress[52, 100]

    def test_off_mask_pixels_are_never_suppressed_regardless_of_floor(self):
        # CodeRabbit PR #14 review finding 2: `_axis_dedup_suppression_mask` computes
        # axis_dist_m/tol_m over the WHOLE raster grid (so the floor alone would flag
        # a land pixel close enough to the axis line, even with zero local width), but
        # the function must only ever return water pixels -- callers compute a
        # suppression RATE against mask.sum(), and the carve itself (mask & ~suppress)
        # only cares about water pixels anyway. Confirms the fix: a pixel well within
        # the floor's own reach of the axis row, but off the water mask, is not
        # suppressed.
        px = 1.0
        transform = _transform(px=px)
        mask = _channel_mask(47, 53, rows=ROWS, cols=COLS)
        pipeline = _pipeline(axis_dedup_cap_m=50.0, axis_dedup_fraction=0.5, axis_dedup_floor_m=5.0)
        pipeline.gdfs["inland_waterways"] = _inland_gdf(_axis_line_wgs84(transform, 49, col_hi=COLS))
        polygon = _polygon_wgs84_covering_raster(transform)

        suppress, _ = pipeline._axis_dedup_suppression_mask(mask, transform, UTM_CRS, px, polygon)

        # Row 45 is off-mask (band is rows 47:53) but only 4m from the axis row --
        # well within the 5m floor's reach -- yet must not be suppressed.
        assert not suppress[45, 5]
        assert not mask[45, 5]  # sanity: this pixel really is off the water mask


class TestNoProximityLeavesMaskUntouched:
    def test_bbox_prefilter_rejects_a_far_away_line(self):
        # The candidate axis line's bbox doesn't fall anywhere near this piece's
        # extent (+ cap margin) -- the cheap bbox prefilter should reject it before
        # any rasterize/distance-transform work, returning all-False directly.
        transform = _transform()
        mask = _channel_mask(30, 70)
        pipeline = _pipeline(axis_dedup_cap_m=50.0)
        far_line = LineString([(10.0, 55.0), (10.1, 55.0)])  # nowhere near Zeeland
        pipeline.gdfs["inland_waterways"] = _inland_gdf(far_line)
        polygon = _polygon_wgs84_covering_raster(transform)

        suppress, _ = pipeline._axis_dedup_suppression_mask(mask, transform, UTM_CRS, PX_M, polygon)

        assert not suppress.any()

    def test_line_within_bbox_but_geometrically_too_far_leaves_channel_untouched(self):
        # The axis line survives the bbox prefilter (it's inside this piece's own
        # extent) but sits genuinely far from the water body -- more than the 50m cap
        # from every water pixel -- so no water pixel should be suppressed.
        transform = _transform()
        mask = _channel_mask(30, 70)  # channel occupies rows 30-69
        pipeline = _pipeline(axis_dedup_cap_m=50.0)
        # Row 95 is (95 - 69) * 5m = 130m from the nearest channel row -- well past
        # the 50m cap.
        pipeline.gdfs["inland_waterways"] = _inland_gdf(_axis_line_wgs84(transform, 95))
        polygon = _polygon_wgs84_covering_raster(transform)

        suppress, _ = pipeline._axis_dedup_suppression_mask(mask, transform, UTM_CRS, PX_M, polygon)

        assert not (mask & suppress).any()

    def test_no_inland_waterways_layer_at_all(self):
        transform = _transform()
        mask = _channel_mask(30, 70)
        pipeline = _pipeline(axis_dedup_cap_m=50.0)
        # pipeline.gdfs has no "inland_waterways" key at all (never loaded).
        polygon = _polygon_wgs84_covering_raster(transform)

        suppress, _ = pipeline._axis_dedup_suppression_mask(mask, transform, UTM_CRS, PX_M, polygon)

        assert not suppress.any()

    def test_empty_inland_waterways_layer(self):
        transform = _transform()
        mask = _channel_mask(30, 70)
        pipeline = _pipeline(axis_dedup_cap_m=50.0)
        pipeline.gdfs["inland_waterways"] = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
        polygon = _polygon_wgs84_covering_raster(transform)

        suppress, _ = pipeline._axis_dedup_suppression_mask(mask, transform, UTM_CRS, PX_M, polygon)

        assert not suppress.any()


class TestDisabledByDefaultReproducesUnchangedSkeletonOutput:
    def _rig(self, monkeypatch, mask, transform, px, poison_line_row):
        """Wires build_skeleton_network to a fixed synthetic (mask, transform, px)
        via _rasterize_water_polygon, and captures the mask _extract_medial_axis_skeleton
        actually receives via a spy that short-circuits real medial-axis work (returns
        an empty skeleton so build_skeleton_network returns right after, without needing
        a real downstream graph build)."""
        pipeline = _pipeline(axis_dedup_cap_m=0.0)
        polygon = _polygon_wgs84_covering_raster(transform)
        # A "poisoning" axis line placed EXACTLY down the mask's own medial axis --
        # if axis-dedup's gating were broken, this would suppress most of the mask
        # even at cap=0.0. This is the load-bearing part of the test: not merely that
        # the code path is skipped, but that a real would-suppress input has zero
        # effect while disabled.
        pipeline.gdfs["inland_waterways"] = _inland_gdf(_axis_line_wgs84(transform, poison_line_row))

        monkeypatch.setattr(pipeline, "_rasterize_water_polygon",
                             lambda *a, **kw: (mask, transform, px))

        captured = {}

        def _spy_extract(m):
            captured["mask"] = m.copy()
            return np.zeros_like(m, dtype=bool), np.zeros(m.shape, dtype=float)

        monkeypatch.setattr(type(pipeline), "_extract_medial_axis_skeleton", staticmethod(_spy_extract))

        pipeline.build_skeleton_network(polygon)
        return captured["mask"]

    def test_cap_zero_mask_is_byte_identical_to_the_raw_rasterized_mask(self, monkeypatch):
        transform = _transform()
        mask = _channel_mask(30, 70)

        captured_mask = self._rig(monkeypatch, mask, transform, PX_M, poison_line_row=49)

        assert np.array_equal(captured_mask, mask)

    def test_enabling_the_same_poisoning_line_actually_changes_the_mask(self, monkeypatch):
        # Companion check: the line placed in the fixture above is not inert by
        # construction -- with axis-dedup enabled it demonstrably carves the mask,
        # confirming the cap-0.0 test above is exercising a real gate, not a no-op setup.
        transform = _transform()
        mask = _channel_mask(30, 70)
        pipeline = _pipeline(axis_dedup_cap_m=50.0)
        polygon = _polygon_wgs84_covering_raster(transform)
        pipeline.gdfs["inland_waterways"] = _inland_gdf(_axis_line_wgs84(transform, 49))

        monkeypatch.setattr(pipeline, "_rasterize_water_polygon",
                             lambda *a, **kw: (mask, transform, PX_M))
        captured = {}

        def _spy_extract(m):
            captured["mask"] = m.copy()
            return np.zeros_like(m, dtype=bool), np.zeros(m.shape, dtype=float)

        monkeypatch.setattr(type(pipeline), "_extract_medial_axis_skeleton", staticmethod(_spy_extract))

        pipeline.build_skeleton_network(polygon)

        assert not np.array_equal(captured["mask"], mask)
        assert int(captured["mask"].sum()) < int(mask.sum())
        assert pipeline.axis_dedup_stats["pieces_processed"] == 1
        assert pipeline.axis_dedup_stats["pieces_with_suppression"] == 1
        assert pipeline.axis_dedup_stats["suppressed_px"] > 0

    def test_axis_dedup_stats_stay_zero_when_disabled(self, monkeypatch):
        transform = _transform()
        mask = _channel_mask(30, 70)

        pipeline = _pipeline(axis_dedup_cap_m=0.0)
        polygon = _polygon_wgs84_covering_raster(transform)
        pipeline.gdfs["inland_waterways"] = _inland_gdf(_axis_line_wgs84(transform, 49))
        monkeypatch.setattr(pipeline, "_rasterize_water_polygon",
                             lambda *a, **kw: (mask, transform, PX_M))
        monkeypatch.setattr(type(pipeline), "_extract_medial_axis_skeleton",
                             staticmethod(lambda m: (np.zeros_like(m, dtype=bool), np.zeros(m.shape, dtype=float))))

        pipeline.build_skeleton_network(polygon)

        assert pipeline.axis_dedup_stats == {
            "pieces_processed": 0, "pieces_with_suppression": 0,
            "suppressed_px": 0, "total_water_px": 0,
        }


class TestLockPolygonsAreNeverSuppressed:
    """Krammersluizen fix: `_add_lock_crossing_edges` derives its chamber entry/exit
    points from where an inland_waterways line crosses a LOCK POLYGON's own boundary,
    then hooks a quadrant search onto whatever coastal graph nodes already exist nearby.
    It runs after the skeleton is built, so if axis-dedup suppresses the coastal
    centerline inside/near a lock -- exactly where an authoritative axis line is
    expected to run, since that's the whole point of a lock -- there may be nothing left
    for it to connect to. Verified directly on data/zeeland_live_clip (Krammersluizen):
    the node axis-dedup removed sat at 0.0m from the lock polygon's own boundary, and
    POI-pair reachability went from 0 lost pairs to 278/10,878 (100% through
    Krammersluizen) before this fix, back to 0 after.
    """

    def test_suppression_is_cancelled_inside_a_lock_polygon(self):
        # Same wide-channel-with-axis-down-the-middle setup as TestCarvesWhenEnabled,
        # but now a lock polygon covers the axis line's row -- suppression there must
        # be cancelled even though the axis line is right on top of it.
        transform = _transform()
        mask = _channel_mask(30, 70)
        pipeline = _pipeline(axis_dedup_cap_m=50.0)
        pipeline.gdfs["inland_waterways"] = _inland_gdf(_axis_line_wgs84(transform, 49))
        polygon = _polygon_wgs84_covering_raster(transform)

        # A lock polygon covering a band around the axis (rows 44:56 x full width),
        # expressed in WGS84 the same way the coastal-water/inland-waterways layers are.
        x0, y0 = transform * (0, 44)
        x1, y1 = transform * (COLS, 56)
        lock_poly_utm = box(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        lock_poly_wgs84 = gpd.GeoSeries([lock_poly_utm], crs=UTM_CRS).to_crs("EPSG:4326").iloc[0]
        pipeline.gdfs["locks"] = gpd.GeoDataFrame(
            geometry=[lock_poly_wgs84], data={"OBJNAM": ["Test Lock"]}, crs="EPSG:4326")

        suppress_with_lock, _ = pipeline._axis_dedup_suppression_mask(
            mask, transform, UTM_CRS, PX_M, polygon)

        # Without the lock layer, the same setup suppresses right on the axis (already
        # covered by TestCarvesWhenEnabled, re-asserted here as the control).
        pipeline_no_lock = _pipeline(axis_dedup_cap_m=50.0)
        pipeline_no_lock.gdfs["inland_waterways"] = _inland_gdf(_axis_line_wgs84(transform, 49))
        suppress_without_lock, _ = pipeline_no_lock._axis_dedup_suppression_mask(
            mask, transform, UTM_CRS, PX_M, polygon)

        assert suppress_without_lock[49, 100]  # control: normally suppressed
        assert not suppress_with_lock[49, 100]  # protected: lock polygon cancels it
        # Well outside the lock polygon's buffered extent (row 5, far north of the
        # lock band and the axis both), suppression is unaffected either way -- both
        # False, since it's far from the axis line regardless of the lock.
        assert not suppress_with_lock[5, 100]
        assert not suppress_without_lock[5, 100]

    def test_protection_buffer_extends_past_the_lock_polygons_own_footprint(self):
        # The protection buffer is axis_dedup_cap_m beyond the lock polygon's own
        # extent (not just its interior) -- a pixel just outside the lock polygon but
        # within the cap distance of it must also be protected, since that's exactly
        # the "just outside the gate" zone _add_lock_crossing_edges's quadrant search
        # needs real nodes in.
        transform = _transform()
        mask = _channel_mask(30, 70)
        pipeline = _pipeline(axis_dedup_cap_m=50.0)
        pipeline.gdfs["inland_waterways"] = _inland_gdf(_axis_line_wgs84(transform, 49))
        polygon = _polygon_wgs84_covering_raster(transform)

        # A small lock polygon confined to a few columns around the axis (cols 90:110),
        # not spanning this row's full suppression reach -- rows 44:56.
        x0, y0 = transform * (90, 44)
        x1, y1 = transform * (110, 56)
        lock_poly_utm = box(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        lock_poly_wgs84 = gpd.GeoSeries([lock_poly_utm], crs=UTM_CRS).to_crs("EPSG:4326").iloc[0]
        pipeline.gdfs["locks"] = gpd.GeoDataFrame(
            geometry=[lock_poly_wgs84], data={"OBJNAM": ["Test Lock"]}, crs="EPSG:4326")

        suppress, _ = pipeline._axis_dedup_suppression_mask(mask, transform, UTM_CRS, PX_M, polygon)

        # Column 100 sits inside the lock polygon's own column range (90:110): protected.
        assert not suppress[49, 100]
        # Column 199 (the raster's last column) is ~445m from the lock polygon's own
        # edge (col 110) in the SAME row -- far past any cap-sized buffer -- so
        # ordinary suppression applies there.
        assert suppress[49, 199]

    def test_no_locks_layer_leaves_suppression_unaffected(self):
        transform = _transform()
        mask = _channel_mask(30, 70)
        pipeline = _pipeline(axis_dedup_cap_m=50.0)
        pipeline.gdfs["inland_waterways"] = _inland_gdf(_axis_line_wgs84(transform, 49))
        polygon = _polygon_wgs84_covering_raster(transform)
        # pipeline.gdfs has no "locks" key at all.

        suppress, _ = pipeline._axis_dedup_suppression_mask(mask, transform, UTM_CRS, PX_M, polygon)

        assert suppress[49, 100]  # unaffected -- suppression proceeds normally

    def test_empty_locks_layer_leaves_suppression_unaffected(self):
        transform = _transform()
        mask = _channel_mask(30, 70)
        pipeline = _pipeline(axis_dedup_cap_m=50.0)
        pipeline.gdfs["inland_waterways"] = _inland_gdf(_axis_line_wgs84(transform, 49))
        pipeline.gdfs["locks"] = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
        polygon = _polygon_wgs84_covering_raster(transform)

        suppress, _ = pipeline._axis_dedup_suppression_mask(mask, transform, UTM_CRS, PX_M, polygon)

        assert suppress[49, 100]

    def test_far_away_lock_polygon_does_not_protect_this_piece(self):
        transform = _transform()
        mask = _channel_mask(30, 70)
        pipeline = _pipeline(axis_dedup_cap_m=50.0)
        pipeline.gdfs["inland_waterways"] = _inland_gdf(_axis_line_wgs84(transform, 49))
        polygon = _polygon_wgs84_covering_raster(transform)
        far_lock = box(10.0, 55.0, 10.01, 55.01)  # nowhere near Zeeland
        pipeline.gdfs["locks"] = gpd.GeoDataFrame(
            geometry=[far_lock], data={"OBJNAM": ["Far Lock"]}, crs="EPSG:4326")

        suppress, _ = pipeline._axis_dedup_suppression_mask(mask, transform, UTM_CRS, PX_M, polygon)

        assert suppress[49, 100]  # unaffected by a lock nowhere near this piece


class TestBboxPrefilterMarginIsLatitudeAware:
    """CodeRabbit PR #14 review finding 1: `_candidates_by_bounds_static` applied one
    scalar margin to both the lat and lon axes of a bbox. Both callers here convert a
    metre margin via `metres / 111320.0` -- correct for latitude everywhere, but
    longitude shrinks by cos(latitude), so the same converted value under-covers the
    east-west search radius (~0.62x at Zeeland's 51.45N). `_lock_protection_mask` is
    where this is actually observable: it buffers a candidate LOCK polygon by
    `axis_dedup_cap_m` AFTER the bbox prefilter, so a lock whose true (unbuffered)
    position is beyond the buggy margin but within a correct one is excluded before
    buffering ever gets a chance to reach back into the piece.
    """

    def test_symmetric_margin_excludes_a_lock_the_correct_margin_includes(self):
        # Reproduces the reviewer's suggested fixture directly against
        # _candidates_by_bounds_static: a lock polygon 40m east of the piece's own
        # bbox, at Zeeland's latitude (~51.45N, cos~0.623). The old single-margin
        # metres/111320.0 conversion gives an effective east-west reach of only
        # ~31m (50 * 0.623) -- short of 40m, so the candidate is wrongly excluded.
        # _lonlat_margin_deg's longitude-aware conversion reaches the full 50m.
        transform = _transform()
        lock_utm = box(EASTING0 + COLS * PX_M + 40.0, NORTHING0_TOP - 56 * PX_M,
                        EASTING0 + COLS * PX_M + 45.0, NORTHING0_TOP - 44 * PX_M)
        lock_wgs84 = gpd.GeoSeries([lock_utm], crs=UTM_CRS).to_crs("EPSG:4326").iloc[0]
        locks_gdf = gpd.GeoDataFrame(geometry=[lock_wgs84], crs="EPSG:4326")
        polygon = _polygon_wgs84_covering_raster(transform)

        from nautical_routing_pipeline import _candidates_by_bounds_static, _lonlat_margin_deg

        old_buggy_margin_deg = 50.0 / 111320.0
        old_candidates = _candidates_by_bounds_static(locks_gdf, polygon, margin=old_buggy_margin_deg)
        assert old_candidates.empty  # confirms the bug really would have missed it

        margin_lon_deg, margin_lat_deg = _lonlat_margin_deg(polygon, 50.0)
        new_candidates = _candidates_by_bounds_static(
            locks_gdf, polygon, margin=margin_lat_deg, margin_lon=margin_lon_deg)
        assert len(new_candidates) == 1

    def test_lock_protection_reaches_a_lock_offset_east_of_the_piece(self):
        # End-to-end version through the real code path: without the fix, this lock
        # (40m east of the piece, buffered by the 50m cap to reach 10m into the
        # piece's own east edge) would never even be considered, and the axis-dedup
        # suppression at the piece's east edge would go unprotected -- fails before
        # the fix, passes after.
        transform = _transform()
        mask = _channel_mask(30, 70)
        pipeline = _pipeline(axis_dedup_cap_m=50.0)
        pipeline.gdfs["inland_waterways"] = _inland_gdf(_axis_line_wgs84(transform, 49))
        lock_utm = box(EASTING0 + COLS * PX_M + 40.0, NORTHING0_TOP - 56 * PX_M,
                        EASTING0 + COLS * PX_M + 45.0, NORTHING0_TOP - 44 * PX_M)
        lock_wgs84 = gpd.GeoSeries([lock_utm], crs=UTM_CRS).to_crs("EPSG:4326").iloc[0]
        pipeline.gdfs["locks"] = gpd.GeoDataFrame(
            geometry=[lock_wgs84], data={"OBJNAM": ["Test Lock East"]}, crs="EPSG:4326")
        polygon = _polygon_wgs84_covering_raster(transform)

        suppress, _ = pipeline._axis_dedup_suppression_mask(mask, transform, UTM_CRS, PX_M, polygon)

        # The lock's 50m buffer reaches ~10m into the piece's east edge (last two
        # columns) -- protected, even though the axis line itself still runs the full
        # width at row 49.
        assert not suppress[49, 198]
        assert not suppress[49, 199]
        # Far from the lock, ordinary suppression is unaffected.
        assert suppress[49, 100]


class TestNavmeshPieceCarving:
    """SPEC-OVERRIDE-ZONES.md §7 follow-up: build_navmesh_region's PSLG/triangle path
    never got axis-dedup treatment -- confirmed as a live gap against the deployed
    Zeeland database (node 509788242410608, Oosterschelde approach, ~9-14m from a real
    inland_waterways line, well inside the 50m cap, never suppressed because nothing
    upstream of build_navmesh_region ever asked). `_axis_dedup_carve_navmesh_pieces`
    bridges the raster suppression logic to the vector-polygon world navmesh pieces
    live in, before they become PSLG/triangulate() input.
    """

    NAVMESH_UTM_CRS = "EPSG:32631"

    @staticmethod
    def _wgs84(geom_utm, crs=None):
        return gpd.GeoSeries([geom_utm], crs=crs or TestNavmeshPieceCarving.NAVMESH_UTM_CRS).to_crs("EPSG:4326").iloc[0]

    def _wide_piece(self):
        # 2km x 1km rectangle -- big enough that local channel width near its centre
        # (~1000m) is far past the 50m cap, so the cap binds throughout most of it,
        # same regime real navmesh (min_navmesh_radius_m=800m disk) pieces are in.
        return box(500000.0, 5700000.0, 502000.0, 5701000.0)

    def _through_line_utm(self):
        # Runs the full width of the piece (and beyond, like a real river), straight
        # through the middle at y=500500 -- exactly the "axis running down the middle
        # of an open piece" case.
        return LineString([(499900.0, 5700500.0), (502100.0, 5700500.0)])

    def test_disabled_by_default_returns_the_polygon_unchanged(self):
        pipeline = _pipeline(axis_dedup_cap_m=0.0)
        piece = self._wide_piece()
        line_wgs84 = self._wgs84(self._through_line_utm())
        pipeline.gdfs["inland_waterways"] = gpd.GeoDataFrame(geometry=[line_wgs84], crs="EPSG:4326")

        result, _, _ = pipeline._axis_dedup_carve_navmesh_pieces(piece, self.NAVMESH_UTM_CRS)

        assert len(result) == 1
        assert result[0].equals(piece)

    def test_no_inland_waterways_layer_is_a_true_no_op(self, monkeypatch):
        pipeline = _pipeline(axis_dedup_cap_m=50.0)
        piece = self._wide_piece()
        # No self.gdfs["inland_waterways"] at all.

        def _fail_if_called(*a, **kw):
            raise AssertionError("_rasterize_water_polygon must not be called with no candidates nearby")
        monkeypatch.setattr(pipeline, "_rasterize_water_polygon", _fail_if_called)

        result, _, _ = pipeline._axis_dedup_carve_navmesh_pieces(piece, self.NAVMESH_UTM_CRS)

        assert len(result) == 1
        assert result[0].equals(piece)

    def test_far_away_line_is_a_true_no_op_no_rasterization(self, monkeypatch):
        pipeline = _pipeline(axis_dedup_cap_m=50.0)
        piece = self._wide_piece()
        far_line_wgs84 = self._wgs84(LineString([(10.0, 55.0), (10.1, 55.0)]), )
        pipeline.gdfs["inland_waterways"] = gpd.GeoDataFrame(geometry=[far_line_wgs84], crs="EPSG:4326")

        def _fail_if_called(*a, **kw):
            raise AssertionError("_rasterize_water_polygon must not be called -- bbox prefilter should reject this")
        monkeypatch.setattr(pipeline, "_rasterize_water_polygon", _fail_if_called)

        result, _, _ = pipeline._axis_dedup_carve_navmesh_pieces(piece, self.NAVMESH_UTM_CRS)

        assert len(result) == 1
        assert result[0].equals(piece)

    def test_axis_line_through_the_middle_carves_and_fragments_the_piece(self):
        pipeline = _pipeline(axis_dedup_cap_m=50.0, axis_dedup_fraction=0.5, axis_dedup_floor_m=5.0)
        piece = self._wide_piece()
        line_wgs84 = self._wgs84(self._through_line_utm())
        pipeline.gdfs["inland_waterways"] = gpd.GeoDataFrame(geometry=[line_wgs84], crs="EPSG:4326")

        result, _, _ = pipeline._axis_dedup_carve_navmesh_pieces(piece, self.NAVMESH_UTM_CRS)

        # The ~100m-wide suppressed band down the middle splits the 1km-tall piece
        # into two fragments -- no water silently dropped, both halves preserved as
        # separate polygons for the caller to triangulate independently.
        assert len(result) == 2
        total_area = sum(p.area for p in result)
        assert total_area < piece.area  # something was actually carved out
        assert total_area > 0.85 * piece.area  # but not the whole piece -- only a
        # ~100m band (suppression is now <= tol, inclusive of the boundary pixel --
        # see the tolerance comparison's own fix -- so the band is a touch over 100m)
        # out of a 1000m-tall piece, so at most ~15% should be gone.
        # Every returned fragment must be a genuine sub-piece of the original.
        for p in result:
            assert piece.buffer(1e-6).contains(p)

    def test_no_suppression_when_line_is_outside_true_tolerance_but_inside_bbox_margin(self):
        # A line whose bbox falls within the piece's own bbox+margin (so it survives
        # the cheap prefilter and rasterization does happen), but that never actually
        # comes within any pixel's real per-pixel tolerance. Confirms the prefilter is
        # only a cheap first pass, not the actual suppression decision.
        #
        # CodeRabbit PR #14 review round 3 finding 2: the original version of this test
        # placed the line 400m north of the piece's own north edge (y=5701000) against a
        # 50m cap -- outside even the bbox+margin prefilter, so it never actually
        # exercised "candidates non-empty, real suppression still empty" at all.
        #
        # SPEC-GRAPH-DENSITY.md §6.3.2: a later version placed the line 40m due north of
        # the piece's north edge -- but once the padding fix landed (rasterizing onto a
        # grid padded by the cap, not just the piece's own unpadded grid), a line 40m
        # north IS now correctly within the 50m cap of the piece's own edge pixels and
        # gets carved -- see TestAllZeroAxisRasterDoesNotPhantomSuppress's sibling case
        # for that scenario. This test now needs a placement the *rectangular*
        # bbox+margin prefilter accepts (survives on x AND y margin independently) but
        # whose true Euclidean distance from the piece's own nearest corner exceeds the
        # cap -- i.e. diagonally past the NE corner by (45m, 45m), each axis within the
        # ~50m margin/pad reach, but Euclidean distance sqrt(45^2+45^2)=~63.6m > 50m cap.
        pipeline = _pipeline(axis_dedup_cap_m=50.0)
        piece = self._wide_piece()
        far_but_bbox_overlapping = LineString([(502045.0, 5701045.0), (502545.0, 5701045.0)])
        line_wgs84 = self._wgs84(far_but_bbox_overlapping)
        pipeline.gdfs["inland_waterways"] = gpd.GeoDataFrame(geometry=[line_wgs84], crs="EPSG:4326")

        # Verify this test actually exercises its intended branch: the bbox prefilter
        # must find this candidate (not reject it before rasterization even runs).
        polygon_wgs84 = self._wgs84(piece)
        margin_lon_deg, margin_lat_deg = _lonlat_margin_deg(polygon_wgs84, pipeline.classification_config.axis_dedup_cap_m)
        candidates = _candidates_by_bounds_static(pipeline.gdfs["inland_waterways"], polygon_wgs84,
                                                   margin=margin_lat_deg, margin_lon=margin_lon_deg)
        assert len(candidates) == 1, "test setup bug: the line must survive the bbox prefilter"

        result, _, _ = pipeline._axis_dedup_carve_navmesh_pieces(piece, self.NAVMESH_UTM_CRS)

        assert len(result) == 1
        assert result[0].equals(piece)

    def test_full_consumption_returns_empty_list(self):
        # A sliver piece where even the FLOOR tolerance (5m) reaches every point --
        # the farthest any point in an 8m-tall piece can be from a centreline axis is
        # 4m, under the floor regardless of local width -- so the whole piece is
        # suppressed. Its water is already covered by the authoritative axis line via
        # _build_inland_network.
        pipeline = _pipeline(axis_dedup_cap_m=50.0, axis_dedup_fraction=0.5, axis_dedup_floor_m=5.0)
        tiny_piece = box(500000.0, 5700000.0, 500060.0, 5700008.0)  # 60m x 8m
        line_wgs84 = self._wgs84(LineString([(499900.0, 5700004.0), (500160.0, 5700004.0)]))
        pipeline.gdfs["inland_waterways"] = gpd.GeoDataFrame(geometry=[line_wgs84], crs="EPSG:4326")

        result, _, _ = pipeline._axis_dedup_carve_navmesh_pieces(tiny_piece, self.NAVMESH_UTM_CRS)

        assert result == []


class TestNavmeshCarveReconnect:
    """SPEC-GRAPH-DENSITY.md §6.3 Phase A: a navmesh fragment's carve-boundary
    perimeter node -- created because axis-dedup's carve cut the piece there -- gets
    wired back to the SPECIFIC inland_waterways line responsible for that carve, via
    the same `_connect_waterway_crossing` `_inject_waterway_crossings` already uses.
    First-ever test coverage of `_connect_waterway_crossing`/`build_navmesh_region`
    (per the code exploration behind this feature: neither had any test before).
    """

    NAVMESH_UTM_CRS = TestNavmeshPieceCarving.NAVMESH_UTM_CRS

    @staticmethod
    def _wgs84(geom_utm):
        return TestNavmeshPieceCarving._wgs84(geom_utm, TestNavmeshCarveReconnect.NAVMESH_UTM_CRS)

    @staticmethod
    def _dense_through_line_utm(y=5700500.0, x_lo=499937.0, x_hi=502113.0, step=113.0):
        # A vertex every ~113m along the same straight path TestNavmeshPieceCarving's
        # own _through_line_utm uses (geometrically identical -- suppression only
        # depends on distance to the line, unaffected by extra collinear vertices) --
        # but _connect_waterway_crossing snaps to the line's NEAREST VERTEX, and the
        # bare 2-vertex version (endpoints ~1100m from the piece's own centre) would
        # exceed even WATERWAY_CONNECTOR_FALLBACK_MAX_M (500m) for most carve-boundary
        # nodes. Real inland_waterways lines are densely vertexed; this matches that.
        # Deliberately off-round (137/113, not 100) so no vertex lands exactly on
        # _wide_piece's own edges (x=500000/502000) -- an exact coincidence there
        # would make a genuine new "inland" node and an existing carve-boundary
        # perimeter node collide at the same rounded coordinate (_get_or_create_node
        # is a global coordinate cache regardless of node_type), merging the two and
        # making a plain ring-boundary edge look like a spurious long "connector".
        xs = np.arange(x_lo, x_hi + step, step)
        return LineString([(float(x), y) for x in xs])

    @staticmethod
    def _inland_node_coords(pipeline):
        return {(d["lon"], d["lat"]) for _, d in pipeline.graph.nodes(data=True)
                if d.get("node_type") == "inland"}

    def test_carved_dead_end_reconnects_to_a_vertex_on_the_responsible_line(self):
        pipeline = _pipeline(axis_dedup_cap_m=50.0, axis_dedup_fraction=0.5, axis_dedup_floor_m=5.0)
        piece = TestNavmeshPieceCarving()._wide_piece()
        line_utm = self._dense_through_line_utm()
        pipeline.gdfs["inland_waterways"] = gpd.GeoDataFrame(geometry=[self._wgs84(line_utm)], crs="EPSG:4326")

        pieces, seam_coords, line_iloc_by_coord = pipeline._axis_dedup_carve_navmesh_pieces(
            piece, self.NAVMESH_UTM_CRS)
        assert len(pieces) == 2  # same fragmentation as the plain-carve test
        assert line_iloc_by_coord  # the carve boundary really did attribute a line
        assert set(line_iloc_by_coord.values()) == {0}  # only one candidate -> index 0

        for p in pieces:
            pipeline.build_navmesh_region(p, self.NAVMESH_UTM_CRS, set(),
                                           carve_line_iloc_by_coord=line_iloc_by_coord)

        assert pipeline.axis_dedup_reconnect_stats["navmesh_candidates"] > 0
        assert pipeline.axis_dedup_reconnect_stats["navmesh_edges"] > 0

        inland_coords = self._inland_node_coords(pipeline)
        assert inland_coords, "expected at least one inland-type node from a reconnect"
        line_vertex_coords = {(round(lon, 5), round(lat, 5)) for lon, lat in self._wgs84(line_utm).coords}
        assert inland_coords <= line_vertex_coords

        # Connector edge lengths stay well under WATERWAY_CONNECTOR_MAX_M for this
        # fixture (the carve boundary sits ~50m -- the cap -- from the line, and the
        # line has a vertex every 100m, so the nearest one is never far).
        inland_nodes = {n: d for n, d in pipeline.graph.nodes(data=True) if d.get("node_type") == "inland"}
        inland_pts_utm = gpd.GeoSeries(
            [Point(d["lon"], d["lat"]) for d in inland_nodes.values()], crs="EPSG:4326"
        ).to_crs(self.NAVMESH_UTM_CRS)
        for (node_id, _), pt_utm in zip(inland_nodes.items(), inland_pts_utm):
            for nbr in pipeline.graph.successors(node_id):
                nbr_data = pipeline.graph.nodes[nbr]
                nbr_pt_utm = gpd.GeoSeries([Point(nbr_data["lon"], nbr_data["lat"])],
                                            crs="EPSG:4326").to_crs(self.NAVMESH_UTM_CRS).iloc[0]
                assert pt_utm.distance(nbr_pt_utm) < WATERWAY_CONNECTOR_MAX_M

    def test_reconnect_targets_the_responsible_line_even_when_not_first_in_index_order(self):
        # Decoy candidate line placed FIRST (inland_waterways index 0): survives the
        # coarse bbox+margin prefilter (same diagonal-past-the-NE-corner placement as
        # TestNavmeshPieceCarving's own bbox-vs-true-tolerance test) but never actually
        # causes any suppression, so it must never appear as a responsible line. The
        # REAL through-line is index 1 -- the one that actually carves the piece. A
        # naive re-enumeration of the (already bbox-subset) candidates, instead of
        # using their true inland_waterways positional index, would silently
        # misattribute this carve boundary to index 0 (the decoy) instead of index 1.
        pipeline = _pipeline(axis_dedup_cap_m=50.0, axis_dedup_fraction=0.5, axis_dedup_floor_m=5.0)
        piece = TestNavmeshPieceCarving()._wide_piece()
        decoy_utm = LineString([(502045.0, 5701045.0), (502545.0, 5701045.0)])
        line_utm = self._dense_through_line_utm()
        pipeline.gdfs["inland_waterways"] = gpd.GeoDataFrame(
            geometry=[self._wgs84(decoy_utm), self._wgs84(line_utm)], crs="EPSG:4326")

        pieces, seam_coords, line_iloc_by_coord = pipeline._axis_dedup_carve_navmesh_pieces(
            piece, self.NAVMESH_UTM_CRS)
        assert line_iloc_by_coord
        assert set(line_iloc_by_coord.values()) == {1}  # never the decoy (index 0)

        for p in pieces:
            pipeline.build_navmesh_region(p, self.NAVMESH_UTM_CRS, set(),
                                           carve_line_iloc_by_coord=line_iloc_by_coord)

        inland_coords = self._inland_node_coords(pipeline)
        assert inland_coords
        line_vertex_coords = {(round(lon, 5), round(lat, 5)) for lon, lat in self._wgs84(line_utm).coords}
        decoy_vertex_coords = {(round(lon, 5), round(lat, 5)) for lon, lat in self._wgs84(decoy_utm).coords}
        assert inland_coords <= line_vertex_coords
        assert not (inland_coords & decoy_vertex_coords)

    def test_land_crossing_connector_is_rejected(self):
        # Bypasses _axis_dedup_carve_navmesh_pieces' own rasterize-with-land step
        # (which would also subtract `land` from the carve's own water mask, muddying
        # what this test targets) and instead exercises build_navmesh_region /
        # _connect_waterway_crossing directly, the way the real caller would after a
        # real carve: a hand-built south fragment whose north edge sits exactly on a
        # carve boundary, with carve_line_iloc_by_coord naming its two corner nodes.
        pipeline = _pipeline(axis_dedup_cap_m=50.0)
        fragment = box(500000.0, 5700000.0, 502000.0, 5700450.0)
        line_utm = self._dense_through_line_utm()  # vertices at y=5700500
        pipeline.gdfs["inland_waterways"] = gpd.GeoDataFrame(geometry=[self._wgs84(line_utm)], crs="EPSG:4326")
        # Spans the gap between the fragment's own north edge (y=5700450) and the axis
        # line (y=5700500) -- intersects any straight connector from a north-edge node
        # up to the line, without touching the fragment's own interior.
        pipeline.gdfs["land"] = gpd.GeoDataFrame(
            geometry=[self._wgs84(box(499900.0, 5700450.0, 502100.0, 5700500.0))], crs="EPSG:4326")
        carve_line_iloc_by_coord = {(500000.0, 5700450.0): 0, (502000.0, 5700450.0): 0}

        pipeline.build_navmesh_region(fragment, self.NAVMESH_UTM_CRS, set(),
                                       carve_line_iloc_by_coord=carve_line_iloc_by_coord)

        assert pipeline.axis_dedup_reconnect_stats["navmesh_candidates"] > 0
        assert pipeline.axis_dedup_reconnect_stats["navmesh_edges"] == 0
        # _connect_waterway_crossing creates the candidate inland node BEFORE its own
        # _crosses_land check (matching _inject_waterway_crossings' existing, unchanged
        # behaviour) -- rejection means no EDGE to it, not that the node never exists.
        inland_nodes = [n for n, d in pipeline.graph.nodes(data=True) if d.get("node_type") == "inland"]
        assert inland_nodes
        for node_id in inland_nodes:
            assert pipeline.graph.degree(node_id) == 0


class TestToleranceBoundaryIsKnownButAcceptedNearMiss:
    """SPEC-OVERRIDE-ZONES.md §7 follow-up's motivating-case verification found a real
    node (51.6078N, 4.1061E, Oosterschelde approach) surviving axis-dedup despite being
    well inside the 50m cap: at that exact pixel (10m/px resolution), axis_dist_m and
    tol_m both landed on exactly 10.00m -- a genuine tie, not a rare coincidence, since
    both are derived from the same pixel-quantized distance_transform_edt grid.

    Tried `<=` instead of strict `<` to close it. REVERTED after measuring the real
    effect: ties of this kind recur throughout a real dataset (suppression rose
    670,804->729,973 px, 5.2%->5.7%, system-wide -- not a narrow single-pixel fix), and
    the broader reach cost a real POI pair near Hansweert (unrelated to any axis line --
    178m from the nearest one) that used to sit 3m from the main component and became a
    fully isolated 5-node island. Gate 4 (zero POI-pair reachability loss) outranks
    closing this one exact tie, so strict `<` stays -- these tests document the accepted
    boundary behaviour (and the measurement that justified accepting it), not a bug.
    """

    def test_exact_tie_between_axis_distance_and_tolerance_is_not_suppressed(self):
        # Reproduces the real tie exactly: px=10m, a channel band whose edge row sits
        # 1 pixel (10m) from the water/land boundary -> width_est = 1*10*2 = 20m ->
        # tol = clip(0.5*20, 5, 50) = 10m (fraction-bound, not cap-bound). The axis
        # line sits exactly 1 row (10m) away from that same pixel -> axis_dist = 10m.
        # axis_dist_m == tol_m == 10.0 exactly: strict < means NOT suppressed --
        # a known, measured, deliberately-accepted near-miss (see class docstring).
        px = 10.0
        transform = _transform(px=px)
        mask = _channel_mask(40, 50)  # band rows 40..49; row 40 is 1 pixel from row 39 (land)
        pipeline = _pipeline(axis_dedup_cap_m=50.0, axis_dedup_fraction=0.5, axis_dedup_floor_m=5.0)
        # Axis line at row 41 -- 1 row (10m) south of row 40.
        pipeline.gdfs["inland_waterways"] = _inland_gdf(_axis_line_wgs84(transform, 41, col_hi=COLS))
        polygon = _polygon_wgs84_covering_raster(transform)

        suppress, _ = pipeline._axis_dedup_suppression_mask(mask, transform, UTM_CRS, px, polygon)

        assert not suppress[40, 100]

    def test_a_pixel_just_inside_the_tie_is_still_suppressed(self):
        # One pixel closer to the axis than the tie case above (row 41 itself, ON the
        # axis line -- axis_dist=0) must still be suppressed regardless of the strict
        # boundary comparison; only the exact-equality case is affected by <  vs <=.
        px = 10.0
        transform = _transform(px=px)
        mask = _channel_mask(40, 50)
        pipeline = _pipeline(axis_dedup_cap_m=50.0, axis_dedup_fraction=0.5, axis_dedup_floor_m=5.0)
        pipeline.gdfs["inland_waterways"] = _inland_gdf(_axis_line_wgs84(transform, 41, col_hi=COLS))
        polygon = _polygon_wgs84_covering_raster(transform)

        suppress, _ = pipeline._axis_dedup_suppression_mask(mask, transform, UTM_CRS, px, polygon)

        assert suppress[41, 100]

    def test_a_pixel_one_step_beyond_the_tie_is_not_suppressed(self):
        # Same setup, one row further from the axis (row 39 would be off-mask, so use
        # the OTHER band edge instead -- row 49, whose own width_est is also 20m by
        # symmetry, with the axis still at row 41): axis_dist = |49-41| = 8 rows = 80m,
        # tol there is still 10m (fraction-bound) -- 80m > 10m, not suppressed.
        px = 10.0
        transform = _transform(px=px)
        mask = _channel_mask(40, 50)
        pipeline = _pipeline(axis_dedup_cap_m=50.0, axis_dedup_fraction=0.5, axis_dedup_floor_m=5.0)
        pipeline.gdfs["inland_waterways"] = _inland_gdf(_axis_line_wgs84(transform, 41, col_hi=COLS))
        polygon = _polygon_wgs84_covering_raster(transform)

        suppress, _ = pipeline._axis_dedup_suppression_mask(mask, transform, UTM_CRS, px, polygon)

        assert not suppress[49, 100]


class TestMultiLineStringCandidatesAreExcluded:
    """CodeRabbit PR #14 review round 3 finding 1: `_build_inland_network` only ever
    ingests `LineString` geometry (no `elif MultiLineString`) -- a MultiLineString
    feature in `inland_waterways` contributes ZERO graph topology. Before this fix,
    axis-dedup's candidate search had no geometry-type filter at all, so it would still
    rasterize a MultiLineString as a valid suppression trigger -- suppressing coastal
    water on the theory that "the authoritative line covers this" when the graph never
    actually got that line. Confirmed harmless on tonight's Zeeland builds (both
    inland_waterways_lines.geojson files in use are pure LineString) but a real,
    plausible gap for NOAA/US ENC data.
    """

    def test_multilinestring_candidate_is_excluded_from_skeleton_suppression(self, caplog):
        import logging
        transform = _transform()
        mask = _channel_mask(30, 70)
        pipeline = _pipeline(axis_dedup_cap_m=50.0)
        # Same position/shape that a plain LineString axis (TestCarvesWhenEnabled)
        # demonstrably DOES suppress -- a MultiLineString wrapping the identical
        # geometry must not.
        line = _axis_line_wgs84(transform, 49)
        from shapely.geometry import MultiLineString
        mls = MultiLineString([list(line.coords)])
        pipeline.gdfs["inland_waterways"] = gpd.GeoDataFrame(geometry=[mls], crs="EPSG:4326")
        polygon = _polygon_wgs84_covering_raster(transform)

        with caplog.at_level(logging.WARNING, logger="nautical_routing_pipeline"):
            suppress, _ = pipeline._axis_dedup_suppression_mask(mask, transform, UTM_CRS, PX_M, polygon)

        assert not suppress.any()
        assert any("not LineString geometry" in r.message for r in caplog.records)

    def test_multilinestring_candidate_is_excluded_from_navmesh_carve(self, caplog):
        import logging
        piece = TestNavmeshPieceCarving()._wide_piece()
        line_utm = TestNavmeshPieceCarving()._through_line_utm()
        from shapely.geometry import MultiLineString
        mls_utm = MultiLineString([list(line_utm.coords)])
        mls_wgs84 = gpd.GeoSeries([mls_utm], crs=TestNavmeshPieceCarving.NAVMESH_UTM_CRS).to_crs("EPSG:4326").iloc[0]
        pipeline = _pipeline(axis_dedup_cap_m=50.0)
        pipeline.gdfs["inland_waterways"] = gpd.GeoDataFrame(geometry=[mls_wgs84], crs="EPSG:4326")

        with caplog.at_level(logging.WARNING, logger="nautical_routing_pipeline"):
            result, _, _ = pipeline._axis_dedup_carve_navmesh_pieces(piece, TestNavmeshPieceCarving.NAVMESH_UTM_CRS)

        assert len(result) == 1
        assert result[0].equals(piece)
        assert any("not LineString geometry" in r.message for r in caplog.records)

    def test_a_real_linestring_candidate_alongside_a_multilinestring_still_suppresses(self):
        # The filter must exclude only the MultiLineString, not poison the whole
        # candidate list -- a genuine LineString in the same layer still works.
        transform = _transform()
        mask = _channel_mask(30, 70)
        pipeline = _pipeline(axis_dedup_cap_m=50.0)
        good_line = _axis_line_wgs84(transform, 49)
        from shapely.geometry import MultiLineString
        bad_mls = MultiLineString([[(4.20, 51.70), (4.21, 51.70)]])  # far away, irrelevant either way
        pipeline.gdfs["inland_waterways"] = gpd.GeoDataFrame(geometry=[good_line, bad_mls], crs="EPSG:4326")
        polygon = _polygon_wgs84_covering_raster(transform)

        suppress, _ = pipeline._axis_dedup_suppression_mask(mask, transform, UTM_CRS, PX_M, polygon)

        assert suppress[49, 100]


class TestAllZeroAxisRasterDoesNotPhantomSuppress:
    """Found while hardening the prefilter-vs-real-tolerance test for CodeRabbit PR #14
    review round 3 finding 2: when every candidate survives the coarse bbox+margin
    prefilter (their bounding boxes overlap the piece) but NONE actually rasterizes onto
    this piece's own grid -- a real case, since a line's bbox is a rectangle and a
    diagonal/L-shaped feature can have its bbox reach into the margin while the line
    itself passes nowhere near the piece -- `axis_raster` ends up all-zero.
    `scipy.ndimage.distance_transform_edt` on an array with NO background pixel
    anywhere does not mean "far away everywhere": it falls back to measuring from an
    implicit point outside the array's own (0,0) corner, producing small, spurious
    distances near that corner. Confirmed directly before this guard: a line 40m
    outside a piece's own raster footprint still wrongly carved a ~1900 sq m sliver at
    the piece's origin corner. Guarded with `if not axis_raster.any(): return zeros`.

    SPEC-GRAPH-DENSITY.md §6.3.2: the guard above caught the phantom-corner artifact
    but not the underlying miss it was papering over -- a candidate within the cap
    margin of the piece's own bbox can have real geometry that never touches the
    piece's OWN (unpadded) raster grid at all, even though it's genuinely within
    tolerance of water pixels near the piece's edge. `_axis_dedup_suppression_mask`
    now rasterizes onto a grid padded by `axis_dedup_cap_m` before this guard runs, so
    a line 40m outside the piece's edge (within the 50m cap) is no longer silently
    dropped -- it's the case exercised below.
    """

    def test_line_just_outside_the_piece_grid_but_within_the_cap_now_carves(self):
        piece = TestNavmeshPieceCarving()._wide_piece()  # box(500000,5700000,502000,5701000)
        # 40m north of the piece's own north edge (5701000) -- inside the 50m cap
        # margin (survives the bbox prefilter) and, since §6.3.2's padding fix, now
        # also within the padded raster grid the carve rasterizes onto.
        line_utm = LineString([(500500.0, 5701040.0), (501500.0, 5701040.0)])
        line_wgs84 = gpd.GeoSeries([line_utm], crs=TestNavmeshPieceCarving.NAVMESH_UTM_CRS).to_crs("EPSG:4326").iloc[0]
        pipeline = _pipeline(axis_dedup_cap_m=50.0)
        pipeline.gdfs["inland_waterways"] = gpd.GeoDataFrame(geometry=[line_wgs84], crs="EPSG:4326")

        result, _, _ = pipeline._axis_dedup_carve_navmesh_pieces(piece, TestNavmeshPieceCarving.NAVMESH_UTM_CRS)

        # The line is 40m from the piece's north edge -- within the 50m cap, so a strip
        # along that edge is now carved away: real suppression, not a no-op, and not a
        # spurious corner sliver (the whole north edge is affected, not just a corner).
        assert len(result) == 1
        assert result[0].area < piece.area
        # Confirm this isn't the old phantom-corner artifact (~1900 sq m at one corner)
        # but a real edge-wide carve along most of the piece's own north edge (~1000m
        # long, ~10m deep -- 50m cap minus the 40m gap to the line -- ~10,000 sq m; a
        # single-corner sliver could never reach this).
        assert piece.area - result[0].area > 8000.0

    def test_line_beyond_even_the_padded_cap_margin_does_not_carve_a_corner_sliver(self):
        piece = TestNavmeshPieceCarving()._wide_piece()  # box(500000,5700000,502000,5701000)
        # 40m north of the piece's own north edge is now reachable (padded grid, see
        # above); push the line's bbox just far enough to still survive the +/-50m
        # bbox+margin prefilter (a diagonal/L-shaped feature's bbox can reach in) while
        # its actual geometry stays outside even the padded raster grid -- this must
        # still return the original piece untouched, no phantom sliver anywhere.
        line_utm = LineString([(500500.0, 5701049.0), (501500.0, 5701200.0)])
        line_wgs84 = gpd.GeoSeries([line_utm], crs=TestNavmeshPieceCarving.NAVMESH_UTM_CRS).to_crs("EPSG:4326").iloc[0]
        pipeline = _pipeline(axis_dedup_cap_m=50.0)
        pipeline.gdfs["inland_waterways"] = gpd.GeoDataFrame(geometry=[line_wgs84], crs="EPSG:4326")

        result, _, _ = pipeline._axis_dedup_carve_navmesh_pieces(piece, TestNavmeshPieceCarving.NAVMESH_UTM_CRS)

        assert len(result) == 1
        assert result[0].equals(piece)
