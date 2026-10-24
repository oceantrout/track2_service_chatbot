"""Credential and environment doctor.

Tells you whether the LLM-backed modules (M2, M4 summaries, M6 answers)
can actually run, without spending real tokens on a big request.

    python -m graphrag_demo.cli doctor
"""

from __future__ import annotations

import os
import sys

from . import config


def check_credentials() -> tuple[bool, str]:
    """Return `(ok, detail)` for whether the Anthropic API is reachable."""
    try:
        import anthropic
    except ImportError:
        return False, "the `anthropic` package is not installed (pip install anthropic)"

    try:
        client = anthropic.Anthropic()
    except Exception as exc:  # noqa: BLE001 - constructor raises many types
        return False, f"could not build a client: {type(exc).__name__}: {exc}"

    # The SDK resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, then the
    # active OAuth profile. A missing key surfaces here as a 401.
    try:
        client.messages.create(
            model=config.MODEL,
            max_tokens=1,
            messages=[{"role": "user", "content": "hi"}],
        )
    except Exception as exc:  # noqa: BLE001 - want the message, not the type
        status = getattr(exc, "status_code", None)
        if status in (401, 403):
            return False, (
                "credentials were found but rejected (401/403). "
                "Run `ant auth login`, or set ANTHROPIC_API_KEY."
            )
        if status == 404:
            return False, f"model {config.MODEL} not found for this account."
        return False, f"{type(exc).__name__}: {exc}"

    return True, f"authenticated, model {config.MODEL} reachable"


def check_packages() -> list[tuple[str, bool, str]]:
    """Report which optional third-party packages are present."""
    checks: list[tuple[str, bool, str]] = []

    def present(module: str) -> bool:
        try:
            __import__(module)
        except ImportError:
            return False
        return True

    for module, why, optional in (
        ("anthropic", "LLM modules (M2, M4, M6)", False),
        ("sklearn", "TF-IDF baseline for M6 comparison", True),
        ("networkx", "graph ops for M3", True),
        ("graspologic", "Leiden community detection for M4", True),
    ):
        ok = present(module)
        note = why if ok else f"{why} - not installed"
        if optional and not ok:
            note += " (M3/M4 fall back to a pure-python implementation)"
        checks.append((module, ok, note))

    return checks


def check_artifacts() -> list[tuple[str, bool, str]]:
    """Report which module artifacts exist yet."""
    out = []
    for label, path in (
        ("M1 corpus", config.CORPUS_PATH),
        ("M2 extraction", config.EXTRACTED_PATH),
        ("M3 graph", config.GRAPH_PATH),
        ("M4 communities", config.COMMUNITIES_PATH),
    ):
        out.append((label, path.exists(), str(path)))
    return out


def run_doctor() -> int:
    print("environment")
    print(f"  python            {sys.version.split()[0]}")
    print(f"  model             {config.MODEL}")
    print(f"  key in env        {'yes' if os.environ.get('ANTHROPIC_API_KEY') else 'no'}")
    print()

    print("packages")
    for name, ok, note in check_packages():
        print(f"  {'ok  ' if ok else 'miss'} {name:<12} {note}")
    print()

    print("artifacts")
    for label, ok, path in check_artifacts():
        print(f"  {'ok  ' if ok else '    '} {label:<14} {path}")
    print()

    print("api")
    ok, detail = check_credentials()
    print(f"  {'ok  ' if ok else 'FAIL'} {detail}")
    if not ok:
        print()
        print("  The offline modules (M1, M3, M5) still work.")
        print("  For the LLM modules, either:")
        print("    - set ANTHROPIC_API_KEY and re-run this command, or")
        print("    - run `ant auth login` to use an OAuth profile instead.")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run_doctor())
