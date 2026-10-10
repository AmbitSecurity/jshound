# Results on a real application

[COMPARISON.md](COMPARISON.md) uses this repo's own fixture, which is the weakest kind of evidence:
the entrant wrote the exam. This is the same three tools on a real target with an **independent
answer key**: Grafana publishes its own OpenAPI document, so what counts as a real endpoint is not
our opinion.

## Reproduce it

```bash
jshound https://play.grafana.org/ -o ./grafana --max-chunks 150
curl -sO https://raw.githubusercontent.com/grafana/grafana/main/public/api-merged.json

python3 bench/truth.py api-merged.json --tool jshound=grafana/openapi.json
python3 bench/run.py grafana/js --host play.grafana.org,grafana-assets.grafana.net
```

The table below was measured with `--max-chunks 150`, and so are the other two tools' numbers,
on the same files. Leave the flag off to read the whole app.

Corpus when this was run: 163 scripts, ~23 MB, including 150 of the 694 lazy chunks the runtime
declares. Grafana ships continuously, so your numbers will differ. The method is the claim, not
the digits.

**Re-measured 2026-10-06 after the receiver and corroboration changes**, on a 159-script corpus
fetched later: `reported 827 / hits 184 / precision 30% / recall 89% / verb 100% / undocumented 56`.
Recall, precision, verb accuracy and hits are unchanged; `reported` and `undocumented` are lower than
the table below because the corpus is a different one, which is the drift this paragraph warns about.
The change's own effect was isolated by scoring the same corpus before and after it: **1047 to 1045
reported, every other column identical**, so the two records it removes are not endpoints the spec
knows about, and no quality column moved. They were `preferences/theme` (a command-palette action id)
and `starred/{min}` (`x = new Map(); x.get(...)`).

**Re-measured 2026-10-09 on the whole app**, 761 files with every chunk the runtime declares,
against the same spec: `reported 1092 / hits 184 / precision 26% / recall 89% / verb 100% /
undocumented 108`. Reading five times as many files found no more of the documented API, so the 22
spec paths still missing are not in the frontend at all; it found 108 undocumented paths against the
table's 69.
Precision falls because every undocumented endpoint counts against it, see below.

## Against Grafana's own OpenAPI document

206 paths / 314 operations, from the JavaScript alone.

| tool | reported | hits | precision | recall | verb accuracy | undocumented |
|---|---:|---:|---:|---:|---:|---:|
| **jshound** | 842 | 184 | 30% | **89%** | **100%** | **69** |
| jsluice | 416 | 91 | 29% | 44% | n/a | 21 |
| LinkFinder | 435 | 91 | 27% | 44% | n/a | 19 |

**Recall is exactly double, at the same precision.** The three tools are not separated by how much
they print. They print comparable amounts, with comparable amounts of it real. They are separated
by how much of the application they reach.

**Verb accuracy is not a column the others can lose; it is one they cannot enter.** jsluice does
report a method for the sinks it understands, which on this corpus it manages for 3 of 1,581
records, and LinkFinder has nowhere to put one. Of every method jshound reported for an endpoint
the spec also declares, it was right every time.

**`undocumented` is the column to read first.** Recovering an endpoint somebody already wrote down
is worth nothing for reconnaissance; the 69 paths that are in no spec are the ones you could not
have got from the documentation: `/api/access-control/user/actions`, `/api/alert-notifiers`,
`/api/alertmanager/{}/api/v2/alerts`.

### Why precision is ~30% for everyone

Because the reference is incomplete, not because two thirds of the output is junk. `api-merged.json`
describes Grafana's public HTTP API; a frontend also calls internal endpoints that appear in no
spec, and the same document is the only thing deciding what counts. Treat precision here as a floor.

## Output quality, same corpus

| tool | paths | api | content | asset | **noise** | with verb | with body |
|---|---:|---:|---:|---:|---:|---:|---:|
| **jshound** | 741 | **184** | 552 | 3 | **2** | **1087** | **40** |
| jsluice | 425 | 94 | 324 | 3 | 4 | 1 | 0 |
| LinkFinder | 446 | 92 | 336 | 4 | 14 | 0 | 0 |

`noise` is minifier debris, strings like `/VwdtK` that contain a slash and nothing else, bucketed
by the same rules for every tool ([classify.py](classify.py)).

Not shown, because the format cannot hold it: every jshound row also carries the page that loaded
the call, the sink it came from, and whether the path resolved fully or still contains a placeholder.

## What this is not

- **One target.** Grafana is a REST application with a real published spec. On a GraphQL or RPC app
  (Linear, Notion, X) there are few distinct paths to recover and these numbers would collapse for
  everyone. Say which kind of app you are testing before quoting any of this.
- **xnLinkFinder is absent, and did not earn a zero.** Its mandatory `-sf` filters by a link's
  *domain*, while paths out of a bundle are relative and have none, so every scope value removes the
  whole output; omitting it makes the tool refuse to start. It is built to crawl live URLs.
- **A moving corpus.** Re-run it. If the numbers no longer hold, the document is wrong and should
  be fixed rather than defended.

## 48 public apps, end to end

Every app in `bench/targets.txt`, run on 2026-10-09 with the one command, from one machine, as
`python3 bench/live.py bench/targets.txt --tag <tag>` reruns it. Nothing was signed into, so this
is what a logged-out browser downloads. Sites change weekly; the method is the claim, not the digits.

46 of 48 apps read: **41,357 files, 12,848 requests**, 5,317 of them with a
body and 3,498 GraphQL operations. 37 declared chunks could not be fetched, no file failed to
scan, and no limit was reached. Blocked: www.reddit.com (HTTP 403), www.tesla.com (Akamai).

| app | files read | requests | with a body | GraphQL | time |
|---|---:|---:|---:|---:|---:|
| lovable.dev | 10,362 | 2,048 | 618 | 0 | 446s |
| claude.ai | 2,920 | 1,623 | 299 | 1 | 331s |
| play.grafana.org | 761 | 1,513 | 80 | 0 | 190s |
| app.hex.tech | 1,448 | 1,332 | 1,227 | 1,217 | 218s |
| us.posthog.com | 2,140 | 998 | 61 | 1 | 220s |
| railway.com | 872 | 975 | 967 | 966 | 114s |
| gitlab.com/explore | 1,394 | 825 | 660 | 622 | 497s |
| app.twenty.com | 1,161 | 460 | 439 | 436 | 164s |
| discord.com/app | 5,793 | 418 | 194 | 10 | 395s |
| supabase.com/dashboard | 2,282 | 347 | 172 | 4 | 377s |
| svelte.dev | 56 | 342 | 4 | 0 | 15s |
| dashboard.clerk.com | 153 | 313 | 52 | 0 | 36s |
| app.amplitude.com | 596 | 271 | 253 | 216 | 64s |
| open.spotify.com | 166 | 144 | 0 | 0 | 72s |
| demo.home-assistant.io | 1,031 | 132 | 11 | 0 | 170s |
| www.dropbox.com | 6 | 116 | 8 | 0 | 52s |
| coda.io | 26 | 109 | 40 | 0 | 34s |
| retool.com | 45 | 94 | 2 | 0 | 14s |
| chatgpt.com | 920 | 85 | 38 | 0 | 84s |
| www.tldraw.com | 193 | 77 | 24 | 0 | 50s |
| x.com | 2,125 | 76 | 32 | 0 | 137s |
| meta.discourse.org | 167 | 69 | 18 | 0 | 59s |
| app.cal.com | 84 | 68 | 9 | 0 | 30s |
| github.com | 1,313 | 63 | 38 | 20 | 130s |
| linear.app | 725 | 44 | 19 | 5 | 57s |
| zapier.com/app/dashboard | 89 | 43 | 12 | 0 | 29s |
| app.slack.com | 17 | 42 | 2 | 0 | 13s |
| www.solidjs.com | 1,056 | 41 | 2 | 0 | 96s |
| mixpanel.com | 684 | 41 | 2 | 0 | 63s |
| nuxt.com | 195 | 28 | 11 | 0 | 53s |
| www.figma.com | 48 | 22 | 7 | 0 | 22s |
| vercel.com | 72 | 19 | 1 | 0 | 28s |
| www.notion.so | 1,717 | 19 | 2 | 0 | 258s |
| app.element.io | 12 | 12 | 1 | 0 | 21s |
| www.loom.com | 76 | 9 | 2 | 0 | 29s |
| excalidraw.com | 114 | 5 | 2 | 0 | 45s |
| app.hubspot.com | 6 | 5 | 2 | 0 | 5s |
| qwik.dev | 7 | 4 | 3 | 0 | 4s |
| material.angular.dev | 119 | 4 | 0 | 0 | 16s |
| webflow.com | 44 | 4 | 3 | 0 | 17s |
| angular.dev | 199 | 3 | 0 | 0 | 51s |
| remix.run | 113 | 1 | 0 | 0 | 8s |
| astro.build | 9 | 1 | 0 | 0 | 2s |
| emberjs.com | 8 | 1 | 0 | 0 | 16s |
| sentry.io | 28 | 1 | 0 | 0 | 7s |
| airtable.com | 5 | 1 | 0 | 0 | 13s |

A small count is usually the page, not the tool: astro.build, emberjs.com and sentry.io's home
pages are marketing sites that call almost nothing, and notion.so serves logged-out visitors a site
whose code names only three of its app's `/api/v3` endpoints. Time is wall time on one machine and moves with its
network; fetching is about two thirds of it.
