#!/usr/bin/env bash
# regress.sh: run `analyze` over a set of saved corpora and tag the output, so two versions of the
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
# showed them making real output worse while the totals looked flat or better, a relative base URL
# that glued Grafana's k8s base onto its legacy REST API (886 -> 887, so a count check would have
# called it a win), and two precheck removals that looked like wins on a 60-file subset and were
# net-negative on the full corpus.
#
# It lives in the repo because it kept being written into /tmp and lost, most recently to a reboot,
# which took ~100 saved corpora and every baseline with it.
#
# CORPORA. This expects one directory per target, as `jshound <target> -o DIR` leaves them:
#
#     jshound https://play.grafana.org/ -o "$CORPORA/t_grafana" --max-chunks 150 -q
#
# Point CORPORA at wherever you keep them. They are deliberately not in the repo: they are megabytes
# of someone else's JavaScript, they go stale as targets ship, and the README's numbers carry the date
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
  echo "regress.sh: no corpora at $CORPORA, set CORPORA, or fetch some first:" >&2
  echo "  jshound https://play.grafana.org/ -o $CORPORA/t_grafana --max-chunks 150 -q" >&2
  exit 2
fi

mkdir -p "$OUT"
# Which checkout's code to run. Default: this one. Point it at a `git worktree` of an older commit to
# produce the "before" run without touching your working copy.
REPO="${JSHOUND_REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
n=0
for d in "$CORPORA"/*/; do
  [ -d "$d" ] || continue
  name="$(basename "$d")"
  # A corpus with nothing to read is not a failure, it is an empty corpus; keep going either way.
  # The fetched files are in `js/` under a results folder; an older corpus is the folder itself.
  src="$d"; [ -d "$d/js" ] && src="$d/js"
  # Two files per corpus: every record the library returns, because a change in the working-out the
  # requests are built from is still a change, and the requests exactly as the command writes them,
  # GraphQL operations included, because that is what a user reads and replays.
  if (cd "$REPO" && PYTHONPATH="$REPO" timeout 1800 python3 - "$src" "$OUT/${TAG}_${name}" <<'PY'
import contextlib, io, json, sys
import jshound
from jshound.export.requests import to_lines
src, base = sys.argv[1], sys.argv[2]
with contextlib.redirect_stderr(io.StringIO()):
    recs = jshound.analyze(src)
    try:
        from jshound.cli import _graphql_operations
        recs = recs + _graphql_operations(src)
    except ImportError:
        pass
json.dump(recs, open(base + ".json", "w"))
with open(base + ".requests.jsonl", "w") as fh:
    for line in to_lines(recs):
        line.pop("declared_in", None)          # a file name, which says nothing about the request
        fh.write(json.dumps(line, sort_keys=True) + "\n")
PY
  ) >/dev/null 2>&1
  then n=$((n+1)); printf '.'; else printf '!'; fi
done
printf '\n%s corpora -> %s/%s_*.json and .requests.jsonl\n' "$n" "$OUT" "$TAG"
