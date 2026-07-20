#!/usr/bin/env bash
# Build a routing-graph SQLite database for a NOAA ENC region end-to-end:
#   1. download NOAA ENC .000 charts for the region (scripts/download_noaa.py)
#   2. extract S-57 layers to GeoJSON        (enc_preprocessor.py)
#   3. build the routing graph SQLite         (nautical_routing_pipeline.py)
#
# Usage:
#   ./build_region.sh <region> [--force] [--name "..."] [--depth-ceiling 6.0]
#
# Sub-region mode — compose a custom group from states already downloaded
# under another region's raw dir (skips re-downloading), optionally clipped
# to a bounding box afterward (e.g. to drop non-Atlantic cells bundled into
# a state's NOAA ZIP, like Great Lakes/Finger Lakes cells in NY's):
#   ./build_region.sh <name> --states ME,NH,MA,RI,CT [--source-region us-east-coast]
#                      [--clip-bbox "min_lon,min_lat,max_lon,max_lat"] [--overlap-deg 0.02]
#
# Examples:
#   ./build_region.sh us-east-coast
#   ./build_region.sh us-caribbean --force
#   ./build_region.sh us-east-new-england --states ME,NH,MA,RI,CT --name "New England"
#   ./build_region.sh us-east-ny-metro --states NY --clip-bbox "-74.5,40.4,-71.7,41.4" --name "NY Harbor & Long Island Sound"
#
# Run scripts/download_noaa.py --list-regions to see all region keys.
set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$BACKEND_DIR"

if [ $# -lt 1 ]; then
    echo "Usage: $0 <region> [--force] [--name \"Human Name\"] [--depth-ceiling 6.0]" >&2
    echo "       $0 <name> --states ST1,ST2 [--source-region us-east-coast] [--clip-bbox \"min_lon,min_lat,max_lon,max_lat\"] [--overlap-deg 0.02]" >&2
    echo "Run scripts/download_noaa.py --list-regions to see available region keys." >&2
    exit 1
fi

REGION="$1"; shift

FORCE=""
NAME=""
DEPTH_CEILING="6.0"
STATES=""
SOURCE_REGION="us-east-coast"
CLIP_BBOX=""
OVERLAP_DEG=""
while [ $# -gt 0 ]; do
    case "$1" in
        --force) FORCE="--force"; shift ;;
        --name) NAME="$2"; shift 2 ;;
        --depth-ceiling) DEPTH_CEILING="$2"; shift 2 ;;
        --states) STATES="$2"; shift 2 ;;
        --source-region) SOURCE_REGION="$2"; shift 2 ;;
        --clip-bbox) CLIP_BBOX="$2"; shift 2 ;;
        --overlap-deg) OVERLAP_DEG="$2"; shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# Human-readable name/description per known region key (override with --name).
case "$REGION" in
    us-east-coast)   DEFAULT_NAME="US East Coast";   DESCRIPTION="US East Coast (Maine to Georgia) coastal waters, based on NOAA ENCs" ;;
    us-gulf-coast)   DEFAULT_NAME="US Gulf Coast";   DESCRIPTION="US Gulf Coast (Florida to Texas) coastal waters, based on NOAA ENCs" ;;
    us-west-coast)   DEFAULT_NAME="US West Coast";   DESCRIPTION="US West Coast (California to Washington) coastal waters, based on NOAA ENCs" ;;
    us-alaska)       DEFAULT_NAME="Alaska";          DESCRIPTION="Alaska coastal waters, based on NOAA ENCs" ;;
    us-hawaii-pacific) DEFAULT_NAME="Hawaii";        DESCRIPTION="Hawaii and Pacific territories coastal waters, based on NOAA ENCs" ;;
    us-great-lakes)  DEFAULT_NAME="Great Lakes";     DESCRIPTION="US Great Lakes and connecting channels, based on NOAA ENCs" ;;
    us-caribbean)    DEFAULT_NAME="Puerto Rico";     DESCRIPTION="Puerto Rico and US Virgin Islands, based on NOAA ENCs" ;;
    *)               DEFAULT_NAME="$REGION";         DESCRIPTION="US coastal waters ($REGION), based on NOAA ENCs" ;;
esac
NAME="${NAME:-$DEFAULT_NAME}"

RAW_DIR="data/raw/$REGION"
GEOJSON_DIR="data/geojson/$REGION"
OUTPUT="data/${REGION//-/_}.sqlite"
LOG_PREFIX="data/${REGION//-/_}"

PYTHON="$BACKEND_DIR/.venv/bin/python3"
if [ ! -x "$PYTHON" ]; then
    echo "No venv found at .venv — run ./install.sh first." >&2
    exit 1
fi

mkdir -p data/raw data/geojson

step() { echo; echo "=== [$REGION] $1 ==="; }

if [ -n "$STATES" ]; then
    step "1/3 compose from already-downloaded states ($SOURCE_REGION): $STATES"
    rm -rf "$RAW_DIR"
    mkdir -p "$RAW_DIR"
    IFS=',' read -ra STATE_ARR <<< "$STATES"
    for st in "${STATE_ARR[@]}"; do
        src="data/raw/$SOURCE_REGION/$st"
        if [ ! -d "$src" ]; then
            echo "  Missing $src — run: $PYTHON scripts/download_noaa.py --region $SOURCE_REGION" >&2
            exit 1
        fi
        ln -s "$BACKEND_DIR/$src" "$RAW_DIR/$st"
        echo "  linked $src -> $RAW_DIR/$st"
    done
else
    step "1/3 download NOAA ENC charts -> $RAW_DIR"
    time "$PYTHON" scripts/download_noaa.py --region "$REGION" --output-dir data/raw $FORCE \
        2>&1 | tee "${LOG_PREFIX}_download.log"
fi

step "2/3 extract S-57 layers to GeoJSON -> $GEOJSON_DIR"
if [ -n "$FORCE" ] || [ ! -d "$GEOJSON_DIR" ] || [ -z "$(ls -A "$GEOJSON_DIR" 2>/dev/null)" ]; then
    time "$PYTHON" enc_preprocessor.py --input "$RAW_DIR" --output "$GEOJSON_DIR" \
        2>&1 | tee "${LOG_PREFIX}_preprocess.log"
else
    echo "  $GEOJSON_DIR already populated, skipping (use --force to redo)."
fi

if [ -n "$CLIP_BBOX" ]; then
    CLIPPED_DIR="data/geojson/${REGION}_clipped"
    OVERLAP_ARGS=()
    if [ -n "$OVERLAP_DEG" ]; then
        OVERLAP_ARGS=(--overlap-deg "$OVERLAP_DEG")
    fi
    step "2b/3 clip to bbox $CLIP_BBOX -> $CLIPPED_DIR${OVERLAP_DEG:+ (overlap ${OVERLAP_DEG}deg)}"
    time "$PYTHON" clip_pilot_data.py --input-dir "$GEOJSON_DIR" --bbox="$CLIP_BBOX" --output-dir "$CLIPPED_DIR" "${OVERLAP_ARGS[@]}" \
        2>&1 | tee "${LOG_PREFIX}_clip.log"
    GEOJSON_DIR="$CLIPPED_DIR"
fi

step "3/3 build routing graph -> $OUTPUT"
time "$PYTHON" nautical_routing_pipeline.py \
    --input-dir "$GEOJSON_DIR" \
    --output "$OUTPUT" \
    --country US \
    --name "$NAME" \
    --description "$DESCRIPTION" \
    --tags '["noaa","enc","coastal"]' \
    --url "https://github.com/marcelrv/signalk-router-data" \
    --license "Public Domain (NOAA)" \
    --copyright "NOAA Office of Coast Survey" \
    --depth-ceiling "$DEPTH_CEILING" \
    2>&1 | tee "${LOG_PREFIX}_build.log"

echo
echo "=== [$REGION] Done: $OUTPUT ==="
ls -lh "$OUTPUT"
