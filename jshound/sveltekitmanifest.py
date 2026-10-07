#!/usr/bin/env python3
"""sveltekitmanifest.py — recover a SvelteKit app's route table from its client entry module.

    sveltekitmanifest.py https://svelte.dev --json routes.json

SvelteKit routes on the client, so the client has to be told every route, and the entry module ships
the table as a literal. Unlike Next and Nuxt there is no manifest FILE — the table is inside the
JavaScript, minified to an export alias (`export { s as dictionary }`) — so it is read by parsing the
module and resolving the alias. See `js/sveltekit.js`; the export name is the only stable handle, which
is what makes this survive minification.

What comes back is a route list with the filesystem spelling intact alongside its URL template, so a
route group is visible as organisational rather than being silently dropped:

    /(authed)/playground/[...id]/embed   ->  /playground/{id}/embed
    /docs/[topic]/[...path=documentation] ->  /docs/{topic}/{path}   (path constrained by `documentation`)

Every route also has a data endpoint. SvelteKit's client fetches a route's load data from
`<route>/__data.json`, and those are live: on svelte.dev `/docs/__data.json`, `/packages/__data.json`
and `/blog/__data.json` all answer 200 with the page's serialised load output, while
`/nope-xyz/__data.json` answers 404. They are emitted as `data_endpoints` because they are real
fetchable JSON surface that no link on the page points to.

LIMITS, MEASURED:
  - A SvelteKit `+server.ts` endpoint is NOT in this table. The client never client-side-routes to an
    API route, so the manifest the client gets does not mention one. Those come from the AST extractor
    reading the code that calls them, like any other endpoint.
  - `server_loads` is reported raw. It is a list of NODE indices, and mapping them back to routes means
    relying on the sign and nesting in the dictionary's values (`[5]`, `[-12]`, `[-7, [2]]`), which is
    not a documented contract — so no per-route "runs a server load" claim is made from it.
"""
import argparse
import json
import os
import subprocess
import sys
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jshound.fetch import document_imports, document_refs, http_get     # noqa: E402

from jshound.nodeenv import node_env, node_failure


def entry_url(doc_url, html):
    """The client entry module, and the app's base path, from a URL the DOCUMENT references.

    Selected structurally rather than by pattern: SvelteKit lays the entry out as
    `<base>/<appDir>/immutable/entry/app.<hash>.js`, and while `appDir` is configurable and the hash is
    not predictable, the `entry/` directory and the `app.` prefix are fixed by the framework. Dropping
    the four known trailing segments leaves `base`, which is what routes are relative to.

    Inline-script imports are searched as well as tag attributes, because SvelteKit's bootstrap is an
    inline `import("./_app/immutable/entry/app.X.js")` and nothing links the entry as a `src` — reading
    attributes alone reported sveltesociety.dev, okupter.com, realworld.svelte.dev and sveltelab.dev as
    "not a SvelteKit app" when all four are one.
    """
    for u in document_refs(doc_url, html) + document_imports(doc_url, html):
        parts = [p for p in urlsplit(u).path.split("/") if p]
        if len(parts) >= 3 and parts[-2] == "entry" and parts[-1].startswith("app.") \
                and parts[-1].endswith(".js"):
            base = "/" + "/".join(parts[:-4]) if len(parts) > 4 else ""
            return u, base.rstrip("/")
    return "", ""


def read(doc_url, timeout=30, workdir=None):
    """Returns the route table, or `available: False` with a reason."""
    # The FINAL url after redirects: relative references in the markup resolve against where the
    # document actually came from, not where it was asked for.
    st, html, doc_url = http_get(doc_url, timeout=timeout, with_url=True)
    if not html:
        return {"available": False, "reason": f"document unreachable (status={st})",
                "document": doc_url}
    entry, base = entry_url(doc_url, html)
    if not entry:
        return {"available": False, "is_sveltekit": False,
                "reason": "not a SvelteKit app (document references no entry/app.<hash>.js)",
                "document": doc_url}

    st, body = http_get(entry, timeout=timeout)
    if not body:
        return {"available": False, "is_sveltekit": True, "entry": entry,
                "reason": f"entry module unreachable (status={st})", "document": doc_url}

    import tempfile
    d = workdir or tempfile.mkdtemp(prefix="sveltekit-")
    fp = os.path.join(d, os.path.basename(urlsplit(entry).path))
    with open(fp, "w") as fh:
        fh.write(body)

    js = os.path.join(os.path.dirname(os.path.abspath(__file__)), "js", "sveltekit.js")
    env = node_env()
    env.setdefault("NODE_PATH", os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "node_modules"))
    try:
        r = subprocess.run(["node", js, fp], capture_output=True, env=env, timeout=120)
        why = node_failure(r)
        if why:
            return {"available": False, "reason": f"reader failed: {why}", "is_sveltekit": True, "entry": entry, "document": doc_url}
        data = json.loads(r.stdout or b"{}")
    except Exception as e:                                 # noqa: BLE001
        return {"available": False, "is_sveltekit": True, "entry": entry,
                "reason": f"parser failed: {e}", "document": doc_url}

    if not data.get("routes"):
        return {"available": False, "is_sveltekit": True, "entry": entry,
                "reason": "entry module declares no route dictionary"
                          + (f" ({data['errors'][0]['error']})" if data.get("errors") else ""),
                "document": doc_url}

    for r in data["routes"]:
        r["template"] = (base + r["template"]) if base else r["template"]
        # SvelteKit's own data protocol: the client asks `<route>/__data.json` for a route's load
        # output. `/` is spelled `/__data.json`, not `//__data.json`.
        r["data_endpoint"] = r["template"].rstrip("/") + "/__data.json"
    data.update({"available": True, "is_sveltekit": True, "document": doc_url,
                 "entry": entry, "base": base,
                 "data_endpoints": sorted({r["data_endpoint"] for r in data["routes"]})})
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
        print(f"  no route table: {d.get('reason')}", file=sys.stderr)
        return 1
    constrained = [r for r in d["routes"] if r["matchers"]]
    print(f"  entry: {d['entry']}", file=sys.stderr)
    print(f"  DECLARED BY THE BUILD: {len(d['routes'])} routes · "
          f"{len(d['data_endpoints'])} __data.json endpoints · "
          f"{len(d['matchers'])} matchers · {len(constrained)} constrained routes", file=sys.stderr)
    for r in d["routes"]:
        print(r["template"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
