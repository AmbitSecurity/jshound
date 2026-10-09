"""table.py: the requests, one per line, for reading rather than for piping.

`urls` is a projection: a URL list cannot carry a method or a body, so the two things that make a
record worth more than a grep hit are dropped on the way out. OpenAPI and HAR carry everything but
are made for a proxy to import, not for a person to read in a terminal.

So this is the glance: what the app asks for, with the parts that are known attached.

    POST   https://api.example.com/v1/subscribe  application/json     {"email":"computed:trim"}
    GET    https://api.example.com/v1/users/{id}
    POST   https://api.example.com/v1/media      multipart/form-data  {"file":"file-upload"}

A marked record stays marked. `{EXPR}` is a slice the analysis could not resolve, a `~` before the
body means `body_partial`, the named fields are real and the list is incomplete, and a `?`
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


def _body(rec):
    """The body as a shape, not a list of names.

    `body: email, pageUri` was the old rendering and it is not a body, a body is a document, and the
    records already hold its shape: `{"email": "computed:trim", "pageUri": "state:href"}`, where each
    value says where the field's value comes from rather than inventing a type the bundle never
    stated. Nested objects nest. So the shape is printed as the JSON it is.

    `body_shape` is preferred over `body_schema` because it survives more cases, a `FormData` upload
    has `{"file": "file-upload"}` as its shape while its schema is the unresolvable `{EXPR}`. When
    neither resolved, the field names alone are still worth printing, as a set rather than as JSON,
    so the two are never confused on sight.
    """
    mark = "~" if rec.get("body_partial") else ""
    shape = rec.get("body_shape")
    if not isinstance(shape, dict) or not shape:
        shape = rec.get("body_schema") if isinstance(rec.get("body_schema"), dict) else None
    if shape:
        # Never cut. This was capped at 64 characters with `,...}`, which put `"messageId":"input:,...}`
        # on screen and, worse, into the saved file, where the rest of the body could not be recovered
        # at all. A long body is a long line; the terminal can wrap it.
        return mark + json.dumps(shape, separators=(",", ":"), sort_keys=False)
    keys = [str(k) for k in (rec.get("body_keys") or []) if str(k)]
    if keys:
        return mark + "{" + ", ".join(keys) + "}"
    return ""


def build(records, with_origin=True):
    """One row per request, with the full URL it goes to. `with_origin=False` shows the path alone."""
    from .requests import display_url, requests_only
    rows, seen = [], set()
    for rec in requests_only(records):
        tmpl = rec.get("template") or rec.get("url") or ""
        path, _p, _resolved = normalize(tmpl)
        if not path or looks_like_source_file(path):
            continue
        method = (rec.get("method") or "GET").upper()
        shown = display_url(rec) if with_origin else path
        body = _body(rec)
        # `content_type` is the header as written. Absent that, `body_encoding` is what the call site
        # showed, and its basis says whether that was read or inferred, which is what the `?` carries.
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

    # Columns run method, URL, content type, body: the body is the only one with no bound on its
    # length, so it goes last and nothing after it can be pushed out of line. Every other column is
    # as wide as its longest entry: a cap made a long URL shove its own row's content type sideways,
    # which reads as a different column.
    def pad(text, width):
        return text + " " * (width + 2 - len(text))

    wm = max(7, max(len(r[0]) for r in rows) + 1)
    wp = max(len(r[1]) for r in rows)
    wc = max(len(r[3]) for r in rows)
    out = []
    for method, shown, body, ctype in sorted(rows, key=lambda r: (r[1], r[0])):
        line = method.ljust(wm) + pad(shown, wp)
        line += pad(ctype, wc) if wc else ""
        out.append((line + body).rstrip())
    return "\n".join(out)


def dumps(records, **kw):
    return build(records, **kw)
