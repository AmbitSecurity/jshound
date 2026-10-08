"""paths.py — turn an extracted template into something a tool can actually request.

The extractor emits a template, not a URL, and the difference matters. `{EXPR}` marks a slice the
analysis could not resolve; `{id}` marks one it resolved to a NAME. Both are placeholders, but only
the second one tells you what belongs there, and an exporter that flattens them together throws away
the more useful half.

So the rule here: a named slice keeps its name, an unresolved slice becomes a positional `param<N>`,
and callers are told which is which. OpenAPI needs every path placeholder to be a declared parameter
with a unique name, which is the only reason `{EXPR}` gets renamed at all.
"""
import re
from urllib.parse import urlsplit

# `{location.origin}`, `{window.location.host}` — the app asking for its own deployment URL. That is a
# SERVER, not a path segment, and leaving it inline produces paths no tool can request.
_ORIGIN = re.compile(r"^\{(?:window\.)?(?:location\.(?:origin|host|href)|origin)\}", re.I)
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
# Rails/Express templates ship their params as `:name`, and they survive into the recovered path
# (`/api/:version/projects/:id`). A spec cannot carry them literally — `:id` would be requested as the
# four characters — so they become placeholders like any other slot. A colon only marks a param when it
# OWNS the segment: Google-style custom methods put one mid-segment (`/v1/accounts:signUp`) and those
# are part of the path, not a slot.
_COLON_PARAM = re.compile(r"(?<=/):([A-Za-z_][A-Za-z0-9_]*)(?=/|$)")
_UNRESOLVED = {"expr", "", "..."}


# A TEMPLATE THAT BEGINS WITH A SLOT HAS NO ADDRESS.
#
# `{BASE_PATH}/api/incident-status` is a real endpoint whose base the analysis could not resolve. It
# is not a relative path, and treating it as one means something downstream supplies an origin: the
# URL exporter used the host that served the FILE, which on supabase.com/dashboard is a CDN, so 45
# endpoints came out as `https://frontend-assets.supabase.com/{BASE_PATH}/api/...`. That CDN serves
# no API. A real-looking route pointed somewhere the application never calls is the worst output this
# tool produces, and it is worse than printing nothing.
#
# `{baseUrl}` was in `_ORIGIN` for the same reason and is just as wrong: it is a value the app
# ASSIGNED, often to a different host than the document. The demo fixture in this repo assigns
# `apiBase: "https://api.demo-app.test"` and `--host demo-app.test` was being substituted over it.
# What stays in `_ORIGIN` is only the forms that mean "this document's own origin" by definition,
# which is the one case where the document host is the right answer rather than a guess.
UNRESOLVED_ORIGIN = "{?}"


def split_origin(template):
    """-> (origin_or_None, path).

    An absolute URL and a `{location.origin}` prefix both give a real origin. A template that starts
    with any OTHER slot gives `UNRESOLVED_ORIGIN`, which is not None: None means "relative to the
    document", and these are not.
    """
    t = (template or "").strip()
    if not t:
        return None, "/"
    m = _ORIGIN.match(t)
    if m:
        return m.group(0), t[m.end():] or "/"
    if "://" in t[:12]:
        u = urlsplit(t)
        if u.scheme and u.netloc:
            return f"{u.scheme}://{u.netloc}", (u.path or "/") + (f"?{u.query}" if u.query else "")
    if t.startswith("{"):
        slot = _PLACEHOLDER.match(t)
        if slot:
            return UNRESOLVED_ORIGIN, t[slot.end():] or "/"
    return None, t


def split_query(path):
    """-> (path, [query names]). OpenAPI forbids a query string in a path key, and Burp rejects the
    document outright when one appears, so the split is mandatory rather than cosmetic."""
    if "?" not in path:
        return path, []
    base, _, qs = path.partition("?")
    names = []
    for part in qs.split("&"):
        k = part.split("=", 1)[0].strip()
        k = _PLACEHOLDER.sub(lambda m: (m.group(1) or "").strip(), k)
        if k and k not in names:
            names.append(k)
    return (base or "/"), names


# A VALUE THAT CANNOT BE A PATH SEGMENT. Resolution sometimes substitutes a string that is real in the
# source but is not a URL component: Grafana's `"-- Mixed --"` is a dropdown placeholder, and it landed
# in 20 paths as `/cloudmigration/migration/-- Mixed --/snapshot`. Whitespace settles it — no server
# routes on a segment with a space in it — and so does a segment made only of punctuation (`--`).
# Turning these back into slots is not a cosmetic filter: `/cloudmigration/migration/{param1}/snapshot`
# is the correct endpoint, and the literal was the error.
_NOT_A_SEGMENT = re.compile(r"\s|^[^A-Za-z0-9_%]+$")


def _desuspect(path):
    segs = path.split("/")
    out, hit = [], False
    for seg in segs:
        if seg and not seg.startswith("{") and _NOT_A_SEGMENT.search(seg):
            out.append("{EXPR}")
            hit = True
        else:
            out.append(seg)
    return "/".join(out), hit


def normalize(template):
    """-> (path, params, resolved) where params is [{"name","resolved"}] in path order.

    `resolved` is False for the whole path when any placeholder was `{EXPR}`: the path is a shape, not
    an address, and a caller that replays it blindly will request a literal "{param1}".
    """
    _, path = split_origin(template)
    if not path.startswith("/"):
        path = "/" + path
    path, _q = split_query(path)
    path = _COLON_PARAM.sub(lambda m: "{" + m.group(1) + "}", path)
    path, _sus = _desuspect(path)
    params, n, fully = [], 0, True

    def sub(m):
        nonlocal n, fully
        raw = (m.group(1) or "").strip()
        if raw.lower() in _UNRESOLVED or raw.startswith("EXPR"):
            n += 1
            fully = False
            name = f"param{n}"
            params.append({"name": name, "resolved": False})
            return "{" + name + "}"
        # A resolved name may still be dotted (`user.id`); OpenAPI parameter names must be simple.
        name = re.sub(r"[^A-Za-z0-9_]", "_", raw).strip("_") or f"param{len(params) + 1}"
        if any(p["name"] == name for p in params):
            name = f"{name}_{len(params) + 1}"
        params.append({"name": name, "resolved": True})
        return "{" + name + "}"

    path = _PLACEHOLDER.sub(sub, path)
    return path, params, fully


# A router config is read out of the bundle, and some apps keep their ORIGINAL source paths in it —
# `/src/command-menu-items/book-slot.command-menu-item.ts` is a build artifact, not a route anyone can
# request. Letting those through turns a precise contract list into something that reads like a grep,
# which is the exact failure this tool exists to avoid, so the exporters drop them.
_SOURCE_EXT = re.compile(r"\.(ts|tsx|jsx|mjs|cjs|vue|svelte|scss|sass|less|styl|map)$", re.I)
_SOURCE_DIR = re.compile(r"^/(src|node_modules|packages|apps|libs|components|pages/api/_)/", re.I)


def looks_like_source_file(path):
    """True when a 'route' is really a path from the developer's filesystem."""
    p = (path or "").split("?", 1)[0]
    return bool(_SOURCE_EXT.search(p) or _SOURCE_DIR.match(p))
