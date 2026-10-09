#!/usr/bin/env node
/* nextmanifest.js, evaluate Next's own served manifests and print what the BUILD declared.
 *
 *     node nextmanifest.js _buildManifest.js _ssgManifest.js _clientMiddlewareManifest.js
 *
 * A Pages-Router build writes its complete route table to `_buildManifest.js` and links it from the
 * document. The file is an assignment, not data:
 *
 *     self.__BUILD_MANIFEST = { "/account/me": ["static/chunks/1ir8gf5k36l2m.js"], ..., sortedPages: [...] }
 *     self.__BUILD_MANIFEST_CB && self.__BUILD_MANIFEST_CB()
 *
 * so it is READ FROM THE AST rather than scraped, the same way `chunkmap.js` reads
 * `__webpack_require__.u`. Brace-counting a minified object is guesswork; walking the assignment's
 * right-hand side returns the structure the framework itself reads at runtime. That distinction is the
 * whole point: these routes are not inferred from filenames or string literals, they are the build's
 * own declaration, so the floor on precision is the build being wrong about itself.
 *
 * None of the file is executed. An earlier version evaluated it in a `vm` context and two escapes out
 * of that were reproduced against it, see `literal.js`, which replaced it. The `*_CB` callbacks are
 * not data and are ignored, which is also what they did before: they short-circuit on an absent
 * callback by design.
 */
const fs = require("fs");
const path = require("path");
const literal = require("./literal.js");

/* Every global this reads. Named rather than walked, so a file cannot enlarge what is read by
 * assigning more. */
const GLOBALS = ["__BUILD_MANIFEST", "__SSG_MANIFEST", "__MIDDLEWARE_MATCHERS"];

function evaluate(file) {
  let src;
  try {
    src = fs.readFileSync(file, "utf8");
  } catch (e) {
    return { error: String((e && e.message) || e) };
  }
  const got = literal.assignedGlobals(src, GLOBALS);
  if (got.error) return { error: got.error };
  return { self: got };
}

/* `[id]` -> `{id}`, `[...slug]` -> `{slug}`, `[[...path]]` -> `{path}`; a `(group)` carries no URL
 * segment and an `@slot` is a parallel route, neither of which appears in a Pages-Router route anyway. */
function template(route) {
  const segs = [];
  for (const s of String(route).split("/")) {
    if (!s) continue;
    if (s.startsWith("(") && s.endsWith(")")) continue;
    if (s.startsWith("@")) continue;
    if (s.startsWith("[") && s.endsWith("]")) {
      const name = s.replace(/^\[+/, "").replace(/\]+$/, "").replace(/^\.\.\./, "");
      segs.push("{" + (name || "param") + "}");
      continue;
    }
    segs.push(s);
  }
  return "/" + segs.join("/");
}

/* A rewrite source is written in Next's path-to-regexp dialect, not the filesystem one:
 * `/studio/:path+`, `/legacy/:id`, `/docs/:slug*`. Same idea, different spelling. */
function rewriteTemplate(s) {
  return String(s).split("/").map(seg =>
    seg.startsWith(":") ? "{" + seg.slice(1).replace(/[*+?]$/, "") + "}" : seg).join("/");
}

/* Addressable by the router but not surface anyone can request as written. */
const INTERNAL = new Set(["/_app", "/_document", "/_error", "/_middleware"]);

function main() {
  const out = { routes: [], api_routes: [], chunks: [], ssg: [], middleware: [], rewrites: [], sources: [], errors: [] };
  const routes = new Map();      // template -> {route, chunks}
  const chunks = new Set();

  for (const file of process.argv.slice(2)) {
    const base = path.basename(file).replace(/\?.*$/, "");
    const r = evaluate(file);
    if (r.error) { out.errors.push({ file: base, error: r.error }); continue; }
    const s = r.self;
    const found = [];

    const bm = s.__BUILD_MANIFEST;
    if (bm && typeof bm === "object") {
      found.push("__BUILD_MANIFEST");
      // sortedPages is the authoritative list; the object's own keys are the same set plus the
      // per-route chunk arrays. Union them so a build that omits sortedPages still reports.
      const declared = new Set();
      for (const p of Array.isArray(bm.sortedPages) ? bm.sortedPages : []) declared.add(p);
      for (const [k, v] of Object.entries(bm)) {
        if (k.startsWith("/")) declared.add(k);
        if (Array.isArray(v)) for (const c of v) if (typeof c === "string" && c.endsWith(".js")) chunks.add(c);
      }
      // `__rewrites` maps a PUBLIC path to an internal destination. Both halves are surface: the
      // source is requestable and may appear in no route list, and the destination names internal
      // structure the client never links to.
      const rw = bm.__rewrites;
      if (rw && typeof rw === "object") {
        for (const phase of ["beforeFiles", "afterFiles", "fallback"]) {
          for (const r of Array.isArray(rw[phase]) ? rw[phase] : []) {
            if (!r || typeof r.source !== "string") continue;
            out.rewrites.push({
              phase,
              source: r.source,
              template: rewriteTemplate(r.source),
              destination: typeof r.destination === "string" ? r.destination : null,
            });
          }
        }
      }

      for (const p of declared) {
        if (!String(p).startsWith("/") || INTERNAL.has(p)) continue;
        const t = template(p);
        const rec = routes.get(t) || { template: t, route: p, chunks: [] };
        if (Array.isArray(bm[p])) rec.chunks = bm[p].filter(c => typeof c === "string");
        routes.set(t, rec);
      }
    }

    const ssg = s.__SSG_MANIFEST;
    if (ssg) {
      found.push("__SSG_MANIFEST");
      // A real build writes this as `new Set([...])`, not an array. It arrives here as an array
      // anyway because the conversion in `vmvalue.js` drains any iterable inside the context, which
      // is also the only place that Set can be recognised, since it belongs to the context's realm
      // and `instanceof Set` against ours is false. That mismatch returned an empty prerendered list
      // every time until a fixture had a non-empty one, so it is worth not forgetting.
      const items = Array.isArray(ssg) ? ssg : [];
      for (const p of items) {
        if (typeof p === "string" && p.startsWith("/")) out.ssg.push(template(p));
      }
    }

    const mw = s.__MIDDLEWARE_MATCHERS;
    if (Array.isArray(mw)) {
      found.push("__MIDDLEWARE_MATCHERS");
      for (const m of mw) {
        if (m && typeof m === "object" && typeof m.originalSource === "string") {
          // `/api/:function*` is the matcher as the developer wrote it in middleware config. Reported
          // verbatim: it states which paths are intercepted, which no route list tells you.
          out.middleware.push({ source: m.originalSource, has_regexp: typeof m.regexp === "string" });
        }
      }
    }

    out.sources.push({ file: base, found });
  }

  for (const rec of [...routes.values()].sort((a, b) => a.template.localeCompare(b.template))) {
    (rec.template.startsWith("/api/") || rec.template === "/api" ? out.api_routes : out.routes).push(rec);
  }
  out.chunks = [...chunks].sort();
  out.ssg = [...new Set(out.ssg)].sort();
  process.stdout.write(JSON.stringify(out, null, 1));
}

main();
