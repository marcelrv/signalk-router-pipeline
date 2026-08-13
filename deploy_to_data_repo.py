#!/usr/bin/env python3
"""
Deploy a routing database to the signalk-router-data repository.

Zips the .sqlite file and places it in the correct folder structure
under the data-repo directory, then regenerates index.json.

Usage:
    python3 backend/deploy_to_data_repo.py \
        --input ./netherlands.sqlite \
        --continent europe \
        --country nl \
        --region netherlands \
        --no-generate-index \
        --data-repo /home/node/signalkdev/router-data
"""

import os
import sys
import json
import gzip
import shutil
import hashlib
import sqlite3
import argparse
import subprocess
from pathlib import Path


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            buf = f.read(65536)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def read_metadata_for_verify(db_path: str) -> dict | None:
    """Quick metadata read for verification before deployment."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='metadata'")
        if not cur.fetchone():
            conn.close()
            return None
        cur.execute("SELECT country, name, description, schema_version FROM metadata LIMIT 1")
        row = cur.fetchone()
        conn.close()
        if not row:
            return None
        return dict(row)
    except Exception as e:
        return None


def deploy(args):
    input_path = os.path.abspath(args.input)
    data_repo = os.path.abspath(args.data_repo)
    continent = args.continent
    country = args.country
    region = args.region

    # Validate input file
    if not os.path.isfile(input_path):
        print(f"ERROR: input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    if not input_path.endswith('.sqlite'):
        print(f"WARNING: input file does not end with .sqlite: {input_path}", file=sys.stderr)

    # Verify metadata exists in the database
    meta = read_metadata_for_verify(input_path)
    if meta is None:
        print(f"ERROR: {input_path} has no metadata table or invalid schema. Run the pipeline with --tags etc.", file=sys.stderr)
        sys.exit(1)

    print(f"Database: {meta.get('country', '?')} — {meta.get('name', '?')} (schema v{meta.get('schema_version', '?')})", file=sys.stderr)

    # Determine output paths
    regions_dir = os.path.join(data_repo, 'regions', continent, country)
    os.makedirs(regions_dir, exist_ok=True)

    base_name = region  # e.g. "netherlands"
    sqlite_filename = f"{base_name}.sqlite"
    zip_filename = f"{base_name}.sqlite.gz"
    zip_path = os.path.join(regions_dir, zip_filename)

    # Gzip the .sqlite file (gzip is smaller than zip, native Python support, and
    # decompress-on-the-fly via Content-Encoding if served from a proper web server,
    # though our GitHub raw use-case will just download and decompress locally)
    print(f"Compressing {input_path} -> {zip_path} ...", file=sys.stderr)
    input_size = os.path.getsize(input_path)
    with open(input_path, 'rb') as f_in:
        with gzip.open(zip_path, 'wb', compresslevel=9) as f_out:
            shutil.copyfileobj(f_in, f_out)
    zip_size = os.path.getsize(zip_path)
    ratio = (1 - zip_size / input_size) * 100
    print(f"  {input_size / 1048576:.1f} MB -> {zip_size / 1048576:.1f} MB ({ratio:.0f}% compression)", file=sys.stderr)

    # Compute sha256 of the .gz file
    checksum = sha256_file(zip_path)

    # Verify the .gz is valid by decompressing to temp
    temp_dir = os.path.join(data_repo, '.tmp_verify')
    os.makedirs(temp_dir, exist_ok=True)
    verify_path = os.path.join(temp_dir, sqlite_filename)
    try:
        with gzip.open(zip_path, 'rb') as f_in:
            with open(verify_path, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
        # Quick sanity check
        conn = sqlite3.connect(verify_path)
        conn.execute("SELECT COUNT(*) FROM nodes").fetchone()
        conn.close()
        os.remove(verify_path)
        os.rmdir(temp_dir)
    except Exception as e:
        os.remove(zip_path)
        shutil.rmtree(temp_dir, ignore_errors=True)
        print(f"ERROR: compressed file verification failed: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"  SHA256: {checksum}", file=sys.stderr)

    # Write a companion .metadata.json with the info the index script needs
    # (So generate_index.py doesn't need to decompress every DB just to read metadata)
    metadata_json_path = os.path.join(regions_dir, f"{base_name}.metadata.json")
    # Copy the sqlite metadata file alongside for faster index generation
    # The index script can use this instead of decompressing
    meta_out = {
        "country": meta["country"],
        "name": meta["name"],
        "description": meta.get("description", ""),
        "schema_version": meta.get("schema_version", 1),
    }
    with open(metadata_json_path, 'w') as f:
        json.dump(meta_out, f, indent=2)

    # Also copy a clean copy of the .sqlite for direct local use (not committed to git)
    # but create a symlink instead to save space
    sqlite_local_path = os.path.join(data_repo, 'regions', continent, country, sqlite_filename)
    if os.path.exists(sqlite_local_path):
        os.remove(sqlite_local_path)

    print(f"\nDeployed: {zip_path}", file=sys.stderr)
    print(f"  Size: {zip_size} bytes ({zip_size / 1048576:.1f} MB)", file=sys.stderr)
    print(f"  Inner file: {sqlite_filename}", file=sys.stderr)
    print(f"  Metadata: {metadata_json_path}", file=sys.stderr)

    # Optionally regenerate index.json
    if args.generate_index:
        print("\nRegenerating index.json and coverage map...", file=sys.stderr)
        index_script = os.path.join(data_repo, 'scripts', 'generate_index.py')
        if os.path.isfile(index_script):
            result = subprocess.run(
                [sys.executable, index_script,
                 '--regions-dir', os.path.join(data_repo, 'regions'),
                 '--output-dir', data_repo],
                capture_output=True, text=True
            )
            print(result.stdout, file=sys.stderr)
            if result.returncode != 0:
                print(f"WARNING: index generation failed:\n{result.stderr}", file=sys.stderr)
        else:
            print(f"WARNING: {index_script} not found", file=sys.stderr)

    print("\nDone. Commit and push the data-repo to publish.", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="Deploy a routing database to the signalk-router-data repository.\n"
                    "Compresses the .sqlite with gzip and places it in the correct folder.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--input", required=True, help="Path to the .sqlite file to deploy")
    parser.add_argument("--continent", required=True,
                        help="Continent folder: europe, north-america, asia, etc.")
    parser.add_argument("--country", required=True,
                        help="Country slug (lowercase): nl, be, de, usa, etc.")
    parser.add_argument("--region", required=True,
                        help="Region slug: netherlands, belgium, chesapeake-bay, etc.")
    parser.add_argument("--data-repo", required=True,
                        help="Path to the local clone of signalk-router-data")
    parser.add_argument("--generate-index", action="store_true", default=True,
                        help="Regenerate index.json after deploy (default: true)")
    parser.add_argument("--no-generate-index", action="store_false", dest="generate_index",
                        help="Skip index.json regeneration")

    args = parser.parse_args()
    deploy(args)


if __name__ == "__main__":
    main()
