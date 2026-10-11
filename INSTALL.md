# Installing Loomlight

Loomlight is a single Python file with no third-party dependencies.

## Prerequisites

Install these yourself before running the setup script (the script never
installs system packages and never uses `sudo`):

| Prerequisite | Why | How |
| --- | --- | --- |
| Python 3.9 or newer (3.11 recommended) | Runs the tool | Your distribution's `python3`; check with `python3 -V` |
| A POSIX terminal | Only `loomlight edit` needs one | Any terminal emulator or SSH session |

Nothing else is required. There is no database, no service, no compiler and no
network access at any point.

## Package manager

**pip**, driven by the committed `requirements.txt` at the project root. That
file lists zero third-party packages on purpose — the whole tool is standard
library — so `pip install -r requirements.txt` is a fast no-op. There is no
second package manager to choose between, and `pyproject.toml` exists only to
describe the project and its optional console command.

## Step 1 — run the setup script

```sh
bash install.sh
```

It checks the interpreter version, runs pip against `requirements.txt`,
installs the optional `loomlight` console command (skipped with a note if the
environment forbids it), byte-compiles `loomlight.py`, imports the module and
runs the regression suite. It is idempotent and it **exits** — it never starts a
server or a background process.

If you would rather not run a script, the equivalent manual steps are:

```sh
python3 -V                                  # expect 3.9+
python3 -m pip install -r requirements.txt  # installs nothing: no dependencies
python3 -c "import loomlight"               # sanity check
```

## Step 2 — create your first project (the seed step)

Loomlight's data is one JSON project file per draft, so the seed step is simply
making one — the file is written already valid and already seed-shaped:

```sh
python3 loomlight.py new --out draft.json --shafts 4 --treadles 2 --structure plain
python3 loomlight.py validate draft.json   # prints: ok
```

Two projects are shipped ready to use in `examples/`:

```sh
python3 loomlight.py render examples/plain-scarf.json
python3 loomlight.py estimate examples/twill-towel.json
```

## Step 3 — run it

```sh
python3 loomlight.py --help
python3 loomlight.py render --color examples/twill-towel.json
python3 loomlight.py estimate examples/twill-towel.json
```

If the console command was installed in step 1, the same commands work as
`loomlight render ...`, `loomlight estimate ...`, and so on.

To watch the whole tool work without typing anything:

```sh
bash demo.sh
```

## Step 4 — run the tests

```sh
python3 -m unittest discover -s tests     # the project's own command
python3 -m pytest tests                   # equivalent, if pytest is available
```

Both must exit 0; the suite has 67 tests and no network or TTY requirement.

## Environment variables

Both are optional; the tool works with none of them set. These are ordinary
process environment variables — Loomlight does not read `.env` files, so export
them in your shell (or pass them inline as shown).

| Variable | Default | Meaning |
| --- | --- | --- |
| `LOOMLIGHT_PASSPHRASE` | unset | Non-interactive passphrase for the locked write commands (`lock`, `edit`, `colors`, `dimensions`). When unset, Loomlight prompts on a hidden terminal prompt, and refuses (exit 3) rather than hanging if there is no terminal. |
| `NO_COLOR` | unset | Set to any non-empty value to force the plain-letter colour fallback instead of ANSI swatches. Output is already letter-based whenever stdout is not a TTY. |

`.env.example` lists both with empty values.

## Uninstalling

```sh
rm -f "$(command -v loomlight)"     # only if you installed the console command
```

Your project files stay where you put them; a lock sidecar next to a draft is
`FILE.lock` and is removed by `python3 loomlight.py lock FILE --remove`.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `python3: command not found` | Install Python 3.9+ and re-run `bash install.sh`. |
| `Loomlight needs Python 3.9 or newer` | Upgrade Python; the tool uses no 3.9+ only syntax but pins the floor deliberately. |
| `edit needs an interactive terminal (stdin is not a TTY)` | `loomlight edit` needs a real terminal. Use `loomlight render FILE > draft.txt` or `loomlight render --color FILE` in scripts and pipes. |
| `passphrase not accepted` | The passphrase does not match the `FILE.lock` sidecar. Three wrong attempts exit 3 and leave the draft untouched. |
| `passphrase required: run in a terminal or set LOOMLIGHT_PASSPHRASE` | The project is locked and stdin is not a terminal — set `LOOMLIGHT_PASSPHRASE`. |
| A colour band prints `k r g ...` instead of colours | stdout is not a TTY, or `NO_COLOR` is set. That is the intended readable fallback. |
