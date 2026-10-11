"""Regression tests for Loomlight.

Run either way::

    python3 -m unittest discover -s tests
    python3 -m pytest tests

Everything runs offline against temporary files; the terminal editor is driven
by a scripted key reader so no test needs a real TTY.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loomlight  # noqa: E402  (import follows the sys.path fix above)


def run(argv: list[str]) -> tuple[int, str, str]:
    """Run loomlight in-process and capture its streams and exit code."""
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = loomlight.main(argv)
    except SystemExit as exc:  # argparse handles --help/--version by exiting
        code = 0 if exc.code is None else int(exc.code)
    return code, out.getvalue(), err.getvalue()


class ProjectCase(unittest.TestCase):
    """Shared fixture: a temporary working directory and helper commands."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def path(self, name: str = "draft.json") -> Path:
        return self.dir / name

    def new(self, *extra: str, name: str = "draft.json") -> Path:
        target = self.path(name)
        code, out, err = run(["new", "--out", str(target), *extra])
        self.assertEqual(code, 0, msg=err + out)
        return target

    def write_json(self, name: str, project: dict) -> Path:
        target = self.path(name)
        target.write_text(json.dumps(project, indent=2), encoding="utf-8")
        return target

    def read_project(self, path: Path) -> dict:
        """Parse a project file with the safe JSON parser (never a code loader)."""
        return json.loads(path.read_text(encoding="utf-8"))


class SeedTests(ProjectCase):
    def test_plain_seed_writes_straight_draw_and_two_treadle_tieup(self) -> None:
        code, out, err = run(
            ["new", "--shafts", "4", "--treadles", "2", "--structure", "plain",
             "--out", str(self.path())]
        )
        self.assertEqual(code, 0, msg=err)
        self.assertIn(str(self.path()), out)
        draft = self.read_project(self.path())
        self.assertEqual(draft["threading"], [1, 2, 3, 4])
        self.assertEqual(draft["treadling"], [1, 2])
        self.assertEqual(draft["structure"], "plain")
        # Treadle 1 lifts the odd shafts, treadle 2 the even ones.
        self.assertEqual([row[0] for row in draft["tieup"]], [True, False, True, False])
        self.assertEqual([row[1] for row in draft["tieup"]], [False, True, False, True])
        self.assertEqual(run(["validate", str(self.path())])[0], 0)

    def test_new_file_renders_and_is_valid_immediately(self) -> None:
        path = self.new()
        code, out, _ = run(["render", str(path)])
        self.assertEqual(code, 0)
        for label in ("threading", "treadling", "tie-up"):
            self.assertIn(label, out)

    def test_plain_seed_rejects_odd_shaft_count_without_writing(self) -> None:
        code, _, err = run(["new", "--shafts", "5", "--out", str(self.path("q.json"))])
        self.assertEqual(code, 2)
        self.assertIn("plain weave needs an even shaft count", err)
        self.assertFalse(self.path("q.json").exists())

    def test_plain_seed_rejects_extra_treadles(self) -> None:
        code, _, err = run(
            ["new", "--shafts", "4", "--treadles", "3", "--out", str(self.path())]
        )
        self.assertEqual(code, 2)
        self.assertIn("plain weave uses 2 treadles", err)
        self.assertFalse(self.path().exists())

    def test_twill_seed_requires_matching_counts_and_names_both(self) -> None:
        code, _, err = run(
            ["new", "--shafts", "8", "--treadles", "4", "--structure", "twill",
             "--out", str(self.path("r.json"))]
        )
        self.assertEqual(code, 2)
        self.assertIn("8 shafts", err)
        self.assertIn("4 treadles", err)
        self.assertFalse(self.path("r.json").exists())

    def test_twill_seed_has_diagonal_two_lift_tieup(self) -> None:
        path = self.new("--shafts", "4", "--treadles", "4", "--structure", "twill",
                        name="t.json")
        draft = self.read_project(path)
        self.assertEqual(draft["treadling"], [1, 2, 3, 4])
        self.assertTrue(all(len(row) == 4 for row in draft["tieup"]))
        self.assertTrue(all(sum(row) == 2 for row in draft["tieup"]))
        self.assertEqual(run(["validate", str(path)])[0], 0)

    def test_new_refuses_to_overwrite_without_force(self) -> None:
        path = self.new()
        before = path.read_bytes()
        code, _, err = run(["new", "--out", str(path)])
        self.assertEqual(code, 2)
        self.assertIn("--force", err)
        self.assertEqual(path.read_bytes(), before)

    def test_force_overwrites_and_shaft_range_is_checked(self) -> None:
        path = self.new()
        self.assertEqual(run(["new", "--out", str(path), "--force", "--shafts", "6"])[0], 0)
        self.assertEqual(self.read_project(path)["shafts"], 6)
        code, _, err = run(["new", "--shafts", "40", "--out", str(self.path("x.json"))])
        self.assertEqual(code, 2)
        self.assertIn("2..32", err)


class ValidatorTests(ProjectCase):
    def test_ok_on_a_freshly_created_project(self) -> None:
        code, out, _ = run(["validate", str(self.new())])
        self.assertEqual(code, 0)
        self.assertIn("ok", out)

    def test_tieup_row_count_is_reported_and_file_untouched(self) -> None:
        draft = self.read_project(self.new())
        draft["tieup"] = draft["tieup"][:3]
        path = self.write_json("bad.json", draft)
        before = path.read_bytes()
        code, _, err = run(["validate", str(path)])
        self.assertEqual(code, 2)
        self.assertIn("tieup: expected 4 rows of 2 booleans, found 3 rows", err)
        self.assertEqual(path.read_bytes(), before)

    def test_unknown_colour_names_the_palette(self) -> None:
        draft = self.read_project(self.new())
        draft["warp"]["stripes"] = [{"color": "teal", "count": 4}]
        path = self.write_json("bad.json", draft)
        code, _, err = run(["validate", str(path)])
        self.assertEqual(code, 2)
        self.assertIn("teal", err)
        self.assertIn("bright_white", err)

    def test_warp_counts_must_sum_to_the_ends(self) -> None:
        draft = self.read_project(self.new())
        draft["warp"]["stripes"] = [{"color": "red", "count": 5}]
        path = self.write_json("bad.json", draft)
        code, _, err = run(["validate", str(path)])
        self.assertEqual(code, 2)
        self.assertIn("the stripe counts sum to 5 but the draft has 4 ends", err)

    def test_negative_dimensions_are_rejected(self) -> None:
        draft = self.read_project(self.new())
        draft["dimensions"]["epi"] = -8
        path = self.write_json("bad.json", draft)
        code, _, err = run(["validate", str(path)])
        self.assertEqual(code, 2)
        self.assertIn("dimensions.epi", err)

    def test_malformed_json_and_missing_file_exit_two(self) -> None:
        broken = self.path("broken.json")
        broken.write_text("{not json", encoding="utf-8")
        code, _, err = run(["validate", str(broken)])
        self.assertEqual(code, 2)
        self.assertIn("not valid JSON", err)
        code, _, err = run(["validate", str(self.path("nope.json"))])
        self.assertEqual(code, 2)
        self.assertIn("no such file", err)

    def test_validate_json_mode(self) -> None:
        code, out, _ = run(["validate", "--json", str(self.new())])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["ok"])


class RenderTests(ProjectCase):
    def test_three_grids_print_with_indices(self) -> None:
        path = self.new()
        code, out, _ = run(["render", str(path)])
        self.assertEqual(code, 0)
        blocks = [block for block in out.split("\n\n") if block.strip()]
        self.assertEqual(len(blocks), 4)  # header + three grids
        self.assertIn("4 shafts, 4 ends, 2 treadles, 2 picks", blocks[0].splitlines()[0])
        threading = blocks[1].splitlines()
        self.assertEqual(threading[0], "threading (one row per shaft, one column per end)")
        self.assertEqual(len(threading), 1 + 1 + 4)
        self.assertEqual(len(blocks[2].splitlines()), 1 + 1 + 2)
        self.assertEqual(len(blocks[3].splitlines()), 1 + 1 + 4)
        self.assertEqual(blocks[1].splitlines()[1].strip(), "1 2 3 4")
        self.assertEqual(blocks[1].splitlines()[2].strip(), "1 X . . .")

    def test_colour_bands_fall_back_to_letters_when_piped(self) -> None:
        path = self.new()
        self.assertEqual(run(["colors", str(path), "--warp", "red:2,blue:2"])[0], 0)
        code, out, _ = run(["render", "--color", str(path)])
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b[", out)
        self.assertIn("r r b b", out)
        self.assertIn("colour key: r red, b blue", out)
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            code, no_colour, _ = run(["render", "--color", str(path)])
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b[", no_colour)
        self.assertIn("warp r r b b", no_colour)

    def test_ansi_swatches_render_when_colour_is_on(self) -> None:
        path = self.new()
        run(["colors", str(path), "--warp-solid", "red"])
        project = loomlight.load_project(path)
        text = "\n".join(loomlight.render_output(project, str(path), True, True, 200))
        self.assertIn("\x1b[41m", text)  # red background swatch
        self.assertIn("weft", text)

    def test_bad_file_prints_the_reason_and_no_grids(self) -> None:
        draft = self.read_project(self.new())
        draft["tieup"] = draft["tieup"][:3]
        path = self.write_json("bad.json", draft)
        code, out, err = run(["render", str(path)])
        self.assertEqual(code, 2)
        self.assertNotIn("threading", out)
        self.assertIn("tieup: expected 4 rows", err)

    def test_wide_draft_is_not_truncated_when_piped(self) -> None:
        path = self.new("--shafts", "32", "--treadles", "2", name="wide.json")
        code, out, _ = run(["render", str(path)])
        self.assertEqual(code, 0)
        body = [line for line in out.splitlines() if "X" in line]
        self.assertTrue(any(len(line) > 60 for line in body))
        # 32 threading marks + 2 treadling marks + 32 tie-up lifts.
        self.assertEqual(out.count("X"), 32 + 2 + 32)

    def test_narrow_terminal_asks_for_a_wider_window(self) -> None:
        path = self.new()
        project = loomlight.load_project(path)
        self.assertIsNone(loomlight.render_output(project, str(path), False, False, 3))


class EstimateTests(ProjectCase):
    def set_full_dimensions(self, path: Path) -> None:
        code, _, err = run(
            ["dimensions", str(path), "--width", "20", "--length", "72", "--epi", "12",
             "--ppi", "10", "--take-up", "10", "--loom-waste", "24", "--weft-waste", "8"]
        )
        self.assertEqual(code, 0, msg=err)

    def test_yardage_matches_the_stated_formulas(self) -> None:
        path = self.new()
        self.set_full_dimensions(path)
        code, out, err = run(["estimate", str(path)])
        self.assertEqual(code, 0, msg=err)
        project = loomlight.load_project(path)
        warp, weft = loomlight.estimate_yards(project)
        self.assertAlmostEqual(warp, (72 * 1.1 + 24) * 12 * 20 / 36)
        self.assertAlmostEqual(weft, 20 * 1.08 * 10 * 72 / 36)
        self.assertGreater(warp, 0)
        self.assertGreater(weft, 0)
        self.assertIn(f"warp  {warp:.2f} yd", out)
        self.assertIn(f"weft  {weft:.2f} yd", out)
        self.assertIn(f"total {warp + weft:.2f} yd", out)
        self.assertIn("x epi 12 x width 20 in / 36", out)
        self.assertEqual(err, "")

    def test_zero_dimensions_warn_with_the_exact_fix_and_still_exit_zero(self) -> None:
        path = self.new()
        code, out, err = run(["estimate", str(path)])
        self.assertEqual(code, 0)
        self.assertIn("warp  0.00 yd", out)
        self.assertIn("weft  0.00 yd", out)
        self.assertIn(
            f"warp yards need width_in: run loomlight dimensions {path} --width <inches>",
            err,
        )
        self.assertIn("--epi", err)
        self.assertIn("--ppi", err)

    def test_zero_width_only_warns_about_width(self) -> None:
        path = self.new()
        run(["dimensions", str(path), "--length", "72", "--epi", "12", "--ppi", "10"])
        code, out, err = run(["estimate", str(path)])
        self.assertEqual(code, 0)
        self.assertEqual(out.count("0.00 yd"), 3)
        self.assertIn("--width <inches>", err)
        self.assertNotIn("--epi", err)

    def test_json_mode_reports_the_same_numbers(self) -> None:
        path = self.new()
        self.set_full_dimensions(path)
        code, out, _ = run(["estimate", "--json", str(path)])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertAlmostEqual(
            payload["total_yards"], payload["warp_yards"] + payload["weft_yards"]
        )


class DimensionsTests(ProjectCase):
    def test_flags_persist_and_the_file_still_validates(self) -> None:
        path = self.new()
        code, out, err = run(
            ["dimensions", str(path), "--width", "20", "--length", "72", "--epi", "12",
             "--ppi", "10", "--take-up", "10", "--loom-waste", "24", "--weft-waste", "8"]
        )
        self.assertEqual(code, 0, msg=err)
        for expected in ("width", "length", "epi", "ppi", "take-up %",
                         "loom waste in", "weft waste %"):
            self.assertIn(expected, out)
        draft = self.read_project(path)
        self.assertEqual(draft["dimensions"]["width_in"], 20)
        self.assertEqual(draft["dimensions"]["weft_waste_pct"], 8)
        self.assertEqual(run(["validate", str(path)])[0], 0)

    def test_single_flag_only_touches_that_field(self) -> None:
        path = self.new()
        run(["dimensions", str(path), "--width", "20", "--length", "72"])
        run(["dimensions", str(path), "--epi", "12"])
        draft = self.read_project(path)
        self.assertEqual(draft["dimensions"]["width_in"], 20)
        self.assertEqual(draft["dimensions"]["length_in"], 72)
        self.assertEqual(draft["dimensions"]["epi"], 12)
        self.assertEqual(draft["dimensions"]["ppi"], 0)

    def test_negative_value_is_rejected_and_file_untouched(self) -> None:
        path = self.new()
        before = path.read_bytes()
        code, _, err = run(["dimensions", str(path), "--epi", "-8"])
        self.assertEqual(code, 2)
        self.assertEqual(err.strip(), "epi must be a number >= 0, got -8")
        self.assertEqual(path.read_bytes(), before)

    def test_non_numeric_value_is_rejected(self) -> None:
        code, _, err = run(["dimensions", str(self.new()), "--width", "wide"])
        self.assertEqual(code, 2)
        self.assertIn("width must be a number >= 0, got wide", err)

    def test_invalid_file_is_never_rewritten(self) -> None:
        draft = self.read_project(self.new())
        draft["tieup"] = draft["tieup"][:2]
        path = self.write_json("bad.json", draft)
        before = path.read_bytes()
        code, _, err = run(["dimensions", str(path), "--width", "20"])
        self.assertEqual(code, 2)
        self.assertIn("tieup", err)
        self.assertEqual(path.read_bytes(), before)

    def test_no_flags_prints_the_block_and_lists_the_flags(self) -> None:
        code, out, _ = run(["dimensions", str(self.new())])
        self.assertEqual(code, 0)
        self.assertIn("dimensions (inches)", out)
        for flag in ("--width", "--length", "--epi", "--ppi", "--take-up",
                     "--loom-waste", "--weft-waste"):
            self.assertIn(flag, out)


class ColorPlanTests(ProjectCase):
    def test_plan_is_saved_validated_and_rendered_as_bands(self) -> None:
        path = self.new()
        code, out, err = run(
            ["colors", str(path), "--warp", "red:2,blue:2", "--weft", "white:2"]
        )
        self.assertEqual(code, 0, msg=err)
        self.assertIn("warp stripes: red x2, blue x2", out)
        self.assertIn("weft picks: white x2", out)
        draft = self.read_project(path)
        self.assertEqual(
            draft["warp"]["stripes"],
            [{"color": "red", "count": 2}, {"color": "blue", "count": 2}],
        )
        self.assertEqual(run(["validate", str(path)])[0], 0)
        code, rendered, _ = run(["render", "--color", str(path)])
        self.assertEqual(code, 0)
        self.assertIn("r r b b", rendered)

    def test_count_mismatch_is_reported_and_file_unchanged(self) -> None:
        path = self.new()
        before = path.read_bytes()
        code, _, err = run(["colors", str(path), "--warp", "red:3,blue:2"])
        self.assertEqual(code, 2)
        self.assertIn("the stripe counts sum to 5 but the draft has 4 ends", err)
        self.assertEqual(path.read_bytes(), before)

    def test_unknown_colour_lists_the_palette(self) -> None:
        code, _, err = run(["colors", str(self.new()), "--warp", "teal:4"])
        self.assertEqual(code, 2)
        self.assertIn("'teal'", err)
        self.assertIn("16 palette names", err)

    def test_zero_count_is_rejected(self) -> None:
        code, _, err = run(["colors", str(self.new()), "--warp", "red:0"])
        self.assertEqual(code, 2)
        self.assertIn("1 or more", err)

    def test_list_colors_prints_the_sixteen_names(self) -> None:
        code, out, _ = run(["colors", "--list-colors"])
        self.assertEqual(code, 0)
        names = out.split()
        self.assertEqual(names, list(loomlight.PALETTE))
        self.assertEqual(len(names), 16)

    def test_solid_shortcuts_cover_the_whole_draft(self) -> None:
        path = self.new("--shafts", "4", "--treadles", "4", "--structure", "twill",
                        name="twill.json")
        code, out, err = run(
            ["colors", str(path), "--warp-solid", "green", "--weft-solid", "gray"]
        )
        self.assertEqual(code, 0, msg=err)
        draft = self.read_project(path)
        self.assertEqual(draft["warp"]["stripes"], [{"color": "green", "count": 4}])
        self.assertEqual(draft["weft"]["picks"], [{"color": "gray", "count": 4}])
        self.assertIn("g g g g", out)

    def test_empty_plan_preview_invites_the_user_to_set_one(self) -> None:
        draft = self.read_project(self.new())
        draft["warp"]["stripes"] = []
        draft["weft"]["picks"] = []
        path = self.write_json("plain.json", draft)
        code, out, _ = run(["colors", str(path)])
        self.assertEqual(code, 0)
        self.assertIn(
            "no warp stripes yet — pass --warp NAME:COUNT or --warp-solid NAME", out
        )
        self.assertIn("palette:", out)

    def test_colors_requires_a_file_unless_listing(self) -> None:
        code, _, err = run(["colors"])
        self.assertEqual(code, 2)
        self.assertIn("FILE is required", err)


class LockTests(ProjectCase):
    passphrase = "warp-weighted"

    def set_lock(self, path: Path) -> None:
        with mock.patch.dict(os.environ, {"LOOMLIGHT_PASSPHRASE": self.passphrase}):
            code, out, err = run(["lock", str(path), "--set"])
        self.assertEqual(code, 0, msg=err)
        self.assertIn("project locked", out)

    def test_set_writes_a_salted_hash_and_never_a_passphrase(self) -> None:
        path = self.new()
        self.set_lock(path)
        sidecar = Path(str(path) + ".lock")
        self.assertTrue(sidecar.exists())
        raw = sidecar.read_text(encoding="utf-8")
        self.assertNotIn(self.passphrase, raw)
        record = json.loads(raw)
        self.assertEqual(record["algo"], "pbkdf2_hmac_sha256")
        self.assertGreaterEqual(record["iterations"], 100_000)
        self.assertTrue(record["salt_b64"])
        self.assertTrue(record["hash_b64"])
        self.assertEqual(run(["lock", str(path)])[0], 0)

    def test_three_wrong_passphrases_exit_three_and_leave_the_file_alone(self) -> None:
        path = self.new()
        self.set_lock(path)
        before = path.read_bytes()
        with mock.patch("loomlight.read_passphrase", lambda prompt: "not-the-passphrase"):
            code, _, err = run(["dimensions", str(path), "--width", "20"])
        self.assertEqual(code, 3)
        self.assertEqual(err.count("passphrase not accepted"), 3)
        self.assertEqual(path.read_bytes(), before)

    def test_correct_passphrase_unlocks_the_write_commands(self) -> None:
        path = self.new()
        self.set_lock(path)
        with mock.patch("loomlight.read_passphrase", lambda prompt: self.passphrase):
            code, _, err = run(["dimensions", str(path), "--width", "20"])
        self.assertEqual(code, 0, msg=err)
        self.assertEqual(self.read_project(path)["dimensions"]["width_in"], 20)

    def test_edit_is_gated_too(self) -> None:
        path = self.new()
        self.set_lock(path)
        with mock.patch("loomlight.read_passphrase", lambda prompt: "wrong"):
            code, _, err = run(["edit", str(path)])
        self.assertEqual(code, 3)
        self.assertIn("passphrase not accepted", err)

    def test_read_only_commands_never_prompt(self) -> None:
        path = self.new()
        self.set_lock(path)
        with mock.patch("loomlight.read_passphrase", side_effect=AssertionError("prompted")):
            self.assertEqual(run(["render", str(path)])[0], 0)
            self.assertEqual(run(["estimate", str(path)])[0], 0)
            self.assertEqual(run(["validate", str(path)])[0], 0)

    def test_remove_requires_the_passphrase_then_deletes_the_sidecar(self) -> None:
        path = self.new()
        self.set_lock(path)
        with mock.patch("loomlight.read_passphrase", lambda prompt: "wrong"):
            code, _, _ = run(["lock", str(path), "--remove"])
        self.assertEqual(code, 3)
        self.assertTrue(Path(str(path) + ".lock").exists())

        with mock.patch("loomlight.read_passphrase", lambda prompt: self.passphrase):
            code, out, err = run(["lock", str(path), "--remove"])
        self.assertEqual(code, 0, msg=err)
        self.assertFalse(Path(str(path) + ".lock").exists())
        self.assertEqual(run(["dimensions", str(path), "--width", "20"])[0], 0)

    def test_unlocked_project_reports_the_command_to_lock_it(self) -> None:
        code, out, _ = run(["lock", str(self.new())])
        self.assertEqual(code, 0)
        self.assertIn("this project is not locked", out)
        self.assertIn("--set", out)

    def test_mismatched_new_passphrases_never_write_a_sidecar(self) -> None:
        path = self.new()
        answers = iter(["one", "two"])
        with mock.patch("loomlight.read_passphrase", lambda prompt: next(answers)):
            code, _, err = run(["lock", str(path), "--set"])
        self.assertEqual(code, 3)
        self.assertIn("passphrases did not match", err)
        self.assertFalse(Path(str(path) + ".lock").exists())

    def test_empty_passphrase_is_refused(self) -> None:
        with mock.patch("loomlight.read_passphrase", lambda prompt: ""):
            code, _, err = run(["lock", str(self.new()), "--set"])
        self.assertEqual(code, 2)
        self.assertIn("must not be empty", err)


class EditorTests(ProjectCase):
    """The editor is driven by a scripted key reader, so no test needs a TTY."""

    def open_editor(self, path: Path, keys: list[str], width: int = 200) -> list[str]:
        frames: list[str] = []
        remaining = list(keys)
        project = loomlight.load_project(path)
        code = loomlight.run_editor(
            path,
            project,
            read_key=lambda: remaining.pop(0) if remaining else "quit",
            write=frames.append,
            width=width,
        )
        self.assertEqual(code, 0)
        return frames

    def screens(self, frames: list[str]) -> list[str]:
        """Every drawn frame, in order (the exit messages are not screens)."""
        return [frame for frame in frames if "threading (" in frame]

    def status(self, frames: list[str]) -> str:
        drawn = self.screens(frames)
        self.assertTrue(drawn, msg="the editor never drew a frame")
        for line in reversed(drawn[-1].splitlines()):
            for token in ("threading:", "treadling:", "tie-up:"):
                if token in line:
                    return line[line.index(token):]
        self.fail("the drawn frame carried no status line")

    def grid_rows(self, screen: str, name: str) -> list[list[str]]:
        """Return the data rows of one grid as token lists."""
        lines = screen.splitlines()
        start = next(
            index for index, line in enumerate(lines) if f"{name} (" in line
        )
        rows: list[list[str]] = []
        for line in lines[start + 2:]:  # skip the title and the column indices
            if not line.strip() or line.strip().startswith(
                ("threading", "treadling", "tie-up")
            ):
                break
            rows.append(line.split())
        return rows

    def test_space_single_selects_a_shaft_and_clears_the_other_rows(self) -> None:
        path = self.new()
        frames = self.open_editor(path, ["right", "space", "save", "quit"])
        self.assertIn("end 2 -> shaft 1", self.status(frames))
        self.assertEqual(self.read_project(path)["threading"], [1, 1, 3, 4])
        # The end-2 column carries exactly one mark: on the shaft-1 row.
        rows = self.grid_rows(self.screens(frames)[-1], "threading")
        self.assertEqual([row[2] for row in rows], ["X", ".", ".", "."])

    def test_space_on_the_selected_cell_is_a_no_op(self) -> None:
        path = self.new()
        before = path.read_bytes()
        frames = self.open_editor(path, ["right", "space", "space", "quit"])
        self.assertIn("end 2 -> shaft 1", self.status(frames))
        drawn = self.screens(frames)
        # The second space changed nothing at all, so the frame is byte-identical.
        self.assertEqual(drawn[-1], drawn[-2])
        self.assertEqual(path.read_bytes(), before)

    def test_tieup_toggles_and_save_persists_it(self) -> None:
        path = self.new()
        self.open_editor(path, ["tab", "tab", "space", "save", "quit"])
        draft = self.read_project(path)
        self.assertFalse(draft["tieup"][0][0])
        code, out, _ = run(["render", str(path)])
        self.assertEqual(code, 0)
        tieup_rows = out.split("tie-up")[1].splitlines()[2:]
        self.assertEqual(tieup_rows[0].split()[1:], [".", "."])

    def test_tieup_status_reports_the_lift_state(self) -> None:
        path = self.new()
        frames = self.open_editor(path, ["tab", "tab", "quit"])
        self.assertIn("tie-up: shaft 1 x treadle 1 -> lift", self.status(frames))

    def test_tab_cycles_through_the_three_grids(self) -> None:
        path = self.new()
        frames = self.open_editor(path, ["tab", "tab", "tab", "quit"])
        self.assertIn("threading: end 1 -> shaft 1", self.status(frames))

    def test_quitting_while_dirty_asks_and_leaves_the_file_alone(self) -> None:
        path = self.new()
        before = path.read_bytes()
        frames = self.open_editor(path, ["right", "space", "quit", "no"])
        self.assertTrue(any("save before quitting? (y/n)" in frame for frame in frames))
        self.assertEqual(path.read_bytes(), before)

    def test_quitting_while_dirty_and_answering_yes_saves(self) -> None:
        path = self.new()
        self.open_editor(path, ["right", "space", "quit", "yes"])
        self.assertEqual(self.read_project(path)["threading"], [1, 1, 3, 4])

    def test_clean_draft_quits_without_asking(self) -> None:
        path = self.new()
        frames = self.open_editor(path, ["quit"])
        self.assertFalse(any("save before quitting" in frame for frame in frames))

    def test_status_line_carries_coordinates_value_and_keys(self) -> None:
        path = self.new()
        frames = self.open_editor(path, ["quit"])
        status = self.status(frames)
        self.assertIn("cell row 1 col 1 = X", status)
        for key in ("arrows/hjkl", "space", "Tab", "s save", "q quit"):
            self.assertIn(key, status)

    def test_header_names_the_file_structure_and_counts(self) -> None:
        path = self.new()
        frames = self.open_editor(path, ["quit"])
        header = self.screens(frames)[0].splitlines()[0]
        self.assertIn(str(path), header)
        self.assertIn("plain weave", header)
        self.assertIn("4 shafts, 4 ends, 2 treadles, 2 picks", header)

    def test_dirty_draft_is_flagged_in_the_header(self) -> None:
        path = self.new()
        frames = self.open_editor(path, ["right", "space", "save", "quit"])
        self.assertTrue(any("[unsaved changes]" in frame for frame in frames))
        # After the save the flag is gone again.
        self.assertNotIn("[unsaved changes]", self.screens(frames)[-1].splitlines()[0])

    def test_narrow_terminal_invites_widening_instead_of_clipping(self) -> None:
        path = self.new()
        frames: list[str] = []
        project = loomlight.load_project(path)
        with self.assertRaises(loomlight.LoomlightError) as caught:
            loomlight.run_editor(
                path, project, read_key=lambda: "quit", write=frames.append, width=2
            )
        self.assertIn("widen the window", caught.exception.message)
        self.assertEqual(frames, [])

    def test_edit_refuses_a_non_tty_stdin_with_a_pointer_to_render(self) -> None:
        code, _, err = run(["edit", str(self.new())])
        self.assertEqual(code, 2)
        self.assertIn("not a TTY", err)
        self.assertIn("render", err)


class HelpTests(ProjectCase):
    def test_top_level_help_lists_every_command_on_short_lines(self) -> None:
        code, out, _ = run(["--help"])
        self.assertEqual(code, 0)
        for command in loomlight.COMMANDS:
            self.assertIn(command, out)
        for line in out.splitlines():
            self.assertLessEqual(len(line), 100, msg=line)

    def test_every_command_has_its_own_help(self) -> None:
        for command in loomlight.COMMANDS:
            with self.subTest(command=command):
                code, out, _ = run([command, "--help"])
                self.assertEqual(code, 0)
                self.assertIn("usage:", out)
                for line in out.splitlines():
                    self.assertLessEqual(len(line), 100, msg=line)

    def test_flag_help_describes_the_contract(self) -> None:
        _, render_help, _ = run(["render", "--help"])
        self.assertIn("FILE", render_help)
        self.assertIn("--color", render_help)
        _, new_help, _ = run(["new", "--help"])
        for flag in ("--out", "--shafts", "--treadles", "--structure", "--force"):
            self.assertIn(flag, new_help)
        _, colors_help, _ = run(["colors", "--help"])
        for flag in ("--warp", "--weft", "--warp-solid", "--weft-solid", "--list-colors"):
            self.assertIn(flag, colors_help)
        _, dimensions_help, _ = run(["dimensions", "--help"])
        for flag in ("--width", "--length", "--epi", "--ppi", "--take-up",
                     "--loom-waste", "--weft-waste"):
            self.assertIn(flag, dimensions_help)
        _, lock_help, _ = run(["lock", "--help"])
        self.assertIn("--set", lock_help)
        self.assertIn("--remove", lock_help)
        _, estimate_help, _ = run(["estimate", "--help"])
        self.assertIn("FILE", estimate_help)
        _, validate_help, _ = run(["validate", "--help"])
        self.assertIn("FILE", validate_help)

    def test_unknown_command_names_the_valid_ones(self) -> None:
        code, _, err = run(["frobnicate"])
        self.assertEqual(code, 2)
        self.assertIn("frobnicate", err)
        for command in loomlight.COMMANDS:
            self.assertIn(command, err)

    def test_bare_invocation_prints_usage_and_exits_zero(self) -> None:
        code, out, _ = run([])
        self.assertEqual(code, 0)
        self.assertIn("usage:", out)

    def test_version_flag(self) -> None:
        code, out, _ = run(["--version"])
        self.assertEqual(code, 0)
        self.assertIn(loomlight.VERSION, out)


if __name__ == "__main__":
    unittest.main()
