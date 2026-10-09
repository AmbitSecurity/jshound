#!/usr/bin/env python3
"""analyze.py: read the JavaScript the crawler already downloaded. Touch nothing.

**This tool makes no network requests.** That is the whole point of it.

The previous design had static analysis do its own fetching: find the scripts a host ships, download them
with a dozen parallel workers, then parse. Three things went wrong at once, all of them predictable in
hindsight:

  - it fetched from a different egress than the crawler used, so one channel got blocked by three
    hosts' edge protection while the other kept working
  - it worked to a deadline, and on a 643 KB page it spent the entire budget walking 1271 links and fetched
    ZERO scripts, then reported "0 endpoints", which reads exactly like "this host has no API"
  - it re-downloaded bundles the crawler had already saved to disk minutes earlier

So this tool does one job: analysis. Fetching is the crawler's job because the crawler is already good at
it, it follows what a page actually loads, handles SPAs, and goes through a stealth browser. Duplicating a
worse version of that inside the JS tooling bought nothing and cost us three hosts.

This is a division of labour between two tools, not a rule about how the target may be reached. Curl it,
probe it, open a browser and click around, whatever the situation calls for.

Chunk enumeration still matters: a bundle names lazy chunks the browser never loaded, and those are real
surface. `--unfetched` prints them so they can be collected, usually by pointing the crawler at them,
since that is the path already set up for it.

  python analyze.py raw/bodies --host help.example.com --out raw/help.static.json
  python analyze.py raw/bodies --unfetched          # chunk URLs nobody has downloaded yet
"""
import argparse, collections, hashlib, json, os, re, subprocess, sys, tempfile
from urllib.parse import urljoin, urlparse
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor

from jshound.nodeenv import node_env, node_failure

# Resolved from the environment so the same file works inside the image and on a dev box.
HERE = os.path.dirname(os.path.abspath(__file__))

# The extractor sits beside this file, in js/.
# Babel and terser are installed globally in the image; NODE_PATH points node at them from any cwd.
NODE_PATH = os.environ.get("NODE_PATH") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                            "node_modules")
AST = os.path.join(HERE, "js", "static_ast.js")
CHUNKMAP = os.path.join(HERE, "js", "chunkmap.js")

# Parsing is bounded by BYTES, not by file count. A batch of 40 tiny chunks is nothing; a batch of 40
# 2 MB bundles is 80 MB into one node process, which dies with an empty stdout and no error, a crash that
# looks exactly like a file with no endpoints in it.
BATCH_BYTES = 6 * 1024 * 1024        # ceiling; the real size is computed per run, see plan_parallelism


# Fixed, so the split is a property of the INPUT and not of the box it ran on.
#
# And FEW, because batch size is not neutral. Cross-file resolution that needs AST nodes, a wrapper's
# callers, a URL assembled through a helper, can only work inside one node process, so a bigger batch
# resolves strictly MORE and never resolves anything wrongly. Measured on one target: with 8MB batches the
# extractor recovers /projects/{projectId}/sandbox/start and /projects/{call:Ge}/commits/{hash}/diff, which
# with 0.3MB batches remain unresolved path literals. Small batches produce MORE rows and WORSE ones.
#
# The corpus-wide module index (see _build_module_index) removed the other half of this, exported
# constants and property values now resolve regardless of the split. What is left needs the nodes
# themselves, so the answer is to keep batches large, bounded by BATCH_BYTES for memory.
DETERMINISTIC_BATCHES = 6


def plan_parallelism(paths):
    """How many workers, and how big a batch, for THIS input on THIS machine.

    Both numbers were fixed constants and the pair of them was self-defeating: four workers against a
    6 MB batch size meant a 14 MB corpus formed three batches, so at most three parsers ever ran and
    raising the worker count changed nothing, measured at 19.8s with 4 workers and 19.4s with 15.

    Batches exist to amortise node's ~110 ms startup over files that parse in ~54 ms each, so they must not
    become one-file-per-process. Aim for a few batches per worker, floor them at 1 MB so startup stays
    amortised, and cap the workers by MEMORY as well as cores because each parser is given several GB and
    a big bundle really does use it.
    """
    import os as _os
    cpu = _os.cpu_count() or 4
    try:
        gb = _os.sysconf("SC_PAGE_SIZE") * _os.sysconf("SC_PHYS_PAGES") / (1024 ** 3)
    except Exception:
        gb = 8
    workers = int(_os.environ.get("ANALYZE_WORKERS") or 0) or max(
        2, min(cpu - 1, max(2, int(gb // 3)), 16))
    total = sum(_os.path.getsize(x) for x in paths if _os.path.exists(x))
    # BATCH SIZE MUST NOT DEPEND ON THE MACHINE. It was derived from the worker count, which is derived
    # from cores and RAM, so the same bundle analysed on two boxes was split differently, and the split
    # changes the ANSWER: cross-file resolution only works when the importer and its import land in the
    # same node process. Measured on one target, 4 workers, only the batch size varied: 32 endpoints differed,
    # including `/projects/{call:Ge}/commits/{hash}/diff` which resolves only when its defining file shares
    # the batch, and 29 path literals that a call site in the same batch explained away.
    #
    # That is a real completeness gap and it is NOT fixed here, a batch-independent module index is the
    # fix. What is fixed here is that the result no longer depends on the hardware: same input, same split,
    # same answer, so a run is comparable with the run before it and a regression is a regression.
    target = max(1, DETERMINISTIC_BATCHES)
    size = int(min(BATCH_BYTES, max(1024 * 1024, total / target)))
    return workers, size


# URL-bearing attributes that are not `href`/`src` on a classic tag. `formaction` overrides a form's own
# target from a button; `data-url`/`data-src` are the convention every component library reaches for.
_URL_ATTRS = {"src", "href", "action", "formaction", "data-src", "data-url", "data-href",
              "data-endpoint", "data-action", "ping", "poster"}

# htmx and friends state the METHOD in the attribute name, which is more than a link ever tells you.
_HTMX_VERB = re.compile(r"^(?:hx|data-hx)-(get|post|put|patch|delete)$", re.I)

# Fetchable-looking values only. Rejects `click`, `concat:a,b`, `javascript:`, `#frag`, bare words.
_LOOKS_URL = re.compile(r"^(?:/(?!/)|\./|\.\./|https?://)", re.I)
_ASSET_EXT = re.compile(r"\.(png|jpe?g|gif|svg|webp|avif|ico|mp4|webm|mp3|wav|woff2?|ttf|eot|css)$", re.I)

# SERVER STATE SERIALISED INTO AN ATTRIBUTE, which the URL-shaped rule above cannot reach: `_LOOKS_URL`
# rejects a value starting `{` or `[` before any other gate, so a JSON DOCUMENT in an attribute is read by
# nothing. Measured over 110 saved documents, this is worth exactly one narrow rule.
#
# Why it matters, with the decisive case first. Slack's `<div data-props="{...}">` carries three auth-flow
# entry points, `https://slack.com/signin/oauth/{apple,google,microsoft}/start?email_first=1&...`, and
# `grep -rF 'signin/oauth'` over the whole corpus, HTML and all 121 scripts, finds them in ZERO files,
# because JSON escapes `/` as `\/`. No text-based tool reaches these; only a parse does. Datadog's
# `<input type="hidden" id="auth_settings">` declares its entire login surface in 688 bytes,
# `login_url`, `openid_login_url=/account/oauth2?provider=google`, `forgot_password_url`,
# `recaptcha_login_url`, the TOTP and MFA-recovery URLs, and `allowed_login_methods`, in a document with
# ZERO `<form>` tags, so `markup_records` never sees it either. Asana's is a 17 KB `<meta content>` naming
# its own `/-/oauth` endpoint and the IdP authorize URLs it accepts.
#
# AN ATTRIBUTE-NAME ALLOWLIST IS THE ONLY WORKABLE GATE, and that is a measured conclusion rather than a
# preference. No test on the VALUE can separate state from a rendered code sample: deno.com puts a
# `deno.json` snippet in a `code` attribute whose `workspaces: ["./api","./core","./web"]` are directory
# names that satisfy every URL-shape test and are not requests. Taking any JSON-valued attribute yields 71
# leaves across 6 sites; this allowlist keeps 24, and all 19 dropped are noise or navigation, GitLab's
# serialised sidebar menu (13), Hex's 100 KB of LaunchDarkly flags (3 URLs, one already in the bundle),
# and that deno code sample (3).
_JSON_STATE_ATTRS = {"data-page", "data-props", "data-react-props", "data-config",
                     "data-settings", "data-state", "data-initial-state", "data-bootstrap"}

# `.js`/`.mjs` belong in the asset gate ON THIS PATH, where a third-party bundle URL is all a CDN config
# leaf ever is, Datadog's four `bundle_url` entries and its `rum_snippet_url` are exactly that. They are
# NOT in `_ASSET_EXT` itself, because elsewhere a `.js` URL is the thing this tool is built to fetch.
_STATE_ASSET_EXT = re.compile(r"\.(m?js)$", re.I)


class _Doc(HTMLParser):
    """Parse saved HTML properly instead of regexing it.

    This was a regex, `<script(?![^>]*\\bsrc=)[^>]*>(.*?)</script\\s*>`, and regexing HTML fails the
    way it always fails: an attribute value containing `>` ends the tag early, casing and quoting variants
    slip through, and malformed markup produces silent nonsense. Concretely it also could not read the
    `type` attribute, so a `<script type="application/json">` config blob was extracted, written out as a
    .js file, handed to Babel, and rejected as a syntax error, the base URL for an entire admin API,
    thrown away at the parse step.

    HTMLParser puts script/style into CDATA mode itself, so the body comes back intact and the end tag is
    found the same way a browser finds it. Stdlib, so it costs the image nothing.
    """

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.scripts = []          # (attrs dict, body) for every inline <script>
        self.links = []            # href/action/src targets declared in markup
        self.metas = []            # (name, content)
        self.forms = []            # {method, action, fields:[{name,type,value}]}, declared contracts
        self.partials = []         # URL-bearing attributes on elements no fixed tag list covers
        self.json_state = []       # (tag, attr, name/id, parsed JSON) for serialised server state
        self._cur = None
        self._form = None

    def handle_starttag(self, tag, attrs):
        # Collapse whitespace in attribute VALUES, which is what a browser does when it resolves a URL.
        # HTML wraps attributes across lines freely, so a form written as
        #     <form action="/categories/ai
        #                  " method="get">
        # yields an action ending in a newline and 30 spaces. Left raw, `/categories/ai` and
        # `/categories/ai\n   ` are two different forms, and one app store page produced 40 form records
        # where there were 22.
        a = {k.lower(): " ".join((v or "").split()) for k, v in attrs}
        if tag == "script" and "src" not in a:
            self._cur = a
        elif tag == "form":
            # A <form> in SERVED markup is the most completely specified request contract there is:
            # method, target and every field name, declared rather than inferred. The crawler reads these
            # from the rendered DOM, but pages fetched without a browser get no snapshot at all, and
            # on a server-rendered app this IS the surface.
            # enctype too. For a form the body encoding is not an inference, it is the HTML spec:
            # application/x-www-form-urlencoded unless enctype says otherwise, and multipart/form-data is
            # how a file gets uploaded. The crawler already read this attribute; the markup path did not,
            # and neither turned it into an encoding, 117 forms across the corpus, not one with an encoding
            # recorded, for a fact that is written in the tag.
            self._form = {"method": (a.get("method") or "GET").upper(),
                          "enctype": (a.get("enctype") or "").lower() or None,
                          "action": a.get("action") or "", "fields": []}
        elif tag in ("input", "select", "textarea") and self._form is not None:
            if a.get("name") and "disabled" not in a:
                self._form["fields"].append({
                    "name": a["name"], "type": a.get("type") or tag,
                    # a hidden field's VALUE is state the server round-trips, csrf tokens, ids, prices
                    "value": a.get("value") if (a.get("type") or "").lower() == "hidden" else None})
        if tag in ("a", "area", "form", "iframe", "link"):
            for k in ("href", "action", "src"):
                if a.get(k):
                    self.links.append((tag, k, a[k]))
        elif tag == "meta" and a.get("name"):
            self.metas.append((a["name"], a.get("content", "")))

        # A URL IS A URL WHATEVER ELEMENT CARRIES IT.
        #
        # The list above is the 1999 set of tags. A server-rendered app addresses its own fragments through
        # custom elements, and those are endpoints in every sense, they answer requests and return markup.
        # Reddit's shreddit ships `<faceplate-partial src="/svc/shreddit/partial/fKScTR/login-step">`, and
        # 24 of them sat in HTML we had already saved, unread: login-step, register-step, phone-auth,
        # username-step, magic-link-register-step, onboarding-flow. The entire authentication flow.
        #
        # htmx is the same idea with the METHOD stated outright, which makes it strictly better evidence
        # than a bare href, `hx-post="/x"` is a declared POST endpoint. Turbo, Unpoly and Hotwire all put
        # a URL on an attribute of a tag no fixed list will ever contain.
        for k, v in a.items():
            # Serialised server state, collected before the URL-shape gate below rejects it for
            # starting with a brace. Three carriers, each the one its framework actually uses:
            # an allowlisted `data-*` attribute, a hidden input's `value` (Datadog), and `<meta
            # content>` (Asana). The value must parse as an object or an array, which is also what
            # keeps Radix UI's `data-state="closed"` out, since a bare word is neither.
            if v and v[0] in "{[" and (
                    k in _JSON_STATE_ATTRS
                    or (tag == "input" and k == "value" and a.get("type", "").lower() == "hidden")
                    or (tag == "meta" and k == "content")):
                try:
                    parsed = json.loads(v)
                except Exception:                      # noqa: BLE001, not JSON is the common case
                    parsed = None
                if isinstance(parsed, (dict, list)):
                    self.json_state.append({"tag": tag, "attr": k,
                                            "name": a.get("name") or a.get("id"), "value": parsed})

            # A URL LOOKS LIKE ONE. Requiring only "the attribute is named src/action" produced 18,434
            # hits on one corpus, almost all of them noise: `<faceplate-tracker action="click">` where
            # `action` is an event name, and `<faceplate-loader src="concat:a,b,c">` where `src` is a
            # module list behind a pseudo-scheme. Neither is a request. A path starts with `/`, or `./`,
            # or a scheme we can actually fetch, anything else is a word that happens to sit on an
            # attribute with a URL-ish name.
            if not v or not _LOOKS_URL.match(v):
                continue
            # A media file is not an endpoint. `<shreddit-player src=...>` and `<faceplate-img src=...>`
            # are assets by any other name, and mixing them into the API surface is the same mistake as
            # counting a CSS cache-buster as a request contract.
            if _ASSET_EXT.search(v.split("?")[0]):
                continue
            m = _HTMX_VERB.match(k)
            if m:
                self.partials.append({"tag": tag, "attr": k, "url": v,
                                      "method": m.group(1).upper(), "name": a.get("name") or a.get("id")})
            # `<use href="/assets/icons-<hash>#pencil">` is an SVG sprite reference, not a request, and
            # it is everywhere: 1,888 of 1,946 hits on GitLab were this one element. SVG's own URL-bearing
            # tags are excluded alongside the media ones for the same reason.
            elif k in _URL_ATTRS and tag not in ("a", "area", "form", "iframe", "link", "script",
                                                 "img", "source", "track", "video", "audio",
                                                 "use", "image", "feimage", "textpath", "mpath",
                                                 "pattern", "filter", "clippath", "lineargradient"):
                self.partials.append({"tag": tag, "attr": k, "url": v,
                                      "method": None, "name": a.get("name") or a.get("id")})

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        if self._cur is not None:
            self.scripts.append((self._cur, data))
            self._cur = None

    def handle_endtag(self, tag):
        if tag == "script":
            self._cur = None
        elif tag == "form" and self._form is not None:
            if self._form["fields"]:
                self.forms.append(self._form)
            self._form = None


def parse_html(text):
    d = _Doc()
    try:
        d.feed(text)
        d.close()
    except Exception:
        pass                       # malformed markup: keep whatever was parsed before it broke
    return d


# A script block is JAVASCRIPT unless its type says otherwise. Everything else in this set is data,
# usually the runtime config an SPA cannot bake in at build time, which is exactly the value that makes
# `{EXPR}` unresolvable.
_JS_TYPES = {"", "text/javascript", "application/javascript", "module",
             "text/babel", "application/ecmascript", "text/ecmascript"}
_DATA_TYPES = {"application/json", "application/ld+json", "text/json",
               "application/x-json", "speculationrules"}


def inline_scripts(d, tmp=None):
    """Pull <script> blocks out of saved HTML and write them where the extractor can see them.

    Written to a TEMP directory, never inside the bodies directory. Writing into the input had two faults:
    it fails outright when the bodies are mounted read-only, and on a second run the files left behind by
    the first are picked up by the directory walk AND extracted again, so every inline script counts twice.
    An analysis pass should not modify the thing it is analysing.

    A page's own inline JavaScript is JavaScript. one scanner service's entire API, POST /api/queue with
    a {url} body, a 201-means-still-scanning poll, a redirect to /results/{uuid}, lives in one inline
    jQuery block on the landing page, and analysing only .js FILES walked straight past it. The agent had
    to open the HTML itself and hand-write the records, which is the tool failing, not the agent excelling.

    The old fetching extractor handled inline scripts; replacing it with a file-walker quietly dropped that.
    """
    if tmp is None:
        import tempfile
        tmp = tempfile.mkdtemp(prefix="inline-")
    os.makedirs(tmp, exist_ok=True)
    made = []
    for root, _, names in _corpus_walk(d):
        for n in names:
            if not n.endswith((".html", ".htm")):
                continue
            try:
                html = open(os.path.join(root, n), errors="ignore").read()
            except Exception:
                continue
            doc = parse_html(html)
            for i, (attrs, body) in enumerate(doc.scripts):
                body = (body or "").strip()
                if len(body) < 40:
                    continue          # a few characters is an analytics ping, not a program
                t = (attrs.get("type") or "").split(";")[0].strip().lower()
                if t and t not in _JS_TYPES:
                    continue          # data, not code, handled by config_values()
                out = os.path.join(tmp, f"{n}.inline{i}.js")
                open(out, "w", errors="ignore").write(body)
                made.append(out)
    return made



# `window.X = "..."`, `var X = "..."`, `X = "..."`, the shapes a server uses to inject a base URL.
_CFG_ASSIGN = re.compile(r"""(?:window|globalThis|self)\s*\.\s*([A-Za-z_$][\w$]*)\s*=\s*['"]([^'"]{1,300})['"]"""
                         r"""|(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=\s*['"]([^'"]{1,300})['"]""")
# A host or an origin: protocol-relative, absolute, or a bare dotted name. Never a path, never prose.
_URLISH = re.compile(r"^(?://[A-Za-z0-9.\-]+|https?://[A-Za-z0-9.\-]+|[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+){1,})"
                     r"(?::\d+)?(?:/[^\s]*)?$")


def config_values(d):
    """Runtime config an SPA cannot bake in at build time, harvested from the HTML it was injected into.

    A static bundle has no way to know its own deployment's base URL, so the server writes it into the page
    and the bundle reads it back. That is why `${k(c.baseUrl)}admin/realms/...` resolves to `{EXPR}`, the
    value was never in any JavaScript file. On auth.example.com it is sitting in

        <script id="environment" type="application/json">
          {"authServerUrl":"https://auth.example.com/auth", "serverBaseUrl":"...", "realm":"master"}

    in a file we already had on disk, and 474 endpoints kept an unresolved base because nothing opened it.

    Returned as a flat map keyed by LEAF PROPERTY NAME, for the same reason the getter index is: the
    receiver (`c`) is precisely what minification renames, while the property (`baseUrl`) survives. Nested
    objects are flattened; only scalar strings are kept, because a base URL is a string.
    """
    out = {}

    def flatten(obj, depth=0):
        if depth > 6 or not isinstance(obj, dict):
            return
        for k, v in obj.items():
            if isinstance(v, str) and 0 < len(v) < 400:
                out.setdefault(str(k), v)
            elif isinstance(v, dict):
                flatten(v, depth + 1)
            elif isinstance(v, list):
                for it in v[:1000]:
                    flatten(it, depth + 1)

    for root, _, names in _corpus_walk(d):
        for n in names:
            if not n.endswith((".html", ".htm")):
                continue
            try:
                doc = parse_html(open(os.path.join(root, n), errors="ignore").read())
            except Exception:
                continue
            for attrs, body in doc.scripts:
                t = (attrs.get("type") or "").split(";")[0].strip().lower()
                if t not in _DATA_TYPES:
                    continue
                try:
                    flatten(json.loads((body or "").strip()))
                except Exception:
                    continue
            # A DEPLOYMENT WRITES ITS BASE URLS INTO AN EXECUTABLE SCRIPT, NOT A JSON BLOB.
            #
            # This function only ever read `<script type="application/json">` and <meta>, so it walked past
            # the one place the answer usually is. On one target the served HTML contains, in plain sight:
            #
            #     window._gatewayHost = "//apigw.app.client.example"
            #     window._adminHost       = "//mgmt.app.client.example"
            #     window._reportHost / _docHost / _syncHost
            #
            # Five backend hosts, declared by the application, while the map filed that application's API
            # under the frontend. A static bundle cannot bake in its own deployment's base URL, that is
            # precisely why the server injects it, so this is the canonical source and it was unread.
            #
            # Kept narrow deliberately: a bare identifier assigned a STRING that looks like a URL or a
            # host. Not every assignment in every inline script; those are a program, not configuration.
            for attrs, body in doc.scripts:
                t = (attrs.get("type") or "").split(";")[0].strip().lower()
                if t and t not in _JS_TYPES:
                    continue
                for m in _CFG_ASSIGN.finditer(body or ""):
                    # two alternation branches: window.X = "..." (1,2) and var X = "..." (3,4)
                    name = m.group(1) or m.group(3)
                    val = m.group(2) if m.group(1) else m.group(4)
                    if name and val and _URLISH.match(val):
                        out.setdefault(str(name), val)

            # <meta name="csrf-token" content="..."> and friends are config too
            for name, content in doc.metas:
                if content and len(content) < 400:
                    out.setdefault(str(name), content)
    return out


def _form_target(action, src_url, src_host, method="GET"):
    """Where a <form> actually submits.

    An absent `action` does NOT mean "/". HTML says a form with no action submits to the document's own URL,
    and the same is true of `action="#"` and of any relative action, all three resolve against the page.
    Recording them as "/" invented a root endpoint that nothing posts to, and did it for 88 of 462 forms
    measured across 64 corpora (15% no action, 4% fragment-only), plus mis-filed the relative ones.

    We already know the serving URL, it was being read only to pull the hostname out of it, so resolve
    properly. Same-origin targets stay paths, which is what the rest of the pipeline stores; cross-origin
    ones keep their absolute form, because a form posting to another host is exactly the fact worth keeping.
    `method` matters because a GET submission overwrites the page's query string and a POST does not.
    """
    act = (action or "").strip()
    if not src_url:
        # No page to resolve against: an absolute or rooted action still stands on its own, anything else
        # cannot be placed and must not be guessed.
        act = act.split("#")[0]
        return act if act.startswith(("/", "http://", "https://")) else ""
    try:
        full = urljoin(src_url, act)
        u = urlparse(full)
    except Exception:
        return act.split("#")[0]
    if src_host and u.netloc and u.netloc != src_host:
        full = full.split("#")[0]
        return full[:-1] if full.endswith("?") else full
    # A GET form REPLACES the query string with its own fields, so carrying the page's query into the target
    # would record a parameter the submission deletes. A POST form keeps it. Both are the spec, not a
    # preference, and the difference is visible whenever a form sits on a page that has a query of its own.
    q = u.query if (method or "GET").upper() != "GET" else ""
    return (u.path or "/") + (("?" + q) if q else "")


# A RELATIVE path is only admitted as a whole string value. `"/v1/embeddings"` is a reference; the same
# characters inside a sentence are prose, and anchoring to the quotes is what keeps this channel precise.
_JSON_STR = re.compile(r'"((?:https?://[^"\s\\]{1,180})?/[A-Za-z0-9._~\-/{}$:%]{2,120})"')
# An ABSOLUTE url needs no such anchor, it names its scheme, its host and its path, and nothing else looks
# like one. This is how documentation code samples get read: an RSC payload renders
# `"children":"curl https://api.x.ai/v1/chat/completions"`, where the URL sits mid-string behind a shell
# command and the anchored pattern above cannot see it. Trailing punctuation is trimmed because a URL at
# the end of a sentence collects it.
_JSON_URL = re.compile(r'https?://[A-Za-z0-9.\-]+\.[A-Za-z]{2,}(?:/[A-Za-z0-9._~\-/{}$:%]{1,120})')
_JSON_ASSET = re.compile(r"\.(js|mjs|css|png|jpe?g|gif|svg|ico|webp|avif|woff2?|ttf|eot|otf|map|mp4|webm|"
                         r"mp3|wav|pdf|wasm|zip|csv)(\?|$)", re.I)
# Segments that are an INSTANCE rather than a route: long digit runs, uuids, hashes, locale codes, and the
# id-plus-slug form every help centre uses, `360043066492-Quanti-anni-devo-avere-per-usare-Reddit`. That
# last one is why the first version of this still emitted 22,051 reddit paths: collapsing bare digit runs
# left the slug making each URL unique, so /hc/{locale}/articles/{id}-{slug} never grouped.
_INST = re.compile(r"^(?:\d{3,}(?:[-.][\w%.-]*)?"       # 15483130362132, 360043513931.json,
                   r"|\d{4,}-[\w%.-]+"                  # 360043066492-How-do-I-report
                   r"|[0-9a-f]{8}-[0-9a-f-]{20,}"       # uuid
                   r"|[0-9a-f]{16,}"                    # hash
                   r"|[a-z]{2}(?:-[a-z]{2})?"           # locale: it, pt-br
                   r"|[\w%.-]{40,})$", re.I)            # any very long segment is a slug or a token
_JSON_MAX_PER_SHAPE = 3
# A backstop, because a content site will always find a shape nobody predicted. Per host, and it emits a
# record rather than passing in silence, same rule as every other cap in this tool.
_JSON_MAX_PER_HOST = 2000
# Only API-SHAPED paths. This channel was justified by a measurement, 61 paths on one scope and 53
# on x that no other channel produced, and every one of those was an API path. Emitting every path-shaped
# string in every saved JSON instead pulls in the content of whatever the app renders: 3,542 api.github.com
# instances from supabase's docs, 2,002 Zendesk article URLs from reddit. That is a third party's content
# enumerated, not our target's surface, and the shape cap cannot fix it because each one genuinely is a
# different URL. Take what the measurement paid for.
_JSON_APIISH = re.compile(r"^/(api|v\d+|graphql|rest|auth|oauth|admin|internal|service|rpc|gateway|_api)\b"
                          r"|/(api|graphql)/", re.I)


def _shape(path):
    """A path with its instance segments collapsed, used ONLY to group, never emitted as a template."""
    return "/".join("*" if _INST.match(seg) else seg for seg in path.split("/"))


# FILES THIS TOOL WRITES BESIDE A CORPUS. Read back as input they are not the target's code, and
# `records.json` in particular was mined as a captured API response: on claude.com 238 records became
# 240 on a re-run, the tool feeding on its own previous answer and compounding on every one after.
# `map` keeps its corpus in a `js/` subfolder now so the two never share a directory; this is the
# backstop for a corpus someone pointed at by hand.
_OWN_OUTPUT = {"records.json", "openapi.json", "endpoints.txt", "requests.har"}


def _corpus_walk(d):
    """`os.walk`, minus the files jshound wrote itself."""
    for root, dirs, names in os.walk(d):
        yield root, dirs, [n for n in names if n not in _OWN_OUTPUT]


def json_records(d, origins=None, documents=None):
    """Endpoint references inside captured RESPONSE bodies, JSON and RSC.

    `js_files()` reads .js, .mjs, .rsc and inline scripts. It has never read .json, so every API response the
    crawler saved went unanalysed, and some of them describe the API. xAI's status endpoint returns one
    object per model naming `base_url` and `uri`, which is an inventory of the inference API; claude.ai's
    docs data carries /v1/sessions/{session_id}/archive, /threads and /resources. Measured before building
    this: 61 paths on one scope and 53 on another that no other channel produced.

    These are responses we RECEIVED. Reading one is recon; nothing here infers a response we did not get.

    The reason this needs care rather than a grep is that one route repeats. Reddit's saved JSON yields
    17,678 "api-ish" strings which are two Zendesk routes,
    /api/v2/help_center/{locale}/articles/{id}.json, enumerated over every locale and article. Emitting
    them would bury the map in instances of one endpoint. So instances are GROUPED by shape (long digit
    runs, uuids, hashes and locale codes collapsed) and at most three per shape are kept, with a record
    saying how many there were. The kept paths stay literal, because the store's paths are literal; the
    shape is a grouping key and is never emitted as though the app had declared it.
    """
    origins, documents = origins or {}, documents or {}
    out = []
    per_shape = collections.Counter()
    per_host = collections.Counter()
    totals = collections.Counter()
    capped = set()
    for root, _, names in _corpus_walk(d):
        for n in names:
            # .rsc as well as .json. RSC flight payloads are passed to the JS extractor by js_files() and
            # come back with ZERO records, Babel accepts the file and finds nothing in it, which is the
            # silent-failure shape this codebase keeps meeting: it looks exactly like success. 898 files on
            # the one scope produced nothing that way, and they carry 106 API paths no other channel has.
            # They are read here as TEXT, which is all that is needed to see a path in one.
            if n.startswith("_") or not n.endswith((".json", ".rsc")):
                continue
            p = os.path.join(root, n)
            try:
                if os.path.getsize(p) > 8_000_000:
                    continue
                text = open(p, errors="replace").read()
            except OSError:
                continue
            src = origins.get(n) or origins.get(os.path.basename(n))
            doc = documents.get(n) or documents.get(os.path.basename(n))
            hits = [m.group(1) for m in _JSON_STR.finditer(text)]
            hits += [m.group(0).rstrip(".,;:)'\"") for m in _JSON_URL.finditer(text)]
            for v in hits:
                if _JSON_ASSET.search(v):
                    continue
                absu = v.lower().startswith(("http://", "https://"))
                path = re.sub(r"^https?://[^/]+", "", v) if absu else v
                if not path.startswith("/") or len(path) < 4:
                    continue
                if not _JSON_APIISH.search(path):
                    continue
                host = _abs_host(v) if absu else (
                    (urlparse(doc).netloc if doc else None) or (urlparse(src).netloc if src else None))
                key = (host, _shape(path))
                totals[key] += 1
                if per_shape[key] >= _JSON_MAX_PER_SHAPE:
                    continue
                if per_host[host] >= _JSON_MAX_PER_HOST:
                    capped.add(host)
                    continue
                per_shape[key] += 1
                per_host[host] += 1
                r = {"_kind": "path-literal", "method": "UNKNOWN", "confidence": "low",
                     "template": v if absu else path, "source": "json-response",
                     "note": "referenced in a JSON response body this crawl saved"}
                if host:
                    r["_host"] = host
                out.append(r)
    for h in capped:
        rec = {"_kind": "truncation", "where": "json-response",
               "note": f"per-host cap of {_JSON_MAX_PER_HOST} json-derived paths reached; more exist"}
        if h:
            rec["_host"] = h
        out.append(rec)
    for key, n in totals.items():
        if n > _JSON_MAX_PER_SHAPE:
            host, shape = key
            rec = {"_kind": "truncation", "where": "json-response", "shape": shape, "instances": n,
                   "kept": _JSON_MAX_PER_SHAPE,
                   "note": f"{n} instances of this shape in saved JSON; kept {_JSON_MAX_PER_SHAPE}"}
            if host:
                rec["_host"] = host
            out.append(rec)
    return out


def _state_leaves(node, path=""):
    """Every string leaf of a parsed blob, with the JSON path that reached it.

    The path is kept because it is the whole provenance of the record: `auth_settings.login_url` says
    where the value came from and what the application calls it, which is what makes the finding
    readable rather than a loose URL with no account of itself.
    """
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _state_leaves(v, f"{path}.{k}" if path else str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _state_leaves(v, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


def doc_state_records(d, origins=None):
    """Request surface the server SERIALISED INTO AN ATTRIBUTE, which no other channel here reads.

    `_LOOKS_URL` rejects a value beginning `{` before any other gate, so a JSON document carried in an
    attribute reaches nothing, and what it carries is routinely the application's declared auth
    surface. Datadog's hidden `auth_settings` input names its login, OIDC, forgot-password, recaptcha,
    TOTP and MFA-recovery endpoints in 688 bytes, in a document with no `<form>` at all. Slack's
    `data-props` names three OAuth entry points that `grep` cannot find in the entire corpus, because
    JSON escapes `/` as `\\/`.

    Scoped by the attribute NAME (see `_JSON_STATE_ATTRS`), because no test on the value can tell
    serialised state from a rendered code sample. Emitted as records and deliberately NOT fed into the
    value-resolution map that `--config` builds: that map is keyed by leaf property name and consumed
    as `_CONFIG.get(node.property.name)` bundle-wide, so a wrong entry silently rewrites unrelated
    URLs, the failure already recorded there, where a borrowed `url` put reddit's ActionCable socket
    at a Zendesk documentation page. Simulated on these blobs it would add 246 keys, 25 of them
    ambiguous inside a single document. Reporting a value is safe; resolving other code with it is not.
    """
    origins = origins or {}
    out, seen = [], set()
    for root, _, names in _corpus_walk(d):
        for n in sorted(names):
            if not n.endswith((".html", ".htm")):
                continue
            src_url = origins.get(n) or origins.get(os.path.basename(n))
            src_host = None
            if src_url:
                try:
                    src_host = urlparse(src_url).netloc or None
                except Exception:                      # noqa: BLE001
                    src_host = None
            try:
                doc = parse_html(open(os.path.join(root, n), errors="ignore").read())
            except Exception:                          # noqa: BLE001
                continue
            for blob in doc.json_state:
                where = blob.get("name") or blob["attr"]
                for jpath, val in _state_leaves(blob["value"]):
                    v = " ".join(val.split())
                    if not v or len(v) > 400 or not _LOOKS_URL.match(v):
                        continue
                    bare = v.split("?")[0]
                    # A bare origin is not an endpoint, the same rule the scoreboard applies when it
                    # refuses to count `/`. Slack's `geoRootUrl` is `https://slack.com/` and Datadog's
                    # `statuspageio_page_url` is a third-party status page root; neither is surface.
                    if bare.rstrip("/").endswith("//") or not urlparse(bare).path.strip("/"):
                        continue
                    # An asset is not an endpoint, and on this path a `.js` URL is a third-party
                    # bundle rather than something to fetch: Datadog's four `bundle_url` leaves and
                    # its `rum_snippet_url` are all CDN scripts.
                    if _ASSET_EXT.search(bare) or _STATE_ASSET_EXT.search(bare):
                        continue
                    key = (src_host, v)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({"_kind": "doc-state", "method": None, "template": v,
                                "sink": f"{blob['tag']}[{blob['attr']}]",
                                "state_path": f"{where}.{jpath}" if jpath else where,
                                "_host": src_host, "_page": src_url})
    return out


def markup_records(d, origins=None):
    """Request contracts declared in SERVED markup.

    A <form> states its method, its target and every field name outright, no inference, no call site to
    attribute. That makes it the most completely specified contract this tool ever produces, and it was
    being read by the crawler's DOM snapshot only. Pages fetched without a browser get no snapshot,
    and on a server-rendered application the markup IS the surface, so anything that never rendered in a
    browser was invisible.

    Only FORMS. The 6,825 link targets in one Discourse sample are forum threads, following links is the
    crawler's channel and emitting them here would inflate the map with instances of routes it already
    walks. Hidden field VALUES are kept because they are round-tripped state: csrf tokens, object ids,
    prices. Unfired by construction, so these land with no status and sit in the residual.
    """
    # Attribute every form to the host that SERVED its page. Without this the records came out with no
    # `_host`, which made them orphans: per-file attribution could neither keep them for the right host nor
    # drop them for the wrong one, so a shared bodies directory either leaked another host's forms in or
    # discarded the real ones. Same fix as the JS records, read the origin map.
    origins = origins or {}
    out, seen = [], set()
    for root, _, names in _corpus_walk(d):
        for n in names:
            if not n.endswith((".html", ".htm")):
                continue
            src_url = origins.get(n) or origins.get(os.path.basename(n))
            src_host = None
            if src_url:
                try:
                    src_host = urlparse(src_url).netloc or None
                except Exception:
                    src_host = None
            try:
                doc = parse_html(open(os.path.join(root, n), errors="ignore").read())
            except Exception:
                continue
            for f in doc.forms:
                fields = [x["name"] for x in f["fields"] if x.get("name")]
                if not fields:
                    continue
                act = _form_target(f["action"], src_url, src_host, f["method"])
                key = (src_host, f["method"], act, tuple(sorted(fields)))
                if key in seen:
                    continue
                seen.add(key)
                hidden = {x["name"]: x["value"] for x in f["fields"]
                          if x.get("value") and (x.get("type") or "").lower() == "hidden"}
                # A GET form has NO BODY, its fields become the query string, so an encoding would be
                # meaningless there. Only a form that sends a body gets one.
                enc = None
                if f["method"] != "GET":
                    et = f.get("enctype") or ""
                    enc = ("multipart" if "multipart" in et
                           else "raw" if "text/plain" in et
                           else "form")            # the HTML default, application/x-www-form-urlencoded
                rec = {"_kind": "form", "method": f["method"], "template": act,
                       "enctype": f.get("enctype"), "body_encoding": enc,
                       "source": "markup", "confidence": "high",
                       "note": "HTML form in served markup, never submitted"}
                if not act:
                    # Nowhere to send it and no page URL to resolve against. Say so rather than inventing a
                    # path; a form with no target is still evidence of the field set.
                    rec["template"] = None
                    rec["unresolved"] = "no action, serving URL unknown"
                if src_host:
                    rec["_host"] = src_host
                if f["method"] == "GET":
                    rec["query_schema"] = fields
                else:
                    rec["body_keys"] = fields
                if hidden:
                    rec["hidden_values"] = hidden
                # A <form> STATES its fields outright, no inference, so it has a declared schema like any
                # other request. Hidden fields are marked because they are round-tripped state (csrf tokens,
                # object ids, prices) rather than anything a user types, which is what makes them worth
                # tampering with.
                rec["body_shape"] = {n: ("hidden" if n in hidden else "?") for n in fields}
                out.append(rec)

            # SERVER-RENDERED PARTIALS. A custom element with a URL attribute is an endpoint that answers
            # requests and returns markup, the same claim a <form action> makes, with the same evidence.
            # Recorded separately from links because a link is somewhere you navigate and this is something
            # the page FETCHES, which is the distinction that decides whether it belongs in the API surface.
            for pt in getattr(doc, "partials", []):
                u = pt.get("url") or ""
                tgt = _form_target(u, src_url, src_host, pt.get("method") or "GET")
                if not tgt:
                    continue
                k2 = (src_host, pt.get("method"), tgt)
                if k2 in seen:
                    continue
                seen.add(k2)
                r2 = {"_kind": "partial", "method": pt.get("method"), "template": tgt,
                      "source": "markup", "confidence": "high",
                      "note": f"<{pt['tag']} {pt['attr']}> server-rendered partial"
                              + (f" ({pt['name']})" if pt.get("name") else "")
                              + ("" if pt.get("method") else "; method not stated, GET assumed by the browser")}
                if src_host:
                    r2["_host"] = src_host
                out.append(r2)
    return out

def js_files(d, with_inline=True):
    out = []
    for root, _, names in _corpus_walk(d):
        for n in names:
            p = os.path.join(root, n)
            # .ts/.tsx are here because a target that ships sourcemaps hands us its ORIGINAL source, and
            # sourcemaps.py writes those into this directory like any other body. The extractor's parser
            # already enables the typescript plugin (see parse()), so they need no special path, they are
            # just the same application with its names and types intact. Without this line they were
            # written, registered, and then walked straight past: 3,412 files, zero records.
            if (n.endswith((".js", ".mjs", ".ts", ".tsx"))
                    or (n.endswith(".rsc") and os.path.getsize(p) < 4_000_000)):
                out.append(p)
    if with_inline:
        out += inline_scripts(d)
    return sorted(set(out))


_CONFIG_FILE = None
_DECLARED_HOSTS = {}
_DECLARED_GATEWAY = None   # the single API base the page declares, if it declares exactly one
_CONFIG_RECORDS = []


_INDEX_FILE = [None]
_FILESET_FILE = [None]
_ORIGINS_FOR_ALIAS = [None]
_BODIES_DIR = [None]


def _build_module_index(paths, workers, batches):
    """Pass one: index every module's exports ONCE, across the whole corpus.

    The extractor's pre-pass builds its export and property index from the files in its own process, which
    means an import resolves only when its defining file happens to share a batch. That is not a module
    index, it is an accident of scheduling, and it changes the ANSWER, not just the runtime: measured on
    one target with the worker count held fixed and only the batch size varied, 32 endpoints differed, an
    imported `BASE` resolving to /api/orders in one arrangement and staying {BASE}/orders in the other.

    So: parse everything once with the collectors switched off, merge what comes back, and hand the result
    to every batch of pass two. Indexing is much cheaper than extracting, which is what makes this
    affordable rather than a doubling.
    """
    # CARRY EVERY FIELD THE INDEX PASS EMITS. This merged exports and propvals and silently dropped
    # anything else, so `callargs`, the per-batch caller argument shapes the extractor now publishes,
    # and the only way a wrapper defined in one chunk can learn what another chunk passes it, arrived
    # at pass two empty. The extractor produced it, the extractor loaded it, and the merge in between
    # threw it away, which made four separate attempts at the body-parameter gap measure an index that
    # was never there.
    exports, propvals, callargs = {}, {}, {}
    # OR, not first-wins. The tRPC marker sits in one file of a corpus, cal.com's is 1 of 79, so any
    # batch seeing it settles the question for all of them. Merging this with the same "first batch
    # wins" rule as the maps would reinstate the bug it exists to fix.
    trpc = False
    # Routing is hash OR path for an app, so two batches disagreeing means the corpus holds both and
    # neither answer is the app's. A conflict reports nothing rather than whichever batch finished first.
    routing, routing_conflict = None, False
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for got in ex.map(lambda b: _run_index(b), batches):
            trpc = trpc or bool(got.get("trpc"))
            r = got.get("routing")
            if r in ("hash", "path"):
                if routing and routing != r:
                    routing_conflict = True
                routing = r
            for k, v in (got.get("exports") or []):
                exports.setdefault(k, v)
            # Unanimous across batches as well as within one: a value poisoned in any batch (sent as
            # null) or disagreeing between two stays poisoned, whichever batch finished first.
            for k, v in (got.get("propvals") or []):
                if k not in propvals:
                    propvals[k] = v
                elif propvals[k] != v:
                    propvals[k] = None
            # union rather than first-wins: one wrapper takes several partial bodies across call sites
            for k, v in (got.get("callargs") or []):
                cur = callargs.setdefault(k, {})
                if isinstance(v, dict):
                    for kk, vv in v.items():
                        if kk not in cur or cur[kk] in ("?", "{EXPR}"):
                            cur[kk] = vv
    fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"exports": list(exports.items()), "propvals": list(propvals.items()),
               "callargs": list(callargs.items()), "trpc": trpc,
               "routing": None if routing_conflict else routing}, fh)
    fh.close()
    _INDEX_FILE[0] = fh.name
    # …and the ORIGINAL name of each saved body, which is the only way a relative import can resolve.
    # Bodies are stored by content hash, so `./app-shared-DKk5IE_B.js` matches no path and no basename we
    # hold, 1,147 distinct specifiers on one target, zero resolvable. The original name is in the URL the
    # crawler fetched it from, and that is recorded; 450 of those 1,147 resolve through it.
    aliases = {}
    for saved, url in (_ORIGINS_FOR_ALIAS[0] or {}).items():
        base = str(url).split("?")[0].split("#")[0].rstrip("/").split("/")[-1]
        if base.endswith((".js", ".mjs")):
            full = os.path.abspath(os.path.join(_BODIES_DIR[0] or "", saved))
            aliases.setdefault(base, full)
            aliases.setdefault(base.rsplit(".", 1)[0], full)
    fs = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"files": [os.path.abspath(p) for p in paths], "aliases": aliases}, fs)
    fs.close()
    _FILESET_FILE[0] = fs.name
    if aliases:
        print(f"  {len(aliases)} original module name(s) mapped onto content-hashed bodies, so relative "
              f"imports can resolve", file=sys.stderr)
    print(f"  module index: {len(exports)} export(s), {len(propvals)} property value(s) across "
          f"{len(paths)} files, independent of how they are batched", file=sys.stderr)


def _run_index(paths):
    if not paths:
        return {}
    try:
        env = node_env(); env["NODE_PATH"] = NODE_PATH
        r = subprocess.run(["node", AST, "--index-only"] + paths, capture_output=True, env=env,
                           timeout=60 + 20 * len(paths))
        return json.loads(r.stdout or b"{}")
    except Exception as e:                                       # noqa: BLE001
        print(f"  ! index pass failed on {len(paths)} files: {str(e)[:120]}", file=sys.stderr)
        return {}


# WHETHER THE EXTRACTOR RAN AT ALL, kept separately from what it found.
#
# With @babel/parser missing, every batch failed and analyze.py still printed
# "3973 scripts -> 74 records" and offered to write them to the store. 74 regex-recovered config values
# reads as a thin result for a thin app; it was a broken toolchain reporting itself as a finding. The
# per-batch warnings scroll past in a long run. The ratio does not.
_AST_OK, _AST_FAILED, _AST_WHY = [0], [0], set()


def _run(tool, paths):
    """One node process per batch, files passed as ARGUMENTS, never concatenated.

    Concatenating them into one stdin stream is the obvious optimisation and it is wrong: two independently
    minified modules will both declare a top-level `v`, Babel raises `Duplicate declaration "v"`, and the
    extractor returns [] for the ENTIRE batch. Which does not look like a crash, it looks like a host with
    no API. The extractor already accepts multiple paths and tags each record with the file it came from.
    """
    if not paths:
        return []
    try:
        env = node_env(); env["NODE_PATH"] = NODE_PATH
        extra = ["--config", _CONFIG_FILE] if _CONFIG_FILE else []
        # The module index and the full file list, both built once over the WHOLE corpus. Without them a
        # batch can only resolve an import whose defining file happened to land in the same process.
        if _INDEX_FILE[0]:
            extra += ["--index", _INDEX_FILE[0]]
        if _FILESET_FILE[0]:
            extra += ["--fileset", _FILESET_FILE[0]]
        r = subprocess.run(["node", tool] + paths + extra, capture_output=True, env=env,
                           timeout=60 + 30 * len(paths))
        # NOT `and not r.stdout`: the extractor's own top-level catch prints `[]` before
        # exiting non-zero, so a crashed batch always has stdout and was counted as a success.
        why = node_failure(r)
        if why:
            print(f"  ! extractor failed on {len(paths)} files: {why}", file=sys.stderr)
            _AST_FAILED[0] += len(paths)
            _AST_WHY.add(why[:120])
            return []
        _AST_OK[0] += len(paths)
        # The extractor's own diagnostics are on stderr and capture_output swallows them, so
        # BP_DEBUG=1 printed nothing when run through here, which is how four attempts at the
        # body-parameter gap were each evaluated against an invisible pass.
        if os.environ.get("BP_DEBUG") and r.stderr:
            sys.stderr.write(r.stderr.decode(errors="replace"))
        return json.loads(r.stdout or b"[]")
    except Exception as e:
        print(f"  ! extractor error on {len(paths)} files: {str(e)[:160]}", file=sys.stderr)
        _AST_FAILED[0] += len(paths)
        _AST_WHY.add(f"{type(e).__name__}: {str(e)[:110]}")
        return []


_BATCH_SIZE = [BATCH_BYTES]


def batches(paths):
    cur, size = [], 0
    for p in paths:
        try:
            n = os.path.getsize(p)
        except Exception:
            continue
        if cur and size + n > _BATCH_SIZE[0]:
            yield cur
            cur, size = [], 0
        cur.append(p); size += n
    if cur:
        yield cur



# Tokens the code carries in a ROUTING HEADER rather than in a hostname, learned from the bundle.
_HEADER_ROUTED = {}          # token -> the header name that carries it

# `["alpha","beta","gamma","delta"].includes(r) && (p["x-service"] = r)`
#
# One regex for one very specific shape: a list of string literals, `.includes(X)`, and an assignment
# of that same X to a header-shaped key. Tying the list to the assignment through the SAME identifier
# is what makes this safe, a bare list of lowercase words is nothing, and a bare header name says
# which header but not which values go in it.
_ROUTING_HEADER = re.compile(
    r"""\[((?:\s*["'][a-z0-9_.-]{2,24}["']\s*,?){2,12})\]\s*\.includes\(\s*([A-Za-z_$][\w$]*)\s*\)"""
    r"""\s*&&\s*\(?[^;{}]{0,40}?\[\s*["']([a-zA-Z0-9-]*(?:host|service|target|backend)[a-zA-Z0-9-]*)["']\s*\]"""
    r"""\s*=\s*\2\b""",
    re.I)


def _find_header_routing(bodies):
    """Which service tokens does this application carry in a HEADER?

    The whole mgmt misattribution turns on one question (when a bundle says `apiHost: APIHost.Mgmt`,
    is `mgmt` a hostname or a header value?), and the application answers it in as many words:

        const {apiGatewayHost: l} = t.session, c = l + a;          // URL is ALWAYS gateway + path
        ["alpha","beta","gamma","delta"].includes(r) && (p["x-service"] = r);

    Both facts are in the source and neither needs an observation, which matters because the repair
    that reads them off the wire cannot run on a first crawl, there is nothing observed yet, and
    that is exactly when a map is most likely to be quoted and least likely to be checked.

    That application ALSO publishes `window._adminHost = "//mgmt.app.client.example"`, so the host exists and
    is real; the application simply does not send these calls to it. A declared hostname is not
    evidence of transport, and this is what outranks it.
    """
    found = {}
    for pth in js_files(bodies, with_inline=True):
        try:
            with open(pth, errors="replace") as fh:
                src = fh.read()
        except OSError:
            continue
        for toks, _ident, header in _ROUTING_HEADER.findall(src):
            for t in re.findall(r"""["']([a-z0-9_.-]{2,24})["']""", toks, re.I):
                found.setdefault(t.lower(), header.lower())
    return found


def _declared_gateway(declared):
    """The ONE API base the page declares, when it declares exactly one.

    A service token in the bundle (`apiHost: APIHost.Alpha`) does not have to name a HOST. One application routes
    six logical services through a single gateway and selects between them with a request header:

        u = session.apiGatewayHost + url;
        ["alpha","beta","gamma","delta"].includes(apiHost) && (headers["x-service"] = apiHost)

    So `scout` will never appear in the declared-host map, because it is not a host, and before this the
    row fell through to the loading document, which put 51 API endpoints on a Webflow marketing site. The
    document is not merely a weak guess for these rows, it is a KNOWN-WRONG one: the code said in as many
    words that the base comes from somewhere else.

    Returns a host only when the page declares exactly one gateway-ish base, so this can never silently
    pick between two candidates. The key is the application's own, same as _declared_host_map.
    """
    if not declared:
        return None
    cands = {v for k, v in declared.items() if "gateway" in k or k in ("api", "apibase", "backend")}
    return cands.pop() if len(cands) == 1 else None


def _declared_host_map(cfg):
    """Service token -> hostname, from the base URLs the served page declares.

    A bundle names its backends with a symbol (`apiHost: APIHost.Mgmt`) and the page injects what those
    symbols mean (`window._adminHost = "//mgmt.app.client.example"`). Both halves are the APPLICATION's
    own identifiers, this correlates two things the target said, rather than matching a vocabulary we
    invented, which is why it generalises past one naming convention.

    Normalised on both sides by stripping the decoration a config key carries (`_`, `Hostname`, `Host`,
    `Url`, `BaseUrl`) and lowercasing, so `Mgmt` meets `_adminHost`.
    """
    out = {}
    for k, v in (cfg or {}).items():
        if not isinstance(v, str) or not v:
            continue
        h = re.sub(r"^//", "", v.strip())
        h = re.sub(r"^https?://", "", h).split("/")[0].split("?")[0]
        if not h or "." not in h or " " in h:
            continue
        raw = str(k).strip().lstrip("_").lower()
        # The KEY has to claim to be a base URL. Without this, every host-shaped string in the page
        # qualifies and JSON-LD walks straight in, `@context=schema.org`, `twitter:image=cdn...`,
        # `availability=schema.org`, any of which could collide with a service token and send a whole
        # route family to a host the application never mentioned. 24 candidates became the 5 real ones.
        if not re.search(r"(hostname|host|baseurl|url|origin|endpoint|domain|server|gateway)$", raw):
            continue
        key = re.sub(r"(hostname|baseurl|host|url|origin|endpoint|domain|server|gateway)$", "", raw)
        if key:
            out.setdefault(key, h)
    return out


def require_corpus(cmd, path):
    """Refuse a path that cannot be a corpus, rather than reporting zero of everything.

    `os.walk` on a path that is not there yields nothing, so a mistyped directory, or a file, or a
    URL, which is the easy mistake because every FETCHING command takes one, came back as
    `0 scripts -> 0 records` and exit 0. That reads as "this host has no API": a claim about the
    target, made out of a typo. A confident zero is the one output this tool must never produce
    quietly, which is the same reason `residual` and `precision` exist.
    """
    if os.path.isdir(path):
        return
    if "://" in str(path):
        raise SystemExit(f"{cmd}: {path!r} is a URL, this reads a directory of already-fetched "
                         f"files; run `jshound fetch {path} -o ./bodies` first")
    if os.path.isfile(path):
        here = os.path.dirname(str(path)) or "."
        raise SystemExit(f"{cmd}: {path!r} is a file, this reads a DIRECTORY of saved bodies "
                         f"(try `jshound {cmd} {here}`)")
    raise SystemExit(f"{cmd}: {path!r}: no such directory")


def _readable_non_js(d):
    """Is there anything here that a non-JavaScript channel reads, a document, or a captured body?"""
    for root, _, names in _corpus_walk(d):
        for n in names:
            if n.startswith("_"):
                continue
            if n.endswith((".html", ".htm", ".json", ".rsc")):
                return True
    return False


def analyze(bodies, host=None, manifest=None):
    """Attribute each record to the host that actually SERVED its file, not to one flag.

    `--host X` stamping every record was fine when a bodies dir held one host and wrong the moment it held
    six: the same directory shared by parallel crawls made every bundle look like it came from whichever
    host was named. Now the origin map decides per file, and `--host` is only the fallback for files whose
    origin is unknown. Records are also filtered to the named host when one is given, so analysing a shared
    directory per host is correct instead of merely convenient.

    USUALLY YOU WANT TO OMIT IT. The origin map places every file on its own, so passing a host mostly
    just discards the rest of the directory, measured on one corpus, `--host www.example.com` cut
    9,262 records to 1,640 and took every other host's endpoints with it. Pass one only when you are
    deliberately analysing a shared bodies directory one host at a time, and remember that it also claims
    every record whose origin is unknown (those rows say so in their note).
    """
    require_corpus("analyze", bodies)
    paths = js_files(bodies)
    # NO JAVASCRIPT IS NOT NO SURFACE. This returned immediately when `js_files()` came back empty,
    # which skipped every channel that does not read JavaScript: `markup_records` (whose own docstring
    # says "on a server-rendered application the markup IS the surface"), `json_records` (captured API
    # responses), `doc_state_records` and the page-injected config.
    #
    # Two real inputs hit this. A HAR of an API-heavy session imports as documents and response bodies
    # with no script among them, and that is the one route this project has to an authenticated
    # surface. A server-rendered page with forms and no bundle is the other. Both produced
    # "0 scripts -> 0 records" while holding the surface in plain sight.
    #
    # The guard still exists, just moved: if there is nothing of ANY kind to read, there is nothing to
    # do. Everything downstream already tolerates an empty `paths`, the JS passes iterate over it.
    if not paths and not _readable_non_js(bodies):
        return [], paths
    # Harvest the page-injected config BEFORE parsing any JavaScript, so the extractor can resolve values
    # that appear nowhere in the code.
    global _CONFIG_FILE
    cfg = config_values(bodies)
    if cfg:
        import tempfile
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(cfg, fh); fh.close()
        _CONFIG_FILE = fh.name
        print(f"  {len(cfg)} runtime config value(s) from injected HTML "
              f"({', '.join(list(cfg)[:5])}…)", file=sys.stderr)
        # Emit them as records too. A template that keeps `{baseUrl}` is only useful if the candidate
        # values are somewhere a reader can find them, the slot names what is missing, these say what it
        # probably is, and keeping them apart is the difference between evidence and a guess.
        _CONFIG_RECORDS[:] = [
            {"_kind": "config-value", "name": k, "value": v, "source": "injected-html"}
            for k, v in cfg.items()
        ]
    global _DECLARED_HOSTS, _DECLARED_GATEWAY
    _DECLARED_HOSTS = _declared_host_map(cfg)
    _DECLARED_GATEWAY = _declared_gateway(_DECLARED_HOSTS)
    if _DECLARED_HOSTS:
        print(f"  {len(_DECLARED_HOSTS)} backend host(s) declared by the served page: "
              + ", ".join(f"{k}={v}" for k, v in list(_DECLARED_HOSTS.items())[:6]), file=sys.stderr)
    if _DECLARED_GATEWAY:
        print(f"  single API gateway declared by the page: {_DECLARED_GATEWAY}. Wrapper-composed URLs "
              f"whose service token is not itself a host resolve here, not to the document", file=sys.stderr)
    global _HEADER_ROUTED
    _HEADER_ROUTED = _find_header_routing(bodies)
    if _HEADER_ROUTED:
        _hdr = sorted(set(_HEADER_ROUTED.values()))[0]
        print(f"  {len(_HEADER_ROUTED)} service token(s) are carried in the {_hdr!r} HEADER, not in a "
              f"hostname: {', '.join(sorted(_HEADER_ROUTED))}\n"
              f"    so they address the gateway even where the page also declares a host for them",
              file=sys.stderr)

    origins = _origin_map(manifest, bodies)
    documents = _document_map(bodies)

    # SAY WHICH SCRIPTS HAVE NO PROVENANCE, BEFORE ANALYSING THEM.
    #
    # A body file with no entry in the origin map has no document and no serving host, so every endpoint
    # recovered from it is attributed by the `--host` fallback, or by nothing at all. That is survivable
    # when it is a stray; it is not survivable when it is the application bundle. On one corpus the
    # unregistered files were the seven hand-fetched `app_*.js`, and `app_index.js` alone carried 448 of
    # the target's API endpoints: the most important file in the analysis was the one nothing could place.
    #
    # A crawler registers every body it saves, appending as it goes, so an
    # unregistered file means a human or an agent put it there by hand. Naming them costs one line and
    # turns a silent misattribution into a question somebody can answer.
    _unreg = [os.path.basename(p_) for p_ in paths
              if ".inline" not in os.path.basename(p_)
              and os.path.basename(p_).split("?")[0] not in origins
              and os.path.basename(p_).split("?")[0] not in documents]
    if _unreg:
        _big = sorted(_unreg, key=lambda n: -os.path.getsize(os.path.join(bodies, n))
                      if os.path.exists(os.path.join(bodies, n)) else 0)
        print(f"  ! {len(_unreg)} script(s) have NO recorded origin. Nothing says which document loaded "
              f"them, so their endpoints are attributed by the --host fallback or not at all: "
              f"{', '.join(_big[:6])}" + (f" (+{len(_big)-6} more)" if len(_big) > 6 else ""),
              file=sys.stderr)
        print(f"    fix: python -m jshound.register_origin {bodies} <file> <url>",
              file=sys.stderr)

    # THE BODIES DIRECTORY IS CONTENT-ADDRESSED. HONOUR THAT.
    #
    # The crawler names each body after the hash of its contents, so the same bundle fetched twice is one
    # file. A script fetched by hand lands beside it under a different name, and then it is not one file:
    # it is parsed twice and every endpoint in it is recorded twice. Measured on one corpus, all seven
    # hand-fetched `app_*.js` were byte-identical to bodies already on disk, 4.5 MB reparsed, and 450
    # duplicate endpoint records which, having no registered origin, were then claimed by the --host
    # fallback and filed under a second host.
    #
    # Deduplicate on the bytes, and when there is a choice keep the copy that has provenance: an origin
    # entry is what decides the host, so the registered twin is strictly more useful than its clone.
    _by_hash, _drop = {}, []
    for p_ in paths:
        if ".inline" in os.path.basename(p_):
            continue                                  # extracted by us into a temp dir; not a fetched body
        try:
            with open(p_, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            continue
        prev = _by_hash.get(digest)
        if prev is None:
            _by_hash[digest] = p_
            continue
        registered = lambda x: os.path.basename(x).split("?")[0] in origins or \
                               os.path.basename(x).split("?")[0] in documents
        keep, toss = (prev, p_) if registered(prev) or not registered(p_) else (p_, prev)
        _by_hash[digest] = keep
        _drop.append(toss)
    if _drop:
        _dropset = set(_drop)
        paths = [p_ for p_ in paths if p_ not in _dropset]
        print(f"  {len(_drop)} duplicate body/bodies skipped (identical bytes under another name): "
              f"{', '.join(sorted(os.path.basename(x) for x in _drop)[:5])}"
              + (f" (+{len(_drop)-5} more)" if len(_drop) > 5 else ""), file=sys.stderr)


    def host_of(path):
        u = origins.get(os.path.basename(path).split("?")[0])
        if not u:
            return None
        try:
            return urlparse(u).netloc or None
        except Exception:
            return None

    # WHICH HOST A RELATIVE PATH BELONGS TO.
    #
    # `host_of` answers "who served this file", which is the right question for an absolute URL and the
    # WRONG one for a relative path. A browser resolves `/svc/graphql` against the DOCUMENT's origin and
    # never against the origin of the script that contains it, so a bundle served from redditstatic.com
    # that fetches `/svc/shreddit/graphql` is making a request to www.reddit.com.
    #
    # This was invisible while only same-host bodies were saved, server and document were the same host.
    # Once a page's bundles are kept wherever their CDN lives it stops being true: reddit.com's 210 scripts
    # arrive from 47 hosts, and attributing their relative paths to the CDN files the application's entire
    # API under a host that does not serve it. `_document.json` records the loading document; where it is
    # absent (a run crawled by an older build) we fall back to the serving host, which is what the
    # old behaviour was anyway.
    def doc_host_of(path):
        u = documents.get(os.path.basename(path).split("?")[0])
        # AN INLINE SCRIPT'S DOCUMENT IS THE PAGE IT IS INLINE IN.
        #
        # inline_scripts() writes each <script> block to `<htmlname>.inline<N>.js`, and the origin map is
        # keyed by the HTML file's basename, so this lookup missed every one of them. On one target that
        # was 3,393 of 4,372 scripts: no document, so no host, so the records fell through to the `--host`
        # fallback, which claims them for whatever host that pass happened to name. Run the pass twice for
        # two hosts and the SAME endpoints are confidently filed under both, which is how 857 endpoints
        # came to sit under two hosts at once and a Webflow marketing site acquired 821 API routes.
        #
        # The document was known the whole time. Strip the suffix this tool itself added and ask again.
        if not u:
            m = re.match(r"^(.*\.html?)\.inline\d+\.js$", os.path.basename(path))
            if m:
                u = documents.get(m.group(1))
        if not u:
            return None
        try:
            return urlparse(u).netloc or None
        except Exception:
            return None

    recs = []
    # Parsing is CPU-bound and embarrassingly parallel, one node process per batch, no shared state, so
    # a hardcoded 4 left twelve cores idle on a sixteen-core machine during the slowest step of the whole
    # pipeline. Scale to the box, leaving one core for everything else. Each node process is given ~3GB, so
    # cap by memory too rather than spawning sixteen parsers on a small container.
    _workers, _bsize = plan_parallelism(paths)
    _BATCH_SIZE[0] = _bsize
    print(f"  parsing {len(paths)} scripts with {_workers} workers, {_bsize/1e6:.1f}MB batches",
          file=sys.stderr)
    # PASS ONE: the module index, over everything, before any extraction. See _build_module_index.
    _ORIGINS_FOR_ALIAS[0] = origins
    _BODIES_DIR[0] = bodies
    _build_module_index(paths, _workers, list(batches(paths)))
    with ThreadPoolExecutor(max_workers=_workers) as ex:
        for batch, got in zip(list(batches(paths)),
                              ex.map(lambda b: _run(AST, b), list(batches(paths)))):
            # A batch is several files in one node process; records carry _chunk, the index within the
            # batch, so each one can still be traced back to the file it came from.
            for r in got:
                i = r.get("_chunk")
                src = batch[i] if isinstance(i, int) and 0 <= i < len(batch) else None
                served = host_of(src) if src else None
                # Three rules, in order of how much the record itself tells us:
                #
                #   absolute template -> the host it NAMES. It was previously credited to the host that
                #     served the file, which meant `https://api.linkedin.com/v2/x` found inside a bundle on
                #     static.licdn.com was attributed to the CDN and therefore dropped by BOTH the
                #     `--host www.linkedin.com` pass and the `--host api.linkedin.com` pass. A record that
                #     states its own host should never be filed under a different one.
                #   relative template -> the DOCUMENT that loaded the file. This is the browser's own rule:
                #     a relative URL resolves against the document's origin, not the script's.
                #   neither -> the serving host, which is all we know.
                #
                # A templated host (`https://license.{EXPR}/x`) names nothing resolvable, so it falls
                # through to the serving host rather than inventing a hostname with a brace in it.
                t = str(r.get("template") or "")
                # PATH-relative is relative too. `account/deactivate` resolves against the document exactly
                # as `/account/deactivate` does, the browser rule is about the ORIGIN, and a leading slash
                # only decides how much of the document's path is kept. Requiring one sent every bare
                # relative call to the host that served the SCRIPT: x ships its bundles from abs.twimg.com,
                # so `account/verify_password`, `account/deactivate` and 200 more legacy API calls were
                # filed under a CDN that serves none of them, while the origin map had recorded the real
                # document, x.com, for all 1,656 of those files.
                #
                # A template whose base is an unresolved slot is NOT relative in this sense: `{apiBase}/foo`
                # names a host we never resolved, and the document is a guess rather than the browser's
                # answer. Those keep the serving host and print with a {BASE?} marker saying so.
                named = _abs_host(t)
                rel = bool(t) and not named and not t.startswith("//") \
                    and not re.match(r"^[a-z][a-z0-9+.\-]*:", t) and not t.startswith("{")
                # A WRAPPER-COMPOSED URL IS NOT A RELATIVE URL.
                #
                # The browser's rule, resolve a relative path against the document origin, applies to a
                # string the browser resolves. It does not apply to a fragment handed to a client that
                # prepends a base, because that string never reaches the URL resolver:
                #     sendRequest({apiHost: APIHost.Beta, method:"post", url:"/v1/compliance-check"})
                # The extractor now flags those (`base_unresolved`, with the selector key and the service
                # token it names). Applying the document rule to them filed 821 API endpoints under a
                # Webflow marketing site on one target and put 857 endpoints under two hosts at once.
                #
                # We still have to write SOME host, the column is NOT NULL and a row with no host helps
                # nobody, so the document remains the best guess. What changes is that it stops being
                # recorded as if it were the browser's answer: `unresolved` says out loud that the base was
                # chosen by application code we could not follow, and `base_hint` carries the service token
                # so a later pass with evidence can rehome the whole group at once.
                if r.get("base_unresolved") and rel:
                    # …unless the page told us what that service is. Both halves are the target's own
                    # naming: the bundle says `apiHost: APIHost.Mgmt`, the served HTML says
                    # `window._adminHost = "//mgmt.app.client.example"`. Where they meet, this stops
                    # being a guess and becomes the application's own answer.
                    hint = str(r.get("base_hint") or "").strip().lower()
                    declared = _DECLARED_HOSTS.get(hint) if hint else None
                    # A TOKEN CARRIED IN A HEADER IS NOT AN ADDRESS, even when a host of that name
                    # is also declared. This branch ordering was backwards and it cost a third of
                    # that application's map: the page publishes `_adminHost` AND routes admin calls through
                    # the gateway with `x-service: mgmt`, so `declared` won and 331 endpoints were
                    # filed under a hostname the application never sends to. Both facts were in the
                    # bundle the whole time; only one of them is about transport.
                    routed = _HEADER_ROUTED.get(hint) if hint else None
                    if routed and _DECLARED_GATEWAY:
                        r["_resolved_base"] = _DECLARED_GATEWAY
                        r["unresolved"] = (
                            f"{r.get('base_key')}={r.get('base_hint')} is carried in the "
                            f"{routed!r} header, not in the hostname: the request builder composes "
                            f"the URL from the gateway ({_DECLARED_GATEWAY}) and names the service "
                            f"in that header."
                            + (f" The page also declares {declared} for it; the application does "
                               f"not send these there." if declared else ""))
                    elif declared:
                        r["_resolved_base"] = declared
                        r["unresolved"] = (f"host resolved from the page's own config: "
                                           f"{r.get('base_key')}={r.get('base_hint')} -> {declared}")
                    elif _DECLARED_GATEWAY:
                        # The token is not a host, this app selects a SERVICE behind one gateway, by
                        # header. The document origin is known-wrong here, and the page named the gateway.
                        r["_resolved_base"] = _DECLARED_GATEWAY
                        r["unresolved"] = (f"{r.get('base_key')}={r.get('base_hint')} is a service token, "
                                           f"not a declared host; resolved to the single API gateway the "
                                           f"page declares ({_DECLARED_GATEWAY})")
                    else:
                        # Nothing legitimate left to say. The document origin is the one answer we KNOW
                        # to be wrong, so it is not used: a row parked on a reserved name is visibly
                        # unattributed and rehomable as a group via base_hint, whereas 51 phantom
                        # endpoints on a marketing site are indistinguishable from real ones.
                        r["_resolved_base"] = "unattributed.invalid"
                        r["unresolved"] = (f"base URL chosen by a client wrapper "
                                           f"({r.get('base_key')}={r.get('base_hint')}) and the page "
                                           f"declares no matching host; NOT attributed to the loading "
                                           f"document, which the code rules out")
                if r.get("_resolved_base"):
                    h = r["_resolved_base"]
                elif named:
                    h = named
                elif rel and src:
                    h = doc_host_of(src) or served
                else:
                    h = served
                if h:
                    r["_host"] = h
                    # The PAGE, not just its host. "Declared somewhere in a 7MB bundle" is not a work item;
                    # "declared by code that /app/reports loads" is.
                    r["_page"] = documents.get(os.path.basename(src or "")) if src else None
                elif host:
                    # THE --host FALLBACK IS A GUESS AND MUST SAY SO.
                    #
                    # This claims every record whose file has no recorded origin. On one target that was
                    # `app_index.js`, 448 API endpoints, the single most valuable file in the analysis,
                    # fetched by hand rather than by the crawler, so it never got an `_origin.jsonl` entry.
                    # The records came out looking exactly as confident as the ones we had observed, and a
                    # second pass for a second host claimed the same records again.
                    r.setdefault("_host", host)
                    r["unresolved"] = (f"host is the --host fallback ({host}): this script has no recorded "
                                       f"origin, so nothing established which document loaded it")
                # The file this record came from. `_chunk` is an index within a BATCH, so it identifies a
                # file only in combination with that batch, useless downstream and actively misleading if
                # compared across batches. Record the path itself while we still have it.
                if src:
                    r["_file"] = src
            recs.extend(got)

    recs.extend(_CONFIG_RECORDS)
    recs.extend(markup_records(bodies, origins))
    # Server state serialised into an attribute, which `_LOOKS_URL` rejects for starting with a brace
    # and so reaches nothing else here. On the corpora measured this is the application's declared auth
    # surface: Datadog's login/OIDC/forgot-password/recaptcha/TOTP/MFA endpoints from a hidden input in
    # a document with no form, and three Slack OAuth entry points that `grep` finds in zero files
    # because JSON escapes the slashes.
    recs.extend(doc_state_records(bodies, origins))
    # Captured JSON response bodies. js_files() has never read .json, so an API response that describes the
    # API went unread, 61 paths on one scope, 53 on x, produced by no other channel.
    recs.extend(json_records(bodies, origins, documents))

    # `gql-endpoint` records pass straight through to ingest, which is where the GraphQL gateway is
    # resolved and always was (see `gql_gw` in ingest.py). A pass lived here that tried to do the same
    # job over `recs` and matched operations on `kind == "graphql"`, the extractor emits them as
    # `_kind: "relay"`, so it addressed nothing on real data while its tests, written against the
    # vocabulary it assumed rather than the one the extractor produces, all passed.
    #
    # A file's own request builder answers calls in that file whose construction we could not read.
    recs = _apply_sole_client_encoding(recs)

    # Union call sites for the same endpoint, now that every record carries the host that loaded its file.
    before_merge = len(recs)
    recs = _merge_by_endpoint(recs)
    if before_merge != len(recs):
        print(f"  merged {before_merge - len(recs)} duplicate call site(s) into their endpoints "
              f"(per host, which the extractor cannot do)", file=sys.stderr)

    # SUPPRESS path literals that a call site already explained, globally, which is the point.
    #
    # collectPathLiterals is a blind sweep: every string in a file, keep anything path-shaped. That is
    # deliberate, because a route table declares its routes as bare strings with no call site. The cost is a
    # second row for every path some OTHER file also calls properly, and the second row reads worse than
    # redundant, `UNKNOWN /rest/issue-report, no call site attributed` next to
    # `POST /rest/issue-report [settingsUserIssueReportRaw]` looks like a gap we do not have.
    #
    # The extractor already does this, twice, and neither reaches far enough: `claimed` is built from ONE
    # FILE, and the cross-file merge sees one BATCH, because _run() spawns a node process per batch. This is
    # the first place that holds every record from every batch, so this is where the reconciliation belongs.
    # Measured: 426 survivors on x, 411 on claude, 383 on one target after the per-batch pass had run.
    #
    # Per HOST, because the same path on two hosts is two endpoints.
    _slot = re.compile(r"\{[^}]*\}")
    explained = set()
    for r in recs:
        m = str(r.get("method") or "").upper()
        if not m or m in ("UNKNOWN", "NONE"):
            continue
        t = r.get("template") or r.get("path")
        if t:
            explained.add((r.get("_host"), _slot.sub("*", str(t))))
    if explained:
        before = len(recs)
        recs = [r for r in recs
                if r.get("_kind") != "path-literal"
                or (r.get("_host"), _slot.sub("*", str(r.get("template") or r.get("path") or "")))
                not in explained]
        if before != len(recs):
            print(f"  {before - len(recs)} path literal(s) dropped: a call site elsewhere in the bundle "
                  f"already recovered their method", file=sys.stderr)

    if host:
        known = [r for r in recs if r.get("_host")]
        if any(r["_host"] != host for r in known):
            drop = len([r for r in known if r["_host"] != host])
            print(f"  attributed by origin: keeping {len(recs)-drop} record(s) served by {host}, "
                  f"dropping {drop} served by other hosts in this shared bodies dir", file=sys.stderr)
            recs = [r for r in recs if r.get("_host") in (host, None)]

    # A REQUEST WITH A BODY BUT NO URL IS NOT AN ENDPOINT, and calling it one made the counts lie:
    # tldraw reported 74 endpoints while its OpenAPI export had 66 paths, because eight records had
    # a resolved body and an unresolved URL and every exporter silently dropped them.
    #
    # They are not noise, tldraw's `["token","distinct_id","groups",…]` is PostHog's capture
    # payload and `["method","jsonrpc","id"]` is a JSON-RPC envelope, both worth knowing about. So
    # they keep their contents and get their own kind, which makes the endpoint count match what
    # exports, and makes "something POSTs a JSON-RPC envelope from here" findable instead of
    # hidden among real endpoints.
    nourl = 0
    for r in recs:
        if (r.get("_kind") or "endpoint") == "endpoint" and not (r.get("template") or r.get("url")):
            r["_kind"] = "request-no-url"
            r.setdefault("note", "a request was recovered, method and body, but its URL did not "
                                 "resolve. Not an endpoint; it has no address to send to.")
            nourl += 1
    if nourl:
        print(f"  {nourl} request(s) had a body but no resolvable URL, recorded as "
              f"request-no-url, not counted as endpoints", file=sys.stderr)
    return recs, paths


_SLOT = re.compile(r"\{[^}]*\}")
# An internal marker, or a name the minifier chose. Neither tells a reader what belongs in the slot.
_POOR_SLOT = re.compile(r"^(EXPR|min|max|search|hash|call:.*|param:[a-z_$][a-z0-9_$]?|[a-z_$][a-z0-9_$]?)$",
                        re.I)


def _slot_quality(t):
    """How informative are this template's slot names? Higher is better.

    `/project/{ref}/settings/log-drains` and `/project/{s}/settings/log-drains` are one endpoint written
    twice, and only the first says what to put there.
    """
    names = _SLOT.findall(str(t or ""))
    if not names:
        return 1000                                   # no slots at all: nothing to improve on
    good = sum(0 if _POOR_SLOT.match(n.strip("{}")) else 1 for n in names)
    return good * 10 - len(names)


_MERGE_FILL = ("op", "security", "media_type", "req_headers", "query_schema", "body_schema",
               "group", "body_from", "credentials", "body_encoding", "content_type")


def _apply_sole_client_encoding(recs):
    """Adopt a file's encoding rule for calls in that file whose own construction did not say.

    collectClientEncoding reads what a request builder does with a body, one target's says
    `r instanceof FormData ? s.body = r : s.body = JSON.stringify(r)` outright. That is a fact about the
    app, but it is not automatically a fact about a given CALL: `L().post(url, body)` reaches that builder
    through a factory, a class and two hops, and the link is cross-module.

    Where the builder is in the SAME FILE as the call, and that file defines exactly ONE rule, the
    inference is a short one: the only HTTP layer in this file encodes bodies this way. Measured across the
    corpus that covers 208 of claude's 414 unresolved writes, 44 of x's 385, 15 of one target's 285.

    It is still an inference, so it is LABELLED as one. `body_encoding_basis` says where the answer came
    from, a construction we read, a content-type the client declared, the HTML spec for a form, or this,
    and a reader who only trusts the first three can filter on it. Recording the value and hiding its
    provenance is how a guess becomes indistinguishable from a measurement.
    """
    WRITE = {"POST", "PUT", "PATCH", "DELETE"}

    def resolved(rule):
        # A conditional rule resolves to json for a body that is not a FormData. We did not see the body's
        # construction, that is why we are here, so the multipart branch stays named in the basis rather
        # than silently dropped.
        return "json" if rule == "json-unless-multipart" else rule

    rules, byhost = {}, {}
    for r in recs:
        if r.get("_kind") != "client-encoding" or not r.get("rule"):
            continue
        if r.get("_file"):
            rules.setdefault(r["_file"], set()).add(r["rule"])
        byhost.setdefault(r.get("_host"), set()).add(resolved(r["rule"]))
    sole = {f: next(iter(v)) for f, v in rules.items() if len(v) == 1}
    # SECOND TIER: the whole HOST agrees. A file-local rule is the shortest inference, but a modern app
    # puts its HTTP layer in one shared module and calls it from two hundred others, so the file tier
    # never fires where it is needed most, one target's createRequestOptions is read correctly and sits in a
    # different file from all 249 of the calls that reach it.
    #
    # Unanimity is what makes this safe, and it is the same test the property index already applies: adopt
    # a value only where the bundle does not contradict itself. Every request builder attributed to this
    # host encodes bodies the same way, so a call on this host whose own construction we could not read
    # encodes that way too. Where a host's builders DISAGREE it refuses, which is most of x, claude and
    # one target, and those hosts genuinely do mix json, multipart and raw. The rule declines to answer
    # exactly where the evidence is mixed.
    host_sole = {h: next(iter(v)) for h, v in byhost.items() if h and len(v) == 1}
    n = m = 0
    for r in recs:
        if r.get("body_encoding"):
            r.setdefault("body_encoding_basis", "read from the call site")
            continue
        if str(r.get("method") or "").upper() not in WRITE:
            continue
        # A request we READ and found to send nothing has no encoding to infer. Labelling
        # `DELETE /workspaces/{id}/files/{file_id}` as json-encoded states a property of a body that does
        # not exist, and contradicts the no-body fact printed beside it.
        if r.get("body_empty") or r.get("body_keys") == []:
            continue
        rule = sole.get(r.get("_file"))
        if rule:
            r["body_encoding"] = resolved(rule)
            r["body_encoding_basis"] = (
                "INFERRED: the only request builder in this file encodes bodies as " + rule +
                "; this call's own body construction was not resolvable")
            n += 1
            continue
        henc = host_sole.get(r.get("_host"))
        if henc:
            r["body_encoding"] = henc
            r["body_encoding_basis"] = (
                "INFERRED: every request builder found on " + str(r.get("_host")) + " encodes bodies as "
                + henc + "; this call's own body construction was not resolvable, and its client is in "
                "another module")
            m += 1
    if n:
        print(f"  {n} body encoding(s) inferred from the sole request builder in the same file "
              f"(labelled as inferred, not read)", file=sys.stderr)
    if m:
        print(f"  {m} body encoding(s) inferred from a host whose request builders all agree "
              f"(labelled as inferred, not read)", file=sys.stderr)
    return recs

def _merge_by_endpoint(recs):
    """Union call sites that describe the SAME endpoint, after the host is known, which is the point.

    The extractor unions bodies across call sites, which is right: an endpoint described twice, once by a
    call site and once by a declarative table, should end up with both halves. But it keys that union on
    (method, template) and it does not know hosts, only this module does, from the origin map.

    So two files loaded by DIFFERENT documents that both POST to /api/shared collapsed into one record. The
    second host's endpoint disappeared, and the survivor's body became the union of both: a contract
    claiming a field that only the other host's code sends. That is fabrication, not omission, and it fires
    exactly where this tool normally runs, one bodies directory holding several hosts' bundles, code served
    from a CDN under a host that is not the app's.

    The extractor now keys its merge per file, so this is where the real union happens, with the host in the
    key. `op` joins the key only for kinds whose identity IS an operation; for a REST route it is the
    developer's function name and two names for one path are still one path.
    """
    OP_KEYED = {"ws-message", "graphql", "server-action", "relay"}
    out, seen = [], {}
    for r in recs:
        t = r.get("template") or r.get("path")
        if not t:                                   # not an endpoint; nothing to merge on
            out.append(r)
            continue
        kind = r.get("kind") or r.get("_kind")
        # KEYED ON THE SLOT-ERASED SHAPE, so one endpoint is one row whatever the code called its
        # parameter. Minified bundles name the same parameter differently at every call site, and each
        # spelling became its own endpoint: x had `/{IY}/affiliates`, `/{c}/affiliates` and
        # `/{username}/affiliates`; supabase had `/project/{r}/…`, `/project/{s}/…` and
        # `/project/{ref}/settings/log-drains`. About 530 duplicate rows across the corpus, every one of
        # them the same request.
        #
        # This erases only SLOTS. A literal path is untouched, so `/server/19/` and `/server/33/` stay two
        # rows, the distinction the codebase deliberately protects, since nothing can tell a version from
        # an object id by looking at the digits.
        key = (r.get("_host"), str(r.get("method")), _SLOT.sub("{}", str(t)),
               str(r.get("op")) if (r.get("op") and kind in OP_KEYED) else "")
        prev = seen.get(key)
        if prev is None:
            seen[key] = r
            out.append(r)
            continue
        # Keep the best-NAMED spelling. The name is not invented, it is the one the application's own code
        # used somewhere, and `{ref}` is worth more to a reader than `{s}`. The spellings that lose are
        # recorded rather than dropped, because a caller naming it `{slug}` and another `{id}` is a fact
        # about the endpoint.
        if _slot_quality(r.get("template")) > _slot_quality(prev.get("template")):
            losing = prev.get("template")
            prev["template"] = r.get("template")
        else:
            losing = r.get("template")
        if losing and losing != prev.get("template"):
            alts = prev.setdefault("template_alts", [])
            if losing not in alts:
                alts.append(losing)
        for f in ("body_keys", "response_keys"):
            if r.get(f):
                if not prev.get(f):
                    prev[f] = list(r[f])
                else:
                    for v in r[f]:
                        if v not in prev[f]:
                            prev[f].append(v)
        if isinstance(r.get("body_src"), dict):
            prev["body_src"] = {**(prev.get("body_src") or {}), **r["body_src"]}
        for f in _MERGE_FILL:
            if prev.get(f) is None and r.get(f) is not None:
                prev[f] = r[f]
        if r.get("body_partial"):
            prev["body_partial"] = True
    return out

def _abs_host(template):
    """The hostname an absolute template names, or None if it names none it can resolve.

    Deliberately strict: a scheme, then a host containing no template slot. `https://api.example.com/v1`
    yields api.example.com; `https://{region}.example.com/v1` and `/v1/things` yield None, because the
    first has no single answer and the second is not making a claim about a host at all.
    """
    m = re.match(r"^(?:https?|wss?)://([^/?#\s]+)", template or "", re.I)
    if not m:
        return None
    hostport = m.group(1)
    if "{" in hostport or "}" in hostport or "$" in hostport or "*" in hostport:
        return None
    host = hostport.rsplit("@", 1)[-1].split(":")[0].lower()
    return host if re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", host) else None


def _document_map(bodies):
    """body file -> the DOCUMENT that loaded it, written by the crawler beside `_origin.json`.

    Separate file, separate concern: `_origin.json` says who served a body, this says which page pulled it
    in. Absent on any run crawled before this existed, and callers fall back to the serving host,
    which is exactly the old behaviour, so an old workspace still analyses the way it always did.
    """
    if not bodies:
        return {}
    m = {}
    # PRIMARY: the `d` field of _origin.jsonl. This map used to read `_document.json` alone, a file the
    # crawler stopped writing when _save_origin became append-only JSONL, and which is therefore absent
    # from EVERY run on disk. The failure was silent and total: doc_host_of() returned None for
    # every file, so each bundle fell back to the host that SERVED it, and a site that ships its code from
    # a CDN lost its entire static surface. LinkedIn puts all 750 of its application bundles on
    # static.licdn.com, so `--host www.linkedin.com` filtered away 98 MB of its own JavaScript and yielded
    # one path literal. The document was recorded correctly the whole time; nothing read it.
    q = os.path.join(bodies, "_origin.jsonl")
    if os.path.exists(q):
        for line in open(q, errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if not r.get("f"):
                continue
            b = os.path.basename(r["f"])
            # A DOCUMENT IS ITS OWN DOCUMENT.
            #
            # `d` is who pulled a body in, which is the right question for a script and the wrong one for
            # a page. A relative URL inside a document resolves against THAT document's URL, always, by
            # spec, never against whichever page happened to link to it. The crawler records a referrer,
            # so app.client.example/home arrived carrying d=www.client.example/account, and every relative
            # path in the app bundle it loads was then filed under the marketing site.
            if r.get("u") and b.lower().endswith((".html", ".htm")):
                m[b] = r["u"]
                continue
            if r.get("d"):
                m.setdefault(b, r["d"])
    # FALLBACK: the legacy sidecar, for a workspace crawled before the JSONL format existed.
    p = os.path.join(bodies, "_document.json")
    if os.path.exists(p):
        try:
            for k, v in json.load(open(p)).items():
                m.setdefault(os.path.basename(k), v)
        except Exception:
            pass
    return m


def _origin_map(manifest, bodies=None):
    """body-file -> the URL it was served from.

    Bodies are stored by CONTENT hash, so a saved file has forgotten where it came from, which is fine
    until a bundle declares its chunks RELATIVELY. `../../map/plugins/x.js` cannot be fetched by anyone
    without knowing the URL of the file that said it.

    The crawl log holds part of that mapping, but only for records that carry a body_file, which is the
    documents, not the ~1000 assets they pulled in. The crawler now writes `_origin.json` beside the bodies
    covering every saved file, so that is the primary source and the log is a fallback. Without it, six
    crawls sharing one bodies dir cannot be told apart and `--host X` mislabels every other host's bundles
    as X, which cost one run a full re-crawl.
    """
    m = {}
    if bodies:
        # Current format: one JSON object per line, appended, safe for concurrent crawls.
        q = os.path.join(bodies, "_origin.jsonl")
        if os.path.exists(q):
            bad = 0
            for line in open(q, errors="replace"):
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    if r.get("f") and r.get("u"):
                        m[os.path.basename(r["f"])] = r["u"]
                except Exception:
                    bad += 1
            if bad:
                print(f"  ! {bad} unreadable line(s) in _origin.jsonl", file=sys.stderr)
        # Legacy single-document format, and the corruption it was prone to. Several crawls sharing one
        # temp filename produced a file holding two concatenated JSON documents; `json.load` raises on
        # that, the old code caught the exception and returned an EMPTY map, and a run with 1533
        # bodies silently lost every host attribution. Decode as many documents as the file contains, and
        # if any of it is unreadable SAY SO rather than returning nothing and looking like a clean miss.
        p = os.path.join(bodies, "_origin.json")
        if os.path.exists(p):
            raw = open(p, errors="replace").read()
            dec, i, docs = json.JSONDecoder(), 0, 0
            while i < len(raw):
                try:
                    d, j = dec.raw_decode(raw, i)
                except Exception:
                    break
                if isinstance(d, dict):
                    m.update({os.path.basename(k): v for k, v in d.items()})
                    docs += 1
                i = j
                while i < len(raw) and raw[i] in " \r\n\t":
                    i += 1
            if docs > 1:
                print(f"  ! _origin.json held {docs} concatenated JSON documents, written by parallel "
                      f"crawls sharing a temp file. Merged them; {len(m)} entries recovered.",
                      file=sys.stderr)
            elif not docs:
                print(f"  ! _origin.json is unreadable. Host attribution will be missing, not empty",
                      file=sys.stderr)
            # Trailing garbage is a THIRD corruption shape and it used to pass in silence, because the
            # warnings above only fire on 0 or >1 documents. reddit's file is one valid document of 148
            # entries followed by 96 bytes of a truncated record, a shorter write landing on a longer
            # file's tail. Exactly one document decoded, so nothing was said, and the run could not tell a
            # partial map from a complete one.
            if i < len(raw) and raw[i:].strip():
                print(f"  ! _origin.json has {len(raw)-i} trailing byte(s) that are not JSON, a shorter "
                      f"write landed on a longer file. {len(m)} entries survived; the rest are GONE, not "
                      f"empty. Re-crawling is the only way to recover them.", file=sys.stderr)
    # Coverage is the number that tells you whether attribution can work at all. A map with 148 entries for
    # 493 saved files means two thirds of the corpus cannot be filed under any host, and every record from
    # those files comes out with `_host: None`, which reads downstream as "no surface here".
    if bodies:
        try:
            saved = len([n for n in os.listdir(bodies)
                         if n.endswith((".js", ".mjs", ".html", ".htm")) and not n.startswith("_")])
            if saved and len(m) < saved * 0.8:
                print(f"  ! origin map covers {len(m)} of {saved} saved script/markup file(s). Records from "
                      f"the rest cannot be attributed to a host.", file=sys.stderr)
        except OSError:
            pass
    if not manifest:
        return m
    for line in open(manifest):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("body_file") and r.get("url"):
            m[os.path.basename(r["body_file"])] = r["url"]
    return m


def unfetched(bodies, manifest=None):
    """Chunk URLs the bundles declare that we do not have on disk.

    This function prints them rather than downloading them, because fetching is not what this tool is for.
    Pointing the crawler at them is the easy path; anything else that fetches works too.
    """
    origins = _origin_map(manifest, bodies)
    documents = _document_map(bodies)
    have = {os.path.basename(p).split("?")[0] for p in js_files(bodies)}
    want = set()
    for p in js_files(bodies):
        try:
            env = node_env(); env["NODE_PATH"] = NODE_PATH
            out = subprocess.run(["node", CHUNKMAP], stdin=open(p, "rb"), env=env,
                                 capture_output=True, timeout=120)
            d = json.loads(out.stdout or b"{}")
            if not isinstance(d, dict):
                continue                      # chunkmap yielded a non-object; nothing to read
        except Exception:
            continue
        src = origins.get(os.path.basename(p))
        pub = d.get("publicPath") or ""
        for c in d.get("chunks", []):
            base = os.path.basename(c.split("?")[0])
            if not base or base in have:
                continue
            if c.startswith("http"):
                want.add(c)
            elif src:
                # publicPath is where the RUNTIME says chunks live; it wins over the declaring file's
                # directory, which is the mistake that had us requesting 187 chunks from the wrong prefix.
                want.add(urljoin(src, pub + c) if pub else urljoin(src, c))
    return sorted(want)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bodies")
    ap.add_argument("--host")
    ap.add_argument("--out")
    ap.add_argument("--unfetched", action="store_true")
    ap.add_argument("--manifest", help="a crawl .jsonl, needed to resolve RELATIVE chunk paths to URLs")
    a = ap.parse_args()

    if a.unfetched:
        w = unfetched(a.bodies, a.manifest)
        if not a.manifest:
            print("  ! no --manifest given: relative chunk paths cannot be resolved to URLs and were "
                  "dropped. Pass the crawl .jsonl.", file=sys.stderr)
        print("\n".join(w))
        print(f"\n# {len(w)} chunk(s) declared but never downloaded, "
              f"collect them with your crawler, then re-run analyze", file=sys.stderr)
        return

    recs, paths = analyze(a.bodies, a.host, a.manifest)
    import collections
    kinds = collections.Counter(r.get("_kind") or "endpoint" for r in recs)
    n_inline = sum(1 for p in paths if ".inline" in p)
    print(f"{len(paths)} scripts ({len(paths)-n_inline} files + {n_inline} inline) "
          f"-> {len(recs)} records  {dict(kinds.most_common(8))}")
    if not paths:
        # An empty result must never be silently indistinguishable from a clean one.
        print("  ! no JavaScript found. That is not 'this host has no API'. It means the crawler saved "
              "nothing here. Check the crawl ran and that bodies are being written.", file=sys.stderr)
    # DID THE EXTRACTOR RUN? Answer this BEFORE writing anything, because a number that came out of a
    # broken toolchain is worse than no number: it gets quoted.
    _ok, _bad = _AST_OK[0], _AST_FAILED[0]
    if _bad and _ok == 0:
        print(f"\n  !! THE AST EXTRACTOR NEVER RAN. All {_bad} JavaScript files failed to parse, so the\n"
              f"     {len(recs)} record(s) above came from the regex fallback alone and are NOT a map of\n"
              f"     this application. Reason: {'; '.join(sorted(_AST_WHY)) or 'unknown'}\n"
              f"     Fix the toolchain and re-run; no results have been written.\n"
              f"     (Usually: npm install -g @babel/parser @babel/traverse, or run inside the image.)",
              file=sys.stderr)
        raise SystemExit(4)
    if _bad > _ok:
        print(f"\n  !! the extractor failed on {_bad} of {_ok + _bad} files, MORE than it parsed.\n"
              f"     Treat the counts below as a floor, not a map. Reason: "
              f"{'; '.join(sorted(_AST_WHY)) or 'unknown'}", file=sys.stderr)
    elif _bad:
        print(f"  ({_bad} of {_ok + _bad} files unparsed, endpoints in those are missing)",
              file=sys.stderr)

    if a.out:
        json.dump(recs, open(a.out, "w"))
        print(f"  -> {a.out}")




if __name__ == "__main__":
    main()
