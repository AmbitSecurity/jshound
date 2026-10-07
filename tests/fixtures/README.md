# Fixtures

One directory per bundler behaviour. Several are **real bundler output** — a tiny app built with the
actual tool — because every one of them broke something that a hand-written approximation would not
have:

| fixture | built with | what it caught |
|---|---|---|
| `parcel-build` | `parcel build` | runtime global is suffixed per build (`parcelRequire5707`); chunk table is an object, not an array |
| `webpack5-module-federation` | `webpack` + `ModuleFederationPlugin` | builder carries no id map at all — ids are in the `__webpack_require__.e(480)` call sites |
| `metro-rn` | `metro build --platform web` | registry is `__d`/`__r`, too generic to match; `__METRO_GLOBAL_PREFIX__` is not |
| `esbuild-esm` | `esbuild --bundle --splitting` | nothing — confirms `esm (no bundler runtime marker)` is the right answer |
| `rollup-esm` | `rollup -f es` | same |
| `webpack4-jsonp` | synthetic | webpack 4 has no `.u`; it builds URLs in a named function |
| `vite-literal-chunks` | synthetic | Vite writes chunk paths down rather than building them |
| `new-url-sinks` | synthetic | `new URL(path, base)` is the URL for every sink, not only `fetch` |

Synthetic fixtures are hand-written and minimal; real ones are committed build output. Never a real
target's JavaScript — that is someone else's code, it dates fast, and it makes the repo a
redistribution of other people's work.

See [../../CONTRIBUTING.md](../../CONTRIBUTING.md). What would help most now is a bundler or a build
configuration **not** in [../../SUPPORT.md](../../SUPPORT.md), or one that is listed and behaves
differently for you — a fixture that fails is a complete contribution.
