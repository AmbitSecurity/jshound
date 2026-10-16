#!/usr/bin/env node
/* static_ast.js, proper AST extraction of network-sink call-sites from JS source.
 *
 * Walks a real JS AST (babel) rather than grepping for `/api/`, so it recovers the
 * CONTRACT, method + path-template + body/param key names, for EVERY call to a network sink, regardless
 * of whether the path contains "api". Handles minified bundles AND original sourcemap source (TS/JSX).
 *
 * The wall we CAN'T cross (by design, marked not guessed): when the URL or a body value is assembled from a
 * runtime variable we can't resolve, the variable slice becomes {EXPR}. That marker IS the signal to hand the
 * call to the dynamic engine, it is not a miss, it is a typed hand-off.
 *
 *   echo "<js source>" | node static_ast.js            # source on stdin
 *   node static_ast.js file1.js file2.js               # source from files
 *   -> JSON array of {method, url, template, body_keys, kind, sink, dynamic} on stdout
 *
 * Sinks recognized:
 *   fetch(url, {method, body})            new Request(url, {...})       navigator.sendBeacon(url, body)
 *   XMLHttpRequest .open(method, url)     WebSocket(url)                EventSource(url)
 *   axios(cfg) / axios.get|post|... (url) / axios({url,method,data})
 *   ky / got / superagent / $.ajax / $.get|post / apollo-ish .query|mutate (documents handled elsewhere)
 *   ANY <ident>.get|post|put|patch|delete|request(url,...) where url is a string/template that looks like a path
 */
const parser = require("./babel.js").parser;

// RUNTIME CONFIG injected into the HTML. A static bundle cannot know its deployment's base URL, so the
// server writes it into the page and the bundle reads it back, which is why `${k(c.baseUrl)}admin/...`
// resolves to {EXPR}: the value was never in any JavaScript file. analyze.py harvests those blobs from the
// saved HTML and passes them with --config. Keyed by leaf property name, because the receiver (`c`) is
// exactly what minification renames while the property (`baseUrl`) survives.
let _CONFIG = new Map();

// PROPERTY VALUES, indexed across every file in the batch.
//
// `${k(c.baseUrl)}admin/realms/...` is opaque only if you stop at `c`. `c` is an instance, instances get
// constructed, and somewhere in the bundle is the line that sets the property:
//     this.baseUrl = t.adminBaseUrl        and the page's config carries adminBaseUrl.
// Two links, both present in the shipped code, and following them turns an unresolved prefix on 474
// endpoints into the deployment's real base URL.
//
// So: index every assignment to a property NAME, `X.prop = expr` and `{prop: expr}`, resolve the
// right-hand side, and keep it. Reading `obj.prop` on an opaque receiver then becomes a lookup. Keyed by
// property name for the same reason as everything else here: the receiver is what minification renames,
// the property is the data contract and survives.
//
// Built in a PRE-PASS over the whole batch, because the assignment routinely lives in a different chunk
// from the read, Keycloak's does.
let _PROPVALS = new Map();
// Property names so common that the cross-file index cannot mean anything by them.
// `_id` IS `id`, AND IT IS THE ONE THAT MATTERS MOST. Every Mongo-backed application keys on `_id`, so
// it is the most generic property name such an app has, and it was missing from a list that already had
// `id`. Measured on one target: a React reducer's "addRow" case returns `{editRow:"NEW", editCopy:{_id:
// "NEW"}}`, a sentinel meaning "this record has no id yet". Whenever that chunk shared a node process with
// the API module, `dr.post(`/drafts/${e._id}/queue`)`, where `e` is an arrow-function parameter, a pure
// runtime value, resolved to `/drafts/NEW/queue` and was emitted as a RESOLVED, sendable address beside
// the correct `/drafts/{_id}/queue`. 37 such paths in one store: /accounts/NEW, /accounts/pause/NEW,
// /queue/NEW/draft, /schedules/NEW/queue.
//
// Reproduced in two runs of the extractor: the API module alone gives only the correct `{_id}` templates;
// the same file batched with the reducer gives both. That is what made it look fixed when the app's
// bundles were re-split, the phantom depends on which files land in one process, not on the code.
//
// `Id`, `uuid` and `slug` are here for the same reason: a value found under one of these names somewhere
// in a bundle says nothing about what it holds at an unrelated call site.
const _GENERIC_PROP = new Set(["url", "path", "host", "hostname", "origin", "uri",
                               "href", "id", "_id", "Id", "uuid", "slug",
                               "name", "type", "key", "value", "data"]);

// A BORROWED VALUE HAS TO BE ABLE TO BE A URL COMPONENT.
//
// `_GENERIC_PROP` above stops a value being borrowed under a name too common to mean anything. This
// stops a value being borrowed when the VALUE itself cannot be an address. Grafana defines its
// mixed-datasource sentinel as one object literal,
//
//     const L = { type: "mixed", uid: "-- Mixed --" };
//
// and the corpus-wide property index then filled every `${x.uid}` slot in the bundle with it,
// producing 20 endpoints like `/datasources/uid/-- Mixed --/health` where the real path is
// `/datasources/uid/{uid}/health`. One object literal, twenty fabricated addresses.
//
// A raw space cannot appear in a URL path or query, it has to be percent-encoded, so a borrowed
// value carrying whitespace is a label, not an address, and `{uid}` is the honest answer. Measured
// across the 21 corpora: 27 templates held whitespace in their skeleton and all 27 were this one
// sentinel, so there is no legitimate case to lose. Deliberately a test on the VALUE and not another
// name added to the set above: the next sentinel will have a different property name and the same
// shape.
const _BORROWABLE = (v) => typeof v === "string" && v !== "" && !/\s/.test(v);

// CROSS-MODULE RESOLUTION, for ESM.
//
// `import{ w as k }from"./o.js"` names its source file. That makes ESM strictly easier to follow than
// webpack, whose module ids are numbers, and unbundle.js, the one tool built for this, speaks only
// webpack. So the easy case was the unbuilt one.
//
// Two things are worth resolving across the boundary:
//   a string export , `export const API_BASE = "/api/v2"` used in a URL somewhere else. This is most of
//                      the ~500 API-ish path literals that have no attributed call site: the constant is
//                      declared in one module and consumed in another.
//   a URL transform , `export function w(e){ return e.endsWith("/") ? e.slice(0,-1) : e }`, Keycloak's
//                      trailing-slash strip. Reading its body turns the passthrough from an inference
//                      into a fact.
//
// Keyed by (resolved file path, exported name). Chunk order matches the argv file order, so the importing
// file is known and a relative specifier resolves against it.
let _EXPORTS = new Map();          // "path\u0000name" -> {str} | {transform:true}
let _CURFILE = null;
// THE tRPC VERDICT IS A PROPERTY OF THE CORPUS, NOT OF A BATCH. cal.com carries the marker in 1 of
// its 79 files, and asking each batch separately rejected the 42 real tRPC endpoints in every batch
// that did not happen to contain that file, the same "accident of scheduling" the module index
// exists to prevent. null means not yet decided; the index pass settles it for the whole corpus.
//
// Declared HERE, at module scope, and not beside the other driver state: the driver runs inside a
// top-level `try {` block, so a `let` declared there is invisible to `extract()` and every call threw
// a ReferenceError that the per-chunk catch turned into "this file has no endpoints", 1091 records
// became 12 and nothing said why.
let _IS_TRPC = null;
// Likewise a property of the corpus: the router is constructed in one chunk and the route table can
// be defined in others, so asking per chunk labels some of an app's routes and not the rest.
// "hash" | "path" | null, and null also means the corpus disagreed with itself.
let _ROUTING = null, _ROUTING_CONFLICT = false;

function _resolveSpec(fromFile, spec) {
  if (!fromFile || !spec || !spec.startsWith(".")) return null;   // bare specifier = npm package, skip
  const path = require("path");
  const base = path.dirname(fromFile);
  const cand = path.resolve(base, spec);
  for (const c of [cand, cand + ".js", cand + ".mjs", path.join(cand, "index.js")]) {
    if (_FILESET.has(c)) return c;
  }
  // Bundlers rewrite chunk names, so the saved file is often named by content hash and the specifier no
  // longer matches any path we hold. Fall back to the BASENAME, which survives more often than not.
  const bn = path.basename(cand).replace(/\.(js|mjs)$/, "");
  const hit = _BYBASE.get(bn);
  if (hit) return hit;
  // …and that basename fallback ALSO fails on a real corpus, because bodies are stored by CONTENT HASH:
  // the file is `8f91eb0c835fc110a8ff.js` and the specifier says `./app-shared-DKk5IE_B.js`, so neither
  // the path nor the basename can ever match. Measured on one target: 1,147 distinct relative specifiers, ZERO
  // resolvable. Cross-file import resolution has never worked on crawled input, only on fixtures whose
  // files kept their real names.
  //
  // The original name is not lost, it is in the URL the body was fetched from, which the crawler records.
  // analyze.py builds basename -> saved path from the origin map and passes it in. 450 of those 1,147
  // specifiers resolve through it.
  return _ALIAS.get(path.basename(cand)) || _ALIAS.get(path.basename(cand) + ".js") || null;
}

let _FILESET = new Set();
let _BYBASE = new Map();
let _ALIAS = new Map();          // ORIGINAL basename (from the fetch URL) -> the content-hashed saved path

function indexExports(ast, file) {
  const path_ = require("path");
  const key = (n) => file + "\u0000" + n;
  const strOf = (node) => {
    if (!node) return null;
    if (node.type === "StringLiteral") return node.value;
    if (node.type === "TemplateLiteral" && node.expressions.length === 0 && node.quasis.length === 1)
      return node.quasis[0].value.cooked ?? null;
    return null;
  };
  const isTransform = (fn) => {
    if (!fn || !/Function/.test(fn.type || "")) return false;
    if ((fn.params || []).length !== 1) return false;
    try {
      const gen = generateSrc(fn.body) || "";
      return !/["'`]\/[A-Za-z0-9_{$]/.test(gen);     // massages its input; does not build a new path
    } catch { return false; }
  };
  traverse(ast, {
    ExportNamedDeclaration(p) {
      try {
        const d = p.node.declaration;
        if (d && d.type === "VariableDeclaration") {
          for (const v of d.declarations || []) {
            if (!v.id || v.id.type !== "Identifier") continue;
            const sv = strOf(v.init);
            if (sv) _EXPORTS.set(key(v.id.name), { str: sv });
            else if (isTransform(v.init)) _EXPORTS.set(key(v.id.name), { transform: true });
          }
        } else if (d && d.type === "FunctionDeclaration" && d.id) {
          if (isTransform(d)) _EXPORTS.set(key(d.id.name), { transform: true });
        }
        // URL builders, exported by name or as a function declaration
        const named = d && d.type === "FunctionDeclaration" && d.id ? [[d.id.name, d.id]]
                    : d && d.type === "VariableDeclaration" ? (d.declarations || []).filter((v) => v.id && v.id.type === "Identifier").map((v) => [v.id.name, v.id])
                    : [];
        for (const sp of p.node.specifiers || [])
          if (sp.type === "ExportSpecifier" && sp.local && sp.exported) named.push([sp.exported.name ?? sp.exported.value, sp.local]);
        for (const [nm, node] of named) {
          const bld = _builderOf(node, p.scope);
          if (bld) _EXPORTS.set(key(nm), Object.assign({}, _EXPORTS.get(key(nm)) || {}, { builder: bld }));
        }
        for (const sp of p.node.specifiers || []) {
          if (sp.type !== "ExportSpecifier" || !sp.local || !sp.exported) continue;
          const b = p.scope.getBinding(sp.local.name);
          const init = b && b.path && b.path.node && b.path.node.init;
          const sv = strOf(init);
          const exp = sp.exported.name ?? sp.exported.value;
          if (sv) _EXPORTS.set(key(exp), { str: sv });
          else if (isTransform(init) || (b && b.path && isTransform(b.path.node)))
            _EXPORTS.set(key(exp), { transform: true });
        }
      } catch {}
    },
  });
}

// Pass one, bundled half: the URL-shaped strings each webpack or Turbopack module exports, so another
// module's `d.API_URL` reads the value module `d` exports rather than a guess by property name.
// A URL BUILDER: a function whose result is a URL made from its parameters, Grafana's
// `getAPIBaseURL = (group, version) => `/apis/${group}/${version}/namespaces/${ns()}``. Recorded as a
// template with the parameters as placeholders, so a call in another module fills them in.
function _builderOf(valueNode, scope) {
  let fn = valueNode, sc = scope;
  if (fn && fn.type === "Identifier") {
    const b = scope && scope.getBinding && scope.getBinding(fn.name);
    const d = b && b.path && b.path.node;
    fn = d && (d.type === "FunctionDeclaration" ? d : d.type === "VariableDeclarator" && d.init ? d.init : null);
    sc = b ? b.scope : scope;
  }
  if (!fn || !/Function/.test(fn.type || "") || !(fn.params || []).length) return null;
  const paramNames = fn.params.map((q) => (q.type === "Identifier" ? q.name : "\x00"));
  const ret = _returned(fn);
  if (!ret) return null;
  const t = resolveUrl(ret, sc, paramNames);
  const tm = t && t.template;
  if (!tm || !tm.includes("\x01") || !/^(?:\/|https?:\/\/)/.test(tm)) return null;
  if (!/\/[\w-]/.test(tm.replace(/\x01\w+\x01|\{[^}]*\}/g, ""))) return null;
  return { tmpl: tm, paramNames };
}
// A builder another module exports, from a callee: `(0,n.TP)(...)`, or `b(...)` with `b` imported.
function _exportedBuilder(callee, scope) {
  let c = callee;
  if (c && c.type === "SequenceExpression") c = c.expressions[c.expressions.length - 1];
  if (!c) return null;
  if (c.type === "Identifier") { const iv = importedValue(c.name, scope); return (iv && iv.builder) || null; }
  if ((c.type === "MemberExpression" || c.type === "OptionalMemberExpression") && !c.computed && c.property) {
    const mid = _moduleOf(c.object, scope);
    if (mid != null) { const ex = _modExport(mid, c.property.name ?? c.property.value); return (ex && ex.builder) || null; }
  }
  return null;
}

function indexBundledExports(ast) {
  for (const ex of _bundledExports(ast)) {
    const k = "mod:" + ex.id + "\u0000" + ex.name;
    if (!ex.value) continue;
    try {
      const bld = _builderOf(ex.value, ex.path.scope);
      if (bld) _EXPORTS.set(k, Object.assign({}, _EXPORTS.get(k) || {}, { builder: bld }));
    } catch {}
    if (_EXPORTS.has(k) && _EXPORTS.get(k).str) continue;
    try {
      const r = strFromNode(ex.value, ex.path.scope, 0, null);
      const v = r && !r.dyn && typeof r.s === "string" ? r.s : null;
      if (v && v.length <= 2000 && (/^(?:https?:)?\/\/|^\//.test(v) || LOOKS_PATH(v))) _EXPORTS.set(k, { str: v });
    } catch {}
  }
}

// Pass two of the index: which exports are API clients, or getters that hand one back, with the base
// each one is built with. Needs pass one loaded, because the base is routinely another module's export.
function _describeExport(valueNode, scope, wrappersByFn) {
  const out = {};
  if (!valueNode) return out;
  let fn = null, fnScope = null;
  if (valueNode.type === "Identifier") {
    const b = scope && scope.getBinding && scope.getBinding(valueNode.name);
    if (b && b.path && b.path.node) {
      if (b.path.node.type === "FunctionDeclaration") { fn = b.path.node; fnScope = b.path.scope; }
      else if (b.path.node.type === "VariableDeclarator" && b.path.node.init && /Function/.test(b.path.node.init.type)) {
        fn = b.path.node.init; fnScope = b.path.get("init").scope;
      }
    }
  }
  if (fn) {
    const r = _returned(fn);
    const c = r && fnScope ? clientOf(r, fnScope, 1) : null;
    if (c) out.returns = c;
    const w = wrappersByFn && wrappersByFn.get(fn);
    if (w && !w.isMethod) out.wrapper = { tmpl: w.tmpl, paramNames: w.paramNames, method: w.method || null,
                                          body_idx: w.body_idx, body_keys: w.body_keys || null, viaFetch: !!w.viaFetch };
    if (!out.wrapper && r && fnScope && (r.type === "CallExpression" || r.type === "AwaitExpression")) {
      const cc = callClient((r.argument || r).callee, fnScope);
      if (cc) out.client = cc;
    }
  } else {
    const c = clientOf(valueNode, scope, 0);
    if (c) out.client = c;
  }
  return out;
}
function indexClientExports(ast, file) {
  const touched = [];
  const WR = collectWrappers(ast);
  const byFn = new Map();
  for (const ws of Object.values(WR)) for (const w of ws) byFn.set(w.fnNode, w);
  const put = (k, desc) => {
    if (!desc.client && !desc.returns && !desc.wrapper) return;
    const v = Object.assign({}, _EXPORTS.get(k) || {}, desc);
    _EXPORTS.set(k, v);
    touched.push([k, v]);
  };
  traverse(ast, {
    ExportNamedDeclaration(p) {
      if (!file) return;
      try {
        const d = p.node.declaration;
        if (d && d.type === "VariableDeclaration") {
          for (const v of d.declarations || []) if (v.id && v.id.type === "Identifier")
            put(file + "\u0000" + v.id.name, _describeExport(v.id, p.scope, byFn));
        } else if (d && d.type === "FunctionDeclaration" && d.id) {
          put(file + "\u0000" + d.id.name, _describeExport(d.id, p.scope, byFn));
        }
        for (const sp of p.node.specifiers || []) {
          if (sp.type !== "ExportSpecifier" || !sp.local || !sp.exported) continue;
          put(file + "\u0000" + (sp.exported.name ?? sp.exported.value), _describeExport(sp.local, p.scope, byFn));
        }
      } catch {}
    },
  });
  for (const ex of _bundledExports(ast)) {
    try { put("mod:" + ex.id + "\u0000" + ex.name, _describeExport(ex.value, ex.path.scope, byFn)); } catch {}
  }
  return touched;
}

// What an imported identifier resolves to, if anything.
function importedValue(name, scope) {
  const b = scope && scope.getBinding && scope.getBinding(name);
  if (!b || !b.path || !b.path.parentPath) return null;
  const decl = b.path.parentPath.node;
  if (!decl || decl.type !== "ImportDeclaration" || !decl.source) return null;
  const file = _resolveSpec(_CURFILE, decl.source.value);
  if (!file) return null;
  const imported = b.path.node.imported
    ? (b.path.node.imported.name ?? b.path.node.imported.value)
    : name;
  return _EXPORTS.get(file + "\u0000" + imported) || null;
}

// ── BUNDLED MODULES: WHO EXPORTS WHAT ───────────────────────────────────────────────────────────────
// A bundle links its modules by id, and writes the links down:
//     webpack, rspack   {5:(e,t,n)=>{ n.d(t,{Z:()=>o}); var r=n(9) }}         exports by n.d, imports by n(id)
//     Turbopack         [202, e=>{ var a=e.i(303); e.s(["post",0,v],202) }]     exports by e.s, imports by e.i
// Minification renames locals, never export names: those are the contract between modules. So
// `(0,A.post)` with `A=e.i(202)` names exactly one export of exactly one module, and its value is read
// from that module. Keyed "mod:<id>\0<name>" in _EXPORTS, beside the file-keyed ES module exports.
function _requiredModule(init, scope) {
  if (!init || init.type !== "CallExpression" || (init.arguments || []).length !== 1) return null;
  const a = init.arguments[0];
  const id = a.type === "NumericLiteral" ? String(a.value)
           : a.type === "StringLiteral" && a.value.length <= 300 ? a.value : null;
  if (id == null) return null;
  const c = init.callee;
  if (c.type === "MemberExpression" && !c.computed && c.object.type === "Identifier" && c.property &&
      (c.property.name === "i" || c.property.name === "r")) return id;           // Turbopack
  if (c.type === "Identifier") {
    if (c.name === "__webpack_require__") return id;
    // webpack's require is the module factory's third parameter
    const b = scope && scope.getBinding && scope.getBinding(c.name);
    const fn = b && b.kind === "param" && b.path.parentPath && b.path.parentPath.node;
    if (fn && fn.params && fn.params.indexOf(b.path.node) === 2) return id;
  }
  return null;
}
// The module a namespace identifier stands for: `A` in `A.post`, given `var A = e.i(202)`.
function _moduleOf(node, scope) {
  if (!node || node.type !== "Identifier") return null;
  const b = scope && scope.getBinding && scope.getBinding(node.name);
  const d = b && b.path && b.path.node;
  if (!d || d.type !== "VariableDeclarator" || !d.id || d.id.type !== "Identifier") return null;
  return _requiredModule(d.init, b.scope);
}
function _modExport(id, name) { return _EXPORTS.get("mod:" + id + "\u0000" + name) || null; }
// The id of the bundled module a node sits in: the key of its factory in the module map (webpack), or the
// id written before the factory in the chunk's array (Turbopack).
function _factoryId(path) {
  for (let p = path.getFunctionParent && path.getFunctionParent(); p; p = p.getFunctionParent && p.getFunctionParent()) {
    const par = p.parentPath && p.parentPath.node;
    if (p.node.type === "ObjectMethod" && p.node.key) {
      const k = p.node.key.value ?? p.node.key.name;
      if (k != null && /^[\w./@~+-]{1,300}$/.test(String(k))) return String(k);
    }
    if (par && par.type === "ObjectProperty" && par.value === p.node && par.key) {
      const k = par.key.value ?? par.key.name;
      if (k != null && (par.key.type === "NumericLiteral" || par.key.type === "StringLiteral" || /^\d+$/.test(String(k))))
        return String(k);
    }
    if (par && par.type === "ArrayExpression") {
      const i = par.elements.indexOf(p.node);
      const prev = i > 0 ? par.elements[i - 1] : null;
      if (prev && (prev.type === "NumericLiteral" || prev.type === "StringLiteral")) return String(prev.value);
    }
  }
  return null;
}
// [{id, name, value, path}] for every export a bundler's runtime declares in this file.
function _bundledExports(ast) {
  const out = [];
  traverse(ast, { CallExpression(p) {
    try {
      const n = p.node, c = n.callee;
      if (c.type !== "MemberExpression" || c.computed || !c.property) return;
      // Turbopack: e.s(["name", 0, value, ...], id)
      if (c.property.name === "s" && n.arguments[0] && n.arguments[0].type === "ArrayExpression") {
        const a1 = n.arguments[1];
        const id = a1 && (a1.type === "NumericLiteral" || a1.type === "StringLiteral") ? String(a1.value) : _factoryId(p);
        if (id == null) return;
        const el = n.arguments[0].elements;
        for (let i = 0; i < el.length; i++) {
          if (!el[i] || el[i].type !== "StringLiteral") continue;
          if (el[i + 1] && el[i + 1].type === "NumericLiteral" && el[i + 2]) {
            out.push({ id, name: el[i].value, value: el[i + 2], path: p }); i += 2;
          } else if (el[i + 1] && /Function/.test(el[i + 1].type)) {
            out.push({ id, name: el[i].value, value: _returned(el[i + 1]), path: p }); i += 1;
          }
        }
        return;
      }
      // webpack: n.d(t, {name: () => local}), and the newer n.d(t, {getters}, {name: value}), whose third
      // argument holds the exported values themselves. Grafana exports `getAPIBaseURL` that way.
      if (c.property.name === "d" && (n.arguments.length === 2 || n.arguments.length === 3) &&
          n.arguments[1].type === "ObjectExpression") {
        const id = _factoryId(p);
        if (id == null) return;
        for (const pr of n.arguments[1].properties) {
          if (!pr.key) continue;
          const name = pr.key.name ?? pr.key.value;
          const v = pr.type === "ObjectMethod" ? _returned(pr) : (pr.value && /Function/.test(pr.value.type) ? _returned(pr.value) : null);
          if (name != null && v) out.push({ id, name: String(name), value: v, path: p });
        }
        const vals = n.arguments[2];
        if (vals && vals.type === "ObjectExpression") for (const pr of vals.properties) {
          if (pr.type !== "ObjectProperty" || !pr.key || !pr.value) continue;
          const name = pr.key.name ?? pr.key.value;
          if (name != null) out.push({ id, name: String(name), value: pr.value, path: p });
        }
      }
    } catch {}
  } });
  return out;
}
// What a function returns, when that is one expression: `() => x`, `function(){return x}`.
function _returned(fn) {
  if (!fn) return null;
  if (fn.body && fn.body.type !== "BlockStatement") return fn.body;
  const rs = ((fn.body && fn.body.body) || []).filter((st) => st.type === "ReturnStatement" && st.argument);
  return rs.length === 1 ? rs[0].argument : null;
}

// ── CLIENT BASES, BY BINDING ────────────────────────────────────────────────────────────────────────
// A request made through an API client goes to the client's base plus the call's path, so the base is a
// value like any other and is found the way any other value is: follow the receiver through its bindings,
// through destructuring (`let {POST: v} = h`), through a getter (`cs()` returning the instance), through
// imports and module exports, to the call that built the client, and read the base out of its options.
//
// This replaced a lookup of the receiver's NAME among clients built in the same file, which only trusted
// an absolute origin because a minified name collides across modules. A binding does not collide, so a
// relative base (`/api/v2`) is as certain as an absolute one. Measured before: 6,170 of the benchmark's
// 13,003 requests had no host, among them every one of supabase's, whose base is three bindings and two
// module exports away from the call.
const _BASE_KEY = /^(?:api|root|service|server)?_?base_?(?:url|uri|path|address|host)?$|^prefix_?url$|^(?:api|root|service|server)?_?(?:base)?_?(?:url|uri|host|hostname|origin|endpoint|server|base)$/i;
const _EXPLICIT_BASE_KEY = /^(?:api|root|service|server)?_?base_?(?:url|uri|path|address|host)?$|^prefix_?url$/i;
// The base a client-building call is configured with, read from its options object: {base, dyn} or null.
function _baseOfCreate(call, scope) {
  if (!call || (call.type !== "CallExpression" && call.type !== "NewExpression")) return null;
  const objs = [];
  const walk = (n, d) => {
    if (!n || d > 3 || objs.length > 4) return;
    if (n.type === "ObjectExpression") { objs.push(n); return; }
    if (n.type === "CallExpression" || n.type === "NewExpression") for (const a of n.arguments || []) walk(a, d + 1);
  };
  walk(call, 0);
  const hits = [];
  for (const o of objs) for (const pr of o.properties) {
    if (pr.type !== "ObjectProperty" || !pr.key || !pr.value) continue;
    const key = String(pr.key.name ?? pr.key.value ?? "");
    // RTK Query: `createApi({baseQuery: fetchBaseQuery({baseUrl})})`, the base is the base query's
    if (key === "baseQuery") {
      const q = clientOf(pr.value, scope, 1);
      if (q) hits.push({ key: "baseUrl", base: q.base, dyn: q.dyn });
      continue;
    }
    if (!_BASE_KEY.test(key)) continue;
    // A key that only MIGHT be a base (`url`, `uri`, `endpoint`, `host`) counts with an absolute origin
    // and nothing else: axios's `{url: "/users"}` is the default request, and reading it as a base made
    // `/users/orders`. A key that says base (`baseURL`, `basePath`, `prefixUrl`) is one whatever its value.
    const explicit = _EXPLICIT_BASE_KEY.test(key);
    const r = strFromNode(pr.value, scope, 0, null);
    const v = r && typeof r.s === "string" ? r.s : "";
    if (!r.dyn && /^(?:https?:)?\/\/[A-Za-z0-9.\-]+(?::\d{1,5})?(?:\/[^\s]*)?$/.test(v)) { hits.push({ key, base: v, dyn: false }); continue; }
    if (!explicit) continue;
    if (!r.dyn && /^\/[^\s]*$/.test(v)) { hits.push({ key, base: v, dyn: false }); continue; }
    // Unresolved: the expression as the code writes it, not a name picked for it.
    if (r.dyn) {
      const whole = /^\{[^{}]*\}$/.test(v) || v === "";
      const src = generateSrc(pr.value);
      if (whole && src && src.length <= 160) hits.push({ key, base: "{" + src + "}", dyn: true });
      else if (/^(?:https?:)?\/\/|^\/|^\{[^{}]+\}\//.test(v)) hits.push({ key, base: v, dyn: true });
    }
  }
  if (!hits.length) return null;
  if (hits.length === 1) return { base: hits[0].base, dyn: hits[0].dyn };
  const explicit = hits.filter((h) => _EXPLICIT_BASE_KEY.test(h.key));
  return explicit.length === 1 ? { base: explicit[0].base, dyn: explicit[0].dyn } : null;
}
// The client a value is, followed through bindings: {base, dyn, alts?} or null.
function clientOf(node, scope, depth) {
  depth = depth || 0;
  if (!node || depth > 10) return null;
  const union = (xs) => {
    const got = xs.filter(Boolean);
    if (!got.length) return null;
    const bases = [...new Set(got.flatMap((g) => [g.base, ...(g.alts || [])]))];
    return { base: bases[0], dyn: got.some((g) => g.dyn), alts: bases.slice(1) };
  };
  switch (node.type) {
    case "SequenceExpression": return clientOf(node.expressions[node.expressions.length - 1], scope, depth + 1);
    case "ParenthesizedExpression": case "AwaitExpression": case "TSAsExpression": case "TSNonNullExpression":
      return clientOf(node.expression || node.argument, scope, depth + 1);
    case "ConditionalExpression": return union([clientOf(node.consequent, scope, depth + 1), clientOf(node.alternate, scope, depth + 1)]);
    case "LogicalExpression": return union([clientOf(node.left, scope, depth + 1), clientOf(node.right, scope, depth + 1)]);
    case "Identifier": {
      const iv = importedValue(node.name, scope);
      if (iv && iv.client) return iv.client;
      const b = scope && scope.getBinding && scope.getBinding(node.name);
      const d = b && b.path && b.path.node;
      // `get = (p, o) => api.get(p, o)`: a function that is one call through a client is that client
      const fnd = d && (d.type === "FunctionDeclaration" ? d : d.type === "VariableDeclarator" && d.init && /Function/.test(d.init.type) ? d.init : null);
      if (fnd) {
        const ret = _returned(fnd);
        const fsc = (b.path.node === fnd ? b.path : b.path.get("init")).scope;
        return ret && (ret.type === "CallExpression" || ret.type === "AwaitExpression") ? callClient((ret.argument || ret).callee, fsc) : null;
      }
      if (!d || d.type !== "VariableDeclarator" || !d.init) return null;
      // `let {POST: v} = h`: a method taken off the client is still the client's
      if (d.id.type === "ObjectPattern") return clientOf(d.init, b.scope, depth + 1);
      if (d.id.type !== "Identifier") return null;
      return clientOf(d.init, b.scope, depth + 1);
    }
    case "MemberExpression": case "OptionalMemberExpression": {
      if (node.computed || !node.property) return null;
      const mid = _moduleOf(node.object, scope);
      if (mid != null) {
        const ex = _modExport(mid, node.property.name ?? node.property.value);
        return ex && ex.client ? ex.client : null;
      }
      return null;
    }
    case "CallExpression": case "OptionalCallExpression": case "NewExpression": {
      // A getter that hands the client back: `cs()`, `(0,A.getClient)()`, `useApi()`
      let c = node.callee;
      if (c && c.type === "SequenceExpression") c = c.expressions[c.expressions.length - 1];
      // RTK Query's `api.enhanceEndpoints(...)` and `api.injectEndpoints(...)` return the same api, so
      // its base is the base of whatever they were called on. Grafana chains both onto its createApi.
      if (node.type !== "NewExpression" && c && (c.type === "MemberExpression" || c.type === "OptionalMemberExpression") &&
          !c.computed && c.property && /^(enhanceEndpoints|injectEndpoints)$/.test(c.property.name || ""))
        return clientOf(c.object, scope, depth + 1);
      if (node.type !== "NewExpression" && c) {
        if (c.type === "Identifier") {
          const iv = importedValue(c.name, scope);
          if (iv && iv.returns) return iv.returns;
          const b = scope && scope.getBinding && scope.getBinding(c.name);
          const d = b && b.path && b.path.node;
          const fn = d && (d.type === "FunctionDeclaration" ? d : d.type === "VariableDeclarator" && d.init && /Function/.test(d.init.type) ? d.init : null);
          if (fn) {
            const ret = _returned(fn);
            const fnScope = (b.path.node === fn ? b.path : b.path.get("init")).scope;
            if (ret) return clientOf(ret, fnScope, depth + 1);
            return null;
          }
        } else if ((c.type === "MemberExpression" || c.type === "OptionalMemberExpression") && !c.computed && c.property) {
          const mid = _moduleOf(c.object, scope);
          if (mid != null) {
            const ex = _modExport(mid, c.property.name ?? c.property.value);
            if (ex && ex.returns) return ex.returns;
          }
        }
      }
      // …or the call that builds it
      return _baseOfCreate(node, scope);
    }
  }
  return null;
}
// A wrapper function another module exports, from a callee: `c(...)` with `c` imported, `(0,A.gt)(...)`.
function _exportedWrapper(callee, scope) {
  let c = callee;
  if (c && c.type === "SequenceExpression") c = c.expressions[c.expressions.length - 1];
  if (!c) return null;
  if (c.type === "Identifier") {
    const iv = importedValue(c.name, scope);
    return (iv && iv.wrapper) || null;
  }
  if ((c.type === "MemberExpression" || c.type === "OptionalMemberExpression") && !c.computed && c.property) {
    const mid = _moduleOf(c.object, scope);
    if (mid != null) { const ex = _modExport(mid, c.property.name ?? c.property.value); return (ex && ex.wrapper) || null; }
  }
  return null;
}
// An exported wrapper's template with one call's arguments put in, keeping the CALLER's own parameters
// as placeholders when the caller is itself being read as a wrapper.
function _composeWrapper(ext, args, scope, paramNames) {
  return ext.tmpl.replace(/\x01(\w+)\x01/g, (_, pn) => {
    const i = ext.paramNames.indexOf(pn);
    const a = i >= 0 ? args[i] : null;
    if (!a) return "{EXPR}";
    const v = strFromNode(a, scope, 0, paramNames || null).s;
    return _NODE_SLOT.test(v) ? "{param:" + pn + "}" : v;
  });
}
// The resolver's name for a value it could not read, after the kind of expression it was.
const _NODE_SLOT = /^\{(?:unary|nullliteral|booleanliteral|numericliteral|object|array|sequence|new|arrowfunction|function|class|this)\}$/;

// The client a call goes through, from its callee: `c.get(...)`, `(0,A.post)(...)`, `v(...)`.
function callClient(callee, scope) {
  let c = callee;
  if (c && c.type === "SequenceExpression") c = c.expressions[c.expressions.length - 1];
  if (!c) return null;
  if (c.type === "MemberExpression" || c.type === "OptionalMemberExpression")
    return clientOf(c.object, scope) || clientOf(c, scope);
  if (c.type === "Identifier") return clientOf(c, scope);
  return null;
}

{
  const _ci = process.argv.indexOf("--config");
  if (_ci > 0 && process.argv[_ci + 1]) {
    try {
      const _raw = JSON.parse(require("fs").readFileSync(process.argv[_ci + 1], "utf8"));
      for (const [k, v] of Object.entries(_raw)) if (typeof v === "string") _CONFIG.set(k, v);
    } catch {}
  }
}
const traverse = require("./babel.js").traverse.default;
const fs = require("fs");

const HTTP_METHODS = new Set(["get", "post", "put", "patch", "delete", "head", "options", "request"]);
// CLIENT HELPERS THAT ALWAYS ISSUE ONE VERB. Stainless-generated SDKs (Anthropic, OpenAI, Cloudflare)
// page every list endpoint through `this._client.getAPIList(path, PageClass, opts)`, which is a GET
// with no `.get` anywhere in sight. Those were the last method-less records in claude.ai's bundled
// Anthropic SDK: every one of the 27 `getAPIList` calls in that file. An alias is only honoured where a
// real HTTP-client method would be, so the receiver gate below still applies.
const VERB_ALIASES = { getAPIList: "get" };
// method-bearing member calls on a client: client.post(url, body) / api.get(url)
const PATHY = /^(https?:\/\/|\/|\.\/|\.\.\/)/; // looks like a URL or path
// A DATE FORMAT IS NOT A PATH. moment/dayjs ship a locale table,
//     {LTS:"h:mm:ss A", LT:"h:mm A", L:"MM/DD/YYYY", LL:"MMMM D, YYYY"}
//, and `L` reached a client-verb sink, so Notion produced an endpoint whose URL was "MM/DD/YYYY".
// Requiring a leading slash would have caught it and would also have thrown away real relative
// paths (`api/datasources/uid/…`, `s3/multipart`), so the test is the shape of a format string
// instead: every character is a date-format token or a separator, and at least one real token is
// present. `/api/v1` is not that; "MM/DD/YYYY", "YYYY-MM-DD HH:mm:ss" and "DD.MM.YYYY" are.
const DATE_FORMAT = (s) =>
  /^[YMDHhmsSAaZzWwEeQqkKgGtTlLoOxX:,.\/\- \[\]]+$/.test(s) &&
  /(YYYY|YY|MM|DD|HH|hh|mm|ss|SSS)/.test(s);

// A path that names a static file: an image, a font, a stylesheet, media, or a script.
const _STATIC_FILE = /\.(?:png|jpe?g|gif|svg|webp|avif|ico|bmp|woff2?|ttf|otf|eot|css|mp4|webm|mov|m4v|mp3|wav|ogg|m?js|map)$/i;
const LOOKS_PATH = (s) =>
  typeof s === "string" &&
  !DATE_FORMAT(s) &&
  (PATHY.test(s) || /^[\w.-]+\/[\w./{}$-]/.test(s)) && // bare "v2/foo" style
  !/\s/.test(s.trim()) &&
  // A URL cannot contain a control character. Minified bundles carry binary-ish string data, packed
  // tables, base64 with stray bytes, and a few of those matched the path shape and came out as endpoints:
  // `x.com/.04\u0013\u0002`, `shop.example.com/B_I+\u0010&57N`. Printed into the site map they also make the
  // map itself a binary file to anything that classifies by content, so grep on it silently returns
  // nothing, the same trap this repo already hit in its own extractor source.
  // eslint-disable-next-line no-control-regex
  !/[\u0000-\u001f\u007f]/.test(s) &&
  s.length < 400 &&
  !/^(image|text|application)\//.test(s) && // mime types
  !/\.(png|jpe?g|gif|svg|woff2?|css|ico)$/i.test(s);

function readAll(streamFiles) {
  if (streamFiles.length) return streamFiles.map((f) => safeRead(f)).join("\n/*__FILEBREAK__*/\n");
  return fs.readFileSync(0, "utf8");
}
function safeRead(f) {
  try {
    return fs.readFileSync(f, "utf8");
  } catch {
    return "";
  }
}

function parse(src) {
  // try full-featured (jsx+ts) first (sourcemap original source), fall back to plain script (minified bundle)
  for (const opts of [
    { sourceType: "unambiguous", plugins: ["jsx", "typescript"], errorRecovery: true },
    { sourceType: "unambiguous", plugins: [], errorRecovery: true },
    { sourceType: "script", plugins: [], errorRecovery: true },
  ]) {
    try {
      return parser.parse(src, opts);
    } catch {}
  }
  return null;
}

// Resolve a node to a string TEMPLATE, marking unresolved runtime pieces as {EXPR}. Returns {template, dynamic}.
// paramNames (optional): when resolving a WRAPPER FUNCTION's body, render references to its parameters as
// \x01name\x01 placeholders (substituted at each call site) instead of {EXPR}, the inter-procedural trace.
function resolveUrl(node, scope, paramNames) {
  // `new URL(path, base)` IS the URL, and every fetch-shaped sink accepts the object itself, not
  // only `.href`. tldraw builds its telemetry calls as
  //     fetch(new URL("/v1/event", this.#cfg.endpoint), {method:"POST", body:…})
  // and this returned {EXPR} for all of them, because strFromNode has no NewExpression case and the
  // only place that understood `new URL` was a later branch requiring an Identifier callee and an
  // explicit `.href`. Handling it HERE fixes every sink at once rather than one branch.
  //
  // newUrlNode also sees through `.href`, `.pathname`, `.toString()` and a local that was assigned
  // one, so all of those spellings resolve the same way. A plain string path makes newUrlNode
  // return null, so there is no recursion back into this function.
  const un = newUrlNode(node, scope);
  if (un && (un.arguments || []).length) {
    const r = resolveNewUrl(un, scope, paramNames);
    if (r && r.template && r.template !== "{EXPR}") return r;
  }
  const out = strFromNode(node, scope, 0, paramNames);
  return { template: out.s, dynamic: out.dyn };
}

// preserve a meaningful identifier as a slot label ({orgId}); drop minified 1-2-char garbage (e, t, Ge) to {EXPR}.
function NAMED(name) {
  // A 1-2 character name is a minifier artefact and carries no information, so it must NOT become a slot
  // label: `{param:e}` and `{bI}` are the same endpoint seen through two chunks, and emitting both
  // inflated one host from 156 routes to 359.
  //
  // But rendering it as a blank `{EXPR}` conflated two different situations, "the name is useless" and
  // "we could not resolve this at all", and only the second is a gap worth chasing. `{min}` is uniform,
  // so dedupe still works, and it says the cause is minification rather than a failure.
  return name && (name.length >= 3 || name === "id") ? "{" + name + "}" : "{min}";
}

// ENUM/SWITCH-dispatched action segments, `let a; switch(e){case 1:a="completion";case 2:a="retry"}; fetch(`${b}/${a}`)`.
// The URL segment is a variable whose possible values are string literals assigned across switch cases / branches.
// babel's binding.constantViolations gives every reassignment → collect the path-segment-like string values so the
// slot can be EXPANDED into one endpoint per action (this is how AI apps build streaming endpoints, /completion etc).
function enumValues(b) {
  const vals = new Set();
  const ok = (s) => typeof s === "string" && s.length && s.length < 40 && /^[\w][\w.-]*$/.test(s); // single path segment
  try {
    const init = b.path.node.init;
    if (init && init.type === "StringLiteral" && ok(init.value)) vals.add(init.value);
    for (const cv of (b.constantViolations || [])) {
      const n = cv.node;
      if (n && n.type === "AssignmentExpression" && n.operator === "=" && n.right &&
          n.right.type === "StringLiteral" && ok(n.right.value)) vals.add(n.right.value);
    }
  } catch {}
  return [...vals];
}
// look up a property VALUE node in an ObjectExpression literal (by key name/string).
function objProp(objExpr, name) {
  for (const p of (objExpr.properties || [])) {
    if (p.type !== "ObjectProperty" && p.type !== "Property") continue;
    const k = p.key && (p.key.name ?? p.key.value);
    if (k === name) return p.value;
  }
  return null;
}
// unwrap Object.freeze(X) / Object.assign({},X) → the underlying ObjectExpression (config objects are often frozen).
function unwrapObj(node) {
  if (node && node.type === "CallExpression" && node.callee && node.callee.type === "MemberExpression") {
    const nm = node.callee.property && node.callee.property.name;
    if (nm === "freeze" && node.arguments[0]) return unwrapObj(node.arguments[0]);
    if (nm === "assign") for (let i = node.arguments.length - 1; i >= 0; i--)
      if (node.arguments[i] && node.arguments[i].type === "ObjectExpression") return node.arguments[i];
  }
  return node;
}
// DFS an ObjectPattern to find the key-PATH that binds `target` (a specific Identifier). For nested destructuring
// `{api:{nca:$_}}`, resolving $_ means walking RHS→api→nca. Returns the key list, or null.
function destructureKeys(pattern, target, depth) {
  if (!pattern || depth > 8) return null;
  if (pattern === target) return [];
  if (pattern.type === "ObjectPattern") for (const p of (pattern.properties || [])) {
    if (p.type !== "ObjectProperty" || !p.key) continue;
    const sub = destructureKeys(p.value, target, depth + 1);
    if (sub) return [p.key.name ?? p.key.value, ...sub];
  }
  return null;
}
// resolve a node to the ObjectExpression LITERAL it refers to: a literal, a (possibly DESTRUCTURED, possibly
// Object.freeze'd) identifier binding, or a nested member into a config object (`cfg.paths`). Depth-bounded. Recovers
// baseURL + path tables that live in a pure-literal config object (very common in real bundles), not just {slot}.
function resolveObjLiteral(node, scope, depth) {
  if (!node || depth > 8) return null;
  node = unwrapObj(node);
  if (node.type === "ObjectExpression") return node;
  if (node.type === "Identifier" && scope && scope.getBinding) {
    try {
      const b = scope.getBinding(node.name);
      if (!b || !b.path || b.path.node.type !== "VariableDeclarator") return null;
      const decl = b.path.node;
      let obj = unwrapObj(decl.init);
      if (decl.id.type === "ObjectPattern") {                 // destructured, mirror the key-path into the init
        const keys = destructureKeys(decl.id, b.identifier, 0);
        if (!keys) return null;
        // The right-hand side of a destructure is usually ANOTHER name, not a literal:
        //   const {balance:Tye, svcB:Yb, staking:Zb} = Cye
        // Bailing unless `decl.init` was already an ObjectExpression meant one extra hop defeated the whole chain,
        // and with it every baseURL the app builds out of its service-config object.
        if (!obj || obj.type !== "ObjectExpression") obj = resolveObjLiteral(decl.init, b.scope || scope, depth + 1);
        for (const k of keys) {
          if (!obj || obj.type !== "ObjectExpression") return null;
          obj = unwrapObj(objProp(obj, k));
          if (obj && obj.type !== "ObjectExpression") obj = resolveObjLiteral(obj, b.scope || scope, depth + 1);
        }
      }
      return obj && obj.type === "ObjectExpression" ? obj : null;
    } catch { return null; }
  }
  if ((node.type === "MemberExpression" || node.type === "OptionalMemberExpression") &&
      !node.computed && node.property && node.property.name) {
    const parent = resolveObjLiteral(node.object, scope, depth + 1);
    if (parent) {
      const v = unwrapObj(objProp(parent, node.property.name));
      if (v && v.type === "ObjectExpression") return v;
      if (v) { const r = resolveObjLiteral(v, scope, depth + 1); if (r) return r; }   // config value that is itself a name
    }
  }
  return null;
}
function strFromNode(node, scope, depth, paramNames) {
  // Two different failures used to render identically. `{deep}` means the expression nested past the
  // recursion limit and might resolve with a bigger budget; a blank `{EXPR}` means we had no idea. Telling
  // them apart is the difference between a tunable and a wall.
  if (!node) return { s: "{EXPR}", dyn: true };
  if (depth > 6) return { s: "{deep}", dyn: true };
  switch (node.type) {
    case "StringLiteral":
      return { s: node.value, dyn: false };
    case "TemplateLiteral": {
      let s = "",
        dyn = false;
      node.quasis.forEach((q, i) => {
        s += q.value.cooked ?? q.value.raw ?? "";
        if (i < node.expressions.length) {
          const e = strFromNode(node.expressions[i], scope, depth + 1, paramNames);
          s += e.s;                                           // keep the (named) slot even when dynamic
          dyn = dyn || e.dyn;
        }
      });
      return { s, dyn };
    }
    case "BinaryExpression":
      if (node.operator === "+") {
        const l = strFromNode(node.left, scope, depth + 1, paramNames);
        const r = strFromNode(node.right, scope, depth + 1, paramNames);
        return { s: l.s + r.s, dyn: l.dyn || r.dyn };
      }
      return { s: "{EXPR}", dyn: true };
    case "Identifier": {
      if (paramNames && paramNames.includes(node.name))       // a wrapper param → substitutable placeholder
        return { s: "\x01" + node.name + "\x01", dyn: false };
      // An identifier IMPORTED from another chunk. `import{API_BASE}from"./config.js"` used in a URL is
      // most of the API-ish path literals that carry no attributed call site, the constant is declared in
      // one module and consumed in another, and stopping at the module boundary loses the endpoint.
      {
        const iv = importedValue(node.name, scope);
        if (iv && iv.str) return { s: iv.str, dyn: false };
      }
      // resolve const/let aliases bound to a string literal in scope (the common `const BASE="/api"` case)
      const b = scope && scope.getBinding && scope.getBinding(node.name);
      // A COMMA EXPRESSION, OR A LATER ASSIGNMENT. supabase's generated API client builds its URL as
      //     let t, {baseUrl: i} = a ?? {}, s = (t = i ?? env.SUPABASE_API_BASE_URL ?? c, new URL(`${t}${n}`).toString())
      // where `s` is the value of the comma expression's last part, and `t` has no initialiser, only one
      // assignment. Both are ordinary values; reading neither lost every one of its 131 `/v1` endpoints.
      if (b && b.path && b.path.node && b.path.node.type === "VariableDeclarator" && depth < 6) {
        const init0 = b.path.node.init;
        // Taken only when it says more than the fallback below would: a value that resolves to nothing
        // but `{EXPR}` loses the one thing the fallback keeps, that a minified name stood there.
        const _informative = (r) => r && typeof r.s === "string" && r.s.replace(/\{[^{}]*\}|\x01\w+\x01/g, "") !== "";
        if (init0 && init0.type === "SequenceExpression" && init0.expressions.length) {
          const last = init0.expressions[init0.expressions.length - 1];
          const un = newUrlNode(last, b.scope);
          const r = un ? (() => { const x = resolveNewUrl(un, b.scope, paramNames); return { s: x.template, dyn: x.dynamic }; })()
                       : strFromNode(last, b.scope, depth + 1, paramNames);
          if (_informative(r)) return r;
        }
        if (!init0 && b.constantViolations && b.constantViolations.length === 1) {
          const cv = b.constantViolations[0];
          if (cv.node && cv.node.type === "AssignmentExpression" && cv.node.operator === "=") {
            const r = strFromNode(cv.node.right, cv.scope, depth + 1, paramNames);
            if (_informative(r)) return r;
          }
        }
      }
      // A DESTRUCTURED name is a property of the initialiser, not the initialiser: `{baseUrl: i} = a ?? {}`
      // is `a.baseUrl`. Reading the whole of `a ?? {}` as `i` handed supabase's API client its options
      // object as a base. The destructuring rule further down is the one that applies.
      const _plain = !(b && b.path && b.path.node && b.path.node.id && b.path.node.id.type !== "Identifier");
      if (b && b.path && b.path.node && _plain) {
        const init = b.path.node.init || (b.path.node.type === "VariableDeclarator" && b.path.node.init);
        // recurse into string/template inits AND into Logical/Binary inits, the latter is the default-base config
        // pattern (`const base = env.API ?? "/api"`, `const u = host + "/v1"`) that later gets concatenated into a URL.
        if (init && init !== node && depth < 6 && (init.type === "StringLiteral" || init.type === "TemplateLiteral" ||
            init.type === "LogicalExpression" || init.type === "BinaryExpression"))
          return strFromNode(init, b.scope, depth + 1, paramNames);

        // A URL BUILDER'S OUTPUT LANDS IN A LOCAL. The commonest API-client shape there is:
        //     const n = Object(o.a)("/api/:version/groups/:id").replace(":id", e);
        //     return i.a.put(n, t);
        // The sink's argument is an Identifier, and because a CallExpression init was not recursed into,
        // it resolved to `{call:…}` and the endpoint was lost, while the very same expression written
        // INLINE inside the sink resolved fine. Measured on GitLab: 112 `/api/:version/...` templates are
        // reached this way and 111 of them came back unattributed, the one exception being the inline form.
        // A ternary init is the same story (`const d = x ? "/api/:version/groups/..." : "/api/:version/namespaces/..."`).
        //
        // Guarded on the RESULT, not the callee: recurse, and keep it only if what came back is actually
        // URL-shaped. `{call:getToken}` and `{EXPR}` fail that test and fall through to the old behaviour,
        // so this can surface a path that was already written in the code but cannot invent one.
        if (init && init !== node && depth < 6 &&
            (init.type === "CallExpression" || init.type === "ConditionalExpression" ||
             init.type === "AwaitExpression")) {
          const r = strFromNode(init, b.scope, depth + 1, paramNames);
          const t0 = r && typeof r.s === "string" ? r.s : "";
          if (/^(https?:\/\/|\/)/.test(t0) && !/^\{/.test(t0)) return r;
        }
      }
      // enum/switch-dispatched var → the segment is one of a known set of action strings (e.g. completion|retry|stop)
      if (b && b.path) {
        const ev = enumValues(b);
        if (ev.length >= 1 && ev.length <= 10) return { s: "\x02" + ev.join("|") + "\x02", dyn: true };
      }
      // DESTRUCTURED CONFIG: `{API_GATEWAY_URL: dS} = CFG[env]`, the local is minified garbage but the
      // PROPERTY KEY is the real name, and collectConfigUrls() has already harvested every candidate value
      // for it. Label the slot with the key so an opaque `{EXPR}` becomes `{cfg:API_GATEWAY_URL}`, which is
      // joinable against the config table instead of being a dead end.
      if (b && b.path && b.path.parentPath) {
        const idNode = b.path.node && b.path.node.id;
        if (idNode && idNode.type === "ObjectPattern") {
          for (const pp of idNode.properties) {
            if (pp.type === "ObjectProperty" && pp.value && pp.value.type === "Identifier" &&
                pp.value.name === node.name && pp.key) {
              const key = String(pp.key.name ?? pp.key.value ?? "");
              if (key && key.length >= 3) return { s: "{cfg:" + key + "}", dyn: true };
            }
          }
        }
      }
      // A bare {EXPR} tells you nothing about WHY resolution stopped. Say where it got stuck: a function
      // parameter, a call result, or a member read, `{window.location.origin}` is self-resolving against
      // the host we already requested, and `{call:getToken}` tells you what to go read next.
      if (b && b.kind === "param") return { s: "{param:" + node.name + "}", dyn: true };
      if (b && b.path && b.path.node && b.path.node.init) {
        const it = b.path.node.init;
        const isrc = generateSrc(it) || "";
        if (it.type === "MemberExpression" && /^[\w.$\[\]'"]{1,48}$/.test(isrc)) {
          // THE RECEIVER IS USUALLY MINIFIER NOISE. `e.subdomain` is about `subdomain`; the `e` is
          // whatever that scope happened to call it and changes between builds. Strip a 1-3 char
          // receiver, keep a meaningful one, `window.location.origin` and `import.meta.env.X` are
          // handled above precisely because their receiver IS the information.
          const trimmed = isrc.replace(/^[A-Za-z_$][\w$]{0,2}\./, "");
          return { s: "{" + (trimmed.includes(".") || trimmed.length > 2 ? trimmed : isrc) + "}",
                   dyn: true };
        }
        if (it.type === "CallExpression" && it.callee) {
          // A SELECTOR SAYS WHAT IT SELECTS. `useSelector(s => s.data.subdomain)` and every
          // hook shaped like it carry the answer in the callback, one level in: the call itself
          // is minified to `(0,g.Iw)(...)` and tells you nothing, while the body names the exact
          // piece of application state the URL is built from.
          //
          // Measured on readme's dashboard: `var e = (0,g.Iw)(function(e){return e.data.subdomain})`
          // produced `/{EXPR}/api-next/v2/projects/me/migrate-to-mdxish`. `{EXPR}` is honest and
          // useless; `{data.subdomain}` is equally honest and tells you what to go and read, at
          // runtime or in a response you have already captured.
          for (const a of (it.arguments || [])) {
            if (a && (a.type === "ArrowFunctionExpression" || a.type === "FunctionExpression")) {
              const body = a.body && a.body.type === "BlockStatement"
                ? (a.body.body || []).filter(x => x.type === "ReturnStatement")[0]?.argument
                : a.body;
              if (body && body.type === "MemberExpression") {
                // drop the callback's own parameter name, which is minifier noise: `e.data.sub`
                // is about `data.sub`, and the `e` is whatever that one arrow happened to call it.
                const msrc = (generateSrc(body) || "").replace(/^[A-Za-z_$][\w$]{0,2}\./, "");
                if (/^[\w.$\[\]'"]{1,48}$/.test(msrc) && msrc.includes("."))
                  return { s: "{" + msrc + "}", dyn: true };
              }
            }
          }
          return { s: "{call:" + _clip(generateSrc(it.callee) || "?", 64) + "}", dyn: true };
        }
      }
      // unresolved: preserve a MEANINGFUL name as the slot label (survives when it's real, e.g. sourcemap 'orgId').
      // minified 1-2 char garbage (e, t, Ge) stays {EXPR}, no signal. 'id' is a real, common param name.
      return { s: NAMED(node.name), dyn: true };
    }
    case "MemberExpression": {
      // `window.location.origin` / `import.meta.env.VITE_API` are self-documenting and often self-resolving
      // (we KNOW the origin, it is the host we fetched). Never flatten them to an opaque {EXPR}.
      const msrc0 = generateSrc(node) || "";
      if (/^(window\.)?location\.(origin|host|hostname|protocol|href)$/.test(msrc0) ||
          /^import\.meta\.env\.[A-Za-z_][\w]*$/.test(msrc0) ||
          /^process\.env\.[A-Za-z_][\w]*$/.test(msrc0))
        return { s: "{" + msrc0 + "}", dyn: true };
    }
    case "MemberExpression_orig":
    case "OptionalMemberExpression": {
      if (!node.computed && node.property && node.property.name) {
        // ANOTHER MODULE'S EXPORT. `d.API_URL` with `d = e.i(101)` is export API_URL of module 101, read
        // from that module, which beats any guess by property name.
        {
          const mid = _moduleOf(node.object, scope);
          if (mid != null) {
            const ex = _modExport(mid, node.property.name);
            if (ex && ex.str) return { s: ex.str, dyn: false };
          }
        }
        // FIRST try to resolve `cfg.base` / `cfg.paths.auth` into the CONFIG OBJECT LITERAL it points at, pure-literal
        // config objects (baseURL + path tables) are extremely common; recover the real string, not a {slot}. This is
        // load-bearing for axios baseURL resolution and lifts recall broadly wherever URLs come from a config object.
        if (depth < 8) {
          const obj = resolveObjLiteral(node.object, scope, depth + 1);
          if (obj) { const v = objProp(obj, node.property.name); if (v) return strFromNode(v, scope, depth + 1, paramNames); }
        }
        // The value may have been injected into the page rather than written in any file. If the HTML
        // handed us one under this property name, that is the actual deployed value, better than a slot.
        // Same generic-name rule as the property index below. A page config blob that happens to contain
        // `url` does not tell you what `this.consumer.url` holds three modules away, that substitution
        // put reddit's ActionCable socket at a Zendesk documentation URL.
        const cfgv = _GENERIC_PROP.has(node.property.name) ? undefined
                                                           : _CONFIG.get(node.property.name);
        if (cfgv && _BORROWABLE(cfgv)) return { s: cfgv, dyn: false };
        // Not named in the page config, but something in the bundle assigned it. `c.baseUrl` is never in
        // the config; `this.baseUrl = t.adminBaseUrl` is in the code and `adminBaseUrl` is in the config.
        // The cross-file property index answers "what value does this property usually hold". For a
        // distinctive name that is a strong signal; for `url`, `path` or `host` it is a coin flip, because
        // every module in the bundle has one and they are unrelated. Measured: reddit's ActionCable does
        // `new I.WebSocket(this.consumer.url, H)`, where the URL is genuinely a runtime value, and the
        // index confidently supplied `https://support.reddithelp.com/` from some other module's `url:`,
        // fabricating a socket at a documentation page. Names this generic must resolve to nothing rather
        // than to whichever literal happened to be indexed first.
        const pv = _GENERIC_PROP.has(node.property.name) ? undefined
                                                         : _PROPVALS.get(node.property.name);
        if (pv && _BORROWABLE(pv)) return { s: pv, dyn: false };
        // property names survive minification (they ARE the data contract) → `${e?.uuid}` becomes {uuid}, not {EXPR}.
        return { s: "{" + node.property.name + "}", dyn: true };
      }
      // A COMPUTED member, `cfg[key]` or `paths[name]`. The property is itself a runtime value, so there
      // is no name to borrow. Distinguishing it from a blank says the lookup table is worth finding.
      return { s: "{computed}", dyn: true };
    }
    case "OptionalCallExpression":
    case "CallExpression": {
      // A URL BUILDER ANOTHER MODULE EXPORTS: its template, with this call's arguments in it.
      {
        const bld = _exportedBuilder(node.callee, scope);
        if (bld) {
          const out = _composeWrapper(bld, node.arguments || [], scope, paramNames);
          return { s: out, dyn: /\{[^}]*\}|\x01/.test(out) };
        }
      }
      // THERE WERE TWO `case "CallExpression"` ARMS IN THIS SWITCH. The first handled .concat() chains and
      // then unconditionally returned {EXPR}, which made this one, encoder unwrapping, user-defined URL
      // builder inlining, everything below, unreachable. It had never run. The duplicate arrived when a
      // `break` was changed to a `return` to stop a crash: the crash went away and ninety lines went dark,
      // and nothing failed loudly enough to notice. Merged now, .concat first.
      const c = node.callee, nm = c && (c.name || (c.property && c.property.name));
      // ES5 output has no template literals: Babel compiles `${a}/x/${b}` down to
      //     "".concat(a, "/x/").concat(b)          or     "/idp/myaccount/emails/".concat(id)
      // so every URL in a bundle targeting older browsers is a .concat() chain.
      if (c && c.type === "MemberExpression" && !c.computed &&
          c.property && c.property.name === "concat") {
        const head = strFromNode(c.object, scope, depth + 1, paramNames);
        let acc = head.s, accDyn = head.dyn;
        for (const a of node.arguments || []) {
          const pp = strFromNode(a, scope, depth + 1, paramNames);
          acc += pp.s;
          accDyn = accDyn || pp.dyn;
        }
        return { s: acc, dyn: accDyn };
      }
      // BABEL'S INTEROP WRAPPER. ES5 webpack output calls an imported function as `Object(U.a)(d)`, not
      // `buildUrl(d)`, the callee is a CallExpression, so every guard below that tests for an Identifier
      // callee silently skipped it. GitLab ships 112 `/api/:version/...` templates reached exactly this
      // way, and all 112 came back {EXPR}. Unwrap one level and re-run this same case on `f(args)`.
      if (c && c.type === "CallExpression" && c.callee && c.callee.name === "Object" &&
          (c.arguments || []).length === 1 && depth < 6) {
        return strFromNode({ ...node, callee: c.arguments[0] }, scope, depth + 1, paramNames);
      }

      // `.replace(":full_path", v)` ON A PATH TEMPLATE IS SLOT SUBSTITUTION, not string mangling.
      // Rails/Express-style templates are built by substituting one colon-param at a time, so the chain
      // `buildUrl(t).replace(":full_path", x).replace(":id", y)` is how the real URL is assembled. Resolve
      // the object, then fill the named slot, keeping it a NAMED slot, because the name is the contract
      // (an authorization sweep needs to know a slot wants a full_path and not an id).
      if (c && c.type === "MemberExpression" && !c.computed && c.property &&
          (c.property.name === "replace" || c.property.name === "replaceAll") &&
          (node.arguments || []).length === 2 && depth < 8) {
        const needle = node.arguments[0];
        if (needle && needle.type === "StringLiteral" && /^:[A-Za-z_][\w]*$/.test(needle.value)) {
          const base = strFromNode(c.object, scope, depth + 1, paramNames);
          if (base && typeof base.s === "string" && base.s.includes(needle.value)) {
            const slot = "{" + needle.value.slice(1) + "}";
            return { s: base.s.split(needle.value).join(slot), dyn: true };
          }
        }
      }

      // A FUNCTION OF NO ARGUMENTS IS ITS BODY. Grafana's `namespace = () => config.namespace` sits in
      // every k8s-style API path, and read through, the slot is `{namespace}` rather than a numbered one.
      if (c && c.type === "Identifier" && !(node.arguments || []).length && depth < 6) {
        const b0 = scope && scope.getBinding && scope.getBinding(c.name);
        const d0 = b0 && b0.path && b0.path.node;
        const f0 = d0 && (d0.type === "FunctionDeclaration" ? d0 : d0.type === "VariableDeclarator" && d0.init && /Function/.test(d0.init.type) ? d0.init : null);
        if (f0 && !(f0.params || []).length) {
          const ret0 = _returned(f0);
          // Only a body with no runtime choice in it. lovable's i18n helper is
          // `() => store ? store.origin ?? "http://fallback.com" : window.location.origin`, and reading it
          // took the server branch's placeholder host for the browser's own origin.
          if (ret0 && ret0.type !== "ConditionalExpression" && ret0.type !== "LogicalExpression") {
            const r0 = strFromNode(ret0, b0.scope, depth + 1, null);
            if (r0 && typeof r0.s === "string" && r0.s && !/^\{(?:EXPR|deep|call:[^}]*)\}$/.test(r0.s)) return r0;
          }
        }
      }
      // `.replace("/platform", "")` on a string we know is just that string, edited. supabase builds its API
      // client's base as `API_URL?.replace("/platform","")`.
      if (c && (c.type === "MemberExpression" || c.type === "OptionalMemberExpression") && !c.computed && c.property &&
          (c.property.name === "replace" || c.property.name === "replaceAll") && (node.arguments || []).length === 2 &&
          node.arguments[0].type === "StringLiteral" && node.arguments[1].type === "StringLiteral" && depth < 8) {
        const base = strFromNode(c.object, scope, depth + 1, paramNames);
        if (base && !base.dyn && typeof base.s === "string") {
          const [a, b] = [node.arguments[0].value, node.arguments[1].value];
          return { s: c.property.name === "replace" ? base.s.replace(a, () => b) : base.s.split(a).join(b), dyn: false };
        }
      }

      // AN UNKNOWN ONE-ARG CALL AROUND A COLON TEMPLATE IS A URL BUILDER. `/api/:version/groups/:id` is
      // self-identifying: nothing else in a bundle looks like that, and a function handed one is building
      // a URL from it. The guard is the SHAPE OF THE ARGUMENT, not the callee, which is what makes this
      // safe when the callee lives in another module and cannot be read, it cannot invent a path,
      // because the path is already there in the argument.
      if ((node.arguments || []).length === 1 && depth < 6) {
        const inner = strFromNode(node.arguments[0], scope, depth + 1, paramNames);
        const t = inner && typeof inner.s === "string" ? inner.s : "";
        if (/^\//.test(t) && /\/:[A-Za-z_][\w]*(\/|$)/.test(t)) return { s: t, dyn: true };
      }

      // unwrap URL-safe encoders so `${encodeURIComponent(orgId)}` → the arg's slot ({orgId}/{uuid}), very common.
      if (/^(encodeURIComponent|encodeURI|String|toString|escape|trim)$/.test(nm || "") && node.arguments[0])
        return strFromNode(node.arguments[0], scope, depth + 1, paramNames);
      // A ONE-IN-ONE-OUT URL TRANSFORM. Keycloak builds every admin URL as `${k(c.baseUrl)}admin/realms/…`
      // where `k` is `s => s.endsWith("/") ? s.slice(0,-1) : s`, a trailing-slash strip. The base resolves
      // fine on its own; the unknown call around it was what forced {EXPR}, so 474 endpoints kept an
      // unresolved prefix because of a four-token helper.
      //
      // Guarded three ways so this cannot invent a URL: exactly one argument, the callee is a user function
      // of one parameter that contains no path literal of its own (so it is massaging its input, not
      // building a different URL), and the argument must already resolve to something URL-shaped. A
      // transform that only reshapes a URL leaves the URL recognisable, that is the whole assumption, and
      // it is stated here rather than hidden.
      if (c && c.type === "Identifier" && node.arguments.length === 1 && depth < 5) {
        const b0 = scope && scope.getBinding && scope.getBinding(c.name);
        const fn0 = b0 && b0.path && b0.path.node && (b0.path.node.init ||
          (b0.path.node.type === "FunctionDeclaration" ? b0.path.node : null));
        const isFn = fn0 && /Function/.test(fn0.type || "");
        if (isFn && (fn0.params || []).length === 1) {
          let hasPathLiteral = false;
          try {
            const gen = generateSrc(fn0.body) || "";
            hasPathLiteral = /["'`]\/[A-Za-z0-9_{$]/.test(gen);
          } catch {}
          // …and the body must actually return its argument. See _returnsItsArgument: the old test was
          // only "no path literal", which a truncating helper passes.
          const p0 = (fn0.params || [])[0];
          const pn = p0 && p0.type === "Identifier" ? p0.name : null;
          if (!hasPathLiteral && pn && _returnsItsArgument(fn0, pn)) {
            const inner = strFromNode(node.arguments[0], scope, depth + 1, paramNames);
            if (inner && /^(https?:\/\/|\/)/.test(String(inner.s || "")))
              return inner;
          }
        }
      }
      // The same transform, but IMPORTED from another chunk so its body is not in this file. Keycloak's is
      // `import{ w as k }` and `k` is a trailing-slash strip living in a different module; without this the
      // whole admin API keeps an unresolved prefix because of a helper we can see called and cannot see
      // defined.
      //
      // This is an INFERENCE, unlike the case above where the body was read: a one-argument call, in a URL
      // template position, whose argument already resolves to a URL, is treated as reshaping that URL. The
      // result is marked dynamic so nothing downstream mistakes it for a literal. Resolving it properly
      // needs the module graph, unbundle.js builds one for webpack, and this bundle is Rollup.
      if (c && c.type === "Identifier" && node.arguments.length === 1 && depth < 5) {
        const bx = scope && scope.getBinding && scope.getBinding(c.name);
        const isImported = bx && (bx.kind === "module" ||
          (bx.path && bx.path.parentPath && bx.path.parentPath.isImportDeclaration &&
           bx.path.parentPath.isImportDeclaration()));
        // If the imported function's body is in another chunk we HOLD, read it rather than guess. A one
        // parameter function containing no path literal of its own is reshaping its input, which makes
        // substituting the resolved value a fact instead of an assumption.
        const xv = isImported ? importedValue(c.name, scope) : null;
        if (xv && xv.transform) {
          const inner2 = strFromNode(node.arguments[0], scope, depth + 1, paramNames);
          if (inner2 && inner2.s && inner2.s !== "{EXPR}") return inner2;
        }
        if (isImported) {
          const inner = strFromNode(node.arguments[0], scope, depth + 1, paramNames);
          // Pass through whatever the argument resolved to, even a named slot. `k(c.baseUrl)` collapsing to
          // {EXPR} threw away the one useful fact, that the missing piece is `baseUrl`, and left a reader
          // with nothing to look up. `{baseUrl}admin/realms/…` names it, and the config values harvested
          // from the page are recorded alongside, so the substitution is a lookup rather than a guess.
          //
          // Deliberately NOT guessing which config key `baseUrl` means: this page carries adminBaseUrl,
          // serverBaseUrl, authServerUrl and authUrl all equal to the same origin, but consoleBaseUrl and
          // resourceUrl differ. Picking one by name similarity would be right here and wrong elsewhere.
          // Keep the SLOT, never the value. We can see this call happen and cannot see what it does,
          // Keycloak's `k` is imported from another chunk, so substituting the base produced
          // `https://auth.example.com/authadmin/realms/...`, a URL missing a separator that 404s. A
          // concrete wrong URL is worse than an honest slot: it reads as a fact and cannot be requested.
          // `{baseUrl}admin/realms/...` names exactly what is missing, and the config values harvested
          // from the page are recorded beside it, so substituting is a lookup a reader can perform and
          // verify. Where the transform IS readable (the branch above) its body proved it only reshapes
          // its input, and substituting there is justified.
          if (inner && inner.s && inner.s !== "{EXPR}") {
            const named = /^[A-Za-z_$][\w$]*$/.test(String(node.arguments[0] &&
                            node.arguments[0].property && node.arguments[0].property.name || ""))
                          ? "{" + node.arguments[0].property.name + "}" : inner.s;
            return { s: named, dyn: true };
          }
        }
      }
      // USER-DEFINED URL-BUILDER: `f(a,b)` where f RETURNS a template referencing its params (e.g. yM(e,t,s) =>
      // `/api/orgs/${e}/cowork/sessions/${t}`). Inline it: resolve the return with the fn's params as placeholders,
      // then substitute the call args (resolved in the caller's scope). Mirror of collectWrappers (fns that CALL a
      // sink); this handles fns that RETURN a url. Depth-bounded so builder→builder can't loop.
      if (c && c.type === "Identifier" && depth < 5) {
        const b = scope && scope.getBinding && scope.getBinding(c.name);
        const fnode = b && b.path && b.path.node && (b.path.node.init ||
          (b.path.node.type === "FunctionDeclaration" ? b.path.node : null));
        if (fnode && (fnode.type === "ArrowFunctionExpression" || fnode.type === "FunctionExpression" || fnode.type === "FunctionDeclaration")) {
          let ret = fnode.body;
          if (ret && ret.type === "BlockStatement") { const rs = ret.body.find(s => s.type === "ReturnStatement"); ret = rs ? rs.argument : null; }
          if (ret && (ret.type === "TemplateLiteral" || ret.type === "BinaryExpression" || ret.type === "StringLiteral")) {
            const pnames = fnode.params.map(p => (p.type === "Identifier" ? p.name : "\x00"));
            const inner = strFromNode(ret, b.scope, depth + 1, pnames);
            if (inner.s.includes("/") && /[a-z]/i.test(inner.s.replace(/\x01\w+\x01/g, ""))) {
              let dyn = inner.dyn;
              const out = inner.s.replace(/\x01(\w+)\x01/g, (_, pn) => {
                const idx = pnames.indexOf(pn), arg = idx >= 0 ? node.arguments[idx] : null;
                if (!arg) { dyn = true; return "{EXPR}"; }
                const u = strFromNode(arg, scope, depth + 1, paramNames); if (u.dyn) dyn = true; return u.s;
              });
              return { s: out, dyn };
            }
          }
        }
      }
      return { s: "{EXPR}", dyn: true };
    }
    // `await getUrl()`, `(url)`, `url as string`, `url!`, wrappers that change nothing about the value.
    // Falling through to the default returned {EXPR} and lost whatever was inside them.
    case "SequenceExpression":
      // `(t = new URLSearchParams, ..., `/v1/logs?${t}`)`: a comma expression's value is its last part
      if (node.expressions && node.expressions.length)
        return strFromNode(node.expressions[node.expressions.length - 1], scope, depth + 1, paramNames);
      return { s: "{EXPR}", dyn: true };
    case "AwaitExpression":
    case "ParenthesizedExpression":
    case "TSAsExpression":
    case "TSNonNullExpression":
      return strFromNode(node.expression || node.argument, scope, depth + 1, paramNames);
    case "TaggedTemplateExpression":
      // `this._client.post(path\`/v1/files/${id}\`)`. Stainless-generated SDKs, which is the
      // Anthropic, OpenAI and Cloudflare ones among others, build EVERY resource URL with a `path` tag
      // that only URL-encodes the interpolations. The URL's shape is the quasi; the tag is escaping.
      // This fell to `default` and rendered as `{taggedtemplate}`, so the call site was discarded and
      // only a method-less route constant survived: 136 records with no verb in one claude.ai file.
      return strFromNode(node.quasi, scope, depth + 1, paramNames);
    case "ConditionalExpression": {
      // a?b:c, take the consequent template but flag dynamic (both branches are real endpoints; caller can note)
      const c = strFromNode(node.consequent, scope, depth + 1, paramNames);
      return { s: c.s, dyn: true };
    }
    case "LogicalExpression": {
      // `x ?? "/api"` / `env.BASE || "/api"`, if the LEFT resolves to a concrete value, it wins (a real override).
      // But when the left is an unresolvable param/env (`{apiPrefix}`), FALL BACK to the RIGHT literal, that's the
      // config-DEFAULT base, the actual runtime value whenever the left isn't supplied (the common case). This is
      // what recovers claude.ai's `apiPrefix ?? "/edge-api"` → /edge-api/bootstrap, and every default-base pattern.
      const l = strFromNode(node.left, scope, depth + 1, paramNames);
      const leftUsable = l.s && l.s !== "" && !l.s.includes("{EXPR}") && !/^\{[^}]*\}$/.test(l.s);
      if (leftUsable) return { s: l.s, dyn: true };
      const rr = strFromNode(node.right, scope, depth + 1, paramNames);
      if (rr.s && rr.s !== "" && !rr.s.includes("{EXPR}")) return { s: rr.s, dyn: rr.dyn };
      return { s: l.s, dyn: true };
    }
    default:
      // Say what the thing was. "{EXPR}" is a blank; "{new}" or "{spread}" tells a reader whether this is
      // worth chasing and lets the residual be grouped by cause rather than counted as one lump.
      return { s: "{" + String(node.type || "EXPR").replace(/Expression$/, "").toLowerCase() + "}",
               dyn: true };
  }
}

// ── BODY SCHEMA: the nested shape, not a flat key list ──────────────────────────────────────────────
// bodyKeys() returns only top-level key NAMES, discarding p.value entirely, so `{user:{id,role}}` came out
// as ["user"] and the nesting was lost. A flat key list is not a schema; you cannot build a valid request
// from it. This walks the same AST and keeps the structure, plus the type wherever the VALUE is a literal
// (TS annotations are erased at build time, but `{limit:50, active:!0}` still carries number/boolean).
const _LIT = (v) => {
  if (!v) return null;
  switch (v.type) {
    case "StringLiteral": return "string";
    case "NumericLiteral": return "number";
    case "BooleanLiteral": return "boolean";
    case "NullLiteral": return "null";
    case "UnaryExpression":                       // minifiers emit !0 / !1 for true / false
      return v.operator === "!" && v.argument && v.argument.type === "NumericLiteral" ? "boolean" : null;
    case "ArrayExpression": return "array";
    default: return null;
  }
};
// Union the argument shapes that CALLERS pass in a given parameter position. See the call site above.
function _shapeFromCallers(binding, paramName, depth) {
  try {
    const fnPath = binding.path && binding.path.parentPath;          // the function owning the param
    if (!fnPath || !fnPath.node) return null;
    const params = fnPath.node.params || [];
    let idx = -1;
    for (let i = 0; i < params.length; i++) {
      const pn = params[i];
      if (pn && pn.type === "Identifier" && pn.name === paramName) { idx = i; break; }
    }
    if (idx < 0) return null;
    const idx_ = idx;

    // How is this function reachable by name? Three shapes cover essentially every bundle:
    //   function foo(e){}            -> the function's own id
    //   const foo = e => {}          -> the variable it is assigned to
    //   { foo(e){} } / { foo: e=>{} }-> the object property key
    let name = null, refPaths = null;
    const n = fnPath.node;
    if (n.id && n.id.name) name = n.id.name;
    const pp = fnPath.parentPath;
    if (!name && pp && pp.node) {
      if (pp.node.type === "VariableDeclarator" && pp.node.id && pp.node.id.name) name = pp.node.id.name;
      else if ((pp.node.type === "ObjectProperty" || pp.node.type === "ObjectMethod") && pp.node.key)
        name = pp.node.key.name || pp.node.key.value || null;
    }
    if (n.type === "ObjectMethod" && n.key) name = n.key.name || n.key.value || name;
    if (!name) return null;

    // A plain identifier binding gives us exact references. An object METHOD has no binding, so fall
    // back to matching `*.name(...)` call sites in the same file, the method name is the index.
    const fscope = fnPath.scope && fnPath.scope.getBinding && fnPath.scope.getBinding(name);
    const args = [];
    if (fscope && fscope.referencePaths) {
      for (const r of fscope.referencePaths) {
        const c = r.parentPath;
        if (c && c.node && c.node.type === "CallExpression" && c.node.callee === r.node)
          args.push({ node: c.node.arguments[idx], scope: c.scope });
        if (args.length > 12) break;
      }
    }
    let byName = false;
    if (!args.length && fnPath.hub && fnPath.hub.file && fnPath.hub.file.path) {
      byName = true;
      fnPath.hub.file.path.traverse({
        CallExpression(cp) {
          if (args.length > 12) { cp.stop(); return; }
          const ce = cp.node.callee;
          if (ce && ce.type === "MemberExpression" && ce.property &&
              (ce.property.name === name || ce.property.value === name))
            args.push({ node: cp.node.arguments[idx], scope: cp.scope });
        }
      });
    }

    // Union what the callers pass. A union, not a first-match: one endpoint routinely takes several
    // partial bodies, the wire showed /user/me sent {phone} while the code's first caller passed
    // {name}, and BOTH are real fields of that endpoint.
    const out = {};
    let got = 0;
    for (const a of args) {
      if (!a.node) continue;
      const sh = bodySchema(a.node, a.scope, depth + 2);
      if (sh && typeof sh === "object" && !Array.isArray(sh)) {
        for (const k of Object.keys(sh)) if (!(k in out) || out[k] === "{EXPR}") out[k] = sh[k];
        got++;
      }
    }
    // Matched by NAME, the union is only as good as the name is rare. Clerk's static `attempt(e)`
    // matched every `*.attempt(...)` in its file, a retry helper's `{initialDelay, factor, jitter}`
    // among them, and 84 keys became the body of POST /me/biometric_credentials/attempt. Past 12 keys
    // it is a collision, the same line the corpus-wide index holds below.
    if (got) return (byName && Object.keys(out).length > 12) ? null : out;
    // Nothing in this file. Ask the corpus-wide index, the caller is usually in another chunk. The
    // index is keyed by NAME, so a common one gathers every same-named call in the bundle: Clerk's
    // `attempt` came back with 84 keys from a retry helper and others (`initialDelay`, `jitter`,
    // `constructor`), reported as the body of POST /me/biometric_credentials/attempt. Past 12 keys a
    // union is a collision, as the other reader of this index already holds, and no body is better
    // than that one.
    const fromIndex = _CALLARGS.get(name + "|" + idx_);
    const nIdx = fromIndex ? Object.keys(fromIndex).length : 0;
    return nIdx && nIdx <= 12 ? fromIndex : null;
  } catch (_) { return null; }
}

function bodySchema(node, scope, depth = 0) {
  node = _unwrapIIFE(node);          // the serializer IIFE, same as bodyKeys
  if (!node || depth > 12) return "{EXPR}";
  if (node.type === "CallExpression") {           // JSON.stringify(x) → look through it
    const c = node.callee;
    if (c && c.type === "MemberExpression" && c.property && c.property.name === "stringify")
      return bodySchema(node.arguments[0], scope, depth + 1);
    return "{EXPR}";
  }
  if (node.type === "ConditionalExpression")
    return bodySchema(node.consequent, scope, depth + 1) ?? bodySchema(node.alternate, scope, depth + 1);
  if (node.type === "LogicalExpression")
    return bodySchema(node.right, scope, depth + 1) ?? bodySchema(node.left, scope, depth + 1);
  if (node.type === "Identifier") {               // follow the variable to its construction site
    // A TYPED PARAMETER IS A SHAPE, and it is the one case no runtime walker can reach: `(data: Foo)`
    // has no object literal to read, anywhere. Tried first because when it is available it is the
    // authoritative answer, it is the contract the application's own compiler checked.
    const ts = tsShapeOfParam(node, scope);
    if (ts && typeof ts === "object") return ts;
    const b = scope && scope.getBinding && scope.getBinding(node.name);
    if (b && b.path && b.path.node && b.path.node.init)
      return bodySchema(b.path.node.init, b.scope, depth + 1);
    // THE BODY IS THE ENCLOSING FUNCTION'S PARAMETER. Ask the callers what they pass.
    //
    // Every mature client wraps its requests: `changeRole(e){ r({method:"post", url:"...", data:e}) }`.
    // At the call site `e` is a parameter, there is no literal to read, and the endpoint arrives with a
    // verb, a path and no contract at all. Measured across six targets: 49% of declared writes had no
    // body, and on one of them a single sink accounted for 109 of them with zero recovered.
    //
    // The shape is not missing, it is one frame up, at whoever calls changeRole. Babel already tracks
    // every reference to a binding, so this walks them, takes the argument in the same position, and
    // unions what the callers agree on. Deliberately conservative: same file only (bundlers keep a
    // module together), literal arguments only, and a cap, because this runs per endpoint.
    if (b && b.kind === "param") {
      const shape = _shapeFromCallers(b, node.name, depth);
      if (shape) return shape;
    }
    if (typeof ts === "string" && ts) return ts;   // a named type we could not resolve still says more than {EXPR}
    return "{EXPR}";
  }
  if (node.type === "ArrayExpression")
    return node.elements.length ? [bodySchema(node.elements[0], scope, depth + 1)] : "array";
  if (node.type !== "ObjectExpression") return _LIT(node) || "{EXPR}";

  const out = {};
  for (const p of node.properties) {
    if ((p.type === "ObjectProperty" || p.type === "ObjectMethod") && p.key) {
      const k = p.key.name ?? p.key.value ?? "{EXPR}";
      const v = p.value;
      if (p.type === "ObjectMethod") { out[k] = "function"; continue; }
      const lit = _LIT(v);
      let val = lit ? lit
              : (v && (v.type === "ObjectExpression" || v.type === "ArrayExpression" ||
                       v.type === "Identifier" || v.type === "ConditionalExpression" ||
                       v.type === "LogicalExpression" || v.type === "CallExpression"))
                ? bodySchema(v, scope, depth + 1)
                : "{EXPR}";
      // `{EXPR}` was thrown at every non-literal value, which is most of a real request body, and the
      // information was not missing, it was being computed by classifyValue() into a SEPARATE field
      // (body_src) that nothing surfaced. classifyValue resolves an identifier to its binding, a parameter
      // to `input:<name>`, a small enum to its members, a uuid/timestamp call to `client-gen:*`, so
      // {"event_name":"{EXPR}"} is really {"event_name":"input:eventName"}. Two analyses of the same node,
      // and we were showing the weaker one.
      if (val === "{EXPR}" && v) {
        const cls = classifyValue(v, scope, depth + 1);
        if (cls && cls !== "?" && cls !== "runtime") val = cls;
      }
      out[k] = val;
    } else if (p.type === "SpreadElement") {      // {...base, x} → merge base's shape in
      const sub = bodySchema(p.argument, scope, depth + 1);
      if (sub && typeof sub === "object" && !Array.isArray(sub)) Object.assign(out, sub);
      else out["...{EXPR}"] = "{EXPR}";
    }
  }
  return out;
}

// ── QUERY STRING: axios/ky-style `params:{…}` and `query:{…}` were never extracted at all. ──
// They are URL parameters, half the input surface of a GET, and WRAPPER_FIELDS only listed them so they
// would not be mistaken for a body. Same treatment as the body: keys plus literal-derived types.
function queryInfo(node, scope) {
  const s = bodySchema(node, scope, 0);
  return s && typeof s === "object" && !Array.isArray(s) && Object.keys(s).length ? s : null;
}

// ── CLIENT-LEVEL HEADERS ────────────────────────────────────────────────────────────────────────────
// Headers are an injection point exactly like body and query params are, but they are almost never written
// at the call site. Real clients set them ONCE, centrally, and every request inherits them, so reading only
// per-call `{headers:{…}}` finds nothing on any app that uses an interceptor, which is most of them.
//
// Three places a client sets headers, all static AST, all in the same bundle:
//   1. instance defaults   axios.create({headers:{…}}) · ky.create({headers:{…}})
//   2. mutable defaults    api.defaults.headers.common["X-Api-Key"] = k
//   3. interceptors        api.interceptors.request.use(c => c.headers.set("x-xsrf-token", t))
//                          ky hooks: {beforeRequest:[req => req.headers.set(…)]}
//                          new Headers() / h.append(…) inside a fetch wrapper
//
// A header NAME is a wire constant, it survives minification the same way a path or a body key does, and
// it is just as likely to be a backtick template literal as a quoted string, so both are accepted.
// Conditions are kept: `if (cfg.method !== 'GET') cfg.headers.set(CSRF, t)` means the header is non-GET only,
// which is a real fact about the contract and not noise.
let _SRC = "";                     // current chunk source, set before traversal (see main)
const _HDR_SET = new Set(["set", "append", "add"]);
function _hdrName(node, scope) {
  if (!node) return null;
  if (node.type === "StringLiteral") return node.value;
  if (node.type === "TemplateLiteral" && node.expressions.length === 0 && node.quasis.length === 1)
    return node.quasis[0].value.cooked ?? node.quasis[0].value.raw ?? null;
  if (node.type === "Identifier" && scope && scope.getBinding) {     // const CSRF = `x-xsrf-token`
    const b = scope.getBinding(node.name);
    const init = b && b.path && b.path.node && b.path.node.init;
    if (init) return _hdrName(init, b.scope);
  }
  return null;
}
// walk upward for an enclosing `if` whose test mentions `method`, that is the GET/non-GET style guard
function _hdrCondition(path) {
  let p = path, hops = 0;
  while (p && hops++ < 8) {
    if (p.node && p.node.type === "IfStatement" && p.node.test) {
      const t = generateSrc(p.node.test);
      if (t && /method/i.test(t)) return t.slice(0, 80);
    }
    if (p.node && p.node.type === "ConditionalExpression" && p.node.test) {
      const t = generateSrc(p.node.test);
      if (t && /method/i.test(t)) return t.slice(0, 80);
    }
    p = p.parentPath;
  }
  return null;
}
function generateSrc(node) {
  try { return node && node.start != null && node.end != null ? _SRC.slice(node.start, node.end) : null; }
  catch { return null; }
}
// ── PER-ENDPOINT CLIENT BINDING ─────────────────────────────────────────────────────────────────────
// Emitting headers at program level is not good enough: a bundle routinely builds SEVERAL instances with
// DIFFERENT config, and which one an endpoint uses decides whether it carries auth, cookies, and CSRF.
// One client builds two, `Yx.create({baseURL:dS,withCredentials:!0})` and `Yx.create({...,withCredentials:!1})`
//, so "the bundle sets x-xsrf-token" is not a fact about any particular request.
//
// The binding is three hops of ordinary variable assignment, all resolvable from babel's scope:
//     dS = "<baseURL>"
//     fS = Yx.create({baseURL: dS, withCredentials: true})      instance
//     hS = async e => await fS.request(e)                        wrapper  -> fS
//     gS = hS                                                    alias    -> hS -> fS
//     fS.interceptors.request.use(c => c.headers.set(`x-xsrf-token`, t))
// so an endpoint calling gS({...}) inherits fS's baseURL, cookie policy and interceptor headers.
//
// Returns { instances: name -> cfg, alias: name -> name } and resolve() follows aliases transitively.
function collectClients(ast) {
  const instances = new Map();      // varName -> {baseURL, withCredentials, headers:[], interceptors:[]}
  const alias = new Map();          // wrapperName -> targetName
  const nameOf = (n) => (n && n.type === "Identifier") ? n.name : null;
  const declName = (path) => {
    const p = path.parentPath;
    if (p && p.node.type === "VariableDeclarator" && p.node.id && p.node.id.type === "Identifier") return p.node.id.name;
    if (p && p.node.type === "AssignmentExpression" && p.node.left.type === "Identifier") return p.node.left.name;
    return null;
  };
  traverse(ast, {
    // instance construction: <lib>.create({...}), axios, ky, got, ofetch all use this shape
    CallExpression(path) {
      const c = path.node.callee;
      if (c && c.type === "MemberExpression" && c.property && c.property.name === "create") {
        const nm = declName(path);
        if (!nm) return;
        const cfg = { baseURL: null, withCredentials: null, headers: [], interceptors: [] };
        const arg = path.node.arguments[0];
        if (arg && arg.type === "ObjectExpression") {
          for (const p of arg.properties) {
            if (p.type !== "ObjectProperty" || !p.key) continue;
            const k = String(p.key.name ?? p.key.value);
            if (/^base(URL|Url|_url)$/.test(k)) {
              const r = strFromNode(p.value, path.scope, 0, null);
              if (r && r.s) cfg.baseURL = r.s;
            } else if (k === "withCredentials" || k === "credentials") {
              const v = p.value;
              cfg.withCredentials = v.type === "BooleanLiteral" ? v.value
                : (v.type === "UnaryExpression" && v.operator === "!" && v.argument && v.argument.type === "NumericLiteral")
                  ? v.argument.value === 0
                  : (v.type === "StringLiteral" ? v.value : null);
            } else if (k === "headers" && p.value && p.value.type === "ObjectExpression") {
              for (const q of p.value.properties)
                if (q.type === "ObjectProperty" && q.key) cfg.headers.push(String(q.key.name ?? q.key.value));
            }
          }
        }
        // `x.create({...})` is a generic factory shape, i18n, date, store and chart libs all use it. Only
        // treat it as an HTTP client when it carries HTTP evidence, else dozens of minified locals get
        // reported as "clients". An interceptor registration promotes an instance separately, below.
        const httpish = cfg.baseURL || cfg.headers.length ||
                        (cfg.withCredentials !== null && cfg.withCredentials !== undefined);
        if (httpish) instances.set(nm, cfg);
        return;
      }
      // interceptor registration: <inst>.interceptors.request.use(fn)
      if (c && c.type === "MemberExpression" && c.property && c.property.name === "use") {
        const src = generateSrc(c.object) || "";
        const m = /^([A-Za-z_$][\w$]*)\.interceptors\.request$/.exec(src);
        if (!m) return;
        const inst = m[1];
        const fn = path.node.arguments[0];
        if (!fn) return;
        const found = [];
        path.get("arguments.0").traverse({
          CallExpression(ip) {
            const ic = ip.node.callee;
            if (ic && ic.type === "MemberExpression" && ic.property && _HDR_SET.has(ic.property.name)) {
              const os = generateSrc(ic.object) || "";
              if (!/head(er|ers)/i.test(os)) return;
              const nm2 = _hdrName(ip.node.arguments[0], ip.scope);
              if (nm2) found.push({ name: nm2, condition: _interceptorGuard(ip) });
            }
          },
          AssignmentExpression(ip) {
            const l = ip.node.left;
            if (!l || l.type !== "MemberExpression") return;
            if (!/head(er|ers)/i.test(generateSrc(l.object) || "")) return;
            const nm2 = l.computed ? _hdrName(l.property, ip.scope) : (l.property && l.property.name);
            if (nm2) found.push({ name: String(nm2), condition: _interceptorGuard(ip) });
          },
        });
        if (found.length) {
          const cfg = instances.get(inst) || { baseURL: null, withCredentials: null, headers: [], interceptors: [] };
          cfg.interceptors.push(...found);
          instances.set(inst, cfg);
        }
      }
    },
    // wrapper / alias: `hS = e => fS.request(e)`  and  `gS = hS`
    VariableDeclarator(path) {
      const id = path.node.id, init = path.node.init;
      if (!id || id.type !== "Identifier" || !init) return;
      if (init.type === "Identifier") { alias.set(id.name, init.name); return; }
      if (init.type === "ArrowFunctionExpression" || init.type === "FunctionExpression") {
        const s = generateSrc(init) || "";
        const m = /([A-Za-z_$][\w$]*)\s*\.\s*(?:request|get|post|put|patch|delete|fetch)\s*\(/.exec(s);
        if (m) alias.set(id.name, m[1]);
      }
    },
  });
  const resolve = (name) => {
    let n = name, hops = 0;
    while (n && !instances.has(n) && alias.has(n) && hops++ < 12) n = alias.get(n);
    return instances.has(n) ? { name: n, cfg: instances.get(n) } : null;
  };
  return { instances, alias, resolve };
}
// an interceptor commonly early-returns for a verb: `if (c.method?.toUpperCase()==='GET') return c;`
// That guard means every header set AFTER it applies to non-GET only, a real contract fact.
function _interceptorGuard(ip) {
  let fnPath = ip.getFunctionParent && ip.getFunctionParent();
  if (!fnPath) return null;
  const body = fnPath.node.body;
  if (!body || body.type !== "BlockStatement") return null;
  for (const st of body.body) {
    if (st.type === "IfStatement" && st.test && st.consequent) {
      const cons = generateSrc(st.consequent) || "";
      const test = generateSrc(st.test) || "";
      if (/return/.test(cons) && /method/i.test(test)) return "skipped when: " + test.slice(0, 60);
    }
    if (st === ip.node) break;
  }
  return null;
}

// ── ENVIRONMENT CONFIG CONSTANTS ────────────────────────────────────────────────────────────────────
// A base URL that "comes from runtime config" is usually NOT a runtime dependency. The standard SPA shape
// is a static per-environment object baked into the bundle, selected by a key at boot:
//     CFG = { prod:{API_GATEWAY_URL:"https://…"}, stage:{API_GATEWAY_URL:"https://…"} }
//     {API_GATEWAY_URL: base} = CFG[pickEnv()]
// so `base` looks unresolvable at the call site while every possible value sits in the same file. Reading
// the object gives you MORE than resolving the one live value: it enumerates staging and gamma hosts that
// production never touches. Any *_URL / *_ORIGIN / *_ENDPOINT / *_HOST / *_ROOT / *_BASE key whose value is
// a URL-shaped literal is collected, wherever it appears.
const _CFG_KEY = /(_URL|_URI|_ORIGIN|_ENDPOINT|_HOST|_ROOT|_BASE|_GATEWAY|_DOMAIN)$/i;
function collectConfigUrls(ast) {
  const out = new Map();
  traverse(ast, {
    ObjectProperty(path) {
      const k = path.node.key && String(path.node.key.name ?? path.node.key.value ?? "");
      if (!k || !_CFG_KEY.test(k)) return;
      const v = path.node.value;
      let val = null;
      if (v.type === "StringLiteral") val = v.value;
      else if (v.type === "TemplateLiteral" && v.expressions.length === 0 && v.quasis.length === 1)
        val = v.quasis[0].value.cooked ?? v.quasis[0].value.raw;
      if (!val) return;
      if (!/^(https?:\/\/|\/)[^\s"'`]{1,200}$/.test(val)) return;
      const key = k + "\u0000" + val;
      if (!out.has(key)) out.set(key, { name: k, value: val });
    },
  });
  return [...out.values()];
}

// A lowercase `x-`-prefixed dashed name IS a header. The general rule below rejects lowercase-dashed
// names so a CSS-ish map (`s["font-size"] = ...`) does not register, and that costs real ones.
// Surveyed across the corpora, 273 distinct `x-*` keys appear and nearly all are genuine HTTP headers:
// `x-grafana-alerting-folder-uid`, `x-algolia-api-key`, `x-airtable-page-load-id`, `x-b3-traceid`,
// `x-2fa-code`, `x-sentry-rate-limits`. Measured, this recovers `x-amz-user-agent` on Grafana and
// `x-deployment-id` on Clerk.
//
// With one exception a name test cannot see: a DOM ATTRIBUTE. Popper.js writes
// `e.attributes["x-out-of-boundaries"] = ""` onto a tooltip element, and that is markup, not a request
// header. `attributes`, `dataset`, `style` and `classList` are property names fixed by the DOM rather
// than chosen by the application, so a minifier cannot rename them, the same reason this file anchors
// on wire constants instead of identifiers. Excluding writes into those leaves 2 recovered and 0
// invented across the corpora.
//
// `x-axis-title` and `x-axis-dropzone` also exist in these bundles and would match the name test, but
// never reach it: a chart config is an object literal, and this path requires a member assignment.
const _XHDR = (k) => /^x-[a-z0-9]+(-[a-z0-9]+)+$/.test(String(k));
const _DOM_CONTAINER = /(^|\.)(attributes|dataset|style|classList)$/;
const _isDomWrite = (objNode) => _DOM_CONTAINER.test((generateSrc(objNode) || "").trim());

function collectClientHeaders(ast) {
  const out = new Map();                       // name -> {name, where, condition}
  // `headers:` in UI code means TABLE COLUMN headers, and that object's keys (role, publicKey,
  // userAssignments…) are not HTTP headers. Discriminate on the shape of the NAME, which is a wire
  // constant with a real convention, rather than on context, context is unreliable after minification.
  // A genuine header either carries a dash, is an x-/sec-/sech- extension, or is a registered field name.
  const STD = new Set(["accept", "accept-encoding", "accept-language", "authorization", "cache-control",
    "content-type", "content-length", "content-disposition", "cookie", "origin", "referer", "user-agent",
    "if-match", "if-none-match", "range", "prefer", "from", "host", "link", "location", "te", "dnt"]);
  // Headers the browser FORBIDS script from setting. If we "found" one, we did not find a header the app
  // sends, we found a library's internal constant list (axios ships exactly such an array) or a response
  // header name. Listing `host` and `te` as part of a request contract is worse than listing nothing,
  // because it invites someone to build a request around fields that can never be sent.
  const FORBIDDEN = new Set(["host", "connection", "content-length", "date", "dnt", "expect", "keep-alive",
    "origin", "referer", "te", "trailer", "transfer-encoding", "upgrade", "via", "proxy-authorization",
    "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "sec-fetch-user", "access-control-request-method",
    "access-control-request-headers",
    // response-side names that appear in the same library tables
    "location", "link", "server", "set-cookie", "etag", "age", "vary", "allow", "expires",
    "last-modified", "content-encoding", "content-disposition", "www-authenticate", "retry-after",
    // real request headers that application code essentially never sets, but which DO appear in the
    // header tables libraries ship (axios enumerates these), so a hit is a false positive far more often
    // than it is a finding
    "from", "range", "if-match", "if-none-match", "if-modified-since", "max-forwards", "pragma",
    // the browser sets these itself and refuses script overrides
    "user-agent", "accept-encoding", "accept-charset", "cookie2", "content-transfer-encoding",
    // rate-limit / tracing families that only ever come back in a RESPONSE
    "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset", "x-sentry-rate-limits",
    "x-request-id", "x-correlation-id", "x-served-by", "x-cache", "x-frame-options",
    "x-content-type-options", "x-xss-protection", "strict-transport-security"]);
  // BCP-47 language tags (cs-CZ, pt-BR) match "Title-Case with a dash" exactly and are not headers.
  const LANGTAG = /^[a-z]{2,3}-[A-Z]{2}$|^[a-z]{2,3}-[A-Z][a-z]{3}$/;
  const looksLikeHeader = (n) => {
    const k = n.toLowerCase();
    if (FORBIDDEN.has(k) || LANGTAG.test(n)) return false;
    if (STD.has(k)) return true;
    if (/^(x|sec|sech|dpop|traceparent|tracestate|baggage)[-_]/.test(k)) return true;
    // "any dashed name" also matches CSS classes and kebab-case data keys (scrolled-desktop, is-active),
    // which produced phantom headers. A custom header that is neither registered nor x-/sec- prefixed is
    // conventionally Title-Cased on the wire, so require evidence of that rather than accepting any dash.
    return n.includes("-") && !/\s/.test(n) && /[A-Z]/.test(n);
  };
  const add = (name, where, condition) => {
    if (!name || !/^[A-Za-z][A-Za-z0-9_-]{1,60}$/.test(name)) return;
    if (!looksLikeHeader(name)) return;
    const k = name.toLowerCase();
    if (!out.has(k)) out.set(k, { name, where, condition: condition || null });
  };
  traverse(ast, {
    // 1 + 2: `c.headers.set(NAME, v)` / `h.append(NAME, v)` / `x.headers[NAME] = v`
    CallExpression(path) {
      const c = path.node.callee;
      if (c && c.type === "MemberExpression" && c.property && _HDR_SET.has(c.property.name)) {
        const obj = c.object;
        const objSrc = generateSrc(obj) || "";
        const named = /head(er|ers)/i.test(objSrc) ||
                      (obj && obj.type === "Identifier" && /^h$|head/i.test(obj.name));
        const n = _hdrName(path.node.arguments[0], path.scope);
        // Same lesson as the assignment path: requiring the RECEIVER to be named "headers" is a gate that
        // minification always defeats, axios ships `l.set("Authorization","Basic "+btoa(…))`, where `l` is
        // a Headers object with a mangled name. The header NAME is the wire constant a minifier cannot
        // touch, so a registered or Title-Cased-dashed name is evidence enough on its own.
        const byName = n && (STD.has(String(n).toLowerCase()) || _XHDR(String(n)) ||
                             (/[A-Z]/.test(String(n)) &&
                              /^[A-Za-z][A-Za-z0-9]*(-[A-Za-z0-9]+)+$/.test(String(n))));
        if (n && (named || byName)) {
          add(n, /interceptor|beforeRequest/i.test(generateSrc(path.parentPath?.parentPath?.node) || "")
                 ? "interceptor" : "client", _hdrCondition(path));
        }
      }
    },
    AssignmentExpression(path) {
      const l = path.node.left;
      if (!l || l.type !== "MemberExpression") return;
      const src = generateSrc(l.object) || "";
      const n = l.computed ? _hdrName(l.property, path.scope) : (l.property && l.property.name);
      if (!n) return;
      if (/head(er|ers)/i.test(src)) { add(n, "defaults", _hdrCondition(path)); return; }
      // Requiring the RECEIVER to be named "headers" is a gate that minification always defeats: the same
      // code ships as `o["X-CSRFToken"]=…`, and `o` matches nothing. Identifier names are exactly what a
      // minifier destroys, so anchor on the header NAME instead, a wire constant the minifier cannot touch.
      // Tightened against lowercase-dashed keys so CSS-ish maps (s["font-size"]=…) do not register.
      const k = String(n);
      if (STD.has(k.toLowerCase()) || (_XHDR(k) && !_isDomWrite(l.object)) ||
          (/[A-Z]/.test(k) && /^[A-Za-z][A-Za-z0-9]*(-[A-Za-z0-9]+)+$/.test(k)))
        add(k, "defaults", _hdrCondition(path));
    },
    // 3: any object literal under a `headers:` key, instance defaults and per-call alike
    ObjectProperty(path) {
      const k = path.node.key && (path.node.key.name ?? path.node.key.value);
      if (String(k).toLowerCase() !== "headers") return;
      let v = path.node.value;
      if (v && v.type === "Identifier" && path.scope.getBinding) {
        const b = path.scope.getBinding(v.name);
        if (b && b.path && b.path.node && b.path.node.init) v = b.path.node.init;
      }
      if (!v || v.type !== "ObjectExpression") return;
      for (const p of v.properties) {
        if (p.type !== "ObjectProperty" || !p.key) continue;
        const n = p.computed ? _hdrName(p.key, path.scope) : (p.key.name ?? p.key.value);
        if (!n) continue;
        // axios nests one level: defaults.headers.{common,get,post,…}.{RealHeader: v}
        if (/^(common|get|post|put|patch|delete|head|options)$/i.test(String(n)) &&
            p.value && p.value.type === "ObjectExpression") {
          for (const q of p.value.properties) {
            if (q.type !== "ObjectProperty" || !q.key) continue;
            const qn = q.computed ? _hdrName(q.key, path.scope) : (q.key.name ?? q.key.value);
            if (qn) add(String(qn), "defaults", _hdrCondition(path));
          }
          continue;
        }
        add(String(n), "client", _hdrCondition(path));
      }
    },
  });
  return [...out.values()];
}

// resolve a headers object → the contract-relevant headers (content-type distinguishes json/form/multipart;
// accept distinguishes SSE/event-stream from json). Follows one identifier hop. Literal string values only.
// The object a function RETURNS, so `headers: buildHeaders(ctx)` can be resolved to the header set it makes.
function _returnedObject(fn, depth = 0) {
  if (!fn || depth > 3) return null;
  if (fn.type === "ArrowFunctionExpression" && fn.body && fn.body.type === "ObjectExpression")
    return fn.body;                                        // concise body: () => ({...})
  const body = fn.body && fn.body.type === "BlockStatement" ? fn.body.body : null;
  if (!body) return null;
  for (let i = body.length - 1; i >= 0; i--) {              // last return wins; header builders end with one
    const st = body[i];
    if (st.type === "ReturnStatement" && st.argument) {
      if (st.argument.type === "ObjectExpression") return st.argument;
      // `return null == e ? e : {a: e.a, b: e.b}`, a generated serializer guards its null case and
      // returns the real shape on the other branch. Reading only a bare ObjectExpression return gave up on
      // every openapi-generator client, which is the dominant shape in a modern bundle.
      if (st.argument.type === "ConditionalExpression") {
        for (const br of [st.argument.consequent, st.argument.alternate])
          if (br && br.type === "ObjectExpression") return br;
      }
      if (st.argument.type === "CallExpression") return null;
    }
  }
  return null;
}

// EVERY header this request carries, not just content-type.
//
// A request we cannot reproduce is useless at audit time, and "how does it authenticate" is not a separate
// question from "what headers does it send", a session cookie and a bearer token are both just headers. The
// hard part is that real apps do not write them at the call site: claude.ai ships
//     fetch(url, {credentials:"include", headers: X(e)})
// where X() returns {"anthropic-client-platform":…, "anthropic-device-id":…, …}. Stopping at the call left
// every endpoint with zero headers and no idea how the app authenticates. So an Identifier is resolved to
// its binding, a CallExpression to the object its callee returns, and spreads are followed one level.
// `xhr.setRequestHeader("X-Requested-With", ...)` states a header as plainly as an object literal does,
// but it is a SEPARATE STATEMENT from the .open() call the sink is recorded at, so reading only .open's
// arguments finds nothing. Walk the receiver's other calls, exactly as formDataAppends does for a body.
function setRequestHeaders(node, scope) {
  if (!node || node.type !== "Identifier") return null;
  const b = scope && scope.getBinding && scope.getBinding(node.name);
  if (!b) return null;
  const out = {};
  for (const ref of (b.referencePaths || [])) {
    const call = ref.parentPath && ref.parentPath.parentPath;
    if (!call || !call.isCallExpression || !call.isCallExpression()) continue;
    const callee = call.node.callee;
    if (!callee || callee.type !== "MemberExpression") continue;
    if ((callee.property && (callee.property.name || callee.property.value)) !== "setRequestHeader") continue;
    const a = call.node.arguments || [];
    if (a[0] && a[0].type === "StringLiteral") out[a[0].value] = true;
  }
  return Object.keys(out).length ? out : null;
}

// The body of an XHR is in `.send(data)`, which is a DIFFERENT STATEMENT from the `.open(method, url)`
// the sink is recorded at. Reading open's arguments recovers the method and the URL and can never recover
// the body, a corpus sample found POSTs to /_sec/verify and /api/web/v2/tracker sitting bodyless for
// exactly this reason. Walk the receiver's other calls, the same way setRequestHeaders does.
function xhrSendBody(node, scope) {
  if (!node || node.type !== "Identifier") return null;
  const b = scope && scope.getBinding && scope.getBinding(node.name);
  if (!b) return null;
  for (const ref of (b.referencePaths || [])) {
    const call = ref.parentPath && ref.parentPath.parentPath;
    if (!call || !call.isCallExpression || !call.isCallExpression()) continue;
    const callee = call.node.callee;
    if (!callee || callee.type !== "MemberExpression") continue;
    if ((callee.property && (callee.property.name || callee.property.value)) !== "send") continue;
    const a0 = (call.node.arguments || [])[0];
    if (!a0) continue;
    const ks = bodyKeys(a0, call.scope, 0);
    if (ks && ks.length) return ks;
  }
  return null;
}

function headerInfo(node, scope, depth = 0) {
  let n = node;
  if (!n || depth > 3) return null;
  // `headers: new Headers({Authorization: bearer})` is the spec-blessed way to build them and it reached
  // here as a NewExpression, matched nothing, and returned null. The header NAMES are literals inside;
  // only the wrapper was in the way.
  if (n.type === "NewExpression" && n.callee && n.callee.name === "Headers")
    return n.arguments && n.arguments[0] ? headerInfo(n.arguments[0], scope, depth + 1) : null;
  if (n.type === "Identifier") {
    const b = scope && scope.getBinding && scope.getBinding(n.name);
    // `const h = new Headers(); h.set("X-Api-Key", k); fetch(u, {headers: h})`, the headers are
    // never written as an object, they are assembled on the binding. `new Headers({…})` already
    // worked because the names sit inside the wrapper; this spelling returned null, so the endpoint
    // reported no headers at all while the names were visible two statements above it. Same shape
    // as searchParams.set on a URL binding, which had the same hole.
    if (b && b.path && b.path.node && b.path.node.init &&
        b.path.node.init.type === "NewExpression" &&
        b.path.node.init.callee && b.path.node.init.callee.name === "Headers") {
      const built = {};
      for (const rp of (b.referencePaths || [])) {
        const mp = rp.parentPath;                                  // h.set
        if (!mp || !mp.node || mp.node.type !== "MemberExpression" || mp.node.object !== rp.node) continue;
        const mName = mp.node.property && mp.node.property.name;
        if (!/^(set|append)$/.test(String(mName || ""))) continue;
        const call = mp.parentPath;
        if (!call || !call.node || call.node.type !== "CallExpression") continue;
        const a0 = call.node.arguments[0];
        if (!a0 || a0.type !== "StringLiteral") continue;
        const v = call.node.arguments[1];
        const vs = v ? strFromNode(v, call.scope, 0, null) : null;
        built[a0.value.toLowerCase()] = vs && vs.s ? vs.s : true;
      }
      const inner = (b.path.node.init.arguments || [])[0]
        ? headerInfo(b.path.node.init.arguments[0], scope, depth + 1) : null;
      const merged = Object.assign({}, inner || {}, built);
      if (Object.keys(merged).length) return merged;
    }
    if (b && b.path && b.path.node) {
      const init = b.path.node.init || (b.path.node.type === "FunctionDeclaration" ? b.path.node : null);
      if (init) {
        if (init.type === "ObjectExpression") n = init;
        else if (init.type === "ArrowFunctionExpression" || init.type === "FunctionExpression" ||
                 init.type === "FunctionDeclaration") n = _returnedObject(init) || n;
        else return headerInfo(init, b.scope, depth + 1);
      }
    }
  }
  if (n.type === "CallExpression") {                        // headers: buildHeaders(ctx)
    // A merging helper is HANDED the headers: `headers: Y([{"anthropic-beta": b}, opts?.headers])`.
    // Read them from what it is given before guessing from what it returns. Stainless's buildHeaders
    // returns {values, nulls}, its own bookkeeping, and that was reported as two header names.
    const given = [];
    for (const a of n.arguments || []) {
      if (a && a.type === "ObjectExpression") given.push(a);
      else if (a && a.type === "ArrayExpression")
        for (const el of a.elements || []) if (el && el.type === "ObjectExpression") given.push(el);
    }
    if (given.length) {
      const merged = {};
      for (const o of given) Object.assign(merged, headerInfo(o, scope, depth + 1) || {});
      // `{apikey: k, customHeaders: h}`: the second key holds MORE headers, it is not one. No
      // header is named "headers", so a key ending in it is a nested bag.
      for (const k of Object.keys(merged)) if (/headers?$/i.test(k)) delete merged[k];
      if (Object.keys(merged).length) return merged;
    }
    const c = n.callee;
    const name = c && (c.name || (c.property && c.property.name));
    if (name && scope && scope.getBinding) {
      const b = scope.getBinding(c.name || "");
      const fn = b && b.path && b.path.node
                 ? (b.path.node.init || (b.path.node.type === "FunctionDeclaration" ? b.path.node : null))
                 : null;
      const ret = fn ? _returnedObject(fn) : null;
      if (ret) n = ret;
    }
  }
  if (!n || n.type !== "ObjectExpression") return null;
  const h = {};
  for (const p of n.properties) {
    if (p.type === "SpreadElement" || p.type === "SpreadProperty") {
      const sub = headerInfo(p.argument, scope, depth + 1);   // {...commonHeaders, "x-thing": v}
      if (sub) Object.assign(h, sub);
      continue;
    }
    if (p.type !== "ObjectProperty" || !p.key) continue;
    const raw = p.computed ? _hdrName(p.key, scope) : (p.key.name ?? p.key.value);
    if (raw === undefined || raw === null) continue;
    const k = String(raw).toLowerCase();
    if (!/^[a-z][a-z0-9-]{1,60}$/.test(k)) continue;         // a header name, not an options field
    // The VALUE is worth as much as the name and was being thrown away for anything that was not a bare
    // string literal, which is exactly the interesting case. `Authorization: "Bearer " + token` recorded
    // as "Authorization: null" tells you nothing; as "Bearer {token}" it tells you the scheme. Traced with
    // the same resolver used for URLs, falling back to the value classifier used for bodies.
    let hv = null;
    if (p.value) {
      try {
        const r = strFromNode(p.value, scope, 0);
        if (r && r.s && r.s !== "{EXPR}") hv = r.s;
      } catch {}
      if (hv === null) {
        try {
          const cls = classifyValue(p.value, scope, 0);
          if (cls && cls !== "?" && cls !== "runtime") hv = cls;
        } catch {}
      }
    }
    h[k] = hv;
  }
  return Object.keys(h).length ? h : null;
}

// react-query mutation body: `De(url,"PATCH",{transformVariables: v => ({...})})`, the request body is the
// mutation's variables run through transformVariables, so the fields live in that fn's RETURN object (the wire
// body) or its PARAM destructuring (the input shape). Common in claude.ai and any react-query app. Returns
// {body_keys, body_src} or null.
function transformBody(node, scope, depth = 0) {
  let fn = node;
  if (fn && fn.type === "Identifier" && depth < 3) {          // named fn (transformVariables: EC) → resolve it
    const b = scope && scope.getBinding && scope.getBinding(fn.name);
    if (b && b.path && b.path.node) { const init = b.path.node.init; if (init) return transformBody(init, b.scope, depth + 1); }
    return null;
  }
  if (!fn || !(fn.type === "ArrowFunctionExpression" || fn.type === "FunctionExpression" || fn.type === "FunctionDeclaration")) return null;
  let ret = fn.body;                                           // 1) the RETURN object = the actual wire body
  if (ret && ret.type === "BlockStatement") { const rs = ret.body.find(s => s.type === "ReturnStatement"); ret = rs ? rs.argument : null; }
  if (ret) { const bk = bodyKeys(ret, scope); if (bk && bk.length) return { body_keys: bk, body_src: bodyFields(ret, scope) }; }
  const p0 = fn.params && fn.params[0];                        // 2) fallback: the PARAM destructuring = input fields
  if (p0 && p0.type === "ObjectPattern") {
    const keys = [], src = {};
    for (const pr of p0.properties) if (pr.type === "ObjectProperty" && pr.key) { const k = pr.key.name ?? pr.key.value; keys.push(k); src[k] = "input:" + k; }
    if (keys.length) return { body_keys: keys, body_src: src };
  }
  return null;
}

// manual node walk collecting `name.prop` member accesses (the response fields a callback reads off its data param)
function collectMemberAccesses(node, name, out, depth) {
  if (!node || typeof node !== "object" || depth > 60 || out.size > 60) return;
  if (node.type === "MemberExpression" && !node.computed && node.object &&
      node.object.type === "Identifier" && node.object.name === name && node.property && node.property.name) {
    const f = node.property.name;
    if (!["then", "catch", "map", "filter", "forEach", "length", "data"].includes(f)) out.add(f);
  }
  for (const k in node) {
    if (k === "loc" || k === "start" || k === "end" || k === "leadingComments" || k === "trailingComments") continue;
    const v = node[k];
    if (Array.isArray(v)) { for (const c of v) collectMemberAccesses(c, name, out, depth + 1); }
    else if (v && typeof v.type === "string") collectMemberAccesses(v, name, out, depth + 1);
  }
}
// RESPONSE schema from a react-query callback (onSuccess/select/onData/transformResponse), its first param IS the
// response; destructured keys or `data.field` accesses = the response fields. 869 onSuccess + 155 select on claude.ai.
function responseFromCallback(node, scope, depth = 0) {
  let fn = node;
  if (fn && fn.type === "Identifier" && depth < 3) {
    const b = scope && scope.getBinding && scope.getBinding(fn.name);
    if (b && b.path && b.path.node && b.path.node.init) return responseFromCallback(b.path.node.init, b.scope, depth + 1);
    return null;
  }
  if (!fn || !(fn.type === "ArrowFunctionExpression" || fn.type === "FunctionExpression")) return null;
  const p0 = fn.params && fn.params[0];
  if (!p0) return null;
  const fields = new Set();
  if (p0.type === "ObjectPattern") {
    for (const pr of p0.properties) if (pr.type === "ObjectProperty" && pr.key) { const k = pr.key.name ?? pr.key.value; if (k && k !== "data") fields.add(k); }
  } else if (p0.type === "Identifier") {
    collectMemberAccesses(fn.body, p0.name, fields, 0);
  }
  return fields.size ? [...fields].slice(0, 1000) : null;
}

// pull an options object's method + body key-names, given the AST node of the 2nd arg (or axios single-config)

// ── THE BASE URL IS NOT ALWAYS THE DOCUMENT ────────────────────────────────────────────────────────────
// A relative path at a real transport sink (`fetch("/x")`) is resolved by the browser against the
// DOCUMENT's origin. That rule is right, and analyze.py applies it. It is wrong the moment the string is
// not a URL but a FRAGMENT handed to a client that prepends a base:
//
//     sendRequest({ apiHost: APIHost.Beta, method: "post", url: "/v1/compliance-check", data })
//
// `/v1/compliance-check` never reaches the URL resolver. Applying the document rule anyway files it
// under the frontend host, measured on one target: 821 API endpoints, including `DELETE /v4/items/{id}`,
// filed under a Webflow marketing site that serves none of them, and 857 endpoints filed under two hosts at
// once. A tester following that map gets a 404 and concludes the endpoint does not exist.
//
// Detection is STRUCTURAL, not a hunt for the name `apiHost`: in an options object that already carries a
// url, any sibling that resolves to a non-path string, or reads a property off a constant table, is
// selecting a base. The exclusion list below is the stable fetch/axios/react-query option vocabulary,
// a fixed language-level set, not target-specific naming, because without it `method:"post"` and
// `credentials:"include"` are also non-path strings.
const _REQ_OPT_KEYS = new Set([
  "method", "url", "endpoint", "path", "route", "uri", "body", "data", "json", "params", "query",
  "searchParams", "headers", "credentials", "withCredentials", "mode", "signal", "timeout", "cache",
  "responseType", "redirect", "referrer", "referrerPolicy", "integrity", "keepalive", "duplex",
  "onSuccess", "onError", "onData", "onSettled", "select", "transformResponse", "transformVariables",
  "variables", "validateStatus", "paramsSerializer", "onUploadProgress", "onDownloadProgress",
  "urlForSentry", "urlForAnalytics", "config", "retry", "retries", "priority", "next",
]);

function baseHint(node, scope) {
  if (!node || node.type !== "ObjectExpression") return null;
  for (const p of node.properties) {
    if (p.type !== "ObjectProperty" || !p.key) continue;
    const key = String(p.key.name || p.key.value || "");
    if (!key || _REQ_OPT_KEYS.has(key)) continue;
    const v = p.value;
    // `apiHost: APIHost.Beta`, the constant table may be unresolvable in a minified bundle, but the
    // PROPERTY name survives minification (it is a key, not a binding) and identifies the service.
    if (v && v.type === "MemberExpression" && !v.computed && v.property) {
      const hint = String(v.property.name ?? v.property.value ?? "");
      if (hint) return { key, hint, resolved: false };
    }
    const r = strFromNode(v, scope, 0);
    if (r && !r.dyn && r.s && r.s !== "{EXPR}" && r.s.length <= 120 && !r.s.startsWith("/")) {
      // a base is a host, an origin, or a service token, never a path, never a verb
      if (!/^(get|post|put|patch|delete|head|options|query)$/i.test(r.s)) {
        return { key, hint: r.s, resolved: true };
      }
    }
  }
  return null;
}


// ── THE APPLICATION OFTEN STATES ITS OWN ROUTE TEMPLATE, NEXT TO THE CALL ──────────────────────────────
// Observability tooling made this a habit: reporting a concrete URL as a transaction name explodes
// cardinality, so clients pass a parameterised twin alongside the real one.
//
//     url: "/billing/".concat(e, "/credit/").concat(t, "/cancel"),
//     urlForSentry: "/billing/:id/credit/:id/cancel"
//
// Matched by VALUE, never by key name. A sibling string whose slot-erased form equals the slot-erased
// form of the URL we reconstructed IS that route's template, whatever the key is called, urlForSentry,
// routeName, transactionName, pathPattern, or a convention nobody has invented yet. (The slot-eraser
// already existed: analyze.py uses the same normalisation to build its merge key.)
//
// It is a CORROBORATOR, not an authority, and the distinction is load-bearing. Measured on one bundle,
// 9 call sites have the two disagreeing structurally, including
//     url: "/business/api-key/deactivate"   vs   urlForSentry: "/business/api-keys/deactivate"
// a typo in the application's own code. The real URL is what gets sent; the declared twin is worth having
// for its SLOT NAMES, which is the part the reconstruction cannot know. `:businessId` versus `:invoiceId`
// is exactly what tells an authorization sweep which id belongs in which hole, substituting the wrong
// kind produces a 400 on the baseline, which is UNTESTED and reads like "denied" to anything careless.
// The query string is not part of the route, and a declared twin routinely omits it, comparing with it
// attached reported `/v3/contacts?customerId={}` against `/v3/contacts` as a contradiction when the two
// agree about every path segment. Compare paths; a difference in query is not a conflict.
const _slotErase = (t) => String(t || "")
  .split("?")[0].split("#")[0]
  .replace(/\{[^}]*\}/g, "{}")
  .replace(/(^|\/):[A-Za-z_]\w*/g, "$1{}")
  .replace(/\/+$/, "");

function declaredTemplate(node, scope, urlTpl, urlKey) {
  if (!node || node.type !== "ObjectExpression" || !urlTpl) return null;
  const want = _slotErase(urlTpl);
  if (!want.includes("/")) return null;
  let conflict = null;
  for (const p of node.properties) {
    if (p.type !== "ObjectProperty" || !p.key) continue;
    const key = String(p.key.name ?? p.key.value ?? "");
    if (!key || key === urlKey) continue;
    const v = p.value;
    let lit = null;
    if (v && v.type === "StringLiteral") lit = v.value;
    else if (v && v.type === "TemplateLiteral" && v.expressions.length === 0 && v.quasis.length === 1)
      lit = v.quasis[0].value.cooked ?? v.quasis[0].value.raw ?? null;
    if (!lit || !lit.startsWith("/")) continue;
    const got = _slotErase(lit);
    if (got === want) return { key, template: lit, agrees: true };
    // same shape, different text: the app contradicts itself. Worth recording, never worth following.
    if (got.split("/").length === want.split("/").length && !conflict)
      conflict = { key, template: lit, agrees: false };
  }
  return conflict;
}

// Carry the slot NAMES from a declared template onto a reconstructed one, without letting the declared
// text override what is actually sent.
function nameSlots(urlTpl, declTpl) {
  if (!urlTpl || !declTpl) return urlTpl;
  const qs = String(urlTpl).indexOf("?");
  const tail = qs >= 0 ? String(urlTpl).slice(qs) : "";          // keep the query exactly as reconstructed
  const u = (qs >= 0 ? String(urlTpl).slice(0, qs) : String(urlTpl)).split("/");
  const d = String(declTpl).split("?")[0].split("/");
  if (u.length !== d.length) return urlTpl;
  const out = u.map((seg, i) => {
    const dn = d[i] || "";
    if (!/^\{.*\}$/.test(seg)) return seg;                       // a literal segment stays literal
    const m = dn.match(/^:([A-Za-z_]\w*)$/) || dn.match(/^\{([A-Za-z_]\w*)\}$/);
    return m ? `{${m[1]}}` : seg;                                 // name it only if the twin names it
  });
  return out.join("/") + tail;
}

function optsInfo(node, scope) {
  const info = { method: null, body_keys: null };
  if (!node || node.type !== "ObjectExpression") return info;
  for (const p of node.properties) {
    if (p.type !== "ObjectProperty" || !p.key) continue;
    const key = p.key.name || p.key.value;
    // Minifiers routinely re-emit short strings as BACKTICK template literals (`GET`), which parse as
    // TemplateLiteral, not StringLiteral. Accepting only StringLiteral here silently missed the method on
    // EVERY call in a Vite-built bundle, 163 endpoints defaulted to GET, including real DELETEs and PATCHes.
    // strFromNode already handles TemplateLiteral for the `url` field; this is the same node type, ten lines up.
    if (key === "method") {
      const v = p.value;
      if (v.type === "StringLiteral") info.method = v.value.toUpperCase();
      else if (v.type === "TemplateLiteral" && v.expressions.length === 0 && v.quasis.length === 1)
        info.method = (v.quasis[0].value.cooked ?? v.quasis[0].value.raw ?? "").toUpperCase() || null;
    }
    if ((key === "body" || key === "data" || key === "json") && p.value) {
      info.has_body_slot = true;                               // the call DOES send a body (resolved or not)
      info.body_keys = bodyKeys(p.value, scope); info.body_src = bodyFields(p.value, scope);
      info.body_schema = bodySchema(p.value, scope);           // the NESTED shape + literal-derived types
      // …and HOW it is encoded, which decides whether the keys mean `a=1` or {"a":1}.
      info.body_encoding = bodyEncoding(p.value, scope);
      if (key === "json" && !info.body_encoding) info.body_encoding = "json";   // the option names it
    }
    if ((key === "params" || key === "query" || key === "searchParams") && p.value)
      info.query_schema = queryInfo(p.value, scope);            // URL parameters, previously dropped entirely
    // `url` is not the only key that names the target: custom clients and error reporters use endpoint/path/
    // route/uri. claude.ai's error reporter carries endpoint:"/v1/mobile/appendMessage", a real route we were
    // throwing away. First one wins, so an explicit `url` still takes precedence.
    if ((key === "url" || key === "endpoint" || key === "path" || key === "route" || key === "uri") &&
        p.value && !info._url) {
      const cand = resolveUrl(p.value, scope);
      if (key === "url" || LOOKS_PATH(cand.template) || /^https?:\/\//.test(cand.template)) {
        info._url = cand; info._urlKey = key;
      }
    }
    if (key === "headers" && p.value) {
      info.headers = headerInfo(p.value, scope);
      // …and the content-type's VALUE, which is what decides the body encoding when the body itself was
      // handed in as a parameter and its construction is nowhere near the call site.
      const ct = mutatedHeaderValue(p.value, scope, "content-type");
      if (ct) info.content_type = ct;
    }
    // `credentials` decides whether the browser attaches cookies at all, which for a cookie-session app IS
    // the auth mechanism. Without it a replayed request silently goes out unauthenticated.
    if (key === "credentials" && p.value && p.value.type === "StringLiteral") info.credentials = p.value.value;
    // axios spells the same fact `withCredentials: true`. It answers the identical question, does the browser
    // attach cookies, so it belongs in the same field rather than being dropped for using the other library's name.
    // …and a minifier writes `true` as `!0`, so matching only BooleanLiteral finds it in source and never in a
    // shipped bundle, precisely the case that matters.
    if (key === "withCredentials" && p.value) {
      const v = p.value;
      const b = v.type === "BooleanLiteral" ? v.value
              : (v.type === "UnaryExpression" && v.operator === "!" && v.argument &&
                 v.argument.type === "NumericLiteral") ? !v.argument.value : null;
      if (b !== null) info.credentials = b ? "include" : "omit";
    }
    if (key === "mode" && p.value && p.value.type === "StringLiteral") info.mode = p.value.value;
    if ((key === "transformVariables" || key === "variables") && p.value) info._tv = p.value;   // react-query mutation body
    if ((key === "onSuccess" || key === "select" || key === "onData" || key === "transformResponse" || key === "onSettled") && p.value && !info.response_keys) {
      const rk = responseFromCallback(p.value, scope);        // response schema from the callback's data param
      if (rk) info.response_keys = rk;
    }
  }
  if (!info.body_keys && info._tv) {                          // no direct body → derive it from the mutation transform
    const tb = transformBody(info._tv, scope);
    if (tb) { info.body_keys = tb.body_keys; info.body_src = tb.body_src; }
  }
  // DESCRIPTOR PASSTHROUGH. Everything above reads a fixed vocabulary, body/data/json, params/query,
  // url/endpoint/path. That vocabulary is a bet on how a client spells things, and the bet loses silently.
  //
  // Keycloak's admin client declares its whole API as a table:
  //     makeRequest({method:"POST", path:"/{id}/role-mappings/clients/{clientUniqueId}",
  //                  urlParamKeys:["id","clientUniqueId"], payloadKey:"roles"})
  // We read `method` and `path`, recognised neither of the other two, and emitted body_keys:null for all 307
  // of its endpoints, while `urlParamKeys` appears 300 times and `payloadKey` 21 times in the same bundle,
  // in objects this function is already walking. The contract was parsed and thrown away.
  //
  // The mistake was interpreting instead of recording, so this records. Any sibling key with a literal value
  // is kept verbatim under `descriptor`; deciding what `payloadKey` MEANS is the consumer's job, and a
  // consumer can learn a new framework's vocabulary without this file knowing anything about it.
  const KNOWN = new Set(["method", "body", "data", "json", "params", "query", "searchParams",
    "url", "endpoint", "path", "route", "uri", "headers", "credentials", "withCredentials",
    "mode", "transformVariables", "variables", "onSuccess", "select", "onData",
    "transformResponse", "onSettled"]);
  for (const p of node.properties) {
    if (p.type !== "ObjectProperty" || !p.key) continue;
    const key = String(p.key.name ?? p.key.value ?? "");
    if (!key || KNOWN.has(key)) continue;
    const v = p.value;
    let val = null;
    if (v.type === "StringLiteral") val = v.value;
    else if (v.type === "BooleanLiteral") val = v.value;
    else if (v.type === "ArrayExpression") {
      const els = v.elements.filter((e) => e && e.type === "StringLiteral").map((e) => e.value);
      if (els.length) val = els;
    }
    if (val === null) continue;
    if (typeof val === "string" && val.length > 200) continue;   // a blob, not a contract
    (info.descriptor ||= {})[key] = val;
  }
  return info;
}

// recover body key NAMES (the wire contract survives minification) from an object / JSON.stringify(obj) / var
// fields that belong to the HTTP WRAPPER, not the payload. If an options object is made ONLY of these AND has a
// body/json/data field, it's a wrapper, the real body is that field's VALUE (trace into it), never these keys.
const WRAPPER_FIELDS = new Set(["body","json","data","method","headers","header","timeout","signal","params","query",
  "stream","credentials","mode","cache","keepalive","redirect","responseType","withCredentials","onUploadProgress",
  "onDownloadProgress","validateStatus","baseURL","url","abortSignal","retries","idempotencyKey"]);
const BODY_FIELDS = ["json","body","data"];   // hold the real payload, in preference order
// Keys that can only ever be client configuration. `query`, `data` and `url` are deliberately absent: each
// is a plausible field name in a real payload, and treating one as configuration would delete a body we had.
const CONFIG_ONLY_FIELDS = new Set(["params","headers","header","method","signal","credentials","mode",
  "cache","keepalive","redirect","responseType","withCredentials","baseURL","abortSignal","retries",
  "timeout","validateStatus","onUploadProgress","onDownloadProgress","stream"]);
function isWrapperOpts(node) {
  if (!node || node.type !== "ObjectExpression") return false;
  let hasBody = false;
  for (const p of node.properties) {
    if (p.type === "SpreadElement") continue;
    if (p.type !== "ObjectProperty" || !p.key) return false;
    const k = p.key.name ?? p.key.value;
    if (BODY_FIELDS.includes(k)) hasBody = true;
    else if (!WRAPPER_FIELDS.has(k)) return false;   // a real payload field present → this is a direct body, not a wrapper
  }
  return hasBody;
}

// Recover the REAL body field names. Traces backward: unwraps JSON.stringify, follows variable bindings (multi-hop),
// resolves spreads, and, crucially, when the arg is a wrapper options object, recurses into its body/json/data
// value instead of emitting the wrapper's own keys. Returns ["{EXPR-body}"] when a wrapper's body can't be resolved
// (an honest "there's a body we couldn't trace", not garbage).
// classify a body field's VALUE by its source, read straight off the AST (not name-guessing): a literal is a known
// constant, crypto.randomUUID()/Date/Intl are client-generated, a fn param is user-input, an enum-var yields its
// value-set, a member access is app state. This is what makes a request CONSTRUCTIBLE, you know how to fill each field.
// A label is cut only when it is absurd, and then visibly: "…" says there was more. Values were cut
// at 20 characters before, so `application/x-www-form-urlencoded` printed as a different string.
function _clip(str, n) {
  str = String(str);
  return str.length > n ? str.slice(0, n) + "\u2026" : str;
}

function classifyValue(v, scope, depth = 0) {
  if (!v || depth > 4) return "?";
  switch (v.type) {
    case "StringLiteral": return "const:" + _clip(String(v.value), 200);
    case "NumericLiteral": case "BooleanLiteral": case "BigIntLiteral": return "const:" + String(v.value);
    case "NullLiteral": return "const:null";
    case "ArrayExpression": {
      // `items: [{sku, qty}]` used to render as the string "array", which throws away the element shape,
      // and an array of objects is where request schemas keep their most interesting fields. Recurse into
      // the elements so a list of objects reports what an element looks like, exactly as a schema would.
      if (!v.elements.length) return "const:[]";
      const shapes = [];
      for (const el of v.elements.slice(0, 4)) {                 // a few is enough to learn the shape
        if (!el || el.type !== "ObjectExpression") continue;
        const f = bodyFields(el, scope, depth + 1);
        if (f) shapes.push(f);
      }
      if (!shapes.length) {
        // homogeneous scalars: say what KIND of scalar rather than just "array"
        const first = v.elements.find((e) => e && e.type !== "SpreadElement");
        const t = first ? classifyValue(first, scope, depth + 1) : "?";
        return typeof t === "string" ? "array<" + t.split(":")[0] + ">" : "array";
      }
      const merged = Object.assign({}, ...shapes);               // union the element shapes we saw
      return [merged];
    }
    case "ObjectExpression": {
      // dynamic/computed keys (`{[toolId]: bool}`) or spread → a RECORD (key set is runtime); note the value type
      const dyn = v.properties.some(p => (p.type === "ObjectProperty" && p.computed) || p.type === "SpreadElement");
      if (dyn) {
        const vp = v.properties.find(p => p.type === "ObjectProperty");
        return "record<key," + (vp ? classifyValue(vp.value, scope, depth + 1) : "?") + ">";
      }
      const nested = bodyFields(v, scope, depth + 1);          // recurse: static nested structure
      return nested || "object";
    }
    case "UnaryExpression": return v.operator === "void" ? "const:undefined" : "computed";
    case "TemplateLiteral": return v.expressions.length ? "computed-string" : "const:" + (v.quasis[0]?.value?.cooked || "");
    case "CallExpression": {
      const c = v.callee, nm = (c && (c.name || (c.property && c.property.name))) || "";
      if (/randomUUID|uuidv?4|^uuid$|nanoid|^cuid|ulid|objectid|v4$/i.test(nm)) return "client-gen:uuid";
      if (/^now$|getTime|toISOString|^Date$|unix|timestamp/i.test(nm)) return "client-gen:timestamp";
      if (/DateTimeFormat|resolvedOptions|timeZone|locale|language|navigator/i.test(nm)) return "client-gen:locale";
      if (/random/i.test(nm)) return "client-gen:random";
      if (/stringify/i.test(nm)) return "json";
      return "computed:" + _clip(nm, 64);
    }
    case "Identifier": {
      const b = scope && scope.getBinding && scope.getBinding(v.name);
      if (b) {
        if (b.kind === "param") return "input:" + _clip(v.name, 64);        // user-supplied argument
        const ev = enumValues(b);
        if (ev.length >= 1 && ev.length <= 8) return "enum:" + ev.join("|");
        const init = b.path.node && b.path.node.init;
        if (init && init.type === "StringLiteral") return "const:" + _clip(init.value, 200);
        if (init) return classifyValue(init, b.scope, depth + 1);
      }
      return "var:" + _clip(v.name, 64);
    }
    case "MemberExpression": case "OptionalMemberExpression":
      return "state:" + ((v.property && v.property.name) || "?");
    case "ConditionalExpression":
      return "enum?:" + classifyValue(v.consequent, scope, depth + 1) + "|" + classifyValue(v.alternate, scope, depth + 1);
    case "LogicalExpression": return classifyValue(v.left, scope, depth + 1);
    default: return "runtime";
  }
}
// per-field SOURCE map for a body object (mirrors bodyKeys' resolution, but keeps each field's value classification)
function bodyFields(node, scope, depth = 0) {
  if (!node || depth > 14) return null;
  if (node.type === "CallExpression") {
    const c = node.callee;
    if (c && c.type === "MemberExpression" && c.property && c.property.name === "stringify") return bodyFields(node.arguments[0], scope, depth + 1);
    return null;
  }
  if (node.type === "Identifier") {
    const fd = formDataAppends(node, scope);                  // multipart body → classify each appended field
    if (fd) { const out = {}; for (const a of fd) out[a.key] = /file|blob|attach|image|upload|dxt/i.test(a.key) ? "file-upload" : classifyValue(a.val, scope); return out; }
    const b = scope && scope.getBinding && scope.getBinding(node.name);
    if (b && b.path && b.path.node && b.path.node.init) {
      const r = bodyFields(b.path.node.init, b.scope, depth + 1);
      if (r && Object.keys(r).length) return r;
    }
    const cs = paramCallSiteArgs(node, scope);       // same call-site hop as bodyKeys, so the TYPES survive too
    if (cs) {
      const out = {};
      for (const a of cs) Object.assign(out, bodyFields(a.node, a.scope, depth + 1) || {});
      if (Object.keys(out).length) return out;
    }
    return null;
  }
  if (node.type === "ConditionalExpression") return { ...(bodyFields(node.consequent, scope, depth + 1) || {}), ...(bodyFields(node.alternate, scope, depth + 1) || {}) };
  if (node.type === "LogicalExpression") return bodyFields(node.left, scope, depth + 1);
  if (node.type === "ObjectExpression") {
    if (isWrapperOpts(node)) {
      for (const bf of BODY_FIELDS) {
        const p = node.properties.find(p => p.type === "ObjectProperty" && p.key && (p.key.name ?? p.key.value) === bf);
        if (p) return bodyFields(p.value, scope, depth + 1);
      }
    }
    const out = {};
    for (const p of node.properties) {
      if (p.type === "ObjectProperty" && p.key) out[p.key.name ?? p.key.value] = classifyValue(p.value, scope);
      else if (p.type === "SpreadElement") Object.assign(out, bodyFields(p.argument, scope, depth + 1) || {});
    }
    return Object.keys(out).length ? out : null;
  }
  return null;
}

// FormData / multipart body: `const fd=new FormData(); fd.append("file",x); fd.append("signature",s); fetch(url,{body:fd})`.
// The fields are the .append("name",val) calls on the var, collected by walking its enclosing function. Common for
// file uploads (juicy attack surface). Returns [{key,val}] or null.
function collectAppends(node, varName, out, depth) {
  if (!node || typeof node !== "object" || depth > 80 || out.length > 40) return;
  if (node.type === "CallExpression" && node.callee && node.callee.type === "MemberExpression" &&
      node.callee.object && node.callee.object.type === "Identifier" && node.callee.object.name === varName &&
      node.callee.property && (node.callee.property.name === "append" || node.callee.property.name === "set") &&
      node.arguments[0] && node.arguments[0].type === "StringLiteral") {
    out.push({ key: node.arguments[0].value, val: node.arguments[1] });   // .append or .set (FormData / URLSearchParams)
  }
  for (const k in node) {
    if (k === "loc" || k === "start" || k === "end" || k.startsWith("_") || k === "leadingComments" || k === "trailingComments") continue;
    const v = node[k];
    if (Array.isArray(v)) { for (const c of v) collectAppends(c, varName, out, depth + 1); }
    else if (v && typeof v.type === "string") collectAppends(v, varName, out, depth + 1);
  }
}
function formDataAppends(node, scope) {                       // node = an Identifier; if it's a FormData, return its appends
  if (!node || node.type !== "Identifier") return null;
  const b = scope && scope.getBinding && scope.getBinding(node.name);
  if (!b || !b.path || !b.path.node) return null;
  const isCtor = (n) => n && n.type === "NewExpression" && n.callee && /^(FormData|URLSearchParams)$/.test(n.callee.name);
  let init = b.path.node.init;
  // A generated client declares the container and assigns it on a branch:
  //     let s; canConsumeForm(r) ? s = new FormData : s = new URLSearchParams; s.append("file", t.file)
  // Requiring an INITIALISER missed every one of those, which is the whole multipart surface of an
  // openapi-generator client, 485 write endpoints across two hosts had a body sitting in plain .append() calls.
  if (!isCtor(init)) {
    init = null;
    for (const cv of (b.constantViolations || [])) {
      const n2 = cv.node;
      if (n2 && n2.type === "AssignmentExpression" && isCtor(n2.right)) { init = n2.right; break; }
    }
  }
  if (!isCtor(init)) return null;
  const out = [];
  if (init.arguments[0] && init.arguments[0].type === "ObjectExpression")     // `new URLSearchParams({grant_type, code})`
    for (const p of init.arguments[0].properties) if (p.type === "ObjectProperty" && p.key) out.push({ key: p.key.name ?? p.key.value, val: p.value });
  collectAppends(b.scope.block, node.name, out, 0);                            // + .append/.set calls
  return out.length ? out : null;
}

// A body that IS one of the enclosing function's parameters is not unknown, it is whatever the callers pass. This
// is 70% of the unresolved write bodies in the corpus (938 of 1345), and in minified code the parameter is named
// `e` or `o`, so the earlier name-keyed lookup skipped it by construction to avoid collisions. Babel's binding
// graph has no such problem: go parameter → its function → that function's own binding → its reference paths →
// the argument at the same index. Scope-correct, so a one-character name resolves exactly as well as a long one.
// Capped at a few call sites: the point is the SHAPE, and the first callers establish it.
// non-method field reads on a parameter, from inside the function that received it
function _paramFieldReads(node, scope) {
  try {
    const b = scope && scope.getBinding && scope.getBinding(node.name);
    if (!b || b.kind !== "param" || !b.path || !b.path.parentPath) return null;
    const fnPath = b.path.parentPath;
    if (!fnPath.node || !Array.isArray(fnPath.node.params)) return null;
    const out = [];
    fnPath.traverse({
      MemberExpression(mp) {
        const o = mp.node.object, pr = mp.node.property;
        if (!(o && o.type === "Identifier" && o.name === node.name && pr && !mp.node.computed)) return;
        const par = mp.parentPath;                       // a property you CALL is a method, not a field
        if (par && par.node && par.node.type === "CallExpression" && par.node.callee === mp.node) return;
        const f = pr.name ?? pr.value;
        if (f && !out.includes(String(f))) out.push(String(f));
      },
    });
    return out.length ? out.slice(0, 1000) : null;
  } catch { return null; }
}

function paramCallSiteArgs(node, scope) {
  const b = scope && scope.getBinding && scope.getBinding(node.name);
  if (!b || b.kind !== "param" || !b.path || !b.path.parentPath) return null;
  const fnPath = b.path.parentPath;
  if (!fnPath.node || !Array.isArray(fnPath.node.params)) return null;
  const idx = fnPath.node.params.indexOf(b.path.node);
  if (idx < 0) return null;
  let name = fnPath.node.id && fnPath.node.id.name;
  if (!name) {
    const pp = fnPath.parentPath && fnPath.parentPath.node;         // arrow bound to a name
    if (pp && pp.type === "VariableDeclarator" && pp.id && pp.id.type === "Identifier") name = pp.id.name;
  }
  if (!name) return null;
  const fb = (fnPath.scope.parent || fnPath.scope).getBinding(name);
  if (!fb || !fb.referencePaths) return null;
  const out = [];
  for (const rp of fb.referencePaths) {
    const cp = rp.parentPath;
    if (cp && cp.node && cp.node.type === "CallExpression" && cp.node.callee === rp.node && cp.node.arguments[idx])
      out.push({ node: cp.node.arguments[idx], scope: cp.scope });
    if (out.length >= 4) break;
  }
  return out.length ? out : null;
}

// `let o = {}; o.name = x; o.email = y; post(url, o)`, the initializer alone says `{}`, and we reported an empty
// body for a request carrying two fields. An empty object followed by property writes is one of the most ordinary
// ways to build a payload; the assignments are right there in the binding's own references.
function mutatedObjectKeys(node, scope) {
  const b = scope && scope.getBinding && scope.getBinding(node.name);
  if (!b || !b.path || !b.path.node) return null;
  const init = b.path.node.init;
  if (!init || init.type !== "ObjectExpression") return null;
  const keys = [];
  for (const p of init.properties)
    if ((p.type === "ObjectProperty" || p.type === "ObjectMethod") && p.key) keys.push(p.key.name ?? p.key.value);
  for (const rp of (b.referencePaths || [])) {
    const mp = rp.parentPath;
    if (!mp || !mp.node || mp.node.type !== "MemberExpression" || mp.node.object !== rp.node) continue;
    const ap = mp.parentPath;
    if (!ap || !ap.node || ap.node.type !== "AssignmentExpression" || ap.node.left !== mp.node) continue;
    const k = mp.node.computed ? (mp.node.property && mp.node.property.value) : (mp.node.property && (mp.node.property.name ?? mp.node.property.value));
    if (k && !keys.includes(k)) keys.push(k);
  }
  return keys.length ? keys : null;
}

// HOW the body goes on the wire. `body_keys: ["locale"]` does not say whether the request carries
// `locale=en` or `{"locale":"en"}`, and those are different requests, a payload aimed at the wrong one is
// rejected before it reaches anything worth testing. The extractor already knows: it walked the node that
// BUILT the body. It knew and it threw the answer away, and `isCtor` even matched FormData and
// URLSearchParams with one regex, collapsing multipart and urlencoded into a single unnamed case.
//
// Reports what the construction proves and nothing more. A plain object handed to an unidentified client
// returns null rather than guessing "json", because a client is free to form-encode it.
function bodyEncoding(node, scope, depth = 0) {
  if (!node || depth > 6) return null;
  if (node.type === "NewExpression") {
    const nm = node.callee && (node.callee.name || (node.callee.property && node.callee.property.name));
    if (nm === "FormData") return "multipart";               // multipart/form-data, boundary + parts
    if (nm === "URLSearchParams") return "form";             // application/x-www-form-urlencoded
  }
  // JSON.stringify(...) is the unambiguous one: whatever it wraps goes as application/json.
  if (node.type === "CallExpression") {
    const c = node.callee;
    if (c && c.type === "MemberExpression" && c.object && c.object.name === "JSON" &&
        c.property && c.property.name === "stringify") return "json";
    if (c && c.property && /^(toFormData|asFormData)$/.test(c.property.name || "")) return "multipart";
  }
  // An identifier: follow it to what it was built from. `const fd = new FormData(); fetch(u,{body:fd})`
  // is the ordinary way to write an upload and the node at the call site says nothing on its own.
  if (node.type === "Identifier" && scope && scope.getBinding) {
    const b = scope.getBinding(node.name);
    if (b && b.path && b.path.node) {
      const init = b.path.node.init;
      if (init) {
        const viaInit = bodyEncoding(init, scope, depth + 1);
        if (viaInit) return viaInit;
      }
      for (const cv of (b.constantViolations || [])) {
        const n2 = cv.node;
        if (n2 && n2.type === "AssignmentExpression") {
          const viaAssign = bodyEncoding(n2.right, scope, depth + 1);
          if (viaAssign) return viaAssign;
        }
      }
    }
  }
  if (node.type === "StringLiteral" || node.type === "TemplateLiteral") return "raw";
  return null;
}

// `body: function(e, t = !1) { return null == e ? e : {reportJSONString: e.reportJSONString, …} }(e.body, !1)`
// An immediately-invoked serializer, which is how openapi-generator and swagger-typescript-api emit a
// request body. The node at the `body:` key is a CallExpression, so every body reader saw a call it could
// not resolve and returned null, the endpoint came out with an operation name, a method, a path, and no
// contract at all. The shape is right there in the function it invokes.
function _unwrapIIFE(node, depth = 0) {
  if (!node || depth > 3) return node;
  if (node.type !== "CallExpression" || !node.callee) return node;
  if (!/^(FunctionExpression|ArrowFunctionExpression)$/.test(node.callee.type)) return node;
  const obj = _returnedObject(node.callee);
  return obj ? _unwrapIIFE(obj, depth + 1) : node;
}

// The VALUE of one header, when the header bag was built by mutation. `mutatedObjectKeys` walks exactly
// this shape and keeps only the key, so `let n = {}; n["Content-Type"] = "application/json"` told us a
// content-type EXISTS and not what it is, and what it is happens to be the thing that settles how the
// body is encoded. Same walk, one field further.
function mutatedHeaderValue(node, scope, wantKey) {
  try {
    const want = String(wantKey).toLowerCase();
    const pick = (k, v) => (k && String(k).toLowerCase() === want && v &&
                            v.type === "StringLiteral") ? v.value : null;
    if (node && node.type === "ObjectExpression") {
      for (const p of node.properties)
        if (p.type === "ObjectProperty" && p.key) {
          const hit = pick(p.key.name ?? p.key.value, p.value);
          if (hit) return hit;
        }
      return null;
    }
    if (!node || node.type !== "Identifier") return null;
    const b = scope && scope.getBinding && scope.getBinding(node.name);
    if (!b) return null;
    const init = b.path && b.path.node && b.path.node.init;
    if (init && init.type === "ObjectExpression") {
      const v = mutatedHeaderValue(init, scope, wantKey);
      if (v) return v;
    }
    for (const rp of (b.referencePaths || [])) {
      const mp = rp.parentPath;
      if (!mp || !mp.node || mp.node.type !== "MemberExpression" || mp.node.object !== rp.node) continue;
      const ap = mp.parentPath;
      if (!ap || !ap.node || ap.node.type !== "AssignmentExpression" || ap.node.left !== mp.node) continue;
      const k = mp.node.computed ? (mp.node.property && mp.node.property.value)
                                 : (mp.node.property && (mp.node.property.name ?? mp.node.property.value));
      const hit = pick(k, ap.node.right);
      if (hit) return hit;
    }
  } catch {}
  return null;
}

// Does this one-parameter function RETURN its argument, reshaped but recognisable?
//
// "Contains no path literal" was the only test, and it does not establish what a function returns.
// `function strip(p){ return p.slice(0,4) }` contains no path literal and truncates its input to "/api";
// `fetch(strip("/api/orders"))` was reported as /api/orders, marked dynamic:false, a URL the code never
// requests, asserted as fully resolved. That is worse than an unresolved template.
//
// So check the shape of the returns instead. Every one must be the parameter itself, or the parameter
// through an operation that keeps it the same URL: a `.replace(…)`, a trailing trim (`slice(0, -n)`), a
// trim/toString. A positive-index slice or substring is a truncation and is refused. Keycloak's
// `s => s.endsWith("/") ? s.slice(0,-1) : s`, the case this branch exists for, passes on both arms.
function _returnsItsArgument(fn, paramName, depth = 0) {
  if (!fn || depth > 3) return false;
  const ok = (n, d) => {
    if (!n || d > 6) return false;
    if (n.type === "Identifier") return n.name === paramName;
    if (n.type === "ConditionalExpression") return ok(n.consequent, d + 1) && ok(n.alternate, d + 1);
    if (n.type === "LogicalExpression") return ok(n.left, d + 1) || ok(n.right, d + 1);
    if (n.type === "TemplateLiteral")                       // `${p}` and nothing else
      return n.expressions.length === 1 && n.quasis.every((q) => !(q.value.cooked || "").trim()) &&
             ok(n.expressions[0], d + 1);
    if (n.type === "CallExpression" && n.callee && n.callee.type === "MemberExpression") {
      const m = n.callee.property && (n.callee.property.name ?? n.callee.property.value);
      if (!ok(n.callee.object, d + 1)) return false;
      if (m === "replace" || m === "replaceAll" || m === "trim" || m === "toString") return true;
      if (m === "slice" || m === "substring") {             // ONLY a trailing trim, never a truncation
        const a = n.arguments || [];
        return a.length === 2 && a[0] && a[0].type === "NumericLiteral" && a[0].value === 0 &&
               a[1] && a[1].type === "UnaryExpression" && a[1].operator === "-";
      }
      return false;
    }
    return false;
  };
  const pname = paramName;
  if (fn.body && fn.body.type !== "BlockStatement") return ok(fn.body, 0);   // concise arrow
  const body = fn.body && fn.body.body ? fn.body.body : null;
  if (!body) return false;
  let sawReturn = false;
  for (const st of body) {
    if (st.type !== "ReturnStatement") continue;
    sawReturn = true;
    if (!ok(st.argument, 0)) return false;
  }
  return sawReturn && !!pname;
}

// A content-type hiding in ANY argument, not just the one we decided was "the options object".
//
// x's client takes it fourth, inside a spread:
//     e.post("account/personalization/p13n_preferences", t, {}, {...r, "content-type": "application/json"})
// so a reader that inspects arg[1] or a known `headers` key finds nothing, and the row comes out
// `body[?]` when the answer is sitting in the call. Custom clients put headers wherever their signature
// says; there is no position to rely on, and there does not need to be, a `content-type` property is
// unambiguous wherever it appears.
function _ctInArgs(args, depth = 0) {
  if (!args || depth > 3) return null;
  for (const a of args) {
    if (!a) continue;
    if (a.type === "ObjectExpression") {
      for (const p of a.properties) {
        if (p.type === "ObjectProperty" && p.key) {
          const k = String(p.key.name ?? p.key.value ?? "").toLowerCase();
          if (k === "content-type" || k === "contenttype") {
            if (p.value && p.value.type === "StringLiteral") return p.value.value;
          }
          // one level in: {headers: {"content-type": …}}
          if (k === "headers" && p.value && p.value.type === "ObjectExpression") {
            const inner = _ctInArgs([p.value], depth + 1);
            if (inner) return inner;
          }
        }
      }
    }
  }
  return null;
}

function bodyKeys(node, scope, depth = 0) {
  if (!node || depth > 14) return null;   // deep enough for JSON.stringify(ternary({...L,{...p}})) spread chains
  node = _unwrapIIFE(node);
  const fd = node.type === "Identifier" && formDataAppends(node, scope);   // multipart/FormData body
  if (fd) return fd.map(a => a.key);
  // `body: new URLSearchParams({grant_type:"code", client_id:cid})`, a form-encoded body, and the single
  // most common shape for an OAuth token request, which makes it a shape worth never missing. It reached
  // here as a NewExpression, matched nothing, and returned null: the endpoint came out contract-free.
  // FormData built the same way is handled above; this is the other constructor that IS a body.
  if (node.type === "Identifier") {
    // The declared type names the fields directly; prefer it over hunting call sites, which is inference.
    const ts = tsShapeOfParam(node, scope);
    if (ts && typeof ts === "object" && !Array.isArray(ts)) {
      const ks = Object.keys(ts);
      if (ks.length) return ks;
    }
    const viaCaller = paramProvenance(node, scope, depth);
    if (viaCaller && viaCaller.length) return viaCaller;
  }
  // `data: e.saveData`, the receiver is unresolvable, the PROPERTY is the whole clue.
  if (node.type === "MemberExpression" && !node.computed && node.property) {
    const pn = node.property.name ?? node.property.value;
    const via = pn && _GETTERS.get(String(pn));
    if (via && via.length) return via;
  }
  if (node.type === "NewExpression") {
    const nm = node.callee && (node.callee.name || (node.callee.property && node.callee.property.name));
    if (nm === "URLSearchParams" || nm === "FormData" || nm === "Headers") {
      const a0 = node.arguments && node.arguments[0];
      if (a0) return bodyKeys(a0, scope, depth + 1);
      // `new FormData()` then `.append(...)` elsewhere, resolved through the identifier path above.
      return null;
    }
  }
  // An array of [key, value] pairs is the other accepted URLSearchParams/FormData input.
  if (node.type === "ArrayExpression" && node.elements.length &&
      node.elements.every((e) => e && e.type === "ArrayExpression" && e.elements.length === 2)) {
    const ks = node.elements.map((e) => e.elements[0])
                 .filter((k) => k && k.type === "StringLiteral").map((k) => k.value);
    if (ks.length) return ks;
  }
  if (node.type === "CallExpression") {
    const c = node.callee;
    if (c && c.type === "MemberExpression" && c.property && (c.property.name === "stringify"))
      return bodyKeys(node.arguments[0], scope, depth + 1);
    // Babel compiles `{...base, extra: v}` into a helper call:
    //     (0,a.A)((0,a.A)({}, e), {}, {cardStatus: e.cardStatus ?? LOST})
    // The helper's NAME is minified past recognition, but the SHAPE is unmistakable, every argument is an
    // object or a reference to one, and the result is their union. That is precisely what a spread produces, so
    // unioning the arguments' keys reconstructs the body whatever the helper happens to be called.
    if (node.arguments.length >= 2 &&
        node.arguments.every((a) => a && (a.type === "ObjectExpression" || a.type === "Identifier" ||
                                          a.type === "CallExpression" || a.type === "MemberExpression")) &&
        // `{}` counts: Babel's inner call is literally `(0,a.A)({}, e)`, whose whole job is to clone `e`.
        // Demanding a NON-EMPTY literal rejected exactly that half and lost the base object's fields.
        node.arguments.some((a) => a.type === "ObjectExpression")) {
      const merged = [];
      for (const a of node.arguments) { const k = bodyKeys(a, scope, depth + 1); if (k) merged.push(...k); }
      const real = [...new Set(merged)].filter((k) => !String(k).startsWith("{"));
      if (real.length) return real;
    }
    // A generated client hands the body to a SERIALIZER: `body: PatchedSettingsRequestToJSON(t.request)`.
    // The serializer is machine-written and its return statement enumerates every field of the request, so it
    // is an exact schema, better than anything a call site would give us. Inline it when it lives in this
    // chunk; when it does not, `_bodyFn` below records the name for the cross-chunk join.
    {
      const nm = _calleeIdent(c);
      if (nm && depth < 6) {
        const obj = _returnedObjectOf(nm, scope, depth);
        if (obj) {
          const keys = [];
          const objScope = _RET_SCOPE.get(obj) || scope;          // the function's, see _RET_SCOPE
          for (const pr of obj.properties) {
            if ((pr.type === "ObjectProperty" || pr.type === "ObjectMethod") && pr.key) keys.push(pr.key.name ?? pr.key.value);
            else if (pr.type === "SpreadElement") { const sk = bodyKeys(pr.argument, objScope, depth + 1); if (sk) keys.push(...sk); }
          }
          if (keys.length) return [...new Set(keys)];
        }
      }
    }
    // Object.assign({}, defaults, overrides), every argument contributes keys; unwrapObj keeps only the last one,
    // which silently halves a merged payload.
    if (c && c.type === "MemberExpression" && c.object && c.object.name === "Object" &&
        c.property && c.property.name === "assign") {
      const merged = [];
      for (const a of node.arguments) { const k = bodyKeys(a, scope, depth + 1); if (k) merged.push(...k); }
      return merged.length ? [...new Set(merged)] : null;
    }
    return null;
  }
  if (node.type === "ObjectExpression") {
    if (isWrapperOpts(node)) {                                   // wrapper → the real body is inside body/json/data
      for (const bf of BODY_FIELDS) {
        const p = node.properties.find(p => p.type === "ObjectProperty" && p.key && (p.key.name ?? p.key.value) === bf);
        if (p) return bodyKeys(p.value, scope, depth + 1) || ["{EXPR-body}"];
      }
    }
    // An options object that is ALL client configuration and carries no body field is a request with NO
    // BODY, and that is a fact we read, not a failure to resolve one. openapi-fetch is the common case:
    //     y().POST("/workspaces/{id}/credit-limits", {params:{path:{…}}, body: r})
    //     y().DELETE("/workspaces/{id}/credit-limits/{limitId}", {params:{path:{…}}})
    // The POST names its body and resolves. The DELETE has no `body` key at all, so isWrapperOpts saw no
    // body field, returned false, and this function fell through to listing the object's own keys, which
    // reported a body field called `params` on 41 one target endpoints. Both halves were wrong: `params` is
    // where the PATH parameters go, and the request it described sends nothing.
    //
    // The trigger is deliberately narrow. `query` is not in it, because `POST /docs/api/search/fts` with
    // `{query: "…"}` is a search request whose body really is one field called query, six of those in the
    // corpus. Only keys that cannot be anything but client configuration count.
    {
      const keys = node.properties
        .filter((p) => p.type === "ObjectProperty" && p.key && !p.computed)
        .map((p) => String(p.key.name ?? p.key.value));
      const allProps = node.properties.every((p) => p.type === "ObjectProperty" && p.key && !p.computed);
      if (allProps && keys.length &&
          !keys.some((k) => BODY_FIELDS.includes(k)) &&
          keys.every((k) => WRAPPER_FIELDS.has(k)) &&
          keys.some((k) => CONFIG_ONLY_FIELDS.has(k)))
        return [];                       // an EMPTY body, established, not an unresolved one
    }
    const keys = [];
    for (const p of node.properties) {
      if ((p.type === "ObjectProperty" || p.type === "ObjectMethod") && p.key) keys.push(p.key.name ?? p.key.value ?? "{EXPR}");
      else if (p.type === "SpreadElement") {                     // {...base, x} → resolve base's keys + merge
        const sk = bodyKeys(p.argument, scope, depth + 1);
        if (sk) keys.push(...sk); else keys.push("...{EXPR}");
      }
    }
    return keys.length ? keys : [];      // {} is an EMPTY body (a known fact), not an unknown one
  }
  // `t ? JSON.stringify(t) : void 0`, take whichever branch actually carries a body
  if (node.type === "ConditionalExpression") {
    return bodyKeys(node.consequent, scope, depth + 1) || bodyKeys(node.alternate, scope, depth + 1);
  }
  if (node.type === "LogicalExpression") {                     // `x && JSON.stringify(x)` / `b ?? {}`
    return bodyKeys(node.right, scope, depth + 1) || bodyKeys(node.left, scope, depth + 1);
  }
  if (node.type === "Identifier") {                              // follow the variable to where it's constructed
    const b = scope && scope.getBinding && scope.getBinding(node.name);
    if (b && b.path && b.path.node && b.path.node.init) {
      const r = bodyKeys(b.path.node.init, b.scope, depth + 1);
      // `{}` is a real answer only if nothing later writes to it
      if (r && r.length) return r;
      const mk = mutatedObjectKeys(node, scope);
      if (mk) return mk;
      if (r) return r;
    }
    if (b && b.path && b.path.node && !b.path.node.init) {       // `let x; … x = {...}`, assigned, not initialised
      for (const cv of (b.constantViolations || [])) {
        const n2 = cv.node;
        if (n2 && n2.type === "AssignmentExpression" && n2.right) {
          const r2 = bodyKeys(n2.right, cv.scope || scope, depth + 1);
          if (r2 && r2.length) return r2;
        }
      }
    }
    // The wrapper often READS the body it was handed:
    //     n = e => post("/2fa", e, {headers:{"X-Brick-Reauthentication": e.currentUserPassword}})
    // Every non-method member access on the body parameter names a field of the body, with no call site, no
    // framework knowledge and no module graph required. It is only ever PARTIAL, the wrapper touches what it
    // needs and passes the rest through, so it is marked as such rather than presented as the schema. Rare
    // (2 of 45 param-bodied writes on one target) but free, and the fields it does surface tend to be the
    // ones the wrapper had a reason to care about.
    const cs = paramCallSiteArgs(node, scope);                   // it's a parameter: ask the callers FIRST,
    if (cs) {                                                    // a caller passes the WHOLE object, the wrapper
      const merged = [];                                         // only the part it needed
      for (const a of cs) { const k = bodyKeys(a.node, a.scope, depth + 1); if (k) merged.push(...k); }
      if (merged.length) return [...new Set(merged)];
    }
    const reads = _paramFieldReads(node, scope);                 // …and the wrapper's own reads as the fallback
    if (reads) return reads;
  }
  if (node.type === "ConditionalExpression") return bodyKeys(node.consequent, scope, depth + 1) || bodyKeys(node.alternate, scope, depth + 1);
  if (node.type === "LogicalExpression") return bodyKeys(node.left, scope, depth + 1) || bodyKeys(node.right, scope, depth + 1);
  return null;
}

// is `ident` a one-hop const/let alias of a known global sink (const n = fetch)?  decidable; deep aliasing isn't.
function bindsTo(ident, scope, target) {
  const b = scope && scope.getBinding && scope.getBinding(ident.name);
  if (!b || !b.path || !b.path.node) return false;
  const init = b.path.node.init;
  return init && init.type === "Identifier" && init.name === target;
}

// does an ObjectExpression have (own, literal-keyed) properties for ALL given names?
function hasKeys(obj, ...names) {
  const present = new Set();
  for (const p of obj.properties)
    if ((p.type === "ObjectProperty" || p.type === "ObjectMethod") && p.key)
      present.add(p.key.name ?? p.key.value);
  return names.every((n) => present.has(n));
}

// GENERALIZED duck-typed sink, the big unlock for custom wrappers & react-query/SWR fetch-hooks. Reading real
// claude.ai code: reads are `t(`/api/org/${s}/model_settings`,{queryKey:[...]})` and writes are
// `or(e,org,"secrets",{method:"POST",body:s})`, the callee is a minified per-resource wrapper (NOT fetch/axios),
// so name-matching misses them and the URL only survives as a literal. But the OPTIONS OBJECT is a fingerprint:
// any call carrying an object with a network-signal key (method/body/json/queryKey/mutationKey) IS a request sink.
// URL = opts.url key, else the first path-like ARGUMENT. method = opts.method, else GET if queryKey (react-query
// read), else POST if a body is present, else GET. The path-like-URL requirement is the false-positive gate.
// the request-config vocabulary, used to tell an options bag apart from a payload when the position is ambiguous
const AXIOS_CFG_KEYS = new Set(["params", "headers", "data", "withCredentials", "signal", "timeout", "responseType",
  "baseURL", "auth", "validateStatus", "cancelToken", "paramsSerializer", "onUploadProgress", "onDownloadProgress",
  "maxRedirects", "proxy", "transformRequest", "transformResponse", "decompress", "credentials", "mode", "cache"]);
// Config keys that are UNAMBIGUOUS, no real payload is named `withCredentials` or `responseType`. Their mere
// presence settles it. The rest of AXIOS_CFG_KEYS (`params`, `data`, `auth`, `mode`, `cache`) are perfectly
// ordinary field names too, so one of them ALONE proves nothing.
const STRONG_CFG_KEYS = new Set(["withCredentials", "responseType", "validateStatus", "paramsSerializer",
  "cancelToken", "baseURL", "onUploadProgress", "onDownloadProgress", "transformRequest", "transformResponse",
  "headers", "signal", "timeout", "maxRedirects", "decompress"]);
// Is this object argument a request CONFIG rather than a payload? Used wherever position alone is ambiguous:
// `.delete(url, X)`, `.post(url, X)` and the wrapper's internal sink. Uppy ships
// `this.post(id + "/preauth/", {params: this.companionKeysParams})`, a genuine body whose single key happens to
// be an axios config name, so a one-key object needs a STRONG key to count as config; otherwise it is data.
// Stainless-generated SDKs (Anthropic's, OpenAI's and others) pass an OPTIONS bag where axios takes data:
//
//     this._client.post("/v1/deployments?beta=true", {body: r, ...t, headers: Y([...])})
//
// Read as data, that reported a body of {"body", "headers"} on claude.ai, the request's own options.
// It is an options bag when it names `body` or `json` (ky's word), every other named key is a wrapper
// key, and a spread or another wrapper key sits beside it: `{body: text}` alone is also exactly what a
// comment API's payload looks like, so it stays a payload. `data` is never unwrapped here, because an
// axios-style client sends this argument as the JSON itself, `data` key and all.
// -> {body: node|null} when it is an options bag (body null: options with no body), else null.
function sdkOptionsBag(node) {
  if (!node || node.type !== "ObjectExpression" || !node.properties.length) return null;
  let bodyProp = null, spread = false;
  const others = [];
  for (const p of node.properties) {
    if (p.type === "SpreadElement") { spread = true; continue; }
    if (p.type !== "ObjectProperty" || !p.key || p.computed) return null;
    const k = String(p.key.name ?? p.key.value);
    if ((k === "body" || k === "json") && !bodyProp) bodyProp = p;
    else others.push(k);
  }
  if (!others.every((k) => WRAPPER_FIELDS.has(k) && k !== "data" && k !== "url")) return null;
  if (bodyProp) return (spread || others.length) ? { body: bodyProp.value } : null;
  // `{...opts, headers: {...}}` on a POST with no body named: options only, and every named key is one
  // no payload carries.
  return (spread && others.length && others.every((k) => CONFIG_ONLY_FIELDS.has(k))) ? { body: null } : null;
}
function looksLikeConfigBag(node) {
  if (!node || node.type !== "ObjectExpression" || !node.properties.length) return false;
  const names = [];
  for (const p of node.properties) {
    if (!p.key) return false;                       // a spread: can't judge, so don't claim it's config
    names.push(String(p.key.name ?? p.key.value));
  }
  if (!names.every((k) => AXIOS_CFG_KEYS.has(k) || k === "method")) return false;
  return names.length >= 2 || names.some((k) => STRONG_CFG_KEYS.has(k));
}
// A TanStack Query options object is not a payload. `useMutation`-style wrappers take the request in the
// first arguments and the CACHE BEHAVIOUR in the last one:
//
//   s(`/api/organizations/${e}/invite_link`, "POST", {...t, async onSuccess(r, ...i){ … }})
//
// Read as a body that reported `["onSuccess","onError"]`, callback names, on 119 of claude.ai's endpoints,
// which is most of what its contract recall appeared to be. The real body is supplied later, at
// `mutate(variables)`, and is genuinely not at this call site: the honest answer is that we do not have it.
//
// A spread is tolerated here, unlike in looksLikeConfigBag, because `{...t, onSuccess(){}}` is the shape
// these wrappers are written in, the spread forwards the caller's options and says nothing either way.
// The decision rests on the NAMED keys: every one of them an option, and at least one that no payload
// would carry.
const RQ_OPTION_KEYS = new Set([
  "onSuccess", "onError", "onSettled", "onMutate", "mutationKey", "mutationFn", "queryKey", "queryFn",
  "retry", "retryDelay", "gcTime", "cacheTime", "staleTime", "meta", "networkMode", "throwOnError",
  "enabled", "select", "placeholderData", "initialData", "scope", "refetchOnWindowFocus",
  "refetchOnMount", "refetchOnReconnect", "refetchInterval", "structuralSharing", "notifyOnChangeProps",
  "keepPreviousData", "suspense", "getNextPageParam", "getPreviousPageParam", "initialPageParam",
]);
const RQ_STRONG = new Set(["onSuccess", "onError", "onSettled", "onMutate", "mutationKey", "mutationFn",
                           "queryKey", "queryFn", "gcTime", "staleTime", "getNextPageParam"]);
// Which positional-method wrappers take their options in the trailing argument. Learned per file from the
// call sites that are UNAMBIGUOUS, one carrying `onSuccess` or `mutationKey` settles the signature, and
// then applied to that wrapper's other calls, where the same argument holds only `{enabled}` or `{meta}`.
//
// Necessary because the key names alone cannot decide those: `POST /mcp/remote_server {enabled: true}` is a
// perfectly ordinary body, and the codebase relies on reading a real payload out of this position (295 of
// claude.ai's bodies come from it). What settles it is the WRAPPER, not the object, `rt(path, "POST", …)`
// takes react-query options in every one of its 27 call sites, and one of them says so out loud.
let _QUERY_OPT_CALLEES = new Set();
function _calleeKey(c) {
  if (!c) return null;
  if (c.type === "Identifier") return c.name;
  if (c.type === "MemberExpression" && c.property && !c.computed)
    return ((c.object && c.object.name) || "?") + "." + (c.property.name ?? c.property.value);
  return null;
}
function indexQueryOptCallees(ast) {
  const out = new Set();
  traverse(ast, { CallExpression(path) {
    try {
      const n = path.node;
      if (n.arguments.length < 3 || !n.arguments[1] || n.arguments[1].type !== "StringLiteral") return;
      const k = _calleeKey(n.callee);
      if (!k) return;
      for (let i = 2; i < n.arguments.length; i++)
        if (looksLikeQueryOptions(n.arguments[i])) { out.add(k); return; }
    } catch {}
  } });
  return out;
}
// All NAMED keys are query options, but none of them proves it on its own. Only trusted for a callee whose
// signature another call site already settled.
function allKeysAreQueryOptions(node) {
  if (!node || node.type !== "ObjectExpression" || !node.properties.length) return false;
  const names = [];
  for (const p of node.properties) {
    if (p.type === "SpreadElement") continue;
    if (!p.key || p.computed) return false;
    names.push(String(p.key.name ?? p.key.value));
  }
  return names.length > 0 && names.every((k) => RQ_OPTION_KEYS.has(k));
}

// A function can be no part of a payload: JSON.stringify drops it and FormData cannot hold one. So a key
// whose value is a function is an option, whatever its name, and a wrapper's own option beside the
// standard ones (claude.ai's `transformVariables`, next to `onMutate`) no longer turns the object into
// a "body" of callback names.
function _isFnValue(p) {
  if (!p) return false;
  if (p.type === "ObjectMethod") return true;
  const v = p.value;
  return !!v && (v.type === "ArrowFunctionExpression" || v.type === "FunctionExpression");
}
function looksLikeQueryOptions(node) {
  if (!node || node.type !== "ObjectExpression" || !node.properties.length) return false;
  const names = [], fnKeys = new Set();
  for (const p of node.properties) {
    if (p.type === "SpreadElement") continue;
    if (!p.key || p.computed) return false;
    const k = String(p.key.name ?? p.key.value);
    names.push(k);
    if (_isFnValue(p)) fnKeys.add(k);
  }
  if (!names.length) return false;
  return names.every((k) => RQ_OPTION_KEYS.has(k) || fnKeys.has(k)) && names.some((k) => RQ_STRONG.has(k));
}
const NET_SIGNAL = new Set(["method", "body", "json", "queryKey", "mutationKey"]);
// TanStack Query CACHE operations carry `queryKey` but never touch the network. Treating them as sinks
// invented hundreds of phantom unresolved endpoints (326 of claude.ai's opts-sink hits were these, i.e.
// most of its "unresolved path" rate was noise, not a tracing failure). Precision, not recall.
const CACHE_OPS = new Set([
  "invalidateQueries", "setQueryData", "setQueriesData", "getQueryData", "getQueriesData",
  "cancelQueries", "removeQueries", "refetchQueries", "resetQueries", "setQueryDefaults",
  "setInfiniteQueryData", "getQueryState", "isFetching", "isMutating",
]);
const _OPTSDBG = !!process.env.JSHOUND_OPTS_DEBUG;
// A write whose body we could not resolve is a specific RE failure, and the failures are not all the same kind:
// a local variable assigned two lines up is a scope walk, a call to a payload builder is an inlining problem, and a
// FormData assembled across ten statements is a different job again. Under JSHOUND_BODY_DEBUG each unresolved body
// carries the SOURCE of the argument we gave up on, so the remaining work can be ranked by what actually occurs
// rather than by what seems likely. Off by default, this is diagnostic weight, not output.
const _BODYDBG = !!process.env.JSHOUND_BODY_DEBUG;
function _dbgBody(rec, node, callNode) {
  if (!_BODYDBG) return rec;
  if (node) {
    rec._bodyType = node.type;
    if (node.start != null) rec._bodyExpr = _SRC.slice(node.start, Math.min(_SRC.length, node.end || node.start + 160)).replace(/\s+/g, " ").slice(0, 160);
  }
  // the surrounding statement, so an unresolved body can be CLASSIFIED rather than merely counted
  const cn = callNode || node;
  if (cn && cn.start != null)
    rec._siteSrc = _SRC.slice(Math.max(0, cn.start - 260), Math.min(_SRC.length, (cn.end || cn.start) + 90)).replace(/\s+/g, " ").slice(0, 350);
  return rec;
}
function _dbgSite(tag, n) {
  if (!_OPTSDBG || n.start == null) return;
  const t = _SRC.slice(Math.max(0, n.start), Math.min(_SRC.length, n.start + 220)).replace(/\s+/g, " ");
  console.error(`[opts:${tag}] ${t}`);
}
function optsSink(n, scope) {
  const cn = n.callee && n.callee.type === "MemberExpression" && n.callee.property &&
             (n.callee.property.name || n.callee.property.value);
  if (cn && CACHE_OPS.has(cn)) return null;          // cache mutation, not a request
  let opts = null, optsIdx = -1, hasQK = false;
  for (let i = 0; i < n.arguments.length; i++) {
    const a = n.arguments[i];
    if (!a || a.type !== "ObjectExpression") continue;
    for (const p of a.properties) {
      if (p.type === "ObjectProperty" && p.key) {
        const k = p.key.name ?? p.key.value;
        if (NET_SIGNAL.has(k)) { opts = a; optsIdx = i; }
        if (k === "queryKey") hasQK = true;
      }
    }
    if (opts) break;
  }
  if (!opts) return null;
  const oi = optsInfo(opts, scope);                       // method, body_keys, _url (from a url: key)
  let url = oi._url;
  if (!url || url.template === "{EXPR}") {                // else: first path-like positional arg (not the opts obj)
    for (let i = 0; i < n.arguments.length; i++) {
      if (i === optsIdx) continue;
      const u = resolveUrl(n.arguments[i], scope);
      const sk = u.template.replace(/\{[^}]*\}/g, "");
      if (LOOKS_PATH(u.template) || LOOKS_PATH(sk) || /^https?:\/\//.test(u.template)) { url = u; break; }
    }
  }
  const method = oi.method || (hasQK ? "GET" : (oi.body_keys ? "POST" : "GET"));
  if (!url) {
    // no resolvable URL, but a real BODY is present (e.g. claude.ai's `ng(i,{method,body:s})` where i is an IIFE
    // result → {EXPR}): still emit a body-carrying record so the body survives; the co-location merge staples it
    // onto the matching resolved-URL record in the same function.
    _dbgSite("nourl", n);
    if (oi.body_keys && oi.body_keys.filter((k) => !String(k).startsWith("{")).length >= 2)
      return { sink: "opts-sink", kind: "xhr", method, template: "{EXPR}", descriptor: oi.descriptor, body_keys: oi.body_keys, body_encoding: oi.body_encoding, content_type: oi.content_type, body_schema: oi.body_schema, query_schema: oi.query_schema, req_headers: oi.headers, credentials: oi.credentials, body_src: oi.body_src, headers: oi.headers, response_keys: oi.response_keys, dynamic: true };
    return null;
  }
  if (!oi.body_keys && ["POST","PUT","PATCH"].includes(String(method).toUpperCase())) _dbgSite("nobody-opts", n);
  const rec = { sink: "opts-sink", kind: "xhr", method, template: url.template, descriptor: oi.descriptor, body_keys: oi.body_keys, body_encoding: oi.body_encoding, content_type: oi.content_type, body_schema: oi.body_schema, query_schema: oi.query_schema, req_headers: oi.headers, credentials: oi.credentials, body_src: oi.body_src, headers: oi.headers, response_keys: oi.response_keys, dynamic: url.dynamic, has_body_slot: !!oi.has_body_slot };
  if (_BODYDBG && !oi.body_keys && ["POST", "PUT", "PATCH"].includes(String(method).toUpperCase()) && opts) {
    const bp = opts.properties.find((p) => p.type === "ObjectProperty" && p.key &&
      ["body", "json", "data", "payload"].includes(String(p.key.name ?? p.key.value)));
    if (bp) _dbgBody(rec, bp.value);
  }
  return rec;
}

function calleeName(node) {
  // return {obj, prop} name strings for a MemberExpression callee, else {fn}
  if (node.type === "MemberExpression" && node.property)
    return { obj: node.object && (node.object.name || (node.object.property && node.object.property.name)),
             objNode: node.object,        // kept so a non-Identifier receiver (an inline client choice) can resolve
             prop: node.property.name || node.property.value };
  if (node.type === "Identifier") return { fn: node.name };
  return {};
}

// A RECEIVER THAT IS PROVABLY NOT AN HTTP CLIENT.
//
// `.get`/`.post`/`.put`/`.delete`/`.head` are HTTP verbs and they are also ordinary methods on several
// built-ins, so the member sink below fired on all of them and the only thing standing between that and
// a fabricated endpoint was whether the ARGUMENT looked path-shaped. Plenty do:
//
//     const cache = new Map();            cache.get("user/profile")       -> GET user/profile
//     const qs = new URLSearchParams(s);  qs.get("redirect/next")         -> GET redirect/next
//
// Both were emitted as endpoints. Arguing about the argument cannot fix this, "user/profile" is a
// perfectly good path and the problem was never the path. The receiver is what is wrong, and where the
// receiver is a local binding its CONSTRUCTION SITE says what it is. That is evidence, not a guess: a
// value built by `new Map()` is a Map, and a Map has no endpoints.
//
// Only built-ins whose methods actually collide with an HTTP verb are listed. The check is deliberately
// one-sided, it rejects a receiver it can PROVE is not a client, and says nothing about the rest, so a
// client this cannot resolve is treated exactly as it was before.
const NON_HTTP_CTORS = new Set([
  "Map", "WeakMap", "Set", "WeakSet",     // get/set/add/delete/has
  "URLSearchParams", "Headers", "FormData",  // get/set/delete/has/append
]);

function receiverIsNotHttpClient(objNode, scope) {
  if (!objNode || objNode.type !== "Identifier" || !scope || !scope.getBinding) return false;
  const b = scope.getBinding(objNode.name);
  if (!b || !b.path || !b.path.node) return false;
  const node = b.path.node;
  const init = node.type === "VariableDeclarator" ? node.init : null;
  if (!init || init.type !== "NewExpression") return false;
  const callee = init.callee;
  const name = callee && (callee.name || (callee.property && callee.property.name));
  return !!name && NON_HTTP_CTORS.has(name);
}

// ── INTER-PROCEDURAL wrapper resolution ─────────────────────────────────────────────────────────────────────
// The deepest RE piece. A custom fetch wrapper is a fn whose body calls a sink with a URL template built from its
// PARAMETERS: `function or(e,t,s,n){ const a=`/v1/sessions/${f(e)}/backend-admin/${s}`; fetch(a,{method:n.method}) }`.
// Name-matching sees `or(...)` as an unknown callee; the URL only survives as a literal fragment ("secrets"). We
// instead (pass 1) record each wrapper's URL-template-in-terms-of-params, then (pass 2) at every call site
// substitute the literal args → the FULL path `/v1/sessions/{EXPR}/backend-admin/database/query`. Per-chunk, so
// minified name collisions (`or` meaning different things in different chunks) resolve correctly & independently.
const SINK_FNS = new Set(["fetch"]);
// Map an axios INSTANCE variable → its resolved baseURL, so a later `X.get("/path")` becomes the FULL url instead of
// collapsing to {EXPR}. `const X = <axios>.create({baseURL: …})` and `this.X = …create(…)`. baseURL is a strong axios
// signal → requiring it avoids matching unrelated `.create()`. baseURL is resolved via strFromNode (incl. the new
// config-object member resolution, so `create({baseURL: cfg.base})` works when cfg is a literal config object).

// ── ANGULARJS DEPENDENCY INJECTION: WHAT IS THIS RECEIVER, REALLY? ────────────────────────────────────
// AngularJS's minification-safe annotation names every injected service positionally:
//
//     .run(["$templateCache", "$interpolate", function (Ds, e$) { Ds.put("/dialogs/error.html", "<div…") }])
//
// so `Ds` is `$templateCache` and `Ds.put(key, template)` writes a cache entry. It is not an HTTP PUT, and
// it was being recorded as one: four fabricated `PUT /dialogs/*.html` endpoints on this target, which then
// attracted a base URL and became `PUT http://www.amcharts.com/lib/3/images/dialogs/error.html`, a route
// that exists nowhere, indistinguishable in the map from one that does.
//
// Reading the annotation is not name-guessing: the array IS Angular's published API for surviving
// minification, in the same category as knowing what `fetch` is called. The service names it contains are
// framework constants, not anything this application chose.
const _NG_NOT_HTTP = new Set([
  "$templateCache", "$cacheFactory", "$rootScope", "$scope", "$q", "$timeout", "$interval", "$window",
  "$document", "$compile", "$parse", "$interpolate", "$filter", "$log", "$injector", "$rootElement",
  "$animate", "$sce", "$sanitize", "$cookies", "$localStorage", "$sessionStorage", "$state",
  "$stateParams", "$location", "$anchorScroll", "$exceptionHandler", "$controller",
]);

function angularServiceOf(objNode, scope) {
  if (!objNode || objNode.type !== "Identifier") return null;
  const b = scope && scope.getBinding && scope.getBinding(objNode.name);
  if (!b || b.kind !== "param" || !b.path) return null;
  const fnPath = b.path.getFunctionParent ? b.path.getFunctionParent() : null;
  if (!fnPath || !fnPath.node) return null;
  const idx = (fnPath.node.params || []).findIndex(
    (pp) => pp && pp.type === "Identifier" && pp.name === objNode.name);
  if (idx < 0) return null;
  // the function is the last element of the annotation array: ["$a","$b", fn]
  const arr = fnPath.parentPath && fnPath.parentPath.node;
  if (!arr || arr.type !== "ArrayExpression") return null;
  const names = (arr.elements || []).filter((e) => e && e.type === "StringLiteral").map((e) => e.value);
  return idx < names.length ? names[idx] : null;
}

function collectAxiosInstances(ast) {
  const inst = {};
  // Anchor on the SHAPE (an object argument carrying `baseURL`), never on the callee being named `create`.
  // One app's staking app builds every one of its nine service clients through a local factory,
  // `qb = ({baseURL,timeout,headers,networkId}) => wye(Zv.create({baseURL,…}), networkId)`, so `qb({baseURL:…})`
  // is an axios instance in every way that matters and matched none of `.create`. Requiring the name is the same
  // mistake as detecting headers by receiver name: minifiers and one wrapper layer both defeat it, while `baseURL`
  // as a config key is essentially unique to HTTP clients.
  // DETECT THE BASE BY THE VALUE, NOT BY THE KEY.
  //
  // This matched the key `baseURL` exactly, and `baseURL` is AXIOS's option name. Applying axios's
  // vocabulary to a client that is not axios is how one target's whole API went missing: their client is a
  // custom class configured `baseUrl: "https://api.client.example"`, one lowercase letter, so 186 of
  // ~280 call sites lost their base and 62 API endpoints were filed under the frontend that served the
  // bundle. A rule that turns on a target's spelling choice also fails on `base_url`, `apiBase`, `urlBase`
  // or a name in any language the developers happen to think in.
  //
  // The VALUE does not have that problem. `https://api.client.example` is an origin whatever key it sits
  // under, and a bare origin inside a client's construction options is a base, there is nothing else it
  // could be. So: find properties whose value RESOLVES to an origin, and fall back to the name only to
  // break a tie between several. Matching a published library option (fetch's `method`, Firebase's
  // `projectId`) stays fine; matching what one application called its own variable does not.
  const IS_ORIGIN = /^(?:https?:)?\/\/[A-Za-z0-9.\-]+(?::\d{1,5})?(?:\/[A-Za-z0-9._~\/-]{0,48})?\/?$/;
  // Deliberately a family, not a literal. Matches base/baseUrl/baseURL/base_url/apiBase/apiBaseUrl/
  // baseHost/origin/endpoint/server/rootUrl and the obvious spelling variants of each.
  const BASEISH = /^(api|root|service|server)?_?(base)?_?(url|uri|host|hostname|origin|endpoint|server|base)$/i;
  // A key that says "base" OUTRIGHT, as opposed to the broad family above. The distinction earns its
  // keep for a smaller reason than it was introduced for: it admits `basePath` and `prefixUrl`, which
  // the family above never matched at all, so an ORIGIN under either of those keys is now a base where
  // before it was ignored. It does NOT admit a relative value, see the value test below for why.
  const EXPLICIT_BASE = /^(?:api|root|service|server)?_?base_?(?:url|uri|path|address|host)?$|^prefix_?url$/i;
  const originProps = (call, scope) => {
    const arg = call.arguments && call.arguments[0];
    if (!arg || arg.type !== "ObjectExpression") return [];
    const hits = [];
    for (const p of arg.properties) {
      if (p.type !== "ObjectProperty" || !p.key || !p.value) continue;
      // Key first: it decides which values are admissible, and it skips resolving the value at all for
      // the properties that cannot be a base anyway.
      const key0 = String(p.key.name ?? p.key.value ?? "");
      const explicit = EXPLICIT_BASE.test(key0);
      if (!explicit && !BASEISH.test(key0)) continue;
      const r = strFromNode(p.value, scope, 0, []);
      if (!r || !r.s || r.s === "{EXPR}") continue;
      // THE VALUE MUST STILL BE AN ORIGIN, and a relative base is deliberately NOT accepted. Measured:
      // accepting one under an explicit base key fixed the fixture case and made Grafana worse, adding
      // four paths like
      //     /apis/dashboard.grafana.app/v0alpha1/namespaces/default/api/datasources/uid/{P}/correlations
      // which glues the k8s-style resource base onto the legacy REST API, two different Grafana APIs,
      // and four correct records (`DELETE /api/datasources/uid/{P}/correlations/{b}`) replaced by wrong
      // ones. The base is real and belongs to an RTK Query client whose own paths are `/correlations`;
      // it reached these calls because `applyBase` looks instances up by receiver NAME, and a minified
      // name collides across modules. An origin value masks that, a wrong origin is obvious and rare,
      // while a relative one silently produces a real-looking route pointed at the wrong API, which is
      // the worst output this tool can produce. Fixing it properly means attributing a base by the
      // receiver's BINDING rather than its name; until then this stays origin-only.
      if (!IS_ORIGIN.test(r.s)) continue;
      // THE VALUE SAYS IT COULD BE A BASE; THE KEY SAYS IT IS ONE.
      //
      // Value-shape alone is not enough and it is worth being precise about why, because the first cut of
      // this dropped the name test entirely and immediately fabricated four endpoints. An options object
      // routinely carries several origins, a docs link, a CDN, an asset root, and they are all origins.
      // amcharts is configured `pathToImages: "http://www.amcharts.com/lib/3/images/"`, which no test on
      // the VALUE can distinguish from an API base, and treating it as one produced
      // `GET http://www.amcharts.com/lib/3/images/environment/list/names` on four unrelated clients.
      //
      // So the key is still consulted, but as a broad, case-insensitive FAMILY rather than the single
      // literal `baseURL`, which was axios's spelling and missed one target's `baseUrl` by one character
      // and took 186 call sites with it. baseUrl, baseURL, base_url, apiBase, apiBaseUrl, baseHost,
      // origin, endpoint, server all match. A name outside the family is not trusted on its own.
      hits.push({ key: key0, base: r.s, dyn: r.dyn });
    }
    return hits;
  };
  // The options object is not always the outermost call's first argument. Generated OpenAPI clients wrap:
  //     client = createClient(createConfig({baseUrl: "https://api.client.example"}))
  // and looking one level deep misses every endpoint behind it. Walk the initialiser subtree for ANY
  // object literal, bounded, and let the value test decide which one is a base.
  // ARGUMENTS ONLY, and only through nested calls. The first version of this also walked `callee`,
  // `object` and `body`, which is how an amcharts options object buried in an unrelated subtree became
  // the base for four different clients: `/environment/list/names` and `/logs/overview`, real one target
  // API paths, came out as `http://www.amcharts.com/lib/3/images/environment/list/names`. A fabricated
  // absolute path is worse than a bare relative one, because the relative path is merely incomplete while
  // the fabricated one is a confident 404 that nothing downstream can tell from a real route.
  //
  // `createClient(createConfig({baseUrl}))` nests through arguments and nothing else, which is the whole
  // shape this needs to reach.
  const objectsIn = (n, depth = 0, out = []) => {
    if (!n || typeof n !== "object" || depth > 3 || out.length > 4) return out;
    if (n.type === "ObjectExpression") { out.push(n); return out; }
    if (n.type === "CallExpression" || n.type === "NewExpression") {
      for (const a of n.arguments || []) objectsIn(a, depth + 1, out);
    }
    return out;
  };
  const isCreate = (n) =>
    !!(n && (n.type === "CallExpression" || n.type === "NewExpression") && objectsIn(n).length);
  const baseOf = (call, scope) => {
    const hits = [];
    for (const obj of objectsIn(call)) hits.push(...originProps({ arguments: [obj] }, scope));
    if (!hits.length) return null;
    if (hits.length === 1) return { base: hits[0].base, dyn: hits[0].dyn };
    // Several origins in one options object, a base beside a docs link or a websocket URL. Break the tie
    // on the NAME here, where it is a hint and not the gate; if that does not single one out, take none.
    // A wrong base fabricates routes that 404, which is worse than a bare relative path that is merely
    // incomplete (the same reasoning as the ambiguous-binding rule below).
    const named = hits.filter((h) => BASEISH.test(h.key));
    if (named.length === 1) return { base: named[0].base, dyn: named[0].dyn };
    return null;
  };
  // A `this.<prop>` key is NOT unique across a bundle. One app's app has many service classes that each do
  // `this.axiosInstance = axios.create({baseURL: …})` with a DIFFERENT base, and keying the map on the bare
  // property name let the last one win: every other class's calls inherited the saved-blocks base, so
  // `this.axiosInstance.post("/events/created")` was emitted as
  // `{API_BASE_URL}/accounts/{_id}/items/events/created`, a route that does not exist.
  // It inflated the contract with dozens of fabricated paths and every one of them 404s.
  // An ambiguous binding must yield NO base, not a wrong one: the bare relative path is true as far as it goes
  // and the firing side probes prefixes anyway, whereas a fabricated absolute path is unfalsifiable noise.
  const bases = {};                       // key -> Set of distinct resolved bases
  try {
    traverse(ast, {
      VariableDeclarator(path) {
        if (path.node.id.type === "Identifier" && isCreate(path.node.init)) {
          const b = baseOf(path.node.init, path.scope);
          if (b) { inst[path.node.id.name] = b; (bases[path.node.id.name] ||= new Set()).add(b.base); }
        }
      },
      AssignmentExpression(path) {
        const n = path.node; if (!isCreate(n.right)) return;
        let key = null;
        if (n.left.type === "Identifier") key = n.left.name;
        else if (n.left.type === "MemberExpression" && n.left.object.type === "ThisExpression" && n.left.property && n.left.property.name)
          key = "this." + n.left.property.name;
        if (key) {
          const b = baseOf(n.right, path.scope);
          if (b) { inst[key] = b; (bases[key] ||= new Set()).add(b.base); }
        }
      }
    });
  } catch {}
  // drop every key the file bound to two different bases, we cannot tell which call site meant which
  for (const k of Object.keys(bases)) if (bases[k].size > 1) delete inst[k];
  // ALIASES. The call sites rarely name the instance directly: an app with a mainnet and a testnet deployment picks
  // between two clients, and the chooser is what the code calls. One app: `n = useMemo(() => e===1 ? clientA : clientB)`,
  // where clientA is the mainnet client and clientB the testnet one, so `n.get("/validators")` hits either
  // staking-api.example.com/api/v2 or staking-api-test…, depending on a runtime chain id. Both are real
  // endpoints and we want both, so a conditional contributes EVERY branch rather than being written off as dynamic.
  // Two passes reaches an alias-of-an-alias; declaration order in the bundle is not dependency order.
  const unwrapAlias = (node, depth) => {
    if (!node || depth > 4) return null;
    if (node.type === "Identifier") return inst[node.name] ? [inst[node.name]] : null;
    if (node.type === "ConditionalExpression") {
      const a = unwrapAlias(node.consequent, depth + 1), b = unwrapAlias(node.alternate, depth + 1);
      return (a || b) ? [...(a || []), ...(b || [])] : null;
    }
    // any single-argument wrapper around a thunk, useMemo(() => X, deps), lazy(() => X), a plain IIFE
    if (node.type === "CallExpression") {
      const fn = node.arguments.find((a) => a && (a.type === "ArrowFunctionExpression" || a.type === "FunctionExpression"));
      if (fn) {
        let ret = fn.body;
        if (ret && ret.type === "BlockStatement") { const rs = ret.body.find((s) => s.type === "ReturnStatement"); ret = rs ? rs.argument : null; }
        return unwrapAlias(ret, depth + 1);
      }
    }
    return null;
  };
  try {
    for (let pass = 0; pass < 2; pass++) traverse(ast, {
      VariableDeclarator(path) {
        const n = path.node;
        if (n.id.type !== "Identifier" || inst[n.id.name] || !n.init) return;
        const got = unwrapAlias(n.init, 0);
        if (!got || !got.length) return;
        const bases = [...new Set(got.map((g) => g.base))];
        inst[n.id.name] = { base: bases[0], dyn: got.some((g) => g.dyn), alts: bases.slice(1) };
      }
    });
  } catch {}
  return inst;
}

// A path through a client, with the client's base in front of it.
function _withBase(inst, u) {
  if (!inst || /^(?:https?:)?\/\//i.test(u.template)) return u;
  // a client bound to more than one base (mainnet/testnet) rides the existing enum-expansion marker, so `push`
  // fans it out into one record per host with no new machinery.
  if (inst.alts && inst.alts.length) {
    const all = [inst.base, ...inst.alts].map((b) => String(b).replace(/\/+$/, ""));
    return { template: "\x02" + all.join("|") + "\x02/" + String(u.template).replace(/^\/+/, ""), dynamic: u.dynamic || inst.dyn };
  }
  return { template: joinBase(inst.base, u.template), dynamic: u.dynamic || inst.dyn };
}

// prefix a resolved base onto a relative path template (dedup the slash); skip if the path is already absolute.
function joinBase(base, tmpl) {
  if (!base || /^https?:\/\//i.test(tmpl)) return tmpl;       // already absolute → leave it
  return base.replace(/\/+$/, "") + "/" + String(tmpl).replace(/^\/+/, "");
}

// ── GENERIC URL-OBJECT sink discovery ────────────────────────────────────────────────────────────────────────
// `new URL(path, base)` constructed then handed to ANY function is a network send, you build a URL object to SEND it.
// Rooting the sink on the URL (not the callee name) catches bare-function axios instances (one per service: sv/av/lv…)
// and any custom client the name-based rules miss. Stack-agnostic by construction.
function newUrlNode(node, scope, depth) {
  depth = depth || 0;
  if (!node || depth > 4) return null;
  if (node.type === "SequenceExpression" && node.expressions.length)
    return newUrlNode(node.expressions[node.expressions.length - 1], scope, depth + 1);
  if (node.type === "NewExpression" && node.callee && node.callee.name === "URL") return node;
  if ((node.type === "MemberExpression" || node.type === "OptionalMemberExpression") && node.property &&
      (node.property.name === "href" || node.property.name === "pathname"))
    return newUrlNode(node.object, scope, depth + 1);
  if (node.type === "CallExpression" && node.callee && node.callee.type === "MemberExpression" &&
      node.callee.property && node.callee.property.name === "toString")
    return newUrlNode(node.callee.object, scope, depth + 1);
  if (node.type === "Identifier" && scope && scope.getBinding) {
    const b = scope.getBinding(node.name);
    if (b && b.path && b.path.node && b.path.node.init) return newUrlNode(b.path.node.init, b.scope, depth + 1);
  }
  return null;
}
// find `…paths.KEY` ANYWHERE in a URL expression (plain member, inside a template literal, under `.replace(...)`, a
// binary concat, etc.) → the registry key. Bridges every idiom to the path table even when the surrounding expr is
// dynamic (`\`${paths.checkUsername}/${e}\``, `paths.invoiceLines.replace("{invoiceId}", e)`).
function findRegistryKey(node, depth) {
  depth = depth || 0;
  if (!node || depth > 7 || typeof node !== "object") return null;
  if ((node.type === "MemberExpression" || node.type === "OptionalMemberExpression") && node.object && node.property && !node.computed) {
    const obj = node.object;
    if ((obj.type === "MemberExpression" && obj.property && (obj.property.name || obj.property.value) === "paths") ||
        (obj.type === "Identifier" && obj.name === "paths"))
      return node.property.name || node.property.value;
  }
  for (const k of ["expressions", "arguments", "callee", "object", "left", "right", "argument", "quasis"]) {
    const v = node[k];
    if (Array.isArray(v)) { for (const c of v) { const r = findRegistryKey(c, depth + 1); if (r) return r; } }
    else if (v && typeof v === "object") { const r = findRegistryKey(v, depth + 1); if (r) return r; }
  }
  return null;
}
function resolveNewUrl(urlNode, scope, paramNames) {   // `new URL(pathArg, baseArg)` → base + path (URL(rel, base) semantics)
  const pathArg = urlNode.arguments[0], baseArg = urlNode.arguments[1];
  const p = pathArg ? resolveUrl(pathArg, scope, paramNames) : { template: "", dynamic: true };
  let base = "";
  if (baseArg) { const bs = strFromNode(baseArg, scope, 0, paramNames || null); if (bs && bs.s && bs.s !== "{EXPR}") base = bs.s; }
  const template = base ? joinBase(base, p.template) : p.template;
  return { template, dynamic: p.dynamic || (!!baseArg && !base), key: findRegistryKey(pathArg) };
}

// enclosing function's binding NAME + params, so a body that's a param can be resolved from that fn's call-sites.
function enclosingFnInfo(path) {
  const fp = path.getFunctionParent && path.getFunctionParent();
  if (!fp) return null;
  const fn = fp.node; let name = null;
  if (fn.id && fn.id.name) name = fn.id.name;                                        // named function
  else if ((fn.type === "ObjectMethod" || fn.type === "ClassMethod" || fn.type === "ClassPrivateMethod") && fn.key)
    name = fn.key.name || fn.key.value;                                              // method shorthand `update(e){…}`, key on the fn
  else {
    const pn = fp.parentPath && fp.parentPath.node;                                  // arrow assigned to a name
    if (pn) {
      if (pn.type === "VariableDeclarator" && pn.id) name = pn.id.name;
      else if (pn.type === "AssignmentExpression" && pn.left) name = pn.left.name || (pn.left.property && pn.left.property.name);
      else if ((pn.type === "ObjectProperty" || pn.type === "Property" || pn.type === "ClassProperty" ||
                pn.type === "PropertyDefinition" || pn.type === "ClassPrivateProperty") && pn.key)   // class field = arrow
        name = pn.key.name || pn.key.value;
    }
  }
  return { name, params: fn.params || [] };
}
// The DEVELOPER'S OWN NAME for the endpoint, plus the resource it was filed under. A hand-written API client is an
// object of named methods grouped by resource:
//   {validators:{ addNewNodeToNetwork: async e => n.post(`/validators/add`, e), … }, auth:{ login: … }}
// `addNewNodeToNetwork` says more about intent than `POST /validators/add` ever will, and the enclosing keys are an
// AUTHORED feature map, strictly better than the one we infer by clustering path prefixes, and available for free.
// Property keys are the data contract, so they survive minification; we were walking straight past them.
// Skip 1-2 char keys: those are minified locals, not names.
const _structuredQ = (v) => !!v && typeof v !== "string" && (Array.isArray(v) ? v.length : Object.keys(v).length);
function opNameOf(path) {
  const chain = [];
  // Start at the enclosing FUNCTION, not the call. `fetchSvcBFees: async e => ({height:"0", result: r.get(…)})`
  // wraps the call in a property named `result`; walking up from the call site names the endpoint after the field
  // it happens to populate. The function IS the operation, so its own key is the name.
  let p = (path.getFunctionParent && path.getFunctionParent()) || path;
  for (let i = 0; i < 24 && p && chain.length < 3; i++) {
    const n = p.node;
    if (n && (n.type === "ObjectProperty" || n.type === "Property" || n.type === "ObjectMethod" ||
              n.type === "ClassMethod" || n.type === "ClassProperty" || n.type === "PropertyDefinition") && n.key) {
      const k = n.key.name ?? n.key.value;
      if (k && /^[A-Za-z_$][\w$]{2,}$/.test(String(k))) chain.push(String(k));
    }
    p = p.parentPath;
  }
  return chain.length ? { op: chain[0], group: chain.slice(1) } : null;
}

// tag a record whose body is a whole PARAM so the post-pass resolves it from the wrapper's call-sites (don't give up
// at "it's a variable", follow it one hop to the caller). Skip 1-2 char minified names (collision-prone).
function tagBodyParam(rec, argNode, path) {
  const info = enclosingFnInfo(path);
  if (!info || !info.name || info.name.length < 3) return;
  const idx = info.params.findIndex((p) => p.type === "Identifier" && p.name === argNode.name);
  if (idx >= 0) { rec._bpFn = info.name; rec._bpIdx = idx; }
}

// The body inside an OPTIONS OBJECT is a parameter just as often as a positional body is:
//   updateBillingAccount = (t, n = {}) => this.request({path:"/billing/account", method:"PATCH", body:t, ...n})
// Generated clients (swagger-typescript-api, openapi-typescript-codegen) emit exactly that for EVERY write in the
// API, so one unhandled shape costs a whole host: one console had 130 of 133 writes with no body, while the
// call sites, `Le.updateBillingAccount({billing_account:x})`, sat in the same bundle, already indexed. Method
// names survive minification because they ARE the contract, so keying the lookup on them is safe here.
function tagOptsBodyParam(rec, callNode, path) {
  if (!rec || !callNode || !Array.isArray(callNode.arguments)) return;
  for (const a of callNode.arguments) {
    if (!a || a.type !== "ObjectExpression") continue;
    const p = a.properties.find((pr) => pr.type === "ObjectProperty" && pr.key &&
      ["body", "json", "data", "payload"].includes(String(pr.key.name ?? pr.key.value)));
    if (!p) continue;
    let v = p.value;
    if (v && v.type === "CallExpression" && v.callee && v.callee.property &&
        v.callee.property.name === "stringify") v = v.arguments[0];
    if (v && v.type === "Identifier") { tagBodyParam(rec, v, path); return; }
  }
}

function collectWrappers(ast) {
  // A null prototype, because the keys are NAMES FROM THE SOURCE. Once class methods are collected, one of
  // those names is `constructor`, and on a plain object `wrappers["constructor"]` returns Object from the
  // prototype chain, so `(wrappers[name] || []).some(...)` threw "some is not a function" and the extractor
  // returned NOTHING for any file containing a class. It failed loudly per file and quietly in aggregate:
  // 11,559 records became 4,703 with no error surfaced, because the batch runner only reports a failure
  // when the process also produces no stdout. `toString`, `valueOf` and `hasOwnProperty` are the same trap.
  const wrappers = Object.create(null);
  const consider = (name, params, fnPath, isMethod) => {
    // Keyed by NAME alone, this kept the first function of that name and ignored the rest. In a minified
    // bundle every other function is called `t`, so a second scope's `t` inherited the first one's URL
    // template, `t("b")` came out as /api/one/b, marked dynamic:false, i.e. asserted as fully resolved
    // when the request is /api/two/b. Keep them all, each tagged with the function node it came from, and
    // let the call site match on the binding it actually resolves to.
    if (!name || !params.length) return;
    if ((wrappers[name] || []).some((w) => w.fnNode === fnPath.node)) return;
    // A parameter WITH A DEFAULT is an AssignmentPattern, not an Identifier, and was being mapped to a
    // sentinel that matches nothing, so `post(path, data, params, headers, suffix = ".json")` never even
    // recognised `suffix` as a parameter, and the default that completes the URL was invisible. The name
    // comes from the left side and the default from the right, and the default is what a call site that
    // omits the argument actually sends.
    const paramNames = params.map((p) => (p.type === "Identifier" ? p.name
                                        : p.type === "AssignmentPattern" && p.left.type === "Identifier"
                                          ? p.left.name : "\x00"));
    const paramDefaults = params.map((p) => (p.type === "AssignmentPattern" ? p.right : null));
    let best = null, bestMethod = null, bestBody = null, bestBodyIdx = null, bestInner = null, bestViaFetch = false;
    fnPath.traverse({
      CallExpression(p) {
        if (best) return;
        const n = p.node, c = n.callee;
        // THE SINK IS A WRAPPER ANOTHER MODULE EXPORTS. claude.ai's `w(e,t){return c(e,t)}`, where `c` is
        // imported and is `fetchAsDocumentAccount(e,t)`, which hands `e` to fetch. Read through it.
        const ext = _exportedWrapper(c, p.scope);
        if (ext) {
          const tm = _composeWrapper(ext, n.arguments, p.scope, paramNames);
          if (tm.includes("\x01")) {
            best = tm; bestViaFetch = !!ext.viaFetch;
            const oa = n.arguments.find((a) => a && a.type === "ObjectExpression");
            bestMethod = (oa && optsInfo(oa, p.scope).method) || ext.method || null;
            if (ext.body_idx != null && n.arguments[ext.body_idx] && n.arguments[ext.body_idx].type === "Identifier") {
              const pi = paramNames.indexOf(n.arguments[ext.body_idx].name);
              if (pi >= 0) bestBodyIdx = pi;
            }
          }
          return;
        }
        // a body sink INSIDE the wrapper: raw fetch, a client method (.get/.post), OR anything carrying an options
        // object (react-query hook / custom fetcher). Broadened so base-param wrappers around ANY sink resolve,
        // claude.ai's org helpers wrap the react-query hook `t(`${e}/invites`,{queryKey})`, not raw fetch.
        const isFetch = c && c.type === "Identifier" && SINK_FNS.has(c.name);
        const isMethodCall = c && c.type === "MemberExpression" && c.property && HTTP_METHODS.has((c.property.name || "").toLowerCase());
        const optsArg = n.arguments.find((a) => a && a.type === "ObjectExpression" &&
          a.properties.some((pr) => pr.type === "ObjectProperty" && pr.key && NET_SIGNAL.has(pr.key.name ?? pr.key.value)));
        if (!(isFetch || isMethodCall || optsArg) || !n.arguments[0]) return;
        // The URL is not always argument ZERO. A client class hands its whole request to one dispatcher:
        //     return this.dispatch({path: `/1.1/${path}${suffix}`, method: "POST", data, params, headers})
        // where argument 0 IS the options object and the path is a key inside it. Reading argument 0 as the
        // URL then resolves an ObjectExpression, finds no template, and the method never registers as a
        // wrapper, so every call through it keeps the bare relative string the caller passed.
        let urlNode = n.arguments[0];
        if (urlNode.type === "ObjectExpression") {
          const pk = urlNode.properties.find((pr) => pr.type === "ObjectProperty" && pr.key &&
                     ["path", "url", "uri", "endpoint"].includes(pr.key.name ?? pr.key.value));
          if (pk) urlNode = pk.value;
        }
        let t = resolveUrl(urlNode, p.scope, paramNames);
        // a method call on a client: the client's base goes in front, as at any other call through it
        if (isMethodCall) { const cb = callClient(c, p.scope); if (cb) t = _withBase(cb, t); }
        // must reference a param; needs a real literal path segment UNLESS it's a definite fetch/Request sink,
        // `fetch(`${base}/${action}`)` is an all-param URL-builder (AI streaming helpers); precision comes from the
        // call-site arg resolution + the LOOKS_PATH filter, not from a literal segment here.
        const realSeg = /[/][\w-]/.test(t.template.replace(/\x01\w+\x01|\{[^}]*\}/g, ""));
        if (t.template.includes("\x01") && (realSeg || isFetch)) {
          best = t.template;   // references a param AND has a real literal path segment
          bestInner = { call: n, scope: p.scope };
          bestViaFetch = !!isFetch;
          // capture the method + body from THIS internal sink so the resolved call-site record is COMPLETE
          const oi = optsArg ? optsInfo(optsArg, p.scope) : {};
          const hasQK = optsArg && optsArg.properties.some((pr) => pr.key && (pr.key.name ?? pr.key.value) === "queryKey");
          bestMethod = oi.method || (isMethodCall ? c.property.name.toUpperCase() : (hasQK ? "GET" : (oi.body_keys ? "POST" : null)));
          // Two body forms were being dropped here, and together they are the single largest cause of bodyless
          // writes in the corpus (129 of 233 unresolved bodies came from wrapper sinks):
          //   1. a POSITIONAL body, `client.post(url, data)` carries no options object, so optsInfo never ran;
          //   2. a body that is one of the WRAPPER'S OWN PARAMETERS, unknowable inside the wrapper, but sitting
          //      in plain sight at every call site. The URL template already resolves through call-site args by
          //      param index; the body simply never learned the same trick.
          let bnode = null;
          if (!optsArg && isMethodCall && /^(post|put|patch)$/i.test(c.property.name || "")) {
            const cand = n.arguments[1] || null;
            // …but arg1 is only the body if it is not a CONFIG bag. `X.post(url,{method,signal})` reported a body of
            // ["method","signal"], a fabricated contract, which is worse than an absent one. Same content test as
            // the DELETE case: config keys mean config.
            bnode = looksLikeConfigBag(cand) ? null : cand;
          }
          if (!bnode && optsArg) {
            const bp = optsArg.properties.find((pr) => pr.type === "ObjectProperty" && pr.key &&
              ["body", "json", "data", "payload"].includes(String(pr.key.name ?? pr.key.value)));
            bnode = bp ? bp.value : null;
            if (bnode && bnode.type === "CallExpression" && bnode.callee && bnode.callee.property &&
                bnode.callee.property.name === "stringify") bnode = bnode.arguments[0] || null;
          }
          bestBody = oi.body_keys || (bnode ? bodyKeys(bnode, p.scope) : null);
          if (bnode && bnode.type === "Identifier") {
            const pi = paramNames.indexOf(bnode.name);
            if (pi >= 0) bestBodyIdx = pi;                 // resolve it from the caller's argument at this index
          }
        }
      },
      NewExpression(p) {
        if (best) return;
        if (p.node.callee && p.node.callee.name === "Request") {
          const t = resolveUrl(p.node.arguments[0], p.scope, paramNames);
          if (t.template.includes("\x01")) { best = t.template; bestViaFetch = true; }
        }
      },
    });
    if (best) (wrappers[name] = wrappers[name] || []).push(
      { tmpl: best, paramNames, paramDefaults, method: bestMethod, body_keys: bestBody,
        body_idx: bestBodyIdx, fnNode: fnPath.node, isMethod: !!isMethod, inner: bestInner,
        viaFetch: bestViaFetch });
  };
  traverse(ast, {
    FunctionDeclaration(p) { consider(p.node.id && p.node.id.name, p.node.params, p); },
    VariableDeclarator(p) {
      const init = p.node.init;
      if (init && (init.type === "FunctionExpression" || init.type === "ArrowFunctionExpression") &&
          p.node.id && p.node.id.type === "Identifier")
        consider(p.node.id.name, init.params, p.get("init"));
    },
    // A CLIENT CLASS is the shape every SDK is written in, and it was not being collected at all:
    //
    //   post(path, data, params, headers, suffix = ".json", host) {
    //     return this.dispatch({path: `/1.1/${path}${suffix}`, method: "POST", data, params, headers});
    //   }
    //
    // The method BUILDS the URL from its own argument, so `client.post("account/deactivate", body)` is
    // POST /1.1/account/deactivate.json, and without this the call site yields the bare relative string
    // `account/deactivate`, which is not a path anyone can request. x's whole legacy API reaches the map
    // that way.
    "ClassMethod|ObjectMethod"(p) {
      const k = p.node.key;
      if (k && !p.node.computed) consider(k.name ?? k.value, p.node.params, p, true);
    },
  });
  // A WRAPPER AROUND A WRAPPER. supabase's generated client has one function per endpoint calling one
  // that adds the base:
  //     g = async (n, a) => fetch(new URL(`${base}${n}`).toString(), a)
  //     o = async (n, e) => g(`/v1/projects/${n}/advisors/performance`, {...e, method: "GET"})
  // `o`'s template was read from its raw call to `g`, without `g`'s base, so every call to `o` came out a
  // bare `/v1/...` path beside the same request with its base. Render the inner wrapper into the outer.
  const byFn = new Map();
  for (const ws of Object.values(wrappers)) for (const w of ws) byFn.set(w.fnNode, w);
  for (let round = 0; round < 3; round++) {
    let changed = false;
    for (const w of byFn.values()) {
      const ic = w.inner && w.inner.call;
      if (!ic || !ic.callee || ic.callee.type !== "Identifier") continue;
      const inner = byFn.get(_fnOfBinding(ic.callee.name, w.inner.scope));
      if (!inner || inner === w || !inner.tmpl) continue;
      const tm = inner.tmpl.replace(/\x01(\w+)\x01/g, (_, pn) => {
        const i = inner.paramNames.indexOf(pn);
        const a = i >= 0 ? ic.arguments[i] : null;
        return a ? strFromNode(a, w.inner.scope, 0, w.paramNames).s : "{EXPR}";
      });
      if (tm !== w.tmpl && tm.includes("\x01")) { w.tmpl = tm; w.viaFetch = !!inner.viaFetch; changed = true; }
      w.inner = null;
    }
    if (!changed) break;
  }
  return wrappers;
}
// The function an identifier ACTUALLY resolves to at this call site. Two scopes each declaring `t` are two
// different functions, and a name lookup cannot tell them apart, which is the whole problem in a minified
// bundle, where names are one letter and reused constantly.
function _fnOfBinding(name, scope) {
  try {
    const b = scope && scope.getBinding && scope.getBinding(name);
    if (!b || !b.path || !b.path.node) return null;
    const n = b.path.node;
    if (/Function/.test(n.type)) return n;                       // function t(){}
    if (n.init && /Function/.test(n.init.type)) return n.init;   // const t = () => {}
    for (const cv of (b.constantViolations || [])) {             // let t; t = function(){}
      const r = cv.node && cv.node.right;
      if (r && /Function/.test(r.type)) return r;
    }
  } catch {}
  return null;
}

// A reference to a JavaScript BUILTIN is never a path segment. A polyfill registry calls its helper with
// `Array.prototype.push`, `String.prototype.charCodeAt`, `Symbol.iterator`, and substituting those into a
// wrapper's URL template produced DELETE /gw/v1/auth/passkeys/Map.prototype.forEach: a route that
// does not exist, filed under a real API prefix, which is worse than the endpoint simply being missing.
// 11 of these on one corpus host. The value is unknowable at this call site, so say so.
const _BUILTIN_ROOT = new Set([
  "Array", "String", "Object", "Number", "Boolean", "Symbol", "Map", "Set", "WeakMap", "WeakSet",
  "Promise", "JSON", "Math", "Date", "RegExp", "Function", "Reflect", "Proxy", "Error", "BigInt",
  "Int8Array", "Uint8Array", "Float32Array", "Float64Array", "ArrayBuffer", "DataView",
  "globalThis", "window", "document", "console", "Intl",
]);
function _isBuiltinRef(str) {
  if (!str) return false;
  if (str.includes(".prototype.") || str.includes("@@")) return true;
  const head = String(str).split(/[.[]/)[0];
  return _BUILTIN_ROOT.has(head) && /[.[]/.test(str);
}

function renderWrapper(w, callArgs, scope) {
  let dynamic = false;
  const tmpl = w.tmpl.replace(/\x01(\w+)\x01/g, (_, pname) => {
    const idx = w.paramNames.indexOf(pname);
    // A DEFAULT completes the URL only for a client-class method, where the default IS part of the
    // address, `post(path, …, suffix = ".json")` builds /1.1/<path>.json when the caller omits it.
    // Applying the same rule to plain function wrappers changed long-standing behaviour and produced
    // /bin/twitter/api-proxy.jsony: a wrapper matched by NAME alone to a call whose argument was never a
    // path. The defaults were added for the class case; keep them there.
    const arg = (idx >= 0 ? callArgs[idx] : null)
             || (idx >= 0 && w.isMethod && w.paramDefaults ? w.paramDefaults[idx] : null);
    if (!arg) { dynamic = true; return "{EXPR}"; }
    const u = strFromNode(arg, scope, 0, null);
    if (_isBuiltinRef(u.s)) { dynamic = true; return "{EXPR}"; }
    // `null`, `void 0` or an object where a URL piece goes says nothing about the piece; the wrapper's
    // own parameter name says what it is. claude.ai's `/api/labs_verification/{param:e}` came out
    // `{nullliteral}` from a call passing null.
    if (_NODE_SLOT.test(u.s)) { dynamic = true; return "{param:" + pname + "}"; }
    if (u.dyn) dynamic = true;
    return u.s;   // keep the resolved base even if it has named {slots}, `/api/organizations/{uuid}` is the win
  });
  if (tmpl.includes("{EXPR}")) dynamic = true;
  return { template: tmpl, dynamic };
}
// method + body from whichever call arg is the options object (carries a NET_SIGNAL key)
function methodBodyFromArgs(args, scope) {
  for (const a of args) {
    if (a && a.type === "ObjectExpression") {
      for (const p of a.properties)
        if (p.type === "ObjectProperty" && p.key && NET_SIGNAL.has(p.key.name ?? p.key.value)) {
          const oi = optsInfo(a, scope);
          return { method: oi.method, body_keys: oi.body_keys, body_encoding: oi.body_encoding, content_type: oi.content_type };
        }
    }
  }
  return { method: null, body_keys: null };
}

// ── RESPONSE-SCHEMA forward-trace ───────────────────────────────────────────────────────────────────────────
// The "nobody does this" frontier. After a request, code consumes the response: `const d=await r.json(); d.members;
// d.next_page_token` or `const {items}=await r.json()` or `(await t.json()).members`. The property accesses on the
// .json() result ARE the response schema (field names survive minification, wire contract). We collect them per
// ENCLOSING FUNCTION, then attach to that function's sink IFF the function has exactly one sink (avoids
// misattribution when a fn makes several calls). Partial by design, high-precision where it fires.
// ── RESPONSE SHAPE FROM THE CALL SITE ────────────────────────────────────────────────────────────────────────
// The existing collector anchors on `.json()`, which only ever sees the raw-fetch idiom. Every client library
// parses the body for you, `const {data} = await api.get(url)` never calls .json(), so for the majority of the
// corpus the response schema was structurally invisible, which is why REST coverage sat at 1.4% while GraphQL,
// where the response shape is written into the query text, was complete.
//
// Anchoring on the CALL instead works for both, and it attaches to the right record by construction rather than
// through the enclosing-function heuristic: we already have the call node, so we can just read how its result is
// consumed. Three consumption shapes cover essentially all of it, await into a binding, await into a member
// access, and a .then() callback.
const _RESP_BUILTIN = new Set([
  "then", "catch", "finally", "length", "constructor", "prototype", "toString", "valueOf", "hasOwnProperty",
  "map", "filter", "forEach", "reduce", "slice", "splice", "push", "pop", "shift", "unshift", "join", "concat",
  "indexOf", "lastIndexOf", "includes", "find", "findIndex", "some", "every", "sort", "reverse", "flat",
  "flatMap", "keys", "values", "entries", "at", "toLowerCase", "toUpperCase", "trim", "split", "replace",
  "startsWith", "endsWith", "match", "substring", "substr", "charAt", "toFixed", "getTime", "toISOString",
  "toJSON", "json", "text", "blob", "arrayBuffer", "formData", "clone", "headers", "ok", "redirected", "url",
  "on", "off", "once", "emit", "subscribe", "unsubscribe", "addEventListener", "removeEventListener",
  "abort", "cancel", "pipe", "close", "destroy", "bind", "call", "apply", "next", "return", "throw",
]);
// The transport envelope, not the payload. axios/ky return {data,status,headers,config}; a schema of
// ["data","status"] describes the LIBRARY, not the API, so unwrap one level and report what is inside.
const _RESP_ENVELOPE = new Set(["data", "body", "result", "response", "payload"]);
// The wider set is only used to ASK "is this nothing but an envelope?". One API wraps every reply as
// {meta, data}, so the recovered schema came back as ["meta","data"] for 90 endpoints, a description of the
// framing, identical for every route, and worse than an empty answer because it reads like a real payload.
// `meta`/`status`/`error` can be genuine fields, which is why they belong here and not in the unwrap set:
// they are grounds for suspecting an envelope, never grounds for stripping one.
const _RESP_WRAPPER = new Set([...(_RESP_ENVELOPE), "meta", "status", "error", "errors", "success", "code",
  "message", "ok", "statusCode", "count", "total"]);
const _respClean = (f) => typeof f === "string" && f && !/^\d+$/.test(f) && !_RESP_BUILTIN.has(f);
// nothing but framing -> we did not recover a schema, and should say so rather than describe the envelope
const _allWrapper = (fields) => fields.length > 0 && fields.every((f) => _RESP_WRAPPER.has(f));

// every `name.FIELD` read inside `fnPath`, optionally through one envelope hop (`name.data.FIELD`)
function _fieldsOf(fnPath, name, viaEnvelope) {
  const out = new Set();
  if (!fnPath || !name) return out;
  try {
    fnPath.traverse({
      MemberExpression(mp) {
        const o = mp.node.object, pr = mp.node.property;
        if (!pr) return;
        if (viaEnvelope) {
          if (!(o && o.type === "MemberExpression" && o.object && o.object.type === "Identifier" &&
                o.object.name === name && o.property && _RESP_ENVELOPE.has(o.property.name))) return;
        } else if (!(o && o.type === "Identifier" && o.name === name)) return;
        // A property you CALL is a method, not a data field. One dashboard's schemas came back as ["on"],
        // ["apiFetch"], ["toUrl"], every one of them an invocation on the variable, none of them anything the
        // server returns. Same reasoning that removed `toLowerCase` from response schemas, generalised: the
        // blocklist can never name every helper an app defines, but the call syntax identifies all of them.
        const par = mp.parentPath;
        if (par && par.node && par.node.type === "CallExpression" && par.node.callee === mp.node) return;
        const f = pr.name ?? pr.value;
        if (_respClean(f)) out.add(f);
      },
    });
  } catch {}
  return out;
}
// A react-query / SWR hook returns {data, isLoading, error, refetch, …}. That is the HOOK's shape, identical for
// every endpoint in the app, and describes the client's state machine rather than the server's reply.
const _HOOK_SHAPE = new Set(["isLoading", "isFetching", "isPending", "isError", "isSuccess", "isIdle", "refetch",
  "mutate", "mutateAsync", "failureCount", "dataUpdatedAt", "errorUpdatedAt", "fetchStatus", "isRefetching",
  "isPlaceholderData", "isStale", "remove", "reset"]);
const _looksLikeHook = (fields) => fields.some((f) => _HOOK_SHAPE.has(f));
function _patternKeys(pattern) {
  const out = new Set();
  for (const pr of (pattern.properties || [])) {
    if (pr.type !== "ObjectProperty" || !pr.key) continue;
    const k = pr.key.name ?? pr.key.value;
    // `const {data:{user,org}} = await get(...)`, the envelope key is noise, its CONTENTS are the schema
    if (_RESP_ENVELOPE.has(k) && pr.value && pr.value.type === "ObjectPattern") {
      for (const k2 of _patternKeys(pr.value)) out.add(k2);
    } else if (_respClean(k)) out.add(k);
  }
  return out;
}

function responseAtCallSite(path) {
  const got = _responseAtCallSite(path);
  // A schema made only of envelope keys describes the client's framing, not the endpoint; a schema carrying
  // hook state describes the client's state machine. Both are identical across every route in the app, so an
  // absent answer is the honest one.
  // Only the BEHAVIOURAL test applies here: hook state is recognisable from one call site, an envelope is not.
  // Whether a key set is framing depends on how often it repeats across the host, which only the caller can see.
  if (!got || _looksLikeHook(got)) return null;
  return got;
}
function _responseAtCallSite(path) {
  try {
    let p = path.parentPath;
    if (!p) return null;
    // `X.then(d => …)`, the callback parameter IS the parsed response
    if (p.node && p.node.type === "MemberExpression" && p.node.property && p.node.property.name === "then" &&
        p.parentPath && p.parentPath.node && p.parentPath.node.type === "CallExpression") {
      const thenCall = p.parentPath;
      const cb = thenCall.node.arguments[0];
      const cbPath = thenCall.get("arguments.0");
      if (cb && (cb.type === "ArrowFunctionExpression" || cb.type === "FunctionExpression") && cb.params[0]) {
        // `fetch(u).then(r => r.json()).then(d => …)`, the FIRST callback receives the Response object, and its
        // only job is to parse. The payload shape is one link further along the chain, so follow it rather than
        // reporting nothing (this is the single commonest raw-fetch idiom there is).
        let body = cb.body;
        if (body && body.type === "BlockStatement") {
          const rs = body.body.find((s) => s.type === "ReturnStatement");
          body = rs ? rs.argument : null;
        }
        const parses = body && body.type === "CallExpression" && body.callee &&
          body.callee.type === "MemberExpression" && body.callee.property &&
          ["json", "text"].includes(body.callee.property.name);
        if (parses) return _responseAtCallSite(thenCall);
        const prm = cb.params[0];
        if (prm.type === "ObjectPattern") {
          const s = _patternKeys(prm);
          // `then(({data}) => data)` names the envelope and reads nothing out of it, that is not a schema
          if (s.size && [...s].every((f) => _RESP_ENVELOPE.has(f))) {
            const inner = (prm.properties.find((pr) => pr.key && _RESP_ENVELOPE.has(pr.key.name ?? pr.key.value)) || {}).value;
            const s2 = inner && inner.type === "Identifier" ? _fieldsOf(cbPath, inner.name, false) : new Set();
            return s2.size ? [...s2] : null;
          }
          return s.size ? [...s] : null;
        }
        if (prm.type === "Identifier") {
          let s = _fieldsOf(cbPath, prm.name, false);
          if (s.size && [...s].every((f) => _RESP_ENVELOPE.has(f))) s = _fieldsOf(cbPath, prm.name, true);
          return s.size ? [...s] : null;
        }
      }
      return null;
    }
    if (p.node && p.node.type === "AwaitExpression") p = p.parentPath;
    if (!p || !p.node) return null;
    if (p.node.type === "VariableDeclarator") {
      const id = p.node.id;
      if (id.type === "ObjectPattern") {
        const s = _patternKeys(id);
        // `const {data} = await get()` with no nesting: follow `data`'s own reads
        if (s.size === 1 && _RESP_ENVELOPE.has([...s][0])) {
          const inner = (id.properties.find((pr) => pr.key && _RESP_ENVELOPE.has(pr.key.name ?? pr.key.value)) || {}).value;
          if (inner && inner.type === "Identifier") {
            const s2 = _fieldsOf(path.getFunctionParent(), inner.name, false);
            if (s2.size) return [...s2];
          }
        }
        return s.size ? [...s] : null;
      }
      if (id.type === "Identifier") {
        let s = _fieldsOf(path.getFunctionParent(), id.name, false);
        if (s.size && [...s].every((f) => _RESP_ENVELOPE.has(f))) s = _fieldsOf(path.getFunctionParent(), id.name, true);
        return s.size ? [...s] : null;
      }
      return null;
    }
    if (p.node.type === "MemberExpression" && p.node.property) {       // (await get(u)).field
      // `(await get(u)).on(...)` is invoking a method on the reply, not naming a field of it. This branch had
      // no call check, which is where One dashboard's ["on"] schemas were coming from.
      if (p.parentPath && p.parentPath.node && p.parentPath.node.type === "CallExpression" &&
          p.parentPath.node.callee === p.node) return null;
      let f = p.node.property.name ?? p.node.property.value;
      if (_RESP_ENVELOPE.has(f) && p.parentPath && p.parentPath.node.type === "MemberExpression" &&
          p.parentPath.node.property) f = p.parentPath.node.property.name ?? p.parentPath.node.property.value;
      return _respClean(f) ? [f] : null;
    }
  } catch {}
  return null;
}

function collectResponseSchemas(ast) {
  const byFn = new Map();
  const add = (fnNode, fields) => {
    if (!fnNode || !fields || !fields.size) return;
    const e = byFn.get(fnNode) || { fields: new Set(), n: 0 };
    fields.forEach((f) => e.fields.add(f)); e.n++;
    byFn.set(fnNode, e);
  };
  // filter only JS builtins that indicate the var is used as a promise/array (not data fields). status/ok/etc are
  // legit API field names ({status:"active"}), the .json() RESULT is data, so don't blocklist real-sounding keys.
  // Every member access on the parsed result was treated as a response FIELD, including methods called on
  // it, `data.toLowerCase()` is not a field named toLowerCase. Only a handful of promise/array names were
  // filtered; the full builtin surface leaks otherwise, and a response schema with endsWith in it is worse
  // than none because it invites reasoning about a field the server never returns.
  const BUILTIN = new Set([
    "then", "catch", "finally", "length", "constructor", "prototype", "toString", "valueOf",
    "hasOwnProperty", "propertyIsEnumerable", "isPrototypeOf", "toLocaleString",
    "map", "filter", "forEach", "reduce", "reduceRight", "slice", "splice", "push", "pop", "shift",
    "unshift", "join", "concat", "indexOf", "lastIndexOf", "includes", "find", "findIndex", "findLast",
    "some", "every", "sort", "reverse", "flat", "flatMap", "fill", "keys", "values", "entries", "at",
    "toLowerCase", "toUpperCase", "trim", "trimStart", "trimEnd", "split", "replace", "replaceAll",
    "startsWith", "endsWith", "padStart", "padEnd", "repeat", "match", "matchAll", "search", "substring",
    "substr", "charAt", "charCodeAt", "codePointAt", "normalize", "localeCompare",
    "toFixed", "toPrecision", "toExponential", "getTime", "toISOString", "toJSON",
  ]);
  const clean = (f) => typeof f === "string" && f && !/^\d+$/.test(f) && !BUILTIN.has(f);
  traverse(ast, {
    CallExpression(path) {
      try {
        const c = path.node.callee;
        if (!(c && c.type === "MemberExpression" && c.property && (c.property.name === "json"))) return;
        const fnNode = (path.getFunctionParent() || {}).node;
        const p1 = path.parentPath;                      // expect AwaitExpression wrapping the .json() call
        if (!p1 || !p1.isAwaitExpression()) return;
        const p2 = p1.parentPath;
        if (!p2) return;
        if (p2.isVariableDeclarator()) {
          const id = p2.node.id;
          if (id.type === "ObjectPattern") {             // const {a,b} = await r.json()
            const fs = new Set();
            for (const pr of id.properties) if (pr.key) { const k = pr.key.name ?? pr.key.value; if (clean(k)) fs.add(k); }
            add(fnNode, fs);
          } else if (id.type === "Identifier") {         // const d = await r.json(); …d.field…
            const fs = new Set();
            const fp = path.getFunctionParent();
            if (fp) fp.traverse({ MemberExpression(mp) {
              const o = mp.node.object;
              if (o && o.type === "Identifier" && o.name === id.name && mp.node.property) {
                // a property that is CALLED is a method on the parsed body, not a field of it
                const par = mp.parentPath;
                if (par && par.node && par.node.type === "CallExpression" && par.node.callee === mp.node) return;
                const f = mp.node.property.name ?? mp.node.property.value; if (clean(f)) fs.add(f);
              }
            } });
            add(fnNode, fs);
          }
        } else if (p2.isMemberExpression() && p2.node.property) {   // (await r.json()).field
          const pp2 = p2.parentPath;                                 // …but not (await r.json()).on(…)
          const called = pp2 && pp2.node && pp2.node.type === "CallExpression" && pp2.node.callee === p2.node;
          const f = p2.node.property.name ?? p2.node.property.value;
          if (!called && clean(f)) add(fnNode, new Set([f]));
        }
      } catch {}
    },
  });
  return byFn;
}

// tRPC: calls are a Proxy member-chain `trpc.<router>.<procedure>.useQuery(input)` / `.useMutation()` / `.query()`
// that hits `/api/trpc/<router>.<procedure>` (query=GET w/ ?input=, mutation=POST). The dotted chain IS the path.
// Gated to tRPC files (has @trpc / /api/trpc/) so plain react-query `useQuery({...})` (bare call, no chain) is safe.
// Hook terminals are tRPC-react-specific: raw react-query's useQuery/useMutation are BARE calls (callee=Identifier),
// never a ≥2-level member chain, so we duck-type these UNGATED (survives cross-batch splitting where the @trpc marker
// lands in a different 40-chunk batch than the proc call). Generic terminals (query/mutate/fetch) are ambiguous
// (DB builders, etc.) so they stay gated to tRPC files.
const TRPC_HOOKS = new Set(["useQuery", "useMutation", "useSuspenseQuery", "useInfiniteQuery",
                            "useSuspenseInfiniteQuery", "usePrefetchQuery"]);
const TRPC_GENERIC = new Set(["query", "mutate", "fetch", "prefetch", "fetchInfinite"]);
// WHAT BUILT THE ROOT OF THIS MEMBER CHAIN.
//
// `trpc.router.procedure.useQuery()` and `api.endpoints.name.useQuery()` are the same shape, so shape
// cannot tell them apart and no amount of argument inspection will either. What differs is what the
// root was CONSTRUCTED by, and where the root is a local binding its construction site says so
// outright: `createTRPCReact()` / `createTRPCProxyClient()` / `createTRPCNext()` on one side,
// `createApi({...})` or `<api>.injectEndpoints({...})` on the other.
//
// One-sided on purpose. It answers "trpc", "rtk", or null for "cannot tell", and a null leaves the
// decision exactly where it was. In a minified bundle the root is usually a cross-module reference
// (`n(7781).api`) with no resolvable construction, which is why this cannot be the only test.
function chainRootFactory(root, scope) {
  if (!root || root.type !== "Identifier" || !scope || !scope.getBinding) return null;
  const b = scope.getBinding(root.name);
  if (!b || !b.path || !b.path.node) return null;
  const node = b.path.node;
  let init = node.type === "VariableDeclarator" ? node.init : null;
  if (init && (init.type === "TSAsExpression" || init.type === "TSNonNullExpression")) init = init.expression;
  if (!init || init.type !== "CallExpression") return null;
  const c = init.callee;
  const name = c && (c.name || (c.property && c.property.name));
  if (!name) return null;
  if (/^createTRPC/.test(name)) return "trpc";
  if (name === "createApi" || name === "injectEndpoints") return "rtk";
  return null;
}

function trpcCall(n, scope, isTrpc) {
  const c = n.callee;
  if (!(c && c.type === "MemberExpression" && c.property)) return null;
  const term = c.property.name;
  const isHook = TRPC_HOOKS.has(term);
  if (!isHook && !(isTrpc && TRPC_GENERIC.has(term))) return null;
  // CORROBORATION. This collector does not read a URL, it MANUFACTURES one, `/api/trpc/` is a
  // string in this file, not in the bundle, so the bundle has to show some sign of tRPC before the
  // manufactured URL is worth anything. `isTrpc` (the `@trpc` / `/api/trpc/` / `createTRPC*` marker)
  // already existed and was consulted only for the GENERIC terminals; hook terminals skipped it
  // entirely, which is how a bundle containing no tRPC produced tRPC endpoints. Measured: Grafana
  // carries the marker in 0 of 159 files and produced 13 such records; cal.com carries it and is a
  // real tRPC app. This rejects every look-alike at once instead of naming them one at a time,
  // `queries.workspace.members.useQuery()` fabricates `/api/trpc/workspace.members` and no property
  // name blocklist can see it coming.
  if (!isTrpc) return null;
  const parts = []; let o = c.object;
  while (o) {                                    // walk the proxy chain back to the root
    if (o.type === "MemberExpression" && o.property && !o.computed) { parts.unshift(o.property.name ?? o.property.value); o = o.object; }
    else break;
  }
  if (parts.length < 2) return null;             // need router.procedure (≥2), distinguishes tRPC from bare useQuery
  if (parts.some(p => p == null || /^\d+$/.test(String(p)) || /^(then|catch|map|filter|forEach|call|apply|bind)$/.test(String(p))))
    return null;                                 // reject array-index / promise / method-chain false positives
  // RTK QUERY HAS THE SAME SHAPE AND IS NOT tRPC. Its generated hooks are
  // `api.endpoints.<name>.useQuery(...)` / `.useMutation()`, a >=2-level member chain ending in a
  // tRPC hook terminal, so the ungated duck-type matched them and INVENTED endpoints: Grafana
  // produced 13 records at paths like /api/trpc/hK.endpoints.upsertRuleGroupForNamespace while the
  // string "/api/trpc" appears zero times in its entire bundle. A fabricated endpoint is worse than
  // a missed one, someone sends requests to a URL that was never there.
  //
  // WHAT BUILT THE ROOT decides it when the root can be read. `createApi`/`injectEndpoints` is RTK
  // and not a request here; `createTRPC*` is tRPC and stays one even if a procedure happens to be
  // called `endpoints`.
  const factory = chainRootFactory(o, scope);
  if (factory === "rtk") return null;
  // FALLBACK, for the common case where the root cannot be read. In a minified bundle RTK's api
  // object arrives as a cross-module reference with no resolvable construction, Grafana's is
  // `hK.endpoints.<name>`, so the property name is the only tell left, and removing this would
  // re-admit exactly the records it was added for. It is a blocklist and is treated as the last
  // resort it is: a chain whose root is PROVEN to be tRPC skips it, which is what stops a real
  // tRPC procedure named `endpoints` from being thrown away.
  if (factory !== "trpc" && parts.some(p => String(p) === "endpoints")) return null;
  const method = /mutat/i.test(term) ? "POST" : "GET";
  const body = method === "POST" && n.arguments[0] ? bodyKeys(n.arguments[0], scope) : null;
  return { sink: "trpc", kind: "trpc", method, template: `/api/trpc/${parts.join(".")}`, body_keys: body, dynamic: false };
}

// ── ZOD/valibot runtime-validator schemas → TYPED field contracts ───────────────────────────────────────────
// The response/request-schema goldmine: TS types are ERASED at compile, but runtime validators SHIP as code.
// `z.object({email: z.string(), role: z.enum([...]).optional()})` is literally in the bundle → exact fields+types.
// Compiled/minified: the namespace is renamed (m.default.object, D.z.object) but `.object({...})` + `.string()`/
// `.number()` leaf chains are the invariant signature. We record named schemas; static_extract links them to
// endpoints by body-key overlap → typed request bodies.
const ZTYPES = new Set(["string", "number", "boolean", "date", "bigint", "enum", "nativeEnum", "array", "object",
  "union", "discriminatedUnion", "literal", "record", "map", "set", "tuple", "any", "unknown", "null", "void",
  "optional", "nullable", "nullish", "coerce", "instanceof", "lazy", "email", "url", "uuid", "pipe"]);
function _zodChain(node) {                                  // returns {type, optional} from a z.string().min().optional() chain
  let n = node, hops = 0, optional = false, base = null, isZod = false;
  const seen = [];
  while (n && hops++ < 24) {
    if (n.type === "CallExpression") n = n.callee;
    else if (n.type === "MemberExpression") { if (n.property && n.property.name) seen.push(n.property.name); n = n.object; }
    else break;
  }
  for (const c of seen) {
    if (ZTYPES.has(c)) { isZod = true; if (!["optional", "nullable", "nullish", "coerce", "pipe"].includes(c)) base = base || c; }
    if (["optional", "nullable", "nullish"].includes(c)) optional = true;
  }
  return isZod ? { type: base || "?", optional } : null;
}
function _zodObject(node) {                                 // node = <ns>.object({...}) → [{key,type,optional}] or null
  if (!node || node.type !== "CallExpression") return null;
  const c = node.callee;
  if (!(c && c.type === "MemberExpression" && c.property && c.property.name === "object")) return null;
  const arg = node.arguments[0];
  if (!arg || arg.type !== "ObjectExpression") return null;
  const fields = []; let zodish = false;
  for (const p of arg.properties) {
    if (p.type !== "ObjectProperty" || !p.key) continue;
    const key = p.key.name ?? p.key.value;
    const z = _zodChain(p.value);
    if (z) { zodish = true; fields.push({ key, type: z.type, optional: z.optional }); }
    else fields.push({ key, type: "?", optional: false });
  }
  return (zodish && fields.length) ? fields : null;
}
function collectZodSchemas(ast) {
  const schemas = [];
  traverse(ast, { CallExpression(path) {
    try {
      const fields = _zodObject(path.node);
      if (!fields) return;
      let name = null;                                     // name from `const X = z.object(...)`
      const vp = path.findParent((p) => p.isVariableDeclarator());
      if (vp && vp.node.id && vp.node.id.type === "Identifier") name = vp.node.id.name;
      schemas.push({ name, fields });
    } catch {}
  } });
  return schemas;
}

// ── DECLARATIVE endpoint tables, `{login:{url:"/api/auth/login",method:"post"}, ...}` (Nuxt auth, API client
// configs, route tables). Not a call site, so the call-based rules miss them; the {url|path, method} object IS the
// contract. Require BOTH a path-like url AND a method (strong HTTP signal → no false positives on random objects).
// header NAMES from a table entry's `headers` value: a literal object, or a local built by assignment
// (`let i={}; i.Authorization = \`Bearer ${t}\`; return {headers:i}`), the generated-client shape.
function headerNamesOf(node, scope) {
  const out = [];
  const push = (k) => { const kk = String(k || "").trim(); if (kk && !out.includes(kk)) out.push(kk); };
  try {
    if (node.type === "ObjectExpression") {
      for (const p of node.properties) if (p.type === "ObjectProperty" && p.key) push(p.key.name ?? p.key.value);
    } else if (node.type === "Identifier") {
      const b = scope && scope.getBinding && scope.getBinding(node.name);
      if (b) {
        const init = b.path && b.path.node && b.path.node.init;
        if (init && init.type === "ObjectExpression")
          for (const p of init.properties) if (p.type === "ObjectProperty" && p.key) push(p.key.name ?? p.key.value);
        for (const rp of (b.referencePaths || [])) {
          const mp = rp.parentPath;
          if (!mp || !mp.node || mp.node.type !== "MemberExpression" || mp.node.object !== rp.node) continue;
          const ap = mp.parentPath;
          if (!ap || !ap.node || ap.node.type !== "AssignmentExpression" || ap.node.left !== mp.node) continue;
          push(mp.node.computed ? (mp.node.property && mp.node.property.value)
                                : (mp.node.property && (mp.node.property.name ?? mp.node.property.value)));
        }
      }
    }
  } catch {}
  return out.length ? out.slice(0, 1000) : null;
}

// `(0, mod.NameToJSON)` / `mod.NameToJSON` / `NameToJSON` -> "NameToJSON". Bundlers wrap a namespace member
// call in a sequence expression to defeat `this` binding; the NAME is what survives and what we can join on.
function _calleeIdent(c) {
  if (!c) return null;
  if (c.type === "SequenceExpression" && c.expressions.length) return _calleeIdent(c.expressions[c.expressions.length - 1]);
  if (c.type === "Identifier") return c.name;
  if ((c.type === "MemberExpression" || c.type === "OptionalMemberExpression") && !c.computed && c.property)
    return c.property.name ?? c.property.value;
  return null;
}
// the ObjectExpression a locally-defined function returns, if it returns one plainly
// The scope each returned object literal belongs to, which is the FUNCTION's, not the caller's. A
// returned `{...i, publicKeyJwk}` must resolve `i` where the function declares it: resolved at the call
// site, Clerk's `i` was the webpack module wrapper's own parameter, and every `i.x` read in a whole
// module, 83 names, became the body of POST /me/biometric_credentials/attempt.
const _RET_SCOPE = new WeakMap();
function _returnedObjectOf(name, scope, depth) {
  try {
    const b = scope && scope.getBinding && scope.getBinding(name);
    if (!b || !b.path || !b.path.node) return null;
    const n = b.path.node;
    let fn = n.type === "FunctionDeclaration" ? n : n.init;
    let fnScope = null;
    try {
      fnScope = n.type === "FunctionDeclaration" ? b.path.scope : (b.path.get("init") || {}).scope;
    } catch { fnScope = null; }
    if (fn && fn.type === "CallExpression" && fn.arguments && fn.arguments.length === 0) return null;
    if (!fn || !/Function/.test(fn.type)) return null;
    let ret = fn.body;
    if (ret && ret.type === "BlockStatement") {
      const rs = ret.body.filter((st) => st.type === "ReturnStatement");
      ret = rs.length ? rs[rs.length - 1].argument : null;      // generators emit a guard return then the real one
    }
    if (ret && ret.type === "CallExpression" && depth < 4) {     // NameToJSON -> NameToJSONTyped(json, false)
      const inner = _calleeIdent(ret.callee);
      if (inner && inner !== name) return _returnedObjectOf(inner, b.scope || scope, depth + 1);
    }
    if (ret && ret.type === "ObjectExpression") {
      if (fnScope) _RET_SCOPE.set(ret, fnScope);
      return ret;
    }
    return null;
  } catch { return null; }
}
// Every locally-defined function that plainly returns an object literal, published so another chunk's call site
// can resolve `SomeRequestToJSON(x)` even though the definition lives in a different file.
function collectSerializers(ast) {
  const out = [];
  const emit = (name, fnPath) => {
    if (!name || name.length < 4 || !/[A-Za-z]/.test(name)) return;
    // openapi-generator emits a PAIR: `XToJSON(json)` simply forwards to `XToJSONTyped(json,false)`, and only
    // the second returns the literal. Call sites use the first. Resolving through the delegation means the name
    // we publish is the name the call site will ask for, otherwise the two halves never meet and the join is
    // silently empty, which is exactly what happened here (142 tagged call sites, 0 resolved).
    const ret = _returnedObjectOf(name, fnPath.scope, 0);
    if (!ret || ret.type !== "ObjectExpression" || !ret.properties.length) return;
    const fields = [];
    for (const pr of ret.properties)
      if ((pr.type === "ObjectProperty" || pr.type === "ObjectMethod") && pr.key)
        fields.push(String(pr.key.name ?? pr.key.value));
    if (fields.length && fields.length <= 120) out.push({ _kind: "serializer", _evidence: true, name, fields: [...new Set(fields)] });
  };
  try {
    traverse(ast, {
      FunctionDeclaration(p) { if (p.node.id) emit(p.node.id.name, p); },
      VariableDeclarator(p) {
        const init = p.node.init;
        if (p.node.id && p.node.id.type === "Identifier" && init && /Function/.test(init.type))
          emit(p.node.id.name, p.get("init"));
      },
    });
  } catch {}
  return out;
}

// a string or an array of strings, resolved through a binding if it is a name
function _stringList(node, scope, depth) {
  depth = depth || 0;
  if (!node || depth > 3) return null;
  if (node.type === "StringLiteral") return [node.value];
  if (node.type === "ArrayExpression")
    return node.elements.filter((e) => e && e.type === "StringLiteral").map((e) => e.value);
  if (node.type === "Identifier" && scope && scope.getBinding) {
    const b = scope.getBinding(node.name);
    if (b && b.path && b.path.node && b.path.node.init) return _stringList(b.path.node.init, b.scope, depth + 1);
  }
  return null;
}
// The operation's own name, from the key the table entry is filed under. `schema`/`config`/`def` are structural
// wrappers the generator inserts, not names anybody chose, so step past them to the key that means something.
const _TABLE_WRAPPER_KEYS = new Set(["schema", "config", "options", "opts", "def", "spec", "definition",
  "request", "get", "post", "put", "patch", "delete", "head", "options", "query", "mutate", "default"]);
function _tableOpName(path) {
  let p = path;
  for (let i = 0; i < 8 && p; i++) {
    const n = p.node;
    if (n && (n.type === "ObjectProperty" || n.type === "Property") && n.key) {
      const k = String(n.key.name ?? n.key.value ?? "");
      if (k && !_TABLE_WRAPPER_KEYS.has(k) && /^[A-Za-z_$][\w$]{2,}$/.test(k)) return k;
    }
    p = p.parentPath;
  }
  return undefined;
}

// A route handle assigned from a tagged template, `$J = T(`user`).put`/user/password``, is called later with
// the payload. Walk from the declaration to the binding, then to every call of it, and union the argument shapes.
// Same move as resolving a function parameter from its call sites; the handle is just a differently-shaped
// function. Capped at a few call sites: the point is the shape, not an exhaustive census.
function _handleCallBody(path) {
  try {
    let p = path.parentPath, name = null, scope = path.scope;
    for (let i = 0; i < 3 && p && !name; i++) {
      const n = p.node;
      if (n && n.type === "VariableDeclarator" && n.id && n.id.type === "Identifier") { name = n.id.name; scope = p.scope; }
      else if (n && n.type === "AssignmentExpression" && n.left && n.left.type === "Identifier") { name = n.left.name; scope = p.scope; }
      p = p.parentPath;
    }
    if (!name) return null;
    const b = scope && scope.getBinding && scope.getBinding(name);
    if (!b || !b.referencePaths) return null;
    const keys = [];
    let used = 0;
    for (const rp of b.referencePaths) {
      const cp = rp.parentPath;
      if (!cp || !cp.node || cp.node.type !== "CallExpression" || cp.node.callee !== rp.node) continue;
      for (const a of cp.node.arguments) {
        if (!a) continue;
        const k = bodyKeys(a, cp.scope, 0);
        if (k && k.length) { keys.push(...k.filter((x) => !String(x).startsWith("{"))); break; }
      }
      if (++used >= 4) break;
    }
    const out = [...new Set(keys)];
    return out.length ? out.slice(0, 1000) : null;
  } catch { return null; }
}

// react-query's mutation shape. `mutationFn` receives the payload, and the payload is supplied by whoever calls
// the hook's `mutate`:
//     const m = useMutation({mutationFn: p => api.post("/x", p)});   …later…   m.mutate({a, b})
// The parameter has no name binding to follow, it belongs to an anonymous arrow handed to a library, so the
// generic call-site walk cannot reach the caller. But the HOOK RESULT is an ordinary binding, and `.mutate` /
// `.mutateAsync` on it carries exactly the body. This is the dominant write idiom in modern React apps, so the
// whole class was invisible.
function _mutationCallBody(path, paramName) {
  try {
    const fp = path.getFunctionParent && path.getFunctionParent();
    if (!fp || !fp.node || !Array.isArray(fp.node.params)) return null;
    const idx = fp.node.params.findIndex((p) => p && p.type === "Identifier" && p.name === paramName);
    if (idx !== 0) return null;                    // mutate() passes exactly one argument
    // arrow -> {mutationFn: arrow} -> {…} -> useMutation({…})  (or useMutation(arrow) directly)
    let p2 = fp.parentPath;
    if (p2 && p2.node && (p2.node.type === "ObjectProperty" || p2.node.type === "Property")) {
      const k = String((p2.node.key && (p2.node.key.name ?? p2.node.key.value)) || "");
      if (!/^(mutationFn|queryFn|mutate|fn)$/.test(k)) return null;
      p2 = p2.parentPath && p2.parentPath.parentPath;      // ObjectExpression -> CallExpression
    }
    if (!p2 || !p2.node || p2.node.type !== "CallExpression") return null;
    const decl = p2.parentPath;
    if (!decl || !decl.node || decl.node.type !== "VariableDeclarator") return null;
    const id = decl.node.id;
    const names = [];
    if (id.type === "Identifier") names.push({ kind: "obj", name: id.name });
    else if (id.type === "ObjectPattern") {
      for (const pr of id.properties) {
        if (pr.type !== "ObjectProperty" || !pr.key) continue;
        const k = String(pr.key.name ?? pr.key.value);
        if (/^mutate(Async)?$/.test(k) && pr.value && pr.value.type === "Identifier")
          names.push({ kind: "fn", name: pr.value.name });
      }
    }
    // The hook result is very often RETURNED by a custom hook rather than used where it is created:
    //     function useInvite(){ const m = useMutation({mutationFn: p => post("/invite", p)}); return m }
    //     function Panel(){ const m = useInvite(); m.mutate({email, role}) }
    // So when the variable is returned, follow the wrapper's own call sites and look for `.mutate` on whatever
    // each caller bound the result to. Wrapping a hook is the normal way to write these, not an edge case.
    const scopes = [decl.scope];
    if (id.type === "Identifier") {
      const outer = decl.getFunctionParent && decl.getFunctionParent();
      const returnsIt = outer && outer.node && JSON.stringify(outer.node.body).indexOf('"' + id.name + '"') >= 0;
      if (returnsIt) {
        let hookName = outer.node.id && outer.node.id.name;
        if (!hookName) {
          const pp = outer.parentPath && outer.parentPath.node;
          if (pp && pp.type === "VariableDeclarator" && pp.id && pp.id.type === "Identifier") hookName = pp.id.name;
        }
        const hb = hookName && (outer.scope.parent || outer.scope).getBinding(hookName);
        for (const rp of ((hb && hb.referencePaths) || []).slice(0, 6)) {
          const cp = rp.parentPath;
          if (!cp || !cp.node || cp.node.type !== "CallExpression" || cp.node.callee !== rp.node) continue;
          const vd = cp.parentPath;
          if (!vd || !vd.node || vd.node.type !== "VariableDeclarator") continue;
          if (vd.node.id && vd.node.id.type === "Identifier") { names.push({ kind: "obj", name: vd.node.id.name }); scopes.push(vd.scope); }
          else if (vd.node.id && vd.node.id.type === "ObjectPattern") {
            for (const pr of vd.node.id.properties)
              if (pr.type === "ObjectProperty" && pr.key && /^mutate(Async)?$/.test(String(pr.key.name ?? pr.key.value)) &&
                  pr.value && pr.value.type === "Identifier") { names.push({ kind: "fn", name: pr.value.name }); scopes.push(vd.scope); }
          }
        }
      }
    }
    if (!names.length) return null;
    const keys = [];
    for (let ni = 0; ni < names.length; ni++) {
      const nm = names[ni];
      const sc = scopes[Math.min(ni, scopes.length - 1)] || decl.scope;
      const b = sc && sc.getBinding && sc.getBinding(nm.name);
      if (!b || !b.referencePaths) continue;
      for (const rp of b.referencePaths) {
        let call = null;
        if (nm.kind === "fn") {
          const cp = rp.parentPath;
          if (cp && cp.node && cp.node.type === "CallExpression" && cp.node.callee === rp.node) call = cp.node;
        } else {
          const mp = rp.parentPath;                                  // m.mutate(arg)
          if (mp && mp.node && mp.node.type === "MemberExpression" && mp.node.object === rp.node &&
              mp.node.property && /^mutate(Async)?$/.test(String(mp.node.property.name || ""))) {
            const cp = mp.parentPath;
            if (cp && cp.node && cp.node.type === "CallExpression" && cp.node.callee === mp.node) call = cp.node;
          }
        }
        if (!call || !call.arguments[0]) continue;
        const k = bodyKeys(call.arguments[0], rp.scope, 0);
        if (k && k.length) keys.push(...k.filter((x) => !String(x).startsWith("{")));
        if (keys.length > 40) break;
      }
    }
    const out = [...new Set(keys)];
    return out.length ? out.slice(0, 1000) : null;
  } catch { return null; }
}

// QUERY STRINGS BUILT IMPERATIVELY. `formDataAppends` already understands
//     const p = new URLSearchParams(); p.set("limit", n); p.append("status", "open")
// but it was only ever consulted for the BODY. The same object put after a `?` is a query string, and three of
// the four ordinary ways to build one, the .set/.append form, the constructor-object form, and
// `new URL(...).searchParams.set(...)`, produced `?{EXPR}` and no parameters at all. Query strings are a
// first-class injection point, so knowing a request takes `limit,cursor,status` and not knowing their names is
// most of the difference between a testable endpoint and a URL.
function _queryFromUrlArg(node, scope) {
  const out = [];
  const add = (k) => { const s2 = String(k || "").trim(); if (s2 && !out.includes(s2)) out.push(s2); };
  const fromIdent = (id) => {
    const fd = formDataAppends(id, scope);
    if (fd) for (const a of fd) add(a.key);
  };
  try {
    if (!node) return null;
    // `\`/items?${p}\`` / `\`/items?${p.toString()}\``, an expression sitting after the question mark
    if (node.type === "TemplateLiteral") {
      let seenQ = false;
      for (let i = 0; i < node.quasis.length; i++) {
        if ((node.quasis[i].value.cooked || "").includes("?")) seenQ = true;
        const ex = node.expressions[i];
        if (!seenQ || !ex) continue;
        let e = ex;
        if (e.type === "CallExpression" && e.callee && e.callee.type === "MemberExpression" &&
            e.callee.property && e.callee.property.name === "toString") e = e.callee.object;
        if (e.type === "Identifier") fromIdent(e);
      }
    }
    // `fetch(u.toString())`, same object, one unwrap away
    if (node.type === "CallExpression" && node.callee && node.callee.type === "MemberExpression" &&
        node.callee.property && node.callee.property.name === "toString")
      return _queryFromUrlArg(node.callee.object, scope);
    // `fetch(u.href)`, the commonest spelling of the same thing, and it was the one that fell
    // through. `fetch(u)` and `fetch(u.toString())` both resolved their searchParams while
    // `fetch(u.href)` reported none, which reads as "this endpoint takes no query parameters".
    if ((node.type === "MemberExpression" || node.type === "OptionalMemberExpression") &&
        node.property && node.property.name === "href")
      return _queryFromUrlArg(node.object, scope);
    // `new URL(path, base)` whose searchParams are set on a binding
    if (node.type === "Identifier") {
      const b = scope && scope.getBinding && scope.getBinding(node.name);
      const init = b && b.path && b.path.node && b.path.node.init;
      if (init && init.type === "NewExpression" && init.callee && init.callee.name === "URL") {
        for (const rp of (b.referencePaths || [])) {
          const mp = rp.parentPath;                                  // u.searchParams
          if (!mp || !mp.node || mp.node.type !== "MemberExpression" || mp.node.object !== rp.node) continue;
          if (!mp.node.property || mp.node.property.name !== "searchParams") continue;
          const cm = mp.parentPath;                                  // u.searchParams.set
          if (!cm || !cm.node || cm.node.type !== "MemberExpression") continue;
          const nm2 = cm.node.property && cm.node.property.name;
          if (!/^(set|append)$/.test(String(nm2 || ""))) continue;
          const call = cm.parentPath;
          if (call && call.node && call.node.type === "CallExpression" && call.node.arguments[0] &&
              call.node.arguments[0].type === "StringLiteral") add(call.node.arguments[0].value);
        }
      } else fromIdent(node);
    }
  } catch {}
  return out.length ? out.slice(0, 1000) : null;
}

// The object an RTK Query endpoint's `query` returns is that endpoint's request, which collectRtk
// reports with its api's base. Read again as a table entry, it was the same request without the base:
// 302 bare copies of Grafana's endpoints once their bases resolved.
function _inRtkEndpoint(path) {
  const fn = path.getFunctionParent && path.getFunctionParent();
  const prop = fn && fn.parentPath;
  if (!prop || prop.node.type !== "ObjectProperty" || !prop.node.key) return false;
  const k = prop.node.key.name ?? prop.node.key.value;
  if (k !== "query" && k !== "queryFn") return false;
  const obj = prop.parentPath, call = obj && obj.parentPath;
  if (!call || call.node.type !== "CallExpression" || call.node.arguments[0] !== obj.node) return false;
  const c = call.node.callee;
  return !!(c && (c.type === "MemberExpression" || c.type === "OptionalMemberExpression") && c.property &&
            /^(query|mutation|infiniteQuery)$/.test(c.property.name || ""));
}

function collectEndpointTables(ast) {
  const eps = [];
  traverse(ast, { ObjectExpression(path) {
    try {
      if (_inRtkEndpoint(path)) return;
      let url = null, method = null, body = null, bodySch = null, hdrs = null, query = null, bodyFn = null;
      let security = null, media = null;
      for (const p of path.node.properties) {
        if (p.type !== "ObjectProperty" || !p.key) continue;
        const k = p.key.name ?? p.key.value;
        if ((k === "url" || k === "path" || k === "endpoint" || k === "uri") &&
            (p.value.type === "StringLiteral" || p.value.type === "TemplateLiteral")) url = resolveUrl(p.value, path.scope);
        else if (k === "method" && p.value.type === "StringLiteral") method = p.value.value.toUpperCase();
        else if ((k === "body" || k === "data") && p.value) {
          body = bodyKeys(p.value, path.scope);
          const bs = bodySchema(p.value, path.scope);
          if (bs && bs !== "{EXPR}") bodySch = bs;
          // unresolved because the serializer is in another chunk, record its NAME to join on later
          if (!body && p.value.type === "CallExpression") bodyFn = _calleeIdent(p.value.callee);
        }
        // The table entry is the WHOLE request, not just its URL and verb. A generated client returns
        // `{path, method, headers, query, body}` with headers and query built into local objects a few lines
        // above; reading only two of the five keys threw away contract we had already parsed.
        else if (k === "headers" && p.value) hdrs = headerNamesOf(p.value, path.scope);
        // A generated client states its AUTH REQUIREMENTS per operation:
        //   convert: {schema:{method:"post", url:"/api/v1/exchange/convert/",
        //                     mediaType:["application/json"],
        //                     security:["AuthHeader","CountryCodeHeader","IPHeader"]}}
        // That is the answer to "what must be sent to be allowed in" written down by the app itself, named
        // scheme by scheme and endpoint by endpoint, and we were reading past it to take only url and method.
        else if (k === "security" && p.value) {
          const sec = _stringList(p.value, path.scope);
          if (sec && sec.length) security = [...new Set(sec)];
        }
        else if ((k === "mediaType" || k === "contentType" || k === "consumes") && p.value) {
          const mt = _stringList(p.value, path.scope);
          if (mt && mt.length) media = mt[0];
        }
        else if ((k === "query" || k === "params" || k === "searchParams") && p.value) {
          const q = bodyKeys(p.value, path.scope);
          if (q && q.length) query = q.filter((x) => !String(x).startsWith("{"));
        }
      }
      // `{url: "", method: "GET"}` is a config default, not an endpoint. An empty template survives every
      // downstream filter, dedupes against nothing, and shows up as a phantom GET on a dozen hosts.
      if (url && method && String(url.template || "").trim() &&
          ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"].includes(method))
        eps.push({ sink: "config-table", kind: "xhr", method, template: url.template, body_keys: body,
                   body_schema: bodySch, req_headers: hdrs, query_schema: query, dynamic: url.dynamic,
                   security, media_type: media, op: _tableOpName(path),
                   body_fn: body ? null : bodyFn });
    } catch {}
  } });
  return eps;
}

// ── RSC SERVER ACTIONS → callable POST surface (the App-Router "crack") ──────────────────────────────────────
// Server actions are invoked from client components via `createServerReference("<hash-id>", callServer, ...)` (or
// registerServerReference). The action ID IS the callable endpoint: POST to the route with header `Next-Action:<id>`.
// The hidden part of RSC (server components → DB) isn't an HTTP endpoint anyway; server actions + route handlers
// are the real surface, and this recovers the action IDs statically.
// NOTE: `collectServerActions` was defined TWICE in this file. The first definition, deleted here,
// returned a list of id STRINGS; the second returns finished `_kind: "server-action"` records. Function
// declarations hoist, so the later one silently won and the earlier one had never run. What it left
// behind was a call site written against the string API feeding the record API, producing
// {_kind:"action", action_id:{_kind:"server-action", …}}, a record nested inside a field. ingest.py
// reads `_kind === "server-action"`, never matched it, and every Next.js server action in every
// run was dropped: 0 rows across seven targets.
//
// Same shape as the duplicate `case "CallExpression"` that sat dead in this file, and the unreachable
// field check in isSocketBinding. A duplicate definition does not fail, it picks one.

// ── RTK-Query: createApi({ endpoints: b => ({ getX: b.query({ query: a => ({url,method,body}) }) }) }) ─────────
// The endpoint builder's query/mutation fn returns a URL string OR {url,method,body}. builder.query→GET,
// builder.mutation→POST. baseUrl lives in fetchBaseQuery (a prefix), so templates are relative, still real paths.
function collectRtk(ast) {
  const eps = [];
  traverse(ast, { CallExpression(path) {
    try {
      const c = path.node.callee;
      if (!(c.type === "MemberExpression" && c.property && (c.property.name === "query" || c.property.name === "mutation"))) return;
      const arg = path.node.arguments[0];
      if (!arg || arg.type !== "ObjectExpression") return;
      const qp = arg.properties.find((p) => p.type === "ObjectProperty" && p.key && (p.key.name === "query" || p.key.name === "queryFn"));
      if (!qp || !(qp.value.type === "ArrowFunctionExpression" || qp.value.type === "FunctionExpression")) return;
      let ret = qp.value.body;
      if (ret.type === "BlockStatement") { const rs = ret.body.find((s) => s.type === "ReturnStatement"); ret = rs ? rs.argument : null; }
      if (!ret) return;
      let method = c.property.name === "mutation" ? "POST" : "GET", template = null, body = null, dyn = false;
      // A BODY THAT EXISTS BUT CANNOT BE RESOLVED IS NOT THE SAME AS NO BODY, and `body_keys: null`
      // said both. RTK Query writes `body: $.check`, where `check` is a TypeScript type erased at
      // compile time, the field names are genuinely not in the JavaScript, but the fact that this
      // request carries a `check` object IS, and it is the difference between "send nothing" and
      // "send a check". Measured on Grafana: 409 rtk-query endpoints, 6 with resolved keys, and the
      // other 403 were indistinguishable from bodyless.
      let bodyParam = null;
      if (ret.type === "ObjectExpression") {
        for (const p of ret.properties) {
          if (p.type !== "ObjectProperty" || !p.key) continue;
          const k = p.key.name ?? p.key.value;
          if (k === "url") { const u = resolveUrl(p.value, path.scope); template = u.template; dyn = u.dynamic; }
          else if (k === "method" && p.value.type === "StringLiteral") method = p.value.value.toUpperCase();
          else if (k === "body") {
            body = bodyKeys(p.value, path.scope);
            if (!body || !body.length) {
              try { bodyParam = (generateSrc(p.value) || "").slice(0, 80) || null; } catch {}
            }
          }
        }
      } else { const u = resolveUrl(ret, path.scope); template = u.template; dyn = u.dynamic; }
      if (template) {
        // The base is the createApi's own base query. `api.injectEndpoints({endpoints})` adds to an api
        // defined elsewhere, so the base is that api's.
        const host = path.findParent((q) => q.node.type === "CallExpression" && q.node.arguments[0] &&
          q.node.arguments[0].type === "ObjectExpression" &&
          q.node.arguments[0].properties.some((r) => r.key && (r.key.name ?? r.key.value) === "endpoints"));
        if (host) {
          const hc = host.node.callee;
          const inj = hc && (hc.type === "MemberExpression" || hc.type === "OptionalMemberExpression") && hc.property &&
                      /^(injectEndpoints|enhanceEndpoints)$/.test(hc.property.name || "");
          const b = inj ? clientOf(hc.object, host.scope) : _baseOfCreate(host.node, host.scope);
          if (b) { const ub = _withBase(b, { template, dynamic: dyn }); template = ub.template; dyn = ub.dynamic; }
        }
        const rec = { sink: "rtk-query", kind: "xhr", method, template, body_keys: body, dynamic: dyn };
        // RTK Query's fetchBaseQuery serialises a non-string, non-FormData body with
        // JSON.stringify and sets content-type: application/json. The endpoint definition
        // therefore never spells the encoding out, and 301 of Grafana's 358 body-bearing endpoints
        // carried none at all, a tester reading that has to guess what to send. Marked as
        // INFERRED with its reason, the way every other inferred field in this file is, because a
        // default is a different kind of fact from something read at the call site.
        if ((body || bodyParam) && !rec.body_encoding) {
          rec.body_encoding = "json";
          rec.content_type = "application/json";
          rec.body_encoding_basis = "INFERRED: RTK Query's fetchBaseQuery JSON-encodes an object "
                                  + "body by default; the endpoint definition does not state it";
        }
        if (!body && bodyParam) {
          rec.body_param = bodyParam;
          rec.note = "a body IS sent; its field names are a compile-time type and are not in the "
                   + "shipped JavaScript. The expression is recorded instead.";
        }
        eps.push(rec);
      }
    } catch {}
  } });
  return eps;
}

// ── Relay: compiled concreteRequest carries params:{name, operationKind, text}, the GraphQL op + (often) its SDL.
function collectRelay(ast) {
  const ops = [];
  traverse(ast, { ObjectExpression(path) {
    try {
      let kind = null, name = null, text = null;
      for (const p of path.node.properties) {
        if (p.type !== "ObjectProperty" || !p.key) continue;
        const k = p.key.name ?? p.key.value;
        if (k === "operationKind" && p.value.type === "StringLiteral") kind = p.value.value;
        else if (k === "name" && p.value.type === "StringLiteral") name = p.value.value;
        else if (k === "text" && p.value.type === "StringLiteral") text = p.value.value;
      }
      if (kind && name) ops.push({ name, kind, text });
    } catch {}
  } });
  return ops;
}

// ── Apollo: gql`...` tags are compiled to DocumentNode AST OBJECT LITERALS at build ({kind:"OperationDefinition",
//   name:{value}, variableDefinitions, selectionSet:{selections:[{kind:"Field",...}]}}). Unlike Relay's flat
//   {operationKind,text}, Apollo ships the full AST, so we RECONSTRUCT the query text by walking the literal.
//   This recovers the complete selection set (every field) statically, no runtime capture / browser needed.
function collectApollo(ast) {
  const sVal = n => (n && n.type === "StringLiteral") ? n.value : null;
  const gp = (o, key) => { if (!o || o.type !== "ObjectExpression") return null; for (const p of o.properties) { const k = p.key && (p.key.name ?? p.key.value); if (k === key) return p.value; } return null; };
  const nv = o => sVal(gp(o, "value"));
  const pType = t => { if (!t) return ""; const k = sVal(gp(t, "kind")); if (k === "NonNullType") return pType(gp(t, "type")) + "!"; if (k === "ListType") return "[" + pType(gp(t, "type")) + "]"; return nv(gp(t, "name")) || ""; };
  const pVal = v => { const k = sVal(gp(v, "kind")); if (k === "Variable") return "$" + nv(gp(v, "name")); if (["IntValue", "FloatValue", "BooleanValue", "EnumValue"].includes(k)) return String(sVal(gp(v, "value"))); if (k === "StringValue") return JSON.stringify(sVal(gp(v, "value")) || ""); if (k === "NullValue") return "null"; if (k === "ListValue") return "[]"; if (k === "ObjectValue") return "{}"; return "…"; };
  const pArgs = a => { if (!a || a.type !== "ArrayExpression" || !a.elements.length) return ""; return "(" + a.elements.map(x => nv(gp(x, "name")) + ": " + pVal(gp(x, "value"))).join(", ") + ")"; };
  const pDir = d => { if (!d || d.type !== "ArrayExpression" || !d.elements.length) return ""; return " " + d.elements.map(x => "@" + nv(gp(x, "name")) + pArgs(gp(x, "arguments"))).join(" "); };   // @include(if:$x)/@skip, dropping these breaks "variable never used"
  const pSel = (ss, frags) => { if (!ss) return ""; const sels = gp(ss, "selections"); if (!sels || sels.type !== "ArrayExpression") return ""; return sels.elements.map(s => { const k = sVal(gp(s, "kind")); if (k === "Field") { const nm = nv(gp(s, "name")); const al = nv(gp(s, "alias")); const sub = pSel(gp(s, "selectionSet"), frags); return (al ? al + ": " : "") + nm + pArgs(gp(s, "arguments")) + pDir(gp(s, "directives")) + (sub ? " { " + sub + " }" : ""); } if (k === "FragmentSpread") { const fn = nv(gp(s, "name")); if (frags) frags.add(fn); return "..." + fn + pDir(gp(s, "directives")); } if (k === "InlineFragment") { const tc = gp(s, "typeCondition"); return "... on " + nv(gp(tc, "name")) + pDir(gp(s, "directives")) + " { " + pSel(gp(s, "selectionSet"), frags) + " }"; } return ""; }).filter(Boolean).join(" "); };
  const pVarDefs = v => { if (!v || v.type !== "ArrayExpression" || !v.elements.length) return ""; return "(" + v.elements.map(vd => "$" + nv(gp(gp(vd, "variable"), "name")) + ": " + pType(gp(vd, "type"))).join(", ") + ")"; };
  const pFrag = (node, frags) => "fragment " + nv(gp(node, "name")) + " on " + nv(gp(gp(node, "typeCondition"), "name")) + " { " + pSel(gp(node, "selectionSet"), frags) + " }";
  const opNodes = {}, fragNodes = {};
  traverse(ast, { ObjectExpression(path) { try { const k = sVal(gp(path.node, "kind")); if (k === "OperationDefinition") { const nm = nv(gp(path.node, "name")); if (nm && !opNodes[nm]) opNodes[nm] = path.node; } else if (k === "FragmentDefinition") { const nm = nv(gp(path.node, "name")); if (nm && !fragNodes[nm]) fragNodes[nm] = path.node; } } catch {} } });
  const out = [];
  for (const [nm, node] of Object.entries(opNodes)) {
    try {
      const op = sVal(gp(node, "operation")) || "query";
      const used = new Set();
      let text = op + " " + nm + pVarDefs(gp(node, "variableDefinitions")) + " { " + pSel(gp(node, "selectionSet"), used) + " }";
      const seen = new Set(), queue = [...used];
      while (queue.length) { const f = queue.pop(); if (seen.has(f) || !fragNodes[f]) continue; seen.add(f); const sub = new Set(); text += " " + pFrag(fragNodes[f], sub); for (const x of sub) if (!seen.has(x)) queue.push(x); }
      out.push({ name: nm, kind: op, text });
    } catch {}
  }
  return out;
}

// ── Connect-RPC / gRPC-web / protobuf-es: generated service def is an object literal
//   { typeName: "pkg.v1.FooService", methods: { getBar: { name: "GetBar", I:.., O:.., kind:.. }, ... } }
// Both Connect and gRPC-web POST to `/{typeName}/{Method.name}`. The service def IS the wire contract, typeNames
// and method names are protocol strings, survive minification. Message-type descriptors also carry `typeName` but
// have NO `methods` object, so requiring `methods` filters them out (incl. the google.protobuf.* runtime internals).
function collectConnect(ast) {
  const eps = [];
  traverse(ast, { ObjectExpression(path) {
    try {
      let typeName = null, methodsNode = null;
      for (const p of path.node.properties) {
        if (p.type !== "ObjectProperty" || !p.key) continue;
        const k = p.key.name ?? p.key.value;
        if (k === "typeName" && p.value.type === "StringLiteral") typeName = p.value.value;
        else if ((k === "methods" || k === "method") && p.value.type === "ObjectExpression") methodsNode = p.value;
      }
      if (!typeName || !methodsNode) return;                       // needs BOTH → real service def, not a message type
      if (/^\.?google\.protobuf\./.test(typeName)) return;         // protobuf-es runtime internals, not app services
      const svc = typeName.replace(/^\./, "");
      for (const m of methodsNode.properties) {
        if (m.type !== "ObjectProperty" || !m.value || m.value.type !== "ObjectExpression") continue;
        let mname = null;
        for (const mp of m.value.properties) {
          if (mp.type !== "ObjectProperty" || !mp.key) continue;
          if ((mp.key.name ?? mp.key.value) === "name" && mp.value.type === "StringLiteral") mname = mp.value.value;
        }
        mname = mname || m.key.name || m.key.value;
        if (!mname || typeof mname !== "string") continue;
        eps.push({ sink: "connect-rpc", kind: "rpc", method: "POST",
                   template: `/${svc}/${mname}`, body_keys: null, dynamic: false });
      }
    } catch {}
  } });
  return eps;
}

// ── SWR: useSWR(key, fetcher), key IS the URL. Import-binding based (works on ESM/sourcemap source; in fully
// bundled code the import is erased → SWR keys still land via the literal-grep backstop). GET (mutations rare).
function collectSwr(ast) {
  const names = new Set(), eps = [];
  traverse(ast, { ImportDeclaration(path) {
    if (/^swr(\/|$)/.test(path.node.source.value)) for (const s of path.node.specifiers) if (s.local) names.add(s.local.name);
  } });
  if (!names.size) return eps;
  traverse(ast, { CallExpression(path) {
    try {
      const c = path.node.callee;
      if (!(c.type === "Identifier" && names.has(c.name))) return;
      const u = resolveUrl(path.node.arguments[0], path.scope);
      const sk = u.template.replace(/\{[^}]*\}/g, "");
      if (LOOKS_PATH(u.template) || LOOKS_PATH(sk)) eps.push({ sink: "useSWR", kind: "fetch", method: "GET", template: u.template, body_keys: null, dynamic: u.dynamic });
    } catch {}
  } });
  return eps;
}

// merge two records for the SAME (method, template), UNION their body fields (a PATCH/POST endpoint called from
// several sites patches different fields; keeping just the first loses the rest), plus response_keys/headers/src.
function _uniqPush(arr, add) { const s = new Set(arr); for (const x of add) if (!s.has(x)) { arr.push(x); s.add(x); } }
function mergeRecord(prev, f) {
  if (f.body_keys) { if (!prev.body_keys) prev.body_keys = [...f.body_keys]; else _uniqPush(prev.body_keys, f.body_keys); }
  if (f.body_src) prev.body_src = { ...(prev.body_src || {}), ...f.body_src };
  if (f.response_keys) { if (!prev.response_keys) prev.response_keys = [...f.response_keys]; else _uniqPush(prev.response_keys, f.response_keys); }
  if (!prev.headers && f.headers) prev.headers = f.headers;
  // Fields that only ONE of the duplicate records carries. The same endpoint is commonly described twice, once
  // by a call site and once by a declarative table, and whichever arrived first was keeping its blanks: the
  // record that knew the operation name and the required auth schemes lost them to a bare earlier duplicate.
  for (const k of ["op", "security", "media_type", "req_headers", "query_schema", "body_schema", "group",
                   "body_from", "credentials"])
    if (prev[k] == null && f[k] != null) prev[k] = f[k];
  return prev;
}

// ---- tagged-template route DSLs -------------------------------------------------------------------------
// A whole class of apps never calls fetch/axios by name. They ship a typed-route DSL where the verb is a
// member and the path is the template tag, and they COMPOSE paths through intermediate variables:
//     Ec = E`/${`accountId`}/images`;  Oc = E`${Ec}/transformations`
//     T(`userDetails`).get`/user/details`
//     T(`accountContractRoute`).post`/accounts/${`accountId`}/contract`
// One dashboard's dashboard is 26MB of JS with ZERO occurrences of `fetch(` or `axios`, every request is one
// of these. Without this sink such a target reports "no API" rather than "not supported", which is the worst
// possible failure because it is indistinguishable from a real answer.
// Inside these DSLs a string-literal interpolation names the slot rather than supplying a value, so
// `${`accountId`}` renders as {accountId}, not as the literal text "accountId".
const _VERB_TAG = /^(get|post|put|patch|delete|del|head|options|query|mutate)$/i;
const _PATHY_TMPL = (t) => /^\//.test(t) || /^(https?|wss?):\/\//.test(t);

function _slotFor(e, map, depth, scope) {
  if (!e) return "{EXPR}";
  if (e.type === "StringLiteral") return "{" + e.value + "}";
  if (e.type === "TemplateLiteral" && e.expressions.length === 0)
    return "{" + (e.quasis[0].value.cooked ?? e.quasis[0].value.raw ?? "") + "}";
  if (e.type === "TemplateLiteral" || e.type === "TaggedTemplateExpression")
    return _renderTagged(e, map, depth + 1, scope);
  if (e.type === "Identifier")
    return _slotForIdentifier(e, map, depth, scope);
  if (e.type === "MemberExpression" && e.property && e.property.name) return "{" + e.property.name + "}";
  return "{EXPR}";
}

function _renderTagged(node, map, depth = 0, scope = null) {
  if (!node || depth > 8) return "{EXPR}";
  const q = node.type === "TaggedTemplateExpression" ? node.quasi : node;
  if (!q || !q.quasis) return "{EXPR}";
  let out = "";
  for (let i = 0; i < q.quasis.length; i++) {
    out += q.quasis[i].value.cooked ?? q.quasis[i].value.raw ?? "";
    if (i < q.expressions.length) out += _slotFor(q.expressions[i], map, depth, scope);
  }
  return out;
}

// RESOLVE BY BINDING, NEVER BY NAME.
//
// This map was keyed by the variable's NAME, file-wide, and a slot was filled with `map.get(name)`.
// In minified code a one-letter name is reused hundreds of times per file, so on claude.ai the SDK's
//     create(e, t, n) { return this._client.post(Kb`/v1/deployments/${e}/archive`, ...) }
// had its `${e}` filled from an unrelated `let e = \`hook_${this.nextCallbackId++}\`` in a hook
// registry elsewhere in the same 1.4 MB file, and 66 SDK routes came out as `/v1/.../hook_{EXPR}`.
// That is a fabricated path segment, which is worse than an honest slot.
//
// So the key is Babel's Binding object, which IS the variable rather than its spelling, and a slot is
// only filled when the identifier at the use site resolves to that same binding.
function _slotForIdentifier(e, map, depth, scope) {
  const b = scope && scope.getBinding ? scope.getBinding(e.name) : null;
  // A parameter's value comes from the caller. Nothing inside this file defines it, so there is no
  // template to unfold, only a name to keep.
  if (!b || b.kind === "param") return "{" + e.name + "}";
  const hit = map.get(b);
  return hit ? _renderTagged(hit.node, map, depth + 1, hit.scope) : "{" + e.name + "}";
}

// binding -> {template node, the scope it was written in}, so `Oc = E\`${Ec}/transformations\`` can be
// unfolded through Ec, resolving Ec where Oc was defined rather than where Oc is used.
function collectTemplateVars(ast) {
  const map = new Map();
  const take = (p, name, rhs) => {
    if (!name || !rhs) return;
    const tagged = rhs.type === "TaggedTemplateExpression" && rhs.tag && rhs.tag.type === "Identifier";
    if (!tagged && rhs.type !== "TemplateLiteral") return;
    const b = p.scope.getBinding(name);
    if (b) map.set(b, { node: rhs, scope: p.scope });
  };
  traverse(ast, {
    AssignmentExpression(p) {
      if (p.node.left.type === "Identifier") take(p, p.node.left.name, p.node.right);
    },
    VariableDeclarator(p) {
      if (p.node.id.type === "Identifier") take(p, p.node.id.name, p.node.init);
    },
  });
  return map;
}


// ── TYPESCRIPT TYPES, WHEN THE TARGET SHIPS ITS OWN SOURCE ─────────────────────────────────────────────
// Types are the one thing minification genuinely destroys, they are erased at compile time and exist
// nowhere in a bundle. Everything else (routes, verbs, wire keys) survives, because a key that crosses
// the wire cannot be renamed without breaking the server.
//
// So when sourcemaps.py recovers a target's original source, this is the payoff, and it is the ONLY
// payoff: measured, original sources add under 1% to endpoint discovery. Of the targets that serve maps,
// most ship real TypeScript (one target 67 type aliases, another 88, a third 93 interfaces); some ship
// post-transpile JS where the types are already gone, and there is nothing to recover there.
//
// A body handed in as `data: CreateInquiryRequest` is unresolvable by every runtime-shape walker in this
// file, there is no object literal to read. The declaration is right there in the same file.
const _TS_PRIM = {
  TSStringKeyword: "string", TSNumberKeyword: "number", TSBooleanKeyword: "boolean",
  TSNullKeyword: "null", TSUndefinedKeyword: "undefined", TSAnyKeyword: "any",
  TSUnknownKeyword: "unknown", TSVoidKeyword: "void", TSBigIntKeyword: "bigint",
  TSNeverKeyword: "never", TSObjectKeyword: "object", TSSymbolKeyword: "symbol",
};

function collectTsTypes(ast) {
  const out = new Map();
  try {
    traverse(ast, {
      TSInterfaceDeclaration(p) { if (p.node.id) out.set(p.node.id.name, p.node.body); },
      TSTypeAliasDeclaration(p) { if (p.node.id) out.set(p.node.id.name, p.node.typeAnnotation); },
      TSEnumDeclaration(p) { if (p.node.id) out.set(p.node.id.name, { type: "__enum" }); },
    });
  } catch { /* a type index is an optimisation; never fail extraction over it */ }
  return out;
}

/** A TS type node -> the same {field: type} shape the runtime walkers produce. */
function tsShape(node, depth = 0, seen = new Set()) {
  if (!node || depth > 6) return null;
  const t = node.type;
  if (_TS_PRIM[t]) return _TS_PRIM[t];
  if (t === "TSLiteralType" && node.literal)
    return "const:" + String(node.literal.value ?? node.literal.raw ?? "");
  if (t === "TSArrayType") { const e = tsShape(node.elementType, depth + 1, seen); return e ? [e] : ["?"]; }
  if (t === "TSTypeAnnotation") return tsShape(node.typeAnnotation, depth + 1, seen);
  if (t === "TSParenthesizedType") return tsShape(node.typeAnnotation, depth + 1, seen);
  if (t === "TSTypeLiteral" || t === "TSInterfaceBody") {
    const o = {};
    for (const m of (node.members || node.body || [])) {
      if (m.type !== "TSPropertySignature" || !m.key) continue;
      const k = m.key.name ?? m.key.value;
      if (k == null) continue;
      const v = tsShape(m.typeAnnotation, depth + 1, seen);
      // Structure beats annotation: a nested object or array is kept as itself. For a leaf, optionality
      // is worth carrying, a required field missing from a request is a different test from an optional
      // one, and it is the difference between probing a validator and probing the handler behind it.
      o[String(k)] = (v && typeof v === "object") ? v
                   : (m.optional ? "optional " : "") + (typeof v === "string" && v ? v : "?");
    }
    return Object.keys(o).length ? o : null;
  }
  if (t === "TSIntersectionType") {
    // `A & B` is one object with both field sets, which is exactly how these codebases compose a
    // request type out of a shared base and the bits specific to one call.
    const o = {};
    for (const x of node.types || []) {
      const v = tsShape(x, depth + 1, seen);
      if (v && typeof v === "object" && !Array.isArray(v)) Object.assign(o, v);
    }
    return Object.keys(o).length ? o : null;
  }
  if (t === "TSUnionType") {
    const parts = (node.types || []).map((x) => tsShape(x, depth + 1, seen)).filter(Boolean);
    const objs = parts.filter((x) => x && typeof x === "object");
    if (objs.length === 1) return objs[0];
    const lits = parts.filter((x) => typeof x === "string");
    return lits.length ? "enum?:" + lits.join("|") : null;
  }
  if (t === "TSTypeReference" && node.typeName) {
    const nm = node.typeName.name || (node.typeName.right && node.typeName.right.name);
    if (!nm || seen.has(nm)) return nm ? "type:" + nm : null;   // cycle guard, and still say what it was
    const decl = _TS_TYPES.get(nm);
    if (!decl) return "type:" + nm;                             // declared in another module; name it
    if (decl.type === "__enum") return "enum:" + nm;
    seen.add(nm);
    const r = tsShape(decl, depth + 1, seen);
    seen.delete(nm);
    return r || ("type:" + nm);
  }
  return null;
}

/** `(data: SomeRequest)` at a body position -> SomeRequest's shape. */
function tsShapeOfParam(node, scope) {
  if (!node || node.type !== "Identifier" || !_TS_TYPES.size) return null;
  const b = scope && scope.getBinding && scope.getBinding(node.name);
  if (!b || b.kind !== "param" || !b.path || !b.path.node) return null;
  const ann = b.path.node.typeAnnotation;
  return ann ? tsShape(ann) : null;
}

let _TS_TYPES = new Map();

// IS THERE tRPC IN THIS FILE, asked of the AST, not of the text.
//
// This was `/@trpc|\/api\/trpc\/|createTRPCReact|.../.test(src)`, and a regex over raw source cannot
// tell code from commentary: a file whose COMMENT mentions `/api/trpc` was marked as a tRPC app, which
// is how the fixture written to prove the marker works ended up disproving it. Comments and
// non-code text are not AST node values, so asking the tree removes the whole class.
//
// Three structural facts, any one of which is evidence:
//   an import/export from `@trpc/...`     , the dependency, in an unbundled or partially-bundled file
//   a name `createTRPC*`                   , the client factory, which survives as a property name
//   a string literal containing `/api/trpc`, the endpoint itself; cal.com ships `R = "/api/trpc"`
//
// Gated on a cheap substring precheck first: if the text is absent entirely there is nothing to
// confirm, and that is the common case over a 1400-chunk walk.
// HASH ROUTING OR PATH ROUTING, and why the obvious test cannot answer it.
//
// This was a regex over the chunk's source, and it was wrong in both directions. Measured over 11
// corpora holding 557 route records it produced exactly TWO labels and both were wrong, Grafana's
// and Slack's, each a path-routed app called hash-routed.
//
// Grafana was called hash-routed because `createHashRouter` appears inside react-router's error string
// ("...if you are using createHashRouter and the user manually changes the URL") and `HashRouter`
// appears in its export map as `HashRouter:()=>At`. A library EXPORTING a router says nothing about
// which one the app renders, and Grafana calls neither factory by name anywhere.
//
// Call sites alone are no better. react-router ships both components and each calls its own history
// factory inside its own body, so every bundle containing react-router contains both calls:
//
//     function createHashHistory(_={}){...}                        // the library's definition
//     Y.current = createHashHistory({window:H, v5Compat:!0})       // inside HashRouter's body
//     Y.current = createBrowserHistory({window:H, v5Compat:!0})    // inside BrowserRouter's body
//
// What distinguishes the app from the library is WHO IS CALLING. A call whose callee binds to a
// function declared in this same file is the library calling itself; a call whose callee is imported,
// or cannot be resolved locally at all, is code reaching into the router from outside. Only the second
// is evidence.
//
// The field stays absent unless one side has evidence and the other does not, an app that genuinely
// renders `<HashRouter>` through a minified alias is not detectable this way, and saying nothing is
// the honest output. None of the corpora here produce a positive, which is why the only true-positive
// coverage is a fixture; a wrong label here is worse than none, because the docstring below is right
// that it decides whether a route table is a map or a work list.
const _HASH_ROUTERS = new Set(["createHashRouter", "createWebHashHistory", "createHashHistory"]);
const _PATH_ROUTERS = new Set(["createBrowserRouter", "createWebHistory", "createBrowserHistory"]);

function detectRouting(ast, src) {
  if (!ast || !src) return null;
  if (!/createHashRouter|createWebHashHistory|createHashHistory|createBrowserRouter|createWebHistory|createBrowserHistory/.test(src))
    return null;
  let hash = false, path = false;
  try {
    traverse(ast, {
      CallExpression(p) {
        const c = p.node.callee;
        const name = c && (c.name || (c.property && !c.computed && c.property.name));
        if (!name) return;
        const isHash = _HASH_ROUTERS.has(name), isPath = _PATH_ROUTERS.has(name);
        if (!isHash && !isPath) return;
        // The library calling itself: the callee binds to a function declared right here.
        if (c.type === "Identifier") {
          const b = p.scope.getBinding(name);
          const kind = b && b.path && b.path.node && b.path.node.type;
          if (kind === "FunctionDeclaration" || kind === "VariableDeclarator" ||
              kind === "FunctionExpression" || kind === "ArrowFunctionExpression") return;
        }
        if (isHash) hash = true; else path = true;
      },
    });
  } catch { return null; }
  if (hash === path) return null;          // both, or neither, nothing to state
  return hash ? "hash" : "path";
}

function detectTrpc(ast, src) {
  if (!/@trpc|\/api\/trpc|createTRPC/.test(src)) return false;
  let found = false;
  const isFactory = (n) => typeof n === "string" && /^createTRPC/.test(n);
  try {
    traverse(ast, {
      ImportDeclaration(p) {
        if (p.node.source && /^@trpc\//.test(String(p.node.source.value || ""))) found = true;
      },
      ExportNamedDeclaration(p) {
        if (p.node.source && /^@trpc\//.test(String(p.node.source.value || ""))) found = true;
      },
      Identifier(p) { if (isFactory(p.node.name)) found = true; },
      StringLiteral(p) {
        const v = String(p.node.value || "");
        if (v.includes("/api/trpc") || /^@trpc\//.test(v) || isFactory(v)) found = true;
      },
      TemplateElement(p) {
        const v = String((p.node.value && p.node.value.cooked) || "");
        if (v.includes("/api/trpc")) found = true;
      },
    });
  } catch { /* a traversal failure is not evidence either way */ }
  return found;
}

function extract(src) {
  _SRC = src;
  const ast = parse(src);
  if (!ast) return [];
  const TMPLVARS = /`/.test(src) ? collectTemplateVars(ast) : new Map();
  // Only when the file actually declares types, a minified bundle never does, so this costs it nothing.
  _TS_TYPES = /\b(interface|type)\s+[A-Z]/.test(src) ? collectTsTypes(ast) : new Map();
  _GETTERS = /\bget\s+[A-Za-z_$]/.test(src) || /\bclass\b/.test(src)
            ? indexObjectGetters(ast) : new Map();
  // gate the two per-chunk collectors on a cheap substring precheck, a wrapper must contain a fetch sink, a
  // response schema must contain `.json(`; skipping chunks without them keeps the 1400-chunk walk fast.
  const WRAPPERS = /fetch\s*\(|queryKey|mutationKey|\bmethod:|\.(?:get|post|put|patch|delete|request)\s*\(/.test(src)
                 ? collectWrappers(ast) : {};
  const WRAPPER_FNS = new Set(Object.values(WRAPPERS).flat().map((w) => w.fnNode));
  // Which wrappers in THIS file take react-query options in their trailing argument, learned from the call
  // sites that say so unambiguously. Per file, because a minified local name means nothing outside one.
  _QUERY_OPT_CALLEES = /onSuccess|onError|mutationKey|queryKey|onSettled/.test(src)
                     ? indexQueryOptCallees(ast) : new Set();
  // UNGATED, and that is the fix. This was gated on an origin literal being present, on the reasoning
  // that "if a file contains no origin literal it can carry no base", which is false. A base is
  // routinely RELATIVE:
  //
  //     const client = axios.create({ baseURL: "/api/v2" });
  //     client.get("/users")        // -> /api/v2/users
  //
  // There is no `//host.tld` anywhere in that file, so the collector never ran, `INSTANCES` came back
  // empty, and the call was reported as `/users`. That is not a missing record, it is a WRONG one, a
  // real-looking route pointed at the wrong place, which is the worst output this tool can produce.
  //
  // The gate existed for speed, so the speed was measured rather than assumed: on Grafana's ten
  // largest chunks, gated 93.4s vs ungated 94.4s for identical output, about 1%, inside the noise,
  // because any chunk big enough to matter contains an origin literal and ran the collector anyway.
  // A premature optimisation whose predicate is wrong is worth deleting, not repairing.
  const INSTANCES = collectAxiosInstances(ast);
  // prefix an axios-instance's baseURL onto a relative-path call: X.get("/p") + X=axios.create({baseURL}) → base+/p.
  const applyBase = (nm, u, callee, scope) => {
    // By binding first: exact, and it reaches clients built in other modules. The name lookup below is
    // what is left for `this.client`, which has no binding to follow.
    let inst = (callee && scope ? callClient(callee, scope) : null) ||
               INSTANCES[nm.obj] || (nm.obj ? INSTANCES["this." + nm.obj] : null);
    // the client can also be chosen inline at the call: `(chainId===mainnet ? mainnetClient : testnetClient).get(path)`.
    // Same two-host fan-out as the aliased form, just written at the use site instead of bound to a name.
    // `(opts?.client ?? client).get({url})`, the defaulting receiver every generated OpenAPI client emits,
    // and the shape that carried 186 of one target's ~280 call sites. The left branch is an optional member
    // read that resolves to nothing; the right is the module-level client with the base on it. Taking
    // whichever branch resolves is the same reasoning the conditional form already used, and `??`/`||`
    // differ from `?:` only in where the branches live on the node.
    if (!inst && nm.objNode &&
        (nm.objNode.type === "ConditionalExpression" || nm.objNode.type === "LogicalExpression")) {
      const branches = nm.objNode.type === "ConditionalExpression"
        ? [nm.objNode.consequent, nm.objNode.alternate]
        : [nm.objNode.left, nm.objNode.right];
      const br = branches
        .map((n) => (n && n.type === "Identifier" ? INSTANCES[n.name] : null)).filter(Boolean);
      if (br.length) {
        const bases = [...new Set(br.map((x) => x.base))];
        inst = { base: bases[0], dyn: br.some((x) => x.dyn), alts: bases.slice(1) };
      }
    }
    return _withBase(inst, u);
  };
  // The corpus verdict when the index pass reached one, this chunk's own reading otherwise, which is
  // the right answer for a direct single-file invocation and for `--index-only` not having run.
  const isTrpc = _IS_TRPC !== null ? _IS_TRPC : detectTrpc(ast, src);
  const found = [];
  const RESP = src.includes(".json(") ? collectResponseSchemas(ast) : new Map();
  // published so a call site in a DIFFERENT chunk can resolve `SomeRequestToJSON(x)` to its field list
  if (/ToJSON|Serializ|toJSON/.test(src)) for (const sz of collectSerializers(ast)) found.push(sz);
  let curFn = null;                            // enclosing function of the record currently being pushed
  let curPath = null;                          // …and its path, for the op-name walk in `push`
  const push = (o) => {
    // ws-message records legitimately have no target: the URL belongs to the socket, not the message.
    if (!o.template && o.kind !== "ws-message") return;
    if (!o.template) { out.push(o); return; }
    o.template = String(o.template).replace(/([^:])\/{2,}/g, "$1/");   // collapse `//` from base+path concat (keep `://`)
    // EXPAND enum-dispatched segments: `\x02completion|retry|stop\x02` → one record per action value
    const em = o.template.match(/\x02([^\x02]*)\x02/);
    if (em) {
      for (const v of em[1].split("|")) push({ ...o, template: o.template.replace(/\x02[^\x02]*\x02/, v) });
      return;
    }
    // keep only things that look like a request target (path/url), drop pure {EXPR} with no literal skeleton,
    // EXCEPT keep it when it carries a substantial request BODY (≥2 real keys). A `fetch({EXPR-url}, {body:{...}})`
    // whose URL is unresolvable still holds a real body worth recovering; the co-location merge later staples that
    // body onto the matching resolved-URL record (the "surround"). Marked _bodyOnly so merge can find it.
    const skeleton = o.template.replace(/\{[^}]*\}/g, "");
    const pathish = LOOKS_PATH(o.template) || LOOKS_PATH(skeleton) || /^(https?|wss?):\/\//.test(o.template);
    const realBody = Array.isArray(o.body_keys) && o.body_keys.filter((k) => !String(k).startsWith("{")).length >= 2;
    // A SOCKET IS A FINDING EVEN WITHOUT ITS URL. `new WebSocket(e)` is how everyone writes it, the URL is
    // assembled at runtime from a base and a token, so this test discarded literally every WebSocket in
    // every bundle, and the map said the application had none. That is the opposite of true for a
    // real-time product, where the socket carries the domain events. The existence of the connection,
    // and any message shape recovered for it, are worth more than the URL we could not resolve.
    const isSocket = o.kind === "ws" || o.kind === "sse" || o.kind === "ws-message";
    if (!pathish && !realBody && !o.registry_key && !isSocket) return;
    if (!pathish && (realBody || o.registry_key)) o._bodyOnly = true;
    if (/\b(example\.(com|org|net)|localhost|127\.0\.0\.1|schema\.org|w3\.org)\b/.test(o.template)) return; // lib placeholders
    // A STATIC FILE IS NOT AN API REQUEST. LOOKS_PATH turned away a relative path naming an image or a
    // font, but a full URL passed on the scheme alone, so Discord's `.mp4` CDN assets, HubSpot's
    // `bundle.production.js` and an `.svg` icon came out as endpoints. Same rule for both now.
    if (!isSocket && _STATIC_FILE.test(o.template.split(/[?#]/)[0])) return;
    // THE WRAPPER ITSELF IS NOT AN ENDPOINT. `req = (p, o) => fetch(BASE + p, o)` emitted
    // `https://api.x.test{param:p}/` beside every real request its callers make. A URL whose whole path is
    // the enclosing wrapper's own parameter says where the wrapper sends things; its call sites say what.
    if (curFn && WRAPPER_FNS.has(curFn) && !String(o.sink || "").startsWith("wrap:")) {
      const rest = o.template.replace(/^(?:https?:)?\/\/[^/{]+/, "");
      if (/^\/?\{param:[\w$]+\}\/?$/.test(rest)) return;
    }
    o._fn = curFn;
    // computed here rather than in `guard` so the 24-step parent walk is paid once per RECORD, not once per
    // visited call expression (the latter is a measurable cost on a 5 MB chunk).
    if (curPath && o.op === undefined) {
      const nm = opNameOf(curPath);
      if (nm) { o.op = nm.op; if (nm.group.length) o.group = nm.group; }
    }
    if (curPath && !_structuredQ(o.query_schema)) {
      try {
        const cn = curPath.node;
        const a0 = cn && cn.arguments && cn.arguments[0];
        const qn = _queryFromUrlArg(a0, curPath.scope);
        if (qn) o.query_schema = qn.map((n) => ({ name: n, source: "urlsearchparams" }));
      } catch {}
    }
    if (curPath && !o.response_keys) {
      const rk = responseAtCallSite(curPath);
      if (rk && rk.length) { o.response_keys = rk.slice(0, 1000); o.response_from = "call-site"; }
    }
    found.push(o);
  };

  // per-node try/catch: a single malformed node must not abort the whole-file traversal (else one bad
  // call-site silently drops ALL of a chunk's endpoints, the 314-match-but-0-output bug). ALSO tracks the
  // enclosing function so response schemas can be attached to the right sink post-hoc.
  const guard = (fn) => (path) => {
    try {
      const fp = path.getFunctionParent();
      curFn = fp ? fp.node : null;
      curPath = path;
      fn(path);
    } catch {}
  };
  const callsByName = new Map();   // callee-name → [{args, scope}] for the inter-procedural body-param resolution
  traverse(ast, {
    TaggedTemplateExpression: guard(function (path) {
      const n = path.node;
      const tag = n.tag;
      if (!tag) return;
      // Bare-tag form: a route-constant registry, where the path is defined in a nested object of templates
      //     zero_trust: { applications: E`/accounts/${`accountId`}/access/apps`, policies: E`…` }
      // and the verb is only applied later at the use site. The path is still the discovery, emitting it
      // with an honest UNKNOWN method beats dropping it. On that dashboard this shape holds ~200 of the
      // ~250 real paths; the verb-tagged form holds the rest.
      if (tag.type === "Identifier") {
        const t = _renderTagged(n, TMPLVARS, 0, path.scope);
        if (!_PATHY_TMPL(t)) return;
        const prop = path.parent && path.parent.type === "ObjectProperty" ? path.parent.key : null;
        push({
          sink: "route-const", kind: "dsl", method: "UNKNOWN", template: t, body_keys: null,
          name: prop ? prop.name || prop.value : undefined, tag: tag.name, dynamic: /\{/.test(t),
        });
        return;
      }
      if (tag.type !== "MemberExpression" || tag.computed) return;
      const verb = tag.property && tag.property.name;
      if (!verb || !_VERB_TAG.test(verb)) return;
      const tmpl = _renderTagged(n, TMPLVARS, 0, path.scope);
      // styled.div`…`, chalk.red`…` and friends also tag off a member; requiring a request-shaped result is
      // what separates a route DSL from every other tagged template in the file.
      if (!_PATHY_TMPL(tmpl)) return;
      const m = verb.toLowerCase();
      // The tagged template DECLARES the route; the body arrives when the resulting handle is invoked:
      //     $J = T(`user`).put`/user/password`      …later…    $J({oldPassword, newPassword})
      // Nothing at the declaration says what is sent, so every write through this DSL had a null body, and on
      // that dashboard the DSL is the entire API. The handle is an ordinary binding, so its call sites are
      // reachable the same way a function's are.
      const meth = (m === "del" ? "DELETE" : m === "query" ? "GET" : m === "mutate" ? "POST" : m).toUpperCase();
      let handleBody = null;
      if (/^(POST|PUT|PATCH|DELETE)$/.test(meth)) handleBody = _handleCallBody(path);
      push({
        sink: "tagged-template", kind: "dsl",
        method: meth,
        template: tmpl, body_keys: handleBody,
        dynamic: /\{/.test(tmpl),
      });
    }),
    NewExpression: guard(function (path) {
      const n = path.node;
      // `new g.WebSocket(...)`, the constructor reached through a module namespace, which is what a
      // bundler emits whenever the global was imported from a shim. Reading only `callee.name` sees
      // nothing there: reddit ships 4 of its 5 sockets as `new g.WebSocket(` and 1 as `new tV.WebSocket(`,
      // so the whole realtime layer was invisible. The property name is what survives minification,
      // the same reason every other collector in this file matches on it.
      const cn = n.callee && (n.callee.name ||
                              (n.callee.property && (n.callee.property.name ?? n.callee.property.value)));
      if (cn === "Request") {
        const u = resolveUrl(n.arguments[0], path.scope);
        const oi = optsInfo(n.arguments[1], path.scope);
        push({ sink: "Request", kind: "fetch", method: oi.method || "GET", template: u.template, body_keys: oi.body_keys, body_encoding: oi.body_encoding, content_type: oi.content_type, body_schema: oi.body_schema, query_schema: oi.query_schema, req_headers: oi.headers, credentials: oi.credentials, dynamic: u.dynamic });
      } else if (cn === "WebSocket") {
        const u = resolveUrl(n.arguments[0], path.scope);
        push({ sink: "WebSocket", kind: "ws", method: "WS", template: u.template, body_keys: null, dynamic: u.dynamic });
      } else if (cn === "EventSource") {
        const u = resolveUrl(n.arguments[0], path.scope);
        push({ sink: "EventSource", kind: "sse", method: "GET", template: u.template, body_keys: null, dynamic: u.dynamic });
      }
    }),
    CallExpression: guard(function (path) {
      const n = path.node;
      const c = n.callee;
      // index this call-site by callee name (for inter-procedural body-param resolution later)
      { const cn0 = c && (c.type === "Identifier" ? c.name : (c.type === "MemberExpression" && c.property ? (c.property.name || c.property.value) : null));
        if (cn0 && cn0.length >= 3) { let arr = callsByName.get(cn0); if (!arr) callsByName.set(cn0, arr = []); if (arr.length < 300) arr.push({ args: n.arguments, scope: path.scope }); } }
      // GENERATED-CLIENT runtime: request(config, {method, url, ...}), openapi-typescript-codegen /
      // swagger-typescript-api / orval. The wrapper name is minified (ue, __request, request…) so we can't
      // key on it; instead we detect the CALL by its options object carrying BOTH method AND url. This is the
      // dominant modern pattern (one target=309 eps, another=openapi) that a callee-name rule misses entirely.
      for (const arg of n.arguments) {
        if (arg && arg.type === "ObjectExpression" && hasKeys(arg, "method", "url")) {
          const oi = optsInfo(arg, path.scope);
          if (oi._url) {
            // NEVER default an unresolved method to GET. A wrong verb is worse than a missing one: it labels
            // `DELETE /organization/{id}` as a safe read, and anything downstream that trusts it will fire a
            // destructive request believing it is idempotent. Unknown must stay loud so it shows in the residual.
            // This call is, by construction, NOT fetch: we identified it by an options object carrying
            // both method and url, which is the generated-client signature. So the base is composed by a
            // wrapper and the document-origin rule must not be applied downstream. Say so on the record.
            const _bh = baseHint(arg, path.scope);
            // The app's own parameterised twin, if it ships one. Used for slot NAMES only, the URL that
            // actually gets sent is the one we reconstructed, and where the two disagree we keep ours and
            // record the disagreement rather than following a typo into a 404.
            const _dt = declaredTemplate(arg, path.scope, oi._url.template, oi._urlKey);
            const _tpl = (_dt && _dt.agrees) ? nameSlots(oi._url.template, _dt.template) : oi._url.template;
            const _rec = { sink: "codegen.request", kind: "xhr", method: oi.method || "UNKNOWN", template: _tpl,
                   template_declared: _dt ? _dt.template : null,
                   template_declared_key: _dt ? _dt.key : null,
                   template_conflict: !!(_dt && !_dt.agrees),
                   body_keys: oi.body_keys, body_encoding: oi.body_encoding, content_type: oi.content_type, body_schema: oi.body_schema, query_schema: oi.query_schema, req_headers: oi.headers, credentials: oi.credentials,
                   headers: oi.headers, dynamic: oi._url.dynamic,
                   base_unresolved: !/^https?:\/\//.test(oi._url.template || ""),
                   base_key: _bh ? _bh.key : null, base_hint: _bh ? _bh.hint : null,
                   _via: (c && (c.type === "Identifier" ? c.name
                        : (c.type === "MemberExpression" && c.object && c.object.type === "Identifier" ? c.object.name : null))) };
            // THE BODY IS OFTEN THE ENCLOSING FUNCTION'S PARAMETER, AND THAT IS RESOLVABLE.
            //
            //     complianceCheck: { createInquiry: async data => sendRequest({..., url: "/x", data }) }
            //
            // `data` is a parameter, so the shape lives at this method's CALL SITES, one hop away. The
            // machinery for that hop already existed, tagOptsBodyParam marks the record, and a later pass
            // joins it against callsByName, but it was only ever invoked from the duck-typed optsSink
            // fallback, never from here. Generated API clients are the dominant modern shape and they all
            // come through this branch, so on a target built that way NOTHING was ever tagged: measured on
            // one bundle, [bp] tagged=0 with 1,694 indexed call sites sitting right there. Six corpora
            // recovered zero nested request bodies for this one reason.
            if (!_rec.body_keys) tagOptsBodyParam(_rec, n, path);
            push(_rec);
            return;
          }
        }
      }
      // fetch(url, opts), incl. one-hop alias `const n = fetch; n(url)` (decidable; arbitrary aliasing is not)
      if (c.type === "Identifier" && (c.name === "fetch" || bindsTo(c, path.scope, "fetch"))) {
        let u = resolveUrl(n.arguments[0], path.scope);
        // `const u = new URL("/api/search", origin); u.searchParams.set(...); fetch(u)` is an ordinary modern
        // idiom, and resolveUrl can only see a variable there, so the record fell through to a bare path
        // literal with UNKNOWN method: the path was found but never attributed to a request. The URL-object
        // resolver already existed for the `F(new URL(...).href)` shape; the plain fetch sink never consulted it.
        if (!LOOKS_PATH(u.template)) {
          const un = newUrlNode(n.arguments[0], path.scope);
          if (un && un.arguments && un.arguments[0]) {
            const u2 = resolveNewUrl(un, path.scope);
            // `new URL("/api/search", location.origin)` renders as `{location.origin}/api/search`, which starts
            // with a slot and so fails a leading-slash test. Check the slot-stripped skeleton too, exactly as the
            // other sinks do for a runtime base prefix.
            const sk2 = u2.template.replace(/\{[^}]*\}/g, "");
            if (LOOKS_PATH(u2.template) || LOOKS_PATH(sk2) || /^https?:/.test(u2.template)) u = u2;
          }
        }
        const oi = optsInfo(n.arguments[1], path.scope);
        push({ sink: c.name === "fetch" ? "fetch" : `fetch(alias:${c.name})`, kind: "fetch", method: oi.method || "GET", template: u.template, body_keys: oi.body_keys, body_encoding: oi.body_encoding, content_type: oi.content_type, body_schema: oi.body_schema, query_schema: oi.query_schema, req_headers: oi.headers, credentials: oi.credentials, body_src: oi.body_src, headers: oi.headers, response_keys: oi.response_keys, dynamic: u.dynamic });
        return;
      }
      // Nuxt / ofetch composables: $fetch(url,opts) · ofetch(url,opts) · useFetch(url,opts) · useLazyFetch(url,opts).
      // url=arg0, GET default. Catches the BARE forms (no opts obj) the duck-typed sink misses. (useAsyncData's URL
      // lives in its handler's inner $fetch → caught when traversal visits that call.) Gated to path-like arg0.
      if (c.type === "Identifier" && /^(\$fetch|ofetch|\$ofetch|useFetch|useLazyFetch|\$api)$/.test(c.name)) {
        const u = resolveUrl(n.arguments[0], path.scope);
        if (LOOKS_PATH(u.template)) {
          const oi = optsInfo(n.arguments[1], path.scope);
          push({ sink: c.name, kind: "fetch", method: oi.method || "GET", template: u.template,
                 body_keys: oi.body_keys, body_encoding: oi.body_encoding, content_type: oi.content_type, body_schema: oi.body_schema, query_schema: oi.query_schema, req_headers: oi.headers, credentials: oi.credentials, body_src: oi.body_src, headers: oi.headers,
                 response_keys: oi.response_keys, dynamic: u.dynamic });
          return;
        }
      }
      const _nm = calleeName(c);
      const nm = _nm && _nm.prop && VERB_ALIASES[_nm.prop] ? { ..._nm, prop: VERB_ALIASES[_nm.prop] } : _nm;
      // XMLHttpRequest .open(method, url)
      if (nm.prop === "open" && n.arguments.length >= 2 && n.arguments[0].type === "StringLiteral") {
        const m = n.arguments[0].value.toUpperCase();
        if (["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"].includes(m)) {
          const u = resolveUrl(n.arguments[1], path.scope);
          const recv = c && c.object && c.object.type === "Identifier" ? c.object : null;
          push({ sink: "XHR.open", kind: "xhr", method: m, template: u.template,
                 body_keys: recv ? xhrSendBody(recv, path.scope) : null,
                 req_headers: recv ? setRequestHeaders(recv, path.scope) : null, dynamic: u.dynamic });
          return;
        }
      }
      // navigator.sendBeacon(url, body)
      if (nm.prop === "sendBeacon") {
        const u = resolveUrl(n.arguments[0], path.scope);
        push({ sink: "sendBeacon", kind: "beacon", method: "POST", template: u.template, body_keys: bodyKeys(n.arguments[1], path.scope), dynamic: u.dynamic });
        return;
      }
      // axios(cfg) single-object-config
      if (nm.fn === "axios" && n.arguments[0] && n.arguments[0].type === "ObjectExpression") {
        const oi = optsInfo(n.arguments[0], path.scope);
        if (oi._url) push({ sink: "axios", kind: "xhr", method: oi.method || "GET", template: oi._url.template, body_keys: oi.body_keys, body_encoding: oi.body_encoding, content_type: oi.content_type, body_schema: oi.body_schema, query_schema: oi.query_schema, req_headers: oi.headers, credentials: oi.credentials, dynamic: oi._url.dynamic });
        return;
      }
      // GENERIC URL-object sink: F(new URL(path, base).href, [body]) for ANY callee F, bare-function axios instances
      // (one per service: sv/av/lv/…) + any custom client the callee-name rules miss. Rooted on the URL, not the name.
      if (c.type === "Identifier" && c.name !== "URL") {
        const un = newUrlNode(n.arguments[0], path.scope);
        if (un && un.arguments && un.arguments[0]) {
          const u = resolveNewUrl(un, path.scope);
          const sk = u.template.replace(/\{[^}]*\}/g, "");
          if (LOOKS_PATH(u.template) || LOOKS_PATH(sk) || /^https?:/.test(u.template) || u.key) {
            const arg1 = n.arguments[1];
            const mb = methodBodyFromArgs(n.arguments.slice(1), path.scope);
            const bodyk = mb.body_keys || (arg1 ? bodyKeys(arg1, path.scope) : null);
            const method = mb.method || (arg1 ? "POST" : "GET");
            const opaque = !bodyk || bodyk.every((k) => /^(\{|input:)/.test(String(k)));
            const rec = { sink: `url-call:${c.name}`, kind: "xhr", method, template: u.template, body_keys: bodyk, dynamic: u.dynamic, registry_key: u.key };
            if (opaque && arg1 && arg1.type === "Identifier") tagBodyParam(rec, arg1, path);   // body is a param → resolve from call-sites
            push(rec);
            return;
          }
        }
      }
      // <client>.get|post|put|patch|delete|request(url[, body|opts]) , axios/ky/got/$.ajax/custom http clients
      if (nm.prop && HTTP_METHODS.has(nm.prop.toLowerCase())) {
        const arg0 = n.arguments[0];
        if (!arg0) return;
        // `new Map().get("user/profile")` is not a request, however path-shaped the argument is.
        if (receiverIsNotHttpClient(nm.objNode, path.scope)) return;
        // AngularJS `$templateCache.put("template/tooltip/tooltip-popup.html", "<div>…</div>")` is a .put()
        // call whose first argument is a path, and it is not a request. One banking-client host contributed
        // ~200 phantom PUT "endpoints" this way. The tell is the SECOND argument: a request body is data,
        // a template cache entry is a markup blob.
        if (nm.prop.toLowerCase() === "put" && n.arguments.length === 2) {
          const a1 = n.arguments[1];
          const lit = a1 && a1.type === "StringLiteral" ? a1.value
                    : a1 && a1.type === "TemplateLiteral" && a1.quasis.length
                      ? (a1.quasis[0].value.cooked || "") : null;
          if (lit !== null && /^\s*<(\w|!)/.test(lit)) return;
        }
        // Angular HttpClient.request(method, url, opts), method as arg0 STRING, url as arg1 (reverse of the
        // positional-method fetcher below). Distinct enough (arg0 is an HTTP-verb literal) to handle here.
        if (nm.prop.toLowerCase() === "request" && arg0.type === "StringLiteral" &&
            ["GET", "POST", "PUT", "PATCH", "DELETE"].includes(arg0.value.toUpperCase()) && n.arguments[1]) {
          const u = resolveUrl(n.arguments[1], path.scope);
          if (LOOKS_PATH(u.template) || LOOKS_PATH(u.template.replace(/\{[^}]*\}/g, ""))) {
            const oi = optsInfo(n.arguments[2], path.scope);
            const ub = applyBase(nm, u, n.callee, path.scope);
            push({ sink: `${nm.obj || "?"}.request`, kind: "xhr", method: arg0.value.toUpperCase(), template: ub.template, body_keys: oi.body_keys, body_encoding: oi.body_encoding, content_type: oi.content_type, body_schema: oi.body_schema, query_schema: oi.query_schema, req_headers: oi.headers, credentials: oi.credentials, dynamic: ub.dynamic });
            return;
          }
        }
        // .request({url,method}) form
        if (arg0.type === "ObjectExpression") {
          const oi = optsInfo(arg0, path.scope);
          if (oi._url) {
            const ub = applyBase(nm, oi._url, n.callee, path.scope);
            const rec0 = { sink: `${nm.obj || "?"}.${nm.prop}`, kind: "xhr", method: oi.method || nm.prop.toUpperCase(), template: ub.template, body_keys: oi.body_keys, body_encoding: oi.body_encoding, content_type: oi.content_type, body_schema: oi.body_schema, query_schema: oi.query_schema, req_headers: oi.headers, credentials: oi.credentials, dynamic: ub.dynamic };
            if (!oi.body_keys) tagOptsBodyParam(rec0, n, path);
            // The content-type may be in an argument we did not treat as the options object, see
            // _ctInArgs. Fills only what is missing; a resolved construction still wins.
            if (!rec0.content_type) {
              const ct = _ctInArgs(n.arguments);
              if (ct) rec0.content_type = ct;
            }
            push(rec0);
          }
          return;
        }
        const u = resolveUrl(arg0, path.scope);
        // require a path-ish first arg so we don't grab array.map/get(0) etc., but check the EXPR-STRIPPED
        // skeleton too, so a runtime base prefix (Angular's this.http.get(this.host + '/rest/x') → '{EXPR}/rest/x')
        // is kept, not dropped for failing to start with '/'.
        const sk = u.template.replace(/\{[^}]*\}/g, "");
        const rk = findRegistryKey(arg0);   // `paths.X` buried in a template-literal url → bridge via the registry
        if (!LOOKS_PATH(u.template) && !LOOKS_PATH(sk) && !/^https?:/.test(u.template) && !rk) return;
        const method = nm.prop.toLowerCase() === "request" ? "GET" : nm.prop.toUpperCase();
        // arg1 is the BODY only for body-bearing verbs (axios/Angular .post(url,body)). For GET/DELETE/HEAD arg1 is
        // an options bag ({params,headers}), reading it as body produced spurious 'params'/'headers' body keys.
        // This is the commonest client shape there is, and it was giving up almost all of its contract: it
        // called bodyKeys() for a flat list of names, never bodySchema(), and ignored the config argument
        // entirely, which is where query params and headers live. Corpus-wide that left body coverage at
        // 3% and query at 8% while the information sat in the very arguments we were already looking at.
        //   axios convention:  post/put/patch(url, DATA, CONFIG)   get/delete/head(url, CONFIG)
        const bodyBearing = /^(POST|PUT|PATCH)$/.test(method);
        // `.delete(url, payload)`. In axios proper arg1 IS config, but plenty of custom clients and wrappers keep
        // the (url, body) shape for DELETE, and reading their payload as config drops it without a trace. Decide by
        // CONTENT rather than by the verb: a config object is made of config keys, anything else is data.
        let delArg = null;
        if (!bodyBearing && method === "DELETE" && n.arguments[1] && n.arguments[1].type === "ObjectExpression" &&
            n.arguments[1].properties.length && !looksLikeConfigBag(n.arguments[1]))
          delArg = n.arguments[1];
        // …and the mirror of the DELETE test: `X.post(url, {method:"POST", signal})` is a CONFIG bag that some
        // clients accept in the data position, and reading it as data reported a body of ["method","signal"],
        // keys no request ever carries. Requiring EVERY key to be a config key keeps real payloads intact.
        const a1 = n.arguments[1];
        const a1IsCfg = bodyBearing && looksLikeConfigBag(a1);
        const a1Opts = bodyBearing && !a1IsCfg ? sdkOptionsBag(a1) : null;
        const dataArg = bodyBearing ? (a1IsCfg ? null : (a1Opts ? a1Opts.body : a1)) : delArg;
        // …and once identified as config, USE it as config rather than discarding it. one target ships
        //   fetchWrapper.post(`${base}/api/v1/refresh`, {data:{}, withCredentials:true, headers:{'X-Client-Type':'WEB'}})
        // where `data:{}` is an empty body but the headers and credential mode are real contract, and dropping the
        // whole argument threw them away along with the fabricated keys.
        const cfgArg = (a1IsCfg || a1Opts) ? a1 : ((bodyBearing || delArg) ? n.arguments[2] : n.arguments[1]);
        const bodyNode = dataArg ? bodyKeys(dataArg, path.scope) : null;
        const bodySch = dataArg ? bodySchema(dataArg, path.scope) : null;
        const cfg = cfgArg ? optsInfo(cfgArg, path.scope) : null;
        let ub = applyBase(nm, u, n.callee, path.scope);   // prefix instance baseURL if X came from axios.create({baseURL})
        // RESOLVE THE BASE THROUGH THE CLIENT'S OWN METHOD. `client.post("account/deactivate", body)` gives
        // the bare string `account/deactivate`, which is not a path anyone can request, the base lives in
        // the method that was called:
        //
        //     post(path, data, params, headers, suffix = ".json") {
        //       return this.dispatch({path: `/1.1/${path}${suffix}`, method: "POST", ...});
        //     }
        //
        // so the request is POST /1.1/account/deactivate.json. The receiver cannot be resolved, it is a
        // destructured factory argument, but the METHOD can, and only when exactly one method of that name
        // in this file builds a URL. Two would mean we do not know which, and guessing asserts an address.
        if (!/^(https?:)?\/\//.test(ub.template) && ub.template[0] !== "/") {
          // The parameter has to sit in a PATH POSITION in the method's template, at the start, or
          // straight after a "/". `/1.1/${path}${suffix}` qualifies; `/bin/twitter/api-proxy.json${x}`
          // does not, and substituting into it produced `/bin/twitter/api-proxy.jsony` and
          // `/bin/twitter/api-proxy.jsonnormal` from arguments that were never path segments. A string is
          // not a path just because it is a string; it is a path because of where it goes.
          const cands = (WRAPPERS[nm.prop] || []).filter(
            (w) => w.isMethod && /(^|\/)\x01/.test(w.tmpl || ""));
          if (cands.length === 1) {
            const r2 = renderWrapper(cands[0], n.arguments, path.scope);
            // Only adopt it if the result is actually a path. A Map's .get() is not an HTTP client.
            if (r2.template && r2.template[0] === "/" && LOOKS_PATH(r2.template))
              ub = { template: r2.template, dynamic: ub.dynamic || r2.dynamic };
          }
        }
        // Not every `.put()` is an HTTP put. Ask what the receiver is before asserting an endpoint.
        {
          const svc = angularServiceOf(n.callee && n.callee.object, path.scope);
          if (svc && _NG_NOT_HTTP.has(svc)) return;
        }
        const rec = { sink: `${nm.obj || "?"}.${nm.prop}`, kind: "xhr", method, template: ub.template,
               // `bodyNode || null` turned the empty ARRAY that means "resolved: no fields" into the null that
               // means "not resolved", erasing the distinction at the moment it was established.
               body_empty: !!(dataArg && Array.isArray(bodyNode) && bodyNode.length === 0),
               body_keys: (bodyNode && bodyNode.length) ? bodyNode : null,
               body_schema: bodySch && bodySch !== "{EXPR}" ? bodySch : (dataArg ? "{EXPR}" : null),
               query_schema: cfg ? cfg.query_schema : null,
               req_headers: cfg ? cfg.headers : null,
               credentials: cfg ? cfg.credentials : null,
               dynamic: ub.dynamic, registry_key: rk,
               // A custom client puts headers wherever its signature says. x's takes them FOURTH, inside a
               // spread, `e.post(url, t, {}, {...r, "content-type": "application/json"})`, so reading a
               // designated options position finds nothing and the row says body[?] while the answer sits
               // in the call. See _ctInArgs.
               content_type: (cfg && cfg.content_type) || _ctInArgs(n.arguments) };
        // The body is very often the wrapper's own parameter, handed straight through:
        //   addNewNodeToNetwork: async e => n.post(`/validators/add`, e)
        // The shape isn't lost, it's one hop up at the caller. The fetch sink already followed that hop; this
        // sink, the commonest client shape there is, gave up and wrote {EXPR}.
        if (dataArg && dataArg.type === "Identifier" && !(bodyNode && bodyNode.length)) {
          tagBodyParam(rec, dataArg, path);
          const mb = _mutationCallBody(path, dataArg.name);
          if (mb) { rec.body_keys = mb; rec.body_from = "mutate-call"; }
        }
        if (!(bodyNode && bodyNode.length)) _dbgBody(rec, dataArg, n);
        push(rec);
        return;
      }
      // tRPC proxy-chain call
      const tp = trpcCall(n, path.scope, isTrpc);
      if (tp) { push(tp); return; }
      // POSITIONAL-METHOD custom fetcher: `De(url, "POST", opts)`, method is arg1 as a string (not in an options
      // object). Very common custom-fetcher signature that the {method:} rules miss. arg0 path-like = the gate.
      if ((c.type === "Identifier" || c.type === "MemberExpression") && n.arguments.length >= 2 &&
          n.arguments[1].type === "StringLiteral" &&
          ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"].includes(n.arguments[1].value.toUpperCase())) {
        const u = resolveUrl(n.arguments[0], path.scope);
        const sk = u.template.replace(/\{[^}]*\}/g, "");
        if (LOOKS_PATH(u.template) || LOOKS_PATH(sk) || /^https?:\/\//.test(u.template)) {
          let body = null, bsrc = null, hdrs = null, rk = null;
          const wantsBody = !["GET", "HEAD", "OPTIONS"].includes(n.arguments[1].value.toUpperCase());
          for (let k = 2; k < n.arguments.length; k++) {
            const a2 = n.arguments[k];
            if (!a2) continue;
            // TanStack Query options are cache behaviour, not payload. Skipped before anything reads keys
            // off them, this argument describes what to do with the RESULT.
            if (looksLikeQueryOptions(a2)) continue;
            if (_QUERY_OPT_CALLEES.has(_calleeKey(c)) && allKeysAreQueryOptions(a2)) continue;
            if (a2.type === "ObjectExpression") {                       // options wrapper: {body:…, headers:…}
              const oi = optsInfo(a2, path.scope);
              if (oi.body_keys || oi.response_keys) { body = oi.body_keys; bsrc = oi.body_src; hdrs = oi.headers; rk = oi.response_keys; break; }
            }
            // …but the arg is often the PAYLOAD ITSELF, not a wrapper: `post(url, "POST", {phone, code})`.
            // A raw payload object carries no NET_SIGNAL key (method/body/json), so optsInfo returns nothing
            // and the body was silently dropped, 295 of claude.ai's 390 missing bodies were exactly this.
            // bodyKeys already understands objects, identifiers, member exprs, JSON.stringify and spreads.
            if (wantsBody) {
              let bk = bodyKeys(a2, path.scope);
              // A wrapper known to take options here can still yield real fields, claude's
              // `useApiMutation(url, "POST", {...e, onError, transformVariables: e => ({...e, source})})`
              // contributes `source` to the variables through the transform. Keep what the transform put
              // there and drop the names that are only ever cache behaviour, instead of taking the object
              // whole or discarding it whole.
              if (bk && _QUERY_OPT_CALLEES.has(_calleeKey(c)))
                bk = bk.filter((k) => !RQ_OPTION_KEYS.has(String(k)));
              if (bk && bk.length) { body = bk; bsrc = bodyFields(a2, path.scope); break; }
            }
          }
          if (!body && wantsBody) _dbgSite("nobody-pos", n);
          push({ sink: "posmethod", kind: "xhr", method: n.arguments[1].value.toUpperCase(), template: u.template, body_keys: body, body_src: bsrc, headers: hdrs, response_keys: rk, dynamic: u.dynamic });
          return;
        }
      }
      // A WRAPPER ANOTHER MODULE EXPORTS, called directly: render it here as it would be rendered there.
      if (!(c.type === "Identifier" && (WRAPPERS[c.name] || []).length)) {
        const ew = _exportedWrapper(n.callee, path.scope);
        if (ew) {
          const rendered = { template: _composeWrapper(ew, n.arguments, path.scope, null), dynamic: false };
          if (rendered.template.includes("{EXPR}")) rendered.dynamic = true;
          const mb = methodBodyFromArgs(n.arguments, path.scope);
          let bk = mb.body_keys || ew.body_keys || null, bs = null;
          if (ew.body_idx != null && n.arguments[ew.body_idx]) {
            bk = bodyKeys(n.arguments[ew.body_idx], path.scope) || bk;
            const sch = bodySchema(n.arguments[ew.body_idx], path.scope);
            if (sch && sch !== "{EXPR}") bs = sch;
          }
          const nm0 = c.type === "Identifier" ? c.name : (calleeName(c).prop || "?");
          push({ sink: `wrap:${nm0}`, kind: "xhr", method: mb.method || ew.method || "GET", template: rendered.template,
                 body_keys: bk, body_schema: bs, dynamic: rendered.dynamic, via_fetch: !!ew.viaFetch,
                 content_type: mb.content_type, body_encoding: mb.body_encoding });
          return;
        }
      }
      // INTER-PROCEDURAL: callee is a custom fetch wrapper we resolved (pass 1) → substitute args into its URL template.
      // Only FUNCTION wrappers here. Class and object methods are registered under the same map by NAME,
      // and this branch ends in `return` when it cannot match a binding, so once `get`, `post`, `send` and
      // every other method name were in it, ordinary calls to a same-named function entered here, failed to
      // bind, and returned, skipping every other sink below. That cost 6,856 of x's 11,559 records.
      if (c.type === "Identifier" && (WRAPPERS[c.name] || []).some((w) => !w.isMethod)) {
        // Match the function this identifier BINDS to here, not merely its spelling. With one candidate
        // and no resolvable binding we still use it, that is the common single-definition case and losing
        // it would cost far more than it protects. With several, a wrong pick asserts a concrete URL the
        // code never requests, so we take only an exact match.
        const cands = WRAPPERS[c.name].filter((w) => !w.isMethod);
        const bound = _fnOfBinding(c.name, path.scope);
        // Matching by NAME alone is the weaker half of this rule, and reading the path out of a dispatch
        // options object made more functions qualify as wrappers, so it started firing on calls it should
        // not: `/bin/twitter/api-proxy.jsony`, `.jsonnormal`, `.jsonthrow`, a wrapper whose parameter is a
        // WHOLE PATH (`/bin/twitter/api-proxy.json${p}`) matched by name to calls passing something else
        // entirely. When the binding is resolved the function is the right one whatever its template looks
        // like; when it is not, only trust a wrapper whose parameter sits in a path position.
        const w = bound ? cands.find((x) => x.fnNode === bound)
                        : (cands.length === 1 && /(^|\/)\x01/.test(cands[0].tmpl || "")
                           ? cands[0] : null);
        if (!w) return;
        const rendered = renderWrapper(w, n.arguments, path.scope);
        const mb = methodBodyFromArgs(n.arguments, path.scope);   // call-site opts (e.g. or(...,{method,body}))
        let bk = mb.body_keys || w.body_keys, bs = null;
        // the wrapper told us WHICH PARAMETER is the body; this call site supplies it
        if (w.body_idx != null && n.arguments[w.body_idx]) {
          const bn = n.arguments[w.body_idx];
          bk = bodyKeys(bn, path.scope) || bk;
          const sch = bodySchema(bn, path.scope);
          if (sch && sch !== "{EXPR}") bs = sch;
        }
        push({ sink: `wrap:${c.name}`, kind: "xhr", method: mb.method || w.method || "GET",
               template: rendered.template, body_keys: bk, body_schema: bs, dynamic: rendered.dynamic,
               via_fetch: !!w.viaFetch });
        return;
      }
      // FALLBACK: no named sink matched, duck-type by the options object (custom wrappers / react-query hooks).
      const ds = optsSink(n, path.scope);
      if (ds) {
        // `(0,A.post)("/platform/auth/{ref}/otp", {body})`: the callee is a client's method, so the
        // client's base goes in front of the path, the same as for `client.post(...)`.
        const cb = callClient(n.callee, path.scope);
        if (cb && ds.template && ds.template !== "{EXPR}") {
          const ub = _withBase(cb, { template: ds.template, dynamic: ds.dynamic });
          ds.template = ub.template; ds.dynamic = ub.dynamic;
        }
        if (!ds.body_keys) tagOptsBodyParam(ds, n, path); push(ds); return;
      }
    }),
  });

  // INTER-PROCEDURAL body resolution: a wrapper `X=(data)=>send(url,data)` has the body as a PARAM, resolve it from
  // X's call-sites (`X({...})`). Follow the variable one hop to the caller instead of giving up at "it's a variable".
  // axios/fetch CONFIG keys, not body fields; filter so a passed-through config object doesn't leak as a body.
  const CONFIG_KEYS = new Set(["withcredentials","baseurl","headers","header","method","params","query","signal",
    "timeout","canceltoken","responsetype","transformrequest","transformresponse","adapter","validatestatus",
    "maxredirects","xsrfcookiename","xsrfheadername","onuploadprogress","ondownloadprogress","credentials","mode",
    "cache","redirect","referrer","integrity","keepalive","body","json","data"]);
  const realKeys = (b) => Array.isArray(b) && b.some((k) => !/^(\{|input:)/.test(String(k)));
  let _bpTag = 0, _bpHasCalls = 0, _bpResolved = 0;
  for (const rec of found) {
    if (rec._bpFn) _bpTag++;
    if (realKeys(rec.body_keys) || !rec._bpFn) continue;
    let calls = callsByName.get(rec._bpFn);
    if (!calls) {
      // THE CALLER IS IN ANOTHER CHUNK, which is the normal case rather than the exception: the API
      // client is one module and the components calling it are dozens of others. Measured on one real
      // bundle file: 70 endpoints tagged as taking a body parameter, call sites found in-file for 27.
      //
      // _CALLARGS is the corpus-wide index of literal argument shapes, keyed name|position, built by
      // the pass that already reads every file once. It holds shapes rather than AST nodes, so it can
      // answer body_shape directly even though it cannot feed the body_keys path below.
      const viaIndex = _CALLARGS.get(rec._bpFn + "|" + rec._bpIdx);
      if (viaIndex && Object.keys(viaIndex).length && Object.keys(viaIndex).length <= 12) {
        const clean = {};
        for (const k of Object.keys(viaIndex)) if (!CONFIG_KEYS.has(k.toLowerCase())) clean[k] = viaIndex[k];
        if (Object.keys(clean).length) {
          rec.body_shape = clean;
          rec.body_keys = Object.keys(clean).slice(0, 1000);
          _bpResolved++;
        }
      }
      continue;
    }
    _bpHasCalls++;
    const keys = new Set();
    for (const call of calls) {
      const a = call.args[rec._bpIdx];
      if (!a || a.type !== "ObjectExpression") continue;              // only trust a LITERAL body at the call-site (kills collisions/pass-through config)
      try { const bk = bodyKeys(a, call.scope); if (bk) for (const k of bk) { const kk = String(k);
        if (!/^(\{|input:)/.test(kk) && !CONFIG_KEYS.has(kk.toLowerCase())) keys.add(kk); } } catch {}
    }
    if (keys.size && keys.size <= 12) {
      rec.body_keys = [...keys].slice(0, 1000); _bpResolved++;          // >12 = collision grab-bag
      // AND THE SHAPE, not just the names. This pass wrote body_keys only, and body_keys is not what
      // anything downstream reads: ingest fills `req_body` from body_shape, the store's map prints
      // body_shape, and the bench measures it. So an endpoint whose body WAS recovered here still
      // arrived with no body, the work was done and filed in a drawer nobody opens.
      //
      // Shapes are unioned across call sites for the same reason the keys are: one endpoint routinely
      // takes several partial bodies. /user/me was observed sending {phone} while the first caller in
      // the code passed {name}, and both are real fields of that endpoint.
      const shape = {};
      for (const call of calls) {
        const a = call.args[rec._bpIdx];
        if (!a || a.type !== "ObjectExpression") continue;
        try {
          const sh = bodySchema(a, call.scope, 0);
          if (sh && typeof sh === "object" && !Array.isArray(sh)) {
            for (const k of Object.keys(sh)) {
              if (CONFIG_KEYS.has(k.toLowerCase())) continue;
              if (!(k in shape) || shape[k] === "{EXPR}" || shape[k] === "?") shape[k] = sh[k];
            }
          }
        } catch {}
      }
      if (Object.keys(shape).length && Object.keys(shape).length <= 12) rec.body_shape = shape;
    }
  }
  if (process.env.BP_DEBUG) console.error(`[bp] tagged=${_bpTag} hadCallSites=${_bpHasCalls} resolved=${_bpResolved} callsByName=${callsByName.size} CALLARGS=${_CALLARGS.size}`);
  // PUBLISH call-site argument shapes so a wrapper defined in ANOTHER chunk can be resolved. one target ships
  //     export const initiateTransfer = async (request: TransferRequest) =>
  //         httpClient.post('/v1/transfers/initiate', request)
  // where the parameter's shape is a TypeScript type, erased at compile time, so unrecoverable from the
  // wrapper itself, while a caller two modules away builds the literal and passes it. Same join as the
  // serializers: the definition and the evidence live in different files, and only the host-level pass sees both.
  // Names shorter than four characters are minified locals and would collide, so they are left alone.
  // Deliberately CHEAP. The first version ran full bodyKeys, binding walks, call-site recursion, FormData
  // scans, once per named call in the chunk, and on a 3 MB bundle with thousands of names that pushed
  // two financial dashboards past a 1260-second budget into a reported zero. A published call-site shape is
  // a hint for another chunk to use, not a place to spend the analysis budget: read a literal, or one hop to a
  // literal, and otherwise move on.
  const _cheapKeys = (node, scope) => {
    if (!node) return null;
    let o = node;
    if (o.type === "Identifier") {
      const b = scope && scope.getBinding && scope.getBinding(o.name);
      o = b && b.path && b.path.node && b.path.node.init;
    }
    if (!o || o.type !== "ObjectExpression" || !o.properties.length) return null;
    const ks = [];
    for (const pr of o.properties)
      if ((pr.type === "ObjectProperty" || pr.type === "ObjectMethod") && pr.key) ks.push(String(pr.key.name ?? pr.key.value));
    return ks.length ? ks : null;
  };
  let _published = 0;
  for (const [nm, calls] of callsByName) {
    if (_published >= 20000) break;
    if (nm.length < 4) continue;
    // BY ARGUMENT INDEX. The wrapper records WHICH parameter is the body (`_bpIdx`); "the first object-ish
    // argument" answers a different question, and for `f(payload, opts)` called as `f(x, {retries:3})` it would
    // confidently return the retry config as the request body.
    const byIdx = new Map(), flat = new Set();
    for (const call of calls.slice(0, 4)) {
      for (let i = 0; i < call.args.length && i < 6; i++) {
        const k = _cheapKeys(call.args[i], call.scope);
        if (!k) continue;
        let set = byIdx.get(i);
        if (!set) byIdx.set(i, set = new Set());
        for (const x of k) { if (set.size < 24) set.add(x); flat.add(x); }
      }
      if (flat.size > 60) break;
    }
    if (!byIdx.size) continue;
    const args = {};
    for (const [i, set] of byIdx) if (set.size <= 24) args[i] = [...set];
    if (Object.keys(args).length) {
      found.push({ _kind: "callargs", _evidence: true, name: nm, fields: [...flat].slice(0, 1000), args });
      _published++;
    }
  }
  // `_bpFn` survives to the host-level pass now; the in-chunk resolution above already had its chance.
  for (const f of found) if (!f._bpFn) delete f._bpIdx;

  // attach RESPONSE schemas: group records by enclosing function; a function with exactly ONE sink gets its
  // response fields attached to that sink (unambiguous). Multi-sink functions are skipped (can't tell which call
  // the .json() belongs to), high-precision over recall.
  const byFn = new Map();
  for (const f of found) if (f._fn) { const a = byFn.get(f._fn) || []; a.push(f); byFn.set(f._fn, a); }
  for (const [fnNode, recs] of byFn) {
    const rs = RESP.get(fnNode);
    if (!rs || !rs.fields.size) continue;
    const fields = [...rs.fields].slice(0, 1000);
    if (recs.length === 1) {
      recs[0].response_keys = [...new Set([...(recs[0].response_keys || []), ...fields])].slice(0, 1000);
    } else if (recs.length <= 4) {
      // A function with several sinks makes the .json() ambiguous, we cannot say WHICH call returned those
      // fields, so this was dropped entirely, which is why response coverage sat near zero. Dropping is not
      // the only honest option: the fields did come back from one of a small, named set of calls, and that
      // is worth recording as long as it is not asserted as fact. Marked ambiguous, with the count, so a
      // consumer can weigh it. Beyond four sinks the association is too weak to be worth anything.
      for (const r of recs) {
        r.response_keys_ambiguous = fields;
        r.response_ambiguity = recs.length;
      }
    }
  }
  for (const f of found) delete f._fn;

  // structured data-layer collectors (gated so they're near-free when absent)
  // `injectEndpoints` and `enhanceEndpoints` too: a file that only adds endpoints to an api defined in
  // another file holds none of the other three, and Grafana declares most of its endpoints that way.
  if (/fetchBaseQuery|reducerPath|createApi\(|injectEndpoints|enhanceEndpoints/.test(src)) for (const r of collectRtk(ast)) found.push(r);
  if (/useSWR|["']swr["']/.test(src)) for (const r of collectSwr(ast)) found.push(r);
  if (/method:\s*["'](?:get|post|put|patch|delete)/i.test(src) && /\b(url|path|endpoint|uri):/.test(src))
    for (const r of collectEndpointTables(ast)) found.push(r);
  if (/typeName:|MethodKind|@?connectrpc|createPromiseClient|createConnectTransport/.test(src))
    for (const r of collectConnect(ast)) found.push(r);

  // dedupe by (method, template), but when the template is still {EXPR} (host/path unresolved), a distinct
  // registry_key IS a distinct endpoint, so key on it too (else N registry-bridged endpoints collapse into one {EXPR}).
  const seen = new Map();
  for (const f of found) {
    // Records that are not endpoints have neither a method nor a template, so keying on those two alone
    // collapsed EVERY serializer in a chunk into a single "undefined undefined" entry, 4 published, 1 survived,
    // and the cross-chunk body join came back empty for a reason that had nothing to do with the join.
    const k = f._kind && !f.template
      ? `${f._kind} ${f.name || ""}`
      : `${f.method} ${f.template}` + (/\{EXPR\}/.test(f.template || "") && f.registry_key ? ` #${f.registry_key}` : "");
    const prev = seen.get(k);
    if (!prev) seen.set(k, f);
    else mergeRecord(prev, f);                                 // UNION body fields across call sites
  }
  const eps = [...seen.values()];
  // Relay compiled requests (gated on relay markers). BRACES matter, this used to leak collectApollo out of the if.
  if (/operationKind|concreteRequest/.test(src)) {
    for (const o of collectRelay(ast)) eps.push(_relayRecord(o.name, o.kind, o.text));
  }
  // Apollo/urql/codegen compiled DocumentNodes → full query text (client-agnostic: any OperationDefinition literal).
  if (/OperationDefinition/.test(src)) {
    for (const o of collectApollo(ast)) eps.push(_relayRecord(o.name, o.kind, o.text));
  }
  // Persisted queries: the id ships, the document does not. Gated on the field name, which is the shape's
  // own signature and appears in no other context.
  if (/queryId/.test(src))
    for (const r of collectPersistedQueries(ast)) eps.push(r);
  // What this app's HTTP layer does with a body. Gated on fetch being present at all.
  if (/\bfetch\s*\(/.test(src))
    for (const r of collectClientEncoding(ast)) eps.push(r);
  // graphql-request / any client passed a PLAIN-STRING query (not gql-compiled) → op name + full text.
  //
  // The gate must also admit a file that simply CONTAINS a named operation. It used to require the file to
  // mention `request(` somewhere, which contradicted the very case the global string sweep inside this
  // collector was written for: `fetch("/graphql", {body: JSON.stringify({query: "mutation X(…)"})})` names
  // no `request` at all, so the sweep that exists to catch it never ran. Measured across the corpus: 8
  // supabase files, 20 real operations, AddressCreate, AddressUpdate, AddressDelete, AddPhone,
  // UserPrivacySettingsSet, extracted by nothing.
  //
  // Admitting on the document's own text is not a loosening. A string that opens `mutation Name(` is a
  // GraphQL operation whatever transport carries it, and the sweep's admission test is stricter than the
  // gate ever was.
  if (/GraphQLClient|graphql-request|\.request\(|\brequest\(/.test(src) ||
      /["'`](?:\\[nrt]|\s)*(?:query|mutation|subscription)\s+[A-Za-z_]\w*\s*[({]/.test(src)) {
    for (const o of collectGraphqlString(ast)) eps.push(_relayRecord(o.op, o.kind, o.text));
  }
  // typed schemas + server actions (gated so they're near-free on chunks lacking the pattern)
  if (/\.object\(\{/.test(src) && /\.(string|number|boolean|enum|array|object|literal|union)\(/.test(src))
    for (const s of collectZodSchemas(ast)) eps.push({ _kind: "zod", name: s.name, zod_fields: s.fields });
  // The collector returns finished `_kind: "server-action"` records. This used to read
  // `for (const id of ...)` and stuff each RECORD into an `action_id` field of a new `_kind: "action"`
  // record, so what came out was {_kind:"action", action_id:{_kind:"server-action", op:…, action_id:…}}.
  // ingest.py reads `_kind === "server-action"`, which never matched, and every Next.js server action in
  // every run was dropped: 0 rows across seven targets. The call site three hundred lines below
  // (`for (const r of collectServerActions(ast)) kept.push(r)`) had it right the whole time.
  if (/erverReference/.test(src))
    for (const r of collectServerActions(ast)) eps.push(r);
  if (/\[\s*\[\s*["']\/|\/services\/|routeTable|proxyTable|gateway/.test(src))
    for (const g of collectGatewayRoutes(ast)) eps.push({ _kind: "gateway", path: g.path, service: g.service });
  // The gate must admit what the collector READS, not only the constructor names minification erases.
  // The name-agnostic pass looks at `uri`, `url` and `endpoint` keys; the gate listed only `uri:`, so a
  // minified `createClient({url})`, urql, graphql-ws, graphql-sse, matched nothing and the pass written
  // to survive minification was skipped on exactly the chunk it was written for.
  if (/HttpLink|ApolloClient|createUploadLink|createClient\(|GraphQLClient|\b(uri|url|endpoint)\s*:/.test(src))
    for (const g of collectGraphqlEndpoints(ast))
      // `via` is the provenance: "client-name" means a recognisable link constructor named it,
      // "uri-path" means the path itself is the evidence. A reader who trusts only the first can filter.
      eps.push({ _kind: g.sibling ? "gql-endpoint-sibling" : "gql-endpoint",
                 uri: g.uri, via: g.via || "client-name", dynamic: !!g.dynamic });
  {
    const clients = collectClients(ast);
    for (const f of eps) {
      if (!f._via) continue;
      const r = clients.resolve(f._via);
      if (!r) continue;
      const inherited = [...(r.cfg.headers || []),
                         ...(r.cfg.interceptors || []).map(i => i.name)];
      if (inherited.length) f.client_headers = [...new Set(inherited)];
      const conds = (r.cfg.interceptors || []).filter(i => i.condition)
                      .map(i => `${i.name}: ${i.condition}`);
      if (conds.length) f.header_conditions = conds;
      if (r.cfg.baseURL) f.base_url = r.cfg.baseURL;
      if (r.cfg.withCredentials !== null && r.cfg.withCredentials !== undefined)
        f.with_credentials = r.cfg.withCredentials;
      f.client = r.name;
    }
    for (const [nm, cfg] of clients.instances)
      eps.push({ _kind: "client", name: nm, base_url: cfg.baseURL, with_credentials: cfg.withCredentials,
                 headers: cfg.headers, interceptor_headers: (cfg.interceptors || []).map(i => i.name) });
  }
  for (const c of collectConfigUrls(ast))
    eps.push({ _kind: "config-url", name: c.name, value: c.value });
  for (const h of collectClientHeaders(ast))
    eps.push({ _kind: "client-header", name: h.name, where: h.where, condition: h.condition });
  if (/\.from\(|\/rest\/v1|supabase/.test(src))
    for (const e of collectSupabase(ast)) eps.push(e);
  if (/initializeApp|firestore\.googleapis|firebaseio\.com|firebaseapp\.com/.test(src))
    for (const e of collectFirebase(ast)) eps.push(e);
  // drop incomplete-base STUBS: a `{EXPR}/groups` record is subsumed once wrapper-resolution produced the full
  // `/api/orgs/{id}/groups`. Only drop records that START with a slot (unresolved base) when a resolved (leading-`/`)
  // record of the same method has a slot-normalized template ending with the stub's, keeps real short paths.
  const norm = (t) => t.replace(/\{[^}]*\}/g, "§");
  const real = eps.filter((e) => !e._kind);
  const resolved = real.filter((e) => e.template[0] === "/");
  const drop = new Set();
  for (const e of real) {
    if (e.template[0] !== "{") continue;
    const ne = norm(e.template);
    for (const b of resolved) {
      if (b !== e && b.method === e.method && norm(b.template).length > ne.length && norm(b.template).endsWith(ne)) { drop.add(e); break; }
    }
  }
  const kept = drop.size ? eps.filter((e) => !drop.has(e)) : eps;

  // Everything above only emits a path once it can be pinned to a recognised network sink. That is the right
  // bar for "this is an endpoint", but it is the wrong bar for recon: one merchant portal ships 102 distinct
  // path literals (/v1/payouts/bulk, /v1/integration/keys, /graphql) behind a transpiled class
  // client, and sink-attribution alone reported ZERO. A path nobody can attribute is still a path we know
  // exists, and silently dropping it is indistinguishable from the target not having it.
  // So harvest the rest, as separate, honestly-labelled record kinds, never mixed into the sink-attributed
  // endpoints. Precision is handled by labelling, not by discarding.
  // A sibling-less `{path:"…"}` is only a route if nothing better already explained it. 1win ships
  // `api.post({path:"/v1/onramp/quote", body:{…}})`, the sink collector reads that correctly as a POST
  // with a four-key body, and the loosened route rule was re-emitting the same path a second time as a
  // contract-free "route". That is worse than noise: it duplicates a record we understand with one we do
  // not, under a label that says the opposite of the truth.
  //
  // The path-literal collector already resolves exactly this conflict by deferring to whatever a sink
  // claimed. Same rule, same reason, so compute the claim set from the sink-attributed endpoints BEFORE
  // any route is added, and let bare routes stand only where no call site speaks for the path.
  const sinkClaimed = new Set();
  for (const e of kept) {
    if (!e.sink) continue;
    const t = e.template || e.path || "";
    if (t) sinkClaimed.add(t.replace(/\{[^}]*\}/g, "*"));
  }
  for (const r of collectRouteTables(ast, src)) {
    if (r.source === "router-config-bare" &&
        sinkClaimed.has(String(r.template || "").replace(/\{[^}]*\}/g, "*"))) continue;
    kept.push(r);
  }
  for (const r of collectSocketMessages(ast, src)) kept.push(r);
  for (const r of collectNavigation(ast)) kept.push(r);
  for (const r of collectJsx(ast)) kept.push(r);
  for (const r of collectServerActions(ast)) kept.push(r);
  const claimed = new Set();
  for (const e of kept) {
    const t = e.template || e.path || "";
    if (t) claimed.add(t.replace(/\{[^}]*\}/g, "*"));
  }
  for (const p of collectPathLiterals(ast, claimed, src)) kept.push(p);
  if (/cookie/i.test(src)) for (const c of collectCookies(ast)) kept.push(c);
  annotateQuery(kept);
  return sanitize(kept);
}

// ── COOKIES ─────────────────────────────────────────────────────────────────────────────────────────────
// A cookie is just a header, but it is the one header script CANNOT set through fetch, the browser attaches
// it, gated on `credentials`. So the contract needs two different facts: WHICH cookies exist (names), and
// whether a given request carries them at all. We had the second (credentials) and, for the first, only
// whatever a single response happened to Set-Cookie. Everything the app manages itself in JavaScript,
// document.cookie writes, js-cookie, and read helpers naming a cookie, was invisible.
//
// The read sites matter as much as the writes: `getCookie("session_id")` names a cookie the app expects the
// server to have set, which a single unauthenticated response will never show us.
const _COOKIE_FN = /^(get|set|remove|erase|delete|has|read|write)?[_-]?cookie$|^cookies?$/i;
const _COOKIE_NAME = /^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$/;

function collectCookies(ast) {
  const out = new Map();
  // Minification renames getCookie() to c(), so a name-based test finds almost nothing. Identify the
  // helpers by BEHAVIOUR instead, any function whose body touches document.cookie is a cookie helper,
  // whatever it ended up being called, and then read the string argument at its call sites. Same
  // principle as the header fix: anchor on what the code does, not on an identifier a minifier rewrites.
  const cookieFns = new Set();
  traverse(ast, {
    "FunctionDeclaration|FunctionExpression|ArrowFunctionExpression"(path) {
      try {
        let touches = false;
        path.traverse({
          MemberExpression(ip) {
            if (ip.node.property && ip.node.property.name === "cookie" &&
                /document/.test(generateSrc(ip.node.object) || "")) touches = true;
          },
        });
        if (!touches) return;
        const p = path.parentPath && path.parentPath.node;
        const nm = (path.node.id && path.node.id.name) ||
                   (p && p.type === "VariableDeclarator" && p.id && p.id.name) ||
                   (p && p.type === "ObjectProperty" && p.key && (p.key.name || p.key.value));
        if (nm) cookieFns.add(String(nm));
      } catch {}
    },
  });
  // A helper that touches document.cookie may also be a generic storage wrapper, and it gets called with
  // header names too, `content-type` and `Authorization` are not cookies. Anything that is a known HTTP
  // header name is rejected: a false cookie is worse than a missing one, because it misdescribes how the
  // app authenticates.
  const NOT_A_COOKIE = new Set(["content-type", "authorization", "accept", "accept-language", "user-agent",
    "cache-control", "content-length", "referer", "origin", "cookie", "set-cookie", "x-requested-with",
    "if-none-match", "etag", "pragma", "expires", "location", "host"]);
  // Two very different levels of evidence end up here, and conflating them would misreport how the app
  // authenticates. A `document.cookie = "sid=..."` write, or a call to something literally named
  // getCookie(), IS a cookie. A call to a minified helper that merely touches document.cookie somewhere in
  // its body is an inference, those helpers are often generic storage wrappers, which is how fromBase64
  // and select got in. Both are kept; only the first is asserted.
  const add = (name, mode, value, confidence) => {
    if (!name || !_COOKIE_NAME.test(String(name))) return;
    if (NOT_A_COOKIE.has(String(name).toLowerCase())) return;
    // `x-` / `sec-` prefixes are a header convention, not a cookie one, X-Request-URL and
    // x-statsig-region reached here through the same generic storage helper.
    if (/^(x|sec|http)-/i.test(String(name))) return;
    const k = String(name);
    const prev = out.get(k) || { _kind: "cookie", name: k, modes: [], confidence: confidence || "high" };
    if (confidence === "high") prev.confidence = "high";
    if (!prev.modes.includes(mode)) prev.modes.push(mode);
    if (value && !prev.value) prev.value = String(value).slice(0, 60);
    out.set(k, prev);
  };
  traverse(ast, {
    // document.cookie = "name=value; path=/"   (the app setting its own cookie)
    AssignmentExpression(path) {
      try {
        const l = path.node.left;
        if (!l || l.type !== "MemberExpression" || !l.property || l.property.name !== "cookie") return;
        if (!/document|doc\b/i.test(generateSrc(l.object) || "")) return;
        const r = strFromNode(path.node.right, path.scope, 0);
        const m = /^([A-Za-z0-9!#$%&'*+.^_`|~-]{1,64})=(.*)$/.exec((r && r.s) || "");
        if (m) add(m[1], "write", m[2].split(";")[0], "high");   // literal document.cookie assignment
      } catch {}
    },
    CallExpression(path) {
      try {
        const c = path.node.callee;
        const prop = c && (c.property && c.property.name);
        const name = c && (c.name || prop);
        if (!name) return;
        if (!_COOKIE_FN.test(String(name)) && !cookieFns.has(String(name))) return;
        // js-cookie style: Cookies.get("x") / Cookies.set("x", v), the object is what is named
        const objSrc = c.type === "MemberExpression" ? (generateSrc(c.object) || "") : "";
        const isCookieObj = /cookie/i.test(objSrc) || /cookie/i.test(String(name)) ||
                            cookieFns.has(String(c.name || "")) || cookieFns.has(String(prop || ""));
        if (!isCookieObj) return;
        const a0 = path.node.arguments[0];
        if (!a0) return;
        const s = strFromNode(a0, path.scope, 0);
        if (!s || !s.s || s.s === "{EXPR}") return;
        const mode = /set|write/i.test(String(name)) ? "write"
                   : /remove|erase|delete/i.test(String(name)) ? "delete" : "read";
        let val = null;
        if (mode === "write" && path.node.arguments[1]) {
          const v = strFromNode(path.node.arguments[1], path.scope, 0);
          if (v && v.s) val = v.s;
        }
        const named = /cookie/i.test(objSrc) || /cookie/i.test(String(name));
        add(s.s, mode, val, named ? "high" : "inferred");
      } catch {}
    },
  });
  return [...out.values()];
}

// ── OUTPUT INVARIANTS ───────────────────────────────────────────────────────────────────────────────────
// Roughly twenty collectors push records, and each one that skips a normalisation step ships a malformed
// record downstream. Patching them individually is a losing game, a structural audit across 224 hosts found
// empty templates, unbalanced slots containing raw minified code, `//` from base+path concatenation, records
// that are nothing but `{EXPR}`, methods like "EMAIL", and XML namespace URLs presented as endpoints.
// One choke point every record passes through is the only way these stay fixed.
const _REAL_VERBS = new Set(["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE",
                             "CONNECT", "WS", "UNKNOWN"]);
// XML/RDF namespace identifiers are URLs by syntax and documents by nothing: they are never requested.
const _NAMESPACE = /^https?:\/\/(www\.)?(w3\.org|xmlns\.|purl\.org|schema\.org|xml\.apache\.org|docbook\.org|inkscape\.org|sodipodi\.sourceforge\.net|creativecommons\.org\/ns)/i;

// A slot whose "name" is a fragment of minified code ({call:function(){if(null!=self}) is noise, but the
// PATH around it is a real endpoint. A regex replace cannot handle the nesting and left the template
// unbalanced, so the record was thrown away, losing genuine endpoints to fix cosmetic ones, which is the
// wrong trade. Walk the string with a depth counter instead and rewrite each slot in place.
function _normSlots(t) {
  let out = "", i = 0;
  while (i < t.length) {
    const c = t[i];
    if (c !== "{") { out += c; i++; continue; }
    let depth = 0, j = i;
    for (; j < t.length; j++) {
      if (t[j] === "{") depth++;
      else if (t[j] === "}") { depth--; if (depth === 0) break; }
    }
    if (j >= t.length) { out += "{EXPR}"; break; }       // unterminated slot: absorb the rest
    const name = t.slice(i + 1, j);
    out += /^[A-Za-z_$][\w.$:-]{0,59}$/.test(name) ? `{${name}}` : "{EXPR}";
    i = j + 1;
  }
  return out;
}

// A GraphQL operation states its own request contract, in types, in its first line.
//
//   mutation SetPhoneNumberV2($phoneNumber: String!, $session: String, $verificationChannel: PhoneVerificationChannel)
//
// That is strictly MORE than we can recover for a REST call: the names are declared rather than inferred,
// and every one carries a type and a nullability marker. We were already capturing the whole document text
// and then throwing this away, 141 operations on one host, every one of them with its variables sitting
// unparsed in a field we had already stored.
//
// Parsed by bracket-matching rather than by regex, because a default value can itself contain parentheses
// or braces (`$filter: Filter = {kind: A}`) and a naive match would stop at the wrong one.
function graphqlVariables(text) {
  const t = String(text || "");
  const m = /\b(query|mutation|subscription)\s+([A-Za-z_][A-Za-z0-9_]*)?\s*\(/.exec(t);
  if (!m) return null;
  let i = m.index + m[0].length, depth = 1, start = i;
  while (i < t.length && depth > 0) {
    const c = t[i];
    if (c === "(") depth++;
    else if (c === ")") depth--;
    else if (c === '"') { i++; while (i < t.length && t[i] !== '"') i += (t[i] === "\\" ? 2 : 1); }
    i++;
  }
  if (depth !== 0) return null;                      // unbalanced: a truncated document, do not guess
  const inner = t.slice(start, i - 1);
  const vars = [];
  let dep = 0, buf = "";
  for (const ch of inner + ",") {                    // split on top-level commas only
    if (ch === "(" || ch === "[" || ch === "{") dep++;
    else if (ch === ")" || ch === "]" || ch === "}") dep--;
    if (ch === "," && dep === 0) { if (buf.trim()) vars.push(buf.trim()); buf = ""; continue; }
    buf += ch;
  }
  const out = [];
  for (const v of vars) {
    const vm = /^\$([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([^=]+?)\s*(?:=|$)/.exec(v);
    if (vm) out.push({ name: vm[1], type: vm[2].trim() });
  }
  return out.length ? out : null;
}

// One shape for every GraphQL collector, so the variable contract cannot be attached by one and forgotten
// by the next two.
// Is this document COMPLETE, or did we capture one fragment of something assembled at runtime? Braintree's
// SDK builds its mutation by conditional concatenation,
//     a = "mutation TokenizeCreditCard($input: TokenizeCreditCardInput!",
//     o && (a += ", $authenticationInsightInput: AuthenticationInsightInput!"),
//     a += ") { tokenizeCreditCard(input: $input) { … } }"
//, so the literal we hold opens a variable list it never closes. Two occurrences in the whole corpus, both
// in that one third-party SDK, which is why the assembly is not traced: it is not worth a general
// string-accumulation pass. But a partial document must not be presented as a whole one. A reader who
// cannot tell "this is the operation" from "this is the first third of it" will try to replay it and get a
// syntax error back from the server with nothing to say why.
const _docTruncated = (t) => {
  const s = String(t || "");
  if (!s) return false;
  let paren = 0, brace = 0, inStr = null;
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (inStr) { if (c === inStr && s[i - 1] !== "\\") inStr = null; continue; }
    if (c === '"' || c === "'") { inStr = c; continue; }
    if (c === "(") paren++; else if (c === ")") paren--;
    else if (c === "{") brace++; else if (c === "}") brace--;
  }
  return paren !== 0 || brace !== 0;
};

function _relayRecord(op, kind, text) {
  const rec = { _kind: "relay", op, opkind: kind, text };
  if (_docTruncated(text)) {
    rec.text_partial = true;
    rec.note = "document is a FRAGMENT: unbalanced parentheses or braces, so it was assembled at runtime " +
               "from more than one literal. The operation exists and is named correctly; the text is not " +
               "replayable as it stands.";
  }
  const vars = graphqlVariables(text);
  if (vars) {
    rec.body_keys = vars.map((v) => v.name);
    rec.variables = vars;                   // names AND types, the operation declared both
    // The operation states a TYPE for every variable, so its shape is better than anything inferred for a
    // REST body: `{campaignId: "ID!"}` rather than `{campaignId: "?"}`. Use it directly.
    const shape = {};
    for (const v of vars) shape[v.name] = v.type;
    rec.body_shape = shape;
  }
  return rec;
}

// A recovered content-type FILLS THE GAP when the construction is invisible. A generated client takes the
// body as a parameter and builds nothing at the call site, which is why 162 of x's 202 recovered bodies had
// no encoding from construction while many of them set the header explicitly.
//
// It fills the gap; it does not override. FormData is the reason: hand one to fetch and the browser sets
// `multipart/form-data; boundary=…` ITSELF, overriding whatever the code asked for, because the boundary is
// generated at send time. A FormData body with a hand-set JSON content-type goes out multipart regardless
// of the source. So construction wins where it is known, and the header answers where it is not.
const _ENC_FROM_CT = (ct) => {
  const v = String(ct || "").toLowerCase();
  if (!v) return null;
  if (v.includes("json")) return "json";                       // application/json, application/*+json
  if (v.includes("x-www-form-urlencoded")) return "form";
  if (v.includes("multipart/")) return "multipart";
  if (v.includes("text/") || v.includes("octet-stream")) return "raw";
  return null;
};

// A PLACEHOLDER IS NOT A FIELD NAME. bodyKeys marks what it could not resolve inline, "{EXPR}" for a
// computed key, "...{EXPR}" for an unresolvable spread, "{EXPR-body}" for a wrapper body it could not
// follow, and those markers travelled in the key LIST, mixed among real fields:
//
//     ['og_image_url', '{EXPR-body}', 'publish_visibility']
//
// which tells a reader to send a parameter literally called {EXPR-body}. 143 of 1,583 recovered bodies
// across the corpus carried one. The fact is real and worth keeping, part of this body is unresolved,
// but it belongs on the record, not in the field list, exactly like `text_partial` on a GraphQL fragment.
const _BODY_PLACEHOLDER = /^\.{0,3}\{EXPR(-body)?\}$/;

function sanitize(records) {
  const out = [];
  for (const r of records) {
    if (Array.isArray(r.body_keys)) {
      const real = r.body_keys.filter((k) => !_BODY_PLACEHOLDER.test(String(k)));
      if (real.length !== r.body_keys.length) {
        r.body_partial = true;
        r.note = (r.note ? r.note + "; " : "") +
                 "body is PARTIAL: part of it is assembled from a value this analysis could not resolve, " +
                 "so the field list is incomplete. The fields named are real.";
        r.body_keys = real.length ? real : null;
      }
    }
    if (r.body_shape && typeof r.body_shape === "object" && !Array.isArray(r.body_shape)) {
      for (const k of Object.keys(r.body_shape))
        if (_BODY_PLACEHOLDER.test(k)) { delete r.body_shape[k]; r.body_partial = true; }
    }
    if (!r.body_encoding && r.content_type) r.body_encoding = _ENC_FROM_CT(r.content_type);
    if (!r.body_encoding) {
      const h = r.req_headers || r.headers;
      if (h && typeof h === "object") {
        for (const k of Object.keys(h)) {
          if (String(k).toLowerCase() === "content-type") {
            const e = _ENC_FROM_CT(h[k]);
            if (e) r.body_encoding = e;
            break;
          }
        }
      }
    }
    let t = r.template;
    if (typeof t === "string") {
      t = t.replace(/[\r\n\t]+/g, "").trim();
      // A FRAGMENT IS NEVER SENT. `/pricing#api` and `/pricing` are one request, the part after `#` is
      // resolved entirely in the browser and no server ever sees it. Keeping it split one endpoint into
      // many: 1,017 of claude's 7,126 endpoints carried one, 14% of that map, including
      // `/docs/en/build-with-claude/overview#feature-availability` sitting beside the same page without it.
      // Strip it before anything else looks at the template, so the dedupe below can do its job.
      const hash = t.indexOf("#");
      if (hash >= 0) t = t.slice(0, hash);
      if (!t) continue;                       // a bare "#anchor" is not an endpoint at all
      // `${base}//${path}` concatenation: collapse repeated slashes but never touch the scheme's "://"
      t = t.replace(/([^:])\/{2,}/g, "$1/");
      t = _normSlots(t);
      if (t.length > 8192) t = t.slice(0, 8192);           // only a generated blob is this long
      if (!t) continue;                                  // empty template: a config default, not an endpoint
      if (/^\{[^}]*\}$/.test(t)) {
        // Nothing but a slot. For a REST path that is unusable, there is no endpoint to speak of. But a
        // socket opened to a runtime URL is still a socket that EXISTS, and a call whose body we recovered
        // is still a contract we learned; dropping either loses a fact because one field was unresolvable.
        // `new WebSocket(e)` is the ordinary way to write it, the URL is always built at runtime, so
        // this rule silently deleted every WebSocket in every bundle. Keep them with a null target, the
        // same way a Next.js server action has no path of its own.
        const worth = r.kind === "ws" || r.kind === "ws-message" ||
                      (r.body_keys && r.body_keys.length) || r.body_shape;
        if (!worth) continue;
        r.template = null;
        r.note = (r.note ? r.note + "; " : "") + "target URL is a runtime value";
        out.push(r);
        continue;
      }
      if (t.split("{").length !== t.split("}").length) continue;   // still unbalanced: unusable
      if (_NAMESPACE.test(t)) continue;                  // XML namespace, never a request
      const am = /^(?:https?|wss?):\/\/([^/?#]*)/i.exec(t);
      if (am && !/[.{$]/.test(am[1]) && am[1] !== "localhost") continue;   // "http://a" is not a host
      r.template = t;
    } else if (t !== undefined && t !== null) {
      continue;                                          // a non-string template is a bug, not data
    }
    // ONE SPELLING FOR "WE DO NOT KNOW". A method field holding something that is not an HTTP verb,
    // "EMAIL" is a config enum that reached one, used to become the string "UNKNOWN", while a path with
    // no call site at all became NULL. Two encodings of the same fact meant every consumer had to test for
    // both, and any that tested for one silently mishandled the other: 530 rows NULL and others "UNKNOWN"
    // in the same table. NULL is the one that SQL already understands, so the string goes; what was
    // actually there is preserved in the note rather than dressed up as a verb.
    if (r.method !== undefined && r.method !== null && !_REAL_VERBS.has(String(r.method).toUpperCase())) {
      const was = String(r.method);
      r.method = null;
      r.note = (r.note ? r.note + "; " : "") + `method field held ${JSON.stringify(was)}, not an HTTP verb`;
    }
    for (const f of ["body_schema", "query_schema"]) {
      const v = r[f];
      if (typeof v === "string" && v !== "{EXPR}") delete r[f];   // a bare type name is not a schema
    }
    // Hold EVERY response schema to the same standard, whichever collector produced it. The older `.json()`
    // and options-object paths were still emitting One dashboard's ["success","result","errors"] envelope and
    // react-query's ["data","isLoading"] hook state, identical on every route, so they say nothing about the
    // endpoint. One choke point means a new collector cannot reintroduce the noise.
    // `post(url, {})` is a KNOWN answer, the request carries no fields, and it is not the same claim as "we
    // could not work out the body". Both currently land as a falsy body_keys and become indistinguishable, which
    // makes a resolved empty body look like a gap in every count downstream.
    if (Array.isArray(r.body_keys) && r.body_keys.length === 0) r.body_empty = true;

    // ONE field that always carries the best body shape we have, so a consumer never has to know which of
    // three fields to look in. `body_src` is the full nested tree, objects, arrays and their element
    // shapes, but it is only produced when every value could be classified, which is a much narrower
    // condition than recovering the key NAMES. Where the tree exists, use it. Where only names exist, emit
    // the names with unknown values rather than nothing: a schema listing fields of unknown type is still
    // a schema, and it is what the flat `body_keys` was always standing in for.
    // A DECLARATIVE CLIENT STATES ITS OWN CONTRACT, and we were walking past it.
    //
    //   addClientRoleMappings = makeRequest({method:"POST", path:"/{id}/role-mappings/clients/{cid}",
    //                                        urlParamKeys:["id","cid"], payloadKey:"roles"})
    //
    // Every literal sibling key of that config is already captured verbatim in `descriptor`. We read
    // `method` and `path` out of it and left `body_keys: null` sitting directly beside `payloadKey:"roles"`
    // in the same record, then reconstructed the identical fact downstream, in another language, by
    // subtracting path parameters from a call site in a different file. Read it here instead.
    const desc = r.descriptor;
    if (desc && typeof desc === "object") {
      if (!r.body_keys || !r.body_keys.length) {
        for (const [dk, dv] of Object.entries(desc)) {
          if (typeof dv === "string" && /payload|body|data/i.test(dk)) { r.body_keys = [dv]; break; }
        }
      }
      if (!r.query_schema) {
        for (const [dk, dv] of Object.entries(desc)) {
          if (Array.isArray(dv) && /quer(y)?param/i.test(dk)) { r.query_schema = dv; break; }
        }
      }
    }
    // KEEP THE RICHEST SHAPE WE ACTUALLY RECOVERED.
    //
    // Three different walkers can describe a body and they were consulted in a fixed order that had the
    // best one missing entirely. `bodySchema()` produces the NESTED tree with per-leaf provenance and every
    // collector already carried it as `body_schema`, and then this assembly ignored that field and built
    // `body_shape` out of the flat key list with a literal "?" per key. Measured on one bundle:
    //
    //   body_schema: {"issueDate": {"year":"computed:getFullYear","month":"{EXPR}","day":"computed:getDate"},
    //                 "attachments":"input:n"}
    //   body_shape : {"issueDate":"?", "attachments":"?"}        <- what reached the store
    //
    // The nesting was recovered, then discarded one assignment later. Across every store in runs/, 455 of
    // 473 leaf values in the shipped schema are the placeholder "?", which is what a request contract
    // looks like when the answer was computed and thrown away.
    //
    // So: score each candidate by how much it actually says (a nested object says more than a typed leaf,
    // a typed leaf says more than "?") and keep the winner, rather than trusting an order.
    const _informative = (o) => {
      if (!o || typeof o !== "object") return 0;
      let n = 0;
      const walk = (x) => {
        if (x && typeof x === "object") { n += 2; for (const v of Object.values(x)) walk(v); }
        else if (x !== undefined && String(x) !== "?" && String(x) !== "{EXPR}") n += 1;
      };
      walk(o);
      return n;
    };
    {
      let best = r.body_shape && typeof r.body_shape === "object" ? r.body_shape : null;
      for (const cand of [r.body_schema, r.body_src]) {
        if (cand && typeof cand === "object" && !Array.isArray(cand) &&
            _informative(cand) > _informative(best)) best = cand;
      }
      if (!best && Array.isArray(r.body_keys) && r.body_keys.length) {
        // Last resort, and honest about it: we know the names and nothing else.
        best = {};
        for (const k of r.body_keys) best[String(k)] = "?";
      }
      if (best) r.body_shape = best;
    }
    if (Array.isArray(r.response_keys)) {
      const ks = r.response_keys.filter((k) => typeof k === "string" && k);
      if (!ks.length || _looksLikeHook(ks)) delete r.response_keys;
      else r.response_keys = ks;
    }
    out.push(r);
  }
  return out;
}

// Query parameters are a first-class injection point, so leaving them buried inside the template string as
// "?token={param:e}&limit=50" is a lossy representation, a consumer would have to re-parse the URL to learn
// anything. Whenever a resolved template carries a query string, lift it into the same structured shape the
// opts-sink produces. The literal values are worth keeping too: "?usePrefix=false&labelNames[]=INBOX" states
// the parameter's real domain, which is exactly what you need to fuzz it credibly.
function annotateQuery(records) {
  for (const e of records) {
    const t = e.template;
    if (!t || typeof t !== "string") continue;
    const qi = t.indexOf("?");
    if (qi < 0) continue;
    // query_schema is EITHER an array of {name,…} (opts-sink) or a nested schema object keyed by param
    // name (queryInfo). Assuming the array form threw a TypeError that the caller swallowed, which turned
    // every affected file into a silent "no endpoints", the whole point of merging here is to add
    // information, so it must adapt to whichever representation the record already carries.
    const existing = e.query_schema;
    const isArr = Array.isArray(existing);
    const isObj = existing && typeof existing === "object" && !isArr;
    const have = new Set(isArr ? existing.map((q) => q && q.name) : isObj ? Object.keys(existing) : []);
    const add = [];
    for (const pair of t.slice(qi + 1).split("&")) {
      if (!pair) continue;
      const eq = pair.indexOf("=");
      const name = eq < 0 ? pair : pair.slice(0, eq);
      const value = eq < 0 ? null : pair.slice(eq + 1);
      if (!name) continue;
      if (/^\{/.test(name)) { e.query_dynamic = true; continue; }   // whole query string is one unresolved expr
      if (have.has(name)) continue;
      have.add(name);
      const rec = { name, source: "template" };
      if (value !== null) {
        // a literal value is evidence about the parameter's domain; a slot just marks it as caller-supplied
        if (/^\{.*\}$/.test(value)) rec.dynamic = true;
        else rec.value = value;
      }
      add.push(rec);
    }
    if (!add.length) continue;
    if (isObj) {
      for (const q of add) existing[q.name] = q.value !== undefined ? q.value : "{dynamic}";
    } else {
      e.query_schema = [...(isArr ? existing : []), ...add];
    }
  }
}

// Client-side route tables: {path:"/x", element|component|children|loader|lazy|handle}. High precision, the
// sibling key is what separates a router config from any other object with a `path` property.
const _ROUTE_SIB = /^(element|component|Component|children|loader|action|lazy|handle|render|page|name|title|redirect|beforeEnter|meta)$/;
// Next.js SERVER ACTIONS. A `"use server"` function is invoked by POSTing to whatever PAGE renders it, with
// the operation identified by a `Next-Action:` header rather than by the path. So the entire mutation surface
// of an App Router app is invisible to anything that enumerates URLs, every action on a page shares one
// path, exactly like GraphQL operations sharing /graphql.
//
// The client never receives the function body, only a reference:
//
//   let N = (0, x.createServerReference)("7ff2bb5c…", x.callServer, void 0, x.findSourceMapURL, "acceptInvite")
//                                         ^ build-scoped id                                      ^ the dev's own name
//
// Argument 5 is the gift: the developer's name for the RPC. That makes a finding writable against a stable
// identity ("the acceptInvite action") instead of a hash that dies at the next deploy, the id is derived at
// build time and changes whenever the code does.
//
// Anchored on the framework's exported function name, which is legitimate here: `createServerReference` is a
// public Next runtime API accessed as a property, so minification renames the local binding (`x`) but never
// the property. Same reasoning as trusting `fetch`.
function collectServerActions(ast) {
  const out = [];
  const calleeName = (c) => {
    if (!c) return null;
    if (c.type === "SequenceExpression") return calleeName(c.expressions[c.expressions.length - 1]);
    if (c.type === "MemberExpression" && !c.computed) return c.property && c.property.name;
    if (c.type === "Identifier") return c.name;
    return null;
  };
  traverse(ast, { CallExpression(path) {
    try {
      if (calleeName(path.node.callee) !== "createServerReference") return;
      const a = path.node.arguments || [];
      const id = a[0] && a[0].type === "StringLiteral" ? a[0].value : null;
      if (!id || !/^[0-9a-f]{16,}$/i.test(id)) return;
      const nm = a[4] && a[4].type === "StringLiteral" ? a[4].value : null;
      out.push({
        _kind: "server-action",
        method: "POST",
        op: nm || null,
        action_id: id,
        // NOT a path. The action POSTs to whichever page renders it, and inventing one would be a guess.
        template: null,
        note: "Next.js Server Action: POST to the page that renders it, with header Next-Action: " + id,
        source: "createServerReference",
      });
    } catch {}
  } });
  return out;
}

// NAVIGATION SINKS. A route the app can navigate to is surface, exactly as much as one it can fetch, and
// none of this was being collected. A census over 25 corpus hosts found ~1,977 navigation-sink occurrences
// against 1,158 network ones, we had built half an extractor and were reading the resulting endpoint
// count as if it were the whole map.
//
// These carry no method and no contract; a navigation target is a destination, not a request with a body.
// They are emitted as `_kind: "nav"` so a consumer can tell "the app can go here" from "the app can call
// this", which are different claims and must not be counted together.
// CLASS GETTERS RETURNING AN OBJECT. `data: e.saveData` is the commonest way a model hands its payload to
// a client: the call site names a PROPERTY and the shape lives in a getter on some class elsewhere in the
// bundle. Discourse ships exactly this,
//     create(e) { return S(`/bookmarks.json`, {method:"POST", data: e.saveData}) }
//     class Bookmark { get saveData() { return {reminder_at:…, name:…, bookmarkable_id:…} } }
//, and we reported the endpoint with no body while six field names sat in the same file.
//
// Indexed by property NAME, because the receiver's type is exactly what minification destroys. That makes
// it a heuristic: two classes could define the same getter differently. Property names like `saveData` are
// distinctive enough that the risk is small, and the alternative is recovering nothing.
let _GETTERS = new Map();
// BACKWARD TRACE. `data: row` where `row` is a function PARAMETER, the shape is not here, it is in
// whoever calls this function. Keycloak does exactly this: `updateRequiredAction({alias:o.alias}, o)`,
// where `o` is a table row built by a `.map()` two frames away.
//
// So: find the enclosing function, find ITS call sites, take the argument in the same position, and
// recurse. If that argument is itself an element of an array (a forEach/map callback parameter), follow
// the array to the expression that produced it. Both hops are bounded, this is a search for a literal,
// not a solver, and it gives up rather than guessing.
function paramProvenance(node, scope, depth) {
  if (!node || node.type !== "Identifier" || depth > 4) return null;
  const b = scope && scope.getBinding && scope.getBinding(node.name);
  if (!b) return null;

  // (a) the identifier is a callback parameter iterating an array: arr.forEach(r => f(r))
  if (b.kind === "param" && b.path && b.path.parentPath) {
    const fn = b.path.parentPath;
    const call = fn.parentPath;
    if (call && call.isCallExpression && call.isCallExpression()) {
      const callee = call.node.callee;
      const iter = callee && callee.type === "MemberExpression" &&
                   /^(forEach|map|filter|flatMap)$/.test(String(callee.property && callee.property.name));
      if (iter) {
        const arr = callee.object;
        const viaArr = arrayElementShape(arr, call.scope, depth + 1);
        if (viaArr) return viaArr;
      }
    }
  }

  // (b) the identifier is a parameter of a named function: find call sites, take the same argument slot
  if (b.kind === "param" && b.path) {
    const fnPath = b.path.getFunctionParent ? b.path.getFunctionParent() : null;
    if (!fnPath) return null;
    const idx = (fnPath.node.params || []).findIndex(
      (pp) => pp && pp.type === "Identifier" && pp.name === node.name);
    if (idx < 0) return null;
    const fnId = fnPath.node.id ? fnPath.node.id.name
               : (fnPath.parentPath && fnPath.parentPath.node && fnPath.parentPath.node.id
                  ? fnPath.parentPath.node.id.name : null);
    if (!fnId) return null;
    const fb = fnPath.scope.getBinding(fnId) || (scope.getBinding && scope.getBinding(fnId));
    for (const ref of (fb && fb.referencePaths) || []) {
      const call = ref.parentPath;
      if (!call || !call.isCallExpression || !call.isCallExpression()) continue;
      const arg = (call.node.arguments || [])[idx];
      if (!arg) continue;
      const ks = bodyKeys(arg, call.scope, depth + 1);
      if (ks && ks.length) return ks;
    }
  }
  return null;
}

// The shape of what an array holds, when the array came from `X.map(cb)` and cb returns an object literal.
function arrayElementShape(arrNode, scope, depth) {
  if (!arrNode || depth > 4) return null;
  let init = arrNode;
  if (arrNode.type === "Identifier") {
    const b = scope && scope.getBinding && scope.getBinding(arrNode.name);
    init = b && b.path && b.path.node && b.path.node.init;
  }
  if (!init || init.type !== "CallExpression") return null;
  const c = init.callee;
  if (!(c && c.type === "MemberExpression" && /^(map|flatMap)$/.test(String(c.property && c.property.name))))
    return null;
  const cb = (init.arguments || [])[0];
  if (!cb || (cb.type !== "ArrowFunctionExpression" && cb.type !== "FunctionExpression")) return null;
  const ret = _returnedObject(cb);
  if (!ret || ret.type !== "ObjectExpression") return null;
  const ks = [];
  for (const pr of ret.properties || [])
    if ((pr.type === "ObjectProperty" || pr.type === "ObjectMethod") && pr.key && !pr.computed)
      ks.push(pr.key.name ?? pr.key.value);
  return ks.filter(Boolean).length ? ks.filter(Boolean) : null;
}

// Resolve an expression far enough to store it: a string literal, or a property read that config answers.
// Deliberately shallow. This runs over every assignment in the bundle, so anything expensive here is paid
// thousands of times, and a value that needs deep resolution is better left to the main pass.
function _shallowValue(node) {
  if (!node) return null;
  if (node.type === "StringLiteral") return node.value;
  if (node.type === "TemplateLiteral" && node.expressions.length === 0 && node.quasis.length === 1)
    return node.quasis[0].value.cooked ?? node.quasis[0].value.raw ?? null;
  if ((node.type === "MemberExpression" || node.type === "OptionalMemberExpression") &&
      !node.computed && node.property && node.property.name)
    return _CONFIG.get(node.property.name) || null;
  if (node.type === "LogicalExpression")                 // `t?.baseUrl || DEFAULT`
    return _shallowValue(node.left) || _shallowValue(node.right);
  return null;
}

// Record a candidate value for a property, and poison the entry the moment two candidates disagree.
const _AMBIG = Symbol("ambiguous");
function _note(into, k, v) {
  if (!into.has(k)) { into.set(k, v); return; }
  const prev = into.get(k);
  if (prev !== v) into.set(k, _AMBIG);
}

// ── CALLER ARGUMENT SHAPES, INDEXED ACROSS THE WHOLE CORPUS ────────────────────────────────────────
// `changeRole(e){ r({url:"...", data:e}) }` hands us a verb, a path and no contract. The shape is one
// frame up at whoever calls changeRole, and in a real bundle that caller is in a DIFFERENT CHUNK, so
// resolving it inside one file finds nothing. Measured: the same-file version fixed the pattern in
// isolation and moved 0 of 604 bodies across six real targets.
//
// This rides along on the index pass that already reads every file once, recording, per method name
// and argument position, the LITERAL object shapes callers pass. Union rather than first-wins: one
// endpoint routinely takes several partial bodies, the wire showed /user/me sending {phone} while the
// first caller in the code passed {name}, and both are real fields.
const _CALLARGS = new Map();

function _shapeLiteralOnly(node, d) {
  if (!node || (d || 0) > 6 || node.type !== "ObjectExpression") return null;
  const out = {};
  for (const p of node.properties) {
    if (p.type !== "ObjectProperty" || !p.key) continue;
    const k = p.key.name != null ? p.key.name : p.key.value;
    if (k == null || typeof k !== "string") continue;
    const v = p.value;
    out[k] = (v && v.type === "ObjectExpression")
      ? (_shapeLiteralOnly(v, (d || 0) + 1) || "?")
      : (_LIT(v) || "?");
  }
  return Object.keys(out).length ? out : null;
}

function indexCallArgs(ast, into) {
  traverse(ast, {
    CallExpression(p) {
      try {
        const ce = p.node.callee;
        let name = null;
        if (ce.type === "MemberExpression" && ce.property && !ce.computed)
          name = ce.property.name != null ? ce.property.name : ce.property.value;
        else if (ce.type === "Identifier") name = ce.name;
        // Short names in minified code are ambiguous by construction: `t.a({x:1})` says nothing about
        // which `a`. A name a human wrote survives minification only when it is a public API surface,
        // and those are the ones worth indexing.
        if (!name || typeof name !== "string" || name.length < 4) return;
        const args = p.node.arguments || [];
        for (let i = 0; i < Math.min(args.length, 3); i++) {
          const sh = _shapeLiteralOnly(args[i], 0);
          if (!sh) continue;
          const key = name + "|" + i;
          const prev = into.get(key);
          if (!prev) into.set(key, sh);
          else for (const k of Object.keys(sh)) if (!(k in prev)) prev[k] = sh[k];
        }
      } catch (_) {}
    }
  });
}

function indexPropertyValues(ast, into) {
  traverse(ast, {
    AssignmentExpression(p) {
      try {
        const l = p.node.left;
        if (l.type !== "MemberExpression" || l.computed || !l.property || !l.property.name) return;
        const v = _shallowValue(p.node.right);
        // Only keep a value the bundle is UNANIMOUS about. Keycloak assigns baseUrl four different ways,
        // `this.baseUrl=t.baseUrl`, `=t.adminBaseUrl`, `=t?.baseUrl||DEFAULT`, and taking the first
        // produced `https://auth.example.com/authadmin/realms/...`, a URL missing a separator that 404s.
        // Disagreement means we do not know, and a named slot is the honest answer; the config values are
        // recorded separately so a reader can resolve it deliberately.
        // A runtime value is a candidate too, and it is the one that says "not known statically".
        // Skipping it let one literal anywhere answer for every object: the Iceberg client's
        // `this.prefix = t` borrowed `prefix:"/"` from path-to-regexp, and printed `//namespaces`.
        _note(into, l.property.name, v || _AMBIG);
      } catch {}
    },
    ObjectProperty(p) {
      try {
        const k = p.node.key && !p.node.computed && (p.node.key.name ?? p.node.key.value);
        if (!k) return;
        const v = _shallowValue(p.node.value);
        if (v) _note(into, String(k), v);
      } catch {}
    },
  });
}

function indexObjectGetters(ast) {
  const m = new Map();
  const keysOf = (obj) => {
    const ks = [];
    for (const pr of obj.properties || []) {
      if ((pr.type === "ObjectProperty" || pr.type === "ObjectMethod") && pr.key && !pr.computed)
        ks.push(pr.key.name ?? pr.key.value);
    }
    return ks.filter(Boolean);
  };
  traverse(ast, {
    "ClassMethod|ObjectMethod"(p) {
      try {
        const n = p.node;
        if (n.kind !== "get" && n.kind !== "method") return;
        const name = n.key && !n.computed && (n.key.name ?? n.key.value);
        if (!name || m.has(name)) return;
        const ret = _returnedObject(n);
        if (ret && ret.type === "ObjectExpression") {
          const ks = keysOf(ret);
          if (ks.length) m.set(String(name), ks);
        }
      } catch {}
    },
  });
  return m;
}

// JSX SURFACE, forms and navigation declared in component code.
//
// A <form> in a component is a request contract as much as one in served markup: it states its method, its
// target and every field name. A <Link to=…> is a navigation target. The extractor had no JSX handling at
// all, so on a React application every form declared in code was invisible unless a crawl happened to
// render the state containing it, which makes form discovery depend on reaching a state, when the
// declaration was sitting in the bundle the whole time.
//
// Both shapes are handled because production ships the second: source `<form action="/x">` appears only in
// sourcemapped or unminified code, while a built bundle contains `jsx("form",{action:"/x",children:[…]})`.
// The element NAME and the attribute names survive minification, they are the DOM contract, so the
// compiled form is as readable as the source one.
// A <form> record, without inventing where it goes.
//
// `action || "/"` claimed that every action-less form posts to the site root. In shipped React that is
// simply false, measured over 830 MB of corpus bundles, 323 of 340 forms (95%) declare no action, because
// submission is an onSubmit handler that calls preventDefault and fetches somewhere else entirely. The
// form never touches "/". Recording it that way manufactured a root endpoint out of the commonest shape
// in the sample.
//
// The field names are still real and still worth having, so the record survives with an explicit null
// target instead of a fabricated one. Where the endpoint actually lives, inside the handler, the fetch
// call site is already extracted as its own sink with its own body keys, so nothing is lost by declining
// to guess here.
function _formRecord(method, action, fields, source) {
  const rec = { _kind: "form", method, source, confidence: "high",
                note: "<form> in " + (source === "jsx" ? "component JSX" : "compiled JSX") +
                      ", never submitted" };
  if (action) {
    rec.template = _canonPath(action);
  } else {
    rec.template = null;
    rec.unresolved = "no action attribute; submission is handled in JS";
  }
  if (String(method).toUpperCase() === "GET") rec.query_schema = fields; else rec.body_keys = fields;
  return rec;
}

function collectJsx(ast) {
  const out = [];
  const NAV_ATTR = { a: "href", area: "href", Link: "to", NavLink: "to", Redirect: "to" };

  const litOf = (n) => {
    if (!n) return null;
    if (n.type === "StringLiteral") return n.value;
    if (n.type === "JSXExpressionContainer") return litOf(n.expression);
    if (n.type === "TemplateLiteral") { try { return strFromNode(n, null, 0).s; } catch { return null; } }
    return null;
  };

  // ---- source JSX -------------------------------------------------------------------------------
  const attrsOf = (open) => {
    const a = {};
    for (const at of open.attributes || []) {
      if (at.type !== "JSXAttribute" || !at.name) continue;
      const v = litOf(at.value);
      if (v !== null) a[at.name.name] = v;
    }
    return a;
  };
  const tagOf = (open) => (open.name && (open.name.name ||
    (open.name.property && open.name.property.name))) || "";

  traverse(ast, {
    JSXElement(p) {
      try {
        const tag = tagOf(p.node.openingElement);
        const at = attrsOf(p.node.openingElement);
        const navAttr = NAV_ATTR[tag];
        if (navAttr && at[navAttr] && /^\//.test(at[navAttr]))
          out.push({ _kind: "nav", method: "GET", template: _canonPath(at[navAttr]), sink: "jsx:" + tag });
        if (tag !== "form") return;
        const fields = [];
        p.traverse({
          JSXOpeningElement(q) {
            const t = tagOf(q.node);
            if (!/^(input|select|textarea)$/i.test(t)) return;
            const fa = attrsOf(q.node);
            if (fa.name) fields.push(fa.name);
          },
        });
        if (!fields.length) return;
        const method = String(at.method || "GET").toUpperCase();
        const rec = _formRecord(method, at.action, fields, "jsx");
        out.push(rec);
      } catch {}
    },

    // ---- compiled JSX: jsx("form",{…}) / jsxs(…) / React.createElement("form",{…}) ---------------
    CallExpression(p) {
      try {
        const c = p.node.callee;
        const nm = c && (c.name || (c.property && c.property.name));
        if (!/^(jsx|jsxs|jsxDEV|jsxsDEV|createElement|h)$/.test(String(nm || ""))) return;
        const a = p.node.arguments || [];
        const tag = a[0] && a[0].type === "StringLiteral" ? a[0].value : null;
        const props = a[1] && a[1].type === "ObjectExpression" ? a[1] : null;
        if (!tag || !props) return;
        const prop = (k) => {
          for (const pr of props.properties || []) {
            if (pr.type !== "ObjectProperty" || pr.computed || !pr.key) continue;
            if ((pr.key.name ?? pr.key.value) === k) return pr.value;
          }
          return null;
        };
        const navAttr = NAV_ATTR[tag];
        if (navAttr) {
          const v = litOf(prop(navAttr));
          if (v && /^\//.test(v))
            out.push({ _kind: "nav", method: "GET", template: _canonPath(v), sink: "jsx:" + tag });
        }
        if (tag !== "form") return;
        const act = litOf(prop("action"));
        const method = String(litOf(prop("method")) || "GET").toUpperCase();
        // children carry the inputs; walk the whole subtree because they nest arbitrarily deep
        const fields = [];
        p.traverse({
          CallExpression(q) {
            const qc = q.node.callee;
            const qn = qc && (qc.name || (qc.property && qc.property.name));
            if (!/^(jsx|jsxs|jsxDEV|jsxsDEV|createElement|h)$/.test(String(qn || ""))) return;
            const qa = q.node.arguments || [];
            const qt = qa[0] && qa[0].type === "StringLiteral" ? qa[0].value : null;
            if (!qt || !/^(input|select|textarea)$/i.test(qt)) return;
            const qp = qa[1];
            if (!qp || qp.type !== "ObjectExpression") return;
            for (const pr of qp.properties || []) {
              if (pr.type !== "ObjectProperty" || pr.computed || !pr.key) continue;
              if ((pr.key.name ?? pr.key.value) !== "name") continue;
              const v = litOf(pr.value);
              if (v) fields.push(v);
            }
          },
        });
        if (!fields.length) return;
        const rec = _formRecord(method, act, fields, "jsx-compiled");
        out.push(rec);
      } catch {}
    },
  });
  return out;
}

function collectNavigation(ast) {
  const out = [];
  const seen = new Set();
  const add = (t, how) => {
    if (!t || typeof t !== "string") return;
    if (!/^([/]|https?:\/\/)/.test(t)) return;          // relative fragments and bare words are not routes
    if (t.length < 2 || t.length > 300) return;
    if (/^\/\//.test(t)) return;                        // protocol-relative asset, not a route
    const k = how + " " + t;
    if (seen.has(k)) return;
    seen.add(k);
    out.push({ _kind: "nav", method: "GET", template: _canonPath(t), sink: how, dynamic: /\{/.test(t) });
  };
  const str = (n, scope) => {
    if (!n) return null;
    try { const r = resolveUrl(n, scope); return r && r.template; } catch { return null; }
  };

  traverse(ast, {
    // location.href = "/x"   |   location = "/x"
    AssignmentExpression(path) {
      try {
        const l = path.node.left;
        if (l.type !== "MemberExpression" || l.computed) return;
        const prop = l.property && l.property.name;
        const objName = l.object && (l.object.name || (l.object.property && l.object.property.name));
        if (prop === "href" && (objName === "location" || objName === "Location"))
          add(str(path.node.right, path.scope), "location.href");
        // a.href = "/x" on a created anchor, the DOM-inserted navigation case
        else if (prop === "href")
          add(str(path.node.right, path.scope), "element.href");
      } catch {}
    },
    CallExpression(path) {
      try {
        const c = path.node.callee, a = path.node.arguments || [];
        if (!c) return;
        const prop = c.type === "MemberExpression" && !c.computed && c.property && c.property.name;
        const objName = c.type === "MemberExpression" && c.object &&
                        (c.object.name || (c.object.property && c.object.property.name));

        // location.assign("/x") / location.replace("/x")
        if ((prop === "assign" || prop === "replace") &&
            (objName === "location" || objName === "Location"))
          return add(str(a[0], path.scope), "location." + prop);

        // window.open("/x")
        if (prop === "open" && (objName === "window" || objName === "self" || objName === "top"))
          return add(str(a[0], path.scope), "window.open");

        // history.pushState(state, title, "/x"), the URL is the THIRD argument
        if (prop === "pushState" || prop === "replaceState")
          return add(str(a[2], path.scope), "history." + prop);

        // router.push("/x") / navigate("/x") / history.push("/x")
        if ((prop === "push" || prop === "replace") &&
            /^(router|navigate|history|nav)$/i.test(String(objName || "")))
          return add(str(a[0], path.scope), "router." + prop);

        // A bare call whose callee is a navigate function: `const nav = useNavigate(); nav("/x")`.
        //
        // Matching the NAME is wrong twice over. It misses `nav`, `go`, `to`, and every other thing a
        // developer might call it, and minification renames all of them to `e` regardless. So resolve the
        // BINDING instead: if the identifier was initialised from useNavigate() / useRouter() / useHistory(),
        // it is a navigate function whatever it ended up being called. That survives minification, because
        // the hook name is an imported property and properties are not renamed.
        if (c.type === "Identifier") {
          const b = path.scope && path.scope.getBinding && path.scope.getBinding(c.name);
          const init = b && b.path && b.path.node && b.path.node.init;
          const initName = init && init.type === "CallExpression" && init.callee &&
                           (init.callee.name || (init.callee.property && init.callee.property.name));
          const fromHook = initName && /^use(Navigate|Router|History|Link)$/.test(String(initName));
          if (fromHook || /^(navigate|redirect|goTo)$/i.test(String(c.name))) {
            const t = str(a[0], path.scope);
            if (t && /^\//.test(t)) add(t, "navigate()");
          }
        }
      } catch {}
    },
  });
  return out;
}

// ── WebSocket MESSAGES ────────────────────────────────────────────────────────────────────────────
//
// `new WebSocket(url)` was recorded with `body_keys: null`: we learned that a socket exists and nothing
// about what travels over it. For a real-time product that is most of the application, the domain
// presence, moderation, and it is the part a crawl can never reach, because messages are sent when a
// user does something and the crawler deliberately does not press buttons. Static analysis is not merely
// the better route here, it is the only one.
//
// Three shapes carry essentially all of it:
//
//   ws.send(JSON.stringify({...}))          raw socket
//   channel.push("new_msg", {body: t})      Phoenix, what one target ships
//   socket.emit("chat", {room, text})       Socket.IO
//
// Matched by BINDING PROVENANCE, not by method name. `.push` is Array.prototype.push and `.emit` is every
// EventEmitter in the bundle; keying on the name would drown the map in noise. So the receiver must be
// traceable to a socket or a channel, which is the same technique used for useNavigate() elsewhere.
const _WS_INIT = /^(WebSocket|SockJS|ReconnectingWebSocket)$/;
// A socket is almost never a local `const`. Real code keeps it on the instance,
// `this.socket.emit("setup", {...})`, so the receiver is a member expression and there is no binding to
// trace. Measured on a real bundle (one target's a socket client class): every send is
// `this.socket.emit(...)`, and requiring a resolvable binding found none of them.
//
// So accept a socket-shaped RECEIVER NAME as well, gated on the file actually containing socket
// machinery. Two independent signals, the field is called `socket`/`ws`/`channel`, and this file
// constructs or imports a socket, which keeps `this.emitter.emit("change", …)` out.
const _SOCK_FIELD = /^(socket|ws|websocket|sock|conn|connection|channel|io|client)$/i;
const _SOCK_EVIDENCE = /socket\.io|engine\.io|new [A-Za-z_$.]*WebSocket\(|phx_join|SockJS|ReconnectingWebSocket/;

function collectSocketMessages(ast, src) {
  const out = [];
  const fileHasSocket = _SOCK_EVIDENCE.test(String(src || ""));
  const isSocketBinding = (node, scope) => {
    if (!node) return null;
    // The FIELD case must be tested before the Identifier guard below. It was written after it, which made
    // it unreachable, the guard returns on anything that is not an Identifier, and `this.socket` is a
    // MemberExpression. Same shape as the duplicate `case "CallExpression"` that sat dead in this file for
    // weeks: an early return turning later code into decoration.
    if (node.type === "MemberExpression" && !node.computed && node.property && fileHasSocket) {
      const f = node.property.name ?? node.property.value;
      if (_SOCK_FIELD.test(String(f || ""))) return String(f).toLowerCase() === "channel" ? "channel" : "socket";
    }
    if (node.type !== "Identifier") return null;
    const b = scope && scope.getBinding && scope.getBinding(node.name);
    const init = b && b.path && b.path.node && b.path.node.init;
    if (!init) return null;
    if (init.type === "NewExpression") {
      const n = init.callee && (init.callee.name || (init.callee.property && init.callee.property.name));
      if (_WS_INIT.test(String(n || ""))) return "socket";
    }
    if (init.type === "CallExpression") {
      const c = init.callee;
      const n = c && (c.name || (c.property && c.property.name));
      if (String(n) === "channel") return "channel";              // phoenix: socket.channel(topic, params)
      if (String(n) === "io" || _WS_INIT.test(String(n || ""))) return "socket";
    }
    return null;
  };
  traverse(ast, {
    CallExpression(path) {
      try {
        const c = path.node.callee;
        if (!c || c.type !== "MemberExpression" || !c.property) return;
        const m = c.property.name ?? c.property.value;
        if (!/^(send|emit|push|on)$/.test(String(m || ""))) return;
        const kindOf = isSocketBinding(c.object, path.scope);
        if (!kindOf) return;
        // `.on(event, handler)` is the INBOUND half, what the server sends us. No body to recover, but
        // the event name is half the protocol and it is free to record.
        if (m === "on") {
          const a0 = (path.node.arguments || [])[0];
          if (a0 && a0.type === "StringLiteral" && a0.value.length > 1) {
            out.push({ sink: "ws.on", kind: "ws-message", method: "WS", template: null,
                       op: a0.value, direction: "inbound", dynamic: false });
          }
          return;
        }
        if (m === "push" && kindOf !== "channel") return;          // Array.push, not a channel push
        const a = path.node.arguments || [];
        // `send(payload)` has no event name; `emit/push(event, payload)` puts it first.
        const named = (m !== "send") && a[0] && a[0].type === "StringLiteral";
        const ev = named ? a[0].value : null;
        const payload = named ? a[1] : a[0];
        if (!payload) return;
        const rec = { sink: "ws." + m, kind: "ws-message", method: "WS",
                      template: null, op: ev, direction: "outbound", dynamic: false };
        const bk = bodyKeys(payload, path.scope);
        if (bk && bk.length) rec.body_keys = bk;
        const bf = bodyFields(payload, path.scope);
        if (bf) rec.body_src = bf;
        if (!rec.body_keys && !ev) return;      // nothing learned; do not emit an empty claim
        out.push(rec);
      } catch {}
    },
  });
  return out;
}

function collectRouteTables(ast, src) {
  const out = [];
  const seen = new Set();

  // A router config is a TREE and children are declared RELATIVE to their parent, `{path:"/app", children:
  // [{path:"users"}]}` means /app/users. Requiring a leading slash therefore threw away every nested route
  // and kept only the roots: 105 of 465 `{path:...}` occurrences on one Keycloak bundle, which read as a
  // complete route list and was a quarter of one.
  //
  // So walk UP from each path-bearing object through its enclosing route objects and join the segments.
  // The parent chain is the structure; the leading slash was never the signal.
  const parentPath = (path) => {
    const segs = [];
    let cur = path.parentPath;
    while (cur) {
      if (cur.isObjectExpression()) {
        for (const pr of cur.node.properties || []) {
          if (pr.type !== "ObjectProperty" || pr.computed) continue;
          const k = pr.key && (pr.key.name || pr.key.value);
          if (k === "path" && pr.value.type === "StringLiteral") { segs.unshift(pr.value.value); break; }
        }
      }
      cur = cur.parentPath;
    }
    return segs;
  };

  // A `path` property alone is ambiguous, it is also how you spell a filesystem path, an SVG asset, or a
  // config key, so the sibling test (`element`, `component`, `loader`, …) is what makes a route object
  // recognisable. That test is right about what it accepts and wrong about what it rejects: an app is free
  // to declare `creatorTag: l({path:"/tags/:tag"})` and let a factory attach the component elsewhere, and
  // then NO route object has a sibling and the whole table disappears. Measured on the corpus: the bare
  // shape is the DOMINANT one among SPAs we recovered nothing from, 6 of 16 hosts, 102 occurrences,
  // while hosts that already worked are dominated by the sibling shape. So it is a real table, not noise.
  //
  // What separates a route table from an incidental `path:` is DENSITY. One `{path:"/usr/lib"}` is a config
  // value; forty sibling-less `{path:"/…"}` objects in one file are a router. So collect the bare ones,
  // then keep them only if enough of them exist to BE a table, and label them distinctly so a consumer can
  // tell a factory-declared route from an explicitly-declared one.
  // WHERE a client route lives decides whether it can be crawled at all, and the router says which.
  // Hash routing keeps the whole route in the URL fragment, which browsers never transmit, so the server
  // has never heard of these paths and answers 404 to every one of them. Measured on Keycloak's admin
  // console: /master/clients, /master/users/abc/settings and a route invented for the test all returned
  // 404 identically, while the bundle held 105 real routes. Path routing is the opposite: the request is
  // made, the server returns the app shell, and on a server-rendered app it returns real content too.
  //
  // Recording which one it is costs a regex over source we have already read, and it is the difference
  // between "this route table is a map" and "this route table is a work list". Stated, never inferred:
  // when neither router appears, the field is absent rather than guessed.
  const routing = _ROUTING || detectRouting(ast, src);

  const bare = [];
  const emit = (t, source) => {
    if (seen.has(t)) return;
    seen.add(t);
    const r = { _kind: "route", method: "GET", template: t, source };
    if (routing) r.routing = routing;
    out.push(r);
  };

  traverse(ast, { ObjectExpression(path) {
    try {
      let p = null, sib = false, nprops = 0, verb = false;
      for (const pr of path.node.properties || []) {
        if (pr.type !== "ObjectProperty" || pr.computed) continue;
        nprops++;
        const k = pr.key.name || pr.key.value;
        if (k === "path" && pr.value.type === "StringLiteral") p = pr.value.value;
        else if (k === "method" && pr.value.type === "StringLiteral" &&
                 _REAL_VERBS.has(String(pr.value.value).toUpperCase())) verb = true;
        else if (_ROUTE_SIB.test(k)) sib = true;
      }
      if (p === null) return;
      // An index route (`path: ""`) resolves to its parent; that is a real destination, not a blank.
      const full = "/" + [...parentPath(path), p]
        .join("/").split("/").filter(Boolean).join("/");
      if (full.length < 2) return;
      const t = _canonPath(full);
      if (sib) return emit(t, "router-config");
      // Sibling-less. Only a path that could be a URL qualifies, and only from an object small enough to be
      // a route descriptor rather than a settings blob that happens to carry a `path`.
      if (!/^\//.test(p) || nprops > 4) return;
      if (_NOT_A_ROUTE.test(p)) return;
      // `{path:"/v1/onramp/quote", method:"POST"}` is an API call descriptor, not a client-side route. It
      // was landing here as a "route", which is the one mislabel that could do harm: a crawler that skips
      // routes because they never reach the server would then skip real endpoints. A verb settles it,
      // client routes do not carry an HTTP method.
      if (verb) return;
      bare.push(t);
    } catch {}
  } });

  if (bare.length >= BARE_ROUTE_MIN) for (const t of bare) emit(t, "router-config-bare");
  return out;
}

// Below this many sibling-less `{path:"/…"}` objects in one file, they are config values rather than a
// route table, and emitting them would invent routes. Above it, the table is the simplest explanation.
const BARE_ROUTE_MIN = 3;

// Things spelled like a path that are not a route: a filesystem location, a bundled asset, an npm specifier.
const _NOT_A_ROUTE = /^\/(usr|etc|var|tmp|home|dev|proc|sys|opt|bin|sbin|lib|mnt|root|node_modules)\/|\.(js|mjs|cjs|ts|tsx|jsx|css|scss|png|jpe?g|svg|gif|webp|avif|ico|woff2?|ttf|eot|map|json|xml|txt|pdf|mp4|webm|wasm)$|\s/;

// Path-shaped string constants the sinks never claimed. Deliberately high-recall and marked low-confidence:
// these are candidates to probe, not proven endpoints.
// Three ecosystems, three ways to write the same parameterised route:
//   Express / React Router   /users/:id
//   Next.js                  /users/[id]   /blog/[...slug]   /shop/[[...path]]
//   OpenAPI / our own output /users/{id}
// Emitting all three verbatim means the same endpoint appears as several distinct records, dedupe never
// fires, and a diff between two sources of the same route reports a false difference. Everything is
// canonicalised to the {name} form on the way out. Only a bracket that owns a WHOLE path segment is a
// parameter, `labelNames[]=x` in a query string and `arr[0]` indexing must survive untouched.
const _NEXT_SEG = /\/\[{1,2}(?:\.{3})?([A-Za-z0-9_]+)\]{1,2}(?=\/|$)/g;
const _EXPRESS_SEG = /\/:([A-Za-z0-9_]+)/g;
// TanStack Router / Remix file-system route strings, which X and a growing number of apps register their
// routes with: t(`/$username/_profile/articles`). Two conventions, and BOTH have to be applied or what
// comes out is a string that can never be requested:
//
//   $name   a dynamic segment                     -> {name}
//   _name   a PATHLESS LAYOUT route. It groups children and contributes NOTHING to the URL.
//
// so that route is /{username}/articles. Emitting the raw form gave x.com 378 "endpoints" that were route
// TREE node names, /$username/_profile/articles, /$username/_followers/verified_followers, none of them
// a path any client ever sends.
//
// Gated on a $-segment being present in the input, because a leading-underscore segment is perfectly legal
// in a real URL, /_next/static/... is one of the most common paths on the web, and only the file-system
// route syntax is evidence that this string is a route tree rather than a path.
const _FS_PARAM = /\/\$([A-Za-z_]\w*)/g;
// A BARE `$` segment is TanStack's splat: `/design/$` matches /design/ and everything under it. It is not a
// regex anchor, though it reads exactly like one, `_NOTPATH` rejects a trailing `$` for that reason, and
// route records do not pass through that filter, so five claude.ai routes came out as `/design/$`,
// `/memory/$`, `/desktop/connected/$`. Unrequestable as written, and a wildcard once rendered.
const _FS_SPLAT = /\/\$(?=\/|$)/g;
const _FS_LAYOUT = /\/_[A-Za-z][\w-]*(?=\/|$)/g;
// Evidence can come from the STRING or from the FILE. `/_full-width/i/chat` is a layout route with no $
// segment of its own, so the per-string test leaves it alone and emits a path X never serves. A file that
// registers `/$username/...` routes is using the file-system syntax throughout, and that is the same
// file-level-evidence idiom `_SOCK_EVIDENCE` uses: judge an ambiguous shape by what else the file contains
// rather than by the shape alone. A Next.js bundle shipping /_next/... has no $-routes anywhere in it, so
// it never qualifies and its underscore paths stay untouched.
const _FS_EVIDENCE = /[`"'(\/]\/\$[A-Za-z_]\w*(?=[\/`"')])/;
const _fsRoute = (s, fileHasFsRoutes) => {
  if (!/\/\$[A-Za-z_]/.test(s) && !fileHasFsRoutes) return s;
  // Rewrite only. NEVER prepend a slash: this line used to read `out.startsWith("/") ? out : "/" + out`,
  // which was harmless while the rule was gated per-string (a string containing `/$` already had one) and
  // became a path FABRICATOR the moment the gate went file-level. Every string in a qualifying file got a
  // leading slash, so x.com gained 1,649 "endpoints" that were CSS custom properties (--sticky-header-height),
  // CSS values (-9999px) and module specifiers (../messages/en.json). A string that is not a path must come
  // out not a path, and be dropped by the checks below like any other.
  return s.replace(_FS_PARAM, "/{$1}").replace(_FS_SPLAT, "/*").replace(_FS_LAYOUT, "");
};
const _canonPath = (s, fileHasFsRoutes) =>
  _fsRoute(String(s), fileHasFsRoutes).replace(_NEXT_SEG, "/{$1}").replace(_EXPRESS_SEG, "/{$1}");

const MAX_PATH_LITERALS = 25000;
const _ASSETY = /\.(js|mjs|cjs|css|png|jpe?g|gif|svg|ico|webp|avif|woff2?|ttf|eot|otf|map|mp4|webm|mp3|wav|pdf|wasm|zip|csv)$/i;
// regexes, CSS, windows paths, URLs, plus two shapes that survived and are provably not requestable:
// XPath axes (/descendant::$1, /child::$1) and local filesystem paths (/usr/local/bin/claude), both of
// which are ordinary string constants in real bundles and both of which look exactly like URL paths.
const _NOTPATH = /[\s<>()\[\]"'`\\|@]|^\/\/|\/{3}|^\/[A-Z]:|^\/\^|\$$|::|^\/(usr|etc|opt|proc|dev)\/(bin|lib|local|share|sbin|include)\/|^\/(Users|home)\/[\w.-]+\/(Desktop|Documents|Library|\.\w)/;
function collectPathLiterals(ast, claimed, src) {
  const seen = new Set(), out = [];
  const fileHasFsRoutes = _FS_EVIDENCE.test(String(src || ""));
  const consider = (raw, node, asRoute) => {
    if (typeof raw !== "string" || raw.length < 4 || raw.length > 200) return;
    // A URL cannot contain a control character. Minified bundles carry binary-ish string data, packed
    // tables, base64 with stray bytes, and a handful matched the path shape and came out as endpoints:
    // `/.04\u0013\u0002`, `/B_I+\u0010&57N`. Printed into the site map they also made the map itself a
    // binary file to anything that classifies by content, so grep over it returned nothing rather than an
    // error, exactly the trap this repo hit in its own extractor source.
    // eslint-disable-next-line no-control-regex
    if (/[\u0000-\u001f\u007f]/.test(raw)) return;
    // Canonicalise BEFORE filtering. The reject-list excludes square brackets to drop CSS selectors and
    // regex fragments, which also silently discarded every Next.js dynamic route (/[lang]/privacy), the
    // single most common route syntax in the corpus. Once [lang] has become {lang} the filter is safe.
    const v = _canonPath(raw, fileHasFsRoutes);
    if (_NOTPATH.test(v) || _ASSETY.test(v)) return;
    // A path whose BASE is a variable is still a path: `${apiBase}/orgs/${id}/plans/active` is one of
    // the most common shapes in real code, and demanding a literal leading "/" discarded every one of them.
    // An ABSOLUTE url is likewise a request the app makes, and it is the form a bundler PRODUCES when it
    // inlines an env-var base at build time, so rejecting it loses exactly the endpoints that were easiest
    // to resolve (href:"https://www.client.example/public/docs/Terms").
    const skeleton = v.replace(/\{[^}]*\}/g, "");
    const isAbs = /^(https?|wss?):\/\/[^/\s]+\/[^\s]/i.test(v);
    const startsSlot = v[0] === "{" && skeleton[0] === "/";
    if (!isAbs && v[0] !== "/" && !startsSlot) return;
    // require a real literal segment in the PATH portion, so bare origins are not mistaken for endpoints
    const pathPart = isAbs ? skeleton.replace(/^[a-z]+:\/\/[^/]*/i, "") : skeleton;
    if (!/\/[A-Za-z0-9_.~-]{2,}/.test(pathPart)) return;
    // `:name` is an Express path param only when it OWNS the segment (/users/:id). Google-style custom
    // methods put a colon mid-segment (/v1/accounts:signUp), and treating those as params collapsed all 12
    // Firebase auth endpoints into one bucket, which the dedupe then threw away.
    // A CLIENT-SIDE ROUTE IS NOT AN ABSOLUTE URL. The sibling test that promotes a literal to a route
    // is "the object declaring this URL also names the view that renders it", and a link table sitting
    // beside a component satisfies it, so Supabase's testimonial wall, `https://x.com/<user>/status/
    // <id>` next to the card that renders it, produced 51 of its 53 "routes". This app's router cannot
    // render x.com. An absolute URL stays recorded as a path-literal, which keeps it in the corpus
    // without claiming it as surface, and `-f urls` and `-f table` stop listing other people's tweets.
    if (asRoute && isAbs) asRoute = false;
    const norm = v.replace(/\$\{[^}]*\}|\{[^}]*\}/g, "*");
    // A ROUTE SEEN SECOND STILL OUTRANKS A PATH-LITERAL SEEN FIRST. The same URL usually appears twice,
    // once in a nav/link table and once in the router manifest, and whichever the traversal reached first
    // decided the type. Only the manifest occurrence carries the sibling that PROVES it is a page, so let
    // it upgrade the record rather than be discarded as a duplicate of the weaker sighting.
    if (seen.has(norm)) {
      if (asRoute) {
        for (const rec of out) {
          if (rec._kind === "path-literal" && String(rec.template || "").replace(/\$\{[^}]*\}|\{[^}]*\}/g, "*") === norm) {
            rec._kind = "route"; rec.confidence = "high";
            rec.note = "client-side route: the object declaring this URL also names the view that renders it";
            break;
          }
        }
      }
      return;
    }
    // Suppress a literal ONLY when it is genuinely the same request already recorded: either an exact match,
    // or the claimed record is the absolute form of this same path (https://api.x/v1/foo vs /v1/foo).
    // Plain suffix matching in either direction is a coincidence, not a duplicate, `/payments/*` being
    // claimed does NOT mean `/orgs/*/payables/vendors/*/payments/*` is the same endpoint. That one
    // over-broad test was silently eating real paths, and it collided far more inside a minified bundle
    // (where a single file holds every claimed template) than across many small original source files.
    for (const c of claimed) if (c === norm || (c.includes("://") && c.endsWith(norm))) return;
    seen.add(norm);
    out.push(asRoute
      ? { _kind: "route", method: "UNKNOWN", confidence: "high", template: v,
          note: "client-side route: the object declaring this URL also names the view that renders it" }
      : { _kind: "path-literal", method: "UNKNOWN", confidence: "low", template: v });
  };
  // A cap that silently drops the tail is the same bug as every other silent truncation in this tool: one
  // Nuxt chunk produced 4038 literals against a 4000 cap and lost a real OAuth endpoint off the end, with
  // nothing in the output to say so. The ceiling is now well above what real files produce, and reaching it
  // emits a record rather than passing in silence.
  let capped = false;
  // react-intl / formatjs message IDs. `{id: "/hFOCx", defaultMessage: "Option"}`, the generated hash
  // sometimes starts with a slash, and 45 of one target's "endpoints" were translation keys: /h2oPn, /pWSBh,
  // /iEWO1. The signature is unambiguous, so this costs nothing: the string is the value of an `id`
  // property in an object that ALSO carries `defaultMessage`. Nothing but formatjs looks like that.
  const _isIntlMessageId = (p) => {
    const prop = p.parentPath;
    if (!prop || !prop.node || prop.node.type !== "ObjectProperty" || prop.node.value !== p.node) return false;
    const key = prop.node.key && (prop.node.key.name ?? prop.node.key.value);
    if (key !== "id") return false;
    const obj = prop.parentPath;
    if (!obj || !obj.node || obj.node.type !== "ObjectExpression") return false;
    return obj.node.properties.some((q) => q.type === "ObjectProperty" && q.key &&
                                    (q.key.name ?? q.key.value) === "defaultMessage");
  };
  // CLIENT ROUTES ARE NOT ENDPOINTS, and the object says which it is.
  //
  // `{url:"/app/records/drafts", chunkName:"dashboards"}` is a router's code-splitting table, a PAGE
  // in the SPA. Emitted as an untyped path-literal it became a candidate API route, and because the base
  // was unresolved it was then attributed to whichever host happened to be guessed: 18 of one target's 199
  // "API" candidates on the gateway host were its own dashboard pages.
  //
  // The distinguishing fact is a sibling, not the string: a URL declared next to the VIEW that renders it
  // is a route. Same shape as _isIntlMessageId above, and it costs nothing when the sibling is absent.
  const _ROUTE_SIBLINGS = new Set(["chunkname", "component", "element", "lazy", "loader",
                                   "page", "view", "screen", "layout", "handle"]);
  const _isClientRoute = (p) => {
    const prop = p.parentPath;
    if (!prop || !prop.node || prop.node.type !== "ObjectProperty" || prop.node.value !== p.node) return false;
    const key = String(prop.node.key && (prop.node.key.name ?? prop.node.key.value) || "").toLowerCase();
    if (key !== "url" && key !== "path" && key !== "to" && key !== "href") return false;
    const obj = prop.parentPath;
    if (!obj || !obj.node || obj.node.type !== "ObjectExpression") return false;
    return obj.node.properties.some((q) => q.type === "ObjectProperty" && q.key &&
      _ROUTE_SIBLINGS.has(String(q.key.name ?? q.key.value ?? "").toLowerCase()));
  };
  traverse(ast, {
    StringLiteral(p) {
      if (_isIntlMessageId(p)) return;
      // A CONCATENATED PREFIX IS PART OF THE PATH.
      //
      // `h + "/records/drafts"` where `h="/home"` is one string, and emitting the right operand alone
      // produced `/records/drafts`, a path that does not exist, sitting next to the real one. The
      // prefix is a plain binding in scope; not reading it invented an endpoint out of half of a real one.
      let val = p.node.value;
      const par = p.parentPath && p.parentPath.node;
      if (par && par.type === "BinaryExpression" && par.operator === "+" && par.right === p.node &&
          val[0] === "/" && par.left) {
        let pre = null;
        if (par.left.type === "StringLiteral") pre = par.left.value;
        else if (par.left.type === "Identifier") {
          try {
            const b = p.scope.getBinding(par.left.name);
            const init = b && b.path && b.path.node && b.path.node.init;
            if (init && init.type === "StringLiteral") pre = init.value;
          } catch {}
        }
        // Only a PATH prefix. An origin is the base-resolution problem and is handled elsewhere; a random
        // string would fabricate a path rather than complete one.
        if (pre && pre[0] === "/" && !pre.endsWith("/") && pre.length < 60) val = pre + val;
      }
      if (out.length < MAX_PATH_LITERALS) consider(val, p.node, _isClientRoute(p));
      else capped = true;
    },
    TemplateLiteral(p) {
      if (out.length >= MAX_PATH_LITERALS) { capped = true; return; }
      const n = p.node;
      let s = "";
      for (let i = 0; i < n.quasis.length; i++) {
        s += n.quasis[i].value.cooked ?? n.quasis[i].value.raw ?? "";
        // emit slots in the SAME {name} form the rest of the extractor uses. Emitting `${name}` here and
        // repairing it with a regex afterwards left odd names (`${$}`) unrepaired, so the identical path
        // recovered from minified vs original source compared as two different templates.
        if (i < n.expressions.length) {
          const e = n.expressions[i];
          const nm = e.type === "Identifier" ? e.name
                   : e.type === "MemberExpression" && e.property && e.property.name ? e.property.name
                   : "EXPR";
          s += "{" + nm + "}";
        }
      }
      consider(s, n);
    },
  });
  if (capped) out.push({ _kind: "truncation", where: "path-literals", limit: MAX_PATH_LITERALS,
                         note: "path-literal harvest hit its cap; some literals in this file were not emitted" });
  return out;
}

// API-GATEWAY routing table, a config array of [prefix, serviceLabel] pairs mapping request prefixes
// to backend services (`[["/services/graphql","graphql"],["/services/billing","billing"],...]`). Worth
// collecting because this is app CONFIG rather than library code, which is exactly why the real
// endpoint path is never produced by any data-layer adapter: the prefix lives here and nowhere else.
function collectGatewayRoutes(ast) {
  const out = [];
  traverse(ast, { ArrayExpression(path) {
    try {
      const pairs = [];
      for (const e of (path.node.elements || [])) {
        if (e && e.type === "ArrayExpression" && e.elements.length >= 2 &&
            e.elements[0] && e.elements[0].type === "StringLiteral" && /^\/[a-z]/i.test(e.elements[0].value) &&
            e.elements[1] && e.elements[1].type === "StringLiteral")
          pairs.push({ path: e.elements[0].value, service: e.elements[1].value });
      }
      if (pairs.length >= 2) out.push(...pairs);          // ≥2 [path,label] pairs = a routing table, not coincidence
    } catch {}
  } });
  return out;
}

// GraphQL client endpoint URI, new HttpLink({uri}) / createHttpLink / createUploadLink / BatchHttpLink /
// ApolloClient({uri}) / createClient({url}). The uri is app CONFIG (not in Apollo/urql itself), often the real
// /services/graphql path a data-layer adapter can't produce. Resolved to a template.
function collectGraphqlEndpoints(ast) {
  const out = [];
  const isLink = (nm) => /^(HttpLink|BatchHttpLink|createHttpLink|createUploadLink|createHttpBatchLink|ApolloClient|createClient|SSELink|GraphQLClient)$/.test(nm || "");
  const visit = (node, scope) => {
    const c = node.callee;
    const nm = c && (c.name || (c.property && c.property.name));
    if (!isLink(nm)) return;
    // graphql-request: `new GraphQLClient(endpoint)`, endpoint is a POSITIONAL string (not a {uri} property).
    if (nm === "GraphQLClient" && node.arguments[0]) {
      const u = resolveUrl(node.arguments[0], scope);
      if (u.template && u.template !== "{EXPR}")
        // `via` is set HERE and not defaulted at the emit site: the dedupe below ranks by it, and an
        // undefined via ranked below `uri-path`, so a recognisably-named client lost to the weaker
        // path-shaped evidence for the same endpoint.
        out.push({ uri: u.template, dynamic: u.dynamic, via: "client-name" });
    }
    for (const arg of (node.arguments || [])) {
      if (!arg || arg.type !== "ObjectExpression") continue;
      for (const p of arg.properties) {
        if (p.type !== "ObjectProperty" || !p.key) continue;
        const k = p.key.name ?? p.key.value;
        if (k === "uri" || k === "url") {
          const u = resolveUrl(p.value, scope);
          if (u.template && u.template !== "{EXPR}")
        // `via` is set HERE and not defaulted at the emit site: the dedupe below ranks by it, and an
        // undefined via ranked below `uri-path`, so a recognisably-named client lost to the weaker
        // path-shaped evidence for the same endpoint.
        out.push({ uri: u.template, dynamic: u.dynamic, via: "client-name" });
        }
      }
    }
  };
  traverse(ast, {
    CallExpression(path) { try { visit(path.node, path.scope); } catch {} },
    NewExpression(path) { try { visit(path.node, path.scope); } catch {} },
  });

  // NAME-AGNOSTIC PASS. Everything above keys off the constructor's NAME, and minification erases it.
  // Measured across five targets: 16 of 18 link-shaped constructions had a name no regex could match.
  // `new cJe({uri:`${ym}/graphql`, cache:…})` IS ApolloClient and `new ZQe({uri:t})` IS createUploadLink,
  // so isLink() fired on neither, no endpoint record was emitted, and twenty's entire GraphQL surface,
  // 925 operations, came out of analysis with no address at all.
  //
  // Widening the gate to "any object literal with a uri property" is WRONG, and measurably so. Of those
  // 16, four are HLS playlist segments (`new f({uri:r, bandwidth, codecs, frameRate})`) and one is a
  // VS Code workspace folder (`new ih.mX({uri:e, name:"", index:0})`). Shape cannot tell a GraphQL link
  // from a video segment, and inventing five GraphQL endpoints is worse than missing one.
  //
  // What CAN tell them apart is the URI's own value. A path whose last segment is `graphql` is a GraphQL
  // endpoint declaration whatever constructed it, the evidence is in the value, not in the name. That
  // admits twenty's one real site and none of the five false ones.
  const cand = [];
  traverse(ast, {
    ObjectProperty(path) {
      try {
        const k = path.node.key && (path.node.key.name ?? path.node.key.value);
        if (k !== "uri" && k !== "url" && k !== "endpoint") return;
        const u = resolveUrl(path.node.value, path.scope);
        if (!u || !u.template) return;
        const t = originOnlySlot(u.template);
        if (!t) return;
        cand.push({ full: u.template, path: t, dynamic: !!u.dynamic, reduced: t !== u.template });
      } catch {}
    },
  });

  // SIBLING ENDPOINTS OF THE SAME SERVER, which is what `len(gql_gw) == 1` downstream depends on being
  // right about. GQL_PATH only recognises a path SPELLED graphql, and a server is free to put a second
  // schema somewhere else: twenty's one bundle constructs three clients, `${ym}/graphql`,
  // `${ym}/metadata` and `${ym}/admin-panel`, and only the first is spelled for it. So the collector
  // reported ONE endpoint, the "exactly one declared, apply it to everything" branch in ingest fired, and
  // 925 operations were stamped /graphql, wrong for the 246 that live at /metadata. Both siblings answer
  // GraphQL introspection for real: `{__typename}` returns `{"data":{"__typename":"Query"}}` on each.
  //
  // A candidate qualifies as a sibling when it shares the EXACT unresolved origin of one that IS spelled
  // graphql. That is a narrow test: same file, same base expression, same construction shape. It is still
  // an inference, so it is labelled `sibling-of-graphql` rather than presented as a read endpoint, and
  // its purpose downstream is to make the ambiguity refusal fire and to NAME the other candidates, not to
  // be chosen between. Naming `/admin-panel` is the difference between a map that knows a third schema
  // exists and one that does not.
  const originOf = (c) => {
    const m = /^(\{[^}]*\})\//.exec(c.full);
    return m ? m[1] : null;
  };
  const gqlish = cand.filter((c) => GQL_PATH.test(c.path));
  const gqlOrigins = new Set(gqlish.map(originOf).filter(Boolean));
  for (const c of gqlish)
    out.push({ uri: c.path, dynamic: c.dynamic || c.reduced, via: "uri-path" });
  if (gqlOrigins.size) {
    for (const c of cand) {
      if (GQL_PATH.test(c.path)) continue;
      const o = originOf(c);
      // A sibling must be path-shaped and must not be an asset or a template hole.
      if (!o || !gqlOrigins.has(o)) continue;
      if (!/^\/[A-Za-z0-9._~\-\/]*$/.test(c.path) || /\.(js|css|png|svg|json|map)$/i.test(c.path)) continue;
      // A SEPARATE KIND, because the count of siblings must NOT become a count of GraphQL endpoints.
      // Of twenty's four, three answer GraphQL and `/rest` is its REST API, nothing static separates
      // them. So feeding these into the "how many endpoints are declared" test would refuse to address
      // any operation on a perfectly ordinary app that ships one GraphQL client and one upload URL,
      // which is the 309-unaddressed-operation gap all over again. They travel as information: named in
      // the note so the agent knows where else to look, never as a veto.
      out.push({ sibling: true, uri: c.path, dynamic: c.dynamic || c.reduced, via: "sibling-endpoint" });
    }
  }

  // Both passes see a client that is BOTH recognisably named and pointed at a graphql path, so the same
  // endpoint arrives twice with two different provenances. The caller dedupes by uri and keeps whichever
  // came last, which silently downgraded `client-name` to `uri-path`. Named evidence is the stronger of
  // the two, a constructor the app calls `HttpLink` is a GraphQL link whatever its path, so it wins.
  const RANK = { "client-name": 3, "uri-path": 2, "sibling-endpoint": 1 };
  const best = new Map();
  for (const e of out) {
    const prev = best.get(e.uri);
    if (!prev || (RANK[e.via] || 0) > (RANK[prev.via] || 0)) best.set(e.uri, e);
  }
  return [...best.values()];
}

// A path whose last segment names a GraphQL endpoint. Self-evidencing, it needs no constructor name.
const GQL_PATH = /(^|\/)(graphql|graphiql|gql)(\/|\?|$)/i;

// Reduce a partly-resolved template to the part that IS resolved, but ONLY when the unresolved part is
// the ORIGIN. twenty's `${ym}/graphql` resolves to `{min}/graphql`: `ym` is imported from another chunk,
// so the HOST is a runtime value while the PATH is a constant. The path is the half worth recording,
// the store keys endpoints per host and resolves the host from the wire, whereas `{min}/graphql` is not
// an address anyone can send.
//
// A slot anywhere OTHER than the origin is not reducible: `/api/{EXPR}/graphql` would become `/graphql`,
// which is an endpoint the app never declares. Returning the template untouched there is the honest
// answer, and GQL_PATH still matches it.
function originOnlySlot(t) {
  if (!t.includes("{")) return t;
  const m = /^(\{[^}]*\})(\/[^{]*)$/.exec(t);
  if (!m) return t;
  // A LEADING SLOT IS NOT AUTOMATICALLY AN ORIGIN, and this used to assume it was. `strFromNode` labels
  // its slots precisely enough to tell them apart, and that label is the only evidence available: a
  // `{param:tenant}` or `{call:getPrefix}` at the front is a PATH PREFIX, so deleting it turns
  // `{param:tenant}/graphql` into a root-level `/graphql` the bundle never declares, a fabricated
  // address, which this codebase treats as worse than an unresolved one.
  //
  // `{EXPR}` and `{min}` mean "we could not read this at all" and "the minifier ate the name", which is
  // what an imported base URL looks like, twenty's `${ym}` is exactly that. A name that reads like a
  // base URL is also admitted. Everything else keeps its slot and stays honest; GQL_PATH still matches.
  const label = m[1].slice(1, -1);
  const originish = label === "EXPR" || label === "min"
    || /^(base|origin|host|url|uri|api|endpoint|server|gateway|root)/i.test(label)
    || /(url|uri|base|origin|host|endpoint)$/i.test(label);
  return originish ? m[2] : t;
}

// PLAIN-STRING GraphQL queries, graphql-request `client.request(\`query ...\`)` / `request(url, \`...\`)`, or any
// client handed a raw SDL string (NOT gql-compiled → collectApollo can't see it). Extract op name + full text from
// the string. (Apollo/urql/codegen compile to DocumentNodes and are already covered by collectApollo.)
function collectGraphqlString(ast) {
  const out = [], seen = new Set();
  const strOf = (node) => {
    if (!node) return null;
    if (node.type === "StringLiteral") return node.value;
    if (node.type === "TemplateLiteral" && node.expressions.length === 0)
      return node.quasis.map((q) => (q.value && (q.value.cooked ?? q.value.raw)) || "").join("");
    return null;
  };
  const isGql = (s) => typeof s === "string" && /^\s*(query|mutation|subscription|fragment)\b|^\s*\{[\s\S]*\}\s*$/.test(s.trim());
  traverse(ast, {
    CallExpression(path) {
      const c = path.node.callee;
      const nm = c && (c.name || (c.property && c.property.name));
      if (nm !== "request") return;                          // request(...) or client.request(...)
      for (const a of path.node.arguments) {
        const s = strOf(a);
        if (!s || !isGql(s)) continue;
        const t = s.trim();
        const m = t.match(/\b(query|mutation|subscription)\s+([A-Za-z_]\w*)/);
        const op = m ? m[2] : "anonymous";
        const kind = m ? m[1] : (t.match(/\b(query|mutation|subscription)\b/) || [, "query"])[1];
        const key = op + "|" + t.slice(0, 40);
        if (seen.has(key)) continue;
        seen.add(key);
        out.push({ op, kind, text: t });
      }
    },
  });
  // A GraphQL document is recognisable from its own text; where it sits in the file is irrelevant.
  // The pass above reads only DIRECT arguments of a call named `request`, so it sees
  // `client.request("query X {...}")` and nothing else. Braintree's SDK ships
  // `t.request({api:"graphQLApi", data:{query:"mutation DeletePaymentMethodFromSingleUseToken($input:...)"}})`
  //, the document is two levels inside an object literal and was invisible. So is every
  // `fetch("/graphql", {body: JSON.stringify({query: "..."})})`, which is the commonest transport there is.
  //
  // So sweep every string literal too. The bar is deliberately stricter here than `isGql`: a global sweep
  // cannot use its "starts with { and ends with }" alternative, which matches JSON blobs, CSS and template
  // fragments. An operation keyword followed by a name and a bracket is unambiguous, nothing else ships
  // `mutation SomeName(` inside a string, so that alone is the admission test.
  const NAMED_OP = /^\s*(query|mutation|subscription)\s+([A-Za-z_]\w*)\s*[({]/;
  traverse(ast, {
    StringLiteral(path) {
      try {
        const t = String(path.node.value || "").trim();
        if (t.length < 12 || t.length > 20000) return;
        const m = NAMED_OP.exec(t);
        if (!m) return;
        const key = m[2] + "|" + t.slice(0, 40);
        if (seen.has(key)) return;
        seen.add(key);
        out.push({ op: m[2], kind: m[1], text: t });
      } catch {}
    },
  });
  return out;
}

// PERSISTED-QUERY TABLES. A GraphQL client that uses persisted queries does not send the document, it
// sends an id the server already knows, so the request on the wire is
// `/i/api/graphql/<queryId>/<OperationName>` and the DOCUMENT appears nowhere in the bundle. Every
// collector in this file that recovers GraphQL works from the document text, so all of them are blind to
// this entire API by construction.
//
// The table itself does ship, as a plain array of {queryId, operationName, operationType} objects. On x
// that is 289 operations across 13 files against the 39 the document-based collectors found, and the 250
// missing ones include the whole write surface, CreateTweet, DeleteTweet, FavoriteTweet, CreateDm.
//
// The id is not decoration: without it the endpoint cannot be called at all, because the path contains it.
// So it is recorded on the record rather than dropped, and the template is built from both parts.
// THE CLIENT'S ENCODING RULE, read rather than guessed.
//
// `client.post("/auth/session/redeem", {code: f})` says nothing about Content-Type, and the usual answer,
// "axios defaults to JSON", is a guess about which library this is. It does not have to be. The rule is
// written in the client's own request builder, and that is where it can be read:
//
//     // shape only, a request builder that decides the encoding from the body's type
//     buildOptions(body, headers) {
//       if (!(body instanceof FormData)) headers["Content-Type"] = "application/json";
//       return { ...headers, body: body instanceof FormData ? body : JSON.stringify(body) };
//     }
//
// That is not a default, it is a decision: JSON unless the caller hands it a FormData. Every one of
// one target's 195 unresolved write bodies is answered by those two lines.
//
// So: find the functions that actually call fetch() and decide a body, and report the rule with the
// evidence. This does NOT attach the rule to call sites, the link from `L().post` to this class is
// cross-module and unproven, it records what the application's HTTP layer does, which is a fact about the
// app and is exactly what a reader needs to interpret every `body[?]` row.
function collectClientEncoding(ast) {
  const out = [], seen = new Set();
  const enclosing = (path) => {
    let p = path;
    while (p) {
      const n = p.node;
      // ClassMethod and ObjectMethod are function-like but their type does not contain "Function", so a
      // /Function/ test walks straight past a class method and reports no name for it, which is most of
      // the real cases, since a client is usually a class.
      if (n && n.type === "ClassMethod" && n.key) return n.key.name ?? n.key.value;
      if (n && n.type === "ObjectMethod" && n.key) return n.key.name ?? n.key.value;
      if (n && /Function/.test(n.type)) {
        if (n.id && n.id.name) return n.id.name;
        const par = p.parentPath && p.parentPath.node;
        if (par && par.type === "ObjectProperty" && par.key) return par.key.name ?? par.key.value;
        if (par && par.type === "ObjectMethod" && par.key) return par.key.name ?? par.key.value;
        if (par && par.type === "ClassMethod" && par.key) return par.key.name ?? par.key.value;
        if (par && par.type === "VariableDeclarator" && par.id && par.id.name) return par.id.name;
        return null;
      }
      p = p.parentPath;
    }
    return null;
  };
  // Anchored on the SHAPE of a request-options object, not on fetch being the next token. A real client
  // builds `{method, headers, body}` in one function and hands it to fetch in another, one target's
  // createRequestOptions is called as `this.createRequestOptions(...)`, so a fetch-anchored scan sees a
  // CallExpression it cannot resolve and finds nothing. An object carrying `body` alongside `method` or
  // `headers` IS a request being built, wherever it is.
  const _optsObj = (obj) => {
    if (!obj || obj.type !== "ObjectExpression") return null;
    const keys = obj.properties.filter((p) => p.type === "ObjectProperty" && p.key)
                               .map((p) => p.key.name ?? p.key.value);
    if (!keys.includes("body")) return null;
    if (!keys.includes("method") && !keys.includes("headers")) return null;
    return obj.properties.find((p) => p.type === "ObjectProperty" && p.key &&
                               (p.key.name ?? p.key.value) === "body");
  };
  traverse(ast, { ObjectExpression(path) {
    try {
      const obj = path.node;
      const bodyProp = _optsObj(obj);
      if (!bodyProp) return;
      // What does this builder do with the body it was HANDED? A literal object is a call site, not a
      // client; a client stringifies or branches on the value's type.
      let rule = bodyEncoding(bodyProp.value, path.scope);
      const src = generateSrc(bodyProp.value);
      const branches = /instanceof\s+FormData/.test(generateSrc(obj) || "");
      if (!rule && /JSON\.stringify/.test(src || "")) rule = "json";
      if (rule === "json" && branches) rule = "json-unless-multipart";
      if (!rule) return;
      const fname = enclosing(path);
      const key = (fname || "?") + "|" + rule;
      if (seen.has(key)) return;
      seen.add(key);
      out.push({ _kind: "client-encoding", name: fname, rule,
                 note: "the app's HTTP layer builds its body this way: " + String(src || "").slice(0, 120) +
                       (branches ? " (and branches on `instanceof FormData`)" : "") });
    } catch {}
  } });
  // The ASSIGNMENT form, which is what a real client does: build `{method, headers}` first, decide the body
  // after. one target's builder is exactly this, `const s={method:e,headers:a}; … s.body=JSON.stringify(r)`,
  // so requiring `body` in the object literal found nothing in the file that answers the question.
  traverse(ast, { AssignmentExpression(path) {
    try {
      const L = path.node.left;
      if (!L || L.type !== "MemberExpression" || L.computed) return;
      if (!L.property || (L.property.name ?? L.property.value) !== "body") return;
      if (!L.object || L.object.type !== "Identifier") return;
      const b = path.scope && path.scope.getBinding && path.scope.getBinding(L.object.name);
      const init = b && b.path && b.path.node && b.path.node.init;
      if (!init || init.type !== "ObjectExpression") return;
      const keys = init.properties.filter((p) => p.type === "ObjectProperty" && p.key)
                                  .map((p) => p.key.name ?? p.key.value);
      if (!keys.includes("method") && !keys.includes("headers")) return;      // not a request being built
      const src = generateSrc(path.node.right) || "";
      let rule = bodyEncoding(path.node.right, path.scope);
      if (!rule && /JSON\.stringify/.test(src)) rule = "json";
      if (!rule) return;
      // Is this assignment the ELSE of a FormData test? Then the rule is conditional, not flat:
      //     r instanceof FormData ? s.body = r : s.body = JSON.stringify(r)
      // Read the enclosing conditional rather than regenerating the whole function's source, on a minified
      // bundle that is a megabyte of codegen per call site, and it was throwing into the catch below, which
      // is why the real client came back with nothing while a toy fixture worked.
      let cond = path.parentPath, depth = 0;
      while (cond && depth++ < 4 && cond.node && cond.node.type !== "ConditionalExpression")
        cond = cond.parentPath;
      const test = cond && cond.node && cond.node.type === "ConditionalExpression" ? cond.node.test : null;
      const testsFormData = !!(test && test.type === "BinaryExpression" && test.operator === "instanceof" &&
                               test.right && test.right.name === "FormData");
      if (rule === "json" && testsFormData) rule = "json-unless-multipart";
      const fname = enclosing(path);
      const key = (fname || "?") + "|" + rule;
      if (seen.has(key)) return;
      seen.add(key);
      out.push({ _kind: "client-encoding", name: fname, rule,
                 note: "the app's HTTP layer builds its body this way: " + src.slice(0, 120) +
                       (rule === "json-unless-multipart"
                        ? ": multipart when the caller passes a FormData, JSON otherwise" : "") });
    } catch {}
  } });
  return out;
}

function collectPersistedQueries(ast) {
  const out = [], seen = new Set();
  const strOf = (v) => (v && v.type === "StringLiteral" ? v.value : null);
  traverse(ast, { ObjectExpression(path) {
    try {
      let qid = null, opName = null, opType = null;
      for (const p of path.node.properties) {
        if (p.type !== "ObjectProperty" || !p.key) continue;
        const k = p.key.name ?? p.key.value;
        if (k === "queryId") qid = strOf(p.value);
        else if (k === "operationName") opName = strOf(p.value);
        else if (k === "operationType") opType = strOf(p.value);
      }
      if (!qid || !opName) return;
      if (!/^[A-Za-z0-9_-]{8,}$/.test(qid) || !/^[A-Za-z_]\w*$/.test(opName)) return;
      const key = opName + "|" + qid;
      if (seen.has(key)) return;
      seen.add(key);
      const kind = /mutation/i.test(opType || "") ? "mutation"
                 : /subscription/i.test(opType || "") ? "subscription" : "query";
      out.push({
        _kind: "relay", op: opName, opkind: kind, query_id: qid,
        method: kind === "query" ? "GET" : "POST",
        // The id IS part of the path. A row without it names an operation nobody can call.
        template: "/i/api/graphql/" + qid + "/" + opName,
        source: "persisted-query-table",
        note: "persisted GraphQL operation: the document is not shipped, the server resolves queryId " +
              qid + ". Variables are not recoverable from the bundle for this shape.",
      });
    } catch {}
  } });
  return out;
}

// Firebase / Firestore: the wire URLs are generic googleapis endpoints (same for EVERY app → noise), so we recover
// only the app-SPECIFIC, actionable bits: the projectId (from initializeApp config) and the COLLECTION names
// (from collection("x")/doc(db,"x",…) literals) → per-collection Firestore document endpoints. Skips the generic
// auth/RPC spam. projectId + collection names are the security-relevant recovery.
function collectFirebase(ast) {
  const out = [], cols = new Set();
  let projectId = null;
  traverse(ast, {
    CallExpression(path) {
      const c = path.node.callee, nm = c && (c.name || (c.property && c.property.name));
      if (nm === "initializeApp" && path.node.arguments[0] && path.node.arguments[0].type === "ObjectExpression") {
        for (const p of path.node.arguments[0].properties) {
          if (p.type === "ObjectProperty" && p.key && (p.key.name ?? p.key.value) === "projectId" &&
              p.value.type === "StringLiteral") projectId = p.value.value;
        }
      }
      if (nm === "collection" || nm === "doc") {
        for (const a of path.node.arguments)
          if (a.type === "StringLiteral" && /^[A-Za-z_][\w-]{1,}$/.test(a.value) && !a.value.includes("/")) cols.add(a.value);
      }
    },
  });
  if (!projectId && cols.size === 0) return out;
  const pj = projectId || "{projectId}";
  const bp = `https://firestore.googleapis.com/v1/projects/${pj}/databases/(default)/documents`;
  for (const col of cols)
    out.push({ sink: "firestore", kind: "xhr", method: "GET", template: `${bp}/${col}`, body_keys: null, dynamic: !projectId });
  return out;
}

// Supabase JS (PostgREST): `supabase.from("table").select("a,b").eq(...).insert(...)` compiles to a URL built
// imperatively (opaque to fetch-sink extraction). Recover it by walking the method CHAIN: table from .from()'s
// string arg, method from the mutation verb present (insert/upsert→POST, update→PATCH, delete→DELETE, else GET),
// columns from .select(). Recovers real DB TABLE + COLUMN names, high-value for authz/RLS. Literal args only.
function collectSupabase(ast) {
  const out = [], seen = new Set();
  const VERB = { insert: "POST", upsert: "POST", update: "PATCH", delete: "DELETE", select: "GET" };
  traverse(ast, {
    CallExpression(path) {
      const c = path.node.callee;
      if (!c || c.type !== "MemberExpression" || !c.property || c.property.name !== "from") return;
      const a0 = path.node.arguments[0];
      const table = a0 && a0.type === "StringLiteral" ? a0.value : null;
      if (!table || !/^[A-Za-z_][\w.]*$/.test(table)) return;      // literal, identifier-ish table name
      // `.from("X")` is not enough on its own. ConfigCat's logger is `em.from("VARIATION_ID")(template, e)`,
      // a tagged-template factory, and matching the method NAME turned eleven of its enum constants into
      // phantom tables on supabase.com: /rest/v1/VARIATION_ID, /SDK_KEY, /EVALUATE_LOG. Anchoring on the name
      // again, and wrong again. A real PostgREST query CHAINS a query method off the result; the impostor
      // INVOKES the result. Require at least one recognised link in the chain.
      let method = "GET", cols = null, p = path.parentPath, guard = 0, chained = false;
      const CHAIN = new Set(["select", "insert", "upsert", "update", "delete", "eq", "neq", "gt", "gte", "lt",
        "lte", "like", "ilike", "is", "in", "contains", "order", "limit", "range", "single", "maybeSingle",
        "match", "filter", "or", "not", "throwOnError", "csv", "explain", "abortSignal", "returns"]);
      while (p && guard++ < 24) {
        if (p.node.type === "MemberExpression" && p.node.property && p.node.property.name &&
            p.parentPath && p.parentPath.node.type === "CallExpression") {
          const mn = p.node.property.name;
          if (CHAIN.has(mn)) chained = true;
          if (VERB[mn] && mn !== "select") method = VERB[mn];
          if (mn === "select") { const sa = p.parentPath.node.arguments[0]; if (sa && sa.type === "StringLiteral") cols = sa.value; }
          p = p.parentPath.parentPath;                              // hop to the next link in the chain
        } else break;
      }
      if (!chained) return;
      let tmpl = `/rest/v1/${table}`;
      if (cols && /^[\w",().*\s:!-]+$/.test(cols)) tmpl += `?select=${cols.replace(/\s+/g, "")}`;
      const key = method + " " + tmpl;
      if (seen.has(key)) return;
      seen.add(key);
      out.push({ sink: "supabase", kind: "xhr", method, template: tmpl, body_keys: null, dynamic: false });
    },
  });
  return out;
}

try {
  // Strip flags before treating argv as a file list. `--config x.json` was being read as two input files,
// which fed JSON to Babel and returned [] for the entire batch, the same silent-empty failure mode this
// tool keeps producing whenever a bad input reaches the parser.
const _argv = process.argv.slice(2);
const _files = [];
let _indexOnly = false, _indexClients = false, _indexIn = null, _filesetIn = null;
for (let i = 0; i < _argv.length; i++) {
  if (_argv[i] === "--config") { i++; continue; }
  if (_argv[i] === "--index") { _indexIn = _argv[++i]; continue; }
  if (_argv[i] === "--fileset") { _filesetIn = _argv[++i]; continue; }
  if (_argv[i] === "--index-only") { _indexOnly = true; continue; }
  if (_argv[i] === "--index-clients") { _indexClients = true; continue; }
  if (_argv[i].startsWith("--")) continue;
  _files.push(_argv[i]);
}
const src = readAll(_files);
  const results = [];
  // Files are passed in BATCHES rather than one per process. Node costs ~110ms to start and a typical file
  // costs ~54ms to parse, so one-file-per-process spent roughly twice as long starting interpreters as it
  // did doing the work, 44,305 spawns is over an hour of pure startup. Each record is tagged with the
  // chunk it came from so per-file provenance survives batching.
  // PRE-PASS. Build the cross-file property index before extracting anything, because the line that
  // assigns a value and the line that reads it are routinely in different chunks.
  const _chunks = src.split("/*__FILEBREAK__*/");
  {
    const path_ = require("path");
    // EVERY file in the corpus, not just this batch. `_resolveSpec` maps `./defs.js` onto a real path by
    // membership in this set, so a batch that does not happen to contain the imported file cannot resolve
    // the import, which made the answer depend on how the work was divided.
    const _allFiles = (() => {
      if (!_filesetIn) return _files;
      try {
        const j = JSON.parse(require("fs").readFileSync(_filesetIn, "utf8"));
        if (Array.isArray(j)) return j;
        for (const [k, v] of Object.entries(j.aliases || {})) _ALIAS.set(k, v);
        return j.files || _files;
      } catch { return _files; }
    })();
    for (const f of _allFiles) {
      const abs = path_.resolve(f);
      _FILESET.add(abs);
      _BYBASE.set(path_.basename(abs).replace(/\.(js|mjs)$/, ""), abs);
    }
    // …and the exports/property values gathered from ALL of them in a prior pass. A module index that
    // only covers the current process is not a module index, it is an accident of scheduling: an imported
    // `BASE` resolved to /api/orders when its defining file shared the batch and stayed {BASE}/orders when
    // it did not, so batch size changed the inferred program. Measured on one target: 32 endpoints differed.
    if (_indexIn) {
      try {
        const j = JSON.parse(require("fs").readFileSync(_indexIn, "utf8"));
        for (const [k, v] of (j.exports || [])) if (!_EXPORTS.has(k)) _EXPORTS.set(k, v);
        for (const [k, v] of (j.propvals || [])) if (!_PROPVALS.has(k)) _PROPVALS.set(k, v);
        for (const [k, v] of (j.callargs || [])) if (!_CALLARGS.has(k)) _CALLARGS.set(k, v);
        // The corpus-wide tRPC verdict, settled by pass one over EVERY file. Without it each batch
        // asked the question of its own files and cal.com, whose marker is in 1 file of 79, lost
        // its 42 real tRPC endpoints in every other batch.
        if (typeof j.trpc === "boolean") _IS_TRPC = j.trpc;
        if (j.routing === "hash" || j.routing === "path") _ROUTING = j.routing;
      } catch (e) {
        process.stderr.write("static_ast: could not read --index: " + e.message + "\n");
      }
    }
    // Only when there is no corpus-wide index. With one, re-indexing this batch's files ADDS
    // nondeterminism rather than information: indexPropertyValues keeps a value only where the bundle is
    // unanimous about it, so a batch that happens to contain a conflicting assignment demotes an entry the
    // full-corpus pass had already settled, and which files share a batch is exactly what must not matter.
    // The index pass already read every file; this loop can only disagree with it.
    if (!_indexIn) {
      for (let i = 0; i < _chunks.length; i++) {
        try {
          const a = parse(_chunks[i]);
          if (!a) continue;
          indexPropertyValues(a, _PROPVALS);
          indexCallArgs(a, _CALLARGS);
          if (detectTrpc(a, _chunks[i])) _IS_TRPC = true;
          const _rt = detectRouting(a, _chunks[i]);
          if (_rt) {
            if (_ROUTING && _ROUTING !== _rt) _ROUTING_CONFLICT = true;
            _ROUTING = _rt;
          }
          if (_files[i]) indexExports(a, path_.resolve(_files[i]));
          _SRC = _chunks[i];
          indexBundledExports(a);
        } catch {}
      }
    }
  }

  if (_indexClients) {
    // Index pass two, with pass one loaded: the clients modules export, and the base each is built with.
    const touched = new Map();
    for (let i = 0; i < _chunks.length; i++) {
      try {
        const a = parse(_chunks[i]);
        if (!a) continue;
        _SRC = _chunks[i];
        _CURFILE = _files[i] ? require("path").resolve(_files[i]) : null;
        for (const [k, v] of indexClientExports(a, _CURFILE)) touched.set(k, v);
      } catch {}
    }
    process.stdout.write(JSON.stringify({ exports: [...touched] }));
    return;
  }

  if (_indexOnly) {
    // Pass one. Parse and index; run no collectors. This is the batch-independent half of the analysis and
    // it is much cheaper than a full extraction, which is what makes two passes affordable.
    process.stdout.write(JSON.stringify({ exports: [..._EXPORTS], propvals: [..._PROPVALS],
                                         callargs: [..._CALLARGS], trpc: _IS_TRPC === true,
                                         routing: _ROUTING_CONFLICT ? null : _ROUTING }));
    return;
  }

  let _chunkIdx = 0;
  for (const chunk of _chunks) {
    const myIdx = _chunkIdx++;
    _CURFILE = _files[myIdx] ? require("path").resolve(_files[myIdx]) : null;
    try {
      for (const r of extract(chunk)) { r._chunk = myIdx; results.push(r); }
    } catch (e) {
      // Swallowing this silently makes a crashed extraction indistinguishable from a file that genuinely
      // has no endpoints, it hid a thrown error on a 3MB bundle and simply reported "[]".
      process.stderr.write("static_ast extract failed: " + (e && e.stack ? e.stack : e) + "\n");
    }
  }
  // final cross-chunk dedupe
  const sers = new Map(), cargs = new Map();
  const seen = new Map(), zods = new Map(), actions = new Set(), relay = new Map(), gateways = new Map(), gqleps = new Map(), gqlsib = new Map(), hdrs = new Map(), clients2 = new Map(), cfgs = new Map();
  for (const f of results) {
    if (f._kind === "zod") {                                                    // dedupe schemas by name+fieldset
      const k = (f.name || "?") + "|" + (f.zod_fields || []).map((x) => x.key).join(",");
      if (!zods.has(k)) zods.set(k, f);
      continue;
    }
    if (f._kind === "action") { actions.add(f.action_id); continue; }
    if (f._kind === "gateway") { gateways.set(f.path, f); continue; }           // dedupe gateway routes by prefix
    if (f._kind === "gql-endpoint") { gqleps.set(f.uri, f); continue; }         // dedupe graphql endpoints by uri
    // ITS OWN BUCKET, for the reason the serializer comment below states: a sibling record has no method
    // and no template, so without one every sibling keyed to "undefined undefined" and exactly one of
    // them survived, twenty's /metadata and /admin-panel vanished and only /rest came out.
    if (f._kind === "gql-endpoint-sibling") { gqlsib.set(f.uri, f); continue; }
    if (f._kind === "relay") { if (!relay.has(f.op) || (!relay.get(f.op).text && f.text)) relay.set(f.op, f); continue; }
    // client-level headers have no method/template, without their own bucket they'd all collapse into a
    // single "undefined undefined" key and only one would survive.
    if (f._kind === "config-url") { const kk = f.name + "|" + f.value; if (!cfgs.has(kk)) cfgs.set(kk, f); continue; }
    if (f._kind === "client") { if (!clients2.has(f.name)) clients2.set(f.name, f); continue; }
    if (f._kind === "client-header") { if (!hdrs.has(f.name.toLowerCase())) hdrs.set(f.name.toLowerCase(), f); continue; }
    // Serializers need a bucket for exactly the reason stated above: no method, no template, so every one of
    // them keyed to "undefined undefined" and a chunk defining forty request schemas published one. Keep the
    // richest definition when a name appears more than once (minified and original-source copies of the same
    // function both arrive here).
    if (f._kind === "serializer" || f._kind === "callargs") {
      const bucket = f._kind === "serializer" ? sers : cargs;
      const prevS = bucket.get(f.name);
      if (!prevS || (f.fields || []).length > (prevS.fields || []).length) bucket.set(f.name, f);
      continue;
    }
    // `method template` is the endpoint's identity for a REST call and is NOT unique for anything whose
    // identity is an operation name. A GraphQL op and a WebSocket message both have one URL and many
    // operations, every socket message came out as `WS null`, so all of them merged into a single record
    // whose body was the union of every payload in the bundle. Include `op` wherever it is what
    // distinguishes them.
    // Records carry their type in `kind` OR `_kind` depending on which collector built them, a sink says
    // {kind:"xhr"}, a server action says {_kind:"server-action"}. This test read only `kind`, so server
    // actions never qualified for the op-in-key rule, every one of them keyed as `POST null`, and a bundle
    // declaring setBirthDate and deleteAccount emitted ONE record. Read both.
    const fkind = f.kind || f._kind;
    // A record that is not an endpoint has neither a method nor a template, so keying on those two alone
    // gives EVERY one of them the key "undefined undefined" and collapses them into a single survivor. The
    // per-file merge learned this already, it is the reason 4 serializers in a chunk became 1, and this
    // merge, which runs across chunks and is the last word, never got the same guard. It cost every
    // client-encoding record but the first: a bundle whose HTTP layer both JSON-stringifies and branches on
    // FormData reported one of the two and dropped the other.
    // AND THE FILE, for anything with a template. This merge runs across every file in the batch and
    // unions their bodies, but a template alone does not identify an endpoint, only (host, method, path)
    // does, and this process does not know hosts. analyze.py assigns them afterwards from the origin map.
    //
    // So two files loaded by DIFFERENT documents that both POST to /api/shared produced ONE record: the
    // second host's endpoint vanished, and the survivor's body became the union of both, a contract
    // claiming a field only the other host's code sends. Fabricated, not merely missing, and it fires
    // exactly where this tool normally operates: one bodies directory holding several hosts' bundles.
    //
    // Keeping the file in the key means no cross-file merging happens HERE. The union is still worth
    // having, so analyze.py performs it after attribution, keyed by (host, method, template, op),
    // see _merge_by_endpoint there.
    const k = (f._kind && !f.template)
      ? `${f._kind} ${f.name || f.op || ""}`
      : `${f._chunk ?? "?"} ${f.method} ${f.template}` +
        (f.op && (fkind === "ws-message" || fkind === "graphql" ||
                  fkind === "server-action") ? ` ${f.op}` : "");
    const prev = seen.get(k);
    if (!prev) seen.set(k, f);
    else mergeRecord(prev, f);                                                  // UNION body fields across chunks/call-sites
  }
  let out = [...sers.values(), ...cargs.values(), ...seen.values(), ...zods.values(), ...relay.values(), ...gateways.values(), ...gqleps.values(), ...gqlsib.values(), ...hdrs.values(), ...clients2.values(), ...cfgs.values()];
  for (const r of actions) out.push(r);          // already finished server-action records; see above

  // CROSS-FILE SUPPRESSION of path literals a call site already explained.
  //
  // collectPathLiterals is a blind sweep: it walks every string in a file and keeps anything path-shaped,
  // with no idea what the string is for. That is deliberate, a route table declares routes as bare
  // strings with no call site anywhere, and it is why those records carry method UNKNOWN.
  //
  // It already takes a `claimed` set so it can stay quiet about paths another collector explained. But
  // `claimed` is built from the records of THE SAME FILE, so the suppression is per-file by construction.
  // x declares its routes in one chunk and calls them from another: /rest/issue-report is a bare literal in
  // chunk 11 and a POST with an operation name in chunk 18, and the store ends up with both, one row that
  // says "POST /rest/issue-report [settingsUserIssueReportRaw]" and one that says "UNKNOWN, no call site
  // attributed". The second is not merely redundant, it is misleading: it reads as a gap we have.
  //
  // The merge above is the first point that sees every file at once, so the reconciliation belongs here.
  // Slot names differ between collectors ({id} vs {conversationId}), so match on the slot-erased shape.
  const explained = new Set();
  for (const r of out) {
    const m = String(r.method || "").toUpperCase();
    if (!m || m === "UNKNOWN") continue;
    const t = r.template || r.path;
    if (t) explained.add(String(t).replace(/\{[^}]*\}/g, "*"));
  }
  const before = out.length;
  out = out.filter((r) => {
    // A METHOD-LESS ROUTE CONSTANT IS THE SAME KIND OF DUPLICATE. Once tagged templates resolved,
    // `this._client.post(path\`/v1/x/${id}\`)` came out as POST, and the route-const scan that had
    // been the only thing keeping that path alive was still emitting it with no verb beside the
    // real one. Client-side page routes (`_kind: "route"`) are left alone on purpose: a page at
    // `/settings` and an API endpoint at `/settings` are two different things that share a path.
    const methodless = !r.method || String(r.method).toUpperCase() === "UNKNOWN";
    const dupable = r._kind === "path-literal" || (methodless && r.sink === "route-const");
    if (!dupable) return true;
    const t = String(r.template || r.path || "").replace(/\{[^}]*\}/g, "*");
    return !(t && explained.has(t));
  });
  if (before !== out.length)
    process.stderr.write(`  ${before - out.length} method-less duplicate(s) suppressed: a call site ` +
                         `already recovered their method\n`);
  // the cross-chunk merge builds records of its own, so the invariants are enforced once more here
  process.stdout.write(JSON.stringify(sanitize(out)));
} catch (e) {
  process.stderr.write("static_ast error: " + e.message + "\n");
  // `[]` with exit 0 is indistinguishable from a corpus that genuinely contains no requests, and the
  // caller counted this batch as a success: one crash silently removed every record in it. The empty
  // array still goes out so nothing downstream has to handle empty stdout, but the exit code says
  // what happened.
  process.exitCode = 1;
  process.stdout.write("[]");
}
