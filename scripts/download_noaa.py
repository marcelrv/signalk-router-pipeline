#!/usr/bin/env python3
"""Download NOAA ENC chart files by region (state ZIP bundles).

NOAA publishes free, unencrypted S-57 ENCs at:
  https://charts.noaa.gov/ENCs/ENCs.shtml

Each US state/territory has a ZIP bundle at:
  https://charts.noaa.gov/ENCs/{state}_ENCs.zip

This script downloads the ZIPs for one or more predefined
regions and unpacks the .000 S-57 files into a local directory.

Usage:
  # List available regions
  python3 scripts/download_noaa.py --list-regions

  # Download a specific region
  python3 scripts/download_noaa.py --region us-east-coast

  # Download all regions (full US)
  python3 scripts/download_noaa.py --region all

  # Force re-download
  python3 scripts/download_noaa.py --region us-east-coast --force
"""

import argparse
import json
import logging
import os
import sys
import zipfile
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BASE_URL = "https://charts.noaa.gov/ENCs/"
STATE_ZIP_TPL = BASE_URL + "{state}_ENCs.zip"
MANIFEST_FILENAME = "manifest.json"

# States/territories available as NOAA ENC ZIP bundles
# (extracted from the NOAA ENC download page)
ALL_STATES = [
    "AK", "AL", "AQ", "BS", "CA", "CT", "DE", "FL", "FM",
    "GA", "GT", "HI", "HT", "ID", "IL", "IN", "LA", "MA",
    "MD", "ME", "MH", "MI", "MN", "MS", "NC", "NH", "NJ",
    "NV", "NY", "OH", "OR", "PA", "PO", "PW", "PR", "RI",
    "SC", "SP", "TX", "VA", "VT", "WA", "WI",
]

REGIONS = {
    "us-east-coast": {
        "states": ["ME", "NH", "MA", "RI", "CT", "NY", "NJ", "DE", "MD", "VA", "NC", "SC", "GA"],
        "description": "US East Coast — Maine to Georgia (Atlantic seaboard)",
    },
    "us-gulf-coast": {
        "states": ["FL", "AL", "MS", "LA", "TX"],
        "description": "US Gulf Coast — Florida to Texas (Gulf of Mexico)",
    },
    "us-west-coast": {
        "states": ["CA", "OR", "WA"],
        "description": "US West Coast — California to Washington (Pacific)",
    },
    "us-alaska": {
        "states": ["AK"],
        "description": "Alaska — including Aleutian Islands and Arctic coast",
    },
    "us-hawaii-pacific": {
        "states": ["HI"],
        "description": "Hawaii and Pacific territories",
    },
    "us-great-lakes": {
        "states": ["MI", "OH", "PA", "IN", "IL", "WI", "MN"],
        "description": "Great Lakes — all five lakes and connecting channels",
    },
    "us-caribbean": {
        "states": ["PR"],
        "description": "Puerto Rico and US Virgin Islands (Caribbean)",
    },
}


def load_manifest(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_manifest(manifest: dict, path: str):
    with open(path, "w") as f:
        json.dump(manifest, f, indent=2)


def state_zip_url(state: str) -> str:
    return STATE_ZIP_TPL.format(state=state)


def download_state_zip(state: str, output_dir: str) -> str | None:
    url = state_zip_url(state)
    dest = os.path.join(output_dir, f"{state}_ENCs.zip")

    logger.info("Downloading %s ENCs from %s ...", state, url)

    req = Request(url)
    try:
        resp = urlopen(req, timeout=600)
    except HTTPError as e:
        if e.code == 404:
            logger.warning("No ENC bundle for %s (404)", state)
        else:
            logger.error("HTTP %d downloading %s: %s", e.code, state, e)
        return None
    except URLError as e:
        logger.error("Failed to download %s: %s", state, e)
        return None
    except Exception as e:
        logger.error("Unexpected error downloading %s: %s", state, e)
        return None

    content = resp.read()
    # Check for zero-byte or tiny files (NOAA serves 0 MB for inland states)
    if len(content) < 1000:
        logger.info("Skipping %s — empty bundle (%d bytes)", state, len(content))
        return None

    os.makedirs(output_dir, exist_ok=True)
    with open(dest, "wb") as f:
        f.write(content)
    logger.info("Saved %s (%d bytes)", dest, len(content))
    return dest


def unzip_state(state: str, zip_path: str, output_dir: str) -> bool:
    chart_dir = os.path.join(output_dir, state)
    os.makedirs(chart_dir, exist_ok=True)

    logger.info("Unpacking %s ENCs into %s ...", state, chart_dir)
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            members = zf.namelist()
            enc_files = [m for m in members if m.upper().endswith(".000")]
            other_files = [m for m in members if not m.upper().endswith(".000")]
            logger.debug("  %d .000 files, %d other files in %s ZIP",
                         len(enc_files), len(other_files), state)
            zf.extractall(chart_dir)
        logger.info("  Extracted %d files to %s", len(members), chart_dir)
        return True
    except zipfile.BadZipFile:
        logger.warning("  Bad zip file for %s", state)
        return False


def process_region(region: str, output_base: str,
                   force: bool, keep_zips: bool) -> int:
    region_names = list(REGIONS.keys()) if region == "all" else [region]

    total_downloaded = 0

    for rname in region_names:
        region_info = REGIONS.get(rname)
        if not region_info:
            logger.error("Unknown region: %s. Use --list-regions to see available regions.", rname)
            continue

        region_dir = os.path.join(output_base, rname)
        manifest_path = os.path.join(region_dir, MANIFEST_FILENAME)
        region_manifest = {} if force else load_manifest(manifest_path)

        logger.info("=== Processing region: %s (%s) ===",
                    rname, region_info["description"])
        logger.info("  States: %s", ", ".join(region_info["states"]))

        for state in region_info["states"]:
            if not force and region_manifest.get(state) is not None:
                logger.debug("  %s — already downloaded, skipping", state)
                continue

            zip_path = download_state_zip(state, region_dir)
            if zip_path:
                unzip_state(state, zip_path, region_dir)
                if not keep_zips:
                    os.remove(zip_path)
                    logger.debug("  Removed zip for %s", state)

                region_manifest[state] = {
                    "downloaded": datetime.now(timezone.utc).isoformat(),
                }
                save_manifest(region_manifest, manifest_path)
                total_downloaded += 1
            else:
                region_manifest[state] = {
                    "downloaded": None,
                    "note": "No data available or download failed",
                }
                save_manifest(region_manifest, manifest_path)

        enc_count = sum(1 for _ in _find_000_files(region_dir))
        logger.info("  Region %s: %d ENC .000 files", rname, enc_count)

    return total_downloaded


def _find_000_files(directory: str) -> list[str]:
    results = []
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for f in files:
            if f.upper().endswith('.000'):
                results.append(os.path.join(root, f))
    return results


def list_regions():
    print("\nAvailable NOAA ENC regions:\n")
    print(f"  {'Region Name':<20} {'States':<30} Description")
    print(f"  {'-'*20} {'-'*30} {'-'*40}")
    for name, info in REGIONS.items():
        states = ", ".join(info["states"])
        print(f"  {name:<20} {states:<30} {info['description']}")
    print()
    print("  all                          All 7 regions combined (full US coverage)")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Download and unpack NOAA ENC chart files by region",
    )
    parser.add_argument("--region", "-r", default="us-caribbean",
                        help="Region to download (default: us-caribbean). Use 'all' for all regions.")
    parser.add_argument("--output-dir",
                        default=os.path.join(os.path.dirname(__file__), "..", "data", "raw"),
                        help="Target base directory for downloaded files (default: data/raw/)")
    parser.add_argument("--keep-zips", action="store_true",
                        help="Keep the downloaded zip files after extraction")
    parser.add_argument("--force", action="store_true",
                        help="Force re-download all files regardless of manifest")
    parser.add_argument("--list-regions", action="store_true",
                        help="List available regions and exit")
    parser.add_argument("--list-files", action="store_true",
                        help="After download, list all .000 files in the output directory")

    args = parser.parse_args()

    if args.list_regions:
        list_regions()
        return

    if args.region != "all" and args.region not in REGIONS:
        logger.error("Unknown region: %s", args.region)
        list_regions()
        sys.exit(1)

    if args.region == "all":
        logger.info("Downloading all %d US regions sequentially...", len(REGIONS))

    downloaded = process_region(args.region, args.output_dir, args.force, args.keep_zips)

    logger.info("Done. Downloaded %d state ZIP(s).", downloaded)

    if args.list_files:
        search_dir = args.output_dir if args.region == "all" else os.path.join(args.output_dir, args.region)
        files = _find_000_files(search_dir)
        print(f"\n{len(files)} ENC .000 files found in {search_dir}:")
        for f in sorted(files):
            rel = os.path.relpath(f, args.output_dir)
            size = os.path.getsize(f)
            print(f"  {rel} ({size // 1024} KB)")


if __name__ == "__main__":
    main()
