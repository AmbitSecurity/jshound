# jshound

Recover a single-page app's API from its JavaScript bundle — full requests, not just URLs.

An SPA ships its whole API surface to the browser, inside the bundle. jshound reads it out: for every
request the app is built to make, the **method**, the **path**, the **body fields** and their content
type, and the **headers**. It exports to OpenAPI, HAR or a URL list, so a proxy's site map fills with
requests you never had to click for.

It is not a grep. The URLs mostly are not in the file as strings:

```console
$ grep -c "settings.a1b2c3d4.js" examples/demo-app/app.4f2a9c1e.js
0
$ jshound chunks examples/demo-app/app.4f2a9c1e.js --quiet
static/chunks/settings.a1b2c3d4.js
static/chunks/billing.e5f6a7b8.js
static/chunks/admin.c9d0e1f2.js
```

Webpack ships a builder and calls it when the user navigates, so the string only exists at runtime:

```js
r.u = e => "static/chunks/" + ({12:"settings",34:"billing",56:"admin"}[e] || e)
         + "." + {12:"a1b2c3d4",34:"e5f6a7b8",56:"c9d0e1f2"}[e] + ".js"
```

Most of a code-split app's code — and so most of its API — is behind those URLs.

## How it works

1. **Fetch.** Loads the page and saves every script it references, then asks each bundle which lazy
   chunks it declares and fetches those too, until no new ones appear. A build's assets usually live
   on a CDN rather than on the site itself; it follows them there.
2. **Parse.** Every file goes through Babel into a syntax tree. **Nothing is executed.**
3. **Trace.** For each `fetch`, `axios`, `XMLHttpRequest` or `sendBeacon` call, it works outwards from
   the call site to the values — the method, the URL, the body's fields, the content type, the headers.
4. **Export.** OpenAPI, HAR, a URL list, or the records themselves.

Step 3 is what makes it accurate. A string scanner finds things that look like paths; this follows
the call, so it knows `/v1/media` is a POST carrying a file in a multipart body. And when a path is
assembled rather than written down — `${this.baseUrl}/orgs/${id}` — it indexes every property
assignment in the corpus first, so it can find the file that assigned `this.baseUrl` and follow it
there. When a piece genuinely will not resolve, the record says `{EXPR}` rather than guessing.

```console
$ jshound analyze examples/demo-app --host demo-app.test -f table
GET    /api/feature-flags
POST   /t/events                  {"sid":"?","evt":"?"}
POST   /v1/media                  {"file":"file-upload"}                multipart/form-data
DELETE /v1/sessions/current
POST   /v1/subscribe              {"email":"computed:trim","pageUri":"state:href"}  application/json
GET    /v1/users/{param_o}/profile
```

Three of those six are only reachable by following a base URL into a different file.

## Install

```bash
git clone https://github.com/AmbitSecurity/jshound && cd jshound
npm install                      # Babel, for parsing
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[fetch]"
```

Needs Python 3.9+ and Node 18+. The venv is not optional on Debian, Ubuntu, Fedora or Homebrew
Python, which refuse to install into the system environment (PEP 668).

## Usage

```bash
jshound fetch https://supabase.com/dashboard -o ./bodies
jshound analyze ./bodies -f table
```

One real application, end to end. Measured 2026-10-07: 216 scripts collected — 68 of them from
`frontend-assets.supabase.com`, where the build actually lives, and 145 lazy chunks the page never
loaded — giving **222 endpoints with a verb on every one** (100 POST, 80 GET, 18 DELETE, 13 PATCH,
8 PUT, 3 HEAD) and a body shape on 53 of them:

```
POST   /platform/auth/{ref}/magiclink              {"email":"state:email"}
POST   /platform/auth/{ref}/users/{id}             {"ban_duration":"enum?:computed-string|input:s"}
POST   /platform/projects/{ref}/content/folders    {"name":"input:t"}
POST   /platform/projects/{ref}/db-password        {"password":"input:t"}
GET    /platform/projects/{ref}/config/postgrest
DELETE /platform/projects/{ref}/functions/{slug}
```

Add `-f openapi -o spec.json` to import the lot into Burp or Caido, `-f har` for a site map, or
`-f urls` to pipe into nuclei or ffuf.

**Behind a login**, `fetch` will not get far, and that is where the surface that matters lives. Use a
browser: export the session as a HAR from DevTools or Burp, then

```bash
jshound from-har session.har -o ./bodies && jshound analyze ./bodies -f table
```

**If the build declares its own route table**, read that instead of inferring anything — a manifest
route is the build's own statement of what it routes, so the only way it is wrong is if the build is
wrong about itself. No fetching, about a second:

```console
$ jshound manifest https://supabase.com/dashboard
  next, declared by the build: 293 routes (103 api, 190 page) · 191 chunks · 2 rewrites
```

### Scope

Scope governs what you may **test**, not what you may **read**. A chunk named by the page's own
webpack runtime is that page's code wherever it is hosted — Supabase's is on
`frontend-assets.supabase.com`, Reddit's is on `redditstatic.com`, and neither is in anyone's scope
file. jshound fetches those scripts, reads them, and sends those hosts nothing else.

Two limits on that. It refuses any URL a target names in private address space — loopback, RFC1918,
link-local — unless the target you gave it is itself private, or you pass `--allow-private`. And
`analyze --host <h>` attributes every record to the host that actually served the file, so one bodies
directory shared by several targets does not blur into one.

## Commands

| | |
|---|---|
| `fetch <url> -o DIR` | download a page's JavaScript, following lazy chunks until no new ones appear |
| `analyze DIR` | read saved JavaScript and report the requests. **Makes no network requests.** |
| `from-har FILE -o DIR` | import a DevTools/Burp/Caido session — the way in for anything behind a login |
| `manifest <url>` | the route table the build itself declares (Next, Nuxt, SvelteKit, Remix, React Router) |
| `chunks FILE` | lazy-chunk URLs, read out of the bundler's own URL builder |
| `modules DIR FILE` | a webpack bundle as a readable module tree |
| `graphql DIR` | GraphQL operations the client ships, with typed variables |
| `sourcemaps DIR` | fetch any `.map` files and recover types and original parameter names |
| `fingerprint DIR` | what built the app, and what to expect from it |
| `residual DIR RECORDS` | what the analysis **missed**, measured against the corpus |
| `precision DIR RECORDS` | what it **invented** |

`analyze` takes a directory, not a file. Output formats: `-f table` (the requests, to read),
`-f openapi`, `-f har`, `-f urls` (a plain URL list), `-f json` (default, records as an array),
`-f jsonl` (one record per line).

## Why not a string scanner

Other JavaScript recon tools extract strings, and three things follow from that.

**They miss most of it.** The lazy-chunk URLs are not in the file, as above, and that is where most
of the app's code lives.

**They cannot give you the request.** A URL on its own does not say whether it is a GET or a DELETE,
or what the body holds, and that is the part you need in order to do anything with it.

**They are noisy.** Translation keys, CSS values, date formats, Map lookups and library placeholders
all look like paths to a regex. jshound rejects those structurally, and ships two commands that
measure its own output rather than asking you to trust it: `precision` reports what it invented,
`residual` what it missed, both against the corpus.

The markers are the other half of that. An unresolvable slice is `{EXPR}`, a partly-resolved body is
`body_partial`, an inferred content type is written `application/json?` with the `?`. The unmarked
records are worth trusting *because* the marked ones exist — a patch that raises the count by
promoting guesses to facts gets declined however good the number looks.

`bench/COMPARISON.md` and `bench/RESULTS.md` run jsluice and LinkFinder over the same corpora, with
the commands to reproduce.

## What it works on

Every row below was measured by running the tool against a real deployed app or a real local build,
on 2026-10-06. Numbers move as sites ship; what matters is which capabilities engage on which stack,
and where the zeros are honest.

### Bundlers

| bundler | lazy chunk URLs | module tree | how |
|---|---|---|---|
| webpack 5 | yes | yes | reads `__webpack_require__.u` out of the syntax tree |
| webpack 4 | yes | yes | no `.u` — matches the named URL builder by shape, not by name |
| Rspack | yes | yes | webpack-compatible, usually emitting ESM |
| Turbopack | yes | — | no builder at all; the chunk list is a literal array |
| Vite / Rollup | yes | n/a | paths are written down (`__vite__mapDeps`); flat ESM has no registry to walk |
| esbuild | yes | n/a | same — reports `esm (no bundler runtime marker)`, which is the right answer, not a detection failure |
| Parcel | yes | — | the runtime global is suffixed per build (`parcelRequire5707`) and the chunk table is an object, not an array |
| Metro (React Native) | n/a | — | ships one bundle, no chunks; detected by `__METRO_GLOBAL_PREFIX__`, since its `__d`/`__r` registry is too generic to match |
| webpack Module Federation | yes | yes | the builder carries no id map, so ids are taken from the `__webpack_require__.e(480)` call sites |

Module ids come in three flavours — numeric (`{134: fn}`), named (`{"src/vector/app.tsx": fn}`) and
deterministic (`{"++os": fn}`) — and the registry is recognised by *shape*, because a key-format test
rejected 848 real modules on GitLab.

### Route tables the build declares

Some frameworks hand over their route table outright instead of making you infer it, and
`jshound manifest` reads it. A declared route is a different kind of result from an extracted one: an
inferred endpoint can be wrong, while a manifest route is the build's own statement of what it
routes, so the only way it is wrong is if the build is wrong about itself.

| framework | where | what you get |
|---|---|---|
| Next, Pages Router | `_buildManifest.js`, linked from the document | every route with its parameters NAMED, per-route chunks, `__rewrites`, the prerendered set, middleware matchers |
| Next, App Router | — | nothing. An App Router build declares no route table |
| Nuxt | `builds/latest.json` → `builds/meta/<id>.json` | prerendered routes, `routeRules` patterns and their redirect/proxy targets |
| SvelteKit | the `dictionary` export of the client entry | every client-routed route, its matchers, and a `__data.json` endpoint each |
| Remix v2 | `window.__remixManifest`, inlined in the document | the route tree, which routes have a loader or take a submission, a `?_data=<id>` endpoint each |
| React Router v7 | `window.__reactRouterManifest`, in a linked `manifest-<hash>.js` | the same, with a `<path>.data` endpoint each |

Measured on 2026-10-06: `supabase.com/dashboard` 293 routes (103 of them `/api/*`), `svelte.dev` 24
routes plus 24 `__data.json` endpoints, `reactrouter.com` 13 routes and 8 data endpoints, `unjs.io` 82
prerendered plus 64 more named only by route rules. `nuxt.com` declared 900 prerendered routes until
it moved to rendering on demand; it now declares none, and that is reported as a manifest that was
read and came back empty — which is a different fact from a manifest that could not be read.

### Measured, per target

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

### Not tested yet

Named rather than left implied:

- **Inertia.js** (Laravel/Rails server-driven SPAs). No reachable target was found. Its `data-page`
  attribute carries a page's whole props as JSON in the document, which is worth reading and isn't
  read yet.
- **Hotwire / Turbo with importmaps** (Rails). basecamp.com and hey.com serve no importmap or
  `turbo-frame`, so neither is a usable sample.
- **An authenticated surface.** Every number above comes from an anonymous fetch, and the endpoints
  behind a login are the ones that matter most. `from-har` is the way in — export the session from
  DevTools or Burp and import it, a path that is verified end to end by a test — but there are no
  numbers here against a real logged-in corpus.

Fixtures for anything missing are the most useful contribution; see [CONTRIBUTING.md](CONTRIBUTING.md).

## What it does not do

- **It does not send requests to the API.** Everything here is what the client is *built* to call.
  Nothing is confirmed against a live server, so expect some endpoints to be gone or gated.
- **It does not run the JavaScript it reads.** Chunk URLs and framework route tables are recovered
  from the parsed syntax tree, so nothing a target ships is executed and a hostile bundle has no way
  to run code on the machine doing the analysis. `fetch` also refuses URLs a target names in private
  address space — loopback, RFC1918, link-local — unless the target you gave it is itself private, or
  you pass `--allow-private`.
- **It needs the JavaScript.** An app behind a login, behind a WAF, or one that injects its own
  scripts at runtime won't yield much to `fetch`. Use a browser and `from-har` instead.
- **GraphQL and RPC apps have few distinct paths**, because everything goes to one. Use `graphql`
  there and read the operation list instead of the endpoint count.
- **Source maps are not a route-discovery mechanism.** They add few endpoints. What they uniquely
  carry is types and original parameter names.
- **A Next App Router build declares no route table**, so `manifest` returns nothing for it. Pages
  Router, Nuxt, SvelteKit, Remix and React Router v7 all do.

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md). The short version: a change to extraction is measured by diffing
records, not by comparing totals — `bench/regress.sh` does that, and the reason it exists is in its
header.

## License

MIT.
