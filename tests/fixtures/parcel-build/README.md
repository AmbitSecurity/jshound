Real output of `parcel build` (Parcel 2). Two things broke on it:

* the runtime global is suffixed per build — `parcelRequire5707`, not `parcelRequire` — so an
  exact-name map detected a real Parcel build as no bundler at all;
* the chunk table is an OBJECT, `Object.assign(o.i ??= {}, {jrWAC: "feature.9e64f881.js"})`, where
  the Vite pass only read arrays.
