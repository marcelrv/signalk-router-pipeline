"""Tier-1 unit tests: pure decision logic in nautical_routing_pipeline.py that
takes a plain row/value and returns a classification, no geodata or I/O needed.
"""
from nautical_routing_pipeline import (
    _is_entry_prohibited,
    _obstruction_depth_disposition,
    _default_data_sources,
    DEFAULT_SOURCE_TIER,
)


class TestIsEntryProhibited:
    def test_restrn_1_is_prohibited(self):
        assert _is_entry_prohibited({"RESTRN": "1"}) is True

    def test_restrn_2_is_not_prohibited(self):
        assert _is_entry_prohibited({"RESTRN": "2"}) is False

    def test_restrn_list_containing_1_is_prohibited(self):
        assert _is_entry_prohibited({"RESTRN": [2, 1]}) is True

    def test_objnam_entry_prohibited_text(self):
        assert _is_entry_prohibited({"OBJNAM": "Entry Prohibited Area"}) is True

    def test_objnam_dutch_toegang_verboden(self):
        assert _is_entry_prohibited({"OBJNAM": "Toegang verboden zone"}) is True

    def test_clean_row_is_not_prohibited(self):
        assert _is_entry_prohibited({"OBJNAM": "Marina Approach"}) is False

    def test_empty_row_is_not_prohibited(self):
        assert _is_entry_prohibited({}) is False

    def test_restrn_non_numeric_does_not_crash(self):
        # A malformed/non-numeric RESTRN must fall through, not raise.
        assert _is_entry_prohibited({"RESTRN": "not-a-number"}) is False


class TestObstructionDepthDisposition:
    def test_valsou_present_is_soft_with_charted_depth(self):
        # A charted sounding always wins, regardless of WATLEV.
        is_hard, depth = _obstruction_depth_disposition({"VALSOU": 3.5, "WATLEV": 1})
        assert is_hard is False
        assert depth == 3.5

    def test_watlev_3_no_valsou_is_soft_zero_depth(self):
        # Always-underwater, unswept: conservative depth constraint, not a hard block.
        is_hard, depth = _obstruction_depth_disposition({"WATLEV": 3})
        assert is_hard is False
        assert depth == 0.0

    def test_watlev_4_no_valsou_is_soft_zero_depth(self):
        # Covers/uncovers: same conservative treatment as WATLEV==3.
        is_hard, depth = _obstruction_depth_disposition({"WATLEV": 4})
        assert is_hard is False
        assert depth == 0.0

    def test_watlev_2_always_dry_is_hard_block(self):
        is_hard, depth = _obstruction_depth_disposition({"WATLEV": 2})
        assert is_hard is True
        assert depth is None

    def test_watlev_missing_is_hard_block(self):
        is_hard, depth = _obstruction_depth_disposition({})
        assert is_hard is True
        assert depth is None

    def test_watlev_unrecognized_floating_is_hard_block(self):
        is_hard, depth = _obstruction_depth_disposition({"WATLEV": 7})
        assert is_hard is True
        assert depth is None

    def test_valsou_non_numeric_falls_back_to_watlev(self):
        is_hard, depth = _obstruction_depth_disposition({"VALSOU": "n/a", "WATLEV": 3})
        assert is_hard is False
        assert depth == 0.0


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
        assert row["default_tier"] == DEFAULT_SOURCE_TIER

    def test_enc_layers_are_source_type_enc(self):
        rows = _default_data_sources()
        for row in rows:
            if row["name"] != "inland_waterways":
                assert row["source_type"] == "enc"
