#!/usr/bin/env python3
"""nextmanifest.py: read a Next.js app's route table out of the manifest the build itself serves.

    nextmanifest.py https://supabase.com/dashboard --json routes.json

A Pages-Router build emits `_buildManifest.js` and links it from the document. It holds every route the
application has, spelled as the developers wrote them, with dynamic parameters still NAMED:

    /api/platform/storage/{ref}/vector-buckets/{id}/indexes/{indexName}

That is a different kind of result from everything else in this repo. The AST extractor infers endpoints
from code and can be wrong about one; a manifest route is the build's own declaration of what it routes,
so the only way it is wrong is if the build is wrong about itself. On Supabase's dashboard it yields 293
routes, 103 of them `/api/*`, against the 0 that deriving routes from chunk filenames returns there.

Three manifests are read when linked, and all three are READ FROM THE SYNTAX TREE rather than scraped
(see `js/nextmanifest.js` for why, and `js/literal.js` for why none of them is run):

    _buildManifest.js             every route, each route's chunks, and `__rewrites`
    _ssgManifest.js               which of those routes were statically generated
    _clientMiddlewareManifest.js  the middleware matchers, as `/api/:function*`

THE MANIFEST URL IS NEVER CONSTRUCTED. Guessing `{origin}/_next/static/{buildId}/_buildManifest.js`
404s on any app with an `assetPrefix`, Supabase serves its studio bundle from
`frontend-assets.supabase.com/studio/<hash>/`, and the guessed URL missed all 293 routes. The document
already contains the correct absolute URL, query string and all, so it is read from there.

LIMIT, MEASURED: an App Router build serves no `_buildManifest.js` at all. Of the Next apps tested,
every Pages-Router one linked a manifest and every App-Router one linked none; see the README. For those
apps the route table has to come from chunk paths (`approutes.py`) or from path literals in the code.
"""
import argparse
import json
import os
import subprocess
import sys
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jshound.fetch import document_refs, http_get                       # noqa: E402

from jshound.nodeenv import node_env, node_failure

# Exact basenames, compared as strings against the parsed URL path. Next names these files itself and
# has never minified or hashed the names, so there is nothing here for a pattern to match loosely.
MANIFESTS = ("_buildManifest.js", "_ssgManifest.js", "_clientMiddlewareManifest.js")


def manifest_urls(doc_url, html):
    """The absolute URLs of the manifests this document links, in the order the names are listed.

    Selection is exact basename equality against `MANIFESTS`, which is why `document_refs` can hand
    over every `src`/`href` unfiltered without costing precision: `_buildManifest.js.map` and
    `my_buildManifest.js` are both rejected because neither basename IS `_buildManifest.js`.
    """
    out = {}
    for absu in document_refs(doc_url, html):
        base = os.path.basename(urlsplit(absu).path)
        if base in MANIFESTS:
            out.setdefault(base, absu)
    return [out[n] for n in MANIFESTS if n in out]


def asset_base_of(urls):
    """Where a declared chunk is actually served, taken from the manifest's own URL.

    Chunk paths inside the manifest are relative (`static/chunks/x.js`), and on an app with an
    `assetPrefix` they resolve under the CDN rather than the document origin, Supabase serves its
    studio bundle from `frontend-assets.supabase.com/studio/<hash>/_next/`. Everything up to and
    including `/_next/` in the URL the manifest was fetched from IS that base, so it never has to be
    guessed. Returns "" when no URL contains `/_next/`.
    """
    for u in urls:
        i = u.find("/_next/")
        if i != -1:
            return u[:i + len("/_next/")]
    return ""


def read(doc_url, timeout=30, workdir=None):
    """Fetch the document, fetch whatever manifests it links, and evaluate them.

    Returns the evaluator's dict plus `manifest_urls`/`document`. `available` is False when the app
    links no manifest, which is the normal outcome for an App Router build rather than a failure.
    """
    # The FINAL url after redirects: relative references in the markup resolve against where the
    # document actually came from, not where it was asked for.
    status, html, doc_url = http_get(doc_url, timeout=timeout, with_url=True)
    if not html:
        return {"available": False, "reason": f"document unreachable (status={status})",
                "document": doc_url, "manifest_urls": []}
    urls = manifest_urls(doc_url, html)
    if not urls:
        # Say which it is. "Expected for an App Router build" is only true of a Next app, and
        # reporting it for a site that was never Next states a fact about the target that is not in
        # evidence, the same mistake as reporting an endpoint nothing in the bundle supports.
        next_app = any(p in html for p in ("/_next/", "__NEXT_DATA__", "__next_f"))
        return {"available": False, "is_next": next_app,
                "reason": ("Next app, but links no _buildManifest.js, an App Router build serves none"
                           if next_app else "not a Next.js app (no /_next/ references in the document)"),
                "document": doc_url, "manifest_urls": []}

    import tempfile
    d = workdir or tempfile.mkdtemp(prefix="nextmanifest-")
    files, fetched = [], []
    for u in urls:
        st, body = http_get(u, timeout=timeout)
        if not body:
            continue
        fp = os.path.join(d, os.path.basename(urlsplit(u).path))
        with open(fp, "w") as fh:
            fh.write(body)
        files.append(fp)
        fetched.append(u)
    if not files:
        return {"available": False, "reason": "manifests linked but none fetched",
                "document": doc_url, "manifest_urls": urls}

    js = os.path.join(os.path.dirname(os.path.abspath(__file__)), "js", "nextmanifest.js")
    env = node_env()
    env.setdefault("NODE_PATH", os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "node_modules"))
    try:
        r = subprocess.run(["node", js] + files, capture_output=True, env=env, timeout=120)
        why = node_failure(r)
        if why:
            return {"available": False, "reason": f"reader failed: {why}", "document": doc_url, "manifest_urls": fetched}
        data = json.loads(r.stdout or b"{}")
    except Exception as e:                                 # noqa: BLE001
        return {"available": False, "reason": f"evaluator failed: {e}",
                "document": doc_url, "manifest_urls": fetched}
    # The manifest URL also hands over the asset prefix for free. Chunk paths inside the manifest are
    # relative (`static/chunks/x.js`), and on an app with an `assetPrefix` they resolve under the CDN,
    # not the document origin: everything up to and including `/_next/` in the manifest's own URL IS
    # that base. Deriving it here means callers never have to guess where a declared chunk is served.
    data.update({"available": True, "is_next": True, "document": doc_url,
                 "manifest_urls": fetched, "asset_base": asset_base_of(fetched)})
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("--json", help="write the full result here")
    ap.add_argument("--timeout", type=int, default=30)
    a = ap.parse_args(argv)

    d = read(a.url, timeout=a.timeout)
    if not d.get("available"):
        print(f"  no build manifest: {d.get('reason')}", file=sys.stderr)
        if a.json:
            json.dump(d, open(a.json, "w"), indent=1)
        return 1

    pages, api = d.get("routes", []), d.get("api_routes", [])
    print(f"  manifests: {', '.join(os.path.basename(urlsplit(u).path) for u in d['manifest_urls'])}",
          file=sys.stderr)
    print(f"  DECLARED BY THE BUILD: {len(pages) + len(api)} routes "
          f"({len(api)} api, {len(pages)} page) · {len(d.get('chunks', []))} chunks · "
          f"{len(d.get('rewrites', []))} rewrites · {len(d.get('ssg', []))} prerendered", file=sys.stderr)
    for r in api:
        print(r["template"])
    for r in pages:
        print(r["template"])
    for m in d.get("middleware", []):
        print(f"# middleware matches {m['source']}", file=sys.stderr)
    if a.json:
        json.dump(d, open(a.json, "w"), indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
