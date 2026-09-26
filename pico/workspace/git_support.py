"""Locale-independent Git process boundary."""

import subprocess


def run_git(args, *, cwd, timeout, check=False):
    """Run Git with its documented UTF-8 output decoded independently of OS locale."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        timeout=timeout,
        check=check,
    )
