"""Suite-level safety: CI must never turn database-test skips into green."""

import os


def pytest_sessionfinish(session, exitstatus):
    if not os.environ.get("INTERDICT_FAIL_ON_SKIPS"):
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    skipped = len(reporter.stats.get("skipped", ())) if reporter else 0
    if skipped:
        session.exitstatus = 1
