"""Tier-1 unit tests for enc_preprocessor.py's pure filename-parsing logic."""
from enc_preprocessor import _usage_band


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
