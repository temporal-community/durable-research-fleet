# Screenshots

All resized to 1920px wide — GitHub renders READMEs at roughly 900px, so the
original 3800px retina captures were about 8× larger than needed for no visible gain.

## Used in the README

| File | What it shows |
|---|---|
| `hero-console.png` | The workbench: outline rail, cited report, sources rail, live footer, `WAITING FOR YOU` with the review card |
| `footer-workers.png` | Just the counter strip — 12 workers, 27 spawned, 84 searches, 25 sources, 6/6 parts, sparkline |
| `temporal-timeline.png` | Temporal Web UI timeline: six overlapping `research_subquestion` Activities, `synthesize`, the 2h review timer, and the `review` Signal ending it early |

## Spares, not currently referenced

| File | Why you might want it |
|---|---|
| `console-done.png` | Same view as the hero but `DONE` — review card gone. Near-duplicate, which is why the README uses only one |
| `temporal-running.png` | Same timeline while still `Running`, with the 2h timer pending rather than fired |
| `console-fanout.png` | Mid-fan-out: 6 workers, parts 2/6, the worker grid and sparkline in the right rail, `peak 22`. **⚠️ Contains the live deployment URL** — see below |

## ⚠️ Before committing `console-fanout.png`

It shows the run permalink in full:

```
https://research-fleet-web-<project-number>.us-central1.run.app/r/research-...
```

Putting that in a README makes the deployment hostname permanently discoverable —
and git history keeps it even if the image is removed later. It only matters while
the stack is up, but it is worth a decision rather than an accident.

Either retake it (the permalink line only renders for a run started in that same
tab — open the `/r/<id>` link fresh and it won't show), or leave the file out.

## Missing: phone views

There are no narrow-viewport captures yet. If you want them, the three-zone layout
collapses to a tab bar below 900px, which is a genuinely different look worth showing:

- `console-ask.png` — the idle ask page at ~390px, footer reading `0 WORKERS`
- `console-report.png` — the report at ~390px with the `Report / Parts / Sources` tabs

`make web-local` binds `0.0.0.0`, so a real phone on the same wifi works
(`http://<your-lan-ip>:8000`), or use devtools at 390px.

## Notes

- Crop out browser chrome, bookmarks and personal tabs.
- The footer count peaks partway through a fan-out, roughly 30–90 seconds in.
- Temporal UI needs a tunnel, since the frontend is deliberately private:
  `make ui LOCAL_UI_PORT=8333` → http://localhost:8333
