#!/usr/bin/env python3
"""Gas Town Kanban — swimlane board for all rigs.

A zero-dependency web app (Python stdlib only) that visualises work across
every Gas Town rig as a Kanban board. Each rig is a swimlane; columns are
Backlog / In Progress / Done.

Live data is read from the beads (`bd`) CLI, which is backed by Dolt. We shell
out to `bd -C <rig-path> sql --json "..."` once per rig and merge the results.

Run:
    python3 server.py [--port 8077] [--town /path/to/footy]

Then open http://localhost:8077/ in a browser.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent

# Issue types that represent infrastructure / orchestration rather than human
# work. These are hidden from the board.
INFRA_TYPES = {"convoy", "event", "molecule", "wisp", "message"}

# ID substring patterns that indicate agent-identity beads (not real work).
# Matched with SQL LIKE on the id column.
NOISE_ID_PATTERNS = [
    "%-polecat-%",
    "%-witness",
    "%-refinery",
    "%-engineer",
    "%-architect",
]

# Title prefixes that flag transient ops logs / convoy duplicates.
NOISE_TITLE_PREFIXES = [
    "🤝 HANDOFF",
    "✓ Patrol",
    "POLECAT_DIED",
    "ZOMBIE_DETECTED",
    "Work: ",
]

# Work types kept on the board, in roughly the order we like to show them.
WORK_TYPE_ORDER = ["epic", "feature", "bug", "task", "chore", "research", "decision"]

# How many recently-closed beads to show per rig in the Done column.
DONE_LIMIT = 25

# Server-side cache TTL (seconds). The frontend polls every 30s; this keeps us
# from hammering Dolt if several browsers are open.
CACHE_TTL = 10


def discover_rigs(town: Path) -> list[dict]:
    """Return [{prefix, name, path}] for every rig declared in routes.jsonl.

    routes.jsonl maps an issue-id prefix to a rig directory (relative to the
    town root). We dedupe by path and keep only rigs that actually have a
    .beads directory on disk.
    """
    routes_file = town / ".beads" / "routes.jsonl"
    rigs: dict[str, dict] = {}
    if not routes_file.exists():
        return []
    for line in routes_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            route = json.loads(line)
        except json.JSONDecodeError:
            continue
        rel = route.get("path", ".")
        prefix = route.get("prefix", "").rstrip("-")
        rig_path = (town / rel).resolve()
        if not (rig_path / ".beads").is_dir():
            continue
        # Name the swimlane after the directory (or "hq" for the town root).
        name = "hq" if rel == "." else rig_path.name
        # Keep the first prefix seen for a given path; multiple prefixes
        # (e.g. hq- and hq-cv-) can map to the same db.
        rigs.setdefault(str(rig_path), {"prefix": prefix, "name": name, "path": str(rig_path)})
    # Stable, friendly ordering: hq last, others alphabetical.
    out = sorted(rigs.values(), key=lambda r: (r["name"] == "hq", r["name"]))
    return out


def _bd_sql(rig_path: str, query: str) -> list[dict]:
    """Run a read-only SQL query against a rig's beads db, return rows.

    Errors (missing db, Dolt hiccup) are swallowed and yield an empty list so
    one sick rig never takes down the whole board.
    """
    try:
        proc = subprocess.run(
            ["bd", "-C", rig_path, "--readonly", "sql", query, "--json"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    if proc.returncode != 0:
        return []
    out = proc.stdout.strip()
    if not out:
        return []
    # `bd` sometimes appends a human-readable trailer after the JSON array.
    end = out.rfind("]")
    if end == -1:
        return []
    try:
        rows = json.loads(out[: end + 1])
    except json.JSONDecodeError:
        return []
    return rows if isinstance(rows, list) else []


def _column_for(status: str) -> str | None:
    """Map a bead status to a board column, or None to hide it."""
    if status in ("open", "ready", "deferred"):
        return "backlog"
    if status in ("in_progress", "hooked", "blocked", "pinned"):
        return "in_progress"
    if status == "closed":
        return "done"
    return None


def _short_assignee(assignee: str | None) -> str | None:
    """Trim a full assignee address (kanban/polecats/jasper) to its tail name."""
    if not assignee:
        return None
    return assignee.rstrip("/").split("/")[-1]


def _noise_filter_sql() -> str:
    """Return a SQL fragment that excludes agent-identity and ops-noise beads."""
    id_exclusions = " AND ".join(f"id NOT LIKE '{p}'" for p in NOISE_ID_PATTERNS)
    title_exclusions = " AND ".join(
        f"title NOT LIKE '{p}%'" for p in NOISE_TITLE_PREFIXES
    )
    return f"({id_exclusions}) AND ({title_exclusions})"


def fetch_rig(rig: dict) -> dict:
    """Fetch and shape one rig's cards into board columns.

    Some rig directories share a single Dolt server: the town root and the
    `gastown` dir both resolve to one aggregate database holding every prefix.
    To keep each swimlane authoritative for its own beads (and avoid showing
    the same bead in several lanes), every query is scoped to this rig's own
    id prefix (e.g. `id LIKE 'ig-%'`).
    """
    type_list = ",".join(f"'{t}'" for t in INFRA_TYPES)
    noise_filter = _noise_filter_sql()
    # Scope to this rig's prefix so shared databases don't bleed across lanes.
    prefix = rig["prefix"]
    prefix_clause = f"id LIKE '{prefix}-%'" if prefix else "1=1"
    base_cols = (
        "id, title, status, priority, issue_type, assignee, owner, "
        "updated_at, created_at"
    )
    # Open / active work: everything not closed and not infrastructure.
    active = _bd_sql(
        rig["path"],
        f"SELECT {base_cols} FROM issues "
        f"WHERE {prefix_clause} AND status != 'closed' AND issue_type NOT IN ({type_list}) "
        f"AND (ephemeral = 0 OR ephemeral IS NULL) AND (pinned = 0 OR pinned IS NULL) "
        f"AND {noise_filter} "
        f"ORDER BY priority ASC, updated_at DESC",
    )
    # Recently closed work for the Done column.
    done = _bd_sql(
        rig["path"],
        f"SELECT {base_cols} FROM issues "
        f"WHERE {prefix_clause} AND status = 'closed' AND issue_type NOT IN ({type_list}) "
        f"AND (ephemeral = 0 OR ephemeral IS NULL) "
        f"AND {noise_filter} "
        f"ORDER BY updated_at DESC LIMIT {DONE_LIMIT}",
    )

    columns: dict[str, list] = {"backlog": [], "in_progress": [], "done": []}
    for row in active + done:
        col = _column_for(row.get("status", ""))
        if col is None:
            continue
        columns[col].append(
            {
                "id": row.get("id"),
                "title": row.get("title", ""),
                "status": row.get("status"),
                "priority": row.get("priority"),
                "type": row.get("issue_type"),
                "assignee": _short_assignee(row.get("assignee")),
                "owner": row.get("owner") or None,
                "updated_at": row.get("updated_at"),
            }
        )
    return {
        "name": rig["name"],
        "prefix": rig["prefix"],
        "columns": columns,
        "counts": {k: len(v) for k, v in columns.items()},
    }


def fetch_bead(town: Path, bead_id: str) -> dict | None:
    """Fetch full detail for a single bead by routing to the right rig."""
    prefix = bead_id.split("-", 1)[0] + "-"
    for rig in discover_rigs(town):
        if not bead_id.startswith(rig["prefix"]):
            continue
        rows = _bd_sql(
            rig["path"],
            "SELECT id, title, description, design, acceptance_criteria, notes, "
            "status, priority, issue_type, assignee, owner, created_by, "
            "created_at, updated_at, closed_at, close_reason "
            f"FROM issues WHERE id = '{bead_id}' LIMIT 1",
        )
        if rows:
            return rows[0]
    # Fallback: try every rig (prefix may not match cleanly).
    for rig in discover_rigs(town):
        rows = _bd_sql(
            rig["path"],
            "SELECT id, title, description, design, acceptance_criteria, notes, "
            "status, priority, issue_type, assignee, owner, created_by, "
            "created_at, updated_at, closed_at, close_reason "
            f"FROM issues WHERE id = '{bead_id}' LIMIT 1",
        )
        if rows:
            return rows[0]
    return None


class Board:
    """Caches the assembled board so concurrent polls don't all hit Dolt."""

    def __init__(self, town: Path):
        self.town = town
        self._cache: dict | None = None
        self._cache_at = 0.0

    def get(self) -> dict:
        now = time.monotonic()
        if self._cache is not None and (now - self._cache_at) < CACHE_TTL:
            return self._cache
        rigs = discover_rigs(self.town)
        board = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "rigs": [fetch_rig(r) for r in rigs],
        }
        self._cache = board
        self._cache_at = now
        return board


def make_handler(board: Board, town: Path):
    index_html = (HERE / "index.html").read_text()

    class Handler(BaseHTTPRequestHandler):
        # Quieter logging — one line per request is enough.
        def log_message(self, fmt, *args):
            sys.stderr.write(
                "%s - %s\n" % (self.address_string(), fmt % args)
            )

        def _send_json(self, obj, status=200):
            payload = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _send_html(self, html):
            payload = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._send_html(index_html)
                return
            if path == "/favicon.ico":
                # No favicon asset; answer 204 so browsers stop logging 404s.
                self.send_response(204)
                self.end_headers()
                return
            if path == "/api/board":
                try:
                    self._send_json(board.get())
                except Exception as exc:  # never 500 the whole board
                    self._send_json({"error": str(exc), "rigs": []}, status=200)
                return
            if path.startswith("/api/bead/"):
                bead_id = path[len("/api/bead/") :]
                detail = fetch_bead(town, bead_id)
                if detail is None:
                    self._send_json({"error": "not found"}, status=404)
                else:
                    self._send_json(detail)
                return
            self.send_response(404)
            self.end_headers()

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description="Gas Town Kanban board server")
    parser.add_argument("--port", type=int, default=8077, help="port (default 8077)")
    parser.add_argument(
        "--town",
        default=os.environ.get("GT_TOWN", "/Users/georgebarnett/code/footy"),
        help="Gas Town root directory (contains .beads/routes.jsonl)",
    )
    args = parser.parse_args(argv)

    town = Path(args.town).resolve()
    if not (town / ".beads" / "routes.jsonl").exists():
        sys.exit(f"error: no .beads/routes.jsonl under {town} — is this a Gas Town root?")

    board = Board(town)
    rigs = discover_rigs(town)
    print(f"Gas Town Kanban — {len(rigs)} rigs: {', '.join(r['name'] for r in rigs)}")
    print(f"Serving http://localhost:{args.port}/  (town: {town})")

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(board, town))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        httpd.shutdown()


if __name__ == "__main__":
    main()
