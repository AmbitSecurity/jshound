# jshound

Recover a web app's API from the JavaScript it ships. Method, path, body fields, content type, headers.

![jshound mapping supabase.com/dashboard: 155 scripts in, 224 endpoints out, with the body contract on each](docs/demo.svg)

Takes a hostname, a list of them, or a scope file on stdin. No credentials and no session: the only
requests it makes are GETs for the JavaScript a browser downloads on page load, which are static
public assets off a CDN. Everything after that happens offline, against files on your disk.

## What's jshound?

Single page apps are one of the most common ways a web application gets built, and an SPA hands its
whole API client to the browser. Every request the app is built to make is in that bundle: the URL,
the method, the field names it sends, the content type, the headers it attaches. If you can read the
bundle properly you do not have to discover the API by clicking through the UI, because the client
already knows all of it.

Reading it properly is the hard part, and it is why extracting strings does not get you there.

### How it works

1. **Collect the whole bundle, not the entry point.** A code split app loads most of its code later,
   on navigation, and never writes those URLs down. Webpack ships a builder and calls it at
   `import()` time, so jshound reads the builder out of the syntax tree, evaluates it for every chunk
   id it can find, fetches the results, and repeats until a round turns up nothing new. On the app
   above that is 145 extra files, and the build lives on a CDN that nobody put in scope.

2. **Find the call sites, not the strings.** Every file is parsed with Babel, and what it looks for
   is the places a request is actually made: `fetch`, `axios`, `XMLHttpRequest.open`,
   `navigator.sendBeacon`, `EventSource`, `WebSocket`, `new URL(...)` passed into one of those, and
   the per-app request wrapper that most real codebases put in front of all of them. A sink has
   arguments; a string does not.

3. **Resolve each argument back to a value.** This is where the contract comes from. A path is
   usually assembled: `${this.baseUrl}/orgs/${id}/members` is three values from three places, and
   the file that assigns `this.baseUrl` is usually not the file that uses it. So every property
   assignment, every export and every const in the whole corpus is indexed in a pass that runs
   first, and then each argument is traced through it: parameters back to their call sites, members
   back to the object literal that defined them, re-exports back to the module that owns them.

4. **Assemble the request from the call, not from the URL.** The method comes from the options
   object, or from the wrapper's own default, or from the sink itself. The body's field names come
   from the object literal being serialised, with a note on where each value came from. The content
   type comes from a `FormData`, a `JSON.stringify`, or an explicit header, and says which. The
   headers come from the client's own defaults, so the `x-` headers an app requires travel with the
   endpoint that needs them.

5. **Mark anything that did not resolve.** An unresolved slice is written `{EXPR}`, an incomplete
   body list gets a `~`, an inferred content type gets a trailing `?`. The unmarked records are
   worth trusting because the marked ones exist, and `precision` and `residual` measure both
   directions against the corpus you just ran.

### What it recovers

| | |
|---|---|
| endpoints | a method and a path for each, with parameters named where the code names them |
| body contracts | the field names, where each value came from, and the content type |
| request headers | the client's own defaults, including the `x-` headers an app requires |
| query parameters | separately from the path, with their names |
| GraphQL operations | the document, the operation name, and the variables with their types |
| framework route tables | what the build itself declares, for Next, Nuxt, SvelteKit, Remix and React Router |
| client-side routes | the router's own table, which is the page surface rather than the API |
| server state | config, feature flags and URLs the server serialised into the document |
| cookies and auth | the names the client sets and reads, and the auth scheme per client |
| validators | `zod` schemas, which are a body contract written down by the developer |

## Installing jshound

```bash
git clone https://github.com/AmbitSecurity/jshound && cd jshound
npm install
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[fetch]"
```

Needs Python 3.9 or newer and Node 18 or newer. The venv is not optional on Debian, Ubuntu, Fedora or
Homebrew Python, which refuse to install into the system environment.

## Try it offline, in thirty seconds

There is a five line bundle in `examples/demo-app`. It is a webpack app that lazy loads three
chunks, and the URLs of those chunks are not in it:

```console
$ grep -c "settings.a1b2c3d4.js" examples/demo-app/app.4f2a9c1e.js
0
$ jshound chunks examples/demo-app/app.4f2a9c1e.js --quiet
static/chunks/settings.a1b2c3d4.js
static/chunks/billing.e5f6a7b8.js
static/chunks/admin.c9d0e1f2.js
```

The bundle ships the builder instead of the answer, and calls it when the user navigates:

```js
r.u = e => "static/chunks/" + ({12:"settings",34:"billing",56:"admin"}[e] || e)
         + "." + {12:"a1b2c3d4",34:"e5f6a7b8",56:"c9d0e1f2"}[e] + ".js"
```

Now read the requests out of the same app:

```console
$ jshound analyze examples/demo-app --host demo-app.test -f table
GET    /api/feature-flags
POST   /t/events                  {"sid":"?","evt":"?"}
POST   /v1/media                  {"file":"file-upload"}                multipart/form-data
DELETE /v1/sessions/current
POST   /v1/subscribe              {"email":"computed:trim","pageUri":"state:href"}  application/json
GET    /v1/users/{param_o}/profile
```

Six endpoints across four different sinks, with a verb on every one. Three of them are only
reachable by following a base URL into a different file, which is the thing a string scanner cannot
do. A test asserts this output, so it cannot quietly stop being true.

## Walk through a real app

```console
$ jshound supabase.com/dashboard
  chunks: 146 declared over 3 round(s), 145 fetched, 0 already had, 1 unreachable
  216 script(s) -> .jshound/supabase.com  [145 chunk-table, 68 script-src, 3 inline]
  13 runtime config value(s) from injected HTML (page, buildId, assetPrefix, viewport, googlebot…)
  62 duplicate body/bodies skipped (identical bytes under another name): 12fc3a97a2a2f5ffa083.js, 1f0f057531ebc61a541b.js, 41d4532fd8371e614d6d.js, 56697fad9a77afed5010.js, 579483ec2246013c6ff1.js (+57 more)
  parsing 155 scripts with 15 workers, 2.3MB batches
  302 original module name(s) mapped onto content-hashed bodies, so relative imports can resolve
  module index: 0 export(s), 3535 property value(s) across 155 files, independent of how they are batched
  34 body encoding(s) inferred from the sole request builder in the same file (labelled as inferred, not read)
  merged 511 duplicate call site(s) into their endpoints (per host, which the extractor cannot do)
  11 path literal(s) dropped: a call site elsewhere in the bundle already recovered their method
  9 request(s) had a body but no resolvable URL, recorded as request-no-url, not counted as endpoints
155 scripts -> 224 endpoints (2242 records)
  kept in .jshound/supabase.com, so `jshound analyze .jshound/supabase.com` re-reads it without fetching again
```

That is the whole log, which is the point: every line of it is a number you can check. The document
on `supabase.com` named four scripts and those named the rest, so **213 of the 217 files came from
`frontend-assets.supabase.com`**, which is where that build actually lives. **145 of them are lazy
chunks the page itself never loaded**, found by evaluating the chunk builder over three rounds until
a round turned up nothing new. Nothing was logged into, and the only requests were GETs for static
assets.

The lines in the middle are the analysis saying what it did and did not resolve: 34 content types
were inferred from the file's own request builder rather than read at the call site, and are marked
`application/json?` for it; 9 requests had a body but no URL anyone could resolve, so they are filed
as `request-no-url` and are not counted among the 224.

The corpus stays on disk, so changing your mind about the format is a re-read and not a re-fetch:

```bash
jshound analyze .jshound/supabase.com -f openapi -o spec.json   # import into Burp or Caido
jshound analyze .jshound/supabase.com -f har                    # a site map
jshound analyze .jshound/supabase.com -f urls                   # pipe into nuclei or ffuf
```

Ask what built it, and what to expect as a result:

```console
$ jshound fingerprint .jshound/supabase.com
  bundler: turbopack · framework: next (from the served document) · module ids: mixed · 215 of 217 files parsed
  209 file(s) of it served from frontend-assets.supabase.com, not from supabase.com
  ! 1 file(s) could not be parsed, so anything in them is missing rather than absent
```

A Next Pages Router build, a Nuxt build, SvelteKit, Remix and React Router all publish their own
route table. Read that instead of inferring anything, because it is the build's own statement of
what it routes. No fetching of assets, about a second:

```console
$ jshound manifest https://supabase.com/dashboard
  next, declared by the build: 293 routes (103 api, 190 page) · 191 chunks · 2 rewrites · 0 prerendered
```

## Several targets, or a scope file

```bash
jshound app.example.com api.example.com
cat scope.txt | jshound
```

Each host gets its own corpus under `.jshound/<host>`, and the records carry the host that actually
served the file, so a shared directory does not blur two targets into one.

## Behind a login

Fetching will not get far against an authenticated app, and that is where the surface that matters
lives. Drive a browser, export the session as a HAR from DevTools or Burp, and read that instead:

```bash
jshound from-har session.har -o ./bodies && jshound analyze ./bodies -f table
```

This is the same analysis over a better corpus. A HAR carries the responses the browser actually
received, so it reaches the code that only loads once you are signed in.

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
