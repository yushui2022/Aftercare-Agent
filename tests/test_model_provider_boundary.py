from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACTIVE_ROOTS = (
    ROOT / "aftercare_agent",
    ROOT / "deploy",
    ROOT / "web" / "src",
    ROOT / ".github",
)
FORBIDDEN = re.compile(r"\bjev\b|typesafe/jev|openrouter", re.IGNORECASE)


def test_experimental_provider_is_absent_from_active_surfaces() -> None:
    matches: list[str] = []
    for base in ACTIVE_ROOTS:
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix in {".pyc", ".map"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if FORBIDDEN.search(text):
                matches.append(str(path.relative_to(ROOT)))

    assert matches == []
