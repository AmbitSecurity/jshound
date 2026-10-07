# A comparison you can run in 30 seconds

Every number here comes from `examples/demo-app/app.4f2a9c1e.js`, a 5-line synthetic webpack bundle
committed to this repo. No corpus to download, no target to agree about, nothing to take on trust.

```bash
jshound analyze examples/demo-app --host demo-app.test -f urls
jshound chunks   examples/demo-app/app.4f2a9c1e.js -q
jsluice  urls      examples/demo-app/app.4f2a9c1e.js
python3 linkfinder.py -i examples/demo-app/app.4f2a9c1e.js -o cli
```

The fixture contains **six** network call sites across four sinks (`fetch`, `XMLHttpRequest.open`,
`navigator.sendBeacon`, and a `FormData` upload), three of which build their URL as
`` `${this.baseUrl}/v1/…` `` where `baseUrl` is assigned in a different module — and a webpack
runtime that builds three lazy-chunk URLs from id maps. Three of the six carry a request body.

| | jshound | jsluice | LinkFinder |
|---|---|---|---|
| endpoints found | **6 / 6** | 3 / 6 | 3 / 6 |
| HTTP verbs | **6** | 2 | 0 |
| body contracts | **3** | 0 | 0 |
| lazy chunk URLs | **3 / 3** | 0 | 0 |
| non-paths emitted | 0 | 1 (`.js`) | 2 (`application/json`, `static/chunks/`) |

## What each one actually prints

**jshound** — six endpoints, every verb, plus the chunk URLs:

```
DELETE  /v1/sessions/current
GET     /api/feature-flags
GET     /v1/users/{param_o}/profile
POST    /t/events
POST    /v1/media
POST    /v1/subscribe
    static/chunks/settings.a1b2c3d4.js
    static/chunks/billing.e5f6a7b8.js
    static/chunks/admin.c9d0e1f2.js
```

**jsluice** — a real tool, and it earns credit here: it understands `XMLHttpRequest.open`,
`fetch` and `sendBeacon`, and it reports the DELETE and the GET correctly. What it cannot do is
follow a value across a module boundary, so all three `${this.baseUrl}` endpoints are missing:

```
-       /_assets/                      [stringLiteral]
-       .js                            [stringLiteral]
DELETE  /v1/sessions/current           [XMLHttpRequest.open]
GET     /api/feature-flags?env=EXPR    [fetch]
-       /t/events                      [navigator.sendBeacon]
-       https://api.demo-app.test      [stringLiteral]
```

Note `.js` — a fragment of the chunk-URL builder, emitted as a URL. (`/_assets/` is the real
publicPath, so that one is fair.) `EXPR` is jsluice's marker for a value it could not resolve, the
same honesty this tool uses for `{EXPR}`.

**LinkFinder** — no verbs at all, and the two most revealing lines are `application/json` (a MIME
type) and `static/chunks/` (the *prefix* of the chunk path, without any of the three chunk URLs
built from it):

```
/_assets/
static/chunks/
application/json
/v1/sessions/current
/api/feature-flags?env=
/t/events
https://api.demo-app.test
```

## Route tables the framework declares

A separate mechanism, and worth stating where it does and does not buy anything. Next, Nuxt and
SvelteKit each ship their route table; `jshound manifest` reads it.

**It buys nothing on Next's `_buildManifest.js`.** That file is a plain object literal of quoted
paths, so a regex extractor reads it perfectly: handed the same Supabase manifest, LinkFinder returns
489 strings including all 103 `/api/*` routes. What differs there is only *finding* the file — its URL
has to be read from the document, because Supabase serves it from an `assetPrefix` on another host —
and *structuring* it: 103 api routes separated from 190 page routes, parameter names kept, rewrites
and middleware matchers extracted, and the 191 chunk paths and framework internals excluded rather
than mixed into the same list.

**It buys a lot on SvelteKit**, where the table is a minified export alias inside the entry module.
Both tools on the same `app.Dy3uvAgJ.js` from svelte.dev:

| | jshound | LinkFinder |
|---|---|---|
| strings returned | 24 | 102 |
| route ids present verbatim | 24 | 18 |
| **requestable URL templates** | **24** | **12** |
| chunk/asset paths mixed in | 0 | 84 |

The 12-vs-24 gap is not about finding strings, it is about interpreting them. `/docs/[topic]/[...path=documentation]`
is not a URL — `/docs/{topic}/{path}` is — and the six routes LinkFinder misses entirely are the
route-group ones (`/(authed)/playground/[...id]/embed`), where the filesystem id and the URL differ
most. Reading them required resolving the `dictionary` export through scope; reading the file at all
required following a dynamic `import()` out of an inline script, since a SvelteKit document links no
script.

## What this does and does not show

It shows the mechanism difference on a case designed to isolate it: cross-module value resolution
and runtime chunk-URL construction. It is **not** evidence about how these tools rank on your
target, and a fixture authored by one of the entrants is the weakest form of evidence there is.

For that, run `bench/run.py` against a real corpus — it buckets every tool's output by the same
rules so minifier debris cannot be counted as attack surface, and attributes each path to the host
that served it so nobody is credited for a third party's endpoints. Then publish the corpus.

**LinkFinder needed `pip install jsbeautifier`** before it would run at all here, and its first
measured score against the manifest was 0 purely because of that. A competitor's zero is worth
checking twice; this one was mine, not theirs.

**xnLinkFinder is not in this table**, and that is not a result it earned: its scope filter is
mandatory and matches on a link's *domain*, while paths out of a bundle are relative and have none,
so every scope value filters the whole output away. It is built to crawl live URLs, which is a
different job from reading files on disk.
