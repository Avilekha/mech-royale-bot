"""Static map: terrain height, building solids, line of sight and a coarse walking grid."""
import json, math, os
from array import array
from collections import deque
import heapq

G = 2.0  # solid grid resolution (m)
NAV = 4  # nav cell = NAV * G metres


class World:
    def __init__(self, mapdir):
        w = json.load(open(os.path.join(mapdir, "v2", "world.json")))
        b = w["bounds"]
        self.minx, self.miny = b["minX"], b["minY"]
        self.maxx, self.maxy = b["maxX"], b["maxY"]
        t = w["terrain"]
        self.tnx, self.tny, self.tmin_x, self.tmin_y, self.tstep = t["nx"], t["ny"], t["minX"], t["minY"], t["step"]
        raw = array("h")
        with open(os.path.join(mapdir, "v2", t["file"]), "rb") as f:
            raw.frombytes(f.read())
        self.th = raw
        self.nx = int((self.maxx - self.minx) / G) + 2
        self.ny = int((self.maxy - self.miny) / G) + 2
        n = self.nx * self.ny
        self.top = array("f", [-1e9]) * n
        self.base = array("f", [1e9]) * n
        for p in w["prisms"]:
            if p.get("kind") in ("building", "stadium", "bridge"):
                self._raster(p)
        self._build_nav()

    # ---- terrain
    def ground(self, x, y):
        fx = (x - self.tmin_x) / self.tstep
        fy = (y - self.tmin_y) / self.tstep
        i = int(fx)
        j = int(fy)
        if i < 0 or j < 0 or i >= self.tnx - 1 or j >= self.tny - 1:
            i = min(max(i, 0), self.tnx - 2)
            j = min(max(j, 0), self.tny - 2)
        ax, ay = min(max(fx - i, 0.0), 1.0), min(max(fy - j, 0.0), 1.0)
        th, nx = self.th, self.tnx
        k = j * nx + i
        h = (th[k] * (1 - ax) + th[k + 1] * ax) * (1 - ay) + (th[k + nx] * (1 - ax) + th[k + nx + 1] * ax) * ay
        return h * 0.01

    # ---- solids
    def _raster(self, p):
        top, base = p["top"], p["base"]
        edges = []
        for ring in [p["ring"]] + list(p.get("holes") or []):
            m = len(ring) // 2
            for a in range(m):
                x0, y0 = ring[2 * a], ring[2 * a + 1]
                x1, y1 = ring[2 * ((a + 1) % m)], ring[2 * ((a + 1) % m) + 1]
                if y0 != y1:
                    edges.append((x0, y0, x1, y1))
        if not edges:
            return
        ys = [e[1] for e in edges] + [e[3] for e in edges]
        j0 = max(0, int((min(ys) - self.miny) / G) - 1)
        j1 = min(self.ny - 1, int((max(ys) - self.miny) / G) + 1)
        for j in range(j0, j1 + 1):
            yc = self.miny + (j + 0.5) * G
            xs = []
            for x0, y0, x1, y1 in edges:
                if (y0 <= yc < y1) or (y1 <= yc < y0):
                    xs.append(x0 + (yc - y0) / (y1 - y0) * (x1 - x0))
            xs.sort()
            for a in range(0, len(xs) - 1, 2):
                i0 = max(0, int(math.ceil((xs[a] - self.minx) / G - 0.5)))
                i1 = min(self.nx - 1, int(math.floor((xs[a + 1] - self.minx) / G - 0.5)))
                row = j * self.nx
                for i in range(i0, i1 + 1):
                    k = row + i
                    if top > self.top[k]:
                        self.top[k] = top
                    if base < self.base[k]:
                        self.base[k] = base

    def cell(self, x, y):
        i = int((x - self.minx) / G)
        j = int((y - self.miny) / G)
        if 0 <= i < self.nx and 0 <= j < self.ny:
            return j * self.nx + i
        return -1

    def los(self, a, b):
        """True when the segment a->b (x,y,z points) is not blocked by buildings or terrain."""
        dx, dy, dz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        d = math.hypot(dx, dy)
        n = max(1, int(d / G))
        top, base = self.top, self.base
        for s in range(1, n):
            f = s / n
            x, y, z = a[0] + dx * f, a[1] + dy * f, a[2] + dz * f
            k = self.cell(x, y)
            if k >= 0 and top[k] > z and base[k] < z:
                return False
            if s % 2 == 0 and self.ground(x, y) > z:
                return False
        return True

    # ---- walking grid
    def _build_nav(self):
        c = G * NAV
        self.cs = c
        self.gx = int((self.maxx - self.minx) / c) + 1
        self.gy = int((self.maxy - self.miny) / c) + 1
        self.blocked = bytearray(self.gx * self.gy)
        for cj in range(self.gy):
            for ci in range(self.gx):
                x = self.minx + (ci + 0.5) * c
                y = self.miny + (cj + 0.5) * c
                g = self.ground(x, y)
                bad = False
                # slope
                if abs(self.ground(x + 3, y) - self.ground(x - 3, y)) > 6 or abs(self.ground(x, y + 3) - self.ground(x, y - 3)) > 6:
                    bad = True
                if not bad:
                    for sj in range(NAV):
                        row = (cj * NAV + sj) * self.nx
                        if row >= self.nx * self.ny:
                            break
                        for si in range(NAV):
                            ii = ci * NAV + si
                            if ii >= self.nx:
                                break
                            k = row + ii
                            if self.top[k] > g + 1.5 and self.base[k] < g + 9:
                                bad = True
                                break
                        if bad:
                            break
                self.blocked[cj * self.gx + ci] = 1 if bad else 0

    def ncell(self, x, y):
        i = int((x - self.minx) / self.cs)
        j = int((y - self.miny) / self.cs)
        return min(max(i, 0), self.gx - 1), min(max(j, 0), self.gy - 1)

    def ncenter(self, i, j):
        return self.minx + (i + 0.5) * self.cs, self.miny + (j + 0.5) * self.cs

    def free_cell_near(self, i, j, r=6):
        if not self.blocked[j * self.gx + i]:
            return i, j
        for rad in range(1, r + 1):
            for dj in range(-rad, rad + 1):
                for di in range(-rad, rad + 1):
                    if max(abs(di), abs(dj)) != rad:
                        continue
                    a, b = i + di, j + dj
                    if 0 <= a < self.gx and 0 <= b < self.gy and not self.blocked[b * self.gx + a]:
                        return a, b
        return i, j

    def field_gen(self, goal):
        """Generator building a BFS distance field to `goal`; yields every 300 nodes, returns the field."""
        gi, gj = self.free_cell_near(*self.ncell(*goal))
        gx, gy, blocked = self.gx, self.gy, self.blocked
        dist = array("d", [1e9]) * (gx * gy)
        dist[gj * gx + gi] = 0
        dq = [(0.0, gj * gx + gi)]
        cnt = 0
        nb = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0), (1, 1, 1.414), (-1, 1, 1.414), (1, -1, 1.414), (-1, -1, 1.414))
        while dq:
            d, k = heapq.heappop(dq)
            if d > dist[k]:
                continue
            cj, ci = divmod(k, gx)
            for di, dj, w in nb:
                a, b = ci + di, cj + dj
                if a < 0 or b < 0 or a >= gx or b >= gy:
                    continue
                kk = b * gx + a
                if blocked[kk]:
                    continue
                if di and dj and (blocked[cj * gx + a] or blocked[b * gx + ci]):
                    continue
                nd = d + w
                if nd < dist[kk]:
                    dist[kk] = nd
                    heapq.heappush(dq, (nd, kk))
            cnt += 1
            if cnt % 300 == 0:
                yield None
        return dist

    def field_dir(self, dist, x, y):
        """Unit vector toward lower distance, or None when the field has no answer here."""
        ci, cj = self.ncell(x, y)
        gx, gy = self.gx, self.gy
        best, bd = None, dist[cj * gx + ci]
        if bd >= 1e8:
            # standing in a blocked cell: head to any neighbour with a finite distance
            bd = 1e9
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                if not di and not dj:
                    continue
                a, b = ci + di, cj + dj
                if 0 <= a < gx and 0 <= b < gy:
                    v = dist[b * gx + a]
                    if v < bd:
                        bd, best = v, (di, dj)
        if best is None:
            return None
        # look further along the descent for a smoother path
        ax, ay = ci + best[0], cj + best[1]
        for _ in range(2):
            nb, nbd = None, dist[ay * gx + ax]
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    a, b = ax + di, ay + dj
                    if (di or dj) and 0 <= a < gx and 0 <= b < gy and dist[b * gx + a] < nbd:
                        nbd, nb = dist[b * gx + a], (a, b)
            if nb is None:
                break
            ax, ay = nb
        px, py = self.ncenter(ax, ay)
        dx, dy = px - x, py - y
        d = math.hypot(dx, dy) or 1.0
        return dx / d, dy / d

    def walkable(self, x, y):
        i, j = self.ncell(x, y)
        return not self.blocked[j * self.gx + i]
