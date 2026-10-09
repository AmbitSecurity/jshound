#!/usr/bin/env node
/* imports.js, every module specifier a JavaScript source references, by parsing it.
 *
 *     node imports.js a.js b.js            # prints {"a.js": ["./chunk.js", ...], ...}
 *
 * Replaces a regex that read `(?:\bfrom|\bimport)\s*\(?\s*["']([^"']+\.m?js)["']`. Measured against
 * that pattern on one file holding every form below, it fails in BOTH directions: it invented
 * `./in-a-comment.js` and `./in-a-string.js`, the word `import` followed by a quoted filename is a
 * match whether or not it is code, and it missed `./side-effect.css` and `./no-extension`, because
 * the specifier is required to end in `.js`. Parsing has neither problem, and it is how the rest of
 * this repo reads JavaScript.
 *
 * Four forms count as a reference, because all four make the browser fetch another module:
 *
 *     import x from "./a.js"           ImportDeclaration
 *     export { y } from "./b.js"       ExportNamedDeclaration with a source
 *     export * from "./c.js"           ExportAllDeclaration
 *     import("./d.js")                 dynamic import, the one SvelteKit's inline bootstrap uses
 *
 * Only static string specifiers are reported. `import(someVariable)` names no file that can be read
 * from here, and guessing at one is how a fetcher starts requesting URLs the app never had.
 */
const fs = require("fs");
const parser = require("./babel.js").parser;
const traverse = require("./babel.js").traverse.default;

function specifiers(src) {
  const out = [];
  let ast;
  try {
    ast = parser.parse(src, {
      sourceType: "unambiguous",
      errorRecovery: true,
      plugins: ["importAttributes", "jsx"],
    });
  } catch (e) {
    return { specifiers: [], error: String((e && e.message) || e) };
  }
  const add = (node) => {
    if (node && node.type === "StringLiteral" && node.value) out.push(node.value);
  };
  traverse(ast, {
    ImportDeclaration(p) { add(p.node.source); },
    ExportNamedDeclaration(p) { add(p.node.source); },
    ExportAllDeclaration(p) { add(p.node.source); },
    CallExpression(p) {
      // `import("./x.js")` parses as a CallExpression whose callee is the Import node.
      if (p.node.callee && p.node.callee.type === "Import") add((p.node.arguments || [])[0]);
    },
  });
  return { specifiers: [...new Set(out)] };
}

function main() {
  const out = {};
  for (const file of process.argv.slice(2)) {
    let src;
    try {
      src = fs.readFileSync(file, "utf8");
    } catch (e) {
      out[file] = { specifiers: [], error: String((e && e.message) || e) };
      continue;
    }
    out[file] = specifiers(src);
  }
  process.stdout.write(JSON.stringify(out));
}

main();
