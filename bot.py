"""Mech Royale bot: survive first, take cheap kills, never miss a deadline.

Modes: roam (loot + stay inside the zone), fight (kite + shoot), hide (break line of sight, regenerate).
"""
import math
import random
import time

import mechfight as mf

try:
    from world import World
except Exception:  # pragma: no cover
    World = None

UPGRADE_RANK = [
    "nanites", "armor", "plating", "gun_damage", "gun_rate", "servos", "gun_mag", "gyros", "gun_velocity",
    "laser_damage", "laser_charge", "lightning_efficiency", "lightning_chain", "mortar_reload", "reactor",
    "capacitor", "lightning_range", "mortar_splash", "jets", "laser_pierce", "mortar_velocity", "sensors",
]
WEAPON_RANK = {"laser": 3, "lightning": 2, "mortar": 1, "gun": 0}


def wrap(a):
    while a > math.pi:
        a -= 2 * math.pi
    while a < -math.pi:
        a += 2 * math.pi
    return a


def hyp(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


class Slot:
    """A time-sliced BFS distance field toward one goal."""

    def __init__(self, world):
        self.w = world
        self.goal = None
        self.field = None
        self.gen = None
        self.want = None

    def request(self, goal, tol=14.0):
        if self.w is None:
            return
        if self.want is not None and math.hypot(goal[0] - self.want[0], goal[1] - self.want[1]) < tol:
            return
        self.want = (goal[0], goal[1])
        self.gen = self.w.field_gen(self.want)
        self.pending = None

    def step(self, deadline):
        if self.gen is None:
            return
        try:
            while time.perf_counter() < deadline:
                next(self.gen)
        except StopIteration as e:
            self.field = e.value
            self.goal = self.want
            self.gen = None
        except Exception:
            self.gen = None

    def dir(self, x, y):
        if self.field is None or self.w is None:
            return None
        return self.w.field_dir(self.field, x, y)


class Bot:
    name = "survivor-1"

    def __init__(self, hello):
        cfg = hello["config"]
        self.cfg = cfg
        self.rng = random.Random(hello.get("seed", 1))
        self.reach = cfg["loot"]["pickup_radius"]
        self.W = None
        if World is not None:
            try:
                t0 = time.time()
                self.W = World(hello["map"]["dir"])
                mf.log("map ready in %.2fs" % (time.time() - t0))
            except Exception as e:  # keep playing without a map
                mf.log("map failed: %r" % (e,))
        self.zone_slot = Slot(self.W)
        self.loot_slot = Slot(self.W)
        self.hide_slot = Slot(self.W)
        self.mode = "roam"
        self.strafe = 1
        self.strafe_until = 0.0
        self.last_dmg_t = -99.0
        self.last_dmg_bearing = None
        self.hide_spot = None
        self.hide_until = 0.0
        self.last_pos = None
        self.stuck_t = 0.0
        self.unstick_until = 0.0
        self.unstick_dir = (1.0, 0.0)
        self.sweep = 0.0
        self.last_goal_dir = (1.0, 0.0)
        self.take_cd = 0.0
        self.mem = {}
        self.pk = {}

    # ------------------------------------------------------------------ helpers
    def steer(self, me, vx, vy, look=7.0):
        """Rotate a wanted direction until the cell ahead is walkable."""
        n = math.hypot(vx, vy)
        if n < 1e-6:
            return 0.0, 0.0
        vx, vy = vx / n, vy / n
        if self.W is None:
            return vx, vy
        x, y = me["pos"][0], me["pos"][1]
        for ang in (0, 0.5, -0.5, 1.0, -1.0, 1.6, -1.6, 2.4, -2.4):
            c, s = math.cos(ang), math.sin(ang)
            dx, dy = vx * c - vy * s, vx * s + vy * c
            if self.W.walkable(x + dx * look, y + dy * look) and self.W.walkable(x + dx * look * 0.5, y + dy * look * 0.5):
                return dx, dy
        return vx, vy

    def nav(self, me, slot, goal):
        """Direction toward goal along the field when ready, else a steered straight line."""
        x, y = me["pos"][0], me["pos"][1]
        slot.request(goal)
        d = slot.dir(x, y) if slot.goal is not None and hyp(slot.goal, goal) < 20 else None
        if d is not None and hyp((x, y), goal) > 6:
            return d
        return self.steer(me, goal[0] - x, goal[1] - y)

    def eye(self, p, size=1.0):
        return [p[0], p[1], p[2] + 8 * size]

    def center(self, m):
        size = 1 + 0.05 * (m.get("cubes") or 0)
        return [m["pos"][0], m["pos"][1], m["pos"][2] + 4.5 * size]

    def lead(self, src, m, speed):
        c = self.center(m)
        v = m["vel"]
        p = c
        for _ in range(3):
            d = math.dist(src, p)
            t = d / max(speed, 1.0)
            p = [c[0] + v[0] * t, c[1] + v[1] * t, c[2]]
        return p

    def aim_at(self, src, p):
        dx, dy, dz = p[0] - src[0], p[1] - src[1], p[2] - src[2]
        d = math.hypot(dx, dy)
        return math.atan2(dy, dx), math.atan2(dz, max(d, 0.1))

    def mortar_pitch(self, d, h, v, g, lo, hi):
        disc = v ** 4 - g * (g * d * d + 2 * h * v * v)
        if disc < 0 or d < 1:
            return None
        th = math.atan((v * v - math.sqrt(disc)) / (g * d))
        if th < lo or th > hi:
            return None
        return th

    # ------------------------------------------------------------------ hide spot
    def pick_hide(self, me, threats, tcen, trad, t):
        if self.W is None or not threats:
            return None
        x, y, z = me["pos"]
        best, bs = None, -1e9
        near = sorted(threats, key=lambda p: hyp(p, (x, y)))[:3]
        for r in (22, 40, 65):
            for k in range(10):
                a = (k / 10) * 2 * math.pi + self.rng.random() * 0.4
                cx, cy = x + r * math.cos(a), y + r * math.sin(a)
                if not self.W.walkable(cx, cy):
                    continue
                if math.hypot(cx - tcen[0], cy - tcen[1]) > trad * 0.9:
                    continue
                cz = self.W.ground(cx, cy) + 4.5
                hidden = 0
                dmin = 1e9
                for p in near:
                    dmin = min(dmin, hyp(p, (cx, cy)))
                    if not self.W.los([p[0], p[1], p[2] + 8], [cx, cy, cz]):
                        hidden += 1
                score = hidden * 100 + min(dmin, 150) * 0.4 - r * 0.25
                # do not run toward the threats
                if dmin < hyp(near[0], (x, y)) - 5:
                    score -= 40
                if score > bs:
                    bs, best = score, (cx, cy)
        return best

    # ------------------------------------------------------------------ main decision
    def act(self, obs):
        t0 = time.perf_counter()
        me = obs["self"]
        t = obs["t"]
        x, y, z = me["pos"]
        hpf = me["hp"] / max(me["max_hp"], 1)
        act = {"tick": obs["tick"]}
        cfg = self.cfg

        # ---- events
        for e in obs["events"]:
            if e["type"] == "damaged" and e.get("weapon") != "zone":
                self.last_dmg_t = t
                if e.get("bearing") is not None:
                    self.last_dmg_bearing = e["bearing"]

        # ---- remembered weapon pickups
        seen = set()
        for p in obs["pickups"]:
            self.pk[p["id"]] = p
            seen.add(p["id"])
        for pid in list(self.pk):
            if pid not in seen and hyp(self.pk[pid]["pos"], (x, y)) < 40:
                del self.pk[pid]
        for e in obs["events"]:
            if e["type"] in ("took", "take_failed") and e.get("id") in self.pk:
                del self.pk[e["id"]]

        # ---- upgrade offer
        if obs["offer"]:
            ids = [c["id"] for c in obs["offer"]["choices"]]
            pick = 0
            bestr = 1e9
            for i, cid in enumerate(ids):
                r = UPGRADE_RANK.index(cid) if cid in UPGRADE_RANK else 50
                if r < bestr:
                    bestr, pick = r, i
            act["pick"] = pick

        # ---- zone target circle
        zn = obs["zone"]
        if t < zn["shrink_start"] - 12:
            tcen, trad = zn["center"], zn["radius"]
        else:
            tcen, trad = zn["next_center"], zn["next_radius"]
        dzone = math.hypot(x - tcen[0], y - tcen[1])
        in_safe = dzone < trad * 0.8 and me["in_zone"]

        # ---- enemies
        enemies = []
        npcs = []
        for m in obs["mechs"]:
            (enemies if m["kind"] == "mech" else npcs).append(m)
        for m in enemies:
            m["_d"] = hyp(m["pos"], (x, y))
        enemies.sort(key=lambda m: m["_d"])
        close = [m for m in enemies if m["_d"] < 120]
        threats = [m["pos"] for m in enemies if m["_d"] < 220]
        recent_hit = (t - self.last_dmg_t) < 3.0
        if not threats and recent_hit and self.last_dmg_bearing is not None:
            b = self.last_dmg_bearing
            threats = [[x + 110 * math.cos(b), y + 110 * math.sin(b), z]]
        if not threats:
            for s in obs["sounds"]:
                if s["kind"] in ("gun", "laser", "mortar", "lightning", "laser_charge") and s["dist"] != "far":
                    b = s["bearing"]
                    d = 40 if s["dist"] == "near" else 100
                    threats = [[x + d * math.cos(b), y + d * math.sin(b), z]]
                    break

        # ---- mode
        if self.mode == "hide":
            if (hpf > 0.8 and not recent_hit) or (not threats and t > self.hide_until and hpf > 0.55):
                self.mode = "roam"
        else:
            want_hide = (hpf < 0.4 and (enemies or recent_hit)) or (len(close) >= 3 and hpf < 0.75)
            if want_hide:
                self.mode = "hide"
                self.hide_spot = None
                self.hide_until = t + 6
        if self.mode != "hide":
            self.mode = "fight" if (close or (enemies and enemies[0]["_d"] < 160)) else "roam"

        # ---- weapons
        slots = {}
        for i, w in enumerate(me["weapons"]):
            if w:
                slots[w["kind"]] = (i, w)
        gun_i, gun = slots.get("gun", (None, None))
        mv = (0.0, 0.0)
        boost = False
        fire = [False, False]
        aim = None
        label = self.mode

        # ---- target choice
        target = None
        tkind = None
        if enemies:
            vis = [m for m in enemies if self.W is None or self.W.los(self.eye(me["pos"]), self.center(m))]
            pool = vis or []
            if pool:
                # prefer weak targets that are not much farther than the nearest
                pool.sort(key=lambda m: m["_d"] + (m.get("hp") or 1400) / 40)
                target = pool[0]
                tkind = "mech"
        if target is None and not enemies and npcs and hpf > 0.6:
            n0 = min(npcs, key=lambda m: hyp(m["pos"], (x, y)))
            if hyp(n0["pos"], (x, y)) < 70 and (self.W is None or self.W.los(self.eye(me["pos"]), self.center(n0))):
                target, tkind = n0, "npc"
        crate = None
        if target is None and not enemies and obs["crates"] and hpf > 0.6 and in_safe:
            c0 = min(obs["crates"], key=lambda c: hyp(c["pos"], (x, y)))
            if hyp(c0["pos"], (x, y)) < 55 and (self.W is None or self.W.los(self.eye(me["pos"]), [c0["pos"][0], c0["pos"][1], c0["pos"][2] + 1])):
                crate = c0

        muz = [x, y, z + 7 * (1 + 0.05 * me["cubes"])]
        # ---- shooting
        if target is not None:
            speed = (gun["stats"]["speed"] if gun else 252)
            p = self.lead(muz, target, speed)
            yaw, pitch = self.aim_at(muz, p)
            d = math.hypot(p[0] - x, p[1] - y)
            aim = [yaw, pitch]
            ey = abs(wrap(yaw - me["aim"][0]))
            ep = abs(pitch - me["aim"][1])
            tol = max(0.02, math.atan2(2.2, max(d, 1)))
            aligned = ey < tol and ep < tol * 1.5
            grange = gun["stats"]["range"] if gun else 230
            if gun and d < grange * 0.95 and aligned and gun.get("reload", 0) <= 0:
                fire[gun_i] = True
            if gun and gun.get("ammo", 1) <= 0:
                act["reload"] = True
            if "lightning" in slots and tkind in ("mech", "npc"):
                li, lw = slots["lightning"]
                if d < lw["stats"]["range"] * 0.85 and ey < 0.2 and me["energy"] > 20:
                    fire[li] = True
            if "laser" in slots and tkind == "mech":
                li, lw = slots["laser"]
                ch = lw.get("charge", 0) or 0
                cond = 25 < d < lw["stats"]["range"] * 0.9 and len(close) <= 2 and hpf > 0.45 and ey < tol * 3
                if cond or (ch > 0 and ey < tol * 6):
                    fire[li] = True
            if "mortar" in slots and tkind == "mech" and 50 < d < 200:
                mi, mw = slots["mortar"]
                ws = cfg["weapons"]["mortar"]
                # lob only when the gun cannot see them or they are far
                if d > grange * 0.9 or (self.W and not self.W.los(muz, self.center(target))):
                    mspeed = mw["stats"].get("speed", ws["speed"]) if mw.get("stats") else ws["speed"]
                    ph = self.mortar_pitch(d, self.center(target)[2] - (z + 9.5), mspeed, ws["gravity"], me["stats"]["min_pitch"], me["stats"]["max_pitch"])
                    if ph is not None:
                        aim = [yaw, ph]
                        if abs(wrap(yaw - me["aim"][0])) < 0.05 and abs(ph - me["aim"][1]) < 0.06:
                            fire[mi] = True
        elif crate is not None:
            yaw, pitch = self.aim_at(muz, [crate["pos"][0], crate["pos"][1], crate["pos"][2] + 1.0])
            aim = [yaw, pitch]
            if gun and abs(wrap(yaw - me["aim"][0])) < 0.08 and gun.get("reload", 0) <= 0:
                fire[gun_i] = True
        elif gun and gun.get("ammo", 12) < gun["stats"]["mag"] * 0.5 and not enemies:
            act["reload"] = True

        # ---- movement
        goal = None
        if not me["in_zone"] or dzone > trad * 0.95:
            goal = tcen
            boost = me["energy"] > 15
            label = "zone"
        elif self.mode == "hide":
            if self.hide_spot is None or t > self.hide_until or hyp(self.hide_spot, (x, y)) < 4:
                if threats and (self.hide_spot is None or t > self.hide_until):
                    self.hide_spot = self.pick_hide(me, threats, tcen, trad, t)
                    self.hide_until = t + 3
                elif hyp(self.hide_spot or (x, y), (x, y)) < 4:
                    pass
            if self.hide_spot is not None and hyp(self.hide_spot, (x, y)) > 4:
                goal = self.hide_spot
                boost = bool(enemies) and me["energy"] > 25 and close and close[0]["_d"] < 90
            else:
                mv = (0.0, 0.0)  # hold still, silent
            label = "hide"
        elif self.mode == "fight" and enemies:
            e0 = enemies[0]
            ex, ey_ = e0["pos"][0] - x, e0["pos"][1] - y
            ed = math.hypot(ex, ey_) or 1.0
            ux, uy = ex / ed, ey_ / ed
            if t > self.strafe_until:
                self.strafe = -self.strafe if self.rng.random() < 0.7 else self.strafe
                self.strafe_until = t + 0.35 + self.rng.random() * 0.7
            px, py = -uy * self.strafe, ux * self.strafe
            rad = 0.0
            if ed < 55:
                rad = -1.0
            elif ed < 80:
                rad = -0.3
            elif ed > 130 and len(close) <= 1 and hpf > 0.7:
                rad = 0.7
            vx, vy = px * 0.9 + ux * rad, py * 0.9 + uy * rad
            if len(close) >= 2:  # several of them: head for the zone center while strafing
                cx, cy = tcen[0] - x, tcen[1] - y
                cn = math.hypot(cx, cy) or 1.0
                vx += cx / cn * 0.4
                vy += cy / cn * 0.4
            mv = self.steer(me, vx, vy, 6.0)
            label = "fight %d" % len(close)
        else:
            # roam: loot, cubes, then drift to the zone center
            lootgoal = None
            if not enemies and hpf > 0.5:
                slot1 = me["weapons"][1] if len(me["weapons"]) > 1 else None
                cur_rank = WEAPON_RANK.get(slot1["kind"], 0) if slot1 else -1
                bestp, bs = None, 1e9
                for p in self.pk.values():
                    r = WEAPON_RANK.get(p["kind"], 0)
                    if p["kind"] == "gun" or p["kind"] in slots or r <= cur_rank:
                        continue
                    d = hyp(p["pos"], (x, y))
                    if d < 260 and math.hypot(p["pos"][0] - tcen[0], p["pos"][1] - tcen[1]) < trad * 0.85 and d - 15 * r < bs:
                        bs, bestp = d - 15 * r, p
                if bestp is not None:
                    lootgoal = bestp["pos"]
                    if hyp(bestp["pos"], (x, y)) < self.reach - 0.5 and abs(bestp["pos"][2] - z) < 3.5 and t > self.take_cd:
                        act["take"] = bestp["id"]
                        if slot1 is not None:
                            act["drop"] = 1
                        self.take_cd = t + 0.5
                    label = "loot " + bestp["kind"]
                elif obs["cubes"] and me["cubes"] < 9:
                    c0 = min(obs["cubes"], key=lambda c: hyp(c["pos"], (x, y)))
                    if hyp(c0["pos"], (x, y)) < 50:
                        lootgoal = c0["pos"]
                        label = "cube"
            if lootgoal is not None:
                goal = lootgoal
            elif crate is not None:
                if hyp(crate["pos"], (x, y)) > 30:
                    goal = crate["pos"]
                label = "crate"
            elif target is not None:
                if hyp(target["pos"], (x, y)) > 35:
                    goal = target["pos"]
                label = "npc"
            elif not in_safe:
                goal = tcen
            else:
                mv = (0.0, 0.0)
                label = "hold"

        if goal is not None and (self.mode != "fight" or label == "zone"):
            slot = self.hide_slot if self.mode == "hide" else (self.loot_slot if label.startswith(("loot", "cube", "crate", "npc")) else self.zone_slot)
            mv = self.nav(me, slot, goal)
            if self.mode == "hide" or slot is self.zone_slot:
                pass
        # field slots do their time-sliced work
        dl = t0 + 0.008
        for s in (self.hide_slot, self.loot_slot, self.zone_slot):
            s.step(dl)

        # ---- dodge incoming bullets
        cx, cy, cz = x, y, z + 4.5
        for pr in obs["projectiles"]:
            vx, vy, vz = pr["vel"]
            sp2 = vx * vx + vy * vy + vz * vz
            if sp2 < 1:
                continue
            rx, ry, rz = pr["pos"][0] - cx, pr["pos"][1] - cy, pr["pos"][2] - cz
            tt = -(rx * vx + ry * vy + rz * vz) / sp2
            if 0 < tt < 0.9 and pr["kind"] != "mortar":
                mx, my, mz = rx + vx * tt, ry + vy * tt, rz + vz * tt
                if math.sqrt(mx * mx + my * my + mz * mz) < 4.5:
                    sh = math.hypot(vx, vy) or 1.0
                    px, py = -vy / sh, vx / sh
                    side = 1.0 if (mx * px + my * py) < 0 else -1.0
                    mv = (px * side, py * side)
                    break
            elif pr["kind"] == "mortar":
                # run away from the landing point if it lands close
                g = cfg["weapons"]["mortar"]["gravity"]
                tl = tt if tt > 0 else 0
                lx = pr["pos"][0] + vx * min(tl + 0.0, 3)
                ly = pr["pos"][1] + vy * min(tl + 0.0, 3)
                if math.hypot(lx - x, ly - y) < 12 and pr["pos"][2] < z + 60:
                    ax, ay = x - lx, y - ly
                    an = math.hypot(ax, ay) or 1.0
                    mv = (ax / an, ay / an)
                    boost = me["energy"] > 20
                    break

        # ---- unstick
        if self.last_pos is not None:
            moved = math.hypot(x - self.last_pos[0], y - self.last_pos[1])
            if (mv[0] or mv[1]) and moved < 0.4 and me["grounded"]:
                self.stuck_t += 0.1
            else:
                self.stuck_t = 0.0
        self.last_pos = (x, y)
        if self.stuck_t > 0.8 and t > self.unstick_until:
            a = self.rng.random() * 2 * math.pi
            self.unstick_dir = (math.cos(a), math.sin(a))
            self.unstick_until = t + 0.8
            self.stuck_t = 0.0
        if t < self.unstick_until:
            mv = self.unstick_dir
            act["jump"] = True
            boost = me["energy"] > 40

        # ---- aim fallback
        if aim is None:
            if recent_hit and self.last_dmg_bearing is not None and not enemies:
                aim = [self.last_dmg_bearing, 0.0]
            elif threats and not enemies:
                aim = [math.atan2(threats[0][1] - y, threats[0][0] - x), 0.0]
            elif enemies:
                aim = [math.atan2(enemies[0]["pos"][1] - y, enemies[0]["pos"][0] - x), 0.0]
            elif mv[0] or mv[1]:
                aim = [math.atan2(mv[1], mv[0]), 0.0]
            else:
                self.sweep = wrap(self.sweep + 0.5)
                aim = [self.sweep, 0.0]

        act["move"] = [round(mv[0], 3), round(mv[1], 3)]
        act["boost"] = bool(boost)
        act["aim"] = [round(aim[0], 4), round(aim[1], 4)]
        act["fire"] = fire
        if "jump" not in act:
            act["jump"] = False

        # ---- debug drawing (cheap)
        mf.draw.label("%s hp%d" % (label, me["hp"]))
        if goal is not None:
            mf.draw.line([x, y, z + 2], [goal[0], goal[1], z + 2], "#40c0ff")
        mf.draw.point([tcen[0], tcen[1], z], "#ffd060", 6)
        if self.hide_spot is not None and self.mode == "hide":
            mf.draw.point([self.hide_spot[0], self.hide_spot[1], z], "#50ff80", 6)
        if target is not None:
            mf.draw.line([x, y, z + 7], target["pos"], "#ff5040")
        return act


if __name__ == "__main__":
    mf.run(Bot)
