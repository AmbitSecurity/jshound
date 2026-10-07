#!/usr/bin/env python3
"""fetch.py — collect a page's JavaScript so `analyze` has something to read.

    jshound fetch https://app.example.com -o ./bodies

The deliberately small one. `analyze` does not fetch, for good reasons recorded in its own
docstring, but a standalone tool still needs a way to get started — so this does the simplest honest
version: load the document, take the `<script src>` tags and inline scripts, save them, and write
the `_origin.jsonl` that lets analysis attribute an endpoint to the host that serves it.

It is NOT a crawler. It does not render, click, log in, follow links or execute anything, so a page
that builds its script tags at runtime will give up less than a real browser would. When that
matters — and on a serious target it does — drive a real crawler or export from your proxy, point
this at nothing, and run `analyze` over what that produced. The `--chunks` pass closes part of the
gap by asking each bundle which lazy chunks it declares and fetching those too, which is where most
of a code-split app's code lives.
"""
import argparse
import hashlib
import json
import os
import re
import sys
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# AN SPA ANSWERS 200 TO EVERYTHING. Asking excalidraw.com for a chunk that does not exist returns
# index.html with a 200, and saving that as `.js` put 46 copies of the same document into a corpus
# of 102 — polluting the analysis, inflating the fetched count, and making a 404 look like a hit.
# A JavaScript response does not begin with a tag.
_LOOKS_HTML = re.compile(r"^\s*(<!doctype|<html|<\?xml)", re.I)


def is_javascript(body, content_type=""):
    if "html" in (content_type or "").lower():
        return False
    return not _LOOKS_HTML.match(body or "")


def http_get(url, timeout=30, with_url=False):
    """(status, text), or (status, text, final_url) with `with_url`.

    curl_cffi when installed — its TLS fingerprint gets past more WAFs.

    `final_url` is the URL AFTER redirects, and callers that resolve relative references against a
    document must use it. kit.svelte.dev redirects to svelte.dev and its markup links `./_app/...`;
    resolving that against the requested URL built `kit.svelte.dev/_app/...`, which answers with an
    error page, and the route table was reported as a parse failure on an app that has one.
    """
    try:
        from curl_cffi import requests as rq
    except ImportError:
        pass
    else:
        # Any transport failure — DNS, TLS, connection reset, timeout — is a normal outcome when
        # pointing at the open internet, not a reason to abort the run with a traceback.
        try:
            r = rq.get(url, impersonate="chrome", timeout=timeout)
            final = str(getattr(r, "url", "") or url)
            return (r.status_code, r.text, final) if with_url else (r.status_code, r.text)
        except Exception:                                  # noqa: BLE001
            return (None, "", url) if with_url else (None, "")
    if True:
        import urllib.error
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as fh:
                body = fh.read().decode("utf-8", "replace")
                final = fh.geturl() or url
                return (fh.status, body, final) if with_url else (fh.status, body)
        except urllib.error.HTTPError as e:
            return (e.code, "", url) if with_url else (e.code, "")
        except Exception:                                   # noqa: BLE001
            return (None, "", url) if with_url else (None, "")


class _Scripts(HTMLParser):  # noqa: D101 — docstring below
    """`<script src>`, inline scripts, and the preload hints modern builds use instead.

    Vite and friends emit `<link rel="modulepreload" href="/main.js">` with no `<script src>` for
    those modules at all — Home Assistant's whole frontend loads that way, and reading only script
    tags found none of it.
    """

    def __init__(self):
        super().__init__()
        self.srcs, self.inline, self._in = [], [], False
        self.origin = {}          # url -> how it was declared

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "link":
            rel = (d.get("rel") or "").lower()
            href = d.get("href") or ""
            if href and ("modulepreload" in rel or
                         (rel == "preload" and (d.get("as") or "").lower() == "script")):
                self.srcs.append(href)
                self.origin.setdefault(href, "modulepreload")
            return
        if tag != "script":
            return
        if d.get("src"):
            self.srcs.append(d["src"])
            self.origin.setdefault(d["src"], "script-src")
        else:
            self._in = True

    def handle_endtag(self, tag):
        if tag == "script":
            self._in = False

    def handle_data(self, data):
        if self._in and data.strip():
            self.inline.append(data)


class _Refs(HTMLParser):
    """Every `src`/`href` on a `<script>` or `<link>`, unfiltered by `rel`.

    Separate from `_Scripts` above, which decides what is WORTH FETCHING as JavaScript and so filters
    on `rel`. This one is for callers that select on the URL itself — the manifest readers look for an
    exact filename, and builds link those files as `<script src>`, as `<link rel=preload as=script>`
    and as `rel=prefetch` depending on the version.
    """

    def __init__(self):
        super().__init__()
        self.refs = []

    def handle_starttag(self, tag, attrs):
        if tag not in ("script", "link"):
            return
        d = dict(attrs)
        u = d.get("src") or d.get("href")
        if u:
            self.refs.append(u)


def document_imports(doc_url, html):
    """Absolute URLs of modules the document's INLINE scripts import.

    SvelteKit's bootstrap is an inline script that does
    `Promise.all([import("./_app/immutable/entry/start.X.js"), import("./_app/immutable/entry/app.Y.js")])`,
    so the entry module appears nowhere as a `src` or `href`. Reading only attributes reported four
    SvelteKit apps as "not SvelteKit"; this is what a browser would follow.
    """
    p = _Scripts()
    try:
        p.feed(html or "")
    except Exception:                                      # noqa: BLE001
        pass
    return inline_imports(doc_url, p.inline)


def inline_imports(doc_url, sources):
    """The same thing, for a caller that has already parsed the document.

    Two things keep this cheap on a server-rendered page, which can carry thousands of inline scripts.
    A source with no `import` token anywhere cannot contain an import — the keyword is required for one
    to exist — so it is skipped before anything is written, and the scratch directory is removed when
    the call returns instead of being left behind once per document.
    """
    candidates = [src for src in (sources or []) if "import" in src]
    if not candidates:
        return []
    import tempfile
    out, seen = [], set()
    with tempfile.TemporaryDirectory(prefix="docimports-") as d:
        paths = []
        for i, src in enumerate(candidates):
            fp = os.path.join(d, f"inline{i}.js")
            with open(fp, "w", errors="replace") as fh:
                fh.write(src)
            paths.append(fp)
        for specs in js_imports(sorted(paths)).values():
            for spec in specs:
                if not spec.startswith((".", "/", "http:", "https:", "//")):
                    continue
                u = urljoin(doc_url, spec)
                if u not in seen:
                    seen.add(u)
                    out.append(u)
    return out


def document_refs(doc_url, html):
    """Absolute URLs of everything the document links, in document order, duplicates kept.

    Exists because the one lesson that keeps repeating is DO NOT CONSTRUCT THE URL. An app with an
    `assetPrefix` or a custom `buildAssetsDir` serves its bundle somewhere the origin does not imply,
    and the document already says where.
    """
    p = _Refs()
    try:
        p.feed(html or "")
    except Exception:                                      # noqa: BLE001 — malformed markup is normal
        pass
    return [urljoin(doc_url, r) for r in p.refs]


def js_imports(paths):
    """{path: [module specifier, ...]} by PARSING each file — see js/imports.js.

    Replaces a regex that both invented specifiers out of comments and strings and missed any that did
    not end in `.js`. Measured on one file holding every import form: 2 invented, 2 missed.
    """
    if not paths:
        return {}
    js = os.path.join(os.path.dirname(os.path.abspath(__file__)), "js", "imports.js")
    env = dict(os.environ)
    env.setdefault("NODE_PATH", os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "node_modules"))
    out = {}
    import subprocess
    for i in range(0, len(paths), 40):            # batched so one huge file cannot blow the heap
        try:
            r = subprocess.run(["node", "--max-old-space-size=4096", js] + list(paths[i:i + 40]),
                               capture_output=True, env=env, timeout=180)
            for k, v in (json.loads(r.stdout or b"{}")).items():
                out[k] = (v or {}).get("specifiers") or []
        except Exception:                          # noqa: BLE001 — a parse batch failing is not fatal
            pass
    return out


def _name(url):
    return hashlib.sha1(url.encode()).hexdigest()[:20] + ".js"


def collect(url, outdir, with_chunks=True, max_chunks=400, timeout=30):
    os.makedirs(outdir, exist_ok=True)
    status, html = http_get(url, timeout)
    if not html:
        print(f"  ! {url} returned {status} with no body", file=sys.stderr)
        return 0
    p = _Scripts()
    p.feed(html)

    from collections import Counter
    how = Counter()
    rows, saved = [], 0
    hname = hashlib.sha1(url.encode()).hexdigest()[:20] + ".html"
    open(os.path.join(outdir, hname), "w", errors="replace").write(html)
    rows.append({"f": hname, "u": url, "d": url})

    for i, src in enumerate(p.inline):
        n = f"inline_{i}_{hashlib.sha1(src.encode()).hexdigest()[:12]}.js"
        open(os.path.join(outdir, n), "w", errors="replace").write(src)
        rows.append({"f": n, "u": url, "d": url})
        saved += 1
        how["inline"] += 1

    # WHERE EACH FILE CAME FROM. Angular's esbuild build declares its chunks as modulepreload links
    # and ships no JS chunk table at all, so `chunks` correctly reports 0 while 21 of 27 files are
    # chunks — a total alone makes that look like a failure. Counting by discovery path is what
    # distinguishes "this build has no chunk table" from "we did not find it", and the counts must
    # sum to the total or the breakdown is just decoration.
    seen = set()
    # An inline script's dynamic `import()` is a real fetch the browser performs, and on SvelteKit it
    # is the ONLY reference to the entry module — nothing links it as a `src`. Counted under its own
    # discovery path so the breakdown still sums to the total.
    bootstrap = [u for u in inline_imports(url, p.inline) if u.startswith("http")]
    todo = [urljoin(url, s) for s in p.srcs] + bootstrap
    from_bootstrap = set(bootstrap)
    for u in todo:
        if u in seen or not u.startswith("http"):
            continue
        seen.add(u)
        st, body = http_get(u, timeout)
        if st != 200 or not body or not is_javascript(body):
            continue
        n = _name(u)
        open(os.path.join(outdir, n), "w", errors="replace").write(body)
        rows.append({"f": n, "u": u, "d": url})
        saved += 1
        raw = next((k for k in p.origin if urljoin(url, k) == u), None)
        if raw is None and u in from_bootstrap:
            how["inline-import"] += 1
        else:
            how[p.origin.get(raw, "script-src")] += 1

    # STATIC ES IMPORTS. An entry module built by Vite/Rollup is often a few lines of re-exports;
    # everything real is in the modules it imports, and those are named right there in the source.
    # Bounded to one extra round so this stays a fetcher and does not become a crawler by accident.
    if with_chunks and seen:
        imports = set()
        by_path = {}
        for u in list(seen):
            fp = os.path.join(outdir, _name(u))
            if os.path.exists(fp):
                by_path[fp] = u
        for fp, specs in js_imports(sorted(by_path)).items():
            u = by_path.get(fp)
            if not u:
                continue
            for spec in specs:
                # A bare or scoped package name is a build-time specifier with no URL to resolve.
                if not spec.startswith((".", "/", "http:", "https:", "//")):
                    continue
                tgt = urljoin(u, spec)
                if tgt not in seen:
                    imports.add(tgt)
        for u in sorted(imports)[:max_chunks]:
            if u in seen:
                continue
            seen.add(u)
            st, body = http_get(u, timeout)
            if st != 200 or not body or not is_javascript(body):
                continue
            n = _name(u)
            open(os.path.join(outdir, n), "w", errors="replace").write(body)
            rows.append({"f": n, "u": u, "d": url})
            saved += 1
            how["es-import"] += 1
        if imports:
            print(f"  {len(imports)} static ES import(s) followed", file=sys.stderr)

    # CHUNK ENUMERATION IS A FIXPOINT, NOT A PASS.
    #
    # A bundle that declares chunks can itself be a chunk. Measured on Grafana: the runtime declares
    # 694, and two of the files beside it declare 6 more between them — a single pass over the
    # initially-loaded scripts finds 694 and stops, and nobody is told the other 6 exist. So each
    # round re-scans everything newly written and keeps going until a round adds nothing, which is
    # the only terminating condition that can honestly claim to be complete.
    #
    # Resolution of a chunk's base stays ambiguous (Grafana's live on a CDN, Element's are
    # root-relative with an empty publicPath), so every candidate is tried and whichever answers 200
    # wins. The counts of declared / fetched / unreachable are reported, because "declared 694,
    # fetched 681, 13 gone" is the difference between enumeration working and enumeration lying.
    declared, fetched_ok, unreachable, already, rounds = set(), 0, 0, 0, 0
    if with_chunks and seen:
        from . import chunk_map
        page_base = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}"
        scanned, frontier = set(), [u for u in seen]
        while frontier and fetched_ok < max_chunks:
            rounds += 1
            candidates = {}
            for u in frontier:
                if u in scanned:
                    continue
                scanned.add(u)
                fp = os.path.join(outdir, _name(u))
                if not os.path.exists(fp):
                    continue
                try:
                    res = chunk_map(open(fp, errors="replace").read())
                except Exception:                           # noqa: BLE001
                    continue
                pub = res.get("publicPath") or ""
                for c in (res.get("chunks") or []):
                    if c.startswith("http"):
                        cands = [c]
                    else:
                        cands = []
                        if pub.startswith("/") or pub.startswith("http"):
                            cands.append(urljoin(page_base, urljoin(pub, c)))
                        cands.append(urljoin(u, c))
                        cands.append(urljoin(page_base + "/", c.lstrip("/")))
                    key = cands[0]
                    if key not in candidates:
                        candidates[key] = cands
            frontier = []
            for key, cands in sorted(candidates.items()):
                if fetched_ok >= max_chunks:
                    break
                if key in declared:
                    continue
                declared.add(key)
                got = False
                for cu in cands:
                    if cu in seen:
                        got = True
                        already += 1          # already pulled in by an earlier pass
                        break
                    seen.add(cu)
                    st, body = http_get(cu, timeout)
                    if st != 200 or not body or not is_javascript(body):
                        continue      # an SPA catch-all is a 404 wearing a 200
                    n = _name(cu)
                    open(os.path.join(outdir, n), "w", errors="replace").write(body)
                    rows.append({"f": n, "u": cu, "d": url})
                    saved += 1
                    how["chunk-table"] += 1
                    fetched_ok += 1
                    frontier.append(cu)      # a chunk can declare chunks: keep going
                    got = True
                    break
                if not got:
                    unreachable += 1
        if declared:
            # Every declared chunk is accounted for, because an unexplained remainder is exactly
            # where a quiet failure hides: 142 declared and 20 fetched reads like a 86% miss until
            # you are told the other 119 were already in hand.
            print(f"  chunks: {len(declared)} declared over {rounds} round(s) — "
                  f"{fetched_ok} fetched, {already} already had, {unreachable} unreachable"
                  + (f", capped at --max-chunks {max_chunks}"
                     if fetched_ok >= max_chunks else ""), file=sys.stderr)

    with open(os.path.join(outdir, "_origin.jsonl"), "a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    detail = ", ".join(f"{v} {k}" for k, v in how.most_common())
    print(f"  {saved} script(s) -> {outdir}" + (f"  [{detail}]" if detail else ""),
          file=sys.stderr)
    return saved


def main(argv=None):
    ap = argparse.ArgumentParser(prog="jshound fetch")
    ap.add_argument("url")
    ap.add_argument("-o", "--out", default="./bodies")
    ap.add_argument("--no-chunks", action="store_true")
    ap.add_argument("--max-chunks", type=int, default=400)
    a = ap.parse_args(argv)
    collect(a.url, a.out, not a.no_chunks, a.max_chunks)


if __name__ == "__main__":
    main()
