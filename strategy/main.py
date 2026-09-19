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

    # The deposit is a solid disc, so standing dead-center is not the mining spot. This
    # is the closest legal spot on our own edge of the ring: hull to hull with it, `+y`
    # being the side away from the map center on our half.
    mining_spot = state.deposit_me.pos + Vec2(0.0, conf.deposit.radius + conf.bot.radius)

    # --- the tank: hold (and, while uncontested, push) the payload ---
    if tank is not None:
        bot_action = action.bots[tank.id]
        bot_action.move_action = move_bot(navigate_to(tank.pos, payload))
        bot_action.turn_action = turn_towards(payload)

    # --- layer 1: heal the tank ---
    if tank is not None:
        for healer in layer1:
            bot_action = action.bots[healer.id]
            bot_action.move_action = move_bot(navigate_to(healer.pos, tank.pos))
            bot_action.turn_action = turn_towards(tank.pos)
            bot_action.special_action = SpecialAction.Healer(fire=True, target=tank.id)

    # --- layer 2: heal layer 1, 3 healers per layer-1 target ---
    for i, healer in enumerate(layer2):
        target = layer1[i // 3]
        bot_action = action.bots[healer.id]
        bot_action.move_action = move_bot(navigate_to(healer.pos, target.pos))
        bot_action.turn_action = turn_towards(target.pos)
        bot_action.special_action = SpecialAction.Healer(fire=True, target=target.id)

    # --- extractors: mine our own deposit ---
    for extractor in extractor_bots:
        bot_action = action.bots[extractor.id]
        bot_action.move_action = move_bot(navigate_to(extractor.pos, mining_spot))
        bot_action.turn_action = turn_towards(state.deposit_me.pos)
        bot_action.special_action = SpecialAction.Extractor(mine=True)

    # --- standby Battle bots: no orders yet, they just sit at their spawn point ---
    # TODO: give these something to do once the chain above is stable -- a rally point,
    # a counter-push trigger, or defense for the healer chain.

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
