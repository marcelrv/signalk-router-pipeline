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
  is, `min_depth_m` the shallowest point along it (0.0 or missing means it was
  never sounded -- treat that as "unknown depth", NOT as "confirmed navigable"
  and NOT as "confirmed dry"), and `nearest_poi`/`nearest_poi_m` the closest
  named place and its straight-line distance from where the stub ends.
- `small_component` -- a small cluster of graph not connected to the rest of
  the network shown. `n_nodes` is its size, `total_length_m` its total extent.

For each number answer exactly one of:

- `keep` -- a boat would use this, or it is the only way to reach somewhere a
  boat goes (a marina, a harbour, a named creek, an anchorage).
- `drop` -- no boat would use this: it ends in open water, a marsh, or mud
  with nothing there, or (for a small component) it looks like a rendering or
  generation artifact rather than a real, separately-reachable body of water.
- `unsure` -- you cannot tell from what is here.

What matters is the shape of charted water at the stub's tip, not what is
merely nearby:

- Look at where the line actually ends, not just what label sits close to it.
  `nearest_poi` is a straight-line distance to the nearest labelled feature in
  the whole chart -- it does NOT mean that feature is what this stub leads to.
  A buoy or daybeacon 200 m away that marks a shipping channel running past
  the stub is not evidence the stub itself goes anywhere; it only supports
  `keep` if the stub is plausibly the approach to, or the water body
  containing, that place (e.g. it ends at the mouth of the cove the daybeacon
  marks the entrance to).
- A stub that ends flush against a shoreline, with the charted water simply
  stopping and land or marsh beginning, is `drop` -- even if it sits inside a
  branching creek system or near a named place elsewhere on the chart. Being
  part of a real creek's mesh does not make every twig of that mesh real; each
  numbered stub is judged on its own tip.
- A stub that ends at a distinct, separately-shaped widening, pocket, bend, or
  fork of charted water (a real cove tip, a canal continuing past the marked
  point, a lobe with its own inlet) is `keep`, even if short -- that shape is
  itself the evidence, independent of any nearby label.
- A stub that ends at a sharp point or notch of a marsh/mudflat polygon that
  is not part of any inlet or drainage (a "corner" of the water body rather
  than an "arm" of it) is `drop` -- this is the most common generator
  artifact: the skeleton follows every jag of the shoreline outline, not just
  real channels.
- If, after applying the two rules above, the tip's shape is genuinely
  ambiguous in the image (e.g. resolution is too coarse to tell a real narrow
  gut from a marsh notch), answer `unsure`. Do not use `unsure` as a
  substitute for looking closely -- use it only when a careful look still
  leaves it genuinely undecidable.
- If two answers seem equally good, answer `unsure`. `unsure` is treated as
  `keep`, so it is always the safe choice. Do not guess.
- For a `small_component`, judge it by whether it sits in a separately
  charted, real body of water (a cove, a side creek, a distinct pond) versus
  having no visible water feature under it at all -- the latter is `drop`,
  the former is `keep` even though it looks disconnected from the rest of
  the graph shown (a missing connecting edge is not evidence the water itself
  is fake).

Your `why` must cite at least one concrete number from this candidate's
`context.json` entry (`length_m`, `min_depth_m`, or `nearest_poi_m` for a
`dead_end_stub`; `n_nodes` or `total_length_m` for a `small_component`) AND
describe the specific geometry you see at THIS candidate: for a stub, which
direction its tip points and what is immediately beyond the end of the line
(open water, a named cove, solid land, a mudflat corner); for a component,
the water body it sits in and whether that body looks real or like a
rendering artifact. What you describe should make its shape different from
other candidates near it. A reason that does not name a number is not
acceptable.

Do not restate a rule from this prompt as your reason. Sentences like "ends at
a distinct, separately-shaped widening (a pocket/cove) of charted water, not
flush with the shoreline" or "ends flush with the shoreline outline" are the
RULE TEXT, not an observation -- copying or lightly rewording them, even with
a number spliced in, is not acceptable. Your `why` must describe what you
actually see at this one tip: is it a rounded lobe, a narrow ditch, a square
corner, a point, does it widen or pinch, what direction does it run. If your
own draft `why` would still read correctly if you swapped in a different
candidate's number and geometry, rewrite it -- it is not specific enough yet.

Before settling on `keep` or `drop`, silently check: what one thing about
this tip's shape, if it were different, would flip your verdict to the
opposite one? You do not need to write this down, but your `why` should make
that distinguishing feature the thing it actually points to (e.g. what makes
this tip a "widening" and not just "wider than the line drawing it" -- name
the shape).

When another numbered candidate in this tile has a tip nearby or of a similar
kind, your `why` should make clear how this tip differs from it (e.g. "unlike
#4's rounded pocket, this one narrows to a point at the marsh edge"). If you
cannot tell them apart, that is itself evidence you should be answering
`unsure` rather than picking a verdict you can't actually distinguish from a
neighbour's.

Answer with JSON only, one entry per number, plus a short reason:

```json
{"6": {"verdict": "drop", "why": "142 m stub runs due north and pinches to a point against the marsh edge -- no separate lobe or widening like #7's rounded cove 80 m away"},
 "7": {"verdict": "keep", "why": "95 m stub opens into a rounded pocket distinctly wider than the approach line, nearest_poi_m 40 m is the marina it feeds; unlike #6's point it has its own curved shoreline"},
 "8": {"verdict": "unsure", "why": "228 m stub, min_depth_m missing, tip shape is ambiguous at this resolution -- can't tell it apart from either a real gut or #6's marsh notch"}}
```

Use only the numbers listed in `context.json`. Do not invent numbers, and do
not include any text outside the JSON object.
