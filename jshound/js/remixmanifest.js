#!/usr/bin/env node
/* remixmanifest.js — evaluate a Remix / React Router framework-mode route manifest.
 *
 *     node remixmanifest.js inline0.js inline1.js ...
 *
 * A Remix document assigns its whole route table to a global in an inline script:
 *
 *     window.__remixManifest = {"entry":{...},"routes":{"root":{...}},"url":"...","version":"..."};
 *
 * EVALUATED rather than scraped, for the same reason `nextmanifest.js` evaluates `_buildManifest.js`:
 * brace-counting a minified object is guesswork, while running the assignment returns the structure the
 * framework itself reads. The context holds `window`/`self` and nothing else — no require, no process —
 * under a wall-clock timeout.
 *
 * What the manifest declares, per route, and why each matters:
 *
 *   path + id      the route tree. Nothing in this repo read it, although `__remixManifest` was already
 *                  used as a fingerprint marker.
 *   hasLoader      the route answers `<path>?_data=<id>`, Remix's own data protocol. Verified on
 *                  hydrogen.shop: `?_data=root` returns 6,272 bytes of text/remix-deferred and
 *                  `?_data=routes/($locale)._index` returns 10,750, while an INVENTED id returns 403 —
 *                  which is what makes this zero-inference. The ids come from the manifest, not a guess.
 *   hasAction      the same path accepts a non-GET submission.
 *   module+imports every chunk that route needs, as absolute URLs.
 */
const fs = require("fs");
const vm = require("vm");
const parser = require("@babel/parser");
const traverse = require("@babel/traverse").default;
const generate = require("@babel/generator").default;

const TIMEOUT_MS = 5000;

/* PARSE THE MODULE, THEN EVALUATE ONLY THE ASSIGNED EXPRESSION.
 *
 * Running the whole script is what the first version did and it recovered nothing: Remix ships the
 * assignment inside a `<script type="module">` that also carries ESM `import` statements, so
 * `vm.runInContext` throws "Cannot use import statement outside a module" before reaching the
 * assignment. Babel parses the module happily; the right-hand side is then a self-contained object
 * literal, and evaluating just that expression in an empty context is both narrower and safer than
 * running the file. */
let flavour = null;

function evaluate(src) {
  let ast;
  try {
    ast = parser.parse(src, { sourceType: "unambiguous", errorRecovery: true });
  } catch {
    return null;
  }
  let rhs = null;
  traverse(ast, {
    AssignmentExpression(p) {
      if (rhs) return;
      const l = p.node.left;
      if (!l || l.type !== "MemberExpression" || l.computed) return;
      const prop = l.property && (l.property.name || l.property.value);
      if (prop !== "__remixManifest" && prop !== "__reactRouterManifest") return;
      rhs = p.node.right;
      flavour = prop === "__reactRouterManifest" ? "react-router-v7" : "remix";
    },
  });
  if (!rhs) return null;
  try {
    const code = generate(rhs, { concise: true }).code;
    const sandbox = Object.create(null);
    vm.createContext(sandbox);
    return vm.runInContext(`(${code})`, sandbox, { timeout: TIMEOUT_MS });
  } catch {
    return null;
  }
}

/* Remix route paths are SEGMENTS that nest through `parentId`, so a child's URL is only meaningful
 * assembled with its ancestors. `root` carries path "" and contributes nothing. An `index` route adds no
 * segment of its own either. `:param` becomes `{param}` and an optional `:param?` keeps the name. */
function fullPath(routes, id, depth) {
  const segs = [];
  let cur = id, guard = 0;
  while (cur && guard++ < 32) {
    const r = routes[cur];
    if (!r) break;
    if (r.path) segs.unshift(r.path);
    cur = r.parentId;
  }
  const out = [];
  for (const seg of segs.join("/").split("/")) {
    if (!seg) continue;
    if (seg.startsWith(":")) {
      out.push("{" + seg.slice(1).replace(/\?$/, "") + "}");
      continue;
    }
    // `($locale)` is an optional segment in the newer file convention; it contributes a slot, not text.
    const opt = /^\((.+)\)$/.exec(seg);
    if (opt) {
      out.push("{" + opt[1].replace(/^\$/, "") + "}");
      continue;
    }
    if (seg.startsWith("$")) {
      out.push("{" + (seg.slice(1) || "splat") + "}");
      continue;
    }
    out.push(seg);
  }
  return "/" + out.join("/");
}

function main() {
  const out = { routes: [], data_endpoints: [], chunks: [], version: null, flavour: null,
                data_protocol: null, errors: [] };
  let manifest = null;
  for (const f of process.argv.slice(2)) {
    let src;
    try {
      src = fs.readFileSync(f, "utf8");
    } catch (e) {
      out.errors.push({ file: f, error: String((e && e.message) || e) });
      continue;
    }
    // Cheap precheck on either global; `evaluate` is what decides.
    if (!src.includes("__remixManifest") && !src.includes("__reactRouterManifest")) continue;
    const m = evaluate(src);
    if (m && m.routes && typeof m.routes === "object") { manifest = m; break; }
  }
  if (!manifest) { process.stdout.write(JSON.stringify(out)); return; }

  out.version = typeof manifest.version === "string" ? manifest.version : null;
  out.flavour = flavour;
  out.data_protocol = flavour === "react-router-v7" ? "<path>.data" : "<path>?_data=<route id>";
  const routes = manifest.routes;
  const chunks = new Set();
  const addChunk = (u) => { if (typeof u === "string" && u) chunks.add(u); };
  addChunk(manifest.entry && manifest.entry.module);
  for (const u of (manifest.entry && manifest.entry.imports) || []) addChunk(u);

  for (const id of Object.keys(routes)) {
    const r = routes[id] || {};
    const template = fullPath(routes, id, 0);
    addChunk(r.module);
    for (const u of r.imports || []) addChunk(u);
    out.routes.push({
      id,
      template,
      segment: typeof r.path === "string" ? r.path : null,
      index: !!r.index,
      parent: r.parentId || null,
      has_loader: !!r.hasLoader,
      has_action: !!r.hasAction,
      has_client_loader: !!r.hasClientLoader,
      has_error_boundary: !!r.hasErrorBoundary,
    });
    // THE DATA PROTOCOL DIFFERS BY VERSION, and this was checked on live targets rather than
    // assumed, because guessing it wrong produces a 200 that carries nothing.
    //
    //   Remix v2            <path>?_data=<route id>   hydrogen.shop: 200, text/remix-deferred,
    //                                                 6,272 B for root; an INVENTED id answers 403
    //   React Router v7     <path>.data               reactrouter.com: /.data 200 text/x-script
    //                                                 1,834 B, /home.data 200, 33,582 B
    //
    // On v7, `?_data=root` answers 200 with text/html — the ordinary page. Emitting it would look
    // like a hit and contain no data at all, which is worse than emitting nothing.
    if (r.hasLoader) {
      if (flavour === "react-router-v7") {
        const t = template === "/" ? "/.data" : template + ".data";
        out.data_endpoints.push({ method: "GET", template: t, query: null, route_id: id });
      } else {
        out.data_endpoints.push({ method: "GET", template, query: `_data=${id}`, route_id: id });
      }
    }
  }
  out.routes.sort((a, b) => a.template.localeCompare(b.template) || a.id.localeCompare(b.id));
  // Several route ids can share one URL: a pathless LAYOUT route contributes no segment, so `root`,
  // `docs` and the splash index route all resolve to `/`. The routes are kept per id, because the id
  // is the provenance and v2's protocol needs it, but a data endpoint is a URL and duplicates of it
  // are noise — reactrouter.com produced `/.data` three times before this.
  const byTemplate = new Map();
  for (const e of out.data_endpoints) {
    const key = e.query ? `${e.template}?${e.query}` : e.template;
    if (!byTemplate.has(key)) byTemplate.set(key, e);
  }
  out.data_endpoints = [...byTemplate.values()]
    .sort((a, b) => a.template.localeCompare(b.template) || String(a.query).localeCompare(String(b.query)));
  out.chunks = [...chunks].sort();
  process.stdout.write(JSON.stringify(out, null, 1));
}

main();
