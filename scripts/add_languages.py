"""Annotate projects-by-category.yaml with each project's programming languages
(from data/projects.json, field "programming-language").  Safe to re-run."""
import json, yaml

FIX = {"node.js": "Node.js", "Typescript": "TypeScript"}


def langs(v):
    pl = v.get("programming-language") or []
    out = []
    for x in [pl] if isinstance(pl, str) else pl:
        for y in x.split(","):
            y = FIX.get(y.strip(), y.strip())
            if y and y not in out:
                out.append(y)
    return out


by_name = {v["name"]: langs(v) for v in json.load(open("data/projects.json")).values()}
doc = yaml.safe_load(open("projects-by-category.yaml", encoding="utf8"))
RETIRED = ("Retired:", "(Dormant)")      # names projects.apache.org gives retired / dormant components
n = miss = dropped = 0
for cat, entries in doc.items():
    keep = [e for e in entries if not any(t in k for k in e for t in RETIRED)]
    dropped += len(entries) - len(keep)
    entries[:] = keep
for entries in doc.values():
    for entry in entries:
        for name, info in entry.items():
            l = by_name.get(name)
            if l:
                info["languages"] = l; n += 1
            else:
                info.pop("languages", None); miss += 1
yaml.safe_dump(doc, open("projects-by-category.yaml", "w", encoding="utf8"), sort_keys=False,
               allow_unicode=True, width=100)
print(f"{n} entries annotated, {miss} without a language, {dropped} retired/dormant removed")
