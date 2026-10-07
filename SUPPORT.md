# What this works on

Every row was measured by running the tool, not by reading a bundler's documentation. Numbers move
as these sites ship; the point is which capabilities engage on which stack, and where the zeros are
honest.

Regenerate any row with:

```bash
jshound fetch <url> -o ./bodies && jshound fingerprint ./bodies
jshound analyze ./bodies && jshound chunks <biggest>.js && jshound graphql ./bodies
```

## Measured

| target | bundler | framework | module ids | endpoints | w/ body | chunks | gql ops | modules |
|---|---|---|---|---|---:|---:|---:|---:|---:|
| Grafana | webpack 5 | — | mixed | 907 | 314 | 697 | 0 | 2 |
| GitLab | **webpack 4** | — | deterministic | 134 | 23 | **1272** | 10 | **648** |
| Loom | rspack | — | mixed | 62 | 13 | **2348** | **141** | 40 |
| Home Assistant | rspack | — | — | 35 | 5 | 1078 | 0 | **366** |
| tldraw | vite (+webpack via Clerk) | — | numeric | 74 | 30 | 171 | 0 | 1 |
| Excalidraw | vite | — | named (paths) | 5 | 2 | 142 | 0 | 1 |
| solidjs.com | vite | — | — | 3 | 1 | 1032 | 0 | 1 |
| Twenty | turbopack | next | mixed | 29 | 7 | 535 | **110** | 1 |
| Vercel | turbopack | next | mixed | 40 | 15 | 19 | 0 | 1 |
| Linear | vite | next | — | 30 | 12 | 121 | 4 | 1 |
| Element | webpack | — | named (paths) | 13 | 2 | 10 | 0 | 12 |
| nuxt.com | vite | nuxt | mixed | 9 | 3 | 87 | 0 | 1 |
| svelte.dev | vite | sveltekit | — | 3 | 1 | 9 | 0 | 1 |
| remix.run | esm (no runtime) | remix | — | 1 | 0 | 0 | 0 | 1 |
| angular.dev | esm (no runtime) | angular | named (paths) | 1 | 0 | 0 | 0 | 1 |
| astro.build | esm (no runtime) | astro | — | 1 | 0 | 0 | 0 | 1 |
| Discourse | esm (no runtime) | **ember** | — | 16 | 7 | 0 | 0 | 1 |
| qwik.dev | esm (no runtime) | **qwik** | — | 4 | 3 | 15 | 0 | 1 |
| material.angular.dev | esm (no runtime) | **angular** | — | 1 | 0 | 0 | 0 | 1 |
| emberjs.com | webpack | ember | — | 1 | 0 | 4 | 0 | 1 |
| Railway | vite | — | — | 7 | 1 | 201 | **954** | — |
| Clerk | rspack | next | — | 260 | 58 | 145 | 0 | — |
| Coda | webpack | — | — | 243 | 154 | 71 | 0 | — |
| hex.tech | rspack | — | — | 16 | 7 | 1254 | **187** | — |
| PostHog | esm (no runtime) | — | — | 151 | 26 | 321 | 0 | — |
| cal.com | turbopack | next | — | 60 | 12 | 18 | 0 | — |
| Supabase | turbopack | next | — | 48 | 35 | 53 | 0 | — |
| Sentry | webpack | — | — | 3 | 0 | 1724 | 0 | — |
| Retool | vite | — | — | 22 | 1 | 0 | 0 | — |
| Amplitude | vite | — | — | 56 | 38 | 25 | **106** | — |
| Notion | rspack | — | — | 15 | 8 | **2457** | 0 | — |
| Zapier | turbopack | next | — | 31 | 11 | 18 | 0 | — |
| Figma | rspack | — | — | 11 | 2 | 0 | 0 | — |
| Airtable | esm (no runtime) | — | — | 5 | 3 | 5 | 0 | — |
| Mixpanel | webpack | next | — | 6 | 5 | 58 | 2 | — |
| Dropbox | webpack | — | — | 109 | 8 | 418 | 0 | — |
| Webflow | webpack | — | — | 63 | 22 | 19 | 1 | — |
| HubSpot | webpack | — | — | 43 | 32 | 128 | 0 | — |
| Slack | rspack | — | — | 12 | 1 | 472 | 0 | — |
| *local build* | **parcel** | — | — | 3 | 2 | 1 | 0 | — |
| *local build* | **webpack MF** | — | named (paths) | 3 | 2 | 2 | 0 | — |
| *local build* | **metro** | — | — | 3 | 1 | 0 | 0 | — |
| *local build* | **esbuild** | — | — | 3 | 2 | 1 | 0 | — |
| *local build* | **rollup** | — | — | 3 | 2 | 1 | 0 | — |

## Bundlers

**webpack 5** — a runtime builder (`__webpack_require__.u`) is evaluated to produce chunk URLs, and
the module registry is walked. Everything engages.

**webpack 4** — no `.u`; chunk URLs come from a named function
(`function jsonpScriptSrc(e){ … ".chunk.js" }`). Matched on shape rather than name. Its jsonp
*runtime* has the same shape and **contains** the builder, so a builder must return a URL from its
own body — without that GitLab reported 2,553 ids seen against 1,272 resolved.

**Rspack** — webpack-compatible, usually emitting ESM. Home Assistant loads its whole frontend
through `<link rel="modulepreload">` and ships no `<script src>` for those modules at all.

**Turbopack** — no URL builder; the chunk list is a literal array, and the runtime announces itself
as `globalThis.TURBOPACK`, a global member rather than an identifier.

**Vite / Rollup** — writes chunk paths down instead of building them: a dynamic import's own
specifier, and the `__vite__mapDeps` dependency table. These are literals, so they are reported as
structured rather than speculative. No module registry exists, so `modules` finds nothing **by
design** — that is the `1` in the column above, not a failure.

**esbuild / Rollup** — verified against real `esbuild --bundle --splitting` and `rollup -f es`
builds. Nothing broke: contracts extract and the split chunks are found from the dynamic-import
specifiers. Both report `esm (no bundler runtime marker)`, which is the correct answer rather than a
detection failure — pure ESM output carries no runtime global, which is also why remix.run,
angular.dev and astro.build read the same way.

**Metro** (React Native, RN Web) — verified against a real `metro build --platform web`. Contracts
extract unchanged, including `new URL(path, base)` and `XMLHttpRequest.open`. Detection needed its
own marker: Metro's registry is `__d(factory, id, deps)` and `__r(id)` — one and two characters,
far too generic — while every bundle opens by assigning `__METRO_GLOBAL_PREFIX__` and
`__BUNDLE_START_TIME__`. It ships one bundle rather than chunks, and its registry is not walked.

**Parcel** — verified against a real `parcel build`. Two things broke on it: the runtime global is
suffixed per build (`parcelRequire5707`, not `parcelRequire`), so an exact-name map saw no bundler
at all; and its chunk table is an **object**, `Object.assign(o.i ??= {}, {jrWAC: "feature.…js"})`,
where the Vite pass only read arrays.

**webpack Module Federation** — verified against a real `ModuleFederationPlugin` build. Its
chunk-URL builder carries no id map — `n.u = e => e + ".5eb33312.js"` — so harvesting object
literals inside the builder found nothing and it was never run. The ids live in the
`__webpack_require__.e(480)` call sites instead, which is where every lazy import asks for its
chunk, so those are now harvested when a builder has no table of its own.

## Module id modes

All three appear in the wild and each one broke registry detection differently:

| mode | looks like | seen on |
|---|---|---|
| numeric | `{134: fn, 6710: fn}` | Grafana, tldraw |
| named | `{"src/vector/app.tsx": fn}` | Element, angular.dev |
| deterministic | `{"++os": fn, "/23t": fn}` | GitLab — 849 entries, one numeric |

The registry is recognised by **shape** — an object whose values are nearly all small-arity
functions — because a key-format test rejected 848 real modules on GitLab.

## A page loads other people's code

Bundler signals are counted across every file and **reported with the host that served them**,
because you cannot separate first-party code by hostname. tldraw is a Vite app that bundles Clerk's
auth SDK — which is webpack, and is served from `clerk.tldraw.com`, tldraw's own subdomain. Trying
to exclude "third-party" hosts also broke Grafana, whose assets live on `grafana-assets.grafana.net`
while its document is `play.grafana.org`: a different registrable domain entirely.

```
$ jshound fingerprint ./bodies
  bundler_hosts: vite → www.tldraw.com (10); webpack → clerk.tldraw.com (49)
```

Read the breakdown before trusting the headline `bundler`, which is simply whichever signal appears
in most files. tldraw's says webpack; the hosts say that is all one auth subdomain, and `modules`
returning nothing for the app itself is correct rather than a failure.

## Frameworks

Named only from the **served document**, parsed as HTML: an element id, a `data-` attribute, the
path a preload points at. A framework global seen only in JavaScript is listed but not crowned —
Grafana's document contains zero Angular markers while its chunks contain ten, because it vendors
AngularJS for plugin compatibility. A corpus with no HTML in it names no framework, and that is
correct rather than a miss.

Operation recovery was checked in both directions: every `(query|mutation|subscription) Name {` in
the corpus is either extracted or is English prose in an enum label — `"stripe subscription updated
webhook"` is not an operation. **Zero real misses across Railway (954), hex.tech (187) and Loom
(141)**, and zero structurally malformed records in a random sample of Railway's.

Detected from the document: Next, Nuxt, SvelteKit, Remix, Astro, Angular, Qwik, Solid, **Ember**
(which names its own app in `<meta name="<app>/config/environment">` — the app name varies, the
suffix does not).

**One exception to the document rule.** A client-rendered Angular app writes nothing into the served
document — `ng-version` is added during hydration — so it would never be named. Its compiler output
is static class properties (`X.ɵfac`, `X.ɵcmp`), which are real member names in the AST:
material.angular.dev carries `ɵfac` 219 times. A framework signal carried by **≥20% of the files**
is therefore allowed to name the framework, reported as `javascript (14/25 files)`. Grafana's
vendored AngularJS is 10 of 163 — 6% — and is still correctly not named.

## Measured misses

`residual` greps every path-shaped string out of a corpus and subtracts everything the extractor
attributed, so the remainder is a bug list rather than an opinion. Across fourteen corpora:

```
Grafana 0 · GitLab 0 · Loom 0 · Home Assistant 0 · Excalidraw 0 · tldraw 0
PostHog 0 · Clerk 0 · Supabase 0 · cal.com 0 · Amplitude 0 · Notion 0 · Zapier 0
Coda 1 (0.1%) · Vercel 8 (1.2%)
```

Both non-zero results were read rather than counted. Vercel's eight are Next *page* routes
(`/internal/registry`, `/internal/revalidate-marketing`) — real surface, but route strings rather
than call sites. Coda's one is `/oauth/token` appearing inside a JSDoc comment and an Atlassian
configuration example: documentation prose that happens to live in the bundle. Neither is an
endpoint the extractor failed to find.

## A document with no `<script src>` at all

A SvelteKit document links no script. Its only `<script>` is an inline bootstrap that does
`Promise.all([import("./_app/immutable/entry/start.X.js"), import("./_app/immutable/entry/app.Y.js")])`,
so a fetcher that reads tag attributes finds nothing to analyse and the extractor never gets a file.
Measured on realworld.svelte.dev: **1 script and 2 records before following inline imports, 22 and 46
after**. The fetcher now parses inline scripts for their dynamic imports and follows them, counted
under an `inline-import` discovery path so the breakdown still sums. Everything else still applies to
those URLs — a 200 carrying `index.html` is rejected whether it was found as a `src` or as an import.

## The fetcher is the limit, not the extractor

On a defended login page the simple fetcher gets very little, and that is where results stop — not
in the analysis. Measured across the sweeps: Shopify returned 2 JavaScript files, Canva 3, Asana 4,
against 121 for Slack and 118 for HubSpot on the same settings. Those three ship almost no
`<script src>` and build their page at runtime.

Nothing in the analyser can fix that; it never sees the code. Use `from-har` there — browse the app
in a real browser, export, and point `analyze` at it. That path also reaches the chunks a browser
only loads after login, which no anonymous crawl sees.

## Where the files come from

`chunks` reads JavaScript, so a build with no JS chunk table reports 0 — and that is not the same as
finding nothing. Angular's esbuild CLI declares its chunks in the document as
`<link rel="modulepreload" href="chunk-B03JrT7s.js">`, so 21 of material.angular.dev's 27 files are
chunks that `fetch` collected while `chunks` correctly reported none. `fetch` therefore reports the
discovery path, and the counts sum to the total:

```
material.angular.dev   26 script(s)  [11 es-import, 10 modulepreload, 3 inline, 2 script-src]
play.grafana.org       34 script(s)  [25 chunk-table, 5 script-src]
```

That is what distinguishes "this build has no chunk table" from "we did not find it".

## Route tables the build declares

Two frameworks hand over their route table outright instead of making you infer it, and a declared
route is a different kind of result from an extracted one: an inferred endpoint can be wrong, while a
manifest route is the build's own statement of what it routes, so the only way it is wrong is if the
build is wrong about itself. `jshound manifest` reads whichever applies.

| framework | where | what it declares |
|---|---|---|
| Next, Pages Router | `_buildManifest.js` linked from the document | every route with parameters NAMED, per-route chunks, `__rewrites`, prerendered set, middleware matchers |
| Nuxt | `<base>builds/latest.json` → `<base>builds/meta/<id>.json` | prerendered routes, `routeRules` patterns and their redirect/proxy targets |
| SvelteKit | the `dictionary` export of the client entry module | every client-routed route, its matchers, and a `__data.json` endpoint each |
| Remix v2 | `window.__remixManifest`, inlined | route tree, which routes have a loader or accept a submission, a `?_data=` endpoint each, every route's chunks |
| React Router v7 | `window.__reactRouterManifest`, in a linked `manifest-<hash>.js` | the same, with a `<path>.data` endpoint each |
| Next, App Router | — | nothing; see below |

### Remix and React Router v7

`__remixManifest` was already used here as a *fingerprint* marker — proof the app is Remix — and
nothing read anything out of it. It declares the route tree, `hasLoader`, `hasAction`, and every
route's chunk URLs.

| target | flavour | routes | data endpoints | submissions | chunks |
|---|---|---|---|---|---|
| reactrouter.com | react-router-v7 | 13 | 8 | 1 (`/color-scheme`) | 18 |
| hydrogen.shop | remix v2 | 2 | 2 | 0 | 23 |

**The data protocol differs by version, and that was checked rather than assumed:**

- **Remix v2** — `<path>?_data=<route id>`. hydrogen.shop: `?_data=root` → 200, 6,272 B of
  `text/remix-deferred`; `?_data=routes/($locale)._index` → 200, 10,750 B; an **invented id** → 403.
- **React Router v7** — `<path>.data`. reactrouter.com: `/home.data` → 200, 33,582 B;
  `/changelog.data` → 200, **801,465 B**; `/healthcheck.data` → 200; `/invented-route.data` → **404**.

**On v7, `?_data=root` answers 200 with `text/html`** — the ordinary page, carrying no data. Emitting
the v2 form there would look like a hit and be worthless, so the protocol is keyed on which global the
manifest was assigned to. The 403 and the 404 on invented ids are what make these safe: the ids come
from the manifest, and for v2 a route list alone would not be enough, because the route id and the URL
path are different strings.

Two parsing details that cost something to learn. v2 inlines the manifest in a `<script type="module">`
that also carries ESM `import` statements, so running the script in a vm throws `Cannot use import
statement outside a module` before the assignment executes — the first version recovered nothing, and
now only the assigned expression is evaluated. v7 links `manifest-<hash>.js` instead, and the hashed
name has to be read from the document, which is the lesson the Next reader already paid for.

Pathless layout routes share a URL — `root`, `docs` and a splash index all resolve to `/` on
reactrouter.com — so routes are kept per id while data endpoints are deduplicated. That output had
`/.data` three times before.

### SvelteKit

SvelteKit has no manifest file — it routes on the client, so the table is *inside* the entry module,
minified to an export alias (`export { s as dictionary }`). It is read by parsing the module and
resolving that alias through scope. Measured, **12 of 16 targets, 205 routes**:

| target | routes | matchers | constrained |
|---|---|---|---|
| sveltesociety.dev | 48 | 0 | 0 |
| scottspence.com | 25 | 0 | 0 |
| svelte.dev | 24 | 1 | 1 |
| shadcn-svelte.com | 18 | 1 | 1 |
| realworld.svelte.dev | 10 | 0 | 0 |
| joyofcode.xyz | 8 | 0 | 0 |
| melt-ui.com, okupter.com | 7 | 0–1 | 0 |
| hn.svelte.dev | 5 | 2 | 2 |

Every route also has a data endpoint: SvelteKit's client fetches load output from
`<route>/__data.json`, and those are live — on shadcn-svelte all six concrete routes answer 200 and
every `__data.json` answers 200, while `/invented-xyz` answers 404. That is fetchable JSON surface no
link on the page points to.

Three things had to be right for this to work at all, and each was wrong first:

- **Resolve the export through SCOPE, not by name.** A minified module reuses one-letter names in
  different scopes, so searching for "the declarator called `R`" finds whichever came first in the
  file. On shadcn-svelte that walked `dictionary` to a Svelte template fragment. A structural check
  backs it up: if the resolved object has no route-id keys, the answer is "no route table" rather
  than a list of invented routes.
- **Read inline-script imports, not just tag attributes.** SvelteKit's bootstrap does
  `import("./_app/immutable/entry/app.X.js")` from an inline script, so the entry is not a `src`
  anywhere. Attributes alone reported sveltesociety.dev, okupter.com, realworld.svelte.dev and
  sveltelab.dev as "not a SvelteKit app" — all four are one, and they account for 70 of the 205 routes.
- **Resolve relative references against the URL the document came from.** kit.svelte.dev redirects to
  svelte.dev and links `./_app/...`; resolving against the *requested* URL pointed at the wrong host.
  This applies to all three readers and is fixed in `http_get`.

A SvelteKit `+server.ts` endpoint is **not** in this table — the client never client-side-routes to an
API route, so the manifest the client receives does not mention one. Those come from the AST extractor
reading the code that calls them. `server_loads` is reported raw for the same reason the chunk-path
derivation is reported separately: mapping its node indices back to routes depends on the sign and
nesting in the dictionary's values (`[5]`, `[-12]`, `[-7, [2]]`), which is not a documented contract,
so no per-route claim is made from it.

Measured, Nuxt — **10 of 19 targets served a readable app manifest, 1341 routes in total**:

| target | prerendered | from route rules | rules |
|---|---|---|---|
| nuxt.com | 900 | 0 | 0 |
| ui.nuxt.com | 189 | 0 | 0 |
| unjs.io | 82 | **64** | 66 |
| content.nuxt.com | 62 | 0 | 0 |
| image.nuxt.com | 53 | 0 | 0 |
| i18n.nuxtjs.org | 44 | 0 | 0 |
| devtools.nuxt.com | 11 | 0 | 0 |

A route rule's *pattern* is a requestable path in its own right, and mostly a different one: 64 of
unjs.io's 66 rules name paths absent from `prerendered`, and each answers 302 where an invented path
answers 404. They are reported in their own field, because "the build rendered this" and "the build
declared a rule matching this" are not the same claim. The rules also carry their destinations —
`{"redirect": "/packages/h3"}`, and on an app that proxies, `{"proxy": "https://api.internal/**"}`,
which names internal structure nothing in the client links to.

Three targets (roe.dev, volta.net, elk.zone) return a readable manifest declaring **zero** prerendered
routes. That is an honest zero — server-rendered with nothing prerendered — and it is reported
differently from the cases where the manifest could not be read at all: www.gitlab.com is Nuxt and
answers 403 (blocked, manifest probably exists), wappalyzer.com answers 200 with HTML (no manifest
there), and a non-Vue site is told it is not Nuxt rather than being given a zero.

## Next.js route tables, declared vs derived

Next is the one framework here that sometimes hands over its route table outright. A **Pages Router**
build writes every route it serves to `_buildManifest.js` and links it from the document, dynamic
parameters still named:

```
/api/platform/storage/{ref}/vector-buckets/{id}/indexes/{indexName}
```

That is a different kind of result from everything else in this repo. An inferred endpoint can be
wrong; a manifest route is the build's own declaration, so the only way it is wrong is if the build is
wrong about itself. `jshound manifest` reads it, `jshound routes` uses it when present, and both
report declared and derived counts separately rather than merging them.

Measured over 34 Next targets — **10 served a manifest, and every one of those was Pages Router**:

| target | page routes | api routes | chunks | rewrites | prerendered |
|---|---|---|---|---|---|
| supabase.com/dashboard | 190 | **103** | 191 | 2 | 0 |
| notion.so | 140 | **24** | 141 | 3 | 8 |
| ted.com | 87 | **20** | 88 | 4 | 26 |
| loom.com | 35 | 0 | 66 | 4 | 33 |
| docs.github.com | 34 | 0 | 53 | 36 | 0 |
| target.com | 16 | 0 | 50 | 3 | 0 |
| hulu.com | 6 | 0 | 19 | 0 | 0 |
| washingtonpost.com | 5 | 0 | 25 | 3 | 0 |
| nike.com | 1 | 0 | 3 | 0 | 0 |

667 routes in total, **147 of them `/api/*`** — Next API routes, which are server code the client
calls rather than pages — plus 655 chunk URLs declared authoritatively. On Supabase's dashboard
`routes` went from 0 to 293 once it read the manifest, and the chunk count from 0 to 436.

Two details that are easy to get wrong and were:

- **The manifest URL is read from the document, never constructed.** Supabase serves its studio
  bundle from `frontend-assets.supabase.com/studio/<hash>/_next/`, so the obvious
  `{origin}/_next/static/{buildId}/_buildManifest.js` 404s and loses all 293 routes. The same URL
  also gives away the `assetPrefix`, which is how declared chunks get fetched from the right host.
- **An App Router build serves no manifest at all**, and then the route table has to come from chunk
  paths — `app/[locale]/login/page-b13a8147.js` → `/login`. That needs the build to emit *named*
  route chunks, and none of the six App Router targets tested do: Turbopack and webpack's
  `deterministic` chunk ids both emit opaque filenames like `0440fqg6n82rw.js`. So `routes` reports
  0 derived routes on those apps and says which source was unavailable. What is left there is path
  literals mined from the code, which is what the fixpoint loop does.

## What the receiver is, versus what the argument looks like

Two of this tool's fabrication bugs had the same shape: a sink that fires on a method NAME, and a
guard that argues about the argument. `.get`/`.post`/`.put`/`.delete` are HTTP verbs and also ordinary
methods on `Map`, `WeakMap`, `URLSearchParams`, `Headers` and `FormData`, so the member-call sink fired
on every one of them and the only thing standing between that and an endpoint was whether the first
argument looked path-shaped. `"user/profile"` looks exactly like a path, because it is one — it was
just a Map key.

Where the receiver is a local binding, its construction site settles it. A value built by `new Map()`
is a Map. The check is one-sided on purpose: it rejects a receiver it can PROVE is not a client and
says nothing about the rest, so `const c = makeClient(); c.get("/v2/orders")` is untouched.

Measured over 21 corpora — **3 records removed, 0 added, 1737 → 1734** — and each removal confirmed at
the call site by resolving the binding:

| corpus | reported | the actual code |
|---|---|---|
| app.hex.tech | `GET /` | `I = new Map(); I.get(F.location.pathname)` |
| play.grafana.org | `GET starred/{min}` | `x = new Map(); x.get(P[0][E])` |
| play.grafana.org | `GET preferences/theme` | a command-palette action id, `{id: "preferences/theme", name: t(...)}` |

Three is a small number, and that is the honest result: this class is rarer in real bundles than the
synthetic case suggests, because a minified Map is usually read with a key that is not path-shaped.
What it removes is specifically the kind that *does* look real.

**A known gap, with its size measured.** The check only resolves an Identifier receiver whose
declarator init is the construction. `let m; m = new Map()` and `this.cache = new Map()` are not
covered, and neither are containers that are not built by a constructor at all — the 5 remaining
records of this kind corpus-wide come from receivers named `_storageService` and `moduleCache`, which
are custom objects. Guessing from the name is what this section exists to avoid, so they stand.

## A manufactured URL owes the bundle evidence

The tRPC collector is different from every other one here: it does not READ a URL, it builds
`/api/trpc/<chain>` out of identifier names, and `/api/trpc/` is a string in this repo rather than in
the bundle. So it owes the bundle some sign that tRPC is there. The decision is layered by how strong
the evidence is:

1. **Is there tRPC in this corpus at all** — an import from `@trpc/...`, a `createTRPC*` name, or a
   string literal containing `/api/trpc`. This is the general rule, and it rejects every look-alike
   at once: `queries.workspace.members.useQuery()` fabricates `/api/trpc/workspace.members` under a
   namespace no blocklist would think to list, and only this rule sees it coming.

   **Measured effect on the 21 corpora here: none.** Only cal.com — a real tRPC app — produces any
   `/api/trpc` records at all, so there was nothing for this rule to remove. The 13 Grafana
   fabrications this collector is known for predate the `endpoints` blocklist and are already gone;
   the claim that this rule removes them would be borrowed from a comment rather than measured, so it
   is not made. What it demonstrably does is close a class the blocklist cannot express, shown on
   `tests/fixtures/trpc-corroboration/no-trpc/`, and it cost cal.com nothing.

   **The verdict is a property of the CORPUS, not a file.** This rule's first version asked the
   question per file and cut cal.com from 45 real tRPC endpoints to 3, because the marker is in 1 of
   its 79 files and the chains are in the others — the same "accident of scheduling" the module index
   exists to prevent. It is now settled once by the corpus-wide index pass, which also *gained* 2
   endpoints cal.com had been missing (`/api/trpc/viewer.insights.routedToPerPeriodCsv` and one
   more), because the old per-chunk reading had been suppressing them too.
2. **What built the chain's root** — `createApi({...})` or `<api>.injectEndpoints({...})` is RTK
   Query; `createTRPC*` is tRPC and stays tRPC even if a procedure is called `endpoints`, which fixes
   a false negative the blocklist could not avoid.
3. **The `endpoints` property-name blocklist**, demoted to last resort. In a minified bundle RTK's api
   object arrives as a cross-module reference with no resolvable construction — Grafana's is
   `hK.endpoints.<name>` — so removing this would re-admit the records it was added for. It is a
   blocklist and is labelled as one; a root proven to be tRPC skips it.

The evidence in (1) is read from the AST, not from the source text. It used to be a regex over raw
source, which cannot tell code from commentary: a file whose comment merely mentioned `/api/trpc` was
marked as a tRPC app. That is not a hypothetical — the fixture written to prove this rule works turned
the marker on with its own explanatory comment, passed for the wrong reason, and is kept as
`tests/fixtures/trpc-corroboration/marker-in-a-comment/`.

## Hash routing or path routing

The field that decides whether a route table is a map or a work list. A hash route lives entirely in
the URL fragment, which browsers never transmit, so the server has never heard of it and answers 404
to every one — measured on Keycloak's admin console, `/master/clients`, `/master/users/abc/settings`
and a route invented for the test all 404'd identically while the bundle held 105 real routes. A path
route is the opposite: the request is made and the shell comes back.

Getting it backwards is therefore worse than saying nothing, and the first implementation got it
backwards. It was a regex over the chunk source, and over 11 corpora holding **557 route records it
produced exactly two labels, and both were wrong** — Grafana's and Slack's, each a path-routed app
called hash-routed. Grafana's came from

```js
"This can also happen if you are using createHashRouter and the user manually changes the URL."
{ HashRouter: () => At, BrowserRouter: () => Bt, createBrowserRouter: () => Ct }
```

— an error message and an export map. A library *exporting* a router says nothing about which one the
app renders, and Grafana calls neither factory by name anywhere in its 159 files.

Call sites are no better on their own. react-router ships both components and each calls its own
history factory inside its own body, so **every** bundle containing react-router contains both calls;
Slack's does:

```js
function createHashHistory(o={}) { ... }                      // the library's own definition
Y.current = createHashHistory({window: H, v5Compat: !0})      // inside HashRouter's body
Y.current = createBrowserHistory({window: H, v5Compat: !0})   // inside BrowserRouter's body
```

So the rule is **who is calling**. A call whose callee binds to a function declared in the same file
is the library calling itself and is not evidence; a call whose callee is imported is code reaching
into the router from outside, and is. Evidence for both sides reports nothing. The verdict is settled
once over the whole corpus, because the router is constructed in one chunk and the route table can
live in another.

Measured result across all 21 corpora: **0 endpoints changed, and the only differences are the two
wrong labels disappearing** — Grafana's 253 routes go from `{hash: 1, unlabelled: 252}` to
`{unlabelled: 253}`, and Slack's single route from `hash` to unlabelled. That is the correct answer in
both cases: the evidence is not in those bundles. None of the 21 corpora here produce a positive,
so the only true-positive coverage is `tests/fixtures/routing-evidence/app-calls-hash/`, and that is
stated rather than dressed up. An app that renders `<HashRouter>` through a minified alias is still
not detectable this way; the reliable test for those is the one the Keycloak measurement actually
used — ask the server.

## One object literal, twenty fabricated addresses

The cross-file property index is what lets `c.baseUrl` resolve when the assignment is three modules
away: it answers "what value does this property usually hold". `_GENERIC_PROP` already stops a value
being borrowed under a name too common to mean anything — `url`, `id`, `name` — after reddit's
ActionCable socket was reported at a Zendesk documentation page.

The other half was missing: a name distinctive enough, holding a value that is not an address.
Grafana defines its mixed-datasource sentinel once,

```js
const L = { type: "mixed", uid: "-- Mixed --" };
```

and the index recorded `uid → "-- Mixed --"` and filled **every** `${x.uid}` slot in the bundle with
it. Twenty endpoints like `/datasources/uid/-- Mixed --/health`, where the real path is
`/datasources/uid/{uid}/health`.

A raw space cannot appear in a URL path or query — it has to be percent-encoded — so a borrowed value
carrying whitespace is a label, not an address, and the named slot is the honest answer. The test is on
the VALUE rather than another name added to `_GENERIC_PROP`, because the next sentinel will have a
different property name and the same shape. Checked before shipping: across the 21 corpora **27
templates held whitespace in their skeleton and all 27 were this one sentinel**, so there was nothing
legitimate to lose.

Removing fabrications RAISED the count, because the fake templates were shadowing the real ones:
Grafana **886 → 888 endpoints**, 27 removed and 29 correct named-slot forms taking their place
(`/datasources/uid/{uid}`, `/cloudmigration/migration/{uid}`). Against Grafana's own OpenAPI document
every column is unchanged — 827 reported, 184 hits, 30% precision, 89% recall, 100% verb accuracy —
which is the expected result, since a fabricated path was never a spec hit.

**`precision` could not see this, and now can.** Its `not-in-corpus` test asks whether any substantial
fragment of a path appears in the corpus. Every fragment of `/datasources/uid/-- Mixed --/health` does,
including the sentinel — the characters were all present, it was the composition that was invented. So
the checker reported 4 questionable while 20 fabrications sat in the output. It now also reports
`unencoded-space`, which would have caught all 27; measured on the other corpora it reports zero, and
on Grafana after the fix it is back to 4 (0.4%).

## Server state serialised into an attribute

The one document channel nothing here read. `_LOOKS_URL` rejects any value beginning `{` before any
other gate, so a JSON **document** carried in an HTML attribute reached nothing — and what it carries
is routinely the application's declared auth surface.

| site | carrier | what it declares |
|---|---|---|
| Datadog | hidden `<input value>` | `login_url`, `openid_login_url=/account/oauth2?provider=google`, `forgot_password_url`, `recaptcha_login_url`, the TOTP and MFA-recovery URLs, `allowed_login_methods=[SAML,google_oidc,standard]` — 688 bytes, in a document with **zero** `<form>` tags |
| Slack | `data-props` | three OAuth entry points, `https://slack.com/signin/oauth/{apple,google,microsoft}/start?email_first=1&…` |
| Asana | `<meta content>` | its own `/-/oauth` endpoint and the IdP authorize URLs it accepts, in a 17 KB blob |

**The Slack case is why this needs a parse and not a search.** `grep -rF 'signin/oauth'` across that
corpus — the document and all 121 scripts — finds those URLs in **zero** files, because JSON escapes
`/` as `\/`. No text-based tool reaches them. Asana's blob is dropped by `config_values()` for a
different reason: it does read `<meta>`, but caps content at 400 characters.

**The gate is the attribute NAME, and that is measured rather than preferred.** No test on the VALUE
can separate serialised state from a rendered code sample — deno.com puts a `deno.json` snippet in a
`code` attribute whose `workspaces: ["./api","./core","./web"]` are directory names that satisfy every
URL-shape test. Taking any JSON-valued attribute yields 71 leaves across 6 sites; the allowlist keeps
24, and all 19 dropped are noise or navigation: GitLab's serialised sidebar menu (13), Hex's 100 KB of
LaunchDarkly flag state (3 URLs, one of which is already in the bundle), and that code sample (3).
All three produce zero now, and each is a case in `tests/fixtures/doc-state/`.

Scope, stated plainly: **77 of 104 saved corpora yield nothing here.** This is a narrow channel that
pays off on auth pages and is silent elsewhere. Across the 21-corpus regression it adds 3 records and
changes **0** endpoints.

Two deliberate limits:

- They are `doc-state`, not endpoints, and reach the `urls` export but **not** OpenAPI. A declared
  request target is what a URL list consumes; a path with no verb would have to invent one.
- They are **not** fed into the value-resolution map `--config` builds. That map is keyed by leaf
  property name and consumed bundle-wide as `_CONFIG.get(node.property.name)`, so one wrong entry
  silently rewrites unrelated URLs — the failure already recorded there, where a borrowed `url` put
  reddit's ActionCable socket at a Zendesk documentation page. Simulated over these blobs it would add
  246 keys, 25 of them ambiguous inside a single document. Reporting a value is safe; resolving other
  code with it is not.

### Three things checked here and found NOT to be gaps

Recorded because each would otherwise look like obvious work:

- **`<script type="application/json">` blobs are already read.** A/B over the real pipeline on all
  five corpora that contain them: 2,876 endpoints, endpoint sets byte-identical with and without the
  blobs. This includes Inertia — its client queries `script[data-page][type="application/json"]`, so
  laravel.com's 8,579-byte payload is already harvested (53 keys). A `<div data-page="{json}">`
  attribute occurs in **0 of 110** saved documents; the Inertia-in-an-attribute premise was wrong.
- **RSC flight payloads add no endpoints.** Zero across four Next App Router targets. A Flight
  deserialiser was written, measured and then deleted rather than shipped unused: the payload's
  strings sit in inline scripts, which are already harvested and parsed as JavaScript, so the whole
  channel was redundant.
- **`window.X = {...}` state is already recovered.** 23 of 23 non-asset relative-path leaves reach the
  output, including nuxt.com's `/openapi.json`, `/.well-known/api-catalog` and
  `/.well-known/mcp/server-card.json`. There is no "state in inline scripts is unread" gap.

## Both directions, after all of tonight's changes

The two checkers answer opposite questions and were run over the corpora again once everything above
was in:

- **`residual` — what did we MISS?** `API-ish literals NOT extracted: 0 (0.0%)` on all twelve corpora
  checked (Grafana, Clerk, PostHog, Linear, cal.com, Supabase, Amplitude, HubSpot, Loom ×2, Hex,
  Slack). Nothing that looks like an API path in any of those bundles goes unclaimed.
- **`precision` — what did we INVENT?** 4 questionable on Grafana (0.4% of 888), and 0 or 1 on every
  other corpus. All four are `?.get` calls whose receiver could not be resolved, which is the one
  remaining shape worth chasing: it is the same weakness that lets a base be attributed to the wrong
  client, and the fix for both is attribution by the receiver's binding rather than its name.

## Looked for a sibling class, found none

The sentinel above was caught by a rule about the VALUE's shape, which raises the obvious question:
what about a borrowed label that contains no whitespace? `"mixed"`, `"default"`, `"unknown"` would all
slip through.

Checked, by scanning every endpoint template in the 21 corpora for a literal segment sitting where an
identifier belongs — directly after `/uid/`, `/id/`, `/token/`, `/users/` and the like. The 18 most
common hits are all legitimate route words: `/account/settings`, `/org/preferences`, `/user/password`,
`/api/teams/search`, PostHog's real `@me` in `/api/users/@me/hedgehog_config`. The premise of the scan
is what is wrong — `/user/password` really is a path, not an id slot — and there is no second instance
of the borrowed-label class in this corpus set. Written down so the next person does not build a
denylist for a problem that is not there.

## One regex that was checked and left alone

The sweep that produced the three fixes above found 28 `.test(src)` call sites in the extractor. All
but a handful only *gate* a collector — a wrong answer costs a wasted parse, not a wrong claim — and
the two that set a field in the output are the tRPC marker and the routing label, both now structural.

One more looked like a third: `body_encoding` falls back to `/JSON\.stringify/` when the structural
read of a body expression comes back empty. It was left as a regex, because it is scoped to that one
expression's generated source rather than the chunk, and no input could be built where it differs
from a walk for the call. `body: "JSON.stringify(x)"` is already answered `raw` by the structural
read, so the fallback never runs; a comment mentioning `JSON.stringify` does not reach it either,
because the generator does not emit comments. Replacing it meant adding a recursive walker for a hole
that does not exist, so it was reverted — written down here so the next sweep does not re-litigate it.

## Where the zeros are honest

- **GraphQL and RPC apps have few distinct paths.** Twenty returns 28 REST endpoints and **110
  GraphQL operations**; Loom returns 62 and **141**; Railway returns 7 and **954** (401 queries,
  521 mutations, 32 subscriptions, 847 with typed variables). Run `graphql` on these, and read the endpoint
  count as the answer for the REST surface only. Notion dispatches everything through
  `/api/v3/<operationName>` computed at runtime and genuinely has almost no distinct paths.
- **Persisted queries can hide the document.** With Automatic Persisted Queries the client may send
  only a SHA-256 hash, and if the document is not in the bundle there is nothing to recover.
  Measured, this is rarer than it sounds: Twenty references APQ and `documentId` and still ships
  110 full operations, because the document is needed for the first request and the fallback. Check
  for `persistedQuery` / `documentId` in a corpus that returns suspiciously few operations.
- **Marketing sites ship little surface.** remix.run, angular.dev and astro.build each return one
  endpoint because that is what they call.
- **`modules` needs a registry.** Vite, Turbopack and plain ESM builds have none.
- **`chunks` needs a JS chunk table.** Angular and other esbuild builds declare chunks in the
  document instead; `fetch` collects them, `chunks` reports 0, and both are right.
- **`manifest` needs a Pages Router build.** On an App Router app it reports that the app is Next but
  serves no manifest; on a site that was never Next it says that instead. It does not guess at a
  stack that is not in evidence.

## Stacks measured 2026-10-06

Eight targets the repo had never been run against, each fetched, fingerprinted and analysed. Reported
as found, zeros included.

| target | stack as detected | scripts | endpoints |
|---|---|---|---|
| discuss.emberjs.com | esm / **ember** | 71 | 37 |
| deno.com | esm / **fresh** | 33 | **42** |
| antdv.com | webpack / — (vue listed, see below) | 137 | 18 |
| tanstack.com | **vite** / — | 211 | 14 |
| qwik.dev | esm / **qwik** | 23 | 4 |
| remix.run | esm / **remix** | 116 | 1 |
| astro.build | esm / **astro** | 13 | 1 |

deno.com is the interesting one: 15 of its 33 scripts were reached through `inline-import`, because
Fresh hydrates islands from inline module scripts and links no `<script src>` for them. Before the
fetcher followed inline imports, that corpus would have been three files.

remix.run and astro.build returning one endpoint each is the honest answer, not a failure — they are
documentation sites that call almost nothing. The low numbers belong next to the high ones.

**Two targets produced nothing, and neither is a gap in the tool.** fresh.deno.dev 404s — the site
moved — and shop.app answers **403** to a non-browser client, so its two scripts are a WAF page. A
zero that comes from the network is not a zero that comes from the analysis, and the first version of
this table recorded both as stack failures before the status codes were checked.

**Two detectors added from this round:**

- **Fresh** is named from the served document, which carries no attribute that says so. It delimits
  each island with a comment pair — `<!--frsh:island:HelloBar:1:-->` — and appends a build cache key
  to every asset (`/favicon.ico?__frsh_c=7e8f41a6...`). A comment is document structure, so this is
  the same kind of evidence as an element id.
- **Vue** is *listed and not crowned*, which is the honest result. `__VUE__` and `__vue_app__` are
  the devtools and DOM-element wire contracts, so they are definitive — and they live in the runtime
  chunk alone: 1 file of antdv.com's 137, under the 20% share a JS-only signal needs before it is
  named, because one file cannot tell "is Vue" from "bundles Vue". `data-v-app` is written during
  hydration and so is absent from the served document, the same way `ng-version` is for Angular.

  `__scopeId` looked like the signal that would carry it, appearing in 37 of those 137 files — but
  the compiled form is `_export_sfc(x, [["__scopeId", "data-v-e4e38630"]])`, so the name is a STRING
  LITERAL. Matching it would contradict the rule this repo's fingerprinting is built on, and the
  first test in `test_fingerprint.py` exists to hold that line. 37 was a text count, not a signal
  count.

## A second round, five more targets

| target | stack as detected | scripts | endpoints |
|---|---|---|---|
| laravel.com | **vite** / — | 114 | 22 |
| gatsbyjs.com | webpack / **gatsby** | 145 | 20 |
| nuxt.studio | vite / **nuxt** (js: nuxt, vue) | 145 | 4 |
| docusaurus.io | **rspack** / — | 406 | 2 |
| hydrogen.shop | esm / **remix** | 32 | 2 |

Three things this round established:

- **A vendored library does not become the framework.** laravel.com ships a chunk named
  `inertia-D-BlvnUF.js` and marketing copy naming Inertia, and is reported as `vite` with no
  framework and an empty JS framework list. That is the 20%-share rule doing its job — the same rule
  that stops Grafana being called an Angular app for bundling AngularJS.
- **Gatsby is now named**, from `<div id="___gatsby">`, an element id that appears exactly once in the
  document. It was previously reported as webpack with no framework.
- **Vue shows up where it should.** nuxt.studio lists `nuxt, vue` in `frameworks_in_js_only` with
  `nuxt` crowned from the document, which is the correct answer for a Nuxt app: Nuxt *is* Vue, and the
  document is what decides.

Docusaurus v3 is an Rspack build, which is worth knowing because Rspack was the least-exercised
bundler here before it.

## Not yet tested

Every bundler and framework claimed above was verified against a real deployed application or a real
local build, and the five built locally — Parcel, webpack Module Federation, Metro, esbuild and
Rollup — are committed as fixtures under `tests/fixtures/`, so the claims rerun rather than being
remembered.

What is genuinely outstanding, named rather than left implied:

- **Inertia.js** (Laravel/Rails server-driven SPAs). No reachable target was found — laravel.com looked
  like one by a text search and is not. Its `data-page` attribute carries the whole page's props as
  JSON in the document, which is reconnaissance-relevant on its own and nothing here reads yet.
- **Hotwire / Turbo with importmaps** (Rails). basecamp.com and hey.com were probed and carry no
  importmap or `turbo-frame` in the served document, so neither is a usable sample.
- **An authenticated surface.** Every number in this file comes from an anonymous fetch. The endpoints
  behind a login are the ones that matter most for reconnaissance and none of them are measured here.

  The route to one is `from-har`: export the session from DevTools or Burp and import it. That path is
  now verified end to end rather than asserted — a HAR carrying a document and two scripts round-trips
  into the endpoints they describe, including the recoveries that need cross-file work (a base held in
  a const and joined at the call site, an axios instance's `baseURL` applied to a relative call, and a
  request header). Only the writing half had a test before; the claim that mattered was that endpoints
  come back, and that is what is now covered. What is still missing is a real logged-in corpus to put
  numbers against, not a working path.

Fixtures for anything not listed are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).
