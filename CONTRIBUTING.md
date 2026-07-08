# Contributing to Interdict

Thank you for your interest in contributing to Interdict. This guide covers how
to set up the repository, run the validation gates, and open a pull request. By
participating in this project, you agree to follow our
[Code of Conduct](CODE_OF_CONDUCT.md).

## Prerequisites

- **Python 3.11+**
- **[uv](https://docs.astral.sh/uv/)** for dependency management
- **Docker** or another container runtime for the seeded Postgres database
- **Postgres client tools** if you want to inspect or reseed the database
  manually

## Set Up the Workspace

Clone the repository and install the development dependency group:

```bash
git clone https://github.com/prisharai/Interdict.git
cd Interdict
uv sync --group dev --python 3.11
```

Start the local Pagila database used by tests and demos:

```bash
docker compose up -d
```

The default development DSN is:

```text
postgresql://postgres:postgres@localhost:5433/pagila
```

## Run the Gates

Run the same core checks that CI enforces:

```bash
uv run ruff check .
uv run black --check .
uv run pytest
uv run python -m benchmarks.ci_latency_gate
```

The latency gate is part of the product contract: Interdict's pass-through path
must stay under the added p99 budget. If a change touches parsing,
classification, policy evaluation, simulation, undo capture, or audit logging,
include the relevant test or benchmark output in the pull request.

## Open a Pull Request

Public PRs are welcome, especially for fixes, documentation, examples, tests,
and small improvements that preserve the engine-core / thin-adapter boundary.
Large architecture changes, new transports, new database backends, or changes
to safety semantics should start with an issue or design sketch.

- Keep PRs focused and reviewable.
- Use clear titles, preferably in Conventional Commits form such as `fix:`,
  `docs:`, `test:`, or `refactor:`.
- Describe the motivation and user-visible behavior change.
- List the validation commands you ran.
- Add or update tests when behavior changes.
- Do not weaken fail-closed behavior for writes without an explicit design
  discussion.

## Repository Notes

- `engine/` contains the transport-agnostic safety core.
- `adapters/` contains the MCP server glue; policy decisions should not live
  here.
- `policies/` contains YAML policy examples.
- `corpus/` contains red and green SQL examples used by correctness tests.
- `benchmarks/` contains the latency harness and CI gate.
- `docs/` contains design notes and forward-looking architecture specs.

## Releasing

The public Python package is
[interdict-db](https://pypi.org/project/interdict-db/). Releases should be cut
only from a green `main` branch with the version in `pyproject.toml` updated and
the README still rendering correctly on PyPI.

## License

By contributing, you agree that your contributions will be licensed under the
[MIT License](LICENSE) that covers this project.
