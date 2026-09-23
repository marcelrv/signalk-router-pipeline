"""Tier-1 unit tests for enc_preprocessor.py's pure filename-parsing logic,
plus a regression test for the RECTRC/NAVLNE attribute passthrough documented
in docs/SPEC-RECOMMENDED-TRACK.md (Option A)."""
import json

import geopandas as gpd
from shapely.geometry import LineString

from enc_preprocessor import ENCToGeoJSONPreprocessor, _usage_band


class TestUsageBand:
    def test_band_1_overview(self):
        assert _usage_band("US1GC09M.000") == "1"

    def test_band_2_general(self):
        assert _usage_band("US2EC02M.000") == "2"

    def test_band_5_approach(self):
        assert _usage_band("US5NYCUG.000") == "5"

    def test_dutch_producer_code(self):
        assert _usage_band("NL2R70990A.000") == "2"

    def test_lowercase_producer_code(self):
        assert _usage_band("us4ny1bw.000") == "4"

    def test_full_path_uses_basename_only(self):
        assert _usage_band("/data/raw/us-east-coast/NY/US5NYCEG.000") == "5"

    def test_unrecognized_pattern_returns_none(self):
        assert _usage_band("readme.txt") is None


class TestRectrcAttributePassthrough:
    """docs/SPEC-RECOMMENDED-TRACK.md Option A: CATTRK/TRAFIC/ORIENT/INFORM
    must survive from a RECTRC S-57 feature into inland_waterways_lines.geojson
    properties, unchanged, with no explicit column selection dropping them."""

    def test_cattrk_trafic_orient_inform_survive(self, tmp_path, monkeypatch):
        rectrc_gdf = gpd.GeoDataFrame(
            {
                "CATTRK": [1],
                "TRAFIC": [3],
                "ORIENT": [89.0],
                "INFORM": ["FROM 0.7 NM off Oswego Outer Pier TO Oswego Harbor"],
                "DRVAL1": [None],
            },
            geometry=[LineString([(-74.01, 40.70), (-74.02, 40.71)])],
            crs="EPSG:4326",
        )

        def fake_read_file(path, layer=None):
            if layer == "RECTRC":
                return rectrc_gdf.copy()
            # DSID (compilation scale) and every other layer probed by
            # _extract_layers_from_file: behave like fiona/GDAL does for a
            # layer that doesn't exist in this (fake) cell.
            raise ValueError(f"layer '{layer}' not found")

        monkeypatch.setattr("enc_preprocessor.gpd.read_file", fake_read_file)

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        pre = ENCToGeoJSONPreprocessor(str(tmp_path), str(out_dir))
        pre._extract_layers_from_file("/fake/US5NYCEG.000")
        pre._merge_and_export()

        out_path = out_dir / "inland_waterways_lines.geojson"
        data = json.loads(out_path.read_text())
        assert len(data["features"]) == 1
        props = data["features"][0]["properties"]
        assert props["CATTRK"] == 1
        assert props["TRAFIC"] == 3
        assert props["ORIENT"] == 89.0
        assert props["INFORM"] == "FROM 0.7 NM off Oswego Outer Pier TO Oswego Harbor"
