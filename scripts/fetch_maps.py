import json, os, sys, urllib.parse, urllib.request
UA = {"User-Agent": "asf-project-map/0.1 (dfoulks@apache.org)"}
maps = {
 "london":   "London Underground Overground DLR Crossrail map alt.svg",
 "tokyo":    "Tokyo Subway Linemap en.svg",
 "paris":    "Paris Metro map complete.svg",
 "new-york": "New York Subway Map Alargule.svg",
 "moscow":   "Moscow metro map sb.svg",
 "shanghai": "Shanghai Metro map en.svg",
 "seoul":    "Seoul subway linemap ko.svg",
 "berlin":   "Berlin U-bahn und S-bahn.svg",
 "madrid":   "Madrid Metro Map.svg",
}
root = "reference-maps"
manifest = []
for city, title in maps.items():
    q = urllib.parse.urlencode({"action":"query","titles":"File:"+title,"prop":"imageinfo",
        "iiprop":"url|size|extmetadata","iiextmetadatafilter":"LicenseShortName|Artist|LicenseUrl","format":"json"})
    req = urllib.request.Request("https://commons.wikimedia.org/w/api.php?"+q, headers=UA)
    page = next(iter(json.load(urllib.request.urlopen(req, timeout=30))["query"]["pages"].values()))
    ii = page["imageinfo"][0]; md = ii["extmetadata"]
    d = os.path.join(root, city); os.makedirs(d, exist_ok=True)
    fn = title.replace(" ", "_")
    data = urllib.request.urlopen(urllib.request.Request(ii["url"], headers=UA), timeout=120).read()
    open(os.path.join(d, fn), "wb").write(data)
    manifest.append({"city":city,"file":f"{root}/{city}/{fn}","bytes":len(data),
        "size":f'{ii["width"]}x{ii["height"]}',"license":md.get("LicenseShortName",{}).get("value"),
        "license_url":md.get("LicenseUrl",{}).get("value"),
        "author_html":md.get("Artist",{}).get("value"),
        "source":"https://commons.wikimedia.org/wiki/File:"+urllib.parse.quote(fn)})
    print(city, fn, len(data), manifest[-1]["license"])
json.dump(manifest, open(os.path.join(root,"manifest.json"),"w"), indent=2)
