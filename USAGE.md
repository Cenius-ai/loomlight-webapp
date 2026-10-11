# Using Loomlight

Everything below runs offline and non-interactively except `loomlight edit`,
which needs a terminal.

## 1. Draft a plain-weave scarf

```sh
python3 loomlight.py new --out scarf.json --shafts 4 --treadles 2 --structure plain
python3 loomlight.py render scarf.json
```

`new` writes a straight-draw threading (`1 2 3 4`), a treadling of `1 2`, a
tie-up where treadle 1 lifts the odd shafts and treadle 2 the even ones, a
zeroed `dimensions` block and a one-stripe warp and one-pick weft plan. The file
validates and renders the moment it is written.

```
scarf.json — plain weave — 4 shafts, 4 ends, 2 treadles, 2 picks

threading (one row per shaft, one column per end)
  1 2 3 4
1 X . . .
2 . X . .
3 . . X .
4 . . . X
```

Useful details:

* `--out` defaults to `draft.json` and refuses to overwrite an existing file:
  it exits 2 and leaves the file byte-identical unless you pass `--force`.
* `--shafts` and `--treadles` accept 2..32. Plain weave needs an even shaft
  count (it writes the 1/3 and 2/4 tie-up) and uses exactly 2 treadles; twill
  needs shafts equal to treadles and writes the diagonal tie-up.
* A rejected seed writes nothing at all.

```sh
python3 loomlight.py new --out triangle.json --shafts 5 --structure plain
# plain weave needs an even shaft count        (exit 2, no file written)

python3 loomlight.py new --out twill8.json --shafts 8 --treadles 8 --structure twill
python3 loomlight.py new --out broken.json --shafts 8 --treadles 4 --structure twill
# twill weave needs shafts and treadles to match, got 8 shafts and 4 treadles
```

## 2. Edit the draft in the terminal

```sh
python3 loomlight.py edit scarf.json
```

The editor draws all three grids — threading, treadling and tie-up — under a
header naming the file, the structure and the counts.

| Key | Effect |
| --- | --- |
| arrow keys or `h` `j` `k` `l` | Move the cursor inside the active grid |
| `Tab` | Switch to the next grid |
| `space` on a threading cell | Select that shaft for that end, clearing the other rows in that column |
| `space` on an already-selected threading or treadling cell | Nothing (the file stores one shaft per end) |
| `space` on a treadling cell | Select that treadle for that pick |
| `space` on a tie-up cell | Toggle that shaft lift for that treadle |
| `s` | Save (re-validates, then writes atomically) |
| `q` | Quit; if the draft is dirty it asks `save before quitting? (y/n)` first |

The status line under the grids shows where the cursor is, what the cell holds
and the key legend:

```
threading: end 2 -> shaft 1 | cell row 1 col 2 = X | keys: arrows/hjkl move, space set, Tab grid, s save, q quit
```

If the window is too narrow to draw the grids honestly, the editor says so and
exits instead of printing a clipped draft.

## 3. Measure the project

```sh
python3 loomlight.py dimensions scarf.json --width 8.5 --length 68 --epi 10 --ppi 8 \
  --take-up 10 --loom-waste 24 --weft-waste 6
```

Flags may be combined or used one at a time; only the fields you pass are
touched. Run it with no flags to see the current block and the list of flags.
Every value must be a finite number of 0 or more:

```
$ python3 loomlight.py dimensions scarf.json --epi -8
epi must be a number >= 0, got -8        (exit 2, file unchanged)
```

The file is validated before and after the edit, so an invalid project is never
rewritten.

## 4. Estimate the yarn

```sh
python3 loomlight.py estimate scarf.json
```

```
scarf.json — plain weave
repeat        4 ends, 2 picks (from the threading and treadling)
width         8.5 in

warp  233.28 yd = (length 68 in x (1 + take-up 10%) + loom waste 24 in) x epi 10 x width 8.5 in / 36
weft  136.15 yd = width 8.5 in x (1 + weft waste 6%) x ppi 8 x length 68 in / 36
total 369.43 yd
```

The epi and ppi come from the `dimensions` block, so the figures follow the
draft you drew. Any input still at zero prints as `0.00 yd` with a warning
naming the exact command that fills it in, and the command still exits 0:

```
$ python3 loomlight.py estimate fresh.json
warp yards need width_in: run loomlight dimensions fresh.json --width <inches>
weft yards need width_in: run loomlight dimensions fresh.json --width <inches>
...
```

`--json` prints the same numbers for scripts:

```sh
python3 loomlight.py estimate --json scarf.json
```

## 5. Plan the warp and weft colours

```sh
python3 loomlight.py colors --list-colors                    # the 16 names, one per line
python3 loomlight.py colors scarf.json --warp blue:2,white:2 --weft white:2
python3 loomlight.py colors scarf.json --warp-solid green --weft-solid gray
python3 loomlight.py colors scarf.json                       # preview only, changes nothing
```

Counts are integers of 1 or more, and a non-empty plan must sum to the drafting
length: ends for the warp, picks for the weft.

```
$ python3 loomlight.py colors scarf.json --warp red:3,blue:2
scarf.json: warp: the stripe counts sum to 5 but the draft has 4 ends    (exit 2)

$ python3 loomlight.py colors scarf.json --warp teal:4
warp: 'teal' is not one of the 16 palette names (black, red, ...)        (exit 2)
```

A failed plan leaves the draft byte-identical.

The planner previews the draft with the warp band across the top, the weft band
beside the treadling grid and the plans listed underneath, in the palette order:

```
threading (one row per shaft, one column per end)
warp r r b b
     1 2 3 4
   1 X . . .
...
colour key: r red, b blue, w white

warp stripes: red x2, blue x2 (4 warp slots)
weft picks: white x2 (2 weft slots)
```

On a terminal those cells are ANSI background swatches; in a pipe or with
`NO_COLOR` set they degrade to one letter per colour (the table in the README),
so redirected output stays readable.

## 6. Render the draft to a file

```sh
python3 loomlight.py render --color scarf.json > scarf.txt
python3 loomlight.py render --color --no-color scarf.json | less
```

`render` writes the threading grid (shafts down, ends across), the treadling
grid (treadles down, picks across) and the tie-up grid (shafts by treadles),
each with row and column indices and a header naming the file, the structure and
the counts. Output is deterministic — the same project file gives the same bytes
— so renders can be diffed.

Grids adapt to the terminal: a window too narrow for the full layout drops to a
compact index-free form, and a window too narrow even for that asks you to widen
it or redirect to a file rather than printing a clipped grid. Redirected output
is never clipped.

## 7. Check a file by hand

```sh
python3 loomlight.py validate scarf.json            # ok
python3 loomlight.py validate --json scarf.json     # {"file": ..., "ok": true}
```

`validate` runs the same single validator every other command uses, and reports
only the first bad field as one plain-English line naming the field, the value
seen and the accepted range:

```
scarf.json: tieup: expected 4 rows of 2 booleans, found 3 rows
scarf.json: warp: the stripe counts sum to 5 but the draft has 4 ends
scarf.json: dimensions.epi: expected a number >= 0, found -8
```

Exits 2 and never touches the file.

## 8. Lock a draft

```sh
python3 loomlight.py lock scarf.json --set        # asks twice, hidden input
python3 loomlight.py lock scarf.json              # project locked — scarf.json.lock
python3 loomlight.py lock scarf.json --remove     # asks for the current passphrase first
```

`--set` writes a sidecar holding only the algorithm, the iteration count, a
random salt and a PBKDF2-HMAC-SHA256 hash — never a passphrase. From then on
`edit`, `colors` and `dimensions` ask for the passphrase, refuse with one
non-enumerating `passphrase not accepted` line per attempt and exit 3 after
three tries, leaving the draft untouched. `render`, `estimate` and `validate`
never prompt.

For scripts and CI, pass the passphrase through the environment instead of a
prompt:

```sh
LOOMLIGHT_PASSPHRASE=loomlight-demo python3 loomlight.py dimensions scarf.json --width 9
LOOMLIGHT_PASSPHRASE=loomlight-demo python3 loomlight.py lock scarf.json --remove
```

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success (including `estimate` warnings for unset measurements) |
| 2 | Validation or usage error |
| 3 | Passphrase refused |

Read the warnings on stderr and the results on stdout:

```sh
python3 loomlight.py render draft.json > draft.txt      # grids to the file
python3 loomlight.py estimate draft.json | tail -1      # just the total
```
