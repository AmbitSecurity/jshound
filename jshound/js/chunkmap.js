#!/usr/bin/env node
/* chunkmap.js — enumerate webpack lazy-chunk URLs by evaluating the runtime's own URL builder.
 *
 * Webpack never writes its lazy chunk URLs down. The runtime ships a function
 *     __webpack_require__.u = e => "static/chunks/" + (({94:"pages"})[e] || e) + "." + {94:"a1b2"}[e] + ".js"
 * and calls it with an id at import() time. Nothing in the bundle contains the string
 * "static/chunks/pages.a1b2.js", so no regex and no literal-collecting AST pass will ever find it — yet
 * that is where most of a Next/webpack app's code, and therefore most of its endpoints, actually lives.
 *
 * The reliable move is to stop pattern-matching and just run it: recover the function, harvest every key
 * from the id->hash maps inside it, and call it once per key. Output is whatever the app itself would
 * request. Same trick for `.miniCssF` is possible but CSS carries no endpoints, so only JS builders run.
 *
 * Input: a JS bundle on stdin.  Output: a JSON object:
 *   { publicPath, chunks[], speculative[], fallback, stats{builders,buildersFailed,idsSeen,idsResolved,
 *     structured,speculative} }
 *
 * `chunks` are built by the runtime's own builder and are what the app would request. `speculative` are
 * chunk-shaped strings recovered by scanning when no builder could be evaluated — a weaker signal, kept
 * separate so a caller never mistakes one for the other. `stats` is there so "this bundle declares no
 * chunks" stays distinguishable from "it declares 40 and we could only build 12".
 */
const { parse } = require("@babel/parser");
const traverse = require("@babel/traverse").default;
const generate = require("@babel/generator").default;

const src = require("fs").readFileSync(0, "utf8");

function parseJs(code) {
  for (const opts of [
    { sourceType: "unambiguous", plugins: ["jsx", "typescript"], errorRecovery: true },
    { sourceType: "script", errorRecovery: true },
  ]) {
    try { return parse(code, opts); } catch (_) {}
  }
  return null;
}

const ast = parseJs(src);
// A FILE THAT WILL NOT PARSE STILL OWES THE CALLER THE DOCUMENTED SHAPE. This wrote "[]" — the
// output format from before stats existed — so every consumer doing `.get("chunks")` crashed on the
// first unparseable file in a corpus, which is how one tldraw inline script took down a whole run.
if (!ast) {
  process.stdout.write(JSON.stringify({
    publicPath: "", chunks: [], speculative: [], fallback: false, parsed: false,
    stats: { builders: 0, buildersFailed: 0, idsSeen: 0, idsResolved: 0,
             structured: 0, speculative: 0 },
  }));
  process.exit(0);
}

// Any identifier the builder closes over resolves to a benign stub, so `r.p + "static/..."` yields
// "static/..." rather than a ReferenceError that would lose the whole map.
// A stub has to be CALLABLE as well as indexable. Element's builder is
//     r.u = e => "bundles/" + r.h() + "/" + ({…}[e] || e) + ".js"
// and a plain object for `r.h` makes `r.h()` throw "not a function", which killed the whole
// evaluation: 8 chunk ids seen, 0 resolved, no URLs, no error anyone would notice.
const CALLABLE = Object.assign(function () { return ""; },
                               { p: "", u: () => "", miniCssF: () => "" });
const STUB = new Proxy({}, {
  has: () => true,
  get: (_t, k) => (k === Symbol.unscopables ? undefined : CALLABLE),
});

// THE BUILDER'S SIBLINGS ARE IN THE FILE. `r.h = () => "488a74c4cbf74401eabd"` sits two expressions
// away from `r.u`, and it holds the build hash every chunk URL needs. Stubbing it yields
// `bundles//styles.js`; reading it yields the URL the app actually requests. So collect every
// `<recv>.<prop> = <function|literal>` and hand the real ones to the evaluation, keeping the stub
// only as the fallback for what is genuinely not here.
const siblings = new Map();          // receiver name -> Map(prop -> source)

function receiverFor(name) {
  const m = siblings.get(name);
  const base = {};
  if (m) {
    for (const [k, src] of m) {
      if (k === "u") continue;       // the builder itself — do not re-enter it
      try { base[k] = new Function("__s", `with(__s){return (${src})}`)(STUB); } catch (_) {}
    }
  }
  return new Proxy(base, {
    has: () => true,
    get: (t, k) => (k in t ? t[k] : (k === Symbol.unscopables ? undefined : CALLABLE)),
  });
}

function scopeFor(name) {
  const recv = name ? receiverFor(name) : null;
  return new Proxy({}, {
    has: () => true,
    get: (_t, k) => (k === Symbol.unscopables ? undefined
                     : (recv && k === name ? recv : CALLABLE)),
  });
}

const builders = [];
// The same function can be reached twice — webpack 5's `r.u = e => …` is an assignment AND a
// function, so both visitors see it and its ids were counted once per visitor. The demo fixture
// went from 3 ids seen to 6 while still producing 3 URLs, which is the kind of stat that quietly
// stops meaning anything.
const _seenBuilders = new Set();
const pushBuilder = (node) => { if (node && !_seenBuilders.has(node)) { _seenBuilders.add(node); builders.push(node); } };
traverse(ast, {
  AssignmentExpression: {
    enter(p) {
      const { left, right } = p.node;
      if (left.type !== "MemberExpression" || left.computed) return;
      const obj = left.object && left.object.name;
      const prop = left.property && (left.property.name ?? left.property.value);
      if (!obj || !prop) return;
      if (!/^(ArrowFunctionExpression|FunctionExpression|StringLiteral|NumericLiteral|TemplateLiteral)$/
            .test(right.type)) return;
      if (!siblings.has(obj)) siblings.set(obj, new Map());
      const m = siblings.get(obj);
      if (!m.has(prop)) { try { m.set(prop, generate(right).code); } catch (_) {} }
    },
  },
});
traverse(ast, {
  AssignmentExpression(p) {
    const { left, right } = p.node;
    if (left.type !== "MemberExpression" || left.computed) return;
    if ((left.property && left.property.name) !== "u") return;
    if (right.type !== "ArrowFunctionExpression" && right.type !== "FunctionExpression") return;
    // A BUILDER TAKES THE CHUNK ID. Anything with no parameter cannot be building a per-chunk URL.
    if (!(right.params || []).length) return;
    right.__recv = left.object && left.object.name;        // remember what the builder calls itself on
    pushBuilder(right);
  },
  // WEBPACK 4 HAS NO `.u`. It emits a named function instead —
  //     function jsonpScriptSrc(e){return r.p+""+({...}[e]||e)+"."+{...}[e]+".chunk.js"}
  // — so a detector keyed on an assignment to `.u` finds nothing at all. GitLab is webpack 4 and
  // reported 0 chunks from a runtime that declares hundreds. Matched on SHAPE rather than on the
  // name, because the name is a webpack-4 implementation detail and the shape is the contract: one
  // parameter, a returned expression that indexes object literals by that parameter, and a string
  // literal ending in .js. `jsonpScriptSrc` is accepted by name too, since it costs nothing.
  Function(p) {
    const n = p.node;
    if (!/FunctionDeclaration|FunctionExpression|ArrowFunctionExpression/.test(n.type)) return;
    if ((n.params || []).length !== 1 || n.params[0].type !== "Identifier") return;
    const param = n.params[0].name;
    // Cheap AST pre-filter before paying for generate(): a chunk builder always carries at least
    // one id map, so a function with no object literal in it cannot be one.
    let hasObj = false;
    try {
      p.traverse({ ObjectExpression(q) { if ((q.node.properties || []).length) { hasObj = true; q.stop(); } } });
    } catch (_) { return; }
    if (!hasObj) return;
    let src;
    try { src = generate(n).code; } catch (_) { return; }
    // A BUILDER IS AS BIG AS THE APP HAS CHUNKS. The first cap here was 20 KB, which rejected
    // GitLab's at 48 KB — its id maps name hundreds of chunks, several with 90-character names —
    // and reported 0 chunks for an app that declares hundreds. The limit exists to bound the cost
    // of generate(), not to describe what a builder looks like.
    if (src.length > 2_000_000) return;
    if (!/\.js["'`]/.test(src)) return;
    // must index something by its own parameter, which is what makes it a per-chunk builder
    if (!new RegExp("\\[\\s*" + param + "\\s*\\]").test(src)) return;
    if (!/\{\s*["'\d]/.test(src)) return;                 // ...and carry at least one id map
    // A URL BUILDER RETURNS THE URL. Without this, webpack 4's jsonp *runtime* also matched — it is
    // a one-parameter function that mentions ".js" and indexes by its parameter — and its ids are
    // not chunk ids. GitLab then reported 2,553 ids seen against 1,272 resolved, which reads as a
    // 50% failure when in truth half the denominator was never a chunk id at all.
    let returnsUrl = false;
    try {
      p.traverse({
        ReturnStatement(q) {
          // ITS OWN return, not a nested function's. GitLab's 55 KB jsonp runtime CONTAINS the
          // 48 KB builder, so a traverse that descends into nested functions sees the inner
          // return and promotes the wrapper too — which is how 1,281 non-chunk ids got into the
          // denominator.
          const fp = q.getFunctionParent();
          if (!fp || fp.node !== n) return;
          if (!q.node.argument) return;
          let rs;
          try { rs = generate(q.node.argument).code; } catch (_) { return; }
          if (/\.m?js(\?[^"'`]*)?["'`]\s*$/.test(rs.trim())) returnsUrl = true;
        },
      });
    } catch (_) { return; }
    if (!returnsUrl) return;
    pushBuilder(n);
  },
  // some builds attach it as an object property: { u: e => ... }
  ObjectProperty(p) {
    const k = p.node.key;
    if ((k.name || k.value) !== "u") return;
    const v = p.node.value;
    // Same rule here, and this is where it mattered: Figma ships `{u: () => i}` and `{u: () => n}`
    // — ordinary object properties that happen to be named `u` — and both were counted as chunk URL
    // builders, reporting 2 builders and 12 ids against 0 resolved. A zero-argument function cannot
    // build a URL for a chunk it was never told about.
    if ((v.type === "ArrowFunctionExpression" || v.type === "FunctionExpression") &&
        (v.params || []).length) pushBuilder(v);
  },
});

// PUBLIC PATH. The builder returns a path RELATIVE to __webpack_require__.p, and webpack concatenates the two at
// runtime. Resolving the result against the runtime file's own directory instead is wrong whenever the chunks are
// served from somewhere else — which is the norm, because the runtime is usually an inline <script> in the HTML
// while the chunks sit on a CDN. one partner portal ships its runtime inline on the page and its 718 chunks
// under `//lf16-cdn-tos.tiktokcdn-us.com/obj/static-tx/i18n/ecom/partner_center/`, so every URL we built pointed at
// the wrong host and 87% of the app was never fetched. The runtime states the base; read it.
let publicPath = "";
traverse(ast, {
  AssignmentExpression(p) {
    const { left, right } = p.node;
    if (left.type !== "MemberExpression" || left.computed) return;
    if ((left.property && left.property.name) !== "p") return;
    const v = right.type === "StringLiteral" ? right.value
            : (right.type === "TemplateLiteral" && right.quasis.length === 1) ? right.quasis[0].value.cooked : null;
    // a bare "" or a single "/" is webpack's default and tells us nothing the caller does not already know
    if (v && v.length > 1 && !publicPath) publicPath = v;
  },
});

const out = new Set();
// HOW COMPLETE WAS THE STRUCTURED PASS? `ids` below is every chunk id the builder's own maps
// declare, so the bundle hands us the denominator for free. Resolving 12 of 40 is a MEASURED
// shortfall, not a suspicion — and it is the case the old `out.size === 0` gate could not see,
// because 12 is not zero. Partial success silently suppressed the safety net, which is how a
// chase comes back looking successful with most of the app missing.
let idsSeen = 0, idsResolved = 0, buildersFailed = 0;
for (const fnNode of builders) {
  let code;
  try { code = generate(fnNode, { compact: true }).code; } catch (_) { continue; }

  // every key of every id->name / id->hash map inside the builder is a real chunk id
  const ids = new Set();
  try {
    traverse(
      { type: "File", program: { type: "Program", body: [{ type: "ExpressionStatement", expression: fnNode }], directives: [], sourceType: "script" } },
      {
        ObjectProperty(p) {
          const k = p.node.key;
          const v = k.type === "NumericLiteral" ? k.value : k.type === "StringLiteral" ? k.value : k.name;
          if (v !== undefined && v !== null) ids.add(v);
        },
      }
    );
  } catch (_) {}
  // IDS FROM THE CALL SITES, when the builder carries no map of its own. A small or
  // single-hash build emits `n.u = e => e + ".5eb33312.js"` — there is no id table inside it, so
  // harvesting ObjectExpressions finds nothing and the builder is never run. Every lazy import
  // still asks for its chunk by id through `__webpack_require__.e(480)`, so the ids are in the
  // program even when they are not in the builder. Measured on a webpack 5 Module Federation
  // build: 0 chunks before, because idsSeen was 0.
  if (!ids.size) {
    traverse(ast, {
      CallExpression(q) {
        const c2 = q.node.callee;
        if (!c2 || c2.type !== "MemberExpression" || c2.computed) return;
        if ((c2.property && c2.property.name) !== "e") return;
        const a0 = (q.node.arguments || [])[0];
        if (!a0) return;
        if (a0.type === "NumericLiteral") ids.add(a0.value);
        else if (a0.type === "StringLiteral" && a0.value.length <= 64) ids.add(a0.value);
      },
    });
  }
  if (!ids.size) continue;

  let fn;
  try {
    fn = new Function("__s", `with(__s){return (${code})}`)(scopeFor(fnNode.__recv));
  } catch (_) { buildersFailed++; continue; }

  idsSeen += ids.size;
  for (const id of ids) {
    let got = false;
    for (const cand of [id, String(id)]) {
      try {
        const u = fn(cand);
        if (typeof u === "string" && u.length > 3 && /\.m?js(\?|$)/.test(u) && !/undefined/.test(u)) {
          out.add(u);
          got = true;
        }
      } catch (_) {}
    }
    if (got) idsResolved++;
  }
}

// VITE / ROLLUP: THE CHUNK PATHS ARE LITERALS, SO THERE IS NO BUILDER TO RUN.
//
// Webpack hides its chunk URLs behind a function; Vite writes them down. Two shapes carry them, and
// both are definite rather than speculative because they are string literals the bundler emitted:
//
//   import("./architectureDiagram-VXUJARFQ-CM5vRAuo.js")      a dynamic import's own specifier
//
//   __vite__mapDeps=(i,m=__vite__mapDeps,d=(m.f||(m.f=[       the shared dependency table, indexed
//     "assets/src-97epdJdY.js","assets/react-dom-BzP9Lo1b.js" by __vite__mapDeps([0,1,2])
//   ])))=>i.map(i=>d[i]);
//
// Measured before writing this: tldraw ships 63 asset-hash chunk paths and excalidraw 96 dynamic
// imports, and the webpack-only pass returned 0 structured chunks for both — they fell through to
// the speculative scanner, which is the weaker signal and says so. Reading the literals moves them
// where they belong.
const CHUNKY = /\.m?js(\?|$)/;
traverse(ast, {
  // import("./chunk-HASH.js") — the specifier IS the URL, relative to the importing module.
  CallExpression(p) {
    const c = p.node.callee;
    if (!c || c.type !== "Import") return;
    const a0 = (p.node.arguments || [])[0];
    if (a0 && a0.type === "StringLiteral" && CHUNKY.test(a0.value)) out.add(a0.value);
  },
  // Parcel writes its table as an OBJECT, not an array:
  //     Object.assign(o.i ??= {}, { jrWAC: "feature.9e64f881.js" })
  // Same rule as the array case — every value must be chunk-shaped, which is what stops an
  // arbitrary object of strings being collected.
  ObjectExpression(p) {
    const props = p.node.properties || [];
    if (!props.length || props.length > 4000) return;
    const vals = [];
    for (const pr of props) {
      if (pr.type !== "ObjectProperty" || pr.computed) return;
      const v = pr.value;
      if (!v || v.type !== "StringLiteral" || !CHUNKY.test(v.value)) return;
      vals.push(v.value);
    }
    for (const v of vals) out.add(v);
  },
  // The dependency table: an array whose entries are all chunk-shaped strings. Requiring every
  // element to match is what keeps this from collecting an arbitrary array of strings.
  ArrayExpression(p) {
    const els = p.node.elements || [];
    if (els.length < 2 || els.length > 4000) return;
    const vals = [];
    for (const e of els) {
      if (!e || e.type !== "StringLiteral" || !CHUNKY.test(e.value)) return;
      vals.push(e.value);
    }
    for (const v of vals) out.add(v);
  },
});

// TURBOPACK. Next.js 15 defaults to it, and it does not ship a URL-building function at all — it writes the
// chunk list out as a literal array:
//     globalThis.TURBOPACK.push([id, {...}, {otherChunks:["static/chunks/26sgpyaquw5sh.js", ...]}])
// Recovering `__webpack_require__.u` finds nothing here, so on x.ai we returned zero chunks while jsluice —
// which does nothing cleverer than collect string literals — found all eight. Read the array.
traverse(ast, {
  MemberExpression(p) {
    const o = p.node.object, pr = p.node.property;
    if (!pr || (pr.name || pr.value) !== "TURBOPACK") return;
    if (!o || (o.name !== "globalThis" && o.name !== "self" && o.name !== "window")) return;
    const call = p.findParent((x) => x.isCallExpression());
    if (!call) return;
    call.traverse({
      ObjectProperty(q) {
        const k = q.node.key && (q.node.key.name ?? q.node.key.value);
        if (k !== "otherChunks" && k !== "chunks" && k !== "chunkList") return;
        if (q.node.value.type !== "ArrayExpression") return;
        for (const el of q.node.value.elements) {
          if (el && el.type === "StringLiteral" && /\.m?js(\?|$)/.test(el.value)) out.add(el.value);
        }
      },
    });
  },
});

// LAST-RESORT LITERAL COLLECTION. Everything above anchors on a specific bundler's shape, which is precise
// where it fires and silent where it does not — and "silent" is the failure mode that costs us a whole app.
// This is the crudest technique in the entire tooling survey and it is the only one that is format-agnostic,
// so it runs strictly as a safety net: ONLY when the structured passes found nothing at all.
//
// It is gated deliberately. Emitting speculative URLs alongside good ones was measured as a net LOSS earlier
// (supabase 377 -> 264 endpoints, 70% slower) because 404s consume the fetch deadline. A net is for catching
// a fall, not for throwing over everything.
// It scans the raw TEXT, not the AST. That is not laziness, it is the requirement: x.ai ships its chunk list
// inside React Server Components flight data —
//     4d:I[549672,["/_next/static/chunks/3f58t6cfa5-9b.js?dpl=...", ...],"IconMark"]
// — which parses as a SINGLE string literal a thousand characters long. Every path is nested inside one
// token, so any AST walk that tests whole string values sees nothing. Walking the text sees all fifteen.
// THE GATE. Two changes from `out.size === 0`:
//
//   * it also fires on a MEASURED shortfall (idsResolved < idsSeen), so a builder that resolved
//     5 of 40 chunks no longer suppresses the net by virtue of 5 not being 0;
//   * what the net finds goes in its OWN bucket rather than being mixed into `chunks`.
//
// The second half is what makes the first half safe. The documented regression (supabase 377 ->
// 264 endpoints, 70% slower) was caused by speculative URLs competing with real ones for the fetch
// deadline, not by collecting them. Kept separate, the caller fetches the structured list first and
// treats a 404 on a speculative URL as the net catching nothing — which is not a hole in the map.
const spec = new Set();
const shortfall = idsResolved < idsSeen;
if (out.size === 0 || shortfall) {
  const rx = /(?:^|["'`,\[\s(=])((?:\.{0,2}\/)?(?:[A-Za-z0-9_@.-]+\/)*[A-Za-z0-9_@.-]+\.m?js)(?:\?[^"'`,\]\s)]*)?/g;
  for (const m of src.matchAll(rx)) {
    const s = m[1];
    if (!s || s.length < 7 || s.length > 300) continue;
    // a chunk path lives in a directory or carries a build hash; bare "index.js" is neither, and matching it
    // would flood the fetch queue with 404s — which was measured as a net loss (supabase 377 -> 264).
    // A BUILT chunk carries a content hash; a module identifier does not. Requiring one is what separates
    // `checklist_main.Dnck2r-82sxtd83.digested.js` from `admin/groups/automatic_membership_count.js` —
    // the latter is an Ember module path that happens to end in .js, and fetching it produces a confident
    // list of URLs that were never assets. Accepting anything with a slash let 207 of those through.
    const hashy = s.split("/").pop().split(/[.\-_]/)
      .some((seg) => seg.length >= 6 && /[0-9]/.test(seg) && /[a-z]/i.test(seg));
    const bundlerDir = /(^|\/)(static\/chunks|_next\/static|assets|dist|build)\//.test(s);
    if (!hashy && !bundlerDir) continue;
    if (/^(https?:)?\/\//.test(s)) continue;
    // A URL THE BUILDER ITSELF FAILED TO BUILD. `.../gamma.undefined.js` is the structured pass's
    // own rejected output, sitting in the text for the scanner to find again. The structured pass
    // drops it for a reason and so does this: it is a guaranteed 404 with a real fetch slot.
    if (/undefined/.test(s)) continue;
    spec.add(s);
  }
}
// never speculate about something the structured pass already resolved
for (const u of out) spec.delete(u);

process.stdout.write(JSON.stringify({
  publicPath,
  chunks: [...out],
  speculative: [...spec],
  fallback: out.size === 0 && spec.size > 0,
  // SAY HOW COMPLETE THIS IS. A caller that cannot tell "this bundle declares no chunks" from
  // "this bundle declares 40 and we built 12 URLs" cannot report a hole it does not know about.
  stats: { builders: builders.length, buildersFailed, idsSeen, idsResolved,
           structured: out.size, speculative: spec.size },
}));
