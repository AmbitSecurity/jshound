#!/usr/bin/env python3
"""fromhar.py: turn a HAR export into a bodies directory `analyze` can read.

    jshound from-har session.har -o ./bodies

The honest answer to every target `fetch` cannot reach. `fetch` does not run JavaScript, so an app
that injects its own script tags, or sits behind a login, or needs a real browser to get past a WAF,
gives up almost nothing, Asana ships no `<script src>` at all. Rather than grow a browser inside
this tool, take the traffic from whatever already did the work:

    Chrome/Firefox DevTools -> Network -> right-click -> Save all as HAR
    Burp Suite -> Proxy history -> save (the HAR Importer's format)
    Caido -> export

Everything the browser loaded is in there, including the chunks it fetched after login, which is
surface no anonymous crawl reaches. The origin manifest is rebuilt from each entry's own URL and
its `Referer`, so endpoints are still attributed to the host that served the code rather than to
whichever CDN happened to deliver it.

Only JavaScript and HTML entries are written; a HAR of a real session is mostly images.
"""
import argparse
import base64
import hashlib
import json
import os
import sys

_JS = ("javascript", "ecmascript", "text/jsx")
_HTML = ("text/html", "application/xhtml")

# CAPTURED RESPONSE BODIES, which `analyze`'s `json_records()` exists to mine and had no way of
# receiving. That function is explicit about its value, "61 paths on one scope and 53 on another that
# no other channel produced", and it reads files ending `.json` or `.rsc`. Nothing in this repo wrote
# one: the fetcher only saves scripts, and this importer accepted only JS and HTML, so a HAR's API
# responses were thrown away at the door.
#
# That is the wrong thing to drop here of all places. A HAR is how you reach an application behind a
# login, and a logged-in session's HAR is mostly API responses, the surface that matters most and the
# only kind this project has no measured numbers for. These are responses RECEIVED, so reading one
# infers nothing.
#
# `+json` is matched as well as `application/json`, because real APIs ship
# `application/vnd.api+json`, `application/problem+json` and friends. `text/x-component` is Next's RSC
# content type.
_JSON = ("application/json", "text/json", "+json")
_RSC = ("text/x-component",)


def _is_wanted(mime, url):
    m = (mime or "").lower()
    if any(x in m for x in _JS) or any(x in m for x in _HTML):
        return True
    if any(x in m for x in _JSON) or any(x in m for x in _RSC):
        return True
    return url.split("?")[0].endswith((".js", ".mjs", ".cjs", ".json", ".rsc"))


def _ext(mime, url):
    """The extension that decides which reader sees this body.

    It matters that this is right rather than merely plausible: a `.json` body handed to Babel as `.js`
    is a parse error and the whole batch's worth of work is lost, which is the failure the document
    parser's own comment records. `js_files()` reads `.js`/`.mjs`/`.rsc`; `json_records()` reads
    `.json`/`.rsc`.
    """
    m = (mime or "").lower()
    bare = url.split("?")[0]
    if "html" in m or bare.endswith((".html", ".htm")):
        return ".html"
    if any(x in m for x in _RSC) or bare.endswith(".rsc"):
        return ".rsc"
    if any(x in m for x in _JSON) or bare.endswith(".json"):
        return ".json"
    return ".js"


def _text(content):
    txt = content.get("text")
    if txt is None:
        return None
    if content.get("encoding") == "base64":
        try:
            return base64.b64decode(txt).decode("utf-8", "replace")
        except Exception:                                      # noqa: BLE001
            return None
    return txt


def convert(har_path, outdir):
    os.makedirs(outdir, exist_ok=True)
    # A HAR is an export from a browser or a proxy, so it is as trustworthy as whatever produced it.
    # `json.load` raises on a truncated export, and RecursionError on a deeply nested one, and neither
    # is worth a traceback when the answer is "this file is not a usable HAR".
    try:
        with open(har_path, encoding="utf-8", errors="replace") as fh:
            har = json.load(fh)
    except FileNotFoundError:
        sys.exit(f"from-har: no such file: {har_path}")
    except (json.JSONDecodeError, RecursionError, UnicodeDecodeError) as e:
        sys.exit(f"from-har: {har_path} is not readable JSON ({type(e).__name__})")
    if not isinstance(har, dict):
        sys.exit(f"from-har: {har_path} is JSON but not a HAR (expected an object with a `log` key)")
    entries = (har.get("log") or {}).get("entries") or []

    rows, written, skipped = [], 0, 0
    for e in entries:
        req, res = e.get("request") or {}, e.get("response") or {}
        url = req.get("url") or ""
        content = res.get("content") or {}
        if not url or not _is_wanted(content.get("mimeType"), url):
            continue
        body = _text(content)
        if not body:
            skipped += 1          # HARs saved without response bodies are the common failure
            continue
        ext = _ext(content.get("mimeType"), url)
        name = hashlib.sha1(url.encode()).hexdigest()[:20] + ext
        with open(os.path.join(outdir, name), "w", errors="replace") as fh:
            fh.write(body)
        # The DOCUMENT that loaded this script is what relative paths inside it resolve against.
        ref = next((h.get("value") for h in (req.get("headers") or [])
                    if (h.get("name") or "").lower() == "referer"), None)
        rows.append({"f": name, "u": url, "d": ref or url})
        written += 1

    with open(os.path.join(outdir, "_origin.jsonl"), "a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")

    print(f"{written} script/document(s) -> {outdir}", file=sys.stderr)
    if skipped:
        print(f"  ! {skipped} entr(ies) had no response body. Most exporters omit bodies unless you "
              f"ask: in DevTools tick 'Preserve log' and save with content.", file=sys.stderr)
    if not written:
        print("  ! nothing written. A HAR with no JavaScript in it analyses to nothing, which is "
              "not the same as a target with no API.", file=sys.stderr)
    return written


def main(argv=None):
    ap = argparse.ArgumentParser(prog="jshound from-har")
    ap.add_argument("har")
    ap.add_argument("-o", "--out", default="./bodies")
    a = ap.parse_args(argv)
    convert(a.har, a.out)


if __name__ == "__main__":
    main()
