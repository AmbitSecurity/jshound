/* literal.js — read a data literal out of a target's JavaScript without running any of it.
 *
 * A build manifest is data that ships as a program:
 *
 *     self.__BUILD_MANIFEST = { "/account/me": ["static/chunks/1ir8gf5k36l2m.js"], sortedPages: [...] };
 *     self.__BUILD_MANIFEST_CB && self.__BUILD_MANIFEST_CB()
 *     self.__SSG_MANIFEST = new Set(["/blog/[...slug]"]);
 *     window.__remixManifest = {"routes":{"root":{...}},"version":"..."};
 *
 * `JSON.parse` cannot read any of that — it is an assignment, and `new Set(...)` is not JSON — so the
 * first version of these readers EVALUATED the file in a `vm` context. That was the wrong tool. A vm
 * context is a separate realm, NOT a sandbox, and two escapes out of it were reproduced against those
 * versions:
 *
 *   1. `vm.createContext({ self: {} })` contextifies the sandbox, but `{}` was built in this realm, so
 *      inside the context `self.constructor.constructor` is our `Function` and
 *      `Function("return process")()` returns the real `process`.
 *   2. `Object.create(null)` closes that, but the value handed back is still the target's, so
 *      `{ get routes() { ... } }` runs that getter when our code reads `.routes` — after the timeout,
 *      on a stack that reaches our frames. `Error.prepareStackTrace` returning the CallSite array then
 *      gives `frames[i].getFunction()`, one of our own functions, and `.constructor("return process")()`
 *      follows from there.
 *
 * So nothing is executed. Babel parses the file, the right-hand side of the assignment is walked as
 * data, and anything that is not data becomes `undefined` rather than a guess — the same discipline
 * `chunkmap.js` uses on `__webpack_require__.u`. A manifest can still be wrong about its own routes,
 * which is what `precision` measures, but reading it cannot run anything here.
 */
const parser = require("@babel/parser");
const traverse = require("@babel/traverse").default;

const MAX_DEPTH = 24;
const MAX_ITEMS = 50000;

/* `undefined` means "not data" — the caller drops it rather than recording a guess. A literal `null`
 * is distinct and comes back as `null`. */
function evalLiteral(node, env, depth) {
  if (!node || depth > MAX_DEPTH) return undefined;
  switch (node.type) {
    case "StringLiteral":
    case "NumericLiteral":
    case "BooleanLiteral":
      return node.value;
    case "NullLiteral":
      return null;
    case "ParenthesizedExpression":
      return evalLiteral(node.expression, env, depth);
    case "Identifier":
      if (node.name === "undefined") return undefined;
      return env && Object.prototype.hasOwnProperty.call(env, node.name) ? env[node.name] : undefined;
    case "TemplateLiteral":
      // Nothing to interpolate without running it, so a template with a hole is not data.
      if (node.expressions.length) return undefined;
      return node.quasis.map(q => (q.value && q.value.cooked) || "").join("");
    case "UnaryExpression": {
      const v = evalLiteral(node.argument, env, depth + 1);
      if (node.operator === "-") return typeof v === "number" ? -v : undefined;
      if (node.operator === "+") return typeof v === "number" ? v : undefined;
      if (node.operator === "!") return v === undefined ? undefined : !v;
      return undefined;
    }
    case "ArrayExpression": {
      const out = [];
      for (const el of node.elements) {
        if (out.length >= MAX_ITEMS) break;
        if (!el) { out.push(null); continue; }                       // an elision
        if (el.type === "SpreadElement") {
          const s = evalLiteral(el.argument, env, depth + 1);
          if (Array.isArray(s)) for (const x of s) out.push(x);
          continue;
        }
        out.push(evalLiteral(el, env, depth + 1));
      }
      return out;
    }
    case "ObjectExpression": {
      const out = {};
      let n = 0;
      for (const pr of node.properties) {
        if (n >= MAX_ITEMS) break;
        if (pr.type === "SpreadElement") {
          const s = evalLiteral(pr.argument, env, depth + 1);
          if (s && typeof s === "object" && !Array.isArray(s)) Object.assign(out, s);
          continue;
        }
        // An ObjectMethod is a method or an accessor. A getter is exactly what escape (2) above used,
        // and it is not data either way, so it is skipped rather than invoked.
        if (pr.type !== "ObjectProperty") continue;
        let key = null;
        const k = pr.key;
        if (!pr.computed && (k.type === "StringLiteral" || k.type === "NumericLiteral")) key = String(k.value);
        else if (!pr.computed && k.type === "Identifier") key = k.name;
        else if (pr.computed) {
          const kv = evalLiteral(k, env, depth + 1);
          if (typeof kv === "string" || typeof kv === "number") key = String(kv);
        }
        if (key === null) continue;
        out[key] = evalLiteral(pr.value, env, depth + 1);
        n++;
      }
      return out;
    }
    case "BinaryExpression": {
      if (node.operator !== "+") return undefined;
      const l = evalLiteral(node.left, env, depth + 1);
      const r = evalLiteral(node.right, env, depth + 1);
      if (l === undefined || r === undefined) return undefined;
      if (typeof l === "number" && typeof r === "number") return l + r;
      const ok = v => typeof v === "string" || typeof v === "number";
      return ok(l) && ok(r) ? String(l) + String(r) : undefined;
    }
    case "LogicalExpression": {
      const l = evalLiteral(node.left, env, depth + 1);
      if (node.operator === "&&") return l === undefined ? undefined : (l ? evalLiteral(node.right, env, depth + 1) : l);
      if (node.operator === "||") return l === undefined ? undefined : (l ? l : evalLiteral(node.right, env, depth + 1));
      if (node.operator === "??") return l === undefined || l === null ? evalLiteral(node.right, env, depth + 1) : l;
      return undefined;
    }
    case "ConditionalExpression": {
      const t = evalLiteral(node.test, env, depth + 1);
      if (t === undefined) return undefined;
      return evalLiteral(t ? node.consequent : node.alternate, env, depth + 1);
    }
    case "NewExpression": {
      // `new Set([...])` is how `_ssgManifest.js` ships its prerendered list. Drained to an array,
      // which is what the reader wanted anyway: a Set built in a vm realm was never `instanceof Set`
      // out here, and that mismatch returned an empty prerendered list until a fixture caught it.
      const name = node.callee && node.callee.type === "Identifier" ? node.callee.name : null;
      if (name !== "Set" && name !== "Array") return undefined;
      if (!node.arguments.length) return [];
      const a = evalLiteral(node.arguments[0], env, depth + 1);
      return Array.isArray(a) ? a : undefined;
    }
    case "CallExpression":
      return callInline(node, env, depth);
    default:
      return undefined;
  }
}

/* Next shrinks a large manifest by hoisting repeated chunk names into the parameters of an
 * immediately-invoked function: `self.__BUILD_MANIFEST = function(a, b) { return {"/": [a, b]} }("x.js",
 * "y.js")`. Binding the arguments to the parameters and reading the returned literal is still not
 * execution — a body containing any statement other than the `return` is refused outright. */
function callInline(node, env, depth) {
  let fn = node.callee;
  while (fn && fn.type === "ParenthesizedExpression") fn = fn.expression;
  if (!fn || (fn.type !== "FunctionExpression" && fn.type !== "ArrowFunctionExpression")) return undefined;
  const params = fn.params || [];
  if (params.some(p => !p || p.type !== "Identifier")) return undefined;
  const local = Object.create(null);
  if (env) for (const k of Object.keys(env)) local[k] = env[k];
  params.forEach((p, i) => {
    local[p.name] = node.arguments[i] ? evalLiteral(node.arguments[i], env, depth + 1) : undefined;
  });
  if (fn.body.type !== "BlockStatement") return evalLiteral(fn.body, local, depth + 1);
  const stmts = fn.body.body || [];
  if (stmts.length !== 1 || stmts[0].type !== "ReturnStatement") return undefined;
  return stmts[0].argument ? evalLiteral(stmts[0].argument, local, depth + 1) : undefined;
}

function parse(src) {
  try {
    return parser.parse(src, { sourceType: "unambiguous", errorRecovery: true });
  } catch (e) {
    return null;
  }
}

/* The named globals a script assigns, as plain data. Named rather than walked, so a target cannot
 * enlarge what is read by assigning more. The first assignment of a name wins. */
function assignedGlobals(src, names) {
  const ast = parse(src);
  if (!ast) return { error: "could not be parsed" };
  const want = new Set(names);
  const out = {};
  traverse(ast, {
    AssignmentExpression(p) {
      const l = p.node.left;
      if (!l) return;
      let prop = null;
      if (l.type === "Identifier") prop = l.name;
      else if (l.type === "MemberExpression") {
        if (!l.computed) prop = l.property && (l.property.name || l.property.value);
        else if (l.property && l.property.type === "StringLiteral") prop = l.property.value;
      }
      if (!prop || !want.has(prop) || Object.prototype.hasOwnProperty.call(out, prop)) return;
      const v = evalLiteral(p.node.right, null, 0);
      if (v !== undefined) out[prop] = v;
    },
  });
  return out;
}

module.exports = { evalLiteral, assignedGlobals, parse, MAX_DEPTH };
