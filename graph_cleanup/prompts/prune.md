You are looking at a nautical chart and an automatically generated routing
graph drawn on top of it. The generator is crude in places: it leaves dead-end
stubs that go nowhere, and small clusters of graph that may or may not be
connected to anything real.

The numbered markers on the second image are the specific things we are unsure
about. For each number, decide whether it should stay in the routing graph.

- `chart.png` is the chart alone.
- `candidates.png` is the same area with the current routing graph drawn on
  top, and each numbered item marked with a circled number.
- `context.json` lists, for each number, what kind of thing it is and what we
  already measured about it -- you do not need to estimate length or depth
  yourself, trust those numbers over anything you judge by eye.

Two kinds of number appear:

- `dead_end_stub` -- a line that stops in the water. `length_m` is how long it
  is, `min_depth_m` the shallowest point along it (may be missing), and
  `nearest_poi`/`nearest_poi_m` the closest named place and its straight-line
  distance from where the stub ends.
- `small_component` -- a small cluster of graph not connected to the rest of
  the network shown. `n_nodes` is its size, `total_length_m` its total extent.

For each number answer exactly one of:

- `keep` -- a boat would use this, or it is the only way to reach somewhere a
  boat goes (a marina, a harbour, a named creek, an anchorage).
- `drop` -- no boat would use this: it ends in open water, a marsh, or mud
  with nothing there, or (for a small component) it looks like a rendering or
  generation artifact rather than a real, separately-reachable body of water.
- `unsure` -- you cannot tell from what is here.

Guidance:

- A stub ending near a named place (small `nearest_poi_m`) is almost always
  `keep`, even if it looks short or ugly on the chart.
- A stub ending in the middle of open water, or inside land/marsh with nothing
  charted there, is `drop`.
- A small component near a real body of water your chart shows as connected to
  the main channel (a cove, a side creek) is probably `keep` -- it may simply
  be missing a connecting edge, which is worth flagging even though this
  question only asks keep/drop. A small component with no visible water
  feature under it at all is more likely `drop`.
- If two answers seem equally good, answer `unsure`. `unsure` is treated as
  `keep`, so it is always the safe choice. Do not guess.

Answer with JSON only, one entry per number, plus a short reason:

```json
{"6": {"verdict": "drop", "why": "ends in the marsh, nothing charted there"},
 "7": {"verdict": "keep", "why": "leads to the marina entrance"},
 "8": {"verdict": "unsure", "why": ""}}
```

Use only the numbers listed in `context.json`. Do not invent numbers, and do
not include any text outside the JSON object.
