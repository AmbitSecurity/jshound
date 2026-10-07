#!/usr/bin/env python3
"""remixmanifest.py — read a Remix / React Router framework-mode route table from the document.

    remixmanifest.py https://hydrogen.shop --json routes.json

A Remix document assigns its entire route manifest to a global in an inline script:

    window.__remixManifest = {"entry":{...},"routes":{"root":{...}},"url":"...","version":"9c45bfe8"}

This repo already used `__remixManifest` as a FINGERPRINT marker — proof the app is Remix — and read
nothing out of it. It declares four things, and none of them were reaching the output:

    routes           the route tree, as `path` segments nested through `parentId`
    hasLoader        the route answers `<path>?_data=<id>`, Remix's own data protocol
    hasAction        the same path accepts a non-GET submission
    module+imports   every chunk that route needs, as absolute URLs

THE DATA ENDPOINTS ARE DECLARED, NOT INFERRED, which is what makes them safe to emit. Verified on
hydrogen.shop: `?_data=root` answers 200 with 6,272 bytes of `text/remix-deferred` and
`?_data=routes/($locale)._index` answers 200 with 10,750 — while an INVENTED route id answers 403.
The ids come from the manifest, so there is nothing to guess; a route list alone would not do, because
the id and the URL path are different strings.

The payload is server data for that route, fetched unauthenticated: hydrogen.shop's `root` loader
returns `isLoggedIn`, the cart, and the shop's `gid://shopify/Shop/55145660472`.
"""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jshound.fetch import _Scripts, document_refs, http_get             # noqa: E402

from jshound.nodeenv import node_env, node_failure


def inline_scripts(html):
    """Every inline <script> body in the document, in order."""
    p = _Scripts()
    try:
        p.feed(html or "")
    except Exception:                                      # noqa: BLE001 — malformed markup is normal
        pass
    return list(p.inline)


def read(doc_url, timeout=30, workdir=None):
    """Returns the manifest's routes, data endpoints and chunks, or `available: False` with a reason."""
    st, html, doc_url = http_get(doc_url, timeout=timeout, with_url=True)
    if not html:
        return {"available": False, "reason": f"document unreachable (status={st})",
                "document": doc_url}
    GLOBALS = ("__remixManifest", "__reactRouterManifest")
    bodies = [s for s in inline_scripts(html) if any(g in s for g in GLOBALS)]
    # React Router v7 does NOT inline it. reactrouter.com links
    # `<link rel="modulepreload" href="/assets/manifest-98aa57ec.js">`, and that file is one
    # statement: `window.__reactRouterManifest={...}`. The manifest's own `url` field names the same
    # file, so the convention is the framework's rather than a guess — but the document is where the
    # hashed name can be read, which is the lesson the Next reader already paid for.
    fetched_from = None
    if not bodies:
        for u in document_refs(doc_url, html):
            base = os.path.basename(u.split("?")[0])
            if not (base.startswith("manifest-") and base.endswith(".js")):
                continue
            st2, body = http_get(u, timeout=timeout)
            if body and any(g in body for g in GLOBALS):
                bodies, fetched_from = [body], u
                break
    if not bodies:
        return {"available": False, "is_remix": any(g in html for g in GLOBALS),
                "reason": "document declares neither window.__remixManifest nor "
                          "window.__reactRouterManifest, and links no manifest-<hash>.js "
                          "(a Remix app in SPA mode, or not Remix)",
                "document": doc_url}

    import tempfile
    d = workdir or tempfile.mkdtemp(prefix="remixmanifest-")
    paths = []
    for i, src in enumerate(bodies):
        fp = os.path.join(d, f"inline{i}.js")
        with open(fp, "w", errors="replace") as fh:
            fh.write(src)
        paths.append(fp)

    js = os.path.join(os.path.dirname(os.path.abspath(__file__)), "js", "remixmanifest.js")
    env = node_env()
    env.setdefault("NODE_PATH", os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "node_modules"))
    try:
        r = subprocess.run(["node", js] + paths, capture_output=True, env=env, timeout=120)
        why = node_failure(r)
        if why:
            return {"available": False, "reason": f"reader failed: {why}", "is_remix": True, "document": doc_url}
        data = json.loads(r.stdout or b"{}")
    except Exception as e:                                 # noqa: BLE001
        return {"available": False, "is_remix": True, "reason": f"evaluator failed: {e}",
                "document": doc_url}
    if not data.get("routes"):
        return {"available": False, "is_remix": True,
                "reason": "window.__remixManifest present but declared no routes",
                "document": doc_url}
    data.update({"available": True, "is_remix": True, "document": doc_url,
                 "manifest_url": fetched_from})
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("--json")
    ap.add_argument("--timeout", type=int, default=30)
    a = ap.parse_args(argv)

    d = read(a.url, timeout=a.timeout)
    if a.json:
        json.dump(d, open(a.json, "w"), indent=1)
    if not d.get("available"):
        print(f"  no remix manifest: {d.get('reason')}", file=sys.stderr)
        return 1
    routes, data_eps = d["routes"], d.get("data_endpoints", [])
    actions = [r for r in routes if r.get("has_action")]
    print(f"  {d.get('flavour')}: {len(routes)} routes · {len(data_eps)} data endpoints "
          f"({d.get('data_protocol')}) · {len(actions)} accept a submission · "
          f"{len(d.get('chunks', []))} chunks · build {d.get('version')}", file=sys.stderr)
    for r in routes:
        print(r["template"])
    for e in data_eps:
        print(f"{e['template']}?{e['query']}" if e.get("query") else e["template"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
