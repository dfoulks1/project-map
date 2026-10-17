# ASF project map

Metro-style ("underground") maps of Apache Software Foundation top-level projects.
Each project is a station; related categories are merged into coloured lines;
a project in categories of two or more lines is an interchange. Each station also
carries the project's programming language(s).

Published pages: `index.html` -> `maps/asf/index.html` (style gallery) and
`maps/asf_metro.html` (interactive map).

## How the maps are built

```
data/projects.json  --(one-off export, see below)-->  projects-by-category.yaml
                                                           |
              scripts/add_languages.py  (adds `languages:`, drops retired/dormant)
                                                           v
 lines.yaml  +  projects-by-category.yaml  --scripts/build_map.py-->  maps/*.svg|png|html
```

| File | Role |
|------|------|
| `data/projects.json`, `data/committees.json` | Snapshot of projects.apache.org data (source of descriptions and `programming-language`). |
| `projects-by-category.yaml` | Build input: `category -> [ "Apache X": {description, languages} ]`. Only active projects that have a known language are listed (no `attic-*`). |
| `lines.yaml` | Config: which categories form which line, line colours, name clean-up (`strip_prefix`, `collapse`, `short_names`), layout knobs and the named style `variants:`. |
| `scripts/add_languages.py` | Adds `languages:` to each entry of the YAML from `data/projects.json` and removes retired/dormant components (names containing `Retired:` or `(Dormant)`). Idempotent. |
| `scripts/build_map.py` | Layout, routing, labelling and rendering (SVG, PNG, interactive HTML, layout JSON). Deterministic for a given input + seed. |
| `scripts/map_template.html` | Page template for the interactive HTML (search, line filter, pan/zoom, detail panel). |
| `scripts/fetch_maps.py` | Downloads real-world metro maps into `reference-maps/` for style reference only; not part of the build. |
| `scripts/mk_yaml.py` | Original export of `projects.json` to the category YAML. It does **not** apply the filtering/category clean-up of the committed YAML, so don't re-run it over the checked-in file; use `add_languages.py` to refresh. |
| `scripts/build_map.py.v1`, `.v2` | Earlier versions of the builder, kept for reference. |

### Setup

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

### Rebuild everything

Run from the repository root:

```
.venv/bin/python scripts/add_languages.py          # refresh languages / filter retired
.venv/bin/python scripts/build_map.py --all-variants maps/asf --with-html     # london, montreal, compact, airy, landscape, grid + gallery
.venv/bin/python scripts/build_map.py --out maps/asf_metro.svg --html maps/asf_metro.html \
        --png maps/asf_metro.png --layout maps/layout.json                    # the main interactive map
```

Other useful flags: `--list-variants`, `--variant NAME`, `--seed N`, `--restarts N`,
`--png-width PX`. See the docstring at the top of `scripts/build_map.py` for the
layout pipeline (hub layout by stress majorization, 0/45/90-degree routing, greedy label placement).

### Programming languages on the maps

* `add_languages.py` reads `programming-language` (a string or list; comma-separated
  values are split, `node.js`/`Typescript` normalised) and stores it as `languages:`.
* **Static SVG/PNG:** a small grey tag after a station name shows the project's main
  (first) language. Java, by far the most common, is left untagged; the footer says so.
  Turn tags off with `style: {show_languages: false}` in `lines.yaml`; the default
  hidden language is `DEFAULT_LANG` in `scripts/build_map.py`.
* **Tooltips (SVG) and the interactive page:** the full language list. In the HTML page,
  the search box also matches a language name exactly (e.g. `Rust`) to highlight those stations.
* Projects without a `programming-language` in the source data are not on the map
  (this was already the case before languages were added).

### Updating the data

1. Replace `data/projects.json` / `data/committees.json` with a fresh projects.apache.org export.
2. Regenerate the category YAML (see `scripts/mk_yaml.py`; re-apply the filtering above if needed), then run `add_languages.py`.
3. Rebuild as above and commit the regenerated `maps/`.
