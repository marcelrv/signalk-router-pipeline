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
#                      [--channel-axes] [--channel-axes-args "--min-confidence 0.6"]
#                      [--stitch-registry data/seam_registry.sqlite]
#                      [--extra-pipeline-args "--sagitta-cap 250.0 --node-merge-m 5.0"]
#                      [--build-mem-limit-gb 11]
#
# Round 25 cross-database seam stitching: pass --stitch-registry to adopt/publish
# shared seam nodes against a global-node registry SQLite (see STITCHING_DESIGN.md
# Section 3). When --clip-bbox is also given, the region's --coverage-bbox (passed
# to nautical_routing_pipeline.py) is derived automatically as --clip-bbox expanded
# by --overlap-deg -- the same expansion clip_pilot_data.py itself applies, so it
# matches the actual clipped data extent. Omit --stitch-registry entirely for
# unchanged single-region behavior.
#
# --extra-pipeline-args "..." passes its value through verbatim (word-split) to
# the nautical_routing_pipeline.py invocation in step 3/3, appended after this
# script's own flags -- e.g. the density-tuning flags
# (--sagitta-cap/--axis-dedup-cap/--node-merge-m/etc., see SPEC-GRAPH-DENSITY.md)
# without hand-editing this script per run.
#
# Step 3/3 (the routing-graph build) runs under a default `ulimit -v` memory
# ceiling (data/BUILD_LOG.md build #32: _split_wide_narrow's erosion step can
# exhaust GEOS's own working memory on a huge/complex coastal_water component;
# _safe_negative_buffer already degrades gracefully on a catchable
# GEOSException/MemoryError, but an unbounded process can instead be killed by
# the Linux OOM-killer once whole-system memory runs low on a shared host -- an
# uncatchable SIGKILL that can take down unrelated processes too. The ceiling
# converts that into a clean, catchable failure inside the pipeline itself).
# Override with --build-mem-limit-gb <N> or the SK_ROUTING_BUILD_MEM_LIMIT_GB
# env var; 0 disables the ceiling entirely for a region that legitimately needs
# more.
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
    echo "       $0 <name> --states ST1,ST2 [--source-region us-east-coast] [--clip-bbox \"min_lon,min_lat,max_lon,max_lat\"] [--overlap-deg 0.02] [--extra-pipeline-args \"...\"]" >&2
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
STITCH_REGISTRY=""
STITCH_BAND_M=""
STITCH_RADIUS_M=""
EXTRA_PIPELINE_ARGS=""
CHANNEL_AXES=""            # --channel-axes: derive marked-channel axes and feed them to the pipeline
CHANNEL_AXES_ARGS=""       # --channel-axes-args "...": extra derive_channel_axes.py options
BUILD_MEM_LIMIT_GB="${SK_ROUTING_BUILD_MEM_LIMIT_GB-11}"  # unset (no colon) -- an
                                                           # explicitly empty env var
                                                           # override means "disabled",
                                                           # same as an explicitly empty
                                                           # --build-mem-limit-gb; only
                                                           # a genuinely UNSET var falls
                                                           # back to the 11GB default.
while [ $# -gt 0 ]; do
    case "$1" in
        --force) FORCE="--force"; shift ;;
        --name) NAME="$2"; shift 2 ;;
        --depth-ceiling) DEPTH_CEILING="$2"; shift 2 ;;
        --states) STATES="$2"; shift 2 ;;
        --source-region) SOURCE_REGION="$2"; shift 2 ;;
        --clip-bbox) CLIP_BBOX="$2"; shift 2 ;;
        --overlap-deg) OVERLAP_DEG="$2"; shift 2 ;;
        --stitch-registry) STITCH_REGISTRY="$2"; shift 2 ;;
        --stitch-band-m) STITCH_BAND_M="$2"; shift 2 ;;
        --stitch-radius-m) STITCH_RADIUS_M="$2"; shift 2 ;;
        --extra-pipeline-args) EXTRA_PIPELINE_ARGS="$2"; shift 2 ;;
        --channel-axes) CHANNEL_AXES="1"; shift ;;
        --channel-axes-args) CHANNEL_AXES_ARGS="$2"; shift 2 ;;
        --build-mem-limit-gb)
            if [ "$#" -lt 2 ]; then
                echo "Error: --build-mem-limit-gb requires a value." >&2
                exit 1
            fi
            BUILD_MEM_LIMIT_GB="$2"; shift 2 ;;
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

if [ -n "$CHANNEL_AXES" ]; then
    # docs/SPEC-CHANNEL-AXES.md: derive marked-channel axis lines (centerlines of
    # FAIRWY/DRGARE polygons and of lateral buoy/beacon chains) from the layers the
    # pipeline is about to read. Runs on the (clipped) input dir so the axes match
    # the build's own extent; writes channel_axes_lines.geojson + a rejected layer
    # and stats JSON next to the other layers.
    step "2c/3 derive marked-channel axes -> $GEOJSON_DIR/channel_axes_lines.geojson"
    time "$PYTHON" derive_channel_axes.py --input-dir "$GEOJSON_DIR" $CHANNEL_AXES_ARGS \
        2>&1 | tee "${LOG_PREFIX}_channel_axes.log"
    EXTRA_PIPELINE_ARGS="$EXTRA_PIPELINE_ARGS --channel-axes"
fi

STITCH_ARGS=()
if [ -n "$STITCH_REGISTRY" ]; then
    STITCH_ARGS+=(--stitch-registry "$STITCH_REGISTRY")
    if [ -n "$CLIP_BBOX" ]; then
        # Coverage bbox = --clip-bbox expanded by --overlap-deg -- the SAME
        # expansion clip_pilot_data.py itself applies before writing the
        # GeoJSON this build reads, so it matches the actual clipped data
        # extent (Round 25, STITCHING_DESIGN.md Section 3.5).
        COVERAGE_BBOX=$("$PYTHON" - "$CLIP_BBOX" "${OVERLAP_DEG:-0}" <<'PYEOF'
import sys
b = [float(x) for x in sys.argv[1].split(",")]
o = float(sys.argv[2] or 0)
print(f"{b[0]-o},{b[1]-o},{b[2]+o},{b[3]+o}")
PYEOF
)
        # =-form is required, not stylistic: every US coverage bbox starts with a
        # negative longitude, and argparse only tolerates a leading "-" on a value
        # that is a bare number ("-74.3"), not on "-74.3,40.4,-71.7,42.9". Passed
        # as two argv entries it fails with "expected one argument" -- which broke
        # the whole stitching path for US regions. Same reason --bbox above uses it.
        STITCH_ARGS+=("--coverage-bbox=$COVERAGE_BBOX")
    fi
    if [ -n "$STITCH_BAND_M" ]; then
        STITCH_ARGS+=(--stitch-band-m "$STITCH_BAND_M")
    fi
    if [ -n "$STITCH_RADIUS_M" ]; then
        STITCH_ARGS+=(--stitch-radius-m "$STITCH_RADIUS_M")
    fi
fi

# Validate before it ever reaches Bash arithmetic ($(( )) below): an unvalidated
# value there is evaluated as an ARITHMETIC EXPRESSION, not just a number (e.g.
# "1+2" silently becomes a 3GB limit), and a malformed one (empty already handled
# above, but e.g. non-numeric or negative) can abort the whole step-3 subshell
# under `set -euo pipefail` with a cryptic error instead of a clear one. Empty
# and "0" are the two valid "disabled" spellings already handled by the `-n`/
# `!= "0"` checks below; anything else must be a plain non-negative integer.
# Normalize to ONE canonical numeric value, used for every enabled/disabled
# check and the arithmetic below -- previously each call site re-checked
# `[ -n ... ] && [ != "0" ]` as a STRING comparison, which a value like "00"
# or "000" passes validation but is never EQUAL to the string "0": that took
# the "enabled" branch with a normalized value of 0, i.e. `ulimit -v 0`,
# which would have prevented the routing process from starting at all.
# BUILD_MEM_LIMIT_GB_NUM=0 is the single, unambiguous "disabled" state.
BUILD_MEM_LIMIT_GB_NUM=0
if [ -n "$BUILD_MEM_LIMIT_GB" ]; then
    case "$BUILD_MEM_LIMIT_GB" in
        ''|*[!0-9]*)
            echo "Error: --build-mem-limit-gb/SK_ROUTING_BUILD_MEM_LIMIT_GB must be a" >&2
            echo "  plain non-negative integer (GB), or empty/0 to disable the ceiling" >&2
            echo "  (got: '$BUILD_MEM_LIMIT_GB')." >&2
            exit 1
            ;;
    esac
    # 10# forces base-10 parsing -- Bash arithmetic otherwise treats a
    # leading-zero value (e.g. "08", plausible from a hand-typed
    # --build-mem-limit-gb) as octal, and "08"/"09" are invalid octal
    # literals, aborting the script under set -euo pipefail.
    BUILD_MEM_LIMIT_GB_NUM=$((10#$BUILD_MEM_LIMIT_GB))
fi

EXTRA_PIPELINE_ARGS_ARR=()
if [ -n "$EXTRA_PIPELINE_ARGS" ]; then
    # Word-split on purpose (like $FORCE above) -- this is a plain space-
    # separated list of flags/values (e.g. "--sagitta-cap 250.0 --node-merge-m
    # 5.0"), not a single token, so it must NOT be double-quoted below.
    read -ra EXTRA_PIPELINE_ARGS_ARR <<< "$EXTRA_PIPELINE_ARGS"
fi

if [ "$BUILD_MEM_LIMIT_GB_NUM" -gt 0 ]; then
    step "3/3 build routing graph -> $OUTPUT (memory ceiling: ${BUILD_MEM_LIMIT_GB_NUM}GB)"
else
    step "3/3 build routing graph -> $OUTPUT (no memory ceiling)"
fi
(
    if [ "$BUILD_MEM_LIMIT_GB_NUM" -gt 0 ]; then
        # ulimit -v is in KB; only scopes this subshell and its children, so
        # steps 1/3 and 2/3 above (already run) and the rest of this script
        # after step 3/3 completes are unaffected.
        #
        # Plain `ulimit -v N` (no -S/-H) sets BOTH the soft and hard limit to
        # N -- two real failure modes confirmed directly, not just a style
        # nit: (1) if this process already inherited a lower HARD limit (some
        # outer constraint, e.g. this exact host's own shared-resource
        # limits), trying to raise it to N fails outright ("cannot modify
        # limit: Invalid argument"), aborting this whole subshell under
        # set -euo pipefail; (2) if the inherited SOFT limit is already lower
        # than N but the hard limit is not, `ulimit -v N` silently RAISES
        # that tighter existing constraint to N instead of respecting it.
        # Fix: compute the tightest of (configured, inherited soft, inherited
        # hard) and apply only that, only via -Sv (the soft limit alone) --
        # never attempts to exceed the inherited hard limit, and never
        # loosens an inherited soft limit that was already tighter.
        CONFIGURED_MEM_KB=$((BUILD_MEM_LIMIT_GB_NUM * 1024 * 1024))
        EFFECTIVE_MEM_KB=$CONFIGURED_MEM_KB
        INHERITED_SOFT_KB=$(ulimit -Sv)
        INHERITED_HARD_KB=$(ulimit -Hv)
        if [ "$INHERITED_SOFT_KB" != "unlimited" ] && [ "$INHERITED_SOFT_KB" -lt "$EFFECTIVE_MEM_KB" ]; then
            EFFECTIVE_MEM_KB="$INHERITED_SOFT_KB"
        fi
        if [ "$INHERITED_HARD_KB" != "unlimited" ] && [ "$INHERITED_HARD_KB" -lt "$EFFECTIVE_MEM_KB" ]; then
            EFFECTIVE_MEM_KB="$INHERITED_HARD_KB"
        fi
        if [ "$EFFECTIVE_MEM_KB" != "$CONFIGURED_MEM_KB" ]; then
            echo "  (inherited ulimit is tighter than ${BUILD_MEM_LIMIT_GB_NUM}GB -- using ${EFFECTIVE_MEM_KB}KB instead)"
        fi
        ulimit -Sv "$EFFECTIVE_MEM_KB"
    fi
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
        "${STITCH_ARGS[@]}" \
        "${EXTRA_PIPELINE_ARGS_ARR[@]}"
) 2>&1 | tee "${LOG_PREFIX}_build.log"

echo
echo "=== [$REGION] Done: $OUTPUT ==="
ls -lh "$OUTPUT"
