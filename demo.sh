#!/usr/bin/env bash
# Loomlight end-to-end demo: creates drafts, plans colours, renders them, edits
# the measurements, estimates the yardage and exercises the passphrase lock.
# Nothing here prompts -- every passphrase comes from LOOMLIGHT_PASSPHRASE -- so
# the whole run is safe to record unattended.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
LOOMLIGHT="$PY loomlight.py"
PASSPHRASE="loomlight-demo"

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  BOLD="$(printf '\033[1m')"
  DIM="$(printf '\033[2m')"
  OFF="$(printf '\033[0m')"
else
  BOLD=""
  DIM=""
  OFF=""
fi

STEP=0
step() {
  STEP=$((STEP + 1))
  printf '\n%s== %d. %s%s\n' "$BOLD" "$STEP" "$1" "$OFF"
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
DRAFT="$WORK/draft.json"
TOWEL="$WORK/towel.json"
BROKEN="$WORK/broken.json"

run() {
  printf '%s$ %s%s\n' "$DIM" "$*" "$OFF"
  "$@"
}

step "version and the landing help"
run $LOOMLIGHT --version
run $LOOMLIGHT --help

step "create a 4-shaft plain-weave draft (--out refuses to clobber)"
run $LOOMLIGHT new --out "$DRAFT" --shafts 4 --treadles 2 --structure plain
run $LOOMLIGHT validate "$DRAFT"

step "render the three grids (pipeable: this is plain text on stdout)"
run $LOOMLIGHT render "$DRAFT"

step "set the draft measurements the estimator reads"
run $LOOMLIGHT dimensions "$DRAFT" --width 20 --length 72 --epi 12 --ppi 10 \
  --take-up 10 --loom-waste 24 --weft-waste 8

step "estimate the warp, weft and total yardage"
run $LOOMLIGHT estimate "$DRAFT"

step "the 16-name colour palette"
run $LOOMLIGHT colors --list-colors

step "plan the warp and weft colours on a 4-shaft twill"
run $LOOMLIGHT new --out "$TOWEL" --shafts 4 --treadles 4 --structure twill
run $LOOMLIGHT colors "$TOWEL" --warp red:2,blue:2 --weft white:4

step "colour bands beside the draft (letters, because this run is piped)"
run $LOOMLIGHT render --color "$TOWEL"

step "shortcuts for a single stripe and a single pick"
run $LOOMLIGHT colors "$TOWEL" --warp-solid green --weft-solid gray
run $LOOMLIGHT render --color "$TOWEL"

step "a wider draft: 8 shafts, 8 treadles, twill"
run $LOOMLIGHT new --out "$WORK/twill8.json" --shafts 8 --treadles 8 --structure twill
run $LOOMLIGHT render "$WORK/twill8.json"

step "lock the draft with a local PBKDF2 passphrase (sidecar, never plaintext)"
printf '%s$ LOOMLIGHT_PASSPHRASE=... %s lock %s --set%s\n' "$DIM" "$LOOMLIGHT" "$DRAFT" "$OFF"
LOOMLIGHT_PASSPHRASE="$PASSPHRASE" $LOOMLIGHT lock "$DRAFT" --set
printf '%s$ cat %s.lock%s\n' "$DIM" "$DRAFT" "$OFF"
cat "$DRAFT.lock"
if grep -q "$PASSPHRASE" "$DRAFT.lock"; then
  echo "the passphrase leaked into the sidecar -- that would be a bug" >&2
  exit 1
fi
echo "no passphrase in the sidecar: only the algorithm, iteration count, salt and hash."

step "the wrong passphrase is refused and the draft is untouched"
before="$(cksum < "$DRAFT")"
set +e
printf '%s$ LOOMLIGHT_PASSPHRASE=wrong %s dimensions %s --width 3%s\n' \
  "$DIM" "$LOOMLIGHT" "$DRAFT" "$OFF"
LOOMLIGHT_PASSPHRASE=wrong $LOOMLIGHT dimensions "$DRAFT" --width 3
wrong_status=$?
set -e
echo "exit $wrong_status (expected 3)"
if [ "$before" != "$(cksum < "$DRAFT")" ]; then
  echo "the draft changed after a refused passphrase -- that would be a bug" >&2
  exit 1
fi

step "the right passphrase unlocks the write commands"
printf '%s$ LOOMLIGHT_PASSPHRASE=... %s dimensions %s --width 21%s\n' \
  "$DIM" "$LOOMLIGHT" "$DRAFT" "$OFF"
LOOMLIGHT_PASSPHRASE="$PASSPHRASE" $LOOMLIGHT dimensions "$DRAFT" --width 21

step "read-only commands never prompt while the draft stays locked"
run $LOOMLIGHT estimate "$DRAFT"

step "the lock is removed again"
printf '%s$ LOOMLIGHT_PASSPHRASE=... %s lock %s --remove%s\n' \
  "$DIM" "$LOOMLIGHT" "$DRAFT" "$OFF"
LOOMLIGHT_PASSPHRASE="$PASSPHRASE" $LOOMLIGHT lock "$DRAFT" --remove
run $LOOMLIGHT lock "$DRAFT"

step "a bad file reports one plain-English line and exits 2"
cp "$TOWEL" "$BROKEN"
"$PY" -c "import json,sys; path=sys.argv[1]; p=json.loads(open(path, encoding='utf-8').read()); p['tieup']=p['tieup'][:2]; open(path,'w',encoding='utf-8').write(json.dumps(p, indent=2))" "$BROKEN"
set +e
run $LOOMLIGHT validate "$BROKEN"
bad_status=$?
run $LOOMLIGHT render "$BROKEN"
set -e
echo "exit $bad_status (expected 2)"

step "an odd shaft count cannot seed a plain weave"
set +e
run $LOOMLIGHT new --out "$WORK/odd.json" --shafts 5 --structure plain
odd_status=$?
set -e
echo "exit $odd_status (expected 2)"

step "the editor needs a real terminal and says so instead of hanging"
set +e
run $LOOMLIGHT edit "$TOWEL"
edit_status=$?
set -e
echo "exit $edit_status (expected 2 outside a terminal)"

step "the shipped sample drafts"
run $LOOMLIGHT estimate examples/plain-scarf.json
run $LOOMLIGHT render --color examples/twill-towel.json

printf '\n%sDemo complete.%s Drafts were written to a temporary directory and removed.\n' \
  "$BOLD" "$OFF"
