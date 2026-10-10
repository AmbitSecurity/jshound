#!/usr/bin/env python3
"""Render a captured `jshound` run as the terminal graphic in the README.

    python3 docs/make_demo_svg.py <captured-stdout> <captured-stderr> -o docs/demo.svg

A screenshot of a real terminal carries whoever took it: a username, a home directory, a prompt
nobody else has. This draws the same thing from the output itself, so the graphic is the tool's real
output and nothing else, and it can be regenerated when the numbers move.

Rows are PICKED, not invented. Every row is a row of the captured stdout, field for field, which is
asserted before anything is drawn. The columns are re-aligned to the picked rows, because the real
table is as wide as its longest URL out of hundreds, and the trailing ellipsis is there because the
rest do not fit in a graphic.
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
STATUS = {"2": "#9ece6a", "3": "#7dcfff", "4": "#e0af68", "5": "#f7768e"}


def fields(line):
    """A table row as (method, url, content type, body). Columns are separated by two or more
    spaces, and none of the four contains two spaces in a row."""
    parts = re.split(r"\s{2,}", line.strip())
    method, url = line.split()[0], line.split()[1]
    rest = [x for x in parts[1:] if x != url] if parts[0] != method else parts[2:] if len(parts) > 2 else []
    ctype = next((x for x in rest if re.match(r"^(application|multipart|text)/", x)), "")
    body = next((x for x in rest if x != ctype), "")
    return method, url, ctype, body


def row_spans(f, wm, wu, wc):
    """-> [(char_offset, text, colour)], with each column at an explicit offset.

    A renderer is free to collapse whitespace between tspans even under `xml:space="preserve"`,
    which drew every verb on top of its own path, so nothing is placed by flowing text.
    """
    method, url, ctype, body = f
    out = [(0, method, VERB.get(method, FG)), (wm, url, FG)]
    if ctype:
        out.append((wm + wu + 2, ctype, CTYPE))
    if body:
        out.append((wm + wu + 2 + (wc + 2 if wc else 0), body, BODY))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stdout")
    ap.add_argument("stderr")
    ap.add_argument("-o", "--out", default="docs/demo.svg")
    ap.add_argument("--rows", type=int, default=14)
    ap.add_argument("--target", default="supabase.com/dashboard")
    ap.add_argument("--max-width", type=int, default=118, help="skip rows wider than this")
    a = ap.parse_args()

    captured = open(a.stdout).read()
    err = open(a.stderr).read().splitlines()
    summary = next((l.rstrip() for l in err if re.search(r"\s->\s+\d+ endpoints", l)), "")
    first = next((l.rstrip() for l in err if re.match(r"^\s+\d{3}\s", l)), "")
    table = [fields(l) for l in captured.splitlines() if re.match(r"^[A-Z]+\s+\S", l)]

    # A spread worth looking at: one of every verb, then rows carrying a body contract, since that
    # is the thing no other tool in this space reports. Rows too wide for a graphic are passed over
    # rather than cut, and the ellipsis at the bottom says the rest exist.
    def fits(f):
        return 7 + len(f[1]) + 2 + len(f[2]) + 2 + len(f[3]) <= a.max_width

    def dull(f):
        # A row whose verb is not known is in the output, honestly, but a sample of what the tool reads
        # should show what it reads.
        return "/_next" in f[1] or f[1].count("/") < 3 or f[0] == "UNKNOWN"

    withbody = [f for f in table if f[3].startswith('{"') and fits(f) and not dull(f)]
    plain = [f for f in table if not f[3] and fits(f) and not dull(f)]
    picked, verbs = [], set()
    for f in withbody + plain:
        if f[0] not in verbs:
            verbs.add(f[0])
            picked.append(f)
    # Then round-robin over API areas (origin plus first path segment), so a graphic of fourteen
    # rows is not fourteen rows of one router.
    def area(f):
        m = re.match(r"^((?:https?://[^/]+|\{[^}]+\})?/[^/]*)", f[1])
        return m.group(1) if m else f[1]

    groups = {}
    for f in withbody:
        groups.setdefault(area(f), []).append(f)
    seen_areas = {area(f) for f in picked}
    while len(picked) < a.rows and any(groups.values()):
        for k in sorted(groups, key=lambda k: (k in seen_areas, k)):
            if groups[k] and len(picked) < a.rows:
                f = groups[k].pop(0)
                if f not in picked:
                    picked.append(f)
        seen_areas = set()
    picked = sorted(picked, key=lambda f: (f[1], f[0]))
    for f in picked:
        assert f in table, f

    wm = max(7, max(len(f[0]) for f in picked) + 1)
    wu = max(len(f[1]) for f in picked)
    wc = max(len(f[2]) for f in picked)
    m = re.match(r"^(\s+)(\d{3})(\s+\S+\s+)(\S+)$", first)
    log = ([(len(m.group(1)), m.group(2), STATUS.get(m.group(2)[0], FG)),
            (len(m.group(1) + m.group(2) + m.group(3)), m.group(4), DIM),
            (len(m.group(1) + m.group(2)) + len(m.group(3)) - len(m.group(3).lstrip()),
             m.group(3).strip(), DIM)] if m else [(0, first, DIM)])
    lines = ([[(0, "$", DIM), (2, f"jshound {a.target}", "#ffffff")],
              log, [(2, "...", DIM)], [(0, summary, NUM)], [(0, "", FG)]]
             + [row_spans(f, wm, wu, wc) for f in picked] + [[(0, "...", DIM)]])
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
