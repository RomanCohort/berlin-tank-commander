import random
from 柏林1945_虎王车长_系统版 import build_state, GameState, maybe_meet_friendly_infantry, maybe_meet_friendly_tank, _consume_fuel

class DummyInput:
    def __init__(self):
        pass
    def read(self, *args, **kwargs):
        return ''


def single_sim(seed:int=0):
    rng = random.Random(seed)
    s = build_state(DummyInput(), name="Sim", callsign="SIM", diff="2", rng=rng, breakout_mode=False)
    stats = {
        'initial_tank_allies': len(getattr(s, 'tank_allies', [])),
        'initial_deployed_garrison': len(getattr(s, 'deployed_garrison', [])),
        'moves': 0,
        'meet_infantry': 0,
        'meet_tank': 0,
        'fuel_consumed': 0,
    }
    # 执行最多 5 次移动，记录触发
    for i in range(5):
        stats['moves'] += 1
        # 模拟移动到同一区域（不改变 location），直接调用触发函数
        if maybe_meet_friendly_infantry(s, reason='move'):
            stats['meet_infantry'] += 1
        if maybe_meet_friendly_tank(s, reason='move'):
            stats['meet_tank'] += 1
        # 模拟消耗：使用基地 move_cost 10
        consumed = _consume_fuel(s, 10, vehicle_model='虎式坦克', terrain=None, vehicles=1)
        stats['fuel_consumed'] += consumed
    return stats


def run_batch(n=10):
    out = []
    for i in range(n):
        out.append(single_sim(seed=i+1))
    # aggregate
    agg = {'initial_tank_allies':0,'initial_deployed_garrison':0,'moves':0,'meet_infantry':0,'meet_tank':0,'fuel_consumed':0}
    for s in out:
        for k in agg:
            agg[k]+=s[k]
    return out, agg

if __name__=='__main__':
    out, agg = run_batch(10)
    print('Per-run:')
    for i,s in enumerate(out,1):
        print(i, s)
    print('\nAggregate over 10 runs:')
    print(agg)
