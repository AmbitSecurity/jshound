#!/usr/bin/env node
/* fingerprint.js, identify the bundler from the AST, not from text that happens to contain a name.
 *
 * A regex over a bundle cannot tell `__webpack_require__(42)` from the string "__webpack_require__"
 * sitting in an error message, a sourcemap comment, or a vendored copy of someone else's loader.
 * Measured while this tool was still grepping: Grafana was reported as a Next app because
 * `__NEXT_DATA__` appears once inside a string, and as an Angular app because `ng-version` appears
 * in chunks it vendors for plugin compatibility. Both readings were wrong and both were confident.
 *
 * So the signals here are SYNTAX:
 *   - a free identifier or binding actually named `__webpack_require__`, `parcelRequire`, …
 *   - a member on the global object: `window.__NUXT__`, `globalThis.__remixContext`
 *   - a chunk-array push: `(self.webpackChunkfoo = …).push(…)`
 *   - an `import()` specifier, which is a real module reference rather than a string that looks like one
 *
 * A name inside a StringLiteral is deliberately NOT a signal. That is the whole point.
 *
 *   node fingerprint.js file.js …      -> {"signals": {...}, "parsed": n, "failed": n}
 */
const { parse } = require("./babel.js").parser;
const traverse = require("./babel.js").traverse.default;
const fs = require("fs");

// identifier name -> signal it implies
// Object.create(null), NOT {}. A bundle is full of identifiers called `toString`, `constructor` and
// `hasOwnProperty`, and on a normal object literal `IDENT["toString"]` returns the inherited method
//, which is truthy, so every one of them was reported as a detected bundler. The first run of this
// file produced nine signals named things like "function valueOf() { [native code] }".
const IDENT = Object.assign(Object.create(null), {
  __webpack_require__: "webpack", __webpack_modules__: "webpack", webpackJsonp: "webpack",
  __webpack_exports__: "webpack", __webpack_public_path__: "webpack",
  __rspack_require__: "rspack", __rspack_esm_id: "rspack", __rspack_esm_ids: "rspack",
  __turbopack_require__: "turbopack", __turbopack_load__: "turbopack",
  __turbopack_context__: "turbopack",
  // Turbopack's chunk list is a global ARRAY, reached as a member: the emitted shape is
  // `(globalThis.TURBOPACK||(globalThis.TURBOPACK=[])).push([...])`, exactly parallel to
  // webpackChunk. Vercel's own site ships 144 of these and was detected as no bundler at all,
  // because the map only held the `__turbopack_*` identifier names.
  TURBOPACK: "turbopack",
  __vite__mapDeps: "vite", __vitePreload: "vite", __vite_legacy_guard: "vite",
  parcelRequire: "parcel",
  __toESM: "esbuild", __commonJS: "esbuild",
  // Metro (React Native, and RN Web). Its module registry is `__d(factory, id, deps)` and
  // `__r(id)`, one and two characters, far too generic to match on. These two are not:
  // every Metro bundle opens by assigning them.
  __METRO_GLOBAL_PREFIX__: "metro", __BUNDLE_START_TIME__: "metro",
  __NEXT_DATA__: "next", __next_f: "next",
  __NUXT__: "nuxt", __nuxt: "nuxt",
  __remixContext: "remix", __remixManifest: "remix", __remixRouteModules: "remix",
  __sveltekit: "sveltekit",
  _$HY: "solid",
  // Angular's compiler output. A client-rendered Angular app puts NOTHING in the served document,
  // `ng-version` is written during hydration, so the document rule alone never names it. These are
  // static class properties (`X.ɵfac = …`), i.e. real member names rather than text: material.angular.dev
  // carries ɵfac 219 times, ɵprov 102, ɵcmp 26.
  "\u0275fac": "angular", "\u0275prov": "angular", "\u0275cmp": "angular",
  "\u0275mod": "angular", "\u0275inj": "angular",
  // Vue has the same problem as Angular, `data-v-app` is written onto the mount element during
  // hydration, so a client-rendered Vue app puts nothing in the served document, but NOT the same
  // answer. These two are definitive, being the devtools and DOM-element wire contracts, and they
  // live in the runtime chunk alone: measured on antdv.com, 1 file of 137. That is below the 20%
  // share a JS-only signal needs before it is crowned, and rightly so, because one file cannot tell
  // "is Vue" from "bundles Vue". So Vue is LISTED in frameworks_in_js_only and not named as the
  // framework, which is the honest answer rather than a missing one.
  //
  // `__scopeId` looked like the signal that would carry it, Vue's SFC compiler emits it for every
  // component with scoped styles, and the text appears in 37 of those 137 files. It is not a signal:
  // the compiled form is `_export_sfc(x, [["__scopeId", "data-v-e4e38630"]])`, so the name is a
  // STRING LITERAL, not an identifier or a property. Matching it would contradict the first rule of
  // this file, which the first test in test_fingerprint.py exists to hold: a name inside a string is
  // not evidence that the thing is running.
  __VUE__: "vue", __vue_app__: "vue",
});
// prefix matches, for the names a bundler generates per-project
const IDENT_PREFIX = [
  [/^webpackChunk/, "webpack"], [/^rspackChunk/, "rspack"],
  [/^__sveltekit_/, "sveltekit"], [/^__turbopack/, "turbopack"],
  // Parcel suffixes its runtime global per build: `parcelRequire5707`, not `parcelRequire`.
  // An exact-name map detected a real Parcel build as no bundler at all.
  [/^parcelRequire/, "parcel"],
];

const files = process.argv.slice(2);
const signals = Object.create(null);
// Per-file results too, so a caller can ask WHICH file carried a signal without paying for one
// process per file, the first version of that cost a node spawn per script and took minutes on a
// 200-file corpus.
const perFile = Object.create(null);
let parsed = 0, failed = 0;

const hit = (name) => { signals[name] = (signals[name] || 0) + 1; };

const classify = (name) => {
  if (!name) return null;
  if (IDENT[name]) return IDENT[name];
  for (const [rx, s] of IDENT_PREFIX) if (rx.test(name)) return s;
  return null;
};

for (const f of files) {
  let ast;
  try {
    ast = parse(fs.readFileSync(f, "utf8"), {
      sourceType: "unambiguous", plugins: ["jsx", "typescript"], errorRecovery: true,
    });
  } catch (_) { failed++; continue; }
  parsed++;
  const seen = new Set();
  try {
    traverse(ast, {
      Identifier(p) {
        // The name must be used AS a name, not be the contents of a string, and not be a property
        // key like `{__NEXT_DATA__: 1}` which says nothing about what is running.
        if (p.parent && p.parent.type === "MemberExpression" && p.parent.property === p.node &&
            !p.parent.computed) {
          const s = classify(p.node.name);
          if (s) seen.add(s);                  // window.__NUXT__, a global read, which is real
          return;
        }
        if (p.parent && p.parent.type === "ObjectProperty" && p.parent.key === p.node) return;
        const s = classify(p.node.name);
        if (s) seen.add(s);
      },
      // import("./chunk-abc.js"), a module reference, not a string that resembles one
      CallExpression(p) {
        if (p.node.callee && p.node.callee.type === "Import") seen.add("esm-dynamic-import");
      },
      ImportDeclaration() { seen.add("esm-module"); },
      ExportNamedDeclaration() { seen.add("esm-module"); },
      ExportDefaultDeclaration() { seen.add("esm-module"); },
    });
  } catch (_) { /* a partial walk still contributes what it saw */ }
  for (const s of seen) hit(s);
  if (seen.size) perFile[f] = [...seen];
}

process.stdout.write(JSON.stringify({ signals, perFile, parsed, failed }));
