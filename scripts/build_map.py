#!/usr/bin/env python3
"""Generate a London-Underground-style metro map from a projects-by-category YAML.

    python3 scripts/build_map.py \
        --projects projects-by-category.yaml --config lines.yaml \
        --out maps/asf_metro.svg [--html maps/asf_metro.html] [--png maps/asf_metro.png] [--layout maps/layout.json]

Options for comparing looks:   --list-variants | --variant NAME | --all-variants DIR
(variants live in the config's `variants:` section and override spacing, style, shape;
--all-variants writes svg+png only unless --with-html is given).

Requires: numpy, PyYAML.  (PNG output additionally needs cairosvg.)

Pipeline
  1. Read categories -> merge them into the configured lines (lines.yaml).
     A project on two or more lines is an interchange ("hub").
  2. Lay the hubs out with stress majorization, order each line's hubs along a
     path (open TSP), and repeat so path lengths reflect how many private
     stations sit between two hubs.  Many random restarts; fewest crossings wins.
  3. Snap hubs to a grid, route every hub-to-hub segment with 0/45/90 degree
     angles, spread each line's private stations along its segments and give
     the rest to two "tails" that run off in the clearest direction.
  4. Draw lines (parallel tracks where lines share a segment), ticks for
     ordinary stations, rings for interchanges, then place labels greedily
     with collision avoidance, plus a legend.

Everything is deterministic for a given input + seed.
"""
import argparse
import collections
import itertools
import json
import math
import os
import sys
from xml.sax.saxutils import escape

import numpy as np
import yaml

DIRS = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]
DIRS = [np.array(d, float) / np.hypot(*d) for d in DIRS]

LINE_W = 7.0       # stroke width of a line
GAP = 6.5          # spacing between parallel tracks in a shared segment
TICK_LEN = 14.0
HUB_GAP = 1.15     # minimum distance between interchanges, in station spacings
SNAP_DIV = 4       # hubs snap to a grid of spacing/SNAP_DIV
ROUTE_STYLE = "mixed"   # mixed | diag | ortho (45-degree routes) | manhattan (90-degree only)
MAX_RUN = 4        # stations in one straight piece before it reads as a straightaway
TERM_BOXES = False # Montreal-style coloured boxes at line ends
LBL, LBL_HUB = 10.5, 12.0
STYLE_DEFAULTS = {"line_width": 7.0, "bundle_gap": 6.5, "tick_len": 14.0, "hub_gap": 1.15,
                  "snap_div": 4, "route_style": "mixed", "max_run": 4, "terminus_boxes": False,
                  "label_size": 10.5, "hub_label_size": 12.0}


def apply_style(cfg):
    """Set the module-level drawing/routing knobs from cfg['style']."""
    global LINE_W, GAP, TICK_LEN, HUB_GAP, SNAP_DIV, ROUTE_STYLE, MAX_RUN, TERM_BOXES, LBL, LBL_HUB
    st = {**STYLE_DEFAULTS, **(cfg.get("style") or {})}
    LINE_W, GAP, TICK_LEN = float(st["line_width"]), float(st["bundle_gap"]), float(st["tick_len"])
    HUB_GAP, SNAP_DIV = float(st["hub_gap"]), int(st["snap_div"])
    ROUTE_STYLE, MAX_RUN = str(st["route_style"]), int(st["max_run"])
    TERM_BOXES = bool(st["terminus_boxes"])
    LBL, LBL_HUB = float(st["label_size"]), float(st["hub_label_size"])
FONT = "Johnston, 'Gill Sans', 'Helvetica Neue', Helvetica, Arial, sans-serif"


# ----------------------------------------------------------------------------
# 1. model
# ----------------------------------------------------------------------------
class Model:
    def __init__(self, cfg, projects):
        self.cfg = cfg
        self.lines = cfg["lines"]
        self.S = float(cfg.get("spacing", 46))
        self.tail_max = int(cfg.get("tail_max", 4))     # stations per line end
        self.seg_max = int(cfg.get("seg_max", 5))       # stations between two interchanges
        self.aspect = cfg.get("aspect")                 # target width/height of the hub layout
        self.finalists = int(cfg.get("finalists", 4))   # layouts routed fully and compared
        self.stations = {}                      # name -> dict(lines, rank, desc)
        listed = set()
        for li, line in enumerate(self.lines):
            for ci, cat in enumerate(line["categories"]):
                listed.add(cat)
                for entry in projects.get(cat) or []:
                    for pname, info in entry.items():
                        name = self.clean(pname)
                        s = self.stations.setdefault(
                            name, {"lines": {}, "desc": "", "raw": []})
                        s["lines"].setdefault(li, ci)
                        s["raw"].append(pname)
                        if not s["desc"] and isinstance(info, dict):
                            s["desc"] = info.get("description", "")
        self.ignored_categories = sorted(set(projects) - listed)
        self.hubs = sorted(n for n, s in self.stations.items() if len(s["lines"]) > 1)
        self.hub_id = {n: i for i, n in enumerate(self.hubs)}
        self.line_hubs = []                     # per line: hub names (unordered)
        self.line_uniques = []                  # per line: private stations, sorted
        for li in range(len(self.lines)):
            members = [n for n, s in self.stations.items() if li in s["lines"]]
            hubs = [n for n in members if n in self.hub_id]
            uniq = sorted((n for n in members if n not in self.hub_id),
                          key=lambda n: (self.stations[n]["lines"][li], n.lower()))
            self.line_hubs.append(hubs)
            self.line_uniques.append(uniq)

    def clean(self, name):
        c = self.cfg
        pre = c.get("strip_prefix", "")
        if pre and name.startswith(pre):
            name = name[len(pre):]
        for t in c.get("strip_text", []):
            name = name.replace(t, "")
        name = name.strip()
        for rule in c.get("collapse", []):
            if name.startswith(rule["prefix"]):
                name = rule["name"]
        return c.get("short_names", {}).get(name, name)


# ----------------------------------------------------------------------------
# 2. hub layout
# ----------------------------------------------------------------------------
def floyd(D):
    D = D.copy()
    for k in range(len(D)):
        D = np.minimum(D, D[:, k:k + 1] + D[k:k + 1, :])
    return D


def classical_mds(D, rng):
    n = len(D)
    J = np.eye(n) - 1.0 / n
    B = -0.5 * J @ (D ** 2) @ J
    w, v = np.linalg.eigh(B)
    idx = np.argsort(w)[::-1][:2]
    X = v[:, idx] * np.sqrt(np.maximum(w[idx], 1e-9))
    return X + rng.normal(0, 0.05 * (X.std() + 1), X.shape)


def stress(X, D, W, iters):
    X = X.copy()
    n = len(X)
    for _ in range(iters):
        for i in rng_order(n, _):
            diff = X[i] - X
            dist = np.linalg.norm(diff, axis=1)
            dist[i] = 1.0
            dist = np.maximum(dist, 1e-6)
            tgt = X + D[i][:, None] * diff / dist[:, None]
            X[i] = (W[i][:, None] * tgt).sum(0) / W[i].sum()
    return X


def rng_order(n, it):
    return range(n) if it % 2 == 0 else range(n - 1, -1, -1)


def tsp_path(idx, X):
    """Open shortest path through the points idx (greedy from each start + 2-opt)."""
    m = len(idx)
    if m <= 2:
        return list(idx)
    P = X[idx]
    dm = np.linalg.norm(P[:, None] - P[None, :], axis=2)
    best, best_len = None, 1e18
    for s in range(m):
        order, left = [s], set(range(m)) - {s}
        while left:
            nxt = min(left, key=lambda j: dm[order[-1], j])
            order.append(nxt)
            left.remove(nxt)
        improved = True
        while improved:
            improved = False
            for i in range(0, m - 1):
                for j in range(i + 1, m):
                    a = dm[order[i - 1], order[i]] if i > 0 else 0
                    b = dm[order[j], order[j + 1]] if j < m - 1 else 0
                    a2 = dm[order[i - 1], order[j]] if i > 0 else 0
                    b2 = dm[order[i], order[j + 1]] if j < m - 1 else 0
                    if a2 + b2 < a + b - 1e-9:
                        order[i:j + 1] = reversed(order[i:j + 1])
                        improved = True
        length = sum(dm[order[k], order[k + 1]] for k in range(m - 1))
        if length < best_len:
            best, best_len = order[:], length
    return [idx[k] for k in best]


def allocate(model, orders):
    """Decide how many private stations sit on each internal segment / tail.

    Tails (the two ends of a line) are capped at model.tail_max stations so lines
    do not shoot off in long spikes; the rest go between interchanges."""
    use = collections.Counter()
    for hubs in orders:
        for a, b in zip(hubs, hubs[1:]):
            use[frozenset((a, b))] += 1
    alloc = []
    for li, hubs in enumerate(orders):
        U = len(model.line_uniques[li])
        segs = [frozenset((a, b)) for a, b in zip(hubs, hubs[1:])]
        private = [i for i, k in enumerate(segs) if use[k] == 1]
        P = len(private)
        tail_total = min(U, 2 * model.tail_max)
        rest = U - tail_total
        seg_k = [0] * len(segs)
        if P and rest > 0:
            give = min(rest, model.seg_max * P)
            base, rem = divmod(give, P)
            for n, i in enumerate(private):
                seg_k[i] = base + (1 if n < rem else 0)
            rest -= give
        tail_total += rest                 # overflow (no room between hubs)
        before = tail_total // 2
        alloc.append({"before": before, "seg": seg_k, "after": tail_total - before})
    return alloc


def target_matrix(model, orders, alloc):
    n = len(model.hubs)
    S = model.S
    INF = 1e9
    E = np.full((n, n), INF)
    np.fill_diagonal(E, 0)
    path = np.zeros((n, n), bool)
    for li, hubs in enumerate(orders):
        for (a, b), k in zip(zip(hubs, hubs[1:]), alloc[li]["seg"]):
            length = max((k + 1) * S, 1.35 * S)
            if E[a, b] < INF:
                length = max(length, E[a, b])
            E[a, b] = E[b, a] = length
            path[a, b] = path[b, a] = True
    D = floyd(E)
    finite = D[D < INF / 2]
    far = (finite.max() if finite.size else S) * 1.2 + S
    D[D >= INF / 2] = far
    W = 1.0 / np.maximum(D, 1.0) ** 2
    W[path] *= 4.0
    np.fill_diagonal(W, 0)
    return D, W


def segs_cross(p, q, r, s):
    def o(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    return (o(p, q, r) * o(p, q, s) < 0) and (o(r, s, p) * o(r, s, q) < 0)


def count_crossings(X, orders):
    edges = set()
    for hubs in orders:
        for a, b in zip(hubs, hubs[1:]):
            edges.add((min(a, b), max(a, b)))
    edges = sorted(edges)
    c = 0
    for (a, b), (cc, d) in itertools.combinations(edges, 2):
        if len({a, b, cc, d}) < 4:
            continue
        if segs_cross(X[a], X[b], X[cc], X[d]):
            c += 1
    return c


def convex_hull(P):
    pts = sorted(map(tuple, P))
    def half(seq):
        h = []
        for p in seq:
            while len(h) >= 2 and (h[-1][0] - h[-2][0]) * (p[1] - h[-2][1]) - \
                    (h[-1][1] - h[-2][1]) * (p[0] - h[-2][0]) <= 0:
                h.pop()
            h.append(p)
        return h
    lo, up = half(pts), half(reversed(pts))
    return np.array(lo[:-1] + up[:-1])


def void_radius(X, orders, S):
    """Radius of the biggest empty circle inside the hub hull.  Big voids are what
    make a closed loop read as a 'ball'."""
    edges = set()
    for hubs in orders:
        for a, b in zip(hubs, hubs[1:]):
            edges.add((min(a, b), max(a, b)))
    pts = [x for x in X]
    for a, b in edges:
        pts += sample([X[a], X[b]], S / 4)
    P = np.array(pts)
    hull = convex_hull(X)
    if len(hull) < 3:
        return 0.0
    lo, hi = X.min(0), X.max(0)
    gx, gy = np.meshgrid(np.arange(lo[0], hi[0], S / 2), np.arange(lo[1], hi[1], S / 2))
    G = np.c_[gx.ravel(), gy.ravel()]
    inside = np.ones(len(G), bool)
    for k in range(len(hull)):
        a, b = hull[k], hull[(k + 1) % len(hull)]
        inside &= ((b[0] - a[0]) * (G[:, 1] - a[1]) - (b[1] - a[1]) * (G[:, 0] - a[0])) >= -1e-6
    G = G[inside]
    if not len(G):
        return 0.0
    best = 0.0
    for k in range(0, len(G), 400):
        d = np.linalg.norm(G[k:k + 400, None] - P[None], axis=2).min(axis=1)
        best = max(best, float(d.max()))
    return best


def layout_hubs(model, restarts, seed):
    n = len(model.hubs)
    if n < 2:
        sys.exit("need at least two interchange stations to build a map")
    S = model.S
    # initial: every pair of hubs that share a line is one hop apart
    hop = np.full((n, n), 1e9)
    np.fill_diagonal(hop, 0)
    for hubs in model.line_hubs:
        ids = [model.hub_id[h] for h in hubs]
        for a, b in itertools.permutations(ids, 2):
            hop[a, b] = 1.0
    D0 = floyd(hop)
    far = D0[D0 < 1e8].max() + 1
    D0[D0 >= 1e8] = far
    D0 = D0 * S * 1.4
    cands = []
    for r in range(restarts):
        rng = np.random.default_rng(seed + r)
        X = classical_mds(D0, rng) if r % 2 == 0 else rng.normal(0, D0.max() / 3, (n, 2))
        W0 = 1.0 / np.maximum(D0, 1.0) ** 2
        np.fill_diagonal(W0, 0)
        X = stress(X, D0, W0, 120)
        for _ in range(3):
            orders = [tsp_path([model.hub_id[h] for h in hubs], X)
                      for hubs in model.line_hubs]
            alloc = allocate(model, orders)
            D, W = target_matrix(model, orders, alloc)
            X = stress(X, D, W, 200)
        orders = [tsp_path([model.hub_id[h] for h in hubs], X) for hubs in model.line_hubs]
        alloc = allocate(model, orders)
        D, W = target_matrix(model, orders, alloc)
        X = stress(X, D, W, 120)
        Xc = X - X.mean(0)
        X = Xc @ np.linalg.svd(Xc, full_matrices=False)[2].T        # x = long axis
        tgt = float(model.aspect) if model.aspect else 1.0
        if tgt < 1.0:
            X = X[:, ::-1].copy()
        cross = count_crossings(X, orders)
        err = float((W * (np.linalg.norm(X[:, None] - X[None], axis=2) - D) ** 2).sum())
        void = void_radius(X, orders, S) / S
        ext = X.max(0) - X.min(0)
        aspect = float(max(ext[0], 1.0) / max(ext[1], 1.0))
        score = (cross * 1000 + err / max(1, n)
                 + 700 * max(0.0, void - 1.9) ** 2 + 600 * abs(math.log(aspect / tgt)))
        cands.append((score, X.copy(), orders, alloc, cross))
        if getattr(model, "verbose", True):
            print(f"  restart {r + 1:2d}/{restarts}: crossings={cross:3d} void={void:4.1f} aspect={aspect:3.1f}",
                  file=sys.stderr)
    cands.sort(key=lambda c: c[0])
    return cands[:max(1, model.finalists)]


# ----------------------------------------------------------------------------
# 3. snapping, routing, tails
# ----------------------------------------------------------------------------
def snap_hubs(X, S):
    g = S / SNAP_DIV
    X = X - X.mean(0)
    cells, placed = {}, []
    order = np.argsort(-np.linalg.norm(X - X.mean(0), axis=1))   # outliers first
    for i in order:
        base = np.round(X[i] / g)
        cand = [base]
        for rad in range(1, 8):
            for dx in range(-rad, rad + 1):
                for dy in range(-rad, rad + 1):
                    if max(abs(dx), abs(dy)) == rad:
                        cand.append(base + np.array([dx, dy]))
        for c in cand:
            p = c * g
            if all(np.linalg.norm(p - q) >= HUB_GAP * S for q in placed):
                cells[i] = p
                placed.append(p)
                break
        else:
            cells[i] = base * g
            placed.append(base * g)
    return np.array([cells[i] for i in range(len(X))])


def route_variant(a, b):
    return {"diag": 0, "ortho": 1}.get(ROUTE_STYLE, (a + b) % 2)


def octi_route(p, q, variant):
    if ROUTE_STYLE == "manhattan":
        if abs(p[0] - q[0]) < 1e-6 or abs(p[1] - q[1]) < 1e-6:
            return [p, q]
        return [p, np.array([q[0], p[1]]) if variant == 0 else np.array([p[0], q[1]]), q]
    dx, dy = q - p
    ax, ay = abs(dx), abs(dy)
    sx, sy = (1 if dx >= 0 else -1), (1 if dy >= 0 else -1)
    if ax < 1e-6 or ay < 1e-6 or abs(ax - ay) < 1e-6:
        return [p, q]
    d = min(ax, ay)
    diag = np.array([sx * d, sy * d])
    straight = np.array([sx * (ax - d), 0.0]) if ax > ay else np.array([0.0, sy * (ay - d)])
    mid = p + (diag if variant == 0 else straight)
    return [p, mid, q]


def poly_len(pts):
    return sum(np.linalg.norm(b - a) for a, b in zip(pts, pts[1:]))


def point_at(pts, dist):
    """Point and unit tangent at arclength dist along polyline."""
    for a, b in zip(pts, pts[1:]):
        L = np.linalg.norm(b - a)
        if dist <= L + 1e-9:
            t = (b - a) / L if L else np.array([1.0, 0.0])
            return a + t * min(dist, L), t
        dist -= L
    a, b = pts[-2], pts[-1]
    return b, (b - a) / max(np.linalg.norm(b - a), 1e-9)


def sample(pts, step):
    out = []
    for a, b in zip(pts, pts[1:]):
        L = np.linalg.norm(b - a)
        for t in np.arange(0, L, step):
            out.append(a + (b - a) * (t / L))
    out.append(pts[-1])
    return out


def offset_poly(pts, o):
    """Parallel polyline offset by o (mitred at bends)."""
    if abs(o) < 1e-9:
        return [p.copy() for p in pts]
    tangents = [(b - a) / np.linalg.norm(b - a) for a, b in zip(pts, pts[1:])]
    normals = [np.array([-t[1], t[0]]) for t in tangents]
    res = [pts[0] + normals[0] * o]
    for i in range(1, len(pts) - 1):
        n = normals[i - 1] + normals[i]
        n = n / np.linalg.norm(n)
        cosh = float(np.dot(n, normals[i]))
        res.append(pts[i] + n * o / max(cosh, 0.5))
    res.append(pts[-1] + normals[-1] * o)
    return res


def clean_pts(pts):
    out = [pts[0]]
    for p in pts[1:]:
        if np.linalg.norm(p - out[-1]) > 1e-6:
            out.append(p)
    return out


def is_octi(u, v):
    d = np.abs(v - u)
    if ROUTE_STYLE == "manhattan":
        return d[0] < 1e-6 or d[1] < 1e-6
    return d[0] < 1e-6 or d[1] < 1e-6 or abs(d[0] - d[1]) < 1e-6


def sharp_turn(pts):
    for a, b, c in zip(pts, pts[1:], pts[2:]):
        u, v = b - a, c - b
        nu, nv = np.linalg.norm(u), np.linalg.norm(v)
        if nu > 1e-6 and nv > 1e-6 and float(np.dot(u, v)) / (nu * nv) < -0.5:
            return True
    return False


def detour(p, q, need, avoid, S):
    """Octilinear p->q route of at least `need` length: p -> waypoint -> q with at
    most three pieces, away from everything else on the map."""
    g = S / 4.0
    lo, hi = np.minimum(p, q) - 2.4 * S, np.maximum(p, q) + 2.4 * S
    A = np.array(avoid) if len(avoid) else np.zeros((0, 2))
    best = None
    for x in np.arange(np.floor(lo[0] / g) * g, hi[0], g):
        for y in np.arange(np.floor(lo[1] / g) * g, hi[1], g):
            w = np.array([x, y])
            if np.linalg.norm(w - p) < 0.6 * S or np.linalg.norm(w - q) < 0.6 * S:
                continue
            if not (is_octi(p, w) or is_octi(w, q)):
                continue
            for var in (0, 1):
                pts = clean_pts(octi_route(p, w, var) + octi_route(w, q, var)[1:])
                L = poly_len(pts)
                if L < need * 0.97 or sharp_turn(pts):
                    continue
                smp = np.array(sample(pts, S / 3)[1:-1])
                dmin = S
                if len(A) and len(smp):
                    dmin = float(np.linalg.norm(smp[:, None] - A[None], axis=2).min())
                score = (L - need) * 0.6 + (len(pts) - 2) * S * 0.25 + max(0.0, 0.8 * S - dmin) * 6
                if best is None or score < best[0]:
                    best = (score, pts)
    return best[1] if best else None


def route_segments(model, H, orders, alloc):
    """Route every hub-to-hub segment; segments that must hold several stations but
    whose hubs are close together get a detour so the stations are not crushed."""
    S = model.S
    need = {}
    for li, hubs in enumerate(orders):
        for i, (a, b) in enumerate(zip(hubs, hubs[1:])):
            key = (min(a, b), max(a, b))
            k = alloc[li]["seg"][i]
            need[key] = max(need.get(key, 0.0), (k + 1) * S * 0.9 if k else 0.0)
    placed = {key: clean_pts(octi_route(H[key[0]], H[key[1]], route_variant(key[0], key[1])))
              for key in need}
    short = sorted((k for k in need if poly_len(placed[k]) < need[k] - 1),
                   key=lambda k: poly_len(placed[k]) - need[k])
    for key in short:
        a, b = key
        p, q = H[a], H[b]
        avoid = [h for i, h in enumerate(H) if i not in (a, b)]
        for k2, pts in placed.items():
            if k2 == key:
                continue
            for sp in sample(pts, S / 3):
                if np.linalg.norm(sp - p) > 0.8 * S and np.linalg.norm(sp - q) > 0.8 * S:
                    avoid.append(sp)
        found = detour(p, q, need[key], avoid, S)
        if found:
            placed[key] = found
    return placed


TAIL_TURNS = [(), (1,), (-1,), (2,), (-2,), (1, 1), (-1, -1), (1, -1), (-1, 1),
              (2, -1), (-2, 1), (1, -2), (-1, 2), (2, 2), (-2, -2)]
TAIL_TURNS += [t for t in itertools.product((1, -1), repeat=3)]
TAIL_TURNS += [t for t in itertools.product((2, -2), (1, -1), (2, -2))]


def tail_shapes(q):
    """(turns, stations-per-piece) options for a tail of q stations."""
    for turns in TAIL_TURNS:
        if ROUTE_STYLE == "manhattan" and any(t % 2 for t in turns):
            continue
        n = len(turns) + 1
        if n > 1 and q < 2 * n:
            continue
        base, rem = divmod(q, n)
        if n > 3 and q < 3 * n:
            continue
        for big_first in ((True, False) if (n > 1 and rem) else (True,)):
            lens = [base] * n
            if rem:
                lens[0 if big_first else -1] += rem
            yield turns, lens


def place_tails(model, H, orders, alloc, segpts):
    """Choose a clear, compact run-off path for the tail at each end of each line.
    Tails may curl (45/90 degree bends mid-gap between stations) so they stay short."""
    S = model.S
    avoid = [h.copy() for h in H]
    for pts in segpts.values():
        avoid += sample(pts, S / 3)
    centre = H.mean(0)
    radius = float(np.max(np.linalg.norm(H - centre, axis=1))) + S
    jobs = []
    for li, hubs in enumerate(orders):
        if not hubs:
            continue
        for side in ("before", "after"):
            q = alloc[li][side]
            if q:
                jobs.append((q, li, side, hubs[0] if side == "before" else hubs[-1]))
    jobs.sort(reverse=True)
    tails = {}
    for q, li, side, hub in jobs:
        origin = H[hub]
        best = None
        A = np.array(avoid)
        for d1 in (range(0, 8, 2) if ROUTE_STYLE == "manhattan" else range(8)):
            for turns, lens in tail_shapes(q):
                ends = [(sum(lens[:k + 1]) + 0.5) * S for k in range(len(lens) - 1)] + [q * S]
                pts, p, start, d = [origin], origin.copy(), 0.0, d1
                for k, e in enumerate(ends):
                    p = p + DIRS[d] * (e - start)
                    pts.append(p.copy())
                    start = e
                    if k < len(turns):
                        d = (d + turns[k]) % 8
                smp = np.array(sample(pts, S / 2)[1:])
                md = float(np.linalg.norm(smp[:, None] - A[None], axis=2).min())
                far = max(0.0, float(np.linalg.norm(pts[-1] - centre)) - radius)
                run = max(lens) - MAX_RUN
                score = (min(md, 1.6 * S) * 3 - 0.35 * far - 0.12 * S * len(turns)
                         - 1.4 * S * max(0, run))
                if best is None or score > best[0]:
                    best = (score, pts)
        tails[(li, side)] = best[1]
        avoid += sample(best[1], S / 3)
    return tails


# ----------------------------------------------------------------------------
# 4. assemble geometry
# ----------------------------------------------------------------------------
def build_geometry(model, H, orders, alloc):
    S = model.S
    nlines = len(model.lines)
    segpts = route_segments(model, H, orders, alloc)
    tails = place_tails(model, H, orders, alloc, segpts)

    users = collections.defaultdict(list)
    for li, hubs in enumerate(orders):
        for a, b in zip(hubs, hubs[1:]):
            users[(min(a, b), max(a, b))].append(li)

    line_paths, stations, termini = [], {}, []   # stations: name -> dict(pos, tan, kind)

    def add_station(name, p, t, li, kind):
        s = stations.setdefault(name, {"pos": p, "tan": t, "kind": kind, "lines": set()})
        s["lines"].add(li)

    for li in range(nlines):
        hubs = orders[li]
        uniq = list(model.line_uniques[li])
        al = alloc[li]
        take = lambda k: [uniq.pop(0) for _ in range(k)]
        path = []
        # tail before
        names = take(al["before"])
        if names:
            tp = tails[(li, "before")]
            for i, nm in enumerate(names):          # nearest-to-end gets first name
                p, t = point_at(tp, S * (len(names) - i))
                add_station(nm, p, t, li, "tick")
            termini.append((names[0], li, point_at(tp, S * len(names))[1]))
            path += list(reversed(tp))
        for i, (a, b) in enumerate(zip(hubs, hubs[1:])):
            key = (min(a, b), max(a, b))
            pts = segpts[key]
            lst = sorted(users[key])
            n = len(lst)
            o = (lst.index(li) - (n - 1) / 2.0) * GAP
            Q = offset_poly(pts, o)
            nms = take(al["seg"][i])
            if nms:
                total = poly_len(pts)
                for j, nm in enumerate(nms):
                    d = total * (j + 1) / (len(nms) + 1)
                    p, t = point_at(pts, d)
                    add_station(nm, p, t, li, "tick")
            fwd = (a == key[0])
            seq = Q if fwd else list(reversed(Q))
            path += [H[a]] + seq + [H[b]]
        if not hubs:
            continue
        if len(hubs) == 1:
            path = [H[hubs[0]]]
        names = take(al["after"])
        if names:
            tp = tails[(li, "after")]
            for i, nm in enumerate(names):
                p, t = point_at(tp, S * (i + 1))
                add_station(nm, p, t, li, "tick")
            termini.append((names[-1], li, point_at(tp, S * len(names))[1]))
            path += tp
        # tail-before already prepended reversed(tp) ending at hub; ensure it joins hub
        line_paths.append((li, path))
    # a line with a single hub still needs its two arms joined through the hub
    for li, hubs in enumerate(orders):
        if len(hubs) == 1:
            arms = []
            if (li, "before") in tails:
                arms += list(reversed(tails[(li, "before")]))
            if (li, "after") in tails:
                arms += tails[(li, "after")]
            line_paths = [(l, p) for l, p in line_paths if l != li]
            line_paths.append((li, arms))
    # hubs
    for name in model.hubs:
        i = model.hub_id[name]
        ls = set(model.stations[name]["lines"])
        stations[name] = {"pos": H[i], "tan": np.array([1.0, 0.0]), "kind": "hub", "lines": ls}
    for nm, li, t in termini:
        stations[nm]["term"] = (li, t)
    return line_paths, stations


# ----------------------------------------------------------------------------
# 5. labels
# ----------------------------------------------------------------------------
def text_w(s, size, bold):
    return len(s) * size * (0.60 if bold else 0.55) + 2


def place_labels(model, stations, line_paths):
    """Greedy label placement; hubs first, then by number of lines."""
    samples = []
    for _, pts in line_paths:
        samples += sample([np.array(p) for p in pts], 5.0)
    samples = np.array(samples)
    marks = np.array([s["pos"] for s in stations.values()])
    placed = []          # (x0, y0, x1, y1)
    out = {}
    order = sorted(stations, key=lambda n: (stations[n]["kind"] != "hub", -len(stations[n]["lines"]), n))
    for name in order:
        st = stations[name]
        bold = st["kind"] == "hub"
        term = st.get("term") if TERM_BOXES else None
        text = name.upper() if term else name
        size = LBL_HUB if (bold or term) else LBL
        w, h = text_w(text, size, bold or bool(term)), size * 1.15
        if term:
            w, h = w + 12, h + 7
        p, t = st["pos"], st["tan"]
        r = (9.5 + 0.9 * max(0, len(st["lines"]) - 2)) if bold else (9.0 if term else 8.0)
        cands = [
            ("E", p[0] + r, p[1] - h / 2, "start"),
            ("W", p[0] - r - w, p[1] - h / 2, "end"),
            ("N", p[0] - w / 2, p[1] - r - h, "middle"),
            ("S", p[0] - w / 2, p[1] + r, "middle"),
            ("NE", p[0] + r * .6, p[1] - r * .9 - h, "start"),
            ("SE", p[0] + r * .6, p[1] + r * .9, "start"),
            ("NW", p[0] - r * .6 - w, p[1] - r * .9 - h, "end"),
            ("SW", p[0] - r * .6 - w, p[1] + r * .9, "end"),
        ]
        # prefer the side perpendicular to the line
        perp = np.array([-t[1], t[0]])
        def pref(c):
            cx = c[1] + w / 2 - p[0]
            cy = c[2] + h / 2 - p[1]
            v = np.array([cx, cy])
            v = v / (np.linalg.norm(v) + 1e-9)
            if term:                         # boxes sit beyond the end of the line
                return -float(np.dot(v, term[1]))
            return -abs(float(np.dot(v, perp)))
        cands.sort(key=pref)
        best = None
        for rank, (tag, x0, y0, anchor) in enumerate(cands):
            x1, y1 = x0 + w, y0 + h
            cost = rank * 0.3
            for (a0, b0, a1, b1) in placed:
                if x0 < a1 + 1.5 and x1 > a0 - 1.5 and y0 < b1 + 1 and y1 > b0 - 1:
                    cost += 100
            m = (samples[:, 0] > x0 - 2.5) & (samples[:, 0] < x1 + 2.5) & \
                (samples[:, 1] > y0 - 2.5) & (samples[:, 1] < y1 + 2.5)
            cost += 3.0 * float(m.sum()) / 3.0
            mk = (marks[:, 0] > x0 - 6) & (marks[:, 0] < x1 + 6) & \
                 (marks[:, 1] > y0 - 6) & (marks[:, 1] < y1 + 6)
            cost += 40.0 * max(0, int(mk.sum()) - 1)
            if best is None or cost < best[0]:
                best = (cost, x0, y0, x1, y1, anchor, size, bold)
        _, x0, y0, x1, y1, anchor, size, bold = best
        placed.append((x0, y0, x1, y1))
        out[name] = {"x0": x0, "y0": y0, "w": x1 - x0, "h": y1 - y0, "anchor": anchor,
                     "size": size, "bold": bold or bool(term), "cost": best[0], "text": text}
        if term:
            out[name].update(box=True, color=model.lines[term[0]]["color"], anchor="middle")
    return out


# ----------------------------------------------------------------------------
# 6. svg
# ----------------------------------------------------------------------------
def fmt(p):
    return f"{p[0]:.1f},{p[1]:.1f}"


def dedupe(pts):
    out = [pts[0]]
    for p in pts[1:]:
        if np.linalg.norm(p - out[-1]) > 1e-6:
            out.append(p)
    return out


def station_ids(stations):
    return {n: i for i, n in enumerate(sorted(stations, key=str.lower))}


def station_records(model, stations, sid):
    """Per-station data for the interactive page (index == sid)."""
    recs = [None] * len(sid)
    for n, i in sid.items():
        s = stations[n]
        raw = []
        for r in model.stations[n]["raw"]:
            r = r[len(model.cfg.get("strip_prefix", "")):] if r.startswith(model.cfg.get("strip_prefix", "\0")) else r
            if r not in raw:
                raw.append(r)
        members = raw if len(raw) > 1 else []
        recs[i] = {"i": i, "name": n, "kind": s["kind"], "lines": sorted(s["lines"]),
                   "desc": "" if members else " ".join(model.stations[n].get("desc", "").split()),
                   "members": members,
                   "x": round(float(s["pos"][0]), 1), "y": round(float(s["pos"][1]), 1)}
    return recs


def render_svg(model, line_paths, stations, labels, cfg, stats, interactive=False):
    """SVG with inline presentation attributes (so it renders anywhere, incl. PNG)
    plus class/data-* hooks that the HTML page's CSS and JS use to restyle it."""
    sid = station_ids(stations)
    titles = not interactive
    allp = [p for _, pts in line_paths for p in pts]
    xs = [p[0] for p in allp] + [l["x0"] for l in labels.values()] + [l["x0"] + l["w"] for l in labels.values()]
    ys = [p[1] for p in allp] + [l["y0"] for l in labels.values()] + [l["y0"] + l["h"] for l in labels.values()]
    margin = 70
    x0, x1 = min(xs) - margin, max(xs) + margin
    y0, y1 = min(ys) - margin - (0 if interactive else 70), max(ys) + margin
    n = len(model.lines)
    cols = 3
    rows = math.ceil(n / cols)
    colw = max(240, (x1 - x0) / cols)
    leg_h = 70 + rows * 34
    y1 += leg_h
    W, Hh = x1 - x0, y1 - y0
    title = cfg.get("title", "Metro map")
    size = "" if interactive else f' width="{W:.0f}" height="{Hh:.0f}"'
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" class="map" role="img" aria-label="{escape(title)}" '
         f'viewBox="{x0:.1f} {y0:.1f} {W:.1f} {Hh:.1f}"{size} font-family="{escape(FONT)}">']
    o.append(f'<title>{escape(title)}</title>')
    o.append(f'<rect class="bg" x="{x0:.1f}" y="{y0:.1f}" width="{W:.1f}" height="{Hh:.1f}" fill="#ffffff"/>')
    if not interactive:   # the HTML page has its own header
        o.append(f'<text class="ttl" x="{x0 + 34:.1f}" y="{y0 + 56:.1f}" font-size="34" font-weight="bold" fill="#0019A8">'
                 f'{escape(cfg.get("title", ""))}</text>')
        o.append(f'<text class="sub" x="{x0 + 36:.1f}" y="{y0 + 80:.1f}" font-size="13" fill="#555">'
                 f'{escape(cfg.get("subtitle", ""))}</text>')
    # lines
    o.append(f'<g class="lines" fill="none" stroke-width="{LINE_W}" stroke-linecap="round" stroke-linejoin="round">')
    for li, pts in line_paths:
        pts = dedupe([np.array(p) for p in pts])
        d = "M" + " L".join(fmt(p) for p in pts)
        t = f'<title>{escape(model.lines[li]["name"])}</title>' if titles else ""
        o.append(f'<path class="line l{li}" data-l="{li}" d="{d}" stroke="{model.lines[li]["color"]}">{t}</path>')
    o.append('</g>')
    # ticks
    o.append('<g class="ticks" stroke="#1c1c1c" stroke-width="2.6" stroke-linecap="butt">')
    for name, s in stations.items():
        if s["kind"] != "tick":
            continue
        if TERM_BOXES and s.get("term"):
            col = model.lines[s["term"][0]]["color"]
            o.append(f'<circle class="st term" data-s="{sid[name]}" cx="{s["pos"][0]:.1f}" cy="{s["pos"][1]:.1f}" '
                     f'r="{LINE_W * 0.62 + 2:.1f}" fill="#ffffff" stroke="{col}" stroke-width="{LINE_W * 0.45 + 1:.1f}"/>')
            continue
        t = s["tan"]
        nrm = np.array([-t[1], t[0]]) * TICK_LEN / 2
        a, b = s["pos"] - nrm, s["pos"] + nrm
        li = next(iter(s["lines"]))
        tt = f'<title>{escape(name)} — {escape(model.lines[li]["name"])}</title>' if titles else ""
        o.append(f'<line class="st tick" data-s="{sid[name]}" x1="{a[0]:.1f}" y1="{a[1]:.1f}" '
                 f'x2="{b[0]:.1f}" y2="{b[1]:.1f}">{tt}</line>')
    o.append('</g>')
    # hubs
    o.append('<g class="hubs" fill="#ffffff" stroke="#1c1c1c" stroke-width="2.6">')
    for name in model.hubs:
        s = stations[name]
        r = 7.0 + 0.9 * max(0, len(s["lines"]) - 2)
        tt = ""
        if titles:
            ln = ", ".join(model.lines[i]["name"] for i in sorted(s["lines"]))
            desc = " ".join(model.stations[name].get("desc", "").split())[:240]
            tt = f'<title>{escape(name)} — {escape(ln)}\n{escape(desc)}</title>'
        o.append(f'<circle class="st hub" data-s="{sid[name]}" cx="{s["pos"][0]:.1f}" '
                 f'cy="{s["pos"][1]:.1f}" r="{r:.1f}">{tt}</circle>')
    o.append('</g>')
    # labels (white halo underneath)
    for halo in (True, False):
        o.append('<g class="labels halo-g" stroke="#ffffff" stroke-width="3.4" stroke-linejoin="round" fill="#ffffff">' if halo
                 else '<g class="labels" fill="#1c1c1c">')
        for name, l in labels.items():
            anchor = l["anchor"]
            tx = l["x0"] + (0 if anchor == "start" else l["w"] if anchor == "end" else l["w"] / 2)
            ty = l["y0"] + l["h"] * 0.78
            wt = ' font-weight="bold"' if l["bold"] else ""
            txt = escape(l.get("text", name))
            if l.get("box"):
                if halo:
                    continue
                o.append(f'<rect class="lblbox" data-s="{sid[name]}" x="{l["x0"]:.1f}" y="{l["y0"]:.1f}" '
                         f'width="{l["w"]:.1f}" height="{l["h"]:.1f}" rx="3" fill="{l["color"]}" stroke="none"/>')
                o.append(f'<text class="lbl box" data-s="{sid[name]}" x="{tx:.1f}" y="{l["y0"] + l["h"] * 0.5 + l["size"] * 0.36:.1f}" '
                         f'font-size="{l["size"]}" text-anchor="middle" font-weight="bold" fill="#ffffff" stroke="none">{txt}</text>')
                continue
            cls = "lbl halo" if halo else "lbl fg"
            o.append(f'<text class="{cls}" data-s="{sid[name]}" x="{tx:.1f}" y="{ty:.1f}" '
                     f'font-size="{l["size"]}" text-anchor="{anchor}"{wt}>{txt}</text>')
        o.append('</g>')
    # legend
    ly = y1 - leg_h + 20
    o.append(f'<line class="rule" x1="{x0 + 34:.1f}" y1="{ly - 8:.1f}" x2="{x1 - 34:.1f}" y2="{ly - 8:.1f}" stroke="#ccc"/>')
    for i, line in enumerate(model.lines):
        cx = x0 + 34 + (i % cols) * colw
        cy = ly + 22 + (i // cols) * 34
        cnt = sum(1 for s in model.stations.values() if i in s["lines"])
        o.append(f'<g class="leg" data-l="{i}">'
                 f'<rect class="hit" x="{cx - 6:.1f}" y="{cy - 20:.1f}" width="{colw - 20:.1f}" height="30" fill="transparent"/>'
                 f'<rect x="{cx:.1f}" y="{cy - 12:.1f}" width="40" height="{LINE_W + 2}" rx="4" fill="{line["color"]}"/>'
                 f'<text class="legtxt" x="{cx + 52:.1f}" y="{cy:.1f}" font-size="15" font-weight="bold" fill="#1c1c1c">'
                 f'{escape(line["name"])}<tspan class="n" font-weight="normal" fill="#666" font-size="12"> · {cnt} stations</tspan></text></g>')
    sy = y1 - 16
    o.append(f'<text class="foot" x="{x0 + 34:.1f}" y="{sy:.1f}" font-size="12" fill="#444">'
             f'<tspan font-weight="bold">○</tspan> interchange (project on several lines)    '
             f'<tspan font-weight="bold">|</tspan> station    ·   {stats["stations"]} stations, '
             f'{stats["hubs"]} interchanges, {len(model.lines)} lines</text>')
    o.append(f'<text class="foot credit" x="{x1 - 34:.1f}" y="{sy:.1f}" font-size="11" fill="#777" text-anchor="end">'
             f'{escape(cfg.get("credit", ""))}</text>')
    o.append('</svg>')
    return "\n".join(o)


def render_html(template, svg, model, stations, cfg, stats):
    sid = station_ids(stations)
    data = {"title": cfg.get("title", ""), "subtitle": cfg.get("subtitle", ""),
            "lines": [{"name": l["name"], "color": l["color"],
                       "count": sum(1 for s in model.stations.values() if i in s["lines"])}
                      for i, l in enumerate(model.lines)],
            "stations": station_records(model, stations, sid), "stats": stats}
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return (template.replace("{{TITLE}}", escape(cfg.get("title", "Metro map")))
            .replace("{{SUBTITLE}}", escape(cfg.get("subtitle", "")))
            .replace("{{CREDIT}}", escape(cfg.get("credit", "")))
            .replace("{{DATA}}", blob)
            .replace("{{SVG}}", svg))


# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
# 7. judging finished geometry, building, variants
# ----------------------------------------------------------------------------
def evaluate(model, line_paths, stations, labels):
    """Score a fully routed map (lower is better): real line crossings away from
    stations, empty voids, straight runs, overlapping labels, overall shape."""
    S = model.S
    segs = []
    for li, pts in line_paths:
        pts = clean_pts([np.array(q) for q in pts])
        segs += [(a, b, li) for a, b in zip(pts, pts[1:])]
    A = np.array([x[0] for x in segs])
    B = np.array([x[1] for x in segs])
    L = np.array([x[2] for x in segs])
    hubs = np.array([stations[h]["pos"] for h in model.hubs])

    def orient(P, Q, R):
        return (Q[:, 0] - P[:, 0]) * (R[:, 1] - P[:, 1]) - (Q[:, 1] - P[:, 1]) * (R[:, 0] - P[:, 0])

    ii, jj = np.triu_indices(len(segs), 1)
    keep = L[ii] != L[jj]
    ii, jj = ii[keep], jj[keep]
    o1, o2 = orient(A[ii], B[ii], A[jj]), orient(A[ii], B[ii], B[jj])
    o3, o4 = orient(A[jj], B[jj], A[ii]), orient(A[jj], B[jj], B[ii])
    hit = (o1 * o2 < -1e-6) & (o3 * o4 < -1e-6)
    crossings = 0
    for k in np.nonzero(hit)[0]:
        t = o3[k] / (o3[k] - o4[k])
        pt = A[ii[k]] + t * (B[ii[k]] - A[ii[k]])
        if len(hubs) == 0 or np.min(np.linalg.norm(hubs - pt, axis=1)) > 24:
            crossings += 1

    pts = []
    for _, path in line_paths:
        pts += sample(clean_pts([np.array(q) for q in path]), S / 4)
    P = np.array(pts)
    hull = convex_hull(P)
    void = 0.0
    if len(hull) >= 3:
        lo, hi = P.min(0), P.max(0)
        gx, gy = np.meshgrid(np.arange(lo[0], hi[0], S / 2), np.arange(lo[1], hi[1], S / 2))
        G = np.c_[gx.ravel(), gy.ravel()]
        inside = np.ones(len(G), bool)
        for k in range(len(hull)):
            a, b = hull[k], hull[(k + 1) % len(hull)]
            inside &= ((b[0] - a[0]) * (G[:, 1] - a[1]) - (b[1] - a[1]) * (G[:, 0] - a[0])) >= -1e-6
        G = G[inside]
        for k in range(0, len(G), 300):
            void = max(void, float(np.linalg.norm(G[k:k + 300, None] - P[None], axis=2).min(axis=1).max()))
    void /= S

    longest, run_pen = 0.0, 0.0
    for _, path in line_paths:
        path = clean_pts([np.array(q) for q in path])
        run, prev = 0.0, None
        for a, b in zip(path, path[1:]):
            d = b - a
            ln = float(np.linalg.norm(d))
            u = d / ln
            if prev is not None and float(np.dot(u, prev)) > 0.999:
                run += ln
            else:
                run = ln
            prev = u
            longest = max(longest, run)
            run_pen += max(0.0, run / S - (MAX_RUN + 1.5)) ** 2 * (1.0 if run == ln else 0.0)
    bad = sum(1 for l in labels.values() if l["cost"] >= 100)
    ext = P.max(0) - P.min(0)
    tgt = float(model.aspect) if model.aspect else 1.0
    shape_pen = 300 * abs(math.log((ext[0] / max(ext[1], 1.0)) / tgt))
    if tgt >= 1.0 and not model.aspect:                 # no preference: tolerate either orientation
        shape_pen = 300 * abs(math.log(max(ext) / max(min(ext), 1.0)))
    score = crossings * 35 + 600 * max(0.0, void - 1.9) ** 2 + 40 * run_pen + 12 * bad + shape_pen
    return {"crossings": crossings, "void": round(void, 2), "longest_run": round(longest / S, 1),
            "labels_overlapping": bad, "aspect": round(float(ext[0] / max(ext[1], 1.0)), 2),
            "score": round(float(score), 1)}


def build(cfg, projects, restarts, seed, verbose=True):
    """Whole pipeline for one config; returns everything the writers need."""
    apply_style(cfg)
    model = Model(cfg, projects)
    model.verbose = verbose
    if verbose:
        print(f"lines={len(model.lines)} stations={len(model.stations)} interchanges={len(model.hubs)}",
              file=sys.stderr)
        if model.ignored_categories:
            print("categories not on any line: " + ", ".join(model.ignored_categories), file=sys.stderr)
    best = None
    for ci, (_, X, orders, alloc, cross) in enumerate(layout_hubs(model, restarts, seed)):
        H = snap_hubs(X, model.S)
        line_paths, stations = build_geometry(model, H, orders, alloc)
        labels = place_labels(model, stations, line_paths)
        m = evaluate(model, line_paths, stations, labels)
        if verbose:
            print(f"  finalist {ci + 1}: {m}", file=sys.stderr)
        if best is None or m["score"] < best[0]["score"]:
            best = (m, line_paths, stations, labels)
    m, line_paths, stations, labels = best
    stats = {"stations": len(stations), "hubs": len(model.hubs), **m}
    return model, line_paths, stations, labels, stats


def write_outputs(res, cfg, out=None, html=None, png=None, png_width=4200, template=None, layout=None):
    model, line_paths, stations, labels, stats = res
    svg = render_svg(model, line_paths, stations, labels, cfg, stats)
    written = []
    if out:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        open(out, "w", encoding="utf8").write(svg)
        written.append(out)
    if html:
        tpl = open(template, encoding="utf8").read()
        isvg = render_svg(model, line_paths, stations, labels, cfg, stats, interactive=True)
        os.makedirs(os.path.dirname(os.path.abspath(html)), exist_ok=True)
        open(html, "w", encoding="utf8").write(render_html(tpl, isvg, model, stations, cfg, stats))
        written.append(html)
    if layout:
        lay = {"stations": {n: {"x": float(v["pos"][0]), "y": float(v["pos"][1]), "kind": v["kind"],
                                 "lines": [model.lines[i]["name"] for i in sorted(v["lines"])]}
                            for n, v in stations.items()}, "stats": stats}
        json.dump(lay, open(layout, "w", encoding="utf8"), indent=1)
        written.append(layout)
    if png:
        try:
            import cairosvg
            cairosvg.svg2png(bytestring=svg.encode("utf8"), write_to=png, output_width=png_width,
                             background_color="white")
            written.append(png)
        except ImportError:
            print("(skipping PNG: pip install cairosvg)", file=sys.stderr)
    return written


def merged_cfg(base, name):
    variants = base.get("variants") or {}
    if name not in variants:
        sys.exit(f"unknown variant {name!r}; have: {', '.join(variants) or '(none)'}")
    cfg = {k: v for k, v in base.items() if k != "variants"}
    for k, v in variants[name].items():
        if k == "description":
            continue
        cfg[k] = {**(cfg.get(k) or {}), **v} if k == "style" else v
    cfg["subtitle"] = f'{cfg.get("subtitle", "")}  ·  option: {name}'.strip(" ·")
    cfg["variant_name"] = name
    cfg["variant_desc"] = variants[name].get("description", "")
    return cfg


def run_variant(job):
    name, opts = job
    base = yaml.safe_load(open(opts["config"], encoding="utf8"))
    projects = yaml.safe_load(open(opts["projects"], encoding="utf8"))
    cfg = merged_cfg(base, name)
    res = build(cfg, projects, int(opts["restarts"] or cfg.get("restarts", 16)),
                int(cfg.get("seed", 7)), verbose=False)
    d = opts["dir"]
    write_outputs(res, cfg, out=f"{d}/{name}.svg", html=f"{d}/{name}.html" if opts.get("with_html") else None,
                  png=f"{d}/{name}.png",
                  png_width=opts["png_width"], template=opts["template"])
    return name, cfg["variant_desc"], res[4]


def write_gallery(d, items):
    cards = []
    for n, desc, st in items:
        page = f"{n}.html" if os.path.exists(os.path.join(d, f"{n}.html")) else f"{n}.png"
        extra = f' · <a href="{n}.html">interactive</a>' if page.endswith(".html") else ""
        cards.append(
            f'<figure><a href="{page}"><img src="{n}.png" alt="{escape(n)}" loading="lazy"></a>'
            f'<figcaption><b>{escape(n)}</b> — {escape(desc)}<br>'
            f'<small>{st["crossings"]} crossings · longest straight run {st["longest_run"]} stations · '
            f'empty-space radius {st["void"]} · {st["labels_overlapping"]} label clashes · '
            f'<a href="{n}.svg">svg</a>{extra}</small></figcaption></figure>')
    open(os.path.join(d, "index.html"), "w", encoding="utf8").write(
        '<!doctype html><meta charset="utf-8"><title>Map options</title>'
        '<style>body{font:15px system-ui;margin:20px;background:#fafafa;color:#1c1c1c}'
        'main{display:grid;grid-template-columns:repeat(auto-fill,minmax(520px,1fr));gap:20px}'
        'figure{margin:0;background:#fff;border:1px solid #ddd;border-radius:8px;padding:10px}'
        'img{width:100%;height:auto;display:block;border-bottom:1px solid #eee}'
        'figcaption{margin-top:8px}small{color:#666}</style>'
        f'<h1>Map options</h1><main>{"".join(cards)}</main>')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--projects", default=os.path.join(root, "projects-by-category.yaml"))
    ap.add_argument("--config", default=os.path.join(root, "lines.yaml"))
    ap.add_argument("--out", default=os.path.join(root, "maps", "asf_metro.svg"))
    ap.add_argument("--html", help="also write an interactive HTML page (inline SVG + CSS + JS)")
    ap.add_argument("--template", default=os.path.join(root, "scripts", "map_template.html"),
                    help="HTML page template used by --html")
    ap.add_argument("--png", help="also write a PNG (needs cairosvg)")
    ap.add_argument("--png-width", type=int, default=4200)
    ap.add_argument("--layout", help="also write the computed layout as JSON")
    ap.add_argument("--restarts", type=int)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--variant", help="apply one named variant from the config's `variants:` section")
    ap.add_argument("--all-variants", metavar="DIR",
                    help="build every variant into DIR (svg + png + html each, plus index.html)")
    ap.add_argument("--jobs", type=int, default=6, help="parallel variant builds")
    ap.add_argument("--with-html", action="store_true",
                    help="with --all-variants: also write the interactive HTML for each (off by default)")
    ap.add_argument("--list-variants", action="store_true")
    a = ap.parse_args()

    base = yaml.safe_load(open(a.config, encoding="utf8"))
    if a.list_variants:
        for n, v in (base.get("variants") or {}).items():
            print(f"{n}: {v.get('description', '')}")
        return
    if a.all_variants:
        import multiprocessing as mp
        os.makedirs(a.all_variants, exist_ok=True)
        opts = {"config": a.config, "projects": a.projects, "restarts": a.restarts, "dir": a.all_variants,
                "png_width": min(a.png_width, 2600), "template": a.template, "with_html": a.with_html}
        names = list(base.get("variants") or {})
        if not names:
            sys.exit("no `variants:` in the config")
        print(f"building {len(names)} variants with {min(a.jobs, len(names))} workers ...", file=sys.stderr)
        with mp.Pool(min(a.jobs, len(names))) as pool:
            items = []
            for n, desc, st in pool.imap_unordered(run_variant, [(n, opts) for n in names]):
                print(f"  done {n}: {st}", file=sys.stderr)
                items.append((n, desc, st))
        items.sort(key=lambda it: names.index(it[0]))
        write_gallery(a.all_variants, items)
        print(f"wrote {a.all_variants}/index.html", file=sys.stderr)
        return

    cfg = merged_cfg(base, a.variant) if a.variant else {k: v for k, v in base.items() if k != "variants"}
    projects = yaml.safe_load(open(a.projects, encoding="utf8"))
    res = build(cfg, projects, a.restarts or int(cfg.get("restarts", 16)),
                a.seed if a.seed is not None else int(cfg.get("seed", 7)))
    for w in write_outputs(res, cfg, out=a.out, html=a.html, png=a.png, png_width=a.png_width,
                           template=a.template, layout=a.layout):
        print(f"wrote {w}", file=sys.stderr)
    print(res[4], file=sys.stderr)


if __name__ == "__main__":
    main()
