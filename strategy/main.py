import math

from . import *

# -------------------------------------------------------------------------------------
# Heal-chain payload stall -- v1
#
# The plan:
#   1. One Battle bot ("the tank") heads straight for the payload and parks in the
#      capture zone. Alone there, it pushes the payload toward the enemy goal every
#      tick -- free progress, and banks us the `PayloadProgress` tiebreak -- until an
#      enemy shows up to contest, at which point the zone freezes completely (see
#      `step_payload`: any nonzero count on both sides holds position outright,
#      regardless of how many bots either side has).
#   2. 3 Healers (layer 1) go heal the tank. `heal_stack_cap` is 3.0, i.e. exactly 3
#      healers' worth of healing lands on one target -- a 4th healer on the same bot
#      would be wasted, which is why layer 1 stops at 3.
#   3. 5 Extractors mine our own deposit for income.
#   4. 9 more Healers (layer 2), 3 per layer-1 healer, so each layer-1 healer is itself
#      protected by a full 3-healer stack.
#   5. Everything built after that is a Battle bot held on standby (no orders yet --
#      just sits at spawn). Real behavior (rally point, counter-push, defense) comes in
#      a later iteration.
#
# This is a first pass on purpose: role assignment is by rank (Nth-smallest id within a
# class), not a persistent assignment, so a dead tank/healer is transparently replaced
# by whichever surviving bot of that class now has the lowest id. No formation spacing,
# no retreat/regroup logic, no reaction to what the enemy is doing yet.

TANK_COUNT = 1
HEALER_LAYER_1 = 3
EXTRACTOR_COUNT = 5
HEALER_LAYER_2 = 9
HEALER_TOTAL = HEALER_LAYER_1 + HEALER_LAYER_2  # 12

# The formation axis every "don't stack" spread below is measured along. Every unit sees
# itself as bottom-left (the engine mirrors team B's world), so "the left edge" -- toward
# x = 0 -- means the same thing regardless of which side we actually are.
LEFT = Vec2(-1.0, 0.0)


def get_strategy(team: int) -> Strategy:
    """This function tells the engine what strategy you want your bot to use."""

    # team == 0 means I am bottom left
    # team == 1 means I am top right

    # Same strategy both sides: the engine mirrors the world for team B, so there is
    # nothing for a side to specialize in.
    print(f"Hello! I am team {'A (bottom left)' if team == 0 else 'B (top right)'}")
    return heal_chain_strategy


def _next_build_class(battle_count: int, healer_count: int, extractor_count: int) -> BotClass:
    """The fabricator build order for this strategy, checked as a priority list: tank
    first, then the first healer layer, then extractors, then the second healer layer,
    then Battle bots for as long as the fabricator keeps firing."""

    if battle_count < TANK_COUNT:
        return BotClass.Battle
    if healer_count < HEALER_LAYER_1:
        return BotClass.Healer
    if extractor_count < EXTRACTOR_COUNT:
        return BotClass.Extractor
    if healer_count < HEALER_TOTAL:
        return BotClass.Healer
    return BotClass.Battle


def _packed_positions(anchor: Vec2, direction: Vec2, count: int, max_dist: float, spacing: float) -> List[Vec2]:
    """`count` positions strung out from `anchor` along `direction`, spaced `spacing`
    apart so neighbors -- and a shared blast's splash radius -- cannot reach two of them
    at once.

    The farthest one sits at `max_dist` (as far from `anchor` as a group parked on the
    same target can be while staying in range of it); the rest pack in closer, one
    spacing at a time, rather than all landing on `anchor` itself.

    Floors at `0.0`, not `spacing`: consecutive slots are already exactly `spacing`
    apart by construction, so clamping the closest one up to `spacing` would only pull
    it *into* its neighbor instead of away from it. A floor of `0.0` just stops a
    crowded group (more bots than `max_dist` comfortably fits at `spacing` apart) from
    wrapping past `anchor` onto the wrong side.
    """
    return [
        anchor + direction * max(0.0, max_dist - (count - 1 - i) * spacing)
        for i in range(count)
    ]


def _fan_directions(count: int, radius: float, spacing: float) -> List[Vec2]:
    """`count` unit directions fanned out around `LEFT`, spread wide enough that two
    points both sitting at `radius` along adjacent directions are still `spacing` apart.

    This is what keeps sibling branches of the heal chain from colliding: layer 1 sits
    at `radius` = `heal_max_dist` from the tank, each on its own ray, and every layer-2
    group then continues straight out along its own parent's ray instead of the shared
    `LEFT` axis. Two rays that do not cross stay at least this separated at any distance
    from the tank -- the gap between them only grows the further out you go -- so it is
    enough to size the fan for the closest ring (layer 1) and every deeper rank inherits
    the guarantee for free.

    `2 * radius * sin(angle / 2)` is the chord length between two points at `radius` on
    rays `angle` degrees apart; solved for `angle` given the chord we want (`spacing`).
    """
    if count <= 1 or radius <= 0.0:
        return [LEFT for _ in range(count)]
    step_deg = math.degrees(2.0 * math.asin(min(1.0, spacing / (2.0 * radius))))
    return [LEFT.rotate_deg(step_deg * (i - (count - 1) / 2.0)) for i in range(count)]


def heal_chain_strategy(state: GameState) -> FleetAction:
    conf = get_config()
    action = FleetAction.new()
    payload = state.payload_pos()

    battle_bots = sorted((b for b in state.fleet_me if b.class_ == BotClass.Battle), key=lambda b: b.id)
    healer_bots = sorted((b for b in state.fleet_me if b.class_ == BotClass.Healer), key=lambda b: b.id)
    extractor_bots = [b for b in state.fleet_me if b.class_ == BotClass.Extractor]

    tank = battle_bots[0] if battle_bots else None
    layer1 = healer_bots[:HEALER_LAYER_1]
    layer2 = healer_bots[HEALER_LAYER_1:HEALER_TOTAL]
    standby_battle = battle_bots[1:]

    # Units should almost never stand close enough to share a blast's splash. A blast
    # damages anything within `base_blaster_splash_radius + bot.radius` of the impact
    # point, so two bots closer than twice that could both be caught by one shot;
    # `SAFE_SPACING` is that threshold, read from config rather than hardcoded.
    SAFE_SPACING = 2.0 * (conf.bot.base_blaster_splash_radius + conf.bot.radius)
    # Stay comfortably inside range rather than right at the edge of it, so jostling
    # from collisions or a target's own movement does not slip a healer/extractor out.
    RANGE_MARGIN = 2.0 * conf.bot.radius

    heal_max_dist = conf.bot.base_heal_range - RANGE_MARGIN
    extract_max_dist = conf.bot.base_extract_range - RANGE_MARGIN

    # The deposit is a solid disc, so standing dead-center is not the mining spot. This
    # is the closest legal spot on our own edge of the ring: hull to hull with it, `+y`
    # being the side away from the map center on our half. Extractors then line up
    # further from it along `LEFT`, same as everyone else.
    mining_spot = state.deposit_me.pos + Vec2(0.0, conf.deposit.radius + conf.bot.radius)

    # --- the tank: hold (and, while uncontested, push) the payload ---
    if tank is not None:
        bot_action = action.bots[tank.id]
        bot_action.move_action = move_bot(navigate_to(tank.pos, payload))
        bot_action.turn_action = turn_towards(payload)

    # --- layer 1: heal the tank. Each healer gets its own ray out of the tank (a fan
    # around LEFT, not a single shared line) so that its layer-2 children -- which
    # continue straight out along that same ray -- never cross another branch's. ---
    layer1_dirs = _fan_directions(len(layer1), heal_max_dist, SAFE_SPACING)
    if tank is not None:
        for healer, direction in zip(layer1, layer1_dirs):
            pos = tank.pos + direction * heal_max_dist
            bot_action = action.bots[healer.id]
            bot_action.move_action = move_bot(navigate_to(healer.pos, pos))
            bot_action.turn_action = turn_towards(tank.pos)
            bot_action.special_action = SpecialAction.Healer(fire=True, target=tank.id)

    # --- layer 2: heal layer 1, 3 healers per layer-1 target, each group lined up
    # behind its own target along that target's own ray out of the tank.
    #
    # Deliberately NOT a fresh fan around each layer-1 parent: that would put every
    # child at max range from its own parent too, but it breaks the one guarantee that
    # actually matters -- tried it, and two different branches' outer children landed on
    # the exact same point. Continuing straight out along the parent's own ray keeps
    # branches strictly diverging (checked: the gap between any two branches only grows
    # with distance from the tank), at the cost of the closest sibling in a group
    # sitting nearer its own target than the others -- 3 mutually-`SAFE_SPACING`-apart
    # bodies do not all fit near the far edge of a `heal_max_dist` circle around one
    # point. Worth another look later; not worth breaking branch separation for now. ---
    for group_start in range(0, len(layer2), 3):
        group = layer2[group_start:group_start + 3]
        parent_idx = group_start // 3
        target = layer1[parent_idx]
        direction = layer1_dirs[parent_idx]
        positions = _packed_positions(target.pos, direction, len(group), heal_max_dist, SAFE_SPACING)
        for healer, pos in zip(group, positions):
            bot_action = action.bots[healer.id]
            bot_action.move_action = move_bot(navigate_to(healer.pos, pos))
            bot_action.turn_action = turn_towards(target.pos)
            bot_action.special_action = SpecialAction.Healer(fire=True, target=target.id)

    # --- extractors: mine our own deposit, lined up behind the mining spot ---
    positions = _packed_positions(mining_spot, LEFT, len(extractor_bots), extract_max_dist, SAFE_SPACING)
    for extractor, pos in zip(extractor_bots, positions):
        bot_action = action.bots[extractor.id]
        bot_action.move_action = move_bot(navigate_to(extractor.pos, pos))
        bot_action.turn_action = turn_towards(state.deposit_me.pos)
        bot_action.special_action = SpecialAction.Extractor(mine=True)

    # --- standby Battle bots: no combat orders yet, just parked -- but still spread out
    # so they do not stack. They continue the same left-pointing line past where the
    # healer chain reaches, one `SAFE_SPACING` apart, so a stray shot can only ever
    # catch one of them.
    # TODO: give these something to do once the chain above is stable -- a rally point,
    # a counter-push trigger, or defense for the healer chain.
    if tank is not None:
        chain_reach = 2.0 * heal_max_dist  # layer 1's reach, plus layer 2's beyond it
        for i, bot in enumerate(standby_battle):
            dist = chain_reach + SAFE_SPACING * (i + 1)
            bot_action = action.bots[bot.id]
            bot_action.move_action = move_bot(navigate_to(bot.pos, tank.pos + LEFT * dist))

    # --- fabricator ---
    battle_count = len(battle_bots)
    healer_count = len(healer_bots)
    extractor_count = len(extractor_bots)
    action.fabricator_next = int(_next_build_class(battle_count, healer_count, extractor_count))

    # Rush whenever we can actually afford it, so the chain fills in as fast as tokens
    # allow instead of waiting on `conf.fabricator.interval`'s natural cadence. No bot is
    # built in the endgame, so do not bother asking then.
    in_endgame = state.tick >= conf.max_ticks - conf.endgame_ticks
    action.rush_order = (not in_endgame
                         and state.fabricator_me.tokens >= conf.fabricator.rush_cost)

    return action
