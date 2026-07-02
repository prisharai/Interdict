# Interdict

**A runtime safety layer between AI agents and Postgres.** Developer preview.

## What it does

Give Claude, Codex, or another AI coding agent direct database access and a
single bad statement can wipe a table. `DELETE FROM clients` with no `WHERE`.
An `UPDATE` that was meant for one row but hits a million. A stray semicolon
that turns one scoped delete into a full-table one. Database permissions don't
help here — they answer *"is this role allowed to touch this table,"* not *"how
much will this particular statement change, and can I take it back?"*

Interdict answers those two questions, on every statement, before damage is done:

- **It measures the real impact before running.** For a risky write it actually
  simulates the statement in a throwaway transaction and reports the count —
  *"this DELETE would affect 2,300,000 rows"* — then asks for confirmation
  instead of just running it. We call that number the statement's **blast
  radius**.
- **It makes writes undoable.** Every write it allows is recorded so you can
  reverse it with one command, with a full audit trail of who did what. Writes
  it can't safely record are blocked rather than run-and-hope.
- **It explains every block.** A blocked statement comes back with a reason code
  and a suggested fix — readable by a human, and machine-readable so an agent
  can correct itself and retry.

The checks that decide *block / allow / confirm* are deterministic and fast
(microseconds), so normal traffic isn't slowed down. Anything fuzzy (an optional
LLM "does this match the stated task?" check) is advisory only and never sits in
the path of a query you're waiting on.

```text
> UPDATE accounts SET balance = 0                ⛔ blocked: no WHERE — would hit every row
                                                   fix: add a WHERE that scopes it
> UPDATE accounts SET balance = 0 WHERE id = 1   ✓ UPDATE 1   (undo id 3811adb4)
> DELETE FROM accounts WHERE balance < 2000      ⚠ confirm: would delete 19 rows  [y/n]
> \undo                                          ✓ reverted — 1 row restored
```

---

## User guide

### 1. Install

Install Interdict from PyPI:

```bash
pip install interdict-db
```

Start the Interdict MCP server:

```bash
AGENT_DB_DSN=postgresql://postgres:postgres@localhost:5433/pagila \
AGENT_OPERATOR_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" \
interdict
```

This is the main product surface: your agent talks to Interdict's MCP server
instead of touching Postgres directly. Interdict is active in an agent chat only
when that MCP server is connected for the chat.

### 2. Connect Claude or Codex

The user does not need to phrase every request as "use Interdict." Once the MCP
server is connected, your agent instructions should say that any database work
inside a larger task must go through Interdict's MCP tools.

**Claude Code:**

```bash
claude mcp add interdict \
  --env AGENT_DB_DSN=postgresql://postgres:postgres@localhost:5433/pagila \
  --env AGENT_OPERATOR_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" \
  -- interdict
```

**Codex** (CLI, or edit `~/.codex/config.toml`):

```bash
codex mcp add interdict \
  --env AGENT_DB_DSN=postgresql://postgres:postgres@localhost:5433/pagila \
  --env AGENT_OPERATOR_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" \
  -- interdict
```

```toml
# ~/.codex/config.toml   (table is mcp_servers, with an underscore)
[mcp_servers.interdict]
command = "interdict"

[mcp_servers.interdict.env]
AGENT_DB_DSN = "postgresql://postgres:postgres@localhost:5433/pagila"
AGENT_OPERATOR_TOKEN = "paste-a-random-token-at-least-32-chars"
```

> **Codex PATH gotcha:** Codex may not inherit your shell's PATH. If it can't
> find `interdict`, use the absolute path from `which interdict` as
> `command`. Verify with `codex mcp list`, then `/mcp` in the Codex TUI.

To check whether Interdict is active in the current chat, ask the agent to call
`interdict_status`.

A held write is approved out-of-band with `approve_query` and the operator
token, which the agent should not see.

### 3. Set up the dev database

Interdict needs a Postgres to talk to. The repo ships a seeded one:

```bash
docker compose up -d        # seeded Postgres on localhost:5433 (Pagila + large tables)
```

First start generates ~5M rows (1–2 min); later starts are instant. Point at any
other database with `AGENT_DB_DSN`. Re-seed from scratch with
`docker compose down -v && docker compose up -d`.

---

## How it works

```
AI agent ──(MCP)──> [Interdict adapter] ──> [SAFETY ENGINE] ──> Postgres
                              parse → classify → policy → (simulate?) → decide
                                                   │           (record undo on writes)
                                               async: audit log, advisory intent check
```

- **The engine** (`engine/`) is a standalone, transport-agnostic core. It parses
  each statement to a real Postgres AST with `pglast` (never string matching, so
  comments, casing, whitespace, alias stars, and wrapped writes like
  `EXPLAIN ANALYZE DELETE …` can't smuggle anything past), classifies it, checks
  it against a declarative YAML policy, and — only for a risky write — simulates
  the blast radius with a time-boxed `BEGIN; … ; ROLLBACK`.
- **The MCP adapter is thin glue** over that engine. The MCP server is the
  product surface: agents call it instead of touching Postgres directly. Policy
  logic never lives in the adapter.
- **The hot path stays cheap.** Only blocking-vs-allowing is on it (in-memory,
  microseconds). Simulation is opt-in, gated to risky writes, and time-boxed
  (`statement_timeout` + `lock_timeout`). Audit logging and the optional LLM
  intent check are async/out-of-band. Writes fail closed on uncertainty; reads
  fail open so the layer can never take down read availability.

It is **not** "git for databases," a migration tool, a semantic layer, or a
replacement for Postgres roles/RLS — those still do their job; this does the part
they structurally can't.

## Measured results

The hard constraint is **don't slow down the database** — a safety layer that
adds latency gets ripped out. Budget: added p99 < 5 ms on the pass-through path,
enforced by a local benchmark gate.

| What | Result |
|---|---|
| Hot-path cost, warm (parse-cache hit) | **2.6 µs** p50 / 2.7 µs p99 |
| Hot-path cost, cold (first sight of a query) | **166 µs** p50 / 189 µs p99 |
| End-to-end overhead vs direct asyncpg | **≈ 0 ms** p50 & p99 — gate PASS |
| Red corpus blocked (false negatives) | 40 statements, **0%** |
| Green corpus allowed (false positives) | 18 statements, **0%** |
| Blast-radius accuracy (precise path) | **exact** affected-row count |
| Undo round-trip | ~4 ms, conflict-checked, exact restore |
| Automated tests | **321** |

Full methodology and per-rate tables: [`benchmarks/RESULTS.md`](benchmarks/RESULTS.md)
and [`benchmarks/METRICS.md`](benchmarks/METRICS.md).

## Configuration reference

Environment variables (used by both modes):

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_DB_DSN` | `postgresql://postgres:postgres@localhost:5433/pagila` | Target Postgres. Default is local-dev only. |
| `AGENT_POLICY` | `policies/default.yaml` | YAML policy loaded at startup. |
| `AGENT_AUDIT_LOG` | `logs/audit.jsonl` | Async JSONL audit log (also feeds `\stats`). Raw SQL/task text is redacted; hashes are kept for correlation. |
| `AGENT_OPERATOR_TOKEN` | unset | Required to approve held writes via MCP. Must be a random token of at least 32 characters. |
| `AGENT_POOL_MIN` / `AGENT_POOL_MAX` | `1` / `10` | asyncpg pool sizing. |

MCP tools the server exposes:

| Tool | Purpose |
|---|---|
| `run_query(sql, stated_task?)` | Classify, policy-check, simulate if risky, then execute or block. |
| `list_pending_approvals()` | Writes currently held for operator approval. |
| `approve_query(approval_id, operator_token)` | Execute a held write when the token matches. |
| `revert_write(action_id, operator_token?)` | Revert a recorded write. |
| `audit_status()` | Audit queue depth, dropped-record count, log path. |

## Honest limits

Kept visible on purpose:

- Semantic correctness is undecidable in general. We catch **blast-radius** and
  **scope-contradiction** cases and make the rest **reversible**; we don't claim
  to catch every "valid SQL but wrong" statement.
- `BEGIN/ROLLBACK` simulation can't undo external side effects (triggers calling
  out, already-consumed sequences) and takes locks — hence the gating and
  time-boxing.
- Reversibility isn't infinite (external calls, cascades, consumed sequences).
  Shapes that can't be recorded for safe undo are blocked by default;
  local evaluation can opt out with `undo.block_non_reversible: false`.
- Audit logging is non-blocking: under overload it drops records rather than
  stalling queries (`audit_status` reports this), and the local JSONL log isn't
  tamper-proof.
- LLM intent checks are advisory only — never the last line of defense, never on
  the hot path.
- This is a local developer preview, not a production recipe. Use a
  least-privilege Postgres role and review your policy before pointing it at real
  data.

## Pre-launch security checklist

Before putting Interdict in front of a real database:

- Publish a privacy policy if you collect or process user data. Know which
  tables may contain personal data, where audit logs live, and who can read
  them.
- Use a dedicated least-privilege Postgres role for `AGENT_DB_DSN`; do not use
  a superuser or owner role in production. If your application uses Supabase,
  keep Row Level Security policies on application-facing tables. Interdict is a
  guardrail, not a replacement for database authorization.
- Keep all secrets server-side. Never expose `AGENT_DB_DSN`,
  `AGENT_OPERATOR_TOKEN`, provider API keys, or PyPI tokens in browser code,
  client bundles, screenshots, logs, or docs.
- Use a random `AGENT_OPERATOR_TOKEN` of at least 32 characters. Short configured
  tokens are rejected.
- If you wrap Interdict in an HTTP API, add authentication, per-user and per-IP
  rate limits, strict CORS for your domains only, security headers, and CAPTCHA
  such as Cloudflare Turnstile on public forms. The bundled MCP server runs over
  stdio and does not provide a public web endpoint.
- Validate requests on the server. Client-side checks are only UX.
- Show generic database errors to users or agents. Keep diagnostics on the
  server side; Interdict returns generic database execution failures and redacts
  raw SQL/task text from its JSONL audit log.
- Test the failure paths: invalid DB credentials, nonexistent tables, duplicate
  approval attempts, wrong operator token, expired or repeated confirmation
  flows, blocked SQL, and undo conflicts.
- Review changes against the current OWASP Top 10 and OWASP API Security Top 10
  if you add a hosted web/API surface.

## Repo layout

```
engine/      # safety core: parse, classify, policy, simulate, undo, audit, intent, session
adapters/    # mcp_server.py (agent layer)
policies/    # declarative YAML policy files
corpus/      # red (should-block) + green (should-allow) query sets
benchmarks/  # latency harness, RESULTS.md, METRICS.md, CI latency gate
db/          # Docker Postgres seed scripts (Pagila + large tables)
tests/       # pytest suite (321 tests)
```
