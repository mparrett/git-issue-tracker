#!/usr/bin/env python3
"""Render a repository's git-issue board as one self-contained HTML page.

git-issue keeps issues as git notes and has no web view. This reads them
through the tool's porcelain (`list`: twelve tab-separated columns per
docs/rfc/draft-ndn-porcelain-output-00.md) plus one `export` and one `show`
per issue for the description and comments, and writes a page that works
offline, on a phone, and in light or dark mode: issues grouped by status in
working order, a status/assignee/search filter kept in the URL hash so a view
is a link, a dependency graph as inline SVG for the issues that have edges,
and each card expanding to its body and comments.

    contrib/board/git-issue-board.py                # the repository you are in
    contrib/board/git-issue-board.py --repo PATH -o board.html

Python 3.8+, standard library only. The page is a snapshot: its footer says
when it was generated and from which commit; fetch the notes and re-run to
refresh. Serve the output directory with any static server for a phone.
"""

import argparse
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys

ANSI = re.compile(r"\x1b\[[0-9;]*m")
COLS = ["repo", "id", "status", "priority", "assignee", "role", "updated",
        "blocks", "depends_on", "parent_of", "relates_to", "title"]
# Display order: what is moving first, what is finished last.
STATUS_ORDER = ["in_progress", "review", "blocked", "open", "deferred", "closed"]
PRIO_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def topo_key(rec):
    return (rec.get("topo", float("inf")), PRIO_RANK.get(rec["priority"], 9), rec["title"].lower())


def gi(clone, *args):
    out = subprocess.run(["git", "-C", clone, "issue", *args], capture_output=True,
                         text=True, env={**os.environ, "GIT_ISSUE_NO_HEADER": "1"})
    return ANSI.sub("", out.stdout)


def read_issues(clone):
    issues = {}
    for line in gi(clone, "--porcelain", "list").splitlines():
        parts = line.split("\t")
        if len(parts) != len(COLS):
            continue
        rec = dict(zip(COLS, parts))
        for k in ("blocks", "depends_on", "parent_of", "relates_to"):
            rec[k] = [x for x in rec[k].split(",") if x]
        issues[rec["id"]] = rec
    # Dependency order comes from the tool's own `topo` rather than being
    # re-derived here. topo leaves closed issues out; they sort after it.
    for i, line in enumerate(gi(clone, "--porcelain", "topo").splitlines()):
        parts = line.split("\t")
        if len(parts) == len(COLS) and parts[1] in issues:
            issues[parts[1]]["topo"] = i
    # One export per id: in 1.0.0-rc6 `export` with no ids prints nothing (#3).
    # show carries the comments, export the clean fields.
    for iid, rec in issues.items():
        fields = {}
        for line in gi(clone, "export", iid).splitlines():
            if line.startswith("---"):
                break
            m = re.match(r"^([a-z_]+): ?(.*)$", line)
            if m:
                fields[m.group(1)] = m.group(2)
            elif line.startswith(" ") and fields:
                last = list(fields)[-1]
                fields[last] += "\n" + line.strip()
        rec["description"] = fields.get("description", "")
        rec["author"] = fields.get("author", "")
        rec["created"] = fields.get("created", "")
        show = gi(clone, "show", iid)
        rec["comments"] = show.split("Comments:\n---------\n", 1)[1].strip() if "Comments:\n" in show else ""
    return issues


def head_info(clone):
    sha = subprocess.run(["git", "-C", clone, "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    remote = subprocess.run(["git", "-C", clone, "remote", "get-url", "origin"],
                            capture_output=True, text=True).stdout.strip()
    remote = re.sub(r"^(https://github\.com/|git@github\.com:)|\.git$", "", remote) or os.path.basename(clone)
    return sha, remote


# --- dependency graph as inline SVG --------------------------------------------
# Only issues that take part in an edge are drawn; a 40-node graph with five
# edges is a list with a picture attached, not a picture. Layers come from the
# longest path over blocks/parent_of edges; relates_to is drawn dashed and does
# not affect layering.

def graph_data(issues):
    # The page lays the graph out itself (drawGraph in JS), so it can redo the
    # layout for whatever the filter leaves visible. A layout fixed at
    # generation time keeps empty columns where hidden issues sat.
    hard, soft = [], []
    for rec in issues.values():
        for t in rec["blocks"] + rec["parent_of"]:
            if t in issues:
                hard.append((rec["id"], t))
        for t in rec["relates_to"]:
            if t in issues and (t, rec["id"]) not in soft:
                soft.append((rec["id"], t))
    ids = sorted({n for e in hard + soft for n in e}, key=lambda n: topo_key(issues[n]))
    if not ids:
        return None
    nodes = [{"id": n, "title": issues[n]["title"], "status": issues[n]["status"]} for n in ids]
    # "</" inside a <script> element would end it early.
    return json.dumps({"nodes": nodes, "hard": hard, "soft": soft}).replace("</", "<\\/")


# --- page ----------------------------------------------------------------------

CSS = """
:root{color-scheme:light;
  --ink:#141a24;--muted:#5b6675;--faint:#8a94a3;--line:#d3dae3;
  --bg:#f5f7fa;--card:#ffffff;--card2:#fcfdff;
  --ok:#15803d;--ok-bg:#e9f6ec;--ok-line:#8fd0a3;
  --active:#1d4ed8;--active-bg:#eaf0fe;--active-line:#9db8f6;
  --defer:#475569;--defer-bg:#eef1f5;
  --block:#b91c1c;--block-bg:#fdecec;
  --warn:#b45309;--warn-bg:#fdf3e1;}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;
  --ink:#e6e9ee;--muted:#9aa4b2;--faint:#6b7686;--line:#2b333f;
  --bg:#0e1218;--card:#161b22;--card2:#1a2029;
  --ok:#56d98a;--ok-bg:#11291b;--ok-line:#255f3c;
  --active:#7ea2ff;--active-bg:#16233f;--active-line:#2f4b8f;
  --defer:#9aa4b2;--defer-bg:#1c222b;
  --block:#f87171;--block-bg:#3a1515;
  --warn:#fbbf24;--warn-bg:#3a2a0a;}}
:root[data-theme="dark"]{color-scheme:dark;
  --ink:#e6e9ee;--muted:#9aa4b2;--faint:#6b7686;--line:#2b333f;
  --bg:#0e1218;--card:#161b22;--card2:#1a2029;
  --ok:#56d98a;--ok-bg:#11291b;--ok-line:#255f3c;
  --active:#7ea2ff;--active-bg:#16233f;--active-line:#2f4b8f;
  --defer:#9aa4b2;--defer-bg:#1c222b;
  --block:#f87171;--block-bg:#3a1515;
  --warn:#fbbf24;--warn-bg:#3a2a0a;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.mono,code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums}
header{background:var(--card);border-bottom:1px solid var(--line);padding:10px 16px;
  display:flex;gap:14px;align-items:center;flex-wrap:wrap;position:sticky;top:0;z-index:2}
h1{font-size:15px;margin:0;font-weight:600;white-space:nowrap}
.scope{font-size:11px;color:var(--faint);border:1px solid var(--line);border-radius:99px;padding:2px 9px}
.counts{color:var(--muted);font-size:12px}
.controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-left:auto}
input[type=search]{padding:5px 9px;border:1px solid var(--line);border-radius:7px;background:var(--card2);
  color:var(--ink);min-width:170px;font-size:13px}
.seg{display:inline-flex;gap:1px;padding:2px;background:var(--card2);border:1px solid var(--line);border-radius:8px;flex-wrap:wrap}
.seg button{appearance:none;border:0;background:transparent;color:var(--muted);font:600 11px/1 inherit;
  font-family:inherit;padding:5px 8px;border-radius:6px;cursor:pointer}
.seg button[aria-pressed="true"]{background:var(--active-bg);color:var(--active)}
main{padding:16px;max-width:1100px;margin:0 auto}
body.wide main{max-width:none}
#wide{appearance:none;border:1px solid var(--line);background:var(--card2);color:var(--muted);
  font:600 11px/1 inherit;font-family:inherit;padding:6px 9px;border-radius:7px;cursor:pointer}
#wide[aria-pressed=true]{color:var(--ink);border-color:var(--active-line)}
section.group,section.graphbox{margin-bottom:22px}
section.group h2,section.graphbox h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--faint);margin:0 0 8px;
  display:flex;gap:8px;align-items:center}
section.group h2 .n{color:var(--muted);font-weight:500}
.issue{background:var(--card);border:1px solid var(--line);border-radius:9px;margin-bottom:8px;padding:0}
.issue summary{list-style:none;cursor:pointer;padding:10px 12px;display:grid;
  grid-template-columns:auto 1fr auto;gap:6px 10px;align-items:baseline}
.issue summary::-webkit-details-marker{display:none}
.issue .id{color:var(--faint);font-size:12px}
.issue .title{font-weight:500}
.issue .meta{grid-column:2/4;display:flex;gap:8px;flex-wrap:wrap;font-size:12px;color:var(--muted)}
.badge{font-size:11px;padding:1px 7px;border-radius:99px;border:1px solid var(--line);white-space:nowrap}
.p-high,.p-critical{color:var(--block);border-color:var(--block);background:var(--block-bg)}
.p-medium{color:var(--warn);border-color:var(--warn);background:var(--warn-bg)}
.p-low{color:var(--defer);background:var(--defer-bg)}
.st{font-size:11px;padding:1px 7px;border-radius:99px;white-space:nowrap}
.st-in_progress{background:var(--active-bg);color:var(--active)}
.st-review{background:var(--active-bg);color:var(--active)}
.st-blocked{background:var(--block-bg);color:var(--block)}
.st-open{background:var(--card2);color:var(--muted);border:1px solid var(--line)}
.st-deferred{background:var(--defer-bg);color:var(--defer)}
.st-closed{background:var(--ok-bg);color:var(--ok)}
.body{padding:0 12px 12px;border-top:1px solid var(--line);color:var(--ink)}
.body p{margin:10px 0;white-space:pre-wrap;overflow-wrap:anywhere}
.body .kv{font-size:12px;color:var(--muted);display:flex;gap:14px;flex-wrap:wrap;margin-top:8px}
.body a{color:var(--active);text-decoration:none}
.comments{margin-top:8px;padding:8px 10px;background:var(--card2);border-radius:7px;font-size:13px;white-space:pre-wrap}
.issue:target{outline:2px solid var(--active);outline-offset:1px}
.issue.hide{display:none}
#graph{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:12px;overflow:auto;margin-bottom:22px}
#graph svg{display:block;max-width:none}
#graph .edge{fill:none;stroke:var(--block);stroke-width:1.6;color:var(--block)}
#graph .edge.soft{stroke:var(--muted);stroke-dasharray:5 4;color:var(--muted)}
#graph .node rect{fill:var(--card2);stroke:var(--line);stroke-width:1.2}
#graph .node.st-in_progress rect{stroke:var(--active-line);fill:var(--active-bg)}
#graph .node.st-blocked rect{stroke:var(--block);fill:var(--block-bg)}
#graph .node.st-closed rect{stroke:var(--ok-line);fill:var(--ok-bg)}
#graph text{fill:var(--ink);font-size:12px}
#graph .nid{fill:var(--faint);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px}
footer{color:var(--faint);font-size:11px;padding:12px 16px 28px;text-align:center}
footer code{font-size:11px}
@media (max-width:600px){main{padding:10px}.controls{margin-left:0}}
"""

JS = """
const Q=document.getElementById('q'),SEG=document.getElementById('seg'),A=document.getElementById('asg');
let status='active',asg='';
const GD=document.getElementById('gdata'),G=GD&&JSON.parse(GD.textContent);
const esc=s=>s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'})[c]);
// Only issues that take part in a visible edge are drawn; a 40-node graph
// with five edges is a list with a picture attached, not a picture. Layers
// come from the longest path over blocks/parent_of edges; relates_to is
// drawn dashed and does not affect layering. Within a layer, topo order.
function drawGraph(vis){
  if(!G)return;
  const box=document.querySelector('.graphbox');
  const hard=G.hard.filter(([a,b])=>vis.has(a)&&vis.has(b)), soft=G.soft.filter(([a,b])=>vis.has(a)&&vis.has(b));
  const used=new Set([...hard,...soft].flat()), nodes=G.nodes.filter(n=>used.has(n.id));
  box.style.display=nodes.length?'':'none';
  if(!nodes.length)return;
  const layer={}; nodes.forEach(n=>layer[n.id]=0);
  for(let i=0;i<nodes.length;i++){
    let changed=false;
    for(const [a,b] of hard)if(layer[b]<layer[a]+1){layer[b]=layer[a]+1;changed=true;}
    if(!changed)break;
  }
  const cols=[]; nodes.forEach(n=>(cols[layer[n.id]]=cols[layer[n.id]]||[]).push(n));
  const W=230,H=44,GX=60,GY=16,PAD=12,pos={};
  cols.forEach((ns,l)=>ns.forEach((n,i)=>pos[n.id]=[PAD+l*(W+GX),PAD+i*(H+GY)]));
  const width=PAD*2+cols.length*W+(cols.length-1)*GX;
  const height=PAD*2+Math.max(...cols.map(c=>c?c.length:0))*(H+GY)-GY;
  const out=[`<svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" role="img" aria-label="dependency graph">`,
    '<defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
    +'<path d="M0 0L10 5L0 10z" fill="currentColor"/></marker></defs>'];
  const edge=(a,b,dashed)=>{
    const [ax,ay]=pos[a],[bx,by]=pos[b];
    let x1=ax+W,y1=ay+H/2,x2=bx,y2=by+H/2,d;
    if(bx===ax){  // same column: join the facing edges, straight down or up
      x1=x2=ax+W/2;
      if(by>ay){y1=ay+H;y2=by;}else{y1=ay;y2=by+H;}
      d=`M${x1},${y1} L${x2},${y2}`;
    }else if(bx<ax){  // backwards: loop under both boxes
      x1=ax+W/2;x2=bx+W/2;y1=y2=Math.max(ay,by)+H;
      d=`M${x1},${y1} C${x1},${y1+40} ${x2},${y2+40} ${x2},${y2}`;
    }else{
      const mx=(x1+x2)/2;
      d=`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
    }
    out.push(`<path class="${dashed?'edge soft':'edge'}" d="${d}" marker-end="url(#arr)"/>`);
  };
  hard.forEach(([a,b])=>edge(a,b,false)); soft.forEach(([a,b])=>edge(a,b,true));
  for(const n of nodes){
    const [x,y]=pos[n.id], t=esc(n.title), short=n.title.length<=34?t:esc(n.title.slice(0,33))+'…';
    out.push(`<a href="#i-${n.id}" class="node st-${n.status}"><rect x="${x}" y="${y}" width="${W}" height="${H}" rx="7"/>`
      +`<text x="${x+10}" y="${y+18}" class="nid">#${n.id}</text>`
      +`<text x="${x+10}" y="${y+34}" class="ntitle">${short}</text><title>${t}</title></a>`);
  }
  out.push('</svg>');
  document.getElementById('graph').innerHTML=out.join('\\n');
}
function apply(){
  const q=Q.value.trim().toLowerCase();
  let shown=0;
  document.querySelectorAll('.issue').forEach(el=>{
    const st=el.dataset.status;
    const okS=status==='all'||(status==='active'?st!=='closed':st===status);
    const okA=!asg||el.dataset.assignee===asg;
    const okQ=!q||el.dataset.text.includes(q);
    const ok=okS&&okA&&okQ; el.classList.toggle('hide',!ok); if(ok)shown++;
  });
  document.querySelectorAll('section.group').forEach(g=>{
    const n=g.querySelectorAll('.issue:not(.hide)').length;
    g.style.display=n?'':'none'; g.querySelector('.n').textContent=n;
  });
  // The graph shows exactly the issues whose cards are showing, so a box
  // never links to a hidden card.
  drawGraph(new Set([...document.querySelectorAll('.issue:not(.hide)')].map(e=>e.id.slice(2))));
  document.getElementById('shown').textContent=shown;
  SEG.querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed',b.dataset.v===status));
  const h=new URLSearchParams(); if(status!=='active')h.set('status',status); if(q)h.set('q',q); if(asg)h.set('asg',asg);
  history.replaceState(null,'',h.toString()?'#'+h:location.pathname);
}
SEG.addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;status=b.dataset.v;apply();});
Q.addEventListener('input',apply); A.addEventListener('change',()=>{asg=A.value;apply();});
(function(){const h=new URLSearchParams(location.hash.slice(1));
  if(h.get('status'))status=h.get('status'); if(h.get('q'))Q.value=h.get('q'); if(h.get('asg')){asg=h.get('asg');A.value=asg;}
  if(location.hash.startsWith('#i-')){status='all';}
  apply();})();
// Width is a per-viewer preference, not part of a shareable view, so it
// lives in localStorage rather than the hash. Storage can throw (private
// windows, file:// in some browsers); the toggle still works for the visit.
const WB=document.getElementById('wide');
function setWide(on){document.body.classList.toggle('wide',on);WB.setAttribute('aria-pressed',on);}
try{setWide(localStorage.getItem('gib-wide')==='1');}catch(e){}
WB.addEventListener('click',()=>{const on=!document.body.classList.contains('wide');setWide(on);
  try{localStorage.setItem('gib-wide',on?'1':'0');}catch(e){}});
// A "blocks #id" link can point at a card the current filter hides: widen
// the filter to All and open the card rather than going nowhere.
window.addEventListener('hashchange',()=>{
  if(!location.hash.startsWith('#i-'))return;
  const el=document.getElementById(location.hash.slice(1));
  if(!el)return;
  if(el.classList.contains('hide')){status='all';asg='';A.value='';Q.value='';apply();}
  el.open=true; el.scrollIntoView();
});
"""


def short_name(s):
    # "Norman Nunley, Jr" -> "Norman"; keeps the full name in the title attribute.
    return s.split(",")[0].split()[0] if s else "—"


def render(issues, graph, sha, remote, clone):
    now = dt.datetime.now(dt.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    # A status outside STATUS_ORDER still gets a group, last, rather than its
    # issues vanishing from the page.
    statuses = STATUS_ORDER + sorted({r["status"] for r in issues.values()} - set(STATUS_ORDER))
    counts = {s: sum(1 for r in issues.values() if r["status"] == s) for s in statuses}
    assignees = sorted({r["assignee"] for r in issues.values() if r["assignee"]})
    parts = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
             f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>{html.escape(remote.split('/')[-1])} issues</title><style>{CSS}</style></head><body>",
             f"<header><h1>{html.escape(remote.split('/')[-1])} issues</h1><span class='scope mono'>git-issue · {html.escape(remote)}</span>",
             f"<span class='counts'><span id='shown'>0</span> shown · {len(issues)} total · "
             + " · ".join(f"{counts[s]} {s.replace('_', ' ')}" for s in statuses if counts[s]) + "</span>",
             "<div class='controls'><div class='seg' id='seg'>"
             + "".join(f"<button data-v='{v}' aria-pressed='false'>{l}</button>" for v, l in
                       [("active", "Active"), ("in_progress", "In progress"), ("review", "Review"), ("blocked", "Blocked"),
                        ("open", "Open"), ("deferred", "Deferred"), ("closed", "Closed"), ("all", "All")])
             + "</div><select id='asg'><option value=''>everyone</option>"
             + "".join(f"<option value='{html.escape(a)}'>{html.escape(a)}</option>" for a in assignees)
             + "</select><input id='q' type='search' placeholder='search title, body, id'>"
             "<button id='wide' aria-pressed='false' title='toggle full width'>Full width</button></div></header><main>"]
    if graph:
        parts.append("<section class='graphbox'><h2>Dependencies <span class='n' style='font-weight:400'>solid = blocks · dashed = relates to · click a box</span></h2>"
                     f"<div id='graph'></div><script type='application/json' id='gdata'>{graph}</script></section>")
    def link_ids(ids):
        return ", ".join(f"<a href='#i-{i}'>#{i}</a>" for i in ids)
    for st in statuses:
        rows = [r for r in issues.values() if r["status"] == st]
        if not rows:
            continue
        rows.sort(key=topo_key)
        parts.append(f"<section class='group' data-status='{st}'><h2>{st.replace('_', ' ')} <span class='n'>{len(rows)}</span></h2>")
        for r in rows:
            text = html.escape(" ".join([r["id"], r["title"], r["description"], r["comments"], r["assignee"]]).lower(), quote=True)
            upd = r["updated"][:10]
            deps = []
            if r["blocks"]: deps.append("blocks " + link_ids(r["blocks"]))
            if r["depends_on"]: deps.append("depends on " + link_ids(r["depends_on"]))
            if r["parent_of"]: deps.append("parent of " + link_ids(r["parent_of"]))
            if r["relates_to"]: deps.append("relates to " + link_ids(r["relates_to"]))
            parts.append(
                f"<details class='issue' id='i-{r['id']}' data-status='{st}' data-assignee='{html.escape(r['assignee'], quote=True)}' data-text='{text}'>"
                f"<summary><span class='id mono'>#{r['id']}</span><span class='title'>{html.escape(r['title'])}</span>"
                f"<span class='badge p-{r['priority']}'>{r['priority']}</span>"
                f"<span class='meta'><span class='st st-{st}'>{st.replace('_', ' ')}</span>"
                f"<span title='{html.escape(r['assignee'])}'>{html.escape(short_name(r['assignee']))}</span>"
                f"<span>updated {upd}</span>" + (f"<span>{' · '.join(deps)}</span>" if deps else "") + "</span></summary>"
                f"<div class='body'><p>{html.escape(r['description']) or '<em>no description</em>'}</p>"
                + (f"<div class='comments'>{html.escape(r['comments'])}</div>" if r["comments"] else "")
                + f"<div class='kv'><span>author {html.escape(r['author'])}</span><span>created {r['created'][:10]}</span>"
                f"<span class='mono'>git issue show {r['id']}</span></div></div></details>")
        parts.append("</section>")
    parts.append(f"</main><footer>generated {now} from {len(issues)} issue notes in <code>{html.escape(clone)}</code> at <code>{sha}</code> · "
                 f"refresh: <code>git fetch origin 'refs/notes/issue-*:refs/notes/issue-*' &amp;&amp; git-issue-board.py</code></footer>"
                 f"<script>{JS}</script></body></html>")
    return "".join(parts)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=".", help="path inside the repository (default: cwd)")
    ap.add_argument("-o", "--out", default="git-issue-board.html")
    a = ap.parse_args()
    top = subprocess.run(["git", "-C", a.repo, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if top.returncode != 0:
        sys.exit(f"git-issue-board: {a.repo} is not inside a git repository")
    clone = top.stdout.strip()
    issues = read_issues(clone)
    if not issues:
        sys.exit(f"git-issue-board: no issues in {clone} (git fetch origin 'refs/notes/issue-*:refs/notes/issue-*' first?)")
    sha, remote = head_info(clone)
    page = render(issues, graph_data(issues), sha, remote, clone)
    if os.path.dirname(a.out):
        os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w") as f:
        f.write(page)
    print(f"git-issue-board: {len(issues)} issues -> {a.out} ({len(page) // 1024} KB)")


if __name__ == "__main__":
    main()
