"""Mech Royale bot v3: zone-first survival, threat-aware movement, and practical combat."""
import math
import mechfight as mf


def unit(x, y):
    length = math.hypot(x, y)
    if length < 1e-9:
        return (0.0, 0.0)
    return (x / length, y / length)


def angle_delta(a, b):
    return (a - b + math.pi) % (2.0 * math.pi) - math.pi


def choose_upgrade(offer):
    choices = offer.get("choices", []) if isinstance(offer, dict) else []
    if not choices:
        return 0
    # Defensive and sustain upgrades are preferred because placement is the objective.
    priorities = [
        ("armor", "health", "regen", "repair", "shield", "damage_taken"),
        ("energy_regen", "energy", "boost", "mobility", "speed"),
        ("sensor", "vision", "radar"),
        ("gun_rate", "damage", "weapon", "reload", "ammo"),
    ]
    for group in priorities:
        for i, choice in enumerate(choices):
            if not isinstance(choice, dict):
                continue
            label = " ".join(str(choice.get(k, "")) for k in ("id", "name", "desc")).lower()
            if any(word in label for word in group):
                return i
    return 0


class Bot:
    name = "survival-py-v3"

    def __init__(self, hello: mf.Hello):
        config = hello.get("config", {})
        self.pickup_radius = config.get("loot", {}).get("pickup_radius", 4.0)
        self.slot = hello.get("slot", 0)

    def act(self, obs: mf.Observation) -> mf.Action:
        me = obs["self"]
        x, y, z = me["pos"]
        tick = obs["tick"]
        zone = obs.get("zone", {})
        action: mf.Action = {"tick": tick}

        # Take the most useful available defensive upgrade.
        offer = obs.get("offer")
        if offer:
            action["pick"] = choose_upgrade(offer)

        # Predict the safe zone position during an active shrink.
        center = zone.get("center", [x, y])
        next_center = zone.get("next_center", center)
        radius = float(zone.get("radius", 500.0))
        next_radius = float(zone.get("next_radius", radius))
        now = float(obs.get("t", 0.0))
        shrink_start = float(zone.get("shrink_start", now + 9999.0))
        shrink_end = float(zone.get("shrink_end", shrink_start + 1.0))
        if shrink_end > shrink_start and now > shrink_start:
            progress = max(0.0, min(1.0, (now - shrink_start) / (shrink_end - shrink_start)))
        else:
            progress = 0.0
        safe_cx = center[0] + (next_center[0] - center[0]) * progress
        safe_cy = center[1] + (next_center[1] - center[1]) * progress
        safe_radius = radius + (next_radius - radius) * progress

        # Give each spawn slot a slightly different interior destination to reduce crowding.
        slot_angle = (self.slot * 2.399963229728653) % (2.0 * math.pi)
        target_radius = max(0.0, min(0.28 * safe_radius, safe_radius - 35.0))
        goal_x = safe_cx + math.cos(slot_angle) * target_radius
        goal_y = safe_cy + math.sin(slot_angle) * target_radius
        goal_dx, goal_dy = goal_x - x, goal_y - y
        goal_dist = math.hypot(goal_dx, goal_dy)
        move_x, move_y = unit(goal_dx, goal_dy)

        # Threat assessment: only real mechs count as opponents; NPCs are ignored for evasion.
        enemies = [m for m in obs.get("mechs", []) if m.get("kind") == "mech" and m.get("id") != me.get("id")]
        nearest = None
        nearest_dist = float("inf")
        for enemy in enemies:
            ex, ey = enemy.get("pos", [x, y, z])[:2]
            d = math.hypot(ex - x, ey - y)
            if d < nearest_dist:
                nearest_dist, nearest = d, enemy

        # Create space from nearby enemies while continuing generally toward safety.
        if nearest is not None and nearest_dist < 42.0:
            ex, ey = nearest["pos"][:2]
            away_x, away_y = unit(x - ex, y - ey)
            strength = 1.35 if nearest_dist < 24.0 else 0.65
            move_x, move_y = unit(move_x + away_x * strength, move_y + away_y * strength)

        # Aim at the nearest visible mech. Face threats even when outside firing range:
        # rear hits deal substantially more damage than frontal hits.
        target = nearest
        aim_yaw = math.atan2(goal_dy, goal_dx) if goal_dist > 1.0 else me.get("aim", [0.0, 0.0])[0]
        aim_pitch = 0.0
        fire = [False, False]
        weapons = me.get("weapons", [None, None])
        if target is not None:
            tx, ty, tz = target.get("pos", [x, y, z])
            tv = target.get("vel", [0.0, 0.0, 0.0])
            horizontal = max(0.1, math.hypot(tx - x, ty - y))
            w0 = weapons[0] if len(weapons) > 0 else None
            w0stats = (w0 or {}).get("stats", {})
            bullet_speed = float(w0stats.get("speed", 252.0))
            travel = min(1.0, horizontal / max(1.0, bullet_speed))
            lead_x = tx + (tv[0] if len(tv) > 0 else 0.0) * travel
            lead_y = ty + (tv[1] if len(tv) > 1 else 0.0) * travel
            aim_yaw = math.atan2(lead_y - y, lead_x - x)
            aim_pitch = math.atan2((tz + 4.5) - (z + 7.0), max(0.1, math.hypot(lead_x - x, lead_y - y)))

            # Slot 0: use the starting gun, reloading before it is completely empty.
            if w0 and w0.get("kind") == "gun":
                ammo = int(w0.get("ammo", 0))
                reload_left = float(w0.get("reload", 0.0))
                distance = math.hypot(tx - x, ty - y)
                gun_range = float(w0stats.get("range", 230.0))
                if ammo <= 2 and reload_left <= 0.0:
                    action["reload"] = True
                elif ammo > 0 and reload_left <= 0.0 and distance <= gun_range:
                    fire[0] = True

            # Slot 1: use the weapon's actual firing mechanic rather than blindly holding it.
            w1 = weapons[1] if len(weapons) > 1 else None
            if w1:
                kind = w1.get("kind")
                stats = w1.get("stats", {})
                distance = math.hypot(tx - x, ty - y)
                if kind == "lightning":
                    if distance <= float(stats.get("range", 45.0)) and float(me.get("energy", 0.0)) >= float(stats.get("energy", 3.0)):
                        fire[1] = True
                elif kind == "laser":
                    charge = float(w1.get("charge", 0.0))
                    laser_range = float(stats.get("range", 280.0))
                    if distance <= laser_range:
                        # Hold to charge; release at a strong charge to fire the beam.
                        fire[1] = charge < min(0.65, float(stats.get("charge", 0.75)))
                elif kind == "mortar":
                    mortar_range = float(stats.get("range", 235.0))
                    if 14.0 < distance < mortar_range:
                        speed = float(stats.get("speed", 72.0))
                        gravity = float(stats.get("gravity", 22.0))
                        height = (tz + 4.5) - (z + 9.5)
                        disc = speed ** 4 - gravity * (gravity * distance ** 2 + 2.0 * height * speed ** 2)
                        if disc >= 0.0:
                            tan_low = (speed ** 2 - math.sqrt(disc)) / max(1e-6, gravity * distance)
                            mortar_pitch = math.atan(tan_low)
                            # Avoid changing aim away from the gun target when the mortar solution is unsafe.
                            aim_pitch = mortar_pitch
                            fire[1] = True

        # If no mech is visible, turn toward recent incoming damage or nearby weapon sounds.
        if target is None:
            for event in reversed(obs.get("events", [])):
                if event.get("type") == "damaged" and event.get("bearing") is not None:
                    aim_yaw = float(event["bearing"])
                    break
            else:
                sounds = obs.get("sounds", [])
                useful = [s for s in sounds if s.get("dist") in ("near", "mid")]
                if useful:
                    sound = min(useful, key=lambda s: float(s.get("age", 99.0)))
                    aim_yaw = float(sound.get("bearing", aim_yaw))

        # Pick up a weapon only when it is already within the pickup radius; don't detour into danger.
        if len(weapons) > 1 and weapons[1] is None:
            candidates = []
            for pickup in obs.get("pickups", []):
                pos = pickup.get("pos", [x, y, z])
                horizontal = math.hypot(pos[0] - x, pos[1] - y)
                vertical = abs(pos[2] - z)
                if horizontal <= self.pickup_radius and vertical <= 4.0:
                    kind = pickup.get("kind")
                    preference = {"laser": 0, "lightning": 1, "mortar": 2}.get(kind, 9)
                    candidates.append((preference, horizontal, pickup))
            if candidates:
                action["take"] = min(candidates, key=lambda item: (item[0], item[1]))[2]["id"]

        # Save energy for emergencies; boost when repositioning or escaping the zone edge.
        distance_from_zone = math.hypot(x - safe_cx, y - safe_cy)
        outside_buffer = distance_from_zone > max(0.0, safe_radius - 25.0)
        energy = float(me.get("energy", 0.0))
        boost = energy > 38.0 and (outside_buffer or goal_dist > 85.0 or (nearest is not None and nearest_dist < 18.0))
        action.update(
            move=[move_x, move_y],
            boost=boost,
            jump=bool(me.get("grounded", False) and tick % 80 == 0),
            aim=[aim_yaw, aim_pitch],
            fire=fire,
        )

        mf.draw.point([goal_x, goal_y, z], "#40c0ff", 5)
        label = f"hp {me.get('hp', 0):.0f} · alive {obs.get('alive', '?')}"
        if nearest is not None:
            label += f" · enemy {nearest_dist:.0f}m"
        mf.draw.label(label)
        return action


mf.run(Bot)
