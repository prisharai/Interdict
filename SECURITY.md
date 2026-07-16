# Security Policy

We take the security of Interdict seriously. Thank you for helping keep the
project and its users safe.

## Production Security Boundary

Interdict is one layer in a defense-in-depth deployment. In production:

- Connect `AGENT_DB_DSN` with a non-owner, non-superuser role limited to the
  schema-qualified tables in `tables.allow`.
- Put approvals, undo evidence, and durable audit events behind a separate role
  and database in `AGENT_CONTROL_DSN`. The application role must have no access
  to `adb_undo` or `interdict_control`.
- Keep the operator token outside agent prompts and transcripts. Approve from a
  human-controlled terminal and use a stable `AGENT_OPERATOR_ID`.
- Do not expose raw Postgres credentials, infrastructure-admin credentials, or
  an unguarded SQL tool to the agent.
- Maintain tested point-in-time recovery and immutable/off-volume backups.
  Interdict's undo records are not a disaster-recovery substitute.

Run `interdict doctor` before startup. The default production profile refuses
to run with an overpowered application role, observe mode, an unqualified or
missing table allowlist, or a missing separate control store.

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues,
discussions, or pull requests.**

Instead, report them privately through GitHub's private vulnerability reporting
for this repository:

1. Go to the [Security tab](https://github.com/prisharai/Interdict/security).
2. Click **Report a vulnerability** to open a private security advisory.

If private vulnerability reporting is unavailable, contact the maintainer at
`pr482@cornell.edu` with a clear subject line such as `Interdict security
report`.

Please include as much of the following as you can:

- A description of the vulnerability and its potential impact
- Steps to reproduce, or a proof of concept
- Affected versions, components, policies, or configurations
- Any suggested mitigation, if you have one

## What to Expect

- We will acknowledge your report as soon as practical and keep you informed as
  we investigate.
- We may ask for additional reproduction details or affected configuration
  information.
- Once a fix is released, we are happy to credit you in the advisory unless you
  prefer to remain anonymous.
- Please give us a reasonable opportunity to address the issue before any public
  disclosure.

## Supported Versions

Interdict is in active pre-1.0 development. Security fixes are applied to the
latest release tracking the `main` branch.
