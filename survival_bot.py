"""Mech Royale bot v2: starter-kit behavior plus cautious survival improvements."""
import math
import mechfight as mf


def unit(x, y):
    length = math.hypot(x, y)
    if length < 1e-9:
        return (0.0, 0.0)
    return (x / length, y / length)


class Bot:
    name = "survival-py-v2"

    def __init__(self, hello: mf.Hello):
        self.reach = hello["config"]["loot"]["pickup_radius"]

    def act(self, obs: mf.Observation) -> mf.Action:
        me = obs["self"]
        x, y, z = me["pos"]
        goal = obs["zone"]["next_center"]
        action: mf.Action = {"tick": obs["tick"]}

        # Prefer defensive upgrades when offered; otherwise keep the starter default.
        offer = obs.get("offer")
        if offer:
            choices = offer.get("choices", [])
            defensive_words = ("armor", "health", "regen", "repair", "shield")
            chosen = 0
            for i, choice in enumerate(choices):
                label = (
                    str(choice.get("id", "")) + " "
                    + str(choice.get("name", "")) + " "
                    + str(choice.get("desc", ""))
                ).lower()
                if any(word in label for word in defensive_words):
                    chosen = i
                    break
            action["pick"] = chosen

        # Restore the starter bot's weapon pickup behavior.
        near = [
            p for p in obs.get("pickups", [])
            if math.dist(p["pos"][:2], (x, y)) < self.reach
        ]
        weapons = me.get("weapons", [None, None])
        if len(weapons) > 1 and weapons[1] is None and near:
            if near[0]["kind"] != "gun":
                action["take"] = near[0]["id"]

        # Keep moving toward the next zone, as the original starter bot did.
        to_goal_x = goal[0] - x
        to_goal_y = goal[1] - y
        move_x, move_y = unit(to_goal_x, to_goal_y)

        # Small emergency dodge only when an enemy mech gets very close.
        enemies = [
            m for m in obs.get("mechs", [])
            if m.get("kind") == "mech"
        ]
        nearest = None
        nearest_distance = float("inf")
        for enemy in enemies:
            ex, ey = enemy["pos"][:2]
            distance = math.hypot(ex - x, ey - y)
            if distance < nearest_distance:
                nearest_distance = distance
                nearest = (ex, ey)

        if nearest is not None and nearest_distance < 24.0:
            away_x, away_y = unit(x - nearest[0], y - nearest[1])
            move_x, move_y = unit(
                move_x + away_x * 0.65,
                move_y + away_y * 0.65,
            )

        # Restore periodic jumping and the starter's zone-based boost.
        action.update(
            move=[move_x, move_y],
            boost=not me["in_zone"],
            jump=bool(me.get("grounded", False) and obs["tick"] % 40 == 0),
        )

        # Aim at the nearest visible mech; otherwise face the next zone.
        yaw = math.atan2(to_goal_y, to_goal_x)
        action["aim"] = [yaw, 0.0]
        action["fire"] = [False, False]
        if enemies:
            target = min(
                enemies,
                key=lambda m: math.dist(m["pos"], me["pos"]),
            )["pos"]
            horizontal_distance = max(math.dist(target[:2], (x, y)), 0.1)
            aim = [
                math.atan2(target[1] - y, target[0] - x),
                math.atan2(target[2] - z - 2.5, horizontal_distance),
            ]
            action.update(aim=aim, fire=[True, True])
            mf.draw.line(
                [x, y, z + 7],
                [target[0], target[1], target[2] + 4.5],
                "#ff5040",
            )

        mf.draw.point([goal[0], goal[1], z], "#40c0ff", 6)
        mf.draw.label(
            f"hp {me['hp']:.0f} · {len(enemies)} mechs · "
            f"nearest {nearest_distance:.0f}m"
            if nearest is not None
            else f"hp {me['hp']:.0f} · {len(enemies)} mechs"
        )
        return action


mf.run(Bot)
