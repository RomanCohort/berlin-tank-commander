# -*- coding: utf-8 -*-
"""快速测试/估算：当前 IS-2 数值下，玩家AP在“命中后”的贯穿率与击毁所需发数。

说明：这里不跑整套交互战斗，只复刻脚本里的关键公式做 Monte Carlo。
"""

from __future__ import annotations

import math
import random
from statistics import mean

# === 从当前脚本配置复制的关键数值（如你继续改脚本，这里也要同步） ===
IS2_HP = 320
IS2_ARMOR = 75
IS2_SLOPE_DEG = 32.0

# 玩家 AP 穿深（与主脚本 `_player_ap_penetration` 同步）
PLAYER_AP_PEN = {"close": 140.0, "medium": 125.0, "long": 110.0}
# 缩窄随机散布以提高远距一致性（与主脚本一致）
PEN_RAND = (0.94, 1.06)

# 玩家 AP 贯穿伤害
PLAYER_AP_DAMAGE_PEN_RANGE = (280, 460)
PLAYER_AP_DAMAGE_PEN_HEAVY_MULT = 0.95

# 重装甲致命贯穿（ratio>=1.30）概率
HEAVY_LETHAL_PROB = 0.70
LETHAL_RATIO = 1.30

# 命中面概率（默认无机动/无压制/无炮塔故障时）
ASPECT_W = [("front", 0.65), ("side", 0.25), ("rear", 0.10)]


def roll_aspect(rng: random.Random) -> str:
    r = rng.random()
    acc = 0.0
    for k, w in ASPECT_W:
        acc += w
        if r <= acc:
            return k
    return "front"


def effective_armor(*, base: float, slope_deg: float, aspect: str, rng: random.Random) -> float:
    # 与脚本一致：侧后装甲倾角更小
    if aspect in ("side", "rear"):
        slope_deg = min(18.0, slope_deg * 0.40)

    impact_deg = slope_deg + rng.uniform(-10.0, 18.0)
    impact_deg = max(0.0, min(75.0, impact_deg))

    denom = max(0.26, math.cos(math.radians(impact_deg)))
    return base / denom


def sample_once(range_tag: str, *, rng: random.Random) -> tuple[int, bool, float]:
    """返回：(消耗发数, 是否出现致命贯穿一击, 贯穿率(0/1))  — 只统计命中后的效果。"""

    hp = IS2_HP
    shots = 0
    lethal_happened = False
    penetrations = 0

    # 装甲剖面（与脚本一致的 IS-2 定义）
    base_front = float(IS2_ARMOR)
    base_side = base_front * 0.80
    base_rear = base_front * 0.65

    while hp > 0 and shots < 30:
        shots += 1

        aspect = roll_aspect(rng)
        base = base_front if aspect == "front" else base_side if aspect == "side" else base_rear
        eff = effective_armor(base=base, slope_deg=IS2_SLOPE_DEG, aspect=aspect, rng=rng)

        pen = PLAYER_AP_PEN[range_tag] * rng.uniform(*PEN_RAND)

        if pen < eff:
            # 未贯穿：震击伤害（脚本为 10~20）
            hp -= rng.randint(10, 20)
            continue

        penetrations += 1
        ratio = pen / max(1.0, eff)

        # 致命贯穿
        if ratio >= LETHAL_RATIO and rng.random() < HEAVY_LETHAL_PROB:
            lethal_happened = True
            hp = 0
            break

        dmg = rng.randint(*PLAYER_AP_DAMAGE_PEN_RANGE)
        dmg = max(80, int(dmg * PLAYER_AP_DAMAGE_PEN_HEAVY_MULT))
        hp -= dmg

    pen_rate = 1.0 if penetrations > 0 else 0.0
    return shots, lethal_happened, pen_rate


def quantile(sorted_vals: list[int], q: float) -> int:
    if not sorted_vals:
        return 0
    if q <= 0:
        return sorted_vals[0]
    if q >= 1:
        return sorted_vals[-1]
    idx = int(round((len(sorted_vals) - 1) * q))
    return sorted_vals[max(0, min(len(sorted_vals) - 1, idx))]


def run(trials: int = 20000, seed: int = 12345) -> None:
    rng = random.Random(seed)

    for range_tag in ("close", "medium", "long"):
        shots_list: list[int] = []
        lethal_cnt = 0
        pen_any_cnt = 0

        for _ in range(trials):
            shots, lethal, pen_any = sample_once(range_tag, rng=rng)
            shots_list.append(shots)
            lethal_cnt += 1 if lethal else 0
            pen_any_cnt += 1 if pen_any else 0

        shots_list.sort()
        print(f"\n=== IS-2 测试（range={range_tag}，trials={trials}）===")
        print(f"命中后：至少发生一次贯穿比例：{pen_any_cnt / trials:.1%}")
        print(f"命中后：触发致命贯穿比例：{lethal_cnt / trials:.1%}")
        print(f"击毁所需发数(命中后)：均值 {mean(shots_list):.2f} | P50 {quantile(shots_list, 0.50)} | P90 {quantile(shots_list, 0.90)}")


if __name__ == "__main__":
    run()
