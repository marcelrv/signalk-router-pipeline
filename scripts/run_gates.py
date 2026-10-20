#!/usr/bin/env python3
"""Run graph_cleanup.validate's gates comparing a candidate build against the
currently-live baseline it is meant to replace.

Usage:
    .venv/bin/python3 scripts/run_gates.py --baseline OLD.sqlite --candidate NEW.sqlite \
        [--max-edge-growth N]

Exits 0 if every gate passes, 1 otherwise. Prints the GateReport either way.
"""
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from graph_cleanup.graph import RoutingGraph
from graph_cleanup import validate


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--baseline", required=True, help="Currently-live .sqlite to compare against")
    ap.add_argument("--candidate", required=True, help="Newly-built .sqlite being evaluated")
    ap.add_argument("--max-edge-growth", type=int, default=0,
                     help="Edges the candidate may add over baseline (default 0 = shrink-only)")
    args = ap.parse_args()

    if not os.path.isfile(args.baseline):
        print(f"NOTE: no baseline at {args.baseline} -- nothing to gate against, skipping (treated as PASS).")
        sys.exit(0)

    base_g = RoutingGraph.load(args.baseline)
    baseline = validate.Baseline.measure(base_g, args.baseline)

    cand_g = RoutingGraph.load(args.candidate)
    report = validate.check(cand_g, baseline, max_edge_growth=args.max_edge_growth)

    print(f"Baseline: {args.baseline} ({baseline.nodes} nodes / {baseline.edges} edges)")
    print(f"Candidate: {args.candidate} ({len(cand_g.nodes)} nodes / {len(cand_g.edges)} edges)")
    print(report)

    sys.exit(0 if report.passed else 1)


if __name__ == "__main__":
    main()
