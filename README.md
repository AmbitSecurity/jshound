# jshound

**Recover a SPA's API contracts from the JavaScript it ships.** Not a list of URLs — the request:
method, path, body fields, content type, and the headers the client attaches.

```
$ jshound analyze ./bodies --host play.grafana.org -f openapi -o spec.json
163 scripts -> 1268 records
-> spec.json
```

750 paths, 89% of everything Grafana documents in its own OpenAPI spec, every HTTP method correct —
[measured](bench/RESULTS.md). Open `spec.json` in Burp's OpenAPI Parser or Caido and the site map
fills with requests you never had to crawl for.

**[What this works on](SUPPORT.md)** — webpack 4 and 5, Rspack, Turbopack, Vite/Rollup, plain ESM;
Next, Nuxt, SvelteKit, Remix, Astro, Angular, Solid; all three webpack module-id modes. Every row
measured against a real application.

## Try it in 30 seconds

```bash
git clone https://github.com/AmbitSecurity/jshound && cd jshound && npm install && pip install -e .
jshound chunks examples/demo-app/app.4f2a9c1e.js
```

```json
{"chunks": ["static/chunks/settings.a1b2c3d4.js",
            "static/chunks/billing.e5f6a7b8.js",
            "static/chunks/admin.c9d0e1f2.js"],
 "stats": {"builders": 1, "idsSeen": 3, "idsResolved": 3, "speculative": 0}}
```

Now grep that bundle for `settings.a1b2c3d4.js`. **It is not in the file.** The runtime assembles it
from two id maps at `import()` time, so the string exists only after the function runs — which is
why no regex and no literal-collecting AST pass can produce it, and why most of a code-split app's
surface is invisible to the tools below.

```bash
jshound analyze examples/demo-app --host demo-app.test -f urls
```

```
DELETE https://demo-app.test/v1/sessions/current
GET    https://demo-app.test/api/feature-flags
GET    https://demo-app.test/v1/users/{param_o}/profile
POST   https://demo-app.test/t/events
POST   https://demo-app.test/v1/media
POST   https://demo-app.test/v1/subscribe
```

Four different sinks, a verb on every one, and three paths that only resolve by following
`this.baseUrl` into a different module. See [examples/](examples/).

---

## Why this exists

Nearly every dedicated JavaScript analysis tool in the ecosystem has stopped.

| Tool | Stars | Last pushed |
|---|---:|---|
| LinkFinder | 4,452 | 2024-04-13 |
| JSFinder | 2,976 | 2021-11-24 |
| SecretFinder | 2,523 | 2024-05-26 |
| jsluice | 1,945 | 2024-05-22 |
| JSFScan.sh | 1,111 | 2023-09-09 |
| xnLinkFinder | 1,594 | 2026-03-08 — the one still maintained |

Five of those six have not been touched in over two years. They were written when JavaScript shipped
as separate files with readable paths. Then bundlers ate the frontend. jsluice — the strongest of them, from Bishop Fox — has carried [an open issue since
2023-11-21](https://github.com/BishopFox/jsluice/issues/24) saying it *"cannot accurately identify
websites with JavaScript bundled using webpack."* Nobody fixed it, because fixing it means no longer
being a string extractor.

Meanwhile the *collection* half of the problem is thriving — gau, getJS, subjs, ParamSpider are all
maintained. **The ecosystem has four healthy tools for downloading JavaScript and none that can read
it.**

On this repo's own 5-line fixture, against the two most-used extractors — reproducible in 30
seconds, numbers asserted by a test, full detail and caveats in
**[bench/COMPARISON.md](bench/COMPARISON.md)**:

| | jshound | jsluice | LinkFinder |
|---|---|---|---|
| endpoints found | **6 / 6** | 3 / 6 | 3 / 6 |
| HTTP verbs | **6** | 2 | 0 |
| body contracts | **3** | 0 | 0 |
| lazy chunk URLs | **3 / 3** | 0 | 0 |

jsluice gets real credit there: it understands `XMLHttpRequest.open`, `fetch` and `sendBeacon`, and
reports two verbs correctly. What it cannot do is follow a value across a module boundary, which is
where the other three endpoints live.

A fixture written by one of the entrants is weak evidence, so the same three tools were run against
a real application with an independent answer key — **Grafana publishes its own OpenAPI document**,
206 paths, so what counts as a real endpoint is not our opinion
(**[bench/RESULTS.md](bench/RESULTS.md)**, with the commands to reproduce it):

| tool | recall | verb accuracy | undocumented found | noise |
|---|---:|---:|---:|---:|
| **jshound** | **89%** | **100%** | **69** | **2** |
| jsluice | 44% | n/a | 21 | 4 |
| LinkFinder | 44% | n/a | 19 | 14 |

Recall is exactly double at the same precision: these tools are not separated by how much they
print, but by how much of the application they reach. And `undocumented` is the column that matters
for reconnaissance — recovering an endpoint somebody already wrote down is worth nothing.

## What "reads it" means

A grep finds strings. A bundle doesn't contain the strings.

**Lazy chunk URLs are never written down.** Webpack ships a function and calls it at `import()` time:

```js
__webpack_require__.u = e => "static/chunks/" + e + "." + {94:"a1b2",96:"c3d4"}[e] + ".js"
```

The string `static/chunks/94.a1b2.js` appears nowhere in the bundle, so no regex and no
literal-collecting AST pass will ever produce it — yet that is where most of the app's code, and
therefore most of its endpoints, lives. jshound recovers the builder and **runs it**, once per id
it can find:

```
$ jshound chunks runtime.js        # play.grafana.org's webpack runtime
{"publicPath":"public/build/", "chunks":[... 694 URLs ...],
 "stats":{"builders":1,"idsSeen":694,"idsResolved":694,"speculative":0}}
```

`stats` is there so "this bundle declares no chunks" stays distinguishable from "it declares 694 and
we could only build 12". That distinction is how two real bugs in this tool were found.

**Values are assembled, not literal.** `${k(c.baseUrl)}admin/realms/...` is opaque only if you stop
at `c`. `c` is an instance, instances get constructed, and somewhere in another chunk is
`this.baseUrl = t.adminBaseUrl`. jshound indexes every property assignment across the whole batch
in a pre-pass and follows the link — which is the difference between an unresolved prefix on 474
endpoints and the deployment's real base URL.

**Where it genuinely cannot resolve something, it says so.** An unresolvable slice becomes `{EXPR}`
and the record is marked `body_partial` with a note: *"part of it is assembled from a value this
analysis could not resolve. The fields named are real."* That marker is a typed hand-off, not a
miss — and it is the reason you can trust the records that aren't marked.

## GraphQL apps are not a dead end — they are the better target

On a REST app the surface is a list of paths. On a GraphQL app there is **one** path, so a tool that
reports `POST /graphql` and stops has found the endpoint and told you nothing. The question is which
operations the client knows how to send, and what each one takes.

Those documents ship in the bundle, because the client has to send them:

```bash
jshound graphql ./bodies --summary
```

Real output from [Twenty](https://github.com/twentyhq/twenty)'s public bundle — **110 operations**,
of which these are four:

```
mutation  GrantWorkspaceCredits   ($workspaceId: UUID!, $amount: Float!,
                                   $type: BillingCreditGrantType!, $reason: String, ...)
mutation  DeleteWorkspaceUsageLimit($workspaceId: UUID!, $usageLimitId: UUID!)
query     WorkspaceBillingAdminPanel($workspaceId: UUID!)
mutation  SetAdminDefaultAiModel  ($tier: AiModelTier!, $modelId: String!)
```

Admin-panel mutations, with every argument typed, recovered from JavaScript served to anyone. Each
`$workspaceId: UUID!` is an authorization test that writes itself — and the **type** is the part
minification cannot destroy, which is what tells you a slot wants a workspace id and not a user id.

A list of names is still a finding you have to hand-translate into requests, so it exports as HAR —
one replayable POST per operation, carrying the full document and a variables skeleton keyed by name
with the GraphQL type as the value:

```bash
jshound graphql ./bodies --har https://api.example.com/graphql -o ops.har
```

The same scan returns **0 operations on Grafana and 0 on Notion**, which is correct: one is REST and
the other is RPC. A tool that found GraphQL in them would be inventing it.

## The output

A record is a contract, not a URL:

```json
{
  "sink": "fetch",  "method": "POST",  "template": "/api/seen/subscribe",
  "body_keys": ["email", "pageUri"],
  "body_schema": { "email": "computed:trim", "pageUri": "state:href" },
  "body_encoding": "json",
  "req_headers": { "content-type": "application/json" },
  "_page": "https://app.example.com/pricing",
  "body_encoding_basis": "read from the call site"
}
```

`body_schema` names each field *and where its value comes from*. `_page` is the page that loads the
code. `body_encoding_basis` distinguishes a fact read at the call site from one inferred across the
host — because a number you cannot audit is a number you cannot quote.

Measured on play.grafana.org (163 scripts): **1,268 records, 854 endpoints, every one with a verb,
302 carrying body information.** That last number distinguishes two things a single count cannot: a
resolved field list where the bundle has one, and — where the field names are a compile-time type
erased at build — the expression that produces the body, so "sends a `check` object" never reads as
"sends nothing".

On Linear — 523 scripts — the same run yields 30 endpoints, because Linear is GraphQL and has few
distinct paths to recover. That is the honest answer for that application, not a tool failure, and
it is worth knowing before you point this at one.

## Straight into your proxy

| `-f` | Format | Consumer |
|---|---|---|
| `openapi` | OpenAPI 3.1 | Burp [OpenAPI Parser](https://portswigger.net/bappstore/6bf7574b632847faaaa4eb5e42f1757c), Caido, Postman, Insomnia |
| `har` | HAR 1.2 | Burp [HAR Importer](https://portswigger.net/bappstore/67c8a830b8de492b91f41b4eac5ab021) → Site Map, Caido, DevTools |
| `urls` | one per line | nuclei, ffuf, httpx, feroxbuster |
| `jsonl` | native records | everything above, plus your own tooling |

Provenance survives every export. An operation whose `x-jshound.path_fully_resolved` is `false`
still contains a placeholder — replaying it without substituting a value requests a literal
`{param1}`. The exports keep that distinction rather than flattening it to look cleaner.

## Install

```bash
git clone https://github.com/AmbitSecurity/jshound && cd jshound
npm install          # @babel/parser, traverse, generator
pip install -e .
```

## Use

```bash
# collect a page's JavaScript, following declared chunks to a fixpoint
jshound fetch https://app.example.com -o ./bodies
#   chunks: 171 declared over 5 round(s), 165 fetched, 0 unreachable

# contracts from a directory of saved JS/HTML — makes NO network requests
jshound analyze ./bodies --host app.example.com -f openapi -o spec.json

# lazy chunks the browser never loaded — webpack's runtime builder AND Vite's literals
jshound chunks main.js -q

# a webpack bundle as a readable module tree
jshound modules ./out main.js

# the route table the BUILD declares — Next, Nuxt or SvelteKit (this one fetches)
jshound manifest https://supabase.com/dashboard
#   next, declared by the build: 293 routes (103 api, 190 page) · 191 chunks · 2 rewrites
jshound manifest https://nuxt.com
#   nuxt, declared by the build: 900 prerendered routes + 0 from route rules
jshound manifest https://svelte.dev
#   sveltekit, declared by the build: 24 routes · 24 __data.json endpoints · 1 matchers
jshound manifest https://reactrouter.com
#   react-router-v7, declared by the build: 13 routes · 8 data endpoints (<path>.data) · 1 accept a submission

# Next.js route enumeration — uses the manifest when there is one, derives from chunks when there isn't
jshound routes https://app.example.com

# what built this app, and what to expect from it (reads ./bodies, does NOT fetch)
jshound fingerprint ./bodies
#   bundler: vite · framework: sveltekit · 23 files scanned

# the GraphQL operations the client ships — type, name, typed variables, selection
jshound graphql ./bodies --summary
# ...or as replayable POSTs to a known endpoint, ready for Burp/Caido
jshound graphql ./bodies --har https://api.example.com/graphql -o ops.har

# the auth surface a server serialised into an attribute — reaches nothing else
#   Datadog declares login/OIDC/forgot-password/recaptcha/TOTP in a hidden input, in a page with no form;
#   Slack's three OAuth entry points are JSON-escaped, so `grep` finds them in zero files.
jshound analyze ./bodies -f urls | grep account

# what did we MISS?
jshound residual ./bodies records.json

# ...and what did we INVENT?
jshound precision ./bodies records.json
```

For anything the simple fetcher cannot reach — an app that injects its own script tags, sits behind
a login, or needs a real browser — bring your own traffic:

```bash
# DevTools -> Network -> Save all as HAR   (or Burp / Caido export)
jshound from-har session.har -o ./bodies
jshound analyze ./bodies -f openapi -o spec.json
```

That path also reaches the chunks a browser only loads *after* login, which no anonymous crawl sees.

As a library:

```python
import jshound

records = jshound.analyze("./bodies", host="app.example.com")
spec    = jshound.to_openapi(records, title="app.example.com")
chunks  = jshound.chunk_urls(open("main.js").read())
```

## Analysis does not fetch

`analyze` reads bodies someone else downloaded, and that split is deliberate. The earlier design did
its own fetching: it pulled from a raw IP while crawling went through a stealth browser, got
firewall-banned from three in-scope hosts, and on one 643 KB page spent its entire budget walking
1,271 links and fetched zero scripts — then reported "0 endpoints", which reads exactly like "this
host has no API."

So: point a crawler, a proxy export, or curl at the target, and point jshound at what came back —
`from-har` takes a DevTools, Burp or Caido export directly. Mostly-passive analysis of bytes you
already have is also the quietest way to do this.

`fetch` is the convenience case and is deliberately small: it reads `<script src>`, inline scripts
and `<link rel=modulepreload>`, follows static ES imports one level, and asks each bundle which lazy
chunks it declares. It does not execute anything, so an app that builds its script tags at runtime
gives up little — use `from-har` there rather than expecting more of it.

## What it does not do

Stated because a recon tool that only reports its wins is a recon tool you cannot calibrate.

- **Flat ESM/Rollup bundles have no module registry**, so `modules` recovers nothing from them —
  correctly reporting "no registry here", not a parse failure. Chunk enumeration and contract
  extraction both work fine on Vite; it is only the module *tree* that needs a registry to exist.
  Check `modules` in the output before drawing conclusions from a small one.
- **App Router apps have no chunk table and no build manifest.** `chunks` recovers almost nothing
  from a Next App Router build because Next generates routing from the filesystem, and such a build
  serves no `_buildManifest.js` either — measured, 10 of 34 Next targets served one and every one of
  those was Pages Router. Where a manifest exists it is the best route source there is; where it does
  not, `routes` falls back to deriving routes from `app/.../page-HASH.js` chunk paths, and that needs
  the build to emit named route chunks. None of the six App Router targets tested do — Turbopack and
  webpack's `deterministic` chunk ids both produce opaque filenames — so `routes` returns 0 routes
  there and says so. Path literals mined from the code are what is left, and that is the honest
  answer for those apps.
- **GraphQL and RPC apps have few distinct paths**, and no tool can invent what the app does not
  have. Reach for `graphql` on the first and accept a short list on the second — Notion dispatches
  everything through `/api/v3/<operationName>` computed at runtime, and that is the honest answer.
- **A declared route and an inferred one are not the same claim**, and the output never merges them.
  `routes` prints the split (`293 declared by the build, 0 derived from chunk paths`) because a
  manifest route is only wrong if the build is wrong about itself, while a derived one is only as
  good as the inference behind it.
- **Source maps are not a route-discovery mechanism** — measured, they add 0–11% of routes and are
  absent on roughly half of targets. What they uniquely carry is *types* and *original parameter
  names*, which minification genuinely destroys.
- Nothing here was observed on the wire. These are the requests the client is **built** to make.

## Measuring inventions

`residual` cannot see a fabricated endpoint. It measures literals that went unclaimed, and an
invention is a claim with no literal behind it — so an extractor can score a perfect zero on
residual while reporting URLs that do not exist. Six such bugs were found in this tool by looking
the other way: a tRPC chain built a path out of minified identifiers, moment's locale table turned
`"MM/DD/YYYY"` into an endpoint, requests with a body but no URL were counted as endpoints, a
`new Map()` lookup was counted as a GET, a bundle containing no tRPC at all reported tRPC
endpoints, and one object literal defining a UI label — Grafana's `{type:"mixed", uid:"-- Mixed --"}` —
was borrowed into every `${x.uid}` slot in the bundle, inventing twenty addresses like
`/datasources/uid/-- Mixed --/health`. `precision` now checks for that class directly, because its
other test cannot: every fragment of that path really is in the corpus, and only the composition was
invented.

The last two have the same cause and the same answer, and it is not a better filter on the string:

- **`.get` is an HTTP verb and also a method on Map, URLSearchParams, Headers and FormData.** The
  sink fired on all of them, and the only thing in the way was whether the argument looked
  path-shaped — which `"user/profile"` does. The argument was never the problem. Where the receiver
  is a local binding its construction site says what it is, so a receiver built by `new Map()` is
  rejected. Measured over 21 corpora: 3 records removed, 0 added, and all 3 verified at the call
  site — Hex reported `GET /` from `I = new Map(); I.get(location.pathname)`.
- **A manufactured URL owes the bundle some evidence.** The tRPC collector does not read a URL, it
  builds `/api/trpc/<chain>` out of identifier names, so it now requires the corpus to show tRPC at
  all. This rejects every look-alike at once instead of naming them: `queries.workspace.members.useQuery()`
  fabricates `/api/trpc/workspace.members`, and no property-name blocklist can see that coming. It
  removed nothing from the 21 corpora measured — only cal.com has tRPC records and it is a real tRPC
  app — and it *gained* 2 endpoints there. The evidence is read from the AST, not the text (a file
  whose *comment* mentioned `/api/trpc` used to count as a tRPC app) and it is settled once over the
  whole corpus, not per file (asking per file cut cal.com from 45 to 3).

```bash
jshound precision ./bodies records.json
```

A fabricated endpoint is worse than a missed one: a miss costs coverage, an invention sends someone
at a URL that was never there and looks identical to a real finding. Two further tests were tried
and **removed rather than shipped** — one flagged 44% of tldraw's endpoints, the other called
`/api/app/user_id/init` questionable because `app` is three characters — because a checker nobody
trusts is worse than no checker. What ships runs at ~3% on real corpora and 0% on the fixture.

## Measuring misses

`residual` exists because a count of endpoints is a number you cannot check — `1546 routes` reads
identically whether the extractor got everything or half of it. So it greps every path-shaped string
out of the bundles, subtracts everything the extractor attributed, and buckets the remainder. One
bucket matters: `MISSED-apiish` is a bug list, and a large one means a tool with a hole, not a target
with a big API.

Run it against your own targets before you trust any of the numbers above.

## License

MIT
