"""plat top — the operator's view.

What this adds over `plat status --watch`, which is why it exists at all:
drill-in to a lot, a live tail of the running agent's output, and keys that act.
Everything it displays comes from the same views the terminal table and Grafana
read, so the three cannot disagree.
"""
from __future__ import annotations

from datetime import datetime

from rich.text import Text
from sqlalchemy import text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, RichLog, Static

from . import db as DB

STATE_COLOUR = {"DONE": "green", "BLOCKED": "red", "PENDING": "grey62",
                "CODING": "cyan", "REVIEWING": "yellow", "GATE": "magenta",
                "DOCS": "cyan", "QUALITY": "yellow"}


def _hms(td) -> str:
    if td is None:
        return "—"
    s = int(td.total_seconds())
    return f"{s // 60:02d}:{s % 60:02d}" if s < 3600 else f"{s // 3600}h{(s % 3600) // 60:02d}"


class PlatTop(App):
    CSS = """
    Screen { layout: vertical; }
    #stats { height: 3; padding: 0 1; content-align: left middle; }
    #body  { height: 1fr; }
    #left  { width: 3fr; border-right: solid $panel; }
    #right { width: 4fr; }
    #detail { height: 1fr; padding: 0 1; overflow-y: auto; }
    #logshdr { height: 1; padding: 0 1; background: $panel; color: $text-muted; }
    #logs  { height: 1fr; }
    DataTable { height: 1fr; }
    """
    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("p", "pause", "pause/resume"),
        Binding("o", "reopen", "reopen blocked lot"),
        Binding("l", "toggle_logs", "logs/events"),
        Binding("r", "refresh", "refresh"),
    ]

    def __init__(self, anchor: str | None = None, all_: bool = False):
        super().__init__()
        self.anchor = anchor
        self.all_ = all_
        self.sel: int | None = None          # lot_id
        self.show_logs = True
        self._last_log_id = 0
        self._last_event_id = 0
        self._sig: tuple | None = None      # what the table currently shows
        self._rebuilding = False

    # ---------------------------------------------------------------- layout
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="stats")
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield DataTable(id="lots", cursor_type="row", zebra_stripes=True)
            with Vertical(id="right"):
                yield Static(id="detail")
                yield Static(id="logshdr")
                yield RichLog(id="logs", wrap=True, markup=False, max_lines=800)
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#lots", DataTable)
        t.add_columns("ticket", "lot", "state", "phase", "model", "att", "elapsed", "cost", "signal")
        self.set_interval(1.0, self.refresh_all)
        self.refresh_all()

    # ----------------------------------------------------------------- data
    def _q(self, sql: str, **kw):
        with DB.engine().begin() as c:
            return c.execute(text(sql), kw).mappings().all()

    def refresh_all(self) -> None:
        # The whole refresh is guarded, not just the first query: every pane
        # touches the database, and a monitor that dies when the database blips
        # is worse than one that says so and keeps running.
        try:
            from .cli import _live_rows  # one query, shared with status
            rows = _live_rows(self.anchor, self.all_)
            self._stats(rows)
            self._lots(rows)
            self._detail(rows)
            self._stream()
        except Exception as e:
            self.query_one("#stats", Static).update(
                Text(f"database unreachable: {e}", "red"))

    def _stats(self, rows) -> None:
        live = sum(1 for r in rows if r["agent"] and
                   r["state"] not in ("DONE", "BLOCKED", "PENDING", "ABORTED"))
        blocked = sum(1 for r in rows if r["needs_human"])
        stale = sum(1 for r in rows if r["stale"])
        spend = sum(r["cost"] for r in rows)
        est = any(r["cost_estimated"] for r in rows)
        t = Text.assemble(
            ("plat top  ", "bold"), (f"{datetime.now():%H:%M:%S}   ", "grey62"),
            (f"{live} agent(s) live   ", "cyan" if live else "grey62"),
            (f"${spend:.2f}{'~' if est else ''}   ", "white"))
        if blocked:
            t.append(f"{blocked} NEEDS YOU   ", "bold red")
        if stale:
            t.append(f"{stale} stale   ", "bold red")
        paused = self._q("SELECT count(*) AS n FROM plats WHERE status = 'paused'")[0]["n"]
        if paused:
            t.append(f"{paused} paused", "bold yellow")
        self.query_one("#stats", Static).update(t)

    def _lots(self, rows) -> None:
        tbl = self.query_one("#lots", DataTable)
        # Rebuilding an unchanged table once a second is what made the panes
        # flicker: clear() emits a row-highlight for a transient key, the handler
        # read that as a new selection and wiped the log pane, and the next tick
        # put it back. Only rebuild when something actually changed.
        sig = tuple((r["lot_id"], r["state"], r["phase"], r["agent"], r["attempt"],
                     round(r["cost"], 2), r["stale"], r["needs_human"]) for r in rows)
        if sig == self._sig:
            return
        self._sig = sig
        keep = tbl.cursor_row
        self._rebuilding = True
        tbl.clear()
        for r in rows:
            sig = []
            if r["stale"]:
                sig.append("STALE")
            if r["needs_human"]:
                sig.append("NEEDS YOU")
            if r["last_chance"]:
                sig.append("LAST TRY")
            elif r["retrying"]:
                sig.append("retry")
            if r["permission_denials"]:
                sig.append(f"{r['permission_denials']} denials")
            tbl.add_row(
                r["ticket"], r["lot"],
                Text(r["state"], STATE_COLOUR.get(r["state"], "white")),
                r["phase"] or "—", (r["agent"] or "—").split("/")[-1],
                str(r["attempt"]), _hms(r["elapsed"]),
                f"${r['cost']:.2f}",
                Text(" ".join(sig), "red" if sig and sig[0] != "retry" else "yellow"),
                key=str(r["lot_id"]))
        if rows:
            tbl.move_cursor(row=min(keep, len(rows) - 1))
            self.sel = rows[min(keep, len(rows) - 1)]["lot_id"]
        self._rebuilding = False

    def _detail(self, rows) -> None:
        if self.sel is None:
            return
        row = next((r for r in rows if r["lot_id"] == self.sel), None)
        if row is None:
            return
        att = self._q(
            "SELECT phase, n, provider, model, cost_usd, cost_estimated, exit_code, "
            "  role_binding, started_at, finished_at "
            "FROM attempts WHERE lot_id = :l ORDER BY id", l=self.sel)
        find = self._q(
            "SELECT f.severity, f.fingerprint, f.claim, f.resolved_in_attempt_id "
            "FROM findings f JOIN attempts a ON a.id = f.attempt_id "
            "WHERE a.lot_id = :l ORDER BY f.id", l=self.sel)
        t = Text()
        t.append(f"{row['ticket']} / {row['lot']}\n", "bold cyan")
        t.append(f"{row['state']}  attempt {row['attempt']}\n\n", STATE_COLOUR.get(row["state"], "white"))
        t.append("attempts\n", "bold")
        for a in att:
            dur = ((a["finished_at"] or datetime.now(a["started_at"].tzinfo))
                   - a["started_at"]).total_seconds()
            mark = " resumed" if (a["role_binding"] or {}).get("resumed") else ""
            t.append(f"  a{a['n']} {a['phase']:<7} {a['provider']}/{a['model']}{mark}"
                     f"  {dur:.0f}s  ${a['cost_usd']:.2f}"
                     f"{'~' if a['cost_estimated'] else ''}\n", "grey70")
        if find:
            t.append("\nfindings\n", "bold")
            for f in find:
                done = "fixed" if f["resolved_in_attempt_id"] else "open"
                t.append(f"  [{f['severity']}] {f['fingerprint']} ({done})\n",
                         "green" if done == "fixed" else "yellow")
                t.append(f"     {f['claim'][:160]}\n", "grey62")
        self.query_one("#detail", Static).update(t)

    def _stream(self) -> None:
        log = self.query_one("#logs", RichLog)
        which = "agent log" if self.show_logs else "event stream"
        self.query_one("#logshdr", Static).update(
            f" {which}   [l] to switch" + (f"   ·  lot {self.sel}" if self.sel else ""))
        if self.show_logs and self.sel is not None:
            rows = self._q(
                "SELECT c.id, c.body FROM log_chunks c JOIN attempts a ON a.id = c.attempt_id "
                "WHERE a.lot_id = :l AND c.id > :since ORDER BY c.id LIMIT 40",
                l=self.sel, since=self._last_log_id)
            for r in rows:
                log.write(r["body"].rstrip())
                self._last_log_id = r["id"]
        elif not self.show_logs:
            rows = self._q("SELECT id, ts, message FROM events WHERE id > :since "
                           "ORDER BY id LIMIT 40", since=self._last_event_id)
            for r in rows:
                log.write(f"{r['ts']:%H:%M:%S}  {r['message']}")
                self._last_event_id = r["id"]

    # -------------------------------------------------------------- actions
    def on_data_table_row_highlighted(self, ev) -> None:
        if self._rebuilding:
            return          # a rebuild's own events are not the user selecting
        if ev.row_key and ev.row_key.value:
            new = int(ev.row_key.value)
            if new != self.sel:
                self.sel, self._last_log_id = new, 0
                self.query_one("#logs", RichLog).clear()

    def action_refresh(self) -> None:
        self.refresh_all()

    def action_toggle_logs(self) -> None:
        self.show_logs = not self.show_logs
        self._last_event_id = 0
        self.query_one("#logs", RichLog).clear()
        self.notify("agent log" if self.show_logs else "event stream")

    def action_pause(self) -> None:
        """Pause or resume the selected lot's plat. Recorded as a human decision."""

        from .decisions import record
        from .models import Lot, Plat
        if self.sel is None:
            return
        with DB.session() as s:
            lot = s.get(Lot, self.sel)
            p = s.get(Plat, lot.plat_id)
            was = p.status
            new = "running" if was == "paused" else "paused"
            record(s, plat_id=p.id, actor="human", actor_detail="plat top",
                   kind="override", decision=f"{new} ({was} -> {new})",
                   rationale="from the monitor", alternatives=[was],
                   inputs={"was": was}, apply=lambda: setattr(p, "status", new))
            self.notify(f"{p.anchor} {was} → {new}")

    def action_reopen(self) -> None:
        """Put a BLOCKED lot back into play. Refuses silently on anything else —
        reopening a running lot would race the process that owns it."""
        from .decisions import record
        from .models import Lot
        if self.sel is None:
            return
        with DB.session() as s:
            lot = s.get(Lot, self.sel)
            if lot.state != "BLOCKED":
                msg = (f"[o] {lot.key} is {lot.state}, not BLOCKED — reopen only "
                       f"applies to a lot that stopped for a human. Reopening a "
                       f"running lot would race the process that owns it.")
                self.notify(msg, severity="warning")
                self.query_one("#logs", RichLog).write(msg)
                return
            record(s, plat_id=lot.plat_id, lot_id=lot.id, actor="human",
                   actor_detail="plat top", kind="override",
                   decision="reopened BLOCKED -> CODING",
                   rationale="reopened from the monitor; no reason recorded — "
                             "use `plat reopen --note` when the reason matters",
                   alternatives=["leave blocked"], inputs={"was": "BLOCKED"},
                   apply=lambda: setattr(lot, "state", "CODING"))
            msg = f"[o] {lot.key} reopened to CODING — `plat start` to continue"
            self.notify(msg)
            self.query_one("#logs", RichLog).write(msg)


def run(anchor: str | None = None, all_: bool = False) -> None:
    PlatTop(anchor, all_).run()
