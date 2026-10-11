#!/usr/bin/env python3
"""Loomlight — weaving draft designer, warp and weft colour planner and yardage estimator.

Loomlight keeps one weaving project in one JSON file, validates it before any
command reads or writes it, edits the draft in a terminal grid editor, plans the
warp and weft colours from a fixed 16-name palette, prints the draft grids to
stdout (so they can be piped or redirected) and turns the draft plus its
dimensions into warp and weft yardage.  Everything is offline: the standard
library only, no network, no external services.

Exit codes: 0 success, 2 validation or usage error, 3 passphrase refused.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import math
import os
import secrets
import shutil
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any, Callable, Sequence

VERSION = "1.0.0"
SCHEMA_VERSION = 1
STRUCTURES = ("plain", "twill")
DEFAULT_OUT = "draft.json"

# Committed design direction for this build: terminal-dark surface, one accent
# (oklch(0.58 0.12 158) == #288f5e) for primary status, muted grey for chrome, and
# never a colour-only signal -- every coloured line also carries its own words.
DESIGN = {
    "accent": "#288f5e",
    "accent_ansi": "\x1b[38;2;40;143;94m",
    "bold": "\x1b[1m",
    "muted": "\x1b[2m",
    "reset": "\x1b[0m",
}

PALETTE: tuple[str, ...] = (
    "black",
    "red",
    "green",
    "yellow",
    "blue",
    "magenta",
    "cyan",
    "white",
    "gray",
    "bright_red",
    "bright_green",
    "bright_yellow",
    "bright_blue",
    "bright_magenta",
    "bright_cyan",
    "bright_white",
)

# ANSI background codes used for the colour bands, and the letter fallback used
# whenever stdout is not a TTY or NO_COLOR is set.
PALETTE_ANSI: dict[str, int] = {
    "black": 40,
    "red": 41,
    "green": 42,
    "yellow": 43,
    "blue": 44,
    "magenta": 45,
    "cyan": 46,
    "white": 47,
    "gray": 100,
    "bright_red": 101,
    "bright_green": 102,
    "bright_yellow": 103,
    "bright_blue": 104,
    "bright_magenta": 105,
    "bright_cyan": 106,
    "bright_white": 107,
}

PALETTE_LETTER: dict[str, str] = {
    "black": "k",
    "red": "r",
    "green": "g",
    "yellow": "y",
    "blue": "b",
    "magenta": "m",
    "cyan": "c",
    "white": "w",
    "gray": "a",
    "bright_red": "R",
    "bright_green": "G",
    "bright_yellow": "Y",
    "bright_blue": "B",
    "bright_magenta": "M",
    "bright_cyan": "C",
    "bright_white": "W",
}

DIMENSION_FIELDS = (
    "width_in",
    "length_in",
    "epi",
    "ppi",
    "take_up_pct",
    "loom_waste_in",
    "weft_waste_pct",
)

# (argparse dest, project field, flag label used in error messages)
DIMENSION_LABELS = (
    ("width", "width_in", "width"),
    ("length", "length_in", "length"),
    ("epi", "epi", "epi"),
    ("ppi", "ppi", "ppi"),
    ("take_up", "take_up_pct", "take-up"),
    ("loom_waste", "loom_waste_in", "loom-waste"),
    ("weft_waste", "weft_waste_pct", "weft-waste"),
)

# The exact command fragment printed when an estimator input is still zero.
FIX_COMMANDS = {
    "width_in": "--width <inches>",
    "length_in": "--length <inches>",
    "epi": "--epi <ends per inch>",
    "ppi": "--ppi <picks per inch>",
}

LOCK_ITERATIONS = 200_000
LOCK_ALGO = "pbkdf2_hmac_sha256"
GRID_NAMES = ("threading", "treadling", "tie-up")

# One place that turns a raw key press into an editor token, shared by the TTY
# reader and by anything else that drives the editor.
KEY_ALIASES = {
    " ": "space",
    "\t": "tab",
    "s": "save",
    "q": "quit",
    "y": "yes",
    "n": "no",
    "h": "left",
    "j": "down",
    "k": "up",
    "l": "right",
    "\x03": "interrupt",
    "\x04": "quit",
}

MARK = "X"
BLANK = "."


class LoomlightError(Exception):
    """A failure the user should read and act on.

    ``code`` is the process exit code: 2 for validation and usage errors, 3 for a
    refused passphrase.  ``message`` may be empty when the failure has already
    been explained line by line (the passphrase attempts).
    """

    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------


def terminal_width() -> int:
    """Return the usable output width; pipes get an effectively unbounded one."""
    isatty = getattr(sys.stdout, "isatty", None)
    if not (isatty and isatty()):
        # Redirected output must never be clipped by a window-size guess.
        return 100_000
    return shutil.get_terminal_size(fallback=(80, 24)).columns


def color_enabled(stream: Any = None) -> bool:
    """True when ANSI colour may be written to ``stream``."""
    if stream is None:
        stream = sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def style(text: str, role: str, enabled: bool) -> str:
    """Wrap ``text`` in the committed accent when colour is enabled."""
    if not enabled:
        return text
    if role == "accent":
        return DESIGN["accent_ansi"] + text + DESIGN["reset"]
    if role == "bold":
        return DESIGN["bold"] + text + DESIGN["reset"]
    if role == "muted":
        return DESIGN["muted"] + text + DESIGN["reset"]
    return text


def note(message: str) -> None:
    """Print one line of context that is not the command's result."""
    print(message, file=sys.stderr)


# ---------------------------------------------------------------------------
# Small value helpers
# ---------------------------------------------------------------------------


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def show(value: Any) -> str:
    """Render a value for an error message, short enough to stay one line."""
    try:
        text = json.dumps(value)
    except (TypeError, ValueError):
        text = repr(value)
    if text is None:
        text = "null"
    if len(text) > 40:
        text = text[:37] + "..."
    return text


def format_number(value: float) -> str:
    """Format a measurement without a pointless trailing .0."""
    if float(value) == int(value):
        return str(int(value))
    return f"{value:.4g}"


# ---------------------------------------------------------------------------
# Project construction and JSON I/O
# ---------------------------------------------------------------------------


def default_dimensions() -> dict[str, Any]:
    return {"units": "in", **{field: 0.0 for field in DIMENSION_FIELDS}}


def build_plain_seed(shafts: int, treadles: int) -> dict[str, Any]:
    if shafts % 2:
        raise LoomlightError("plain weave needs an even shaft count")
    if treadles != 2:
        raise LoomlightError(f"plain weave uses 2 treadles, got {treadles}")
    threading = list(range(1, shafts + 1))
    treadling = [1, 2]
    # Treadle 1 lifts the odd shafts and treadle 2 the even ones: at 4 shafts this
    # is the familiar 1/3 and 2/4 tie-up.
    tieup = [[shaft % 2 == 1, shaft % 2 == 0] for shaft in range(1, shafts + 1)]
    return {"threading": threading, "treadling": treadling, "tieup": tieup}


def build_twill_seed(shafts: int, treadles: int) -> dict[str, Any]:
    if shafts != treadles:
        raise LoomlightError(
            f"twill weave needs shafts and treadles to match, "
            f"got {shafts} shafts and {treadles} treadles"
        )
    threading = list(range(1, shafts + 1))
    treadling = list(range(1, treadles + 1))
    tieup = [[False] * treadles for _ in range(shafts)]
    for index in range(shafts):
        first = index + 1
        second = (index + 1) % shafts + 1
        tieup[index][first - 1] = True
        tieup[index][second - 1] = True
    return {"threading": threading, "treadling": treadling, "tieup": tieup}


def build_new_project(
    shafts: int, treadles: int, structure: str, name: str
) -> dict[str, Any]:
    """Build a fresh project that already passes the loader."""
    if not 2 <= shafts <= 32:
        raise LoomlightError(f"shafts must be an integer in 2..32, got {shafts}")
    if not 2 <= treadles <= 32:
        raise LoomlightError(f"treadles must be an integer in 2..32, got {treadles}")
    if structure == "plain":
        seed = build_plain_seed(shafts, treadles)
    elif structure == "twill":
        seed = build_twill_seed(shafts, treadles)
    else:
        raise LoomlightError(f"structure must be plain or twill, got {structure!r}")
    return {
        "schema_version": SCHEMA_VERSION,
        "name": name,
        "structure": structure,
        "shafts": shafts,
        "treadles": treadles,
        "threading": seed["threading"],
        "treadling": seed["treadling"],
        "tieup": seed["tieup"],
        "warp": {"stripes": [{"color": "blue", "count": shafts}]},
        "weft": {"picks": [{"color": "white", "count": treadles}]},
        "dimensions": default_dimensions(),
    }


def project_text(project: dict[str, Any]) -> str:
    """Serialise a project deterministically: same project in, same bytes out."""
    return json.dumps(project, indent=2, ensure_ascii=False) + "\n"


def atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file in the same directory)."""
    directory = path.parent if str(path.parent) else Path(".")
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LoomlightError(f"{path}: cannot create {directory} ({exc.strerror or exc})")
    try:
        handle, temp_name = tempfile.mkstemp(
            dir=str(directory), prefix=path.name + ".", suffix=".tmp"
        )
    except OSError as exc:
        raise LoomlightError(f"{path}: cannot write file ({exc.strerror or exc})")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        raise LoomlightError(f"{path}: cannot write file ({exc.strerror or exc})")


def save_project(path: Path, project: dict[str, Any]) -> None:
    """Validate then write -- the file on disk is never left half written."""
    error = validate_project(project)
    if error is not None:
        raise LoomlightError(f"{path}: {error}")
    atomic_write(path, project_text(project))


# ---------------------------------------------------------------------------
# The single load-time validator
# ---------------------------------------------------------------------------


def validate_plan(
    plan: Any, section: str, key: str, noun: str, expected_sum: int, unit_word: str
) -> str | None:
    """Validate one stripe or pick plan; return the first bad field or None."""
    if not isinstance(plan, dict):
        return f"{section}: expected an object with a {key!r} list, found {show(plan)}"
    entries = plan.get(key)
    if not isinstance(entries, list):
        return (
            f"{section}.{key}: expected a list of colour/count entries, "
            f"found {show(entries)}"
        )
    total = 0
    for index, entry in enumerate(entries):
        where = f"{section}.{key}[{index}]"
        if not isinstance(entry, dict):
            return (
                f"{where}: expected an object with 'color' and 'count', "
                f"found {show(entry)}"
            )
        color = entry.get("color")
        if color not in PALETTE:
            return (
                f"{where}.color: {show(color)} is not one of the 16 palette names "
                f"({', '.join(PALETTE)})"
            )
        count = entry.get("count")
        if not is_int(count) or count < 1:
            return f"{where}.count: expected an integer of 1 or more, found {show(count)}"
        total += count
    if entries and total != expected_sum:
        return (
            f"{section}: the {noun} counts sum to {total} but the draft has "
            f"{expected_sum} {unit_word}"
        )
    return None


def validate_dimensions(dimensions: Any) -> str | None:
    if not isinstance(dimensions, dict):
        return f"dimensions: expected an object, found {show(dimensions)}"
    if dimensions.get("units") != "in":
        return f"dimensions.units: expected 'in', found {show(dimensions.get('units'))}"
    for field in DIMENSION_FIELDS:
        value = dimensions.get(field)
        if not is_number(value) or value < 0:
            return f"dimensions.{field}: expected a number >= 0, found {show(value)}"
    return None


def validate_project(project: Any) -> str | None:
    """Return the first problem as one plain-English line, or None when valid."""
    if not isinstance(project, dict):
        return f"project: expected a JSON object, found {show(project)}"

    version = project.get("schema_version")
    if not is_int(version) or version != SCHEMA_VERSION:
        return (
            f"schema_version: expected the integer {SCHEMA_VERSION}, "
            f"found {show(version)}"
        )

    if "name" in project and not isinstance(project["name"], str):
        return f"name: expected a string, found {show(project['name'])}"

    shafts = project.get("shafts")
    if not is_int(shafts) or not 2 <= shafts <= 32:
        return f"shafts: expected an integer in 2..32, found {show(shafts)}"

    treadles = project.get("treadles")
    if not is_int(treadles) or not 2 <= treadles <= 32:
        return f"treadles: expected an integer in 2..32, found {show(treadles)}"

    structure = project.get("structure")
    if structure not in STRUCTURES:
        return f"structure: expected 'plain' or 'twill', found {show(structure)}"

    threading = project.get("threading")
    if not isinstance(threading, list):
        return (
            f"threading: expected a list of {shafts} shaft numbers in 1..{shafts}, "
            f"found {show(threading)}"
        )
    if len(threading) != shafts:
        return (
            f"threading: expected {shafts} ends (one 1-based shaft per end), "
            f"found {len(threading)}"
        )
    for index, shaft in enumerate(threading, start=1):
        if not is_int(shaft) or not 1 <= shaft <= shafts:
            return (
                f"threading: end {index} has shaft {show(shaft)}, "
                f"expected an integer in 1..{shafts}"
            )

    treadling = project.get("treadling")
    if not isinstance(treadling, list):
        return (
            f"treadling: expected a list of {treadles} treadle numbers in 1..{treadles}, "
            f"found {show(treadling)}"
        )
    if len(treadling) != treadles:
        return (
            f"treadling: expected {treadles} picks (one 1-based treadle per pick), "
            f"found {len(treadling)}"
        )
    for index, treadle in enumerate(treadling, start=1):
        if not is_int(treadle) or not 1 <= treadle <= treadles:
            return (
                f"treadling: pick {index} has treadle {show(treadle)}, "
                f"expected an integer in 1..{treadles}"
            )

    tieup = project.get("tieup")
    expected = f"{shafts} rows of {treadles} booleans"
    if not isinstance(tieup, list):
        return f"tieup: expected {expected}, found {show(tieup)}"
    if len(tieup) != shafts:
        return f"tieup: expected {expected}, found {len(tieup)} rows"
    for row_index, row in enumerate(tieup, start=1):
        if not isinstance(row, list) or len(row) != treadles:
            found = f"{len(row)} columns" if isinstance(row, list) else show(row)
            return f"tieup: expected {expected}, row {row_index} has {found}"
        for column_index, cell in enumerate(row, start=1):
            if not isinstance(cell, bool):
                return (
                    f"tieup: shaft {row_index} treadle {column_index} "
                    f"must be true or false, found {show(cell)}"
                )

    error = validate_plan(
        project.get("warp"), "warp", "stripes", "stripe", shafts, "ends"
    )
    if error is not None:
        return error
    error = validate_plan(project.get("weft"), "weft", "picks", "pick", treadles, "picks")
    if error is not None:
        return error

    return validate_dimensions(project.get("dimensions"))


def load_project(path: Path) -> dict[str, Any]:
    """Read and validate a project; raise LoomlightError with one clear line."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise LoomlightError(f"{path}: no such file")
    except IsADirectoryError:
        raise LoomlightError(f"{path}: is a directory, not a project file")
    except OSError as exc:
        raise LoomlightError(f"{path}: cannot read file ({exc.strerror or exc})")
    try:
        project = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LoomlightError(
            f"{path}: not valid JSON (line {exc.lineno} column {exc.colno}: {exc.msg})"
        )
    error = validate_project(project)
    if error is not None:
        raise LoomlightError(f"{path}: {error}")
    return project


# ---------------------------------------------------------------------------
# The shared grid renderer
# ---------------------------------------------------------------------------


def threading_matrix(project: dict[str, Any]) -> list[list[str]]:
    """One row per shaft, one column per end."""
    threading = project["threading"]
    return [
        [MARK if threading[end] == shaft else BLANK for end in range(project["shafts"])]
        for shaft in range(1, project["shafts"] + 1)
    ]


def treadling_matrix(project: dict[str, Any]) -> list[list[str]]:
    """One row per treadle, one column per pick."""
    treadling = project["treadling"]
    return [
        [MARK if treadling[pick] == treadle else BLANK for pick in range(len(treadling))]
        for treadle in range(1, project["treadles"] + 1)
    ]


def tieup_matrix(project: dict[str, Any]) -> list[list[str]]:
    """One row per shaft, one column per treadle."""
    return [[MARK if cell else BLANK for cell in row] for row in project["tieup"]]


def project_matrices(project: dict[str, Any]) -> list[list[list[str]]]:
    return [threading_matrix(project), treadling_matrix(project), tieup_matrix(project)]


def grid_width(
    matrix: Sequence[Sequence[str]], cell_width: int, label_width: int, indices: bool
) -> int:
    columns = len(matrix[0]) if matrix else 0
    return (label_width + 1 if indices else 0) + columns * cell_width


def format_grid(
    matrix: Sequence[Sequence[str]],
    cell_width: int = 2,
    label_width: int | None = None,
    indices: bool = True,
    highlight: tuple[int, int] | None = None,
    ansi: bool = False,
) -> list[str]:
    """Render one grid with aligned columns and optional row/column indices."""
    rows = len(matrix)
    columns = len(matrix[0]) if rows else 0
    if not rows or not columns:
        return []
    if label_width is None:
        label_width = len(str(rows))
    lines: list[str] = []
    if indices:
        header = " " * (label_width + 1) + "".join(
            str(column).ljust(cell_width) for column in range(1, columns + 1)
        )
        lines.append(header.rstrip())
    for row_index, row in enumerate(matrix):
        prefix = f"{row_index + 1:>{label_width}} " if indices else ""
        cells: list[str] = []
        for column_index, mark in enumerate(row):
            if highlight == (row_index, column_index) and ansi:
                cells.append(f"\x1b[7m{mark.ljust(cell_width)}\x1b[0m")
            else:
                cells.append(mark.ljust(cell_width))
        line = prefix + "".join(cells)
        lines.append(line if ansi else line.rstrip())
    return lines


def band_cell(color: str, cell_width: int, ansi: bool) -> str:
    if ansi:
        return f"\x1b[{PALETTE_ANSI[color]}m{' ' * cell_width}\x1b[0m"
    return PALETTE_LETTER[color].ljust(cell_width)


def band_cells(colors: Sequence[str], cell_width: int, ansi: bool) -> str:
    return "".join(band_cell(color, cell_width, ansi) for color in colors)


def dotted_band(count: int, cell_width: int) -> str:
    return "." * (count * cell_width)


def plan_colors(entries: Sequence[dict[str, Any]]) -> list[str]:
    """Expand a stripe or pick plan into one colour name per end or pick."""
    colors: list[str] = []
    for entry in entries:
        colors.extend([entry["color"]] * entry["count"])
    return colors


def used_colors(project: dict[str, Any]) -> list[str]:
    """Colours present in either plan, ordered by the palette."""
    present = {
        entry["color"]
        for entries in (project["warp"]["stripes"], project["weft"]["picks"])
        for entry in entries
    }
    return [name for name in PALETTE if name in present]


def color_legend(project: dict[str, Any]) -> str | None:
    used = used_colors(project)
    if not used:
        return None
    pairs = ", ".join(f"{PALETTE_LETTER[name]} {name}" for name in used)
    return f"colour key: {pairs}"


def draft_needs(project: dict[str, Any], with_color: bool) -> tuple[int, int]:
    """Return (full_width, compact_width) the draft needs on screen."""
    shafts = project["shafts"]
    treadles = project["treadles"]
    label_width = max(len(str(max(shafts, treadles))), 4 if with_color else 0)
    matrices = project_matrices(project)
    full = max(grid_width(matrix, 2, label_width, True) for matrix in matrices)
    compact = max(grid_width(matrix, 1, label_width, False) for matrix in matrices)
    return full, compact


def narrow_message(what: str, width: int, needed: int, hint: str) -> str:
    return (
        f"{what}: this terminal is {width} columns wide and the draft needs {needed} "
        f"-- widen the window or {hint}"
    )


def render_output(
    project: dict[str, Any],
    file_label: str,
    with_color: bool,
    ansi: bool,
    width: int,
) -> list[str] | None:
    """Render the three draft grids, with colour bands; None when too narrow."""
    shafts = project["shafts"]
    treadles = project["treadles"]
    ends = len(project["threading"])
    picks = len(project["treadling"])
    label_width = max(len(str(max(shafts, treadles))), 4 if with_color else 0)

    threading = threading_matrix(project)
    treadling = treadling_matrix(project)
    tieup = tieup_matrix(project)
    matrices = (threading, treadling, tieup)

    full_width = max(grid_width(m, 2, label_width, True) for m in matrices)
    compact_width = max(grid_width(m, 1, label_width, False) for m in matrices)
    if width >= full_width:
        cell_width, indices = 2, True
    elif width >= compact_width:
        cell_width, indices = 1, False
    else:
        return None

    header = (
        f"{file_label} — {project['structure']} weave — {shafts} shafts, "
        f"{ends} ends, {treadles} treadles, {picks} picks"
    )
    lines = [header, ""]

    warp_entries = project["warp"]["stripes"]
    lines.append("threading (one row per shaft, one column per end)")
    if with_color:
        if warp_entries:
            band = band_cells(plan_colors(warp_entries), cell_width, ansi)
        else:
            band = dotted_band(ends, cell_width)
        lines.append("warp".ljust(label_width + 1) + band)
    lines.extend(format_grid(threading, cell_width, label_width, indices))
    lines.append("")

    weft_entries = project["weft"]["picks"]
    treadling_lines = format_grid(treadling, cell_width, label_width, indices)
    lines.append("treadling (one row per treadle, one column per pick)")
    if with_color:
        # One swatch per pick, drawn beside its treadling row; the column-index
        # line is skipped so the band lines up with the picks, not the header.
        weft_colors = plan_colors(weft_entries) if weft_entries else None
        pad = max(len(line) for line in treadling_lines)
        header_rows = len(treadling_lines) - len(treadling)
        for row_index, line in enumerate(treadling_lines):
            if row_index < header_rows:
                lines.append(line.ljust(pad) + "  weft")
                continue
            pick = row_index - header_rows
            if weft_colors is None or not 0 <= pick < len(weft_colors):
                swatch = "." * cell_width
            else:
                swatch = band_cell(weft_colors[pick], cell_width, ansi)
            lines.append(line.ljust(pad) + "  " + swatch)
    else:
        lines.extend(treadling_lines)
    lines.append("")

    lines.append("tie-up (one row per shaft, one column per treadle)")
    lines.extend(format_grid(tieup, cell_width, label_width, indices))

    if with_color:
        lines.append("")
        if not warp_entries:
            lines.append(
                "warp: no stripes yet -- run loomlight colors FILE --warp-solid NAME"
            )
        if not weft_entries:
            lines.append(
                "weft: no picks yet -- run loomlight colors FILE --weft-solid NAME"
            )
        if not ansi:
            legend = color_legend(project)
            if legend:
                lines.append(legend)
    return lines


# ---------------------------------------------------------------------------
# Yardage estimator
# ---------------------------------------------------------------------------


def estimate_yards(project: dict[str, Any]) -> tuple[float, float]:
    """Warp and weft yards for the draft's dimensions."""
    dims = project["dimensions"]
    width = float(dims["width_in"])
    length = float(dims["length_in"])
    epi = float(dims["epi"])
    ppi = float(dims["ppi"])
    take_up = float(dims["take_up_pct"])
    loom_waste = float(dims["loom_waste_in"])
    weft_waste = float(dims["weft_waste_pct"])
    warp = (length * (1 + take_up / 100) + loom_waste) * epi * width / 36
    weft = width * (1 + weft_waste / 100) * ppi * length / 36
    return warp, weft


def estimate_warnings(project: dict[str, Any], path: Path) -> list[str]:
    """One line per zero input, naming the exact command that fills it in."""
    dims = project["dimensions"]
    affected = {
        "width_in": ("warp", "weft"),
        "length_in": ("warp", "weft"),
        "epi": ("warp",),
        "ppi": ("weft",),
    }
    warnings: list[str] = []
    for field in ("width_in", "length_in", "epi", "ppi"):
        if dims[field]:
            continue
        for figure in affected[field]:
            warnings.append(
                f"{figure} yards need {field}: "
                f"run loomlight dimensions {path} {FIX_COMMANDS[field]}"
            )
    return warnings


# ---------------------------------------------------------------------------
# Passphrase lock
# ---------------------------------------------------------------------------


def lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def hash_passphrase(passphrase: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, iterations)


def read_lock(path: Path) -> dict[str, Any] | None:
    """Return the lock record, or None when the project is unlocked."""
    sidecar = lock_path(path)
    if not sidecar.exists():
        return None
    try:
        record = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LoomlightError(f"{sidecar}: unreadable lock sidecar ({exc})")
    if not isinstance(record, dict) or record.get("algo") != LOCK_ALGO:
        raise LoomlightError(f"{sidecar}: not a Loomlight lock sidecar")
    return record


def verify_passphrase(record: dict[str, Any], passphrase: str) -> bool:
    try:
        salt = base64.b64decode(record["salt_b64"], validate=True)
        expected = base64.b64decode(record["hash_b64"], validate=True)
        iterations = int(record["iterations"])
    except (KeyError, TypeError, ValueError):
        return False
    if iterations < 1:
        return False
    candidate = hash_passphrase(passphrase, salt, iterations)
    return hmac.compare_digest(candidate, expected)


def read_passphrase(prompt: str) -> str:
    """Read a passphrase: env override first, then a hidden terminal prompt.

    LOOMLIGHT_PASSPHRASE is the documented non-interactive escape hatch, so a
    script (or a recorded demo) never blocks on a prompt.
    """
    override = os.environ.get("LOOMLIGHT_PASSPHRASE")
    if override:
        return override
    isatty = getattr(sys.stdin, "isatty", None)
    if not (isatty and isatty()):
        raise LoomlightError(
            "passphrase required: run in a terminal or set LOOMLIGHT_PASSPHRASE",
            3,
        )
    return getpass.getpass(prompt)


def require_unlock(
    path: Path, prompt: Callable[[str], str] | None = None, attempts: int = 3
) -> None:
    """Refuse write commands while the project stays locked."""
    if prompt is None:
        prompt = read_passphrase
    record = read_lock(path)
    if record is None:
        return
    for _ in range(attempts):
        passphrase = prompt(f"passphrase for {lock_path(path).name}: ")
        if passphrase and verify_passphrase(record, passphrase):
            return
        # One non-enumerating line, identical whether the file or the passphrase
        # was wrong; the message is already on screen, so the raise stays silent.
        note("passphrase not accepted")
    raise LoomlightError("", 3)


def set_lock(path: Path, prompt: Callable[[str], str] | None = None) -> Path:
    """Store a fresh salted PBKDF2 hash beside the project."""
    if prompt is None:
        prompt = read_passphrase
    if read_lock(path) is not None:
        require_unlock(path, prompt)
    first = prompt("new passphrase: ")
    if not first:
        raise LoomlightError("passphrase must not be empty")
    second = prompt("repeat passphrase: ")
    if first != second:
        raise LoomlightError("passphrases did not match", 3)
    salt = secrets.token_bytes(16)
    digest = hash_passphrase(first, salt, LOCK_ITERATIONS)
    record = {
        "algo": LOCK_ALGO,
        "iterations": LOCK_ITERATIONS,
        "salt_b64": base64.b64encode(salt).decode("ascii"),
        "hash_b64": base64.b64encode(digest).decode("ascii"),
    }
    sidecar = lock_path(path)
    atomic_write(sidecar, json.dumps(record, indent=2) + "\n")
    return sidecar


def remove_lock(path: Path, prompt: Callable[[str], str] | None = None) -> Path:
    """Delete the sidecar once the current passphrase is accepted."""
    if prompt is None:
        prompt = read_passphrase
    record = read_lock(path)
    sidecar = lock_path(path)
    if record is None:
        raise LoomlightError(f"{path} is not locked -- nothing to remove")
    require_unlock(path, prompt)
    try:
        sidecar.unlink()
    except OSError as exc:
        raise LoomlightError(f"{sidecar}: cannot remove lock ({exc.strerror or exc})")
    return sidecar


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def parse_plan(spec: str, section: str) -> list[dict[str, Any]]:
    """Parse 'red:2,blue:2' into stripe or pick entries."""
    entries: list[dict[str, Any]] = []
    for chunk in spec.split(","):
        piece = chunk.strip()
        if not piece:
            raise LoomlightError(
                f"{section}: empty entry in {spec!r} -- expected NAME:COUNT"
            )
        name, colon, raw = piece.partition(":")
        name = name.strip()
        if not colon or not name:
            raise LoomlightError(
                f"{section}: {piece!r} is missing a count -- expected NAME:COUNT "
                f"(for example red:2)"
            )
        if name not in PALETTE:
            raise LoomlightError(
                f"{section}: {name!r} is not one of the 16 palette names "
                f"({', '.join(PALETTE)})"
            )
        raw = raw.strip()
        try:
            count = int(raw)
        except ValueError:
            raise LoomlightError(
                f"{section}: count for {name} must be an integer of 1 or more, "
                f"found {show(raw)}"
            )
        if count < 1:
            raise LoomlightError(
                f"{section}: count for {name} must be an integer of 1 or more, "
                f"found {count}"
            )
        entries.append({"color": name, "count": count})
    return entries


def check_palette(name: str, section: str) -> str:
    if name not in PALETTE:
        raise LoomlightError(
            f"{section}: {name!r} is not one of the 16 palette names "
            f"({', '.join(PALETTE)})"
        )
    return name


def cmd_new(args: argparse.Namespace) -> int:
    out = Path(args.out)
    if out.exists() and not args.force:
        raise LoomlightError(f"{out} already exists -- pass --force to overwrite it")
    treadles = args.treadles
    if treadles is None:
        treadles = 2 if args.structure == "plain" else args.shafts
    project = build_new_project(args.shafts, treadles, args.structure, out.stem or "draft")
    save_project(out, project)
    print(
        f"created {out} — {args.structure} weave, {args.shafts} shafts, "
        f"{treadles} treadles"
    )
    print(f"next: loomlight render {out}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    path = Path(args.file)
    try:
        load_project(path)
    except LoomlightError as exc:
        if args.json:
            print(json.dumps({"file": str(path), "ok": False, "error": exc.message}))
        raise
    if args.json:
        print(json.dumps({"file": str(path), "ok": True}))
    else:
        print(style("ok", "accent", color_enabled()))
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    path = Path(args.file)
    project = load_project(path)
    ansi = args.color and not args.no_color and color_enabled()
    width = terminal_width()
    lines = render_output(project, str(path), args.color, ansi, width)
    if lines is None:
        needed, _ = draft_needs(project, args.color)
        raise LoomlightError(
            narrow_message(
                "render",
                width,
                needed,
                f"redirect to a file: loomlight render {path} > draft.txt",
            )
        )
    print(style(lines[0], "accent", color_enabled()))
    for line in lines[1:]:
        print(line)
    return 0


def cmd_estimate(args: argparse.Namespace) -> int:
    path = Path(args.file)
    project = load_project(path)
    dims = project["dimensions"]
    warp, weft = estimate_yards(project)
    ends = len(project["threading"])
    picks = len(project["treadling"])
    warnings = estimate_warnings(project, path)

    if args.json:
        print(
            json.dumps(
                {
                    "file": str(path),
                    "warp_yards": round(warp, 4),
                    "weft_yards": round(weft, 4),
                    "total_yards": round(warp + weft, 4),
                    "repeat": {"ends": ends, "picks": picks},
                    "dimensions": dims,
                    "warnings": warnings,
                }
            )
        )
        return 0

    width = format_number(dims["width_in"])
    length = format_number(dims["length_in"])
    epi = format_number(dims["epi"])
    ppi = format_number(dims["ppi"])
    take_up = format_number(dims["take_up_pct"])
    waste = format_number(dims["loom_waste_in"])
    weft_waste = format_number(dims["weft_waste_pct"])

    print(style(f"{path} — {project['structure']} weave", "accent", color_enabled()))
    print(f"repeat        {ends} ends, {picks} picks (from the threading and treadling)")
    print(f"width         {width} in")
    print("")
    print(
        f"warp  {warp:.2f} yd = (length {length} in x (1 + take-up {take_up}%) "
        f"+ loom waste {waste} in) x epi {epi} x width {width} in / 36"
    )
    print(
        f"weft  {weft:.2f} yd = width {width} in x (1 + weft waste {weft_waste}%) "
        f"x ppi {ppi} x length {length} in / 36"
    )
    print(f"total {warp + weft:.2f} yd")
    for line in warnings:
        note(line)
    return 0


def cmd_dimensions(args: argparse.Namespace) -> int:
    path = Path(args.file)
    project = load_project(path)
    if any(getattr(args, dest) is not None for dest, _, _ in DIMENSION_LABELS):
        require_unlock(path)

    updates: dict[str, float] = {}
    for dest, field, flag in DIMENSION_LABELS:
        raw = getattr(args, dest)
        if raw is None:
            continue
        try:
            value = float(raw)
        except ValueError:
            raise LoomlightError(f"{flag} must be a number >= 0, got {raw}")
        if not math.isfinite(value) or value < 0:
            raise LoomlightError(f"{flag} must be a number >= 0, got {raw}")
        updates[field] = value

    if updates:
        project["dimensions"].update(updates)
        save_project(path, project)

    dims = project["dimensions"]
    labels = {
        "width_in": "width",
        "length_in": "length",
        "epi": "epi",
        "ppi": "ppi",
        "take_up_pct": "take-up %",
        "loom_waste_in": "loom waste in",
        "weft_waste_pct": "weft waste %",
    }
    print(style(f"dimensions (inches) — {path}", "accent", color_enabled()))
    for field in DIMENSION_FIELDS:
        print(f"  {labels[field]:<15}{format_number(dims[field])}")
    if not updates:
        flags = ", ".join(f"--{flag}" for _, _, flag in DIMENSION_LABELS)
        print("")
        print(f"nothing changed -- set any of: {flags}")
    return 0


def cmd_colors(args: argparse.Namespace) -> int:
    if args.list_colors:
        for name in PALETTE:
            print(name)
        return 0
    if not args.file:
        raise LoomlightError("colors: FILE is required unless --list-colors is used")
    if args.warp and args.warp_solid:
        raise LoomlightError("colors: pass --warp or --warp-solid, not both")
    if args.weft and args.weft_solid:
        raise LoomlightError("colors: pass --weft or --weft-solid, not both")

    path = Path(args.file)
    project = load_project(path)

    warp_entries = parse_plan(args.warp, "warp") if args.warp else None
    weft_entries = parse_plan(args.weft, "weft") if args.weft else None
    if args.warp_solid:
        warp_entries = [
            {
                "color": check_palette(args.warp_solid, "warp"),
                "count": len(project["threading"]),
            }
        ]
    if args.weft_solid:
        weft_entries = [
            {
                "color": check_palette(args.weft_solid, "weft"),
                "count": len(project["treadling"]),
            }
        ]

    if warp_entries is None and weft_entries is None:
        return preview_colors(project, path)
    require_unlock(path)
    if warp_entries is not None:
        project["warp"]["stripes"] = warp_entries
    if weft_entries is not None:
        project["weft"]["picks"] = weft_entries
    save_project(path, project)
    return preview_colors(project, path)


def preview_colors(project: dict[str, Any], path: Path, width: int | None = None) -> int:
    ansi = color_enabled()
    if width is None:
        width = terminal_width()
    lines = render_output(project, str(path), True, ansi, width)
    if lines is None:
        needed, _ = draft_needs(project, True)
        raise LoomlightError(
            narrow_message(
                "colors",
                width,
                needed,
                f"redirect to a file: loomlight colors {path} > colors.txt",
            )
        )
    print(style(lines[0], "accent", color_enabled()))
    for line in lines[1:]:
        print(line)
    print("")
    for key, noun, total in (
        ("warp", "stripe", len(project["threading"])),
        ("weft", "pick", len(project["treadling"])),
    ):
        entries = project[key]["stripes" if key == "warp" else "picks"]
        if entries:
            listing = ", ".join(f"{entry['color']} x{entry['count']}" for entry in entries)
            print(f"{key} {noun}s: {listing} ({total} {key} slots)")
        else:
            print(
                f"no {key} {noun}s yet — pass --{key} NAME:COUNT or "
                f"--{key}-solid NAME"
            )
            print(
                textwrap.fill(
                    "palette: " + ", ".join(PALETTE),
                    width=88,
                    subsequent_indent=" " * 9,
                )
            )
    return 0


def cmd_lock(args: argparse.Namespace) -> int:
    path = Path(args.file)
    load_project(path)
    sidecar = lock_path(path)
    if args.set and args.remove:
        raise LoomlightError("lock: pass --set or --remove, not both")
    if args.set:
        set_lock(path)
        print(style(f"project locked — {sidecar}", "accent", color_enabled()))
        print("edit, colors and dimensions now ask for the passphrase")
        return 0
    if args.remove:
        remove_lock(path)
        print(f"lock removed — {sidecar}")
        print("write commands no longer prompt")
        return 0
    if read_lock(path) is None:
        print(f"this project is not locked — run: loomlight lock {path} --set")
    else:
        print(f"project locked — {sidecar}")
        print(f"run: loomlight lock {path} --remove to unlock")
    return 0


# ---------------------------------------------------------------------------
# Terminal grid editor
# ---------------------------------------------------------------------------


class Editor:
    """Cursor, cells and dirty flag for the three draft grids."""

    def __init__(self, project: dict[str, Any]) -> None:
        self.project = project
        self.dirty = False
        self.active = 0
        self.cursors: list[tuple[int, int]] = [(0, 0), (0, 0), (0, 0)]

    def matrix(self, index: int) -> list[list[str]]:
        return project_matrices(self.project)[index]

    def size(self, index: int) -> tuple[int, int]:
        matrix = self.matrix(index)
        return len(matrix), len(matrix[0])

    def move(self, row_delta: int, column_delta: int) -> None:
        rows, columns = self.size(self.active)
        row, column = self.cursors[self.active]
        self.cursors[self.active] = (
            min(max(row + row_delta, 0), rows - 1),
            min(max(column + column_delta, 0), columns - 1),
        )

    def next_grid(self) -> None:
        self.active = (self.active + 1) % len(GRID_NAMES)

    def activate(self) -> None:
        """Space: single-select for threading and treadling, toggle for tie-up."""
        row, column = self.cursors[self.active]
        project = self.project
        if self.active == 0:
            if project["threading"][column] == row + 1:
                # Already the selected shaft: nothing the one-per-end format could store.
                return
            project["threading"][column] = row + 1
        elif self.active == 1:
            if project["treadling"][column] == row + 1:
                return
            project["treadling"][column] = row + 1
        else:
            project["tieup"][row][column] = not project["tieup"][row][column]
        self.dirty = True

    def status_line(self) -> str:
        row, column = self.cursors[self.active]
        project = self.project
        if self.active == 0:
            what = f"end {column + 1} -> shaft {project['threading'][column]}"
        elif self.active == 1:
            what = f"pick {column + 1} -> treadle {project['treadling'][column]}"
        else:
            lift = "lift" if project["tieup"][row][column] else "no lift"
            what = f"shaft {row + 1} x treadle {column + 1} -> {lift}"
        value = self.matrix(self.active)[row][column]
        keys = "arrows/hjkl move, space set, Tab grid, s save, q quit"
        return (
            f"{GRID_NAMES[self.active]}: {what} | cell row {row + 1} col {column + 1} "
            f"= {value} | keys: {keys}"
        )


def editor_screen(
    editor: Editor, file_label: str, width: int, ansi: bool, note_text: str = ""
) -> list[str] | None:
    """Return the editor frame, or None when the terminal cannot hold the grids."""
    project = editor.project
    ends = len(project["threading"])
    picks = len(project["treadling"])
    full_width, compact_width = draft_needs(project, False)
    if width < compact_width:
        return None
    cell_width, indices = (2, True) if width >= full_width else (1, False)
    label_width = len(str(max(project["shafts"], project["treadles"])))

    header = (
        f"{file_label} — {project['structure']} weave — {project['shafts']} shafts, "
        f"{ends} ends, {project['treadles']} treadles, {picks} picks"
    )
    if editor.dirty:
        header += "  [unsaved changes]"
    lines = [header, ""]
    for index, name in enumerate(GRID_NAMES):
        rows, columns = editor.size(index)
        active = index == editor.active
        lines.append(f"{'>' if active else ' '} {name} ({rows} x {columns})")
        highlight = editor.cursors[index] if active else None
        lines.extend(
            format_grid(
                editor.matrix(index), cell_width, label_width, indices, highlight, ansi
            )
        )
        lines.append("")
    status = editor.status_line()
    if note_text:
        status = f"{note_text} | {status}"
    lines.append(style(status, "muted", ansi))
    return lines


def read_key_tty() -> str:
    """Read one key press from a cbreak terminal as a raw key token."""
    import termios
    import tty

    handle = sys.stdin.fileno()
    saved = termios.tcgetattr(handle)
    try:
        tty.setcbreak(handle)
        char = sys.stdin.read(1)
        if char == "":
            return "quit"
        if char == "\x1b":
            rest = sys.stdin.read(2)
            return {"[A": "up", "[B": "down", "[C": "right", "[D": "left"}.get(
                rest, "unknown"
            )
        return char
    finally:
        termios.tcsetattr(handle, termios.TCSADRAIN, saved)


def normalize_key(key: str) -> str:
    """Map a raw key press onto an editor token, leaving tokens untouched."""
    return KEY_ALIASES.get(key, key)


def run_editor(
    path: Path,
    project: dict[str, Any],
    read_key: Callable[[], str],
    write: Callable[[str], None],
    width: int | None = None,
    ansi: bool = False,
    clear: bool = False,
) -> int:
    """Run the editor loop; returns the process exit code."""
    editor = Editor(project)
    if width is None:
        width = terminal_width()
    if editor_screen(editor, str(path), width, ansi) is None:
        needed, _ = draft_needs(project, False)
        raise LoomlightError(
            narrow_message(
                "edit",
                width,
                needed,
                f"redirect the draft instead: loomlight render {path} > draft.txt",
            )
        )
    note_text = ""
    while True:
        screen = editor_screen(editor, str(path), width, ansi, note_text)
        if clear:
            write("\x1b[2J\x1b[H")
        write("\n".join(screen) + "\n")
        note_text = ""
        key = normalize_key(read_key())
        if key == "interrupt":
            raise KeyboardInterrupt
        if key == "quit":
            if not editor.dirty:
                write(f"left the draft editor — {path} unchanged\n")
                return 0
            write("save before quitting? (y/n) ")
            while True:
                answer = normalize_key(read_key())
                if answer == "yes":
                    save_project(path, editor.project)
                    editor.dirty = False
                    write(f"\nsaved {path}\n")
                    return 0
                if answer in ("no", "quit"):
                    # "quit" here means end of input: leave the draft on disk alone.
                    write(f"\nleft the draft editor — {path} unchanged\n")
                    return 0
                write("\nsave before quitting? (y/n) ")
        elif key == "save":
            save_project(path, editor.project)
            editor.dirty = False
            note_text = f"saved {path}"
        elif key == "tab":
            editor.next_grid()
        elif key == "space":
            editor.activate()
        elif key in ("up", "down", "left", "right"):
            deltas = {"up": (-1, 0), "down": (1, 0), "left": (0, -1), "right": (0, 1)}
            editor.move(*deltas[key])
        else:
            note_text = "unknown key"


def cmd_edit(args: argparse.Namespace) -> int:
    path = Path(args.file)
    project = load_project(path)
    require_unlock(path)
    isatty = getattr(sys.stdin, "isatty", None)
    if not (isatty and isatty()):
        raise LoomlightError(
            "edit needs an interactive terminal (stdin is not a TTY) -- "
            f"use loomlight render {path} to print the draft instead"
        )
    return run_editor(
        path,
        project,
        read_key=read_key_tty,
        write=sys.stdout.write,
        width=terminal_width(),
        ansi=color_enabled(),
        clear=True,
    )


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

COMMANDS = (
    "new",
    "edit",
    "colors",
    "dimensions",
    "render",
    "estimate",
    "validate",
    "lock",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loomlight",
        description=(
            "Loomlight — weaving draft designer, warp and weft colour planner and "
            "yardage estimator. Offline, standard library only."
        ),
        epilog=(
            "Every command validates the project file before it reads or writes, so "
            "a bad file reports one plain-English line and exits 2."
        ),
    )
    parser.add_argument("--version", action="version", version=f"loomlight {VERSION}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    new = sub.add_parser("new", help="create a plain or twill draft that already validates")
    new.add_argument("--out", default=DEFAULT_OUT, metavar="PATH", help=f"file to write (default {DEFAULT_OUT})")
    new.add_argument("--shafts", type=int, default=4, metavar="N", help="shafts to draft, 2..32 (default 4)")
    new.add_argument("--treadles", type=int, default=None, metavar="N", help="treadles, 2..32 (default 2 plain, shafts twill)")
    new.add_argument("--structure", choices=STRUCTURES, default="plain", help="plain or twill weave (default plain)")
    new.add_argument("--force", action="store_true", help="overwrite an existing file")
    new.set_defaults(func=cmd_new)

    validate = sub.add_parser("validate", help="check a file and print ok or the first problem")
    validate.add_argument("file", metavar="FILE", help="project JSON to check")
    validate.add_argument("--json", action="store_true", help="print the result as JSON")
    validate.set_defaults(func=cmd_validate)

    render = sub.add_parser("render", help="print the threading, treadling and tie-up grids")
    render.add_argument("file", metavar="FILE", help="project JSON to print")
    render.add_argument("--color", action="store_true", help="add the warp and weft colour bands")
    render.add_argument("--no-color", action="store_true", help="force the plain-letter colour fallback")
    render.set_defaults(func=cmd_render)

    estimate = sub.add_parser("estimate", help="print warp, weft and total yards with formulas")
    estimate.add_argument("file", metavar="FILE", help="project JSON to estimate")
    estimate.add_argument("--json", action="store_true", help="print the yardage as JSON")
    estimate.set_defaults(func=cmd_estimate)

    dimensions = sub.add_parser(
        "dimensions", help="set the imperial measurements the estimator reads"
    )
    dimensions.add_argument("file", metavar="FILE", help="project JSON to edit")
    for dest, _field, flag in DIMENSION_LABELS:
        dimensions.add_argument(
            f"--{flag}", dest=dest, metavar="N", help=f"{flag}: inches, percent or count (0 or more)"
        )
    dimensions.set_defaults(func=cmd_dimensions)

    colors = sub.add_parser("colors", help="set the warp and weft colour plans and preview the bands")
    colors.add_argument("file", metavar="FILE", nargs="?", help="project JSON to edit")
    colors.add_argument("--warp", metavar="NAME:COUNT,...", help="warp stripe plan, for example red:2,blue:2")
    colors.add_argument("--weft", metavar="NAME:COUNT,...", help="weft pick plan, for example white:4")
    colors.add_argument("--warp-solid", metavar="NAME", help="one warp stripe covering every end")
    colors.add_argument("--weft-solid", metavar="NAME", help="one weft pick covering every pick")
    colors.add_argument("--list-colors", action="store_true", help="list the 16 palette names and exit")
    colors.set_defaults(func=cmd_colors)

    edit = sub.add_parser("edit", help="edit the draft grids in the terminal editor")
    edit.add_argument("file", metavar="FILE", help="project JSON to edit")
    edit.set_defaults(func=cmd_edit)

    lock = sub.add_parser("lock", help="set, remove or report the local passphrase lock")
    lock.add_argument("file", metavar="FILE", help="project JSON to lock")
    lock.add_argument("--set", action="store_true", help="store a new passphrase hash beside the file")
    lock.add_argument("--remove", action="store_true", help="remove the sidecar after checking the passphrase")
    lock.set_defaults(func=cmd_lock)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        build_parser().print_help()
        return 0
    if not arguments[0].startswith("-") and arguments[0] not in COMMANDS:
        print(
            f"loomlight: unknown command {arguments[0]!r} — valid commands: "
            + ", ".join(COMMANDS),
            file=sys.stderr,
        )
        return 2

    parser = build_parser()
    args = parser.parse_args(arguments)
    try:
        return int(args.func(args))
    except LoomlightError as exc:
        if exc.message:
            print(exc.message, file=sys.stderr)
        return exc.code
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except OSError:
            pass
        return 0
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
