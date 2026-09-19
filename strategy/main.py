import math

from . import *

# -------------------------------------------------------------------------------------
# Heal-chain payload stall -- v5
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
#      The fan points away from wherever the payload is currently *heading*, not a
#      fixed world direction -- see `formation_back` -- so the chain does not end up on
#      the wrong side of the tank once the payload's path rounds a corner.
#   3. 5 Extractors mine our own deposit, placed by searching for spots that actually
#      have a clear shot at it rather than assuming a fixed offset is clear. If enemies
#      show up at the deposit, extractors flee rather than feed free kills; a small raid
#      (<= 2) gets matched by a temporary defense force pulled from standby, one bigger
#      than the raid; a bigger assault gets the deposit abandoned outright -- no more
#      extractors built while it holds, existing ones retreat, and the fabricator spends
#      that capacity on Battle/Healer bots instead.
#   4. 9 more Healers (layer 2), 3 per layer-1 healer, fanned the same way (and in the
#      same direction) around their own parent -- so each layer-1 healer is protected by
#      a full 3-healer stack.
#   5. Everything built after that is a Battle bot on standby. Layer 1 gets kept alive
#      by layer 2's healing, but nothing heals layer 2 itself -- so standby bodyguards
#      split between guarding the tank directly (close range, anti-rush) and guarding
#      layer 2 specifically (the chain's actual soft spot), rather than a wide ring that
#      does neither job well and wanders into enemy territory on the far side.
#   6. Once fully built (fleet at cap) with tokens to spare, extractors have nothing
#      left to fund -- a full fleet silently refuses rushes -- so they self-destruct.
#      Of the 5 freed slots, 2 become general healers (see 8) and the rest Battle.
#   7. Every Battle bot engages the nearest enemy it has a clear shot at, whatever else
#      it is doing that tick. The tank guards specifically do this as coordinated
#      groups of up to 4, all committing to the same target -- see `_pick_group_target`
#      for why (it is not what it sounds like: simultaneous hits do not stack damage).
#   8. Once extraction retires, 2 more Healers roam to whichever standby Battle bot --
#      tank guard or layer-2 guard -- is hurt worst. Nothing else heals them: they are
#      outside the tank-heal-tree entirely (layer 1 <- tank, layer 2 <- layer 1).
#
# Still a first pass: role assignment is by rank (Nth-smallest id within a class), not a
# persistent assignment (except the healer sub-roles -- see `_healer_roles` -- which
# specifically need it since layer 2 is a position, not just a label), so a dead
# tank/healer is transparently replaced by whichever surviving bot of that class now has
# the lowest id.

TANK_COUNT = 1
HEALER_LAYER_1 = 3
EXTRACTOR_COUNT = 5
HEALER_LAYER_2 = 9
HEALER_TOTAL = HEALER_LAYER_1 + HEALER_LAYER_2  # 12
GENERAL_HEALER_COUNT = 2  # extra floating healers, built only after extraction retires
TANK_GUARD_COUNT = 4  # of the standby Battle bots, how many stay glued to the tank
FOCUS_GROUP_SIZE = 4  # tank guards commit to one shared target per group this size
CAPTURE_TANGENT_DELTA = 0.01  # capture-progress step used to sample the payload's heading
DEPOSIT_GUARD_COUNT = 2  # Battle bots that escort the extractors from the start
# Extractors bunch within this arc on the sheltered side of the deposit. Deliberately
# tight -- the deposit is in the blaster scan mask, so it blocks shots, and hiding
# behind it is worth more than the splash-spacing we keep everywhere else.
DEPOSIT_COVER_ARC = 18.0

# Center-rush abort. If this many enemies are inside blaster range of the formation
# *and* healers are already dropping below half health, the chain is losing faster than
# reinforcements can walk in, so it gives ground instead of dying in place: the tank
# falls back along the payload path (everything else is anchored to the tank, so the
# whole formation goes with it) toward our own side, where replacements arrive sooner.
CENTER_RUSH_THRESHOLD = 5
RETREAT_HURT_HEALERS = 2
RETREAT_CAPTURE_STEP = 0.12  # how far back along the path to rally, in capture progress
RETREAT_HOLD_TICKS = 240  # regroup for this long, then go contest again
PAYLOAD_FLANKER_COUNT = 2  # standby bots posted off-axis to shoot into the payload's shadow
# Where the flankers stand, as rotations off `back` (the direction the formation trails
# in). Just the two flanks, deliberately not a post directly opposite the formation:
# checked the geometry, and a shooter on either flank already clears the payload by
# 1.3-2.4 units (vs its 0.75 radius) against anything anywhere on the far side, so the
# opposite post adds no coverage -- and reaching it means walking through the enemy,
# which in practice just got those bots killed in transit. Each flank also covers the
# other's one blind spot, straight across the payload from it.
FLANKER_ANGLES = (90.0, -90.0)

# ####################################################################################
# TESTING ONLY -- SET BACK TO False BEFORE `mm-cli submit`.
#
# Makes team B sit still and issue no orders, so a local `mm-cli run` shows this bot
# operating completely unobstructed: the payload actually travels (in a mirrored match
# both sides contest it and it never moves at all), which is the only way to watch the
# formation round the path's corners. Submitting with this on would hand every match
# where we are seeded as team B to the opponent for free.
# ####################################################################################
IDLE_OPPONENT_FOR_TESTING = False

# `LEFT` is the fallback formation heading for the rare tick where the payload's local
# direction of travel cannot be sampled (see `formation_back`). Every unit sees itself
# as bottom-left (the engine mirrors team B's world), so a fixed "toward the map edge"
# fallback means the same thing regardless of which side we actually are.
# `RIGHT` is just a zero-degree reference for the full-circle placements (the guard
# rings, the extractor search), which have no preferred heading.
LEFT = Vec2(-1.0, 0.0)
RIGHT = Vec2(1.0, 0.0)

# Once this flips, it is a module-level flag rather than something derived fresh from
# `state` each tick: once we decide extraction is done for the match, we do not want a
# temporary dip in fleet size from combat losses to talk us back into rebuilding
# extractors instead of the Battle/Healer bots we actually want by then.
_extraction_retired = False

# Persistent healer sub-role assignment: bot id -> 'layer1' / 'layer2' / 'general'.
#
# Layer 1 and layer 2 are positions, not just labels -- a layer-2 healer has already
# walked out to sit next to one specific layer-1 parent. Picking roles fresh each tick
# by "Nth-lowest id in this class" breaks the moment a low id gets freed by something
# unrelated and recycled: self-destructing 5 extractors frees ids that are lower than
# the layer-2 healers built earlier (extractors were built before layer 2 in the
# original order), so a plain re-sort shoves 2 brand-new, still-at-spawn healers into
# layer 2 and bumps 2 already-in-position layer-2 healers out to general duty instead --
# a real, reproduced disruption, not a hypothetical one. This dict keeps a healer in
# whatever role it already earned until it dies, and only lets new arrivals fill roles
# that are actually short.
_healer_roles = {}

# The tick the center-rush retreat expires, if nothing re-triggers it before then.
_retreat_until_tick = -1


def do_nothing(state: GameState) -> FleetAction:
    """Issue no orders at all. Only used as the idle sparring partner -- see
    `IDLE_OPPONENT_FOR_TESTING`."""
    return FleetAction.new()


def get_strategy(team: int) -> Strategy:
    """This function tells the engine what strategy you want your bot to use."""

    # team == 0 means I am bottom left
    # team == 1 means I am top right

    if IDLE_OPPONENT_FOR_TESTING and team == 1:
        print("Hello! I am team B (top right) -- IDLE, for testing only")
        return do_nothing

    # Otherwise the same strategy both sides: the engine mirrors the world for team B,
    # so there is nothing for a side to specialize in.
    print(f"Hello! I am team {'A (bottom left)' if team == 0 else 'B (top right)'}")
    return heal_chain_strategy


def _next_build_class(battle_count: int, healer_count: int, extractor_count: int, extraction_retired: bool) -> BotClass:
    """The fabricator build order for this strategy, checked as a priority list: tank
    first, then the first healer layer, then extractors (unless retired -- see
    `heal_chain_strategy`), then the 2 Battle bots that escort them, then the second
    healer layer, then -- once extraction has retired -- the 2 general healers, then
    Battle bots for as long as the fabricator keeps firing."""

    if battle_count < TANK_COUNT:
        return BotClass.Battle
    if healer_count < HEALER_LAYER_1:
        return BotClass.Healer
    if not extraction_retired and extractor_count < EXTRACTOR_COUNT:
        return BotClass.Extractor
    # The deposit escort comes right after the extractors it is meant to arrive with,
    # rather than at the back of the queue behind 9 more healers.
    if not extraction_retired and battle_count < TANK_COUNT + DEPOSIT_GUARD_COUNT:
        return BotClass.Battle
    if healer_count < HEALER_TOTAL:
        return BotClass.Healer
    if extraction_retired and healer_count < HEALER_TOTAL + GENERAL_HEALER_COUNT:
        return BotClass.Healer
    return BotClass.Battle


def _fan_directions(center: Vec2, count: int, radius: float, spacing: float) -> List[Vec2]:
    """`count` unit directions fanned out around `center`, spread wide enough that two
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
        return [center for _ in range(count)]
    step_deg = math.degrees(2.0 * math.asin(min(1.0, spacing / (2.0 * radius))))
    return [center.rotate_deg(step_deg * (i - (count - 1) / 2.0)) for i in range(count)]


def _formation_back(state: GameState) -> Vec2:
    """Which way is "behind" the payload right now, i.e. the direction the healer chain
    should extend in -- the opposite of the payload's current direction of travel along
    its own path, not a fixed world-space heading.

    A fixed heading (say, always toward the map edge) is only "away from the fight" for
    as long as the payload happens to be moving in the one direction that heading
    actually points away from. `conf.payload_path` bends -- team A's push goes right,
    then down, then left, then down again -- so a fixed heading eventually points
    *toward* where the payload is going instead of away from it, and the chain ends up
    on the wrong side once the payload rounds that corner.

    Samples the path's own local tangent instead: `payload_pos` gives the payload's
    position at any capture value, so nudging capture slightly forward and slightly
    back and taking the direction between those two points is the direction of travel
    *right here*, whatever segment of the path this is. The chain faces the opposite of
    that. Falls back to `LEFT` only in the degenerate case of both samples landing on
    the same point (the very ends of the path, where the match is also about to end on
    a payload win anyway).
    """
    ahead = payload_pos(min(1.0, state.capture + CAPTURE_TANGENT_DELTA))
    behind = payload_pos(max(-1.0, state.capture - CAPTURE_TANGENT_DELTA))
    back = (behind - ahead).normalize_or_zero()
    return back if back.norm_sq() > 0.0 else LEFT


def _ring_positions(center: Vec2, count: int, radius: float) -> List[Vec2]:
    """`count` positions evenly spaced around a full circle of `radius` around `center`."""
    if count <= 0:
        return []
    return [center + RIGHT.rotate_deg(360.0 * i / count) * radius for i in range(count)]


def _harvest_positions(conf: GameConfig, deposit_pos: Vec2, count: int, cover_dir: Optional[Vec2] = None) -> List[Vec2]:
    """`count` legal, mine-able spots around `deposit_pos`.

    An extractor's ray only needs `line_of_sight` to the deposit -- unlike the blaster
    and heal ranges, nothing here needs a tight formation -- so this searches outward
    for a spot that actually has that sightline instead of assuming a fixed offset is
    clear. A fixed offset put several extractors behind a wall with no shot at the
    deposit at all.

    With no `cover_dir`, aims each slot at an evenly spaced angle around the deposit so
    they spread out. With one, bunches them all into a tight arc pointing that way
    instead: the deposit is in the blaster's scan mask, so it blocks shots, and putting
    the whole crew in its shadow is what buys them time to keep mining under fire.
    Either way, if the exact spot is blocked it tries nearby angles at growing radii
    until it finds one that actually has the sightline.
    """
    max_dist = conf.bot.base_extract_range - 2.0 * conf.bot.radius
    ring_step = 2.0 * conf.bot.radius + 0.2
    angle_jitters = [0.0, 20.0, -20.0, 40.0, -40.0, 60.0, -60.0, 80.0, -80.0, 100.0, -100.0]
    cover_angle = cover_dir.angle_deg() if cover_dir is not None else None

    positions: List[Vec2] = []
    for i in range(count):
        if cover_angle is None:
            base_angle = 360.0 * i / count
        else:
            base_angle = cover_angle + DEPOSIT_COVER_ARC * (i - (count - 1) / 2.0)
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


def _pick_group_target(anchor: Vec2, enemies, blaster_range: float, claimed: set) -> Optional[BotState]:
    """The nearest enemy in range with a clear shot from `anchor`, skipping anyone in
    `claimed` -- the targets earlier groups this tick already committed to.

    Worth being explicit about why this exists, since it is easy to overstate: hits do
    NOT stack within a tick. The instant a blast connects, its target's
    `invulnerable_until_tick` is set for the rest of that tick's resolution, so every
    other blast landing on the same bot the same tick is simply skipped -- "one blast
    per bot per tick is a rule, not a tuning knob", per the engine's own comment on
    exactly this line. Four bots firing on the same target the same tick does not do
    4x damage; it does the same 3.0 as one bot would.

    What coordinating actually buys: a lone attacker can only refire every
    `blaster_cooldown` (60) ticks, but a target is only invulnerable for
    `base_invulnerability_ticks` (15) after being hit. Four bots locked onto the same
    target, cycling independently, are enough to guarantee *someone's* cooldown is
    ready the instant that 15-tick window closes -- sustaining close to the real
    maximum damage rate (`blaster_damage / base_invulnerability_ticks`) instead of each
    bot's own much slower 1-in-60 cadence. Splitting groups across different targets
    (via `claimed`) spreads that pressure across more enemies instead of every group
    dogpiling whichever one is nearest.
    """
    best = None
    best_dist_sq = math.inf
    max_range_sq = blaster_range * blaster_range
    for enemy in enemies:
        if enemy.id in claimed:
            continue
        dist_sq = anchor.dist_sq(enemy.pos)
        if dist_sq > max_range_sq or not line_of_sight(anchor, enemy.pos):
            continue
        if dist_sq < best_dist_sq:
            best, best_dist_sq = enemy, dist_sq
    return best


def _pick_zone_target(bot_pos: Vec2, enemies, zone_center: Vec2, zone_radius: float, blaster_range: float) -> Optional[BotState]:
    """The nearest enemy inside the payload's capture zone that this bot can actually
    shoot from where it stands.

    The payload is in the blaster's scan mask (allies are not), so it blocks shots --
    an enemy sitting on the far side of it from our formation cannot be hit at all,
    which makes the payload a free shield for anyone contesting the zone. The flankers
    that use this stand off-axis for exactly that reason, so what matters here is not
    just range but the `line_of_sight` check: it is what confirms this particular bot
    has an angle into the shadow rather than a view of the payload's near face."""
    best = None
    best_dist_sq = math.inf
    zone_radius_sq = zone_radius * zone_radius
    blaster_range_sq = blaster_range * blaster_range
    for enemy in enemies:
        if zone_center.dist_sq(enemy.pos) > zone_radius_sq:
            continue
        dist_sq = bot_pos.dist_sq(enemy.pos)
        if dist_sq > blaster_range_sq or not line_of_sight(bot_pos, enemy.pos):
            continue
        if dist_sq < best_dist_sq:
            best, best_dist_sq = enemy, dist_sq
    return best


def _engage_if_possible(bot_action: BotAction, bot_pos: Vec2, enemies, blaster_range: float) -> bool:
    """A single Battle bot's combat behavior: if there is a target in range with a
    clear shot, turn onto it and fire, overriding whatever facing its positional job
    would otherwise want. Used directly by bots not coordinating as a group (the tank,
    the layer-2 guards, the deposit defense force), and as the tank guards' fallback
    when their group's shared target is not actually reachable from their own exact
    position."""
    target = _find_target(bot_pos, enemies, blaster_range)
    if target is None:
        return False
    bot_action.turn_action = turn_towards(target.pos)
    bot_action.special_action = SpecialAction.Battle(fire=True)
    return True


def _assign_healer_roles(healer_bots: List[BotState]):
    """Splits `healer_bots` into (layer1, layer2, general), keeping each healer in
    whatever role it was already assigned (see `_healer_roles`) and only handing out
    roles to bots seen for the first time -- lowest id first, and only into whichever
    bucket still has room, layer 1 before layer 2 before general."""
    global _healer_roles

    alive_ids = {b.id for b in healer_bots}
    for bid in list(_healer_roles):
        if bid not in alive_ids:
            del _healer_roles[bid]

    counts = {"layer1": 0, "layer2": 0, "general": 0}
    for role in _healer_roles.values():
        counts[role] += 1

    # Promote survivors inward to close gaps left by casualties, before considering new
    # builds. Without this a dead layer-1 healer left layer 1 short until a brand-new
    # bot finished walking across the map -- and layer 2 kept every member, so the
    # layer-2-to-parent mapping had more groups than there were parents to hand them
    # to. That was a hard IndexError crash, not a degradation, and it fired exactly
    # when losses started mounting. Promoting also just plays better: an already-placed
    # layer-2 healer is next to the tank in seconds, a fresh build is not.
    for short, donor in (("layer1", "layer2"), ("layer2", "general")):
        target_size = HEALER_LAYER_1 if short == "layer1" else HEALER_LAYER_2
        while counts[short] < target_size and counts[donor] > 0:
            for healer in sorted(healer_bots, key=lambda b: b.id):
                if _healer_roles.get(healer.id) == donor:
                    _healer_roles[healer.id] = short
                    counts[donor] -= 1
                    counts[short] += 1
                    break
            else:
                break

    for healer in sorted(healer_bots, key=lambda b: b.id):
        if healer.id in _healer_roles:
            continue
        if counts["layer1"] < HEALER_LAYER_1:
            role = "layer1"
        elif counts["layer2"] < HEALER_LAYER_2:
            role = "layer2"
        else:
            role = "general"
        _healer_roles[healer.id] = role
        counts[role] += 1

    by_role = {"layer1": [], "layer2": [], "general": []}
    for healer in healer_bots:
        by_role[_healer_roles[healer.id]].append(healer)
    return by_role["layer1"], by_role["layer2"], by_role["general"]


def heal_chain_strategy(state: GameState) -> FleetAction:
    global _extraction_retired, _retreat_until_tick

    conf = get_config()
    action = FleetAction.new()
    payload = state.payload_pos()
    back = _formation_back(state)

    battle_bots = sorted((b for b in state.fleet_me if b.class_ == BotClass.Battle), key=lambda b: b.id)
    healer_bots = sorted((b for b in state.fleet_me if b.class_ == BotClass.Healer), key=lambda b: b.id)
    extractor_bots = [b for b in state.fleet_me if b.class_ == BotClass.Extractor]

    battle_count = len(battle_bots)
    healer_count = len(healer_bots)
    extractor_count = len(extractor_bots)

    tank = battle_bots[0] if battle_bots else None
    layer1, layer2, general_healers = _assign_healer_roles(healer_bots)
    standby_battle = battle_bots[1:]

    # Units should almost never stand close enough to share a blast's splash. A blast
    # damages anything within `base_blaster_splash_radius + bot.radius` of the impact
    # point, so two bots closer than twice that could both be caught by one shot;
    # `SAFE_SPACING` is that threshold, read from config rather than hardcoded.
    SAFE_SPACING = 2.0 * (conf.bot.base_blaster_splash_radius + conf.bot.radius)
    # Stay comfortably inside heal range rather than right at the edge of it, so
    # jostling from collisions or the tank's own movement does not slip a healer out.
    heal_max_dist = conf.bot.base_heal_range - 2.0 * conf.bot.radius
    # How far a guard (or a general healer) stands from whoever it is protecting or
    # healing: outside that ally's own splash zone even in the worst case (the impact
    # point landing on the near edge of the ally's own hull, `conf.bot.radius` closer
    # than its center), plus the guard's own hull needing to clear the blast from
    # there, plus a small safety margin.
    guard_standoff = conf.bot.base_blaster_splash_radius + 2.0 * conf.bot.radius + 0.2
    # The healer chain's own overall reach from the tank, used to decide when an
    # approaching unit has arrived in the chain's neighborhood and should switch from
    # heading for the tank to taking its own exact spot.
    chain_reach = 2.0 * heal_max_dist

    # --- deposit: no abandon logic any more. Extractors hold and mine until they die;
    # the only concession to being shot at is where they stand. Whoever is threatening
    # the deposit sets a cover direction -- the far side of the node from them -- and
    # the crew bunches there, because the deposit is in the blaster's scan mask and so
    # blocks shots outright. Two Battle bots escort them from the start. ---
    deposit_threats = [e for e in state.fleet_other
                        if state.deposit_me.pos.dist_sq(e.pos) <= conf.bot.blaster_range ** 2]
    cover_dir = None
    if deposit_threats:
        centroid = deposit_threats[0].pos
        for enemy in deposit_threats[1:]:
            centroid = centroid + enemy.pos
        centroid = centroid * (1.0 / len(deposit_threats))
        cover_dir = (state.deposit_me.pos - centroid).normalize_or_zero()
        if cover_dir.norm_sq() == 0.0:
            cover_dir = None

    # --- center-rush abort: a big push landing on the formation while healers are
    # already dropping means the chain loses faster than reinforcements can walk in, so
    # give ground rather than feed it. The tank rallies back along the payload path
    # toward our own side; everything else is positioned relative to the tank, so the
    # whole formation withdraws with it. Latched for `RETREAT_HOLD_TICKS` so it is a
    # decision, not a per-tick flinch, and bounded so we always go back to contest. ---
    center_threats = sum(1 for e in state.fleet_other
                         if payload.dist_sq(e.pos) <= conf.bot.blaster_range ** 2)
    hurt_healers = sum(1 for h in healer_bots if h.health < conf.bot.health * 0.5)
    if center_threats >= CENTER_RUSH_THRESHOLD and hurt_healers >= RETREAT_HURT_HEALERS:
        _retreat_until_tick = state.tick + RETREAT_HOLD_TICKS
    retreating = state.tick <= _retreat_until_tick
    # Where the formation forms up: the payload normally, a point further back down the
    # path while withdrawing.
    center = payload_pos(max(-1.0, state.capture - RETREAT_CAPTURE_STEP)) if retreating else payload

    # --- the tank: hold (and, while uncontested, push) the payload ---
    if tank is not None:
        bot_action = action.bots[tank.id]
        bot_action.move_action = move_bot(navigate_to(tank.pos, center))
        bot_action.turn_action = turn_towards(center)
        _engage_if_possible(bot_action, tank.pos, state.fleet_other, conf.bot.blaster_range)

    # --- layer 1: heal the tank, fanned out around it, facing away from the payload's
    # current direction of travel (`back`) rather than a fixed heading -- see
    # `_formation_back`. Chases the tank directly until close, then peels off to its
    # own ray -- see `_approach`. ---
    if tank is not None:
        for healer, direction in zip(layer1, _fan_directions(back, len(layer1), heal_max_dist, SAFE_SPACING)):
            pos = tank.pos + direction * heal_max_dist
            bot_action = action.bots[healer.id]
            bot_action.move_action = move_bot(_approach(healer.pos, tank.pos, pos, heal_max_dist))
            bot_action.turn_action = turn_towards(tank.pos)
            bot_action.special_action = SpecialAction.Healer(fire=True, target=tank.id)

    # --- layer 2: heal layer 1, 3 healers per layer-1 target, each group fanned out
    # around its own parent the same way layer 1 fans around the tank, and in the same
    # `back` direction. Also chases the tank first -- not its own (possibly
    # still-arriving) parent -- since the tank is the one point everyone in the chain
    # is converging on. ---
    # Round-robin across however many layer-1 parents are actually alive rather than
    # slicing into fixed groups of 3 and indexing `layer1` by group number: that
    # indexing assumed layer 1 was always full, and crashed outright the moment it was
    # not. Spreading whatever layer 2 we have over whatever layer 1 we have degrades
    # instead, which is what we want when the formation is already taking losses.
    per_parent: List[List[BotState]] = [[] for _ in layer1]
    for i, healer in enumerate(layer2):
        if not layer1:
            break  # no parents left alive at all -- nothing for layer 2 to attach to
        per_parent[i % len(layer1)].append(healer)
    for parent, group in zip(layer1, per_parent):
        for healer, direction in zip(group, _fan_directions(back, len(group), heal_max_dist, SAFE_SPACING)):
            pos = parent.pos + direction * heal_max_dist
            bot_action = action.bots[healer.id]
            bot_action.move_action = move_bot(_approach(healer.pos, tank.pos if tank is not None else parent.pos, pos, chain_reach))
            bot_action.turn_action = turn_towards(parent.pos)
            bot_action.special_action = SpecialAction.Healer(fire=True, target=parent.id)

    # --- extractors: retire once there is nowhere left for the tokens to go; flee
    # instead of mining while the deposit is contested (any threat at all, small raid
    # or heavy assault alike -- the defense force above is what's supposed to clear a
    # small one, not the extractors themselves); otherwise mine wherever around the
    # deposit actually has a sightline. ---
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
        # Mine until dead. No fleeing: running away just meant dying tired, and the
        # rebuild-into-the-grinder loop was worse than holding. `cover_dir` puts the
        # crew in the deposit's shot shadow when there is someone to hide from.
        positions = _harvest_positions(conf, state.deposit_me.pos, extractor_count, cover_dir)
        for extractor, pos in zip(extractor_bots, positions):
            bot_action = action.bots[extractor.id]
            bot_action.move_action = move_bot(_approach(extractor.pos, state.deposit_me.pos, pos, conf.bot.base_extract_range))
            bot_action.turn_action = turn_towards(state.deposit_me.pos)
            bot_action.special_action = SpecialAction.Extractor(mine=True)

    # --- general healers: once extraction has retired, roam to whichever standby
    # Battle bot -- tank guard or layer-2 guard -- is hurt worst. Nothing else heals
    # them, since the tank-heal-tree only covers the tank and its own two layers. ---
    if general_healers and standby_battle:
        injured_order = sorted(standby_battle, key=lambda b: b.health)
        groups = {}
        for i, healer in enumerate(general_healers):
            target = injured_order[i % len(injured_order)]
            groups.setdefault(target.id, (target, []))[1].append(healer)
        for target, healers_here in groups.values():
            for healer, direction in zip(healers_here, _fan_directions(back, len(healers_here), heal_max_dist, SAFE_SPACING)):
                pos = target.pos + direction * heal_max_dist
                bot_action = action.bots[healer.id]
                bot_action.move_action = move_bot(_approach(healer.pos, tank.pos if tank is not None else target.pos, pos, chain_reach))
                bot_action.turn_action = turn_towards(target.pos)
                bot_action.special_action = SpecialAction.Healer(fire=True, target=target.id)

    # --- standby Battle bots: bodyguards, not a wide perimeter. Layer 1 gets kept
    # alive by layer 2's healing, but nothing heals layer 2 -- so guards split between
    # the tank (close range, catches anything that gets in melee-close to threaten the
    # tank and the nearby layer-1 healers) and layer 2 specifically (the chain's actual
    # unguarded soft spot). The tank guard ring is a full circle -- it has no "wrong
    # side" to fall onto the way a one-sided fan would, so it does not need `back` --
    # but the rear guards orbit wherever their layer-2 healer actually is, so they
    # follow the same `back`-oriented repositioning automatically. Both groups chase
    # the tank first while still far out, same as the healers, so the whole formation
    # can just walk together when the tank relocates with the payload. ---
    if tank is not None and standby_battle:
        # The escort is first in the queue so it is built alongside the extractors and
        # travels out with them, rather than arriving after the deposit is already lost.
        deposit_guards = standby_battle[:DEPOSIT_GUARD_COUNT]
        rest = standby_battle[DEPOSIT_GUARD_COUNT:]
        tank_guards = rest[:TANK_GUARD_COUNT]
        flankers = rest[TANK_GUARD_COUNT:TANK_GUARD_COUNT + PAYLOAD_FLANKER_COUNT]
        rear_guards = rest[TANK_GUARD_COUNT + PAYLOAD_FLANKER_COUNT:]

        # Deposit escort: hold station on the deposit and shoot whatever comes for the
        # extractors. Posted on the threatened side, between the raiders and the crew
        # hiding behind the node.
        guard_face = cover_dir * -1.0 if cover_dir is not None else back
        for bot, pos in zip(deposit_guards,
                            _ring_positions(state.deposit_me.pos + guard_face * (conf.deposit.radius + guard_standoff),
                                            len(deposit_guards), guard_standoff)):
            bot_action = action.bots[bot.id]
            bot_action.move_action = move_bot(_approach(bot.pos, state.deposit_me.pos, pos, conf.bot.base_extract_range))
            _engage_if_possible(bot_action, bot.pos, state.fleet_other, conf.bot.blaster_range)

        # --- payload flankers: the payload blocks blaster fire (it is in the scan mask,
        # unlike allies), so an enemy standing on the far side of it from our formation
        # is untouchable -- the payload works as a free shield for anyone contesting the
        # zone. The tank and its guards are all bunched on one side, so that shadow is
        # wide. These bots post off-axis instead -- directly opposite the formation
        # first, then the two flanks -- just outside the capture radius, so between them
        # every angle into the zone is covered by somebody. They shoot into the zone by
        # preference and fall back to normal engagement when it is empty. ---
        flank_radius = conf.payload.capture_radius + conf.bot.radius
        for bot, angle in zip(flankers, FLANKER_ANGLES):
            post = center + back.rotate_deg(angle) * flank_radius
            bot_action = action.bots[bot.id]
            bot_action.move_action = move_bot(_approach(bot.pos, center, post, flank_radius * 2.0))
            zone_target = _pick_zone_target(bot.pos, state.fleet_other, payload,
                                            conf.payload.capture_radius, conf.bot.blaster_range)
            if zone_target is not None:
                bot_action.turn_action = turn_towards(zone_target.pos)
                bot_action.special_action = SpecialAction.Battle(fire=True)
            else:
                _engage_if_possible(bot_action, bot.pos, state.fleet_other, conf.bot.blaster_range)

        tank_guard_positions = _ring_positions(tank.pos, len(tank_guards), guard_standoff)
        for bot, pos in zip(tank_guards, tank_guard_positions):
            bot_action = action.bots[bot.id]
            bot_action.move_action = move_bot(_approach(bot.pos, tank.pos, pos, guard_standoff * 2.0))

        # Tank guards focus fire in groups of up to `FOCUS_GROUP_SIZE`, one shared
        # target per group -- see `_pick_group_target` for exactly what this does and
        # does not buy (it is not simultaneous burst damage). A guard without its own
        # clear shot at the group's target falls back to its own nearest enemy instead
        # of sitting idle.
        claimed_targets = set()
        for i in range(0, len(tank_guards), FOCUS_GROUP_SIZE):
            group = tank_guards[i:i + FOCUS_GROUP_SIZE]
            target = _pick_group_target(group[0].pos, state.fleet_other, conf.bot.blaster_range, claimed_targets)
            if target is not None:
                claimed_targets.add(target.id)
            for bot in group:
                bot_action = action.bots[bot.id]
                if (target is not None
                        and bot.pos.dist_sq(target.pos) <= conf.bot.blaster_range ** 2
                        and line_of_sight(bot.pos, target.pos)):
                    bot_action.turn_action = turn_towards(target.pos)
                    bot_action.special_action = SpecialAction.Battle(fire=True)
                else:
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
