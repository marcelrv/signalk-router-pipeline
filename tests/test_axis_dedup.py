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
from shapely.geometry import LineString, box

from nautical_routing_pipeline import ClassificationConfig, NauticalRoutingPipeline

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

        suppress = pipeline._axis_dedup_suppression_mask(mask, transform, UTM_CRS, PX_M, polygon)

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

        suppress = pipeline._axis_dedup_suppression_mask(mask, transform, UTM_CRS, PX_M, polygon)
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
        wide_suppress = wide_pipeline._axis_dedup_suppression_mask(
            wide_mask, wide_transform, UTM_CRS, PX_M, wide_polygon)

        # Narrow channel: 40m tall (rows 46:54) -> local width near the centre tops
        # out around 40m, so fraction*width (~20m) stays under the cap -- the
        # width-coupled fraction binds instead of the flat 50m cap.
        narrow_transform = _transform()
        narrow_mask = _channel_mask(46, 54)
        narrow_pipeline = _pipeline(axis_dedup_cap_m=50.0, axis_dedup_fraction=0.5, axis_dedup_floor_m=5.0)
        narrow_pipeline.gdfs["inland_waterways"] = _inland_gdf(_axis_line_wgs84(narrow_transform, 49))
        narrow_polygon = _polygon_wgs84_covering_raster(narrow_transform)
        narrow_suppress = narrow_pipeline._axis_dedup_suppression_mask(
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
        # A very narrow channel where fraction*width (3m) would fall BELOW the 5m
        # floor if the floor weren't applied -- use 1m pixels so a 6m-tall band is
        # representable. width_est at the centre ~6m -> fraction*width = 3m, but the
        # floor must clip that up to 5m.
        px = 1.0
        transform = _transform(px=px)
        mask = _channel_mask(47, 53, rows=ROWS, cols=COLS)  # 6 rows tall @ 1m px = 6m
        pipeline = _pipeline(axis_dedup_cap_m=50.0, axis_dedup_fraction=0.5, axis_dedup_floor_m=5.0)
        pipeline.gdfs["inland_waterways"] = _inland_gdf(_axis_line_wgs84(transform, 49, col_hi=COLS))
        polygon = _polygon_wgs84_covering_raster(transform)

        suppress = pipeline._axis_dedup_suppression_mask(mask, transform, UTM_CRS, px, polygon)

        # 4m from the axis row (row 49 -> row 45, 4 rows @ 1m): inside the 5m floor,
        # so suppressed, even though fraction*width alone (3m) would not have reached
        # this far.
        assert suppress[45, 100]
        # 3m from the axis (row 46): also inside the floor.
        assert suppress[46, 100]

    def test_floor_does_not_shrink_tolerance_below_5m_even_off_channel(self):
        # Confirms the floor applies to EVERY pixel's tol, not just ones inside a
        # channel -- distance_transform_edt(mask) is 0 off-mask, so width_est is 0
        # there, and fraction*0=0 would give tol=0 without the floor.
        px = 1.0
        transform = _transform(px=px)
        mask = _channel_mask(47, 53, rows=ROWS, cols=COLS)
        pipeline = _pipeline(axis_dedup_cap_m=50.0, axis_dedup_fraction=0.5, axis_dedup_floor_m=5.0)
        pipeline.gdfs["inland_waterways"] = _inland_gdf(_axis_line_wgs84(transform, 49, col_hi=COLS))
        polygon = _polygon_wgs84_covering_raster(transform)

        suppress = pipeline._axis_dedup_suppression_mask(mask, transform, UTM_CRS, px, polygon)

        # Off-mask pixel 4m from the axis row: still within the 5m floor tolerance,
        # even though it carries zero local width.
        assert suppress[45, 5]  # far from the channel's own columns, but same row band


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

        suppress = pipeline._axis_dedup_suppression_mask(mask, transform, UTM_CRS, PX_M, polygon)

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

        suppress = pipeline._axis_dedup_suppression_mask(mask, transform, UTM_CRS, PX_M, polygon)

        assert not (mask & suppress).any()

    def test_no_inland_waterways_layer_at_all(self):
        transform = _transform()
        mask = _channel_mask(30, 70)
        pipeline = _pipeline(axis_dedup_cap_m=50.0)
        # pipeline.gdfs has no "inland_waterways" key at all (never loaded).
        polygon = _polygon_wgs84_covering_raster(transform)

        suppress = pipeline._axis_dedup_suppression_mask(mask, transform, UTM_CRS, PX_M, polygon)

        assert not suppress.any()

    def test_empty_inland_waterways_layer(self):
        transform = _transform()
        mask = _channel_mask(30, 70)
        pipeline = _pipeline(axis_dedup_cap_m=50.0)
        pipeline.gdfs["inland_waterways"] = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
        polygon = _polygon_wgs84_covering_raster(transform)

        suppress = pipeline._axis_dedup_suppression_mask(mask, transform, UTM_CRS, PX_M, polygon)

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

        suppress_with_lock = pipeline._axis_dedup_suppression_mask(
            mask, transform, UTM_CRS, PX_M, polygon)

        # Without the lock layer, the same setup suppresses right on the axis (already
        # covered by TestCarvesWhenEnabled, re-asserted here as the control).
        pipeline_no_lock = _pipeline(axis_dedup_cap_m=50.0)
        pipeline_no_lock.gdfs["inland_waterways"] = _inland_gdf(_axis_line_wgs84(transform, 49))
        suppress_without_lock = pipeline_no_lock._axis_dedup_suppression_mask(
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

        suppress = pipeline._axis_dedup_suppression_mask(mask, transform, UTM_CRS, PX_M, polygon)

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

        suppress = pipeline._axis_dedup_suppression_mask(mask, transform, UTM_CRS, PX_M, polygon)

        assert suppress[49, 100]  # unaffected -- suppression proceeds normally

    def test_empty_locks_layer_leaves_suppression_unaffected(self):
        transform = _transform()
        mask = _channel_mask(30, 70)
        pipeline = _pipeline(axis_dedup_cap_m=50.0)
        pipeline.gdfs["inland_waterways"] = _inland_gdf(_axis_line_wgs84(transform, 49))
        pipeline.gdfs["locks"] = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
        polygon = _polygon_wgs84_covering_raster(transform)

        suppress = pipeline._axis_dedup_suppression_mask(mask, transform, UTM_CRS, PX_M, polygon)

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

        suppress = pipeline._axis_dedup_suppression_mask(mask, transform, UTM_CRS, PX_M, polygon)

        assert suppress[49, 100]  # unaffected by a lock nowhere near this piece
