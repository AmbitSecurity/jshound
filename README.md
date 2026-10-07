# jshound

Recover a web app's API from the JavaScript it ships. Method, path, body fields, content type, headers.

```console
$ jshound supabase.com/dashboard
155 scripts -> 165 endpoints (766 records)

POST   /platform/auth/{ref}/magiclink                  {"email":"state:email"}
POST   /platform/auth/{ref}/otp                        {"phone":"state:phone"}
POST   /platform/projects/{ref}/db-password            {"password":"input:t"}
POST   /v1/branches/{branch_id_or_ref}                 {"branch_name":"input:t","git_branch":"input:n","persistent",...}
DELETE /v1/projects/{param_n}/vanity-subdomain
POST   /v2/projects/{ref}/advisors/run                 {"data":{"type":"string","attributes":{"lints":"computed:map"}}}
...
```

One hostname. No crawling, no clicking through the UI, no login. 64 GET, 64 POST, 15 DELETE,
13 PATCH, 8 PUT and 1 HEAD, measured 2026-10-07. 213 of the 218 files it collected came from
`frontend-assets.supabase.com`, which is where that build actually lives, and 145 of them were lazy
chunks the page never loaded.

## What's jshound?

A single page app ships its entire API client to the browser. Every request the app is built to make
is sitting in that bundle: the URL, the method, the fields it sends, the headers it attaches. jshound
reads it out and hands you the requests.

SPAs are how web applications get built now. The recon tooling for them was written when JavaScript
shipped as readable files with readable paths, and then bundlers ate the frontend. The tools that
pull strings out of JavaScript still work exactly as well as they ever did, which is to say they
reach a fraction of a modern app and tell you nothing about what to send to it.

There are two reasons a search through the bundle does not work, both of them from the app above.

**The URLs are built at runtime.** A code split app never writes its lazy chunk URLs down. Webpack
ships a function and calls it when the user navigates:

```js
r.u = e => "static/chunks/" + ({12:"settings",34:"billing"}[e] || e) + "." + {12:"a1b2c3d4"}[e] + ".js"
```

So the URL is nowhere in the file:

```console
$ grep -c "settings.a1b2c3d4.js" examples/demo-app/app.4f2a9c1e.js
0
$ jshound chunks examples/demo-app/app.4f2a9c1e.js --quiet
static/chunks/settings.a1b2c3d4.js
static/chunks/billing.e5f6a7b8.js
static/chunks/admin.c9d0e1f2.js
```

Most of a code split app's code lives behind those URLs, so most of its API does too. jshound reads
the builder out of the syntax tree and evaluates it, without running any of the target's code.

**The paths are assembled.** `${this.baseUrl}/orgs/${id}` is three values from three places, and the
file that assigns `this.baseUrl` is usually not the file that uses it. jshound indexes every property
assignment across the whole corpus first, then follows the link.

## Why jshound?

- **Full requests, not a URL list.** Method, path, body fields, content type, headers.
- **Follows lazy chunks** until no new ones appear, including the ones served from a CDN the page
  links but nobody put in scope.
- **Reads the route table** where the build declares one: Next, Nuxt, SvelteKit, Remix, React Router.
  A declared route is the build's own statement of what it routes, not a guess from a filename.
- **Exports to OpenAPI, HAR and URL lists.** Burp, Caido, nuclei and ffuf take the output directly.
- **Says what it could not resolve.** `{EXPR}` for an unresolved slice, `~` for an incomplete body, a
  trailing `?` for an inferred content type. The unmarked records are worth trusting because the
  marked ones exist.
- **Measures itself.** `precision` reports what it invented and `residual` what it missed, both
  against the corpus you just ran it on.
- **Never executes the target's code.** Everything is read from the parsed syntax tree.
- **Works behind a login**, by reading a HAR you exported from a browser or a proxy.

## Installing jshound

```bash
git clone https://github.com/AmbitSecurity/jshound && cd jshound
npm install
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[fetch]"
```

Needs Python 3.9 or newer and Node 18 or newer. The venv is not optional on Debian, Ubuntu, Fedora or
Homebrew Python, which refuse to install into the system environment.

## How to use jshound

One hostname does the whole thing: collect the JavaScript, follow the lazy chunks, read the requests.

```bash
jshound app.example.com
```

The JavaScript is kept under `.jshound/<host>`, because collecting it is the slow part and every
other command reads a directory of it. So the formats are a re-read, not a re-fetch:

```bash
jshound analyze .jshound/app.example.com -f openapi -o spec.json   # import into Burp or Caido
jshound analyze .jshound/app.example.com -f har                    # a site map
jshound analyze .jshound/app.example.com -f urls                   # pipe into nuclei or ffuf
```

Behind a login, fetching will not get far, and that is where the surface that matters lives. Drive a
browser, export the session as a HAR from DevTools or Burp, and read that instead:

```bash
jshound from-har session.har -o ./bodies && jshound analyze ./bodies -f table
```

If the build declares its own route table, read that. No fetching, about a second:

```console
$ jshound manifest https://supabase.com/dashboard
  next, declared by the build: 293 routes (103 api, 190 page) · 191 chunks · 2 rewrites
```

Offline, on the example app in this repo, which a test pins so it cannot go stale:

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

## Commands

| | |
|---|---|
| `jshound <host>` | the default. Fetch an app's JavaScript and read it, in one command |
| `fetch <url> -o DIR` | collect only, following lazy chunks until no new ones appear |
| `analyze DIR` | read saved JavaScript and report the requests. Makes no network requests |
| `from-har FILE -o DIR` | import a DevTools, Burp or Caido session. The way in behind a login |
| `manifest <url>` | the route table the build itself declares |
| `chunks FILE` | lazy chunk URLs, read out of the bundler's own URL builder |
| `modules DIR FILE` | a webpack bundle as a readable module tree |
| `graphql DIR` | GraphQL operations the client ships, with typed variables |
| `sourcemaps DIR` | fetch any `.map` files and recover types and original parameter names |
| `fingerprint DIR` | what built the app, and what to expect from it |
| `residual DIR RECORDS` | what the analysis missed, measured against the corpus |
| `precision DIR RECORDS` | what it invented |

Formats: `-f table` (the requests, to read), `-f openapi`, `-f har`, `-f urls`, `-f json` (records as
an array), `-f jsonl` (one record per line). `analyze` takes a directory, not a file.

## Scope

Scope governs what you may **test**, not what you may **read**. A chunk named by the page's own
webpack runtime is that page's code wherever it is hosted. Supabase's lives on
`frontend-assets.supabase.com`, Reddit's on `redditstatic.com`, and neither is in anyone's scope
file. jshound fetches those scripts, reads them, and sends those hosts nothing else.

Two limits on that. It refuses any URL a target names in private address space (loopback, RFC1918,
link local) unless the target you gave it is itself private, or you pass `--allow-private`. And
`analyze --host <h>` attributes every record to the host that actually served the file, so one
directory shared by several targets does not blur into one.

## What it works on

Every row below was measured by running the tool against a real deployed app or a real local build,
on 2026-10-06. Numbers move as sites ship; what matters is which capabilities engage on which stack,
and where the zeros are honest.

### Bundlers

| bundler | lazy chunk URLs | module tree | how |
|---|---|---|---|
| webpack 5 | yes | yes | reads `__webpack_require__.u` out of the syntax tree |
| webpack 4 | yes | yes | has no `.u`, so the named URL builder is matched by shape rather than by name |
| Rspack | yes | yes | webpack-compatible, usually emitting ESM |
| Turbopack | yes | n/a | no builder at all; the chunk list is a literal array |
| Vite / Rollup | yes | n/a | paths are written down (`__vite__mapDeps`); flat ESM has no registry to walk |
| esbuild | yes | n/a | same. Reports `esm (no bundler runtime marker)`, which is the right answer and not a detection failure |
| Parcel | yes | n/a | the runtime global is suffixed per build (`parcelRequire5707`) and the chunk table is an object, not an array |
| Metro (React Native) | n/a | n/a | ships one bundle, no chunks; detected by `__METRO_GLOBAL_PREFIX__`, since its `__d`/`__r` registry is too generic to match |
| webpack Module Federation | yes | yes | the builder carries no id map, so ids are taken from the `__webpack_require__.e(480)` call sites |

Module ids come in three flavours: numeric (`{134: fn}`), named (`{"src/vector/app.tsx": fn}`) and
deterministic (`{"++os": fn}`). The registry is recognised by *shape*, because a key-format test
rejected 848 real modules on GitLab.

### Route tables the build declares

Some frameworks hand over their route table outright instead of making you infer it, and
`jshound manifest` reads it. A declared route is a different kind of result from an extracted one: an
inferred endpoint can be wrong, while a manifest route is the build's own statement of what it
routes, so the only way it is wrong is if the build is wrong about itself.

| framework | where | what you get |
|---|---|---|
| Next, Pages Router | `_buildManifest.js`, linked from the document | every route with its parameters NAMED, per-route chunks, `__rewrites`, the prerendered set, middleware matchers |
| Next, App Router | n/a | nothing. An App Router build declares no route table |
| Nuxt | `builds/latest.json` → `builds/meta/<id>.json` | prerendered routes, `routeRules` patterns and their redirect/proxy targets |
| SvelteKit | the `dictionary` export of the client entry | every client-routed route, its matchers, and a `__data.json` endpoint each |
| Remix v2 | `window.__remixManifest`, inlined in the document | the route tree, which routes have a loader or take a submission, a `?_data=<id>` endpoint each |
| React Router v7 | `window.__reactRouterManifest`, in a linked `manifest-<hash>.js` | the same, with a `<path>.data` endpoint each |

Measured on 2026-10-06: `supabase.com/dashboard` 293 routes (103 of them `/api/*`), `svelte.dev` 24
routes plus 24 `__data.json` endpoints, `reactrouter.com` 13 routes and 8 data endpoints, `unjs.io` 82
prerendered plus 64 more named only by route rules. `nuxt.com` declared 900 prerendered routes until
it moved to rendering on demand; it now declares none, and that is reported as a manifest that was
read and came back empty, which is a different fact from a manifest that could not be read.

### Measured, per target

| target | bundler | framework | module ids | endpoints | w/ body | chunks | gql ops | modules |
|---|---|---|---|---|---:|---:|---:|---:|---:|
| Grafana | webpack 5 | n/a | mixed | 907 | 314 | 697 | 0 | 2 |
| GitLab | **webpack 4** | n/a | deterministic | 134 | 23 | **1272** | 10 | **648** |
| Loom | rspack | n/a | mixed | 62 | 13 | **2348** | **141** | 40 |
| Home Assistant | rspack | n/a | n/a | 35 | 5 | 1078 | 0 | **366** |
| tldraw | vite (+webpack via Clerk) | n/a | numeric | 74 | 30 | 171 | 0 | 1 |
| Excalidraw | vite | n/a | named (paths) | 5 | 2 | 142 | 0 | 1 |
| solidjs.com | vite | n/a | n/a | 3 | 1 | 1032 | 0 | 1 |
| Twenty | turbopack | next | mixed | 29 | 7 | 535 | **110** | 1 |
| Vercel | turbopack | next | mixed | 40 | 15 | 19 | 0 | 1 |
| Linear | vite | next | n/a | 30 | 12 | 121 | 4 | 1 |
| Element | webpack | n/a | named (paths) | 13 | 2 | 10 | 0 | 12 |
| nuxt.com | vite | nuxt | mixed | 9 | 3 | 87 | 0 | 1 |
| svelte.dev | vite | sveltekit | n/a | 3 | 1 | 9 | 0 | 1 |
| remix.run | esm (no runtime) | remix | n/a | 1 | 0 | 0 | 0 | 1 |
| angular.dev | esm (no runtime) | angular | named (paths) | 1 | 0 | 0 | 0 | 1 |
| astro.build | esm (no runtime) | astro | n/a | 1 | 0 | 0 | 0 | 1 |
| Discourse | esm (no runtime) | **ember** | n/a | 16 | 7 | 0 | 0 | 1 |
| qwik.dev | esm (no runtime) | **qwik** | n/a | 4 | 3 | 15 | 0 | 1 |
| material.angular.dev | esm (no runtime) | **angular** | n/a | 1 | 0 | 0 | 0 | 1 |
| emberjs.com | webpack | ember | n/a | 1 | 0 | 4 | 0 | 1 |
| Railway | vite | n/a | n/a | 7 | 1 | 201 | **954** | n/a |
| Clerk | rspack | next | n/a | 260 | 58 | 145 | 0 | n/a |
| Coda | webpack | n/a | n/a | 243 | 154 | 71 | 0 | n/a |
| hex.tech | rspack | n/a | n/a | 16 | 7 | 1254 | **187** | n/a |
| PostHog | esm (no runtime) | n/a | n/a | 151 | 26 | 321 | 0 | n/a |
| cal.com | turbopack | next | n/a | 60 | 12 | 18 | 0 | n/a |
| Supabase | turbopack | next | n/a | 48 | 35 | 53 | 0 | n/a |
| Sentry | webpack | n/a | n/a | 3 | 0 | 1724 | 0 | n/a |
| Retool | vite | n/a | n/a | 22 | 1 | 0 | 0 | n/a |
| Amplitude | vite | n/a | n/a | 56 | 38 | 25 | **106** | n/a |
| Notion | rspack | n/a | n/a | 15 | 8 | **2457** | 0 | n/a |
| Zapier | turbopack | next | n/a | 31 | 11 | 18 | 0 | n/a |
| Figma | rspack | n/a | n/a | 11 | 2 | 0 | 0 | n/a |
| Airtable | esm (no runtime) | n/a | n/a | 5 | 3 | 5 | 0 | n/a |
| Mixpanel | webpack | next | n/a | 6 | 5 | 58 | 2 | n/a |
| Dropbox | webpack | n/a | n/a | 109 | 8 | 418 | 0 | n/a |
| Webflow | webpack | n/a | n/a | 63 | 22 | 19 | 1 | n/a |
| HubSpot | webpack | n/a | n/a | 43 | 32 | 128 | 0 | n/a |
| Slack | rspack | n/a | n/a | 12 | 1 | 472 | 0 | n/a |
| *local build* | **parcel** | n/a | n/a | 3 | 2 | 1 | 0 | n/a |
| *local build* | **webpack MF** | n/a | named (paths) | 3 | 2 | 2 | 0 | n/a |
| *local build* | **metro** | n/a | n/a | 3 | 1 | 0 | 0 | n/a |
| *local build* | **esbuild** | n/a | n/a | 3 | 2 | 1 | 0 | n/a |
| *local build* | **rollup** | n/a | n/a | 3 | 2 | 1 | 0 | n/a |

### Not tested yet

Named rather than left implied:

- **Inertia.js** (Laravel/Rails server-driven SPAs). No reachable target was found. Its `data-page`
  attribute carries a page's whole props as JSON in the document, which is worth reading and isn't
  read yet.
- **Hotwire / Turbo with importmaps** (Rails). basecamp.com and hey.com serve no importmap or
  `turbo-frame`, so neither is a usable sample.
- **An authenticated surface.** Every number above comes from an anonymous fetch, and the endpoints
  behind a login are the ones that matter most. `from-har` is the way in: export the session from
  DevTools or Burp and import it, a path verified end to end by a test. What is missing is numbers
  against a real logged-in corpus, not a working path.

Fixtures for anything missing are the most useful contribution; see [CONTRIBUTING.md](CONTRIBUTING.md).

## What it does not do

- **It does not send requests to the API.** Everything here is what the client is *built* to call.
  Nothing is confirmed against a live server, so expect some endpoints to be gone or gated.
- **It does not run the JavaScript it reads.** Chunk URLs and framework route tables are recovered
  from the parsed syntax tree, so nothing a target ships is executed and a hostile bundle has no way
  to run code on the machine doing the analysis. `fetch` also refuses URLs a target names in private
  address space (loopback, RFC1918, link local) unless the target you gave it is itself private, or
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
records, not by comparing totals. `bench/regress.sh` does that, and the reason it exists is in its
header.

## License

MIT.
