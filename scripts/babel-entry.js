// Entry point for jshound/js/babel.js, which `npm run bundle` builds from this file.
//
// The extractors need three Babel packages. Bundled into one file that ships inside the Python
// package, an installed jshound needs node on the PATH and nothing else: no `npm install`, no
// checkout, no node_modules. The versions are the ones package-lock.json pins.
module.exports = {
  parser: require("@babel/parser"),
  traverse: require("@babel/traverse"),
  generator: require("@babel/generator"),
};
