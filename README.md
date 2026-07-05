# Interdict

[![CI](https://github.com/prisharai/Interdict/actions/workflows/ci.yml/badge.svg)](https://github.com/prisharai/Interdict/actions/workflows/ci.yml)

**The safety layer between AI agents and Postgres.**

AI agents write to production databases now, and one bad statement can erase a
table. Permissions can't help — they answer *"may this role touch this table,"*
not *"how much will this statement change, and can I take it back?"* Interdict
answers both, on every statement, before damage is done:

- **Measures real impact first.** A risky write is simulated in a throwaway
  transaction and reported — *"this DELETE would affect 2,300,000 rows"* —
  then held for human approval instead of running.
- **Makes every write undoable.** Allowed writes are recorded so one command
  reverses them, with a full audit trail. Writes that can't be safely recorded
  are blocked, not run-and-hoped.
- **Explains every block** with a machine-readable reason and a suggested fix,
  so the agent corrects itself and retries.

```text
> UPDATE accounts SET balance = 0                ⛔ blocked: no WHERE — would hit every row
> UPDATE accounts SET balance = 0 WHERE id = 1   ✓ UPDATE 1   (undo id 3811adb4)
> DELETE FROM accounts WHERE balance < 2000      ⚠ held: would delete 2,300,000 rows → approve
> \undo                                          ✓ reverted — rows restored
```

## The numbers

| What | Result |
|---|---|
| Cost added per statement (warm) | **2.6 µs** p50 / 2.7 µs p99 |
| End-to-end overhead vs raw asyncpg | **≈ 0 ms** — CI fails any build over 5 ms p99 |
| Dangerous statements missed (red corpus) | **0%** of 40 |
| Safe statements wrongly blocked (green corpus) | **0%** of 18 |
| Blast-radius measurement | **exact** row counts, live |
| Undo round-trip | ~4 ms, conflict-checked, exact restore |
| Automated tests | **343**, run in CI on every commit |

Tests include real-parallelism **race tests** (a held write executes exactly
once across concurrent retries), **fault injection** (backends killed
mid-write, timeouts inside undo capture, audit disk failure — always
fail-closed, no partial effects), and **evasion attacks** (comments, casing,
encodings, smuggled second statements). Full methodology:
[`benchmarks/RESULTS.md`](benchmarks/RESULTS.md).

## Quick start

```bash
pip install interdict-db

# point it at your Postgres and start the MCP server
AGENT_DB_DSN=postgresql://user:password@host:5432/yourdb \
AGENT_OPERATOR_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" \
interdict
```

On startup Interdict prints the exact `claude mcp add` / `codex mcp add`
command to connect your agent. Verify it's active by asking the agent to call
`interdict_status`. (No database handy? `docker compose up -d` in this repo
gives you a seeded one on `localhost:5433`.)

**Approving a held write** happens in *your* terminal, never in the chat — the
operator token can't leak to the agent:

```bash
interdict pending                 # see held writes with their blast radius
interdict approve <approval_id>   # then the agent calls run_approved_query
interdict deny <approval_id>
```

Holds expire after 30 minutes so stale measurements can't be acted on. Every
successful write returns an `undo_id`; ask the agent to call `revert_write`
to reverse it.

## How it works

```
AI agent ──(MCP)──> [thin adapter] ──> [SAFETY ENGINE] ──> Postgres
                        parse → classify → policy → (simulate?) → decide
                                              │        (record undo on writes)
                                          async: audit log, advisory intent check
```

- **Real parser, never regex.** Statements are parsed to a Postgres AST
  (`pglast`), so comments, casing, aliases, and wrapped writes can't smuggle
  anything past.
- **The hot path is microseconds of in-memory work.** Simulation runs only for
  risky writes, time-boxed with statement and lock timeouts. Logging and LLM
  intent checks are async — never in the path of a query the agent waits on.
- **Writes fail closed, reads fail open.** Uncertainty blocks a write; the
  safety layer can never take down read availability.
- The engine is a standalone, transport-agnostic core; the MCP server is thin
  glue. A wire-protocol proxy can reuse the engine unchanged.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_DB_DSN` | local dev DSN | Target Postgres. |
| `AGENT_POLICY` | `policies/default.yaml` | Database-agnostic policy; `policies/pagila.yaml` shows a locked-down allowlist. |
| `AGENT_OPERATOR_TOKEN` | unset | Required to approve held writes. Min 32 random chars. |
| `AGENT_APPROVAL_TTL_SECONDS` | `1800` | How long a held write stays approvable. |
| `AGENT_AUDIT_LOG` | `~/.interdict/audit.jsonl` | Async audit log; raw SQL is redacted, hashes kept. |

MCP tools: `run_query`, `list_pending_approvals`, `run_approved_query`,
`revert_write`, `interdict_status`.

## Honest limits

- We catch **blast-radius** and **scope-contradiction** mistakes and make the
  rest reversible — we don't claim to catch every "valid SQL but wrong" query.
- Simulation and undo can't reverse external side effects (triggers calling
  out, consumed sequences, cascades). Shapes that can't be undone safely are
  blocked by default.
- LLM intent checks are advisory only — never the last line of defense.
- This is a developer preview: use a least-privilege Postgres role and review
  your policy before pointing it at production data.

## Repo layout

```
engine/      # safety core: parse, classify, policy, simulate, undo, audit
adapters/    # MCP server
policies/    # YAML policies
corpus/      # red (block) + green (allow) query sets
benchmarks/  # latency harness + CI gate
tests/       # 343 tests: correctness, races, faults, evasion
website/     # landing page (interdict.vercel.app)
```
