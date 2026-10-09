#!/usr/bin/env node
/* sveltekit.js, recover a SvelteKit app's route table from its client entry module.
 *
 *     node sveltekit.js app.Dy3uvAgJ.js
 *
 * SvelteKit's client router needs to know every route in order to route on the client, so the entry
 * module ships the table. In a production build it is minified to an alias:
 *
 *     const s = { "/": [2], "/(authed)/apps": [3], "/docs/[topic]/[...path=documentation]": [9] };
 *     export { s as dictionary, o as server_loads, i as matchers, a as nodes };
 *
 * so the local name is meaningless and only the EXPORT NAME is stable. This resolves the export alias
 * to its local binding and reads the object there, which is why it survives minification, there is no
 * identifier, path shape or object literal being matched on, just the module's own export contract.
 *
 * Nothing is evaluated: the table is a literal, so parsing it is enough. `server_loads` is an array of
 * node indices rather than routes, and the dictionary's values are each route's node indices, so which
 * routes run a server `load` is an intersection of the two.
 */
const fs = require("fs");
const parser = require("./babel.js").parser;
const traverse = require("./babel.js").traverse.default;

/* Route ids are filesystem-shaped, and the mapping to a URL is SvelteKit's own:
 *   (group)                     organisational, contributes no segment
 *   [slug]                      dynamic
 *   [[slug]]                    optional
 *   [...rest]                   rest
 *   [slug=matcher]              dynamic, constrained by a named matcher
 *   /docs/[topic]/[...path=documentation]  ->  /docs/{topic}/{path}
 */
function template(id) {
  const segs = [];
  for (const s of String(id).split("/")) {
    if (!s) continue;
    if (s.startsWith("(") && s.endsWith(")")) continue;
    if (s.startsWith("[")) {
      const inner = s.replace(/^\[+/, "").replace(/\]+$/, "").replace(/^\.\.\./, "");
      segs.push("{" + (inner.split("=")[0] || "param") + "}");
      continue;
    }
    segs.push(s);
  }
  return "/" + segs.join("/");
}

/* The matcher a constrained segment names, e.g. `documentation` in `[...path=documentation]`. It is a
 * function in the app's own code that decides whether a value is acceptable for that segment, so
 * knowing one exists tells you the segment is NOT free-form. */
function matchersOf(id) {
  const out = [];
  for (const s of String(id).split("/")) {
    const m = /^\[+\.{0,3}([^\]=]*)=([^\]]+)\]+$/.exec(s);
    if (m) out.push({ param: m[1], matcher: m[2] });
  }
  return out;
}

/* Node indices, flattened and unsigned. A dictionary value is not a flat list of numbers: real builds
 * hold `[5]`, `[-12]` and `[-7, [2]]`, nesting the layout indices and signing some of them. The sign
 * and the nesting encode something about layouts and error boundaries that is NOT documented as a
 * stable contract, so this reads the indices and deliberately draws no conclusion from how they were
 * written, abs() and a recursive walk give the same node set under every spelling seen. */
function nodeIndices(node) {
  const out = [];
  (function walk(n) {
    if (!n) return;
    if (n.type === "NumericLiteral") { out.push(Math.abs(n.value)); return; }
    if (n.type === "UnaryExpression" && n.operator === "-") return walk(n.argument);
    if (n.type === "ArrayExpression") { for (const e of n.elements) walk(e); }
  })(node);
  return [...new Set(out)].sort((a, b) => a - b);
}

/* THE EXPORT'S BINDING, RESOLVED THROUGH SCOPE, not by searching the module for the name.
 *
 * This is the whole correctness story of this file. A minified module reuses one-letter names in
 * different scopes, so looking up "the declarator called R" finds whichever came first in the file.
 * On shadcn-svelte that walked `dictionary = R` to an unrelated `L`, then to `N`, and landed on a
 * Svelte template fragment, a route table that was not a route table. Babel already knows which
 * declaration an identifier refers to; asking its scope is both shorter and the only answer that is
 * right under minification.
 *
 * `through` then follows the value a bounded number of hops, because builds wrap these exports:
 * directly (`= {...}`), through a helper call (`= t({...})`), and through another binding
 * (`= C(L)` where `L` holds the object).
 */
function through(scope, name, want, depth) {
  if (!name || depth > 8) return null;
  const b = scope.getBinding(name);
  if (!b || !b.path || !b.path.node) return null;
  const node = b.path.node;
  const init = node.type === "VariableDeclarator" ? node.init : null;
  if (!init) return null;
  if (init.type === want) return init;
  if (init.type === "Identifier") return through(b.path.scope, init.name, want, depth + 1);
  if (init.type === "CallExpression") {
    for (const arg of init.arguments || []) {
      if (!arg) continue;
      if (arg.type === want) return arg;
      if (arg.type === "Identifier") {
        const r = through(b.path.scope, arg.name, want, depth + 1);
        if (r) return r;
      }
    }
  }
  return null;
}

/* exported name -> the node it resolves to, for the shape asked for. Handles both
 * `export { s as dictionary }` and `export const dictionary = {...}`. */
function resolveExports(ast, wanted) {
  const out = new Map();
  traverse(ast, {
    ExportNamedDeclaration(p) {
      for (const sp of p.node.specifiers || []) {
        const ex = sp.exported && (sp.exported.name !== undefined ? sp.exported.name : sp.exported.value);
        const lo = sp.local && sp.local.name;
        if (!ex || !lo || !(ex in wanted) || out.has(ex)) continue;
        const n = through(p.scope, lo, wanted[ex], 0);
        if (n) out.set(ex, n);
      }
      const d = p.node.declaration;
      if (d && d.type === "VariableDeclaration") {
        for (const v of d.declarations || []) {
          const nm = v.id && v.id.name;
          if (!nm || !(nm in wanted) || out.has(nm)) continue;
          const n = through(p.scope, nm, wanted[nm], 0);
          if (n) out.set(nm, n);
        }
      }
    },
  });
  return out;
}

/* A route dictionary has route ids for keys. Checked before anything is reported, so a resolution
 * that went somewhere plausible but wrong produces "no dictionary" rather than invented routes,
 * the asymmetry that matters here is that a fabricated route costs more than a missed one. */
function looksLikeDictionary(obj) {
  const keys = (obj.properties || []).map(pr =>
    pr.key && (pr.key.value !== undefined ? pr.key.value : pr.key.name));
  const paths = keys.filter(k => typeof k === "string" && k.startsWith("/"));
  return keys.length > 0 && paths.length === keys.length;
}

function main() {
  const out = { routes: [], server_load_nodes: [], matchers: [], errors: [] };
  for (const file of process.argv.slice(2)) {
    let ast;
    try {
      ast = parser.parse(fs.readFileSync(file, "utf8"), { sourceType: "module", errorRecovery: true });
    } catch (e) {
      out.errors.push({ file, error: String((e && e.message) || e) });
      continue;
    }
    const found = resolveExports(ast, {
      dictionary: "ObjectExpression",
      matchers: "ObjectExpression",
      server_loads: "ArrayExpression",
    });

    const dict = found.get("dictionary");
    if (!dict) {
      out.errors.push({ file, error: "no dictionary export resolves to an object literal" });
      continue;
    }
    if (!looksLikeDictionary(dict)) {
      out.errors.push({ file, error: "the dictionary export resolved to an object that is not a "
                                     + "route table (no route-id keys)" });
      continue;
    }

    // Raw, and reported as such. `server_loads` is a list of NODE indices, and mapping those back to
    // routes means relying on the dictionary's sign/nesting encoding, which is not a documented
    // contract, so no per-route "runs a server load" claim is made from it here.
    for (const n of nodeIndices(found.get("server_loads"))) out.server_load_nodes.push(n);

    // `matchers` is an object whose KEYS are the matcher names the routes may reference.
    const mInit = found.get("matchers");
    if (mInit) {
      for (const pr of mInit.properties) {
        const k = pr.key && (pr.key.value !== undefined ? pr.key.value : pr.key.name);
        if (k) out.matchers.push(String(k));
      }
    }
    for (const pr of dict.properties) {
      const id = pr.key && (pr.key.value !== undefined ? pr.key.value : pr.key.name);
      if (typeof id !== "string" || !id.startsWith("/")) continue;
      out.routes.push({
        id,
        template: template(id),
        nodes: nodeIndices(pr.value),
        matchers: matchersOf(id),
      });
    }
  }
  out.routes.sort((a, b) => a.template.localeCompare(b.template));
  out.server_load_nodes = [...new Set(out.server_load_nodes)].sort((a, b) => a - b);
  out.matchers = [...new Set(out.matchers)].sort();
  process.stdout.write(JSON.stringify(out, null, 1));
}

main();
