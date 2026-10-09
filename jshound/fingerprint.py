#!/usr/bin/env python3
"""fingerprint.py: identify what built this app, and therefore what to expect from it.

    jshound fingerprint ./bodies

Every capability in this tool depends on the bundler, and each one hides things differently. Knowing
which you are looking at turns "this returned 3 endpoints" into either "that is the answer" or "that
is a gap", and that distinction is the whole difference between a recon tool and a number.

What is detected, and what each implies:

    webpack          a module registry and a runtime chunk-URL builder: `chunks` and `modules` both
                     work. The module-id mode matters and is reported separately.
    rspack           webpack-compatible output, often ESM (`__rspack_esm_id`); same handling.
    vite / rollup    flat ESM, no registry: `modules` finds nothing by design, while `chunks` reads
                     the literal `import()` specifiers and the `__vite__mapDeps` table.
    turbopack        no URL builder at all; the chunk list ships as a literal array.
    parcel / esbuild detected and reported; neither ships a registry this tool can walk.
    metro            React Native and RN Web. Contract extraction works; its `__d(f, id, deps)`
                     registry is not walked, and it ships a single bundle rather than chunks.

Frameworks are reported separately because they answer a different question, where routing lives,
not how code is packed. Remix and SvelteKit are both Vite underneath, so `chunks` behaves like Vite
on them while `routes` is a Next App Router concern only.

Module id modes, because each one broke registry detection differently on a real target:

    numeric          {134: fn}                              webpack's default
    named            {"src/vector/app.tsx": fn}             `moduleIds: "named"`, pnpm paths
    deterministic    {"++os": fn, "/23t": fn}               4-char base64 hashes

Reports what it SAW rather than guessing a version number, and says `unknown` when it saw nothing,
an unrecognised bundler is a reason to check the output by hand, not a reason to assume a failure.
"""
import argparse
import json
import os
import re
import sys
import subprocess
from html.parser import HTMLParser

from jshound.nodeenv import node_env, node_failure

# BUNDLER AND FRAMEWORK ARE DIFFERENT QUESTIONS, and conflating them made the output useless for
# deciding what to run. The bundler determines whether there is a module registry to walk and how
# chunk URLs are formed; the framework determines where routing lives. Remix is Vite underneath and
# SvelteKit is too, knowing only "sveltekit" tells you nothing about `chunks`, and knowing only
# "vite" tells you nothing about `routes`.
# ────────────────────────────── HTML: parsed, not matched ──────────────────────────────
#
# A framework announces itself in the served document, and it does so STRUCTURALLY: an element id, a
# data- attribute, the href a modulepreload points at. Searching the document as text finds the same
# names inside inline scripts, JSON blobs and comments, which is how `__NEXT_DATA__` appearing once
# inside a string got Grafana reported as a Next app. So the document is parsed and the signals are
# read off real tags and attributes.
class _DocSignals(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found = set()

    def handle_comment(self, data):
        # Fresh delimits each hydration island with a comment pair:
        #     <!--frsh:island:HelloBar:1:--> … <!--/frsh:island-->
        # A comment is part of the document's structure, not a string inside a bundle, so this is the
        # same kind of evidence as an element id, and it is the only marker a Fresh page carries that
        # names the framework outright.
        if "frsh:island" in (data or ""):
            self.found.add("fresh")

    def handle_starttag(self, tag, attrs):
        d = {k.lower(): (v or "") for k, v in attrs}

        # Ember names its own app in a config meta: <meta name="discourse/config/environment">,
        # <meta name="ember-website/config/environment">. The app name varies, the suffix does not.
        if tag == "meta" and d.get("name", "").endswith("/config/environment"):
            self.found.add("ember")

        # <script id="__NEXT_DATA__">, an element id, not a string in a bundle
        if tag == "script" and d.get("id") == "__NEXT_DATA__":
            self.found.add("next")

        # Gatsby mounts into `<div id="___gatsby">`, and that id appears exactly once in the document.
        # Same class of evidence as the Next one above: an element id the framework writes, not a name
        # that happens to occur in a bundle. Measured on gatsbyjs.com, which was otherwise reported as
        # webpack with no framework at all.
        if d.get("id") == "___gatsby":
            self.found.add("gatsby")

        # data-astro-*, ng-version, q:container are attributes the framework writes onto elements
        for k in d:
            if k.startswith("data-astro"):
                self.found.add("astro")
            elif k == "ng-version":
                self.found.add("angular")
            elif k.startswith("ng-") and tag in ("app-root", "html", "body"):
                self.found.add("angular")
            elif k.startswith("q:"):
                self.found.add("qwik")
            elif k == "data-sveltekit-preload-data" or k.startswith("data-svelte"):
                self.found.add("sveltekit")
            elif k.startswith("data-remix") or k.startswith("data-nuxt"):
                self.found.add("remix" if "remix" in k else "nuxt")

        # The path a script or preload points at is the build's own directory layout.
        url = d.get("src") or d.get("href") or ""
        for needle, name in (("/_next/", "next"), ("/_app/immutable/", "sveltekit"),
                             ("/_astro/", "astro"), ("/_nuxt/", "nuxt"),
                             ("/build/_assets/", "remix"), ("/@vite/", "vite"),
                             # Fresh (Deno) appends a build cache key to every asset it serves:
                             # `/favicon.ico?__frsh_c=7e8f41a6...`. Its islands are marked in HTML
                             # COMMENTS rather than attributes, which `handle_comment` below reads.
                             ("__frsh_c=", "fresh")):
            if needle in url:
                self.found.add(name)

        if tag == "astro-island":
            self.found.add("astro")
        if tag == "app-root":
            self.found.add("angular")


def document_signals(path):
    """Framework names announced by the served document's structure."""
    try:
        src = open(path, encoding="utf-8", errors="replace").read(4_000_000)
    except OSError:
        return set()
    p = _DocSignals()
    try:
        p.feed(src)
    except Exception:                                        # noqa: BLE001
        pass                                                 # a partial parse still reports what it saw
    return p.found


# ────────────────────────────── JS: the AST, via node ──────────────────────────────
def js_signals(js_files):
    """-> ({signal: files}, parsed, failed, {file: [signals]}) from js/fingerprint.js."""
    if not js_files:
        return {}, 0, 0, {}
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "js", "fingerprint.js")
    env = {**node_env(),
           "NODE_PATH": os.environ.get("NODE_PATH") or os.path.join(
               os.path.dirname(here), "node_modules")}
    sig, per_file, parsed, failed = {}, {}, 0, 0
    for i in range(0, len(js_files), 60):                    # argv has limits; batch it
        batch = js_files[i:i + 60]
        try:
            r = subprocess.run(["node", "--max-old-space-size=4096", script, *batch],
                               capture_output=True, text=True, env=env, timeout=600)
        except Exception:                                    # noqa: BLE001
            failed += len(batch)
            continue
        # `json.loads(r.stdout or "{}")` added nothing to EITHER counter when the helper crashed, so
        # a missing `@babel/parser` reported `js_parsed: 0, js_parse_failures: 0`, no files read and
        # nothing wrong with any of them.
        if node_failure(r):
            failed += len(batch)
            continue
        d = json.loads(r.stdout or "{}")
        for k, v in (d.get("signals") or {}).items():
            sig[k] = sig.get(k, 0) + v
        per_file.update(d.get("perFile") or {})
        parsed += d.get("parsed", 0)
        failed += d.get("failed", 0)
    return sig, parsed, failed, per_file


_ESM = {"esm-module", "esm-dynamic-import"}
_BUNDLER_NAMES = {"webpack", "rspack", "turbopack", "vite", "rollup", "parcel", "esbuild",
                  "metro"}
_MODULE_MAP = re.compile(r"[{,]\s*(\d{1,7}|\"[^\"]{1,200}\"|'[^']{1,200}')\s*:\s*(?:function|\()")


def _id_mode(keys):
    if not keys:
        return None
    numeric = sum(1 for k in keys if k.isdigit())
    pathy = sum(1 for k in keys if ("/" in k or k.endswith((".js", ".ts", ".tsx", ".vue"))))
    short = sum(1 for k in keys if 2 <= len(k) <= 8 and not k.isdigit())
    n = len(keys)
    if numeric / n >= 0.8:
        return "numeric"
    if pathy / n >= 0.5:
        return "named (source paths)"
    if short / n >= 0.8:
        return "deterministic (hashed)"
    return "mixed"


def _host_of(bodies):
    """-> ({saved filename: serving host}, document host) from the crawler's origin manifest."""
    q = os.path.join(bodies, "_origin.jsonl")
    if not os.path.exists(q):
        return {}, None
    import urllib.parse as _u
    by_file, doc = {}, None
    for line in open(q, errors="replace"):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("f") and r.get("u"):
            by_file[r["f"]] = _u.urlsplit(r["u"]).netloc.lower()
        if doc is None and r.get("d"):
            doc = _u.urlsplit(r["d"]).netloc.lower()
    return by_file, doc


def scan(bodies):
    # `os.walk` on a path that is not there yields nothing, so a mistyped directory, or a URL, which
    # is the easy mistake because every other fetching command takes one, reported `files_scanned: 0`
    # and a null bundler. That reads as "this app has no detectable stack", which is a claim about the
    # target made from a typo.
    if not os.path.isdir(bodies):
        what = ("a URL. This reads a directory of already-fetched files; "
                "run `jshound <url>` first, which saves them under .jshound/<host>/js"
                if "://" in str(bodies) else "not a directory")
        raise SystemExit(f"fingerprint: {bodies!r} is {what}")
    js_files, docs, id_keys = [], [], []
    for root, _d, fs_ in os.walk(bodies):
        for f in sorted(fs_):
            p = os.path.join(root, f)
            if f.endswith((".js", ".mjs", ".cjs")):
                js_files.append(p)
            elif f.endswith((".html", ".htm")):
                docs.append(p)

    # A PAGE LOADS OTHER PEOPLE'S CODE, AND YOU CANNOT TELL BY HOSTNAME. tldraw is a Vite app that
    # bundles Clerk's auth SDK, which is webpack, and Clerk is served from clerk.tldraw.com, the
    # app's own subdomain. Excluding "third-party" hosts therefore cannot work, and trying it broke
    # Grafana, whose own assets live on grafana-assets.grafana.net while its document is
    # play.grafana.org: a different registrable domain entirely.
    #
    # So nothing is excluded. Every signal is counted, and WHICH HOST carried it is reported beside
    # it, enough to see that tldraw's webpack is all on one auth subdomain, without the tool
    # silently deciding whose code matters.
    host_of, doc_host = _host_of(bodies)
    sig, parsed, failed, per_file = js_signals(js_files)
    by_host = {}
    for pth, names in per_file.items():
        h = host_of.get(os.path.basename(pth))
        if not h:
            continue
        for k in names:
            if k in _BUNDLER_NAMES:
                by_host.setdefault(k, {}).setdefault(h, 0)
                by_host[k][h] += 1
    doc_found = set()
    for d in docs:
        doc_found |= document_signals(d)

    # Module id mode still reads the text, and says so: it is a shape question about keys rather
    # than a claim about what is running, and a wrong answer here mislabels nothing.
    for p in js_files[:40]:
        try:
            src = open(p, encoding="utf-8", errors="replace").read(3_000_000)
        except OSError:
            continue
        for m in _MODULE_MAP.finditer(src):
            k = m.group(1).strip("\"'")
            if k:
                id_keys.append(k)
        if len(id_keys) > 4000:
            break

    bundlers = sorted(((k, v) for k, v in sig.items() if k in _BUNDLER_NAMES),
                      key=lambda kv: -kv[1])
    js_frameworks = sorted(((k, v) for k, v in sig.items()
                            if k not in _BUNDLER_NAMES and k not in _ESM), key=lambda kv: -kv[1])

    # THE DOCUMENT DECIDES, EXCEPT WHEN THERE IS NOTHING IN IT. The document rule exists to stop
    # vendored code being crowned: Grafana bundles AngularJS for its plugin system, 10 files of 163,
    # and a count-based rule called it an Angular app. But a client-rendered Angular app writes
    # NOTHING into the served document, `ng-version` is added during hydration, so that rule alone
    # can never name it. A signal carried by a fifth of the corpus is the application itself, not
    # something it bundles; 6% is not.
    primary_f = sorted(doc_found)[0] if doc_found else None
    basis = "served document" if doc_found else None
    if not primary_f and js_frameworks and parsed:
        top, count = js_frameworks[0]
        if count / parsed >= 0.2:
            primary_f, basis = top, f"javascript ({count}/{parsed} files)"
    if not basis:
        basis = "no document parsed" if not docs else "no framework marker in any document"

    return {
        "files_scanned": len(js_files) + len(docs),
        "document_host": doc_host,
        "bundler_hosts": {k: sorted(v.items(), key=lambda kv: -kv[1])
                          for k, v in sorted(by_host.items())},
        "js_parsed": parsed, "js_parse_failures": failed,
        # A clean ESM build leaves no runtime marker at all: Rollup and esbuild emit plain modules,
        # so remix.run, astro.build and angular.dev carry 112, 5 and 25 ESM files respectively and
        # not one bundler global between them. Reporting None there reads as a detection failure
        # when it is the correct answer, so say which it is.
        "bundler": (bundlers[0][0] if bundlers else
                    ("esm (no bundler runtime marker)" if sig.get("esm-module") else None)),
        "bundlers": [{"name": n, "files": c} for n, c in bundlers],
        # The document decides the framework; a global seen only in JS is listed, not crowned.
        "framework": primary_f,
        "framework_basis": basis,
        "frameworks_in_document": sorted(doc_found),
        "frameworks_in_js_only": [{"name": n, "files": c} for n, c in js_frameworks],
        "esm_output": sig.get("esm-module", 0),
        "dynamic_imports": sig.get("esm-dynamic-import", 0),
        "module_id_mode": _id_mode(id_keys),
        "module_ids_sampled": len(id_keys),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(prog="jshound fingerprint")
    ap.add_argument("bodies")
    ap.add_argument("-o", "--out")
    a = ap.parse_args(argv)
    res = scan(a.bodies)
    payload = json.dumps(res, indent=2)
    if a.out:
        open(a.out, "w").write(payload)
    else:
        print(payload)
    if not res["bundlers"] and not res["framework"]:
        print("  no bundler signature found, check the output by hand rather than assuming a "
              "failure.", file=sys.stderr)


if __name__ == "__main__":
    main()
