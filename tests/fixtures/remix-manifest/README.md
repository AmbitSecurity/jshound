# Remix and React Router v7 route manifests

`__remixManifest` was already used in this repo as a FINGERPRINT marker — proof the app is Remix — and
nothing read anything out of it. It declares the route tree, which routes have a loader, which accept a
submission, and every route's chunk URLs.

## The data protocol differs by version, and that was checked rather than assumed

| | global | protocol | verified |
|---|---|---|---|
| Remix v2 | `window.__remixManifest` | `<path>?_data=<route id>` | hydrogen.shop: `?_data=root` → 200, 6,272 B `text/remix-deferred`; an **invented id** → 403 |
| React Router v7 | `window.__reactRouterManifest` | `<path>.data` | reactrouter.com: `/home.data` → 200, 33,582 B; `/changelog.data` → 200, **801,465 B**; `/invented-route.data` → **404** |

**On v7, `?_data=root` answers 200 with `text/html`** — the ordinary page, carrying no data at all.
Emitting it would look like a hit and be worthless, which is why the protocol is keyed on which global
the manifest was assigned to.

The 403 and the 404 on invented ids/routes are what make these endpoints safe to emit: the ids come
from the manifest, so nothing is guessed. A route list alone would not be enough for v2, because the
route id and the URL path are different strings.

## Two shapes to parse

- **v2 inlines it** inside a `<script type="module">` that also has ESM `import` statements. Running
  the script in a vm throws `Cannot use import statement outside a module` before the assignment
  executes — the first version of this recovered nothing for exactly that reason. Only the assigned
  expression is evaluated now.
- **v7 links it** as `manifest-<hash>.js`. The manifest's own `url` field names the same file, so the
  convention is the framework's; the document is still where the hashed name has to be read, which is
  the lesson the Next reader already paid for with an `assetPrefix`.

## Pathless layout routes share a URL

`root`, `docs` and a splash index route all resolve to `/` on reactrouter.com. Routes are kept per id,
because the id is the provenance and v2's protocol needs it, but a data endpoint is a URL, so those
are deduplicated — that output had `/.data` three times before.
