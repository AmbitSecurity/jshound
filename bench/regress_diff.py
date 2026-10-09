#!/usr/bin/env python3
"""regress_diff.py: diff two tagged `bench/regress.sh` runs record by record.

    python3 bench/regress_diff.py before after

Prints per-corpus endpoint counts with what was LOST and GAINED, then lists the actual templates. The
listing is the point: a total tells you nothing about whether a change was an improvement.

Measured examples from one session, each of which a count-only check would have read wrong:

    886 -> 888   27 fabricated templates removed, 29 real ones recovered in their place (good)
    886 -> 887   4 correct records displaced by 5 wrong ones (reverted)
   1736 -> 1737  1 real endpoint gained, 1 parameter name degraded `{param:f}` -> `{unary}` (reverted)

So read `lost` first, and read the names rather than the numbers.
"""
import collections
import glob
import json
import os
import sys


def load(p):
    try:
        with open(p) as fh:
            return json.load(fh)
    except Exception:                                      # noqa: BLE001
        return []


def endpoints(rs):
    """Only records that CLAIM an address. Evidence kinds are excluded, because a change in how many
    path literals were noticed is not a change in the reported surface."""
    return {(str(r.get("method") or ""), str(r.get("template") or r.get("url") or ""))
            for r in rs
            if (r.get("_kind") or "endpoint") == "endpoint" and (r.get("template") or r.get("url"))}


def kinds(rs):
    return collections.Counter((r.get("_kind") or "endpoint") for r in rs)


def _requests(path):
    """{(method, url): [line, ...]} from a .requests.jsonl."""
    out = collections.defaultdict(list)
    try:
        for ln in open(path):
            d = json.loads(ln)
            out[(d.get("method"), d.get("url"))].append(d)
    except OSError:
        pass
    return out


def _field(lines, f):
    return sorted(json.dumps(x.get(f), sort_keys=True) for x in lines)


def request_diff(a_tag, b_tag, names, out):
    """The output a user reads: requests lost and gained, and what changed in the ones on both sides."""
    rows = []
    for n in names:
        pa, pb = (os.path.join(out, f"{t}_{n}.requests.jsonl") for t in (a_tag, b_tag))
        if not (os.path.exists(pa) and os.path.exists(pb)):
            continue
        A, B = _requests(pa), _requests(pb)
        both = set(A) & set(B)
        rows.append((n, A, B, sorted(set(A) - set(B), key=str), sorted(set(B) - set(A), key=str),
                     {f: [k for k in sorted(both, key=str) if _field(A[k], f) != _field(B[k], f)]
                      for f in ("body", "headers", "content_type")}))
    if not rows:
        return
    print(f"\n{'REQUESTS':<22}{'before':>8}{'after':>8}{'lost':>6}{'gained':>8}{'body':>6}{'headers':>9}{'ctype':>7}")
    print("-" * 74)
    for n, A, B, lost, gained, ch in rows:
        flag = "  <<<" if lost or gained or any(ch.values()) else ""
        print(f"{n[:21]:<22}{len(A):>8}{len(B):>8}{len(lost):>6}{len(gained):>8}{len(ch['body']):>6}"
              f"{len(ch['headers']):>9}{len(ch['content_type']):>7}{flag}")
    print("-" * 74)
    for n, A, B, lost, gained, ch in rows:
        for k in lost[:15]:
            print(f"  LOST    {n}: {k[0]} {k[1]}")
        for k in gained[:8]:
            print(f"  gained  {n}: {k[0]} {k[1]}")
        for f in ("body", "headers", "content_type"):
            for k in ch[f][:6]:
                print(f"  {f:8s}{n}: {k[0]} {k[1][:70]}\n            before {', '.join(_field(A[k], f))[:150]}"
                      f"\n            after  {', '.join(_field(B[k], f))[:150]}")


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    a_tag, b_tag = sys.argv[1], sys.argv[2]
    out = os.environ.get("OUT", os.path.expanduser("~/.cache/jshound-regress"))
    names = sorted({os.path.basename(p)[len(a_tag) + 1:-5]
                    for p in glob.glob(os.path.join(out, f"{a_tag}_*.json"))})
    if not names:
        print(f"no runs tagged {a_tag!r} in {out}", file=sys.stderr)
        return 2

    rows, tot_lost, tot_gained = [], 0, 0
    for n in names:
        pa = os.path.join(out, f"{a_tag}_{n}.json")
        pb = os.path.join(out, f"{b_tag}_{n}.json")
        if not os.path.exists(pb):
            continue
        ra, rb = load(pa), load(pb)
        ea, eb = endpoints(ra), endpoints(rb)
        lost, gained = ea - eb, eb - ea
        tot_lost += len(lost)
        tot_gained += len(gained)
        rows.append((n, ea, eb, lost, gained, kinds(ra), kinds(rb)))

    print(f"{'corpus':<18}{'before':>8}{'after':>8}{'lost':>6}{'gained':>8}")
    print("-" * 48)
    for n, ea, eb, lost, gained, _ka, _kb in rows:
        flag = "  <<<" if lost or gained else ""
        print(f"{n:<18}{len(ea):>8}{len(eb):>8}{len(lost):>6}{len(gained):>8}{flag}")
    print("-" * 48)
    print(f"{'TOTAL':<18}{sum(len(r[1]) for r in rows):>8}{sum(len(r[2]) for r in rows):>8}"
          f"{tot_lost:>6}{tot_gained:>8}\n")

    # LOST FIRST. A removal is the only direction that can silently cost real surface.
    for n, _ea, _eb, lost, gained, ka, kb in rows:
        if lost:
            print(f"=== {n}: {len(lost)} LOST")
            for m, t in sorted(lost)[:25]:
                print(f"    {m:<7}{t}")
        if gained:
            print(f"=== {n}: {len(gained)} GAINED")
            for m, t in sorted(gained)[:25]:
                print(f"    {m:<7}{t}")
        new_kinds = {k: v for k, v in kb.items() if k not in ka}
        if new_kinds:
            print(f"=== {n}: new record kinds {new_kinds}")
    request_diff(a_tag, b_tag, names, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
