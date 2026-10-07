/* Nothing in this file runs — it is READ. Every line below would have done something to the host under
 * the `vm` evaluator this replaced, so the route table doubles as the assertion: if the realm escape
 * works, `sortedPages` gains an `/escaped-<pid>` entry, and if statements run at all, `process.exit`
 * ends the run before anything is printed. */
var escaped = null;
try { escaped = self.constructor.constructor("return process")(); } catch (e) {}
self.__BUILD_MANIFEST = {
  "/a": ["static/chunks/a.js"],
  sortedPages: ["/a"]
};
if (escaped) { self.__BUILD_MANIFEST.sortedPages.push("/escaped-" + escaped.pid); }
process.exit(1);
