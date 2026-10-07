/* The second escape, which `Object.create(null)` did not close: the evaluator handed back the target's
 * own object, so reading a property off it ran the target's getter in the host realm, after the
 * timeout, on a stack reaching into host frames. `Error.prepareStackTrace` turns that stack into
 * CallSites, `getFunction()` returns a host function, and its `.constructor` is the host `Function`.
 * A getter is not data, so it is skipped rather than invoked, and `/b` still comes back. */
self.__BUILD_MANIFEST = {
  "/b": ["static/chunks/b.js"],
  get sortedPages() {
    Error.prepareStackTrace = function (e, frames) { return frames; };
    var frames = new Error().stack;
    for (var i = 0; i < frames.length; i++) {
      var f = frames[i].getFunction && frames[i].getFunction();
      if (f) {
        try { return ["/escaped-" + f.constructor("return process")().pid]; } catch (e) {}
      }
    }
    return ["/b"];
  }
};
