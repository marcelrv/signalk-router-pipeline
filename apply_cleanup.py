#!/usr/bin/env python3
"""Run post-build routing-graph cleanup on a built `.sqlite`
(`docs/SPEC-GRAPH-CLEANUP.md`).

Two modes:

    # Pass A: run the deterministic passes, write the ops, apply, gate, save
    ./apply_cleanup.py --db data/us_east_md_channel_axes.sqlite \
        --ops data/md_cleanup.ops.jsonl --out data/us_east_md_clean.sqlite

    # Replay an existing ops file (a model's verdicts, or a previous run)
    ./apply_cleanup.py --db ... --ops ... --replay --out ...

Nothing is written unless every gate in `graph_cleanup/validate.py` passes; use
`--dry-run` to measure without saving, and `--force` only with a reason.
"""
import argparse
import os
import sys
import time
from typing import List, Optional, Sequence, Tuple

from graph_cleanup import RoutingGraph, ops as ops_mod
from graph_cleanup import simplify, trace, validate


def _parse_probe(spec: str) -> Tuple[float, float, float, float]:
    parts = [float(x) for x in spec.replace(" ", "").split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            "--probe takes lat1,lon1,lat2,lon2")
    return tuple(parts)  # type: ignore[return-value]


def _snap_probes(g: RoutingGraph, probes: Sequence[Tuple[float, float, float, float]]
                 ) -> List[Tuple[int, int]]:
    if not probes:
        return []
    idx = trace.NodeIndex(g)
    out = []
    for lat1, lon1, lat2, lon2 in probes:
        a, b = idx.nearest(lat1, lon1), idx.nearest(lat2, lon2)
        if a is not None and b is not None and a != b:
            out.append((a, b))
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", required=True, help="built routing .sqlite to clean")
    p.add_argument("--ops", required=True,
                   help="ops.jsonl to write (Pass A) or replay (--replay)")
    p.add_argument("--out", help="cleaned .sqlite to write; omit for --dry-run")
    p.add_argument("--replay", action="store_true",
                   help="apply an existing ops file instead of generating one")
    p.add_argument("--dry-run", action="store_true",
                   help="measure and gate, write nothing")
    p.add_argument("--tolerance-m", type=float, default=20.0,
                   help="Douglas-Peucker tolerance, capped per chain by the "
                        "charted half-width (default: %(default)s)")
    p.add_argument("--no-smooth", action="store_true",
                   help="skip corridor smoothing (the one pass that moves geometry)")
    p.add_argument("--no-redundant", action="store_true",
                   help="skip redundant-edge removal")
    p.add_argument("--min-confidence", type=float, default=0.0,
                   help="ignore ops below this confidence when applying")
    p.add_argument("--author", action="append", default=None, metavar="PREFIX",
                   help="only apply ops whose author starts with PREFIX "
                        "(repeatable, e.g. --author det: --author ai:claude)")
    p.add_argument("--probe", type=_parse_probe, action="append", default=None,
                   metavar="LAT1,LON1,LAT2,LON2",
                   help="route probe to gate on (repeatable)")
    p.add_argument("--max-edge-growth", type=int, default=0, metavar="N",
                   help="opt-in: let the `counts` gate tolerate up to N extra edges "
                        "over the baseline, for edge-adding changes only (default 0 = "
                        "shrink-only; node growth is never tolerated, so the node-count "
                        "check stays strict). NOTE: the ops this CLI applies "
                        "(drop_node/splice_node/drop_edge/move_node) cannot add edges, so "
                        "today this cannot change a result here; it exists for future "
                        "edge-adding ops. Baseline-vs-candidate comparisons of separate "
                        "builds call validate.check(max_edge_growth=N) directly")
    p.add_argument("--force", action="store_true",
                   help="save even if a gate fails (say why in the BUILD_LOG entry)")
    args = p.parse_args(argv)

    if not args.dry_run and not args.out:
        p.error("--out is required unless --dry-run")
    if args.max_edge_growth < 0:
        p.error(f"--max-edge-growth must be >= 0 (got {args.max_edge_growth})")
    if args.replay and not os.path.exists(args.ops):
        p.error(f"--replay needs an existing ops file: {args.ops}")

    t0 = time.time()
    print(f"loading {args.db}")
    g = RoutingGraph.load(args.db)
    print(f"  {len(g.nodes)} nodes / {len(g.edges)} edges; "
          f"{len(g.protected)} protected (navmesh seam) nodes; "
          f"{g.dropped_self_loops} self-loops dropped")

    n_poi = trace.protect_poi_nodes(g, args.db)
    print(f"  protecting {n_poi} further nodes that POIs snap to")

    probes = _snap_probes(g, args.probe or [])
    print("measuring baseline (POI snapping, reachability, components)...")
    baseline = validate.Baseline.measure(g, args.db, probe_pairs=probes)
    print(f"  {len(baseline.pois)} POIs, "
          f"{len(baseline.reachable_pairs)} reachable pairs, "
          f"largest component {baseline.largest_component_fraction:.4f} by length")

    if args.replay:
        ops = list(ops_mod.read_ops(args.ops))
        print(f"replaying {len(ops)} ops from {args.ops}")
        result = ops_mod.apply(g, ops, min_confidence=args.min_confidence,
                               authors=args.author)
    else:
        print(f"generating deterministic ops (tolerance {args.tolerance_m}m)...")
        passes = simplify.run_all(g, tolerance_m=args.tolerance_m,
                                  smooth=not args.no_smooth,
                                  redundant=not args.no_redundant)
        for name, got in passes.items():
            print(f"  {name:<10} {len(got):6d} ops")
        ops = simplify.flatten(passes)
        # run_all already applied everything but the last pass; replay from a
        # clean load so the saved file and the saved database agree exactly.
        g = RoutingGraph.load(args.db)
        result = ops_mod.apply(g, ops, min_confidence=args.min_confidence,
                               authors=args.author)

    print(result.summary())
    if result.skipped_reasons:
        print(f"  skipped: {result.skipped_reasons}")
    if result.by_author:
        print(f"  by author: {result.by_author}")

    print("gates:")
    report = validate.check(g, baseline, max_edge_growth=args.max_edge_growth)
    for gate in report.gates:
        print(f"  {gate}")

    if not report.passed and not args.force:
        print(f"\nFAILED {len(report.failures())} gate(s); nothing written. "
              f"Use --force only with a reason for the BUILD_LOG entry.",
              file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"\ndry run, nothing written ({time.time() - t0:.1f}s)")
        return 0

    if not args.replay:
        ops_mod.write_ops(args.ops, ops, append=False)
        print(f"  wrote {len(ops)} ops to {args.ops}")

    print(f"writing {args.out}")
    g.save(args.out)
    size_mb = os.path.getsize(args.out) / 1e6
    print(f"done in {time.time() - t0:.1f}s; {args.out} is {size_mb:.1f} MB")
    print("\nRemember: every .sqlite build gets an entry in data/BUILD_LOG.md "
          "before it counts as done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
