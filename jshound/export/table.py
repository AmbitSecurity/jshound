"""table.py — the request, one per line, for reading rather than for piping.

`urls` is a projection: a URL list cannot carry a method or a body, so the two things that make a
record worth more than a grep hit are dropped on the way out. OpenAPI and HAR carry everything but
are made for a proxy to import, not for a person to read in a terminal.

So this is the glance: what the app asks for, with the parts that are known attached.

    POST   /v1/subscribe   {"email":"computed:trim","pageUri":"state:href"}  application/json
    GET    /v1/users/{id}/profile
    POST   /v1/media       {"file":"file-upload"}                             multipart/form-data

A marked record stays marked. `{EXPR}` is a slice the analysis could not resolve, a `~` before the
body means `body_partial` — the named fields are real and the list is incomplete — and a `?`
after the content type means the encoding was INFERRED from the other request builders in the file
rather than read at the call site. An unresolved origin stays written as the placeholder it came from
rather than being quietly dropped or quietly filled in. Sorting is by path so that related routes sit
together.
"""
import json

from .paths import UNRESOLVED_ORIGIN, looks_like_source_file, normalize, split_origin

_MEDIA = {
    "json": "application/json",
    "multipart": "multipart/form-data",
    "form": "application/x-www-form-urlencoded",
    "urlencoded": "application/x-www-form-urlencoded",
    "text": "text/plain",
}


# How wide a rendered body may get before it is cut. A body is the most informative column here and
# also the only one that can run to hundreds of characters.
_BODY_MAX = 64


def _body(rec):
    """The body as a shape, not a list of names.

    `body: email, pageUri` was the old rendering and it is not a body — a body is a document, and the
    records already hold its shape: `{"email": "computed:trim", "pageUri": "state:href"}`, where each
    value says where the field's value comes from rather than inventing a type the bundle never
    stated. Nested objects nest. So the shape is printed as the JSON it is.

    `body_shape` is preferred over `body_schema` because it survives more cases — a `FormData` upload
    has `{"file": "file-upload"}` as its shape while its schema is the unresolvable `{EXPR}`. When
    neither resolved, the field names alone are still worth printing, as a set rather than as JSON,
    so the two are never confused on sight.
    """
    mark = "~" if rec.get("body_partial") else ""
    shape = rec.get("body_shape")
    if not isinstance(shape, dict) or not shape:
        shape = rec.get("body_schema") if isinstance(rec.get("body_schema"), dict) else None
    if shape:
        text = json.dumps(shape, separators=(",", ":"), sort_keys=False)
        if len(text) > _BODY_MAX:
            text = text[:_BODY_MAX - 4] + ",...}"
        return mark + text
    keys = [str(k) for k in (rec.get("body_keys") or []) if str(k)]
    if keys:
        return mark + "{" + ", ".join(keys) + "}"
    return ""


def build(records, with_origin=False):
    rows, seen = [], set()
    for rec in records:
        if (rec.get("_kind") or "endpoint") not in ("endpoint", "route"):
            continue
        tmpl = rec.get("template") or rec.get("url") or ""
        origin, _rest = split_origin(tmpl)
        path, _p, _resolved = normalize(tmpl)
        if not path or looks_like_source_file(path):
            continue
        method = (rec.get("method") or "GET").upper()
        shown = path
        if with_origin:
            base = ("" if origin == UNRESOLVED_ORIGIN else
                    origin if origin else (f"https://{rec['_host']}" if rec.get("_host") else ""))
            shown = base + path
        body = _body(rec)
        # `content_type` is the header as written. Absent that, `body_encoding` is what the call site
        # showed — a `FormData` argument is multipart whether or not anyone set a header — and its
        # basis says whether that was read or inferred, which is the difference the `?` carries.
        ctype = rec.get("content_type") or rec.get("media_type") or ""
        if not ctype and rec.get("body_encoding"):
            ctype = _MEDIA.get(rec["body_encoding"], rec["body_encoding"])
            if str(rec.get("body_encoding_basis") or "").startswith("INFERRED"):
                ctype += "?"
        key = (method, shown, body, ctype)
        if key in seen:
            continue
        seen.add(key)
        rows.append((method, shown, body, ctype))

    if not rows:
        return ""

    # `f"{s:<{w}}"` does not pad a string already wider than the column, so one long path or one long
    # body list ran straight into the next column with no space at all. Two spaces is the floor, and
    # that row is simply wider than the rest.
    def pad(text, width):
        return text + " " * max(2, width - len(text))

    wm = max(7, max(len(r[0]) for r in rows) + 1)
    wp = min(max(len(r[1]) for r in rows), 48)
    wb = min(max(len(r[2]) for r in rows), 38)
    out = []
    for method, shown, body, ctype in sorted(rows, key=lambda r: (r[1], r[0])):
        # `ljust`, not `pad`: `wm` already clears the longest verb, and pad's two-space floor pushed
        # DELETE one column further right than every other row.
        line = method.ljust(wm) + (pad(shown, wp) if (body or ctype) else shown)
        if body:
            line += pad(body, wb) if ctype else body
        out.append((line + ctype).rstrip())
    return "\n".join(out)


def dumps(records, **kw):
    return build(records, **kw)
