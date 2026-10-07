"""classify.py — sort extracted paths into what a tester can actually use.

A raw count rewards noise, and that makes the obvious benchmark backwards. Run LinkFinder over a
webpack bundle and it reports 314 "endpoints"; read them and they are documentation links plus
strings like `/VwdtK` and `/+U0Y/` — minifier output that happens to contain a slash. A table with a
single `paths` column scores that above a tool that found four real contracts.

So every tool's output is bucketed the same way, by the same rules, and the buckets are reported
separately:

    api        requestable and API-shaped — /api/..., /v2/..., /graphql, /auth/...
    content    a real URL, but a page: /user-guide/..., /blog/..., /pricing
    asset      /static/..., /_next/..., *.css — real, rarely interesting
    fragment   minifier debris: short, consonant-dense, base64-ish. Not a path at all.

`fragment` is the column that matters when comparing against string extractors, because it is the
cost of their method and it is invisible in a total.
"""
import re

_API = re.compile(r"^/(api|v\d+|graphql|gql|rest|rpc|auth|oauth|oidc|admin|internal|service|"
                  r"gateway|_api|webhooks?|callback)\b|^/[a-z0-9-]+/(v\d+|api)\b", re.I)
_ASSET = re.compile(r"^/(static|_next|assets?|images?|img|fonts?|media|icons?|css|js|dist|build|"
                    r"public)/|\.(css|js|mjs|png|jpe?g|gif|svg|ico|webp|woff2?|ttf|map)$", re.I)
_VOWEL = re.compile(r"[aeiou]", re.I)


def is_fragment(p):
    """A minified identifier that happens to contain a slash, not a path."""
    s = p.strip("/")
    if not s or "/" not in p:
        return False
    segs = [x for x in s.split("/") if x]
    if not segs:
        return True
    short = [x for x in segs if len(x) <= 8]
    if len(short) != len(segs):
        return False
    # Debris is short, mixed-case or symbol-bearing, and vowel-poor — `VwdtK`, `+U0Y`, `nv0ml`.
    odd = 0
    for x in segs:
        if re.search(r"[+=]", x) or (len(_VOWEL.findall(x)) == 0) or (
                re.search(r"[a-z]", x) and re.search(r"[A-Z]", x) and len(x) <= 6):
            odd += 1
    return odd >= max(1, len(segs) - 1) and len(segs) <= 2


def bucket(p):
    if _API.match(p):
        return "api"
    if _ASSET.search(p):
        return "asset"
    if is_fragment(p):
        return "fragment"
    return "content"


def tally(paths):
    out = {"api": 0, "content": 0, "asset": 0, "fragment": 0}
    for p in paths:
        out[bucket(p)] += 1
    return out
