from . import *


def get_strategy(team: int) -> Strategy:
    """This function tells the engine what strategy you want your bot to use."""

    # team == 0 means I am bottom left
    # team == 1 means I am top right

    if team == 0:
        print("Hello! I am team A (on the bottom left)")
        return basic_strategy
    else:
        print("Hello! I am team B (on the top right)")
        return do_nothing

    # NOTE when actually submitting your bot, you probably want to have the SAME strategy
    # for both sides: the engine mirrors the world for the top-right team, so there is
    # nothing for a side to specialise in.

def do_nothing(state: GameState) -> FleetAction:
    """The smallest strategy there is: issue no orders at all."""
    return FleetAction.new()

def basic_strategy(state: GameState) -> FleetAction:
    """Assign one bot to extract from our deposit, one bot to hold the payload, and send
    every remaining bot after the nearest enemy."""

    # NOTE `get_config()` is the whole rulebook for this match -- bot stats, payload
    # speed, deposit layout, fabricator prices, the map. It is fixed for the match and
    # available from the first tick, so read it instead of hardcoding numbers: the values
    # below are tuned between seasons and your bot picks up the change for free.
    conf = get_config()

    # NOTE Do not worry about what side your bot is on!
    # The engine mirrors the world for you if you are on top,
    # so to you, you are always on the bottom left. Your fleet is always `fleet_me`.

    action = FleetAction.new()

    payload = state.payload_pos()

    # make a battle bot by default
    next_bot = BotClass.Battle

    # `next_bot_creation: 0` means both fleets' very first build is always a Extractor
    # (the engine's own default), and that first bot always lands in slot 0 -- so bot id 0
    # missing means our extractor died and the fabricator should replace it before anything
    # else.
    if not state.fleet_me.get(0):
        next_bot = BotClass.Extractor

    # The deposit is a solid disc, so standing dead-center is not the mining spot. This is
    # the closest legal spot on our own edge of the ring: hull to hull with it, `+y` being
    # the side away from the map center on our half.
    #
    # You do not actually have to hug the ring -- an extractor mines anything within
    # `conf.bot.base_extract_range` that it has a sightline to (`line_of_sight`), and only
    # walls block that ray, not bots. Standing back is safer.
    mining_spot = state.deposit_me.pos + Vec2(0.0, conf.deposit.radius + conf.bot.radius)

    assigned_contester = False

    for bot in state.fleet_me:

        # `fleet_me` iterates the bots you actually have -- dead slots are skipped, so there
        # is no mask to check and no empty slot to guard against. `FleetAction.bots` is
        # indexed by bot id, and a bot's id is its slot.
        bot_action = action.bots[bot.id]

        if bot.class_ == BotClass.Extractor:
            bot_action.move_action = move_bot(navigate_to(bot.pos, mining_spot))
            bot_action.turn_action = turn_towards(state.deposit_me.pos)
            bot_action.special_action = SpecialAction.Extractor(mine=True)
            continue

        if not assigned_contester:
            # NOTE You do not have to write a pathfinder. `navigate_to` walks around walls
            # for you, using a map of the arena the engine works out before the match
            # starts. Call it every tick with where the bot is now -- it is one step, not a
            # plan, so it re-routes by itself as things move.
            #
            # `payload - bot.pos` would walk straight at the point and grind into the first
            # wall in the way.
            bot_action.move_action = move_bot(navigate_to(bot.pos, payload))
            assigned_contester = True
            continue

        # find the closest enemy
        closest_enemy = None
        for enemy in state.fleet_other:
            if closest_enemy is None or bot.pos.dist_sq(enemy.pos) < bot.pos.dist_sq(closest_enemy):
                closest_enemy = enemy.pos

        if closest_enemy is None:
            break
        bot_action.move_action = move_bot(navigate_to(bot.pos, closest_enemy))
        bot_action.turn_action = turn_towards(closest_enemy)

        # Only pull the trigger when the shot can actually land: in range, and with a wall
        # free sightline. A shot puts the blaster on `conf.bot.blaster_cooldown` ticks
        # whether or not it hits anything, so firing at a wall costs you the next real one.
        in_range = (bot.pos.dist(closest_enemy) <= conf.bot.blaster_range
                    and line_of_sight(bot.pos, closest_enemy))
        bot_action.special_action = SpecialAction.Battle(fire=in_range)

    action.fabricator_next = int(next_bot)

    # Rush orders are the only thing tokens buy. Ask for one when we can actually pay
    # `conf.fabricator.rush_cost`, and not once the endgame has started -- no bot is built
    # in the last `conf.endgame_ticks` of the match, so the tokens would just sit there.
    #
    # Rust's `GameState::in_endgame(conf)` has no Python binding, so spell the phase out.
    in_endgame = state.tick >= conf.max_ticks - conf.endgame_ticks
    action.rush_order = (not in_endgame
                         and state.fabricator_me.tokens >= conf.fabricator.rush_cost)

    return action
