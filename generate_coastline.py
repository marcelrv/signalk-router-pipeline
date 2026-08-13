#!/usr/bin/env python3
"""
Generate the bundled world-countries backdrop for the Data Manager minimap.

Downloads Natural Earth 1:50m admin-0 countries (public domain), lightly
simplifies it, trims attributes, and writes a standard GeoJSON
FeatureCollection to public/world-countries.json.

This is a DEV-TIME tool only. The plugin never fetches anything at runtime —
it serves the committed file from public/ so the minimap works fully offline.

Requirements: ogr2ogr (GDAL) and curl on PATH.

Output: ~1.1 MB GeoJSON (242 country features, borders + NAME + ISO_A2),
recognizable at minimap scale while staying well under a ~2 MB pack budget.
"""

import os
import subprocess
import sys
import tempfile

SOURCE_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
    "master/geojson/ne_50m_admin_0_countries.geojson"
)
SIMPLIFY_DEG = "0.02"      # ~2 km tolerance — fine for a small backdrop map
COORD_PRECISION = "3"      # ~110 m — plenty for a minimap

OUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "public", "world-countries.json",
)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        raw = os.path.join(tmp, "ne_50m_countries.geojson")
        print(f"Downloading Natural Earth 50m countries...")
        subprocess.run(["curl", "-sSL", "-o", raw, SOURCE_URL], check=True)
        if not os.path.exists(raw) or os.path.getsize(raw) < 1000:
            sys.exit("Download failed or file too small")

        print(f"Simplifying (tolerance={SIMPLIFY_DEG}deg, precision={COORD_PRECISION})...")
        if os.path.exists(OUT_PATH):
            os.remove(OUT_PATH)
        subprocess.run([
            "ogr2ogr", "-f", "GeoJSON",
            "-simplify", SIMPLIFY_DEG,
            "-lco", f"COORDINATE_PRECISION={COORD_PRECISION}",
            "-select", "NAME,ISO_A2",
            OUT_PATH, raw,
        ], check=True)

    size_kb = os.path.getsize(OUT_PATH) / 1024
    print(f"Wrote {OUT_PATH} ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
