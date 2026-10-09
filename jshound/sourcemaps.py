#!/usr/bin/env python3
"""sourcemaps.py: if the target ships its own source, read that instead of the minified copy.

    jshound sourcemaps ./bodies --scope app.example.com
    jshound sourcemaps ./bodies --dry-run          report what is available and stop

THIS TOOL FETCHES, and with `fetch` it is the only part of jshound that does. `analyze` deliberately
does not, and that division stands: this requests only `<url>.map` beside URLs already recorded in the
corpus's `_origin.jsonl`, so it reaches no host whose JavaScript you do not already have. `--scope`
narrows that further to the hosts you name; without it, every recorded host is in scope.

WHAT IT IS WORTH, MEASURED, and the answer moved once Vite became common. The original figure here
was "0-11% of routes", taken on webpack-era targets. Re-measured:

    Excalidraw   57 maps,  699 sources   endpoints  5 ->  8  (+60%), nothing lost
    tldraw      141 maps,  982 sources   endpoints 66 -> 72   (+9%), nothing lost
                                         zod schemas 68 -> 105 (+54%)

So the gain varies by an order of magnitude and arrives in two different forms. On Excalidraw it is
endpoints: /v1/ai/diagram-to-code/generate-streaming and a Firestore documents path exist nowhere in
the minified bundle. On tldraw it is barely endpoints at all but 37 more runtime-validator schemas,
which is 37 more request contracts with real field types.

It is still not a route-discovery mechanism to LEAN on, roughly half of targets ship no maps at
all, and a pipeline that depends on them stops working there. But "0-11%" understated it, and Vite
ships maps in production by default, which is why it reads differently now than it did.

What they uniquely carry is the two things minification genuinely destroys:

  * TYPES, erased at compile time and recoverable from nowhere else. The original of an API client
    reads `createInquiry(data: ComplianceCheckRequest)` with the interface beside it; the
    shipped copy reads `async e => ...`.
  * ORIGINAL PARAMETER NAMES, `businessId`, not `e`. Which is what tells an authorization sweep
    that a slot wants a business id and not an invoice id.

Everything else, routes, verbs, hosts, wire keys, is in the minified bundle, because a key that
crosses the wire cannot be renamed without breaking the server.

HOW IT DELIVERS THEM. It adds no analysis path. Each original source is written into the bodies
directory as an ordinary file with a registered origin, so `analyze` picks it up exactly the way it
picks up anything else.
"""
import argparse
import concurrent.futures as cf
import json
import os
import re
import sys

from .register_origin import register

SKIP = re.compile(r"(^|/)(node_modules|webpack/runtime|webpack/bootstrap)/")


# A `.map` is the largest thing this ever downloads, Grafana's biggest is 38 MB, and it is still
# the target that chooses the size, so the read is bounded. Higher than `fetch.MAX_BODY` because a
# map legitimately dwarfs the bundle it describes; a truncated one fails `json.loads` and is skipped,
# which is the right outcome for something this far outside the normal range.
MAX_MAP = 256 * 1024 * 1024


def fetch(url, timeout=40):
    """(status, text). Shares `fetch.http_get`, which is where the transport, the UA and the read cap
    live, this had its own copy, and that copy was the one place a response was read uncapped."""
    from jshound.fetch import http_get
    return http_get(url, timeout=timeout, max_bytes=MAX_MAP)


def script_urls(bodies):
    """-> ([js urls], {url: document that loaded it}) from the crawler's origin manifest.

    The origin map, not a store: a script is an asset and most never reach a site map with a URL at
    all. Measured on one corpus, a store held 4 script URLs where `_origin.jsonl` held 7,948, of
    which 309 were the application's own bundles.
    """
    urls, seen, doc_of = [], set(), {}
    q = os.path.join(bodies, "_origin.jsonl")
    if not os.path.exists(q):
        return [], {}
    for line in open(q, errors="replace"):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        u = (r.get("u") or "").split("?")[0]
        if u.endswith(".js") and u not in seen:
            seen.add(u)
            urls.append(u)
        # The DOCUMENT that loaded the bundle, not the bundle's own URL. An original source compiled
        # into a bundle belongs, for resolving its relative paths, wherever that bundle was loaded.
        # Registering it against the bundle instead filed 453 recovered endpoints under a CDN that
        # serves none of them.
        if u and r.get("d"):
            doc_of.setdefault(u, r["d"])
    return urls, doc_of


def in_scope(url, hosts):
    if not hosts:
        return True
    h = re.sub(r"^https?://", "", url).split("/")[0].split(":")[0].lower()
    return any(h == s or h.endswith("." + s) for s in hosts)


def safe_name(src):
    keep = re.sub(r"[^A-Za-z0-9._-]", "_", src.replace("webpack://", "").lstrip("./"))
    return keep[-120:] or "source"


def harvest(bodies, hosts=None, dry_run=False, max_sources=4000, workers=8):
    urls, doc_of = script_urls(bodies)
    urls = [u for u in urls if in_scope(u, hosts)]
    print(f"{len(urls)} script URL(s) recorded by the crawler", file=sys.stderr)
    if not urls:
        return 0

    def probe(u):
        try:
            status, text = fetch(u + ".map")
            if status == 200 and text[:1] == "{":
                d = json.loads(text)
                if d.get("sourcesContent"):
                    return u, d
        except Exception:                                  # noqa: BLE001
            pass
        return None

    with cf.ThreadPoolExecutor(workers) as ex:
        maps = [m for m in ex.map(probe, urls) if m]

    if not maps:
        print("no sourcemaps served, the minified extraction is the whole story here, which is the "
              "normal case for about half of targets", file=sys.stderr)
        return 0

    total = sum(len([s for s in d.get("sources", []) if not SKIP.search(s or "")]) for _u, d in maps)
    print(f"{len(maps)} sourcemap(s), {total} first-party source file(s)", file=sys.stderr)
    if dry_run:
        return total

    written, pairs = 0, []
    for u, d in maps:
        doc = doc_of.get(u, u)
        for src, content in zip(d.get("sources") or [], d.get("sourcesContent") or []):
            if not content or SKIP.search(src or "") or written >= max_sources:
                continue
            name = f"srcmap_{safe_name(src)}"
            with open(os.path.join(bodies, name), "w", errors="replace") as fh:
                fh.write(content)
            # Registered against the DOCUMENT, so relative paths inside resolve to the app's host.
            # register() takes (filename, url) PAIRS. Appending them flat made it unpack a single
            # string, so every recovered source was written to disk and then NONE of them were
            # attributed, 699 files on Excalidraw, each one analysed later against the --host
            # fallback instead of the page that loaded it. The files existed, so the failure looked
            # like success until the traceback was read.
            pairs.append((name, doc))
            written += 1
    if pairs:
        register(bodies, pairs)
    print(f"wrote {written} original source file(s) into {bodies}\n"
          f"  now run: jshound analyze {bodies}", file=sys.stderr)
    return written


def main(argv=None):
    ap = argparse.ArgumentParser(prog="jshound sourcemaps")
    ap.add_argument("bodies")
    ap.add_argument("--scope", help="comma-separated hosts to fetch from (default: any)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-sources", type=int, default=4000)
    a = ap.parse_args(argv)
    hosts = [h.strip().lower() for h in (a.scope or "").split(",") if h.strip()]
    harvest(a.bodies, hosts, a.dry_run, a.max_sources)


if __name__ == "__main__":
    main()
