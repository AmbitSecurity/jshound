Real output of `metro build --platform web` (React Native / RN Web). Contract extraction worked on
it unchanged — fetch, `new URL(path, base)` and `XMLHttpRequest.open` all resolved — but the bundler
went undetected: Metro's registry is `__d(factory, id, deps)` and `__r(id)`, one and two characters,
far too generic to match on. `__METRO_GLOBAL_PREFIX__` and `__BUNDLE_START_TIME__` are not, and every
Metro bundle opens by assigning them.
