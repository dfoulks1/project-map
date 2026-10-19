"""Build projects-by-category.yaml from data/projects.json + data/committees.json.

Every active project with a description is included; retired/dormant components are
dropped.  Projects without a usable category (none, or the placeholder `no-tlp-doap`)
go under `uncategorized`.  Language names that appear as categories are dropped, and
`programming-language` is stored as `languages:` when present.  Deterministic, so it
is safe to run from cron."""
import json, yaml, collections  # run from the repository root

FIX = {"node.js": "Node.js", "Typescript": "TypeScript"}
RETIRED = ("Retired:", "(Dormant)")       # names projects.apache.org gives retired / dormant components
NO_CATEGORY = {"no-tlp-doap", "retired"}  # placeholders, not real categories
EXCLUDE = {"Apache Community Development"}  # a PMC, not a software project

p = json.load(open("data/projects.json"))
ch = {c["id"]: c for c in json.load(open("data/committees.json"))}
norm = lambda s: " ".join(s.split())


def langs(v):
    pl = v.get("programming-language") or []
    out = []
    for x in [pl] if isinstance(pl, str) else pl:
        for y in x.split(","):
            y = FIX.get(y.strip(), y.strip())
            if y and y not in out:
                out.append(y)
    return out


LINE_CATS = {c for l in yaml.safe_load(open("lines.yaml"))["lines"] for c in l["categories"]}
# a category named like a language is noise, unless a line uses it (e.g. `xml`)
ALL_LANGS = {x.lower() for v in p.values() for x in langs(v)} - {c.lower() for c in LINE_CATS}
src = collections.Counter()
out = collections.defaultdict(list)
for pid, v in sorted(p.items(), key=lambda kv: kv[1]["name"].lower()):
    name = v["name"]
    if any(t in name for t in RETIRED) or "retired" in (v.get("category") or []) or name in EXCLUDE:
        src["skipped"] += 1
        continue
    if v.get("description"): d = v["description"]; src["description"] += 1
    elif v.get("shortdesc"): d = v["shortdesc"]; src["shortdesc"] += 1
    elif ch.get(v["pmc"], {}).get("charter"): d = ch[v["pmc"]]["charter"]; src["charter"] += 1
    else: src["no description, skipped"] += 1; continue
    l = langs(v)
    cats = [c for c in (v.get("category") or []) if c.lower() not in ALL_LANGS and c not in NO_CATEGORY]
    info = {"description": norm(d)}
    if l:
        info["languages"] = l
    for cat in cats or ["uncategorized"]:
        out[cat].append({name: dict(info)})
doc = {k: out[k] for k in sorted(out, key=str.lower)}
yaml.safe_dump(doc, open("projects-by-category.yaml", "w", encoding="utf8"), sort_keys=False,
               allow_unicode=True, width=100)
print(dict(src), len(doc), "categories,", len({k for es in doc.values() for e in es for k in e}), "projects")
