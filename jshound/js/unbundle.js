#!/usr/bin/env node
/* unbundle.js, turn a shipped webpack bundle back into a module TREE an agent can read.
 *
 * A bundler splits code and then has to link it again, and it writes that linkage into the output: numeric
 * module ids, an export table (`r.d(exports,{Public:()=>local})`) and imports by id (`var o = r(56502)`). Nothing
 * about that is lossy. What minification destroyed is LOCAL names; what it kept, module boundaries, the import
 * graph, and every exported and property name, because those are the contract, is exactly the structure needed
 * to navigate the app instead of grepping 26 MB of one line.
 *
 * Recognise the module map by SHAPE (an object whose keys are numeric ids and whose values are functions of
 * (module, exports, require)), because webpack emits it two ways: `chunk.push([[ids],{…}])` for lazy chunks and
 * a bare object inside the runtime IIFE for the entry. Keying on `.push` finds one and misses the other.
 *
 *   node js/unbundle.js OUTDIR file.js [file2.js …]
 */
const { parse } = require("./babel.js").parser;
const traverse = require("./babel.js").traverse.default;
const generate = require("./babel.js").generator.default;
const fs = require("fs"), path = require("path");

const outdir = process.argv[2];
const inputs = process.argv.slice(3);
if (!outdir || !inputs.length) { console.error("usage: unbundle.js OUTDIR file.js…"); process.exit(1); }

// WHAT COUNTS AS A MODULE REGISTRY.
//
// Two things made this miss almost everything it was pointed at, both measured:
//
//   * It demanded EVERY property conform and returned false on the first that did not. A webpack
//     map holds hundreds of entries and one outlier, a getter, a non-function value, a four-arg
//     factory, threw the whole registry away. GitLab's 1.4 MB bundle yielded 3 modules.
//   * It demanded NUMERIC ids. Builds using `moduleIds: "named"`, and anything pnpm-resolved, key
//     the map by source path instead: Element ships
//     `"../../node_modules/.pnpm/matrix-js-sdk@43.0.0/.../logger.ts"` and came back with 0 imports
//     and 0 exports from 2.6 MB.
//
// So: a key is an id if it is numeric OR looks like a module path, and the map is a registry when
// most of it conforms rather than all of it. The proportion is what keeps this honest, an
// arbitrary object of functions does not reach 80% id-shaped keys, and the floor on count stops a
// two-property object from qualifying on a coincidence.
const NUMERIC_ID = /^\d{1,7}$/;
const PATH_ID = /[\/\\]|\.(js|mjs|cjs|jsx|ts|tsx|vue|svelte)$/i;

// THE SHAPE IS THE SIGNAL, NOT THE KEY FORMAT.
//
// webpack emits module ids in whatever `optimization.moduleIds` is set to, and this tool kept
// discovering a new one the hard way:
//
//   numeric        {134: function, 6710: function}            the default, and all this used to accept
//   path           {"../../node_modules/.pnpm/…/logger.ts": …} `named`, and anything pnpm-resolved
//   deterministic  {"++os": …, "+18S": …, "/23t": …}           4-char opaque hashes
//
// GitLab's 1.4 MB bundle is the third kind: 849 entries, every value a function, exactly one key
// numeric, so a key-format test rejected 848 real modules. Element is the second kind and returned
// 0 imports and 0 exports from 2.6 MB.
//
// What does not vary is the shape: a registry is an object whose values are nearly all small-arity
// functions, keyed by ids rather than by meaningful names. Uniformly short keys are the tell for the
// hashed case, a hand-written object of methods has names of varied, meaningful length, and the
// floor on property count is what stops a small object of callbacks qualifying by coincidence.
const isNamedId = (k) => NUMERIC_ID.test(k) || (k.length >= 4 && k.length <= 400 && PATH_ID.test(k));

const looksLikeModuleMap = (node) => {
  if (!node || node.type !== "ObjectExpression") return false;
  const props = node.properties || [];
  if (props.length < 2) return false;

  const keys = [];
  let fnVals = 0;
  for (const pr of props) {
    if (pr.type === "SpreadElement") return false;
    const k = pr.key && (pr.key.value ?? pr.key.name);
    if (k === undefined || k === null) return false;
    keys.push(String(k));
    const fn = pr.type === "ObjectMethod" ? pr : pr.value;
    if (fn && /Function|ObjectMethod/.test(fn.type) && (fn.params || []).length <= 3) fnVals++;
  }
  // Nearly every value must be a module factory.
  if (fnVals / props.length < 0.8) return false;

  const named = keys.filter(isNamedId).length;
  if (named / keys.length >= 0.8) return named >= 2;

  // Hashed ids: uniformly short, and enough of them that it is not an accident.
  const lens = new Set(keys.map((k) => k.length));
  const maxLen = Math.max(...keys.map((k) => k.length));
  return props.length >= 8 && lens.size <= 2 && maxLen <= 8;
};

const mods = new Map();          // id -> {code, exports:[], imports:[]}
const packed = new Set();        // inputs that really were webpack bundles
for (const file of inputs) {
  let ast;
  try { ast = parse(fs.readFileSync(file, "utf8"), { sourceType: "unambiguous", plugins: ["jsx", "typescript"], errorRecovery: true }); }
  catch { continue; }
  traverse(ast, {
    ObjectExpression(p) {
      if (!looksLikeModuleMap(p.node)) return;
      for (const prop of p.node.properties) {
        const id = String(prop.key.value ?? prop.key.name);
        if (mods.has(id)) continue;
        const fn = prop.type === "ObjectMethod" ? prop : prop.value;
        const req = (fn.params || [])[2];
        const reqName = req && req.type === "Identifier" ? req.name : null;
        const rec = { exports: [], imports: [], code: "" };
        try { rec.code = generate(fn.body, { compact: false, concise: false }).code; } catch {}
        if (reqName && fn.body) {
          try {
            traverse(fn.body, {
              noScope: true,
              CallExpression(q) {
                const c = q.node.callee;
                if (c && c.type === "MemberExpression" && c.object && c.object.name === reqName &&
                    c.property && c.property.name === "d" && q.node.arguments[1] &&
                    q.node.arguments[1].type === "ObjectExpression")
                  for (const e of q.node.arguments[1].properties)
                    if (e.key) rec.exports.push(String(e.key.name ?? e.key.value));
                if (c && c.type === "Identifier" && c.name === reqName) {
                  const a = q.node.arguments[0];
                  if (a && (a.type === "NumericLiteral" || a.type === "StringLiteral")) rec.imports.push(String(a.value));
                }
              },
            });
          } catch {}
        }
        rec.imports = [...new Set(rec.imports)];
        // An object of numeric keys mapping to functions is NOT enough. Vite ships lookup tables of exactly that
        // shape, and accepting them left `mods` non-empty on a Vite app, so the ESM path never ran and one API
        // produced a 2 MB tree from a 14 MB cache with zero endpoints in it. A webpack module is one whose body
        // USES the require it was handed, an export table or an import call. Behaviour, not shape.
        if (rec.exports.length || rec.imports.length) { mods.set(id, rec); packed.add(file); }
      }
      p.skip();
    },
  });
}

// NAME each module from its own content. Minification destroyed local identifiers but not string literals, and
// the strings are what carry meaning: a module holding "TWO_FACTOR_CREATE" and "/2fa" is the two-factor API
// whether or not its variables are called `n` and `s`. A tree of 3100 files called 15646.js is navigable only by
// grep; the same tree with 15646.two-factor.js can be browsed. Route paths beat action constants beat any other
// literal, because a path names a capability.
const nameOf = (code) => {
  const paths = [...code.matchAll(/["'`](\/[a-z][\w./-]{2,40})["'`]/g)].map((m) => m[1]);
  const consts = [...code.matchAll(/["'`]([A-Z][A-Z0-9_]{5,40})["'`]/g)].map((m) => m[1]);
  const pick = (arr) => {
    if (!arr.length) return null;
    const c = new Map();
    for (const x of arr) c.set(x, (c.get(x) || 0) + 1);
    return [...c.entries()].sort((a, b) => b[1] - a[1] || b[0].length - a[0].length)[0][0];
  };
  let base = pick(paths);
  if (base) base = base.split("/").filter(Boolean).slice(0, 2).join("-");
  if (!base) {
    const k = pick(consts);
    if (k) base = k.toLowerCase().replace(/_/g, "-").split("-").slice(0, 3).join("-");
  }
  if (!base) return null;
  base = base.replace(/[^a-z0-9.-]/gi, "-").replace(/-+/g, "-").replace(/^-|-$/g, "").slice(0, 40).toLowerCase();
  return base && /[a-z]/.test(base) ? base : null;
};

// ESM FALLBACK. Only webpack packs modules into a numeric-id map; Vite, rolldown and esbuild ship real ES
// modules, one chunk per file, and the import graph is written as `import{x}from"./other.js"`. For those the
// unbundling is already done, the work is just to read the graph off the imports and keep the same shape of
// output, so everything downstream (naming, reachability) does not care which bundler built the app.
// NOT either/or. A cache routinely holds a third-party bundle alongside the app's own chunks, one API ships
// Stripe's SDK, which is webpack-built, so `mods` came back non-empty and the ESM path never ran for the Vite
// chunks that are the actual application. Whether a FILE was packed is a per-file fact, so treat it that way and
// send everything else through the ESM reader.
{
  const loose = inputs.filter((f) => !packed.has(f));
  // Specifiers name the ORIGINAL chunk (`gcAIReply-BXt6mTEW.js`) while the cache stores files renumbered
  // (`0042.js`), so the graph has to go through the index recording which URL each file came from.
  const urlToFile = new Map();
  // url -> saved file. Two formats, and the FIRST of them has never existed: `_index.json` is read here
  // and in specs.py and is written by nothing, 0 of 23 workspaces on disk contain one. Wrapped in a
  // silent catch, so the capability has simply never worked and never said so. The crawler records the
  // same mapping in `_origin.jsonl` ({f: savedName, u: sourceUrl}), which is the file that actually
  // exists; the same coupling to a filename the writer stopped producing cost the whole extractor its
  // host attribution (see _document_map in analyze.py).
  const dir = path.dirname(loose[0] || inputs[0]);
  try {
    for (const line of fs.readFileSync(path.join(dir, "_origin.jsonl"), "utf8").split("\n")) {
      if (!line.trim()) continue;
      let r; try { r = JSON.parse(line); } catch { continue; }
      if (!r.f || !r.u) continue;
      const b = String(r.u).split("?")[0].split("#")[0].split("/").pop();
      if (b) urlToFile.set(b, path.basename(r.f));
    }
  } catch {}
  try {
    for (const f of (JSON.parse(fs.readFileSync(path.join(dir, "_index.json"), "utf8")).files || [])) {
      const b = String(f.url).split("?")[0].split("#")[0].split("/").pop();
      if (b) urlToFile.set(b, path.basename(f.path));
    }
  } catch {}
  const byFile = new Map();
  for (const file of loose) {
    let code = "";
    try { code = fs.readFileSync(file, "utf8"); } catch { continue; }
    if (code.length < 200) continue;
    const specs = [];
    for (const rx of [/\bfrom\s*["']([^"']{1,300})["']/g, /\bimport\s*\(\s*["']([^"']{1,300})["']/g,
                      /\bimport\s*["']([^"']{1,300})["']/g]) {
      for (const m of code.matchAll(rx)) specs.push(m[1]);
    }
    byFile.set(path.basename(file), { code, specs });
  }
  // a specifier resolves to whichever fetched file it names; chunk filenames are unique by construction
  const base = (sp) => sp.split("?")[0].split("#")[0].split("/").pop();
  const names = new Set(byFile.keys());
  for (const [f, v] of byFile) {
    const id = f.replace(/\.js$/, "");
    const imports = [...new Set(v.specs.map(base).map((b) => urlToFile.get(b) || b)
                                       .filter((b) => b && b !== f && names.has(b))
                                       .map((b) => b.replace(/\.js$/, "")))];
    // ESM exports are named in the module itself
    const ex = [];
    for (const m of v.code.matchAll(/\bexport\s*\{([^}]{1,400})\}/g))
      for (const part of m[1].split(","))
        ex.push((part.split(/\s+as\s+/).pop() || "").trim());
    if (!mods.has(id)) mods.set(id, { code: v.code, exports: ex.filter(Boolean).slice(0, 200), imports });
  }
}

fs.mkdirSync(path.join(outdir, "modules"), { recursive: true });
const index = [];
for (const [id, m] of mods) {
  const nm = nameOf(m.code);
  const fname = nm ? `${id}.${nm}.js` : `${id}.js`;
  // A MODULE ID CAN CONTAIN A PATH SEPARATOR, in both of the shapes that produce one: `named` ids
  // are source paths (`../../node_modules/.pnpm/…/logger.ts`) and `deterministic` ids are base64,
  // whose alphabet includes `/`, GitLab ships a module called `0/Qg`. Writing that straight to
  // disk threw ENOENT and killed the whole run after the modules before it had been written, so the
  // output looked merely small rather than aborted. Create the parent, and keep the structure: for
  // a path-keyed bundle it reconstructs the source tree, which is the point of this tool.
  // A path id routinely starts outside the build root, `../../node_modules/.pnpm/x@1/src/logger.ts`
  // is a normal webpack `named` id, and writing it verbatim would place files anywhere on disk the
  // bundle asked for. Dropping those was the first fix and it was wrong: it silently lost real
  // modules. Flatten the escape instead, so the module is recovered AND stays inside the output.
  const safeName = fname.replace(/^(\.\.[\/\\])+/, "").replace(/(^|[\/\\])\.\.(?=[\/\\]|$)/g, "$1__");
  const root = path.resolve(path.join(outdir, "modules"));
  const full = path.join(root, safeName);
  if (!path.resolve(full).startsWith(root)) continue;        // belt and braces
  fs.mkdirSync(path.dirname(full), { recursive: true });
  fs.writeFileSync(full, m.code);
  index.push({ id, file: safeName, name: nm, bytes: m.code.length,
               exports: m.exports, imports: m.imports });
}
// who imports whom, the thing that makes the tree navigable
const importers = {};
for (const m of index) for (const i of m.imports) (importers[i] ||= []).push(m.id);
for (const m of index) m.imported_by = importers[m.id] || [];
fs.writeFileSync(path.join(outdir, "index.json"), JSON.stringify(index, null, 1));
// A README so the tree explains itself to whoever opens it next. The named modules are, in practice, the
// FIRST-PARTY ones: vendor code (React internals, lodash, polyfills) carries no route paths and no domain
// constants, so the naming heuristic separates the app from its dependencies as a side effect.
const named = index.filter((m) => m.name).sort((a, b) => b.bytes - a.bytes);
const hubs = [...index].sort((a, b) => b.imported_by.length - a.imported_by.length).slice(0, 15);
fs.writeFileSync(path.join(outdir, "README.md"), [
  `# recovered module tree`, ``,
  `${index.length} modules, ${named.length} named from their own string constants.`,
  `Names come from route paths and SCREAMING_SNAKE constants: minification destroys local identifiers but`,
  `not string literals, and the literals are what carry meaning. Unnamed modules are almost all vendor code.`,
  ``, `## application modules (largest first)`, ``,
  ...named.slice(0, 60).map((m) => `- \`modules/${m.file}\`: ${m.bytes} bytes, ${m.exports.length} exports, imported by ${m.imported_by.length}`),
  ``, `## most-imported (the shared infrastructure)`, ``,
  ...hubs.map((m) => `- \`modules/${m.file}\`: imported by ${m.imported_by.length}`),
  ``, `## index.json`, ``,
  `Every module: id, file, name, bytes, exports, imports, imported_by. Follow \`imported_by\` to find`,
  `the callers of an API module; follow \`imports\` to find the client it goes through.`, ``,
].join("\n"));
console.log(JSON.stringify({ modules: index.length,
  with_exports: index.filter(m => m.exports.length).length,
  with_imports: index.filter(m => m.imports.length).length,
  bytes: index.reduce((a, m) => a + m.bytes, 0) }));
