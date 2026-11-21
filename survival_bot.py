"""First improved Mech Royale bot: prioritize the safe zone and avoid nearby mechs."""
import math
import mechfight as mf


def unit(x, y):
    length = math.hypot(x, y)
    if length < 1e-9:
        return (0.0, 0.0)
    return (x / length, y / length)


class Bot:
    name = "survival-py-v1"

    def __init__(self, hello: mf.Hello):
        self.pickup_radius = hello["config"]["loot"]["pickup_radius"]

    def act(self, obs: mf.Observation) -> mf.Action:
        me = obs["self"]
        x, y, z = me["pos"]
        zone = obs["zone"]
        next_x, next_y = zone["next_center"]
        next_radius = zone["next_radius"]

        action: mf.Action = {"tick": obs["tick"]}

        # Pick a defensive upgrade when one is offered; otherwise use choice 0.
        offer = obs.get("offer")
        if offer:
            choices = offer.get("choices", [])
            defensive_words = ("armor", "health", "regen", "repair", "shield")
            chosen = 0
            for i, choice in enumerate(choices):
                label = (choice.get("id", "") + " " + choice.get("name", "") + " " + choice.get("desc", "")).lower()
                if any(word in label for word in defensive_words):
                    chosen = i
                    break
            action["pick"] = chosen

        # Move toward the next safe circle only when outside a comfortable buffer.
        to_zone_x = next_x - x
        to_zone_y = next_y - y
        zone_distance = math.hypot(to_zone_x, to_zone_y)
        move_x = 0.0
        move_y = 0.0
        if zone_distance > next_radius * 0.72:
            move_x, move_y = unit(to_zone_x, to_zone_y)

        # Create space from nearby enemies instead of charging directly into them.
        enemies = [m for m in obs.get("mechs", []) if m.get("kind") == "mech"]
        escape_x = 0.0
        escape_y = 0.0
        for enemy in enemies:
            ex, ey = enemy["pos"][:2]
            dx = x - ex
            dy = y - ey
            distance = max(math.hypot(dx, dy), 1.0)
            if distance < 55.0:
                # Closer enemies exert a stronger push away from us.
                weight = (55.0 - distance) / 55.0
                escape_x += (dx / distance) * weight
                escape_y += (dy / distance) * weight

        move_x += escape_x * 1.25
        move_y += escape_y * 1.25
        move_x, move_y = unit(move_x, move_y)
        action["move"] = [move_x, move_y]

        # Save boost energy unless we're outside the current zone or far from safety.
        outside_current = not me.get("in_zone", True)
        needs_urgent_travel = zone_distance > next_radius * 0.9
        energy = me.get("energy", 100)
        action["boost"] = bool((outside_current or needs_urgent_travel) and energy > 25)

        # Keep the simple starter targeting for now; we'll improve leading/weapon logic next.
        targets = sorted(
            obs.get("mechs", []),
            key=lambda target: math.dist(target["pos"], me["pos"]),
        )
        if targets:
            target = targets[0]["pos"]
            horizontal_distance = math.dist(target[:2], (x, y))
            yaw = math.atan2(target[1] - y, target[0] - x)
            pitch = math.atan2(target[2] - z - 2.5, max(horizontal_distance, 0.1))
            action["aim"] = [yaw, pitch]
            action["fire"] = [True, True]
        else:
            # Look toward the safe area when no target is visible.
            action["aim"] = [math.atan2(to_zone_y, to_zone_x), 0.0]
            action["fire"] = [False, False]

        mf.draw.point([next_x, next_y, z], "#40c0ff", 6)
        mf.draw.label(f"hp {me.get('hp', 0):.0f} · zone {zone_distance:.0f}m · {len(enemies)} mechs")
        return action


mf.run(Bot)
