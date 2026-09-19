import math

from . import *

# -------------------------------------------------------------------------------------
# Heal-chain payload stall -- v3
#
# The plan:
#   1. One Battle bot ("the tank") heads straight for the payload and parks in the
#      capture zone. Alone there, it pushes the payload toward the enemy goal every
#      tick -- free progress, and banks us the `PayloadProgress` tiebreak -- until an
#      enemy shows up to contest, at which point the zone freezes completely (see
#      `step_payload`: any nonzero count on both sides holds position outright,
#      regardless of how many bots either side has).
#   2. 3 Healers (layer 1) go heal the tank, fanned out on separate rays around it so
#      they stay `SAFE_SPACING` apart. `heal_stack_cap` is 3.0, i.e. exactly 3 healers'
#      worth of healing lands on one target -- a 4th on the same bot would be wasted.
#   3. 5 Extractors mine our own deposit, placed by searching for spots that actually
#      have a clear shot at it rather than assuming a fixed offset is clear.
#   4. 9 more Healers (layer 2), 3 per layer-1 healer, fanned the same way around their
#      own parent -- so each layer-1 healer is protected by a full 3-healer stack.
#   5. Everything built after that is a Battle bot on standby. Layer 1 gets kept alive
#      by layer 2's healing, but nothing heals layer 2 itself -- so standby bodyguards
#      split between guarding the tank directly (close range, anti-rush) and guarding
#      layer 2 specifically (the chain's actual soft spot), rather than a wide ring that
#      does neither job well and wanders into enemy territory on the far side.
#   6. Once fully built (fleet at cap) with tokens to spare, extractors have nothing
#      left to fund -- a full fleet silently refuses rushes -- so they self-destruct to
#      free their slots for more Healers/Battle bots instead of sitting there earning
#      tokens nobody can spend.
#   7. Every Battle bot (tank, tank guards, and layer-2 guards alike) engages the
#      nearest enemy it has a clear shot at, whatever else it is doing that tick.
#
# Still a first pass: role assignment is by rank (Nth-smallest id within a class), not a
# persistent assignment, so a dead tank/healer is transparently replaced by whichever
# surviving bot of that class now has the lowest id.

TANK_COUNT = 1
HEALER_LAYER_1 = 3
EXTRACTOR_COUNT = 5
HEALER_LAYER_2 = 9
HEALER_TOTAL = HEALER_LAYER_1 + HEALER_LAYER_2  # 12
TANK_GUARD_COUNT = 4  # of the standby Battle bots, how many stay glued to the tank

# `LEFT` is the healer fan's general heading -- away from the payload, toward the map
# edge. Every unit sees itself as bottom-left (the engine mirrors team B's world), so
# "the left edge" means the same thing regardless of which side we actually are.
# `RIGHT` is just a zero-degree reference for the full-circle placements (the guard
# rings, the extractor search), which have no preferred heading.
LEFT = Vec2(-1.0, 0.0)
RIGHT = Vec2(1.0, 0.0)

# Once this many ticks worth of mining go idle with nowhere to spend it (see the
# extractor-retirement check), it is a module-level flag rather than something derived
# fresh from `state` each tick: once we decide extraction is done for the match, we do
# not want a temporary dip in fleet size from combat losses to talk us back into
# rebuilding extractors instead of the Battle/Healer bots we actually want by then.
_extraction_retired = False


def get_strategy(team: int) -> Strategy:
    """This function tells the engine what strategy you want your bot to use."""

    # team == 0 means I am bottom left
    # team == 1 means I am top right

    # Same strategy both sides: the engine mirrors the world for team B, so there is
    # nothing for a side to specialize in.
    print(f"Hello! I am team {'A (bottom left)' if team == 0 else 'B (top right)'}")
    return heal_chain_strategy


def _next_build_class(battle_count: int, healer_count: int, extractor_count: int, extraction_retired: bool) -> BotClass:
    """The fabricator build order for this strategy, checked as a priority list: tank
    first, then the first healer layer, then extractors (unless retired -- see
    `heal_chain_strategy`), then the second healer layer, then Battle bots for as long
    as the fabricator keeps firing."""

    if battle_count < TANK_COUNT:
        return BotClass.Battle
    if healer_count < HEALER_LAYER_1:
        return BotClass.Healer
    if not extraction_retired and extractor_count < EXTRACTOR_COUNT:
        return BotClass.Extractor
    if healer_count < HEALER_TOTAL:
        return BotClass.Healer
    return BotClass.Battle


def _fan_directions(count: int, radius: float, spacing: float) -> List[Vec2]:
    """`count` unit directions fanned out around `LEFT`, spread wide enough that two
    points both sitting at `radius` along adjacent directions are still `spacing` apart.

    Used for both healer layers: layer 1 fans around the tank, and each layer-2 group
    fans around its own layer-1 parent the same way. That means two different layer-2
    groups can land close to each other if their parents happen to be close together --
    tolerated on purpose, since a group of 3 mutually-`spacing`-apart bodies cannot all
    also sit far from a shared parent within a bounded range. Spacing out from your own
    3 siblings and standing at full range from your own target both matter more than
    never coming near an unrelated branch.

    `2 * radius * sin(angle / 2)` is the chord length between two points at `radius` on
    rays `angle` degrees apart; solved for `angle` given the chord we want (`spacing`).
    """
    if count <= 1 or radius <= 0.0:
        return [LEFT for _ in range(count)]
    step_deg = math.degrees(2.0 * math.asin(min(1.0, spacing / (2.0 * radius))))
    return [LEFT.rotate_deg(step_deg * (i - (count - 1) / 2.0)) for i in range(count)]


def _ring_positions(center: Vec2, count: int, radius: float) -> List[Vec2]:
    """`count` positions evenly spaced around a full circle of `radius` around `center`."""
    if count <= 0:
        return []
    return [center + RIGHT.rotate_deg(360.0 * i / count) * radius for i in range(count)]


def _harvest_positions(conf: GameConfig, deposit_pos: Vec2, count: int) -> List[Vec2]:
    """`count` legal, mine-able spots around `deposit_pos`.

    An extractor's ray only needs `line_of_sight` to the deposit -- unlike the blaster
    and heal ranges, nothing here needs a tight formation -- so this searches outward
    for a spot that actually has that sightline instead of assuming a fixed offset is
    clear. A fixed offset put several extractors behind a wall with no shot at the
    deposit at all.

    Aims each slot at an evenly spaced angle around the deposit first (so they spread
    out when nothing is in the way), then, if that exact spot is blocked, tries nearby
    angles at growing radii until it finds one that works.
    """
    max_dist = conf.bot.base_extract_range - 2.0 * conf.bot.radius
    ring_step = 2.0 * conf.bot.radius + 0.2
    angle_jitters = [0.0, 20.0, -20.0, 40.0, -40.0, 60.0, -60.0, 80.0, -80.0, 100.0, -100.0]

    positions: List[Vec2] = []
    for i in range(count):
        base_angle = 360.0 * i / count
        spot = None
        radius = conf.deposit.radius + conf.bot.radius + 0.1
        while spot is None and radius <= max_dist:
            for jitter in angle_jitters:
                candidate = deposit_pos + RIGHT.rotate_deg(base_angle + jitter) * radius
                if point_free(candidate) and line_of_sight(candidate, deposit_pos):
                    spot = candidate
                    break
            radius += ring_step
        # Fall back to the deposit's own edge, unvalidated, rather than leaving an
        # extractor with nowhere to go at all -- should only trigger if the deposit is
        # very badly boxed in.
        positions.append(spot if spot is not None else deposit_pos + RIGHT.rotate_deg(base_angle) * (conf.deposit.radius + conf.bot.radius))
    return positions


def _approach(bot_pos: Vec2, anchor: Vec2, final_target: Vec2, near_dist: float) -> Vec2:
    """The move step toward `final_target`, but chasing `anchor` instead while still
    far from it.

    A crowd of units each individually beelining for its own precisely-angled final
    slot is what jammed at a single-tile gap: everyone approaching from a slightly
    different angle collides with everyone else doing the same, instead of filing
    through. Heading for one shared point (the tank, or the deposit) while still far
    away funnels the crowd through a bottleneck single-file the way pathfinding usually
    resolves that, and each unit only peels off to its own exact spot once it is
    already close to where that spot is.
    """
    if bot_pos.dist_sq(anchor) > near_dist * near_dist:
        return navigate_to(bot_pos, anchor)
    return navigate_to(bot_pos, final_target)


def _find_target(bot_pos: Vec2, enemies, max_range: float) -> Optional[BotState]:
    """The nearest enemy within `max_range` that this bot actually has a clear shot at."""
    best = None
    best_dist_sq = math.inf
    max_range_sq = max_range * max_range
    for enemy in enemies:
        dist_sq = bot_pos.dist_sq(enemy.pos)
        if dist_sq > max_range_sq or not line_of_sight(bot_pos, enemy.pos):
            continue
        if dist_sq < best_dist_sq:
            best, best_dist_sq = enemy, dist_sq
    return best


def _engage_if_possible(bot_action: BotAction, bot_pos: Vec2, enemies, blaster_range: float) -> bool:
    """Every Battle bot's combat behavior, regardless of what else it is doing this
    tick: if there is a target in range with a clear shot, turn onto it and fire,
    overriding whatever facing its positional job would otherwise want. Picking the
    *nearest* target in range is what makes this double as "shoot anything that gets
    too close" for the guards -- a closing enemy becomes the nearest one automatically."""
    target = _find_target(bot_pos, enemies, blaster_range)
    if target is None:
        return False
    bot_action.turn_action = turn_towards(target.pos)
    bot_action.special_action = SpecialAction.Battle(fire=True)
    return True


def heal_chain_strategy(state: GameState) -> FleetAction:
    global _extraction_retired

    conf = get_config()
    action = FleetAction.new()
    payload = state.payload_pos()

    battle_bots = sorted((b for b in state.fleet_me if b.class_ == BotClass.Battle), key=lambda b: b.id)
    healer_bots = sorted((b for b in state.fleet_me if b.class_ == BotClass.Healer), key=lambda b: b.id)
    extractor_bots = [b for b in state.fleet_me if b.class_ == BotClass.Extractor]

    battle_count = len(battle_bots)
    healer_count = len(healer_bots)
    extractor_count = len(extractor_bots)

    tank = battle_bots[0] if battle_bots else None
    layer1 = healer_bots[:HEALER_LAYER_1]
    layer2 = healer_bots[HEALER_LAYER_1:HEALER_TOTAL]
    standby_battle = battle_bots[1:]

    # Units should almost never stand close enough to share a blast's splash. A blast
    # damages anything within `base_blaster_splash_radius + bot.radius` of the impact
    # point, so two bots closer than twice that could both be caught by one shot;
    # `SAFE_SPACING` is that threshold, read from config rather than hardcoded.
    SAFE_SPACING = 2.0 * (conf.bot.base_blaster_splash_radius + conf.bot.radius)
    # Stay comfortably inside heal range rather than right at the edge of it, so
    # jostling from collisions or the tank's own movement does not slip a healer out.
    heal_max_dist = conf.bot.base_heal_range - 2.0 * conf.bot.radius
    # How far a guard stands from whoever it is protecting: outside that ally's own
    # splash zone even in the worst case (the impact point landing on the near edge of
    # the ally's own hull, `conf.bot.radius` closer than its center), plus the guard's
    # own hull needing to clear the blast from there, plus a small safety margin.
    guard_standoff = conf.bot.base_blaster_splash_radius + 2.0 * conf.bot.radius + 0.2
    # The healer chain's own overall reach from the tank, used to decide when an
    # approaching unit has arrived in the chain's neighborhood and should switch from
    # heading for the tank to taking its own exact spot.
    chain_reach = 2.0 * heal_max_dist

    # --- the tank: hold (and, while uncontested, push) the payload ---
    if tank is not None:
        bot_action = action.bots[tank.id]
        bot_action.move_action = move_bot(navigate_to(tank.pos, payload))
        bot_action.turn_action = turn_towards(payload)
        _engage_if_possible(bot_action, tank.pos, state.fleet_other, conf.bot.blaster_range)

    # --- layer 1: heal the tank, fanned out around it. Chases the tank directly until
    # close, then peels off to its own ray -- see `_approach`. ---
    if tank is not None:
        for healer, direction in zip(layer1, _fan_directions(len(layer1), heal_max_dist, SAFE_SPACING)):
            pos = tank.pos + direction * heal_max_dist
            bot_action = action.bots[healer.id]
            bot_action.move_action = move_bot(_approach(healer.pos, tank.pos, pos, heal_max_dist))
            bot_action.turn_action = turn_towards(tank.pos)
            bot_action.special_action = SpecialAction.Healer(fire=True, target=tank.id)

    # --- layer 2: heal layer 1, 3 healers per layer-1 target, each group fanned out
    # around its own parent the same way layer 1 fans around the tank. Also chases the
    # tank first -- not its own (possibly still-arriving) parent -- since the tank is
    # the one point everyone in the chain is converging on. ---
    for group_start in range(0, len(layer2), 3):
        group = layer2[group_start:group_start + 3]
        parent = layer1[group_start // 3]
        for healer, direction in zip(group, _fan_directions(len(group), heal_max_dist, SAFE_SPACING)):
            pos = parent.pos + direction * heal_max_dist
            bot_action = action.bots[healer.id]
            bot_action.move_action = move_bot(_approach(healer.pos, tank.pos if tank is not None else parent.pos, pos, chain_reach))
            bot_action.turn_action = turn_towards(parent.pos)
            bot_action.special_action = SpecialAction.Healer(fire=True, target=parent.id)

    # --- extractors: retire once there is nowhere left for the tokens to go, otherwise
    # mine our own deposit wherever around it actually has a sightline ---
    if not _extraction_retired and extractor_count > 0 and battle_count + healer_count + extractor_count >= BOTS_MAX:
        # A full fleet silently refuses a rush without even charging for it, so once we
        # are capped, mining income has nothing left to buy. Only require enough for
        # one rush, not all 5 slots at once: combat attrition already spends tokens on
        # refilling losses, so waiting for a big reserve to pile up on top of that could
        # take far longer than just freeing the slots and rebuilding gradually.
        if state.fabricator_me.tokens >= conf.fabricator.rush_cost:
            _extraction_retired = True

    if _extraction_retired:
        for extractor in extractor_bots:
            action.bots[extractor.id].self_destruct = True
    else:
        positions = _harvest_positions(conf, state.deposit_me.pos, extractor_count)
        for extractor, pos in zip(extractor_bots, positions):
            bot_action = action.bots[extractor.id]
            bot_action.move_action = move_bot(_approach(extractor.pos, state.deposit_me.pos, pos, conf.bot.base_extract_range))
            bot_action.turn_action = turn_towards(state.deposit_me.pos)
            bot_action.special_action = SpecialAction.Extractor(mine=True)

    # --- standby Battle bots: bodyguards, not a wide perimeter. Layer 1 gets kept
    # alive by layer 2's healing, but nothing heals layer 2 -- so guards split between
    # the tank (close range, catches anything that gets in melee-close to threaten the
    # tank and the nearby layer-1 healers) and layer 2 specifically (the chain's actual
    # unguarded soft spot). Both groups stand just outside splash range of whoever they
    # guard, and both chase the tank first while still far out, same as the healers, so
    # the whole formation -- healers, guards, all of it -- can just walk together when
    # the tank relocates with the payload. ---
    if tank is not None and standby_battle:
        tank_guards = standby_battle[:TANK_GUARD_COUNT]
        rear_guards = standby_battle[TANK_GUARD_COUNT:]

        tank_guard_positions = _ring_positions(tank.pos, len(tank_guards), guard_standoff)
        for bot, pos in zip(tank_guards, tank_guard_positions):
            bot_action = action.bots[bot.id]
            bot_action.move_action = move_bot(_approach(bot.pos, tank.pos, pos, guard_standoff * 2.0))
            _engage_if_possible(bot_action, bot.pos, state.fleet_other, conf.bot.blaster_range)

        if layer2:
            # Round-robin the rear guards across the layer-2 healers so extra guards
            # (once the fleet is big enough to have more than 9 standby left over)
            # double up instead of piling onto just the first few.
            per_healer: List[List[BotState]] = [[] for _ in layer2]
            for i, bot in enumerate(rear_guards):
                per_healer[i % len(layer2)].append(bot)
            for healer, guards in zip(layer2, per_healer):
                for bot, pos in zip(guards, _ring_positions(healer.pos, len(guards), guard_standoff)):
                    bot_action = action.bots[bot.id]
                    bot_action.move_action = move_bot(_approach(bot.pos, tank.pos, pos, chain_reach))
                    _engage_if_possible(bot_action, bot.pos, state.fleet_other, conf.bot.blaster_range)
        else:
            # No layer 2 yet to guard -- fall in around the tank instead of standing
            # around with no orders.
            for bot, pos in zip(rear_guards, _ring_positions(tank.pos, len(rear_guards), guard_standoff)):
                bot_action = action.bots[bot.id]
                bot_action.move_action = move_bot(_approach(bot.pos, tank.pos, pos, guard_standoff * 2.0))
                _engage_if_possible(bot_action, bot.pos, state.fleet_other, conf.bot.blaster_range)

    # --- fabricator ---
    action.fabricator_next = int(_next_build_class(battle_count, healer_count, extractor_count, _extraction_retired))

    # Rush whenever we can actually afford it, so the chain fills in as fast as tokens
    # allow instead of waiting on `conf.fabricator.interval`'s natural cadence. No bot is
    # built in the endgame, so do not bother asking then.
    in_endgame = state.tick >= conf.max_ticks - conf.endgame_ticks
    action.rush_order = (not in_endgame
                         and state.fabricator_me.tokens >= conf.fabricator.rush_cost)

    return action
