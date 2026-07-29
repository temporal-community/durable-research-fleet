# Screenshots

Drop PNGs in this folder using **exactly these filenames** — the README already
points at them, so they appear as soon as the files exist.

| Filename | What to capture | Suggested width |
|---|---|---|
| `hero-console.png` | The report view on a wide screen: outline rail left, report centre, sources right, counters in the footer. This is the one people see first. | ~1600px |
| `console-ask.png` | The idle ask page at phone width, footer showing `0 WORKERS`. | ~390px |
| `console-report.png` | The finished report at phone width, scrolled to show `##` sections and a `[n]` citation. | ~390px |
| `footer-workers.png` | The footer **mid-fan-out**, with the worker count high. Crop to just the counter strip — this is the money shot. | ~1600px, cropped |
| `temporal-history.png` | Temporal Web UI event history for one run, showing the six `ActivityTaskStarted` events at the same timestamp. Proof the fan-out is real. | ~1400px |

## Getting them

Phone views: browser devtools at 390px wide, or a real phone on the same wifi
(`make web-local` binds `0.0.0.0`, so `http://<your-lan-ip>:8000` works).

Wide views: any desktop browser ≥1080px — the three-zone layout only appears above
900px.

The footer shot needs timing. Start a question, then watch the counter climb; it
peaks partway through the fan-out, roughly 30–90 seconds in, and falls back to zero a
few minutes after the report is drafted.

For the Temporal UI, tunnel to the VM (the frontend is deliberately private):

```bash
make ui LOCAL_UI_PORT=8333        # then http://localhost:8333
```

Pick a run, open **Event History**, and look for the block of
`ActivityTaskScheduled` / `ActivityTaskStarted` events sharing a timestamp.

## Notes

- PNG preferred. Keep each under ~500KB so the repo stays small.
- Crop out your browser chrome, bookmarks and any personal tabs.
- The page commits to a dark theme, so screenshots look consistent either way.
- **Check for your passcode and the live URL** before committing — the deployed
  hostname and `DEMO_PASSCODE` are both visible in a full-window capture.
