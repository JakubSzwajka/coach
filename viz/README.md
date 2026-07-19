# Garmin Coach — visualiser

Stupid-simple, buildless dashboard over `data/derived/`. No npm, no bundler.

## Run

From the **repo root** (so `/data/derived/` is reachable):

```bash
python -m http.server 8000
# open http://localhost:8000/viz/
```

## Views

- **Dashboard** — athlete header (VO₂max, weight, thresholds, training days) + latest-day cards.
- **Trends** — readiness, HRV, sleep hours, resting HR, stress, VO₂max over 30/90/all days.
- **Activities** — sortable table of all activities (click headers to sort).

## Structure (the modular seam)

```
viz/
  index.html          shell + nav
  css/tokens.css      all colors/spacing — restyle here
  css/app.css
  js/
    app.js            entry: load data, start router
    data.js           THE seam — only file that knows the data layout
    router.js         hash routing
    util.js           formatting + DOM helper
    charts/line.js    Chart.js wrapper (one config, reused)
    views/            dashboard.js, trends.js, activities.js
```

**Rule:** views never fetch or hardcode paths — only `data.js` does. That is
what keeps this portable to React / a real backend later: swap `data.js`, keep
the views.

## Data it reads

- `data/derived/timeline.jsonl` — one rollup per day
- `data/derived/athlete.json` — profile/thresholds
- `data/derived/activities.json` — activity manifest (built by the collector)

Chart.js is loaded from a CDN (`esm.run`/`jsdelivr`), so an internet connection
is needed on first load.
