"""Unit tests for --channel-axes ingestion (docs/SPEC-CHANNEL-AXES.md).

`derive_channel_axes.py` writes `channel_axes_lines.geojson`; under `--channel-axes`
the pipeline appends those lines (filtered by confidence) to `inland_waterways`
before densification, and `_build_inland_network` stamps the derived edges with
their own data_sources row. Off by default: byte-identical output.

All fixtures are synthetic geometry near Zeeland -- no real chart data.
"""

import geopandas as gpd
import pytest
from shapely.geometry import LineString

from nautical_routing_pipeline import (
    NauticalRoutingPipeline,
    _default_data_sources,
)


def _pipeline(use_channel_axes=False, min_confidence=0.5):
    # __init__ only assigns attributes (no file I/O) -- safe to build directly, same
    # pattern as tests/test_inland_densify.py's _pipeline().
    p = NauticalRoutingPipeline(data_paths={}, db_path=":memory:",
                                use_channel_axes=use_channel_axes,
                                channel_axes_min_confidence=min_confidence)
    p.coords_to_node = {}
    p._inland_split_cuts = {}
    p.layer_source_ids = {s["name"]: i + 1 for i, s in enumerate(_default_data_sources(use_channel_axes))}
    return p


def _inland():
    return gpd.GeoDataFrame({"OBJNAM": ["Oosterlandse Rak"]},
                            geometry=[LineString([(3.70, 51.44), (3.71, 51.44)])], crs="EPSG:4326")


def _axes():
    return gpd.GeoDataFrame(
        {"axis_kind": ["polygon_centerline", "mark_chain", "mark_chain"],
         "confidence": [0.9, 0.7, 0.4],
         "channel_name": ["Fairway", "Good chain", "Weak chain"]},
        geometry=[LineString([(3.72, 51.45), (3.73, 51.45)]),
                  LineString([(3.74, 51.45), (3.75, 51.45)]),
                  LineString([(3.76, 51.45), (3.77, 51.45)])],
        crs="EPSG:4326")


class TestDataSources:
    def test_default_rows_unchanged_without_flag(self):
        names = [s["name"] for s in _default_data_sources()]
        assert "channel_axes" not in names

    def test_channel_axes_row_appended_last(self):
        base = [s["name"] for s in _default_data_sources()]
        with_axes = [s["name"] for s in _default_data_sources(True)]
        assert with_axes[:-1] == base          # every existing id is preserved
        assert with_axes[-1] == "channel_axes"
        assert _default_data_sources(True)[-1]["source_type"] == "derived"


class TestValidation:
    @pytest.mark.parametrize("bad", [float("nan"), -0.1, 1.5, float("inf")])
    def test_rejects_out_of_range(self, bad):
        with pytest.raises(ValueError):
            NauticalRoutingPipeline._validate_channel_axes_min_confidence(bad)

    @pytest.mark.parametrize("ok", [0.0, 0.5, 1.0])
    def test_accepts_range(self, ok):
        NauticalRoutingPipeline._validate_channel_axes_min_confidence(ok)


class TestMerge:
    def test_off_leaves_inland_untouched(self):
        p = _pipeline(use_channel_axes=False)
        inland = _inland()
        p.gdfs = {"inland_waterways": inland, "channel_axes": _axes()}
        p._merge_channel_axes()
        assert p.gdfs["inland_waterways"] is inland
        assert "layer_key" not in p.gdfs["inland_waterways"].columns

    def test_on_appends_axes_above_confidence(self):
        p = _pipeline(use_channel_axes=True, min_confidence=0.5)
        p.gdfs = {"inland_waterways": _inland(), "channel_axes": _axes()}
        p._merge_channel_axes()
        merged = p.gdfs["inland_waterways"]
        assert len(merged) == 3                       # 1 inland + 2 axes (0.9, 0.7); 0.4 dropped
        assert list(merged["layer_key"]) == ["inland_waterways", "channel_axes", "channel_axes"]
        assert merged.crs.to_epsg() == 4326
        assert p.channel_axes_stats["merged"] == 2 and p.channel_axes_stats["loaded"] == 3

    def test_on_with_no_inland_layer(self):
        p = _pipeline(use_channel_axes=True, min_confidence=0.0)
        p.gdfs = {"channel_axes": _axes()}
        p._merge_channel_axes()
        assert len(p.gdfs["inland_waterways"]) == 3

    def test_on_with_missing_axes_layer_is_a_noop(self):
        p = _pipeline(use_channel_axes=True)
        inland = _inland()
        p.gdfs = {"inland_waterways": inland,
                  "channel_axes": gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")}
        p._merge_channel_axes()
        assert p.gdfs["inland_waterways"] is inland


class TestInlandNetworkProvenance:
    def test_edges_stamped_with_their_layer_source_id(self):
        p = _pipeline(use_channel_axes=True, min_confidence=0.5)
        p.gdfs = {"inland_waterways": _inland(), "channel_axes": _axes()}
        p._merge_channel_axes()
        p._build_inland_network()
        inland_id = p.layer_source_ids["inland_waterways"]
        axes_id = p.layer_source_ids["channel_axes"]
        by_src = {}
        for u, v, d in p.graph.edges(data=True):
            assert d["edge_type"] == "inland"
            by_src.setdefault(d["source_id"], 0)
            by_src[d["source_id"]] += 1
        assert by_src == {inland_id: 2, axes_id: 4}   # 1 + 2 lines, both directions each

    def test_without_layer_key_column_all_edges_use_inland_id(self):
        p = _pipeline(use_channel_axes=False)
        p.gdfs = {"inland_waterways": _inland()}
        p._build_inland_network()
        ids = {d["source_id"] for _, _, d in p.graph.edges(data=True)}
        assert ids == {p.layer_source_ids["inland_waterways"]}


class TestNavmeshCarveExclusion:
    """docs/SPEC-CHANNEL-AXES.md: derived axes are excluded from the NAVMESH-side
    axis-dedup carve by default (no centerline twin to suppress in a triangulated
    mesh); the skeleton-side mask, which passes no exclusion, still sees them."""

    def _setup(self):
        from tests.test_axis_dedup import (
            _axis_line_wgs84, _channel_mask, _polygon_wgs84_covering_raster, _transform, PX_M, UTM_CRS)
        p = _pipeline(use_channel_axes=True)
        p.classification_config.axis_dedup_cap_m = 50.0
        transform = _transform()
        mask = _channel_mask(30, 70)
        gdf = gpd.GeoDataFrame({"layer_key": ["channel_axes"]},
                               geometry=[_axis_line_wgs84(transform, 49)], crs="EPSG:4326")
        p.gdfs["inland_waterways"] = gdf
        return p, mask, transform, _polygon_wgs84_covering_raster(transform), PX_M, UTM_CRS

    def test_excluded_axis_suppresses_nothing(self):
        p, mask, transform, polygon, px, utm = self._setup()
        suppress, _ = p._axis_dedup_suppression_mask(mask, transform, utm, px, polygon,
                                                     exclude_layer_key="channel_axes")
        assert not suppress.any()

    def test_same_axis_still_carves_the_skeleton_mask(self):
        p, mask, transform, polygon, px, utm = self._setup()
        suppress, _ = p._axis_dedup_suppression_mask(mask, transform, utm, px, polygon)
        assert suppress[49, 100]

    def test_charted_line_is_not_excluded(self):
        p, mask, transform, polygon, px, utm = self._setup()
        p.gdfs["inland_waterways"]["layer_key"] = "inland_waterways"
        suppress, _ = p._axis_dedup_suppression_mask(mask, transform, utm, px, polygon,
                                                     exclude_layer_key="channel_axes")
        assert suppress[49, 100]


class TestNavmeshCarveFastPath:
    """CodeRabbit (PR #24): when derived axes are excluded from the navmesh carve, a
    piece whose only nearby lines are derived axes must take the no-candidates fast
    path -- no rasterization at all -- rather than rasterize and then discard."""

    UTM = "EPSG:32631"

    def _setup(self, monkeypatch, layer_key):
        from shapely.geometry import box
        p = _pipeline(use_channel_axes=True)
        p.classification_config.axis_dedup_cap_m = 50.0
        piece = box(500000.0, 5700000.0, 502000.0, 5701000.0)
        line = gpd.GeoSeries([LineString([(499900.0, 5700500.0), (502100.0, 5700500.0)])],
                             crs=self.UTM).to_crs("EPSG:4326").iloc[0]
        p.gdfs["inland_waterways"] = gpd.GeoDataFrame({"layer_key": [layer_key]}, geometry=[line], crs="EPSG:4326")
        calls = []
        orig = p._rasterize_water_polygon

        def _spy(*a, **kw):
            calls.append(1)
            return orig(*a, **kw)
        monkeypatch.setattr(p, "_rasterize_water_polygon", _spy)
        return p, piece, calls

    def test_only_derived_axes_nearby_skips_rasterization(self, monkeypatch):
        p, piece, calls = self._setup(monkeypatch, "channel_axes")
        result, seams, attrib = p._axis_dedup_carve_navmesh_pieces(piece, self.UTM)
        assert calls == []
        assert len(result) == 1 and result[0].equals(piece) and not seams and not attrib

    def test_charted_line_nearby_still_rasterizes_and_carves(self, monkeypatch):
        p, piece, calls = self._setup(monkeypatch, "inland_waterways")
        result, _, _ = p._axis_dedup_carve_navmesh_pieces(piece, self.UTM)
        assert calls == [1]
        assert len(result) >= 2          # the through-line carves the piece in two

    def test_opt_in_flag_lets_derived_axes_carve(self, monkeypatch):
        p, piece, calls = self._setup(monkeypatch, "channel_axes")
        p.classification_config.channel_axes_navmesh_carve = True
        result, _, _ = p._axis_dedup_carve_navmesh_pieces(piece, self.UTM)
        assert calls == [1] and len(result) >= 2
