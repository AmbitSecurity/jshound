"""urls.py — one absolute URL per line, for the tools that take a list.

nuclei, ffuf, httpx, feroxbuster and every `while read` loop ever written want exactly this and
nothing else. The method and the body cannot survive the format, so this is a projection, not an
export — reach for OpenAPI or HAR when the consumer can hold more.

Paths that still contain an unresolved placeholder are omitted by default: a line reading
`https://x.com/api/users/{param1}` is not a URL, and feeding it to a fuzzer produces a 404 that
looks like a finding. `--include-unresolved` prints them anyway, because when you are picking fuzz
targets by hand the shape is exactly what you want.
"""
from .paths import UNRESOLVED_ORIGIN, looks_like_source_file, normalize, split_origin


def build(records, include_unresolved=False, with_method=False):
    out, seen = [], set()
    for rec in records:
        # `doc-state` belongs here and nowhere else. These are URLs the SERVER serialised into the
        # document — Datadog's `login_url`, `forgot_password_url` and `recaptcha_login_url`, Slack's
        # three OAuth entry points — so they are declared request targets, which is exactly what a
        # URL list is for. They carry no method, and this projection does not need one: that is why
        # they are not in the OpenAPI export, where a path without a verb would have to invent one.
        if (rec.get("_kind") or "endpoint") not in ("endpoint", "route", "doc-state"):
            continue
        tmpl = rec.get("template") or rec.get("url") or ""
        origin, _ = split_origin(tmpl)
        path, _p, resolved = normalize(tmpl)
        if looks_like_source_file(path):
            continue
        if not resolved and not include_unresolved:
            continue
        # An UNRESOLVED origin is not a missing one. `{BASE_PATH}/api/incident-status` is a real
        # endpoint whose base the analysis could not resolve, and the host that served the FILE is
        # not it: supabase.com/dashboard serves its bundle from a CDN that answers no API call, so
        # this produced 45 lines pointed at a host the app never talks to. A URL list is a list of
        # things you can request, so these are left out of it unless they are asked for.
        if origin == UNRESOLVED_ORIGIN:
            if not include_unresolved:
                continue
            base = origin
        else:
            base = origin if (origin and "://" in origin) else (
                f"https://{rec['_host']}" if rec.get("_host") else "")
        if not base:
            continue
        line = f"{(rec.get('method') or 'GET').upper()} {base}{path}" if with_method else base + path
        if line not in seen:
            seen.add(line)
            out.append(line)
    return sorted(out)


def dumps(records, **kw):
    return "\n".join(build(records, **kw))
