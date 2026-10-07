# Benchmark

```bash
python bench/run.py ./bodies --host app.example.com
```

Runs every installed analyser over the **same** corpus of already-downloaded files and prints a
table. Tools missing from PATH are listed as absent rather than skipped silently.

Install the comparisons you want counted:

```bash
go install github.com/BishopFox/jsluice/cmd/jsluice@latest
git clone https://github.com/GerbenJavado/LinkFinder
pip install xnLinkFinder
```

**The table understates the result on purpose.** Every tool is reduced to a set of paths, because a
path is the only unit all of them emit. jshound's real output is a method, a body schema with
field provenance, a content type, the client's headers and the page that loads the call, and four
of those five have no column here. `+method` and `+body` report how much of each tool's result the
comparison cannot represent.

Source-file paths (`/src/foo.ts`) are dropped from every tool alike, so no one is credited for
build artifacts.

Publish the corpus you ran against, or the numbers mean nothing.
