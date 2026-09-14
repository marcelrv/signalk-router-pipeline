You are looking at a nautical chart with numbered junctions marked on the
routing graph drawn over it.

Draw the route most boats take through this area, by listing the numbered
junctions in order from one side of the picture to the other.

- Follow the deeper water and the buoyed channel where there is one.
- Stay off banks, drying areas, and the shoal side of any buoy.
- Take the route a local boater would take, not the mathematically shortest
  line on the graph.
- Do not think about any particular boat's draft. Give the route boats
  generally use.
- If there are genuinely two different routes that different boats take -- for
  example a deep ship channel and a shallower inshore route -- give both and
  say what makes them different.
- If no sensible through-route passes through this area at all, answer
  `{"main": []}`.

Answer with JSON only:

```json
{"main": [14, 27, 31, 55],
 "alternate": [14, 22, 38, 55],
 "alternate_reason": "shallower inshore route inside the bar"}
```

Use only numbers that appear on the image. Do not invent junction numbers, and
do not include any text outside the JSON object.
