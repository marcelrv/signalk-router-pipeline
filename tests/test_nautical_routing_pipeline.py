"""Tier-1 unit tests: pure decision logic in nautical_routing_pipeline.py that
takes a plain row/value and returns a classification, no geodata or I/O needed.
"""
from nautical_routing_pipeline import (
    _default_data_sources,
)


class TestDefaultDataSources:
    def test_names_are_unique(self):
        rows = _default_data_sources()
        names = [r["name"] for r in rows]
        assert len(names) == len(set(names))

    def test_every_row_has_required_fields(self):
        for row in _default_data_sources():
            for field in ("name", "source_type", "attribution_text", "default_tier"):
                assert field in row

    def test_fairways_and_inland_waterways_present(self):
        names = {r["name"] for r in _default_data_sources()}
        assert "fairways" in names
        assert "inland_waterways" in names

    def test_inland_waterways_is_ienc_tier1(self):
        row = next(r for r in _default_data_sources() if r["name"] == "inland_waterways")
        assert row["source_type"] == "ienc"
        # Asserts the literal expected value, not DEFAULT_SOURCE_TIER -- comparing
        # against the same constant the source uses would pass even if that
        # constant were changed to something other than tier 1 for the wrong reason.
        assert row["default_tier"] == 1

    def test_enc_layers_are_source_type_enc(self):
        rows = _default_data_sources()
        for row in rows:
            if row["name"] != "inland_waterways":
                assert row["source_type"] == "enc"
