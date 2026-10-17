import json, yaml, collections
p = json.load(open("data/projects.json"))
ch = {c["id"]: c for c in json.load(open("data/committees.json"))}
norm = lambda s: " ".join(s.split())
src = collections.Counter()
out = collections.defaultdict(list)
for pid, v in sorted(p.items(), key=lambda kv: kv[1]["name"].lower()):
    if v.get("description"): d = v["description"]; src["description"] += 1
    elif v.get("shortdesc"): d = v["shortdesc"]; src["shortdesc"] += 1
    elif ch[v["pmc"]].get("charter"): d = ch[v["pmc"]]["charter"]; src["charter"] += 1
    else: d = ""; src["none"] += 1
    for cat in (v.get("category") or ["uncategorized"]):
        out[cat].append({v["name"]: {"description": norm(d)}})
doc = {k: out[k] for k in sorted(out, key=str.lower)}
yaml.safe_dump(doc, open("projects-by-category.yaml", "w"), sort_keys=False, allow_unicode=True, width=100)
print(dict(src), len(doc), sum(map(len, doc.values())))
