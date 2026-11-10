#!/usr/bin/env python3
"""Render the neofetch-style profile card (dark_mode.svg + light_mode.svg) with live GitHub stats.

Token: $GH_TOKEN / $GITHUB_TOKEN, else `gh auth token`. Private repos and contributions are counted
when the token can see them (classic PAT with `repo`, or fine-grained with read access to all repos).
"""
import colorsys
import datetime as dt
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html import escape
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parent
LOGIN = "richyk1"
AVATAR = ROOT / "avatar.png"  # background-removed cutout; transparent pixels become blank space
RAMP = " .:-=+*#%@"  # sparse → dense
ART_COLS, ART_ROWS = 40, 24
WIDTH = 62  # info panel width in characters
CHAR_W, LINE_H, PAD = 9.6, 20, 15  # 16px monospace advances ~0.6em
# Commits touching more lines than this are counted as commits but left out of Lines of Code:
# they are generated/vendored dumps (one repo alone adds 7.9M and deletes 12.4M lines that way).
MAX_COMMIT_LINES = 10_000

THEMES = {  # Catppuccin Mocha / Latte
    "dark": dict(bg="#1e1e2e", fg="#cdd6f4", key="#cba6f7", value="#89dceb", cc="#585b70", add="#a6e3a1", dele="#f38ba8"),
    "light": dict(bg="#eff1f5", fg="#4c4f69", key="#8839ef", value="#209fb5", cc="#9ca0b0", add="#40a02b", dele="#d20f39"),
}

# (key, value) rows; None = "." spacer; ("", "") = empty line; ("#", title) = header.
# Pairs ((k, v), (k, v)) render as two columns. Values: $stat substitutes, [add]..[/add] / [del]..[/del] colour.
CONTENT = [
    ("#", "koolkid@richyk1"),
    ("Uptime", "$uptime"),
    ("Host", "Europe"),
    ("Kernel", "Binary reverse engineering"),
    ("Shell", "phenom"),
    None,
    ("Languages.Systems", "C, C++, C#, Rust"),
    ("Languages.Scripting", "Python, TypeScript, JavaScript"),
    ("Languages.Web", "React, Next.js, Node.js, GraphQL"),
    None,
    ("Interests.Now", "Binary RE, fast code"),
    ("Interests.Collab", "Binary exploitation, VM obfuscation"),
    ("Debuffs", "Machine learning, hardware, networking"),
    ("Trained.By", "TheNewBoston"),
    ("", ""),
    ("#", "Contact"),
    ("Discord", "koolkid#9999"),
    ("StackOverflow", "users/9256754"),
    ("GitHub", "richyk1"),
    ("", ""),
    ("#", "GitHub Stats"),
    (("Repos", "$repos"), ("Stars", "$stars")),
    (("Contributed", "$contributed"), ("Followers", "$followers")),
    (("Commits", "$commits"), ("Contribs", "$contribs")),
    ("Lines of Code", "$loc ( [add]$loc_add++[/add], [del]$loc_del--[/del] )"),
]

# ---------------------------------------------------------------- stats


def token() -> str:
    tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    return tok or subprocess.run(["gh", "auth", "token"], check=True, capture_output=True, text=True).stdout.strip()


def gql(tok: str, query: str, **variables):
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"bearer {tok}", "User-Agent": f"{LOGIN}-profile-card"},
    )
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.load(resp)
            break
        except urllib.error.HTTPError as e:  # history diffs are computed on demand; GitHub's gateway times out under load
            if e.code not in (502, 503, 504) or attempt == 4:
                raise
            time.sleep(2 ** attempt)
    if body.get("errors"):
        raise RuntimeError(body["errors"])
    return body["data"]


USER_Q = "query($login:String!){user(login:$login){id createdAt followers{totalCount}}}"
REPOS_Q = """query($login:String!,$after:String){user(login:$login){repositories(first:100,after:$after,
  ownerAffiliations:[OWNER,COLLABORATOR,ORGANIZATION_MEMBER]){pageInfo{hasNextPage endCursor}
  nodes{name isFork stargazerCount owner{login}}}}}"""
HISTORY_Q = """query($owner:String!,$name:String!,$id:ID!,$after:String){repository(owner:$owner,name:$name){
  defaultBranchRef{target{...on Commit{history(first:50,after:$after,author:{id:$id}){
  totalCount pageInfo{hasNextPage endCursor} nodes{additions deletions parents{totalCount}}}}}}}}"""
CONTRIB_Q = """query($login:String!,$from:DateTime!,$to:DateTime!){user(login:$login){
  contributionsCollection(from:$from,to:$to){contributionCalendar{totalContributions}}}}"""


def repo_history(tok: str, owner: str, name: str, uid: str):
    commits = adds = dels = 0
    after = None
    while True:
        ref = gql(tok, HISTORY_Q, owner=owner, name=name, id=uid, after=after)["repository"]["defaultBranchRef"]
        if ref is None:  # empty repository
            return 0, 0, 0
        hist = ref["target"]["history"]
        commits = hist["totalCount"]
        # Merge diffs repeat the merged branch's changes, which are already counted on their own commits.
        sized = [(n["additions"], n["deletions"]) for n in hist["nodes"] if n["parents"]["totalCount"] <= 1]
        adds += sum(a for a, d in sized if a + d <= MAX_COMMIT_LINES)
        dels += sum(d for a, d in sized if a + d <= MAX_COMMIT_LINES)
        if not hist["pageInfo"]["hasNextPage"]:
            return commits, adds, dels
        after = hist["pageInfo"]["endCursor"]


def uptime(since: dt.date, today: dt.date) -> str:
    y, m, d = today.year - since.year, today.month - since.month, today.day - since.day
    if d < 0:
        m -= 1
        d += (today.replace(day=1) - dt.timedelta(days=1)).day
    if m < 0:
        y -= 1
        m += 12
    return ", ".join(f"{n} {u}{'s' if n != 1 else ''}" for n, u in ((y, "year"), (m, "month"), (d, "day")))


def fetch_stats() -> dict:
    tok = token()
    user = gql(tok, USER_Q, login=LOGIN)["user"]
    created = dt.datetime.fromisoformat(user["createdAt"].replace("Z", "+00:00"))
    now = dt.datetime.now(dt.timezone.utc)

    repos, after = [], None
    while True:
        page = gql(tok, REPOS_Q, login=LOGIN, after=after)["user"]["repositories"]
        repos += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    owned = [r for r in repos if r["owner"]["login"].lower() == LOGIN.lower()]
    sources = [r for r in repos if not r["isFork"]]  # forks would double-count upstream history

    with ThreadPoolExecutor(8) as pool:
        histories = list(pool.map(lambda r: repo_history(tok, r["owner"]["login"], r["name"], user["id"]), sources))
        years = range(created.year, now.year + 1)
        contribs = sum(pool.map(
            lambda y: gql(tok, CONTRIB_Q, login=LOGIN,
                          **{"from": f"{y}-01-01T00:00:00Z", "to": f"{y}-12-31T23:59:59Z"})
            ["user"]["contributionsCollection"]["contributionCalendar"]["totalContributions"],
            years,
        ))
    adds = sum(h[1] for h in histories)
    dels = sum(h[2] for h in histories)
    n = "{:,}".format
    return {
        "uptime": uptime(created.date(), now.date()),
        "repos": n(len(owned)),
        "stars": n(sum(r["stargazerCount"] for r in owned)),
        "contributed": n(sum(1 for h in histories if h[0])),
        "followers": n(user["followers"]["totalCount"]),
        "commits": n(sum(h[0] for h in histories)),
        "contribs": n(contribs),
        "loc": n(adds - dels),
        "loc_add": n(adds),
        "loc_del": n(dels),
    }

# ---------------------------------------------------------------- info panel


def segments(value: str, stats: dict):
    """Split a value into (text, css class) runs after substituting $stats; longest names first."""
    for k in sorted(stats, key=len, reverse=True):
        value = value.replace(f"${k}", stats[k])
    out, pos = [], 0
    for m in re.finditer(r"\[(add|del)\](.*?)\[/\1\]", value):
        if m.start() > pos:
            out.append((value[pos:m.start()], "value"))
        out.append((m.group(2), "dele" if m.group(1) == "del" else "add"))
        pos = m.end()
    if pos < len(value):
        out.append((value[pos:], "value"))
    return out


def kv(key: str, value: str, width: int, stats: dict):
    val = segments(value, stats)
    dots = width - len(key) - 1 - 2 - sum(len(t) for t, _ in val)
    if dots < 1:
        raise ValueError(f"line too long by {1 - dots}: {key}: {value}")
    keys = []
    for i, part in enumerate(key.split(".")):
        keys += [(".", None)] * bool(i) + [(part, "key")]
    return keys + [(":", None), (" " + "." * dots + " ", "cc")] + val


def info_lines(stats: dict):
    rows = []
    for row in CONTENT:
        if row is None:
            rows.append([(".", "cc")])
        elif row == ("", ""):
            rows.append([])
        elif row[0] == "#":
            lead = "- " if rows else ""
            rows.append([(lead + row[1], None), (" " + "─" * (WIDTH - len(lead) - len(row[1]) - 1), "cc")])
        elif isinstance(row[0], tuple):
            body = WIDTH - 2
            left = (body - 3) // 2
            rows.append([(". ", "cc")] + kv(*row[0], left, stats) + [(" | ", None)] + kv(*row[1], body - 3 - left, stats))
        else:
            rows.append([(". ", "cc")] + kv(row[0], row[1], WIDTH - 2, stats))
    return rows

# ---------------------------------------------------------------- ascii art


def ascii_art(mode: str):
    """Rows of (glyph, '#rgb' | None), coloured from the avatar's own pixels."""
    im = Image.open(AVATAR).convert("RGBA")
    ys, xs = np.nonzero(np.asarray(im)[..., 3] > 128)
    crop = im.crop((xs.min(), ys.min(), xs.max() + 1, ys.max() + 1))
    target = ART_COLS * CHAR_W / (ART_ROWS * LINE_H)  # w/h of the art box
    w, h = crop.size
    cw, ch = (round(h * target), h) if w / h < target else (w, round(w / target))
    canvas = Image.new("RGBA", (cw, ch))
    bust = ys.max() + 1 >= im.height - 2  # cut off at the bottom edge: sit on the floor, else centre
    canvas.paste(crop, ((cw - w) // 2, ch - h if bust else (ch - h) // 2))
    rgb = canvas.convert("RGB").filter(ImageFilter.UnsharpMask(radius=3, percent=180, threshold=2))
    rgb.putalpha(canvas.getchannel("A"))
    small = np.asarray(rgb.convert("RGBa").resize((ART_COLS, ART_ROWS), Image.BOX).convert("RGBA")) / 255
    on = small[..., 3] > 0.45
    lum = small[..., :3] @ np.array([0.299, 0.587, 0.114])
    lo, hi = np.percentile(lum[on], [3, 97])
    norm = np.clip((lum - lo) / max(hi - lo, 1e-6), 0, 1)
    dens = norm if mode == "dark" else 1 - norm  # dense glyph = bright on dark bg, dark on light bg
    out = []
    for y in range(ART_ROWS):
        row = []
        for x in range(ART_COLS):
            if not on[y, x]:
                row.append((" ", None))
                continue
            hh, ll, ss = colorsys.rgb_to_hls(*small[y, x, :3])
            ll = max(ll, 0.5) if mode == "dark" else min(ll, 0.38)  # keep contrast against the background
            rgb_ = colorsys.hls_to_rgb(hh, ll, min(1, ss * 1.5))
            col = "#" + "".join("0123456789abcdef"[round(c * 15)] for c in rgb_)
            row.append((RAMP[1 + round(dens[y, x] * (len(RAMP) - 2))], col))
        out.append(row)
    return out

# ---------------------------------------------------------------- svg


def info_xml(segs):
    return "".join(escape(t) if c is None else f'<tspan class="{c}">{escape(t)}</tspan>' for t, c in segs)


def art_xml(row):
    """Merge same-colour glyphs into runs; spaces join whatever run is open."""
    out, run, run_col = [], "", None
    for ch, col in row + [("\0", "END")]:
        if ch != " " and col != run_col:
            if run:
                out.append(escape(run) if run_col is None else f'<tspan fill="{run_col}">{escape(run)}</tspan>')
            run, run_col = "", col
        run += ch
    return "".join(out).rstrip()


def svg(theme: dict, art, info) -> str:
    n = max(len(art), len(info))
    info_x = round(PAD + (ART_COLS + 2) * CHAR_W)
    width = round(info_x + WIDTH * CHAR_W + PAD)
    height = 30 + n * LINE_H
    y = lambda i: 30 + i * LINE_H
    art_body = "\n".join(f'<tspan x="{PAD}" y="{y(i)}">{art_xml(r)}</tspan>' for i, r in enumerate(art))
    info_body = "\n".join(f'<tspan x="{info_x}" y="{y(i)}">{info_xml(s)}</tspan>' for i, s in enumerate(info))
    return f"""<?xml version='1.0' encoding='UTF-8'?>
<svg xmlns="http://www.w3.org/2000/svg" xml:space="preserve" font-family="ConsolasFallback,Consolas,Menlo,monospace" width="{width}px" height="{height}px" font-size="16px">
<style>
@font-face {{ src: local('Consolas'), local('Consolas Bold'); font-family: 'ConsolasFallback'; font-display: swap; -webkit-size-adjust: 109%; size-adjust: 109%; }}
.key {{fill: {theme['key']};}} .value {{fill: {theme['value']};}} .cc {{fill: {theme['cc']};}} .add {{fill: {theme['add']};}} .dele {{fill: {theme['dele']};}}
text, tspan {{white-space: pre;}}
</style>
<rect width="{width}px" height="{height}px" fill="{theme['bg']}" rx="15"/>
<text>
{art_body}
</text>
<text fill="{theme['fg']}">
{info_body}
</text>
</svg>
"""


if __name__ == "__main__":
    stats = fetch_stats()
    print(json.dumps(stats, indent=1))
    info = info_lines(stats)
    for mode in ("dark", "light"):
        (ROOT / f"{mode}_mode.svg").write_text(svg(THEMES[mode], ascii_art(mode), info))
