"""Rename the bundled Claude ACP persona without changing its behavior."""

from __future__ import annotations

import re
from pathlib import Path


DISPLAY_NAME = "TCMB Asistan"
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


def main() -> None:
    from jupyter_ai_acp_client.acp_personas import claude

    rename_persona(Path(claude.__file__).resolve())


if __name__ == "__main__":
    main()
