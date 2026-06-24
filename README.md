# Gas Town Kanban

A zero-dependency web Kanban board that visualises work across **every Gas Town
rig** as swimlanes. Built for `kb-69556b41`.

## What it shows

- **One swimlane per rig** — discovered automatically from
  `<town>/.beads/routes.jsonl` (gastown, igor, footy_track, knowledge_mcp,
  films_to_fly_to, kanban, hq).
- **Three columns** — Backlog (`open`/`deferred`), In Progress
  (`in_progress`/`hooked`/`blocked`), Done (recently `closed`).
- **Cards** — title, bead id, priority, type, and assignee (polecat name).
- **Click a card** → modal with full bead detail (description, design,
  acceptance criteria, notes, close reason).
- **Auto-refresh** every 30s, with a live/offline indicator. Rigs can be
  collapsed; the choice survives refreshes.

## Run

```bash
python3 server.py                 # serves http://localhost:8077/
python3 server.py --port 9000     # custom port
python3 server.py --town /path/to/footy   # custom Gas Town root
```

Then open <http://localhost:8077/> in a browser. **No external dependencies** —
Python 3 standard library only.

## How it works

- **Backend** (`server.py`) — `http.server`-based. For each rig it shells out to
  `bd -C <rig-path> --readonly sql "<query>" --json` to read live data straight
  from the beads Dolt database. Results are cached server-side for 10s so
  multiple open browsers don't hammer Dolt.
- **Frontend** (`index.html`) — a single static page of vanilla JS that polls
  `/api/board` and renders the swimlanes; card clicks fetch `/api/bead/<id>`.

### Endpoints

| Route | Returns |
|-------|---------|
| `GET /` | the board UI |
| `GET /api/board` | all rigs + columns + cards (JSON) |
| `GET /api/bead/<id>` | full detail for one bead (JSON) |

### A note on shared databases

The town root (`.`) and the `gastown` directory share one aggregate Dolt
database that holds every prefix, while other rigs have isolated databases.
To keep each swimlane authoritative for its own work (and avoid a bead showing
up in several lanes), every query is scoped to that rig's id prefix
(`id LIKE 'ig-%'`, etc.).

## Resilience

A sick or slow rig never takes down the board: per-rig queries have a 20s
timeout and failures degrade to an empty lane rather than a 500.
