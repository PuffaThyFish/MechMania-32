"""Everything a strategy needs, in one import.

`strategy/main.py` does `from . import *` and gets all of this. The wire types come from
`core._generated`, which `mm-cli run` writes from the engine's own layout registry -- so
if your editor cannot resolve them, run `mm-cli run` once.

The helpers below are thin wrappers over the engine's own code, reached through a C ABI --
so the behaviour you get is the behaviour the referee gets. `core/channel.py` has the full
docstring for every one of them; the one-line notes here are just an index.
"""

from typing import List, Optional

from core.channel import (
    Budget,
    EngineChannel,
    EngineError,
    Strategy,
    corridor_clear,
    diff_degrees,
    disc_free,
    get_budget,
    get_config,
    line_of_sight,
    move_bot,
    navigate_to,
    normalize_degrees,
    path_length,
    payload_pos,
    point_free,
    point_seg_dist,
    route_waypoints,
    turn_to_angle,
    turn_towards,
)
from core._generated.bindings import (
    BOTS_MAX,
    MAP_SIZE,
    PAYLOAD_PATH_LEN,
    BotAction,
    BotArray,
    BotClass,
    BotConfig,
    BotState,
    Deposit,
    FabricatorState,
    FleetAction,
    GameConfig,
    GameState,
    MapTile,
    MoveAction,
    SpecialAction,
    SpecialState,
    Team,
    TurnAction,
    Vec2,
)

__all__ = [
    # -------------------------------------------------------------------------------
    # the world
    # -------------------------------------------------------------------------------
    # `GameState`: tick, capture, fleet_me/fleet_other, deposit_me/deposit_other,
    # fabricator_me/fabricator_other, and `state.payload_pos()`.
    "GameState",
    # The fixed rules of the match, from `get_config()`: `conf.bot`, `conf.payload`,
    # `conf.deposit`, `conf.fabricator`, `conf.max_ticks`, `conf.endgame_ticks`,
    # `conf.payload_path`, `conf.map`.
    "GameConfig",
    # One bot: id, health, pos, vel, angle, turn_vel, invulnerable_until_tick, and the
    # properties `class_`, `next_fire_tick`, `shot`, `healing`, `extracting`.
    "BotState",
    # `fleet_me` / `fleet_other`. Iterating skips dead slots; also `.get(id)`, `.ids()`,
    # `.is_full()`, `len()` and `[id]`.
    "BotArray",
    # Battle / Healer / Extractor.
    "BotClass",
    # `conf.bot`: speed, health, turn_speed, blaster_range/damage/cooldown, heal_per_tick,
    # extract_rate, and the fixed `base_*` stats.
    "BotConfig",
    # `state.deposit_me`: pos, and `extractors` (a per-team bitmask of who holds a slot).
    "Deposit",
    # `state.fabricator_me`: tokens, and next_bot_creation (an absolute tick).
    "FabricatorState",
    # `conf.map` tiles: Empty or Wall.
    "MapTile",
    # Team.Me / Team.Other, and `.other_team()`. Indexes `state.fleets()` and friends.
    "Team",
    # new, dist, dist_sq, norm, norm_sq, dot, normalize_or_zero, rotate_deg/rotate_rad,
    # angle_deg/angle_rad, from_angle_deg/from_angle_rad, and + - * / operators.
    "Vec2",
    # -------------------------------------------------------------------------------
    # what you give back
    # -------------------------------------------------------------------------------
    # `FleetAction.new()`, then `action.bots[id]`, plus fleet-wide `fabricator_next`
    # (which class to build next) and `rush_order` (pay `conf.fabricator.rush_cost` now).
    "FleetAction",
    # One bot's orders: move_action, turn_action, special_action, self_destruct.
    "BotAction",
    # A direction; anything longer than 1 is normalized for you.
    "MoveAction",
    # Direction(power) / TargetRotation(deg) / TargetPosition(pos).
    "TurnAction",
    # Battle(fire) / Healer(fire, target) / Extractor(mine) -- this is what decides the
    # class a bot acts as this tick.
    "SpecialAction",
    "SpecialState",
    # What `get_strategy` hands back: (GameState) -> FleetAction.
    "Strategy",
    # The three constructors, so you do not have to spell the variants out.
    "move_bot",
    "turn_to_angle",
    "turn_towards",
    # -------------------------------------------------------------------------------
    # what you can ask the engine
    # -------------------------------------------------------------------------------
    # The match rules. Fixed for the whole match, available from the first tick.
    "get_config",
    # One tick's move delta around walls. A step, not a plan: call it every tick and it
    # re-routes itself. Never fails.
    "navigate_to",
    # Walking distance around walls; None when there is no route.
    "path_length",
    # The corners of that route, both endpoints excluded. Allocates.
    "route_waypoints",
    # Could a bot (with its radius) walk the straight line a -> b?
    "corridor_clear",
    # Zero-radius sightline: what the blaster and the extractor ray actually check.
    "line_of_sight",
    # Is a disc of this radius clear of walls?
    "disc_free",
    # Can a bot stand centred here?
    "point_free",
    # Distance from a point to a segment.
    "point_seg_dist",
    # Wrap an angle into 0..360.
    "normalize_degrees",
    # The signed shortest rotation between two headings -- what you want for aiming.
    "diff_degrees",
    # Where the payload sits at an arbitrary capture value; `state.payload_pos()` is this
    # at the current tick.
    "payload_pos",
    # -------------------------------------------------------------------------------
    # your compute budget
    # -------------------------------------------------------------------------------
    # What you have left to spend, in engine ticks: gate an expensive search on
    # `get_budget().remaining` rather than being sat out for the ticks an overspend costs.
    # `remaining` can go negative -- an overspend is a debt, repaid in ticks you do not get
    # to act on.
    "get_budget",
    "Budget",
    # -------------------------------------------------------------------------------
    # the channel itself -- `__main__.py` drives this, a strategy does not touch it
    # -------------------------------------------------------------------------------
    "EngineChannel",
    "EngineError",
    # -------------------------------------------------------------------------------
    # constants
    # -------------------------------------------------------------------------------
    "BOTS_MAX",
    "MAP_SIZE",
    "PAYLOAD_PATH_LEN",
    # -------------------------------------------------------------------------------
    # stdlib, so a strategy can annotate without its own imports
    # -------------------------------------------------------------------------------
    "List",
    "Optional",
]

# A few things the Rust starterpack has that do not cross the C ABI, so they have no Python
# equivalent: `GameState::in_endgame(conf)` (spell it out as
# `state.tick >= conf.max_ticks - conf.endgame_ticks`), `GameState::health_pool(team)`,
# `GameConfig::is_wall(x, y)` (read `conf.map[x][y]` instead), `mirror_pos`/`mirror_vel`,
# and the raw navigation graph `topology()`. Everything a strategy actually needs is above.
