import importlib, importlib.util, sys, random, os

# Load the main game module by file path to avoid encoding/module-name issues
GAME_PATH = os.path.join(r"c:\Users\LENOVO\Desktop\新建文件夹 (3)", "柏林1945_虎王车长_系统版.py")
spec = importlib.util.spec_from_file_location("game_module", GAME_PATH)
game = importlib.util.module_from_spec(spec)
sys.modules["game_module"] = game
spec.loader.exec_module(game)

# For batch simulation, override interactive chooser to pick defaults/first option
def _auto_choose(ins, prompt, options, *, default=None, allow_quit=True, allow_restart=True):
    try:
        if default is not None and default in options:
            return default
        keys = list(options.keys())
        return keys[0] if keys else ""
    except Exception:
        return ""

game.choose = _auto_choose

# We will run deterministic-ish simulations by seeding per-run
def run_once(seed, moves_per_run=20):
    random.seed(seed)
    ins = game.InputStream(scripted=None, default_when_empty=True)
    rng = random.Random(seed)
    try:
        s = game.build_state(ins, name="AI", callsign="AI-1", diff="2", rng=rng)
    except TypeError:
        # fallback if signature differs
        s = game.build_state(ins, "AI", "AI-1", "2", rng)
    stats = {
        'fuel_start': getattr(s, 'fuel', 0),
        'fuel_end': None,
        'fuel_consumed': 0,
        'meet_infantry': 0,
        'meet_tank': 0,
        'ally_scavenge_triggers': 0,
        'support_used': 0,
    }
    # small wrapper helpers if module exposes functions
    for mv in range(moves_per_run):
            # choose a neighbor to move to (inject into InputStream) and call action_move
            try:
                meta = getattr(game, 'MAP_META', {}).get(s.location_key, {})
                neigh = meta.get('adj') if isinstance(meta.get('adj'), list) else None
                if not neigh:
                    # fallback to any other location
                    neigh = [k for k in getattr(game, 'LOCATIONS', {}).keys() if k != s.location_key]
                if not neigh:
                    # nothing to move to: consume small fuel and continue
                    try:
                        game._consume_fuel(s, 1, vehicle_model='虎式坦克')
                    except Exception:
                        pass
                else:
                    # programmatic move: choose neighbor, compute cost, consume fuel, update state
                    target = s.rng.choice(neigh)
                    # compute base cost from MAP_META
                    base_cost = int(getattr(game, 'MAP_META', {}).get(target, {}).get('move_cost', 8) or 8)
                    fatigue = int(s.counters.get('fatigue', 0) or 0)
                    if fatigue >= 60:
                        base_cost += 1
                    try:
                        eff = game.weather_effects(s)
                        mult = float(eff.get('move_mult', 1.0))
                        base_cost = int(max(1, round(base_cost * mult)))
                    except Exception:
                        pass
                    MOVE_FUEL_MULT = 3
                    cost = int(max(1, int(base_cost) * MOVE_FUEL_MULT))
                    if int(getattr(s, 'fuel', 0)) >= int(cost):
                        s.location_key = target
                        s.explored.add(target)
                        s.counters['explore'] = s.counters.get('explore', 0) + 1
                        s.moves_this_round += 1
                        s.action_points -= 1
                        terrain_now = getattr(game, 'MAP_META', {}).get(target, {}).get('terrain')
                        consumed = game._consume_fuel(s, cost, vehicle_model='虎式坦克', terrain=terrain_now, vehicles=1)
                        s.damage += 2
                        # friendly encounters and ally scavenge
                        try:
                            game.maybe_meet_friendly_infantry(s, reason='move')
                        except Exception:
                            pass
                        try:
                            game.maybe_meet_friendly_tank(s, reason='move')
                        except Exception:
                            pass
                        # If an encounter happened due to scavenge, maybe_trigger_event would have
                        # marked s._last_encounter_reason and resolve_encounter may have already
                        # triggered a post-encounter reward; nothing extra needed here.
                        # ally scavenging
                        try:
                            rnd = s.rng
                            for t in list(getattr(s, 'tank_allies', [])):
                                if not getattr(t, 'alive', False):
                                    continue
                                last = int(getattr(t, 'last_scavenge_round', 0) or 0)
                                if last == int(getattr(s, 'round_number', 0) or 0):
                                    continue
                                if int(getattr(t, 'morale', 0) or 0) >= getattr(game, 'AUTO_RETAIN_SCAVENGE_MORALE', 65) and rnd.random() < getattr(game, 'ALLY_MOVE_SCAVENGE_P', 0.14):
                                    try:
                                        game.event_reward_supply(s)
                                    except Exception:
                                        pass
                                    setattr(t, 'last_scavenge_round', int(getattr(s, 'round_number', 0) or 0))
                                    s.clamp()
                            for u in list(getattr(s, 'allies', [])):
                                if not getattr(u, 'alive', False):
                                    continue
                                last = int(getattr(u, 'last_scavenge_round', 0) or 0)
                                if last == int(getattr(s, 'round_number', 0) or 0):
                                    continue
                                if int(getattr(u, 'morale', 0) or 0) >= getattr(game, 'AUTO_RETAIN_SCAVENGE_MORALE', 65) and rnd.random() < getattr(game, 'INFANTRY_MOVE_SCAVENGE_P', 0.06):
                                    try:
                                        game.event_reward_supply(s)
                                    except Exception:
                                        pass
                                    setattr(u, 'last_scavenge_round', int(getattr(s, 'round_number', 0) or 0))
                                    s.clamp()
                        except Exception:
                            pass
            except Exception:
                try:
                    game._consume_fuel(s, 1, vehicle_model='虎式坦克')
                except Exception:
                    pass
        # try to detect encounters via counters or flags
            # detect new deployed garrison or tank allies as encounters
            try:
                dg = getattr(s, 'deployed_garrison', [])
                stats['meet_infantry'] += max(0, len(dg) - stats.get('_prev_deployed_garrison_len', 0))
                stats['_prev_deployed_garrison_len'] = len(dg)
            except Exception:
                pass
            try:
                ta = getattr(s, 'tank_allies', [])
                stats['meet_tank'] += max(0, len(ta) - stats.get('_prev_tank_allies_len', 0))
                stats['_prev_tank_allies_len'] = len(ta)
            except Exception:
                pass
            # detect scavenge attempts by checking last_scavenge_round on allies
            try:
                round_num = int(getattr(s, 'round_number', 0) or 0)
                scavenge_count = 0
                for t in list(getattr(s, 'tank_allies', [])) + list(getattr(s, 'allies', [])):
                    if int(getattr(t, 'last_scavenge_round', -1) or -1) == round_num:
                        scavenge_count += 1
                stats['ally_scavenge_triggers'] += scavenge_count
            except Exception:
                pass
            # detect support usage by comparing support_battles_left against initial assumed max (10)
            try:
                used = 0
                for t in getattr(s, 'tank_allies', []):
                    if int(getattr(t, 'support_battles_left', 0) or 0) < int(getattr(t, '_initial_support_battles_left', 10) or 10):
                        used += 1
                stats['support_used'] += used
            except Exception:
                pass
    stats['fuel_end'] = getattr(s, 'fuel', 0)
    stats['fuel_consumed'] = stats['fuel_start'] - stats['fuel_end']
    return stats

if __name__ == '__main__':
    RUNS = 100
    moves_per_run = 20
    agg = {'fuel_consumed':0,'meet_infantry':0,'meet_tank':0,'ally_scavenge_triggers':0,'support_used':0}
    for i in range(RUNS):
        st = run_once(1000 + i, moves_per_run=moves_per_run)
        for k in agg:
            agg[k] += st.get(k,0)
    print(f"Runs: {RUNS}, moves_per_run: {moves_per_run}")
    print({k: agg[k] for k in agg})
    print('Per-run averages:')
    for k in agg:
        print(f"  {k}: {agg[k]/RUNS}")
