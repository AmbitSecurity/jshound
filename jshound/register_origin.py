#!/usr/bin/env python3
"""register_origin.py — tell the analyser where a script came from.

    register_origin.py <bodies-dir> <file> <url> [<file> <url> ...]
    register_origin.py ./bodies app_index.js https://statics.example.com/app/index.js

Every body the crawler saves is recorded in `<bodies>/_origin.jsonl` along with the URL it was fetched
from and the document that loaded it. Analysis reads that map to decide which host an endpoint belongs
to: a relative path in a bundle is a request to the DOCUMENT's host, not to the CDN that served the
file, and without an origin entry neither is known.

A script fetched by hand — curl, a python one-liner, anything that is not the crawler or
your crawler — lands in the bodies directory with no entry, and then every endpoint in it is
attributed by the `--host` fallback. That is not a small loss. On one corpus the seven hand-fetched
`app_*.js` files were unregistered and `app_index.js` alone held 448 of the target's API endpoints: the
most valuable file in the analysis was the one nothing could place, and a second analysis pass for a
second host claimed the same endpoints again, filing 857 of them under two hosts at once.

So: if you fetch a bundle yourself, register it. It is one command and it is the difference between an
endpoint filed under the host that serves it and an endpoint filed under a marketing site.
"""
import json, os, sys


def register(bodies, pairs, document=None):
    q = os.path.join(bodies, "_origin.jsonl")
    seen = set()
    if os.path.exists(q):
        for line in open(q, errors="replace"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("f"):
                seen.add(os.path.basename(r["f"]))
    n = 0
    with open(q, "a") as fh:
        for fname, url in pairs:
            base = os.path.basename(fname)
            if base in seen:
                print(f"  {base}: already registered", file=sys.stderr)
                continue
            if not os.path.exists(os.path.join(bodies, base)):
                print(f"  {base}: not in {bodies}, nothing to register", file=sys.stderr)
                continue
            # `d` is the DOCUMENT that loaded the script, which is what decides the host of a relative
            # path. Absent an explicit one, the fetch URL is the best available answer and is marked as
            # such rather than being presented as the document.
            rec = {"f": base, "u": url, "d": document or url}
            if not document:
                rec["d_inferred"] = True
            fh.write(json.dumps(rec) + "\n")
            n += 1
    return n


def main():
    a = sys.argv[1:]
    if len(a) < 3 or len(a[1:]) % 2:
        sys.exit(__doc__)
    bodies, rest = a[0], a[1:]
    doc = None
    if "--document" in rest:
        i = rest.index("--document")
        doc = rest[i + 1]
        rest = rest[:i] + rest[i + 2:]
    pairs = list(zip(rest[0::2], rest[1::2]))
    n = register(bodies, pairs, doc)
    print(f"registered {n} origin(s) in {os.path.join(bodies, '_origin.jsonl')}")


if __name__ == "__main__":
    main()
