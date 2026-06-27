"""Optional local TUI: an interactive, guarded SQL terminal.

Interdict's primary product surface is the MCP layer between an AI agent and
Postgres. This TUI is a local demo/admin console for a person writing SQL by
hand. Every statement goes through the same engine the agents do: it's parsed,
policy-checked, and -- for a risky write -- simulated so the blast radius is
shown *before* anything runs. Destructive writes require an explicit keystroke
to confirm; every executed write prints an undo id you can revert with one
command.

This module is pure rendering + input over ``engine.session.GuardedSession``. No
policy / simulation / undo logic lives here. Swap ``rich`` for a web frontend
later and the engine is untouched (CLAUDE.md sec. 5). ``rich`` is UI-only and is
never imported by ``engine/`` or anywhere near the request path (sec. 4).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import asyncpg
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from engine.audit import AuditLog
from engine.policy import Policy
from engine.session import BLOCK, CONFIRM, GuardedSession
from engine.simulate import load_unique_columns
from engine.undo import UndoStore

DB_DSN = os.environ.get(
    "AGENT_DB_DSN", "postgresql://postgres:postgres@localhost:5433/pagila"
)
POLICY_PATH = os.environ.get(
    "AGENT_POLICY",
    str(Path(__file__).resolve().parent.parent / "policies" / "default.yaml"),
)
AUDIT_LOG_PATH = os.environ.get("AGENT_AUDIT_LOG", "logs/audit.jsonl")

console = Console()


def _fmt_rows(n: int | None) -> str:
    return "unknown" if n is None else f"{n:,}"


# --- rendering ---------------------------------------------------------------


def _render_block(prop) -> None:
    """A policy violation: red, with the machine-readable reason + suggested fix."""
    body = Text()
    body.append(prop.sql + "\n\n", style="bold white")
    for v in prop.violations:
        body.append(f"✗ {v['reason_code']}\n", style="bold red")
        body.append(f"  {v['message']}\n", style="red")
        if v.get("suggested_fix"):
            body.append(f"  fix: {v['suggested_fix']}\n", style="yellow")
    console.print(
        Panel(body, title="⛔ BLOCKED", border_style="red", title_align="left")
    )


def _render_confirm(prop) -> None:
    """A risky write: amber panel showing the measured blast radius."""
    rows = prop.blast_radius
    method = prop.blast_method or "?"
    reversible = prop.is_write and not prop.blast_timed_out
    body = Text()
    body.append(prop.effective_sql + "\n\n", style="bold white")
    body.append("Blast radius: ", style="white")
    body.append(f"{_fmt_rows(rows)} rows", style="bold yellow")
    body.append(f"  ({method})\n", style="dim")
    if prop.blast_timed_out:
        body.append(
            "⚠ simulation timed out — true impact could not be bounded\n",
            style="bold red",
        )
    body.append("Reversible: ", style="white")
    body.append(
        "yes — an undo id will be kept\n" if reversible else "see result\n",
        style="green" if reversible else "yellow",
    )
    console.print(
        Panel(
            body,
            title="⚠ CONFIRM WRITE",
            border_style="yellow",
            title_align="left",
        )
    )


def _render_result(res) -> None:
    if res.refused:
        console.print(f"[dim]· not executed ({res.refused})[/dim]")
        return
    if res.error:
        console.print(f"[bold red]error:[/bold red] {res.error}")
        return
    if res.rows:
        _render_table(res.rows)
    status = res.status or "ok"
    line = f"[green]✓[/green] {status}"
    if res.action_id:
        line += (
            f"   [dim]undo id[/dim] [bold cyan]{res.action_id[:8]}[/bold cyan]"
            "  [dim](\\undo to revert)[/dim]"
        )
    elif res.reversible is False and res.undo_reason:
        line += f"   [yellow]not reversible: {res.undo_reason}[/yellow]"
    console.print(line)


def _render_table(rows: list[dict], limit: int = 20) -> None:
    cols = list(rows[0].keys())
    table = Table(show_header=True, header_style="bold cyan", box=None)
    for c in cols:
        table.add_column(str(c))
    for r in rows[:limit]:
        table.add_row(*[str(r[c]) for c in cols])
    console.print(table)
    if len(rows) > limit:
        console.print(f"[dim]… {len(rows) - limit} more row(s)[/dim]")


def audit_savings(path: str) -> dict:
    """Summarize what the safety layer caught, from the audit log.

    Tolerant of both event vocabularies -- the local TUI session
    (propose/execute/override/revert) and the MCP adapter ('query' events) --
    so `interdict-stats` works against whatever wrote the log.
    """
    s = {
        "guarded": 0,
        "blocked": 0,
        "held": 0,
        "executed": 0,
        "overrides": 0,
        "reverts": 0,
        "held_rows": 0,
        "max_blast": 0,
    }
    p = Path(path)
    if not p.exists():
        return s
    for line in p.read_text().splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        ev = e.get("event")
        if ev == "propose":
            s["guarded"] += 1
            v = e.get("verdict")
            if v == "block":
                s["blocked"] += 1
            elif v == "confirm":
                s["held"] += 1
                br = e.get("blast_radius") or 0
                s["held_rows"] += br
                s["max_blast"] = max(s["max_blast"], br)
        elif ev == "query":  # MCP adapter
            s["guarded"] += 1
            if e.get("blocked"):
                s["blocked"] += 1
            elif e.get("requires_confirmation"):
                s["held"] += 1
        elif ev == "execute":
            s["executed"] += 1
        elif ev == "execute_override":
            s["overrides"] += 1
        elif ev == "revert":
            s["reverts"] += 1
    return s


def render_stats(path: str) -> None:
    s = audit_savings(path)
    caught = s["blocked"] + s["held"]
    head = Text()
    head.append(f"{caught}", style="bold cyan")
    head.append(" risky statement(s) blocked or held before touching your data.\n")
    if s["max_blast"]:
        head.append("Largest measured blast radius held: ", style="dim")
        head.append(f"{s['max_blast']:,} rows\n", style="bold yellow")
    table = Table(show_header=False, box=None)
    table.add_column(style="white")
    table.add_column(justify="right", style="bold")
    table.add_row("Statements guarded", str(s["guarded"]))
    table.add_row("Blocked", f"[red]{s['blocked']}[/red]")
    table.add_row("Held for confirmation", f"[yellow]{s['held']}[/yellow]")
    table.add_row("Executed", f"[green]{s['executed']}[/green]")
    table.add_row("Manual overrides", str(s["overrides"]))
    table.add_row("Reverts (undo)", str(s["reverts"]))
    console.print(
        Panel(
            head,
            title="📊 your safety savings",
            border_style="cyan",
            title_align="left",
        )
    )
    console.print(table)


_HELP = """[bold]Commands[/bold]
  [cyan]\\help[/cyan]              this help
  [cyan]\\stats[/cyan]             what the safety layer has caught (your savings)
  [cyan]\\override[/cyan]          run the last BLOCKED statement anyway (your call)
  [cyan]\\undo[/cyan]              revert the most recent write
  [cyan]\\revert <id>[/cyan]       revert a specific undo id
  [cyan]\\history[/cyan]           show this session's executed writes
  [cyan]\\tables[/cyan]            list tables the policy allows
  [cyan]\\quit[/cyan]              leave the local SQL console

Type any SQL to run it through the safety layer. Risky writes show their
blast radius and ask before executing; allowed reads/writes just run.
You write the SQL, so a block is advice, not a wall: \\override runs it anyway
(audited, and still undoable when the statement's shape allows)."""


# --- the optional local SQL console loop -------------------------------------


async def human_mode(sess: GuardedSession, policy: Policy) -> None:
    console.print(
        Panel(
            Text.from_markup(
                "[bold]Local SQL console[/bold] — you write SQL, Interdict has "
                "your back.\nEvery destructive write is simulated and shown "
                "before it runs. [dim]\\help for commands.[/dim]"
            ),
            border_style="cyan",
        )
    )
    history: list[tuple[str, str]] = []  # (action_id, sql) for executed writes
    last_blocked = None  # the most recent BLOCKed proposal, for \override

    while True:
        try:
            raw = Prompt.ask(
                "[bold green]interdict[/bold green] [dim]▸[/dim]"
            ).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye[/dim]")
            return
        if not raw:
            continue

        # --- meta commands ---
        if raw in ("\\quit", "\\exit", "\\q"):
            console.print("[dim]bye[/dim]")
            return
        if raw in ("\\help", "\\h", "\\?"):
            console.print(_HELP)
            continue
        if raw == "\\stats":
            render_stats(AUDIT_LOG_PATH)
            continue
        if raw == "\\override":
            await _do_override(sess, history, last_blocked)
            last_blocked = None
            continue
        if raw == "\\tables":
            allowed = sorted(policy.allowed_tables or [])
            console.print(", ".join(allowed) if allowed else "[dim]all tables[/dim]")
            continue
        if raw == "\\history":
            if not history:
                console.print("[dim]no writes yet this session[/dim]")
            for aid, sql in history:
                tag = f"[cyan]{aid[:8]}[/cyan]" if aid else "[dim]—[/dim]"
                console.print(f"  {tag}  {sql}")
            continue
        if raw == "\\undo" or raw.startswith("\\revert"):
            await _do_revert(sess, history, raw)
            continue
        if raw.startswith("\\"):
            console.print(f"[red]unknown command[/red] {raw}  [dim](\\help)[/dim]")
            continue

        # --- SQL ---
        last_blocked = await _run_sql(sess, history, raw)


async def _run_sql(sess, history, sql):
    """Run one SQL statement. Returns the Proposal if it was BLOCKED (so the
    caller can stash it for \\override), else None."""
    prop = await sess.propose(sql, actor="human")

    if prop.verdict == BLOCK:
        _render_block(prop)
        console.print(
            "[dim]· you wrote this — [/dim][bold]\\override[/bold]"
            "[dim] to run it anyway[/dim]"
        )
        return prop
    if prop.verdict == CONFIRM:
        _render_confirm(prop)
        ans = Prompt.ask(
            "  Execute?", choices=["y", "n"], default="n", show_choices=True
        )
        if ans != "y":
            console.print("[dim]· cancelled[/dim]")
            return
        res = await sess.execute(prop, force=True)
    else:  # ALLOW / PASSTHROUGH
        res = await sess.execute(prop)

    _render_result(res)
    if res.executed and res.action_id:
        history.append((res.action_id, sql))


async def _do_override(sess, history, last_blocked) -> None:
    """The human escape hatch: run the most recently BLOCKED statement anyway.

    Deliberate (explicit command + a second confirmation), loud, and audited in
    the engine. Still routed through undo capture, so an overridden write is
    revertible when its shape allows.
    """
    if last_blocked is None:
        console.print("[dim]nothing to override (no recent block)[/dim]")
        return
    prop = last_blocked

    body = Text()
    body.append(
        "You are about to run a statement the safety layer BLOCKED.\n\n",
        style="bold red",
    )
    body.append(prop.sql + "\n\n", style="white")
    for v in prop.violations:
        body.append(f"• {v['reason_code']}: {v['message']}\n", style="red")
    body.append("\nIf it runs it ", style="white")
    if prop.is_write:
        body.append("can still be undone (\\undo).\n", style="green")
    else:
        body.append("may NOT be undoable.\n", style="bold red")
    console.print(
        Panel(body, title="⚠ OVERRIDE BLOCK", border_style="red", title_align="left")
    )

    ans = Prompt.ask("  Override and run anyway?", choices=["y", "n"], default="n")
    if ans != "y":
        console.print("[dim]· override cancelled[/dim]")
        return
    res = await sess.execute(prop, actor="human", override=True)
    _render_result(res)
    if res.executed and res.action_id:
        history.append((res.action_id, prop.sql))


async def _do_revert(sess, history, raw) -> None:
    if raw.startswith("\\revert"):
        parts = raw.split()
        if len(parts) < 2:
            console.print("[red]usage:[/red] \\revert <undo id>")
            return
        action_id = parts[1]
    else:  # \undo -> most recent
        if not history:
            console.print("[dim]nothing to undo[/dim]")
            return
        action_id = history[-1][0]

    # Accept an 8-char prefix from the rendered output.
    full = next((a for a, _ in history if a.startswith(action_id)), action_id)
    try:
        result = await sess.revert(full, actor="human")
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        return
    if result.ok:
        console.print(
            f"[green]✓ reverted[/green] [cyan]{full[:8]}[/cyan]"
            f"  ({result.rows_restored} row(s) restored)"
        )
        history[:] = [(a, s) for a, s in history if a != full]
    else:
        console.print(f"[red]revert failed:[/red] {result.error}")


# --- optional local TUI wiring ----------------------------------------------


def _agent_mode_help() -> None:
    snippet = (
        "codex mcp add interdict \\\n"
        "  --env AGENT_DB_DSN=postgresql://postgres:postgres@localhost:5433/pagila \\\n"
        "  -- interdict"
    )
    console.print(
        Panel(
            Text.from_markup(
                "[bold]Agent layer[/bold] — this is the main Interdict use case."
                "\n\nPoint your MCP client (Claude Code, Codex, Cursor, …) at "
                "the server below. When the MCP server is connected, Interdict "
                "is active for that chat; when it is not connected, it is "
                "inactive.\n\n"
                f"[dim]{snippet}[/dim]"
            ),
            border_style="magenta",
            title="🤖 Agent layer",
            title_align="left",
        )
    )


async def _build_session() -> tuple[GuardedSession, Policy, asyncpg.Pool, AuditLog]:
    policy = Policy.load(POLICY_PATH)
    pool = await asyncpg.create_pool(dsn=DB_DSN, min_size=1, max_size=4, timeout=5)
    async with pool.acquire() as c:
        unique_columns = await load_unique_columns(c)
    audit = AuditLog(AUDIT_LOG_PATH)
    await audit.start()
    store = UndoStore(policy.undo)
    sess = GuardedSession(
        pool,
        policy,
        undo_store=store,
        unique_columns=unique_columns,
        audit=audit,
        # Local TUI: the person writing the SQL may deliberately override a
        # block. The MCP/agent adapter never sets this, so agents cannot.
        allow_override=True,
    )
    return sess, policy, pool, audit


async def _amain() -> None:
    console.print(
        Panel(
            Text.from_markup(
                "[bold cyan]Interdict[/bold cyan]  "
                "[dim]runtime safety layer between AI agents and Postgres[/dim]\n\n"
                "  [bold]1[/bold]  Configure the AI-agent MCP layer\n"
                "  [bold]2[/bold]  Open optional local SQL console"
            ),
            border_style="cyan",
            title="welcome",
            title_align="left",
        )
    )
    choice = Prompt.ask("Choose", choices=["1", "2"], default="2")
    if choice == "1":
        _agent_mode_help()
        return

    try:
        sess, policy, pool, audit = await _build_session()
    except (OSError, asyncpg.PostgresError) as exc:
        console.print(
            f"[bold red]Could not reach Postgres[/bold red] at {DB_DSN}\n{exc}\n"
            "[dim]Start the dev DB with `docker compose up -d`.[/dim]"
        )
        return
    try:
        await human_mode(sess, policy)
    finally:
        await audit.stop()
        await pool.close()


def main() -> None:
    """Console-script entry point (`interdict-tui`).

    ``interdict-stats`` prints the savings summary and exits; ``interdict-tui``
    opens the optional local SQL console.
    """
    if sys.argv[1:2] == ["stats"]:
        render_stats(AUDIT_LOG_PATH)
        return
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass


def stats_main() -> None:
    """Console-script entry point (`interdict-stats`)."""
    render_stats(AUDIT_LOG_PATH)


if __name__ == "__main__":
    main()
