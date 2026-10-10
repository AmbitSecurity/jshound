"""Draw docs/how-it-works.svg: the four stages, and one request traced through three modules.

The example at the bottom is real output: those three files, run through jshound, give exactly the
request shown. Same palette and type as docs/demo.svg.

    python3 docs/make_how_svg.py
"""
import os
from xml.sax.saxutils import escape

W, H = 1200, 610
CW, FS = 8.4, 14          # monospace advance and size; every run is pinned to CW per character
BG, PANEL, EDGE = "#16181d", "#1f222a", "#2a2e38"
FG, DIM, BLUE, GREEN, YELLOW, PURPLE, CYAN = ("#c0caf5", "#6b7394", "#7aa2f7", "#9ece6a", "#e0af68",
                                              "#bb9af7", "#7dcfff")
out = []


def text(x, y, s, fill=FG, size=FS, weight=None):
    w = f' font-weight="{weight}"' if weight else ""
    cw = CW * size / FS
    out.append(f'<text x="{x:.1f}" y="{y:.1f}" fill="{fill}" font-size="{size}"{w} xml:space="preserve" '
               f'textLength="{len(s) * cw:.1f}" lengthAdjust="spacingAndGlyphs">{escape(s)}</text>')


def runs(x, y, parts):
    """One line in several colours: [(text, colour), ...]. Returns the x of each part's start."""
    starts = []
    for s, fill in parts:
        starts.append(x)
        if s:
            text(x, y, s, fill)
        x += len(s) * CW
    return starts


def box(x, y, w, h, fill=PANEL):
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{fill}" stroke="{EDGE}"/>')


def arrow(x1, y1, x2, y2, colour=DIM):
    out.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{colour}" stroke-width="2" '
               f'marker-end="url(#head)"/>')


# ── the four stages ────────────────────────────────────────────────────────────────────────────────
stages = [
    ("1  Collect", [["the page you give it"], ["every script it loads,", "on any host"],
                    ["lazy chunks and imports,", "round after round, until", "nothing new turns up"]]),
    ("2  Parse", [["every file, with Babel;", "nothing it ships is run"],
                  ["each place a request is", "made: fetch, XHR, axios,", "API clients, GraphQL, RPC"]]),
    ("3  Trace", [["each argument back to", "its value, through", "variables, imports and", "other modules"],
                  ["what cannot be resolved", "stays as the code wrote it"]]),
    ("4  Write", [["endpoints.txt"], ["requests.jsonl"], ["openapi.json"], ["urls.txt"]]),
]
cx, cy, cw_, ch, gap = 30, 28, 255, 212, 40
for i, (title, items) in enumerate(stages):
    x = cx + i * (cw_ + gap)
    box(x, cy, cw_, ch)
    text(x + 18, cy + 32, title, BLUE, 16, "bold")
    y = cy + 64
    for item in items:
        for j, ln in enumerate(item):
            if j == 0 and i < 3:
                text(x + 18, y, "•", DIM)
            text(x + 34 if i < 3 else x + 18, y, ln, FG if i < 3 else GREEN)
            y += 21
        if i < 3:
            y += 6
    if i < 3:
        arrow(x + cw_ + 6, cy + ch / 2, x + cw_ + gap - 8, cy + ch / 2)

# ── one request, three modules ─────────────────────────────────────────────────────────────────────
py = 272
box(30, py, 1140, 310, BG)
text(52, py + 34, "One request, traced through three modules", DIM)
rows = [
    ("config.js", [("export const ", PURPLE), ("API_URL", YELLOW), (" = ", FG), ('"https://api.acme.io"', GREEN),
                   (";", FG)]),
    ("client.js", [("export const ", PURPLE), ("api", CYAN), (" = axios.create({ baseURL: ", FG), ("API_URL", YELLOW),
                   (" });", FG)]),
    ("invites.js", [("api", CYAN), (".post(`/orgs/${", FG), ("orgId", FG), ("}/invites`, { email, role: ", FG),
                    ('"member"', GREEN), (" })", FG)]),
]
code_x, label_x, y0, dy = 190, 52, py + 84, 58
marks = []
for k, (label, parts) in enumerate(rows):
    y = y0 + k * dy
    box(label_x - 10, y - 26, 1080, 40, PANEL)
    text(label_x, y, label, DIM)
    marks.append((runs(code_x, y, parts), parts, y))


def centre(row, part):
    starts, parts, y = marks[row]
    return starts[part] + len(parts[part][0]) * CW / 2, y


# where each value is defined, to where it is used
for (r1, p1), (r2, p2), colour in [((0, 1), (1, 3), YELLOW), ((1, 1), (2, 0), CYAN)]:
    x1, ya = centre(r1, p1)
    x2, yb = centre(r2, p2)
    out.append(f'<path d="M{x1:.1f} {ya + 6} C{x1:.1f} {ya + 30} {x2:.1f} {yb - 44} {x2:.1f} {yb - 22}" '
               f'fill="none" stroke="{colour}" stroke-width="1.6" stroke-dasharray="4 4" marker-end="url(#head2)"/>')

ry = y0 + 3 * dy + 14
text(label_x, ry - 22, "jshound", DIM)
runs(code_x, ry - 22, [("POST ", BLUE), ("https://api.acme.io", YELLOW), ("/orgs/{param_orgId}/invites", FG)])
runs(code_x, ry + 2, [('{"email":"input:email","role":"string"}', GREEN)])

svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
       f'font-family="ui-monospace, SFMono-Regular, \'SF Mono\', Menlo, Consolas, \'DejaVu Sans Mono\', monospace" '
       f'font-size="{FS}">',
       '<defs>'
       f'<marker id="head" markerWidth="10" markerHeight="10" refX="8" refY="5" orient="auto">'
       f'<path d="M0 0 L10 5 L0 10 z" fill="{DIM}"/></marker>'
       f'<marker id="head2" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto">'
       f'<path d="M0 0 L8 4 L0 8 z" fill="{DIM}"/></marker>'
       '</defs>',
       f'<rect width="{W}" height="{H}" rx="10" fill="{BG}"/>'] + out + ["</svg>"]
path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "how-it-works.svg")
with open(path, "w") as fh:
    fh.write("\n".join(svg) + "\n")
print(path)
