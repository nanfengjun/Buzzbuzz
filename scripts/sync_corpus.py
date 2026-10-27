#!/usr/bin/env python3
"""Copy exported notes into the self-contained skill with tokens removed.

Source notes are read-only. Existing destination files are overwritten with
sanitized copies; files that disappeared upstream are left in place so this
script does not silently delete archival material.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re


def sanitize(text: str) -> str:
    # Keep the stable note URL and remove volatile access/query parameters.
    text = re.sub(
        r"(?m)^(- 笔记链接：)(https://www\.xiaohongshu\.com/explore/[A-Za-z0-9]+)(?:\?[^\n]*)$",
        r"\1\2",
        text,
    )
    return text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", help="Original exported notes directory")
    parser.add_argument("destination", help="Skill-local notes directory")
    args = parser.parse_args()

    source = Path(args.source).expanduser().resolve()
    destination = Path(args.destination).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)

    notes = sorted(source.glob("*.md"))
    if not notes:
        raise SystemExit(f"No markdown notes found under {source}")

    for path in notes:
        text = path.read_text(encoding="utf-8")
        if "## 笔记正文" not in text:
            continue
        (destination / path.name).write_text(sanitize(text), encoding="utf-8")

    print(f"Synced {len(notes)} notes to {destination}")


if __name__ == "__main__":
    main()
