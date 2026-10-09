#!/usr/bin/env python3
"""fetch.py: collect a page's JavaScript so `analyze` has something to read.

    jshound fetch https://app.example.com -o ./bodies

The deliberately small one. `analyze` does not fetch, for good reasons recorded in its own
docstring, but a standalone tool still needs a way to get started, so this does the simplest honest
version: load the document, take the `<script src>` tags and inline scripts, save them, and write
the `_origin.jsonl` that lets analysis attribute an endpoint to the host that serves it.

It is NOT a crawler. It does not render, click, log in, follow links or execute anything, so a page
that builds its script tags at runtime will give up less than a real browser would. When that
matters, and on a serious target it does, drive a real crawler or export from your proxy, point
this at nothing, and run `analyze` over what that produced. The `--chunks` pass closes part of the
gap by asking each bundle which lazy chunks it declares and fetching those too, which is where most
of a code-split app's code lives.
"""
import argparse
import functools
import hashlib
import json
import os
import re
import sys
import ipaddress
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from jshound.nodeenv import node_env

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# AN SPA ANSWERS 200 TO EVERYTHING. Asking excalidraw.com for a chunk that does not exist returns
# index.html with a 200, and saving that as `.js` put 46 copies of the same document into a corpus
# of 102, polluting the analysis, inflating the fetched count, and making a 404 look like a hit.
# A JavaScript response does not begin with a tag.
_LOOKS_HTML = re.compile(r"^\s*(<!doctype|<html|<\?xml)", re.I)


def is_javascript(body, content_type=""):
    if "html" in (content_type or "").lower():
        return False
    return not _LOOKS_HTML.match(body or "")


# AN UPSTREAM PROXY, for the whole run. Set by the CLI, read by every request this module makes.
#
# What it is for, measured rather than assumed: rate limits, geofences and IP bans. What it is NOT
# for is a bot-protection challenge. Through a residential exit in Riyadh on Saudi Telecom, claude.ai,
# lovable.dev and tesla.com returned byte-for-byte the same 403 they return from a datacenter VPN in
# New York, because "Just a moment..." and Akamai's Bot Manager are decided by whether the client
# runs JavaScript, and no HTTP client does, whatever its IP or its TLS fingerprint.
#
# `PROXY_CA` is the proxy's own root, for one that intercepts TLS. Bright Data does on port 33335.
PROXY = None
PROXY_CA = None

# WHICH BROWSER TO LOOK LIKE, and what to try when that one is refused.
#
# A CURRENT ONE. On 2026-10-09, from one datacenter IP, claude.ai served curl_cffi's Chrome 150 the
# real page 11 times in 12 and challenged its Chrome 142 all 9 times (on two curl_cffi releases),
# and a real Chrome 154 got the real page both times it was tried. The list this replaced, Safari 17
# and 18, Firefox 144, Chrome 131 and 142, was a year out of date by then, and every profile in it
# was challenged. Two likely reasons, both of which get worse with age: the
# browsers people run update every four weeks, so an old version is rare in real traffic, and an
# old curl_cffi default is what a year of bots has been sending.
#
# So the list is not written down here. It is curl_cffi's own newest Chrome, Firefox and Safari,
# which its maintainers move forward each release, and `pip install -U curl_cffi` is the update.
#
# No single profile is safe to rely on either. On 2026-10-08 Safari 17 got into all sixteen of a set
# of sites while Chrome 142 got into thirteen, and across two runs ten minutes apart Chrome 131 and
# Firefox both got into claude.ai and then were challenged: a refusal is partly a reputation score
# that moves. So the document is retried down this list when it comes back refused, and whichever
# profile gets a real page is kept for everything else that target is asked for, because switching
# fingerprints mid-session is itself a signal and a challenge cookie is bound to the client that
# earned it.
def _newest_profiles():
    """curl_cffi's newest Chrome, Firefox and Safari, in that order."""
    try:
        from curl_cffi.requests import impersonate as _imp
    except Exception:                                      # noqa: BLE001  (absent: urllib is used)
        return ("chrome", "firefox", "safari")
    got = (getattr(_imp, n, None) for n in ("DEFAULT_CHROME", "DEFAULT_FIREFOX", "DEFAULT_SAFARI"))
    return tuple(dict.fromkeys(p for p in got if p)) or ("chrome", "firefox", "safari")


PROFILES = _newest_profiles()
IMPERSONATE = PROFILES[0]


def set_client(profile=None):
    """Pin one impersonation profile, or None to go back to rotating through PROFILES."""
    global IMPERSONATE, _PINNED
    _PINNED = bool(profile)
    IMPERSONATE = profile or PROFILES[0]


_PINNED = False

# EVERY REQUEST, AS IT HAPPENS. Called with (url, status, nbytes) after each GET this module makes, so a
# caller can show what was fetched without this module deciding how. Set by the CLI; None means silent.
ON_REQUEST = None


def _report(url, status, nbytes):
    if ON_REQUEST is not None:
        try:
            ON_REQUEST(url, status, nbytes)
        except Exception:                                  # noqa: BLE001
            pass                                           # a display hook must never stop a fetch


def set_proxy(url=None, ca=None):
    """Route every request this module makes through `url`, trusting `ca` for its TLS."""
    global PROXY, PROXY_CA
    PROXY = url or None
    PROXY_CA = ca or None


# WHO SAID NO. A refused document used to be read as a page with no scripts in it, and the summary
# then explained that a server-rendered site can legitimately have no API, which is the wrong story
# for a 403. These are each vendor's own markers on its own block or challenge page. A marker only
# counts on a response that is an error or is the size of an interstitial, so a real app that merely
# loads one of these vendors' scripts is not mistaken for a block.
# Each vendor's markers in two kinds. STRONG ones appear only on a refusal or challenge page, so they
# count whatever the status. WEAK ones are the vendor's ordinary script tag or variable, which sits on
# real pages too: Cloudflare injects `/cdn-cgi/challenge-platform/scripts/...` into normal pages, and
# demo.home-assistant.io, a 6.5 KB SPA shell carrying that script, was reported as "blocked by
# Cloudflare challenge" and not read at all. A weak marker counts only on a refusal status, which
# includes the 202 AWS WAF answers a challenge with.
_BLOCK = (
    ("Cloudflare challenge", ("_cf_chl_opt", "<title>Just a moment", "challenge-platform/h/"),
     ("challenge-platform",)),
    ("Cloudflare block", ("cf-error-details", "Attention Required! | Cloudflare"), ()),
    # The challenge answers 200, so the status says nothing and none of the Akamai denial markers are
    # on it: tesla.com served this 2.5 KB page to the one profile that got past the 403s, and its two
    # obfuscated sensor scripts were read as the application.
    ("Akamai challenge", ("sec-if-cpt-container",), ()),
    ("Akamai", ("errors.edgesuite.net", "Reference&#32;&#35;"), ()),
    ("DataDome", ("geo.captcha-delivery.com",), ("datadome",)),
    ("PerimeterX", ("px-captcha",), ("_pxAppId",)),
    ("Imperva", ("Incapsula incident ID",), ("_Incapsula_Resource",)),
    ("AWS WAF", (), ("awswaf", "aws-waf-token")),
    ("Vercel", ("Vercel Security Checkpoint",), ()),
)

def blocked_by(status, html):
    """The vendor that refused the request, or None when this looks like a real response."""
    body = html or ""
    refused = status is not None and (status >= 400 or status == 202)
    # A full-size page with a 2xx is the application, whatever scripts it carries.
    if not refused and len(body) >= 12000:
        return None
    for vendor, strong, weak in _BLOCK:
        if any(m in body for m in strong) or (refused and any(m in body for m in weak)):
            return vendor
    if status in (401, 403, 429, 503) and len(body) < 4000:
        return f"HTTP {status}"
    return None


# ONE SESSION PER BROWSER PROFILE, the way a browser keeps its connections.
#
# Every request used to be a fresh client: a new DNS lookup, a new TCP and TLS handshake, no
# cookies. That is slow, and it is not what a browser looks like, since a browser loading a page
# and its scripts reuses one connection and sends back the cookies it was given. A curl_cffi
# Session shared across threads keeps a connection handle per thread, so the chunk fetches reuse
# connections, DNS answers and TLS sessions, and carry cookies like `__cf_bm`.
#
# AND NO IPv6 LOOKUP WHERE THERE IS NO IPv6. On a network without an IPv6 route, curl still asks
# DNS for an IPv6 address, and a resolver that never answers that question for a domain without
# one (Vercel's, for a start) makes every single request wait 10 seconds for it: svelte.dev took
# 258 seconds for 25 files, and 0.06s a request once the lookup was skipped. Chrome makes the same
# check before it asks for IPv6 at all, so this is a browser's behaviour, not a workaround.
_SESSIONS = {}
_SESSIONS_LOCK = __import__("threading").Lock()
_V6 = []


def ipv6_reachable():
    """Whether this machine has a route to the IPv6 internet. Asked once; sends no packets."""
    if not _V6:
        import socket
        ok = False
        try:
            s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
            try:
                s.connect(("2001:4860:4860::8888", 53))   # a route lookup, nothing is sent
                ok = True
            finally:
                s.close()
        except (OSError, AttributeError):
            ok = False
        _V6.append(ok)
    return _V6[0]


def _session(profile, max_bytes=None):
    """The shared session for one profile. With `max_bytes`, curl itself stops a larger download."""
    key = (profile, max_bytes)
    with _SESSIONS_LOCK:
        if key not in _SESSIONS:
            from curl_cffi import requests as rq
            from curl_cffi.const import CurlIpResolve, CurlOpt
            opts = {}
            if max_bytes:
                opts[CurlOpt.MAXFILESIZE_LARGE] = max_bytes
            if not ipv6_reachable():
                opts[CurlOpt.IPRESOLVE] = CurlIpResolve.V4
            _SESSIONS[key] = rq.Session(impersonate=profile, curl_options=opts or None)
        return _SESSIONS[key]


# A response is read up to this and no further. The size of a body is the target's choice, and one
# URL is enough: pointed at a generator, an uncapped read took 1,032 MB of RSS and wrote 524 MB into
# the corpus directory before anything looked at it. The largest real chunk measured across the
# corpora is under 12 MB, and a `.map` is the only thing that gets near this, which is why
# `sourcemaps.py` raises it rather than this being tight.
MAX_BODY = 64 * 1024 * 1024


def http_get(url, timeout=30, with_url=False, max_bytes=MAX_BODY):
    """(status, text), or (status, text, final_url) with `with_url`.

    curl_cffi when installed, its TLS fingerprint gets past more WAFs.

    `final_url` is the URL AFTER redirects, and callers that resolve relative references against a
    document must use it. kit.svelte.dev redirects to svelte.dev and its markup links `./_app/...`;
    resolving that against the requested URL built `kit.svelte.dev/_app/...`, which answers with an
    error page, and the route table was reported as a parse failure on an app that has one.

    Truncated at `max_bytes`. A truncated body is still returned, because a bundle cut off mid-file
    still parses to something and the alternative is losing the whole target to one oversized file.
    """
    try:
        from curl_cffi import requests as rq
    except ImportError:
        pass
    else:
        # Any transport failure, DNS, TLS, connection reset, timeout, is a normal outcome when
        # pointing at the open internet, not a reason to abort the run with a traceback.
        try:
            kw = {}
            if PROXY:
                kw["proxies"] = {"http": PROXY, "https": PROXY}
                if PROXY_CA:
                    kw["verify"] = PROXY_CA
            # A plain read, so the connection goes back to the session for the next request; curl
            # stops it at `max_bytes` (error 63). Only then is it read again as a stream, keeping the
            # first `max_bytes`: a bundle cut off mid-file still parses to something, and dropping
            # it would lose the whole target to one oversized file. A streamed read never reuses its
            # connection, which is why it is the exception and not the rule.
            try:
                r = _session(IMPERSONATE, max_bytes).get(url, timeout=timeout, **kw)
                raw = r.content
            except Exception as e:                         # noqa: BLE001
                if getattr(e, "code", None) != 63:
                    raise
                r = _session(IMPERSONATE).get(url, timeout=timeout, stream=True, **kw)
                try:
                    buf, n = [], 0
                    for part in r.iter_content():
                        buf.append(part)
                        n += len(part)
                        if n >= max_bytes:
                            break
                    raw = b"".join(buf)[:max_bytes]
                finally:
                    try:
                        r.close()
                    except Exception:                      # noqa: BLE001
                        pass
            enc = getattr(r, "encoding", None) or "utf-8"
            try:
                body = raw.decode(enc, "replace")
            except LookupError:
                body = raw.decode("utf-8", "replace")
            final = str(getattr(r, "url", "") or url)
            _report(url, r.status_code, len(raw))
            return (r.status_code, body, final) if with_url else (r.status_code, body)
        except Exception:                                  # noqa: BLE001
            _report(url, None, 0)
            return (None, "", url) if with_url else (None, "")
    if True:
        import urllib.error
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        opener = urllib.request.urlopen
        if PROXY:
            import ssl
            ctx = ssl.create_default_context(cafile=PROXY_CA) if PROXY_CA else None
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}),
                urllib.request.HTTPSHandler(context=ctx)).open
        try:
            with opener(req, timeout=timeout) as fh:
                raw = fh.read(max_bytes)
                body = raw.decode("utf-8", "replace")
                final = fh.geturl() or url
                _report(url, fh.status, len(raw))
                return (fh.status, body, final) if with_url else (fh.status, body)
        except urllib.error.HTTPError as e:
            _report(url, e.code, 0)
            return (e.code, "", url) if with_url else (e.code, "")
        except Exception:                                   # noqa: BLE001
            _report(url, None, 0)
            return (None, "", url) if with_url else (None, "")


# WHAT A TARGET IS ALLOWED TO MAKE THIS FETCH.
#
# Only the first URL is the user's. Everything after it, script srcs, `import()` specifiers, and
# every chunk URL built out of a bundle's own runtime, is named by the target, and a chunk URL that
# starts with `http` was fetched verbatim. So a page declaring
# `http://169.254.169.254/latest/meta-data/iam/security-credentials/` as one of its chunks had that
# fetched and written into the corpus directory, which is a credential read performed by whoever ran
# the tool, from inside their network.
#
# Pointing this at an internal or local app is normal use, though, so the rule is about WHO chose the
# address rather than which address it is: if the target the user named is itself private, private
# space is in scope for that run. `--allow-private` says so explicitly for the mixed case.
# Cached: a corpus run resolves the same two or three hostnames a few hundred times, and one lookup
# per chunk URL was most of the wall clock in the scope tests.
@functools.lru_cache(maxsize=4096)
def host_is_private(host):
    """True for loopback, link-local, RFC1918 and the other reserved ranges. Unresolvable is False,
    the transport will fail and say so, and guessing here would hide a real DNS problem."""
    if not host:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return True
    return False


class _Scripts(HTMLParser):  # noqa: D101, docstring below
    """`<script src>`, inline scripts, and the preload hints modern builds use instead.

    Vite and friends emit `<link rel="modulepreload" href="/main.js">` with no `<script src>` for
    those modules at all, Home Assistant's whole frontend loads that way, and reading only script
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
    on `rel`. This one is for callers that select on the URL itself, the manifest readers look for an
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
    A source with no `import` token anywhere cannot contain an import, the keyword is required for one
    to exist, so it is skipped before anything is written, and the scratch directory is removed when
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
    except Exception:                                      # noqa: BLE001, malformed markup is normal
        pass
    return [urljoin(doc_url, r) for r in p.refs]


def js_imports(paths, failed=None):
    """{path: [module specifier, ...]} by PARSING each file, see js/imports.js.

    Replaces a regex that both invented specifiers out of comments and strings and missed any that did
    not end in `.js`. Measured on one file holding every import form: 2 invented, 2 missed.

    Files are parsed 40 to a process. A batch that fails is retried one file at a time, so one file
    that crashes the parser costs that file and not the 39 beside it, and any file that still fails is
    appended to `failed` when the caller passes a list, rather than disappearing.
    """
    if not paths:
        return {}
    js = os.path.join(os.path.dirname(os.path.abspath(__file__)), "js", "imports.js")
    env = node_env()
    env.setdefault("NODE_PATH", os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "node_modules"))
    out = {}
    import subprocess

    def run(batch):
        r = subprocess.run(["node", "--max-old-space-size=4096", js] + list(batch),
                           capture_output=True, env=env, timeout=SCAN_TIMEOUT)
        if r.returncode != 0:
            raise RuntimeError(f"imports.js exited {r.returncode}")
        return {k: (v or {}).get("specifiers") or [] for k, v in json.loads(r.stdout or b"{}").items()}

    for i in range(0, len(paths), 40):            # batched so one huge file cannot blow the heap
        batch = list(paths[i:i + 40])
        try:
            out.update(run(batch))
        except Exception:                          # noqa: BLE001
            for one in batch:
                try:
                    out.update(run([one]))
                except Exception:                  # noqa: BLE001
                    if failed is not None:
                        failed.append(one)
    return out


def _chunk_map_or_none(path):
    """What one file declares, or None when it could not be read (and the caller counts that)."""
    from . import chunk_map
    try:
        return chunk_map(open(path, errors="replace").read())
    except Exception:                              # noqa: BLE001
        return None


def _aligned(declaring_url, chunk_path):
    """The chunk's URL, built by overlapping its path with the URL of the file that declared it.

    A relative chunk path is relative to the BUILD's asset root, which is neither the declaring
    file's directory nor the document's origin. Supabase's dashboard is the clearest case: the
    runtime lives at

        https://frontend-assets.supabase.com/studio/<hash>/_next/static/chunks/1s0mal.js

    and declares `static/chunks/1gdj.js` with an empty `publicPath`. Joining against the declaring
    file gives `.../_next/static/chunks/static/chunks/1gdj.js` and joining against the document gives
    `https://supabase.com/static/chunks/1gdj.js`, both 404. 126 declared chunks fetched 0.

    The build root is recoverable without knowing the framework, because the declared path and the
    declaring URL OVERLAP: the URL already contains `static/chunks/`, so cutting it at that segment
    and appending the path reconstructs the root. Returns None when there is no overlap, which is the
    normal case for a build whose `publicPath` already names the root.
    """
    seg = chunk_path.lstrip("./").split("/", 1)[0]
    if not seg or seg.startswith("?"):
        return None
    path = urlsplit(declaring_url).path
    at = path.find("/" + seg + "/")
    if at < 0:
        return None
    root = declaring_url.split("?", 1)[0]
    root = root[:root.index(path) + at + 1] if path in root else None
    return root + chunk_path.lstrip("./") if root else None


def chunk_candidates(declaring_url, chunk_path, public_path="", page_base=""):
    """Every URL a declared chunk path might live at, best guess first.

    One function because there are two callers and they must not disagree: `collect` fetches these,
    and `unfetched` hands them to whatever else is doing the fetching, a browser, usually. When the
    two had their own copies, the fetcher learned about asset roots and the reporter did not.

    Order matters only for speed, since the caller takes whichever answers 200 with JavaScript in it.
    """
    if chunk_path.startswith("http"):
        return [chunk_path]
    out = []
    aligned = _aligned(declaring_url, chunk_path)
    if aligned:
        out.append(aligned)
    # `publicPath` is where the RUNTIME says its chunks live, so it beats the declaring file's own
    # directory whenever the build sets it.
    if public_path.startswith("/") or public_path.startswith("http"):
        out.append(urljoin(page_base or declaring_url, urljoin(public_path, chunk_path)))
    out.append(urljoin(declaring_url, chunk_path))
    if page_base:
        out.append(urljoin(page_base + "/", chunk_path.lstrip("/")))
    seen, uniq = set(), []
    for u in out:
        if u and u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def _name(url):
    return hashlib.sha1(url.encode()).hexdigest()[:20] + ".js"


# HOW MANY LAZY CHUNKS TO FETCH, AND HOW MANY AT ONCE.
#
# 400 was set when the largest app measured declared about 700 chunks. claude.ai declares about
# 2,000, and at 400 jshound read 43% of it, which is why `PerformAction`, the endpoint that sends a
# chat message, was not in the output: it sits in a chunk that was never downloaded. Notion declares
# 2,457 and Loom 2,348. So the cap is now far past any app measured, and only a runaway (a target
# that answers every chunk name with a new chunk) ever reaches it. Hitting it is reported.
#
# Eight at a time: these are static files, almost always off a CDN, and fetching them one at a time
# made a 2,000-chunk app take minutes. pentest's own chase runs lower because a self-hosted target is
# not a CDN; a target that pushes back can be slowed with --max-chunks or pinned with --client.
#
# Scanning a fetched file for what it declares is a node process per file, run SCAN_WORKERS at once.
# One at a time, claude.ai's 1,258 files kept a single core busy for minutes while the rest sat idle.
MAX_CHUNKS = 50000
SCAN_WORKERS = max(2, min(16, os.cpu_count() or 4))
from . import SCAN_TIMEOUT  # noqa: E402
CHUNK_WORKERS = 8


def collect(url, outdir, with_chunks=True, max_chunks=MAX_CHUNKS, timeout=30, allow_private=False,
            report=None):
    os.makedirs(outdir, exist_ok=True)
    # The user named this one, so it is fetched whatever it resolves to. Every URL after it was named
    # by the target, see `host_is_private`, and is refused if it points into private space, unless
    # the target itself does, which is what scanning a local or internal app looks like.
    private_ok = bool(allow_private) or host_is_private(urlsplit(url).hostname)
    refused = set()

    def in_scope(u):
        if private_ok:
            return True
        if host_is_private(urlsplit(u).hostname):
            refused.add(u)
            return False
        return True

    global IMPERSONATE
    status, html = http_get(url, timeout)
    vendor = blocked_by(status, html)
    tried = [IMPERSONATE]
    if vendor and not _PINNED:
        first = (status, html, vendor)
        for prof in PROFILES:
            if prof in tried:
                continue
            IMPERSONATE = prof
            tried.append(prof)
            status, html = http_get(url, timeout)
            vendor = blocked_by(status, html)
            if not vendor:
                break
        if vendor:
            # Every profile refused. Report the FIRST refusal, which is what the default client got,
            # rather than whichever happened to be tried last.
            status, html, vendor = first
            IMPERSONATE = PROFILES[0]
    if report is not None:
        report.update(status=status, bytes=len(html or ""), blocked=vendor, scripts_by_host={},
                      profile=IMPERSONATE, tried=tried)
    if not html:
        print(f"  ! {url} returned {status} with no body", file=sys.stderr)
        return 0
    if vendor:
        # Saved anyway, because the page is evidence of what refused us. But nothing it references is
        # fetched: a challenge page's scripts are the challenge, not the application.
        hname = hashlib.sha1(url.encode()).hexdigest()[:20] + ".html"
        os.makedirs(outdir, exist_ok=True)
        open(os.path.join(outdir, hname), "w", errors="replace").write(html)
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
    # chunks, a total alone makes that look like a failure. Counting by discovery path is what
    # distinguishes "this build has no chunk table" from "we did not find it", and the counts must
    # sum to the total or the breakdown is just decoration.
    seen = set()
    # An inline script's dynamic `import()` is a real fetch the browser performs, and on SvelteKit it
    # is the ONLY reference to the entry module, nothing links it as a `src`. Counted under its own
    # discovery path so the breakdown still sums to the total.
    bootstrap = [u for u in inline_imports(url, p.inline) if u.startswith("http")]
    todo = [urljoin(url, s) for s in p.srcs] + bootstrap
    from_bootstrap = set(bootstrap)
    for u in todo:
        if u in seen or not u.startswith("http"):
            continue
        seen.add(u)
        if not in_scope(u):
            continue
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

    # ONE FIXPOINT FOR EVERYTHING A FILE DECLARES.
    #
    # A file names more code in two ways: a lazy chunk whose URL the bundler's runtime builds, and a
    # static `import` of another module. Both used to be followed, but separately, and imports only
    # from the scripts the page itself loaded, so a lazy chunk importing a shared module of its own,
    # which is how Vite and Rollup split code, had that module skipped without a word. Now every file
    # fetched, whatever found it, is scanned for both, and the loop runs until a round adds nothing,
    # the only stopping rule that can claim to be complete. Measured on Grafana back when this was
    # one pass over the initial scripts: the runtime declares 694 chunks, and two files beside it
    # declare 6 more that the single pass never saw.
    #
    # A chunk's base stays ambiguous (Grafana's live on a CDN, Element's are root-relative with an
    # empty publicPath), so every candidate is tried and whichever answers 200 wins. Declared,
    # fetched, unreachable and unscannable are all counted, because "declared 694, fetched 681, 13
    # gone" is the difference between enumeration working and enumeration lying.
    declared, fetched_ok, unreachable, already, rounds = set(), 0, 0, 0, 0
    spec_tried = 0
    unscanned = set()
    capped = False
    candidates = {}
    if with_chunks and seen:
        from concurrent.futures import ThreadPoolExecutor
        page_base = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}"
        scanned, frontier = set(), sorted(seen)
        while frontier and fetched_ok < max_chunks:
            rounds += 1
            files = []
            for u in frontier:
                if u in scanned:
                    continue
                scanned.add(u)
                fp = os.path.join(outdir, _name(u))
                if os.path.exists(fp):
                    files.append((u, fp))
            candidates = {}
            with ThreadPoolExecutor(SCAN_WORKERS) as ex:
                maps = list(ex.map(lambda f: _chunk_map_or_none(f[1]), files))
            for (u, fp), res in zip(files, maps):
                if res is None:
                    unscanned.add(fp)
                    continue
                pub = res.get("publicPath") or ""
                # TWO LISTS, TWO CONFIDENCES. `chunks` is what the bundler's own runtime builds.
                # `speculative` is what chunkmap found by scanning the text when the structured pass
                # came up short, which is the only way a Next.js `_buildManifest.js` is read: its
                # route table is wrapped in a function, and on www.loom.com its 66 page bundles were
                # all speculative, so none of the site's other pages was fetched. Both are fetched,
                # structured first, and a speculative URL that is not there is the net catching
                # nothing, not a hole in the map.
                for kind in ("chunks", "speculative"):
                    for c in (res.get(kind) or []):
                        cands = chunk_candidates(u, c, pub, page_base)
                        if cands and cands[0] not in candidates:
                            candidates[cands[0]] = (cands, "chunk-table" if kind == "chunks" else "speculative")
            by_path = {fp: u for u, fp in files}
            failed = []
            for fp, specs in js_imports(sorted(by_path), failed=failed).items():
                u = by_path.get(fp)
                for spec in specs:
                    # A bare or scoped package name is a build-time specifier with no URL to resolve.
                    if not u or not spec.startswith((".", "/", "http:", "https:", "//")):
                        continue
                    tgt = urljoin(u, spec)
                    if tgt not in candidates:
                        candidates[tgt] = ([tgt], "es-import")
            unscanned.update(failed)
            frontier = []
            # Decide this round's work in one thread, fetch it in several, apply it in one. The
            # fetch is the only slow part, and it is the only part that touches nothing shared.
            work = []
            # Structured before speculative, so a cap, if one is ever hit, cuts the guesses first.
            for key, (cands, kind) in sorted(candidates.items(), key=lambda kv: (kv[1][1] == "speculative", kv[0])):
                if key in declared:
                    continue
                if fetched_ok + len(work) >= max_chunks:
                    capped = True
                    break
                declared.add(key)
                if kind == "speculative":
                    spec_tried += 1
                if any(cu in seen for cu in cands):
                    already += 1              # already pulled in by an earlier pass
                    continue
                todo = [cu for cu in cands if in_scope(cu)]
                seen.update(cands)
                work.append((todo, kind))

            def fetch_one(item):
                todo, kind = item
                for cu in todo:
                    st, body = http_get(cu, timeout)
                    if st == 200 and body and is_javascript(body):
                        return cu, body, kind
                return None, None, kind       # an SPA catch-all is a 404 wearing a 200

            with ThreadPoolExecutor(CHUNK_WORKERS) as ex:
                for cu, body, kind in ex.map(fetch_one, work):
                    if cu is None:
                        if kind != "speculative":
                            unreachable += 1
                        continue
                    n = _name(cu)
                    open(os.path.join(outdir, n), "w", errors="replace").write(body)
                    rows.append({"f": n, "u": cu, "d": url})
                    saved += 1
                    how[kind] += 1
                    fetched_ok += 1
                    frontier.append(cu)       # what it declares is the next round
        if declared:
            # Every declared chunk is accounted for, because an unexplained remainder is exactly
            # where a quiet failure hides: 142 declared and 20 fetched reads like a 86% miss until
            # you are told the other 119 were already in hand.
            if report is not None:
                report.update(chunks_declared=len(declared) - spec_tried + (len(candidates) if capped else 0),
                              chunks_fetched=fetched_ok, chunks_unreachable=unreachable,
                              chunks_capped=capped, speculative_tried=spec_tried,
                              speculative_found=how.get("speculative", 0))
            print(f"  chunks and imports: {len(declared) - spec_tried} declared over {rounds} round(s), "
                  f"{fetched_ok} fetched, {already} already had, {unreachable} unreachable; "
                  f"{how.get('speculative', 0)} of {spec_tried} found by scanning the text"
                  + (f", capped at --max-chunks {max_chunks}"
                     if fetched_ok >= max_chunks else ""), file=sys.stderr)
        # A file that could not be scanned is a hole of unknown size: whatever it declares is missing,
        # and nothing else would say so.
        if report is not None:
            report["files_unscanned"] = len(unscanned)

    # Named and counted, not silently dropped: a refusal here is the one case where the target has
    # surface this did not read, and the fix is a flag rather than a mystery.
    if refused:
        hosts = sorted({urlsplit(u).hostname or "?" for u in refused})
        shown = ", ".join(hosts[:4]) + (", ..." if len(hosts) > 4 else "")
        print(f"  ! refused {len(refused)} URL(s) the target named in private address space "
              f"({shown}), pass --allow-private if that is surface you meant to read",
              file=sys.stderr)

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
    ap.add_argument("--max-chunks", type=int, default=MAX_CHUNKS)
    ap.add_argument("--allow-private", action="store_true",
                    help="allow URLs the target names in private address space (loopback, RFC1918, link-local); on by default when the target itself is private")
    a = ap.parse_args(argv)
    collect(a.url, a.out, not a.no_chunks, a.max_chunks, allow_private=a.allow_private)


if __name__ == "__main__":
    main()


def unfetched(bodies, manifest=None):
    """Chunk URLs the bundles in `bodies` declare that are not on disk there.

    This prints rather than downloads, because the thing that already has a session, a stealth
    browser and an egress the target answers is whatever collected the corpus, not this. The
    standalone path is `collect`, which fetches them itself; the integration path is this, handing
    the list back to a crawler.

    The URL building is `chunk_candidates`, the same function `collect` fetches through, so the two
    cannot drift. That mattered: resolving a relative chunk against the declaring file's own
    directory is wrong whenever the build has an asset root, and supabase.com/dashboard declared 126
    chunks and retrieved none of them that way.
    """
    from .analyze import _origin_map, js_files
    from . import chunk_map

    origins = _origin_map(manifest, bodies)
    have = {os.path.basename(p).split("?")[0] for p in js_files(bodies)}
    want, unresolved = set(), 0
    for p in js_files(bodies, with_inline=False):
        try:
            res = chunk_map(open(p, errors="replace").read())
        except Exception:                                   # noqa: BLE001
            continue
        src = origins.get(os.path.basename(p))
        pub = res.get("publicPath") or ""
        for c in (res.get("chunks") or []):
            base = os.path.basename(str(c).split("?")[0])
            if not base or base in have:
                continue
            if str(c).startswith("http"):
                want.add(c)
            elif src:
                cands = chunk_candidates(src, c, pub, "")
                if cands:
                    want.add(cands[0])
            else:
                # No recorded origin means a relative path cannot become a URL. Counted, because a
                # silent drop here looks identical to a bundle that declared nothing.
                unresolved += 1
    return sorted(want), unresolved
