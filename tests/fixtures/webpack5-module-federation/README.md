Real output of webpack 5 with `ModuleFederationPlugin`. Its chunk-URL builder carries no id map at
all, `n.u = e => e + ".5eb33312.js"`, so harvesting object literals inside the builder found
nothing and it was never run. The ids are in the `__webpack_require__.e(480)` call sites, which is
where every lazy import asks for its chunk.
