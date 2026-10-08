#!/usr/bin/env python3
"""Render a captured `jshound` run as the terminal graphic in the README.

    python3 docs/make_demo_svg.py <captured-stdout> <captured-stderr> -o docs/demo.svg

A screenshot of a real terminal carries whoever took it: a username, a home directory, a prompt
nobody else has. This draws the same thing from the output itself, so the graphic is the tool's real
output and nothing else, and it can be regenerated when the numbers move.

Rows are PICKED, not invented. Every line in the result is present verbatim in the captured stdout,
which `--check` re-asserts, and the trailing ellipsis is there because 224 rows do not fit in a
graphic.
"""
import argparse
import html
import re
import sys

BG, BAR, FG, DIM = "#16181d", "#1f222a", "#c0caf5", "#565f89"
VERB = {"GET": "#9ece6a", "POST": "#e0af68", "DELETE": "#f7768e",
        "PUT": "#bb9af7", "PATCH": "#bb9af7", "HEAD": "#7dcfff"}
BODY, CTYPE, NUM = "#7dcfff", "#6b7394", "#7aa2f7"
FONT = ("ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, "
        "'DejaVu Sans Mono', monospace")
PAD_X, TOP, LH, FS = 26, 62, 23, 14.5
# A monospace advance is 0.6em in every face this names. Columns are placed with it
# rather than by flowing text, so alignment does not depend on the renderer.
ADV = FS * 0.6
_CT = re.compile(r"\s(application/[\w.+-]+\??|multipart/[\w-]+\??|text/[\w-]+\??)\s*$")


def spans(line):
    """-> [(char_offset, text, colour)] for one row.

    Every segment carries its COLUMN, and the renderer is given an explicit x for each one. Letting
    consecutive tspans flow and padding them with whitespace tspans is the obvious way to do this
    and it does not survive: a renderer is free to collapse whitespace between tspans even under
    `xml:space="preserve"`, which drew every verb on top of its own path.
    """
    m = re.match(r"^([A-Z]+)(\s+)(\S+)(.*)$", line)
    if not m:
        return [(0, line, FG)]
    verb, gap, path, rest = m.groups()
    out = [(0, verb, VERB.get(verb, FG)), (len(verb) + len(gap), path, FG)]
    at = len(verb) + len(gap) + len(path)
    ct = _CT.search(rest)
    if ct:
        body = rest[:ct.start(1)]
        out.append((at + len(body) - len(body.lstrip()), body.strip(), BODY))
        out.append((at + ct.start(1), ct.group(1), CTYPE))
    elif rest.strip():
        out.append((at + len(rest) - len(rest.lstrip()), rest.strip(), BODY))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stdout")
    ap.add_argument("stderr")
    ap.add_argument("-o", "--out", default="docs/demo.svg")
    ap.add_argument("--rows", type=int, default=14)
    a = ap.parse_args()

    captured = open(a.stdout).read()
    # NOT `" endpoints (" in l`: "merged 511 duplicate call site(s) into their endpoints (per
    # host..." matches that and is not the summary.
    summary = next((l.strip() for l in open(a.stderr)
                    if re.match(r"^\d+ scripts -> \d+ endpoints \(", l.strip())), "")
    rows = [l.rstrip() for l in captured.splitlines() if re.match(r"^[A-Z]+\s+/", l)]
    # A spread worth looking at: the ones carrying a body contract first, since that is the thing no
    # other tool in this space reports, then fill with plain ones so the verbs are not all POST.
    def dull(r):
        path = r.split()[1]
        return path == "/" or path.startswith(("/_next", "/_app")) or path.count("/") < 2

    withbody = [r for r in rows if '{"' in r and not dull(r)]
    plain = [r for r in rows if '{"' not in r and not dull(r)]
    # One of every verb that appears, so the graphic does not look like a list of POSTs, then fill
    # with body-carrying rows because a body contract is the thing no other tool in this space
    # reports.
    picked, verbs = [], set()
    for r in withbody + plain:
        v = r.split()[0]
        if v not in verbs:
            verbs.add(v)
            picked.append(r)
    picked += [r for r in withbody if r not in picked]
    picked = picked[:a.rows]
    picked.sort(key=lambda r: (r.split()[1], r.split()[0]))
    for r in picked:
        assert r in captured, r

    lines = ([[(0, "$", DIM), (2, "jshound supabase.com/dashboard", "#ffffff")],
              [(0, summary, NUM)], [(0, "", FG)]]
             + [spans(r) for r in picked] + [[(0, "...", DIM)]])
    w = max((max((c + len(t)) for c, t, _ in ln) if ln else 0) for ln in lines)
    W = int(PAD_X * 2 + w * ADV) + 10
    H = TOP + LH * len(lines) + 20

    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
         f'viewBox="0 0 {W} {H}" font-family="{html.escape(FONT)}" font-size="{FS}">',
         f'<rect width="{W}" height="{H}" rx="10" fill="{BG}"/>',
         f'<path d="M0 10a10 10 0 0 1 10-10h{W - 20}a10 10 0 0 1 10 10v26H0z" fill="{BAR}"/>']
    for i, c in enumerate(("#ff5f56", "#ffbd2e", "#27c93f")):
        o.append(f'<circle cx="{22 + i * 19}" cy="18" r="6" fill="{c}"/>')
    for n, ln in enumerate(lines):
        y = TOP + n * LH
        for col, text, colour in ln:
            if not text:
                continue
            x = PAD_X + col * ADV
            # `textLength` pins the run to exactly its column width. Without it the layout is at
            # the mercy of whatever monospace the viewer has, and a 0.6em assumption that is a few
            # thousandths out drifts far enough across a 100-character row to run the body into the
            # content type. A README graphic is rendered on someone else's machine, so it cannot
            # depend on ours.
            o.append(f'<text x="{x:.1f}" y="{y}" fill="{colour}" xml:space="preserve" '
                     f'textLength="{len(text) * ADV:.1f}" lengthAdjust="spacingAndGlyphs">'
                     f'{html.escape(text)}</text>')
    o.append("</svg>")
    open(a.out, "w").write("\n".join(o) + "\n")
    print(f"  {a.out}: {len(picked)} rows, {W}x{H}", file=sys.stderr)


if __name__ == "__main__":
    main()
