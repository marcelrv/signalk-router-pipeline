#!/usr/bin/env python3
"""Prepare and run the AI review of what Pass A left unresolved
(`docs/SPEC-GRAPH-CLEANUP.md` §6).

    # Prepare tiles only -- look at them yourself before spending anything
    ./review_region.py --db data/us_east_md_cleanup_a.sqlite \
        --input-dir data/geojson/us-east-md-v5_clipped \
        --out-dir data/review/md --prepare-only

    # Cheap sanity check with the offline mock backend (no API key needed)
    ./review_region.py --db ... --input-dir ... --out-dir data/review/md \
        --backend mock

    # A handful of tiles for real, before trusting the harness with a budget
    ./review_region.py --db ... --input-dir ... --out-dir data/review/md \
        --backend claude --limit 3

    # A local OpenAI-compatible server (llama.cpp); free, LAN only
    ./review_region.py --db ... --input-dir ... --out-dir data/review/md \
        --backend local --limit 3

    # The full gold-set run
    ./review_region.py --db ... --input-dir ... --out-dir data/review/md \
        --backend claude --ops-out data/md_ai_review.ops.jsonl
"""
import argparse
import glob
import os
import sys
from typing import List, Optional, Sequence

from graph_cleanup import RoutingGraph, ops as ops_mod
from graph_cleanup import candidates as candidates_mod
from graph_cleanup import prepare, runner, tiles as tiles_mod


def _find_tile_dirs(out_dir: str) -> List[str]:
    return sorted(d for d in glob.glob(os.path.join(out_dir, "*"))
                 if os.path.isdir(d) and os.path.exists(os.path.join(d, "context.json")))


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", required=True, help="routing .sqlite to review "
                   "(normally the output of apply_cleanup.py's Pass A)")
    p.add_argument("--input-dir", required=True,
                   help="clipped GeoJSON layers for chart context")
    p.add_argument("--out-dir", required=True, help="where tile directories live")
    p.add_argument("--max-stub-length-m", type=float,
                   default=3000.0, help="dead ends longer than this are assumed "
                   "real and skipped (default: %(default)s)")
    p.add_argument("--max-component-size", type=int, default=30,
                   help="components larger than this need Pass C, not this tool "
                        "(default: %(default)s)")
    p.add_argument("--tile-m", type=float, default=tiles_mod.DEFAULT_TILE_M,
                   help="review tile size in metres (default: %(default)s)")
    p.add_argument("--bbox", type=str, default=None,
                   metavar="MIN_LON,MIN_LAT,MAX_LON,MAX_LAT",
                   help="only review candidates whose anchor falls in this box "
                        "-- pilot one area before a full-region run")
    p.add_argument("--sample-tiles", type=int, default=None,
                   help="randomly sample this many tiles instead of every tile "
                        "the region produces -- for a gold-set run "
                        "(docs/SPEC-GRAPH-CLEANUP.md 6: 'stratified sample "
                        "(~300 tiles)'). This is a plain random sample, not "
                        "stratified by candidate kind or region -- pass "
                        "--bbox per area if you want that control")
    p.add_argument("--sample-seed", type=int, default=0,
                   help="seed for --sample-tiles, for a reproducible sample")
    p.add_argument("--prepare-only", action="store_true",
                   help="write tiles and stop -- look at them yourself first")
    p.add_argument("--backend", choices=("mock", "claude", "local"), default="mock",
                   help="mock costs nothing and proves the harness works; "
                        "claude is real money; local is an OpenAI-compatible "
                        "server (llama.cpp), see --local-url (default: %(default)s)")
    p.add_argument("--model", default=None, help="override the backend's default model")
    p.add_argument("--effort", default=None,
                   help="claude backend: low/medium/high/xhigh/max")
    p.add_argument("--local-url", default=None,
                   help="local backend: base URL incl. /v1 (default: $LOCAL_LLM_URL, "
                        "else http://192.168.10.111:8000/v1); $LOCAL_LLM_MODEL and "
                        "$LOCAL_LLM_API_KEY are honoured too")
    p.add_argument("--temperature", type=float, default=None,
                   help="local backend sampling temperature (default 0.2)")
    p.add_argument("--max-tokens", type=int, default=None,
                   help="local backend max completion tokens (default 4096)")
    p.add_argument("--timeout", type=float, default=None,
                   help="local backend per-request timeout in seconds (default 300)")
    p.add_argument("--connect-timeout", type=float, default=None,
                   help="local backend connect timeout in seconds (default 10); "
                        "separate from --timeout so a dead host fails fast")
    p.add_argument("--max-consecutive-backend-errors", type=int,
                   default=runner.DEFAULT_MAX_CONSECUTIVE_BACKEND_ERRORS,
                   help="abort the run after this many tiles in a row fail with a "
                        "backend error (dead/wedged server); 0 disables "
                        "(default: %(default)s)")
    p.add_argument("--max-consecutive-degraded", type=int,
                   default=runner.DEFAULT_MAX_CONSECUTIVE_DEGRADED,
                   help="abort after this many tiles in a row whose reply was unusable "
                        "(all-unsure fallback; HTTP 200 garbage never counts as a "
                        "backend error); 0 disables (default: %(default)s)")
    p.add_argument("--enable-thinking", action="store_true",
                   help="local backend: let a Qwen3-style model think before answering "
                        "(off by default; raise --max-tokens if you use this)")
    p.add_argument("--local-json-schema", action="store_true",
                   help="local backend: send a JSON-schema response_format so the "
                        "server constrains decoding to a valid answer")
    p.add_argument("--prompt-label", default=None,
                   help="local backend: free-text prompt version recorded in the "
                        "per-tile local_audit.jsonl (the prompt's sha256 is always recorded)")
    p.add_argument("--limit", type=int, default=None,
                   help="only answer the first N unanswered tiles -- use this "
                        "before trusting --backend claude with a full run")
    p.add_argument("--no-resume", action="store_true",
                   help="re-answer tiles that already have an answer.json")
    p.add_argument("--ops-out", help="write resulting drop ops here (ops.jsonl)")
    p.add_argument("--author", default=None,
                   help="op author tag; default is ai:<backend's model>")
    args = p.parse_args(argv)
    if args.backend == "local":
        from graph_cleanup.backends.local_openai import normalize_url
        try:  # fail before loading the graph / rendering tiles
            normalize_url(args.local_url)
        except ValueError as exc:
            p.error(f"--local-url/$LOCAL_LLM_URL: {exc}")

    print(f"loading {args.db}")
    g = RoutingGraph.load(args.db)
    print(f"  {len(g.nodes)} nodes / {len(g.edges)} edges")

    print("finding candidates...")
    cands = candidates_mod.find_all(g, db_path=args.db, input_dir=args.input_dir,
                                    max_stub_length_m=args.max_stub_length_m,
                                    max_component_size=args.max_component_size)
    by_kind: dict = {}
    for c in cands:
        by_kind[c.kind] = by_kind.get(c.kind, 0) + 1
    print(f"  {len(cands)} candidates: {by_kind}")

    if args.bbox:
        min_lon, min_lat, max_lon, max_lat = (float(x) for x in args.bbox.split(","))
        before = len(cands)
        cands = [c for c in cands
                if min_lon <= candidates_mod.anchor_latlon(g, c)[1] <= max_lon
                and min_lat <= candidates_mod.anchor_latlon(g, c)[0] <= max_lat]
        print(f"  --bbox restricts to {len(cands)}/{before} candidates")

    if not cands:
        print("nothing to review.")
        return 0

    all_tiles = tiles_mod.build_tiles(g, cands, tile_m=args.tile_m)
    print(f"  {len(all_tiles)} tiles "
          f"(avg {len(cands) / len(all_tiles):.1f} candidates/tile)")

    if args.sample_tiles and args.sample_tiles < len(all_tiles):
        import random
        rng = random.Random(args.sample_seed)
        all_tiles = rng.sample(all_tiles, args.sample_tiles)
        print(f"  --sample-tiles: reduced to {len(all_tiles)} tiles "
              f"(seed={args.sample_seed})")

    print(f"writing tiles to {args.out_dir}")
    prepare.write_all(all_tiles, g, args.out_dir, input_dir=args.input_dir)
    # This run's own tile directories, not a scan of --out-dir -- reusing
    # --out-dir across runs with a different --bbox/--sample-tiles would
    # otherwise pick up stale directories left over from an earlier, out-of-
    # scope run and silently answer/emit ops for them too.
    current_tile_dirs = [os.path.join(args.out_dir, tile.id) for tile in all_tiles]

    if args.prepare_only:
        print("prepared, not answered (--prepare-only). "
              f"Look at {args.out_dir}/<tile>/candidates.png before spending anything.")
        return 0

    if args.backend == "mock":
        from graph_cleanup.backends.mock import MockBackend
        backend = MockBackend()
        model_name = "mock"
    elif args.backend == "local":
        from graph_cleanup.backends.local_openai import LocalOpenAIBackend
        kwargs = {"enable_thinking": args.enable_thinking,
                  "json_schema": args.local_json_schema,
                  "prompt_label": args.prompt_label}
        for key, val in (("base_url", args.local_url), ("model", args.model),
                         ("temperature", args.temperature),
                         ("max_tokens", args.max_tokens), ("timeout", args.timeout),
                         ("connect_timeout", args.connect_timeout)):
            if val is not None:
                kwargs[key] = val
        backend = LocalOpenAIBackend(**kwargs)
        model_name = backend.model
        print(f"backend: local, url={backend.url}, model={model_name}, "
              f"temperature={backend.temperature}, thinking={backend.enable_thinking}")
        print("raw model output is audited per tile in <tile>/local_audit.jsonl")
    else:
        from graph_cleanup.backends.claude import ClaudeBackend, DEFAULT_MODEL, DEFAULT_EFFORT
        kwargs = {}
        if args.model:
            kwargs["model"] = args.model
        if args.effort:
            kwargs["effort"] = args.effort
        backend = ClaudeBackend(**kwargs)
        model_name = kwargs.get("model", DEFAULT_MODEL)
        print(f"backend: claude, model={model_name}, "
              f"effort={kwargs.get('effort', DEFAULT_EFFORT)}")
        print("this spends real money -- Ctrl-C now if that wasn't the intent.")

    tile_dirs = current_tile_dirs
    if args.limit:
        # a degraded (all-unsure fallback) or stale answer counts as pending
        pending = tile_dirs if args.no_resume else [
            d for d in tile_dirs if runner.answer_status(d) != "valid"]
        tile_dirs = pending[:args.limit]
        print(f"--limit {args.limit}: this run will answer {len(tile_dirs)} tile(s)")

    print(f"answering {len(tile_dirs)} tiles...")
    try:
        stats = runner.run_all(
            backend, tile_dirs, resume=not args.no_resume,
            max_consecutive_backend_errors=args.max_consecutive_backend_errors,
            max_consecutive_degraded=args.max_consecutive_degraded)
    except runner.BackendCircuitOpen as exc:
        print(f"  {exc.stats.summary()}")
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"  {stats.summary()}")
    if hasattr(backend, "summary"):
        print(f"  {backend.summary()}")

    if args.ops_out:
        author = args.author or f"ai:{model_name}"
        try:
            result_ops = runner.answers_to_ops(current_tile_dirs, author=author)
        except runner.StaleAnswerError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        n = ops_mod.write_ops(args.ops_out, result_ops, append=False)
        print(f"wrote {n} drop ops to {args.ops_out} (author={author})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
