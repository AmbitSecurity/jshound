#!/usr/bin/env python3
"""nuxtmanifest.py — read a Nuxt app's route list out of the app manifest its build serves.

    nuxtmanifest.py https://nuxt.com --json routes.json

Nuxt's counterpart to Next's `_buildManifest.js` is a two-step lookup under its build assets
directory, and it is JSON rather than JavaScript, so nothing needs evaluating:

    <base>builds/latest.json          ->  {"id": "1fba331f-...", "timestamp": 1791242423396}
    <base>builds/meta/<id>.json       ->  {"prerendered": [900 routes], "matcher": {...}}

`prerendered` is every route the build rendered ahead of time — 900 of them on nuxt.com — and it is a
declaration, not an inference, exactly like the Next manifest: the only way it is wrong is if the build
is wrong about itself. `matcher` holds the route rules (`routeRules` in nuxt.config) keyed by path
pattern, which is how an app states that `/api/**` is proxied or `/admin/**` is server-only. It is
absent on an app that configures none.

The rule PATTERNS are routes too, and mostly different ones: of unjs.io's 66 rules, 64 name paths that
are not in `prerendered`, and each answers 302 where an invented path answers 404. They are reported
separately as `rule_routes` because "the build rendered this" and "the build declared a rule matching
this" are different claims, and collapsing them would hide which is which.

THE BASE IS READ FROM THE DOCUMENT. Nuxt's assets directory defaults to `/_nuxt/` but `buildAssetsDir`
and `app.baseURL` both move it, and an app behind a CDN moves it to another host — the same trap that
cost 293 routes on Supabase when the Next manifest URL was constructed from the origin. So the base is
whatever prefix of a URL the document itself references ends at `/_nuxt/`.

LIMIT, MEASURED: `prerendered` lists PRE-RENDERED routes, so a fully server-rendered Nuxt app with no
prerendering declares an empty list even though it has routes, and an app built with
`experimental.appManifest: false` serves no manifest at all (www.gitlab.com is Nuxt and has no readable
`latest.json`). Both report as unavailable with the reason, rather than as zero routes.
"""
import argparse
import json
import os
import sys
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jshound.fetch import document_refs, http_get                       # noqa: E402

ASSETS_DIR = "/_nuxt/"


def assets_base(doc_url, html):
    """The build assets base, taken from a URL the document references. "" when there is none."""
    for u in document_refs(doc_url, html):
        sp = urlsplit(u)
        i = sp.path.find(ASSETS_DIR)
        if i != -1:
            return f"{sp.scheme}://{sp.netloc}{sp.path[:i + len(ASSETS_DIR)]}"
    return ""


def _json(url, timeout):
    """(data, reason, status). `reason` distinguishes the ways this fails, because they mean
    different things about the target: a 401/403 is a WAF between you and a manifest that probably
    exists, a 404 is a build that does not serve one, and valid-status-but-not-JSON is a static host
    answering a miss with the SPA's index.html — which is why the status code is not trusted alone.
    """
    st, body = http_get(url, timeout=timeout)
    if not body:
        return None, f"unreachable (status={st})", st
    try:
        return json.loads(body), None, st
    except ValueError:
        if st in (401, 403):
            return None, f"blocked (status={st})", st
        return None, f"no manifest here (status={st}, response was not JSON)", st


def read(doc_url, timeout=30):
    """Returns the manifest plus `routes`/`route_rules`, or `available: False` with a reason."""
    # The FINAL url after redirects: relative references in the markup resolve against where the
    # document actually came from, not where it was asked for.
    st, html, doc_url = http_get(doc_url, timeout=timeout, with_url=True)
    if not html:
        return {"available": False, "reason": f"document unreachable (status={st})",
                "document": doc_url}
    base = assets_base(doc_url, html)
    if not base:
        return {"available": False, "is_nuxt": False,
                "reason": f"not a Nuxt app (document references no {ASSETS_DIR} assets)",
                "document": doc_url}

    latest, err, st = _json(base + "builds/latest.json", timeout)
    if latest is None:
        why = ("a request in front of the app is blocking it" if st in (401, 403)
               else "an app built with experimental.appManifest:false serves none")
        return {"available": False, "is_nuxt": True, "assets_base": base, "status": st,
                "reason": f"Nuxt app, but builds/latest.json {err} — {why}",
                "document": doc_url}
    bid = latest.get("id")
    if not bid:
        return {"available": False, "is_nuxt": True, "assets_base": base,
                "reason": "builds/latest.json declares no build id", "document": doc_url}

    meta, err, st = _json(f"{base}builds/meta/{bid}.json", timeout)
    if meta is None:
        return {"available": False, "is_nuxt": True, "assets_base": base, "build_id": bid,
                "status": st, "reason": f"builds/meta/{bid}.json {err}", "document": doc_url}

    routes = sorted({r for r in (meta.get("prerendered") or []) if isinstance(r, str)})
    m = meta.get("matcher") or {}
    rules = []
    for kind in ("static", "wildcard", "dynamic"):
        for pattern, rule in (m.get(kind) or {}).items():
            rules.append({"kind": kind, "pattern": pattern, "rule": rule})
    # A route rule's PATTERN is itself a requestable path, and mostly not a prerendered one. On
    # unjs.io 64 of the 66 rules name paths absent from `prerendered`, and each one answers 302 where
    # an invented path answers 404 — so these are 64 real routes, verified, that the prerendered list
    # alone misses. Kept in their own field: a prerendered route was rendered, a rule pattern was
    # merely declared to match, and the two are not the same claim.
    rule_routes = sorted({r["pattern"] for r in rules
                          if isinstance(r["pattern"], str) and r["pattern"].startswith("/")}
                         - set(routes))
    return {"available": True, "is_nuxt": True, "document": doc_url, "assets_base": base,
            "build_id": bid, "timestamp": meta.get("timestamp"),
            "routes": routes, "route_rules": rules, "rule_routes": rule_routes,
            "manifest_urls": [base + "builds/latest.json", f"{base}builds/meta/{bid}.json"]}


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
        print(f"  no app manifest: {d.get('reason')}", file=sys.stderr)
        return 1
    print(f"  assets base: {d['assets_base']}  build {d['build_id']}", file=sys.stderr)
    print(f"  DECLARED BY THE BUILD: {len(d['routes'])} prerendered routes + "
          f"{len(d['rule_routes'])} from route rules · {len(d['route_rules'])} rules total",
          file=sys.stderr)
    for r in d["routes"] + d["rule_routes"]:
        print(r)
    for r in d["route_rules"]:
        print(f"# routeRule [{r['kind']}] {r['pattern']} -> {json.dumps(r['rule'])[:120]}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
