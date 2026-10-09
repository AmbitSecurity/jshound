#!/usr/bin/env python3
"""snapshot.py: record what this tool extracts from a set of corpora, so a change has to prove it.

    python bench/snapshot.py /tmp/corpus_a /tmp/corpus_b -o before.json
    ...make a change...
    python bench/snapshot.py /tmp/corpus_a /tmp/corpus_b -o after.json
    python bench/snapshot.py --diff before.json after.json

A fix that raises one number while quietly lowering another is the normal failure mode here: the
resolver change that took GitLab from 7 endpoints to 134 could just as easily have cost Grafana its
chunk enumeration, and a spot-check of the target you were working on would not have noticed. So
every capability is recorded against every corpus, and the diff reports BOTH directions, a
regression is any number that went down, and it is printed first.

The corpora are yours; nothing is committed. Point it at whatever you have.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import jshound   # noqa: E402
from jshound.export.paths import looks_like_source_file, normalize   # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from classify import tally   # noqa: E402


def measure(corpus):
    name = os.path.basename(corpus.rstrip("/"))
    out = {"corpus": name}
    try:
        recs = jshound.analyze(corpus)
    except Exception as e:                                   # noqa: BLE001
        return {"corpus": name, "error": f"{type(e).__name__}: {e}"}

    eps = [r for r in recs if (r.get("_kind") or "endpoint") == "endpoint"]
    paths = {normalize(r.get("template") or r.get("url") or "")[0] for r in eps}
    paths = {p for p in paths if p and not looks_like_source_file(p)}
    t = tally(paths)
    out.update({
        # The least stable number here, and the one to read last: it counts every record kind
        # including internal ones (callargs, client-encoding, config-value), so a change to how an
        # intermediate is emitted moves it without changing a single endpoint. Judge a run by
        # endpoints / with_verb / with_body / chunks, which are the claims.
        "records": len(recs),
        "endpoints": len(eps),
        "with_verb": sum(1 for r in eps if r.get("method") and r.get("method") != "UNKNOWN"),
        "with_body": sum(1 for r in eps
                         if r.get("body_keys") or isinstance(r.get("body_schema"), dict)),
        "paths": len(paths), "api": t["api"], "noise": t["fragment"],
    })

    chunks, parse_fail = set(), 0
    for f in sorted(os.listdir(corpus)):
        if not f.endswith((".js", ".mjs", ".cjs")):
            continue
        try:
            res = jshound.chunk_map(open(os.path.join(corpus, f), errors="replace").read())
        except Exception:                                    # noqa: BLE001
            parse_fail += 1
            continue
        # Tolerate the pre-stats output format (a bare array), so this harness can measure an older
        # build as a baseline instead of crashing on it, which is exactly what it did the first
        # time it was pointed at one.
        if isinstance(res, list):
            res = {"chunks": res}
        chunks.update(res.get("chunks") or [])
    out["chunks"] = len(chunks)
    out["chunk_scan_failures"] = parse_fail

    try:
        out["gql_operations"] = len(jshound.graphql_operations(corpus))
    except Exception:                                        # noqa: BLE001
        out["gql_operations"] = None
    return out


# Lower is worse for everything except these, where lower is better.
_LOWER_IS_BETTER = {"noise", "chunk_scan_failures"}


def diff(before, after):
    b = {r["corpus"]: r for r in before}
    a = {r["corpus"]: r for r in after}
    regressions, improvements = [], []
    for name in sorted(set(b) | set(a)):
        rb, ra = b.get(name, {}), a.get(name, {})
        if "error" in ra and "error" not in rb:
            regressions.append(f"{name}: now ERRORS, {ra['error']}")
            continue
        for k in sorted(set(rb) | set(ra)):
            if k in ("corpus", "error"):
                continue
            x, y = rb.get(k), ra.get(k)
            if not isinstance(x, int) or not isinstance(y, int) or x == y:
                continue
            worse = (y > x) if k in _LOWER_IS_BETTER else (y < x)
            (regressions if worse else improvements).append(
                f"{name}.{k}: {x} -> {y} ({y - x:+d})")
    return regressions, improvements


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("corpora", nargs="*")
    ap.add_argument("-o", "--out")
    ap.add_argument("--diff", nargs=2, metavar=("BEFORE", "AFTER"))
    a = ap.parse_args()

    if a.diff:
        regressions, improvements = diff(json.load(open(a.diff[0])), json.load(open(a.diff[1])))
        if regressions:
            print(f"REGRESSIONS ({len(regressions)}):")
            for r in regressions:
                print(f"  {r}")
        else:
            print("REGRESSIONS: none")
        print(f"\nimprovements ({len(improvements)}):")
        for i in improvements:
            print(f"  {i}")
        return 1 if regressions else 0

    rows = []
    for c in a.corpora:
        if not os.path.isdir(c):
            continue
        r = measure(c)
        rows.append(r)
        print(f"  {r['corpus']}: {json.dumps({k: v for k, v in r.items() if k != 'corpus'})}",
              file=sys.stderr)
    payload = json.dumps(rows, indent=1)
    if a.out:
        open(a.out, "w").write(payload)
        print(f"-> {a.out}", file=sys.stderr)
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
