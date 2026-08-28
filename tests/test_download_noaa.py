"""Tier-1 unit tests for scripts/download_noaa.py: pure helpers and static
config sanity (catches typos in REGIONS/ALL_STATES before they ship)."""
import json

from download_noaa import (
    ALL_STATES,
    REGIONS,
    load_manifest,
    save_manifest,
    state_zip_url,
)


class TestStateZipUrl:
    def test_formats_state_into_noaa_url(self):
        assert state_zip_url("FL") == "https://charts.noaa.gov/ENCs/FL_ENCs.zip"


class TestManifestRoundtrip:
    def test_save_then_load_roundtrips(self, tmp_path):
        path = str(tmp_path / "manifest.json")
        manifest = {"FL": {"downloaded": "2026-01-01T00:00:00+00:00"}}
        save_manifest(manifest, path)
        assert load_manifest(path) == manifest

    def test_load_missing_file_returns_empty_dict(self, tmp_path):
        assert load_manifest(str(tmp_path / "nope.json")) == {}

    def test_load_corrupt_json_returns_empty_dict(self, tmp_path):
        path = tmp_path / "manifest.json"
        path.write_text("{not valid json")
        assert load_manifest(str(path)) == {}


class TestRegionsConfig:
    def test_every_region_state_is_a_known_state(self):
        # Catches a typo'd state code before it silently 404s at download time.
        for region, info in REGIONS.items():
            for state in info["states"]:
                assert state in ALL_STATES, f"{region} lists unknown state {state!r}"

    def test_every_region_has_states_and_description(self):
        for region, info in REGIONS.items():
            assert info["states"], f"{region} has an empty states list"
            assert info["description"], f"{region} has no description"

    def test_all_states_has_no_duplicates(self):
        assert len(ALL_STATES) == len(set(ALL_STATES))

    def test_all_states_are_two_letter_codes(self):
        for state in ALL_STATES:
            assert len(state) == 2
            assert state.isupper()
