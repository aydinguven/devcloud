"""Rename the bundled Claude ACP persona without changing its behavior."""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path


DISPLAY_NAME = "TCMB Asistan"
PACKAGE_NAME = "jupyter-ai-acp-client"
PERSONA_MODULE = Path("jupyter_ai_acp_client/acp_personas/claude.py")
_CLAUDE_NAME = re.compile(
    r"(?m)^(?P<prefix>\s*name\s*=\s*)['\"]Claude['\"](?P<suffix>\s*,)"
)


def rename_persona(source_path: Path) -> None:
    source = source_path.read_text(encoding="utf-8")
    updated, count = _CLAUDE_NAME.subn(
        rf'\g<prefix>"{DISPLAY_NAME}"\g<suffix>',
        source,
    )
    if count != 1:
        raise RuntimeError(
            "Expected exactly one Claude persona display name in "
            f"{source_path}, found {count}"
        )
    source_path.write_text(updated, encoding="utf-8")


def installed_persona_path() -> Path:
    try:
        source_path = Path(distribution(PACKAGE_NAME).locate_file(PERSONA_MODULE))
    except PackageNotFoundError as exc:
        raise RuntimeError(f"Required package {PACKAGE_NAME!r} is not installed") from exc

    source_path = source_path.resolve()
    if not source_path.is_file():
        raise RuntimeError(f"Claude persona module was not found at {source_path}")
    return source_path


def main() -> None:
    rename_persona(installed_persona_path())


if __name__ == "__main__":
    main()
