"""Tier-1 unit tests for deploy_to_data_repo.py's upload_asset(), covering the
release-existence-check logic fixed during PR #7 review (previously relied on
pattern-matching the upload command's stderr for "release not found", which
broke on wording changes or auth/network failures)."""
import subprocess

import pytest

import deploy_to_data_repo as ddr


def _completed(returncode, stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stderr=stderr)


class TestUploadAsset:
    def test_uploads_directly_when_release_exists(self, monkeypatch):
        calls = []

        def fake_run_gh(args, **kwargs):
            calls.append(args)
            if args[:2] == ["release", "view"]:
                return _completed(0)
            if args[:2] == ["release", "upload"]:
                return _completed(0)
            raise AssertionError(f"unexpected gh call: {args}")

        monkeypatch.setattr(ddr, "run_gh", fake_run_gh)
        ddr.upload_asset("/tmp/region.sqlite.gz", "routing-databases-latest", "org/repo")

        kinds = [c[:2] for c in calls]
        assert ["release", "view"] in kinds
        assert ["release", "upload"] in kinds
        assert ["release", "create"] not in [c[:2] for c in calls]

    def test_creates_release_when_missing_then_does_not_also_upload(self, monkeypatch):
        calls = []

        def fake_run_gh(args, **kwargs):
            calls.append(args)
            if args[:2] == ["release", "view"]:
                return _completed(1, stderr="release not found")
            if args[:2] == ["release", "create"]:
                return _completed(0)
            raise AssertionError(f"unexpected gh call: {args}")

        monkeypatch.setattr(ddr, "run_gh", fake_run_gh)
        ddr.upload_asset("/tmp/region.sqlite.gz", "routing-databases-latest", "org/repo")

        kinds = [c[:2] for c in calls]
        assert ["release", "view"] in kinds
        assert ["release", "create"] in kinds
        # "release create" is given the asset path directly, so a separate
        # "release upload" call would double-publish it.
        assert ["release", "upload"] not in kinds

    def test_exits_nonzero_when_release_creation_fails(self, monkeypatch):
        def fake_run_gh(args, **kwargs):
            if args[:2] == ["release", "view"]:
                return _completed(1, stderr="release not found")
            if args[:2] == ["release", "create"]:
                return _completed(1, stderr="permission denied")
            raise AssertionError(f"unexpected gh call: {args}")

        monkeypatch.setattr(ddr, "run_gh", fake_run_gh)
        with pytest.raises(SystemExit) as exc_info:
            ddr.upload_asset("/tmp/region.sqlite.gz", "routing-databases-latest", "org/repo")
        assert exc_info.value.code == 1

    def test_exits_nonzero_when_upload_fails_on_existing_release(self, monkeypatch):
        def fake_run_gh(args, **kwargs):
            if args[:2] == ["release", "view"]:
                return _completed(0)
            if args[:2] == ["release", "upload"]:
                return _completed(1, stderr="network error")
            raise AssertionError(f"unexpected gh call: {args}")

        monkeypatch.setattr(ddr, "run_gh", fake_run_gh)
        with pytest.raises(SystemExit) as exc_info:
            ddr.upload_asset("/tmp/region.sqlite.gz", "routing-databases-latest", "org/repo")
        assert exc_info.value.code == 1

    def test_does_not_mistake_auth_failure_for_missing_release(self, monkeypatch):
        # A "release view" failure for any reason (auth, network) is treated the
        # same as "missing" by upload_asset's simple returncode check -- it will
        # attempt to create, which then fails loudly instead of silently
        # misreporting the release as absent. This test documents that
        # behavior so a future change can't quietly start assuming returncode
        # != 0 always means "genuinely missing" without a visible failure.
        create_calls = []

        def fake_run_gh(args, **kwargs):
            if args[:2] == ["release", "view"]:
                return _completed(1, stderr="authentication required")
            if args[:2] == ["release", "create"]:
                create_calls.append(args)
                return _completed(1, stderr="authentication required")
            raise AssertionError(f"unexpected gh call: {args}")

        monkeypatch.setattr(ddr, "run_gh", fake_run_gh)
        with pytest.raises(SystemExit):
            ddr.upload_asset("/tmp/region.sqlite.gz", "routing-databases-latest", "org/repo")
        assert create_calls  # attempted create, surfaced the real error via exit(1)
