#!/usr/bin/env python3
"""residual.py — measure what static analysis MISSED, not what it found.

A count of endpoints is a number you cannot check. `1546 routes` reads exactly the same whether the
extractor got everything or half, which is how a run reported "routes and contracts captured"
while holding 7 contracts across 479 endpoints.

The checkable measurement is a RESIDUAL. Crudely grep every path-shaped string out of the saved bundles,
subtract everything the extractor attributed, and classify the remainder. The regex over-collects wildly —
asset paths, i18n keys, CSS selectors, license text — so the remainder is bucketed rather than reported as
one number, and only one bucket matters:

    MISSED-apiish   looks like an API path, and the extractor never mentioned it

That bucket is a bug list. Every entry is either a real endpoint the extractor should have found, or a
false positive that says something about the regex — and both are worth knowing. A run where it is large is
not a target with a big API, it is a tool with a hole.

  jshound residual <bodies-dir> <records.json>    what analyze missed in what your crawler saved
  residual.py <bodies-dir> <records.json> -v    also print the missed paths

Reads a saved bodies directory rather than a crawler's internal cache.
"""
import json, os, re, sys
from collections import Counter

_PATHLIT = re.compile(r'["\'`](/[A-Za-z0-9._~\-/{}$:]{2,120})["\'`]')
_ASSETEXT = re.compile(r'\.(js|mjs|css|png|jpe?g|gif|svg|ico|webp|woff2?|ttf|eot|map|mp4|webm|pdf|wasm|txt|xml)$', re.I)
_ASSETDIR = re.compile(r'^/(assets|static|_next|images?|img|fonts?|media|icons?|css|js|dist|build|public)/', re.I)
_APIISH = re.compile(r'^/(api|v\d+|graphql|rest|auth|oauth|admin|internal|service|rpc|gateway|_api)\b|'
                     r'^/[a-z0-9-]+/(v\d+|api)\b', re.I)
# Text files the crawler saves alongside bundles. Reading a 3MB HTML page for path literals is fine; reading
# a PNG is not, and `errors="ignore"` would happily turn one into megabytes of mojibake.
_READABLE = re.compile(r'\.(js|mjs|cjs|ts|jsx|tsx|json|html?|rsc|txt|map)$', re.I)


def classify(bodies, records_path, verbose=False):
    from jshound.analyze import require_corpus
    require_corpus("residual", bodies)
    from jshound import read_records
    records = read_records(records_path)
    claimed = set()
    for r in records:
        t = r.get("template") or r.get("uri") or r.get("path") or r.get("value") or ""
        if not t:
            continue
        lit = re.split(r"\{", str(t))[0].rstrip("/")
        # AN EXTRACTED TEMPLATE IS OFTEN ABSOLUTE, and the literal it came from is not. tldraw's
        # telemetry is written `fetch(new URL("/v1/event", cfg.endpoint))`, so the literal in the
        # bundle is `/v1/event` while the record resolves to
        # `https://clerk-telemetry.com/v1/event` — a path-shaped test on the record then never
        # matched, and residual reported two endpoints as MISSED that the extractor had resolved
        # better than the literal. A false miss in the one tool whose job is measuring misses is
        # worse than no measurement.
        if "://" in lit:
            path = "/" + lit.split("://", 1)[1].split("/", 1)[1] if "/" in lit.split("://", 1)[1] else ""
            if len(path) > 1:
                claimed.add(path.rstrip("/"))
        elif lit.startswith("/") and len(lit) > 1:
            claimed.add(lit)

    found = set()
    files = 0
    for root, _, names in os.walk(bodies):
        for n in names:
            p = os.path.join(root, n)
            # The crawler names bodies by content hash, so most have no extension at all. Sniff instead of
            # trusting the name — the same rule the bundle fetcher learned when a `.js.gz` cost a whole host.
            if not _READABLE.search(n):
                try:
                    with open(p, "rb") as fh:
                        head = fh.read(512)
                    if b"\x00" in head:
                        continue
                except Exception:
                    continue
            try:
                src = open(p, errors="ignore").read()
            except Exception:
                continue
            files += 1
            for m in _PATHLIT.finditer(src):
                found.add(m.group(1))

    buckets, missed = Counter(), []
    for p in found:
        base = re.split(r"\{|\$", p)[0].rstrip("/")
        if any(base.startswith(c) or c.startswith(base) for c in claimed if len(c) > 3):
            buckets["claimed"] += 1
        elif _ASSETEXT.search(p) or _ASSETDIR.match(p):
            buckets["asset"] += 1
        elif _APIISH.search(p):
            buckets["MISSED-apiish"] += 1
            missed.append(p)
        elif p.count("/") == 1 and len(p) < 30:
            buckets["single-segment"] += 1
        else:
            buckets["other"] += 1

    # A score off an empty corpus is not a zero, it is an absence of evidence — and "0 missed, 0.0%"
    # is the most reassuring line this tool can print. It is refused rather than printed.
    if not files:
        raise SystemExit(f"residual: read 0 files from {bodies!r} — nothing to measure the records "
                         "against, so there is no residual to report")
    tot = sum(buckets.values()) or 1
    miss = buckets.get("MISSED-apiish", 0)
    print(f"  files read          : {files}")
    print(f"  path literals       : {len(found)}")
    print(f"  templates extracted : {len(claimed)}")
    print(f"  buckets             : {dict(buckets)}")
    print(f"  >>> API-ish literals NOT extracted: {miss}  ({100 * miss / tot:.1f}% of literals)")
    if verbose and missed:
        for p in sorted(missed)[:120]:
            print(f"      {p}")
    return {"files": files, "literals": len(found), "claimed": len(claimed),
            "buckets": dict(buckets), "missed": sorted(missed)}


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if len(args) < 2:
        sys.exit(__doc__)
    classify(args[0], args[1], verbose="-v" in sys.argv)
