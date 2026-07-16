<div align="center">
<h1>Interdict: Runtime Safety for Agent-Written SQL</h1>

![Status: Alpha](https://img.shields.io/badge/status-alpha-orange?style=for-the-badge)
[![PyPI](https://img.shields.io/pypi/v/interdict-db?style=for-the-badge&logo=pypi&logoColor=white)](https://pypi.org/project/interdict-db/)
[![Python](https://img.shields.io/pypi/pyversions/interdict-db?style=for-the-badge&logo=python&logoColor=white&label=)](https://pypi.org/project/interdict-db/)
[![CI](https://img.shields.io/github/actions/workflow/status/prisharai/Interdict/ci.yml?branch=main&style=for-the-badge&label=CI&logo=github)](https://github.com/prisharai/Interdict/actions/workflows/ci.yml)
[![Homepage](https://img.shields.io/badge/Homepage-0f766e?style=for-the-badge&logo=google-chrome&logoColor=white)](https://interdict.vercel.app/)
[![Docs](https://img.shields.io/badge/Docs-0f766e?style=for-the-badge&logo=readthedocs&logoColor=white)](https://github.com/prisharai/Interdict/tree/main/docs)
[![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)](LICENSE)
</div>

---

> [!IMPORTANT]
> **Interdict is an alpha developer preview** and under active development.
> It is designed for agent-operated Postgres workflows, not as a replacement
> for least-privilege database roles, backups, or human review of production
> policies.

<p align="center">
  <a href="#installation">Install</a> |
  <a href="#quickstart">Quickstart</a> |
  <a href="#how-it-works">How it works</a> |
  <a href="#benchmarks">Benchmarks</a> |
  <a href="#configuration">Configuration</a> |
  <a href="#honest-limits">Limits</a> |
  <a href="#documentation">Docs</a> |
  <a href="#contributing">Contributing</a> |
  <a href="#security">Security</a>
</p>

**Interdict** is the safety layer between AI agents and Postgres. Agents can now
issue real SQL against real databases; ordinary permissions answer "may this
role touch this table?", but not "how much will this statement change?" or "can
I undo it if the agent is wrong?" Interdict answers those questions before
damage is done.

It parses SQL into a Postgres AST, applies deterministic policy, measures every
write without executing it, holds high-impact changes for operator approval,
and records supported writes so a human can approve reverting them.
Blocks return structured explanations and repair hints so the agent can correct
itself and retry.

> **Platforms.** Interdict requires **Python 3.11+** and a reachable
> **Postgres** database. The current public adapter is an MCP server; the safety
> engine is transport-agnostic.

## Installation

```bash
pip install interdict-db
```

Working on Interdict itself? Install the local development environment instead:

```bash
uv sync --group dev --python 3.11
docker compose up -d
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full local validation loop.

## Quickstart

For local evaluation, explicitly select the relaxed development profile:

```bash
AGENT_SAFETY_PROFILE=development \
AGENT_DB_DSN=postgresql://user:password@host:5432/yourdb \
AGENT_OPERATOR_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" \
interdict
```

On startup Interdict prints the exact `claude mcp add` / `codex mcp add`
command to connect your agent. Verify the server is active by asking the agent
to call `interdict_status`.

No database handy? Start the seeded Pagila development database:

```bash
docker compose up -d
```

Approvals happen in **your terminal**, never in the agent chat. The operator
token is not exposed to the model.

```bash
interdict pending                 # list held writes and their blast radius
interdict approve <approval_id>   # then the agent calls run_approved_query
interdict deny <approval_id>
```

Holds expire after 30 minutes so stale measurements cannot be acted on. A
supported successful write returns an `undo_id`. Reversal is also human-gated:

```text
agent: request_revert(action_id) -> human: interdict approve <id>
agent: run_approved_revert(approval_id)
```

## Production setup

Production mode is the default and refuses to start when the database boundary
is unsafe. The application connection must be a non-owner, non-superuser role
with access only to explicitly allowed tables. Approvals, undo evidence, and a
durable audit copy must use a different database and a different role through
`AGENT_CONTROL_DSN`.

1. Create a production policy with schema-qualified tables:

   ```yaml
   mode: enforce
   require_qualified_tables: true
   tables:
     allow: [public.orders, public.customers]
   ```

2. Review the least-privilege SQL template. This command only prints SQL; it
   never changes a database:

   ```bash
   AGENT_POLICY=policies/production.yaml interdict init
   ```

3. Use distinct credentials and run the deployment check:

   ```bash
   export AGENT_SAFETY_PROFILE=production
   export AGENT_DB_DSN='postgresql://interdict_app:...@app-host/appdb'
   export AGENT_CONTROL_DSN='postgresql://interdict_control:...@control-host/controldb'
   export AGENT_POLICY='policies/production.yaml'
   export AGENT_OPERATOR_ID='oncall@example.com'
   export AGENT_OPERATOR_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
   interdict doctor
   interdict
   ```

For an upgrade from the older in-application `adb_undo` schema, run
`interdict migrate-control`. It is idempotent and copy-only: it does not delete
the old records. Verify the copy and your backups before manually revoking or
archiving the legacy schema.

## Benchmarks

Interdict keeps a hard latency budget: the pass-through path must stay under
**5 ms added p99**, and CI fails if the gate is exceeded.

| What | Result |
|---|---|
| Cost added per statement, warm path | **2.6 us p50 / 2.7 us p99** |
| End-to-end overhead vs raw asyncpg | **measurement-noise floor**; CI gate requires added p99 < 5 ms |
| Dangerous statements missed, red corpus | **0%** of 40 |
| Safe statements wrongly blocked, green corpus | **0%** of 18 |
| Blast-radius measurement | exact row counts, live |
| Undo round-trip | ~4 ms, conflict-checked restore |
| Automated tests | **370**, run in CI on every commit |

The benchmark methodology, caveats, and raw tables live in
[benchmarks/RESULTS.md](benchmarks/RESULTS.md). CI runs lint, the full pytest
suite against seeded Postgres 16, and the latency gate on every push and pull
request.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_DB_DSN` | local dev DSN | Target Postgres connection string. |
| `AGENT_CONTROL_DSN` | unset | Separate control database for approvals, undo evidence, and durable audit events; required in production. |
| `AGENT_SAFETY_PROFILE` | `production` | `production` fails startup on unsafe privileges; `development` relaxes deployment topology. |
| `AGENT_POLICY` | `policies/default.yaml` | Database-agnostic safety policy. |
| `AGENT_OPERATOR_TOKEN` | unset | Required to approve held writes; use at least 32 random characters. |
| `AGENT_OPERATOR_ID` | unset | Stable identity recorded for the human operator; required in production. |
| `AGENT_APPROVAL_TTL_SECONDS` | `1800` | How long a held write stays approvable. |
| `AGENT_AUDIT_LOG` | `~/.interdict/audit.jsonl` | Async audit log with raw SQL redacted and hashes retained. |

`policies/pagila.yaml` shows a stricter allowlist-style policy for the bundled
development database.

## Honest limits

- Interdict only governs SQL sent through its adapter. Do not give the agent a
  raw database DSN, cloud-admin token, shell with production credentials, or a
  second unguarded database tool.
- Undo is bounded compensation, not a backup system. Interdict blocks automatic
  undo for unsupported statement shapes, user triggers, cascading foreign-key
  actions, oversized captures, and conflicts detected during restore.
- Keep encrypted backups and point-in-time recovery in a different failure
  domain from the production volume, and regularly test restoration. A backup
  on the same volume does not protect against volume deletion.
- Least privilege, dev/staging separation, reviewed migrations, CI/CD gates,
  provider deletion protection, and human incident procedures remain required.
- The MCP adapter cannot intercept destructive cloud API calls such as deleting
  a Railway volume. The agent must not possess credentials scoped to those
  actions; enforce that boundary in your cloud IAM system.

## Development

```bash
uv sync --group dev --python 3.11
docker compose up -d
uv run ruff check .
uv run black --check .
uv run pytest
uv run python -m benchmarks.ci_latency_gate
```

The seeded database mirrors the GitHub Actions service container:
`postgresql://postgres:postgres@localhost:5433/pagila`.

## Documentation

- [Homepage](https://interdict.vercel.app/) -- product overview and demo.
- [Design doc](docs/DESIGN.md) -- current architecture and engineering rules.
- [v2 architecture spec](docs/SPEC_V2.md) -- forward roadmap and interface
  decisions.
- [Benchmark results](benchmarks/RESULTS.md) -- measured latency and correctness
  results.
- [Research notes](research/README.md) -- study harness and validation material.

## Repository Layout

```text
engine/      safety core: parse, classify, policy, measure, undo, audit
adapters/    MCP server
policies/    YAML policies
corpus/      red and green query sets
benchmarks/  latency harness and CI gate
tests/       correctness, race, fault-injection, evasion, and MCP tests
examples/    local demo script
website/     landing page source for interdict.vercel.app
research/    study harness, figures, and paper artifacts
docs/        design notes and architecture specs
```

## Contributing

Contributions are welcome for bug fixes, documentation, examples, tests, and
tightly scoped improvements. Please read [CONTRIBUTING.md](CONTRIBUTING.md)
before opening a pull request and follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Security

Please do not report security vulnerabilities through public issues or pull
requests. Follow [SECURITY.md](SECURITY.md) for private reporting guidance.

## License

Interdict is licensed under the [MIT License](LICENSE).
