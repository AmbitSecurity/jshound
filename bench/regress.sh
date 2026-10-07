#!/usr/bin/env bash
# regress.sh — run `analyze` over a set of saved corpora and tag the output, so two versions of the
# extractor can be diffed record-by-record instead of compared on totals.
#
#     bench/regress.sh before          # on the committed tree
#     ...make a change...
#     bench/regress.sh after
#     python3 bench/regress_diff.py before after
#
# WHY THIS EXISTS. A change to the extractor is not measurable by a count: tonight's sentinel fix
# removed 27 fabricated templates and ADDED 29 real ones, so the total went up by two while the output
# changed in 56 places. Four separate changes were reverted this session because a per-record diff
# showed them making real output worse while the totals looked flat or better — a relative base URL
# that glued Grafana's k8s base onto its legacy REST API (886 -> 887, so a count check would have
# called it a win), and two precheck removals that looked like wins on a 60-file subset and were
# net-negative on the full corpus.
#
# It lives in the repo because it kept being written into /tmp and lost — most recently to a reboot,
# which took ~100 saved corpora and every baseline with it.
#
# CORPORA. This expects directories of saved bodies, one per target, as `jshound fetch` writes them:
#
#     jshound fetch https://play.grafana.org/ -o "$CORPORA/t_grafana" --max-chunks 150
#
# Point CORPORA at wherever you keep them. They are deliberately not in the repo: they are megabytes
# of someone else's JavaScript, they go stale as targets ship, and the numbers in SUPPORT.md are dated
# for that reason.
set -u

CORPORA="${CORPORA:-$HOME/.cache/jshound-corpora}"
OUT="${OUT:-$HOME/.cache/jshound-regress}"
TAG="${1:-}"

if [ -z "$TAG" ]; then
  echo "usage: CORPORA=<dir> bench/regress.sh <tag>" >&2
  exit 2
fi
if [ ! -d "$CORPORA" ]; then
  echo "regress.sh: no corpora at $CORPORA — set CORPORA, or fetch some first:" >&2
  echo "  jshound fetch https://play.grafana.org/ -o $CORPORA/t_grafana --max-chunks 150" >&2
  exit 2
fi

mkdir -p "$OUT"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
n=0
for d in "$CORPORA"/*/; do
  [ -d "$d" ] || continue
  name="$(basename "$d")"
  # A corpus with nothing to read is not a failure, it is an empty corpus; keep going either way.
  if timeout 900 python3 -m jshound.cli analyze "$d" -o "$OUT/${TAG}_${name}.json" >/dev/null 2>&1
  then n=$((n+1)); printf '.'; else printf '!'; fi
done
printf '\n%s corpora -> %s/%s_*.json\n' "$n" "$OUT" "$TAG"
