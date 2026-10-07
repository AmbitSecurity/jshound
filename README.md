# jshound

Find a web app's API by reading the JavaScript it ships you.

Point it at a site. It downloads the app's JavaScript, parses it, and tells you the HTTP requests
the app is built to make — the method, the path, the body fields, the content type and the headers.
No crawling, no clicking through the UI, no logging in, and nothing sent to the target beyond
fetching the files a browser would fetch anyway.

```
POST   /v1/subscribe              body: email, pageUri
GET    /v1/users/{id}/profile
POST   /v1/media                  body: file          multipart/form-data
DELETE /v1/sessions/current
POST   /t/events                  body: sid, evt
```

Export it as OpenAPI and your proxy's site map fills with requests you never had to find.

## Install

```bash
git clone https://github.com/AmbitSecurity/jshound && cd jshound
npm install         # Babel, for parsing
pip install -e .
```

Python 3.9+ and Node 18+.

## Quick start

Two commands against a real site:

```bash
jshound fetch https://example.com -o ./bodies     # download its JavaScript
jshound analyze ./bodies -f urls                  # read it
```

Run against `vercel.com/login`, that takes about 75 seconds and returns 39 endpoints with the method
on every one — 25 POST, 11 GET, 3 DELETE — and body fields on 11 of them. Nothing was crawled and
nothing was logged into.

Offline, on the example app in this repo:

```bash
$ jshound analyze examples/demo-app --host demo-app.test -f urls
DELETE https://demo-app.test/v1/sessions/current
GET    https://demo-app.test/api/feature-flags
GET    https://demo-app.test/v1/users/{param_o}/profile
POST   https://demo-app.test/t/events
POST   https://demo-app.test/v1/media
POST   https://demo-app.test/v1/subscribe
```

Three of those six paths are only reachable by following a base URL into a different file, and the
file that defines it is not the file that uses it.

## Commands

| | |
|---|---|
| `fetch <url> -o DIR` | download a page's JavaScript, following lazy chunks until no new ones appear |
| `analyze DIR` | read saved JavaScript and report the requests. **Makes no network requests.** |
| `from-har FILE -o DIR` | import a DevTools/Burp/Caido session — the way in for anything behind a login |
| `manifest <url>` | the route table the build itself declares (Next, Nuxt, SvelteKit, Remix, React Router) |
| `chunks FILE` | lazy-chunk URLs, by running the bundler's own URL builder |
| `modules DIR FILE` | a webpack bundle as a readable module tree |
| `graphql DIR` | GraphQL operations the client ships, with typed variables |
| `sourcemaps DIR` | fetch any `.map` files and recover types and original parameter names |
| `fingerprint DIR` | what built the app, and what to expect from it |
| `residual DIR RECORDS` | what the analysis **missed**, measured against the corpus |
| `precision DIR RECORDS` | what it **invented** |

`analyze` takes a directory, not a file. Output formats: `-f openapi`, `-f har`, `-f urls`,
`-f jsonl` (default).

## Why not grep

Existing JavaScript scanners extract strings. That produces three problems, and all three get worse
the more modern the app is.

**They miss most of it.** A bundled app never writes its lazy-chunk URLs down. Webpack ships a
function and calls it when the user navigates:

```js
__webpack_require__.u = e => "static/chunks/" + e + "." + {94:"a1b2",96:"c3d4"}[e] + ".js"
```

`static/chunks/94.a1b2.js` is not in the file. It exists only once that function runs. Most of the
app's code — and so most of its API — is behind those URLs. jshound recovers the function and runs
it.

**They can't give you the request.** A URL on its own is a guess. You still don't know whether it's
a GET or a DELETE, or what the body looks like. jshound reports the method, the body fields and the
content type, because it reads the call, not the string.

**They're noisy.** A string that looks like a path usually isn't one. Translation keys, CSS values,
date formats, Map lookups and library placeholders all look like paths to a regex. jshound rejects
those structurally and ships two commands, `precision` and `residual`, that measure what it invented
and what it missed — on its own output.

On the 5-line example in this repo, against the two most-used extractors:

| | jshound | jsluice | LinkFinder |
|---|---|---|---|
| endpoints found | **6 / 6** | 3 / 6 | 3 / 6 |
| HTTP methods | **6** | 2 | 0 |
| body contracts | **3** | 0 | 0 |
| lazy chunk URLs | **3 / 3** | 0 | 0 |

jshound's column is asserted by `tests/test_fixture_claims.py`, so it cannot quietly stop being
true. The fixture itself was written by this repo, though, which makes it weak evidence either way —
`bench/COMPARISON.md` and `bench/RESULTS.md` run the same tools against real applications and give
the commands to reproduce it.

## Where values come from

When a path is assembled rather than written out, jshound follows it. `${this.baseUrl}admin/realms`
is opaque if you stop at `this.baseUrl` — but something, somewhere, assigned it, often in a different
file. Every property assignment in the corpus is indexed first, so the link can be followed.

When it genuinely cannot resolve something, it says so rather than guessing. An unresolvable piece
becomes `{EXPR}`, and a partly-resolved body is marked `body_partial` with a note saying the named
fields are real and the list is incomplete. Those markers are the reason the unmarked records are
worth trusting.

## What it does not do

- **It does not send requests to the API.** Everything here is what the client is *built* to call.
  Nothing is confirmed against a live server, so expect some endpoints to be gone or gated.
- **It needs the JavaScript.** An app behind a login, behind a WAF, or one that injects its own
  scripts at runtime won't yield much to `fetch`. Use a browser and `from-har` instead.
- **GraphQL and RPC apps have few distinct paths**, because everything goes to one. Use `graphql`
  there and read the operation list instead of the endpoint count.
- **Source maps are not a route-discovery mechanism.** They add few endpoints. What they uniquely
  carry is types and original parameter names.
- **A Next App Router build declares no route table**, so `manifest` returns nothing for it. Pages
  Router, Nuxt, SvelteKit, Remix and React Router v7 all do.

[SUPPORT.md](SUPPORT.md) lists every bundler and framework this has actually been run against, with
the numbers and the zeros.

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md). The short version: a change to extraction is measured by diffing
records, not by comparing totals — `bench/regress.sh` does that, and the reason it exists is in its
header.

## License

MIT.
