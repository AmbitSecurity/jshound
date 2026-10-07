# Contributing

The useful contribution here is usually **a fixture, not a patch**.

Bundlers change, and each one hides endpoints differently. webpack 5 builds chunk URLs at runtime
while webpack 4 uses a named function; Vite writes them down as literals; Turbopack ships a literal
array; Module Federation ships no id table at all; Parcel keys its table by object. Every one of
those was a gap until a fixture proved it, and the README's **What it works on** section records what
is verified against what.

So the most useful thing you can send is a bundler, or a build configuration, that the README does
not already cover, or one it does cover that behaves differently for you. That starts with a file
that fails.

## Adding a fixture

Drop a minimal bundle into `tests/fixtures/<bundler>-<what-it-exercises>/` with an `expected.json`
beside it:

```
tests/fixtures/vite-flat-esm/
  app.js            the smallest bundle that reproduces the problem
  expected.json     {"endpoints": [{"method": "POST", "template": "/v1/thing"}], "chunks": []}
  README.md         one paragraph: what the bundler does here, and why a grep cannot see it
```

Rules that keep the corpus usable:

- **Synthetic, or your own build output, never scraped.** Hand-write the smallest bundle that
  reproduces the shape, or commit the output of building a tiny app with the bundler itself, which
  is how `parcel-build`, `metro-rn` and `webpack5-module-federation` were made. Never a PRIVATE or
  authenticated target's JavaScript, and never anything that names a real target: it is someone
  else's code, it dates fast, and it makes the repo a redistribution of other people's work.

  One narrow exception, which three fixtures use: a PUBLIC build manifest may be excerpted down to
  the few lines a test needs, with hostnames replaced by reserved example domains. Those are route
  paths and identifiers rather than program code, and they are attributed in [NOTICE](NOTICE).
- **Minimal.** If deleting a line still reproduces the gap, delete it.
- **Say what should happen.** `expected.json` is the claim. A fixture with no expectation is a bug
  report, which is also welcome. Just open an issue instead.

A failing fixture is a complete contribution. You do not have to fix it.

## Patches

If you do fix it, the one rule that is not negotiable: **a result the analysis could not resolve
must stay marked.** An unresolvable slice becomes `{EXPR}`, a partial body gets `body_partial`, and
an inferred encoding records its basis. Every one of those markers is what lets someone downstream
trust the records that are *not* marked. A patch that raises the count by quietly promoting guesses
to facts makes the tool worse, and it will be declined however good the number looks.

Run the suite before opening a PR:

```bash
pip install -e . pytest openapi-spec-validator && pytest tests -q
for f in jshound/js/*.js; do node --check "$f"; done
```

## Benchmarks

Numbers in a PR need the corpus they came from, or they are not numbers. `bench/run.py` buckets
every tool's output by the same rules so that minifier debris cannot be counted as attack surface.
if you are reporting a comparison, use it, and say what you ran it against.

## Changing the extractor: diff the records, not the totals

A change to extraction is not measurable by a count. Run it both ways and diff per record:

```bash
CORPORA=~/.cache/jshound-corpora bench/regress.sh before    # on the unmodified tree
# ...make the change...
CORPORA=~/.cache/jshound-corpora bench/regress.sh after
python3 bench/regress_diff.py before after
```

Build the corpora with `jshound fetch`, one directory per target, and keep them outside the repo;
they are megabytes of someone else's JavaScript and they go stale as targets ship, which is why the
README's measured tables carry the date they were taken.

**Read `lost` first, and read the template names rather than the numbers.** Three real results from one
session, each of which a count would have read backwards:

| | what the count said | what the records said |
|---|---|---|
| 886 to **888** | small gain | 27 fabricated templates removed, 29 real ones recovered in their place. Kept |
| 886 to **887** | small gain | a base glued onto the wrong API; 4 correct records displaced by 5 wrong ones. Reverted |
| 1736 to **1737** | small gain | 1 real endpoint gained, one parameter name degraded from `{param:f}` to `{unary}`. Reverted |

Two of those three changes looked like improvements and made the output worse. A third looked
net-negative on a 60-file subset of one corpus and was fine on the whole thing. Subsets flatter
changes, so run the full set before believing a result.

## Licensing of contributions

By submitting a contribution you license it under the MIT License in this repository's
[LICENSE](LICENSE) file.
