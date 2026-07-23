"""Minimal ANSI terminal styling for the human-facing setup wizard.

No emojis, ever -- plain ASCII tags (``[ OK ]``, ``[FAIL]``) and box-drawing
characters only. Color and the connection-test spinner are skipped whenever
stdout isn't a real terminal (piped output, CI logs, or a test harness
replacing ``sys.stdout``) or ``NO_COLOR`` is set, so scripted and
non-interactive runs still see stable, uncolored plain text.
"""

from __future__ import annotations

import itertools
import os
import sys
import threading
import time

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[36m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"


def _enabled() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _wrap(text: str, *codes: str) -> str:
    if not _enabled():
        return text
    return "".join(codes) + text + _RESET


def bold(text: str) -> str:
    return _wrap(text, _BOLD)


def dim(text: str) -> str:
    return _wrap(text, _DIM)


def accent(text: str) -> str:
    return _wrap(text, _BOLD, _CYAN)


def good(text: str) -> str:
    return _wrap(text, _BOLD, _GREEN)


def warn(text: str) -> str:
    return _wrap(text, _BOLD, _YELLOW)


def bad(text: str) -> str:
    return _wrap(text, _BOLD, _RED)


def banner(title: str, subtitle: str | None = None) -> None:
    """A boxed header. Falls back to a plain title line without a tty."""
    if not _enabled():
        print(title)
        if subtitle:
            print(subtitle)
        return
    width = max(len(title), len(subtitle or "")) + 2
    print(_wrap("┌" + "─" * (width + 2) + "┐", _BOLD, _CYAN))
    print(
        _wrap("│ ", _BOLD, _CYAN) + bold(title.ljust(width)) + _wrap(" │", _BOLD, _CYAN)
    )
    if subtitle:
        print(
            _wrap("│ ", _BOLD, _CYAN)
            + dim(subtitle.ljust(width))
            + _wrap(" │", _BOLD, _CYAN)
        )
    print(_wrap("└" + "─" * (width + 2) + "┘", _BOLD, _CYAN))


def section(title: str) -> None:
    """A short divider marking the start of one wizard stage."""
    print()
    print(accent(title))
    print(dim("-" * len(title)))


class step:
    """Context manager: an animated status line while a blocking call runs.

    Prints ``[ OK ] message (1.2s)`` or ``[FAIL] message (0.3s)`` on exit.
    Without a tty, prints the message once on enter and the tag on exit --
    no animation, no carriage-return tricks, so log files stay readable.
    """

    _FRAMES = "|/-\\"

    def __init__(self, message: str) -> None:
        self.message = message
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start = 0.0

    def __enter__(self) -> step:
        self._start = time.time()
        if _enabled():
            self._thread = threading.Thread(target=self._animate, daemon=True)
            self._thread.start()
        else:
            print(f"  ... {self.message}")
        return self

    def _animate(self) -> None:
        for frame in itertools.cycle(self._FRAMES):
            if self._stop.is_set():
                return
            sys.stdout.write(f"\r  {_wrap(frame, _CYAN)} {self.message}")
            sys.stdout.flush()
            time.sleep(0.08)

    def __exit__(self, exc_type, exc, tb) -> bool:
        elapsed = time.time() - self._start
        if _enabled():
            self._stop.set()
            if self._thread is not None:
                self._thread.join()
            sys.stdout.write("\r\033[K")
        tag = good("[ OK ]") if exc_type is None else bad("[FAIL]")
        print(f"{tag} {self.message} ({elapsed:.1f}s)")
        return False  # never swallow the exception
