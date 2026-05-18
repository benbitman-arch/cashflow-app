"""Hardcoded customer names to ignore in the OMD DEBT file.

These are the customer names that were highlighted yellow in the source file
on 2026-05-14 — brokers / related entities whose receivables should not be
counted toward cash flow.

`is_excluded(name)` does whitespace-collapsed case-insensitive matching, so
small future variations ("OM  FL" vs "OM FL") are handled.

The Streamlit UI exposes `add_name()` so the boss can extend the list at
runtime; new entries are appended below the EXCLUDE_NAMES literal.
"""

from __future__ import annotations

import re
from pathlib import Path

EXCLUDE_NAMES: list[str] = [
    "5th FIVE CS",
    "CJL Diamond Brokers",
    "CLARA",
    "Cachet Fine Jewellery",
    "Dana Point Jewelers, Inc.",
    "Diamond Vault Inc.",
    "Diamond and Memories, Inc.",
    "FM Trading",
    "Fm Trading Co.",
    "G KROWN INC",
    "Gem & You Corp.",
    "Gnat Jewelry Atelier",
    "Green Rocks Diamonds LLC",
    "In Bloom Holding Inc. ($USD)",
    "Little Shiny Box",
    "Mimi et Cie., LLC",
    "OM  FL",
    "OM EUROPE",
    "OM FL",
    "OM JEWELRY INC",
    "OMD-Sky Diamonds",
    "RAY'S JEWELRY INTERNATIONAL",
    "Rockwell House dba Frank Darling",
    "SKY- israel  SALES",
    "UPOMD LLC",
    "om london-usa client",
    'א.ו.ר.י יהלומי שמעון בלולו בע"מ',
    'אברי-דיאם בע"מ',
    "בבייב יעקב",
    'הויזמן י.א יהלומים בע"מ',
    "יהלומי נשר",
    'יהלומי רפי בן-דוד נתניה בע"מ',
    "עדי שטילמן",
    'ש.נ. אסיה (ישראל) בע"מ',
    "Diamond E Company",
    "Trinity Import",
    "איתן גורנשטיין",
    "דוידוב יעקוב",
]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip()).casefold()


_EXCLUDE_SET = {_norm(n) for n in EXCLUDE_NAMES}


def is_excluded(name: str) -> bool:
    return _norm(name) in _EXCLUDE_SET


def add_name(name: str) -> bool:
    """Append a new name to EXCLUDE_NAMES and persist to this file. Returns True if added."""
    name = name.strip()
    if not name or is_excluded(name):
        return False
    EXCLUDE_NAMES.append(name)
    _EXCLUDE_SET.add(_norm(name))
    _persist()
    return True


def _persist() -> None:
    path = Path(__file__)
    text = path.read_text(encoding="utf-8")
    start = text.index("EXCLUDE_NAMES: list[str] = [")
    end = text.index("]\n", start) + 1
    rendered = (
        "EXCLUDE_NAMES: list[str] = [\n"
        + "".join(f"    {_pyrepr(n)},\n" for n in EXCLUDE_NAMES)
        + "]"
    )
    path.write_text(text[:start] + rendered + text[end:], encoding="utf-8")


def _pyrepr(s: str) -> str:
    if '"' in s and "'" not in s:
        return "'" + s + "'"
    return '"' + s.replace('"', '\\"') + '"'
