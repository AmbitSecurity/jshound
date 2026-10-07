Vite/Rollup writes its chunk paths down instead of building them at runtime, in two shapes: a
dynamic import's own literal specifier, and the `__vite__mapDeps` dependency table that
`__vitePreload` indexes into. The webpack-only pass found neither — measured on real builds, tldraw
ships 63 asset-hash chunk paths and excalidraw 96 dynamic imports, and both returned 0 structured
chunks.

`notChunks` is here on purpose: an array of plain strings must not be collected as a dependency
table. Requiring every element to be chunk-shaped is the only thing separating the two.
