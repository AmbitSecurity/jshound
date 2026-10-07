import { d, t } from "../chunks/Bex9Cvfl.js";
const N = d(`<!> <!>`, 1);
const L = N();
const C = x => x;
const R = C(L);
function helper() {
  const s = { "not": "a route table" };
  return s;
}
const s = {
  "/": [5],
  "/(authed)/apps": [-7, [2]],
  "/blog": [-12],
  "/blog/[slug]": [13],
  "/docs/[topic]/[...path=documentation]": [14, [2, 3]],
  "/maybe/[[opt]]": [15]
};
const o = [0, 2, 3, 4];
const i = t({ documentation(e) { return e; } });
const a = [() => import("../nodes/0.js")];
export { s as dictionary, i as matchers, a as nodes, o as server_loads };
