#!/usr/bin/env python3
"""approutes.py — enumerate a Next.js App Router app's routes, and close over its chunks.

    approutes.py https://app.example.com --out chunks.json [--max-rounds 5]

A Pages-Router/webpack app writes its lazy-chunk URLs into `__webpack_require__.u`, and `chunkmap.js`
recovers them by evaluating it. An App Router app has no such table — measured, `chunkmap.js` recovers 3
URLs from a 1,043-file corpus — because Next generates routing from the FILESYSTEM. There is no
`createBrowserRouter([...])` anywhere in the bundle to find, which is why static route extraction returned
9 routes for an app that plainly has hundreds.

There are two places the route table can come from, and they are not equally trustworthy.

THE BUILD'S OWN MANIFEST, when there is one. A Pages-Router build serves `_buildManifest.js` and links
it from the document: every route, named parameters intact, plus the chunk list and the rewrites. Those
are declarations, not inferences, so `nextmanifest.py` is tried first and its routes are reported as
`declared`. On Supabase's dashboard that is 293 routes and 436 chunks where this file alone found 0.

CHUNK PATHS, when there is not — which is every App Router build, since those serve no manifest. The
route table is still there; it is just spelled in the chunk PATHS:

    app/[locale]/(auth-layout)/login/page-b13a8147.js        ->  /login
    app/[locale]/(custom-layout)/user/[username]/page-eb.js  ->  /user/{username}
    app/[locale]/(live-layout)/live/[id]/loading-44e.js      ->  /live/{id}

`(group)` is a route GROUP and contributes no URL segment. `[param]` is dynamic. A `page` chunk means the
path is a real route; `layout`/`loading`/`error`/`not-found`/`default` describe one without being one.

This half only works if the build emits NAMED route chunks, and measured, current ones do not: Turbopack
and webpack's `deterministic` ids both produce opaque filenames (`0440fqg6n82rw.js`), and none of the six
App Router targets tested yielded a single route this way. The derived count is reported separately from
the declared one so a zero here is visible rather than averaged away.

That makes enumeration a FIXPOINT rather than a lookup: each page you fetch names the chunks for its own
subtree, those chunk paths name further routes, and you stop when a round adds nothing. This is what
closes the gap that link-following leaves — /inbox and /account/settings are not linked from an anonymous
homepage, so a crawler that only follows <a href> can never reach the code behind them.
"""
import argparse, json, os, re, sys, urllib.parse
from concurrent.futures import ThreadPoolExecutor

from jshound.nodeenv import node_env

# Next serves its chunk directory at more than one path. webpack builds put it at
# `static/chunks/`, Turbopack at `static/immutable/chunks/` — and keying on the literal
# `static/chunks/` found ZERO chunks on every Turbopack target, which is now Next's default.
# Matched on the `chunks/` directory under a `static/` ancestor instead of on one fixed spelling.
CHUNK = re.compile(r'static/(?:[A-Za-z0-9_-]+/)*chunks/(?:[A-Za-z0-9%_()\[\].,$~@-]+/)*[A-Za-z0-9%_()\[\].,$~@-]+\.js')
LEAF = re.compile(r'^(page|route)-[0-9a-f]{8,}$')
NOT_A_ROUTE = {"layout", "loading", "error", "not-found", "default", "template", "global-error"}


def route_of(chunk_path):
    """A route, or None if this chunk describes a route without being one."""
    p = urllib.parse.unquote(chunk_path)
    if "static/chunks/app/" not in p and not p.startswith("app/"):
        return None
    p = p.split("static/chunks/", 1)[-1]
    parts = p[len("app/"):].split("/") if p.startswith("app/") else p.split("/")
    leaf = re.sub(r"\.js$", "", parts[-1])
    base = re.sub(r"[-.][0-9a-f]{8,}$", "", leaf)
    if base in NOT_A_ROUTE or not LEAF.match(leaf):
        return None
    segs = []
    for s in parts[:-1]:
        if s.startswith("(") and s.endswith(")"):      # route group: organisational only
            continue
        if s.startswith("@"):                          # parallel route slot, not a URL segment
            continue
        if s.startswith("[") and s.endswith("]"):
            name = s.strip("[]").lstrip(".")           # [id], [...slug], [[...path]]
            segs.append("{" + (name or "param") + "}")
            continue
        segs.append(s)
    return "/" + "/".join(segs) if segs else "/"


def fetchable(route):
    """A concrete URL for a templated route. `{locale}` is Next's own prefix, not a real segment."""
    r = route
    if r.startswith("/{locale}"):
        r = r[len("/{locale}"):] or "/"
    return re.sub(r"\{[^}]*\}", "x", r) or "/"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("base")
    ap.add_argument("--out")
    ap.add_argument("--max-rounds", type=int, default=6)
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    # `fetch.py` and `sourcemaps.py` both treat this as optional and fall back to urllib; this did
    # not, so `jshound routes` was the one documented command that died with a bare ImportError on
    # the install the README prescribes.
    try:
        from curl_cffi import requests as rq
    except ImportError:
        sys.exit("routes: this command needs curl_cffi for its TLS fingerprint — "
                 "install it with `pip install \"jshound[fetch]\"`")

    # AUTHORITATIVE SOURCE FIRST. A Pages-Router build serves its whole route table and its chunk list
    # in `_buildManifest.js`; those are the build's own declarations, not inferences from filenames, so
    # when an app links one there is nothing left to guess at. It also short-circuits the fixpoint: the
    # loop below exists to DISCOVER routes, and a declared route is already discovered, so the declared
    # chunks are seeded (literals still get mined from all of them) without re-fetching 293 pages to
    # learn what the manifest already said. An App Router build links no manifest and takes the loop.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from jshound.nextmanifest import read as read_manifest
    mf = read_manifest(a.base)
    declared, declared_chunks, asset_base = set(), set(), ""
    if mf.get("available"):
        declared = {r["template"] for r in mf.get("api_routes", []) + mf.get("routes", [])}
        declared_chunks = set(mf.get("chunks", []))
        asset_base = mf.get("asset_base") or ""
        print(f"  manifest: {len(declared)} routes DECLARED by the build "
              f"({sum(1 for r in declared if r.startswith('/api/'))} api) · "
              f"{len(declared_chunks)} chunks · {len(mf.get('rewrites', []))} rewrites", file=sys.stderr)
    else:
        print(f"  manifest: none ({mf.get('reason')})", file=sys.stderr)

    def get(path):
        for hdr in ({}, {"Rsc": "1"}):     # some routes 404 plainly and answer over RSC
            try:
                x = rq.get(a.base.rstrip("/") + path, impersonate="chrome", timeout=25, headers=hdr)
                if x.status_code == 200 and len(x.text) > 200:
                    return {m.group(0) for m in CHUNK.finditer(x.text)}
            except Exception:
                pass
        return set()

    # THE SEED PROBLEM. Fetching "/" names only the home route's own app chunks, so deriving routes from
    # chunk paths alone converges immediately at one route — there is nothing to expand from. The seed has
    # to come from the code, and it does: the chunks the homepage loads contain the app's own path literals
    # (/account/settings, /dashboard/inbox, /dashboard/ledger/payouts). Those are strings the developers
    # wrote, not guesses, and each one visited names a further subtree's chunks.
    #
    # So the loop alternates two sources — path literals mined from fetched chunks, and routes decoded from
    # app/ chunk paths — until neither produces anything new.
    ROUTEISH = re.compile(r"^/[a-z0-9][a-z0-9\-_/]{1,60}$", re.I)
    seen_chunk_urls, literal_routes = set(), set()

    def mine_literals(chunk_urls):
        """Run the extractor over newly fetched chunks and keep the path literals that look like routes."""
        import subprocess, tempfile
        fresh = [c for c in chunk_urls if c not in seen_chunk_urls]
        if not fresh:
            return set()
        seen_chunk_urls.update(fresh)
        d = tempfile.mkdtemp(prefix="approutes-")
        files = []
        def dl(c):
            # `asset_base` comes from the manifest's own URL, so it is right even when the app serves
            # its bundle from a CDN under an `assetPrefix` — where both origin-relative guesses 404.
            cands = ([urllib.parse.urljoin(asset_base, c)] if asset_base else []) + [
                a.base.rstrip("/") + "/_next/" + c, a.base.rstrip("/") + "/" + c]
            for u in cands:
                try:
                    x = rq.get(u, impersonate="chrome", timeout=25)
                    if x.status_code == 200 and len(x.text) > 200:
                        fp = os.path.join(d, os.path.basename(c))
                        open(fp, "w").write(x.text)
                        return fp
                except Exception:
                    pass
            return None
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            files = [f for f in ex.map(dl, fresh) if f]
        if not files:
            return set()
        ast_js = os.path.join(os.path.dirname(os.path.abspath(__file__)), "js", "static_ast.js")
        env = node_env()
        env.setdefault("NODE_PATH", os.path.join(  # repo-local; the image sets NODE_PATH itself
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "node_modules"))
        out = set()
        for i in range(0, len(files), 30):                 # batch so one huge chunk cannot blow the heap
            try:
                r2 = subprocess.run(["node", "--max-old-space-size=4096", ast_js] + files[i:i+30],
                                    capture_output=True, env=env, timeout=240)
                for rec in json.loads(r2.stdout or b"[]"):
                    t = str(rec.get("template") or "")
                    if rec.get("_kind") in ("path-literal", "route", "nav") and ROUTEISH.match(t):
                        out.add(t)
            except Exception:
                pass
        return out

    chunks, visited, frontier = set(declared_chunks), set(), ["/"]
    for rnd in range(a.max_rounds):
        todo = [p for p in frontier if p not in visited]
        if not todo:
            break
        visited |= set(todo)
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            for got in ex.map(get, todo):
                chunks |= got
        routes = {route_of(c) for c in chunks}
        routes.discard(None)
        literal_routes |= mine_literals(sorted(chunks))
        frontier = sorted({fetchable(r) for r in routes} | literal_routes)
        new = [p for p in frontier if p not in visited]
        print(f"  round {rnd+1}: fetched {len(todo):4} · chunks {len(chunks):5} · "
              f"routes-from-chunks {len(routes):4} · from-literals {len(literal_routes):4} · "
              f"new to visit {len(new)}", file=sys.stderr)
        if not new:
            break
    derived = sorted({r for r in (route_of(c) for c in chunks) if r})
    routes = sorted(declared | set(derived))
    # Provenance is reported separately and never merged away: a declared route is as certain as the
    # build, a derived one is only as certain as the inference that produced it. Collapsing the two into
    # one number would hide exactly the distinction that makes the declared ones worth having.
    print(f"\n  CONVERGED: {len(routes)} routes "
          f"({len(declared)} declared by the build, {len(derived)} derived from chunk paths) · "
          f"{len(chunks)} chunk urls · {len(visited)} pages fetched", file=sys.stderr)
    if a.out:
        json.dump({"chunks": sorted(chunks), "routes": routes,
                   "routes_declared": sorted(declared), "routes_derived": derived,
                   "routes_from_literals": sorted(literal_routes),
                   "rewrites": mf.get("rewrites", []), "middleware": mf.get("middleware", []),
                   "visited": sorted(visited)}, open(a.out, "w"), indent=1)
    for r in routes:
        print(r)


if __name__ == "__main__":
    main()
