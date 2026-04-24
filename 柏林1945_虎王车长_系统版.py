# -*- coding: utf-8 -*-
# 文字冒险游戏（系统版）- 1945年柏林市区：虎王坦克车长
# 说明：历史背景下的虚构个人经历；聚焦求生与选择，不美化侵略战争与暴行。

from __future__ import annotations

import os
import math
import random
import time
import traceback
import shutil
import sys
import pickle
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

# --- 单次时间判定事件（通用）
import json
from datetime import datetime

# 运行版本戳：用于确认当前运行的是哪一次修改后的脚本/打包物
BUILD_STAMP = "2026-01-02"

# 全局燃油/挽留配置
# 每次基础燃油消耗会乘以此系数（可临时通过状态/增益调整）
FUEL_CONSUMPTION_MULT = 0.90
# 连续参战次数阈值：达到此值后触发挽留判定
BATTLES_BEFORE_RETAIN = 4
# 若友军士气高于此阈值，则可额外坚持一次而无需消耗补给
RETENTION_MORALE_AUTO_KEEP = 75
# 当友军士气高但玩家无法立即补给时，友军尝试自主搜集补给的士气阈值
AUTO_RETAIN_SCAVENGE_MORALE = 65
# 友军自主搜集补给的最大尝试次数（移除上限：置为 None 表示不再使用固定上限）
MAX_ALLY_SCAVENGE_TRIES = None

# 友军在平时移动/行动时自主搜刮的参数
ALLY_MOVE_SCAVENGE_P = 0.22
INFANTRY_MOVE_SCAVENGE_P = 0.12

# 按车型与地形的历史化燃油消耗倍率（相对基准）
# 这些倍率用于把游戏中的“基础消耗”映射为更接近史实的消耗差异。
MODEL_FUEL_MULTIPLIER = {
    "虎式坦克": 1.8,
    "虎王坦克": 1.9,
    "豹式坦克": 1.25,
    "黑豹坦克": 1.25,
    "四号坦克": 0.9,
    "突击炮III": 0.85,
    "斐迪南突击炮": 1.6,
    "防空坦克": 0.9,
    "四号防空坦克": 0.9,
    "Sd.Kfz.251装甲运兵车": 0.5,
}

# 简单的地形耗油修正：城市废墟/瓦砾会提高燃油消耗
TERRAIN_FUEL_MULTIPLIER = {
    None: 1.0,
    "街道": 1.0,
    "废墟": 1.35,
    "郊外": 1.1,
    "堤坝": 1.15,
    "车库": 1.0,
}

# 物品区域定价：基础价与地区倍率（单价以金条计）
BASE_SELL_PRICES = {
    "燃油桶": 1,
    "弹药箱": 1,
    "炮弹箱": 2,
    "备件": 1,
    "装甲板": 1,
    "电台电池": 1,
    "急救包": 1,
    "医疗包": 1,
    # 兼容旧代码或其他事件中使用的同义词
    "药品": 1,
    # 燃料类别名，确保能在出售界面列出
    "纯燃料桶": 3,
    "燃油罐": 1,
    "油桶": 1,
    "工具箱": 1,
    "侦察设备": 2,
    "地图碎片": 3,
    "润滑油": 1,
    "口粮": 1,
    "烟幕弹": 1,
    "伪装网": 2,
    "咖啡": 1,
}

# 地区价格倍率：按 s.location_key 区分（可扩展/配置）
REGION_PRICE_MULTIPLIER = {
    "city_center": 1.5,
    "suburb": 0.9,
    "industrial": 0.7,
}

def get_item_price(s: "GameState", item_name: str) -> int:
    """返回给定状态下某物品在当前地区的单价（以金条计）。

    逻辑：取基础价 * 地区倍率并四舍五入，至少为1。
    """
    try:
        base = int(BASE_SELL_PRICES.get(item_name, 1) or 1)
    except Exception:
        base = 1
    loc = str(getattr(s, "location_key", "")).strip()
    try:
        mult = float(REGION_PRICE_MULTIPLIER.get(loc, 1.0))
    except Exception:
        mult = 1.0
    # 好感越高越便宜：从辖区 favor 线性折扣，favor=0 -> 折扣0%，favor=100 -> 折扣上限
    try:
        sec = s.sectors.get(loc) if getattr(s, 'sectors', None) else None
        favor = int(getattr(sec, 'favor', 0) or 0) if sec is not None else 0
    except Exception:
        favor = int(getattr(s, 'favor', 0) or 0) if hasattr(s, 'favor') else 0

    # 最大折扣比例（配置）：好感100时最多打折 35%
    MAX_FAVOR_DISCOUNT = 0.35
    try:
        favor_clamped = max(0, min(100, int(favor)))
    except Exception:
        favor_clamped = 0
    favor_discount = (favor_clamped / 100.0) * MAX_FAVOR_DISCOUNT

    raw_price = float(base) * float(mult)
    discounted = raw_price * max(0.0, 1.0 - favor_discount)
    price = max(1, int(round(discounted)))
    return price


# 地区购买额度：限制在某地区可购买的次数（出售不受限制）
DEFAULT_REGION_PURCHASE_LIMIT = 5
REGION_PURCHASE_LIMITS = {
    "city_center": 3,
    "suburb": 6,
    "industrial": 4,
}

def _region_purchase_used_key(loc: str) -> str:
    return f"region_purchased_{loc}"

def region_purchase_remaining(s: "GameState", loc: Optional[str] = None) -> int:
    loc = (loc or str(getattr(s, "location_key", "")).strip())
    try:
        limit = int(REGION_PURCHASE_LIMITS.get(loc, DEFAULT_REGION_PURCHASE_LIMIT))
    except Exception:
        limit = DEFAULT_REGION_PURCHASE_LIMIT
    try:
        used = int(s.counters.get(_region_purchase_used_key(loc), 0) or 0)
    except Exception:
        used = 0
    return max(0, limit - used)

def region_consume_purchase(s: "GameState", loc: Optional[str] = None, count: int = 1) -> None:
    loc = (loc or str(getattr(s, "location_key", "")).strip())
    key = _region_purchase_used_key(loc)
    try:
        s.counters[key] = int(s.counters.get(key, 0) or 0) + int(count)
    except Exception:
        try:
            s.counters[key] = int(count)
        except Exception:
            pass


def _pct(value: Any, *, clamp: Tuple[int, int] = (0, 100)) -> int:
    """把数值稳定地显示为百分比整数。

    支持输入为比例(0~1)或百分数(0~100)并自动识别；
    对于 1.02 这类“略超 1 的比例”也按比例处理；
    最终会四舍五入并夹紧到 clamp 范围内，避免出现 -5% / 105% 等异常显示。
    """
    try:
        x = float(value)
    except Exception:
        return int(clamp[0])
    if math.isnan(x) or math.isinf(x):
        return int(clamp[0])

    # 自动判断：绝对值不大于 1.5 时更可能是“比例”
    if abs(x) <= 1.5:
        pct = x * 100.0
    else:
        pct = x

    lo, hi = clamp
    pct = max(float(lo), min(float(hi), pct))
    return int(round(pct))


def _consume_fuel(
    s: "GameState",
    base_cost: int,
    vehicle_model: Optional[str] = None,
    terrain: Optional[str] = None,
    vehicles: int = 1,
) -> int:
    """统一的燃油消耗接口：按全局倍率、车型与地形修正，返回实际扣除量。

    - `base_cost`：游戏内部的基础消耗值（通常来自地图 move_cost 等）。
    - `vehicle_model`：若提供则按车型倍率放大/缩小消耗。
    - `terrain`：若提供则按地形倍率修正（瓦砾/废墟消耗更大）。
    - `vehicles`：同时消耗的车辆数（例如同行多辆坦克时可按辆数放大）。
    """
    cost = _calc_fuel_cost(s, base_cost, vehicle_model=vehicle_model, terrain=terrain, vehicles=vehicles)
    try:
        s.fuel = max(0, int(getattr(s, "fuel", 0) or 0) - cost)
    except Exception:
        pass
    return cost


def _calc_fuel_cost(
    s: "GameState",
    base_cost: int,
    vehicle_model: Optional[str] = None,
    terrain: Optional[str] = None,
    vehicles: int = 1,
) -> int:
    """计算燃油消耗量但不修改 state。

    注意：`vehicles` 仍保留用于兼容旧逻辑，但新逻辑应倾向于“逐车调用、vehicles=1”。
    """
    try:
        mult = float(globals().get("FUEL_CONSUMPTION_MULT", 1.0))
    except Exception:
        mult = 1.0

    # 强行推进会明显增加油耗
    try:
        if int(s.buffs.get("强行推进", 0) or 0) > 0:
            mult += 0.25
    except Exception:
        pass

    fatigue = int(s.counters.get("fatigue", 0) or 0)
    if fatigue >= 80:
        mult += 0.20
    elif fatigue >= 60:
        mult += 0.10

    if s.debuffs.get("engine_damage", 0) > 0:
        mult += 0.15

    # 车型与地形修正
    try:
        model_mult = float(MODEL_FUEL_MULTIPLIER.get(vehicle_model, 1.0))
    except Exception:
        model_mult = 1.0
    try:
        terrain_mult = float(TERRAIN_FUEL_MULTIPLIER.get(terrain, 1.0))
    except Exception:
        terrain_mult = 1.0

    per_vehicle = int(math.ceil(float(max(0, int(base_cost))) * max(0.0, mult) * model_mult * terrain_mult))
    return int(per_vehicle) * max(1, int(vehicles))


def _tank_ally_consumes_fuel(s: "GameState", t: Any) -> bool:
    """判断某个友军坦克在当前回合是否应计入燃油消耗。

    新机制：友军拥有独立油箱，只要仍跟随且存活，就会消耗自己的燃油。
    """
    try:
        return bool(getattr(t, "alive", True))
    except Exception:
        return False


def _iter_fuel_consuming_tank_allies(s: "GameState"):
    """迭代所有本回合会消耗燃油的友军坦克（仅过滤死亡）。"""
    try:
        allies = getattr(s, "tank_allies", []) or []
    except Exception:
        allies = []
    for t in allies:
        if _tank_ally_consumes_fuel(s, t):
            yield t


def _consume_tank_ally_fuel(
    s: "GameState",
    t: Any,
    base_cost: int,
    *,
    terrain: Optional[str] = None,
) -> int:
    """扣除单个友军坦克自己的燃油（不影响玩家 s.fuel）。"""
    try:
        fuel = int(getattr(t, "fuel", 0) or 0)
    except Exception:
        fuel = 0
    cost = _calc_fuel_cost(s, base_cost, vehicle_model=getattr(t, "model", None), terrain=terrain, vehicles=1)
    try:
        setattr(t, "fuel", max(0, fuel - int(cost)))
    except Exception:
        pass
    try:
        if hasattr(t, "clamp"):
            t.clamp()
    except Exception:
        pass
    return int(cost)


def _calc_fuel_cost_with_allies(
    s: "GameState",
    base_cost: int,
    vehicle_model: Optional[str] = None,
    terrain: Optional[str] = None,
) -> int:
    """按“玩家+每辆友军逐车计算”预估本次总燃油需求，不修改 state。"""
    total = _calc_fuel_cost(s, base_cost, vehicle_model=vehicle_model, terrain=terrain, vehicles=1)
    for t in _iter_fuel_consuming_tank_allies(s):
        total += _calc_fuel_cost(s, base_cost, vehicle_model=getattr(t, "model", None), terrain=terrain, vehicles=1)
    return int(total)


def _consume_fuel_with_allies(
    s: "GameState",
    base_cost: int,
    vehicle_model: Optional[str] = None,
    terrain: Optional[str] = None,
) -> int:
    """按“玩家+每辆友军逐车计算”扣除燃油。

    新机制：玩家与友军拥有独立油箱：
    - 玩家消耗从 `s.fuel` 扣除；
    - 每辆友军坦克消耗从该坦克自己的 `t.fuel` 扣除。
    返回值为“玩家+友军”的总消耗量（用于展示/统计），但不会从同一个油箱里合并扣除。
    """
    total = _consume_fuel(s, base_cost, vehicle_model=vehicle_model, terrain=terrain, vehicles=1)
    for t in _iter_fuel_consuming_tank_allies(s):
        total += _consume_tank_ally_fuel(s, t, base_cost, terrain=terrain)
    return int(total)


def _fuel_consuming_vehicles(s: "GameState") -> int:
    """计算在消耗燃油时应计入的车辆数（包含玩家车辆及会消耗燃油的友军坦克）。

    规则：统计所有存活的 `tank_allies`；返回值至少为 1（玩家本车）。
    """
    try:
        cnt = 0
        for _ in _iter_fuel_consuming_tank_allies(s):
            cnt += 1
        return max(1, 1 + int(cnt))
    except Exception:
        return 1

def _script_dir() -> str:
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except Exception:
        return os.getcwd()

_SCRIPT_DIR = _script_dir()


def _user_data_dir() -> str:
    # 尽量写到用户目录，避免 exe 所在目录不可写导致存档失败
    base = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "柏林1945_虎王车长_系统版")

_DATA_DIR = _user_data_dir()
_EVENTS_BASENAME = "events_shown.json"
_EVENTS_FILE = os.path.join(_DATA_DIR, _EVENTS_BASENAME)
_LEGACY_EVENTS_FILE = os.path.join(_SCRIPT_DIR, _EVENTS_BASENAME)
_CRASH_DIR = os.path.join(_DATA_DIR, "crash_reports")


_SAVE_DIR = os.path.join(_DATA_DIR, "saves")

# 存档槽位：文件层面原本已按 1~9 夹紧，但菜单只展示 1~3。
# 这里统一为 1~9，并额外提供一个独立的自动存档文件。
SAVE_SLOT_MIN = 1
SAVE_SLOT_MAX = 9
AUTOSAVE_BASENAME = "autosave.pkl"


def _save_path(slot: int) -> str:
    slot = int(slot)
    slot = max(SAVE_SLOT_MIN, min(SAVE_SLOT_MAX, slot))
    return os.path.join(_SAVE_DIR, f"save_slot_{slot}.pkl")


def _autosave_path() -> str:
    return os.path.join(_SAVE_DIR, AUTOSAVE_BASENAME)


def _rotate_backups(path: str, *, keep: int = 2) -> None:
    """滚动备份：path -> path.bak -> path.bak2 ..."""
    try:
        keep = int(keep)
    except Exception:
        keep = 2
    keep = max(0, min(5, keep))
    if keep <= 0:
        return
    if not os.path.exists(path):
        return

    # 先把旧备份往后挪
    for i in range(keep, 1, -1):
        src = f"{path}.bak" if i == 2 else f"{path}.bak{i-1}"
        dst = f"{path}.bak{i}"
        if os.path.exists(src):
            try:
                os.replace(src, dst)
            except Exception:
                pass

    # 再生成最新 .bak
    try:
        shutil.copy2(path, f"{path}.bak")
    except Exception:
        pass


def _try_load_state_from_path(path: str) -> Optional["GameState"]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            blob = pickle.load(f)
        if isinstance(blob, dict) and "state" in blob:
            st = blob.get("state")
        else:
            st = blob
        if isinstance(st, GameState):
            st.clamp()
            return st
        return None
    except Exception:
        return None


def _dump_save_blob(state: "GameState") -> Dict[str, Any]:
    return {
        "version": 1,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "meta": {
            "name": getattr(state, "name", ""),
            "callsign": getattr(state, "callsign", ""),
            "round": int(getattr(state, "round_number", 0) or 0),
            "location": str(getattr(state, "location_key", "")),
            "vp": int(getattr(state, "victory_points", 0) or 0),
            "fuel": int(getattr(state, "fuel", 0) or 0),
            "damage": int(getattr(state, "damage", 0) or 0),
        },
        "state": state,
    }


def save_game(state: "GameState", *, slot: int) -> bool:
    try:
        _ensure_dir(_SAVE_DIR)
        path = _save_path(slot)
        tmp = f"{path}.tmp"
        _rotate_backups(path, keep=2)
        blob = _dump_save_blob(state)
        with open(tmp, "wb") as f:
            pickle.dump(blob, f, protocol=pickle.HIGHEST_PROTOCOL)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def save_autosave(state: "GameState") -> bool:
    """自动存档：不占用槽位，写到 autosave.pkl。"""
    try:
        _ensure_dir(_SAVE_DIR)
        path = _autosave_path()
        tmp = f"{path}.tmp"
        _rotate_backups(path, keep=2)
        blob = _dump_save_blob(state)
        with open(tmp, "wb") as f:
            pickle.dump(blob, f, protocol=pickle.HIGHEST_PROTOCOL)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def load_game(*, slot: int) -> Optional["GameState"]:
    path = _save_path(slot)
    st = _try_load_state_from_path(path)
    if st is not None:
        return st
    # 回退：尝试备份
    for bak in (f"{path}.bak", f"{path}.bak2"):
        st = _try_load_state_from_path(bak)
        if st is not None:
            return st
    return None


def load_autosave() -> Optional["GameState"]:
    path = _autosave_path()
    st = _try_load_state_from_path(path)
    if st is not None:
        return st
    for bak in (f"{path}.bak", f"{path}.bak2"):
        st = _try_load_state_from_path(bak)
        if st is not None:
            return st
    return None


def _save_slot_info(slot: int) -> str:
    path = _save_path(slot)
    if not os.path.exists(path):
        return f"{slot}. (空)"
    try:
        with open(path, "rb") as f:
            blob = pickle.load(f)
        if isinstance(blob, dict):
            meta = blob.get("meta") if isinstance(blob.get("meta"), dict) else {}
            ts = str(blob.get("saved_at", ""))
            name = str(meta.get("name", ""))
            callsign = str(meta.get("callsign", ""))
            rd = int(meta.get("round", 0) or 0)
            loc = str(meta.get("location", ""))
            vp = int(meta.get("vp", 0) or 0)
            fuel = int(meta.get("fuel", 0) or 0)
            dmg = int(meta.get("damage", 0) or 0)
            tag = f"{name}/{callsign} 回合{rd} 地点{loc} VP{vp} 油{fuel} 损{dmg}"
            if ts:
                tag += f" | {ts}"
            return f"{slot}. {tag}"
        return f"{slot}. (存档存在，但元信息不可读)"
    except Exception:
        return f"{slot}. (存档损坏或不可读)"


def menu_save_game(ins: "InputStream", s: "GameState") -> None:
    print("\n存档：")
    print(f"- A. 自动存档：{_autosave_path()}")
    for i in range(SAVE_SLOT_MIN, SAVE_SLOT_MAX + 1):
        print("- " + _save_slot_info(i))
    raw = get_valid_input(ins, f"选择存档位({SAVE_SLOT_MIN}-{SAVE_SLOT_MAX}，回车取消)：", default="")
    if raw.strip() == "":
        return
    try:
        slot = int(raw)
    except ValueError:
        print("输入无效。")
        return
    if slot < SAVE_SLOT_MIN or slot > SAVE_SLOT_MAX:
        print("无效存档位。")
        return
    ok = save_game(s, slot=slot)
    if ok:
        print(f"存档成功：{_save_path(slot)}")
    else:
        print("存档失败：请确认是否有写入权限。")


def menu_load_game(ins: "InputStream") -> Optional["GameState"]:
    print("\n读取存档：")
    apath = _autosave_path()
    auto_tag = "(不存在)" if not os.path.exists(apath) else ""
    print(f"- A. 自动存档 {auto_tag}")
    for i in range(SAVE_SLOT_MIN, SAVE_SLOT_MAX + 1):
        print("- " + _save_slot_info(i))
    raw = get_valid_input(ins, f"选择存档位({SAVE_SLOT_MIN}-{SAVE_SLOT_MAX}，输入A自动存档，回车取消)：", default="")
    if raw.strip() == "":
        return None
    if raw.strip().lower() == "a":
        st = load_autosave()
        if st is None:
            print("读取失败：自动存档不存在或已损坏。")
            return None
        print("读取成功（自动存档）。")
        return st
    try:
        slot = int(raw)
    except ValueError:
        print("输入无效。")
        return None
    if slot < SAVE_SLOT_MIN or slot > SAVE_SLOT_MAX:
        print("无效存档位。")
        return None
    st = load_game(slot=slot)
    if st is None:
        print("读取失败：该存档不存在或已损坏。")
        return None
    print("读取成功。")
    return st


def _ensure_dir(path: str) -> None:
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _atomic_write_text(path: str, text: str, *, encoding: str = "utf-8") -> None:
    _ensure_dir(os.path.dirname(path) or ".")
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding=encoding, newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _best_effort_backup(path: str) -> Optional[str]:
    if not os.path.exists(path):
        return None
    bak = f"{path}.bak"
    try:
        shutil.copy2(path, bak)
        return bak
    except Exception:
        return None


def _load_json_best_effort(path: str) -> Optional[Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_crash_report(exc: BaseException, *, where: str = "", state: Optional["GameState"] = None) -> str:
    _ensure_dir(_CRASH_DIR)
    report_path = os.path.join(_CRASH_DIR, f"crash_{_timestamp()}.log")
    lines: List[str] = []
    lines.append(f"time: {datetime.now().isoformat(timespec='seconds')}")
    if where:
        lines.append(f"where: {where}")
    lines.append(f"python: {sys.version.replace(os.linesep, ' ')}")
    lines.append(f"argv: {sys.argv}")
    lines.append("")
    lines.append("traceback:")
    lines.append("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    if state is not None:
        try:
            snap = _snapshot_state(state)
            lines.append("")
            lines.append("state_snapshot:")
            lines.append(json.dumps(snap, ensure_ascii=False, indent=2))
        except Exception:
            pass
    try:
        _atomic_write_text(report_path, "\n".join(lines) + "\n")
    except Exception:
        return ""
    return report_path


def _snapshot_state(s: "GameState") -> Dict[str, Any]:
    # 仅用于崩溃报告：保留关键数值，避免序列化复杂对象导致再次报错
    return {
        "name": getattr(s, "name", None),
        "callsign": getattr(s, "callsign", None),
        "difficulty_key": getattr(s, "difficulty_key", None),
        "round_number": getattr(s, "round_number", None),
        "action_points": getattr(s, "action_points", None),
        "location_key": getattr(s, "location_key", None),
        "victory_points": getattr(s, "victory_points", None),
        "fuel": getattr(s, "fuel", None),
        "mg_ammo": getattr(s, "mg_ammo", None),
        "ap_shells": getattr(s, "ap_shells", None),
        "he_shells": getattr(s, "he_shells", None),
        "gold_bars": getattr(s, "gold_bars", None),
        "passes": getattr(s, "passes", None),
        "morale": getattr(s, "morale", None),
        "damage": getattr(s, "damage", None),
        "city_collapse": getattr(s, "city_collapse", None),
        "ended": getattr(s, "ended", None),
        "ending_id": getattr(s, "ending_id", None),
        "inventory": dict(getattr(s, "inventory", {}) or {}),
        "buffs": dict(getattr(s, "buffs", {}) or {}),
        "debuffs": dict(getattr(s, "debuffs", {}) or {}),
        "counters": dict(getattr(s, "counters", {}) or {}),
        "explored": sorted(list(getattr(s, "explored", set()) or set())),
        "shown_events": sorted(list(getattr(s, "shown_events", set()) or set())),
        "story_flags": dict(getattr(s, "story_flags", {}) or {}),
        "story_vars": dict(getattr(s, "story_vars", {}) or {}),
    }


def _maybe_migrate_legacy_events_file() -> None:
    # 旧版本把 events_shown.json 放在脚本目录；新版本放用户目录
    try:
        if os.path.abspath(_EVENTS_FILE) == os.path.abspath(_LEGACY_EVENTS_FILE):
            return
        if os.path.exists(_EVENTS_FILE):
            return
        if not os.path.exists(_LEGACY_EVENTS_FILE):
            return
        _ensure_dir(os.path.dirname(_EVENTS_FILE) or ".")
        shutil.copy2(_LEGACY_EVENTS_FILE, _EVENTS_FILE)
    except Exception:
        pass

def _load_shown() -> set:
    _maybe_migrate_legacy_events_file()
    if not os.path.exists(_EVENTS_FILE):
        return set()

    data = _load_json_best_effort(_EVENTS_FILE)
    if isinstance(data, list):
        return set(map(str, data))

    # 主文件损坏：尝试从备份恢复
    bak = f"{_EVENTS_FILE}.bak"
    data_bak = _load_json_best_effort(bak)
    if isinstance(data_bak, list):
        try:
            # 用备份覆盖主文件，避免每次启动都重复“空集合”
            _atomic_write_text(_EVENTS_FILE, json.dumps(data_bak, ensure_ascii=False))
        except Exception:
            pass
        return set(map(str, data_bak))

    # 主文件与备份都不可用：隔离损坏文件，回退为空
    try:
        corrupt = os.path.join(_DATA_DIR, f"{_EVENTS_BASENAME}.corrupt.{_timestamp()}")
        os.replace(_EVENTS_FILE, corrupt)
    except Exception:
        pass
    return set()

def _save_shown(shown_set: set) -> None:
    # 原子写入 + 备份：避免断电/崩溃导致文件半写入
    _best_effort_backup(_EVENTS_FILE)
    payload = json.dumps(sorted(list(map(str, shown_set))), ensure_ascii=False)
    _atomic_write_text(_EVENTS_FILE, payload)


def repair_local_files(*, verbose: bool = True) -> bool:
    """尝试修复本地持久化文件（目前仅包含 events_shown.json）。

    返回 True 表示已修复或文件健康，False 表示无法修复（但已尽量隔离损坏文件）。
    """
    _maybe_migrate_legacy_events_file()
    ok = True

    if not os.path.exists(_EVENTS_FILE):
        if verbose:
            print(f"修复：未发现 {_EVENTS_BASENAME}，无需处理。")
        return True

    data = _load_json_best_effort(_EVENTS_FILE)
    if isinstance(data, list):
        if verbose:
            print(f"修复：{_EVENTS_BASENAME} 正常（条目数：{len(data)}）。")
        return True

    if verbose:
        print(f"修复：{_EVENTS_BASENAME} 可能已损坏，尝试从备份恢复...")

    bak = f"{_EVENTS_FILE}.bak"
    data_bak = _load_json_best_effort(bak)
    if isinstance(data_bak, list):
        try:
            _atomic_write_text(_EVENTS_FILE, json.dumps(data_bak, ensure_ascii=False))
            if verbose:
                print("修复：已从 .bak 备份恢复成功。")
            return True
        except Exception:
            ok = False

    # 无备份可用：隔离原文件
    try:
        corrupt = os.path.join(_DATA_DIR, f"{_EVENTS_BASENAME}.corrupt.{_timestamp()}")
        os.replace(_EVENTS_FILE, corrupt)
        if verbose:
            print(f"修复：已隔离损坏文件为：{corrupt}")
    except Exception:
        ok = False

    return ok

def check_and_show_once(event_id: str, text: str, show_callback: Callable[[str], None], *,
                        trigger_datetime: Optional[datetime]=None,
                        trigger_game_time_seconds: Optional[float]=None,
                        current_game_time_seconds: Optional[float]=None) -> bool:
    """
    通用单次文字剧情触发器。
    - event_id: 唯一字符串ID，确保只触发一次。
    - text: 要显示的剧情文本（字符串）。
    - show_callback: 显示函数，接收一个字符串参数（例如 UI 的文本显示函数或 print）。
    - trigger_datetime: 若使用真实世界时间触发，传入 datetime 对象（本地时间）。
    - trigger_game_time_seconds: 若使用游戏内时间触发，传入触发所需的游戏秒数阈值。
    - current_game_time_seconds: 使用游戏时间触发时，传入当前游戏流逝秒数。
    返回 True 如果刚触发并显示，False 否则。
    """
    shown = _load_shown()
    if event_id in shown:
        return False

    now = datetime.now()
    triggered = False

    if trigger_datetime is not None:
        if now >= trigger_datetime:
            triggered = True

    if trigger_game_time_seconds is not None and current_game_time_seconds is not None:
        if current_game_time_seconds >= trigger_game_time_seconds:
            triggered = True

    if triggered:
        try:
            show_callback(text)
        except Exception:
            print(text)
        shown.add(event_id)
        _save_shown(shown)
        return True

    return False

# 示例辅助：若游戏中没有专用 UI 回调，可传入 print 作为 show_callback；
# 若需要按存档分离持久化，请将 _EVENTS_FILE 改为包含存档ID的文件名。

# --- 章节文本与效果（每3回合进入下一章）
CHAPTER_INTERVAL = 3
CHAPTERS: Dict[str, str] = {
    'chapter_01': '''第1章 最后的补充：
迪尔斯车组接收了一辆新“虎王”，并迎来一名稚嫩的新兵——十七岁的亚历克斯。
老兵们对这辆新坦克有一种近乎仪式的检查：他们用缺油的手摸过车身，给履带临时缠上布片，凑合着替换一处缺少的支撑件。
亚历克斯站在一旁，既带着敬畏又带着不安——他看着刻在炮塔边缘的新刻字和别人留下的小礼物，仿佛看到了一段段被泥土遮蔽的生活碎片。''',
    'chapter_02': '''第2章 霍诺夫的阴影：
一次日常的起动测试在霍诺夫的修理仓内失控。一道被磨损的电火花引燃了油管处的残留汽油，浓烟迅速吞没了器材架与布匹。
火光被扑灭后，主要的光学瞄准镜被高温毁坏，车组被迫在随后的战斗中用经验与粗糙的替代瞄准器在黑暗里瞄准目标。''',
    'chapter_03': '''第3章 十七岁的装填手：
亚历克斯在实战中第一次颤抖，但他迅速学会了换壳与装弹的节奏。深夜里，他和一名老兵在残墙边谈起家乡、音乐与破碎理想。
通过这些细节，他从少年稚气一步步迈向一种在战场上能保持节奏的沉着。''',
    'chapter_04': '''第4章 破碎的防线：
车组随队沿着奥得河—尼斯河的残破防线推进，碉堡之间一片焦土。通讯时断时续，撤退的人群像潮水般涌动。
坦克穿行在断墙与半塌的建筑间，遭遇反坦克火力，战场一时间充满混乱与噪音。''',
    'chapter_05': '''第5章 炮手的抉择：
在泰尔托运河桥的激烈遭遇中，科特用一发精确射击击毁了一辆苏军IS-2。这一次击毁成为他的个人里程碑，却也在他心中留下矛盾——为战果短暂自豪之余，他也感到一种对生命被抹去般的空虚。''',
    'chapter_06': '''第6章 地下医院：
迪尔斯走进掩体医院的临时输液室，看到堆满的烂伤与匮乏的药品。护士和医护人员疲惫地交换着稀缺的信息与药品配给单，战争将民用设施彻底改造成了资源分配与伦理冲突的现场。''',
    'chapter_07': '''第7章 中尉的接替：
一位新中尉带着冷峻的命令到来，他引用高层的最新指示，言辞简短却带来压力。
迪尔斯與中尉之间出现微妙的信任博弈：服从命令会换来短时安全，还是坚持经验更能让人活下来？''',
    'chapter_08': '''第8章 燃烧的城市：
空袭夜里到来，城市的天际被火焰撕裂。民众慌忙钻进地铁与废墟，补给队在爆炸中被迫分散。
在废墟间的求生小故事让车组成员短暂注意到平民命运的脆弱。''',
    'chapter_09': '''第9章 党卫军的阴影：
一次偶然的举动让迪尔斯露出左腋下的旧日纹身，这个血型纹身带出一名老战友的记忆。通过纹身的回忆，党卫军的筛选与身份标识在车组的日常里显得突兀而沉重。''',
    'chapter_10': '''第10章 四月二十日的警示：
希特勒生日那天，车组接到了可能获勋的通知。颁勋前的彩排與官僚流程充满形式感，与前线的荒诞现实形成鲜明对照。''',
    'chapter_11': '''第11章 波茨坦广场的伏击：
在周密的侦察与伪装下，车组参与一场夜间伏击，成功引诱并摧毁一批苏军装甲车，取得局部战术性胜利。这一成功既带来短暂的士气，也加重了补给与伤亡的代价。''',
    'chapter_12': '''第12章 勃兰登堡门的红旗：
在勃兰登堡门的废墟前，科特用机枪压制试图插旗的敌兵，近战与政治象征交织成紧绷的一幕。''',
    'chapter_13': '''第13章 地下的狂欢：
总理府地下的一少部分高层在末日中纵情消遣，那里的怪诞细节与外面街头的死亡形成强烈对照，展示社会不同层面的心理分化。''',
    'chapter_14': '''第14章 希特勒的牙医：
通过一名牙医助手的视角，发现元首健康恶化的蛛丝马迹，医院外的戒备与权力中心的恐惧交织。''',
    'chapter_15': '''第15章 婚礼进行曲：
希特勒与爱娃的匆忙婚礼成为最后的仪式之一，外界炮火逐步逼近，婚礼与战争消息交错，显得荒诞且悲凉。''',
    'chapter_16': '''第16章 最后的油料：
车组秘密潜入被遗弃的补给仓库，在炮火下抢夺燃料。燃料成了生死线，分配引发微小但真实的冲突。''',
    'chapter_17': '''第17章 动物园高地的陷落：
这处高地一旦失守，防线进一步缩小。车组在高地的争夺与撤退中见证了地形对巷战的决定性影响。''',
    'chapter_18': '''第18章 人民冲锋队：
迪尔斯被迫与民兵协同作战，但民兵的简陋装备与缺乏训练导致队伍被削弱，理想被现实耗尽。''',
    'chapter_19': '''第19章 孤儿院的救援：
车组在废墟中发现被困孤儿并冒险救出数名孩子。孩子的恐惧与车组成员短暂的温柔片刻成为战场中难得的人性闪光。''',
    'chapter_20': '''第20章 叛徒的代价：
费格莱恩被指控为叛徒并遭到粗糙审判与处决，团队内部对“清洗”的不同态度在紧张与恐惧中暴露。''',
    'chapter_21': '''第21章 戈培尔的孩子们：
宣传机构内的子女命运被揭露：一些被保护，一些迷失。宣传体系试图保护核心家庭，但意识形态对下一代的扭曲显而易见。''',
    'chapter_22': '''第22章 国会大厦的阴影：
车组为守卫国会大厦提供支援，建筑内部火光、旗帜撕裂与士兵的反思共同构成象征性的战斗。''',
    'chapter_23': '''第23章 地下世界的逃亡：
车组与少队利用地铁隧道机动，暂时避开地面火力，但隧道内的窒息恐惧与迷路风险也成为新的威胁。''',
    'chapter_24': '''第24章 苏军战俘的启示：
短暂俘获一名苏军士兵后，与其交流揭示了敌方士兵的动机与物资状况，冲破了单一的敌人形象。''',
    'chapter_25': '''第25章 电台的谎言：
戈培尔的广播对前线播放希望的言辞，而前线士兵却听到截然不同的现实；电台内部的制作流程与前线反应形成反差。''',
    'chapter_26': '''第26章 火焰中的柏林：
大面积城市火灾蔓延，动物尸骸、变色的河水与街区废墟共同构成城市毁灭的长远景象。''',
    'chapter_27': '''第27章 党卫军逃兵：
若干党卫军成员选择逃离，这一行为暴露了内部纪律的崩溃，也带来复杂的同袍反应。''',
    'chapter_28': '''第28章 最后的空中支援：
飞行员们在稀少的空中支援任务中承受巨大压力，空中救援越来越危险而稀缺。''',
    'chapter_29': '''第29章 希特勒的遗嘱：
遗旨的传达被篡改与解读，引发车组成员对忠诚与现实的深刻反思。''',
    'chapter_30': '''第30章 焚尸的硝烟：
参与焚烧遗体的过程被仪式化地描述，参与者在荒诞的礼仪中承受心理负担。''',
    'chapter_31': '''第31章 五月一日的黎明：
戈培尔自杀的消息传播后，车组与周围人的反应各不相同：有人借酒麻醉，有人低声祈祷。''',
    'chapter_32': '''第32章 克罗尔歌剧院的伤员：
车组参与将伤员转移，临时担架与疲惫的医护构成搬运中的混乱场景。''',
    'chapter_33': '''第33章 自杀的命令：
在绝望的压力下，目睹或被敦促自尽的场景成为对群体心理崩溃的极端呈现。''',
    'chapter_34': '''第34章 最后的无线电：
通过残存无线电的短促联系，车组听到模糊消息，最后一条信号带来短暂的希望或绝望。''',
    'chapter_35': '''第35章 地下突围会议：
残余指挥层制定最后突围计划，会议中的人员分配与牺牲选择暴露决策的仓促与荒谬。''',
    'chapter_36': '''第36章 摧毁虎王：
车组讨论是否自毁坦克以防被缴获，这是一场现实与情感交织的争论，坦克被视为战友也被视为物件。''',
    'chapter_37': '''第37章 蒙克的加入：
旅队长蒙克加入突围小组，他的命令与号召力短暂凝聚残兵，但也暴露决策上的缺陷。''',
    'chapter_38': '''第38章 魏登达默桥的弹雨：
突围队在桥梁处遭遇密集火力，烟幕与牵引装甲的细节展现突围的绝望与勇气。''',
    'chapter_39': '''第39章 搭载人员的死亡：
坦克外搭载的民众与士兵在交火中遭受惨重伤亡，车组在甲板上留下血迹与短暂的悼念。''',
    'chapter_40': '''第40章 结局：
根据玩家的选择与数值展现多种结局，情感收束与数值走向共同决定最终场面。''',
}


def _apply_chapter_effect(s: 'GameState', idx: int) -> None:
    """对GameState应用更丰富的章节效果并触发特殊事件（生成救援任务、加入成员、设置buff/debuff等）。"""
    def mod(attr: str, delta: int) -> None:
        if hasattr(s, attr):
            setattr(s, attr, getattr(s, attr) + delta)

    descs: List[str] = []

    if idx == 1:
        add_item(s, '燃油桶', 1)
        mod('morale', 3)
        descs.append('获得1个燃油桶，士气+3。')
        if crew_role_state(s, '装填手') == 'missing' and not any((m.name == '亚历克斯' and m.role == '装填手') for m in s.crew):
            # 剧情新加入：熟练度初始为 0（原状态），后续可通过训练/战斗成长
            s.crew.append(CrewMember(role='装填手', name='亚历克斯', proficiency=0))
            descs.append('新兵亚历克斯加入车组（装填手）。')

    elif idx == 2:
        mod('damage', 10)
        s.debuffs['optics_broken'] = max(s.debuffs.get('optics_broken', 0), 5)
        descs.append('维修仓火灾：光学瞄准器损坏，未来若干回合射击精度受影响；损伤+10。')

    elif idx == 3:
        add_item(s, '急救包', 1)
        mod('morale', 5)
        s.counters['training'] = s.counters.get('training', 0) + 1
        # 训练：显式提升装填手熟练度（优先点名“亚历克斯”）
        boosted = False
        for m in s.crew:
            if m.alive and m.role == '装填手' and m.name == '亚历克斯':
                m.proficiency = int(getattr(m, 'proficiency', 0) or 0) + 12
                m.clamp()
                boosted = True
                break
        if not boosted:
            for m in s.crew:
                if m.alive and m.role == '装填手':
                    m.proficiency = int(getattr(m, 'proficiency', 0) or 0) + 10
                    m.clamp()
                    boosted = True
                    break
        if boosted:
            descs.append('亚历克斯学习换壳与装弹节奏：士气+5，并获得1个急救包；装填手熟练度提升。')
        else:
            descs.append('亚历克斯学习换壳与装弹节奏：士气+5，并获得1个急救包。')

    elif idx == 4:
        _consume_fuel_with_allies(s, 7, vehicle_model="虎式坦克")
        mod('city_collapse', 4)
        descs.append('沿残破防线推进：燃油-7，城市崩溃+4。')

    elif idx == 5:
        mod('victory_points', 3)
        mod('morale', -2)
        descs.append('击毁IS-2：胜利点+3，但内心矛盾导致士气-2。')

    elif idx == 6:
        mod('city_collapse', 5)
        qm = Quest(id='Q_hospital', title='援助地下医院', desc='为掩体医院运送药品或人员。', target=1, reward_points=4)
        s.quests.append(qm)
        descs.append('发现地下医院的烂伤与缺药：城市崩溃+5；新增委托“援助地下医院”。')

    elif idx == 7:
        mod('morale', -3)
        descs.append('新中尉到任：士气-3，车组内部紧张升级。')

    elif idx == 8:
        mod('city_collapse', 6)
        s.civilians_helped += 1
        descs.append('空袭来临：城市崩溃+6，同时记录救助1名平民。')

    elif idx == 9:
        mod('morale', -2)
        descs.append('党卫军回忆带来压抑感：士气-2。')

    elif idx == 10:
        mod('victory_points', 1)
        descs.append('荒诞的授勋：胜利点+1（象征）。')

    elif idx == 11:
        mod('victory_points', 5)
        mod('fuel', -8)
        s.counters['wins'] = s.counters.get('wins', 0) + 1
        descs.append('波茨坦广场伏击成功：胜利点+5，燃油-8，记录胜利。')

    elif idx == 12:
        mod('damage', 8)
        mod('morale', -4)
        descs.append('勃兰登堡门的近战：损伤+8，士气-4。')

    elif idx == 13:
        mod('city_collapse', 2)
        mod('morale', -5)
        descs.append('总理府地下的狂欢：士气-5，城市崩溃+2。')

    elif idx == 14:
        s.counters['intel'] = s.counters.get('intel', 0) + 1
        descs.append('牙医发现细微病征：情报计数+1。')

    elif idx == 15:
        mod('morale', 1)
        descs.append('匆忙婚礼：短暂的情感安抚，士气+1。')

    elif idx == 16:
        add_item(s, '燃油桶', 3)
        s.counters['fuel_raids'] = s.counters.get('fuel_raids', 0) + 1
        descs.append('秘密抢夺燃料：获得3个燃油桶，记录抢夺次数+1。')

    elif idx == 17:
        mod('city_collapse', 6)
        descs.append('动物园高地陷落：城市崩溃+6。')

    elif idx == 18:
        mod('morale', -4)
        descs.append('与人民冲锋队协同失败：士气-4。')

    elif idx == 19:
        rm = RescueMission(id='RM_orphan', title='孤儿院救援', desc='救出孤儿并护送至安全地带', expires_round=s.round_number + 5, difficulty=0.6)
        s.rescue_missions.append(rm)
        s.civilians_helped += 2
        mod('morale', 4)
        descs.append('孤儿院救援：新增救援任务并救出若干孩子，士气+4。')

    elif idx == 20:
        mod('morale', -6)
        descs.append('叛徒被处决：团队恐惧加剧，士气-6。')

    elif idx == 21:
        s.counters['propaganda'] = s.counters.get('propaganda', 0) + 1
        descs.append('戈培尔的孩子们事件：宣传计数+1。')

    elif idx == 22:
        mod('victory_points', 2)
        descs.append('国会大厦支援战：胜利点+2。')

    elif idx == 23:
        s.buffs['underground_maneuver'] = max(s.buffs.get('underground_maneuver', 0), 3)
        descs.append('地铁隧道机动：获得短期地下机动增益（减少地面威胁）。')

    elif idx == 24:
        s.counters['intel'] = s.counters.get('intel', 0) + 1
        mod('morale', 1)
        descs.append('俘虏带来情报：情报+1，士气+1。')

    elif idx == 25:
        mod('morale', -2)
        descs.append('电台的谎言：士气-2。')

    elif idx == 26:
        mod('city_collapse', 8)
        descs.append('城市大火蔓延：城市崩溃+8。')

    elif idx == 27:
        s.counters['deserters'] = s.counters.get('deserters', 0) + 1
        mod('morale', -3)
        descs.append('党卫军逃兵事件：纪律动摇，士气-3。')

    elif idx == 28:
        mod('morale', 3)
        descs.append('最后的空中支援：士气+3（短期）。')

    elif idx == 29:
        s.counters['orders_changed'] = s.counters.get('orders_changed', 0) + 1
        descs.append('遗旨传达混乱：记录命令变动。')

    elif idx == 30:
        mod('morale', -4)
        descs.append('焚尸的硝烟：心理负担，士气-4。')

    elif idx == 31:
        mod('morale', -10)
        descs.append('戈培尔自杀消息：士气大幅下降-10。')

    elif idx == 32:
        s.counters['evacuations'] = s.counters.get('evacuations', 0) + 1
        descs.append('参与伤员转移：记录转移事件。')

    elif idx == 33:
        mod('morale', -12)
        descs.append('自杀命令触目惊心：士气-12。')

    elif idx == 34:
        mod('morale', 2)
        descs.append('最后的无线电带来短暂希望：士气+2。')

    elif idx == 35:
        s.counters['escape_plan'] = s.counters.get('escape_plan', 0) + 1
        descs.append('地下突围会议记录：突围计划+1。')

    elif idx == 36:
        s.buffs['self_destruct_discussion'] = 1
        descs.append('关于摧毁虎王的争论被提出：设置自毁讨论标记。')

    elif idx == 37:
        mod('morale', 3)
        s.counters['monk_arrived'] = 1
        descs.append('蒙克加入突围小组：士气+3。')

    elif idx == 38:
        mod('damage', 10)
        mod('fuel', -5)
        descs.append('魏登达默桥弹雨：损伤+10，燃油-5。')

    elif idx == 39:
        s.counters['civ_casualties'] = s.counters.get('civ_casualties', 0) + 1
        mod('morale', -5)
        descs.append('外搭人员大量伤亡：士气-5，记录民众伤亡。')

    elif idx == 40:
        descs.append('进入结局阶段：当前数值将影响分支结局。')

    else:
        descs.append('章节触发，但未定义特殊效果。')

    s.clamp()
    for d in descs:
        print(f"[章节效果] 第{idx}章 生效：{d}")


def maybe_trigger_chapter(s: 'GameState') -> None:
    """若已到达章节触发点则显示对应章节并应用效果（每次运行仅触发一次）。

    返回本次刚触发的章节序号；若未触发则返回 None。
    """
    if s.round_number <= 0:
        return None
    # 第1章已在开局强制触发；此处用于后续按固定间隔推进。
    if (s.round_number - 1) % CHAPTER_INTERVAL != 0:
        return None
    chapter_idx = (s.round_number - 1) // CHAPTER_INTERVAL + 1
    if chapter_idx < 1 or chapter_idx > 40:
        return None

    eid = f"chapter_{chapter_idx:02d}"
    if eid in s.shown_events:
        return None
    s.shown_events.add(eid)

    text = CHAPTERS.get(eid, f"第{chapter_idx}章（无文本）")
    narrate(text)
    _apply_chapter_effect(s, chapter_idx)
    return chapter_idx


def force_trigger_chapter(s: 'GameState', chapter_idx: int) -> None:
    """强制触发指定章节（用于开局直接进入第一章）。"""
    if chapter_idx < 1 or chapter_idx > 40:
        return
    eid = f"chapter_{chapter_idx:02d}"
    if eid in s.shown_events:
        return
    s.shown_events.add(eid)
    text = CHAPTERS.get(eid, f"第{chapter_idx}章（无文本）")
    narrate(text)
    _apply_chapter_effect(s, chapter_idx)


def _story_mark_seen(s: 'GameState', event_id: str) -> bool:
    """返回 True 表示这是首次触发（本次运行内）。"""
    if event_id in s.shown_events:
        return False
    s.shown_events.add(event_id)
    return True


def story_choice(
    ins: 'InputStream',
    s: 'GameState',
    *,
    event_id: str,
    title: str,
    text: str,
    options: Dict[str, Tuple[str, Callable[[], None]]],
    default: str,
) -> None:
    """显示一次性的关键剧情选择。

    options: {"1": ("选项文案", effect_fn), ...}
    """
    if not _story_mark_seen(s, event_id):
        return
    print("\n" + "-" * 70)
    print(f"【关键抉择】{title}")
    print("-" * 70)
    narrate(text)
    print("-" * 70)
    menu = {k: v[0] for k, v in options.items()}
    c = choose(ins, "你的选择：", menu, default=default)
    try:
        options[c][1]()
    except Exception:
        # 即便某个分支效果失败，也尽量不让游戏中断
        pass
    s.clamp()


def maybe_trigger_story_for_chapter(ins: 'InputStream', s: 'GameState', chapter_idx: int) -> None:
    """在章节节点投放更强的分支选择，改变后续事件/战斗/结局。"""

    def _mod_current_sector(*, favor: int = 0, fall: int = 0) -> None:
        sec = s.sectors.get(s.location_key)
        if sec is None:
            return
        sec.favor += int(favor)
        sec.fall += int(fall)

    def _default() -> str:
        return "2" if ins.default_when_empty else "1"

    def _try_spend_item(item: str, cnt: int) -> bool:
        try:
            return bool(spend_item(s, item, int(cnt)))
        except Exception:
            return False

    def _try_spend_currency(*, gold: int = 0, passes: int = 0) -> bool:
        try:
            return bool(spend_currency(s, gold=int(gold), passes=int(passes)))
        except Exception:
            return False

    def _try_grant_tank_support() -> bool:
        try:
            return bool(grant_friendly_tank_support(s))
        except Exception:
            return False

    # 第2章：光学损坏后是否冒险修复
    if chapter_idx == 2:
        story_choice(
            ins,
            s,
            event_id="SC_OPTICS_02",
            title="瞄准镜的代价",
            text=(
                "\n火灾让主要光学瞄准器报废。驾驶员（兼机械）说：可以尝试临时修复，但要拆下更多部件——"
                "这会让你们短时间暴露在街区里。\n"
            ),
            options={
                "1": (
                    "冒险停留修复（损伤↓，但可能触发一次额外遭遇）",
                    lambda: (
                        s.debuffs.pop("optics_broken", None),
                        setattr(s, "damage", max(0, s.damage - 6)),
                        setattr(s, "morale", s.morale - 1),
                        s.buffs.__setitem__("额外遭遇", 1),
                    ),
                ),
                "2": (
                    "带着缺陷前进（保守行军；短期命中受影响）",
                    lambda: (
                        s.debuffs.__setitem__("optics_broken", max(5, s.debuffs.get("optics_broken", 0))),
                        setattr(s, "morale", s.morale + 1),
                    ),
                ),
            },
            default=_default(),
        )

    # 第3章：弹药纪律——保守/火力压制
    if chapter_idx == 3:
        story_choice(
            ins,
            s,
            event_id="SC_AMMO_03",
            title="弹药纪律",
            text=(
                "\n装填手把剩余炮弹清点完毕：数量不够支撑‘每个路口都打一发’。"
                "炮手说：要么更谨慎地开炮，要么更果断地压制——两种都要付代价。\n"
            ),
            options={
                "1": (
                    "严格节约（更稳；获得弹药箱）",
                    lambda: (
                        s.story_flags.__setitem__("ammo_conserve", True),
                        s.buffs.__setitem__("观察", max(1, int(s.buffs.get("观察", 0) or 0))),
                        add_item(s, "弹药箱", 1),
                        setattr(s, "morale", s.morale + 1),
                    ),
                ),
                "2": (
                    "火力压制（推进更快；但更容易卷入交火）",
                    lambda: (
                        s.story_flags.__setitem__("ammo_conserve", False),
                        s.buffs.__setitem__("强行推进", max(2, int(s.buffs.get("强行推进", 0) or 0))),
                        setattr(s, "morale", s.morale - 2),
                        setattr(s, "victory_points", s.victory_points + 1),
                    ),
                ),
            },
            default=_default(),
        )

    # 第4章：伪装与噪音——隐蔽/硬闯
    if chapter_idx == 4:
        story_choice(
            ins,
            s,
            event_id="SC_CAMO_04",
            title="伪装与噪音",
            text=(
                "\n通信员提醒：敌人的观察哨越来越多。你们可以尝试做一次更‘干净’的隐蔽推进，"
                "也可以赌速度能赢过目光。\n"
            ),
            options={
                "1": (
                    "就地布置伪装（伪装+2；遭遇更少）",
                    lambda: (
                        s.story_flags.__setitem__("camo_prepared", True),
                        s.buffs.__setitem__("伪装", max(2, int(s.buffs.get("伪装", 0) or 0))),
                        _consume_fuel_with_allies(s, 1, vehicle_model="虎式坦克"),
                        setattr(s, "morale", s.morale + 1),
                    ),
                ),
                "2": (
                    "不浪费时间（强行推进；遭遇更频繁）",
                    lambda: (
                        s.story_flags.__setitem__("camo_prepared", False),
                        s.buffs.__setitem__("强行推进", max(2, int(s.buffs.get("强行推进", 0) or 0))),
                        _consume_fuel_with_allies(s, 1, vehicle_model="虎式坦克"),
                        setattr(s, "morale", s.morale - 1),
                    ),
                ),
            },
            default=_default(),
        )

    # 第5章：路口暗号——相信/忽略
    if chapter_idx == 5:
        story_choice(
            ins,
            s,
            event_id="SC_SIGNS_05",
            title="路口暗号",
            text=(
                "\n墙上有用粉笔写的箭头与符号：像是驻军留下的暗号。"
                "你必须决定是相信它，还是把它当成诱饵。\n"
            ),
            options={
                "1": (
                    "相信暗号（胜利点+1；观察+1）",
                    lambda: (
                        s.story_flags.__setitem__("trusted_signs", True),
                        setattr(s, "victory_points", s.victory_points + 1),
                        s.buffs.__setitem__("观察", max(1, int(s.buffs.get("观察", 0) or 0))),
                    ),
                ),
                "2": (
                    "忽略暗号（多走冤枉路；油耗↑）",
                    lambda: (
                        s.story_flags.__setitem__("trusted_signs", False),
                        _consume_fuel_with_allies(s, 3, vehicle_model="虎式坦克"),
                        setattr(s, "morale", s.morale - 1),
                    ),
                ),
            },
            default=_default(),
        )

    # 第6章：炮塔维护——停留/继续
    if chapter_idx == 6:
        story_choice(
            ins,
            s,
            event_id="SC_MAINT_06",
            title="炮塔维护",
            text=(
                "\n炮塔转动时发出轻微异响。驾驶员（兼机械）说：‘现在处理，代价是时间；’"
                "炮手说：‘不处理，代价可能是在交火里。’\n"
            ),
            options={
                "1": (
                    "停下做一次维护（损伤↓；稳固+2）",
                    lambda: (
                        s.story_flags.__setitem__("maint_done", True),
                        setattr(s, "damage", max(0, s.damage - 5)),
                        s.buffs.__setitem__("稳固", max(2, int(s.buffs.get("稳固", 0) or 0))),
                        setattr(s, "morale", s.morale - 1),
                    ),
                ),
                "2": (
                    "继续前进（节奏↑；但留下隐患）",
                    lambda: (
                        s.story_flags.__setitem__("maint_done", False),
                        setattr(s, "morale", s.morale + 1),
                        s.debuffs.__setitem__("turret_jam", max(1, int(s.debuffs.get("turret_jam", 0) or 0))),
                    ),
                ),
            },
            default=_default(),
        )

    # 第7章：新中尉到任——服从/坚持经验
    if chapter_idx == 7:
        story_choice(
            ins,
            s,
            event_id="SC_LIEUTENANT_07",
            title="命令与经验",
            text=(
                "\n新中尉要求你们‘按命令穿越开阔地’，理由是时间不够。"
                "炮手低声说：那条路像是给反坦克炮准备的靶场。\n"
            ),
            options={
                "1": (
                    "表面服从、暗中改道（辖区好感↑，但士气略降）",
                    lambda: (
                        s.story_flags.__setitem__("lieutenant_deceived", True),
                        setattr(s, "morale", s.morale - 2),
                        _mod_current_sector(favor=3),
                        s.buffs.__setitem__("观察", 1),
                    ),
                ),
                "2": (
                    "严格执行命令（短期风险↑，但胜利点更容易增加）",
                    lambda: (
                        s.story_flags.__setitem__("lieutenant_obeyed", True),
                        setattr(s, "victory_points", s.victory_points + 1),
                        s.buffs.__setitem__("强行推进", 2),
                    ),
                ),
            },
            default=_default(),
        )

    # 第8章：无线电截获——情报/求援
    if chapter_idx == 8:
        story_choice(
            ins,
            s,
            event_id="SC_INTERCEPT_08",
            title="断续的截获",
            text=(
                "\n通信员截获到一段断续的通话：像是对方在协调封锁线。"
                "你可以把它当作情报，也可以立刻用它去换一次求援回应。\n"
            ),
            options={
                "1": (
                    "记录情报并谨慎推进（观察+1；胜利点+1）",
                    lambda: (
                        s.story_flags.__setitem__("intel_saved", True),
                        s.buffs.__setitem__("观察", max(1, int(s.buffs.get("观察", 0) or 0))),
                        setattr(s, "victory_points", s.victory_points + 1),
                    ),
                ),
                "2": (
                    "尝试用截获频率求援（若有电台电池则消耗1；求援+1）",
                    lambda: (
                        s.story_flags.__setitem__("intel_saved", False),
                        (_try_spend_item("电台电池", 1) or True),
                        s.buffs.__setitem__("求援", max(1, int(s.buffs.get("求援", 0) or 0))),
                        setattr(s, "morale", s.morale + 1),
                    ),
                ),
            },
            default=_default(),
        )

    # 第9章：溃兵的请求——收拢/放行
    if chapter_idx == 9:
        story_choice(
            ins,
            s,
            event_id="SC_ROUTED_09",
            title="溃兵的请求",
            text=(
                "\n你们在后街拐角遇到几名溃散士兵。他们看着虎王，眼神里是希望与恐惧："
                "‘带上我们，或者至少给点弹药。’\n"
            ),
            options={
                "1": (
                    "分出弹药并指路（机枪弹-40；辖区好感+4）",
                    lambda: (
                        s.story_flags.__setitem__("aided_routed", True),
                        setattr(s, "mg_ammo", max(0, s.mg_ammo - 40)),
                        setattr(s, "morale", s.morale + 3),
                        _mod_current_sector(favor=4, fall=-1),
                    ),
                ),
                "2": (
                    "拒绝（保持资源；士气-2）",
                    lambda: (
                        s.story_flags.__setitem__("aided_routed", False),
                        setattr(s, "morale", s.morale - 2),
                    ),
                ),
            },
            default=_default(),
        )

    # 第10章：修理厂的争执——抢修/继续
    if chapter_idx == 10:
        story_choice(
            ins,
            s,
            event_id="SC_DEPOT_10",
            title="修理厂的争执",
            text=(
                "\n你们短暂回到一处修理厂残址。有人说应该立刻抢修关键部件，有人说应该趁还动得了继续走。\n"
            ),
            options={
                "1": (
                    "立刻抢修（损伤-8；获得备件x1）",
                    lambda: (
                        s.story_flags.__setitem__("depot_repaired", True),
                        setattr(s, "damage", max(0, s.damage - 8)),
                        setattr(s, "morale", s.morale - 1),
                        add_item(s, "备件", 1),
                    ),
                ),
                "2": (
                    "继续前进（强行推进+2；士气+1）",
                    lambda: (
                        s.story_flags.__setitem__("depot_repaired", False),
                        s.buffs.__setitem__("强行推进", max(2, int(s.buffs.get("强行推进", 0) or 0))),
                        setattr(s, "morale", s.morale + 1),
                    ),
                ),
            },
            default=_default(),
        )

    # 第12章：临时交易——交易/拒绝
    if chapter_idx == 12:
        story_choice(
            ins,
            s,
            event_id="SC_MARKET_12",
            title="临时交易",
            text=(
                "\n有人在废楼里摆出一张桌子：不问来历，只问你能付出什么。"
                "这像是机会，也像是陷阱。\n"
            ),
            options={
                "1": (
                    "用金条换一批必需品（金条-1；工具箱+1；药品+1）",
                    lambda: (
                        s.story_flags.__setitem__("black_market", True),
                        (_try_spend_currency(gold=1) or True),
                        add_item(s, "工具箱", 1),
                        add_item(s, "药品", 1),
                        setattr(s, "morale", s.morale - 1),
                        setattr(s, "city_collapse", s.city_collapse + 1),
                    ),
                ),
                "2": (
                    "拒绝交易（士气+2）",
                    lambda: (
                        s.story_flags.__setitem__("black_market", False),
                        setattr(s, "morale", s.morale + 2),
                    ),
                ),
            },
            default=_default(),
        )

    # 第14章：地图碎片——按线索行动/继续确认
    if chapter_idx == 14:
        story_choice(
            ins,
            s,
            event_id="SC_MAPS_14",
            title="地图碎片",
            text=(
                "\n你们把几张地图碎片摊在一起：能拼出一个方向，但缺口仍然模糊。"
                "现在就按它行动，还是继续收集更多确认？\n"
            ),
            options={
                "1": (
                    "现在就按线索行动（胜利点+1；突围情报↑）",
                    lambda: (
                        s.story_flags.__setitem__("escape_intel", True),
                        setattr(s, "victory_points", s.victory_points + 1),
                        add_item(s, "地图碎片", 1),
                    ),
                ),
                "2": (
                    "继续收集确认（观察+1；士气+1）",
                    lambda: (
                        s.story_flags.__setitem__("escape_intel", False),
                        s.buffs.__setitem__("观察", max(1, int(s.buffs.get("观察", 0) or 0))),
                        setattr(s, "morale", s.morale + 1),
                    ),
                ),
            },
            default=_default(),
        )

    # 第16章：燃油分配——自保/分给平民
    if chapter_idx == 16:
        story_choice(
            ins,
            s,
            event_id="SC_FUEL_16",
            title="最后的油料",
            text=(
                "\n你们抢到燃油，但外面有人敲着残墙喊：‘给一点，我们带着孩子走。’"
                "你知道，分出去就意味着突围更难。\n"
            ),
            options={
                "1": (
                    "分出一部分（士气↑、平民记录↑，但燃油↓）",
                    lambda: (
                        _consume_fuel_with_allies(s, 10, vehicle_model="虎式坦克"),
                        setattr(s, "morale", s.morale + 6),
                        setattr(s, "civilians_helped", s.civilians_helped + 1),
                        s.story_flags.__setitem__("shared_fuel", True),
                    ),
                ),
                "2": (
                    "全部留下（燃油↑，但士气↓）",
                    lambda: (
                        setattr(s, "fuel", min(200, s.fuel + 6)),
                        setattr(s, "morale", s.morale - 4),
                        s.story_flags.__setitem__("kept_fuel", True),
                    ),
                ),
            },
            default=_default(),
        )

    # 第18章：最后的电池——自用/分享
    if chapter_idx == 18:
        story_choice(
            ins,
            s,
            event_id="SC_BATTERY_18",
            title="最后的电池",
            text=(
                "\n你们手里还剩下不多的电池。通信员说：‘我们可以把它留给自己求援，’"
                "又有人说：‘把它交给街区电台，也许能让更多人听到撤离指引。’\n"
            ),
            options={
                "1": (
                    "留给自己（若有电台电池则消耗1；求援+1；士气+1）",
                    lambda: (
                        s.story_flags.__setitem__("battery_kept", True),
                        (_try_spend_item("电台电池", 1) or True),
                        s.buffs.__setitem__("求援", max(1, int(s.buffs.get("求援", 0) or 0))),
                        setattr(s, "morale", s.morale + 1),
                    ),
                ),
                "2": (
                    "交给街区（辖区好感+5；士气+2）",
                    lambda: (
                        s.story_flags.__setitem__("battery_shared", True),
                        _mod_current_sector(favor=5, fall=-1),
                        setattr(s, "morale", s.morale + 2),
                    ),
                ),
            },
            default=_default(),
        )

    # 第36章：是否准备自毁
    if chapter_idx == 36:
        story_choice(
            ins,
            s,
            event_id="SC_SCUTTLE_36",
            title="摧毁虎王",
            text=(
                "\n你们讨论是否预先布置自毁：如果突围失败，至少不让它落入他人之手。"
                "但自毁准备会占用时间与资源，也会让人更早承认结局。\n"
            ),
            options={
                "1": (
                    "准备自毁（后续突围失败时更容易保全乘员）",
                    lambda: (
                        s.story_flags.__setitem__("scuttle_prepared", True),
                        setattr(s, "morale", s.morale - 2),
                        setattr(s, "damage", min(100, s.damage + 2)),
                    ),
                ),
                "2": (
                    "不准备（保留资源；但失败代价更大）",
                    lambda: (
                        s.story_flags.__setitem__("scuttle_prepared", False),
                        setattr(s, "morale", s.morale + 1),
                    ),
                ),
            },
            default=_default(),
        )

    # 第19章：孤儿院救援——停下/继续
    if chapter_idx == 19:
        story_choice(
            ins,
            s,
            event_id="SC_ORPHANAGE_19",
            title="孤儿院的门",
            text=(
                "\n你们在一处半塌的院墙旁看到‘孤儿院’的牌子。里面有动静，但外面太安静。"
                "你知道：停下意味着风险，继续意味着把一个答案留在身后。\n"
            ),
            options={
                "1": (
                    "停下尝试救出孩子（士气↑、平民记录↑，但触发额外遭遇）",
                    lambda: (
                        setattr(s, "civilians_helped", s.civilians_helped + 2),
                        setattr(s, "morale", s.morale + 6),
                        setattr(s, "victory_points", s.victory_points + 2),
                        add_item(s, "急救包", 1),
                        s.story_flags.__setitem__("saved_orphans", True),
                        s.buffs.__setitem__("额外遭遇", 1),
                    ),
                ),
                "2": (
                    "继续前进（保持节奏；士气↓）",
                    lambda: (
                        setattr(s, "morale", s.morale - 3),
                        s.story_flags.__setitem__("saved_orphans", False),
                    ),
                ),
            },
            default=_default(),
        )

    # 第25章：夜间行军——隐蔽/强推
    if chapter_idx == 25:
        story_choice(
            ins,
            s,
            event_id="SC_NIGHT_25",
            title="夜间行军",
            text=(
                "\n夜色足够厚，你们可以更隐蔽地推进；也可以借黑暗做一次更冒险的强推。\n"
            ),
            options={
                "1": (
                    "隐蔽推进（伪装+2；士气+1；燃油-2）",
                    lambda: (
                        s.story_flags.__setitem__("night_stealth", True),
                        s.buffs.__setitem__("伪装", max(2, int(s.buffs.get("伪装", 0) or 0))),
                        _consume_fuel_with_allies(s, 2, vehicle_model="虎式坦克"),
                        setattr(s, "morale", s.morale + 1),
                    ),
                ),
                "2": (
                    "趁黑强推（强行推进+2；胜利点+1；士气-2）",
                    lambda: (
                        s.story_flags.__setitem__("night_stealth", False),
                        s.buffs.__setitem__("强行推进", max(2, int(s.buffs.get("强行推进", 0) or 0))),
                        setattr(s, "victory_points", s.victory_points + 1),
                        setattr(s, "morale", s.morale - 2),
                    ),
                ),
            },
            default=_default(),
        )

    # 第33章：缺口情报——确认/赌一把
    if chapter_idx == 33:
        story_choice(
            ins,
            s,
            event_id="SC_GAP_33",
            title="缺口情报",
            text=(
                "\n地图碎片与传言终于拼出一个可能的缺口。你可以再花时间确认，"
                "也可以立刻把它当成最后的路。\n"
            ),
            options={
                "1": (
                    "确认路线（突围情报↑；燃油-2；士气+1）",
                    lambda: (
                        s.story_flags.__setitem__("escape_intel", True),
                        _consume_fuel_with_allies(s, 2, vehicle_model="虎式坦克"),
                        setattr(s, "morale", s.morale + 1),
                    ),
                ),
                "2": (
                    "赌一把（强行推进+1；士气-1）",
                    lambda: (
                        s.story_flags.__setitem__("escape_intel", False),
                        s.buffs.__setitem__("强行推进", max(1, int(s.buffs.get("强行推进", 0) or 0))),
                        setattr(s, "morale", s.morale - 1),
                    ),
                ),
            },
            default=_default(),
        )

    # 第34章：乘员优先——保全/硬撑
    if chapter_idx == 34:
        story_choice(
            ins,
            s,
            event_id="SC_CREW_34",
            title="乘员优先",
            text=(
                "\n你们开始讨论‘最坏的情况’：当钢铁走不动时，什么应该被优先？"
                "这是技术问题，也是选择问题。\n"
            ),
            options={
                "1": (
                    "优先保全乘员（后续突围更稳；士气-1）",
                    lambda: (
                        s.story_flags.__setitem__("crew_first", True),
                        setattr(s, "morale", s.morale - 1),
                        s.buffs.__setitem__("观察", max(1, int(s.buffs.get("观察", 0) or 0))),
                    ),
                ),
                "2": (
                    "硬撑到底（胜利点+1；士气-1）",
                    lambda: (
                        s.story_flags.__setitem__("crew_first", False),
                        setattr(s, "victory_points", s.victory_points + 1),
                        setattr(s, "morale", s.morale - 1),
                    ),
                ),
            },
            default=_default(),
        )

    # 第38章：最后的补给——换取/保留
    if chapter_idx == 38:
        story_choice(
            ins,
            s,
            event_id="SC_LAST_SUPPLY_38",
            title="最后的补给",
            text=(
                "\n有人告诉你：附近还能换到一小批补给，但要付出你最后的货币。"
                "你可以把它当成突围前最后一次押注，也可以把它留作更不可预测的时刻。\n"
            ),
            options={
                "1": (
                    "立刻换取补给（优先消耗金条1，否则消耗通行证1；药品+1；烟幕弹+1）",
                    lambda: (
                        s.story_flags.__setitem__("last_supply", True),
                        (_try_spend_currency(gold=1) or _try_spend_currency(passes=1) or True),
                        add_item(s, "药品", 1),
                        add_item(s, "烟幕弹", 1),
                        setattr(s, "morale", s.morale + 1),
                    ),
                ),
                "2": (
                    "保留货币（士气+1；观察+1）",
                    lambda: (
                        s.story_flags.__setitem__("last_supply", False),
                        setattr(s, "morale", s.morale + 1),
                        s.buffs.__setitem__("观察", max(1, int(s.buffs.get("观察", 0) or 0))),
                    ),
                ),
            },
            default=_default(),
        )

    # 第39章：最后的命令——保全/推进
    if chapter_idx == 39:
        story_choice(
            ins,
            s,
            event_id="SC_LAST_ORDER_39",
            title="最后的命令",
            text=(
                "\n你知道真正的选择已经不多：保全乘员，还是把钢铁的速度压到极限。\n"
            ),
            options={
                "1": (
                    "保全乘员（观察+1；士气+2）",
                    lambda: (
                        s.story_flags.__setitem__("crew_first", True),
                        setattr(s, "morale", s.morale + 2),
                        s.buffs.__setitem__("观察", max(1, int(s.buffs.get("观察", 0) or 0))),
                    ),
                ),
                "2": (
                    "推进到底（强行推进+2；士气-2）",
                    lambda: (
                        s.story_flags.__setitem__("crew_first", False),
                        s.buffs.__setitem__("强行推进", max(2, int(s.buffs.get("强行推进", 0) or 0))),
                        setattr(s, "morale", s.morale - 2),
                    ),
                ),
            },
            default=_default(),
        )

    # 第22章：装甲支援的承诺——接纳/婉拒
    if chapter_idx == 22:
        story_choice(
            ins,
            s,
            event_id="SC_ALLY_22",
            title="装甲支援的承诺",
            text=(
                "\n一支友军小队带来口信：附近有辆还能动的装甲车，愿意在你们需要时靠拢。"
                "接纳意味着要协调路线与节奏；婉拒意味着独自承担风险。\n"
            ),
            options={
                "1": (
                    "接纳并约定信号（获得一次友军坦克支援；若无法编入则通行证+1）",
                    lambda: (
                        s.story_flags.__setitem__("accepted_armor_help", True),
                        (_try_grant_tank_support() or setattr(s, "passes", s.passes + 1)),
                        setattr(s, "morale", s.morale + 1),
                    ),
                ),
                "2": (
                    "婉拒（观察+1；士气+1）",
                    lambda: (
                        s.story_flags.__setitem__("accepted_armor_help", False),
                        s.buffs.__setitem__("观察", max(1, int(s.buffs.get("观察", 0) or 0))),
                        setattr(s, "morale", s.morale + 1),
                    ),
                ),
            },
            default=_default(),
        )

    # 第35章：突围会议——路线选择（影响后续遭遇与突围成功率）
    if chapter_idx == 35:
        story_choice(
            ins,
            s,
            event_id="SC_BREAKOUT_ROUTE_35",
            title="地下突围会议",
            text=(
                "\n会议里给出三条可能的路线：地铁隧道、桥梁正面、公园边缘。"
                "每条都不安全，但每条都代表一种你愿意承担的代价。\n"
            ),
            options={
                "1": (
                    "地铁隧道：更隐蔽（突围成功↑，但燃油消耗↑）",
                    lambda: (
                        s.story_vars.__setitem__("breakout_route", 1),
                        _consume_fuel_with_allies(s, 4, vehicle_model="虎式坦克"),
                        s.buffs.__setitem__("观察", 1),
                    ),
                ),
                "2": (
                    "桥梁正面：更直接（胜利点↑，但遭遇更频繁）",
                    lambda: (
                        s.story_vars.__setitem__("breakout_route", 2),
                        setattr(s, "victory_points", s.victory_points + 1),
                        s.buffs.__setitem__("强行推进", 2),
                    ),
                ),
                "3": (
                    "公园边缘：可机动（撤离更容易，但士气波动）",
                    lambda: (
                        s.story_vars.__setitem__("breakout_route", 3),
                        setattr(s, "morale", s.morale + 1),
                        add_item(s, "烟幕弹", 1),
                    ),
                ),
            },
            default="2" if ins.default_when_empty else "1",
        )



def _configure_stdio_utf8() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def is_selftest() -> bool:
    return any(arg in ("selftest", "--selftest") for arg in sys.argv[1:]) or os.environ.get("SELFTEST") == "1"


SELFTEST = is_selftest()

# 自检辅助：为用例提供“强制触发”钩子（不影响非自检运行）
SELFTEST_CONTEXT: Dict[str, object] = {}


def _selftest_reset_context(flags: Optional[Dict[str, object]] = None) -> None:
    if not SELFTEST:
        return
    SELFTEST_CONTEXT.clear()
    if flags:
        SELFTEST_CONTEXT.update(flags)


def _selftest_get(key: str, default: object = None) -> object:
    return SELFTEST_CONTEXT.get(key, default)


def _selftest_pop(key: str, default: object = None) -> object:
    try:
        return SELFTEST_CONTEXT.pop(key)
    except KeyError:
        return default


class RestartGame(Exception):
    pass


class InputStream:
    def __init__(self, scripted: Optional[List[str]] = None, *, default_when_empty: bool = False):
        self.scripted = list(scripted or [])
        self.default_when_empty = default_when_empty

    def input(self, prompt: str) -> str:
        if self.scripted:
            v = self.scripted.pop(0)
            print(prompt, end="")
            print(v)
            return v
        if self.default_when_empty:
            print(prompt, end="")
            print("")
            return ""
        try:
            return input(prompt)
        except EOFError:
            # 非交互环境或输入流被关闭：按“退出”处理
            raise SystemExit(0)
        except KeyboardInterrupt:
            # 允许 Ctrl+C 作为安全退出
            print("\n已中断。")
            raise SystemExit(0)


def get_valid_input(
    ins: InputStream,
    prompt: str,
    *,
    valid: Optional[Callable[[str], bool]] = None,
    default: Optional[str] = None,
    allow_quit: bool = True,
    allow_restart: bool = True,
) -> str:
    while True:
        s = ins.input(prompt).strip()
        if s == "" and default is not None:
            return default
        lowered = s.lower()
        if allow_quit and lowered in {"q", "quit", "退出"}:
            raise SystemExit(0)
        if allow_restart and lowered in {"r", "restart", "重开"}:
            raise RestartGame()
        if valid is None or valid(s):
            return s
        print("输入无效，请重试。")


def choose(
    ins: InputStream,
    prompt: str,
    options: Dict[str, str],
    *,
    default: Optional[str] = None,
    allow_quit: bool = True,
    allow_restart: bool = True,
) -> str:
    keys = list(options.keys())

    def _valid(s: str) -> bool:
        lowered = s.lower()
        if allow_quit and lowered in {"q", "quit", "退出"}:
            return True
        if allow_restart and lowered in {"r", "restart", "重开"}:
            return True
        return lowered in keys or lowered == "突围" or lowered == "突围"

    for k, v in options.items():
        print(f"{k}. {v}")

    s = get_valid_input(ins, prompt, valid=_valid, default=default, allow_quit=allow_quit, allow_restart=allow_restart)
    lowered = s.lower()
    if allow_quit and lowered in {"q", "quit", "退出"}:
        raise SystemExit(0)
    if allow_restart and lowered in {"r", "restart", "重开"}:
        raise RestartGame()
    return lowered


def banner() -> None:
    print("=" * 70)
    print(f"《柏林1945：虎王车长》（系统版）  build:{BUILD_STAMP}")
    print("提示：输入 q 退出；输入 r 重开。")
    print("=" * 70)


def _grant_initial_support_if_missing(s: "GameState") -> None:
    """兜底：若本局尚未发放过开局支援且当前没有装甲友军，则补发一次。

    说明：
    - 新开局会走 build_state() 调用；
    - 读取旧存档时，如果回合数很早且 tank_allies 为空，也会补发，避免“没有触发”。
    """
    try:
        counters = getattr(s, "counters", None)
        if counters is None:
            return
        if int(counters.get("initial_support_granted", 0) or 0) > 0:
            return
    except Exception:
        return

    try:
        if not hasattr(s, "tank_allies") or getattr(s, "tank_allies", None) is None:
            s.tank_allies = []
    except Exception:
        return

    try:
        if len(getattr(s, "tank_allies", []) or []) > 0:
            s.counters["initial_support_granted"] = 1
            return
    except Exception:
        pass

    try:
        # 地形信息来自地图元数据，不从 SectorState 读取（SectorState 不保证有 terrain 字段）
        terrain0 = MAP_META.get(getattr(s, "location_key", ""), {}).get("terrain")

        # 两辆 Sd.Kfz.251
        for _ in range(2):
            name = f"Sd.Kfz.251-{s.rng.randint(11,99)}"
            t = TankAlly(
                name=name,
                model="Sd.Kfz.251装甲运兵车",
                hp=90 + s.rng.randint(0, 30),
                armor=10 + s.rng.randint(0, 8),
                accuracy=56 + s.rng.randint(0, 8),
                morale=48 + s.rng.randint(0, 12),
            )
            _randomize_tank_ally_supplies(s, t)
            # 需求：开局单位油料弹药加满
            try:
                setattr(t, "fuel", 200)
                setattr(t, "shells", 30)
            except Exception:
                pass
            t.clamp()
            try:
                setattr(t, "_joined_round", int(getattr(s, "round_number", 0) or 0))
            except Exception:
                pass
            s.tank_allies.append(t)
            print(
                f"\n🚩 开局支援：{t.name} 已加入（燃油{int(getattr(t, 'fuel', 0) or 0)}，弹药{int(getattr(t, 'shells', 0) or 0)}）"
            )

        # 一辆虎式坦克
        name = f"虎式坦克-{s.rng.randint(11,99)}"
        tiger = TankAlly(
            name=name,
            model="虎式坦克",
            hp=160 + s.rng.randint(0, 30),
            armor=110 + s.rng.randint(0, 30),
            accuracy=58 + s.rng.randint(0, 8),
            morale=58 + s.rng.randint(0, 18),
        )
        _randomize_tank_ally_supplies(s, tiger)
        # 需求：开局单位油料弹药加满
        try:
            setattr(tiger, "fuel", 200)
            setattr(tiger, "shells", 30)
        except Exception:
            pass
        tiger.clamp()
        try:
            setattr(tiger, "_joined_round", int(getattr(s, "round_number", 0) or 0))
        except Exception:
            pass
        s.tank_allies.append(tiger)
        print(
            f"\n🚩 开局支援：{tiger.name} 已加入（燃油{int(getattr(tiger, 'fuel', 0) or 0)}，弹药{int(getattr(tiger, 'shells', 0) or 0)}）"
        )

        # 两队党卫军作为开局驻军（加入 deployed_garrison）
        for i in range(2):
            unit = _make_garrison_unit(s.rng, terrain=terrain0, force_type="党卫军")
            s.deployed_garrison.append((s.location_key, unit))
            print(f"\n🚩 开局支援：党卫军增援 {unit.name} 已就位。")

        # 适度提升士气
        s.morale = min(MORALE_MAX, int(s.morale) + 4)

        s.counters["initial_support_granted"] = 1
        s.clamp()
    except Exception as e:
        try:
            print(f"\n⚠️ 开局支援注入失败：{e}")
        except Exception:
            pass


def _rng_from_env() -> random.Random:
    seed_env = os.environ.get("SEED")
    if seed_env is None:
        return random.Random()
    try:
        return random.Random(int(seed_env))
    except ValueError:
        return random.Random(seed_env)


MEDALS: List[Tuple[int, str]] = [
    (0, "无勋章"),
    (6, "铁十字二级"),
    # 提高“铁十字一级”及以上勋章门槛（更偏长期战果）
    (60, "铁十字一级"),
    (120, "骑士铁十字"),
    # 骑士铁十字之后显著更难获得（提升阈值）
    (200, "橡叶骑士铁十字"),
    (300, "橡叶与剑骑士铁十字"),
    (400, "橡叶、剑与钻石骑士铁十字"),
    (420, "大铁十字"),
]


ACHIEVEMENTS: List[Dict[str, object]] = [
    {
        "id": "tank_ring_1",
        "title": "坦克击毁环·I",
        "desc": "击毁2辆敌军装甲目标（坦克/自走炮等）。",
        "counter": "enemy_tank_kills",
        "target": 2,
    },
    {
        "id": "tank_ring_2",
        "title": "坦克击毁环·II",
        "desc": "累计击毁5辆敌军装甲目标。",
        "counter": "enemy_tank_kills",
        "target": 5,
    },
    {
        "id": "tank_ring_3",
        "title": "坦克击毁环·III",
        "desc": "累计击毁8辆敌军装甲目标。",
        "counter": "enemy_tank_kills",
        "target": 8,
    },
    {
        "id": "tank_ring_4",
        "title": "坦克击毁环·IV",
        "desc": "累计击毁12辆敌军装甲目标。",
        "counter": "enemy_tank_kills",
        "target": 12,
    },
    {
        "id": "tank_ring_5",
        "title": "坦克击毁环·V",
        "desc": "累计击毁18辆敌军装甲目标。",
        "counter": "enemy_tank_kills",
        "target": 18,
    },
    {
        "id": "is2_hunter",
        "title": "巨兽终结者",
        "desc": "击毁1辆IS-2。",
        "counter": "enemy_is2_kills",
        "target": 1,
    },
    {
        "id": "is2_hunter_2",
        "title": "巨兽终结者·II",
        "desc": "累计击毁3辆IS-2。",
        "counter": "enemy_is2_kills",
        "target": 3,
    },
    {
        "id": "enemy_sweeper_10",
        "title": "巷战清扫",
        "desc": "累计击毁15个敌方目标。",
        "counter": "enemy_kills",
        "target": 15,
    },
    {
        "id": "enemy_sweeper_25",
        "title": "巷战清扫·II",
        "desc": "累计击毁40个敌方目标。",
        "counter": "enemy_kills",
        "target": 40,
    },
    {
        "id": "gun_silencer",
        "title": "火炮哑火",
        "desc": "累计摧毁4个反坦克炮阵地。",
        "counter": "enemy_at_gun_kills",
        "target": 4,
    },
    {
        "id": "gun_silencer_2",
        "title": "火炮哑火·II",
        "desc": "累计摧毁8个反坦克炮阵地。",
        "counter": "enemy_at_gun_kills",
        "target": 8,
    },
    {
        "id": "flawless_1",
        "title": "零损失·一场",
        "desc": "赢下一场遭遇战且无乘员/友军伤亡。",
        "counter": "flawless_clears",
        "target": 1,
    },
    {
        "id": "flawless_3",
        "title": "零损失·三场",
        "desc": "累计4场零损失胜利。",
        "counter": "flawless_clears",
        "target": 4,
    },
    {
        "id": "wins_1",
        "title": "脱离接触",
        "desc": "赢下2场遭遇战（战斗清空敌人并撤离）。",
        "counter": "wins",
        "target": 2,
    },
    {
        "id": "wins_5",
        "title": "市区幸存者",
        "desc": "累计赢下8场遭遇战。",
        "counter": "wins",
        "target": 8,
    },
    {
        "id": "explore_3",
        "title": "路标·I",
        "desc": "移动/探索5次。",
        "counter": "explore",
        "target": 5,
    },
    {
        "id": "explore_8",
        "title": "路标·II",
        "desc": "移动/探索12次。",
        "counter": "explore",
        "target": 12,
    },
    {
        "id": "scavenge_3",
        "title": "拾荒者",
        "desc": "搜索补给5次。",
        "counter": "scavenge",
        "target": 5,
    },
    {
        "id": "scavenge_8",
        "title": "废墟仓库",
        "desc": "搜索补给12次。",
        "counter": "scavenge",
        "target": 12,
    },
    {
        "id": "assist_3",
        "title": "撤离护航·I",
        "desc": "支援撤离4次。",
        "counter": "assist",
        "target": 4,
    },
    {
        "id": "assist_7",
        "title": "撤离护航·II",
        "desc": "支援撤离10次。",
        "counter": "assist",
        "target": 10,
    },
    {
        "id": "repairs_3",
        "title": "车间常客·I",
        "desc": "进行修理/维护4次。",
        "counter": "repairs",
        "target": 4,
    },
    {
        "id": "repairs_8",
        "title": "车间常客·II",
        "desc": "进行修理/维护12次。",
        "counter": "repairs",
        "target": 12,
    },
    {
        "id": "rest_3",
        "title": "喘息时刻",
        "desc": "休整5次。",
        "counter": "rest",
        "target": 5,
    },
    {
        "id": "fatigue_80",
        "title": "强行军",
        "desc": "疲劳达到90（过度机动会显著影响战斗表现）。",
        "counter": "fatigue",
        "target": 90,
    },
    {
        "id": "explored_5",
        "title": "地形熟悉·I",
        "desc": "探索过7处不同地点。",
        "special": "explored",
        "target": 7,
    },
    {
        "id": "explored_10",
        "title": "地形熟悉·II",
        "desc": "探索过12处不同地点。",
        "special": "explored",
        "target": 12,
    },
    {
        "id": "map_frag_3",
        "title": "缺口线索",
        "desc": "收集5张地图碎片。",
        "inventory": "地图碎片",
        "target": 5,
    },
    {
        "id": "toolbox_1",
        "title": "工具到手",
        "desc": "获得1个工具箱。",
        "inventory": "工具箱",
        "target": 1,
    },
    {
        "id": "civilians_2",
        "title": "护送撤离",
        "desc": "帮助平民/伤员撤离3次。",
        "attr": "civilians_helped",
        "target": 3,
    },
    {
        "id": "crew_saved_5",
        "title": "把人带回来",
        "desc": "累计救回7名战友。",
        "attr": "crew_saved",
        "target": 7,
    },
    {
        "id": "gold_5",
        "title": "硬通货",
        "desc": "持有8根金条。",
        "attr": "gold_bars",
        "target": 8,
    },
]


def get_rank(points: int) -> str:
    # 返回基于胜利点的“勋章”称号（保持旧调用接口不变）
    for threshold, medal in reversed(MEDALS):
        if points >= threshold:
            return medal
    return MEDALS[0][1]


def _ach_counter(s: "GameState", key: str) -> int:
    try:
        return int(s.counters.get(key, 0))
    except Exception:
        return 0


def _ach_progress(s: "GameState", a: Dict[str, object]) -> int:
    """计算成就进度。

    兼容旧版：默认从 s.counters 读取；
    扩展：
    - attr: 读取 GameState 的数值字段
    - inventory: 读取背包物品数量
    - special: 读取派生值（如 explored 个数）
    """
    try:
        counter = str(a.get("counter", "") or "")
        if counter:
            return _ach_counter(s, counter)
    except Exception:
        pass

    try:
        attr = str(a.get("attr", "") or "")
        if attr:
            return int(getattr(s, attr, 0) or 0)
    except Exception:
        pass

    try:
        inv = str(a.get("inventory", "") or "")
        if inv:
            bag = getattr(s, "inventory", {}) or {}
            if isinstance(bag, dict):
                return int(bag.get(inv, 0) or 0)
    except Exception:
        pass

    try:
        sp = str(a.get("special", "") or "")
        if sp == "explored":
            return len(getattr(s, "explored", set()) or set())
    except Exception:
        pass

    return 0


def check_and_unlock_achievements(s: "GameState") -> List[str]:
    """检查并解锁成就，返回本次新解锁的成就标题列表。"""
    newly: List[str] = []
    if not hasattr(s, "achievements") or s.achievements is None:
        s.achievements = set()

    for a in ACHIEVEMENTS:
        aid = str(a.get("id", ""))
        if not aid or aid in s.achievements:
            continue
        counter = str(a.get("counter", ""))
        target = int(a.get("target", 0) or 0)
        if target <= 0:
            continue
        if _ach_progress(s, a) >= target:
            s.achievements.add(aid)
            newly.append(str(a.get("title", aid)))
    return newly


def show_achievements(s: "GameState") -> None:
    print("成就：")
    if not getattr(s, "achievements", None):
        print("- 暂无（行动、战斗、支援、探索、收集等都可能解锁）")
    for a in ACHIEVEMENTS:
        aid = str(a.get("id", ""))
        title = str(a.get("title", ""))
        desc = str(a.get("desc", ""))
        target = int(a.get("target", 0) or 0)
        cur = _ach_progress(s, a)
        state = "已解锁" if (getattr(s, "achievements", set()) and aid in s.achievements) else f"{cur}/{target}"
        print(f"- {title}：{state}（{desc}）")


DIFFICULTY = {
    "1": {
        "name": "公路旅行",
        "start": {"fuel": 200, "ammo": 40, "morale": 68, "damage": 4},
        "danger": 0.75,
    },
    "2": {
        "name": "1945",
        "start": {"fuel": 120, "ammo": 40, "morale": 65, "damage": 6},
        "danger": 0.85,
    },
    "3": {
        "name": "回到苏军总部",
        "start": {"fuel": 90, "ammo": 38, "morale": 60, "damage": 10},
        "danger": 0.95,
    },
    "突围": {
        "name": "突围",
        "start": {"fuel": 150, "ammo": 35, "morale": 70, "damage": 8},
        "danger": 1.0,
    },
    "4": {
        "name": "自定义",
        # start/danger 仅用于兜底；真正数值会从 GameState.custom_difficulty 读取
        "start": {"fuel": 120, "ammo": 40, "morale": 65, "damage": 6},
        "danger": 0.85,
    },
}


def _clamp_int(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        x = int(value)
    except Exception:
        x = int(default)
    return max(int(lo), min(int(hi), x))


def _clamp_float(value: Any, lo: float, hi: float, default: float) -> float:
    try:
        x = float(value)
    except Exception:
        x = float(default)
    if math.isnan(x) or math.isinf(x):
        x = float(default)
    return max(float(lo), min(float(hi), x))


def state_difficulty_preset(s: "GameState") -> Dict[str, Any]:
    """从 GameState 获取难度预设（支持自定义）。

    约定：difficulty_key == "4" 且 custom_difficulty 是 dict 时，优先使用其 danger/start。
    旧存档没有 custom_difficulty 字段时会自动回退到 DIFFICULTY。
    """
    key = str(getattr(s, "difficulty_key", "2") or "2")
    base = dict(DIFFICULTY.get(key) or DIFFICULTY.get("2") or {})
    if key != "4":
        return base

    cd = getattr(s, "custom_difficulty", None)
    if not isinstance(cd, dict):
        return base

    start0 = base.get("start") if isinstance(base.get("start"), dict) else {}
    start = dict(start0)
    cd_start = cd.get("start")
    if isinstance(cd_start, dict):
        start.update(cd_start)

    danger = cd.get("danger", base.get("danger", 0.85))
    base["start"] = start
    base["danger"] = danger
    base["name"] = "自定义"
    return base


def state_danger(s: "GameState") -> float:
    preset = state_difficulty_preset(s)
    return _clamp_float(preset.get("danger", 0.85), 0.50, 1.20, 0.85)


def _prompt_custom_difficulty(ins: "InputStream") -> Dict[str, Any]:
    """交互式构建自定义难度配置，返回可写入 GameState.custom_difficulty 的 dict。"""
    base_key = choose(
        ins,
        "自定义难度：选择一个基础模板(1-3)：",
        {
            "1": "公路旅行（资源多、遭遇少）",
            "2": "标准（推荐）",
            "3": "绝境（资源少、遭遇多）",
        },
        default="2",
    )
    base = DIFFICULTY.get(base_key, DIFFICULTY["2"])
    start = base.get("start") if isinstance(base.get("start"), dict) else {}

    # 说明：这里尽量只做“难度相关核心数值”的自定义，不引入额外页面/系统。
    fuel = get_valid_input(ins, "开局燃油(0-200)：", default=str(start.get("fuel", 120)))
    morale = get_valid_input(ins, f"开局士气(0-{MORALE_MAX})：", default=str(start.get("morale", 65)))
    damage = get_valid_input(ins, "开局损伤(0-100)：", default=str(start.get("damage", 6)))

    # 弹药：用更直观的 AP/HE 分开输入（各 0-40），避免只给总数不好分配。
    ap_shells = get_valid_input(ins, "开局AP炮弹(0-40)：", default="22")
    he_shells = get_valid_input(ins, "开局HE炮弹(0-40)：", default="18")
    mg_ammo = get_valid_input(ins, "开局机枪弹(0-400)：", default=str({"1": 200, "2": 170, "3": 140}.get(base_key, 170)))
    base_armor = get_valid_input(ins, "我方基础装甲(0-160)：", default=str({"1": 112, "2": 105, "3": 98}.get(base_key, 105)))

    danger = get_valid_input(ins, "遭遇强度倍率danger(0.50-1.20，越高越危险)：", default=str(base.get("danger", 0.85)))

    cfg = {
        "base_key": str(base_key),
        "danger": _clamp_float(danger, 0.50, 1.20, float(base.get("danger", 0.85))),
        "start": {
            "fuel": _clamp_int(fuel, 0, 200, int(start.get("fuel", 120))),
            "morale": _clamp_int(morale, 0, MORALE_MAX, int(start.get("morale", 65))),
            "damage": _clamp_int(damage, 0, 100, int(start.get("damage", 6))),
            "ap_shells": _clamp_int(ap_shells, 0, 40, 22),
            "he_shells": _clamp_int(he_shells, 0, 40, 18),
            "mg_ammo": _clamp_int(mg_ammo, 0, 400, int({"1": 200, "2": 170, "3": 140}.get(base_key, 170))),
            "base_armor": _clamp_int(base_armor, 0, 160, int({"1": 112, "2": 105, "3": 98}.get(base_key, 105))),
        },
    }
    return cfg


# --- 战斗：装甲/空中支援/友军坦克（可调参数）
# 装甲减伤：按“装甲值→比例减伤”计算，对反坦克与火炮类结构损伤生效。
ARMOR_PLATE_BONUS = 10
ARMOR_PLATE_MAX = 4
PLAYER_ARMOR_REDUCTION_SCALE = 55.0
PLAYER_ARMOR_MAX_REDUCTION = 0.72

# 士气：提高上限，并减少单次波动对整体的影响
MORALE_MAX = 200

# 燃油耗尽宽限：燃油为 0 后仍可继续若干回合。
# 计数在每个回合开始时 +1；当连续空油回合数达到 (宽限+1) 时触发结局。
FUEL_EMPTY_GRACE_ROUNDS = 3

# 士气崩溃宽限：士气为 0 后仍可继续若干回合。
MORALE_ZERO_GRACE_ROUNDS = 3

# 城市崩溃宽限：崩溃度到 100 后仍可继续若干回合。
CITY_COLLAPSE_GRACE_ROUNDS = 3

# --- 天气系统（轻量）
# 说明：不引入新菜单/新页面，仅提供“当前天气显示 + 回合推进时变化”，并对
# - 移动耗油
# - 遭遇概率
# - 战斗命中
# 做小幅修正。
WEATHER_TABLE: Dict[str, Dict[str, object]] = {
    # move_mult: 移动耗油倍率
    # encounter_delta: 对 maybe_trigger_event() 的 base 概率做加法修正（-0.1~+0.1 级别）
    # player_hit_delta/enemy_hit_delta: 命中修正（整数，最终仍会被上下限夹紧）
    "clear": {"name": "晴", "move_mult": 1.00, "encounter_delta": 0.02, "player_hit_delta": 2, "enemy_hit_delta": 2},
    "cloudy": {"name": "阴", "move_mult": 1.00, "encounter_delta": 0.00, "player_hit_delta": 0, "enemy_hit_delta": 0},
    "rain": {"name": "雨", "move_mult": 1.15, "encounter_delta": -0.02, "player_hit_delta": -6, "enemy_hit_delta": -6},
    "fog": {"name": "雾", "move_mult": 1.05, "encounter_delta": -0.06, "player_hit_delta": -10, "enemy_hit_delta": -10},
}

# 空中支援：战斗中可能出现雅克3或伊尔2；可用机枪对空射击驱离
AIR_SUPPORT_SPAWN_CHANCE = 0.18
AIR_SUPPORT_SPAWN_CHANCE_BOSS = 0.28
AA_MG_COST = 12
MG_FIRE_COST = 9


ALLY_TANK_FREE_SUPPORT_MIN = 6
ALLY_TANK_FREE_SUPPORT_MAX = 10

# 主炮弹种切换的基础冷却（回合数）——用于更精确的换弹节奏历史化
MAIN_GUN_SHELL_SWITCH_BASE = {"AP": 1, "HE": 2}

# 远程炮击/空袭的最小全局冷却（以遭遇/回合计数衡量），以避免高频骚扰
OFFMAP_ARTY_COOLDOWN_DEFAULT = 3
AIR_SUPPORT_COOLDOWN_DEFAULT = 6

# 命中/故障（可调参数）：更高命中、更低故障
PLAYER_BASE_HIT_CHANCE = 76
PLAYER_HIT_CHANCE_MIN = 15
PLAYER_HIT_CHANCE_MAX = 92

# 玩家主炮伤害（平衡）：按游戏内抽象刻度，不对应真实毫米/战果
# 注：AP 伤害被大幅提升，以体现“穿甲弹一发致残/致毁”的爽感。
PLAYER_AP_DAMAGE_SOFT_RANGE = (220, 360)   # 对非装甲/轻目标（掩体/火力点等）
PLAYER_AP_DAMAGE_PEN_RANGE = (280, 460)    # 贯穿后的主要伤害
PLAYER_AP_DAMAGE_PEN_HEAVY_MULT = 0.95     # 对重装甲隔舱吸收（仍然很痛）

# --- 敌方火力节奏（平衡）
# IS-2 主炮射击冷却：开火后需要等待 N 个战斗回合。
IS2_MAIN_GUN_COOLDOWN_TURNS = 3

MG_JAM_CHANCE_LOW_AMMO = 0.09   # mg_ammo <= 12
MG_JAM_CHANCE_MED_AMMO = 0.05   # mg_ammo <= 24
MG_JAM_FATIGUE_BONUS = 0.02     # fatigue >= 60
MG_JAM_CHANCE_CAP = 0.15

GUN_BREECH_BLOCK_CHANCE = 0.16  # gun_breech debuff 时本回合无法开火的概率
LOADER_WOUNDED_DELAY_CHANCE = 0.25
LOADER_STRESS_DELAY_CHANCE = 0.18

ENEMY_MODULE_FAULT_BASE = 0.04
ENEMY_MODULE_FAULT_INTENSITY_DIV = 180.0
ENEMY_MODULE_FAULT_INTENSITY_CAP = 0.14
ENEMY_MODULE_FAULT_AT_BONUS = 0.04
ENEMY_MODULE_FAULT_MIN = 0.02
ENEMY_MODULE_FAULT_MAX = 0.22


def player_armor_rating(s: 'GameState') -> int:
    base = int(getattr(s, 'base_armor', 0) or 0)
    plates = int(getattr(s, 'armor_plates', 0) or 0)
    return max(0, min(200, base + plates * ARMOR_PLATE_BONUS))



LOCATIONS = {
    "1": {"name": "米特街区", "risk": 0.55},
    "2": {"name": "蒂尔加滕边缘", "risk": 0.45},
    "3": {"name": "动物园高架桥附近", "risk": 0.6},
    "4": {"name": "工业区残段", "risk": 0.5},
    "5": {"name": "地铁入口与瓦砾带", "risk": 0.65},
    "6": {"name": "勃兰登堡门废墟", "risk": 0.7},
    "7": {"name": "亚历山大广场", "risk": 0.6},
    "8": {"name": "柏林大教堂周边", "risk": 0.55},
    "9": {"name": "夏洛滕堡宫废墟", "risk": 0.4},
    "10": {"name": "斯潘道区残垣", "risk": 0.5},
    # 新增地图（继续分别丰富：额外30处）
    "11": {"name": "波茨坦广场废墟", "risk": 0.65},
    "12": {"name": "国会大厦外围", "risk": 0.72},
    "13": {"name": "施普雷河堤岸", "risk": 0.52},
    "14": {"name": "弗里德里希大街断口", "risk": 0.6},
    "15": {"name": "安哈尔特车站遗址", "risk": 0.58},
    "16": {"name": "施普雷河桥头", "risk": 0.62},
    "17": {"name": "东站货场残区", "risk": 0.56},
    "18": {"name": "西区公寓废墟群", "risk": 0.48},
    "19": {"name": "犹太区巷道", "risk": 0.57},
    "20": {"name": "旧市政厅周边", "risk": 0.63},
    "21": {"name": "河畔仓库带", "risk": 0.54},
    "22": {"name": "医院废楼", "risk": 0.5},
    "23": {"name": "消防局旧址", "risk": 0.46},
    "24": {"name": "广播电台大楼外", "risk": 0.66},
    "25": {"name": "电话局地下通道", "risk": 0.68},
    "26": {"name": "运河闸门附近", "risk": 0.53},
    "27": {"name": "动物园后街", "risk": 0.51},
    "28": {"name": "国立图书馆外墙", "risk": 0.55},
    "29": {"name": "大学区废墟", "risk": 0.49},
    "30": {"name": "兵营残墙", "risk": 0.61},
    "31": {"name": "水厂与管线区", "risk": 0.47},
    "32": {"name": "旧火车调车场", "risk": 0.59},
    "33": {"name": "玻璃温室残架", "risk": 0.44},
    "34": {"name": "工人新村街口", "risk": 0.5},
    "35": {"name": "郊外林带边缘", "risk": 0.41},
    "36": {"name": "农田沟渠路", "risk": 0.45},
    "37": {"name": "工坊与修理棚", "risk": 0.52},
    "38": {"name": "砖窑旧址", "risk": 0.43},
    "39": {"name": "小型桥涵", "risk": 0.48},
    "40": {"name": "废弃营地", "risk": 0.57},
    "41": {"name": "郊外公路断桥", "risk": 0.46},
    "42": {"name": "林间检查点", "risk": 0.5},
    "43": {"name": "河堤缺口", "risk": 0.44},
    "44": {"name": "废弃修理厂", "risk": 0.49},
    "45": {"name": "临时难民营", "risk": 0.41},
    "46": {"name": "小镇外缘", "risk": 0.38},
    "47": {"name": "铁路堤旁", "risk": 0.47},
    "48": {"name": "农舍与谷仓", "risk": 0.42},
    "49": {"name": "炮兵阵地残址", "risk": 0.55},
    "50": {"name": "城外林道出口", "risk": 0.36},

    # 继续扩充地图：更丰富的市区细部与郊外节点（51-80）
    "51": {"name": "动物园防空塔残迹", "risk": 0.68, "desc": "巨大的混凝土残体像一座黑色山丘，视野很好，但也更容易被盯上。", "tags": ["制高点", "残骸", "炮火观察"]},
    "52": {"name": "废弃歌剧院", "risk": 0.62, "desc": "大厅穹顶塌了一半，舞台后方通向一串狭窄的后勤通道。", "tags": ["回声", "狭窄入口", "可伏击"]},
    "53": {"name": "破损电力站", "risk": 0.60, "desc": "碎裂的绝缘瓷与焦黑电缆散落一地，靠近会让人紧张。", "tags": ["工业残段", "电缆", "掩体少"]},
    "54": {"name": "下水道枢纽", "risk": 0.70, "desc": "污水与蒸汽交织，岔路多、声音复杂，适合躲藏也适合埋伏。", "tags": ["地下", "岔路", "能见度低"]},
    "55": {"name": "地铁维修隧道", "risk": 0.66, "desc": "维修门后是一条长直通道，脚步声会被放大，前进要非常谨慎。", "tags": ["隧道", "回音", "可绕行"]},
    "56": {"name": "临时指挥所地下室", "risk": 0.72, "desc": "墙上还留着标记与电台桌，像是仓促撤离后的空壳。", "tags": ["情报", "遗留文件", "疑似陷阱"]},
    "57": {"name": "废弃警署", "risk": 0.58, "desc": "铁门歪斜，走廊里碎玻璃遍地，隔间很多。", "tags": ["隔间", "近距离", "视线阻断"]},
    "58": {"name": "市集残摊", "risk": 0.55, "desc": "摊棚木梁折断，散落的箱笼像迷宫一样把道路切碎。", "tags": ["杂物堆", "掩体多", "行动迟缓"]},
    "59": {"name": "钟楼狙击点", "risk": 0.64, "desc": "楼梯间狭窄而陡，顶部能看见大片街区。", "tags": ["制高点", "狙击", "易暴露"]},
    "60": {"name": "施普雷河驳船坞", "risk": 0.57, "desc": "残破的驳船半沉在水里，岸边堆着被雨淋透的木箱。", "tags": ["河畔", "仓储", "湿滑"]},
    "61": {"name": "断桥下涵洞", "risk": 0.63, "desc": "断裂桥面下方有一条涵洞，能短暂避开街面火力。", "tags": ["绕行", "潮湿", "光线差"]},
    "62": {"name": "铁路维修坑道", "risk": 0.55, "desc": "铁轨旁的维修坑道连接着几处机具间，气味刺鼻。", "tags": ["机具", "狭窄", "可藏身"]},
    "63": {"name": "临时救护点", "risk": 0.50, "desc": "几张担架与空瓶散落，墙角还有未带走的绷带箱。", "tags": ["救护", "补给线", "人迹"]},
    "64": {"name": "废弃印刷厂", "risk": 0.59, "desc": "滚筒机与铅字盒倾倒，黑墨把地面染得发亮。", "tags": ["机器", "噪音回响", "可搜刮"]},
    "65": {"name": "市政档案室", "risk": 0.61, "desc": "文件柜像墙一样堆叠，通道只容许单列通过。", "tags": ["档案", "通道窄", "易堵塞"]},

    "66": {"name": "河堤野渡口", "risk": 0.46, "desc": "水位不稳的浅滩，车辆通过要靠运气与耐心。", "tags": ["浅滩", "泥泞", "视野开阔"]},
    "67": {"name": "沼泽草甸", "risk": 0.43, "desc": "草丛里全是暗水坑，履带很容易打滑。", "tags": ["暗水", "泥泞", "慢行"]},
    "68": {"name": "防御壕沟线", "risk": 0.54, "desc": "断续的壕沟与散兵坑串成一条线，像是曾经的最后防线。", "tags": ["壕沟", "掩体", "火力点"]},
    "69": {"name": "被炸毁的粮仓", "risk": 0.47, "desc": "谷物发霉的气味飘散，坍塌屋顶下还残留几处梁柱。", "tags": ["谷物", "坍塌", "可掩蔽"]},
    "70": {"name": "乡间路口路障", "risk": 0.50, "desc": "临时路障把路口封死一半，留下只有经验才知道的缝隙。", "tags": ["路障", "检查", "绕行"]},
    "71": {"name": "伪装炮位", "risk": 0.52, "desc": "伪装网被雨水压垮，下面似乎还留着炮架痕迹。", "tags": ["伪装", "炮位", "埋伏"]},
    "72": {"name": "林中废车队", "risk": 0.48, "desc": "几辆烧毁车辆横在林间小道旁，金属外壳仍发出冷光。", "tags": ["残车", "遮蔽", "可能有物资"]},
    "73": {"name": "泥泞林道", "risk": 0.44, "desc": "雨后的林道把轮迹变成深沟，速度提不起来。", "tags": ["慢行", "侧滑", "隐蔽"]},
    "74": {"name": "采石坑", "risk": 0.45, "desc": "开阔的坑地像一个盆地，进出都容易被观察。", "tags": ["开阔", "低洼", "易被压制"]},
    "75": {"name": "小桥与浅滩", "risk": 0.42, "desc": "木桥已摇摇欲坠，只能谨慎通过，或试着走浅滩。", "tags": ["桥", "浅水", "风险可控"]},
    "76": {"name": "河畔水泵站", "risk": 0.44, "desc": "泵房里残留工具与管线，像是曾经的生命线。", "tags": ["管线", "工具", "湿冷"]},
    "77": {"name": "半塌的修理棚", "risk": 0.47, "desc": "棚顶塌落，地面留着拖拽痕与油污。", "tags": ["维修", "油污", "可补给"]},
    "78": {"name": "林道哨位", "risk": 0.46, "desc": "临时哨位旁堆着空弹壳与烟头，说明这里并不安静。", "tags": ["警戒", "火力点", "可观察"]},
    "79": {"name": "废弃教区墓园", "risk": 0.50, "desc": "墓碑与残墙形成天然掩体，路线却容易被堵死。", "tags": ["掩体", "狭路", "压迫感"]},
    "80": {"name": "郊外岔路口", "risk": 0.39, "desc": "通向更远处的分岔路，风里带着林子的味道。", "tags": ["岔路", "方向选择", "离城更近"]},
}

# 地图元数据：邻接关系、地形、移动成本（额外消耗燃油）、事件权重修正等
MAP_META: Dict[str, Dict[str, object]] = {
    "1": {"adj": ["2", "3", "11"], "terrain": "市区", "move_cost": 10, "event_mod": 0.0},
    "2": {"adj": ["1", "4", "7"], "terrain": "公园", "move_cost": 9, "event_mod": -0.02},
    "3": {"adj": ["1", "5", "27", "51"], "terrain": "高架", "move_cost": 10, "event_mod": 0.05},
    "4": {"adj": ["2", "14", "17", "53", "64"], "terrain": "工业", "move_cost": 11, "event_mod": 0.03},
    "5": {"adj": ["3", "16", "21", "54", "55"], "terrain": "地铁", "move_cost": 12, "event_mod": 0.08},
    "6": {"adj": ["11", "12"], "terrain": "象征性广场", "move_cost": 12, "event_mod": 0.12},
    "7": {"adj": ["2", "11", "20", "52", "58"], "terrain": "广场", "move_cost": 10, "event_mod": 0.04},
    "8": {"adj": ["11", "28", "59", "79"], "terrain": "教堂", "move_cost": 10, "event_mod": 0.03},
    "9": {"adj": ["18", "34"], "terrain": "贵族区", "move_cost": 8, "event_mod": -0.03},
    "10": {"adj": ["30", "36"], "terrain": "郊区", "move_cost": 11, "event_mod": 0.01},
    "11": {"adj": ["1", "6", "12", "52"], "terrain": "市区废墟", "move_cost": 11, "event_mod": 0.06},
    "12": {"adj": ["11", "13", "56"], "terrain": "政府附近", "move_cost": 13, "event_mod": 0.15},
    "13": {"adj": ["12", "16", "21", "60", "66"], "terrain": "堤岸", "move_cost": 10, "event_mod": 0.02},
    "14": {"adj": ["4", "15", "64"], "terrain": "断街", "move_cost": 11, "event_mod": 0.05},
    "15": {"adj": ["14", "17"], "terrain": "车站", "move_cost": 11, "event_mod": 0.06},
    "16": {"adj": ["5", "13", "55", "61"], "terrain": "桥头", "move_cost": 12, "event_mod": 0.07},
    "17": {"adj": ["4", "15", "32", "62"], "terrain": "货场", "move_cost": 11, "event_mod": 0.04},
    "18": {"adj": ["9", "29"], "terrain": "公寓区", "move_cost": 9, "event_mod": -0.01},
    "19": {"adj": ["21", "27", "57"], "terrain": "巷道", "move_cost": 10, "event_mod": 0.03},
    "20": {"adj": ["7", "21", "57", "58", "65"], "terrain": "广场边缘", "move_cost": 10, "event_mod": 0.04},
    "21": {"adj": ["5", "13", "20", "60"], "terrain": "仓库带", "move_cost": 10, "event_mod": 0.03},
    "22": {"adj": ["28", "24", "63"], "terrain": "医院", "move_cost": 9, "event_mod": -0.02},
    "23": {"adj": ["24", "34", "57"], "terrain": "消防局", "move_cost": 9, "event_mod": -0.01},
    "24": {"adj": ["22", "23", "25", "56"], "terrain": "电台", "move_cost": 12, "event_mod": 0.07},
    "25": {"adj": ["24", "32", "54", "65"], "terrain": "地下通道", "move_cost": 13, "event_mod": 0.1},
    "26": {"adj": ["21", "31", "60"], "terrain": "运河边", "move_cost": 10, "event_mod": 0.02},
    "27": {"adj": ["3", "19", "51"], "terrain": "后街", "move_cost": 9, "event_mod": 0.0},
    "28": {"adj": ["8", "22", "59", "65"], "terrain": "图书馆", "move_cost": 9, "event_mod": -0.01},
    "29": {"adj": ["18", "34"], "terrain": "大学区", "move_cost": 9, "event_mod": -0.02},
    "30": {"adj": ["10", "32"], "terrain": "兵营", "move_cost": 11, "event_mod": 0.05},
    "31": {"adj": ["26", "36", "76"], "terrain": "水厂", "move_cost": 10, "event_mod": 0.01},
    "32": {"adj": ["17", "30", "62", "25"], "terrain": "调车场", "move_cost": 11, "event_mod": 0.04},
    "33": {"adj": ["34", "39"], "terrain": "温室", "move_cost": 8, "event_mod": -0.03},
    "34": {"adj": ["9", "23", "29"], "terrain": "街口", "move_cost": 9, "event_mod": 0.0},
    "35": {"adj": ["36", "40", "67", "79"], "terrain": "林带边缘", "move_cost": 7, "event_mod": -0.04},
    "36": {"adj": ["10", "31", "35", "48"], "terrain": "农道", "move_cost": 8, "event_mod": -0.02},
    "37": {"adj": ["17", "38", "53"], "terrain": "工坊", "move_cost": 9, "event_mod": 0.01},
    "38": {"adj": ["37", "39", "74"], "terrain": "砖窑", "move_cost": 8, "event_mod": -0.01},
    "39": {"adj": ["33", "38", "61", "75"], "terrain": "桥涵", "move_cost": 8, "event_mod": 0.0},
    "40": {"adj": ["35", "41", "45", "72"], "terrain": "营地", "move_cost": 9, "event_mod": 0.02},
    "41": {"adj": ["40", "42", "47", "73"], "terrain": "公路", "move_cost": 9, "event_mod": -0.01},
    "42": {"adj": ["41", "49", "50", "68", "70", "78"], "terrain": "检查点", "move_cost": 10, "event_mod": 0.03},
    "43": {"adj": ["35", "45", "50", "66"], "terrain": "堤坝", "move_cost": 8, "event_mod": -0.02},
    "44": {"adj": ["37", "47", "53", "77"], "terrain": "修理厂", "move_cost": 9, "event_mod": 0.01},
    "45": {"adj": ["40", "43", "46", "63", "72"], "terrain": "营地", "move_cost": 8, "event_mod": -0.03},
    "46": {"adj": ["45", "48", "50", "69", "77"], "terrain": "小镇", "move_cost": 7, "event_mod": -0.04},
    "47": {"adj": ["41", "44", "48", "62"], "terrain": "铁路", "move_cost": 9, "event_mod": 0.0},
    "48": {"adj": ["36", "46", "47", "69"], "terrain": "农舍", "move_cost": 7, "event_mod": -0.03},
    "49": {"adj": ["42", "30", "68", "71"], "terrain": "阵地", "move_cost": 11, "event_mod": 0.06},
    "50": {"adj": ["42", "43", "46", "73", "78", "80"], "terrain": "出口", "move_cost": 7, "event_mod": -0.05},

    # --- 新增节点（51-80）
    "51": {"adj": ["3", "27", "52"], "terrain": "阵地", "move_cost": 11, "event_mod": 0.06},
    "52": {"adj": ["7", "11", "51"], "terrain": "市区废墟", "move_cost": 10, "event_mod": 0.05},
    "53": {"adj": ["4", "37", "44"], "terrain": "工业", "move_cost": 11, "event_mod": 0.03},
    "54": {"adj": ["5", "25", "55"], "terrain": "地下通道", "move_cost": 13, "event_mod": 0.10},
    "55": {"adj": ["5", "54", "16"], "terrain": "地铁", "move_cost": 12, "event_mod": 0.08},
    "56": {"adj": ["12", "24", "65"], "terrain": "政府附近", "move_cost": 13, "event_mod": 0.14},
    "57": {"adj": ["19", "20", "23", "58"], "terrain": "市区", "move_cost": 10, "event_mod": 0.01},
    "58": {"adj": ["7", "20", "57", "64"], "terrain": "广场边缘", "move_cost": 10, "event_mod": 0.03},
    "59": {"adj": ["8", "28", "65"], "terrain": "教堂", "move_cost": 10, "event_mod": 0.03},
    "60": {"adj": ["13", "21", "26"], "terrain": "仓库带", "move_cost": 10, "event_mod": 0.03},
    "61": {"adj": ["16", "39", "66"], "terrain": "桥涵", "move_cost": 9, "event_mod": 0.01},
    "62": {"adj": ["17", "32", "47"], "terrain": "铁路", "move_cost": 9, "event_mod": 0.00},
    "63": {"adj": ["22", "45"], "terrain": "医院", "move_cost": 9, "event_mod": -0.02},
    "64": {"adj": ["4", "14", "58"], "terrain": "工业", "move_cost": 11, "event_mod": 0.02},
    "65": {"adj": ["20", "25", "28", "56", "59"], "terrain": "图书馆", "move_cost": 9, "event_mod": -0.01},

    "66": {"adj": ["13", "43", "61", "75", "76", "67"], "terrain": "堤坝", "move_cost": 8, "event_mod": -0.02},
    "67": {"adj": ["35", "66", "68", "79"], "terrain": "林带边缘", "move_cost": 7, "event_mod": -0.03},
    "68": {"adj": ["42", "49", "67", "71"], "terrain": "阵地", "move_cost": 11, "event_mod": 0.05},
    "69": {"adj": ["48", "46", "70"], "terrain": "农舍", "move_cost": 7, "event_mod": -0.02},
    "70": {"adj": ["42", "69", "71", "73"], "terrain": "检查点", "move_cost": 10, "event_mod": 0.02},
    "71": {"adj": ["49", "68", "70", "72"], "terrain": "阵地", "move_cost": 11, "event_mod": 0.05},
    "72": {"adj": ["40", "45", "71"], "terrain": "营地", "move_cost": 9, "event_mod": 0.02},
    "73": {"adj": ["41", "50", "70", "74", "80"], "terrain": "公路", "move_cost": 9, "event_mod": -0.01},
    "74": {"adj": ["38", "73", "75"], "terrain": "砖窑", "move_cost": 8, "event_mod": -0.01},
    "75": {"adj": ["39", "66", "74"], "terrain": "桥涵", "move_cost": 8, "event_mod": 0.00},
    "76": {"adj": ["31", "66", "77"], "terrain": "水厂", "move_cost": 9, "event_mod": 0.01},
    "77": {"adj": ["44", "46", "76"], "terrain": "修理厂", "move_cost": 9, "event_mod": 0.01},
    "78": {"adj": ["42", "50", "80"], "terrain": "检查点", "move_cost": 10, "event_mod": 0.02},
    "79": {"adj": ["8", "35", "67"], "terrain": "教堂", "move_cost": 8, "event_mod": -0.02},
    "80": {"adj": ["50", "73", "78"], "terrain": "出口", "move_cost": 7, "event_mod": -0.04},
}

# 根据地形偏好定义事件类型权重（用于在随机事件中偏向某类事件）
TERRAIN_EVENT_WEIGHTS: Dict[str, Dict[str, float]] = {
    "市区": {"supply": 1.0, "assist": 1.0, "simple": 1.0, "medical": 0.9, "map": 1.0, "intel": 1.0, "mechanical": 1.0, "morale": 1.0},
    "市区废墟": {"supply": 1.1, "assist": 1.0, "simple": 1.0, "medical": 0.9, "map": 1.1, "intel": 1.0, "mechanical": 1.0, "morale": 1.0},
    "仓库带": {"supply": 1.6, "assist": 0.9, "simple": 1.0, "map": 0.8, "medical": 0.8, "intel": 1.0, "mechanical": 1.0, "morale": 1.0},
    "地铁": {"supply": 1.2, "assist": 1.0, "simple": 1.0, "map": 1.0, "medical": 0.9, "intel": 1.0, "mechanical": 1.0, "morale": 0.9},
    "医院": {"medical": 2.0, "supply": 0.9, "simple": 0.9, "map": 0.6, "assist": 1.0, "intel": 0.8, "mechanical": 0.7, "morale": 1.0},
    "电台": {"intel": 1.8, "supply": 1.0, "simple": 1.0, "map": 0.8, "assist": 0.9, "medical": 0.8, "mechanical": 0.9, "morale": 1.0},
    "车站": {"supply": 1.3, "simple": 1.0, "map": 1.1, "assist": 1.0, "medical": 0.8, "intel": 1.0},
    "货场": {"supply": 1.4, "simple": 1.0, "map": 0.9, "assist": 0.9},
    "公园": {"simple": 1.1, "assist": 1.1, "map": 1.0, "supply": 0.9},
    "后街": {"assist": 1.3, "simple": 1.0, "map": 1.0, "supply": 0.9},
    "郊区": {"map": 1.4, "simple": 1.0, "supply": 0.9, "medical": 0.8},
    "营地": {"assist": 1.4, "morale": 1.2, "supply": 1.0, "simple": 1.0, "medical": 1.1, "map": 1.0},
    "公路": {"simple": 1.2, "map": 1.1, "supply": 0.9, "assist": 1.0, "intel": 1.0},
    "检查点": {"intel": 1.3, "simple": 1.1, "supply": 1.0, "assist": 0.9, "mechanical": 1.0},
    "堤坝": {"map": 1.2, "simple": 1.0, "supply": 0.9, "assist": 1.0, "morale": 1.0},
    "修理厂": {"mechanical": 1.5, "supply": 1.2, "simple": 1.0, "assist": 0.8, "intel": 0.9},
    "铁路": {"supply": 1.2, "simple": 1.1, "map": 1.1, "assist": 0.9, "intel": 1.0},
    "农舍": {"morale": 1.3, "assist": 1.2, "supply": 0.9, "simple": 1.0, "medical": 1.0, "map": 1.1},
    "阵地": {"simple": 1.2, "supply": 1.1, "intel": 1.1, "mechanical": 1.0, "assist": 0.8},
    "出口": {"map": 1.6, "morale": 1.1, "simple": 1.0, "assist": 1.0, "supply": 0.8},
    # 其他地形使用较中性的默认分布
}


def map_menu(ins: InputStream, s: GameState) -> None:
    while True:
        loc = LOCATIONS.get(s.location_key, {}).get("name", "未知")
        meta = MAP_META.get(s.location_key, {})
        terrain = str(meta.get("terrain", "未知"))
        risk = _pct(LOCATIONS.get(s.location_key, {}).get("risk", 0.0))
        cost = int(meta.get("move_cost", 10))
        adj = meta.get("adj", []) if isinstance(meta, dict) else []
        if not isinstance(adj, list):
            adj = []

        print("\n地图：")
        extra = ""
        try:
            v0 = LOCATIONS.get(s.location_key, {})
            tags0 = v0.get("tags") if isinstance(v0, dict) else None
            if isinstance(tags0, list) and tags0:
                extra = f"｜特征:{'、'.join(map(str, tags0[:3]))}"
        except Exception:
            extra = ""
        print(f"当前位置：{s.location_key}. {loc}｜地形:{terrain}｜风险{risk}%｜移动耗油:{cost}{extra}")
        if adj:
            print("相邻区域：")
            for k in adj:
                name = LOCATIONS.get(str(k), {}).get("name", "未知")
                r = _pct(LOCATIONS.get(str(k), {}).get("risk", 0.0))
                mark = "(已探索)" if str(k) in s.explored else ""
                print(f"- {k}. {name} 风险{r}% {mark}")
        else:
            print("相邻区域：无（该点未配置邻接，移动界面将回退到全地图）。")

        c = choose(
            ins,
            "选择(1-4)：",
            {
                "1": "查看全图列表",
                "2": "查看某地区详情",
                "3": "标记当前位置已探索",
                "4": "返回",
            },
            default="4",
        )
        if c == "1":
            show_map(s)
        elif c == "2":
            key = get_valid_input(ins, "输入地区编号（如 12）：", default=s.location_key).strip()
            if key not in LOCATIONS:
                print("地区编号不存在。")
                continue
            v = LOCATIONS.get(key, {})
            m = MAP_META.get(key, {})
            name = v.get("name", "未知")
            r = _pct(v.get("risk", 0.0))
            t = m.get("terrain", "未知") if isinstance(m, dict) else "未知"
            mc = m.get("move_cost", 10) if isinstance(m, dict) else 10
            a = m.get("adj", []) if isinstance(m, dict) else []
            print(f"\n{key}. {name}")
            print(f"- 风险：{r}%")
            print(f"- 地形：{t}")
            print(f"- 移动耗油：{mc}")
            print(f"- 邻接：{', '.join(map(str, a)) if a else '无'}")
            try:
                desc = v.get("desc") if isinstance(v, dict) else None
                if isinstance(desc, str) and desc.strip():
                    print(f"- 现场：{desc.strip()}")
                tags = v.get("tags") if isinstance(v, dict) else None
                if isinstance(tags, list) and tags:
                    print(f"- 特征：{'、'.join(map(str, tags))}")
            except Exception:
                pass
            sec = s.sectors.get(key)
            if sec is not None:
                print(f"- 辖区：好感{sec.favor} 沦陷{sec.fall} 驻军{len(sec.garrison_units)}")
            print("")
        elif c == "3":
            s.explored.add(s.location_key)
            print("已标记。")
        else:
            return

# 地形对应驻军类型偏好（用于在初始化与招募时更倾向生成特定类型驻军）
TERRAIN_GARRISON_PREFERENCE: Dict[str, Dict[str, float]] = {
    "市区": {"国民冲锋队": 0.6, "国防军": 0.7, "党卫军": 0.5, "反坦克组": 0.9, "工兵": 1.0, "医疗组": 1.0, "狙击组": 0.9, "反坦克炮": 0.6, "侦察组": 0.8, "机枪队": 1.2, "88炮": 0.25},
    "市区废墟": {"国民冲锋队": 0.7, "国防军": 0.8, "党卫军": 0.6, "反坦克组": 0.9, "工兵": 1.0, "医疗组": 0.9, "狙击组": 1.0, "反坦克炮": 0.6, "侦察组": 0.9, "机枪队": 1.1, "88炮": 0.30},
    "仓库带": {"国民冲锋队": 0.6, "国防军": 0.8, "党卫军": 0.6, "反坦克组": 1.1, "工兵": 1.0, "反坦克炮": 1.0, "狙击组": 0.8, "医疗组": 0.8, "侦察组": 0.8, "机枪队": 1.05, "88炮": 0.35},
    "地铁": {"国民冲锋队": 0.6, "国防军": 0.6, "党卫军": 0.5, "工兵": 1.1, "侦察组": 1.0, "医疗组": 0.9},
    "医院": {"医疗组": 2.0, "国民冲锋队": 0.5, "国防军": 0.6, "工兵": 0.7},
    "电台": {"侦察组": 1.4, "国民冲锋队": 0.6, "国防军": 0.7, "工兵": 0.9},
    "车站": {"国民冲锋队": 0.7, "国防军": 0.8, "党卫军": 0.6, "狙击组": 0.9, "工兵": 1.0},
    "货场": {"国民冲锋队": 0.6, "国防军": 0.8, "党卫军": 0.6, "反坦克组": 1.2, "反坦克炮": 1.1, "工兵": 1.0, "机枪队": 1.0, "88炮": 0.55},
    "公园": {"狙击组": 1.1, "侦察组": 1.0, "国民冲锋队": 0.5, "国防军": 0.6},
    "后街": {"侦察组": 1.2, "狙击组": 1.1, "国民冲锋队": 0.6, "国防军": 0.6},
    "郊区": {"反坦克炮": 1.3, "国民冲锋队": 0.6, "国防军": 0.7, "工兵": 0.9, "机枪队": 0.9, "88炮": 0.80},
    "兵营": {"反坦克炮": 1.4, "反坦克组": 1.3, "国民冲锋队": 0.6, "国防军": 0.9, "机枪队": 1.1, "88炮": 1.1},
    "营地": {"医疗组": 1.3, "国民冲锋队": 0.7, "国防军": 0.7, "侦察组": 1.0, "工兵": 0.9},
    "公路": {"侦察组": 1.1, "反坦克组": 1.0, "国民冲锋队": 0.6, "国防军": 0.7},
    "检查点": {"反坦克组": 1.1, "狙击组": 1.0, "国防军": 0.8, "侦察组": 1.0},
    "堤坝": {"狙击组": 1.1, "侦察组": 1.1, "国民冲锋队": 0.6, "工兵": 0.9},
    "修理厂": {"工兵": 1.2, "反坦克组": 1.0, "国防军": 0.8, "医疗组": 0.9},
    "铁路": {"反坦克组": 1.1, "国防军": 0.8, "侦察组": 0.9, "狙击组": 0.9},
    "农舍": {"国民冲锋队": 0.7, "医疗组": 1.1, "侦察组": 1.0, "国防军": 0.6},
    "阵地": {"反坦克炮": 1.3, "反坦克组": 1.2, "国防军": 0.9, "工兵": 1.0, "机枪队": 1.0, "88炮": 1.6},
    "出口": {"侦察组": 1.2, "狙击组": 1.0, "国防军": 0.7, "国民冲锋队": 0.6},
    # 其余地形默认使用中性偏好
}
def show_map(s: GameState) -> None:
    print("\n地图概览：")
    for k, v in LOCATIONS.items():
        meta = MAP_META.get(k, {})
        name = v.get("name", "未知")
        risk = int(v.get("risk", 0) * 100)
        terrain = meta.get("terrain", "未知")
        cost = meta.get("move_cost", 10)
        adj = meta.get("adj", [])
        mark = "(你在此)" if s.location_key == k else ("[已探索]" if k in s.explored else "")
        print(f"{k}. {name} {mark} — 风险{risk}%｜地形:{terrain}｜移动耗油:{cost}｜邻接:{len(adj)}")
    print("")


ITEMS = {
    "燃油桶": {"desc": "开封即用：燃油+60。", "type": "consumable"},
    "弹药箱": {"desc": "补给箱：机枪弹+120；小概率额外AP+4或HE+4。", "type": "consumable"},
    "炮弹箱": {"desc": "补弹：AP+8、HE+8。", "type": "consumable"},
    "烟幕弹": {"desc": "战术烟幕：下次遭遇战可选择无损撤离（消耗）。", "type": "consumable"},
    "急救包": {"desc": "稳定队伍：士气+8。", "type": "consumable"},
    "香烟": {"desc": "提神压压惊：士气+8。", "type": "consumable"},
    "备件": {"desc": "维修材料：用于修理/技能；也可直接使用（损伤-10）。", "type": "material"},
    "电台电池": {"desc": "电台耗材：用于电台求援；部分事件也会消耗。", "type": "material"},
    "地图碎片": {"desc": "剧情道具：收集5张解锁‘郊外缺口’路线。", "type": "quest"},
    "医疗包": {"desc": "治疗：随机一名伤员HP+35并减压；若无人伤则士气+5。", "type": "consumable"},
    "药品": {"desc": "处置伤情：随机伤员HP+18~28并减压；回合开始可能自动消耗照顾重伤。", "type": "consumable"},
    "工具箱": {"desc": "检修：损伤-20，士气+5；并压制多项车辆故障（各-1回合）。", "type": "consumable"},
    "侦察设备": {"desc": "情报优势：下次移动不会触发随机遭遇（消耗）。", "type": "consumable"},
    "伪装网": {"desc": "隐蔽推进：遭遇概率×0.8，持续2回合。", "type": "consumable"},
    "纯燃料桶": {"desc": "高品质燃料：燃油+120（稀有）。", "type": "consumable"},
    "弹药": {"desc": "弹药捆：机枪弹+280，AP+8，HE+8。", "type": "consumable"},
    "咖啡": {"desc": "提神：士气+15，行动点+1；10%概率额外士气-5。", "type": "consumable"},
    "装甲板": {"desc": "加装：永久装甲+10（最多4层）。", "type": "consumable"},
    "口粮": {"desc": "补给：疲劳-15，士气+2。", "type": "consumable"},
    "润滑油": {"desc": "武器维护：清除机枪卡壳；并降低卡壳概率2回合。", "type": "consumable"},
}


SKILLS = {
    "鼓舞": {"cooldown": 3, "desc": "士气+45（冷却3回合）"},
    "观察": {"cooldown": 2, "desc": "降低下一次遭遇风险（冷却2回合）"},
    "紧急抢修": {"cooldown": 4, "desc": "消耗1个备件：大幅抢修损伤并缓解多项故障（冷却4回合）"},
    "电台求援": {"cooldown": 5, "desc": "消耗1个电台电池：一次事件结果更有利（冷却5回合）"},
    "稳固阵位": {"cooldown": 4, "desc": "战斗中：2回合内敌方命中略降、模块故障概率略降（冷却4回合）"},
}


CREW_ROLES = [
    "车长",
    "驾驶员",
    "炮手",
    "装填手",
    "通信员",
]


# 车长经验联动：车长熟练度会为其他岗位提供额外加成（“领导力/指挥协同”）。
# - 加成只作用于非车长岗位；
# - 若该岗位并非 OK 且由车长顶替执行，则不再额外叠加（避免对同一人重复计算）。
# 说明：熟练度为 0 时仍等同“原状态/无加成”（兼容旧存档）。
COMMANDER_LEADERSHIP_SHARE = 1.0  # 0..100 -> 0..100


CREW_NAMES = [
    "阿尔特",
    "奥托",
    "弗里茨",
    "海因里希",
    "鲁道夫",
    "威利",
    "彼得",
    "埃里希",
]


@dataclass
class CrewMember:
    role: str
    name: str
    hp: int = 100
    stress: int = 0
    # 熟练度：0-100。用于战斗加成（命中/装填迟滞/机动撤离等）。
    # 设计：熟练度=0 等同“原状态/无加成”（兼容旧存档缺字段）。
    proficiency: int = 0
    alive: bool = True

    def clamp(self) -> None:
        self.hp = max(0, min(100, self.hp))
        self.stress = max(0, min(100, self.stress))
        try:
            prof = int(getattr(self, "proficiency", 0) or 0)
        except Exception:
            prof = 0
        self.proficiency = max(0, prof)
        if self.hp <= 0:
            self.alive = False


@dataclass
class SectorState:
    favor: int = 50  # 好感：0-100
    fall: int = 45   # 沦陷：0-100（越高越危险）
    garrison_units: List["GarrisonUnit"] = field(default_factory=list)  # 驻军单位列表（更像原作）

    def clamp(self) -> None:
        self.favor = max(0, min(100, self.favor))
        self.fall = max(0, min(100, self.fall))
        # 清理无效单位
        self.garrison_units = [u for u in self.garrison_units if u.alive]


@dataclass
class GarrisonUnit:
    unit_type: str
    name: str
    hp: int = 100
    armor: int = 2
    power: int = 12
    morale: int = 55
    alive: bool = True

    def clamp(self) -> None:
        self.hp = max(0, min(220, int(self.hp)))
        self.armor = max(0, min(60, int(self.armor)))
        self.power = max(1, min(35, self.power))
        self.morale = max(0, min(100, self.morale))
        self.alive = self.hp > 0


@dataclass
class TankAlly:
    name: str
    model: str = "友军坦克"
    hp: int = 120
    armor: int = 90
    accuracy: int = 62
    morale: int = 55
    # 友军独立补给：燃油、主炮弹与机枪弹（可为 0；为 0 时相应能力受限）
    fuel: int = 0
    shells: int = 0
    mg_ammo: int = 0
    # 参战次数限制（原机制）：每辆友军坦克最多连续参与3次战斗；达到后可用大量补给挽留，否则离队。
    # 兼容旧存档：可能不存在该字段，使用 getattr/setattr 兜底。
    battles_fought: int = 0
    # 初始免费支援次数：战斗时优先消耗该次数；用完后进入原3次挽留机制。
    # 兼容旧存档：字段可能不存在，clamp() 会补齐。
    support_battles_left: int = 0
    alive: bool = True

    def clamp(self) -> None:
        # 旧存档兼容：补齐新增字段
        try:
            bf = int(getattr(self, "battles_fought", 0) or 0)
        except Exception:
            bf = 0
        setattr(self, "battles_fought", max(0, min(999, bf)))
        try:
            sbl = int(getattr(self, "support_battles_left", 0) or 0)
        except Exception:
            sbl = 0
        setattr(self, "support_battles_left", max(0, min(999, sbl)))
        try:
            fuel = int(getattr(self, "fuel", 0) or 0)
        except Exception:
            fuel = 0
        setattr(self, "fuel", max(0, min(200, fuel)))
        try:
            shells = int(getattr(self, "shells", 0) or 0)
        except Exception:
            shells = 0
        setattr(self, "shells", max(0, min(30, shells)))
        try:
            mg = int(getattr(self, "mg_ammo", 0) or 0)
        except Exception:
            mg = 0
        # 友军机枪弹容量上限与玩家一致（但友军通常更少）
        setattr(self, "mg_ammo", max(0, min(400, mg)))
        self.hp = max(0, min(200, int(self.hp)))
        self.armor = max(0, min(160, int(self.armor)))
        self.accuracy = max(10, min(90, int(self.accuracy)))
        self.morale = max(0, min(100, int(self.morale)))
        if self.hp <= 0:
            self.alive = False


def is_breakout_mode(s: "GameState") -> bool:
    return str(getattr(s, "difficulty_key", "") or "") == "突围"


@dataclass
class RescueMission:
    id: str
    title: str
    desc: str
    expires_round: int
    difficulty: float  # 0.0-1.0


@dataclass
class DelegatedTask:
    id: str
    title: str
    desc: str
    kind: str
    origin_sector_key: str
    start_round: int
    remaining_rounds: int
    base_risk: float  # 0.0-1.0
    assigned_unit: Optional["GarrisonUnit"] = None
    reward_points: int = 0
    reward_items: Dict[str, int] = field(default_factory=dict)
    counter_effects: Dict[str, int] = field(default_factory=dict)
    quest_progress: Dict[str, int] = field(default_factory=dict)
    sector_favor_delta: int = 0
    sector_fall_delta: int = 0
    status: str = "active"  # active / success / failed / canceled
    result_text: str = ""


@dataclass
class Commission:
    id: str
    title: str
    desc: str
    counter: str
    target: int
    reward_points: int
    # 重新刷新支持：记录接取时计数器的基准值，进度按“增量”计算，避免累计计数导致新委托秒完成
    start: int = 0
    progress: int = 0
    done: bool = False

    def sync(self, counters: Dict[str, int]) -> None:
        if self.done:
            return
        base = int(self.start or 0)
        now = int(counters.get(self.counter, 0) or 0)
        delta = max(0, now - base)
        self.progress = min(int(self.target), delta)
        if self.progress >= self.target:
            self.done = True


@dataclass
class Quest:
    id: str
    title: str
    desc: str
    target: int
    progress: int = 0
    reward_points: int = 0
    done: bool = False
    # 任务板刷新支持：若 counter 非空，则按 counters 的“增量”自动推进
    counter: str = ""
    start: int = 0
    refreshable: bool = False

    def sync(self, counters: Dict[str, int]) -> None:
        if self.done:
            return
        if not self.counter:
            return
        base = int(self.start or 0)
        now = int(counters.get(self.counter, 0) or 0)
        delta = max(0, now - base)
        self.progress = min(int(self.target), delta)
        if self.progress >= self.target:
            self.done = True

    def add(self, amount: int) -> None:
        if self.done:
            return
        self.progress = min(self.target, self.progress + amount)
        if self.progress >= self.target:
            self.done = True


def _side_quest_templates() -> List[Dict[str, object]]:
    # 任务板（可刷新支线任务）：不干扰剧情任务(Q1~Q5 / Q_*).
    # 进度全部走 counters，避免到处插 _quest_progress。
    return [
        {"title": "巷口探索", "desc": "探索2处区域。", "counter": "explore", "target": 2, "reward": 2},
        {"title": "巷口探索", "desc": "探索3处区域。", "counter": "explore", "target": 3, "reward": 3},
        {"title": "废墟搜刮", "desc": "搜索补给2次。", "counter": "scavenge", "target": 2, "reward": 2},
        {"title": "废墟搜刮", "desc": "搜索补给3次。", "counter": "scavenge", "target": 3, "reward": 3},
        {"title": "火线援助", "desc": "支援撤离2次。", "counter": "assist", "target": 2, "reward": 3},
        {"title": "火线援助", "desc": "支援撤离3次。", "counter": "assist", "target": 3, "reward": 4},
        {"title": "临战检修", "desc": "进行2次修理/维护。", "counter": "repairs", "target": 2, "reward": 2},
        {"title": "临战检修", "desc": "进行3次修理/维护。", "counter": "repairs", "target": 3, "reward": 3},
        {"title": "喘口气", "desc": "休整2次，压住压力。", "counter": "rest", "target": 2, "reward": 2},
        {"title": "喘口气", "desc": "休整3次，稳住士气。", "counter": "rest", "target": 3, "reward": 3},
        {"title": "接触战", "desc": "经历2次遭遇（无论胜负）。", "counter": "encounters", "target": 2, "reward": 2},
        {"title": "接触战", "desc": "经历3次遭遇（无论胜负）。", "counter": "encounters", "target": 3, "reward": 3},
        {"title": "肃清敌人", "desc": "本回合不进行移动/探索，累计肃清敌人2回合。", "counter": "hold_rounds", "target": 2, "reward": 3},
        {"title": "肃清敌人", "desc": "本回合不进行移动/探索，累计肃清敌人3回合。", "counter": "hold_rounds", "target": 3, "reward": 4},
        {"title": "肃清敌人", "desc": "本回合不进行移动/探索，累计肃清敌人4回合。", "counter": "hold_rounds", "target": 4, "reward": 5},
    ]


def sync_counter_quests(s: GameState) -> None:
    """同步任务板类（counter 驱动）任务进度。"""
    for q in getattr(s, "quests", []) or []:
        try:
            q.sync(s.counters)
        except Exception:
            continue


def refresh_side_quests(s: GameState, *, keep: int = 2) -> None:
    """刷新支线任务：完成后移除并补新，始终保持 keep 个进行中任务。"""
    try:
        keep = int(keep)
    except Exception:
        keep = 2
    keep = max(0, min(5, keep))
    if keep <= 0:
        return

    # 只针对 refreshable=True 的支线任务做轮换
    refreshed: List[Quest] = []
    active_side = [q for q in s.quests if bool(getattr(q, "refreshable", False))]
    # 移除已完成且已结算奖励的支线任务（reward_points 会在 complete_quests_if_any 里清零）
    for q in s.quests:
        if not bool(getattr(q, "refreshable", False)):
            refreshed.append(q)
            continue
        if bool(getattr(q, "done", False)) and int(getattr(q, "reward_points", 0) or 0) <= 0:
            continue
        refreshed.append(q)
    s.quests = refreshed

    templates = _side_quest_templates()
    if not templates:
        return

    # 去重：按 (counter, target, title)
    existing = {
        (str(getattr(q, "counter", "")), int(getattr(q, "target", 0) or 0), str(getattr(q, "title", "")))
        for q in s.quests
        if bool(getattr(q, "refreshable", False))
    }

    serial = int(s.counters.get("side_quest_serial", 0) or 0)
    # 当前进行中的支线任务数量
    def _active_count() -> int:
        return sum(1 for q in s.quests if bool(getattr(q, "refreshable", False)) and (not bool(getattr(q, "done", False))))

    while _active_count() < keep:
        picked: Optional[Dict[str, object]] = None
        tries = 0
        while tries < 12:
            tries += 1
            cand = s.rng.choice(templates)
            key = (str(cand.get("counter")), int(cand.get("target", 0) or 0), str(cand.get("title")))
            if key in existing:
                continue
            if key[1] <= 0 or not key[0]:
                continue
            picked = cand
            break
        if picked is None:
            break

        serial += 1
        sid = f"SQ{serial}"
        counter = str(picked.get("counter"))
        start = int(s.counters.get(counter, 0) or 0)
        q = Quest(
            id=sid,
            title=str(picked.get("title")),
            desc=str(picked.get("desc")),
            target=int(picked.get("target", 1) or 1),
            reward_points=int(picked.get("reward", 0) or 0),
            counter=counter,
            start=start,
            refreshable=True,
        )
        s.quests.append(q)
        existing.add((q.counter, int(q.target), q.title))

    s.counters["side_quest_serial"] = serial


def ensure_side_quests(s: GameState, *, keep: int = 2) -> None:
    """保证存在任务板支线任务（用于旧存档加载后补齐）。"""
    if not getattr(s, "quests", None):
        return
    if any(bool(getattr(q, "refreshable", False)) for q in s.quests):
        # 已有任务板任务，照常刷新即可
        refresh_side_quests(s, keep=keep)
        return
    refresh_side_quests(s, keep=keep)


@dataclass
class GameState:
    name: str
    callsign: str
    difficulty_key: str
    rng: random.Random
    round_number: int = 1
    action_points: int = 3
    moves_this_round: int = 0
    battles_this_round: int = 0
    max_moves_per_round: int = 2
    max_battles_per_round: int = 2
    victory_points: int = 0
    fuel: int = 100
    mg_ammo: int = 200
    ap_shells: int = 10
    he_shells: int = 8
    morale: int = 55
    damage: int = 15
    # 我方车辆防护：用于战斗内按比例减伤（不影响移动自然磨损）
    base_armor: int = 105
    armor_plates: int = 0
    danger_bias: float = 1.0
    # 天气：用于轻量影响移动/遭遇/命中（兼容旧存档缺少字段）
    weather_key: str = "cloudy"
    weather_turns_left: int = 0
    location_key: str = "1"
    explored: set[str] = field(default_factory=set)
    sectors: Dict[str, SectorState] = field(default_factory=dict)
    # 驻军支援：已呼叫并将参与“下一场遭遇战”的驻军单位（战斗结束后返回原辖区）
    deployed_garrison: List[Tuple[str, "GarrisonUnit"]] = field(default_factory=list)
    city_collapse: int = 10
    inventory: Dict[str, int] = field(default_factory=dict)
    # 货币：用于驻军交易/临时支援
    gold_bars: int = 0
    passes: int = 0
    buffs: Dict[str, int] = field(default_factory=dict)
    debuffs: Dict[str, int] = field(default_factory=dict)
    skill_cooldowns: Dict[str, int] = field(default_factory=dict)
    quests: List[Quest] = field(default_factory=list)
    commissions: List[Commission] = field(default_factory=list)
    counters: Dict[str, int] = field(default_factory=dict)
    crew: List[CrewMember] = field(default_factory=list)
    fleeing_enemies: List[str] = field(default_factory=list)
    rescue_missions: List[RescueMission] = field(default_factory=list)
    delegated_tasks: List[DelegatedTask] = field(default_factory=list)
    task_log: List[str] = field(default_factory=list)
    civilians_helped: int = 0
    crew_saved: int = 0
    crew_lost: int = 0
    ended: bool = False
    ending_id: Optional[str] = None
    # 仅本次运行内使用：避免“章节/关键抉择”跨周目永久消失
    shown_events: set[str] = field(default_factory=set)
    # 剧情分支：用于影响后续事件/战斗/结局
    story_flags: Dict[str, bool] = field(default_factory=dict)
    story_vars: Dict[str, int] = field(default_factory=dict)
    # 友军坦克：在移动/搜索时可能加入，并在后续战斗中并肩作战
    tank_allies: List['TankAlly'] = field(default_factory=list)
    # 战斗相关：记录“上一次最后装填的主炮弹种”（用于下场战斗开局上膛）
    last_loaded_shell: Optional[str] = None
    # 成就：并行于“勋章(胜利点)”的长期记录
    achievements: set[str] = field(default_factory=set)
    # 自定义难度：difficulty_key == "4" 时生效；旧存档可能没有该字段（使用 getattr 访问）。
    custom_difficulty: Optional[Dict[str, Any]] = None

    def clamp(self) -> None:
        self.fuel = max(0, min(200, self.fuel))
        self.mg_ammo = max(0, min(400, self.mg_ammo))
        self.ap_shells = max(0, min(40, self.ap_shells))
        self.he_shells = max(0, min(40, self.he_shells))
        self.gold_bars = max(0, min(99, int(self.gold_bars)))
        self.passes = max(0, min(99, int(self.passes)))
        self.morale = max(0, min(MORALE_MAX, self.morale))
        self.damage = max(0, min(100, self.damage))
        self.base_armor = max(0, min(160, int(self.base_armor)))
        self.armor_plates = max(0, min(ARMOR_PLATE_MAX, int(self.armor_plates)))
        self.victory_points = max(0, self.victory_points)
        self.city_collapse = max(0, min(100, self.city_collapse))
        for sec in self.sectors.values():
            sec.clamp()
            for u in sec.garrison_units:
                u.clamp()
        for m in self.crew:
            m.clamp()
        for t in self.tank_allies:
            t.clamp()
        self.tank_allies = [t for t in self.tank_allies if t.alive]

        # 若补到有油，清空“空油回合”计数，避免下一次空油沿用旧倒计时
        try:
            if int(self.fuel) > 0 and int(self.counters.get("fuel_empty_rounds", 0) or 0) > 0:
                self.counters["fuel_empty_rounds"] = 0
            # 若本回合刚进入空油状态且尚未开始计数，则初始化为 1（含本回合）
            if int(self.fuel) <= 0 and int(self.counters.get("fuel_empty_rounds", 0) or 0) <= 0:
                self.counters["fuel_empty_rounds"] = 1
        except Exception:
            pass

        # 士气崩溃倒计时：恢复则清零；首次进入<=0则初始化为1（含本回合）
        try:
            if int(self.morale) > 0 and int(self.counters.get("morale_zero_rounds", 0) or 0) > 0:
                self.counters["morale_zero_rounds"] = 0
            if int(self.morale) <= 0 and int(self.counters.get("morale_zero_rounds", 0) or 0) <= 0:
                self.counters["morale_zero_rounds"] = 1
        except Exception:
            pass

        # 城市崩溃倒计时：低于100则清零；首次到100则初始化为1（含本回合）
        try:
            if int(self.city_collapse) < 100 and int(self.counters.get("collapse_max_rounds", 0) or 0) > 0:
                self.counters["collapse_max_rounds"] = 0
            if int(self.city_collapse) >= 100 and int(self.counters.get("collapse_max_rounds", 0) or 0) <= 0:
                self.counters["collapse_max_rounds"] = 1
        except Exception:
            pass


def add_item(s: GameState, name: str, count: int = 1) -> None:
    s.inventory[name] = s.inventory.get(name, 0) + count


def spend_item(s: GameState, name: str, count: int = 1) -> bool:
    have = s.inventory.get(name, 0)
    if have < count:
        return False
    left = have - count
    if left <= 0:
        s.inventory.pop(name, None)
    else:
        s.inventory[name] = left
    return True


def wallet_text(s: GameState) -> str:
    return f"金条{int(getattr(s, 'gold_bars', 0) or 0)}｜通行证{int(getattr(s, 'passes', 0) or 0)}"


def _randomize_tank_ally_supplies(s: GameState, ally: Any) -> None:
    """为新加入的友军坦克随机初始化独立燃油与弹药。"""
    try:
        rng = getattr(s, "rng", None) or random.Random()
    except Exception:
        rng = random.Random()
    model = str(getattr(ally, "model", "") or "")

    if model in ("虎式坦克", "斐迪南突击炮"):
        fuel = rng.randint(40, 95)
        shells = rng.randint(1, 7)
        mg = rng.randint(30, 90)
    elif model in ("豹式坦克", "四号坦克"):
        fuel = rng.randint(35, 85)
        shells = rng.randint(0, 7)
        mg = rng.randint(20, 80)
    elif model in ("防空坦克", "四号防空坦克"):
        fuel = rng.randint(30, 75)
        shells = rng.randint(0, 9)
        # 防空坦克配备较多机枪弹用于对空扫射
        mg = rng.randint(60, 180)
    elif model == "Sd.Kfz.251装甲运兵车":
        fuel = rng.randint(25, 70)
        shells = rng.randint(0, 6)
        # 装甲运兵车以机枪为主，机枪弹量较大
        mg = rng.randint(80, 220)
    else:
        fuel = rng.randint(30, 80)
        shells = rng.randint(0, 7)
        mg = rng.randint(20, 100)

    try:
        setattr(ally, "fuel", int(fuel))
        setattr(ally, "shells", int(shells))
        setattr(ally, "mg_ammo", int(mg))
        # 兼容旧机制字段：保留但明确置零，避免旧逻辑残留误触发
        setattr(ally, "support_battles_left", 0)
        setattr(ally, "battles_fought", 0)
    except Exception:
        pass
    try:
        if hasattr(ally, "clamp"):
            ally.clamp()
    except Exception:
        pass


def _handle_tank_ally_supply_requests(ins: "InputStream", s: GameState) -> None:
    """友军坦克用完补给后的索要逻辑。

    - fuel <= 0：会索要燃油；不给则离队（无法继续跟随）。
    - shells <= 0：会索要主炮弹药；不给或无法补给则离队。
    - mg_ammo <= 0（机枪型车辆）：会索要机枪弹；不给或无法补给则离队。
    为避免同回合重复弹窗，对每辆坦克按回合做一次性询问。
    """
    try:
        cur_round = int(getattr(s, "round_number", 0) or 0)
    except Exception:
        cur_round = 0

    # 迭代副本：可能会移除离队坦克
    try:
        tanks = list(getattr(s, "tank_allies", []) or [])
    except Exception:
        tanks = []

    for t in tanks:
        if not getattr(t, "alive", True):
            continue

        # --- 燃油索要：不给则离队
        try:
            fuel = int(getattr(t, "fuel", 0) or 0)
        except Exception:
            fuel = 0
        if fuel <= 0:
            try:
                last_ask = int(getattr(t, "_ask_fuel_round", -999) or -999)
            except Exception:
                last_ask = -999
            if last_ask != cur_round:
                try:
                    setattr(t, "_ask_fuel_round", cur_round)
                except Exception:
                    pass

                print(f"\n友军装甲：{getattr(t, 'name', 'Unknown')} 油箱见底，无法继续跟随。")
                if int(s.inventory.get("燃油桶", 0) or 0) <= 0:
                    print("你没有燃油桶可供补给。该友军决定离队。")
                    s.tank_allies = [x for x in getattr(s, "tank_allies", []) if id(x) != id(t)]
                else:
                    raw = get_valid_input(ins, "是否提供1个燃油桶补给友军？（1=提供，0=不提供）：", default="1")
                    if raw.strip() == "1" and spend_item(s, "燃油桶", 1):
                        try:
                            setattr(t, "fuel", int(getattr(t, "fuel", 0) or 0) + 60)
                            if hasattr(t, "clamp"):
                                t.clamp()
                        except Exception:
                            pass
                        print(f"你补给了燃油：{getattr(t, 'name', 'Unknown')} 恢复燃油并继续跟随。")
                    else:
                        print(f"你未能补给燃油：{getattr(t, 'name', 'Unknown')} 决定离队。")
                        s.tank_allies = [x for x in getattr(s, "tank_allies", []) if id(x) != id(t)]

        model = str(getattr(t, "model", ""))

        # --- 弹药索要：可不给但无法战斗（主炮）
        try:
            shells = int(getattr(t, "shells", 0) or 0)
        except Exception:
            shells = 0
        if shells <= 0 and model not in ("Sd.Kfz.251装甲运兵车", "防空坦克", "四号防空坦克"):
            try:
                last_ask = int(getattr(t, "_ask_shells_round", -999) or -999)
            except Exception:
                last_ask = -999
            if last_ask != cur_round:
                try:
                    setattr(t, "_ask_shells_round", cur_round)
                except Exception:
                    pass

                print(f"\n友军装甲：{getattr(t, 'name', 'Unknown')} 主炮弹药耗尽（可继续跟随，但无法战斗）。")
                can_box = int(s.inventory.get("炮弹箱", 0) or 0) > 0
                can_pack = int(s.inventory.get("弹药", 0) or 0) > 0
                can_ammo = int(s.inventory.get("弹药箱", 0) or 0) > 0
                if not (can_box or can_pack or can_ammo):
                    print("你没有弹药/弹药箱/炮弹箱可供补给，该友军决定离队。")
                    s.tank_allies = [x for x in getattr(s, "tank_allies", []) if id(x) != id(t)]
                    continue
                raw = get_valid_input(ins, "是否补给友军弹药？（1=补给，0=不补给）：", default="0")
                if raw.strip() != "1":
                    print(f"你未补给弹药：{getattr(t, 'name', 'Unknown')} 决定离队。")
                    s.tank_allies = [x for x in getattr(s, "tank_allies", []) if id(x) != id(t)]
                    continue

                # 优先使用炮弹箱，其次弹药，其次弹药箱
                gained = 0
                mg_gain = 0
                if spend_item(s, "炮弹箱", 1):
                    gained = int(getattr(s, "rng", random.Random()).randint(6, 10))
                elif spend_item(s, "弹药", 1):
                    gained = int(getattr(s, "rng", random.Random()).randint(8, 12))
                    mg_gain = int(getattr(s, "rng", random.Random()).randint(160, 260))
                elif spend_item(s, "弹药箱", 1):
                    gained = int(getattr(s, "rng", random.Random()).randint(3, 6))
                    mg_gain = int(getattr(s, "rng", random.Random()).randint(80, 140))
                if gained > 0 or mg_gain > 0:
                    try:
                        if gained > 0:
                            setattr(t, "shells", int(getattr(t, "shells", 0) or 0) + gained)
                        if mg_gain > 0:
                            setattr(t, "mg_ammo", int(getattr(t, "mg_ammo", 0) or 0) + mg_gain)
                        if hasattr(t, "clamp"):
                            t.clamp()
                    except Exception:
                        pass
                    if mg_gain > 0:
                        print(
                            f"你补给了弹药：{getattr(t, 'name', 'Unknown')} 获得{gained}发主炮弹，机枪弹+{mg_gain}。"
                        )
                    else:
                        print(f"你补给了弹药：{getattr(t, 'name', 'Unknown')} 获得{gained}发可用弹药。")
                else:
                    print(f"你未能实际补给弹药：{getattr(t, 'name', 'Unknown')} 决定离队。")
                    s.tank_allies = [x for x in getattr(s, "tank_allies", []) if id(x) != id(t)]

        # --- 机枪弹索要：仅机枪型车辆（可不给但无法战斗）
        if model in ("Sd.Kfz.251装甲运兵车", "防空坦克", "四号防空坦克"):
            try:
                mg = int(getattr(t, "mg_ammo", 0) or 0)
            except Exception:
                mg = 0
            if mg <= 0:
                try:
                    last_ask = int(getattr(t, "_ask_mg_round", -999) or -999)
                except Exception:
                    last_ask = -999
                if last_ask != cur_round:
                    try:
                        setattr(t, "_ask_mg_round", cur_round)
                    except Exception:
                        pass

                    print(f"\n友军装甲：{getattr(t, 'name', 'Unknown')} 机枪弹耗尽（可继续跟随，但无法战斗）。")
                    can_pack = int(s.inventory.get("弹药", 0) or 0) > 0
                    can_ammo = int(s.inventory.get("弹药箱", 0) or 0) > 0
                    if not (can_pack or can_ammo):
                        print("你没有弹药/弹药箱可供补给，该友军决定离队。")
                        s.tank_allies = [x for x in getattr(s, "tank_allies", []) if id(x) != id(t)]
                        continue
                    raw = get_valid_input(ins, "是否补给友军机枪弹？（1=补给，0=不补给）：", default="0")
                    if raw.strip() != "1":
                        print(f"你未补给机枪弹：{getattr(t, 'name', 'Unknown')} 决定离队。")
                        s.tank_allies = [x for x in getattr(s, "tank_allies", []) if id(x) != id(t)]
                        continue

                    mg_gain = 0
                    if spend_item(s, "弹药", 1):
                        mg_gain = int(getattr(s, "rng", random.Random()).randint(160, 260))
                    elif spend_item(s, "弹药箱", 1):
                        mg_gain = int(getattr(s, "rng", random.Random()).randint(90, 160))
                    if mg_gain > 0:
                        try:
                            setattr(t, "mg_ammo", int(getattr(t, "mg_ammo", 0) or 0) + mg_gain)
                            if hasattr(t, "clamp"):
                                t.clamp()
                        except Exception:
                            pass
                        print(f"你补给了机枪弹：{getattr(t, 'name', 'Unknown')} 机枪弹+{mg_gain}。")
                    else:
                        print(f"你未能实际补给机枪弹：{getattr(t, 'name', 'Unknown')} 决定离队。")
                        s.tank_allies = [x for x in getattr(s, "tank_allies", []) if id(x) != id(t)]


def can_spend_currency(s: GameState, *, gold: int = 0, passes: int = 0) -> bool:
    return int(getattr(s, "gold_bars", 0) or 0) >= gold and int(getattr(s, "passes", 0) or 0) >= passes


def spend_currency(s: GameState, *, gold: int = 0, passes: int = 0) -> bool:
    if gold < 0 or passes < 0:
        return False
    if not can_spend_currency(s, gold=gold, passes=passes):
        return False
    s.gold_bars = int(getattr(s, "gold_bars", 0) or 0) - int(gold)
    s.passes = int(getattr(s, "passes", 0) or 0) - int(passes)
    s.clamp()
    return True


def grant_friendly_tank_support(s: GameState) -> bool:
    """事件/交易给一辆友军坦克支援；若已达上限则返回 False。"""
    # 取消任何情况下的友军坦克数量上限：总是允许新增友军坦克。

    # 允许玩家拒绝友军加入：
    # - 若在 GameState 中设置了 `auto_refuse_ally` 为真，则自动拒绝并返回 False。
    # - 若运行在交互式终端（stdin 是 tty），则询问玩家是否接受（回车默认接受）。
    try:
        if bool(getattr(s, "auto_refuse_ally", False)):
            print("\n（设定：自动拒绝友军加入）")
            return False
    except Exception:
        pass
    try:
        if sys.stdin is not None and hasattr(sys.stdin, "isatty") and sys.stdin.isatty():
            try:
                ans = input("友军请求加入队伍，是否接受？（1=接受，0=拒绝，回车默认接受）：").strip()
                if ans == "0":
                    print("你决定拒绝该友军的加入。")
                    return False
            except Exception:
                # 若任何交互失败（例如无输入流），继续默认行为
                pass
    except Exception:
        pass

    sec = s.sectors.get(s.location_key)
    favor = int(sec.favor) if sec is not None else 50

    w_panther = 1.0
    w_pz4 = 0.7
    w_ferd = 0.35
    # 需求：Sd.Kfz 更常见
    w_sdkfz = 8.0
    # 需求：降低防空坦克生成权重
    w_aa = 0.3
    w_tiger = 0.25
    # 使用章节索引判断友军权重随剧情/回合上升
    chap_thr = int(math.ceil(10.0 / float(CHAPTER_INTERVAL)))
    chapter_idx = max(1, min(40, (int(getattr(s, "round_number", 1) or 1) - 1) // int(CHAPTER_INTERVAL) + 1))
    if chapter_idx >= chap_thr:
        w_pz4 += 0.15
        w_ferd += 0.15
        w_aa += 0.10
        w_tiger += 0.05
    # 章节线性微增：随章节轻微提高出现更重型号的概率
    w_panther += 0.02 * float(max(0, chapter_idx - 1))
    w_tiger += 0.01 * float(max(0, chapter_idx - 1))
    if favor >= 65:
        w_ferd += 0.50
        w_aa += 0.10
        w_sdkfz += 0.05
        w_tiger += 0.10
    if s.damage >= 75:
        w_ferd = max(0.10, w_ferd - 0.10)
    model = s.rng.choices(
        ["豹式坦克", "四号坦克", "斐迪南突击炮", "Sd.Kfz.251装甲运兵车", "防空坦克", "虎式坦克"],
        weights=[w_panther, w_pz4, w_ferd, w_sdkfz, w_aa, w_tiger],
        k=1,
    )[0]

    # 旧名兼容：统一写入新名称
    if model == "突击炮III":
        model = "四号坦克"
    if model == "四号防空坦克":
        model = "防空坦克"

    if model == "豹式坦克":
        # 需求：豹式更强但更脆
        tmpl = {"hp": (120, 160), "armor": (70, 90), "acc": (64, 78), "morale": (45, 74)}
    elif model == "四号坦克":
        # 需求：突击炮III 改名为四号坦克，并降低装甲
        tmpl = {"hp": (100, 135), "armor": (48, 68), "acc": (56, 70), "morale": (45, 72)}
    elif model == "Sd.Kfz.251装甲运兵车":
        # 需求：大幅降低防御（一炮击毁）
        tmpl = {"hp": (40, 70), "armor": (12, 24), "acc": (52, 70), "morale": (45, 74)}
    elif model == "防空坦克":
        # 需求：大幅降低防御（一炮击毁），并命名为“防空坦克”
        tmpl = {"hp": (65, 90), "armor": (20, 40), "acc": (56, 74), "morale": (45, 72)}
    elif model == "虎式坦克":
        # 需求：增加更强的虎式友方单位（接近玩家）
        tmpl = {"hp": (150, 185), "armor": (95, 135), "acc": (62, 76), "morale": (45, 74)}
    else:
        tmpl = {"hp": (135, 170), "armor": (105, 130), "acc": (52, 66), "morale": (42, 70)}

    suffix = s.rng.randint(11, 99)
    name = f"{model}-{suffix}"
    ally = TankAlly(
        model=model,
        name=name,
        hp=s.rng.randint(*tmpl["hp"]),
        armor=s.rng.randint(*tmpl["armor"]),
        accuracy=s.rng.randint(*tmpl["acc"]),
        morale=s.rng.randint(*tmpl["morale"]),
    )
    _randomize_tank_ally_supplies(s, ally)
    ally.clamp()
    s.tank_allies.append(ally)
    s.morale += 2
    print(f"\n🚩 支援到达：{name} 在烟尘间与你会合。")
    print(f"（对方补给情况：燃油{int(getattr(ally, 'fuel', 0) or 0)}，弹药{int(getattr(ally, 'shells', 0) or 0)}）")
    s.clamp()
    return True


def _ensure_weather_state(s: GameState) -> None:
    """确保旧存档也具备天气字段，避免 AttributeError。"""
    if not hasattr(s, "weather_key"):
        setattr(s, "weather_key", "cloudy")
    if not hasattr(s, "weather_turns_left"):
        setattr(s, "weather_turns_left", 0)
    # 若存档里出现未知 key，则回退到阴天
    wk = str(getattr(s, "weather_key", "cloudy") or "cloudy")
    if wk not in WEATHER_TABLE:
        setattr(s, "weather_key", "cloudy")


def weather_effects(s: GameState) -> Dict[str, object]:
    _ensure_weather_state(s)
    wk = str(getattr(s, "weather_key", "cloudy") or "cloudy")
    return dict(WEATHER_TABLE.get(wk, WEATHER_TABLE["cloudy"]))


def fire_weather_multiplier(s: GameState) -> float:
    """火焰/燃烧类效果的天气倍率。

    目标：让雨天/潮湿明显削弱火焰类杀伤，晴天略增强；不追求物理严谨，仅服务玩法。
    """
    _ensure_weather_state(s)
    wk = str(getattr(s, "weather_key", "cloudy") or "cloudy")
    if wk == "rain":
        return 0.75
    if wk == "fog":
        return 0.90
    if wk == "clear":
        return 1.10
    return 1.00


def fire_weather_prob_scale(s: GameState) -> float:
    """火焰导致‘设备熏扰/灼烤’等附带效果的概率缩放。"""
    _ensure_weather_state(s)
    wk = str(getattr(s, "weather_key", "cloudy") or "cloudy")
    if wk == "rain":
        return 0.70
    if wk == "fog":
        return 0.85
    if wk == "clear":
        return 1.08
    return 1.00


def _fire_weather_hint(s: GameState) -> str:
    m = fire_weather_multiplier(s)
    if m <= 0.80:
        return "雨天削弱火焰"
    if m >= 1.08:
        return "晴天火势更猛"
    return ""


def weather_text(s: GameState) -> str:
    eff = weather_effects(s)
    return str(eff.get("name", "阴"))


def tick_weather(s: GameState) -> None:
    """回合开始更新天气：维持若干回合后再随机变化；仅在变化时提示一次。"""
    _ensure_weather_state(s)

    left = int(getattr(s, "weather_turns_left", 0) or 0)
    if left > 0:
        setattr(s, "weather_turns_left", left - 1)
        return

    prev = str(getattr(s, "weather_key", "cloudy") or "cloudy")

    # 权重：更常见阴/晴；雨/雾较少。后期城市崩溃高时更易出现雾/雨。
    w_clear, w_cloudy, w_rain, w_fog = 0.32, 0.38, 0.22, 0.08
    collapse = int(getattr(s, "city_collapse", 0) or 0)
    if collapse >= 70:
        w_fog += 0.03
        w_rain += 0.03
        w_clear = max(0.18, w_clear - 0.05)
        w_cloudy = max(0.25, w_cloudy - 0.01)

    new_key = s.rng.choices(
        ["clear", "cloudy", "rain", "fog"],
        weights=[w_clear, w_cloudy, w_rain, w_fog],
        k=1,
    )[0]

    # 天气持续回合数：晴/阴更久；雨/雾更短
    if new_key in ("clear", "cloudy"):
        dur = s.rng.randint(2, 4)
    elif new_key == "rain":
        dur = s.rng.randint(1, 3)
    else:
        dur = s.rng.randint(1, 2)

    setattr(s, "weather_key", new_key)
    setattr(s, "weather_turns_left", max(0, dur - 1))

    if new_key != prev:
        try:
            prev_name = str(WEATHER_TABLE.get(prev, WEATHER_TABLE["cloudy"]).get("name", "阴"))
        except Exception:
            prev_name = "阴"
        print(f"\n天气变化：{prev_name} → {weather_text(s)}。")


def status_line(s: GameState) -> None:
    loc = LOCATIONS[s.location_key]["name"]
    rank = get_rank(s.victory_points)
    sector = s.sectors.get(s.location_key)
    sector_text = ""
    if sector is not None:
        sector_text = f" | 好感{sector.favor} 沦陷{sector.fall} 驻军{len(sector.garrison_units)}"
    armor_text = f" | 装甲{player_armor_rating(s)}"
    tank_text = ""
    if getattr(s, "tank_allies", None):
        tank_text = f" | 友军坦克{len(s.tank_allies)}"

    tank_kills = int(s.counters.get("enemy_tank_kills", 0))
    ach_count = len(getattr(s, "achievements", set()) or set())
    ach_text = f" | 坦克击毁{tank_kills} 成就{ach_count}" if (tank_kills > 0 or ach_count > 0) else ""
    w = weather_text(s)
    print(
        f"[回合{s.round_number} | 行动{s.action_points} | {loc} | 天气{w}]\n"
        f"[燃油{s.fuel}/200 | 机枪弹{s.mg_ammo}/400 | 炮弹AP{s.ap_shells}/40 HE{s.he_shells}/40 | 士气{s.morale}/{MORALE_MAX} | 损伤{s.damage}/100{armor_text}{tank_text} | 胜利点{s.victory_points}({rank}){ach_text} | 崩溃{s.city_collapse}/100{sector_text} | 逃窜敌人{len(s.fleeing_enemies)}]"
    )

    # 燃油提醒：同一回合只提示一次，避免每次行动都刷屏
    try:
        warned_round = int(s.counters.get("fuel_warn_round", 0) or 0)
    except Exception:
        warned_round = 0
    if warned_round != int(s.round_number):
        if int(s.fuel) <= 0:
            try:
                empty_rounds = int(s.counters.get("fuel_empty_rounds", 0) or 0)
            except Exception:
                empty_rounds = 0
            left = max(0, int(FUEL_EMPTY_GRACE_ROUNDS) - int(empty_rounds) + 1)
            if left <= 0:
                print("⚠️ 燃油已耗尽：机会已用尽，本回合将触发结局判定。")
            else:
                print(f"⚠️ 燃油已耗尽：还有{left}回合机会找到燃料（含本回合），否则触发结局。")
            s.counters["fuel_warn_round"] = int(s.round_number)
        elif int(s.fuel) <= 40:
            print(f"⚠️ 燃油告急：剩余 {int(s.fuel)}。")
            s.counters["fuel_warn_round"] = int(s.round_number)

    # 士气提醒：同一回合只提示一次
    try:
        mwarn = int(s.counters.get("morale_warn_round", 0) or 0)
    except Exception:
        mwarn = 0
    if mwarn != int(s.round_number):
        if int(s.morale) <= 0:
            try:
                mr = int(s.counters.get("morale_zero_rounds", 0) or 0)
            except Exception:
                mr = 0
            left = max(0, int(MORALE_ZERO_GRACE_ROUNDS) - int(mr) + 1)
            if left <= 0:
                print("⚠️ 士气已崩溃：机会已用尽，本回合将触发结局判定。")
            else:
                print(f"⚠️ 士气已崩溃：还有{left}回合机会稳住局势（含本回合），否则触发结局。")
            s.counters["morale_warn_round"] = int(s.round_number)
        elif int(s.morale) <= 20:
            print(f"⚠️ 士气濒危：当前 {int(s.morale)}。")
            s.counters["morale_warn_round"] = int(s.round_number)

    # 城市崩溃提醒：同一回合只提示一次
    try:
        cwarn = int(s.counters.get("collapse_warn_round", 0) or 0)
    except Exception:
        cwarn = 0
    if cwarn != int(s.round_number):
        if int(s.city_collapse) >= 100:
            try:
                cr = int(s.counters.get("collapse_max_rounds", 0) or 0)
            except Exception:
                cr = 0
            left = max(0, int(CITY_COLLAPSE_GRACE_ROUNDS) - int(cr) + 1)
            if left <= 0:
                print("⚠️ 城市已崩溃：机会已用尽，本回合将触发结局判定。")
            else:
                print(f"⚠️ 城市已崩溃：还有{left}回合机会组织撤离（含本回合），否则触发结局。")
            s.counters["collapse_warn_round"] = int(s.round_number)
        elif int(s.city_collapse) >= 90:
            print(f"⚠️ 城市濒临崩溃：崩溃度 {int(s.city_collapse)}/100。")
            s.counters["collapse_warn_round"] = int(s.round_number)


def narrate(text: str) -> None:
    for line in text.strip("\n").splitlines():
        print(line.rstrip())


def end_game(s: GameState, ending_id: str, title: str, text: str) -> None:
    s.ended = True
    s.ending_id = ending_id
    print("\n" + "-" * 70)
    print(f"结局：{title}")
    print("-" * 70)
    narrate(text)
    print("-" * 70)
    print(
        f"记录：胜利点{s.victory_points}({get_rank(s.victory_points)})；坦克击毁{int(s.counters.get('enemy_tank_kills', 0))}；成就{len(getattr(s, 'achievements', set()) or set())}；帮助平民{s.civilians_helped}次；救回战友{s.crew_saved}人；乘员损失{s.crew_lost}；探索{len(s.explored)}处"
    )


def maybe_add_initial_quests(s: GameState) -> None:
    if s.quests:
        return
    s.quests.append(
        Quest(
            id="Q1",
            title="护送撤离",
            desc="在城市里帮助平民/伤员脱离危险区（通过‘支援撤离’或事件触发）。",
            target=2,
            reward_points=6,
        )
    )
    s.quests.append(
        Quest(
            id="Q2",
            title="补给回收",
            desc="搜集补给（通过‘搜索补给’获得燃油桶/弹药箱/备件）。",
            target=3,
            reward_points=5,
        )
    )
    s.quests.append(
        Quest(
            id="Q3",
            title="找到缺口",
            desc="收集地图碎片（5/5）以解锁通向郊外的‘缺口’路线。",
            target=5,
            reward_points=8,
        )
    )

    s.quests.append(
        Quest(
            id="Q4",
            title="织密频段",
            desc="通过‘搜索补给’或战后清理获得电台电池/侦察设备（累计2件），让联络更稳定。",
            target=2,
            reward_points=6,
        )
    )

    s.quests.append(
        Quest(
            id="Q5",
            title="失真信号",
            desc="追踪一段断续的广播：它也许能换来一条‘更不坏的路’（3阶段）。",
            target=3,
            reward_points=10,
        )
    )


def init_counters_and_commissions(s: GameState) -> None:
    if s.counters:
        return
    # 计数器（贴近原作“委托进度计数器”的味道）
    s.counters = {
        "explore": 0,
        "scavenge": 0,
        "assist": 0,
        "rest": 0,
        "repairs": 0,
        "wins": 0,
        "losses": 0,
        "encounters": 0,
    }

    # 首次委托：以当前计数器为基准
    s.commissions = [
        Commission(
            id="C1",
            title="街区通行",
            desc="累计探索3处区域。",
            counter="explore",
            target=3,
            reward_points=4,
            start=int(s.counters.get("explore", 0) or 0),
        ),
        Commission(
            id="C2",
            title="补给搜集",
            desc="累计搜索补给3次。",
            counter="scavenge",
            target=3,
            reward_points=4,
            start=int(s.counters.get("scavenge", 0) or 0),
        ),
        Commission(
            id="C3",
            title="坚持到最后",
            desc="累计遭遇战胜利3次。",
            counter="wins",
            target=3,
            reward_points=6,
            start=int(s.counters.get("wins", 0) or 0),
        ),
    ]


def _commission_templates() -> List[Dict[str, object]]:
    # 委托池：数量适中，避免花样过多；进度由 counters 增量驱动
    return [
        {"title": "街区通行", "desc": "探索2处区域。", "counter": "explore", "target": 2, "reward": 3},
        {"title": "街区通行", "desc": "探索3处区域。", "counter": "explore", "target": 3, "reward": 4},
        {"title": "补给搜集", "desc": "搜索补给2次。", "counter": "scavenge", "target": 2, "reward": 3},
        {"title": "补给搜集", "desc": "搜索补给3次。", "counter": "scavenge", "target": 3, "reward": 4},
        {"title": "坚持到最后", "desc": "赢得2次遭遇战。", "counter": "wins", "target": 2, "reward": 4},
        {"title": "坚持到最后", "desc": "赢得3次遭遇战。", "counter": "wins", "target": 3, "reward": 6},
        {"title": "守住节奏", "desc": "休整2次，稳住士气与压力。", "counter": "rest", "target": 2, "reward": 3},
        {"title": "临战维护", "desc": "进行2次修理/维护。", "counter": "repairs", "target": 2, "reward": 3},
        {"title": "伸出援手", "desc": "支援撤离2次。", "counter": "assist", "target": 2, "reward": 4},
        {"title": "在火线穿行", "desc": "累计遭遇战次数2次。", "counter": "encounters", "target": 2, "reward": 3},
    ]


def refresh_commissions(s: GameState, *, keep: int = 3) -> None:
    """完成的委托会被替换成新的委托（刷新）。

    - 仅保留进行中的委托数量为 keep（默认3）。
    - 新委托以当前 counters 作为 start 基准，进度按增量累积。
    """
    try:
        keep = int(keep)
    except Exception:
        keep = 3
    keep = max(1, min(6, keep))

    # 移除已完成（完成即刷新）
    active = [c for c in s.commissions if not getattr(c, "done", False)]
    s.commissions = active

    # 生成新委托
    templates = _commission_templates()
    if not templates:
        return

    # 避免重复：用 (counter, target, title) 做粗去重
    existing = {(c.counter, int(c.target), c.title) for c in s.commissions}
    serial = int(s.counters.get("commission_serial", 0) or 0)

    while len(s.commissions) < keep:
        # 按回合轻量调整：越到后期越可能出现 target 略高的条目
        tries = 0
        picked: Optional[Dict[str, object]] = None
        while tries < 12:
            tries += 1
            cand = s.rng.choice(templates)
            key = (str(cand.get("counter")), int(cand.get("target", 0) or 0), str(cand.get("title")))
            if key in existing:
                continue
            if key[1] <= 0:
                continue
            picked = cand
            break
        if picked is None:
            break

        serial += 1
        cid = f"C{serial}"
        counter = str(picked.get("counter"))
        start = int(s.counters.get(counter, 0) or 0)
        newc = Commission(
            id=cid,
            title=str(picked.get("title")),
            desc=str(picked.get("desc")),
            counter=counter,
            target=int(picked.get("target", 1) or 1),
            reward_points=int(picked.get("reward", 0) or 0),
            start=start,
        )
        s.commissions.append(newc)
        existing.add((newc.counter, int(newc.target), newc.title))

    s.counters["commission_serial"] = serial



def sync_commissions(s: GameState) -> None:
    for c in s.commissions:
        before = c.done
        c.sync(s.counters)
        if not before and c.done:
            print(f"\n📌 委托完成：{c.title}（+{c.reward_points}胜利点）")
            s.victory_points += c.reward_points
            c.reward_points = 0
    # 完成即刷新：补充新的委托，保持任务板常有可做项
    try:
        refresh_commissions(s, keep=3)
    except Exception:
        pass
    s.clamp()


def show_commissions(s: GameState) -> None:
    if not s.commissions:
        return
    print("\n委托：")
    for c in s.commissions:
        flag = "已完成" if c.done else "进行中"
        print(f"- {c.title} [{flag}] {c.progress}/{c.target}")


def _make_garrison_unit(rng: random.Random, terrain: Optional[str] = None, force_type: Optional[str] = None) -> GarrisonUnit:
    # 列表型驻军：不同单位类型略有差异，贴近原作“驻军列表”的感觉
    # 新：支持基于地形的偏好（在 init_sectors 中会传入 terrain）
    base_choices = {
        "国民冲锋队": 0.18,
        "国防军": 0.12,
        "党卫军": 0.10,
        "反坦克组": 0.15,
        "工兵": 0.15,
        "医疗组": 0.10,
        "狙击组": 0.06,
        "反坦克炮": 0.06,
        "侦察组": 0.08,
        # 新增：机枪队（稳定压制）、88炮（极高伤害但很脆）
        "机枪队": 0.05,
        "88炮": 0.02,
    }
    # 简单随机抽取，稍后在 init_sectors 可按地形再做偏好
    types = list(base_choices.keys())
    weights = [base_choices[t] for t in types]

    # 若传入 terrain，则用偏好映射放大或缩小对应权重
    if terrain:
        pref = TERRAIN_GARRISON_PREFERENCE.get(terrain, {})
        if pref:
            new_weights: List[float] = []
            for t, w in zip(types, weights):
                factor = float(pref.get(t, 1.0))
                new_weights.append(max(0.0, w * factor))
            # 若所有权重为0，退回原始权重
            if any(wt > 0 for wt in new_weights):
                weights = new_weights

    # 正规化并抽取（或强制指定类型）
    if isinstance(force_type, str) and force_type in types:
        unit_type = force_type
    else:
        total = sum(weights)
        if total <= 0:
            weights = [1.0 for _ in weights]
        unit_type = rng.choices(types, weights=weights, k=1)[0]

    suffix = rng.randint(11, 99)
    name = f"{unit_type}-{suffix}"
    # 基于类型设定战力/士气/血量
    hp = 90
    armor = 0
    if unit_type in ("国民冲锋队", "国防军", "党卫军"):
        # 不同步兵细分可有少量差异，整体略微提高生命与火力以强化我方驻防可信度
        if unit_type == "国民冲锋队":
            power = rng.randint(8, 14)
            morale = rng.randint(35, 65)
            hp = rng.randint(80, 110)
        elif unit_type == "国防军":
            # 稍微增强常规军的耐久与火力，使其在驻防时更可靠
            power = rng.randint(12, 20)
            morale = rng.randint(48, 82)
            hp = rng.randint(100, 140)
        else:  # 党卫军
            # 党卫军在数值上略胜一筹，反映训练与装备优势
            power = rng.randint(14, 26)
            morale = rng.randint(50, 90)
            hp = rng.randint(110, 160)
    elif unit_type == "反坦克组":
        power = rng.randint(14, 24)
        morale = rng.randint(42, 75)
        hp = rng.randint(90, 120)
    elif unit_type == "工兵":
        # 工兵在攻防两端都有价值：提高生命与适度火力
        power = rng.randint(10, 18)
        morale = rng.randint(48, 82)
        hp = rng.randint(90, 130)
    elif unit_type == "医疗组":
        # 医疗组牺牲直接火力以提高恢复/韧性影响，增加生命与士气
        power = rng.randint(6, 12)
        morale = rng.randint(55, 88)
        hp = rng.randint(80, 110)
    elif unit_type == "狙击组":
        # 狙击组提供高单体输出与士气加成，增强其致命性与生存
        power = rng.randint(14, 22)
        morale = rng.randint(58, 88)
        hp = rng.randint(80, 120)
    elif unit_type == "侦察组":
        # 侦察组更注重生存与侦查，增加机动/生存相关属性
        power = rng.randint(10, 18)
        morale = rng.randint(45, 82)
        hp = rng.randint(75, 110)
        armor = rng.randint(2, 10)
    elif unit_type == "反坦克炮":
        # 提高反坦克炮的耐久与装甲，使其在野战防守中更具存在感
        power = rng.randint(18, 28)
        morale = rng.randint(40, 78)
        hp = rng.randint(110, 170)
        armor = rng.randint(14, 36)
    elif unit_type == "机枪队":
        # 机枪队在防守中更具威胁，增强其压制能力和生存力
        power = rng.randint(16, 26)
        morale = rng.randint(50, 90)
        hp = rng.randint(100, 150)
        armor = rng.randint(6, 16)
    elif unit_type == "88炮":
        # 极高伤害、低耐久：容易被摧毁
        # 保持强力但降低极易被秒的倾向：适度提高血量/装甲下限
        power = rng.randint(20, 28)
        morale = rng.randint(36, 72)
        hp = rng.randint(60, 100)
        armor = rng.randint(14, 30)
    else:
        power = rng.randint(8, 14)
        morale = rng.randint(42, 75)
        hp = rng.randint(75, 115)

    u = GarrisonUnit(unit_type=unit_type, name=name, hp=hp, armor=armor, power=power, morale=morale)
    u.clamp()
    return u


def init_sectors(s: GameState) -> None:
    if s.sectors:
        return
    for k in LOCATIONS.keys():
        # 初始随机但可控：每区至少 3 个驻军，数量与好感/沦陷/地形有关
        favor = s.rng.randint(40, 70)
        fall = s.rng.randint(30, 70)
        units: List[GarrisonUnit] = []
        terrain = MAP_META.get(k, {}).get("terrain")

        # 基于偏好：优先在更稳定或重要辖区多配驻军
        # 提高基础驻军密度以增强防守可信度
        base_count = 4
        if favor >= 55 and fall <= 50:
            base_count += 1
        if fall <= 40:
            base_count += 1 if s.rng.random() < 0.45 else 0

        # 小概率生成额外单位（模拟驻军密度差异）
        if s.rng.random() < 0.22:
            base_count += 1

        # 根据地形微调：例如兵营/货场/车库/阵地更可能出现反坦克或火炮
        heavy_terrain = {"兵营", "车库", "堤坝", "阵地", "港口", "货场"}
        for i in range(base_count):
            force_type = None
            if terrain in heavy_terrain and s.rng.random() < 0.30:
                # 更倾向部署反坦克、机枪或重炮以加强防守
                force_type = s.rng.choices(["反坦克炮", "机枪队", "88炮"], weights=[0.5, 0.35, 0.15], k=1)[0]
            # 生成时传入 terrain 与可能的强制类型以影响选择
            unit = _make_garrison_unit(s.rng, terrain, force_type=force_type)
            units.append(unit)

        s.sectors[k] = SectorState(favor=favor, fall=fall, garrison_units=units)


def init_crew(s: GameState) -> None:
    # 允许在已有成员时“按缺编补齐”，避免剧情/招募先于初始化导致岗位缺失
    used = set()
    for m in s.crew:
        try:
            used.add(str(getattr(m, "name", "")))
        except Exception:
            pass
    for role in CREW_ROLES:
        # 设计：开局装填手由剧情加入的“亚历克斯”担任，初始车组不生成装填手
        if role == "装填手":
            continue
        # 若该岗位已有人（存活或已编入），不重复生成
        try:
            if any(getattr(m, "role", None) == role for m in s.crew):
                continue
        except Exception:
            pass
        if role == "车长":
            # 车长即玩家本人：不受“士气低落/逃跑”影响（不参与随机伤亡抽取与回合压力增长）
            name = s.name
        else:
            name = s.rng.choice(CREW_NAMES)
            while name in used or name == s.name:
                name = s.rng.choice(CREW_NAMES)
        used.add(name)
        # 初始车组：熟练度初始为 50
        s.crew.append(CrewMember(role=role, name=name, proficiency=50))


def _member_proficiency(m: CrewMember) -> int:
    try:
        return max(0, int(getattr(m, "proficiency", 0) or 0))
    except Exception:
        return 0


def crew_role_proficiency(s: GameState, role: str) -> int:
    """返回指定岗位存活成员中的最高熟练度（缺员则返回 0）。"""
    # 兼容：机械师职责与驾驶员融合
    if role == "机械师":
        role = "驾驶员"
    try:
        alive = [m for m in s.crew if m.alive and m.role == role]
    except Exception:
        alive = []
    if not alive:
        return 0
    return max(_member_proficiency(m) for m in alive)


def crew_role_state(s: GameState, role: str) -> str:
    """返回指定岗位状态：ok / wounded / missing。

    - ok：至少有一名该岗位乘员存活且HP>60
    - wounded：该岗位仍有人存活，但全部HP<=60
    - missing：该岗位无人存活（或从未编入）
    """
    # 兼容：机械师职责与驾驶员融合
    if role == "机械师":
        role = "驾驶员"
    alive = [m for m in s.crew if m.alive and m.role == role]
    if not alive:
        return "missing"
    if any(m.hp > 60 for m in alive):
        return "ok"
    return "wounded"


def _commander_available_for_cover(s: GameState) -> bool:
    """车长是否能顶替其他岗位。

    规则：只要车长仍存活即可顶替；不要求 HP>60。
    （注意：不修改 crew_role_state 的语义，避免影响剧情“缺编补齐”等逻辑。）
    """
    try:
        return any(m.alive and m.role == "车长" and int(getattr(m, "hp", 0) or 0) > 0 for m in s.crew)
    except Exception:
        return False


def crew_effective_role_state(s: GameState, role: str) -> str:
    """返回“可用于执行工作”的岗位状态。

    - 机枪由通信员决定；
    - 车长可以顶替其他任一岗位：当岗位非 ok（missing/wounded）时，若车长可用，则视作 ok。
    """
    role = str(role)
    base = crew_role_state(s, role)
    if base == "ok" or role == "车长":
        return base
    if _commander_available_for_cover(s):
        return "ok"
    return base


def crew_effective_role_proficiency(s: GameState, role: str) -> int:
    """返回执行该岗位工作时可用的熟练度（考虑车长顶替）。"""
    role = str(role)
    base = crew_role_proficiency(s, role)
    if role == "车长":
        return base

    state = crew_role_state(s, role)
    # 若岗位非 OK 且车长可顶替，则视作车长在执行（避免“车长给自己加成”重复叠加）
    if state != "ok" and _commander_available_for_cover(s):
        return max(base, crew_role_proficiency(s, "车长"))

    # 岗位可正常执行时：应用“车长经验联动”加成
    commander_prof = crew_role_proficiency(s, "车长")
    try:
        bonus = int(round(float(commander_prof) * float(COMMANDER_LEADERSHIP_SHARE)))
    except Exception:
        bonus = 0
    return max(0, int(base) + int(bonus))


def crew_missing_roles(s: GameState) -> List[str]:
    missing: List[str] = []
    for role in CREW_ROLES:
        if crew_role_state(s, role) == "missing":
            missing.append(role)
    return missing


def crew_impact_notes(s: GameState) -> List[str]:
    """用于提示玩家：当前减员会带来哪些主要影响。"""
    notes: List[str] = []

    driver = crew_role_state(s, "驾驶员")
    gunner = crew_role_state(s, "炮手")
    loader = crew_role_state(s, "装填手")
    radio = crew_role_state(s, "通信员")

    if driver == "missing":
        notes.append("缺少驾驶员（兼机械）：移动/机动更耗油且撤离/突围成功率降低；修理/抢修效果减弱")
    elif driver == "wounded":
        notes.append("驾驶员负伤（兼机械）：机动与撤离效率下降；修理效率下降")

    if gunner == "missing":
        notes.append("缺少炮手：主炮命中显著下降")
    elif gunner == "wounded":
        notes.append("炮手负伤：主炮命中下降")

    if loader == "missing":
        notes.append("缺少装填手：主炮可能装填迟滞（本回合无法开火）")
    elif loader == "wounded":
        notes.append("装填手负伤：主炮偶尔装填迟滞")

    if radio == "missing":
        notes.append("缺少通信员：机枪火力难以组织；电台求援效果减弱")
    elif radio == "wounded":
        notes.append("通信员负伤：机枪火力与电台求援稳定性下降")

    return notes


def crew_status_summary(s: GameState) -> str:
    alive = sum(1 for m in s.crew if m.alive)
    total = len(s.crew)
    wounded = sum(1 for m in s.crew if m.alive and m.hp <= 60)
    missing = crew_missing_roles(s)
    miss_text = f"，缺编{len(missing)}岗" + ("(" + "、".join(missing) + ")" if missing else "")
    return f"乘员{alive}/{total} 存活，伤员{wounded}{miss_text}"


def show_crew(s: GameState) -> None:
    print("\n乘员：")
    for m in s.crew:
        state = "阵亡" if not m.alive else ("受伤" if m.hp <= 60 else "正常")
        prof = _member_proficiency(m)
        print(f"- {m.role} {m.name}：{state}（HP{m.hp}/100 压力{m.stress}/100 熟练度{prof}/100）")


def relieve_crew_stress(
    s: GameState,
    *,
    amount: int,
    mode: str = "all",
    include_commander: bool = True,
    target: Optional[CrewMember] = None,
) -> List[Tuple[CrewMember, int, int]]:
    """缓解乘员压力。

    - mode="all": 对所有存活乘员生效
    - mode="one": 对单名乘员生效（默认从高压力者中抽取）

    返回 (成员, before, after) 列表，便于事件文本展示。
    """

    amt = max(0, int(amount))
    if amt <= 0:
        return []

    eligible = [m for m in s.crew if m.alive and (include_commander or m.role != "车长")]
    if not eligible:
        return []

    chosen: List[CrewMember]
    if mode == "one":
        if target is not None and target in eligible:
            chosen = [target]
        else:
            # 倾向从“压力更高”的成员中抽取
            weights = [max(1, int(m.stress)) for m in eligible]
            try:
                picked = s.rng.choices(eligible, weights=weights, k=1)[0]
            except Exception:
                picked = eligible[0]
            chosen = [picked]
    else:
        chosen = eligible

    out: List[Tuple[CrewMember, int, int]] = []
    for m in chosen:
        before = int(m.stress)
        m.stress = max(0, before - amt)
        m.clamp()
        out.append((m, before, int(m.stress)))
    return out


def apply_stress_relief(s: GameState, spec: object) -> None:
    """按事件配置应用压力缓解，并输出一行简短提示。"""

    mode = "all"
    amount = 0
    include_commander = True

    if isinstance(spec, dict):
        mode = str(spec.get("mode", mode))
        try:
            amount = int(spec.get("amount", 0) or 0)
        except Exception:
            amount = 0
        include_commander = bool(spec.get("include_commander", include_commander))
    elif isinstance(spec, tuple) and len(spec) >= 2:
        mode = str(spec[0])
        try:
            amount = int(spec[1])
        except Exception:
            amount = 0
    elif isinstance(spec, int):
        amount = int(spec)

    results = relieve_crew_stress(
        s,
        amount=amount,
        mode=("one" if mode == "one" else "all"),
        include_commander=include_commander,
    )
    if not results:
        return

    if len(results) == 1:
        m, before, after = results[0]
        print(f"（{m.role}-{m.name} 压力 {before}→{after}）")
        return

    # all 模式：只提示最明显的两名，避免刷屏
    show = sorted(results, key=lambda x: (x[1] - x[2]), reverse=True)[:2]
    parts = [f"{m.role}-{m.name} {before}→{after}" for (m, before, after) in show]
    print("（车组压力略有缓解：" + "；".join(parts) + "）")


def init_ammo_tracking(s: GameState) -> None:
    """初始化弹药跟踪数据结构。

    - 在 `s` 上创建 `_ammo_prev` 快照和 `ammo_usage` 累计字典。
    - 仅在未初始化时执行。
    """
    if getattr(s, "_ammo_tracking_inited", False):
        return
    try:
        s._ammo_prev = {
            "mg_ammo": int(getattr(s, "mg_ammo", 0) or 0),
            "ap_shells": int(getattr(s, "ap_shells", 0) or 0),
            "he_shells": int(getattr(s, "he_shells", 0) or 0),
            # allies: map id -> {"shells": n, "mg": m}
            "allies": {
                id(t): {"shells": int(getattr(t, "shells", 0) or 0), "mg": int(getattr(t, "mg_ammo", 0) or 0)}
                for t in getattr(s, "tank_allies", []) or []
            },
        }
    except Exception:
        s._ammo_prev = {"mg_ammo": 0, "ap_shells": 0, "he_shells": 0, "allies": {}}
    s.ammo_usage = {"player_mg": 0, "player_ap": 0, "player_he": 0, "allies": {}, "allies_mg": {}}
    s._ammo_tracking_inited = True


def record_ammo_deltas(s: GameState) -> None:
    """按回合计算并累计弹药消耗（正数表示消耗量）。

    逻辑：比较上次快照与当前数值的差值（前-后），若为正则累加到 `s.ammo_usage`。
    同时更新快照用于下一回合比较。
    """
    if not getattr(s, "_ammo_tracking_inited", False):
        init_ammo_tracking(s)
    prev = getattr(s, "_ammo_prev", {}) or {}
    try:
        curr_mg = int(getattr(s, "mg_ammo", 0) or 0)
        curr_ap = int(getattr(s, "ap_shells", 0) or 0)
        curr_he = int(getattr(s, "he_shells", 0) or 0)
    except Exception:
        curr_mg = curr_ap = curr_he = 0

    used_mg = max(0, int(prev.get("mg_ammo", 0)) - curr_mg)
    used_ap = max(0, int(prev.get("ap_shells", 0)) - curr_ap)
    used_he = max(0, int(prev.get("he_shells", 0)) - curr_he)

    if not hasattr(s, "ammo_usage") or s.ammo_usage is None:
        s.ammo_usage = {"player_mg": 0, "player_ap": 0, "player_he": 0, "allies": {}}

    s.ammo_usage["player_mg"] = s.ammo_usage.get("player_mg", 0) + used_mg
    s.ammo_usage["player_ap"] = s.ammo_usage.get("player_ap", 0) + used_ap
    s.ammo_usage["player_he"] = s.ammo_usage.get("player_he", 0) + used_he

    # 友军逐辆比对（按 id 快照）
    prev_allies = prev.get("allies", {}) or {}
    curr_allies = {}
    for t in getattr(s, "tank_allies", []) or []:
        tid = id(t)
        prev_entry = prev_allies.get(tid, {}) if isinstance(prev_allies.get(tid, {}), dict) else {"shells": int(prev_allies.get(tid, 0) or 0), "mg": 0}
        before_shells = int(prev_entry.get("shells", int(getattr(t, "shells", 0) or 0)))
        before_mg = int(prev_entry.get("mg", int(getattr(t, "mg_ammo", 0) or 0)))
        curr_shells = int(getattr(t, "shells", 0) or 0)
        curr_mg = int(getattr(t, "mg_ammo", 0) or 0)
        used_shells = max(0, before_shells - curr_shells)
        used_mg = max(0, before_mg - curr_mg)
        name = getattr(t, "name", str(tid))
        if used_shells > 0:
            s.ammo_usage["allies"][name] = s.ammo_usage["allies"].get(name, 0) + used_shells
        if used_mg > 0:
            s.ammo_usage.setdefault("allies_mg", {})
            s.ammo_usage["allies_mg"][name] = s.ammo_usage["allies_mg"].get(name, 0) + used_mg
        curr_allies[tid] = {"shells": curr_shells, "mg": curr_mg}

    # 更新快照
    s._ammo_prev = {"mg_ammo": curr_mg, "ap_shells": curr_ap, "he_shells": curr_he, "allies": curr_allies}


def show_ammo_usage_report(s: GameState) -> None:
    """在控制台输出当前累计的耗弹统计（玩家与友军）。"""
    print("\n耗弹报告：")
    usage = getattr(s, "ammo_usage", None)
    if not usage:
        print("暂无耗弹统计。请先开始游戏以自动跟踪（按回合统计）。")
        return
    print(
        f"玩家 机枪弹: {usage.get('player_mg',0)} 发 | AP炮弹: {usage.get('player_ap',0)} 发 | HE炮弹: {usage.get('player_he',0)} 发"
    )
    allies = usage.get("allies", {}) or {}
    if allies:
        print("友军耗弹：")
        for name, amt in allies.items():
            print(f" - {name}: {amt} 发")
    else:
        print("无友军耗弹记录。")


def tick_round_start(s: GameState) -> None:
    _ensure_weather_state(s)
    # 记录并累计上一回合的弹药消耗变化（以回合为粒度）
    try:
        record_ammo_deltas(s)
    except Exception:
        pass
    # 先基于“上一回合”的行动记录累计疲劳
    prev_moves = int(getattr(s, "moves_this_round", 0))
    prev_battles = int(getattr(s, "battles_this_round", 0))

    fatigue = int(s.counters.get("fatigue", 0))
    fatigue += 1 + prev_moves + prev_battles * 2
    if s.damage >= 70:
        fatigue += 1
    if s.morale <= 30:
        fatigue += 1
    s.counters["fatigue"] = max(0, min(100, fatigue))

    # 肃清敌人计数：基于“上一回合是否移动/探索”累计。
    # - hold_rounds：累计肃清敌人回合数（用于任务板增量计数）
    # - hold_streak：当前连续肃清敌人回合数（备用/显示用）
    try:
        prev_moved = int(s.counters.get("moved_this_round", 0) or 0)
    except Exception:
        prev_moved = 0
    if s.round_number > 1:
        if prev_moved <= 0:
            s.counters["hold_rounds"] = int(s.counters.get("hold_rounds", 0) or 0) + 1
            s.counters["hold_streak"] = int(s.counters.get("hold_streak", 0) or 0) + 1
        else:
            s.counters["hold_streak"] = 0
    s.counters["moved_this_round"] = 0

    s.action_points = 3
    s.moves_this_round = 0
    s.battles_this_round = 0

    # 燃油耗尽倒计时：在回合开始推进一次（用于“空油宽限回合”结局判定）
    try:
        empty_rounds = int(s.counters.get("fuel_empty_rounds", 0) or 0)
    except Exception:
        empty_rounds = 0
    if int(getattr(s, "fuel", 0) or 0) <= 0:
        s.counters["fuel_empty_rounds"] = max(0, empty_rounds) + 1
    else:
        if empty_rounds:
            s.counters["fuel_empty_rounds"] = 0

    # 天气推进（回合开始）
    tick_weather(s)
    # 冷却递减
    for k in list(s.skill_cooldowns.keys()):
        s.skill_cooldowns[k] = max(0, s.skill_cooldowns[k] - 1)
        if s.skill_cooldowns[k] == 0:
            s.skill_cooldowns.pop(k, None)

    # 章节/状态类持续效果递减（仅处理明确是“回合数”的标记）
    if s.debuffs.get("optics_broken", 0) > 0:
        s.debuffs["optics_broken"] = max(0, s.debuffs["optics_broken"] - 1)
        if s.debuffs["optics_broken"] == 0:
            s.debuffs.pop("optics_broken", None)

    # 车辆关键部件故障递减（更真实的战斗后遗症）
    for k in ["gun_breech", "turret_jam", "engine_damage", "radio_damage", "mg_jam"]:
        if s.debuffs.get(k, 0) > 0:
            s.debuffs[k] = max(0, int(s.debuffs[k]) - 1)
            if s.debuffs[k] == 0:
                s.debuffs.pop(k, None)
    if s.buffs.get("强行推进", 0) > 0:
        s.buffs["强行推进"] = max(0, s.buffs["强行推进"] - 1)
        if s.buffs["强行推进"] == 0:
            s.buffs.pop("强行推进", None)
    # 物品/技能类buff递减
    for buff in ["烟幕", "侦察", "伪装", "润滑", "稳固"]:
        if s.buffs.get(buff, 0) > 0:
            s.buffs[buff] = max(0, s.buffs[buff] - 1)
            if s.buffs[buff] == 0:
                s.buffs.pop(buff, None)

    # 轻微自然消耗
    s.morale -= 1
    if s.damage >= 60:
        s.morale -= 1

    # 药品机制：伤员需要持续处置；缺药会带来轻微负担
    wounded = [m for m in s.crew if m.alive and m.role != "车长" and m.hp <= 60]
    if wounded:
        critical = [m for m in wounded if m.hp <= 40]
        if critical and s.inventory.get("药品", 0) > 0:
            # 自动优先照顾重伤：每回合最多消耗1份药品
            spend_item(s, "药品", 1)
            target = s.rng.choice(critical)
            before = target.hp
            heal = s.rng.randint(12, 20)
            target.hp = min(100, target.hp + heal)
            target.stress = max(0, target.stress - 6)
            target.clamp()
            print(f"你们消耗1份药品为重伤员做处置：{target.role}-{target.name} HP {before}→{target.hp}。")
        elif s.inventory.get("药品", 0) <= 0:
            # 缺药：只做简单处置（每回合触发一次，避免过重惩罚）
            target = s.rng.choice(wounded)
            target.stress += 2
            if s.rng.random() < 0.25:
                target.hp = max(0, target.hp - 1)
            target.clamp()
            s.morale -= 1
            print("缺少药品：伤员只能做简单处置，压力上升。")

    # 自动急救包：若有成员生命值低于10且背包中有急救包，则自动使用急救包将其生命提高至40
    low_hp_members = [m for m in s.crew if m.alive and m.hp < 10]
    for m in low_hp_members:
        if s.inventory.get("急救包", 0) > 0:
            used = spend_item(s, "急救包", 1)
            if used:
                before = m.hp
                m.hp = max(m.hp, 40)
                m.clamp()
                print(f"自动使用1个急救包为{m.role}-{m.name}急救：HP {before}→{m.hp}。")
            else:
                break

    # 委派任务：回合推进与到期结算
    tick_delegated_tasks(s)

    # 城市崩溃度：由辖区沦陷与车辆状态叠加
    if s.sectors:
        avg_fall = sum(sec.fall for sec in s.sectors.values()) / max(1, len(s.sectors))
        inc = 1 if avg_fall >= 55 else 0
        inc += 1 if avg_fall >= 70 else 0
        inc += 1 if s.damage >= 75 else 0
        s.city_collapse += inc

    # 检查没有驻军的地区：沦陷度设为满
    for sec_key, sec in s.sectors.items():
        live_garrison = [u for u in sec.garrison_units if u.alive]
        if not live_garrison:
            if sec.fall < 100:
                print(f"⚠️ {sec_key} 地区没有驻军，沦陷度立即升至满值！")
            sec.fall = 100
        sec.clamp()

    # 士气/城市崩溃倒计时：在回合开始推进一次（用于“宽限回合”结局判定）
    try:
        morale_zero_rounds = int(s.counters.get("morale_zero_rounds", 0) or 0)
    except Exception:
        morale_zero_rounds = 0
    if int(getattr(s, "morale", 0) or 0) <= 0:
        s.counters["morale_zero_rounds"] = max(0, morale_zero_rounds) + 1
    else:
        if morale_zero_rounds:
            s.counters["morale_zero_rounds"] = 0

    try:
        collapse_rounds = int(s.counters.get("collapse_max_rounds", 0) or 0)
    except Exception:
        collapse_rounds = 0
    if int(getattr(s, "city_collapse", 0) or 0) >= 100:
        s.counters["collapse_max_rounds"] = max(0, collapse_rounds) + 1
    else:
        if collapse_rounds:
            s.counters["collapse_max_rounds"] = 0

    # 乘员压力随回合累积
    for m in s.crew:
        if not m.alive:
            continue
        if m.role == "车长":
            # 车长不受常规压力增长影响
            continue
        m.stress += 1
        if s.damage >= 70:
            m.stress += 1
        if s.morale <= 30:
            m.stress += 1

    # 救援任务：会出现、会过期（更贴近原作的列表机制）
    if s.rescue_missions:
        s.rescue_missions = [m for m in s.rescue_missions if m.expires_round >= s.round_number]
    if not s.rescue_missions and s.rng.random() < 0.18:
        mid = f"R{s.round_number}"
        s.rescue_missions.append(
            RescueMission(
                id=mid,
                title="院落求援",
                desc="附近院落传来求救信号：可能有伤员/步兵被困。",
                expires_round=s.round_number + 2,
                difficulty=0.55,
            )
        )

    # 驻军概率补充弹药：机枪弹更常见，炮弹更稀有
    maybe_garrison_resupply(s)
    # 驻军/溃散人员补入：更像“增援率”
    maybe_garrison_reinforcement(s)
    s.clamp()


def maybe_garrison_resupply(s: GameState) -> None:
    sec = s.sectors.get(s.location_key)
    if sec is None or not sec.garrison_units:
        return

    # 好感越高越愿意补给；沦陷越高越难腾出物资
    p = 0.22
    p += (sec.favor - 40) * 0.002
    p -= (sec.fall - 45) * 0.0015
    p = max(0.04, min(0.42, p))
    if s.rng.random() >= p:
        return

    mg_gain = s.rng.randint(32, 70) + min(30, len(sec.garrison_units) * 6)
    s.mg_ammo += mg_gain

    ap_gain = 0
    he_gain = 0
    r = s.rng.random()
    if r < 0.25:
        ap_gain = 1
        s.ap_shells += 1
    elif r < 0.40:
        he_gain = 1
        s.he_shells += 1

    if ap_gain or he_gain:
        print(f"\n📦 驻军送来补给：机枪弹+{mg_gain}，并额外补充炮弹（AP+{ap_gain} HE+{he_gain}）。")
    else:
        print(f"\n📦 驻军送来补给：机枪弹+{mg_gain}。")


def maybe_garrison_reinforcement(s: GameState) -> None:
    """驻军/溃散人员补入（回合开始判定）。

    - 好感越高、沦陷越低，越可能补入一个单位。
    - 单位类型会受地形偏好影响（包括机枪队/88炮）。
    """
    sec = s.sectors.get(s.location_key)
    if sec is None:
        return

    # 上限：避免辖区驻军无限膨胀
    if len(sec.garrison_units) >= 10:
        return

    p = 0.06
    p += (sec.favor - 50) * 0.0022
    p -= (sec.fall - 55) * 0.0020
    if s.morale >= 70:
        p += 0.01
    if s.damage >= 75:
        p -= 0.01
    p = max(0.02, min(0.22, p))

    if s.rng.random() >= p:
        return

    terrain = MAP_META.get(s.location_key, {}).get("terrain")
    sec.garrison_units.append(_make_garrison_unit(s.rng, terrain))
    sec.favor = min(100, sec.favor + 1)
    print("\n🪖 驻军得到补入：街区里有人重新被组织起来，加入当地防线。")
    sec.clamp()


def complete_quests_if_any(s: GameState) -> None:
    for q in s.quests:
        if q.done and q.reward_points > 0:
            print(f"\n🎯 任务完成：{q.title}（+{q.reward_points}胜利点）")
            s.victory_points += q.reward_points
            q.reward_points = 0

            # 任务完成附带效果（不增加新页面/菜单）
            if q.id == "Q4" and not bool(getattr(s, "story_flags", {}).get("signal_net", False)):
                s.story_flags["signal_net"] = True
                s.buffs["侦察"] = max(1, int(s.buffs.get("侦察", 0) or 0))
                print("你们把零散的电池与器材拼成一套更可靠的联络：获得一次侦察优势。")

            if q.id == "Q5":
                # 让任务链对系统有真实反馈：提供突围情报倾向（与既有 attempt_escape 接口对齐）
                if not bool(getattr(s, "story_flags", {}).get("escape_intel", False)):
                    s.story_flags["escape_intel"] = True
                    print("你们把那段断续信号的坐标拼合起来：突围情报已记录。")

                # 分支结算：共享→提升辖区好感；私下交易→获得金条
                if bool(getattr(s, "story_flags", {}).get("q5_shared_garrison", False)):
                    sec = s.sectors.get(s.location_key)
                    if sec is not None:
                        sec.favor += 6
                        sec.clamp()
                    print("驻军收到你们的记录：街区里的人更愿意帮你们一把。")
                elif bool(getattr(s, "story_flags", {}).get("q5_trade", False)):
                    s.gold_bars += 1
                    print("你们把信息换成了实打实的筹码：获得1根金条。")

            s.clamp()

    # 完成即刷新：仅针对任务板支线任务（refreshable=True）
    try:
        refresh_side_quests(s, keep=2)
    except Exception:
        pass


def show_quests(s: GameState) -> None:
    if not s.quests:
        return
    # 已结算完成的任务默认不展示，避免列表越积越长；但任务数据仍保留（用于解锁/剧情判断）。
    qs = [
        q
        for q in s.quests
        if (not bool(getattr(q, "done", False)))
        or int(getattr(q, "reward_points", 0) or 0) > 0
        or bool(getattr(q, "refreshable", False))
    ]
    if not qs:
        return
    print("\n任务：")
    for q in qs:
        flag = "已完成" if q.done else "进行中"
        print(f"- {q.title} [{flag}] {q.progress}/{q.target}")


def show_task_requirements() -> None:
    print("\n任务要求/说明：")
    print("- 任务进度：显示为 progress/target，达到 target 即视为完成。")
    print("- 任务板支线：部分任务可刷新；完成后会自动补新，保持任务板常有可做项。")
    print("- 救援任务：有剩余回合数；到期会失效，建议优先处理或尽快委派。")
    print("- 委派任务：到期自动结算；成功率受任务风险、单位战力/士气、辖区态势影响。")
    print("- 胜利点：完成任务/委托可获得胜利点，用于提升勋章等级；任务日志记录最近变化。")


def _log_task(s: GameState, text: str) -> None:
    stamp = f"[回合{s.round_number}]"
    s.task_log.append(f"{stamp} {text}")
    # 控制长度，避免无限增长
    if len(s.task_log) > 40:
        s.task_log = s.task_log[-40:]


def show_rescue_missions(s: GameState) -> None:
    if not s.rescue_missions:
        return
    print("\n救援任务：")
    for i, m in enumerate(s.rescue_missions, 1):
        left = max(0, m.expires_round - s.round_number)
        print(f"- ({i}) {m.title} [剩余{left}回合] 难度{_pct(getattr(m, 'difficulty', 0.0))}%")


def show_delegated_tasks(s: GameState) -> None:
    active = [t for t in s.delegated_tasks if t.status == "active"]
    done = [t for t in s.delegated_tasks if t.status != "active"]
    if not active and not done:
        return

    if active:
        print("\n委派任务：")
        for t in active:
            who = t.assigned_unit.name if t.assigned_unit else "(未知单位)"
            print(f"- {t.title} [{who}] 剩余{t.remaining_rounds}回合")
    if done:
        recent = done[-4:]
        print("\n委派记录（最近）：")
        for t in recent:
            who = t.assigned_unit.name if t.assigned_unit else "(未知单位)"
            flag = "成功" if t.status == "success" else ("失败" if t.status == "failed" else "取消")
            msg = (t.result_text or "").strip()
            extra = f"：{msg}" if msg else ""
            print(f"- {t.title} [{who}] {flag}{extra}")


def show_task_log(s: GameState) -> None:
    if not s.task_log:
        return
    print("\n任务日志（最近）：")
    for line in s.task_log[-8:]:
        print(f"- {line}")


def _delegation_success_chance(s: GameState, task: DelegatedTask) -> float:
    # 基础：风险越高成功率越低；单位战力/士气、辖区态势会影响结果
    p = 1.0 - float(task.base_risk)
    u = task.assigned_unit
    if u is not None:
        p += (u.power - 10) * 0.012
        p += (u.morale - 50) * 0.004
    sec = s.sectors.get(task.origin_sector_key)
    if sec is not None:
        p -= (sec.fall - 50) * 0.004
        p += (sec.favor - 50) * 0.002
    # 夹紧
    return max(0.05, min(0.95, p))


def tick_delegated_tasks(s: GameState) -> None:
    if not s.delegated_tasks:
        return
    for t in s.delegated_tasks:
        if t.status != "active":
            continue
        t.remaining_rounds -= 1
        if t.remaining_rounds > 0:
            continue

        u = t.assigned_unit
        p = _delegation_success_chance(s, t)
        roll = s.rng.random()
        sec = s.sectors.get(t.origin_sector_key)

        if roll < p:
            t.status = "success"
            # 奖励
            if t.reward_points:
                s.victory_points += int(t.reward_points)
            for item, qty in t.reward_items.items():
                add_item(s, item, int(qty))
            for k, v in t.counter_effects.items():
                s.counters[k] = s.counters.get(k, 0) + int(v)
            for qid, amt in t.quest_progress.items():
                _quest_progress(s, qid, int(amt))
            if sec is not None:
                sec.favor += int(t.sector_favor_delta)
                sec.fall += int(t.sector_fall_delta)
            if u is not None:
                u.morale += 4
                u.clamp()
            # 归队
            if u is not None and u.alive and sec is not None:
                sec.garrison_units.append(u)
            t.result_text = f"完成，成功率{int(p*100)}%"
            print(f"\n✅ 委派完成：{t.title}（成功）")
            _log_task(s, f"委派任务完成：{t.title}（成功）")
        else:
            t.status = "failed"
            lost = False
            if u is not None:
                # 失败可能导致减员
                loss_p = 0.18 + float(t.base_risk) * 0.35
                if s.rng.random() < max(0.05, min(0.7, loss_p)):
                    u.alive = False
                    lost = True
                else:
                    u.morale -= 8
                    u.power = max(1, u.power - 1)
                    u.clamp()
            if sec is not None:
                sec.favor -= 4
                sec.fall += 4
            if (u is not None) and (not lost) and u.alive and sec is not None:
                sec.garrison_units.append(u)
            tail = "小队失联" if lost else f"未达成目标（成功率{int(p*100)}%）"
            t.result_text = tail
            print(f"\n⚠️ 委派失败：{t.title}（{tail}）")
            _log_task(s, f"委派任务失败：{t.title}（{tail}）")

    s.clamp()


def _available_delegation_templates(s: GameState) -> List[Dict[str, object]]:
    # 模板尽量“轻”，只提供少量高价值选项。
    templates: List[Dict[str, object]] = []
    templates.append(
        {
            "key": "recon",
            "title": "街区侦察",
            "desc": "派出小队探明路口火力与封锁线，降低后续行动的突然性。",
            "kind": "侦察",
            "rounds": 1,
            "risk": 0.42,
            "rewards": {"vp": 1},
            "counters": {"explore": 1},
            "favor": 2,
            "fall": -1,
        }
    )
    templates.append(
        {
            "key": "scavenge",
            "title": "搜集补给",
            "desc": "在废墟间搜集可用物资，收益不稳定但长期回报可观。",
            "kind": "补给",
            "rounds": 2,
            "risk": 0.50,
            "rewards": {"vp": 1, "items": {"弹药箱": 1}},
            "counters": {"scavenge": 1},
            "favor": 1,
            "fall": 0,
        }
    )
    templates.append(
        {
            "key": "escort",
            "title": "护送撤离",
            "desc": "护送一批平民/伤员穿过炮火线，可能显著提升辖区好感。",
            "kind": "撤离",
            "rounds": 1,
            "risk": 0.55,
            "rewards": {"vp": 2},
            "quests": {"Q1": 1},
            "counters": {"assist": 1},
            "favor": 6,
            "fall": -2,
        }
    )
    templates.append(
        {
            "key": "repair_parts",
            "title": "搜罗备件",
            "desc": "从车库/工地搜罗备件与工具，用于后续修理。",
            "kind": "维修",
            "rounds": 1,
            "risk": 0.38,
            "rewards": {"vp": 1, "items": {"备件": 1}},
            "counters": {"repairs": 1},
            "favor": 0,
            "fall": 0,
        }
    )

    # 若当前存在救援任务，允许把它转为委派执行
    if s.rescue_missions:
        m = s.rescue_missions[0]
        left = max(0, m.expires_round - s.round_number)
        rescue_risk = 0.45 + float(m.difficulty) * 0.6
        rescue_risk = max(0.05, min(0.95, rescue_risk))
        templates.insert(
            0,
            {
                "key": "rescue",
                "title": f"委派救援：{m.title}",
                "desc": f"{m.desc}（窗口剩余{left}回合）",
                "kind": "救援",
                "rounds": 1,
                "risk": rescue_risk,
                "rewards": {"vp": 3},
                "quests": {"Q1": 1},
                "favor": 4,
                "fall": -1,
                "consume_rescue": True,
            },
        )
    return templates


def menu_delegation(ins: InputStream, s: GameState) -> None:
    sec = s.sectors.get(s.location_key)
    if sec is None or not sec.garrison_units:
        print("\n当前辖区没有可用驻军小队可供委派。")
        return

    print("\n可委派驻军小队：")
    unit_map: Dict[str, GarrisonUnit] = {}
    for i, u in enumerate(sec.garrison_units, 1):
        key = str(i)
        unit_map[key] = u
        print(f"- ({key}) {u.name}：{u.unit_type} 战力{u.power} 士气{u.morale}")
    unit_choice = choose(ins, "选择小队编号(回车取消)：", unit_map, default="")
    if unit_choice not in unit_map:
        return

    templates = _available_delegation_templates(s)
    if not templates:
        print("\n当前没有可委派的任务选项。")
        return

    print("\n可委派任务：")
    tmap: Dict[str, Dict[str, object]] = {}
    for i, t in enumerate(templates, 1):
        key = str(i)
        tmap[key] = t
        rr = _pct(t.get("risk", 0.5))
        rounds = int(t.get("rounds", 1))
        print(f"- ({key}) {t.get('title')}（预计{rounds}回合，风险{rr}%）")
    tc = choose(ins, "选择任务编号(回车取消)：", tmap, default="")
    if tc not in tmap:
        return

    template = tmap[tc]
    u = unit_map[unit_choice]

    # 从辖区移出该单位（占用中）
    try:
        sec.garrison_units.remove(u)
    except ValueError:
        pass

    tid = f"D{s.round_number}-{len(s.delegated_tasks)+1}"
    task = DelegatedTask(
        id=tid,
        title=str(template.get("title")),
        desc=str(template.get("desc")),
        kind=str(template.get("kind")),
        origin_sector_key=s.location_key,
        start_round=s.round_number,
        remaining_rounds=int(template.get("rounds", 1)),
        base_risk=float(template.get("risk", 0.5)),
        assigned_unit=u,
        reward_points=int((template.get("rewards") or {}).get("vp", 0) if isinstance(template.get("rewards"), dict) else 0),
        reward_items=dict((template.get("rewards") or {}).get("items", {}) if isinstance(template.get("rewards"), dict) else {}),
        counter_effects=dict(template.get("counters", {}) if isinstance(template.get("counters"), dict) else {}),
        quest_progress=dict(template.get("quests", {}) if isinstance(template.get("quests"), dict) else {}),
        sector_favor_delta=int(template.get("favor", 0)),
        sector_fall_delta=int(template.get("fall", 0)),
    )
    s.delegated_tasks.append(task)
    _log_task(s, f"已委派{u.name}执行：{task.title}（预计{task.remaining_rounds}回合）")
    print(f"\n📨 已委派：{u.name} → {task.title}（预计{task.remaining_rounds}回合）")

    # 若委派的是救援任务，则消费掉该救援窗口（避免重复派发）
    if bool(template.get("consume_rescue")) and s.rescue_missions:
        s.rescue_missions.pop(0)
    s.clamp()


def show_sector_overview(s: GameState) -> None:
    print("\n辖区概览：")
    for k, v in LOCATIONS.items():
        sec = s.sectors.get(k)
        if sec is None:
            continue
        mark = "(当前)" if k == s.location_key else ""
        unit_count = len(sec.garrison_units)
        # 汇总类型与平均能力
        type_counts: Dict[str, int] = {}
        avg_power = 0
        avg_morale = 0
        if unit_count > 0:
            for u in sec.garrison_units:
                type_counts[u.unit_type] = type_counts.get(u.unit_type, 0) + 1
                avg_power += u.power
                avg_morale += u.morale
            avg_power = int(avg_power / unit_count)
            avg_morale = int(avg_morale / unit_count)
        summary = ", ".join(f"{t}x{c}" for t, c in type_counts.items()) if type_counts else "无"
        print(f"- {v['name']}{mark}：好感{sec.favor} 沦陷{sec.fall} 驻军{unit_count}（{summary}） 平均战力{avg_power} 平均士气{avg_morale}")


def apply_item_effect(s: GameState, item: str) -> None:
    # “开箱/使用补给”的产出倍率：想更夸张就继续调大
    LOOT_OPEN_MULT = 4
    if item == "燃油桶":
        s.fuel += 15 * LOOT_OPEN_MULT
    elif item == "弹药箱":
        s.mg_ammo += 45 * LOOT_OPEN_MULT
        # 炮弹较稀有：弹药箱里小概率夹带
        r = s.rng.random()
        if r < 0.20:
            s.ap_shells += 1 * LOOT_OPEN_MULT
            print(f"弹药箱里还夹着{1 * LOOT_OPEN_MULT}发AP炮弹。")
        elif r < 0.35:
            s.he_shells += 1 * LOOT_OPEN_MULT
            print(f"弹药箱里还夹着{1 * LOOT_OPEN_MULT}发HE炮弹。")
    elif item == "炮弹箱":
        ap_gain = 2
        he_gain = 2
        s.ap_shells += ap_gain
        s.he_shells += he_gain
        print(f"炮弹补充：AP+{ap_gain} HE+{he_gain}。")
    elif item == "烟幕弹":
        # 标记：下一次遭遇可无损撤离
        s.buffs["烟幕"] = 1
    elif item in ("香烟", "急救包"):
        s.morale += 8
    elif item == "备件":
        # 备件默认不直接使用，保留给修理/技能
        s.damage = max(0, s.damage - 10)
    elif item == "电台电池":
        s.buffs["电台电量"] = s.buffs.get("电台电量", 0) + 1
    elif item == "地图碎片":
        pass
    elif item == "医疗包":
        # 治疗伤员：随机缓解一名“负伤/状态较差”的乘员
        candidates = [m for m in s.crew if m.alive and m.role != "车长" and m.hp <= 60]
        if candidates:
            target = s.rng.choice(candidates)
            before = target.hp
            target.hp = min(100, target.hp + 35)
            target.stress = max(0, target.stress - 10)
            target.clamp()
            print(f"医疗包生效：{target.role}-{target.name} HP {before}→{target.hp}。")
        else:
            print("医疗包生效：但车组无人明显负伤，士气+5。")
            s.morale += 5
    elif item == "药品":
        # 药品：更适合在“有人受伤但未必到需要医疗包”的情况下使用
        candidates = [m for m in s.crew if m.alive and m.role != "车长" and m.hp <= 70]
        if candidates:
            target = s.rng.choice(candidates)
            before = target.hp
            heal = s.rng.randint(18, 28)
            target.hp = min(100, target.hp + heal)
            target.stress = max(0, target.stress - 8)
            target.clamp()
            print(f"药品生效：为{target.role}-{target.name}处置伤情（HP {before}→{target.hp}）。")
        else:
            print("药品生效：但当前没有需要处理的伤员，士气+3。")
            s.morale += 3
    elif item == "工具箱":
        # 高级修理：损伤-20，士气+5
        s.damage = max(0, s.damage - 20)
        s.morale += 5
        # 同时尽量压制若干“关键部件故障”（不保证完全排除）
        fixed_any = False
        for k in ["gun_breech", "turret_jam", "engine_damage", "radio_damage", "mg_jam", "optics_broken"]:
            if s.debuffs.get(k, 0) > 0:
                s.debuffs[k] = max(0, int(s.debuffs.get(k, 0)) - 1)
                if s.debuffs.get(k, 0) <= 0:
                    s.debuffs.pop(k, None)
                fixed_any = True
        if fixed_any:
            print("工具箱生效：损伤减少20，士气+5，并对车辆故障做了压制处理。")
        else:
            print("工具箱生效：损伤减少20，士气+5。")
    elif item == "侦察设备":
        # 提供情报：下次移动时避免随机遭遇
        s.buffs["侦察"] = 1
        print("侦察设备生效：下次移动避免随机遭遇。")
    elif item == "伪装网":
        # 减少被发现：遭遇风险-20%，持续2回合
        s.buffs["伪装"] = max(s.buffs.get("伪装", 0), 2)  # 至少2回合
        print("伪装网生效：遭遇风险降低，持续2回合。")
    elif item == "纯燃料桶":
        # 燃油+30（稀有）
        s.fuel += 30 * LOOT_OPEN_MULT
        print(f"纯燃料桶生效：燃油+{30 * LOOT_OPEN_MULT}。")
    elif item == "弹药":
        # 机枪弹+70，炮弹AP+2 HE+2
        s.mg_ammo += 70 * LOOT_OPEN_MULT
        s.ap_shells += 2 * LOOT_OPEN_MULT
        s.he_shells += 2 * LOOT_OPEN_MULT
        print(f"弹药生效：机枪弹+{70 * LOOT_OPEN_MULT}，AP+{2 * LOOT_OPEN_MULT}，HE+{2 * LOOT_OPEN_MULT}。")
    elif item == "咖啡":
        # 提振精神：士气+15，行动点+1，但有小概率过度刺激导致士气-5
        s.morale += 15
        s.action_points += 1
        if s.rng.random() < 0.1:
            s.morale -= 5
            print("咖啡生效：士气+15，行动点+1。但过度刺激导致士气-5。")
        else:
            print("咖啡生效：士气+15，行动点+1。")
    elif item == "装甲板":
        before = player_armor_rating(s)
        if s.armor_plates >= ARMOR_PLATE_MAX:
            print("装甲板无法继续加装：已达到上限。")
        else:
            s.armor_plates += 1
            after = player_armor_rating(s)
            print(f"你们把装甲板焊到车体薄弱处：装甲 {before}→{after}。")
    elif item == "":
        before = int(s.counters.get("fatigue", 0))
        s.counters["fatigue"] = max(0, before - 15)
        s.morale += 2
        print(f"口粮下肚：疲劳 {before}→{int(s.counters.get('fatigue', 0))}，士气+2。")
    elif item == "润滑油":
        if s.debuffs.get("mg_jam", 0) > 0:
            s.debuffs.pop("mg_jam", None)
            print("润滑油生效：机枪卡壳已清除。")
        s.buffs["润滑"] = max(2, int(s.buffs.get("润滑", 0) or 0))
        print("润滑油生效：机枪状态更顺畅（2回合）。")
    s.clamp()


def maybe_resupply_shells_from_inventory(ins: "InputStream", s: GameState, *, want_shell: str) -> bool:
    """当主炮弹不足时，允许从背包中临时补充。

    - 不新增战斗菜单项：只在玩家尝试开火但弹药为 0 时提示。
    - 优先使用更“直接补弹”的物资：弹药/炮弹箱，其次才是弹药箱。
    """
    want_shell = str(want_shell).upper().strip() or "AP"
    if want_shell not in ("AP", "HE"):
        want_shell = "AP"

    if want_shell == "AP" and int(getattr(s, "ap_shells", 0) or 0) > 0:
        return False
    if want_shell == "HE" and int(getattr(s, "he_shells", 0) or 0) > 0:
        return False

    candidates: List[Tuple[str, str]] = []
    if int(s.inventory.get("弹药", 0) or 0) > 0:
        candidates.append(("弹药", "机枪弹与AP/HE炮弹都会补充（更稳定）"))
    if int(s.inventory.get("炮弹箱", 0) or 0) > 0:
        tip = "补充AP/HE炮弹（数量随机）"
        if want_shell == "HE":
            tip += "；注意：结果为随机，不保证一定出HE"
        candidates.append(("炮弹箱", tip))
    if int(s.inventory.get("弹药箱", 0) or 0) > 0:
        tip = "补充机枪弹（并有小概率夹带炮弹）"
        candidates.append(("弹药箱", tip))

    if not candidates:
        return False

    shell_name = "AP" if want_shell == "AP" else "HE"
    have_ap = int(getattr(s, "ap_shells", 0) or 0)
    have_he = int(getattr(s, "he_shells", 0) or 0)
    print(f"\n主炮{shell_name}炮弹不足（当前 AP{have_ap} / HE{have_he}）。")

    if len(candidates) == 1:
        item, tip = candidates[0]
        menu = {
            "1": f"使用背包物资：{item} x1（{tip}）",
            "2": "暂不使用",
        }
    else:
        menu = {"0": "暂不使用"}
        for i, (item, tip) in enumerate(candidates, 1):
            menu[str(i)] = f"使用：{item} x1（{tip}）"

    default_choice = "1" if getattr(ins, "default_when_empty", False) else ("2" if "2" in menu else "0")
    pick = choose(ins, "是否使用背包物资补充炮弹？", menu, default=default_choice)

    if pick in ("0", "2"):
        return False

    if pick == "1" and "1" in menu and len(candidates) == 1:
        item = candidates[0][0]
    else:
        try:
            idx = int(pick) - 1
        except Exception:
            return False
        if idx < 0 or idx >= len(candidates):
            return False
        item = candidates[idx][0]

    if not spend_item(s, item, 1):
        return False
    print(f"你让装填手撬开并取用：{item}。")
    apply_item_effect(s, item)
    return True


def craft_item(ins: InputStream, s: GameState) -> None:
    print("\n合成配方：")
    recipes = {
        "炮弹箱": {"弹药箱": 1, "desc": "用1个弹药箱改装成炮弹箱（更偏主炮补给）"},
        "工具箱": {"备件": 2, "desc": "用2个备件合成工具箱"},
        "纯燃料桶": {"燃油桶": 2, "desc": "用2个燃油桶合成纯燃料桶"},
        "弹药": {"弹药箱": 2, "desc": "用2个弹药箱合成弹药"},
        "口粮": {"香烟": 1, "desc": "用1包香烟换取一份口粮（疲劳缓解）"},
        "润滑油": {"备件": 1, "desc": "用1个备件换取一瓶润滑油（机枪更可靠）"},
    }
    for idx, (result, req) in enumerate(recipes.items(), 1):
        desc = req["desc"]
        print(f"{idx}. {desc}")
    print("0. 返回")
    raw = get_valid_input(ins, "选择合成配方编号：", default="0")
    if raw == "0":
        return
    try:
        idx = int(raw)
    except ValueError:
        print("输入无效。")
        return
    if idx < 1 or idx > len(recipes):
        print("无效编号。")
        return
    result = list(recipes.keys())[idx - 1]
    req = recipes[result]
    req_items = {k: v for k, v in req.items() if k != "desc"}
    can_craft = True
    for item, cnt in req_items.items():
        if s.inventory.get(item, 0) < cnt:
            can_craft = False
            break
    if not can_craft:
        print("材料不足。")
        return
    for item, cnt in req_items.items():
        spend_item(s, item, cnt)
    add_item(s, result, 1)
    print(f"合成成功：{result}")


def menu_inventory(ins: InputStream, s: GameState) -> None:
    if not s.inventory:
        print("背包为空。")
        return
    print("\n背包：")
    items = list(s.inventory.items())
    for idx, (name, cnt) in enumerate(items, 1):
        desc = ITEMS.get(name, {}).get("desc", "")
        print(f"{idx}. {name} x{cnt}（{desc}）")
    print("0. 返回")
    print("s. 合成物品")
    raw = get_valid_input(ins, "选择要使用的物品编号（或0返回，s合成）：", default="0")
    if raw == "0":
        return
    if raw.lower() == "s":
        craft_item(ins, s)
        return
    try:
        idx = int(raw)
    except ValueError:
        print("输入无效。")
        return
    if idx < 1 or idx > len(items):
        print("无效编号。")
        return
    name, _ = items[idx - 1]
    if not spend_item(s, name, 1):
        print("物品不足。")
        return
    apply_item_effect(s, name)
    print(f"已使用：{name}")


def can_use_skill(s: GameState, skill: str) -> Tuple[bool, str]:
    if skill in s.skill_cooldowns:
        return False, f"冷却中（剩余{s.skill_cooldowns[skill]}回合）"
    # 突围难度：释放技能不消耗物品，因此也不需要物品前置。
    if not is_breakout_mode(s):
        if skill == "紧急抢修" and s.inventory.get("备件", 0) <= 0:
            return False, "需要1个备件"
        if skill == "电台求援" and s.inventory.get("电台电池", 0) <= 0:
            return False, "需要1个电台电池"
    return True, ""


def use_skill(s: GameState, skill: str) -> None:
    cd = SKILLS[skill]["cooldown"]
    if skill == "鼓舞":
        s.morale += 45
    elif skill == "观察":
        s.buffs["观察"] = 1
    elif skill == "紧急抢修":
        if not is_breakout_mode(s):
            spend_item(s, "备件", 1)
        mech = crew_role_state(s, "驾驶员")
        if mech == "missing":
            # 缺少驾驶员（兼机械）时依然能做“更强的应急处置”，但会受限
            s.damage = max(0, s.damage - 22)
            s.morale += 2
            print("缺少驾驶员（兼机械）：紧急抢修只能做有限处置（效果减弱）。")
        elif mech == "wounded":
            s.damage = max(0, s.damage - 30)
            s.morale += 3
            print("驾驶员负伤（兼机械）：紧急抢修效果略受影响。")
        else:
            s.damage = max(0, s.damage - 40)
            s.morale += 4

        # 额外：尽量排除战斗中常见的“卡滞/故障”（不改变技能入口，仅增强真实感）
        if mech != "missing":
            fix = 2 if mech == "wounded" else 4
            for k in ["gun_breech", "turret_jam", "engine_damage", "radio_damage", "optics_broken"]:
                if s.debuffs.get(k, 0) > 0:
                    s.debuffs[k] = max(0, int(s.debuffs[k]) - fix)
                    if s.debuffs[k] == 0:
                        s.debuffs.pop(k, None)
    elif skill == "电台求援":
        if not is_breakout_mode(s):
            spend_item(s, "电台电池", 1)
        s.buffs["求援"] = 1
        radio = crew_role_state(s, "通信员")
        if radio == "missing":
            s.morale += 1
            print("缺少通信员：电台求援联系不稳（效果减弱）。")
        elif radio == "wounded":
            s.morale += 1
            print("通信员负伤：电台求援稳定性下降（效果略受影响）。")
        else:
            s.morale += 2
    elif skill == "稳固阵位":
        # 战斗增益：不直接加数值，避免通用失衡；由遭遇战读取并转换为战斗内短效
        s.buffs["稳固"] = max(2, int(s.buffs.get("稳固", 0) or 0))
        s.morale += 1
    s.skill_cooldowns[skill] = cd
    s.clamp()


def menu_skills(ins: InputStream, s: GameState) -> None:
    print("\n技能：")
    skills = list(SKILLS.keys())
    for idx, sk in enumerate(skills, 1):
        ok, why = can_use_skill(s, sk)
        state = "可用" if ok else f"不可用：{why}"
        print(f"{idx}. {sk}（{SKILLS[sk]['desc']}）[{state}]")
    print("0. 返回")
    raw = get_valid_input(ins, "选择技能编号（或0返回）：", default="0")
    if raw == "0":
        return
    try:
        idx = int(raw)
    except ValueError:
        print("输入无效。")
        return
    if idx < 1 or idx > len(skills):
        print("无效编号。")
        return
    sk = skills[idx - 1]
    ok, why = can_use_skill(s, sk)
    if not ok:
        print(f"无法使用：{why}")
        return
    use_skill(s, sk)
    print(f"已发动技能：{sk}")


def _build_location_unique_events(s: "GameState") -> List[Dict[str, object]]:
    """为当前地区构建专属随机事件（每地区 5-10 条）。

    设计目标：
    - 每个地区默认生成 7-9 条“只在该地区出现”的专属事件（更像手写场景，而非纯模板句）。
    - 不引入新系统：沿用 random_event 的既有字段（type/delta/item/buff/choice/require_ap/require_item...）。
    - 专属事件默认标记 once=True：结算后写入 shown_events，避免同一存档反复刷同一条。
    - 数值影响克制：以小幅资源/士气/损伤变化为主，避免破坏难度曲线。
    """
    key = str(getattr(s, "location_key", "") or "")
    loc = LOCATIONS.get(key, {}) if isinstance(LOCATIONS, dict) else {}
    meta = MAP_META.get(key, {}) if isinstance(MAP_META, dict) else {}

    name = str(loc.get("name", "未知地区")) if isinstance(loc, dict) else "未知地区"
    terrain = str(meta.get("terrain", "")) if isinstance(meta, dict) else ""
    desc = ""
    tags: List[str] = []
    try:
        if isinstance(loc, dict):
            d0 = loc.get("desc")
            if isinstance(d0, str) and d0.strip():
                desc = d0.strip()
            t0 = loc.get("tags")
            if isinstance(t0, list):
                tags = [str(x) for x in t0 if str(x).strip()]
    except Exception:
        desc = ""
        tags = []

    def _seen(eid: str) -> bool:
        return isinstance(eid, str) and bool(eid) and (eid in getattr(s, "shown_events", set()) or set())

    def _mk_simple(*, suffix: str, text: str, delta: Optional[Dict[str, int]] = None,
                   item: Optional[Tuple[str, int]] = None, buff: Optional[Tuple[str, int]] = None,
                   sector: Optional[Dict[str, int]] = None) -> Dict[str, object]:
        ev: Dict[str, object] = {
            "type": "simple",
            "id": f"EV_LOC_{key}_{suffix}",
            "once": True,
            "region": True,
            "text": text,
        }
        if isinstance(delta, dict) and delta:
            ev["delta"] = dict(delta)
        if isinstance(item, tuple) and len(item) == 2:
            ev["item"] = item
        if isinstance(buff, tuple) and len(buff) == 2:
            ev["buff"] = buff
        if isinstance(sector, dict) and sector:
            ev["sector"] = dict(sector)
        return ev

    def _mk_choice(*, suffix: str, text: str, options: Dict[str, Dict[str, object]]) -> Dict[str, object]:
        return {
            "type": "choice",
            "id": f"EV_LOC_{key}_{suffix}",
            "once": True,
            "region": True,
            "text": text,
            "options": options,
        }

    def _loot_by_terrain() -> Tuple[str, int]:
        # 专属事件的“主奖励”尽量与地形挂钩
        if terrain in ("医院",):
            return ("药品", 1)
        if terrain in ("电台",):
            return ("电台电池", 1)
        if terrain in ("修理厂", "工业"):
            return ("备件", 1)
        if terrain in ("地铁", "地下通道"):
            return ("地图碎片", 1)
        if terrain in ("农舍", "小镇", "营地"):
            return ("口粮", 1)
        if terrain in ("检查点", "阵地", "政府附近"):
            return ("通行证", 1)
        return ("弹药箱", 1)

    def _flavor_line() -> str:
        if desc:
            return desc
        if tags:
            short = "、".join(tags[:3])
            return f"这里的特征：{short}。"
        if terrain:
            return f"地形是‘{terrain}’，每一步都更像在做选择。"
        return "你们只能靠经验判断下一步。"

    def _join_features(*, max_items: int = 3) -> str:
        parts: List[str] = []
        if terrain:
            parts.append(f"地形：{terrain}")
        if tags:
            parts.append("特征：" + "、".join(tags[:max_items]))
        if not parts:
            return ""
        return "（" + "；".join(parts) + "）"

    def _seed_hint() -> str:
        # 小小的“更像手写”的变化：用地区信息拼出一句自然的开头
        f = _join_features(max_items=2)
        if f:
            return f"【{name}】{f}"
        return f"【{name}】"

    def _tiny_sector_bonus() -> Dict[str, int]:
        # 让部分地形更自然地产生“辖区互动”
        if terrain in ("检查点", "阵地", "政府附近"):
            return {"favor": 3}
        if terrain in ("营地", "农舍", "小镇"):
            return {"favor": 2, "fall": -1}
        if terrain in ("仓库带", "货场", "车站"):
            return {"favor": 2}
        return {}

    events: List[Dict[str, object]] = []

    # --- 重要地标：额外给一条更“定制”的事件 ---
    if key in ("6", "12", "24", "22", "51", "54", "55", "65"):
        if key == "6":
            events.append(
                _mk_choice(
                    suffix="LANDMARK_A",
                    text=(
                        f"【{name}】象征性的残骸近在眼前。你们可以在此停下做一次更谨慎的观察，"
                        "也可以快速通过，把情绪压在钢铁后面。"
                    ),
                    options={
                        "1": {"label": "停下观察并标记火力点（耗1行动点；观察+1）", "require_ap": 1, "cost_ap": 1, "buff": ("观察", 1), "delta": {"morale": 1}},
                        "2": {"label": "快速通过（省事，但心里更沉）", "delta": {"morale": -1, "fuel": 1}},
                    },
                )
            )
        elif key == "12":
            events.append(
                _mk_choice(
                    suffix="LANDMARK_A",
                    text=(
                        f"【{name}】附近的防线像被钉死在瓦砾里。驻军想借虎王的存在稳住人心，"
                        "但你知道每一次停留都可能换来炮火的回应。"
                    ),
                    options={
                        "1": {"label": "给驻军留下一点弹链与暗号（机枪弹-18；好感+5）", "delta": {"ammo": -18, "morale": 1}, "sector": {"favor": 5}},
                        "2": {"label": "把时间留给你们自己（观察+1）", "buff": ("观察", 1), "delta": {"morale": 1}},
                    },
                )
            )
        elif key == "24":
            events.append(
                _mk_choice(
                    suffix="LANDMARK_A",
                    text=(
                        f"【{name}】门口散落着频率表与纸屑。通信员说：我们能从这里‘听见’一点东西。"
                    ),
                    options={
                        "1": {"label": "花1行动点记录频点（观察+1；胜利点+1）", "require_ap": 1, "cost_ap": 1, "buff": ("观察", 1), "delta": {"vp": 1}},
                        "2": {"label": "拆一节电池就走（获得电台电池x1）", "loot": ("电台电池", 1), "delta": {"morale": 1}},
                    },
                )
            )
        elif key == "22":
            events.append(
                _mk_choice(
                    suffix="LANDMARK_A",
                    text=(
                        f"【{name}】的走廊里还有担架的拖痕。你可以停下帮一把，或者告诉自己：你们也快撑不住。"
                    ),
                    options={
                        "1": {"label": "花1行动点协助搬运（士气+2；推进援助任务）", "require_ap": 1, "cost_ap": 1, "delta": {"morale": 2}, "quest": ("Q_hospital", 1)},
                        "2": {"label": "捐出药品（需要药品x1；士气+3；胜利点+1）", "require_item": ("药品", 1), "cost_item": ("药品", 1), "delta": {"morale": 3, "vp": 1}, "quest": ("Q_hospital", 1)},
                        "3": {"label": "不久留（士气-1）", "delta": {"morale": -1}},
                    },
                )
            )
        elif key == "51":
            events.append(
                _mk_choice(
                    suffix="LANDMARK_A",
                    text=(
                        f"【{name}】的混凝土残体像一座黑山。登上去，你能看得更远；"
                        "但更远的视野也意味着更容易被看见。"
                    ),
                    options={
                        "1": {"label": "登高观察（耗1行动点；观察+1；可能引来交火）", "require_ap": 1, "cost_ap": 1, "buff": ("观察", 1), "delta": {"morale": 1}, "extra_encounter": 1},
                        "2": {"label": "绕开高处（更稳）", "delta": {"fuel": 1, "morale": 1}},
                    },
                )
            )
        elif key in ("54", "55"):
            events.append(
                _mk_choice(
                    suffix="LANDMARK_A",
                    text=(
                        f"【{name}】里回声复杂。你可以用时间换取一段更安全的绕行路线，"
                        "也可以赌直线能省下燃油。"
                    ),
                    options={
                        "1": {"label": "花1行动点做路线标记（获得地图碎片x1；燃油+1）", "require_ap": 1, "cost_ap": 1, "loot": ("地图碎片", 1), "delta": {"fuel": 1, "morale": 1}},
                        "2": {"label": "直接穿过（更快，但更紧张）", "delta": {"fuel": 1, "morale": -2}},
                    },
                )
            )
        elif key == "65":
            events.append(
                _mk_choice(
                    suffix="LANDMARK_A",
                    text=(
                        f"【{name}】的文件柜像墙一样把通道切开。你可以花时间翻找一张能用的通行证，"
                        "也可以只做最基本的路线记录。"
                    ),
                    options={
                        "1": {"label": "花1行动点翻找（获得通行证x1；士气+1）", "require_ap": 1, "cost_ap": 1, "delta": {"passes": 1, "morale": 1}},
                        "2": {"label": "只记录路线（观察+1）", "buff": ("观察", 1), "delta": {"morale": 1}},
                    },
                )
            )

    # --- 关键地区：完全手写专属事件（每处 >= 10 条） ---
    # 说明：这里的文本不走模板句式，尽量给“地点独特记忆点”；仍保持数值影响克制。
    if key in ("1", "6", "11", "12", "24", "51", "54", "65"):
        if key == "1":  # 米特街区
            events.extend(
                [
                    _mk_choice(
                        suffix="HW01",
                        text=(
                            "米特街区的楼宇密得像一本合上的账册。你们从一条窄巷挤出来，"
                            "看见墙上用粉笔写着几行简短的数字和箭头——像是有人在做‘最后的路线记录’。\n"
                            "你可以花时间把它抄下来，也可以告诉自己：这种纸面上的确定感，往往最靠不住。"
                        ),
                        options={
                            "1": {"label": "花1行动点抄下记录（观察+1；胜利点+1）", "require_ap": 1, "cost_ap": 1, "buff": ("观察", 1), "delta": {"vp": 1, "morale": 1}},
                            "2": {"label": "只记住大概方向（燃油+1）", "delta": {"fuel": 1, "morale": 0}},
                            "3": {"label": "把它当作诱饵，立刻离开（士气-1）", "delta": {"morale": -1, "fuel": 1}},
                        },
                    ),
                    _mk_simple(
                        suffix="HW02",
                        text=(
                            "你们穿过一处半塌的办公室。碎玻璃上还有一层薄薄的灰，桌面却意外整洁，"
                            "像是有人离开前把每一样东西都摆回了‘该在的位置’。那种秩序感反而让人心里发紧。"
                        ),
                        delta={"morale": 1},
                        buff=("观察", 1),
                    ),
                    _mk_choice(
                        suffix="HW03",
                        text=(
                            "一辆被遗弃的有轨电车横在路口，车厢像一个空壳。车长的直觉告诉你："
                            "这里既是遮蔽，也是笼子。\n"
                            "你要不要把它当作临时掩护点，做一次更‘干净’的穿越？"
                        ),
                        options={
                            "1": {"label": "靠电车掩护绕过去（观察+1；燃油-1）", "buff": ("观察", 1), "delta": {"fuel": -1, "morale": 1}},
                            "2": {"label": "直接通过（更快，但更紧张）", "delta": {"fuel": 1, "morale": -2, "damage": 1}},
                        },
                    ),
                    _mk_simple(
                        suffix="HW04",
                        text=(
                            "一间面包店的后门半开着，里面空得只剩烤盘与一股说不清的焦味。"
                            "装填手从柜台底下翻出一小包干硬的口粮，像是从另一个世界掉出来的。"
                        ),
                        item=("口粮", 1),
                        delta={"morale": 2},
                    ),
                    _mk_choice(
                        suffix="HW05",
                        text=(
                            "你们在楼道里遇到两名守夜的民兵。他们看见虎王时眼神亮了一瞬，"
                            "随即又像想起了什么似的垂下去。\n"
                            "他们问：‘能不能给点弹？我们守着这条楼梯。’"
                        ),
                        options={
                            "1": {"label": "分出一点弹链（机枪弹-15；好感+3）", "delta": {"ammo": -15, "morale": 1}, "sector": {"favor": 3}},
                            "2": {"label": "给他们一支烟，替代承诺（消耗香烟x1；士气+1）", "require_item": ("香烟", 1), "cost_item": ("香烟", 1), "delta": {"morale": 1}},
                            "3": {"label": "拒绝并离开（士气-1）", "delta": {"morale": -1, "fuel": 1}},
                        },
                    ),
                    _mk_simple(
                        suffix="HW06",
                        text=(
                            "一段楼梯塌了一半，你们不得不小心绕行。驾驶员在狭窄空间里反复修正方向，"
                            "每一次履带轻微的摩擦声都像在提醒：这里不是道路，是碎裂的屋子。"
                        ),
                        delta={"fuel": -2, "morale": -1, "damage": 1},
                    ),
                    _mk_choice(
                        suffix="HW07",
                        text=(
                            "你听见楼上有水滴落下的声音，节奏稳定得像钟摆。通信员说："
                            "‘这种稳定，反而像有人故意留的记号。’\n"
                            "你要不要花时间确认楼上是不是一个可用的观察点？"
                        ),
                        options={
                            "1": {"label": "花1行动点上楼确认（观察+1；可能引来交火）", "require_ap": 1, "cost_ap": 1, "buff": ("观察", 1), "delta": {"morale": 1}, "extra_encounter": 1},
                            "2": {"label": "别碰它（更稳）", "delta": {"fuel": 1, "morale": 0}},
                        },
                    ),
                    _mk_simple(
                        suffix="HW08",
                        text=(
                            "你们在一处废弃的电话机旁看见一张便条：‘如果还能接通，就说我们还活着。’"
                            "没人知道这句话有没有被说出去，但它让车组沉默了好一会儿。"
                        ),
                        delta={"morale": -1},
                        stress_relief={"mode": "all", "amount": 2},
                    ),
                    _mk_simple(
                        suffix="HW09",
                        text=(
                            "驾驶员把工具在手里转了一圈，最后还是做了一次例行紧固。"
                            "‘不是为了省损伤，’他说，‘是为了让手别抖。’"
                        ),
                        delta={"damage": -3, "morale": 1},
                    ),
                    _mk_choice(
                        suffix="HW10",
                        text=(
                            "你们在一个狭小院落里发现一箱没来得及带走的旧货。箱盖上写着模糊的字，"
                            "像是仓库编号。装填手想把它搬上车，但院落太窄，停留会很显眼。"
                        ),
                        options={
                            "1": {"label": "花1行动点搬运（获得弹药箱x1；士气+1）", "require_ap": 1, "cost_ap": 1, "loot": ("弹药箱", 1), "delta": {"morale": 1}},
                            "2": {"label": "放弃（燃油+1）", "delta": {"fuel": 1, "morale": 0}},
                        },
                    ),
                    _mk_simple(
                        suffix="HW11",
                        text=(
                            "在米特街区，‘方向感’像一种奢侈。你们终于找到一条相对完整的路面，"
                            "短暂地把行进节奏重新拼了回来。"
                        ),
                        delta={"fuel": 2, "morale": 1},
                    ),
                ]
            )

        elif key == "6":  # 勃兰登堡门废墟
            events.extend(
                [
                    _mk_simple(
                        suffix="HW01",
                        text=(
                            "你们从侧巷望向勃兰登堡门的残影。它曾经代表秩序与仪式，现在只剩风穿过断裂石面的声音。"
                            "你突然意识到：‘象征’并不会挡住任何东西，但它能让人误以为自己还有理由坚持。"
                        ),
                        delta={"morale": -1, "vp": 1},
                    ),
                    _mk_choice(
                        suffix="HW02",
                        text=(
                            "门前的开阔地像一张摊开的纸。你可以用烟幕把自己从纸上擦掉一会儿，"
                            "也可以赌对方注意力不在这里。"
                        ),
                        options={
                            "1": {"label": "消耗烟幕弹x1，通过开阔地（观察+1）", "require_item": ("烟幕弹", 1), "cost_item": ("烟幕弹", 1), "buff": ("观察", 1), "delta": {"morale": 1}},
                            "2": {"label": "直接穿越（更快，但更冒险）", "delta": {"fuel": 1, "morale": -2, "damage": 3}, "extra_encounter": 1},
                        },
                    ),
                    _mk_choice(
                        suffix="HW03",
                        text=(
                            "一名疲惫的传令兵从断墙后钻出来，递给通信员一张写得很急的纸条："
                            "字迹潦草、指令不完整，但它至少说明‘有人还在尝试指挥’。"
                        ),
                        options={
                            "1": {"label": "按纸条调整路线（观察+1；燃油-1）", "buff": ("观察", 1), "delta": {"fuel": -1, "morale": 1}},
                            "2": {"label": "把纸条当作噪音（燃油+1）", "delta": {"fuel": 1, "morale": 0}},
                        },
                    ),
                    _mk_simple(
                        suffix="HW04",
                        text=(
                            "你们在一块倒塌的石雕旁短暂停车。炮手用指尖在灰尘上写下一个名字，又很快抹掉。"
                            "没人问是谁。问题在这里没有答案。"
                        ),
                        delta={"morale": -1},
                        stress_relief={"mode": "one", "amount": 10},
                    ),
                    _mk_choice(
                        suffix="HW05",
                        text=(
                            "门侧的阴影里有几名守卫在分配最后的弹药。他们盯着你们的炮塔看了一眼，"
                            "像是在计算你们还能替他们挡多久。"
                        ),
                        options={
                            "1": {"label": "留下一点弹链与暗号（机枪弹-20；好感+4）", "delta": {"ammo": -20, "morale": 1}, "sector": {"favor": 4}},
                            "2": {"label": "给他们一包急救（需要急救包x1；士气+2）", "require_item": ("急救包", 1), "cost_item": ("急救包", 1), "delta": {"morale": 2, "vp": 1}},
                            "3": {"label": "不久留（燃油+1）", "delta": {"fuel": 1, "morale": 0}},
                        },
                    ),
                    _mk_simple(
                        suffix="HW06",
                        text=(
                            "你们绕过残破的栏杆，履带碾过碎石，声音在空旷处被放大。"
                            "那种‘自己正在被听见’的感觉让人心里更累。"
                        ),
                        delta={"morale": -1, "fuel": -1},
                    ),
                    _mk_choice(
                        suffix="HW07",
                        text=(
                            "通信员指着一处高点说：从那里能看清楚三条街的动静。"
                            "驾驶员反问：那三条街也能看清你。"
                        ),
                        options={
                            "1": {"label": "登高快速观察（耗1行动点；观察+1；可能引来交火）", "require_ap": 1, "cost_ap": 1, "buff": ("观察", 1), "delta": {"morale": 1}, "extra_encounter": 1},
                            "2": {"label": "不冒险（更稳）", "delta": {"morale": 1, "fuel": 1}},
                        },
                    ),
                    _mk_simple(
                        suffix="HW08",
                        text=(
                            "你们在门廊的残砖里摸到一小段仍可用的导线。驾驶员说这东西不值钱，"
                            "但它能把一个松动的连接点重新拧紧。"
                        ),
                        item=("备件", 1),
                        delta={"morale": 1},
                    ),
                    _mk_simple(
                        suffix="HW09",
                        text=(
                            "门前的风让人清醒，也让人难以入睡。你知道这句话听起来像矫情，"
                            "但在这种地方，清醒本身就像一种代价。"
                        ),
                        delta={"morale": -1},
                    ),
                    _mk_choice(
                        suffix="HW10",
                        text=(
                            "一名老兵靠在墙边，说他记得这里曾经有巡逻路线与‘安全点’。"
                            "他说得很慢，像在把记忆一块块掰开给你看。"
                        ),
                        options={
                            "1": {"label": "花1行动点听完并记下（观察+1；燃油+1）", "require_ap": 1, "cost_ap": 1, "buff": ("观察", 1), "delta": {"fuel": 1, "morale": 1}},
                            "2": {"label": "只点头离开（士气+0）", "delta": {"morale": 0, "fuel": 1}},
                        },
                    ),
                    _mk_simple(
                        suffix="HW11",
                        text=(
                            "你们离开勃兰登堡门时，没有人回头。不是因为不在乎，而是因为回头意味着承认某些东西。"
                        ),
                        delta={"morale": 1},
                    ),
                ]
            )

        elif key == "11":  # 波茨坦广场废墟
            events.extend(
                [
                    _mk_simple(
                        suffix="HW01",
                        text=(
                            "波茨坦广场像被撕碎的路标堆成的空地。旧广告牌残片在风里嘎吱作响，"
                            "你听见那声音时，脑子里冒出的不是口号，而是‘该往哪儿走’。"
                        ),
                        delta={"morale": -1, "fuel": -1},
                    ),
                    _mk_choice(
                        suffix="HW02",
                        text=(
                            "广场的开阔让你们暴露得过于彻底，但它也意味着可以更快穿过去。"
                            "你要用燃油换速度，还是用时间换更稳的路线？"
                        ),
                        options={
                            "1": {"label": "快穿（燃油-2；胜利点+1；更冒险）", "delta": {"fuel": -2, "vp": 1, "morale": -1, "damage": 2}, "extra_encounter": 1},
                            "2": {"label": "贴边绕行（观察+1；燃油-1）", "buff": ("观察", 1), "delta": {"fuel": -1, "morale": 1}},
                        },
                    ),
                    _mk_simple(
                        suffix="HW03",
                        text=(
                            "你们在一块倾斜的路牌后发现几枚散落的炮弹箱封条。炮手说："
                            "‘箱子没了，但至少说明这里曾经有过补给。’"
                        ),
                        delta={"morale": 1},
                        item=("地图碎片", 1),
                    ),
                    _mk_choice(
                        suffix="HW04",
                        text=(
                            "一处地下入口被木板草草封住。木板上钉着几颗弯钉，看起来是匆忙做的。"
                            "你可以撬开它，或者把它当作‘不该碰’的信号。"
                        ),
                        options={
                            "1": {"label": "花1行动点撬开查看（获得备件x1；但更冒险）", "require_ap": 1, "cost_ap": 1, "loot": ("备件", 1), "delta": {"morale": 1}, "extra_encounter": 1},
                            "2": {"label": "不碰它（更稳）", "delta": {"fuel": 1, "morale": 0}},
                        },
                    ),
                    _mk_simple(
                        suffix="HW05",
                        text=(
                            "通信员在一面墙上看到一串极短的电报码，抄到一半又停住了。"
                            "‘这不像给我们看的，’他说，‘更像给某个已经不在的人。’"
                        ),
                        delta={"morale": -1},
                        buff=("观察", 1),
                    ),
                    _mk_choice(
                        suffix="HW06",
                        text=(
                            "你们看见几名士兵在拉扯一辆推车：里面是零碎的工具与一台坏掉的电台。"
                            "他们问能不能借你们的牵引钩拖一段路。"
                        ),
                        options={
                            "1": {"label": "花1行动点帮他们拖一段（获得电台电池x1；好感+2）", "require_ap": 1, "cost_ap": 1, "loot": ("电台电池", 1), "delta": {"morale": 1}, "sector": {"favor": 2}},
                            "2": {"label": "拒绝（燃油+1）", "delta": {"fuel": 1, "morale": 0}},
                        },
                    ),
                    _mk_simple(
                        suffix="HW07",
                        text=(
                            "广场的回声让你们误判了距离。你们停了一秒确认方向，那一秒像被人拿走了一样。"
                        ),
                        delta={"morale": -1},
                    ),
                    _mk_simple(
                        suffix="HW08",
                        text=(
                            "你们在一处半塌的 kiosk 里翻到几包香烟。包装被水泡得发软，但里面还能抽。"
                        ),
                        item=("香烟", 1),
                        delta={"morale": 2},
                    ),
                    _mk_choice(
                        suffix="HW09",
                        text=(
                            "炮手指着一条更短的路，说那能省下燃油；驾驶员说那条路的路面像陷阱。"
                            "你需要做决定：省油，还是省损伤。"
                        ),
                        options={
                            "1": {"label": "走短路（燃油+2；损伤+2）", "delta": {"fuel": 2, "damage": 2, "morale": -1}},
                            "2": {"label": "走稳路（燃油-2；损伤-1）", "delta": {"fuel": -2, "damage": -1, "morale": 1}},
                        },
                    ),
                    _mk_simple(
                        suffix="HW10",
                        text=(
                            "你们离开波茨坦广场时，脚下的路面终于连成一条线。那种‘能走下去’的感觉，"
                            "不是希望，只是暂时不用选择。"
                        ),
                        delta={"fuel": 1, "morale": 1},
                    ),
                    _mk_simple(
                        suffix="HW11",
                        text=(
                            "装填手把一颗小螺母放进口袋，说这是‘好运’。你没纠正他。"
                        ),
                        delta={"morale": 1},
                    ),
                ]
            )

        elif key == "12":  # 国会大厦外围
            events.extend(
                [
                    _mk_simple(
                        suffix="HW01",
                        text=(
                            "国会大厦外围的瓦砾像被反复翻过。你看到的不是一条防线，而是很多条防线叠在一起，"
                            "每一条都比上一条更仓促。"
                        ),
                        delta={"morale": -1, "vp": 1},
                    ),
                    _mk_choice(
                        suffix="HW02",
                        text=(
                            "一名军官拿着地图冲你喊：‘把炮塔对准那个缺口！我们需要你们稳住两分钟。’\n"
                            "你知道两分钟意味着什么：意味着你们也会被记在同一张表上。"
                        ),
                        options={
                            "1": {"label": "答应并就位（耗1行动点；胜利点+2；损伤+2）", "require_ap": 1, "cost_ap": 1, "delta": {"vp": 2, "damage": 2, "morale": -1}, "sector": {"favor": 4}},
                            "2": {"label": "只给出方向建议（观察+1；好感+2）", "buff": ("观察", 1), "delta": {"morale": 1}, "sector": {"favor": 2}},
                        },
                    ),
                    _mk_choice(
                        suffix="HW03",
                        text=(
                            "你们被拉到一处临时弹药点：几箱东西散落在泥里。负责分配的人盯着你们的炮塔，"
                            "问得很直白：‘你们还剩多少？’"
                        ),
                        options={
                            "1": {"label": "用机枪弹换一次通行（机枪弹-25；通行证+1）", "delta": {"ammo": -25, "passes": 1, "morale": 1}},
                            "2": {"label": "拒绝交换（燃油+1）", "delta": {"fuel": 1, "morale": 0}},
                        },
                    ),
                    _mk_simple(
                        suffix="HW04",
                        text=(
                            "你们在墙根下发现一条被压扁的担架。通信员蹲下把绑带重新系紧，"
                            "像是在修一件已经没用的东西。那动作却让人稍微稳了一点。"
                        ),
                        delta={"morale": 1},
                        stress_relief={"mode": "all", "amount": 3},
                    ),
                    _mk_choice(
                        suffix="HW05",
                        text=(
                            "外围的守军把你们当作‘还能动的重物’。他们请求你们给出一个信号："
                            "要撤还是要守。你说出口的一句话，会被他们当作答案。"
                        ),
                        options={
                            "1": {"label": "建议分散撤到下一个街区（沦陷-1；好感+2）", "sector": {"fall": -1, "favor": 2}, "delta": {"morale": 1}},
                            "2": {"label": "建议肃清敌人到夜里（胜利点+1；士气-1）", "sector": {"favor": 3}, "delta": {"vp": 1, "morale": -1}},
                            "3": {"label": "不表态（更干净）", "delta": {"morale": 0, "fuel": 1}},
                        },
                    ),
                    _mk_simple(
                        suffix="HW06",
                        text=(
                            "你们沿着外围的残墙缓慢移动。灰尘落在观察孔边缘，像给视野加了一层薄膜。"
                            "炮手把手指按在瞄具上，按得很用力。"
                        ),
                        delta={"fuel": -1, "morale": -1},
                    ),
                    _mk_choice(
                        suffix="HW07",
                        text=(
                            "一名通信兵递来一块备用电池，说是‘从楼里拆下来的’。他问你们能不能帮他把电台送回去。"
                        ),
                        options={
                            "1": {"label": "花1行动点送回去（获得电台电池x1；好感+3）", "require_ap": 1, "cost_ap": 1, "loot": ("电台电池", 1), "delta": {"morale": 1}, "sector": {"favor": 3}},
                            "2": {"label": "只收下电池（士气+0）", "loot": ("电台电池", 1), "delta": {"morale": 0}},
                        },
                    ),
                    _mk_simple(
                        suffix="HW08",
                        text=(
                            "你们在一个沙袋堆里摸到一罐润滑油。看起来是某个机枪手留下的，"
                            "他可能再也用不上了。"
                        ),
                        item=("润滑油", 1),
                        delta={"morale": 1},
                    ),
                    _mk_choice(
                        suffix="HW09",
                        text=(
                            "外围的路口有一处明显的观察线。你可以用‘耐心’换安全，也可以用‘速度’换过去。"
                        ),
                        options={
                            "1": {"label": "耐心等待间隙（观察+1；燃油-1）", "buff": ("观察", 1), "delta": {"fuel": -1, "morale": 1}},
                            "2": {"label": "冲过去（燃油-1；损伤+3；更冒险）", "delta": {"fuel": -1, "damage": 3, "morale": -2}, "extra_encounter": 1},
                        },
                    ),
                    _mk_simple(
                        suffix="HW10",
                        text=(
                            "你们离开外围时，耳边的命令声终于变小。你发现自己并不更轻松，"
                            "只是更清楚：下一次停留会更难拒绝。"
                        ),
                        delta={"morale": 1},
                    ),
                    _mk_simple(
                        suffix="HW11",
                        text=(
                            "在国会大厦外围，人们说话很少，因为每一句话都像在占用别人的时间。"
                        ),
                        delta={"morale": -1, "fuel": 1},
                    ),
                ]
            )

        elif key == "24":  # 广播电台大楼外
            events.extend(
                [
                    _mk_simple(
                        suffix="HW01",
                        text=(
                            "电台大楼外墙的标语已经被烟熏得发灰。你忽然觉得那些字并不是被毁掉了，"
                            "而是终于变得和它们应得的样子一致：轻、薄、经不起触碰。"
                        ),
                        delta={"morale": 1},
                    ),
                    _mk_choice(
                        suffix="HW02",
                        text=(
                            "通信员在门厅里翻出一张频率表，上面用红笔圈了几个点。"
                            "他问：要不要花时间把它记牢？"
                        ),
                        options={
                            "1": {"label": "花1行动点记录频率（观察+1；胜利点+1）", "require_ap": 1, "cost_ap": 1, "buff": ("观察", 1), "delta": {"vp": 1, "morale": 1}},
                            "2": {"label": "直接拆电池（获得电台电池x1）", "loot": ("电台电池", 1), "delta": {"morale": 1}},
                        },
                    ),
                    _mk_choice(
                        suffix="HW03",
                        text=(
                            "播音间的桌上还有一段未读完的稿子，字句整齐得像完全不认识外面的世界。"
                            "炮手说：‘把它带走没意义。’通信员却说：‘意义有时候是给自己看的。’"
                        ),
                        options={
                            "1": {"label": "带走稿纸当作路标（观察+1）", "buff": ("观察", 1), "delta": {"morale": 1}},
                            "2": {"label": "把稿纸塞回抽屉（士气+0）", "delta": {"morale": 0, "fuel": 1}},
                        },
                    ),
                    _mk_simple(
                        suffix="HW04",
                        text=(
                            "电台走廊里散落着耳机与磁带盒。你们把脚步放得很轻，像怕惊醒什么。"
                        ),
                        delta={"morale": 1, "fuel": -1},
                    ),
                    _mk_choice(
                        suffix="HW05",
                        text=(
                            "通信员说：我们可以用电台发一段‘假指令’，让对方误判；"
                            "也可以干脆把时间用来求援。两者都要付代价。"
                        ),
                        options={
                            "1": {"label": "发假指令（胜利点+1；更冒险）", "delta": {"vp": 1, "morale": -1}, "extra_encounter": 1},
                            "2": {"label": "电台求援（需要电台电池x1；求援+1）", "require_item": ("电台电池", 1), "cost_item": ("电台电池", 1), "buff": ("求援", 1), "delta": {"morale": 2}},
                        },
                    ),
                    _mk_simple(
                        suffix="HW06",
                        text=(
                            "你们在配电间找到一小盒保险丝。它们看起来不起眼，却像一种‘还能修’的暗示。"
                        ),
                        item=("备件", 1),
                        delta={"morale": 1},
                    ),
                    _mk_choice(
                        suffix="HW07",
                        text=(
                            "地下室里有一台半坏的发电机。驾驶员看了两眼说：拆它能换来备件，"
                            "但点亮它也许能换来更明确的回应。"
                        ),
                        options={
                            "1": {"label": "拆走零件（获得备件x1；士气+1）", "loot": ("备件", 1), "delta": {"morale": 1}},
                            "2": {"label": "花1行动点短时间点亮电台（胜利点+1；士气+2）", "require_ap": 1, "cost_ap": 1, "delta": {"vp": 1, "morale": 2}},
                        },
                    ),
                    _mk_simple(
                        suffix="HW08",
                        text=(
                            "你们离开电台时，通信员把耳机在手里掂了掂，又放回去。"
                            "他说：‘听见太多，反而走不动。’"
                        ),
                        delta={"morale": 1},
                    ),
                    _mk_simple(
                        suffix="HW09",
                        text=(
                            "一张被踩皱的节目单上印着当天的时间表。时间表仍然完整，却再也不会被执行。"
                        ),
                        delta={"morale": -1},
                    ),
                    _mk_choice(
                        suffix="HW10",
                        text=(
                            "通信员说：如果我们再待一分钟，他能把一段更清晰的频点记下来。"
                            "你知道那一分钟可能换来信息，也可能换来麻烦。"
                        ),
                        options={
                            "1": {"label": "花1行动点再等一分钟（观察+1）", "require_ap": 1, "cost_ap": 1, "buff": ("观察", 1), "delta": {"morale": 1}},
                            "2": {"label": "立刻走（燃油+1）", "delta": {"fuel": 1, "morale": 0}},
                        },
                    ),
                    _mk_simple(
                        suffix="HW11",
                        text=(
                            "你忽然想起：电台最擅长的从来不是广播，而是让人相信‘还有人知道答案’。"
                        ),
                        delta={"morale": 1},
                    ),
                ]
            )

        elif key == "51":  # 动物园防空塔残迹
            events.extend(
                [
                    _mk_simple(
                        suffix="HW01",
                        text=(
                            "防空塔的混凝土像一块不肯碎的骨头。你们在它的阴影里短暂停留，"
                            "第一次觉得‘遮蔽’不只是墙，还有重量。"
                        ),
                        delta={"morale": 2, "fuel": -1},
                    ),
                    _mk_choice(
                        suffix="HW02",
                        text=(
                            "炮手想上到更高处看一眼。驾驶员说：上去很容易，下来就不一定了。"
                            "你要不要赌一次视野？"
                        ),
                        options={
                            "1": {"label": "登高观察（耗1行动点；观察+1；更冒险）", "require_ap": 1, "cost_ap": 1, "buff": ("观察", 1), "delta": {"morale": 1}, "extra_encounter": 1},
                            "2": {"label": "不登高，贴着阴影走（损伤-1；士气+1）", "delta": {"damage": -1, "morale": 1}},
                        },
                    ),
                    _mk_simple(
                        suffix="HW03",
                        text=(
                            "你们在塔脚下找到一箱生锈工具。驾驶员挑了几件还能用的，"
                            "像在把‘还能修’这件事捡回来。"
                        ),
                        item=("工具箱", 1),
                        delta={"morale": 1},
                    ),
                    _mk_choice(
                        suffix="HW04",
                        text=(
                            "塔内的楼梯间回声很重，你们的脚步像在敲击空心的鼓。"
                            "你可以用时间换一次更安全的路线标记，也可以立刻离开。"
                        ),
                        options={
                            "1": {"label": "花1行动点做路线标记（地图碎片x1；士气+1）", "require_ap": 1, "cost_ap": 1, "loot": ("地图碎片", 1), "delta": {"morale": 1}},
                            "2": {"label": "立刻离开（燃油+1）", "delta": {"fuel": 1, "morale": 0}},
                        },
                    ),
                    _mk_simple(
                        suffix="HW05",
                        text=(
                            "你们在塔内一处角落看到几张临时床铺。没人了，只有褪色的毛毯。\n"
                            "那种‘曾经有人在这里等到天亮’的感觉，让人既安心又难受。"
                        ),
                        delta={"morale": 1},
                        stress_relief={"mode": "all", "amount": 4},
                    ),
                    _mk_choice(
                        suffix="HW06",
                        text=(
                            "有人说塔里还藏着补给，但也有人说塔里从来就不缺埋伏。"
                            "你愿意为一个‘也许’停留多久？"
                        ),
                        options={
                            "1": {"label": "花1行动点搜索（获得弹药箱x1；更冒险）", "require_ap": 1, "cost_ap": 1, "loot": ("弹药箱", 1), "delta": {"morale": 1}, "extra_encounter": 1},
                            "2": {"label": "只捡走显眼的东西（获得香烟x1）", "loot": ("香烟", 1), "delta": {"morale": 1}},
                        },
                    ),
                    _mk_simple(
                        suffix="HW07",
                        text=(
                            "风从塔的裂缝里灌进来，像一台不肯停的机器。炮手把领口拉高，说："
                            "‘这声音像是提醒我们：时间在走。’"
                        ),
                        delta={"morale": -1, "fuel": 1},
                    ),
                    _mk_simple(
                        suffix="HW08",
                        text=(
                            "驾驶员在塔脚下做了一次快速紧固。‘不为性能，’他说，‘为手感。’"
                        ),
                        delta={"damage": -2, "morale": 1},
                    ),
                    _mk_choice(
                        suffix="HW09",
                        text=(
                            "一名陌生的士兵在阴影里对你们挥手，示意有一条更隐蔽的出口。"
                            "你不知道他是谁，但你知道他不想在开阔地里走。"
                        ),
                        options={
                            "1": {"label": "跟随他指的出口（观察+1；燃油-1）", "buff": ("观察", 1), "delta": {"fuel": -1, "morale": 1}},
                            "2": {"label": "不跟（燃油+1）", "delta": {"fuel": 1, "morale": 0}},
                        },
                    ),
                    _mk_simple(
                        suffix="HW10",
                        text=(
                            "你们离开防空塔时，身后那块混凝土像还在注视你们。它不会倒下，"
                            "但你们知道：能倒下的东西，才会被人记住。"
                        ),
                        delta={"morale": 1},
                    ),
                    _mk_simple(
                        suffix="HW11",
                        text=(
                            "塔的阴影把你们的轮廓切得很干净。你忽然希望自己也能像它一样，"
                            "只需要站着，不需要决定。"
                        ),
                        delta={"morale": -1, "fuel": 1},
                    ),
                ]
            )

        elif key == "54":  # 下水道枢纽
            events.extend(
                [
                    _mk_simple(
                        suffix="HW01",
                        text=(
                            "下水道枢纽的气味像一堵墙。蒸汽从岔口冒出来，你们的脚步声被回声拆成好几份。"
                            "你们不是在走路，是在猜：哪一条路会把人带回地面。"
                        ),
                        delta={"morale": -1, "fuel": -1},
                    ),
                    _mk_choice(
                        suffix="HW02",
                        text=(
                            "岔路口的墙上有几道很新的划痕，像是有人用金属做过标记。"
                            "你可以相信它，也可以当作陷阱。"
                        ),
                        options={
                            "1": {"label": "花1行动点沿标记走（地图碎片x1；士气+1）", "require_ap": 1, "cost_ap": 1, "loot": ("地图碎片", 1), "delta": {"morale": 1}},
                            "2": {"label": "不信标记，自己摸路（燃油-1；观察+1）", "buff": ("观察", 1), "delta": {"fuel": -1, "morale": 1}},
                            "3": {"label": "直接走最宽的那条（更快但更冒险）", "delta": {"fuel": 1, "morale": -2, "damage": 1}, "extra_encounter": 1},
                        },
                    ),
                    _mk_simple(
                        suffix="HW03",
                        text=(
                            "你们在一处侧室里看到几只空罐头和一张湿透的纸。纸上只有一句话：‘别走直线。’"
                        ),
                        buff=("观察", 1),
                        delta={"morale": 1},
                    ),
                    _mk_choice(
                        suffix="HW04",
                        text=(
                            "蒸汽更浓了。驾驶员说：再走下去，发动机进水的风险会上来。"
                            "你可以花时间绕远，也可以赌一把。"
                        ),
                        options={
                            "1": {"label": "绕远（燃油-2；损伤-1）", "delta": {"fuel": -2, "damage": -1, "morale": 1}},
                            "2": {"label": "赌一把直穿（燃油+1；损伤+3）", "delta": {"fuel": 1, "damage": 3, "morale": -1}},
                        },
                    ),
                    _mk_simple(
                        suffix="HW05",
                        text=(
                            "你们在污水边缘找到一段还没完全坏掉的润滑油布包。你们谁都没问它从哪来。"
                        ),
                        item=("润滑油", 1),
                        delta={"morale": 1},
                    ),
                    _mk_choice(
                        suffix="HW06",
                        text=(
                            "你听见远处有很轻的敲击声，像有人用金属在回应另一个人。"
                            "你可以花时间去确认那是不是‘出口方向’，也可以假装没听见。"
                        ),
                        options={
                            "1": {"label": "花1行动点循声确认（观察+1；更冒险）", "require_ap": 1, "cost_ap": 1, "buff": ("观察", 1), "delta": {"morale": 1}, "extra_encounter": 1},
                            "2": {"label": "不追声音（燃油+1）", "delta": {"fuel": 1, "morale": 0}},
                        },
                    ),
                    _mk_simple(
                        suffix="HW07",
                        text=(
                            "在地下，时间感会消失。你让车组轮流报数确认位置，像在用语言把自己固定住。"
                        ),
                        stress_relief={"mode": "all", "amount": 4},
                        delta={"morale": 1},
                    ),
                    _mk_choice(
                        suffix="HW08",
                        text=(
                            "你们在一处干燥的高台边找到一只工具箱。它很重，搬走会拖慢你们，"
                            "但它也意味着下一次故障不必靠祈祷。"
                        ),
                        options={
                            "1": {"label": "花1行动点搬走（工具箱x1；士气+1）", "require_ap": 1, "cost_ap": 1, "loot": ("工具箱", 1), "delta": {"morale": 1}},
                            "2": {"label": "放弃（燃油+1）", "delta": {"fuel": 1, "morale": 0}},
                        },
                    ),
                    _mk_simple(
                        suffix="HW09",
                        text=(
                            "你们终于看见一处向上的梯井。梯井外面透进来一点点灰白的光，"
                            "那点光并不温暖，但它足够告诉你：方向是对的。"
                        ),
                        delta={"morale": 2, "fuel": 1},
                    ),
                    _mk_simple(
                        suffix="HW10",
                        text=(
                            "离开枢纽前，通信员在墙上做了一个只有你们懂的标记。"
                            "他说：‘万一要回来，至少别回来两次。’"
                        ),
                        delta={"morale": 1},
                        item=("地图碎片", 1),
                    ),
                    _mk_simple(
                        suffix="HW11",
                        text=(
                            "地下的空气让人烦躁。你们没有吵架，但每个人都说话更短了。"
                        ),
                        delta={"morale": -1, "fuel": 1},
                    ),
                ]
            )

        elif key == "65":  # 市政档案室
            events.extend(
                [
                    _mk_simple(
                        suffix="HW01",
                        text=(
                            "市政档案室的气味不是霉，而是纸。纸的味道让你想起‘制度’这两个字："
                            "它曾经能安排每个人的生活，现在却只能堆成墙。"
                        ),
                        delta={"morale": 1},
                    ),
                    _mk_choice(
                        suffix="HW02",
                        text=(
                            "文件柜上的标签还清晰：配给、通行、登记。你意识到这里的‘钥匙’并不在金属，"
                            "而在纸上。\n"
                            "你要不要花时间翻出一张还能用的通行凭证？"
                        ),
                        options={
                            "1": {"label": "花1行动点翻找（通行证+1；士气+1）", "require_ap": 1, "cost_ap": 1, "delta": {"passes": 1, "morale": 1}},
                            "2": {"label": "只记录柜号与路线（观察+1）", "buff": ("观察", 1), "delta": {"morale": 1}},
                        },
                    ),
                    _mk_simple(
                        suffix="HW03",
                        text=(
                            "你们在一叠表格里看到几张被退回的申请：‘缺少印章’、‘缺少签字’。"
                            "那些拒绝在此刻看起来荒诞得像笑话，但没人笑。"
                        ),
                        delta={"morale": -1},
                    ),
                    _mk_choice(
                        suffix="HW04",
                        text=(
                            "通信员发现一张旧城区平面图，图上用铅笔标注了几条‘绕行’路线。"
                            "你可以把它带走，也可以只记下其中一条最关键的。"
                        ),
                        options={
                            "1": {"label": "花1行动点抄下路线（地图碎片x1；观察+1）", "require_ap": 1, "cost_ap": 1, "loot": ("地图碎片", 1), "buff": ("观察", 1), "delta": {"morale": 1}},
                            "2": {"label": "只记一条（燃油+1）", "delta": {"fuel": 1, "morale": 0}},
                        },
                    ),
                    _mk_simple(
                        suffix="HW05",
                        text=(
                            "档案室里有一间小茶水间。柜子里只剩一小罐咖啡粉，"
                            "你们分着闻了一下味道，像是把‘还算人’这件事确认了一遍。"
                        ),
                        item=("咖啡", 1),
                        delta={"morale": 1},
                    ),
                    _mk_choice(
                        suffix="HW06",
                        text=(
                            "你们翻到一张空白通行证，缺少最后的盖章。驾驶员说：‘拿走也没用。’"
                            "通信员说：‘在现在，有时候‘像真的’就够了。’"
                        ),
                        options={
                            "1": {"label": "拿走当备用（通行证+1；士气-1）", "delta": {"passes": 1, "morale": -1}},
                            "2": {"label": "不拿（士气+0）", "delta": {"morale": 0, "fuel": 1}},
                        },
                    ),
                    _mk_simple(
                        suffix="HW07",
                        text=(
                            "档案柜把通道切得很窄。你们挪动时不敢发出太大声音，"
                            "那种小心翼翼的节奏反而让炮手的手更稳。"
                        ),
                        delta={"damage": -2, "morale": 1},
                    ),
                    _mk_choice(
                        suffix="HW08",
                        text=(
                            "一名陌生人从柜子后探出头，手里攥着一张写着地址的纸。他说自己知道一条更安全的路，"
                            "但他想要一点‘保证’。"
                        ),
                        options={
                            "1": {"label": "给他香烟（需要香烟x1；观察+1）", "require_item": ("香烟", 1), "cost_item": ("香烟", 1), "buff": ("观察", 1), "delta": {"morale": 1}},
                            "2": {"label": "给他电台电池（需要电台电池x1；燃油+2）", "require_item": ("电台电池", 1), "cost_item": ("电台电池", 1), "delta": {"fuel": 2, "morale": 1}},
                            "3": {"label": "拒绝（士气-1）", "delta": {"morale": -1, "fuel": 1}},
                        },
                    ),
                    _mk_simple(
                        suffix="HW09",
                        text=(
                            "你们在一只文件袋里找到几枚备用订书钉与小零件。驾驶员把它们当作可用材料收起来。"
                        ),
                        item=("备件", 1),
                        delta={"morale": 1},
                    ),
                    _mk_simple(
                        suffix="HW10",
                        text=(
                            "离开档案室时，你突然觉得自己带走的不只是物资。你们把一小段‘可被记录的世界’\n"
                            "塞回了背包里。"
                        ),
                        delta={"morale": 2},
                    ),
                    _mk_simple(
                        suffix="HW11",
                        text=(
                            "在这里，你第一次如此明确地意识到：城市不是由砖石组成的，而是由一份份纸、一条条名字组成的。"
                        ),
                        delta={"morale": -1, "vp": 1},
                    ),
                ]
            )

    # --- 通用“地区专属”事件：为每个地区生成 7-9 条 ---
    # 1) 氛围/环境：与地区名绑定（轻结果）
    base_delta: Dict[str, int] = {}
    if terrain in ("公园", "林带边缘"):
        base_delta = {"morale": 2, "fuel": 1}
    elif terrain in ("地铁", "地下通道"):
        base_delta = {"morale": -1, "fuel": -1}
    elif terrain in ("工业", "修理厂"):
        base_delta = {"damage": -2, "morale": 1}
    elif terrain in ("检查点", "阵地", "政府附近"):
        base_delta = {"morale": -1, "vp": 1}
    elif terrain in ("农舍", "小镇", "营地"):
        base_delta = {"morale": 2}
    elif terrain in ("仓库带", "货场", "车站"):
        base_delta = {"morale": 1, "ammo": 6}
    else:
        base_delta = {"morale": 1}

    events.append(
        _mk_simple(
            suffix="S1",
            text=f"{_seed_hint()} {_flavor_line()}",
            delta=base_delta,
        )
    )

    # 2) 细节搜刮：更贴合地点与“人留下的痕迹”
    loot2 = _loot_by_terrain()
    extra_line = ""
    if desc:
        extra_line = f"你扫过现场：{desc}"
    elif tags:
        extra_line = f"你记住这些细节：{'、'.join(tags[:3])}。"
    else:
        extra_line = "你们不敢久留，但也不想空手而归。"
    events.append(
        _mk_choice(
            suffix="C0",
            text=(
                f"{_seed_hint()} 你们暂时把引擎声压到最低。{extra_line}\n"
                "要不要花一点时间把这里搜得更像样？"
            ),
            options={
                "1": {
                    "label": "花1行动点细搜（获得物资；士气+1）",
                    "require_ap": 1,
                    "cost_ap": 1,
                    "delta": {"morale": 1},
                    "loot": loot2,
                    "sector": _tiny_sector_bonus() or None,
                },
                "2": {"label": "只做快速检查（更保守）", "delta": {"morale": 0, "fuel": 1}},
            },
        )
    )

    # 3) 风险判断：路面/能见度/回声等（轻惩罚或轻奖励）
    risk_text = (
        f"{_seed_hint()} 你们不得不承认：在这里，信息本身就是资源。"
        "你可以把时间花在‘确认’上，也可以把燃油花在‘绕开’上。"
    )
    opt_risk_1: Dict[str, object] = {"label": "慢一点，确认路线（观察+1；燃油-1）", "buff": ("观察", 1), "delta": {"fuel": -1, "morale": 1}}
    opt_risk_2: Dict[str, object] = {"label": "绕开可疑区域（更耗油但更稳）", "delta": {"fuel": -3, "morale": 1, "damage": -1}}
    opt_risk_3: Dict[str, object] = {"label": "直接通过（省事，但更紧张）", "delta": {"fuel": 1, "morale": -2, "damage": 2}}
    if terrain in ("地铁", "地下通道"):
        opt_risk_3["extra_encounter"] = 1
    events.append(_mk_choice(suffix="C2", text=risk_text, options={"1": opt_risk_1, "2": opt_risk_2, "3": opt_risk_3}))

    # 4) 车辆状态：小修/检查（用物品换更明显的收益）
    fix_text = (
        f"{_seed_hint()} 驾驶员（兼机械）摸了摸车体外壳：热还没退。"
        "你可以用备件做一次‘真正的处理’，也可以把它留到以后。"
    )
    events.append(
        _mk_choice(
            suffix="C3",
            text=fix_text,
            options={
                "1": {
                    "label": "消耗备件x1做现场处置（损伤-10；士气+1）",
                    "require_item": ("备件", 1),
                    "cost_item": ("备件", 1),
                    "delta": {"damage": -10, "morale": 1, "fuel": -1},
                },
                "2": {"label": "只做例行检查（损伤-2）", "delta": {"damage": -2, "morale": 0}},
                "3": {"label": "不处理，保持节奏（士气-1）", "delta": {"morale": -1, "fuel": 1}},
            },
        )
    )

    # 5) 人与秩序：是否介入辖区小纠纷/协同（带轻微辖区变化）
    sec_text = (
        f"{_seed_hint()} 你们看见几名驻军在争吵：要不要守在这里？要不要撤？"
        "虎王的出现会放大他们的期待。你要不要说点什么？"
    )
    sec_bonus = _tiny_sector_bonus()
    if not sec_bonus:
        sec_bonus = {"favor": 2}
    events.append(
        _mk_choice(
            suffix="C4",
            text=sec_text,
            options={
                "1": {"label": "用一句话把他们的队形‘拉回来’（好感+；士气+1）", "sector": sec_bonus, "delta": {"morale": 1}},
                "2": {"label": "给一点现实建议：分散、别聚堆（沦陷-1；士气+0）", "sector": {"fall": -1, "favor": 1}, "delta": {"morale": 0}},
                "3": {"label": "不介入（更干净，但更冷）", "delta": {"morale": -1, "fuel": 1}},
            },
        )
    )

    # 6) 情报/电台：不同地形有不同的“合理收获”
    intel_text = (
        f"{_seed_hint()} 你们捕捉到一些碎片信息：口令、标记、或者只是重复的脚步声。"
        "你可以把它变成优势，也可以把它当作噪音。"
    )
    intel_opt1: Dict[str, object] = {"label": "记录下来（观察+1；胜利点+1）", "buff": ("观察", 1), "delta": {"vp": 1, "morale": 1}}
    intel_opt2: Dict[str, object] = {"label": "立刻换成实物（获得电台电池x1）", "loot": ("电台电池", 1), "delta": {"morale": 1}}
    intel_opt3: Dict[str, object] = {"label": "把注意力收回到驾驶与炮塔（损伤-2）", "delta": {"damage": -2, "morale": 0}}
    if terrain == "电台":
        intel_opt1["delta"] = {"vp": 1, "morale": 2}
        intel_opt2["loot"] = ("电台电池", 1)
    elif terrain in ("医院",):
        intel_opt2 = {"label": "把碎片信息换成药（获得药品x1）", "loot": ("药品", 1), "delta": {"morale": 1}}
    elif terrain in ("修理厂", "工业"):
        intel_opt2 = {"label": "把线索当作‘零件去处’（获得备件x1）", "loot": ("备件", 1), "delta": {"morale": 1}}
    events.append(_mk_choice(suffix="C5", text=intel_text, options={"1": intel_opt1, "2": intel_opt2, "3": intel_opt3}))

    # 7) 额外：若有“制高点/狙击/可伏击”等标签，给一条更贴合的战术抉择
    if ("制高点" in tags) or ("狙击" in tags) or ("可伏击" in tags):
        events.append(
            _mk_choice(
                suffix="C6",
                text=(
                    f"{_seed_hint()} 这里的角度‘太好’了：好到让你怀疑是不是别人也在用同样的角度看你。"
                    "你可以把它当作机会，也可以把它当作陷阱。"
                ),
                options={
                    "1": {"label": "占住角度做一次压制（胜利点+1；可能引来交火）", "delta": {"vp": 1, "morale": -1}, "extra_encounter": 1},
                    "2": {"label": "只做一次快速观察（观察+1）", "buff": ("观察", 1), "delta": {"morale": 1}},
                    "3": {"label": "马上离开（燃油+1）", "delta": {"fuel": 1, "morale": 0}},
                },
            )
        )

    # 8) 额外：若地点有“绕行/岔路/地下”等标签，再补一条路线事件
    if ("绕行" in tags) or ("岔路" in tags) or (terrain in ("地铁", "地下通道")):
        events.append(
            _mk_choice(
                suffix="C7",
                text=(
                    f"{_seed_hint()} 你们在一处不起眼的拐角发现‘多出来的一条路’。"
                    "它可能是逃生口，也可能只是更深的迷路。"
                ),
                options={
                    "1": {"label": "花1行动点确认并做标记（获得地图碎片x1；士气+1）", "require_ap": 1, "cost_ap": 1, "loot": ("地图碎片", 1), "delta": {"morale": 1}},
                    "2": {"label": "把它留给以后（燃油+1）", "delta": {"fuel": 1, "morale": 0}},
                },
            )
        )

    # 过滤：once 事件若已出现过则不再投放
    filtered: List[Dict[str, object]] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if bool(ev.get("once")):
            eid = ev.get("id")
            if isinstance(eid, str) and eid and _seen(eid):
                continue
        filtered.append(ev)
    return filtered


def random_event(ins: InputStream, s: GameState) -> None:
    """随机事件：探索时可能发生的各种事件（继续分别丰富：事件池≥30条）。"""

    # 本次事件结果汇总（让结果更“可读”）
    result_delta: Dict[str, int] = {
        "fuel": 0,
        "ammo": 0,
        "ap_shells": 0,
        "he_shells": 0,
        "gold": 0,
        "passes": 0,
        "morale": 0,
        "damage": 0,
        "vp": 0,
        "collapse": 0,
    }
    items_gained: Dict[str, int] = {}
    items_spent: Dict[str, int] = {}
    ap_spent_total = 0

    def _bump(d: Dict[str, int], key: str, val: int) -> None:
        if val == 0:
            return
        d[key] = int(d.get(key, 0) or 0) + int(val)

    def _fmt_signed(v: int) -> str:
        v = int(v)
        return f"+{v}" if v > 0 else str(v)

    def _print_event_result() -> None:
        parts: List[str] = []
        mapping = [
            ("fuel", "燃油"),
            ("ammo", "机枪弹"),
            ("ap_shells", "AP炮弹"),
            ("he_shells", "HE炮弹"),
            ("gold", "金条"),
            ("passes", "通行证"),
            ("morale", "士气"),
            ("damage", "损伤"),
            ("vp", "胜利点"),
            ("collapse", "崩溃"),
        ]
        for k, label in mapping:
            dv = int(result_delta.get(k, 0) or 0)
            if dv != 0:
                parts.append(f"{label}{_fmt_signed(dv)}")
        if ap_spent_total > 0:
            parts.append(f"行动点-{ap_spent_total}")

        io: List[str] = []
        if items_spent:
            spent_txt = "、".join([f"{n}x{c}" for n, c in items_spent.items() if int(c) > 0])
            if spent_txt:
                io.append(f"消耗：{spent_txt}")
        if items_gained:
            gain_txt = "、".join([f"{n}x{c}" for n, c in items_gained.items() if int(c) > 0])
            if gain_txt:
                io.append(f"获得：{gain_txt}")

        if not parts and not io:
            return
        left = "；".join(parts) if parts else ""
        right = "；".join(io) if io else ""
        if left and right:
            print(f"【结果】{left}｜{right}")
        elif left:
            print(f"【结果】{left}")
        else:
            print(f"【结果】{right}")

    def apply_delta(
        *,
        fuel: int = 0,
        ammo: int = 0,
        ap_shells: int = 0,
        he_shells: int = 0,
        gold: int = 0,
        passes: int = 0,
        morale: int = 0,
        damage: int = 0,
        vp: int = 0,
        collapse: int = 0,
    ) -> None:
        _bump(result_delta, "fuel", fuel)
        _bump(result_delta, "ammo", ammo)
        _bump(result_delta, "ap_shells", ap_shells)
        _bump(result_delta, "he_shells", he_shells)
        _bump(result_delta, "gold", gold)
        _bump(result_delta, "passes", passes)
        _bump(result_delta, "morale", morale)
        _bump(result_delta, "damage", damage)
        _bump(result_delta, "vp", vp)
        _bump(result_delta, "collapse", collapse)
        s.fuel += fuel
        # 兼容旧事件键名 ammo：此处代表机枪弹（更易得）
        s.mg_ammo += ammo
        s.ap_shells += ap_shells
        s.he_shells += he_shells
        s.gold_bars += gold
        s.passes += passes
        s.morale += morale
        s.damage += damage
        s.victory_points += vp
        s.city_collapse += collapse

    def grant(item: str, qty: int = 1) -> None:
        q = max(1, int(qty) if isinstance(qty, int) else 1)
        items_gained[item] = int(items_gained.get(item, 0) or 0) + q
        for _ in range(q):
            add_item(s, item)

    # 事件定义：尽量“城市生存/抉择感”，不做血腥描写
    # type:
    # - "supply": 调用 event_reward_supply
    # - "assist": 调用 event_assist_evacuation
    # - "choice": 小型分支（带选项与后续影响）
    # - "simple": 应用数值变化/道具/任务
    events: List[Dict[str, object]] = [
        # 1-8：补给与物资
        {"type": "supply", "text": "你们在废墟夹缝里找到一处被遗忘的补给。"},
        {"type": "simple", "text": "你们从一辆抛弃的运输车上拆下可用零件。", "item": ("备件", 1), "delta": {"morale": 1}},
        {"type": "simple", "text": "你们在倒塌的仓库里翻到几枚还能用的电池。", "item": ("电台电池", 1), "delta": {"morale": 1}},
        {"type": "simple", "text": "你们发现一小箱烟幕弹，或许能在下一次脱离接触时救命。", "item": ("烟幕弹", 1)},
        {"type": "simple", "text": "你们找到一只急救包，队伍状态稳定了些。", "item": ("急救包", 1), "delta": {"morale": 3}},
        {"type": "simple", "text": "你们在废弃药房的抽屉里找到几板药品，足够应付一次紧急处置。", "item": ("药品", 1), "delta": {"morale": 1}},
        {"type": "simple", "text": "你们把一段破损油管临时封住，燃油消耗稍微可控。", "delta": {"fuel": 6, "morale": 1}},
        {"type": "simple", "text": "你们把散落的机枪弹链重新整理，浪费减少了一点。", "delta": {"ammo": 8}},
        {"type": "simple", "text": "你们找到一桶还能用的燃油，但搬运时耽误了些时间。", "item": ("燃油桶", 1), "delta": {"morale": -1}},

        # 新：货币/交易线索
        {"type": "simple", "text": "你们在被炸开的保险柜里摸到一根金条：足够换到几次关键补给。", "delta": {"gold": 1, "morale": 1}},
        {"type": "simple", "text": "你们在废弃联络点捡到一张通行证：也许能换取特殊支援。", "delta": {"passes": 1}},
        {"type": "simple", "text": "你们在塌陷的弹药堆里翻出几发还能用的炮弹。", "delta": {"ap_shells": 1, "he_shells": 1, "morale": 1}},

        # 9-16：天气/道路/机械
        {"type": "simple", "text": "小雨落下，瓦砾路面变得泥泞。", "delta": {"fuel": -1, "morale": -1}},
        {"type": "simple", "text": "风沙扬起，能见度下降，你们只能放慢速度。", "delta": {"fuel": -1, "morale": -1}},
        {"type": "simple", "text": "夜色逼近，你们的判断变得更保守。", "delta": {"morale": -2}},
        {"type": "simple", "text": "浓雾笼罩，你们只能依靠经验与直觉前进。", "delta": {"fuel": -1, "morale": -1}},
        {"type": "simple", "text": "一段道路塌陷，你们不得不绕行。", "delta": {"fuel": -4, "morale": -1}},
        {"type": "simple", "text": "履带压过钢筋残端，车体震动让人心烦。", "delta": {"damage": 2, "morale": -1}},
        {"type": "simple", "text": "你们临时更换一处小部件，损伤略有缓解。", "delta": {"damage": -4, "morale": 1}},
        {"type": "simple", "text": "发动机短暂熄火，重新点火后你决定更谨慎地走。", "delta": {"fuel": -2, "morale": -1}},

        # 17-24：平民/信息/任务推进
        {"type": "simple", "text": "你们遇到一群躲藏的平民，他们请求你们指一条更安全的路。", "delta": {"morale": 3, "vp": 1}, "quest": ("Q1", 1)},
        {"type": "assist", "text": "你们发现有人在火线附近徘徊，你决定把他们带出危险区。"},
        {"type": "simple", "text": "墙上留着简短标记：‘此路不通’。你们避免了无谓消耗。", "delta": {"fuel": 2, "morale": 1}},
        {"type": "simple", "text": "有人递来一张手绘纸条：附近哪里可能有缺口。", "delta": {"vp": 1}, "quest": ("Q3", 1), "item": ("地图碎片", 1)},
        {"type": "simple", "text": "你们收到一段断续无线电：保持分散、别把钢铁当成答案。", "delta": {"morale": 2}},
        {"type": "simple", "text": "你们在地下室发现一些干粮与水，虽然不多，但让人心里稳一点。", "delta": {"morale": 2}},
        {"type": "simple", "text": "你们在相对安全的角落里静坐了几分钟。没人说话，但每个人都松了口气。", "stress_relief": {"mode": "all", "amount": 3}, "delta": {"morale": 1}},
        {"type": "simple", "text": "炮手把一段旧笑话讲完，装填手终于笑了一声。紧绷感散去一点。", "stress_relief": {"mode": "one", "amount": 12}, "delta": {"morale": 1}},
        {"type": "simple", "text": "你让车组轮流做一次深呼吸与检查清单。节奏回来了。", "stress_relief": {"mode": "all", "amount": 4}},
        {"type": "simple", "text": "你们在废报纸里看到一段消息，真假难辨，但足以让人烦躁。", "delta": {"morale": -2, "collapse": 1}},
        {"type": "simple", "text": "你们找到一处临时避难点的路标，至少知道有人努力过。", "delta": {"morale": 2}},

        # 25-32：低烈度风险/取舍
        {"type": "simple", "text": "你们短暂暴露在开阔地带，必须快速通过。", "delta": {"fuel": -2, "morale": -1, "damage": 1}},
        {"type": "simple", "text": "你们误入一段死胡同，倒车与转向让油耗上升。", "delta": {"fuel": -3, "morale": -1}},
        {"type": "simple", "text": "你们听到近处的脚步声，只能屏息等待对方远去。", "delta": {"morale": -2}},
        {"type": "simple", "text": "你们发现一处可疑路障，谨慎绕过，避免了更大的麻烦。", "delta": {"fuel": -1, "morale": 1, "vp": 1}},
        {"type": "simple", "text": "你们在废弃掩体里找到一点点可用弹药。", "item": ("弹药箱", 1), "delta": {"morale": 1}},
        {"type": "simple", "text": "你们清理出一条勉强可通行的路，付出体力但减少后续损耗。", "delta": {"morale": -1, "fuel": 2, "damage": -1}},
        {"type": "simple", "text": "驾驶员（兼机械）坚持做了例行检查：这次它是对的。", "delta": {"damage": -3, "morale": 1}},
        {"type": "simple", "text": "你们决定不追逐传闻里的‘大补给’，保持节奏。", "delta": {"morale": 1}},

        # 新：少量“陷阱事件”（低烈度、强调抉择；不做血腥描写）
        {
            "type": "choice",
            "id": "EV_TRAP_01",
            "text": "路边有一只被布草盖住的小箱子：太干净、太完整，像是专门等人来捡。驾驶员低声说：‘这东西看着不对。’",
            "options": {
                "1": {"label": "花1行动点检查并拆除可疑机关（获得备件x1；士气+1）", "require_ap": 1, "cost_ap": 1, "loot": ("备件", 1), "delta": {"morale": 1}},
                "2": {"label": "直接拖走（更快，但风险↑）", "delta": {"damage": 4, "morale": -2}, "loot": ("弹药箱", 1), "extra_encounter": 1},
                "3": {"label": "绕开它（更稳，但多耗点油）", "delta": {"fuel": -2, "morale": 1}},
            },
        },
        {
            "type": "choice",
            "id": "EV_TRAP_02",
            "text": "前方路面被瓦砾和断裂钢筋覆盖，像一张随时会咬住履带的网。你可以慢慢爬过去，也可以绕远路。",
            "options": {
                "1": {"label": "低速通过（更稳，但耗时）", "delta": {"fuel": -1, "morale": -1, "damage": 1}},
                "2": {"label": "绕远路（更耗油，但更安全）", "delta": {"fuel": -3, "morale": 1}},
                "3": {"label": "硬闯（更快，但更冒险）", "delta": {"fuel": 1, "morale": -2, "damage": 5}, "extra_encounter": 1},
            },
        },
        {
            "type": "choice",
            "id": "EV_TRAP_03",
            "text": "一段门廊里挂着细得几乎看不见的线，像是随便一碰就会引来麻烦。炮手盯着阴影：‘要么排查，要么离开。’",
            "options": {
                "1": {"label": "花1行动点排查（士气+1；获得烟幕弹x1）", "require_ap": 1, "cost_ap": 1, "delta": {"morale": 1}, "loot": ("烟幕弹", 1)},
                "2": {"label": "用一段机枪点射压住角落（机枪弹-8；更冒险）", "delta": {"ammo": -8, "morale": -1}, "extra_encounter": 1},
                "3": {"label": "倒车退出（稳，但耽误节奏）", "delta": {"fuel": -1, "morale": 1}},
            },
        },
        {
            "type": "choice",
            "id": "EV_TRAP_04",
            "text": "你们准备穿过一扇塌陷的车库门时，驾驶员看见地面一段不自然的‘新土’：像是有人刚埋过什么，也像是有人刚挖过什么。",
            "options": {
                "1": {"label": "花1行动点绕着边缘探一下（更稳；士气+1）", "require_ap": 1, "cost_ap": 1, "delta": {"morale": 1, "fuel": -1}},
                "2": {"label": "用工具箱做一次临时铺垫（需要工具箱x1；损伤↓）", "require_item": ("工具箱", 1), "cost_item": ("工具箱", 1), "delta": {"damage": -2, "morale": 1, "fuel": -1}},
                "3": {"label": "不管了直接过去（更快，但更冒险）", "delta": {"fuel": 1, "morale": -2, "damage": 4}, "extra_encounter": 1},
            },
        },
        {
            "type": "choice",
            "id": "EV_TRAP_05",
            "text": "通信员在残墙后的电线堆里听到一段短促的信号：像是‘求援’，又像是‘诱饵’。你知道在这座城市里，声音本身也可能是陷阱。",
            "options": {
                "1": {"label": "无视信号，保持静默（更稳）", "delta": {"morale": 1}},
                "2": {"label": "花1行动点监听并做记录（观察+1；但可能引来麻烦）", "require_ap": 1, "cost_ap": 1, "buff": ("观察", 1), "delta": {"morale": 1}, "extra_encounter": 1},
                "3": {"label": "用电台回发一次试探（需要电台电池x1；胜利点+1，但更冒险）", "require_item": ("电台电池", 1), "cost_item": ("电台电池", 1), "delta": {"vp": 1, "morale": -1, "collapse": 1}, "extra_encounter": 1},
            },
        },
        {
            "type": "choice",
            "id": "EV_TRAP_06",
            "text": "拐角处有一块破木牌，上面只剩半句话：‘……别走中间。’地面上看不出明显痕迹，但你不敢把履带的重量当成运气。",
            "options": {
                "1": {"label": "贴墙慢行（更稳，但慢）", "delta": {"fuel": -1, "morale": -1, "damage": 1}},
                "2": {"label": "绕远路（更安全，但耗油）", "delta": {"fuel": -3, "morale": 1}},
                "3": {"label": "硬走最宽的路（更快，但更冒险）", "delta": {"fuel": 1, "morale": -2, "damage": 5}, "extra_encounter": 1},
            },
        },
        {
            "type": "choice",
            "id": "EV_TRAP_07",
            "text": "路边靠着一只油桶，桶身擦得过分干净，像是刻意留给过路者。装填手伸手时，你下意识按住他：‘先别急。’",
            "options": {
                "1": {"label": "花1行动点检查桶身与周围（获得燃油桶x1；士气+1）", "require_ap": 1, "cost_ap": 1, "loot": ("燃油桶", 1), "delta": {"morale": 1}},
                "2": {"label": "直接搬走（可能有收获，但更冒险）", "loot": ("燃油桶", 1), "delta": {"damage": 3, "morale": -2}, "extra_encounter": 1},
                "3": {"label": "把它留在那（更稳；继续前进）", "delta": {"fuel": 1, "morale": 1}},
            },
        },
        {
            "type": "choice",
            "id": "EV_TRAP_08",
            "text": "楼道口的沙袋摆得很像‘守军阵地’，但沙袋上没有灰，反而像刚搬过来。你闻到一股淡淡的机油味。",
            "options": {
                "1": {"label": "花1行动点远距离确认（观察+1；士气+1）", "require_ap": 1, "cost_ap": 1, "buff": ("观察", 1), "delta": {"morale": 1}},
                "2": {"label": "绕开这条楼道（更稳，但耗油）", "delta": {"fuel": -2, "morale": 1}},
                "3": {"label": "强行穿过去（更快，但更冒险）", "delta": {"fuel": 1, "morale": -2, "damage": 3}, "extra_encounter": 1},
            },
        },
        {
            "type": "choice",
            "id": "EV_TRAP_09",
            "text": "一条窄街上散落着几只整齐的空弹箱，像是在暗示这里‘刚发生过战斗’。但你总觉得它们摆放得过于刻意。",
            "options": {
                "1": {"label": "花1行动点沿侧翼绕行（更稳；士气+1）", "require_ap": 1, "cost_ap": 1, "delta": {"fuel": -1, "morale": 1}},
                "2": {"label": "用烟幕弹遮蔽通过（消耗烟幕弹x1；损伤更小）", "require_item": ("烟幕弹", 1), "cost_item": ("烟幕弹", 1), "delta": {"fuel": -1, "morale": 1, "damage": -1}},
                "3": {"label": "直接走中间（更快，但更冒险）", "delta": {"fuel": 1, "morale": -2, "damage": 4}, "extra_encounter": 1},
            },
        },
        {
            "type": "choice",
            "id": "EV_TRAP_10",
            "text": "你们看到一块写着‘检查点’的路牌，字迹很新。按理说这里不该还有人维护标识。",
            "options": {
                "1": {"label": "花1行动点观察周边（观察+1；避免无谓损耗）", "require_ap": 1, "cost_ap": 1, "buff": ("观察", 1), "delta": {"fuel": 1, "morale": 1}},
                "2": {"label": "拿通行证换取一次“正面通过”的底气（消耗通行证x1）", "require_item": ("通行证", 1), "cost_item": ("通行证", 1), "delta": {"vp": 1, "morale": 1}},
                "3": {"label": "不管路牌，直接穿越（更快，但更冒险）", "delta": {"fuel": 1, "morale": -2, "damage": 4}, "extra_encounter": 1},
            },
        },
        {
            "type": "choice",
            "id": "EV_TRAP_11",
            "text": "一辆侧翻的卡车挡住道路，车厢里露出几只木箱。太像‘礼物’了，也太像‘钩子’。",
            "options": {
                "1": {"label": "花1行动点从外侧撬开一只（获得弹药箱x1；更冒险）", "require_ap": 1, "cost_ap": 1, "loot": ("弹药箱", 1), "delta": {"morale": 1}, "extra_encounter": 1},
                "2": {"label": "只把卡车推开一点点，留箱子不动（更稳）", "delta": {"fuel": -1, "morale": 1}},
                "3": {"label": "从卡车底下硬挤过去（更快，但损伤↑）", "delta": {"fuel": 1, "morale": -2, "damage": 5}},
            },
        },
        {
            "type": "choice",
            "id": "EV_TRAP_12",
            "text": "巷口有一盏还亮着的信号灯，灯光在灰尘里显得不真实。你不确定它是在指引，还是在标记。",
            "options": {
                "1": {"label": "花1行动点关掉信号灯（更隐蔽；伪装+1）", "require_ap": 1, "cost_ap": 1, "buff": ("伪装", 1), "delta": {"morale": 1}},
                "2": {"label": "绕开有光的巷口（更稳）", "delta": {"fuel": -1, "morale": 1}},
                "3": {"label": "顶着灯光通过（更快，但更冒险）", "delta": {"fuel": 1, "morale": -2, "damage": 3}, "extra_encounter": 1},
            },
        },
        {
            "type": "choice",
            "id": "EV_TRAP_13",
            "text": "一段下坡路上有一片‘过于平整’的碎石层，履带压上去的感觉像踩在空心地板。",
            "options": {
                "1": {"label": "低速探路（更稳，但耗时）", "delta": {"fuel": -1, "morale": -1, "damage": 1}},
                "2": {"label": "绕开下坡（更安全，但耗油）", "delta": {"fuel": -3, "morale": 1}},
                "3": {"label": "硬压过去（更快，但更冒险）", "delta": {"fuel": 1, "morale": -2, "damage": 6}, "extra_encounter": 1},
            },
        },
        {
            "type": "choice",
            "id": "EV_TRAP_14",
            "text": "你们在门洞里发现一条新挂的绳子，像是‘标记路线’。但标记这件事，本身就值得怀疑。",
            "options": {
                "1": {"label": "花1行动点把绳子拆下带走（获得绳索x1；士气+1）", "require_ap": 1, "cost_ap": 1, "loot": ("绳索", 1), "delta": {"morale": 1}},
                "2": {"label": "不碰任何标记，绕开（更稳）", "delta": {"fuel": -2, "morale": 1}},
                "3": {"label": "相信标记走直线（更快，但更冒险）", "delta": {"fuel": 1, "morale": -2, "damage": 3}, "extra_encounter": 1},
            },
        },
        {
            "type": "choice",
            "id": "EV_TRAP_15",
            "text": "一处半塌的门脸里散发出刺鼻气味：像是燃料泄漏。你不确定它是意外，还是故意用来逼人绕路。",
            "options": {
                "1": {"label": "绕开泄漏点（更稳，但耗油）", "delta": {"fuel": -2, "morale": 1}},
                "2": {"label": "花1行动点用布条做简易封堵（损伤↓；士气+1）", "require_ap": 1, "cost_ap": 1, "delta": {"damage": -2, "morale": 1, "fuel": -1}},
                "3": {"label": "快速穿过（更快，但更冒险）", "delta": {"fuel": 1, "morale": -2, "damage": 4}, "extra_encounter": 1},
            },
        },
        {
            "type": "choice",
            "id": "EV_TRAP_16",
            "text": "你们经过一条窄桥，桥面木板被换过几块，看起来像刚维修。问题是：谁还在维修？",
            "options": {
                "1": {"label": "低速逐块压过去（更稳）", "delta": {"fuel": -1, "morale": -1, "damage": 1}},
                "2": {"label": "绕开桥（更安全，但耗油）", "delta": {"fuel": -3, "morale": 1}},
                "3": {"label": "加速冲桥（更快，但损伤↑）", "delta": {"fuel": 1, "morale": -2, "damage": 6}},
            },
        },
        {
            "type": "choice",
            "id": "EV_TRAP_17",
            "text": "一个小窗口里透出微弱的手电光，光在固定角度晃了两次，像暗号，也像引诱你们靠近。",
            "options": {
                "1": {"label": "不靠近，直接离开（更稳）", "delta": {"morale": 1, "fuel": 1}},
                "2": {"label": "花1行动点确认情况（可能得到情报，但更冒险）", "require_ap": 1, "cost_ap": 1, "delta": {"vp": 1, "morale": 1}, "extra_encounter": 1},
                "3": {"label": "用香烟换一句话（消耗香烟x1；士气+2）", "require_item": ("香烟", 1), "cost_item": ("香烟", 1), "delta": {"morale": 2, "vp": 1}},
            },
        },
        {
            "type": "choice",
            "id": "EV_TRAP_18",
            "text": "一段长走廊里摆着几面碎镜子，反光会把你们的位置‘说出去’。你可以清理它们，也可以赌运气。",
            "options": {
                "1": {"label": "花1行动点清理碎镜（更隐蔽；伪装+1）", "require_ap": 1, "cost_ap": 1, "buff": ("伪装", 1), "delta": {"morale": 1}},
                "2": {"label": "绕开走廊（更稳，但耗油）", "delta": {"fuel": -2, "morale": 1}},
                "3": {"label": "直接通过（更快，但更冒险）", "delta": {"fuel": 1, "morale": -2, "damage": 3}, "extra_encounter": 1},
            },
        },
        {
            "type": "choice",
            "id": "EV_TRAP_19",
            "text": "地上有一排新鲜脚印，刻意从一条小门洞穿过去。追脚印很诱人，但在这里，‘诱人’往往就是陷阱的一部分。",
            "options": {
                "1": {"label": "不追脚印，按自己的路线走（更稳）", "delta": {"morale": 1}},
                "2": {"label": "花1行动点快速侦察（观察+1；但更冒险）", "require_ap": 1, "cost_ap": 1, "buff": ("观察", 1), "delta": {"morale": 1}, "extra_encounter": 1},
                "3": {"label": "追脚印走捷径（更快，但可能吃亏）", "delta": {"fuel": 2, "morale": -2, "damage": 4}},
            },
        },
        {
            "type": "choice",
            "id": "EV_TRAP_20",
            "text": "你们看到一处‘临时补给点’的粉笔字，旁边还画了一个箭头。箭头太直白了，直白得不真实。",
            "options": {
                "1": {"label": "把粉笔字擦掉并离开（更稳；士气+1）", "delta": {"morale": 1, "fuel": 1}},
                "2": {"label": "花1行动点确认补给点（可能有收获，但更冒险）", "require_ap": 1, "cost_ap": 1, "loot": ("备件", 1), "delta": {"morale": 1}, "extra_encounter": 1},
                "3": {"label": "不检查，直接按箭头走（更快，但更冒险）", "delta": {"fuel": 1, "morale": -2, "damage": 4}, "extra_encounter": 1},
            },
        },
        {
            "type": "choice",
            "id": "EV_TRAP_21",
            "text": "一段地铁入口被木板钉住，木板上留着‘安全’两个字。你盯着那两个字，觉得它们比任何危险都可疑。",
            "options": {
                "1": {"label": "花1行动点撬开一条缝观察（地图碎片x1；更冒险）", "require_ap": 1, "cost_ap": 1, "loot": ("地图碎片", 1), "delta": {"morale": 1}, "extra_encounter": 1},
                "2": {"label": "绕开入口（更稳）", "delta": {"fuel": -1, "morale": 1}},
                "3": {"label": "强行进入（更快，但更冒险）", "delta": {"fuel": 1, "morale": -2, "damage": 5}, "extra_encounter": 1},
            },
        },
        {
            "type": "choice",
            "id": "EV_TRAP_22",
            "text": "一台半坏的发电机还在发出嗡鸣声。它既可能是资源，也可能是‘信号源’——把你们暴露出去。",
            "options": {
                "1": {"label": "关机并离开（更隐蔽；士气+1）", "delta": {"morale": 1, "fuel": 1}},
                "2": {"label": "花1行动点拆走一段线圈（获得备件x1；更冒险）", "require_ap": 1, "cost_ap": 1, "loot": ("备件", 1), "delta": {"morale": 1}, "extra_encounter": 1},
                "3": {"label": "不管它，快速通过（更快，但更冒险）", "delta": {"fuel": 1, "morale": -2, "damage": 3}, "extra_encounter": 1},
            },
        },
        {
            "type": "choice",
            "id": "EV_TRAP_23",
            "text": "门口地面有一圈细小的白粉，像是做过‘边界标记’。你不确定那是用于警示，还是用于‘确认你们经过’。",
            "options": {
                "1": {"label": "绕开白粉圈（更稳）", "delta": {"fuel": -1, "morale": 1}},
                "2": {"label": "花1行动点把白粉扫散（更隐蔽；士气+1）", "require_ap": 1, "cost_ap": 1, "delta": {"morale": 1, "fuel": -1}},
                "3": {"label": "直接压过去（更快，但更冒险）", "delta": {"fuel": 1, "morale": -2, "damage": 4}, "extra_encounter": 1},
            },
        },
        {
            "type": "choice",
            "id": "EV_TRAP_24",
            "text": "一处窗台上放着一份完整的医疗包。太完整了，完整得像在提醒你：‘快拿走我。’",
            "options": {
                "1": {"label": "花1行动点检查后取走（获得医疗包x1；士气+1）", "require_ap": 1, "cost_ap": 1, "loot": ("医疗包", 1), "delta": {"morale": 1}},
                "2": {"label": "不拿，保持节奏（更稳）", "delta": {"morale": 1, "fuel": 1}},
                "3": {"label": "直接伸手拿（更快，但更冒险）", "loot": ("医疗包", 1), "delta": {"morale": -2, "damage": 3}, "extra_encounter": 1},
            },
        },
        {
            "type": "choice",
            "id": "EV_TRAP_25",
            "text": "你们经过一处临时路障，路障上有一枚醒目的布条。布条像是在告诉你：‘从这边走。’",
            "options": {
                "1": {"label": "花1行动点从高处确认（观察+1；更稳）", "require_ap": 1, "cost_ap": 1, "buff": ("观察", 1), "delta": {"morale": 1}},
                "2": {"label": "绕行路障（更安全，但耗油）", "delta": {"fuel": -3, "morale": 1}},
                "3": {"label": "照布条指引通过（更快，但更冒险）", "delta": {"fuel": 1, "morale": -2, "damage": 5}, "extra_encounter": 1},
            },
        },
        {
            "type": "choice",
            "id": "EV_TRAP_26",
            "text": "巷子里有一阵规律的敲击声，像是有人在‘示意安全’，也像是有人在‘校准距离’。你不想把自己的位置交给节奏。",
            "options": {
                "1": {"label": "保持静默并绕开（更稳）", "delta": {"fuel": -1, "morale": 1}},
                "2": {"label": "花1行动点等一轮敲击结束再动（更稳；士气+1）", "require_ap": 1, "cost_ap": 1, "delta": {"morale": 1, "fuel": -1}},
                "3": {"label": "趁敲击掩护快速通过（更快，但更冒险）", "delta": {"fuel": 1, "morale": -2, "damage": 4}, "extra_encounter": 1},
            },
        },
        {
            "type": "choice",
            "id": "EV_TRAP_27",
            "text": "你们发现一张被钉在门框上的‘路线图’，线条画得很清楚，清楚得像是专门给陌生人看的。",
            "options": {
                "1": {"label": "花1行动点抄下关键点（获得地图碎片x1；观察+1）", "require_ap": 1, "cost_ap": 1, "loot": ("地图碎片", 1), "buff": ("观察", 1), "delta": {"morale": 1}},
                "2": {"label": "把路线图撕掉并离开（更稳；士气+1）", "delta": {"morale": 1, "fuel": 1}},
                "3": {"label": "照着路线图走（更快，但更冒险）", "delta": {"fuel": 2, "morale": -2, "damage": 4}, "extra_encounter": 1},
            },
        },

        # 现实机制：敌方炮兵校射/炮火骚扰（带取舍）
        {
            "type": "choice",
            "id": "EV_CHOICE_ARTY_01",
            "text": "远处传来沉闷的炮声。几发炮弹在街区外缘炸开——敌方炮兵在校射，你们必须决定是停下隐蔽，还是趁间隙快速穿过。",
            "options": {
                "1": {"label": "停下找掩体等待（更稳，但耗时）", "delta": {"fuel": -1, "morale": -1, "damage": 1}},
                "2": {"label": "趁间隙快速通过（更冒险）", "delta": {"fuel": -2, "morale": -2, "damage": 6}, "extra_encounter": 1},
            },
        },

        # 33-36：地图/侦察能力
        {"type": "simple", "text": "你们在废墟中发现了一张标有线索的地图碎片。", "item": ("地图碎片", 1), "quest": ("Q3", 1)},
        {"type": "simple", "text": "通信员提醒你：用眼睛而不是情绪做决定。", "buff": ("观察", 1), "delta": {"morale": 1}},
        {"type": "simple", "text": "你们用地图把路线重新标注，少走了一个岔路。", "delta": {"fuel": 2, "morale": 1}},
        {"type": "simple", "text": "你们在墙缝里摸到一张潮湿的地图碎片，但仍然可用。", "item": ("地图碎片", 1), "quest": ("Q3", 1), "delta": {"morale": -1}},

        # 37-44：带选择的小分支（更像“路口抉择”）
        {
            "type": "choice",
            "id": "EV_CHOICE_CIV_01",
            "text": "你们在倒塌楼梯间听到孩子的哭声。停下可能会暴露，但不停下又像把人留给瓦砾。",
            "options": {
                "1": {"label": "停下寻找（士气↑，可能触发遭遇）", "delta": {"morale": 4, "vp": 1}, "flag": ("helped_child_once", True), "extra_encounter": 1},
                "2": {"label": "继续前进（燃油略省，但士气↓）", "delta": {"fuel": 2, "morale": -3}, "flag": ("ignored_calls_once", True)},
            },
        },
        {
            "type": "choice",
            "id": "EV_CHOICE_SUPPLY_02",
            "text": "你们发现两处补给点：一处更近但暴露，一处更远但隐蔽。",
            "options": {
                "1": {"label": "抢近处（更可能拿到机枪弹）", "delta": {"ammo": 10, "morale": -1}, "loot": ("弹药箱", 1), "extra_encounter": 1},
                "2": {"label": "绕去隐蔽处（更可能拿到备件）", "delta": {"fuel": -2, "morale": 1}, "loot": ("备件", 1)},
            },
        },
        {
            "type": "choice",
            "id": "EV_CHOICE_RADIO_03",
            "text": "电台里传来两段互相矛盾的指令：一段要求集结，一段要求分散。",
            "options": {
                "1": {"label": "选择集结（胜利点↑，但崩溃↑）", "delta": {"vp": 1, "collapse": 2, "morale": -1}, "flag": ("followed_rally", True)},
                "2": {"label": "选择分散（士气↑，但驻军好感可能↓）", "delta": {"morale": 2}, "flag": ("stayed_disperse", True)},
            },
        },
        {
            "type": "choice",
            "id": "EV_CHOICE_PRISONER_04",
            "text": "你们遇到一名惊慌失措的敌方士兵，他举起手，反复说自己只想活着。",
            "options": {
                "1": {"label": "放走并指路（士气↑，后续遭遇风险↓）", "delta": {"morale": 3}, "buff": ("观察", 1), "flag": ("spared_prisoner", True)},
                "2": {"label": "绑走审问（情报↑，但行动更慢）", "delta": {"fuel": -2, "vp": 1, "morale": -1}, "flag": ("took_prisoner", True)},
            },
        },
        {
            "type": "choice",
            "id": "EV_CHOICE_AIRRAID_05",
            "text": "警报声在断墙间回荡：空袭逼近。你可以停下寻找掩体，也可以趁爆炸与烟尘的混乱硬闯一段路。",
            "options": {
                "1": {"label": "立刻找掩体等待（更安全，节奏更稳）", "delta": {"fuel": -1, "morale": -1, "damage": -2}, "buff": ("观察", 1), "flag": ("airraid_sheltered_once", True)},
                "2": {"label": "趁混乱突进并搜刮（可能有收获，但更冒险）", "delta": {"fuel": -2, "morale": -2, "damage": 3, "vp": 1}, "loot": ("弹药箱", 1), "extra_encounter": 1, "flag": ("airraid_rushed_once", True)},
            },
        },

        # 45-54：新区域/地形补充事件
        {"type": "simple", "text": "你们在营地边缘遇到一名疲惫的医护人员，他只留下几句忠告与一包绷带。", "item": ("医疗包", 1), "delta": {"morale": 1}},
        {"type": "simple", "text": "公路断桥旁散落着被抛弃的行军物资，数量不多但能救急。", "item": ("燃油桶", 1), "delta": {"morale": -1}},
        {"type": "simple", "text": "检查点残骸里有一台坏掉的电台，你们勉强拆出还能用的电池。", "item": ("电台电池", 1), "delta": {"morale": 1}},
        {"type": "simple", "text": "堤坝缺口附近水汽弥漫，你们趁雾气遮掩短暂调整路线。", "buff": ("观察", 1), "delta": {"fuel": 1}},
        {"type": "simple", "text": "废弃修理厂里找到一套旧工具，虽然生锈，但能顶一阵。", "item": ("工具箱", 1), "delta": {"morale": 1}},
        {"type": "simple", "text": "农舍与谷仓里留着一点干粮，没人知道主人去了哪里。", "delta": {"morale": 3}},
        {"type": "simple", "text": "铁路堤旁的碎石让履带受力不均，你们小心通过仍免不了磕碰。", "delta": {"damage": 2, "morale": -1}},
        {"type": "simple", "text": "阵地残址里有几箱掩埋的弹链，你们带走其中一部分。", "item": ("弹药箱", 1)},
        {
            "type": "choice",
            "id": "EV_CHOICE_CAMP_06",
            "text": "难民营里有人想搭车离开，也有人指责你们会把危险带来。你需要决定是否停留协调。",
            "options": {
                "1": {"label": "停下协调与安抚（士气↑，但更容易遭遇）", "delta": {"morale": 4, "vp": 1}, "extra_encounter": 1, "flag": ("camp_helped_once", True)},
                "2": {"label": "快速通过（更省时间，但士气↓）", "delta": {"morale": -2, "fuel": 1}, "flag": ("camp_ignored_once", True)},
            },
        },
        {
            "type": "choice",
            "id": "EV_CHOICE_BRIDGE_07",
            "text": "断桥边有一条狭窄便道：能省油但更颠簸；也可以绕远路，更稳却更耗燃油。",
            "options": {
                "1": {"label": "走便道（燃油省，损伤风险↑）", "delta": {"fuel": 2, "damage": 3, "morale": -1}},
                "2": {"label": "绕远路（更稳，但耗油）", "delta": {"fuel": -3, "morale": 1}},
            },
        },

        # 新机制：街头溃军（可加入我方/提高好感）
        {
            "type": "choice",
            "id": "EV_CHOICE_ROUTED_01",
            "text": "你们在大街拐角遇到一股溃散的士兵：有人丢了枪，有人只剩眼神。他们看见虎王，犹豫着跟上。",
            "options": {
                "1": {
                    "label": "收拢他们并分发弹药（机枪弹-20；好感+6；获得支援单位）",
                    "delta": {"ammo": -20, "morale": 1},
                    "sector": {"favor": 6, "fall": -1},
                    "garrison_add": ["机枪队", "国防军"],
                },
                "2": {
                    "label": "让他们去附近驻军据点（好感+2）",
                    "sector": {"favor": 2},
                    "delta": {"morale": 1},
                },
            },
        },

        # 新机制：受损坦克/车辆（花剩余行动点修理换支援）
        {
            "type": "choice",
            "id": "EV_CHOICE_DAMAGED_TANK_01",
            "text": "你们在街角发现一辆受损的己方装甲车辆：履带脱落、烟管破损。车组正犹豫是弃车还是抢救。",
            "options": {
                "1": {
                    "label": "花1行动点做临时处置（换取金条与感谢）",
                    "require_ap": 1,
                    "cost_ap": 1,
                    "delta": {"gold": 1, "morale": 2},
                    "sector": {"favor": 2},
                },
                "2": {
                    "label": "花2行动点协助更彻底修理（换通行证与炮弹）",
                    "require_ap": 2,
                    "cost_ap": 2,
                    "delta": {"passes": 1, "morale": 3},
                    "loot": ("炮弹箱", 1),
                    "sector": {"favor": 3, "fall": -1},
                },
                "3": {
                    "label": "投入全部剩余行动点抢救（换来装甲支援）",
                    "require_ap": 1,
                    "cost_ap": "all",
                    "tank_support": 1,
                    "delta": {"morale": 4, "vp": 1},
                    "sector": {"favor": 4},
                },
                "4": {"label": "不冒险，继续前进", "delta": {"morale": -1}},
            },
        },

        # 新：气味、噪音与小型取舍（更偏“生存感”）
        {"type": "simple", "text": "你们在废弃咖啡馆的柜台后摸到一小罐咖啡粉。它不重要，但它让人想起‘正常’。", "item": ("咖啡", 1), "delta": {"morale": 1}},
        {"type": "simple", "text": "你们在倒塌的洗衣房里找到几卷干净布料，简单包扎让人安心些。", "item": ("医疗包", 1), "delta": {"morale": 1}},
        {"type": "simple", "text": "你们听到远处的扩音器与回声，字句听不清，但足以让人烦躁。", "delta": {"morale": -2, "collapse": 1}},
        {"type": "simple", "text": "驾驶员坚持把油路又检查了一遍：这次没有惊喜。", "delta": {"damage": -1, "morale": 1}},
        {
            "type": "choice",
            "id": "EV_CHOICE_ENGINE_02",
            "text": "发动机声线有些不对。你可以现在处理，或者赌它能撑到下一次休整。",
            "options": {
                "1": {
                    "label": "用1个备件做现场处置（损伤↓）",
                    "require_item": ("备件", 1),
                    "cost_item": ("备件", 1),
                    "delta": {"damage": -8, "morale": 1, "fuel": -1},
                },
                "2": {"label": "先不动它（省事，但更不安心）", "delta": {"morale": -2}},
            },
        },
        {
            "type": "choice",
            "id": "EV_CHOICE_SMOKE_08",
            "text": "前方街口可能有观察哨。你可以用烟幕做一次更干净的脱离，也可以赌对方没看见。",
            "options": {
                "1": {
                    "label": "消耗1枚烟幕弹，降低风险（更稳）",
                    "require_item": ("烟幕弹", 1),
                    "cost_item": ("烟幕弹", 1),
                    "delta": {"morale": 1},
                    "buff": ("观察", 1),
                },
                "2": {"label": "直接通过（更省时间，但更冒险）", "delta": {"morale": -1, "damage": 2}, "extra_encounter": 1},
            },
        },

        # --- 追加：更多随机事件（30条；以“好事件”为主） ---
        # 1-21：直接收益（补给/修理/士气/侦察）
        {"type": "simple", "text": "你们在一间半塌的小卖部里找到几包香烟。不是必需品，但它让人能把手从颤抖里解放出来。", "item": ("香烟", 1), "delta": {"morale": 2}},
        {"type": "simple", "text": "你们在被抛弃的油罐旁发现一只‘纯燃料桶’，包装完好得让人不敢相信。", "item": ("纯燃料桶", 1), "delta": {"morale": 1}},
        {"type": "simple", "text": "你们在废弃厨房的柜子里找到一份口粮。量不多，但足以让人熬过今天。", "item": ("口粮", 1), "delta": {"morale": 1}},
        {"type": "simple", "text": "你们从一台坏掉的机床旁摸到一罐润滑油。它能把很多‘小问题’压在爆发前。", "item": ("润滑油", 1), "delta": {"morale": 1}},
        {"type": "simple", "text": "你们找到一张折叠好的伪装网。现在这就是你们的‘隐身术’。", "item": ("伪装网", 1), "delta": {"morale": 1}},
        {"type": "simple", "text": "你们在哨所残骸里捡到一套侦察设备，镜片虽然裂了，但仍能用。", "item": ("侦察设备", 1), "delta": {"morale": 1}},
        {"type": "simple", "text": "你们从废墟里拖出一块还能用的装甲板。钢铁很冷，但它会替你们挡下一次意外。", "item": ("装甲板", 1), "delta": {"morale": 1}},
        {"type": "simple", "text": "你们在塌陷的补给堆里翻到一捆完整的弹药：今天至少不会‘空手’。", "item": ("弹药", 1), "delta": {"morale": 1}},
        {"type": "simple", "text": "驾驶员从残余油料里抽出还能用的部分，直接灌进油箱。", "delta": {"fuel": 20, "morale": 1}},
        {"type": "simple", "text": "驾驶员（兼机械）用几分钟把异响定位并做了微调：这车还能再撑一阵。", "delta": {"damage": -6, "morale": 1}},
        {"type": "simple", "text": "炮手重新校准了瞄具与观察角度，至少下一次不会那么‘盲’。", "buff": ("观察", 1), "delta": {"morale": 1}},
        {"type": "simple", "text": "通信员把断裂的线缆重新接好，电台杂音少了一点。", "buff": ("观察", 1), "delta": {"morale": 2}},
        {"type": "simple", "text": "你们把散落的弹链重新编排，手指被割破，但换来更可靠的火力。", "delta": {"ammo": 22, "morale": 1}},
        {"type": "simple", "text": "你们在壁龛里摸到一只炮弹箱：不多，但很关键。", "item": ("炮弹箱", 1), "delta": {"morale": 1}},
        {"type": "simple", "text": "你们从修理台下拖出一只工具箱。有人认真准备过，只是没机会用完。", "item": ("工具箱", 1), "delta": {"morale": 1}},
        {"type": "simple", "text": "你们找到一个医疗包。它意味着‘还有办法’。", "item": ("医疗包", 1), "delta": {"morale": 1}},
        {"type": "simple", "text": "你们在急救站废墟里找到一些药品，至少能撑过下一次伤情波动。", "item": ("药品", 1), "delta": {"morale": 1}},
        {"type": "simple", "text": "你们在床垫夹层里摸到一个急救包，装填手把它小心地塞进随身袋。", "item": ("急救包", 1), "delta": {"morale": 1}},
        {"type": "simple", "text": "你们沿着墙上的记号绕开了塌方区，省下一段无谓的消耗。", "delta": {"fuel": 4, "morale": 1}},
        {"type": "simple", "text": "你让车组在掩体里安静休整几分钟：把呼吸找回来，也把手的动作找回来。", "stress_relief": {"mode": "all", "amount": 6}, "delta": {"morale": 2}},
        {"type": "simple", "text": "车长把路线、风险点、撤离口重新写进脑子里：下一步会更像‘选择’，而不是‘被推着走’。", "buff": ("搜索加成", 1), "delta": {"morale": 1}},

        # 22-29：带选择（多数选项是正向、少数是小代价换更大收益）
        {
            "type": "choice",
            "id": "EV_CHOICE_GOOD_101",
            "text": "你们在墙洞后发现一个上锁的军需箱。用时间撬开它能换来补给，但停留也意味着风险。",
            "options": {
                "1": {"label": "花1行动点撬开（获得弹药箱x1；士气+1）", "require_ap": 1, "cost_ap": 1, "loot": ("弹药箱", 1), "delta": {"morale": 1}},
                "2": {"label": "放弃箱子，保持节奏（燃油+1）", "delta": {"fuel": 1}},
            },
        },
        {
            "type": "choice",
            "id": "EV_CHOICE_GOOD_102",
            "text": "你们遇到一名懂机械的老工人。他说自己还能‘听出’发动机的问题，只是需要一点代价。",
            "options": {
                "1": {"label": "付出1根金条请他处理（损伤-15；士气+2）", "delta": {"gold": -1, "damage": -15, "morale": 2}},
                "2": {"label": "用1个备件换他帮你做快速检查（损伤-8；士气+1）", "require_item": ("备件", 1), "cost_item": ("备件", 1), "delta": {"damage": -8, "morale": 1}},
                "3": {"label": "道谢离开（不冒险）", "delta": {"morale": 0}},
            },
        },
        {
            "type": "choice",
            "id": "EV_CHOICE_GOOD_103",
            "text": "一台半坏的无线电还在断续发声。你们可以试着呼叫一次支援，也可以把电池拆走当补给。",
            "options": {
                "1": {"label": "消耗1枚电台电池尝试呼叫（获得装甲支援；士气+2）", "require_item": ("电台电池", 1), "cost_item": ("电台电池", 1), "tank_support": 1, "delta": {"morale": 2}},
                "2": {"label": "拆走还能用的电台电池（获得电台电池x1；士气+1）", "loot": ("电台电池", 1), "delta": {"morale": 1}},
            },
        },
        {
            "type": "choice",
            "id": "EV_CHOICE_GOOD_104",
            "text": "走廊尽头压着几块装甲板。你可以把它们带走增强防护，但搬运会拖慢节奏。",
            "options": {
                "1": {"label": "带走一块装甲板（获得装甲板x1；燃油-1）", "loot": ("装甲板", 1), "delta": {"fuel": -1, "morale": 1}},
                "2": {"label": "只做标记，下次再说（士气+1）", "delta": {"morale": 1}},
            },
        },
        {
            "type": "choice",
            "id": "EV_CHOICE_GOOD_105",
            "text": "一名平民把你拉到墙角，小声说他知道一条相对安全的路：但他想要一点口粮作为交换。",
            "options": {
                "1": {"label": "给出1份口粮换路线（燃油+3；士气+3；观察+1）", "require_item": ("口粮", 1), "cost_item": ("口粮", 1), "delta": {"fuel": 3, "morale": 3}, "buff": ("观察", 1)},
                "2": {"label": "不交易，只听一部分（燃油+1；士气+1）", "delta": {"fuel": 1, "morale": 1}},
            },
        },
        {
            "type": "choice",
            "id": "EV_CHOICE_GOOD_106",
            "text": "你们在煤棚里发现一只纯燃料桶。它很沉：要么花时间搬走，要么就地抽取部分油料。",
            "options": {
                "1": {"label": "花1行动点搬走（获得纯燃料桶x1；士气+1）", "require_ap": 1, "cost_ap": 1, "loot": ("纯燃料桶", 1), "delta": {"morale": 1}},
                "2": {"label": "快速抽取部分油料（燃油+12）", "delta": {"fuel": 12}},
            },
        },
        {
            "type": "choice",
            "id": "EV_CHOICE_GOOD_107",
            "text": "一处临时医务点正在撤离。你们既可以带走一部分物资自保，也可以把手里的药品留下换取一点‘人心’。",
            "options": {
                "1": {"label": "带走能拿的物资（获得医疗包x1与急救包x1；士气+1）", "loot": ("医疗包", 1), "delta": {"morale": 1}},
                "2": {"label": "留下1份药品（需要药品x1；士气+4；胜利点+1）", "require_item": ("药品", 1), "cost_item": ("药品", 1), "delta": {"morale": 4, "vp": 1}},
            },
        },
        {
            "type": "choice",
            "id": "EV_CHOICE_GOOD_108",
            "text": "你们找到一杯还温热的咖啡（不知道是谁的）。喝下去会更清醒，但也可能是个陷阱。",
            "options": {
                "1": {"label": "把咖啡收好（获得咖啡x1）", "loot": ("咖啡", 1), "delta": {"morale": 1}},
                "2": {"label": "不碰陌生东西（更稳）", "delta": {"morale": 0}},
            },
        },

        # 30：额外正向小收益
        {"type": "simple", "text": "你们在碎砖与灰尘里摸到一根金条。它并不能解决眼前的问题，但能让未来多一点选择。", "delta": {"gold": 1, "morale": 1}},
    ]

    # --- 地区专属事件：按 location_key 注入（每个地区至少 1~2 条） ---
    # 放在事件链之前/之后都可；这里放在事件链之后之前影响不大。
    try:
        events.extend(_build_location_unique_events(s))
    except Exception:
        # 专属事件不应导致随机事件系统崩溃
        pass

    # --- 可重复事件链：地铁/地下通道的“隧道线索” ---
    # 设计目标：不引入新系统，仅用 story_vars 做阶段；完成后短冷却，可再次触发。
    terrain0 = str(MAP_META.get(s.location_key, {}).get("terrain", ""))
    if terrain0 in ("地铁", "地下通道"):
        stage = int(s.story_vars.get("tunnel_chain_stage", 0) or 0)
        last = int(s.story_vars.get("tunnel_chain_last", -999) or -999)
        cooldown = 4
        can_restart = (s.round_number - last) >= cooldown

        # 若阶段异常，自动归零
        if stage < 0 or stage > 2:
            stage = 0
            s.story_vars["tunnel_chain_stage"] = 0

        # 只有在可重启窗口才投放第1段
        if stage == 0 and can_restart:
            events.append(
                {
                    "type": "choice",
                    "id": "EV_CHAIN_TUNNEL_01",
                    "text": "你们在站台尽头发现一扇半掩的紧急门。里面通向更深的隧道：更安静，也更不确定。",
                    "options": {
                        "1": {
                            "label": "下去侦察（耗1行动点；获得地图碎片；士气+1）",
                            "require_ap": 1,
                            "cost_ap": 1,
                            "delta": {"morale": 1},
                            "loot": ("地图碎片", 1),
                            "var": {"tunnel_chain_stage": 1},
                            "set_var_round": "tunnel_chain_last",
                        },
                        "2": {
                            "label": "标记位置撤离（燃油+1；士气-1）",
                            "delta": {"fuel": 1, "morale": -1},
                            "var": {"tunnel_chain_stage": 0},
                            "set_var_round": "tunnel_chain_last",
                        },
                    },
                }
            )
        elif stage == 1:
            events.append(
                {
                    "type": "choice",
                    "id": "EV_CHAIN_TUNNEL_02",
                    "text": "隧道里岔路增多，回声让距离难以判断。你必须选择是追逐‘补给’的方向，还是优先找一条更快的上行通道。",
                    "options": {
                        "1": {
                            "label": "追补给方向（耗1行动点；可能更危险）",
                            "require_ap": 1,
                            "cost_ap": 1,
                            "delta": {"morale": 1},
                            "var": {"tunnel_chain_stage": 2},
                            "extra_encounter": 1,
                            "set_var_round": "tunnel_chain_last",
                        },
                        "2": {
                            "label": "优先找上行通道（耗1行动点；获得口粮；士气+2）",
                            "require_ap": 1,
                            "cost_ap": 1,
                            "delta": {"morale": 2},
                            "loot": ("口粮", 1),
                            "var": {"tunnel_chain_stage": 0},
                            "set_var_round": "tunnel_chain_last",
                        },
                    },
                }
            )
        elif stage == 2:
            events.append(
                {
                    "type": "choice",
                    "id": "EV_CHAIN_TUNNEL_03",
                    "text": "你们在隧道侧室里发现一处被木箱掩住的补给点。看起来有人来不及带走：也许现在属于你们。",
                    "options": {
                        "1": {
                            "label": "快速搬运并撤离（获得润滑油+1与弹药箱+1；士气+1）",
                            "delta": {"morale": 1},
                            "loot": ("润滑油", 1),
                            "var": {"tunnel_chain_stage": 0},
                            "set_var_round": "tunnel_chain_last",
                        },
                        "2": {
                            "label": "多搜一会儿（获得工具箱+1；但更可能引来交火）",
                            "delta": {"morale": 1},
                            "loot": ("工具箱", 1),
                            "extra_encounter": 1,
                            "var": {"tunnel_chain_stage": 0},
                            "set_var_round": "tunnel_chain_last",
                        },
                    },
                }
            )

    # --- 可重复事件链：电台的“广播室线索” ---
    # 目标：在电台地形强化“情报/呼叫/误导”的主题；完成后短冷却可再触发。
    if terrain0 == "电台":
        stage = int(s.story_vars.get("radio_chain_stage", 0) or 0)
        last = int(s.story_vars.get("radio_chain_last", -999) or -999)
        cooldown = 4
        can_restart = (s.round_number - last) >= cooldown

        if stage < 0 or stage > 2:
            stage = 0
            s.story_vars["radio_chain_stage"] = 0

        if stage == 0 and can_restart:
            events.append(
                {
                    "type": "choice",
                    "id": "EV_CHAIN_RADIO_01",
                    "text": "你们从电台楼外墙进入半塌的走廊。门牌写着‘播音间’，里面还残留着纸屑与频率表。",
                    "options": {
                        "1": {
                            "label": "花1行动点记录频率（获得侦察优势）",
                            "require_ap": 1,
                            "cost_ap": 1,
                            "delta": {"morale": 1},
                            "buff": ("观察", 1),
                            "var": {"radio_chain_stage": 1},
                            "set_var_round": "radio_chain_last",
                        },
                        "2": {
                            "label": "只拿走还能用的电池（更实用）",
                            "loot": ("电台电池", 1),
                            "delta": {"morale": 1},
                            "var": {"radio_chain_stage": 0},
                            "set_var_round": "radio_chain_last",
                        },
                    },
                }
            )
        elif stage == 1:
            events.append(
                {
                    "type": "choice",
                    "id": "EV_CHAIN_RADIO_02",
                    "text": "通信员低声说：我们可以发一段‘假指令’，诱导对方误判；也可以用这点时间向驻军求援。",
                    "options": {
                        "1": {
                            "label": "发出假指令（更冒险，但也许能换来喘息）",
                            "delta": {"morale": -1, "vp": 1},
                            "buff": ("侦察", 1),
                            "extra_encounter": 1,
                            "var": {"radio_chain_stage": 2},
                            "set_var_round": "radio_chain_last",
                        },
                        "2": {
                            "label": "尝试求援（需要1枚电台电池）",
                            "require_item": ("电台电池", 1),
                            "cost_item": ("电台电池", 1),
                            "delta": {"morale": 2},
                            "buff": ("求援", 1),
                            "var": {"radio_chain_stage": 2},
                            "set_var_round": "radio_chain_last",
                        },
                    },
                }
            )
        elif stage == 2:
            events.append(
                {
                    "type": "choice",
                    "id": "EV_CHAIN_RADIO_03",
                    "text": "电台地下室有一台半坏的发电机。你可以把它拆走当备件，也可以尝试短时间点亮电台，换取一次更明确的支援回应。",
                    "options": {
                        "1": {
                            "label": "拆走发电机零件（获得备件与润滑油）",
                            "loot": ("备件", 1),
                            "delta": {"morale": 1},
                            "var": {"radio_chain_stage": 0},
                            "set_var_round": "radio_chain_last",
                        },
                        "2": {
                            "label": "短时间点亮电台（耗1行动点；可能获得装甲支援）",
                            "require_ap": 1,
                            "cost_ap": 1,
                            "tank_support": 1,
                            "delta": {"morale": 3, "vp": 1},
                            "var": {"radio_chain_stage": 0},
                            "set_var_round": "radio_chain_last",
                        },
                    },
                }
            )

    # --- 可重复事件链：医院的“分诊与地下室” ---
    # 目标：让医院地形更像“资源/伦理/压力”的交叉点；用道具驱动而不新增菜单。
    if terrain0 == "医院":
        stage = int(s.story_vars.get("hospital_chain_stage", 0) or 0)
        last = int(s.story_vars.get("hospital_chain_last", -999) or -999)
        cooldown = 5
        can_restart = (s.round_number - last) >= cooldown

        if stage < 0 or stage > 2:
            stage = 0
            s.story_vars["hospital_chain_stage"] = 0

        if stage == 0 and can_restart:
            events.append(
                {
                    "type": "choice",
                    "id": "EV_CHAIN_HOSPITAL_01",
                    "text": "医院废楼里有人做着简陋分诊。一个护士看见你们的制服，先是警惕，随后只剩疲惫：‘有药吗？或者帮我搬一下人。’",
                    "options": {
                        "1": {
                            "label": "拿出1份药品（士气↑，并推进‘援助地下医院’）",
                            "require_item": ("药品", 1),
                            "cost_item": ("药品", 1),
                            "delta": {"morale": 3, "vp": 1},
                            "quest": ("Q_hospital", 1),
                            "var": {"hospital_chain_stage": 1},
                            "set_var_round": "hospital_chain_last",
                        },
                        "2": {
                            "label": "花1行动点协助搬运（更慢，但稳）",
                            "require_ap": 1,
                            "cost_ap": 1,
                            "delta": {"morale": 2},
                            "quest": ("Q_hospital", 1),
                            "var": {"hospital_chain_stage": 1},
                            "set_var_round": "hospital_chain_last",
                        },
                        "3": {
                            "label": "不久留（你们也快撑不住了）",
                            "delta": {"morale": -2},
                            "var": {"hospital_chain_stage": 0},
                            "set_var_round": "hospital_chain_last",
                        },
                    },
                }
            )
        elif stage == 1:
            events.append(
                {
                    "type": "choice",
                    "id": "EV_CHAIN_HOSPITAL_02",
                    "text": "你们被带到一间临时手术室。医生说：‘我们缺的不只是药，还有能让人继续工作的东西。’",
                    "options": {
                        "1": {
                            "label": "捐出1个医疗包（士气↑，并推进任务）",
                            "require_item": ("医疗包", 1),
                            "cost_item": ("医疗包", 1),
                            "delta": {"morale": 4, "vp": 1},
                            "quest": ("Q_hospital", 1),
                            "var": {"hospital_chain_stage": 2},
                            "set_var_round": "hospital_chain_last",
                        },
                        "2": {
                            "label": "递上一杯咖啡（士气↑，但你们自己的库存会少）",
                            "require_item": ("咖啡", 1),
                            "cost_item": ("咖啡", 1),
                            "delta": {"morale": 3},
                            "var": {"hospital_chain_stage": 2},
                            "set_var_round": "hospital_chain_last",
                        },
                        "3": {
                            "label": "说抱歉，赶路要紧",
                            "delta": {"morale": -1},
                            "var": {"hospital_chain_stage": 0},
                            "set_var_round": "hospital_chain_last",
                        },
                    },
                }
            )
        elif stage == 2:
            events.append(
                {
                    "type": "choice",
                    "id": "EV_CHAIN_HOSPITAL_03",
                    "text": "你们在地下室看到一排没来得及带走的箱子。带走它们能让你们活得更久，但也意味着有人会少一分机会。",
                    "options": {
                        "1": {
                            "label": "带走一部分（获得药品与口粮；士气↓）",
                            "loot": ("药品", 1),
                            "delta": {"morale": -2},
                            "var": {"hospital_chain_stage": 0},
                            "set_var_round": "hospital_chain_last",
                        },
                        "2": {
                            "label": "把箱子原封不动留下（士气↑，并推进任务）",
                            "delta": {"morale": 3, "vp": 1},
                            "quest": ("Q_hospital", 1),
                            "var": {"hospital_chain_stage": 0},
                            "set_var_round": "hospital_chain_last",
                        },
                    },
                }
            )

    # 自检：可强制指定要抽到的事件（用于覆盖事件链/选项解析等，不影响正常运行）
    forced_event_id = _selftest_pop("force_event_id") if SELFTEST else None
    forced_event: Optional[Dict[str, object]] = None
    if isinstance(forced_event_id, str) and forced_event_id:
        for ev in events:
            if str(ev.get("id", "")) == forced_event_id:
                forced_event = ev
                break
        if forced_event is None:
            print(f"[自检] 未找到强制事件：{forced_event_id}，改为随机抽取。")

    # 按区域地形偏好对事件进行两阶段抽取：先选事件类型，再从对应事件池中抽取具体事件
    terrain = MAP_META.get(s.location_key, {}).get("terrain", "")
    default_weights = {"supply": 1.0, "assist": 1.0, "simple": 1.0, "medical": 1.0, "map": 1.0, "intel": 1.0, "mechanical": 1.0, "morale": 1.0, "choice": 1.0}
    terrain_weights = TERRAIN_EVENT_WEIGHTS.get(terrain, default_weights)

    # 搜索加成：显著提高“补给/医疗/机械/情报/路口抉择”类事件的出现率
    if s.buffs.pop("搜索加成", 0) > 0:
        try:
            tw = dict(terrain_weights) if isinstance(terrain_weights, dict) else dict(default_weights)
        except Exception:
            tw = dict(default_weights)
        for k in list(tw.keys()):
            base_w = float(tw.get(k, 1.0))
            if k in ("supply", "medical", "map", "intel", "mechanical", "choice"):
                tw[k] = base_w * 3.0
            elif k == "simple":
                tw[k] = base_w * 0.45
            else:
                tw[k] = base_w * 1.15
        terrain_weights = tw

    def _classify_event(ev: Dict[str, object]) -> str:
        t = str(ev.get("type", "simple"))
        if t in ("supply", "assist", "choice"):
            return t
        # 根据掉落物推断更具体的类别
        item = ev.get("item")
        if isinstance(item, tuple) and len(item) >= 1:
            name = item[0]
            if name in ("急救包", "医疗包", "药品"):
                return "medical"
            if name == "地图碎片":
                return "map"
            if name in ("燃油桶", "弹药箱", "备件", "电台电池", "烟幕弹"):
                return "supply"
        # 根据 delta 内容判断
        delta = ev.get("delta")
        if isinstance(delta, dict):
            if int(delta.get("damage", 0)) < 0:
                return "mechanical"
            if int(delta.get("morale", 0)) > 0:
                return "morale"
        return "simple"

    # 构建事件池（按类别分组），便于按地形权重先选类别再抽取
    from collections import defaultdict

    EVENT_POOL: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    for ev in events:
        cat = _classify_event(ev)
        EVENT_POOL[cat].append(ev)

    # 确保所有预期类别存在（便于权重映射）
    for k in default_weights.keys():
        EVENT_POOL.setdefault(k, [])

    # 先按类别权重选类别
    cats = [c for c in EVENT_POOL.keys()]
    cat_weights = [float(terrain_weights.get(c, default_weights.get(c, 1.0))) for c in cats]

    if forced_event is not None:
        e = forced_event
    else:
        if not cats:
            # 万一没有事件定义，回退到基础文本
            e = {"type": "simple", "text": "你们在废墟里遇到了一件难以描述的小事。"}
        else:
            if all(w == 0.0 for w in cat_weights):
                try:
                    chosen_cat = s.rng.choice(cats)
                except Exception:
                    chosen_cat = cats[0]
            else:
                try:
                    chosen_cat = s.rng.choices(cats, weights=cat_weights, k=1)[0]
                except Exception:
                    chosen_cat = s.rng.choice(cats)

            pool = EVENT_POOL.get(chosen_cat, [])
            if pool:
                try:
                    # 若当前类别里包含“地区专属事件”，给它们一个额外权重，避免被大池稀释
                    region_pool = [x for x in pool if isinstance(x, dict) and bool(x.get("region"))]
                    if region_pool and s.rng.random() < 0.38:
                        e = s.rng.choice(region_pool)
                    else:
                        e = s.rng.choice(pool)
                except Exception:
                    e = s.rng.choice(events)
            else:
                # 若所选类别为空，退回至全部事件中随机
                e = s.rng.choice(events)

    print(str(e.get("text", "你们在废墟里遇到了一件难以描述的小事。")))

    etype = str(e.get("type", "simple"))
    if etype == "supply":
        event_reward_supply(s)
    elif etype == "assist":
        run_assist_evacuation(ins, s)
    elif etype == "choice":
        eid = str(e.get("id", f"EV_CHOICE_{s.round_number}"))
        text = str(e.get("text", "你们在废墟里遇到一处需要决定的路口。"))
        opts = e.get("options")
        if not isinstance(opts, dict):
            # 回退
            print(text)
            apply_delta(morale=1)
        else:
            print(text)
            menu: Dict[str, str] = {}
            visible_opts: Dict[str, Dict[str, object]] = {}
            for k, v in opts.items():
                if not isinstance(v, dict):
                    continue
                req = int(v.get("require_ap", 0) or 0)
                if req > 0 and s.action_points < req:
                    continue

                # 可选：物品门槛（用于更丰富的剧情取舍）
                req_item = v.get("require_item")
                if isinstance(req_item, tuple) and len(req_item) == 2:
                    iname, icnt = req_item
                    if isinstance(iname, str):
                        need = int(icnt) if isinstance(icnt, int) else 1
                        if int(s.inventory.get(iname, 0)) < need:
                            continue
                kk = str(k)
                menu[kk] = str(v.get("label", "(未命名选项)"))
                visible_opts[kk] = v
            if not visible_opts:
                print("你想做点什么，但时间与精力都不允许。")
                apply_delta(morale=-1)
                _print_event_result()
                s.clamp()
                return

            # 事件选择：不可“退出”，必须做出选择；默认值需保证落在可见选项内
            if ins.default_when_empty and ("2" in menu):
                default = "2"
            else:
                default = sorted(menu.keys())[0]

            c = choose(ins, "选择：", menu, default=default)
            chosen = visible_opts.get(c)
            if isinstance(chosen, dict):
                # 行动点消耗（用于“花剩余行动点”类事件）
                cost = chosen.get("cost_ap")
                if cost is not None:
                    if cost == "all":
                        spend = max(0, int(s.action_points))
                    else:
                        try:
                            spend = int(cost)
                        except Exception:
                            spend = 0
                    if spend > 0:
                        if s.action_points < spend:
                            print("行动点不足，无法执行该选择。")
                            return
                        s.action_points -= spend
                        ap_spent_total += spend

                # 可选：消耗物品（用于更丰富的剧情取舍）
                cost_item = chosen.get("cost_item")
                if isinstance(cost_item, tuple) and len(cost_item) == 2:
                    iname, icnt = cost_item
                    if isinstance(iname, str):
                        need = int(icnt) if isinstance(icnt, int) else 1
                        if not spend_item(s, iname, need):
                            print("物品不足，无法执行该选择。")
                            return
                        items_spent[iname] = int(items_spent.get(iname, 0) or 0) + need

                d = chosen.get("delta")
                if isinstance(d, dict):
                    apply_delta(
                        fuel=int(d.get("fuel", 0)),
                        ammo=int(d.get("ammo", 0)),
                        ap_shells=int(d.get("ap_shells", 0)),
                        he_shells=int(d.get("he_shells", 0)),
                        gold=int(d.get("gold", 0)),
                        passes=int(d.get("passes", 0) or d.get("pass", 0) or 0),
                        morale=int(d.get("morale", 0)),
                        damage=int(d.get("damage", 0)),
                        vp=int(d.get("vp", 0)),
                        collapse=int(d.get("collapse", 0)),
                    )
                loot = chosen.get("loot")
                if isinstance(loot, tuple) and len(loot) == 2:
                    lname, lqty = loot
                    if isinstance(lname, str):
                        grant(lname, int(lqty) if isinstance(lqty, int) else 1)

                # 可选：推进任务（choice 也允许）
                quest = chosen.get("quest")
                if isinstance(quest, tuple) and len(quest) == 2:
                    qid, amount = quest
                    if isinstance(qid, str):
                        _quest_progress(s, qid, int(amount) if isinstance(amount, int) else 1)
                buff = chosen.get("buff")
                if isinstance(buff, tuple) and len(buff) == 2:
                    bname, bval = buff
                    if isinstance(bname, str):
                        s.buffs[bname] = max(int(bval) if isinstance(bval, int) else 1, s.buffs.get(bname, 0))
                flag = chosen.get("flag")
                if isinstance(flag, tuple) and len(flag) == 2:
                    fk, fv = flag
                    if isinstance(fk, str):
                        s.story_flags[fk] = bool(fv)

                # 新：压力缓解
                if "stress_relief" in chosen:
                    apply_stress_relief(s, chosen.get("stress_relief"))

                # 新：写入剧情变量（用于事件链阶段/计数等）
                var = chosen.get("var")
                if isinstance(var, tuple) and len(var) == 2:
                    vk, vv = var
                    if isinstance(vk, str):
                        try:
                            s.story_vars[vk] = int(vv)
                        except Exception:
                            pass
                elif isinstance(var, dict):
                    for vk, vv in var.items():
                        if not isinstance(vk, str):
                            continue
                        try:
                            s.story_vars[vk] = int(vv)
                        except Exception:
                            continue

                set_var_round = chosen.get("set_var_round")
                if isinstance(set_var_round, str) and set_var_round:
                    s.story_vars[set_var_round] = int(s.round_number)
                elif isinstance(set_var_round, list):
                    for k in set_var_round:
                        if isinstance(k, str) and k:
                            s.story_vars[k] = int(s.round_number)
                if int(chosen.get("extra_encounter", 0) or 0) > 0:
                    s.buffs["额外遭遇"] = max(1, s.buffs.get("额外遭遇", 0))

                # 新：坦克支援
                if int(chosen.get("tank_support", 0) or 0) > 0:
                    if not grant_friendly_tank_support(s):
                        # 若友军坦克已达上限，改为给一张通行证作补偿
                        s.passes += 1
                        print("\n支援无法编入：队列已满（改为获得通行证x1）。")

                # 影响辖区（好感/沦陷）
                sec = s.sectors.get(s.location_key)
                sec_delta = chosen.get("sector")
                if sec is not None and isinstance(sec_delta, dict):
                    sec.favor += int(sec_delta.get("favor", 0) or 0)
                    sec.fall += int(sec_delta.get("fall", 0) or 0)
                    sec.clamp()

                # 新：加入驻军单位（用于溃军/临时组织）
                add_spec = chosen.get("garrison_add")
                if sec is not None and add_spec is not None:
                    terrain0 = MAP_META.get(s.location_key, {}).get("terrain")
                    unit_type: Optional[str] = None
                    if isinstance(add_spec, str):
                        unit_type = add_spec
                    elif isinstance(add_spec, list) and add_spec:
                        picks = [x for x in add_spec if isinstance(x, str)]
                        if picks:
                            unit_type = s.rng.choice(picks)
                    elif isinstance(add_spec, dict):
                        t0 = add_spec.get("type")
                        if isinstance(t0, str):
                            unit_type = t0
                    if unit_type:
                        sec.garrison_units.append(_make_garrison_unit(s.rng, terrain0, force_type=unit_type))
                        sec.clamp()
                        print(f"\n🪖 新增支援：{unit_type} 加入当地驻军。")
            # 标记该选择事件在本次运行已出现
            s.shown_events.add(eid)
    else:
        delta = e.get("delta")
        if isinstance(delta, dict):
            apply_delta(
                fuel=int(delta.get("fuel", 0)),
                ammo=int(delta.get("ammo", 0)),
                ap_shells=int(delta.get("ap_shells", 0)),
                he_shells=int(delta.get("he_shells", 0)),
                gold=int(delta.get("gold", 0)),
                passes=int(delta.get("passes", 0) or delta.get("pass", 0) or 0),
                morale=int(delta.get("morale", 0)),
                damage=int(delta.get("damage", 0)),
                vp=int(delta.get("vp", 0)),
                collapse=int(delta.get("collapse", 0)),
            )

        item = e.get("item")
        if isinstance(item, tuple) and len(item) == 2:
            name, qty = item
            if isinstance(name, str):
                grant(name, int(qty) if isinstance(qty, int) else 1)

        quest = e.get("quest")
        if isinstance(quest, tuple) and len(quest) == 2:
            qid, amount = quest
            if isinstance(qid, str):
                _quest_progress(s, qid, int(amount) if isinstance(amount, int) else 1)

        buff = e.get("buff")
        if isinstance(buff, tuple) and len(buff) == 2:
            bname, bval = buff
            if isinstance(bname, str):
                s.buffs[bname] = max(int(bval) if isinstance(bval, int) else 1, s.buffs.get(bname, 0))

        # 新：压力缓解
        if "stress_relief" in e:
            apply_stress_relief(s, e.get("stress_relief"))

    _print_event_result()

    # 新：支持 once 事件（不限于 choice）：一旦结算完成就标记，避免反复刷同一条专属事件。
    try:
        if isinstance(e, dict) and bool(e.get("once")):
            eid0 = e.get("id")
            if isinstance(eid0, str) and eid0:
                s.shown_events.add(eid0)
    except Exception:
        pass
    s.clamp()


def event_reward_supply(s: GameState) -> None:
    # 轻量掉落
    # 支持外部标记以临时提升燃油桶掉落权重（用于战后额外搜刮）
    favor_fuel = bool(getattr(s, "_favor_fuel_for_post_scavenge", False))
    # 清除临时标记（只影响本次调用）
    try:
        if favor_fuel:
            s._favor_fuel_for_post_scavenge = False
    except Exception:
        pass
    roll = s.rng.random()
    got_name: Optional[str] = None
    got_qty = 0

    # 严格历史/可玩性调整：降低直接获得金条/通行证的概率，避免刷币
    special = s.rng.random()
    # 极低概率直接获得金条/通行证
    if special < 0.01:
        s.gold_bars += 1
        print("你在瓦砾缝隙里摸到一根金条。")
        print("【结果】获得：金条 x1")
        s.clamp()
        return
    if special < 0.03:
        s.passes += 1
        print("你翻到一张还能用的通行证。")
        print("【结果】获得：通行证 x1")
        s.clamp()
        return

    # 燃油桶基线概率为 0.2；若被标记为 favor_fuel，则适度提升（例如 +0.15）
    fuel_threshold = 0.2 + (0.15 if favor_fuel else 0.0)
    fuel_threshold = min(0.6, fuel_threshold)
    if roll < fuel_threshold:
        got_name, got_qty = "燃油桶", 1
        add_item(s, got_name, got_qty)
        print("你找到一只燃油桶。")
        _quest_progress(s, "Q2", 1)
    elif roll < 0.4:
        got_name, got_qty = "弹药箱", 1
        add_item(s, got_name, got_qty)
        print("你找到一个弹药箱。")
        _quest_progress(s, "Q2", 1)
    elif roll < 0.45:
        got_name, got_qty = "炮弹箱", 1
        add_item(s, got_name, got_qty)
        print("你找到一只标着‘炮弹’的箱子：数量不多，但很关键。")
        _quest_progress(s, "Q2", 1)
    elif roll < 0.55:
        got_name, got_qty = "备件", 1
        add_item(s, got_name, got_qty)
        print("你拆到一些还能用的备件。")
        _quest_progress(s, "Q2", 1)
    elif roll < 0.60:
        got_name, got_qty = "装甲板", 1
        add_item(s, got_name, got_qty)
        print("你找到几块还能用的装甲板：也许能焊到车体薄弱处。")
        _quest_progress(s, "Q2", 1)
    elif roll < 0.65:
        got_name, got_qty = "电台电池", 1
        add_item(s, got_name, got_qty)
        print("你从废弃电台里取出一枚电池。")
        _quest_progress(s, "Q4", 1)
    elif roll < 0.75:
        got_name, got_qty = "急救包", 1
        add_item(s, got_name, got_qty)
        print("你找到一个急救包。")
    elif roll < 0.78:
        got_name, got_qty = "药品", 1
        add_item(s, got_name, got_qty)
        print("你找到一些药品：足够做一次伤情处置。")
    elif roll < 0.8:
        got_name, got_qty = "烟幕弹", 1
        add_item(s, got_name, got_qty)
        print("你捡到一枚烟幕弹。")
    elif roll < 0.85:
        got_name, got_qty = "医疗包", 1
        add_item(s, got_name, got_qty)
        print("你发现一个医疗包，里面有绷带和药品。")
    elif roll < 0.9:
        got_name, got_qty = "工具箱", 1
        add_item(s, got_name, got_qty)
        print("你找到一个工具箱，里面有修理工具。")
    elif roll < 0.95:
        got_name, got_qty = "侦察设备", 1
        add_item(s, got_name, got_qty)
        print("你捡到一套侦察设备。")
        _quest_progress(s, "Q4", 1)
    else:
        got_name, got_qty = "地图碎片", 1
        add_item(s, got_name, got_qty)
        print("你捡到一张地图碎片，上面标着可能的‘缺口’。")
        _quest_progress(s, "Q3", 1)

    if got_name and got_qty > 0:
        print(f"【结果】获得：{got_name} x{got_qty}")


def post_encounter_reward_event(s: GameState, *, boss: bool, outcome: str) -> None:
    """遭遇战结束后的奖励性事件。

    目标：给玩家明确的“战后收益”体感；不引入负面结果；强度随 boss/胜利提升。
    - cleared：必触发
    - withdraw：有概率触发（较小收益）
    """
    # 设计变更：不再存在“BOSS战”特化，统一按普通遭遇处理。
    boss = False

    if outcome not in ("cleared", "withdraw"):
        return

    flags = dict(getattr(s, "story_flags", {}) or {})

    if outcome == "withdraw":
        # 撤离也可能有收获，但不保证
        if s.rng.random() >= 0.45:
            return

    print("\n【战后】硝烟稍散，你们抓住片刻清理现场。")

    tiers = 1

    for _ in range(tiers):
        # 章节/剧情：让某些分支带来“战后收益倾向”（不引入负面结果）
        if bool(flags.get("saved_orphans", False)) and s.rng.random() < 0.08:
            gain = 5
            s.morale += gain
            try:
                relieve_crew_stress(s, amount=8, mode="all", include_commander=True)
            except Exception:
                pass
            print(f"你记起自己为何还在这里：士气+{gain}。")
            continue
        if bool(flags.get("black_market", False)) and s.rng.random() < 0.03:
            s.gold_bars += 1
            s.morale += 1
            print("你们靠着灰色渠道换来一点筹码：获得1根金条。")
            continue
        if bool(flags.get("battery_shared", False)) and s.rng.random() < 0.03:
            add_item(s, "电台电池", 1)
            print("你们在清理时留下一枚电台电池：联络还能继续。")
            _quest_progress(s, "Q4", 1)
            continue

        roll = s.rng.random()
        # 降低直接触发物资回收的基线概率，转而更多给出士气/修复类回报
        if roll < 0.52:
            # 物资回收：直接给一份“补给事件”掉落
            event_reward_supply(s)
        elif roll < 0.80:
            # 稳住节奏：士气与压力缓解
            gain = 4
            s.morale += gain
            try:
                relieve_crew_stress(s, amount=6, mode="all", include_commander=True)
            except Exception:
                pass
            print(f"你们确认彼此还在，紧绷稍退：士气+{gain}。")
        elif roll < 0.94:
            # 结构与故障：轻微修复（不消耗物品）
            fix = 6
            s.damage = max(0, int(s.damage) - fix)
            # 顺带压制常见故障 1 回合
            for k in ["gun_breech", "turret_jam", "engine_damage", "radio_damage", "optics_broken", "mg_jam"]:
                if int(s.debuffs.get(k, 0) or 0) > 0:
                    s.debuffs[k] = max(0, int(s.debuffs.get(k, 0)) - 1)
                    if int(s.debuffs.get(k, 0)) <= 0:
                        s.debuffs.pop(k, None)
            print(f"你们趁机检查车体与机构：损伤-{fix}。")
        else:
            # 罕见：一点“关键资源”
            s.gold_bars += 1
            s.morale += 1
            print("你们在混乱里摸到一根金条：也许能换到关键补给。")

    s.clamp()


def post_encounter_proficiency_gain(s: GameState, *, boss: bool, outcome: str) -> None:
    """遭遇战后熟练度成长（不增加新菜单/页面）。"""
    if not getattr(s, "crew", None):
        return
    # 设计变更：不再存在“BOSS战”特化，统一按普通遭遇处理。
    boss = False
    # outcome 可能包括：cleared/withdraw/escape 等
    gain_all = 0
    if outcome == "cleared":
        gain_all = 2 if boss else 1
    elif outcome in ("withdraw", "escape"):
        gain_all = 1
    else:
        gain_all = 0

    if gain_all <= 0:
        return

    for m in s.crew:
        if not getattr(m, "alive", False):
            continue
        before = _member_proficiency(m)
        m.proficiency = before + gain_all
        # 关键岗位在胜利后额外加一点，强化“战斗可以加强”的体感
        if outcome == "cleared" and m.role in ("炮手", "装填手", "驾驶员"):
            m.proficiency = int(getattr(m, "proficiency", before) or before) + 1
        m.clamp()

    print(f"【熟练度】战斗经验提升：全员+{gain_all}。")
    s.clamp()


def event_sell_resources(ins: "InputStream", s: GameState) -> None:
    """允许玩家出售库存中的若干资源以换取金条（交互式）。

    使用简单的定价表，单位为金条。若库存内没有可售物品则直接返回。
    """
    # 使用全局价格表与地区倍率，通过 get_item_price 计算单价

    inv = dict(getattr(s, "inventory", {}) or {})
    sellable = {k: v for k, v in inv.items() if k in BASE_SELL_PRICES and int(v) > 0}
    if not sellable:
        print("当前没有可出售的资源。")
        return

    # 构建选项字典
    keys = list(sellable.keys())
    while True:
        options = {}
        for i, name in enumerate(keys, start=1):
            qty = int(sellable.get(name, 0) or 0)
            price = get_item_price(s, name)
            options[str(i)] = f"{name} x{qty}（单价：{price} 金条）"
        options["0"] = "取消/返回"

        choice = choose(ins, "选择要出售的物品：", options, default="0")
        if choice == "0":
            return
        try:
            idx = int(choice) - 1
            if idx < 0 or idx >= len(keys):
                print("选择无效。")
                continue
            item = keys[idx]
            have = int(inv.get(item, 0) or 0)
            max_q = have
            qraw = get_valid_input(ins, f"出售数量（1-{max_q}，回车取消）：", valid=lambda x: x.isdigit() and 1 <= int(x) <= max_q, default="0")
            if qraw.strip() == "0":
                continue
            q = int(qraw)
            unit_price = get_item_price(s, item)
            total = unit_price * q
            ok = spend_item(s, item, q)
            if not ok:
                print("物品不足，出售失败。")
                # 重新同步库存并返回到顶部循环
                inv = dict(getattr(s, "inventory", {}) or {})
                sellable = {k: v for k, v in inv.items() if k in BASE_SELL_PRICES and int(v) > 0}
                if not sellable:
                    return
                keys = list(sellable.keys())
                continue
            s.gold_bars = int(getattr(s, 'gold_bars', 0) or 0) + int(total)
            print(f"出售成功：{item} x{q} → 获得 金条 x{total}")
            s.clamp()
            # 更新本地 inventory
            inv = dict(getattr(s, "inventory", {}) or {})
            sellable = {k: v for k, v in inv.items() if k in BASE_SELL_PRICES and int(v) > 0}
            if not sellable:
                print("已无可出售物资，返回。")
                return
            keys = list(sellable.keys())
        except Exception:
            print("出售操作异常，请重试。")
            inv = dict(getattr(s, "inventory", {}) or {})
            sellable = {k: v for k, v in inv.items() if k in BASE_SELL_PRICES and int(v) > 0}
            if not sellable:
                return
            keys = list(sellable.keys())


def _quest_progress(s: GameState, quest_id: str, amount: int) -> None:
    for q in s.quests:
        if q.id == quest_id:
            before = q.done
            q.add(amount)
            if q.id == "Q3" and int(amount) > 0:
                try:
                    p = int(getattr(q, "progress", 0) or 0)
                    t = int(getattr(q, "target", 0) or 0)
                except Exception:
                    p, t = 0, 0
                if t > 0:
                    if p < t:
                        extra = ""
                        if p == 1:
                            extra = "你们只确定了大致方向。"
                        elif p == 2:
                            extra = "路线开始成形，但仍有盲区。"
                        elif p == 3:
                            extra = "你们能排除几条死路。"
                        elif p == 4:
                            extra = "缺口位置几乎清晰，只差最后一块。"
                        print(f"【缺口线索】地图碎片：{p}/{t}。{extra}")
                    else:
                        print(f"【缺口线索】地图碎片：{t}/{t}（路线已拼合）。")
                        print("【解锁】移动菜单出现：郊外缺口。")
            if not before and q.done:
                # 奖励稍后统一发放
                pass
            return


def resolve_encounter(
    ins: InputStream,
    s: GameState,
    *,
    boss: bool = False,
    garrison_allies: Optional[List["GarrisonUnit"]] = None,
    encounter_mode: str = "normal",
    ignore_battle_cap: bool = False,
    post_reward_event: bool = True,
) -> str:
    # 设计变更：去除BOSS战特化。外部即便传入 boss=True，也按普通遭遇处理。
    boss = False

    @dataclass
    class EnemyUnit:
        kind: str
        name: str
        hp: int
        armor: int
        accuracy: int
        dmg_min: int
        dmg_max: int
        elite: bool = False
        morale: int = 50
        alive: bool = True
        status: Dict[str, int] = field(default_factory=dict)

        def clamp(self) -> None:
            self.hp = max(0, min(999, int(self.hp)))
            self.armor = max(0, min(120, int(self.armor)))
            self.accuracy = max(1, min(95, int(self.accuracy)))
            self.morale = max(0, min(100, int(self.morale)))
            self.alive = self.hp > 0

    # 我方战斗内状态（不跨回合持久化）
    player_suppressed_turns = 0
    player_fortify_turns = 0
    if s.buffs.get("稳固", 0) > 0:
        # 将“周回buff”转换为战斗内短效（避免跨多场战斗叠加）
        player_fortify_turns = 2
        s.buffs.pop("稳固", None)

    def _player_damage_after_armor(raw_dmg: int, *, top_attack: bool = False) -> int:
        """按我方装甲值计算结构损伤（s.damage）增量。"""
        raw_dmg = int(raw_dmg)
        if raw_dmg <= 0:
            return 0
        armor = float(player_armor_rating(s))
        reduction = armor / (armor + float(PLAYER_ARMOR_REDUCTION_SCALE)) if armor > 0 else 0.0
        reduction = min(float(PLAYER_ARMOR_MAX_REDUCTION), max(0.0, reduction))
        if top_attack:
            # 顶攻更难被装甲完全抵消（仍受一定保护）
            reduction *= 0.55
        eff = int(math.ceil(raw_dmg * (1.0 - reduction)))
        return max(1, eff)

    def _apply_armor_damage(raw_dmg: int) -> int:
        """应用装甲损耗，受攻击影响。"""
        raw_dmg = int(raw_dmg)
        if raw_dmg <= 0:
            return 0
        # 损耗量：基于攻击强度，随机1到max(1, raw_dmg//8)
        damage_amount = s.rng.randint(1, max(1, raw_dmg // 8))
        # 优先损耗装甲板
        if s.armor_plates > 0:
            old_plates = s.armor_plates
            s.armor_plates = max(0, s.armor_plates - damage_amount)
            return old_plates - s.armor_plates
        else:
            # 然后损耗基础装甲
            old_armor = s.base_armor
            s.base_armor = max(0, s.base_armor - damage_amount)
            return old_armor - s.base_armor

    def _apply_player_structure_damage(raw_dmg: int, *, top_attack: bool = False) -> int:
        eff = _player_damage_after_armor(raw_dmg, top_attack=top_attack)

        # 友军工兵：协助加固/排险，降低我方结构损伤（s.damage）
        # 设计：固定减免 1 点；可把小额损伤抵消为 0（但不影响装甲损耗）。
        try:
            if any(getattr(u, "alive", False) and getattr(u, "unit_type", "") == "工兵" for u in allies):
                eff = max(0, int(eff) - 1)
        except Exception:
            pass

        s.damage += eff
        # 同时应用装甲损耗
        armor_lost = _apply_armor_damage(raw_dmg)
        if armor_lost > 0:
            print(f"⚠️ 装甲损耗：-{armor_lost}（当前装甲{player_armor_rating(s)}）")
        return eff

    def _enemy_templates() -> Dict[str, Dict[str, object]]:
        return {
            # 步兵/火力点
            "反坦克组": {"hp": 70, "armor": 2, "acc": 44, "dmg": (8, 14)},
            "步兵班": {"hp": 80, "armor": 1, "acc": 40, "dmg": (6, 13)},
            "突击队": {"hp": 78, "armor": 1, "acc": 46, "dmg": (8, 16)},
            "火箭筒组": {"hp": 62, "armor": 0, "acc": 44, "dmg": (10, 20)},
            "狙击组": {"hp": 50, "armor": 0, "acc": 55, "dmg": (6, 12)},
            "工兵": {"hp": 60, "armor": 0, "acc": 40, "dmg": (6, 13)},
            "重机枪点": {"hp": 85, "armor": 12, "acc": 60, "dmg": (10, 22)},
            "迫击炮组": {"hp": 75, "armor": 0, "acc": 44, "dmg": (12, 22)},
            # 火炮
            "反坦克炮": {"hp": 100, "armor": 40, "acc": 56, "dmg": (20, 40)},
            # 轻装甲支援：装甲车（会打照明弹/引导炮火）
            "装甲车": {"hp": 110, "armor": 30, "acc": 56, "dmg": (10, 18)},
            # 车辆：卡车（机枪火力平台，会卸载步兵）
            "卡车": {"hp": 85, "armor": 8, "acc": 60, "dmg": (0, 0)},
            # 兼容旧名称：将装甲侦察车/侦察装甲车映射为卡车
            "侦察装甲车": {"hp": 85, "armor": 8, "acc": 60, "dmg": (0, 0)},
            "装甲侦察车": {"hp": 85, "armor": 8, "acc": 60, "dmg": (0, 0)},
            "SU-76": {"hp": 120, "armor": 48, "acc": 54, "dmg": (12, 24)},
            "T-34": {"hp": 140, "armor": 75, "acc": 46, "dmg": (12, 22)},
            # IS-2：提高耐久但避免“打不穿”的挫败；目标是中距离下命中后通常 1~2 发解决。
            "IS-2": {"hp": 320, "armor": 120, "acc": 50, "dmg": (50, 80)},
            # 特种：喷火坦克（近距离火焰压制）
            "喷火坦克": {"hp": 140, "armor": 60, "acc": 60, "dmg": (12, 26)},
            "88炮": {"hp": 130, "armor": 60, "acc": 48, "dmg": (60, 110)},
        }

    # --- 精英敌人（“近卫”）：基础数值与随章节增长的强化系数
    ELITE_ACCURACY_BONUS_BASE = 10
    ELITE_SKILL_MULT_BASE = 1.35
    ELITE_SKILL_ADD_BASE = 0.02
    ELITE_FLEE_MULT_BASE = 1.60

    # 全局章节索引（便于在多处使用）
    chapter_idx_global = max(1, min(40, (int(getattr(s, "round_number", 1) or 1) - 1) // int(CHAPTER_INTERVAL) + 1))

    # 按章节线性提高精英强度（保守增长），并设上限以避免数值失控
    ELITE_ACCURACY_BONUS = int(min(30, round(ELITE_ACCURACY_BONUS_BASE * (1.0 + 0.02 * float(chapter_idx_global - 1)))))
    ELITE_SKILL_MULT = min(2.0, ELITE_SKILL_MULT_BASE * (1.0 + 0.012 * float(chapter_idx_global - 1)))
    ELITE_SKILL_ADD = min(0.15, ELITE_SKILL_ADD_BASE + 0.004 * float(chapter_idx_global - 1))
    ELITE_FLEE_MULT = min(3.0, ELITE_FLEE_MULT_BASE * (1.0 + 0.02 * float(chapter_idx_global - 1)))

    def _elite_spawn_chance(kind: str) -> float:
        """本次生成某个敌人种类时，被提升为“近卫”的概率。"""
        # 基础：低但稳定；随风险/回合/章节递增。
        base_risk = float(LOCATIONS.get(s.location_key, {}).get("risk", 0.0))
        danger = float(state_danger(s))
        risk = base_risk * danger

        # 当前章节：使用全局 `CHAPTER_INTERVAL` 来计算当前属于第几章。
        # 例如 CHAPTER_INTERVAL=3 则 round 1-3 -> chapter 1, 4-6 -> chapter 2
        chapter_idx = max(1, min(40, (int(getattr(s, "round_number", 1) or 1) - 1) // int(CHAPTER_INTERVAL) + 1))

        # 需求：章节越往后增长更快；最终章节几乎/完全是近卫。
        # 最终章直接锁定为 100%，避免被其他修正项稀释。
        if chapter_idx >= 40:
            return 1.0

        p = 0.18
        p += 0.05 if risk >= 0.60 else 0.0
        p += 0.05 if risk >= 0.70 else 0.0
        # 用章节索引替代硬编码回合阈值
        thr1 = int(math.ceil(10.0 / float(CHAPTER_INTERVAL)))
        thr2 = int(math.ceil(20.0 / float(CHAPTER_INTERVAL)))
        p += 0.05 if chapter_idx >= thr1 else 0.0
        p += 0.05 if chapter_idx >= thr2 else 0.0

        # 章节加成：前期温和、后期陡增（使用二次项）。
        # 第1章：+0
        # 第20章：约 +0.22
        # 第39章：约 +0.86（此时大量接近全近卫；第40章在上方直接 100%）
        x = float(chapter_idx - 1)
        p += 0.0032 * x + 0.00095 * (x * x)

        # 重火力/装甲单位更可能出现“近卫”编制
        if kind in ("IS-2", "T-34", "SU-76", "反坦克炮", "装甲车", "喷火坦克", "迫击炮组"):
            p += 0.05
        return max(0.10, min(1.0, p))

    def _skill_prob(p: float, e: "EnemyUnit") -> float:
        """统一缩放敌方特殊动作/技能触发概率。"""
        p = float(p)
        # 对精英单位按章节适度放大技能触发概率，受上限约束
        if getattr(e, "elite", False):
            # 章节索引（与生成/精英比例一致）
            try:
                chapter_idx = max(1, min(40, (int(getattr(s, "round_number", 1) or 1) - 1) // int(CHAPTER_INTERVAL) + 1))
            except Exception:
                chapter_idx = 1
            # 随章节缓慢增加精英技能暴露率，随着章节增长精英使用更多复杂动作
            chap_mult = 1.0 + min(0.25, 0.008 * float(max(0, chapter_idx - 1)))
            p = p * float(ELITE_SKILL_MULT) * chap_mult + float(ELITE_SKILL_ADD)
        # 强制上限，避免技能满屏/失衡
        return max(0.0, min(0.95, p))

    def _spawn_enemy(kind: str) -> EnemyUnit:
        # 兼容旧名称：侦察装甲车/装甲侦察车统一视作“卡车”
        if kind in ("侦察装甲车", "装甲侦察车"):
            kind = "卡车"
        t = _enemy_templates()[kind]
        suffix = s.rng.randint(11, 99)
        force_elite_all = (str(encounter_mode) in ("breakout_large", "breakout"))
        is_elite = True if force_elite_all else (s.rng.random() < _elite_spawn_chance(kind))
        name_prefix = "近卫" if is_elite else ""
        u = EnemyUnit(
            kind=kind,
            name=f"{name_prefix}{kind}-{suffix}",
            hp=int(t["hp"]),
            armor=int(t["armor"]),
            accuracy=int(t["acc"]) + (int(ELITE_ACCURACY_BONUS) if is_elite else 0),
            dmg_min=int(t["dmg"][0]),
            dmg_max=int(t["dmg"][1]),
            elite=bool(is_elite),
            morale=s.rng.randint(35, 70),
        )
        u.clamp()
        return u

    def _terrain_tag() -> str:
        return str(MAP_META.get(s.location_key, {}).get("terrain", "市区"))

    def _initial_range_tag_from_terrain(terrain: str) -> str:
        """战斗开局的交战距离：由地形决定（close/medium/long）。"""
        t = str(terrain)
        if t in ("地铁", "地下通道"):
            return "close"
        if t in ("郊区", "公路", "出口", "阵地", "堤坝"):
            return "long"
        if t in (
            "市区",
            "市区废墟",
            "政府附近",
            "巷道",
            "断街",
            "仓库带",
            "公寓区",
            "车站",
            "医院",
            "电台",
            "工业",
            "货场",
            "桥头",
        ):
            return "close"
        return "medium"

    def _engagement_range_tag(*, terrain: str, smoke: int, maneuver: int) -> str:
        """抽象交战距离：close/medium/long。用于影响命中与穿深。"""
        # 视线更差（烟幕）时更像近距离接触，但命中会被烟幕独立惩罚。
        if smoke > 0:
            return "close" if s.rng.random() < 0.65 else "medium"
        if terrain in ("市区", "市区废墟", "医院", "电台"):
            return "close" if s.rng.random() < 0.55 else "medium"
        if terrain in ("地铁", "地下通道"):
            return "close"
        # 开阔区域更容易拉开距离；机动时也更可能变为中距离交火
        if maneuver > 0 and s.rng.random() < 0.55:
            return "medium"
        return "medium" if s.rng.random() < 0.70 else "long"

    def _armor_profile_for_target(t: EnemyUnit) -> Dict[str, float]:
        """返回目标的简化装甲模型（数值仍沿用游戏内部抽象刻度）。"""
        # 以现有 armor 数值为基准，给出前/侧/后与倾角（度）。
        # 注：这里不追求毫米级历史精度，只做“相对更像真实”的结构性差异。
        kind = t.kind
        if kind in ("侦察装甲车", "装甲侦察车"):
            base = float(t.armor)
            return {"front": base, "side": base * 0.65, "rear": base * 0.55, "slope_deg": 10.0, "class": "light"}
        if kind == "SU-76":
            base = float(t.armor)
            return {"front": base, "side": base * 0.70, "rear": base * 0.50, "slope_deg": 12.0, "class": "light"}
        if kind == "装甲车":
            base = float(t.armor)
            return {"front": base, "side": base * 0.72, "rear": base * 0.60, "slope_deg": 18.0, "class": "light"}
        if kind == "T-34":
            base = float(t.armor)
            return {"front": base, "side": base * 0.78, "rear": base * 0.65, "slope_deg": 55.0, "class": "medium"}
        if kind == "喷火坦克":
            base = float(t.armor)
            return {"front": base, "side": base * 0.80, "rear": base * 0.66, "slope_deg": 45.0, "class": "medium"}
        if kind == "IS-2":
            base = float(t.armor)
            return {"front": base, "side": base * 0.80, "rear": base * 0.65, "slope_deg": 32.0, "class": "heavy"}
        if kind == "反坦克炮":
            base = float(t.armor)
            return {"front": base, "side": base * 0.60, "rear": base * 0.45, "slope_deg": 0.0, "class": "gun"}

        base = float(t.armor)
        return {"front": base, "side": base * 0.75, "rear": base * 0.60, "slope_deg": 18.0 if base >= 20 else 0.0, "class": "unknown"}

    def _roll_hit_aspect(*, maneuver: int, target_suppressed: bool) -> str:
        """命中面：front/side/rear。机动与压制更容易打到侧后。"""
        p_front, p_side, p_rear = 0.65, 0.25, 0.10
        if s.debuffs.get("turret_jam", 0) > 0:
            # 炮塔卡滞时更难抓到侧后窗口
            p_front, p_side, p_rear = 0.82, 0.14, 0.04
        if target_suppressed:
            p_front -= 0.10
            p_side += 0.07
            p_rear += 0.03
        if maneuver > 0:
            p_front -= 0.08
            p_side += 0.06
            p_rear += 0.02
        r = s.rng.random()
        if r < max(0.05, p_front):
            return "front"
        if r < max(0.05, p_front) + max(0.05, p_side):
            return "side"
        return "rear"

    def _effective_armor(*, base: float, slope_deg: float, aspect: str) -> float:
        """用简化倾角模型计算有效防护。"""
        # 侧后装甲通常倾角更小；正面更可能靠倾角产生跳弹。
        if aspect in ("side", "rear"):
            slope_deg = min(18.0, slope_deg * 0.40)
        # 冲击角随机：街巷与烟尘让入射角更离散
        impact_deg = slope_deg + s.rng.uniform(-10.0, 18.0)
        impact_deg = max(0.0, min(75.0, impact_deg))
        # cos 越小，有效厚度越大
        denom = max(0.26, math.cos(math.radians(impact_deg)))
        return base / denom

    def _player_ap_penetration(*, range_tag: str) -> float:
        """玩家88口径AP的抽象穿深（带距离衰减与随机散布）。"""
        # 仍按游戏内抽象刻度，不宣称真实毫米。
        # 调整为更贴近历史/统一刻度的穿深基准（严格还原方向）
        # 提高中远距穿深基线并缩窄弹道随机散布，降低 long 距离击毁的尾部概率
        base = {"close": 140.0, "medium": 125.0, "long": 110.0}[range_tag]
        # 缩窄随机性：减少低端波动，提升远距一致性
        return base * s.rng.uniform(0.94, 1.06)

    def _apply_track_or_stun(t: EnemyUnit) -> None:
        """非穿透也可能造成履带/机动受限（用状态表现）。"""
        if t.armor < 20:
            return
        if s.rng.random() < 0.16:
            t.status["机动受限"] = max(2, t.status.get("机动受限", 0))
            print(f"{t.name} 履带/悬挂受损：机动受限。")

    def _generate_enemies() -> List[EnemyUnit]:
        terrain = _terrain_tag()

        flags = dict(getattr(s, "story_flags", {}) or {})
        intel_adv = bool(flags.get("intel_saved", False)) or bool(flags.get("escape_intel", False))
        stealth_adv = bool(flags.get("night_stealth", False)) or bool(flags.get("camo_prepared", False))

        # 基于地形生成敌方构成（不再区分BOSS战）
        risk = float(LOCATIONS[s.location_key]["risk"]) * float(state_danger(s))
        # 章节索引（用于替换硬编码回合阈值）
        chapter_idx = max(1, min(40, (int(getattr(s, "round_number", 1) or 1) - 1) // int(CHAPTER_INTERVAL) + 1))
        # 章节线性系数：随着章节增长，逐步提高重装甲出现权重（避免突增）
        chapter_heavy_scale = 1.0 + 0.02 * float(max(0, chapter_idx - 1))

        if str(encounter_mode) == "breakout_large":
            # 突围：连续大型战斗。全近卫，且装甲比例显著提高。
            base_n = s.rng.randint(9, 12)
        elif str(encounter_mode) == "breakout":
            # 突围难度：中等规模战斗，精英比例较高
            base_n = s.rng.randint(6, 9)
        else:
            base_n = s.rng.randint(3, 5)
            base_n += 1 if risk >= 0.55 else 0
            base_n += 1 if risk >= 0.70 else 0
            thr = int(math.ceil(12.0 / float(CHAPTER_INTERVAL)))
            base_n += 1 if chapter_idx >= thr else 0
            base_n = max(4, min(10, base_n))

        if str(encounter_mode) not in ("breakout_large", "breakout"):
            # 夜行/伪装：更容易绕开大编组（不保证安全，仅降低平均数量）
            if stealth_adv:
                base_n = max(3, base_n - 1)
            # 数量随机：围绕基础数量上下浮动；高风险/后期浮动更大
            spread = 1 + (1 if risk >= 0.70 else 0) + (1 if chapter_idx >= thr else 0)
            base_min = max(3, base_n - 1)
            base_max = min(12, base_n + spread)
            base_n = s.rng.randint(base_min, base_max)

        weights: Dict[str, float] = {
            "反坦克组": 1.0,
            "步兵班": 0.85,
            "突击队": 0.55,
            "火箭筒组": 0.35,
            "工兵": 0.7,
            "狙击组": 0.6,
            "重机枪点": 0.5,
            "迫击炮组": 0.35,
            "反坦克炮": 0.35,
            "装甲车": 0.22,
            "卡车": 0.30,
            "SU-76": 0.18,
            "T-34": 0.25,
            # 原“BOSS核心”改为普通敌人：提高出现权重
            "IS-2": 0.14,
            "喷火坦克": 0.10,
        }
        if terrain in ("市区", "市区废墟", "医院", "电台"):
            weights["重机枪点"] += 0.25
            weights["狙击组"] += 0.15
            weights["装甲车"] += 0.08
            weights["喷火坦克"] += 0.10
        if terrain in ("兵营", "郊区", "货场"):
            weights["反坦克炮"] += 0.25
            weights["T-34"] += 0.25
            weights["SU-76"] += 0.22
            weights["卡车"] += 0.18
            weights["IS-2"] += 0.08
            weights["装甲车"] += 0.06
        if terrain in ("地铁", "地下通道"):
            weights["狙击组"] += 0.25
            weights["工兵"] += 0.25
            weights["装甲车"] += 0.10
            weights["喷火坦克"] += 0.16
            # 狭窄地形对装甲不友好
            weights["T-34"] = max(0.05, weights["T-34"] - 0.18)
            weights["SU-76"] = max(0.03, weights["SU-76"] - 0.12)
            weights["卡车"] = max(0.05, weights["卡车"] - 0.12)
            weights["IS-2"] = max(0.02, weights["IS-2"] - 0.05)

        # 后期与高风险：更可能遇到重装甲
        if risk >= 0.65:
            weights["IS-2"] += 0.10
        # 基于章节线性微调重装甲权重，T-34/SU-76/IS-2 受益
        weights["IS-2"] += 0.02 * float(max(0, chapter_idx - 1))
        weights["T-34"] += 0.01 * float(max(0, chapter_idx - 1))
        weights["SU-76"] += 0.008 * float(max(0, chapter_idx - 1))
        # 应用整体章节对重装甲的比例放大
        for hk in ("IS-2", "T-34", "SU-76", "装甲车", "喷火坦克"):
            if hk in weights:
                weights[hk] = float(weights[hk]) * float(chapter_heavy_scale)

        if str(encounter_mode) == "breakout_large":
            # 突围：显著提高装甲权重，降低纯步兵权重
            for k in ("步兵班", "突击队", "工兵", "狙击组"):
                weights[k] = max(0.02, float(weights.get(k, 0.1)) * 0.18)
            for k in ("反坦克组", "火箭筒组", "迫击炮组", "反坦克炮"):
                weights[k] = max(0.05, float(weights.get(k, 0.1)) * 0.90)

            # 装甲单位大幅提高（坦克比例上升）
            weights["装甲车"] = max(0.20, float(weights.get("装甲车", 0.22)) * 1.90)
            weights["SU-76"] = max(0.25, float(weights.get("SU-76", 0.18)) * 2.80)
            weights["T-34"] = max(0.32, float(weights.get("T-34", 0.25)) * 3.10)
            weights["IS-2"] = max(0.25, float(weights.get("IS-2", 0.14)) * 3.20)
        elif str(encounter_mode) == "breakout":
            # 突围难度：适度提高装甲权重
            for k in ("步兵班", "突击队", "工兵", "狙击组"):
                weights[k] = max(0.05, float(weights.get(k, 0.1)) * 0.50)
            for k in ("反坦克组", "火箭筒组", "迫击炮组", "反坦克炮"):
                weights[k] = max(0.10, float(weights.get(k, 0.1)) * 0.80)

            # 装甲单位提高
            weights["装甲车"] = max(0.15, float(weights.get("装甲车", 0.22)) * 1.50)
            weights["SU-76"] = max(0.20, float(weights.get("SU-76", 0.18)) * 2.00)
            weights["T-34"] = max(0.25, float(weights.get("T-34", 0.25)) * 2.20)
            weights["IS-2"] = max(0.18, float(weights.get("IS-2", 0.14)) * 2.50)
            weights["喷火坦克"] = max(0.18, float(weights.get("喷火坦克", 0.10)) * 2.60)
            # 卡车在突围混乱中更少见
            weights["卡车"] = max(0.01, float(weights.get("卡车", 0.30)) * 0.20)

        # 章节分支：情报降低重火力权重；夜行/伪装降低照明/引导单位权重
        if intel_adv:
            weights["IS-2"] = max(0.02, float(weights.get("IS-2", 0.08)) * 0.80)
            weights["T-34"] = max(0.08, float(weights.get("T-34", 0.25)) * 0.90)
            weights["SU-76"] = max(0.06, float(weights.get("SU-76", 0.18)) * 0.90)
            weights["反坦克炮"] = max(0.10, float(weights.get("反坦克炮", 0.35)) * 0.88)
        if stealth_adv:
            weights["装甲车"] = max(0.05, float(weights.get("装甲车", 0.22)) * 0.70)
            weights["重机枪点"] = max(0.12, float(weights.get("重机枪点", 0.5)) * 0.90)

        if str(encounter_mode) == "breakout_large":
            # 明确保证“坦克比例大幅提高”，避免单纯靠权重仍抽到大量步兵
            armored_kinds = ["装甲车", "SU-76", "T-34", "IS-2", "喷火坦克"]
            # 至少一半为装甲单位，且不少于4
            tank_min = max(4, base_n // 2)
            tank_max = max(tank_min, base_n - 2)
            tank_n = int(s.rng.randint(tank_min, tank_max))
            other_n = int(base_n - tank_n)

            tank_weights = [
                float(weights.get("装甲车", 0.22)),
                float(weights.get("SU-76", 0.18)),
                float(weights.get("T-34", 0.25)),
                float(weights.get("IS-2", 0.14)),
                float(weights.get("喷火坦克", 0.10)),
            ]
            tank_pack = s.rng.choices(armored_kinds, weights=tank_weights, k=tank_n)

            other_candidates = [k for k in weights.keys() if k not in set(armored_kinds)]
            other_weights = [float(weights.get(k, 0.0)) for k in other_candidates]
            other_pack = s.rng.choices(other_candidates, weights=other_weights, k=other_n) if other_n > 0 else []

            pack = list(tank_pack) + list(other_pack)
            try:
                s.rng.shuffle(pack)
            except Exception:
                pass
        elif str(encounter_mode) == "breakout":
            # 突围难度：保证一定比例的装甲单位
            armored_kinds = ["装甲车", "SU-76", "T-34", "IS-2", "喷火坦克"]
            # 至少1/3为装甲单位，且不少于2
            tank_min = max(2, base_n // 3)
            tank_max = max(tank_min, base_n - 3)
            tank_n = int(s.rng.randint(tank_min, tank_max))
            other_n = int(base_n - tank_n)

            tank_weights = [
                float(weights.get("装甲车", 0.22)),
                float(weights.get("SU-76", 0.18)),
                float(weights.get("T-34", 0.25)),
                float(weights.get("IS-2", 0.14)),
                float(weights.get("喷火坦克", 0.10)),
            ]
            tank_pack = s.rng.choices(armored_kinds, weights=tank_weights, k=tank_n)

            other_candidates = [k for k in weights.keys() if k not in set(armored_kinds)]
            other_weights = [float(weights.get(k, 0.0)) for k in other_candidates]
            other_pack = s.rng.choices(other_candidates, weights=other_weights, k=other_n) if other_n > 0 else []

            pack = list(tank_pack) + list(other_pack)
            try:
                s.rng.shuffle(pack)
            except Exception:
                pass
        else:
            kinds = list(weights.keys())
            w = [weights[k] for k in kinds]
            pack = s.rng.choices(kinds, weights=w, k=base_n)

        enemies = [_spawn_enemy(k) for k in pack]
        return enemies

    def _pick_target(enemies: List[EnemyUnit], *, prefer_armored: bool = False) -> Optional[int]:
        alive = [e for e in enemies if e.alive]
        if not alive:
            return None

        if prefer_armored:
            armored = [e for e in alive if e.armor >= 20]
            if armored:
                alive = armored
        # 返回 enemies 的索引
        return enemies.index(s.rng.choice(alive))

    def _hit_chance_base() -> int:
        acc = int(PLAYER_BASE_HIT_CHANCE)
        gunner = crew_role_state(s, "炮手")
        if gunner == "missing":
            acc -= 24
        elif gunner == "wounded":
            acc -= 12

        # 熟练度：提升命中（与上下限共同作用）
        if gunner != "missing":
            gp = crew_effective_role_proficiency(s, "炮手")
            # 0..100 -> 0..+8（熟练度=0 等同原状态）
            delta = int(gp // 13)
            if gunner == "wounded":
                delta = int(round(delta * 0.60))
            acc += delta

        if s.debuffs.get("optics_broken", 0) > 0:
            acc -= 12
        if s.debuffs.get("turret_jam", 0) > 0:
            acc -= 10
        if s.buffs.get("观察", 0) > 0:
            acc += 5
        if s.morale <= 30:
            acc -= 8
        if s.morale >= 70:
            acc += 4
        if s.damage >= 75:
            acc -= 6
        if s.buffs.get("强行推进", 0) > 0:
            # 心态更“赌”，命中略差
            acc -= 4
        if player_suppressed_turns > 0:
            acc -= 8
        # 天气：命中修正（双方都受影响，但仍受上下限夹紧）
        try:
            acc += int(weather_effects(s).get("player_hit_delta", 0) or 0)
        except Exception:
            pass
        # 小幅提升玩家基础命中率
        return max(int(PLAYER_HIT_CHANCE_MIN), min(int(PLAYER_HIT_CHANCE_MAX), acc))

    def _is_sdkfz(t: TankAlly) -> bool:
        return str(getattr(t, "model", "") or "") == "Sd.Kfz.251装甲运兵车"

    def _sdkfz_ensure_cargo_units(t: TankAlly) -> List[GarrisonUnit]:
        cargo = getattr(t, "_sdkfz_cargo_units", None)

        # 需求：固定载员为两个班（反坦克组 + 党卫军）。
        # 兼容旧存档：若已存在但不符合规格，则直接规范化为固定配置。
        if isinstance(cargo, list) and len(cargo) == 2:
            try:
                types = sorted(str(getattr(u, "unit_type", "") or "") for u in cargo)
            except Exception:
                types = []
            if types == ["反坦克组", "党卫军"]:
                return cargo

        u1 = GarrisonUnit(
            unit_type="反坦克组",
            name=f"反坦克组-{s.rng.randint(11, 99)}",
            hp=88,
            armor=6,
            power=18,
            morale=58,
        )
        u2 = GarrisonUnit(
            unit_type="党卫军",
            name=f"党卫军班-{s.rng.randint(11, 99)}",
            hp=96,
            armor=8,
            power=19,
            morale=62,
        )
        u1.clamp()
        u2.clamp()
        setattr(u1, "_from_sdkfz", True)
        setattr(u2, "_from_sdkfz", True)
        fixed = [u1, u2]
        setattr(t, "_sdkfz_cargo_units", fixed)
        return fixed

    def _sdkfz_unload_infantry_now(t: TankAlly, *, announce: bool) -> None:
        if not _is_sdkfz(t):
            return
        if bool(getattr(t, "_sdkfz_unloaded", False)):
            return
        cargo = _sdkfz_ensure_cargo_units(t)
        joined: List[GarrisonUnit] = []
        for u in cargo:
            if not getattr(u, "alive", True):
                continue
            if not bool(getattr(u, "_from_sdkfz", False)):
                setattr(u, "_from_sdkfz", True)
            allies.append(u)
            joined.append(u)
        setattr(t, "_sdkfz_unloaded", True)
        setattr(t, "_sdkfz_unloaded_units", joined)
        if announce and joined:
            print("Sd.Kfz.251 放下步兵：" + "、".join(u.name for u in joined) + " 加入战斗。")

    def _sdkfz_infantry_return_or_flee(t: TankAlly) -> None:
        if not _is_sdkfz(t):
            return
        units = getattr(t, "_sdkfz_unloaded_units", None)
        if not isinstance(units, list) or not units:
            return
        sec0 = s.sectors.get(s.location_key)
        returned_any = False
        fled_any = False
        # 规则：若 Sd.Kfz.251 在战斗中被击毁，步兵立刻脱离战斗，尝试就地回归驻军。
        for u in list(units):
            if not getattr(u, "alive", True):
                continue
            try:
                if u in allies:
                    allies.remove(u)
            except Exception:
                pass

            ok = False
            if sec0 is not None:
                try:
                    already = any(id(x) == id(u) for x in sec0.garrison_units)
                except Exception:
                    already = False
                if already:
                    ok = True
                elif len(sec0.garrison_units) < 10:
                    sec0.garrison_units.append(u)
                    ok = True
            if ok:
                returned_any = True
                print(f"步兵就地回归驻军：{u.name} 退入街区防线。")
            else:
                fled_any = True
                u.hp = 0
                u.clamp()
                print(f"步兵溃逃：{u.name} 无法回归驻军，溃散消失。")

        # 从载具载员中移除（避免下一场又重复出现）
        try:
            cargo = getattr(t, "_sdkfz_cargo_units", None)
            if isinstance(cargo, list) and cargo:
                cargo = [x for x in cargo if x not in units]
                setattr(t, "_sdkfz_cargo_units", cargo)
        except Exception:
            pass
        try:
            setattr(t, "_sdkfz_unloaded_units", [])
        except Exception:
            pass
        if sec0 is not None and (returned_any or fled_any):
            sec0.clamp()

    def _apply_enemy_fire(e: EnemyUnit, *, range_tag: str, smoke: int, maneuver: int, support: int) -> None:
        nonlocal player_suppressed_turns
        if not e.alive:
            return
        
        # 目标选择：敌方会更频繁打击我方友军（驻军/友军坦克），让 HP 系统真正生效
        infantry_small_arms = {"步兵班", "突击队", "工兵"}
        at_kinds = {"反坦克组", "火箭筒组"}
        gun_kinds = {"反坦克炮", "SU-76", "T-34", "IS-2"}
        
        def _pick_fire_target() -> tuple[str, Optional[object]]:
            # 默认打玩家；若存在友军则按威胁与火力类型分配火力
            try:
                live_g = [u for u in allies if getattr(u, "alive", False)]
            except Exception:
                live_g = []
            try:
                live_t = [t for t in tank_allies if getattr(t, "alive", False)]
            except Exception:
                live_t = []
            
            if not live_g and not live_t:
                return ("player", None)
            
            # 若我方有高威胁反装甲火力，敌方更可能压制它
            has_high_threat_g = any(getattr(u, "unit_type", "") in ("88炮", "反坦克炮", "反坦克组") for u in live_g)
            
            w_player, w_g, w_t = 0.50, 0.25, 0.25
            if e.kind == "狙击组":
                w_player, w_g, w_t = 0.82, 0.12, 0.06
            elif e.kind == "迫击炮组":
                w_player, w_g, w_t = 0.40, 0.38, 0.22
            elif e.kind in at_kinds or e.kind in gun_kinds or e.kind == "反坦克炮":
                w_player, w_g, w_t = 0.50, 0.15, 0.35
                if has_high_threat_g:
                    w_g += 0.10
                    w_player = max(0.35, w_player - 0.10)
            elif e.kind in infantry_small_arms or e.kind in ("重机枪点", "卡车"):
                w_player, w_g, w_t = 0.48, 0.44, 0.08

            # 精英更偏好打坦克/反装甲目标，且更具威胁感知
            try:
                if getattr(e, "elite", False):
                    w_t += 0.08
                    w_g += 0.04
                    w_player = max(0.10, w_player - 0.06)
                    # 章节微加成使晚期精英更为“聪明”
                    chapter_idx = max(1, min(40, (int(getattr(s, "round_number", 1) or 1) - 1) // int(CHAPTER_INTERVAL) + 1))
                    w_t += 0.01 * float(max(0, chapter_idx - 1))
            except Exception:
                pass
            
            # 没有对应目标就把权重转回玩家
            if not live_g:
                w_player += w_g
                w_g = 0.0
            if not live_t:
                w_player += w_t
                w_t = 0.0
            
            choice = s.rng.choices(["player", "garrison", "tank"], weights=[w_player, w_g, w_t], k=1)[0]
            if choice == "garrison" and live_g:
                return ("garrison", s.rng.choice(live_g))
            if choice == "tank" and live_t:
                return ("tank", s.rng.choice(live_t))
            return ("player", None)
        
        target_kind, target_obj = _pick_fire_target()
        if e.status.get("压制", 0) > 0:
            # 被压制的单位更难有效射击
            acc = e.accuracy - 18
        else:
            acc = e.accuracy

        smoke_pen = 18
        maneuver_pen = 12
        support_pen = 10

        # 敌方照明弹：会削弱烟幕的遮蔽效果
        if smoke > 0 and illumination_turns > 0:
            smoke_pen = max(6, int(smoke_pen * 0.55))

        # 我方车辆状态会影响“机动/协同”的实际收益
        if s.debuffs.get("engine_damage", 0) > 0:
            maneuver_pen = max(6, int(maneuver_pen * 0.55))
        if s.debuffs.get("radio_damage", 0) > 0 or crew_role_state(s, "通信员") == "missing":
            support_pen = max(5, int(support_pen * 0.65))
        if e.kind == "迫击炮组":
            # 间接火力：烟幕与机动仍有效，但削弱幅度更小
            smoke_pen = 8
            maneuver_pen = 6
            support_pen = 6
        elif e.kind == "重机枪点":
            # 固定火力点：更依赖视线
            smoke_pen = 22
            maneuver_pen = 14

        # 距离：远距离显著降低双方命中；敌方受影响更大
        if range_tag == "close":
            acc += 2
        elif range_tag == "long":
            acc -= 24

        if smoke > 0:
            acc -= smoke_pen
        if maneuver > 0:
            acc -= maneuver_pen
        if support > 0:
            acc -= support_pen

        # 稳固阵位：短时间降低敌方有效命中
        if player_fortify_turns > 0:
            acc -= 6

        # 天气：敌方命中修正
        try:
            acc += int(weather_effects(s).get("enemy_hit_delta", 0) or 0)
        except Exception:
            pass

        # 不额外叠加“濒死更容易被打中”的雪球效果

        acc = max(8, min(90, acc))
        # 对玩家目标施加小幅减伤/减命中以降低总体难度
        try:
            if target_kind == "player":
                acc = max(8, acc - 6)
        except Exception:
            pass
        if s.rng.randint(1, 100) <= acc:
            raw = s.rng.randint(e.dmg_min, e.dmg_max)
            try:
                if target_kind == "player":
                    raw = max(1, int(round(float(raw) * 0.92)))
            except Exception:
                pass

            # 敌方命中后可能“跳弹”（我方装甲吸收）：主要影响反坦克火力与火炮
            raw_for_intensity = raw

            def _apply_enemy_hit_to_garrison(u: GarrisonUnit, *, raw: int) -> None:
                base = float(raw)
                if e.kind in infantry_small_arms:
                    base *= s.rng.uniform(0.90, 1.35)
                elif e.kind in ("重机枪点", "卡车"):
                    base *= s.rng.uniform(1.10, 1.75)
                elif e.kind == "迫击炮组":
                    base *= s.rng.uniform(1.55, 2.40)
                elif e.kind in at_kinds:
                    base *= s.rng.uniform(2.20, 3.40)
                elif e.kind in gun_kinds or e.kind == "反坦克炮":
                    base *= s.rng.uniform(2.80, 4.20)
                else:
                    base *= s.rng.uniform(1.10, 1.80)
            
                if u.unit_type in ("88炮", "反坦克炮"):
                    base *= 1.15
            
                dmg = max(1, int(base - float(getattr(u, "armor", 0)) * 0.65))
                u.hp -= dmg
                u.clamp()
                print(f"敌方火力命中友军：{e.name} 击中 {u.name}（-{dmg}，HP{u.hp}）。")
                if not u.alive:
                    print(f"友军被击毁：{u.name} 在火力与烟尘中失去战斗力。")
        
            def _apply_enemy_hit_to_tank(t: TankAlly, *, raw: int, smoke: int, maneuver: int) -> None:
                # 小武器对装甲目标主要是扰乱；反坦克/火炮才会造成有效伤害
                if e.kind in infantry_small_arms or e.kind in ("重机枪点", "卡车"):
                    t.clamp()
                    print(f"敌方火力压制友军装甲：{e.name} 的弹雨敲击 {t.name}。")
                    return
            
                armor_rating = float(getattr(t, "armor", 80))
                base = float(raw)
                if e.kind == "迫击炮组":
                    base *= s.rng.uniform(1.35, 2.00)
                elif e.kind in at_kinds:
                    base *= s.rng.uniform(2.00, 2.90)
                elif e.kind in gun_kinds or e.kind == "反坦克炮":
                    base *= s.rng.uniform(2.40, 3.60)
                else:
                    base *= s.rng.uniform(1.60, 2.40)
            
                p_ric = 0.08 + (armor_rating / (armor_rating + 140.0)) * 0.20
                if maneuver > 0:
                    p_ric += 0.04
                p_ric = max(0.06, min(0.45, p_ric))
                ric = s.rng.random() < p_ric
                glancing = (smoke > 0 or maneuver > 0) and s.rng.random() < 0.20
            
                if ric:
                    base *= 0.55
                if glancing:
                    base *= 0.80
            
                # 装甲吸收（抽象）：仍让高装甲更耐打
                base -= armor_rating * (0.22 if (e.kind in at_kinds or e.kind in gun_kinds or e.kind == "反坦克炮") else 0.12)
                dmg = max(1, int(base))
                t.hp -= dmg
                t.clamp()
                tag = "跳弹" if ric else ("擦伤" if glancing else "命中")
                print(f"敌方反装甲火力{tag}：{e.name} 击中 {t.name}（-{dmg}，HP{t.hp}）。")
                if not t.alive:
                    print(f"友军坦克被击毁：{t.name} 冒烟停下，不再回应电台。")
                    # 需求：Sd.Kfz.251 在战斗中被摧毁 -> 步兵就地回归驻军；无法回归则溃散消失
                    _sdkfz_infantry_return_or_flee(t)

            # 若火力打在友军身上：直接结算友军 HP（不影响玩家结构/乘员/模块）
            if target_kind == "garrison" and isinstance(target_obj, GarrisonUnit):
                _apply_enemy_hit_to_garrison(target_obj, raw=raw)
                return
            if target_kind == "tank" and isinstance(target_obj, TankAlly):
                _apply_enemy_hit_to_tank(target_obj, raw=raw, smoke=smoke, maneuver=maneuver)
                return

            if e.kind in infantry_small_arms:
                print(f"敌方火力命中：{e.name} 的弹雨敲击装甲。")
            elif e.kind == "狙击组":
                # 狙击：主要杀伤乘员
                print(f"狙击火力命中：{e.name} 的子弹迫使你们收缩。")
                player_suppressed_turns = max(player_suppressed_turns, 1)
                if s.rng.random() < 0.55:
                    candidates = [m for m in s.crew if m.alive and m.role != "车长"]
                    if candidates:
                        m = s.rng.choice(candidates)
                        hit = s.rng.randint(14, 30)
                        m.hp -= hit
                        m.stress += 14
                        if m.hp <= 0:
                            s.crew_lost += 1
                            print(f"乘员损失：{m.role} {m.name} 未能归队。")
                        else:
                            print(f"乘员受伤：{m.role} {m.name} 状态恶化。")
            elif e.kind in ("重机枪点", "卡车"):
                # 机枪：主要压制我方
                sup = 2 if e.kind == "重机枪点" else 1
                player_suppressed_turns = max(player_suppressed_turns, sup)
                print(f"机枪火力压制：{e.name} 迫使你们压低身位（我方压制+{sup}）。")
            elif e.kind in at_kinds:
                armor_rating = float(player_armor_rating(s))
                p_ric = 0.14 + (armor_rating / (armor_rating + 120.0)) * 0.18
                if maneuver > 0:
                    p_ric += 0.04
                p_ric = max(0.08, min(0.42, p_ric))
                ric = s.rng.random() < p_ric
                glancing = (smoke > 0 or maneuver > 0) and s.rng.random() < 0.28
                if ric:
                    raw_for_intensity = max(1, int(raw * 0.55))
                    dmg = max(1, int(raw * (0.28 if glancing else 0.38)))
                else:
                    dmg = max(2, int(raw * (0.65 if glancing else 0.95)))
                eff = _apply_player_structure_damage(dmg)
                absorbed = max(0, dmg - eff)
                if absorbed > 0:
                    tag = "跳弹" if ric else "冲击"
                    print(f"敌方反坦克火力命中：{e.name} 造成{tag}（损伤+{eff}，装甲吸收{absorbed}）。")
                else:
                    tag = "跳弹" if ric else "冲击"
                    print(f"敌方反坦克火力命中：{e.name} 造成{tag}（损伤+{eff}）。")
            elif e.kind in gun_kinds:
                armor_rating = float(player_armor_rating(s))
                p_ric = 0.10 + (armor_rating / (armor_rating + 110.0)) * 0.16
                if maneuver > 0:
                    p_ric += 0.03
                p_ric = max(0.06, min(0.35, p_ric))
                ric = s.rng.random() < p_ric
                glancing = (smoke > 0 or maneuver > 0) and s.rng.random() < 0.18
                if ric:
                    raw_for_intensity = max(1, int(raw * 0.50))
                    dmg = max(2, int(raw * (0.30 if glancing else 0.45)))
                else:
                    dmg = max(4, int(raw * (0.70 if glancing else 1.05)))
                eff = _apply_player_structure_damage(dmg)
                absorbed = max(0, dmg - eff)
                if absorbed > 0:
                    tag = "跳弹" if ric else "重击"
                    print(f"敌方炮击命中：{e.name} 造成{tag}（损伤+{eff}，装甲吸收{absorbed}）。")
                else:
                    tag = "跳弹" if ric else "重击"
                    print(f"敌方炮击命中：{e.name} 造成{tag}（损伤+{eff}）。")
            else:
                print(f"敌方火力命中：{e.name} 造成冲击。")

            # 不再因战斗过程扣除士气（仍保留结构/乘员/压制/模块故障等代价）

            # 模块损伤：命中后小概率出现关键故障（持续若干“回合”）
            # 注：步兵火力不产生结构损伤，但仍可能通过震击/碎片导致故障。
            intensity = raw_for_intensity
            if e.kind in at_kinds or e.kind in gun_kinds:
                intensity = raw_for_intensity + 10
            p_mod = float(ENEMY_MODULE_FAULT_BASE) + min(
                float(ENEMY_MODULE_FAULT_INTENSITY_CAP),
                intensity / float(ENEMY_MODULE_FAULT_INTENSITY_DIV),
            )
            if e.kind in ("反坦克炮", "IS-2"):
                p_mod += float(ENEMY_MODULE_FAULT_AT_BONUS)
            if smoke > 0:
                p_mod -= 0.01
            if maneuver > 0:
                p_mod -= 0.01
            if player_fortify_turns > 0:
                p_mod -= 0.04
            # 章节维护：降低关键故障概率（偏“可靠性提升”）
            if bool(getattr(s, "story_flags", {}).get("maint_done", False)):
                p_mod *= 0.90
            if s.rng.random() < max(float(ENEMY_MODULE_FAULT_MIN), min(float(ENEMY_MODULE_FAULT_MAX), p_mod)):
                pool = [
                    ("optics_broken", 2.2),
                    ("gun_breech", 1.6),
                    ("turret_jam", 1.4),
                    ("engine_damage", 1.3),
                    ("radio_damage", 0.9),
                ]
                keys = [k for k, _ in pool]
                w = [float(x) for _, x in pool]
                k = s.rng.choices(keys, weights=w, k=1)[0]
                dur = 2
                if k == "optics_broken":
                    dur = 3
                elif k == "engine_damage":
                    dur = 3
                s.debuffs[k] = max(dur, int(s.debuffs.get(k, 0)))
                msg = {
                    "optics_broken": "观瞄受损：瞄准更吃力。",
                    "gun_breech": "炮闩/炮闩机构异常：装填节奏被打乱。",
                    "turret_jam": "炮塔/回转机构卡滞：指向更不灵活。",
                    "engine_damage": "发动机/传动异响：机动变迟缓。",
                    "radio_damage": "电台受扰：协同与联络变不稳定。",
                }[k]
                print(msg)

            # 乘员受伤/压力：保留给“反坦克/火炮/爆炸”类（步兵小武器不直接穿透）
            if e.kind in at_kinds or e.kind in gun_kinds or e.kind == "迫击炮组":
                if s.rng.random() < 0.18 + (0.08 if e.kind in ("反坦克炮", "IS-2") else 0.0):
                    candidates = [m for m in s.crew if m.alive and m.role != "车长"]
                    if candidates:
                        m = s.rng.choice(candidates)
                        hit = s.rng.randint(12, 28)
                        m.hp -= hit
                        m.stress += 10
                        if m.hp <= 0:
                            s.crew_lost += 1
                            print(f"乘员损失：{m.role} {m.name} 未能归队。")
                        else:
                            print(f"乘员受伤：{m.role} {m.name} 状态恶化。")
        else:
            print(f"敌方火力落空：{e.name} 的射击被掩体与烟尘吞没。")

    def _try_grenade(e: EnemyUnit, *, smoke: int, maneuver: int) -> bool:
        """低命中率手榴弹：不增加 s.damage，主要杀伤乘员/扰乱节奏。"""
        if not e.alive:
            return False
        if e.status.get("压制", 0) > 0:
            return False
        # 低命中率；烟幕更容易贴近，机动会降低被准确投掷到位的概率
        # 略微提高投掷命中概率并在烟幕下更易命中，机动仍然有利
        p = 0.36
        if smoke > 0:
            p += 0.12
        if maneuver > 0:
            p -= 0.04
        p = max(0.15, min(0.55, p))
        if s.rng.random() >= _skill_prob(p, e):
            return False

        candidates = [m for m in s.crew if m.alive and m.role != "车长"]
        dmg = s.rng.randint(1, 4)
        eff = _apply_player_structure_damage(dmg)
        print(f"手榴弹爆开：{e.name} 的投掷造成冲击（损伤+{eff}）。")
        if candidates:
            m = s.rng.choice(candidates)
            hit = s.rng.randint(12, 28)
            m.hp -= hit
            m.stress += 10
            if m.hp <= 0:
                s.crew_lost += 1
                print(f"乘员损失：{m.role} {m.name} 未能归队。")
            else:
                print(f"乘员受伤：{m.role} {m.name} 状态恶化。")

        # 小概率造成外部设备故障（仍不算结构损伤）
        # 增加小概率对外设的扰乱
        if s.rng.random() < 0.20:
            s.debuffs["radio_damage"] = max(2, int(s.debuffs.get("radio_damage", 0)))
            print("爆炸碎片扰乱了电台：联络更不稳定。")
        return True

    def _try_cluster_grenade(e: EnemyUnit, *, smoke: int, maneuver: int) -> bool:
        """集束手榴弹：更强的爆炸碎片/扰乱，概率更低。"""
        nonlocal player_suppressed_turns
        if not e.alive:
            return False
        if e.status.get("压制", 0) > 0:
            return False

        # 概率：贴近（烟幕）更容易投到位；我方机动会降低落点有效性
        # 集束手榴弹更具杀伤性，整体概率上调；工兵/突击队表现更好
        p = 0.22
        if e.kind == "工兵":
            p += 0.08
        elif e.kind == "突击队":
            p += 0.06
        if smoke > 0:
            p += 0.10
        if maneuver > 0:
            p -= 0.03
        p = max(0.10, min(0.50, p))
        if s.rng.random() >= _skill_prob(p, e):
            return False

        # 结构损伤：顶攻/薄弱部位被碎片敲击（不做过强，主要体现在士气/乘员/故障上）
        raw = s.rng.randint(3, 5) + (1 if smoke > 0 and s.rng.random() < 0.45 else 0)
        eff = _apply_player_structure_damage(raw, top_attack=True)
        player_suppressed_turns = max(player_suppressed_turns, 1)
        print(f"集束手榴弹爆开：{e.name} 的投掷在车体周围连环炸响（损伤+{eff}，我方压制+1）。")

        # 更高概率波及乘员
        candidates = [m for m in s.crew if m.alive and m.role != "车长"]
        if candidates and s.rng.random() < 0.85:
            m = s.rng.choice(candidates)
            hit = s.rng.randint(14, 32)
            m.hp -= hit
            m.stress += 14
            if m.hp <= 0:
                s.crew_lost += 1
                print(f"乘员损失：{m.role} {m.name} 未能归队。")
            else:
                print(f"乘员受伤：{m.role} {m.name} 状态恶化。")

        # 小概率造成外部设备故障（通信/观瞄更常见）
        if s.rng.random() < 0.30:
            s.debuffs["radio_damage"] = max(2, int(s.debuffs.get("radio_damage", 0)))
            print("爆炸碎片扰乱了电台：联络更不稳定。")
        if s.rng.random() < 0.22:
            s.debuffs["optics_broken"] = max(2, int(s.debuffs.get("optics_broken", 0)))
            print("碎片打在观瞄上：瞄准更吃力。")
        return True

    def _enemy_ai_step(
        e: EnemyUnit,
        enemies: List[EnemyUnit],
        *,
        range_tag: str,
        smoke: int,
        maneuver: int,
        support: int,
        tno: int,
        max_turns: int,
    ) -> None:
        nonlocal player_suppressed_turns
        nonlocal illumination_turns, offmap_arty_turns, offmap_arty_intensity
        if not e.alive:
            return

        # 装甲车：倾向于打照明弹并尝试引导远程炮火
        if e.kind == "装甲车":
            # 小冷却：避免每回合都引导
            cd = int(e.status.get("引导冷却", 0))
            if cd > 0:
                e.status["引导冷却"] = cd - 1
                if e.status["引导冷却"] <= 0:
                    e.status.pop("引导冷却", None)

            # 照明弹：在我方烟幕存在时优先点亮，削弱遮蔽
            if smoke > 0 and illumination_turns <= 0 and e.status.get("压制", 0) == 0:
                p_flare = _skill_prob(0.22, e)
                if s.rng.random() < p_flare:
                    illumination_turns = 1
                    print(f"⚠️ {e.name} 打出照明弹：烟幕的遮蔽效果被削弱。")

            # 引导炮火：若本场未触发炮火骚扰，装甲车有概率把校射带进来
            if offmap_arty_turns <= 0 and e.status.get("压制", 0) == 0 and e.status.get("引导冷却", 0) == 0:
                # 烟幕/我方机动会让引导更难
                p_call = 0.08
                if smoke > 0:
                    p_call -= 0.03
                if maneuver > 0:
                    p_call -= 0.03
                p_call = max(0.03, min(0.14, p_call))
                if s.rng.random() < _skill_prob(p_call, e):
                    offmap_arty_turns = 1
                    offmap_arty_intensity = max(offmap_arty_intensity, 2)
                    e.status["引导冷却"] = 2
                    print(f"⚠️ {e.name} 正在用信号引导炮火：远处传来校射声。")

            _apply_enemy_fire(e, range_tag=range_tag, smoke=smoke, maneuver=maneuver, support=support)
            return

        # 喷火坦克：近距离火焰压制（更伤士气/乘员，结构伤害较轻）
        if e.kind == "喷火坦克":
            cd = int(e.status.get("喷火冷却", 0))
            if cd > 0:
                e.status["喷火冷却"] = cd - 1
                if e.status["喷火冷却"] <= 0:
                    e.status.pop("喷火冷却", None)
            else:
                terrain = _terrain_tag()
                if range_tag == "close" and e.status.get("压制", 0) == 0:
                    p = 0.42
                    if smoke > 0:
                        p -= 0.06
                    if maneuver > 0:
                        p -= 0.04
                    if terrain in ("市区", "市区废墟", "地铁", "地下通道", "医院"):
                        p += 0.06
                    p = max(0.18, min(0.55, p))
                    if s.rng.random() < _skill_prob(p, e):
                        # 火焰更偏向“器材/乘员”而非纯结构撕裂
                        fmul = fire_weather_multiplier(s)
                        fprob = fire_weather_prob_scale(s)
                        raw = s.rng.randint(2, 5)
                        raw = max(1, int(round(float(raw) * float(fmul))))
                        eff = _apply_player_structure_damage(raw)
                        player_suppressed_turns = max(player_suppressed_turns, 1)
                        hint = _fire_weather_hint(s)
                        hint_text = f"；{hint}" if hint else ""
                        print(f"⚠️ 火焰喷射：{e.name} 近距离扫过街口（损伤+{eff}，我方压制+1{hint_text}）。")
                        # 主要效果：烧毁/熏扰关键设备
                        if s.rng.random() < min(0.98, 0.55 * fprob):
                            s.debuffs["optics_broken"] = max(2, int(s.debuffs.get("optics_broken", 0)))
                            print("热浪与烟雾扰乱观瞄：瞄准更吃力。")
                        if s.rng.random() < min(0.98, 0.30 * fprob):
                            s.debuffs["radio_damage"] = max(2, int(s.debuffs.get("radio_damage", 0)))
                            print("火焰与烟尘冲进天线/电台：联络更不稳定。")
                        if s.rng.random() < min(0.98, 0.18 * fprob):
                            s.debuffs["engine_damage"] = max(2, int(s.debuffs.get("engine_damage", 0)))
                            print("高温灼烤传动：机动变迟缓。")
                        if s.rng.random() < min(0.98, 0.16 * fprob):
                            s.debuffs["mg_jam"] = max(1, int(s.debuffs.get("mg_jam", 0)))
                            print("机枪受热与灰尘影响：短暂卡壳。")
                        candidates = [m for m in s.crew if m.alive and m.role != "车长"]
                        if candidates and s.rng.random() < 0.75:
                            m = s.rng.choice(candidates)
                            hit = s.rng.randint(14, 28)
                            hit = max(1, int(round(float(hit) * float(fmul))))
                            m.hp -= hit
                            m.stress += 22
                            if m.hp <= 0:
                                s.crew_lost += 1
                                print(f"乘员损失：{m.role} {m.name} 未能归队。")
                            else:
                                print(f"乘员受伤：{m.role} {m.name} 状态恶化。")
                        e.status["喷火冷却"] = 1
                        return

            _apply_enemy_fire(e, range_tag=range_tag, smoke=smoke, maneuver=maneuver, support=support)
            return

        # 卡车：每回合卸载步兵；自身视作机枪火力
        if e.kind == "卡车":
            dropped = int(e.status.get("卸载", 0))
            if dropped < 3 and len([x for x in enemies if x.alive]) < 14 and s.rng.random() < _skill_prob(0.70, e):
                e.status["卸载"] = dropped + 1
                enemies.append(_spawn_enemy("步兵班"))
                print(f"卡车停靠：{e.name} 放下一批步兵。")
            _apply_enemy_fire(e, range_tag=range_tag, smoke=smoke, maneuver=maneuver, support=support)
            return

        # 突击队：尝试攀附坦克
        if e.kind == "突击队":
            # 需求：若进入永久攀附，则直到战斗结束前都会造成效果，且无法被消灭
            if e.status.get("攀附永续", 0) > 0:
                if e.status.get("攀附延迟", 0) > 0:
                    # 宽限回合：本回合不生效
                    return
                first_attach = (not bool(getattr(e, "elite", False))) and int(e.status.get("攀附首回合", 0) or 0) > 0
                candidates = [m for m in s.crew if m.alive and m.role != "车长"]
                if candidates:
                    m = s.rng.choice(candidates)
                    hit = s.rng.randint(10, 22)
                    m.hp -= hit
                    m.stress += 14
                    print(f"突击队攀附作乱：{e.name} 迫使你们分神处置（无法清除）。")
                    if m.hp <= 0:
                        s.crew_lost += 1
                        print(f"乘员损失：{m.role} {m.name} 未能归队。")
                    else:
                        print(f"乘员受伤：{m.role} {m.name} 状态恶化。")
                if first_attach:
                    e.status.pop("攀附首回合", None)
                    e.status.pop("攀附永续", None)
                    print(f"{e.name} 刚发动攀附攻击就被乘员赶下车体。")
                return

            # 旧的短效攀附（仍可被机枪驱离）
            if e.status.get("攀附", 0) > 0:
                if e.status.get("攀附延迟", 0) > 0:
                    # 宽限回合：本回合不生效
                    return
                first_attach = (not bool(getattr(e, "elite", False))) and int(e.status.get("攀附首回合", 0) or 0) > 0
                candidates = [m for m in s.crew if m.alive and m.role != "车长"]
                if candidates:
                    m = s.rng.choice(candidates)
                    hit = s.rng.randint(10, 22)
                    m.hp -= hit
                    m.stress += 14
                    print(f"突击队攀附作乱：{e.name} 迫使你们分神处置。")
                    if m.hp <= 0:
                        s.crew_lost += 1
                        print(f"乘员损失：{m.role} {m.name} 未能归队。")
                    else:
                        print(f"乘员受伤：{m.role} {m.name} 状态恶化。")
                if first_attach:
                    e.status.pop("攀附首回合", None)
                    e.status.pop("攀附", None)
                    print(f"{e.name} 刚发动攀附攻击就被乘员赶下车体。")
                    return
                e.status["攀附"] = max(0, int(e.status.get("攀附", 0)) - 1)
                if e.status.get("攀附", 0) <= 0:
                    e.status.pop("攀附", None)
                return

            # 宽限回合结算：过了一回合仍未攻击，则永久不可清除；该回合无效果
            if e.status.get("攀附准备", 0) > 0:
                e.status["攀附准备"] = max(0, int(e.status.get("攀附准备", 0)) - 1)
                if e.status.get("攀附准备", 0) <= 0:
                    e.status.pop("攀附准备", None)
                    if int(e.status.get("本回合被攻击", 0) or 0) <= 0:
                        e.status["攀附永续"] = 1
                        e.status["攀附延迟"] = 1
                        e.status["攀附首回合"] = 1
                        print(f"⚠️ {e.name} 牢牢攀附：你们错过了清除窗口，它将持续造成影响直到战斗结束。")
                    else:
                        # 被攻击过（不论是否命中/击杀），则仍进入“可清除”的短效攀附
                        e.status["攀附"] = 2
                        e.status["攀附延迟"] = 1
                        e.status["攀附首回合"] = 1
                return

            if e.status.get("压制", 0) == 0:
                p = 0.18
                if smoke > 0:
                    p += 0.10
                if maneuver <= 0:
                    p += 0.05
                p = max(0.08, min(0.42, p))
                if s.rng.random() < _skill_prob(p, e):
                    # 需求：攀附尝试要有提示；并提供一回合宽限（该回合无效果）
                    e.status["攀附准备"] = 1
                    print(f"⚠️ 突击队冲近：{e.name} 试图攀上车体！若本回合不攻击它，下回合将难以清除。")
                    return

            # 未能攀附则投手榴弹
            if not _try_cluster_grenade(e, smoke=smoke, maneuver=maneuver):
                _try_grenade(e, smoke=smoke, maneuver=maneuver)
            return

        # 工兵：平时手榴弹；最后一回合若未击杀，会用燃烧弹重点杀伤乘员
        if e.kind == "工兵":
            if tno == max_turns:
                if e.status.get("压制", 0) == 0 and s.rng.random() < _skill_prob(0.62, e):
                    candidates = [m for m in s.crew if m.alive and m.role != "车长"]
                    fmul = fire_weather_multiplier(s)
                    fprob = fire_weather_prob_scale(s)
                    hint = _fire_weather_hint(s)
                    hint_text = f"（{hint}）" if hint else ""
                    print(f"燃烧弹投来：{e.name} 逼得你们在车内艰难处置。{hint_text}")
                    if candidates:
                        m = s.rng.choice(candidates)
                        hit = s.rng.randint(18, 35)
                        hit = max(1, int(round(float(hit) * float(fmul))))
                        m.hp -= hit
                        m.stress += 18
                        if m.hp <= 0:
                            s.crew_lost += 1
                            print(f"乘员损失：{m.role} {m.name} 未能归队。")
                        else:
                            print(f"乘员受伤：{m.role} {m.name} 状态恶化。")
                    # 额外：燃烧更容易烧坏/熏扰设备
                    if s.rng.random() < min(0.98, 0.55 * fprob):
                        s.debuffs["optics_broken"] = max(2, int(s.debuffs.get("optics_broken", 0)))
                        print("火光与烟尘干扰观瞄：瞄准更吃力。")
                    if s.rng.random() < min(0.98, 0.38 * fprob):
                        s.debuffs["radio_damage"] = max(2, int(s.debuffs.get("radio_damage", 0)))
                        print("浓烟灌入电台舱：联络更不稳定。")
                    if s.rng.random() < min(0.98, 0.22 * fprob):
                        s.debuffs["engine_damage"] = max(2, int(s.debuffs.get("engine_damage", 0)))
                        print("燃烧灼烤管线：机动变迟缓。")
                    return

            if not _try_cluster_grenade(e, smoke=smoke, maneuver=maneuver):
                _try_grenade(e, smoke=smoke, maneuver=maneuver)
            return

        # 步兵班：不打结构伤害；主要投手榴弹与扰乱
        if e.kind == "步兵班":
            if not _try_cluster_grenade(e, smoke=smoke, maneuver=maneuver):
                _try_grenade(e, smoke=smoke, maneuver=maneuver)
            return

        # 卡车更倾向于在受创后撤离（机动受限会降低撤离概率）
        if e.kind == "卡车" and e.hp <= 45 and s.rng.random() < (0.45 * (float(ELITE_FLEE_MULT) if e.elite else 1.0) * (0.55 if e.status.get("机动受限", 0) > 0 else 1.0)):
            e.alive = False
            tag = f"逃窜敌人-{s.round_number}-{s.rng.randint(10,99)}"
            s.fleeing_enemies.append(tag)
            print(f"敌人趁乱撤离：{e.name} 迅速消失在街口。")
            return

        # 低血量可能撤退（留下“逃窜敌人”）
        if e.hp <= 25 and s.rng.random() < (0.22 * (float(ELITE_FLEE_MULT) if e.elite else 1.0) * (0.60 if e.status.get("机动受限", 0) > 0 else 1.0)):
            e.alive = False
            tag = f"逃窜敌人-{s.round_number}-{s.rng.randint(10,99)}"
            s.fleeing_enemies.append(tag)
            print(f"敌人趁乱撤离：{e.name} 消失在断墙后。")
            return

        # IS-2：主炮装填慢，显著降低射击频率（开火后需等待 5 个战斗回合）
        if e.kind == "IS-2":
            cd = int(e.status.get("主炮冷却", 0))
            if cd > 0:
                return
            _apply_enemy_fire(e, range_tag=range_tag, smoke=smoke, maneuver=maneuver, support=support)
            # 本回合已开火；由于冷却在“回合开始”递减，这里 +1 用于保证完整 5 回合等待。
            e.status["主炮冷却"] = int(IS2_MAIN_GUN_COOLDOWN_TURNS) + 1
            return

        _apply_enemy_fire(e, range_tag=range_tag, smoke=smoke, maneuver=maneuver, support=support)

    def _player_attack(enemies: List[EnemyUnit], *, mode: str, target_idx: int, range_tag: str, smoke: int, maneuver: int) -> None:
        if target_idx < 0 or target_idx >= len(enemies):
            return
        t = enemies[target_idx]
        if not t.alive:
            return

        # 需求：用于“突击队攀附宽限回合”判定——只要玩家本回合选择攻击它，就视作“攻击过”。
        t.status["本回合被攻击"] = 1

        base_acc = _hit_chance_base()
        if smoke > 0:
            base_acc -= 6
        if maneuver > 0:
            # 机动占位/调整姿态：更容易拿到更好的射击窗口
            base_acc += 4
        if mode == "AP":
            base_acc -= 8 if t.armor >= 20 else 0
        elif mode == "HE":
            base_acc += 6 if t.armor <= 0 else -2
        elif mode == "MG":
            base_acc += 10
            if s.mg_ammo <= 12:
                base_acc -= 8
            elif s.mg_ammo <= 24:
                base_acc -= 4

        # 我方命中率不受距离影响（距离只影响穿深/溅射等，不影响是否命中）
        base_acc = max(10, min(int(PLAYER_HIT_CHANCE_MAX), base_acc))

        if mode == "MG" and t.armor >= 20:
            print("机枪对装甲目标几乎无效。")
            # 仍可造成压制
            t.status["压制"] = max(2, t.status.get("压制", 0))
            return

        def _apply_he_splash(*, center_idx: int, primary_dmg: int, range_tag: str, direct_hit: bool) -> None:
            """我方 HE 的破片溅射/冲击波：按敌人列表顺序波及“近处”的后续敌人。"""
            if primary_dmg <= 0:
                return

            # 越远越难波及更多目标
            max_targets = 1 if range_tag == "long" else 2
            falloffs = (0.40, 0.22) if direct_hit else (0.30, 0.16)

            hit_n = 0
            for j in range(center_idx + 1, len(enemies)):
                if hit_n >= max_targets:
                    break
                other = enemies[j]
                if not getattr(other, "alive", False):
                    continue

                factor = float(falloffs[min(hit_n, len(falloffs) - 1)])
                dmg = int(max(1, int(primary_dmg * factor * s.rng.uniform(0.85, 1.15))))
                # 装甲目标对破片/冲击波更耐受
                if int(getattr(other, "armor", 0) or 0) >= 20:
                    dmg = int(round(dmg * 0.55))

                # 近卫/精英：破片更多转化为压制而非直接伤害
                if dmg > 0 and getattr(other, "elite", False):
                    converted = max(1, int(round(dmg * 0.65)))
                    dmg = max(0, dmg - converted)
                    base_sup = 2 + (1 if converted >= 12 else 0)
                    other.status["压制"] = max(base_sup, other.status.get("压制", 0))
                if dmg <= 0:
                    other.status["压制"] = max(1, other.status.get("压制", 0))
                    hit_n += 1
                    continue

                other.hp -= dmg
                other.status["压制"] = max(1, other.status.get("压制", 0))
                other.clamp()
                print(f"破片溅射：{other.name} 受到波及（-{dmg}）。")
                if other.kind == "突击队" and other.status.get("攀附永续", 0) > 0 and (not other.alive):
                    other.hp = 1
                    other.alive = True
                    other.clamp()
                    print(f"{other.name} 被破片波及仍未被清除：它继续攀附作乱。")
                elif not other.alive:
                    print(f"目标失去战斗力：{other.name} 不再构成威胁。")
                    _record_enemy_destroyed(other, by="player")
                hit_n += 1

        def _apply_he_near_miss(target: EnemyUnit, *, range_tag: str, scale: float = 1.0) -> None:
            """HE 近失爆炸：即便未直击（或被机动化解直击），仍以破片/冲击波造成杀伤。"""
            near_factor = {"close": 0.86, "medium": 0.64, "long": 0.48}.get(range_tag, 0.64)

            if target.armor <= 10:
                # 步兵/火力点：近失就可能直接打散
                base = int(target.hp) + s.rng.randint(18, 55)
                dmg = max(12, int(base * near_factor))
            elif target.armor < 20 or target.kind in ("反坦克炮",):
                # 轻装甲/火炮阵地：可能被震毁或被破片撕裂
                base = s.rng.randint(110, 170)
                absorbed = int(target.armor * 0.10)
                dmg = max(15, int((base - absorbed) * near_factor))
                if s.rng.random() < 0.15:
                    _apply_track_or_stun(target)
            else:
                # 对中重装甲：近失更多表现为压制/轻度震击
                base = s.rng.randint(10, 20) if target.armor < 45 else s.rng.randint(0, 6)
                dmg = int(base * near_factor)

            if dmg > 0 and float(scale) != 1.0:
                dmg = int(round(dmg * float(scale)))

            # 近卫/精英：破片伤害减少，并将削减部分转为压制
            if dmg > 0 and getattr(target, "elite", False):
                converted = max(1, int(round(dmg * 0.65)))
                dmg = max(0, dmg - converted)
                base_sup = 2 + (1 if converted >= 12 else 0)
                target.status["压制"] = max(base_sup, target.status.get("压制", 0))

            if dmg > 0:
                target.hp -= dmg
                print(f"近失爆炸：{target.name} 仍被破片波及（-{dmg}）。")

                # 溅射与破片：还会波及近处（按顺序）的敌人
                _apply_he_splash(center_idx=target_idx, primary_dmg=dmg, range_tag=range_tag, direct_hit=False)

            target.status["压制"] = max(1, target.status.get("压制", 0))
            target.clamp()
            if target.kind == "突击队" and target.status.get("攀附永续", 0) > 0 and (not target.alive):
                target.hp = 1
                target.alive = True
                target.clamp()
                print(f"{target.name} 被爆炸波及仍未被清除：它继续攀附作乱。")
            elif not target.alive:
                print(f"目标失去战斗力：{target.name} 不再构成威胁。")
                _record_enemy_destroyed(target, by="player")

        # 机枪现实机制：弹药偏少/疲劳更容易卡壳（短效）
        if mode == "MG":
            if s.debuffs.get("mg_jam", 0) > 0:
                print("机枪卡壳：你们只能快速排除故障，错过射击窗口。")
                return
            p_jam = 0.0
            if s.mg_ammo <= 12:
                p_jam = float(MG_JAM_CHANCE_LOW_AMMO)
            elif s.mg_ammo <= 24:
                p_jam = float(MG_JAM_CHANCE_MED_AMMO)
            fatigue = int(s.counters.get("fatigue", 0))
            if fatigue >= 60:
                p_jam += float(MG_JAM_FATIGUE_BONUS)
            # 润滑油：降低卡壳概率（按回合递减）
            if s.buffs.get("润滑", 0) > 0:
                p_jam *= 0.55
            if p_jam > 0 and s.rng.random() < min(float(MG_JAM_CHANCE_CAP), p_jam):
                s.debuffs["mg_jam"] = 1
                print("机枪走火不畅：弹链/供弹出现卡滞（本回合机枪失效）。")
                return

        if mode == "MG" and t.kind == "突击队" and (t.status.get("攀附", 0) > 0 or t.status.get("攀附永续", 0) > 0):
            # 需求：若突击队进入“永久攀附”，将无法被驱离/消灭
            if t.status.get("攀附永续", 0) > 0:
                t.status["压制"] = max(2, t.status.get("压制", 0))
                print(f"机枪扫射：你们试图驱离 {t.name}，但对方死死攀住车体（无法清除）。")
            else:
                # 机枪可以把攀附者逼退/打散
                t.status.pop("攀附", None)
                t.status["压制"] = max(2, t.status.get("压制", 0))
                print(f"机枪扫射：你们把 {t.name} 逼退下车体（压制）。")

        hit = s.rng.randint(1, 100) <= base_acc
        if not hit:
            print(f"射击失手：炮弹/弹雨在 {t.name} 附近爆散。")
            if mode == "HE":
                _apply_he_near_miss(t, range_tag=range_tag)
                return

            if mode == "MG":
                t.status["压制"] = max(1, t.status.get("压制", 0))
            return

        # 精英（近卫）：更高机动规避概率（命中后仍可能被其机动/掩体化解）
        if getattr(t, "elite", False):
            evade = 0.12
            if smoke > 0:
                evade += 0.05
            if maneuver > 0:
                evade += 0.06
            if t.status.get("机动受限", 0) > 0:
                evade *= 0.55
            if s.rng.random() < min(0.38, evade):
                print(f"目标机动规避：{t.name} 借助机动与掩体化解了致命打击。")
                # 设计要求：即便目标机动规避直击，HE 破片/冲击波仍可能波及。
                if mode == "HE":
                    _apply_he_near_miss(t, range_tag=range_tag, scale=0.85)
                return

        if mode in ("AP", "HE") and t.kind == "反坦克炮":
            # 反坦克炮阵地：主炮直接命中即可压制/摧毁
            t.hp = 0
            print(f"主炮命中：{t.name} 被一炮击毁，火炮阵地瞬间哑火。")
            t.clamp()
            if not t.alive:
                print(f"目标失去战斗力：{t.name} 不再构成威胁。")
            return

        if mode in ("AP", "HE") and t.kind in ("卡车", "侦察装甲车", "装甲侦察车"):
            # 轻装甲：即便未完全贯穿，也很难继续作战
            t.hp = 0
            print(f"主炮命中：{t.name} 被一炮击毁，轻装甲在爆炸中解体。")
            t.clamp()
            if not t.alive:
                print(f"目标失去战斗力：{t.name} 不再构成威胁。")
            return

        if mode in ("AP", "HE") and t.kind == "装甲车":
            t.hp = 0
            print(f"主炮命中：{t.name} 轻装甲被击穿，迅速失去战斗力。")
            t.clamp()
            if not t.alive:
                print(f"目标失去战斗力：{t.name} 不再构成威胁。")
            return

        if mode == "AP":
            # 更真实的AP：命中面/倾角/距离影响“是否贯穿”，贯穿后才更可能一发致残。
            if t.armor < 20:
                # 对非装甲目标：穿甲主要是“直接打穿掩体”，伤害不如HE，但仍可快速击溃
                dmg = s.rng.randint(int(PLAYER_AP_DAMAGE_SOFT_RANGE[0]), int(PLAYER_AP_DAMAGE_SOFT_RANGE[1]))
                t.hp -= dmg
                print(f"穿甲命中：{t.name} 被打散（-{dmg}）。")
            else:
                prof = _armor_profile_for_target(t)
                aspect = _roll_hit_aspect(maneuver=maneuver, target_suppressed=(t.status.get("压制", 0) > 0))
                base_armor = float(prof[aspect])
                eff_armor = _effective_armor(base=base_armor, slope_deg=float(prof["slope_deg"]), aspect=aspect)
                pen = _player_ap_penetration(range_tag=range_tag)

                # 规则：我方穿甲弹必然击穿除 IS-2 之外的装甲目标
                force_penetrate = (t.kind != "IS-2")

                # 跳弹/未贯穿
                if (not force_penetrate) and (pen < eff_armor):
                    shock = s.rng.randint(10, 20)
                    t.hp -= shock
                    t.status["压制"] = max(1, t.status.get("压制", 0))
                    print(f"穿甲命中：{t.name} 未能贯穿（{aspect}，震击-{shock}）。")
                    _apply_track_or_stun(t)
                else:
                    ratio = pen / max(1.0, eff_armor)
                    # 贯穿：根据超穿比与目标等级决定是否“立刻失能”
                    cls = str(prof.get("class", "unknown"))
                    if cls in ("light", "gun") and ratio >= 1.10:
                        t.hp = 0
                        print(f"贯穿：{t.name}（{aspect}）被一击击毁。")
                    elif cls == "medium" and ratio >= 1.22 and s.rng.random() < 0.72:
                        t.hp = 0
                        print(f"贯穿：{t.name}（{aspect}）内部失能，迅速停火。")
                    elif cls == "heavy" and ratio >= 1.30 and s.rng.random() < 0.70:
                        t.hp = 0
                        print(f"贯穿：{t.name}（{aspect}）遭到致命破坏。")
                    else:
                        dmg = s.rng.randint(int(PLAYER_AP_DAMAGE_PEN_RANGE[0]), int(PLAYER_AP_DAMAGE_PEN_RANGE[1]))
                        # 贯穿仍会受到装甲与隔舱吸收（抽象）
                        dmg = max(80, int(dmg * (float(PLAYER_AP_DAMAGE_PEN_HEAVY_MULT) if cls == "heavy" else 1.0)))
                        t.hp -= dmg
                        t.status["压制"] = max(2, t.status.get("压制", 0))
                        print(f"贯穿命中：{t.name}（{aspect}）受创（-{dmg}）。")
        elif mode == "HE":
            # 高爆弹对步兵/火力点应具备毁灭性杀伤
            if t.armor <= 10:
                # 设计调整：增强我方HE直击的破片溅射/冲击波体感（仅我方炮弹）。
                dmg = t.hp + s.rng.randint(40, 75)
            else:
                # 对装甲：更多是震击/压制与轻度结构损伤
                dmg = s.rng.randint(34, 62)
                absorbed = int(t.armor * 0.10)
                dmg = max(10, dmg - absorbed)
                if s.rng.random() < 0.20:
                    _apply_track_or_stun(t)
            t.hp -= dmg
            t.status["压制"] = max(2, t.status.get("压制", 0))
            print(f"高爆命中：{t.name} 被冲击波压住（-{dmg}，压制）。")

            # 溅射与破片：还会波及近处（按顺序）的敌人
            _apply_he_splash(center_idx=target_idx, primary_dmg=dmg, range_tag=range_tag, direct_hit=True)
        else:  # MG
            # 机枪对非装甲目标造成更高压制与伤害（距离大幅影响伤害）
            if range_tag == "close":
                dmg = s.rng.randint(24, 40)
            elif range_tag == "medium":
                dmg = s.rng.randint(16, 28)
            else:  # long
                dmg = s.rng.randint(8, 16)
            t.hp -= dmg
            t.status["压制"] = max(2, t.status.get("压制", 0))
            print(f"机枪命中：{t.name} 被迫缩回掩体（-{dmg}，压制）。")

        t.clamp()
        if t.kind == "突击队" and t.status.get("攀附永续", 0) > 0 and (not t.alive):
            # 不允许被消灭：最低保留1HP
            t.hp = 1
            t.alive = True
            t.clamp()
            print(f"{t.name} 在火力中仍死死攀附：你们无法彻底清除它。")
            return
        if not t.alive:
            print(f"目标失去战斗力：{t.name} 不再构成威胁。")
            _record_enemy_destroyed(t, by="player")

    def _select_target_menu(enemies: List[EnemyUnit], *, hint: str) -> int:
        alive = [e for e in enemies if e.alive]
        if not alive:
            return 0
        menu: Dict[str, str] = {"0": "取消瞄准/返回"}
        for i, e in enumerate(enemies, 1):
            if not e.alive:
                continue
            status = "压制" if e.status.get("压制", 0) > 0 else ""
            armor = f" 装甲{e.armor}" if e.armor > 0 else ""
            menu[str(i)] = f"{e.name}（HP{e.hp}{armor} {status}）"
        default = "1" if "1" in menu else (sorted(k for k in menu.keys() if k != "0")[0])
        raw = choose(ins, f"{hint}(0返回)：", menu, default=default)
        if raw == "0":
            return -1
        try:
            idx = int(raw) - 1
        except ValueError:
            idx = int(default) - 1
        return max(0, min(len(enemies) - 1, idx))

    s.counters["encounters"] = s.counters.get("encounters", 0) + 1
    s.battles_this_round += 1

    # 叙述保留（不再区分BOSS战）
    title = "街巷遭遇"
    location_desc = "瓦砾与断墙之间遭遇突发交火。"
    narrate(
        f"""
【{title}】
{location_desc}
你需要做的不是‘赢’，而是把损失压到最低。
"""
    )

    enemies = _generate_enemies()
    print("\n敌情判断：")
    for e in enemies:
        tag = "装甲" if e.armor >= 20 else "步兵火力"
        print(f"- {e.name}（{tag}）")

    # --- 友军：驻军支援/驻军遇袭（与玩家并肩作战）
    # 1) 玩家呼叫的“驻军支援”：战斗后返回原辖区
    deployed_pairs = list(s.deployed_garrison)
    s.deployed_garrison.clear()
    deployed_units: List[GarrisonUnit] = [u for _, u in deployed_pairs if u.alive]

    # 2) 事件触发带入的驻军友军：属于当前辖区驻军，不需要“归队”处理
    extra_allies: List[GarrisonUnit] = []
    if garrison_allies:
        extra_allies = [u for u in garrison_allies if getattr(u, "alive", True)]

    # 合并去重（同一个对象可能被重复传入）
    allies: List[GarrisonUnit] = []
    seen_ids: set[int] = set()
    for u in deployed_units + extra_allies:
        if id(u) in seen_ids:
            continue
        seen_ids.add(id(u))
        allies.append(u)

    def _alive_allies() -> List[GarrisonUnit]:
        return [u for u in allies if u.alive]

    def _post_battle_medic_support() -> None:
        """医疗组战后处置：治疗友军步兵与车组成员（战后）。

        规则：仅当本场参战友军中存在存活“医疗组”时触发。
        """
        try:
            has_medic = any(getattr(u, "alive", False) and getattr(u, "unit_type", "") == "医疗组" for u in allies)
        except Exception:
            has_medic = False
        if not has_medic:
            return

        healed_units = 0
        healed_crew = 0

        # 治疗参战友军（步兵/火力点等驻军单位）：小幅回血
        for u in allies:
            if not getattr(u, "alive", False):
                continue
            if str(getattr(u, "unit_type", "") or "") == "医疗组":
                continue
            before = int(getattr(u, "hp", 0) or 0)
            if before < 180:
                u.hp = min(180, before + 12)
                if int(getattr(u, "hp", 0) or 0) > before:
                    healed_units += 1
            # 稳住士气（不计入“治疗人数”）
            try:
                u.morale = min(100, int(getattr(u, "morale", 50) or 50) + 2)
            except Exception:
                pass
            try:
                u.clamp()
            except Exception:
                pass

        # 治疗车组成员：小幅回血 + 减压
        for m in getattr(s, "crew", []) or []:
            if not getattr(m, "alive", False):
                continue
            before_hp = int(getattr(m, "hp", 0) or 0)
            before_stress = int(getattr(m, "stress", 0) or 0)
            if before_hp < 100:
                m.hp = min(100, before_hp + 8)
            if before_stress > 0:
                m.stress = max(0, before_stress - 8)
            if int(getattr(m, "hp", 0) or 0) > before_hp or int(getattr(m, "stress", 0) or 0) < before_stress:
                healed_crew += 1
            try:
                m.clamp()
            except Exception:
                pass

        if healed_units > 0 or healed_crew > 0:
            print(f"【战后】医疗组处置伤情：步兵{healed_units}人、乘员{healed_crew}人。")

    def _ally_prefers_armored(u: GarrisonUnit) -> bool:
        return u.unit_type in ("反坦克组", "反坦克炮", "88炮")

    def _ally_cd_tick_and_get(u: GarrisonUnit, key: str) -> int:
        cds = getattr(u, "_skill_cds", None)
        if not isinstance(cds, dict):
            cds = {}
            setattr(u, "_skill_cds", cds)
        try:
            return int(cds.get(key, 0) or 0)
        except Exception:
            return 0

    def _ally_cd_set(u: GarrisonUnit, key: str, value: int) -> None:
        cds = getattr(u, "_skill_cds", None)
        if not isinstance(cds, dict):
            cds = {}
            setattr(u, "_skill_cds", cds)
        cds[key] = max(0, int(value))

    def _ally_tick_all_cds(units: List[GarrisonUnit]) -> None:
        for u in units:
            cds = getattr(u, "_skill_cds", None)
            if not isinstance(cds, dict) or not cds:
                continue
            for k in list(cds.keys()):
                try:
                    cds[k] = max(0, int(cds.get(k, 0) or 0) - 1)
                except Exception:
                    cds[k] = 0

    def _ally_try_skill(u: GarrisonUnit, enemies: List[EnemyUnit]) -> bool:
        nonlocal support_turns, maneuver_turns
        if not getattr(u, "alive", False):
            return False

        def _alive_enemy_idxs(*, armored: Optional[bool] = None) -> List[int]:
            idxs: List[int] = []
            for i, e in enumerate(enemies):
                if not getattr(e, "alive", False):
                    continue
                if armored is True and int(getattr(e, "armor", 0) or 0) < 20:
                    continue
                if armored is False and int(getattr(e, "armor", 0) or 0) >= 20:
                    continue
                idxs.append(i)
            return idxs

        def _pick_enemy(*, armored: Optional[bool] = None, prefer_elite: bool = False) -> Optional[EnemyUnit]:
            idxs = _alive_enemy_idxs(armored=armored)
            if not idxs:
                return None
            if prefer_elite:
                elites = [i for i in idxs if bool(getattr(enemies[i], "elite", False))]
                if elites:
                    idxs = elites
            return enemies[s.rng.choice(idxs)]

        # 反坦克组：对装甲/火炮做一次高伤害齐射（带冷却）
        if u.unit_type == "反坦克组":
            if _ally_cd_tick_and_get(u, "AT齐射") > 0:
                return False
            armored_idxs = [i for i, e in enumerate(enemies) if e.alive and e.armor >= 20]
            if not armored_idxs:
                return False
            tidx = s.rng.choice(armored_idxs)
            t = enemies[tidx]

            p = 0.48
            if range_tag == "close":
                p += 0.10
            elif range_tag == "long":
                p -= 0.14
            if smoke_turns > 0:
                p -= 0.05
            p = max(0.18, min(0.70, p))
            if s.rng.random() >= p:
                _ally_cd_set(u, "AT齐射", 2)
                return False

            dmg = s.rng.randint(48, 90)
            if str(getattr(t, "kind", "")) == "IS-2":
                dmg = int(dmg * 0.75)
            t.hp -= dmg
            t.status["压制"] = max(2, t.status.get("压制", 0))
            if s.rng.random() < 0.30:
                t.status["机动受限"] = max(1, t.status.get("机动受限", 0))
            print(f"反坦克齐射：{u.name} 打击 {t.name}（-{dmg}，压制）。")
            t.clamp()
            if not t.alive:
                print(f"目标失去战斗力：{t.name} 被反坦克火力击毁。")
                _record_enemy_destroyed(t, by="garrison")
            _ally_cd_set(u, "AT齐射", 3)
            return True

        # 反坦克炮：对装甲目标进行一次“直射”（更高伤害，冷却更长）
        if u.unit_type == "反坦克炮":
            if _ally_cd_tick_and_get(u, "直射") > 0:
                return False
            t = _pick_enemy(armored=True)
            if t is None:
                return False
            p = 0.40
            if range_tag == "close":
                p += 0.10
            elif range_tag == "long":
                p -= 0.14
            if smoke_turns > 0:
                p -= 0.05
            p = max(0.14, min(0.62, p))
            if s.rng.random() >= p:
                _ally_cd_set(u, "直射", 2)
                return False
            dmg = s.rng.randint(64, 110)
            if str(getattr(t, "kind", "")) == "IS-2":
                dmg = int(dmg * 0.70)
            t.hp -= dmg
            t.status["压制"] = max(2, t.status.get("压制", 0))
            if s.rng.random() < 0.40:
                _apply_track_or_stun(t)
            print(f"反坦克炮直射：{u.name} 命中 {t.name}（-{dmg}）。")
            t.clamp()
            if not t.alive:
                print(f"目标失去战斗力：{t.name} 被反坦克炮击毁。")
                _record_enemy_destroyed(t, by="garrison")
            _ally_cd_set(u, "直射", 4)
            return True

        # 党卫军：手榴弹突击（优先软目标），造成更高压制（带冷却）
        if u.unit_type == "党卫军":
            if _ally_cd_tick_and_get(u, "手榴弹") > 0:
                return False
            soft_idxs = [i for i, e in enumerate(enemies) if e.alive and e.armor < 20]
            if not soft_idxs:
                return False
            tidx = s.rng.choice(soft_idxs)
            t = enemies[tidx]

            p = 0.52
            if range_tag == "close":
                p += 0.16
            elif range_tag == "long":
                p -= 0.18
            if smoke_turns > 0:
                p += 0.05
            if maneuver_turns > 0:
                p -= 0.04
            p = max(0.14, min(0.78, p))
            if s.rng.random() >= p:
                _ally_cd_set(u, "手榴弹", 2)
                return False

            if range_tag == "close":
                dmg = s.rng.randint(26, 44)
            elif range_tag == "medium":
                dmg = s.rng.randint(18, 34)
            else:
                dmg = s.rng.randint(12, 26)
            t.hp -= dmg
            t.status["压制"] = max(3, t.status.get("压制", 0))
            print(f"手榴弹突击：{u.name} 压制 {t.name}（-{dmg}，压制）。")
            t.clamp()
            if not t.alive:
                print(f"目标失去战斗力：{t.name} 被爆炸与破片打散。")
                _record_enemy_destroyed(t, by="garrison")
            _ally_cd_set(u, "手榴弹", 3)
            return True

        # 国防军：战术点射（对当前威胁目标造成稳定伤害与压制）
        if u.unit_type == "国防军":
            if _ally_cd_tick_and_get(u, "点射") > 0:
                return False
            t = _pick_enemy(armored=None)
            if t is None:
                return False
            p = 0.40
            if range_tag == "long":
                p -= 0.10
            if smoke_turns > 0:
                p -= 0.04
            p = max(0.18, min(0.70, p))
            if s.rng.random() >= p:
                _ally_cd_set(u, "点射", 1)
                return False
            dmg = s.rng.randint(10, 18) if int(getattr(t, "armor", 0) or 0) < 20 else s.rng.randint(3, 7)
            t.hp -= dmg
            t.status["压制"] = max(2, t.status.get("压制", 0))
            print(f"战术点射：{u.name} 压制 {t.name}（-{dmg}，压制）。")
            t.clamp()
            if not t.alive:
                print(f"目标失去战斗力：{t.name} 被火力压垮。")
                _record_enemy_destroyed(t, by="garrison")
            _ally_cd_set(u, "点射", 2)
            return True

        # 国民冲锋队：人海冲锋（主要制造压制，对软目标更有效）
        if u.unit_type == "国民冲锋队":
            if _ally_cd_tick_and_get(u, "冲锋") > 0:
                return False
            t = _pick_enemy(armored=False)
            if t is None:
                return False
            p = 0.32
            if range_tag == "close":
                p += 0.14
            elif range_tag == "long":
                p -= 0.16
            if smoke_turns > 0:
                p += 0.06
            p = max(0.12, min(0.75, p))
            if s.rng.random() >= p:
                _ally_cd_set(u, "冲锋", 1)
                return False
            dmg = s.rng.randint(10, 22)
            t.hp -= dmg
            t.status["压制"] = max(3, t.status.get("压制", 0))
            print(f"人海冲锋：{u.name} 贴近压制 {t.name}（-{dmg}，压制）。")
            t.clamp()
            if not t.alive:
                print(f"目标失去战斗力：{t.name} 被混乱冲击打散。")
                _record_enemy_destroyed(t, by="garrison")
            _ally_cd_set(u, "冲锋", 3)
            return True

        # 工兵：爆破包（对装甲/火力点更有效，附带故障/机动受限）
        if u.unit_type == "工兵":
            if _ally_cd_tick_and_get(u, "爆破") > 0:
                return False
            t = _pick_enemy(armored=None)
            if t is None:
                return False
            p = 0.28
            if range_tag == "close":
                p += 0.18
            elif range_tag == "long":
                p -= 0.18
            if smoke_turns > 0:
                p += 0.06
            if maneuver_turns > 0:
                p -= 0.04
            p = max(0.10, min(0.78, p))
            if s.rng.random() >= p:
                _ally_cd_set(u, "爆破", 1)
                return False
            if int(getattr(t, "armor", 0) or 0) >= 20:
                dmg = s.rng.randint(36, 68)
                if str(getattr(t, "kind", "")) == "IS-2":
                    dmg = int(dmg * 0.78)
                t.hp -= dmg
                if s.rng.random() < 0.55:
                    _apply_track_or_stun(t)
                t.status["压制"] = max(2, t.status.get("压制", 0))
                print(f"爆破包命中：{u.name} 破坏 {t.name}（-{dmg}）。")
            else:
                dmg = s.rng.randint(18, 34)
                t.hp -= dmg
                t.status["压制"] = max(3, t.status.get("压制", 0))
                print(f"爆破包投掷：{u.name} 震慑 {t.name}（-{dmg}，压制）。")
            t.clamp()
            if not t.alive:
                print(f"目标失去战斗力：{t.name} 被爆破摧毁。")
                _record_enemy_destroyed(t, by="garrison")
            _ally_cd_set(u, "爆破", 3)
            return True

        # 狙击组：点杀（优先精英软目标）
        if u.unit_type == "狙击组":
            if _ally_cd_tick_and_get(u, "点杀") > 0:
                return False
            t = _pick_enemy(armored=False, prefer_elite=True)
            if t is None:
                return False
            p = 0.44
            if range_tag == "long":
                p += 0.06
            if smoke_turns > 0:
                p -= 0.04
            p = max(0.18, min(0.82, p))
            if s.rng.random() >= p:
                _ally_cd_set(u, "点杀", 1)
                return False
            dmg = s.rng.randint(24, 44)
            if bool(getattr(t, "elite", False)):
                dmg = int(dmg * 0.85)
            t.hp -= dmg
            t.status["压制"] = max(2, t.status.get("压制", 0))
            print(f"狙击点杀：{u.name} 命中 {t.name}（-{dmg}）。")
            t.clamp()
            if not t.alive:
                print(f"目标失去战斗力：{t.name} 被狙击击溃。")
                _record_enemy_destroyed(t, by="garrison")
            _ally_cd_set(u, "点杀", 3)
            return True

        # 侦察组：照明/引导（提升我方协同与机动窗口）
        if u.unit_type == "侦察组":
            if _ally_cd_tick_and_get(u, "引导") > 0:
                return False
            p = 0.42
            if smoke_turns > 0:
                p -= 0.04
            p = max(0.22, min(0.72, p))
            if s.rng.random() >= p:
                _ally_cd_set(u, "引导", 1)
                return False
            support_turns = max(support_turns, 1)
            maneuver_turns = max(maneuver_turns, 1)
            print(f"侦察引导：{u.name} 标记目标区（掩护+1，机动+1）。")
            _ally_cd_set(u, "引导", 3)
            return True

        # 机枪队：压制扫射（命中 1-2 个软目标）
        if u.unit_type == "机枪队":
            if _ally_cd_tick_and_get(u, "扫射") > 0:
                return False
            idxs = _alive_enemy_idxs(armored=False)
            if not idxs:
                return False
            burst = 2 if len(idxs) >= 2 and s.rng.random() < 0.55 else 1
            pick = s.rng.sample(idxs, k=burst) if len(idxs) >= burst else idxs
            p = 0.46
            if range_tag == "long":
                p -= 0.10
            if smoke_turns > 0:
                p -= 0.04
            p = max(0.18, min(0.80, p))
            if s.rng.random() >= p:
                _ally_cd_set(u, "扫射", 1)
                return False
            print(f"机枪扫射：{u.name} 对步兵火力点实施压制。")
            for i in pick:
                t = enemies[i]
                if not t.alive:
                    continue
                dmg = s.rng.randint(14, 26) if range_tag != "long" else s.rng.randint(10, 20)
                t.hp -= dmg
                t.status["压制"] = max(3, t.status.get("压制", 0))
                t.clamp()
                print(f"- 压制命中：{t.name}（-{dmg}，压制）。")
                if not t.alive:
                    print(f"目标失去战斗力：{t.name} 被机枪火力打散。")
                    _record_enemy_destroyed(t, by="garrison")
            _ally_cd_set(u, "扫射", 3)
            return True

        # 88炮：致命直射（对装甲极高伤害，冷却较长）
        if u.unit_type == "88炮":
            if _ally_cd_tick_and_get(u, "致命直射") > 0:
                return False
            t = _pick_enemy(armored=True)
            if t is None:
                return False
            p = 0.30
            if range_tag == "medium":
                p += 0.04
            if range_tag == "close":
                p += 0.06
            if smoke_turns > 0:
                p -= 0.05
            p = max(0.12, min(0.62, p))
            if s.rng.random() >= p:
                _ally_cd_set(u, "致命直射", 2)
                return False
            dmg = s.rng.randint(88, 140)
            if str(getattr(t, "kind", "")) == "IS-2":
                dmg = int(dmg * 0.78)
            t.hp -= dmg
            t.status["压制"] = max(2, t.status.get("压制", 0))
            print(f"88炮直射：{u.name} 击穿 {t.name}（-{dmg}）。")
            t.clamp()
            if not t.alive:
                print(f"目标失去战斗力：{t.name} 被88炮击毁。")
                _record_enemy_destroyed(t, by="garrison")
            _ally_cd_set(u, "致命直射", 5)
            return True

        # 医疗组：急救（为受创友军恢复部分HP，并稳住士气）
        if u.unit_type == "医疗组":
            if _ally_cd_tick_and_get(u, "急救") > 0:
                return False
            wounded = [x for x in allies if getattr(x, "alive", False) and x.unit_type != "医疗组" and int(getattr(x, "hp", 0) or 0) <= 60]
            if not wounded:
                return False
            p = 0.55
            p = max(0.20, min(0.85, p))
            if s.rng.random() >= p:
                _ally_cd_set(u, "急救", 1)
                return False
            tgt = s.rng.choice(wounded)
            heal = s.rng.randint(16, 28)
            tgt.hp += heal
            tgt.morale = min(100, int(getattr(tgt, "morale", 50) or 50) + 6)
            tgt.clamp()
            s.morale += 1
            print(f"战地急救：{u.name} 处理 {tgt.name} 伤势（HP+{heal}）。")
            _ally_cd_set(u, "急救", 3)
            return True

        return False

    medic_used = False

    def _allies_act(enemies: List[EnemyUnit]) -> None:
        nonlocal medic_used
        live = _alive_allies()
        if not live:
            return

        # 每回合开始：递减友军技能冷却
        _ally_tick_all_cds(live)

        # 医疗组：提供战斗中的稳定（不攻击）
        if any(u.unit_type == "医疗组" for u in live):
            # 每场战斗只触发一次，避免数值膨胀
            if not medic_used:
                medic_used = True
                s.morale += 1
                s.damage = max(0, s.damage - 1)
                print("友军医疗组稳住了局面：士气+1，损伤-1。")

        attackers = [u for u in live if u.unit_type != "医疗组"]
        if not attackers:
            return

        # 友军技能：每回合最多触发 1 次（避免刷屏/失衡）
        skill_fired = False
        for u0 in list(attackers):
            if skill_fired:
                break
            try:
                if _ally_try_skill(u0, enemies):
                    skill_fired = True
            except Exception:
                pass

        # 每回合最多 2 次友军有效射击，避免刷屏
        shots = min(2, len(attackers))
        for _ in range(shots):
            u = s.rng.choice(attackers)
            tidx = _pick_target(enemies, prefer_armored=_ally_prefers_armored(u))
            if tidx is None:
                return
            t = enemies[tidx]
            if not t.alive:
                continue

            acc = 35 + int(u.power * 2.0) + int((u.morale - 50) * 0.2)
            if u.unit_type == "狙击组":
                acc += 10
            if u.unit_type == "机枪队":
                acc += 6
            if u.unit_type == "88炮":
                acc += 8
            if _ally_prefers_armored(u) and t.armor >= 20:
                acc -= 6
            acc = max(12, min(85, acc))

            if s.rng.randint(1, 100) > acc:
                continue

            # 伤害与压制：仍偏向“牵制”，但 88 炮会打出极高伤害
            if u.unit_type == "88炮":
                if t.armor >= 20:
                    dmg = s.rng.randint(60, 95)
                    t.status["压制"] = max(2, t.status.get("压制", 0))
                else:
                    dmg = s.rng.randint(26, 48)
                    t.status["压制"] = max(1, t.status.get("压制", 0))
            elif u.unit_type == "机枪队":
                if t.armor >= 20:
                    dmg = s.rng.randint(2, 6)
                else:
                    dmg = s.rng.randint(8, 14)
                t.status["压制"] = max(2, t.status.get("压制", 0))
            elif _ally_prefers_armored(u) and t.armor >= 20:
                dmg = s.rng.randint(9, 16) + max(0, (u.power - 10) // 3)
                t.status["压制"] = max(1, t.status.get("压制", 0))
            elif u.unit_type == "狙击组":
                dmg = s.rng.randint(6, 12)
                t.status["压制"] = max(1, t.status.get("压制", 0))
            else:
                dmg = s.rng.randint(4, 10)
                t.status["压制"] = max(1, t.status.get("压制", 0))
            t.hp -= dmg
            print(f"友军火力命中：{u.name} 牵制 {t.name}（-{dmg}，压制）。")
            t.clamp()
            if not t.alive:
                print(f"目标失去战斗力：{t.name} 被友军火力压垮。")
                _record_enemy_destroyed(t, by="garrison")

    if allies:
        print("\n友军加入战斗：" + "、".join(u.name for u in allies if u.alive))

    # --- 友军坦克（持久化跟随）
    tank_allies: List[TankAlly] = [t for t in getattr(s, "tank_allies", []) if t.alive]
    if tank_allies:
        print("友军装甲加入：" + "、".join(t.name for t in tank_allies))

    # --- Sd.Kfz.251：战斗一开始就放下步兵（不再随机延迟）
    for t in tank_allies:
        if not _is_sdkfz(t):
            continue
        try:
            setattr(t, "_sdkfz_unloaded", False)
            setattr(t, "_sdkfz_unloaded_units", [])
            setattr(t, "_sdkfz_unload_turn", 1)
            _sdkfz_ensure_cargo_units(t)
        except Exception:
            pass
    for t in tank_allies:
        if _is_sdkfz(t):
            try:
                _sdkfz_unload_infantry_now(t, announce=True)
            except Exception:
                pass

    # 战斗开始快照：用于“零损失”成就判定
    crew_lost_start = int(getattr(s, "crew_lost", 0))

    def _is_armored_target(e: EnemyUnit) -> bool:
        # 装甲目标：坦克/自走炮等（用 kind + armor 双判定）
        if e.kind in ("T-34", "IS-2", "SU-76"):
            return True
        return int(getattr(e, "armor", 0)) >= 20

    def _record_enemy_destroyed(e: EnemyUnit, *, by: str) -> None:
        # 并立成就计数：只要是我方体系造成击毁都计入（玩家/驻军/友军坦克）
        s.counters["enemy_kills"] = int(s.counters.get("enemy_kills", 0)) + 1
        # 设计：击杀“近卫”（精英）敌人奖励 1 胜利点（每个单位只结算一次）。
        if bool(getattr(e, "elite", False)) and (not bool(getattr(e, "_elite_vp_awarded", False))):
            setattr(e, "_elite_vp_awarded", True)
            s.victory_points += 1
            print("【战果】击溃近卫单位：胜利点+1。")
        if _is_armored_target(e):
            s.counters["enemy_tank_kills"] = int(s.counters.get("enemy_tank_kills", 0)) + 1
        if e.kind == "IS-2":
            s.counters["enemy_is2_kills"] = int(s.counters.get("enemy_is2_kills", 0)) + 1
        if e.kind == "反坦克炮":
            s.counters["enemy_at_gun_kills"] = int(s.counters.get("enemy_at_gun_kills", 0)) + 1

        newly = check_and_unlock_achievements(s)
        if newly:
            print("\n【成就解锁】" + "、".join(newly))

    # 战斗开始时的存活快照：用于避免“战斗中已被击毁”后又叠加战后随机损失
    battle_start_alive_ally_ids: set[int] = {id(u) for u in allies if u.alive}
    battle_start_alive_tank_ids: set[int] = {id(t) for t in tank_allies if t.alive}

    def _tank_allies_act(enemies: List[EnemyUnit], *, range_tag: str, smoke: int, maneuver: int) -> None:
        live = [t for t in tank_allies if t.alive]
        if not live:
            return

        # 防空坦克可尝试驱离敌机
        nonlocal air_kind, air_turns_left

        def _ally_model_profile(model: str) -> Dict[str, object]:
            if model == "豹式坦克":
                # 豹式：穿透良好、伤害稳定
                return {"pen": {"close": 92.0, "medium": 84.0, "long": 76.0}, "dmg": (80, 130)}
            if model in ("四号坦克", "突击炮III"):
                return {"pen": {"close": 70.0, "medium": 62.0, "long": 54.0}, "dmg": (46, 74)}
            if model == "斐迪南突击炮":
                # 重突：极高穿深但命中/机动较差
                return {"pen": {"close": 98.0, "medium": 90.0, "long": 82.0}, "dmg": (78, 120)}
            if model == "Sd.Kfz.251装甲运兵车":
                return {"pen": {"close": 20.0, "medium": 16.0, "long": 12.0}, "dmg": (12, 26)}
            if model in ("防空坦克", "四号防空坦克"):
                return {"pen": {"close": 36.0, "medium": 28.0, "long": 22.0}, "dmg": (28, 48)}
            if model == "虎式坦克":
                return {"pen": {"close": 102.0, "medium": 94.0, "long": 84.0}, "dmg": (92, 150)}
            return {"pen": {"close": 82.0, "medium": 75.0, "long": 66.0}, "dmg": (52, 86)}

        def _ally_ap_penetration(model: str) -> float:
            prof = _ally_model_profile(model)
            pen_map = prof.get("pen")
            base = 75.0
            if isinstance(pen_map, dict):
                base = float(pen_map.get(range_tag, 75.0))
            return base * s.rng.uniform(0.92, 1.08)

        for tnk in live:
            model = str(getattr(tnk, "model", "友军坦克"))

            # 弹药检查：主炮/机枪弹检查（按车型区分）
            try:
                shells = int(getattr(tnk, "shells", 0) or 0)
            except Exception:
                shells = 0
            try:
                mg = int(getattr(tnk, "mg_ammo", 0) or 0)
            except Exception:
                mg = 0
            # 若为以机枪为主的型号（Sd.Kfz/防空坦克），按机枪弹判断；否则按主炮弹判断
            if model in ("Sd.Kfz.251装甲运兵车", "防空坦克", "四号防空坦克"):
                if mg <= 0:
                    if not bool(getattr(tnk, "_no_shells_notice", False)):
                        setattr(tnk, "_no_shells_notice", True)
                        print(f"友军装甲：{tnk.name} 弹药耗尽，无法开火。")
                    # 仍允许 Sd.Kfz 放下增援（即使机枪弹耗尽）
                    if model != "Sd.Kfz.251装甲运兵车":
                        continue
            else:
                if shells <= 0:
                    if not bool(getattr(tnk, "_no_shells_notice", False)):
                        setattr(tnk, "_no_shells_notice", True)
                        print(f"友军装甲：{tnk.name} 弹药耗尽，无法开火。")
                    continue

            # 旧名兼容：战斗中统一显示为新名称
            if model == "突击炮III":
                model = "四号坦克"
                tnk.model = model
                try:
                    tnk.name = str(getattr(tnk, "name", "")).replace("突击炮III", model)
                except Exception:
                    pass
            if model == "四号防空坦克":
                model = "防空坦克"
                tnk.model = model
                try:
                    tnk.name = str(getattr(tnk, "name", "")).replace("四号防空坦克", model)
                except Exception:
                    pass

            # Sd.Kfz.251：会放下反坦克组与党卫军（一次），并以机枪压制步兵
            if model == "Sd.Kfz.251装甲运兵车":
                unload_turn = getattr(tnk, "_sdkfz_unload_turn", None)
                if not isinstance(unload_turn, int):
                    unload_turn = 1
                if (not bool(getattr(tnk, "_sdkfz_unloaded", False))) and (tno >= int(unload_turn)):
                    setattr(tnk, "_sdkfz_unloaded", True)
                    cargo = getattr(tnk, "_sdkfz_cargo_units", None)
                    if isinstance(cargo, list) and cargo:
                        joined: List[GarrisonUnit] = []
                        for u in cargo:
                            if getattr(u, "alive", True):
                                if not bool(getattr(u, "_from_sdkfz", False)):
                                    setattr(u, "_from_sdkfz", True)
                                allies.append(u)
                                joined.append(u)
                        setattr(tnk, "_sdkfz_unloaded_units", joined)
                        if joined:
                            print("Sd.Kfz.251 抵达并放下载员增援：" + "、".join(u.name for u in joined) + " 加入战斗。")

                infantry_idxs = [i for i, e in enumerate(enemies) if e.alive and e.armor < 20]
                if infantry_idxs:
                    # 机枪压制也视作消耗该车机枪弹储备（按 MG_FIRE_COST）
                    try:
                        cur = int(getattr(tnk, "mg_ammo", 0) or 0)
                        dec = int(MG_FIRE_COST)
                        setattr(tnk, "mg_ammo", max(0, cur - dec))
                        if hasattr(tnk, "clamp"):
                            tnk.clamp()
                    except Exception:
                        pass
                    tidx = s.rng.choice(infantry_idxs)
                    tgt = enemies[tidx]
                    dmg = s.rng.randint(16, 30) if range_tag != "long" else s.rng.randint(10, 20)
                    tgt.hp -= dmg
                    tgt.status["压制"] = max(2, tgt.status.get("压制", 0))
                    print(f"装甲运兵车火力：{tnk.name} 机枪压制 {tgt.name}（-{dmg}，压制）。")
                    tgt.clamp()
                    if not tgt.alive:
                        print(f"目标失去战斗力：{tgt.name} 被装甲运兵车火力击溃。")
                        _record_enemy_destroyed(tgt, by="tank_ally")
                continue

            # 防空坦克：优先驱离敌机；否则对步兵进行大范围溅射杀伤
            if model in ("防空坦克", "四号防空坦克"):
                # 防空行动消耗机枪弹（按 AA_MG_COST）
                try:
                    cur = int(getattr(tnk, "mg_ammo", 0) or 0)
                    if cur < int(AA_MG_COST):
                        continue
                    setattr(tnk, "mg_ammo", max(0, cur - int(AA_MG_COST)))
                    if hasattr(tnk, "clamp"):
                        tnk.clamp()
                except Exception:
                    pass
                if air_kind and air_turns_left > 0:
                    p_drive = 0.42
                    if smoke > 0:
                        p_drive -= 0.08
                    if s.debuffs.get("optics_broken", 0) > 0:
                        p_drive -= 0.06
                    p_drive = max(0.18, min(0.70, p_drive))
                    if s.rng.random() < p_drive:
                        print(f"防空火力奏效：{tnk.name} 逼退敌机，上空暂时清静。")
                        air_kind = None
                        air_turns_left = 0
                    else:
                        print(f"{tnk.name} 防空火力扫射上空，但敌机仍在盘旋。")

                infantry_idxs = [i for i, e in enumerate(enemies) if e.alive and e.armor < 20]
                if infantry_idxs:
                    burst = min(len(infantry_idxs), 3)
                    pick = s.rng.sample(infantry_idxs, k=burst) if len(infantry_idxs) >= burst else infantry_idxs
                    print(f"防空炮扫射：{tnk.name} 对步兵火力点进行覆盖打击。")
                    for tidx in pick:
                        tgt = enemies[tidx]
                        if not tgt.alive:
                            continue
                        dmg = s.rng.randint(22, 42) if range_tag != "long" else s.rng.randint(16, 32)
                        tgt.hp -= dmg
                        tgt.status["压制"] = max(2, tgt.status.get("压制", 0))
                        tgt.clamp()
                        print(f"- 溅射命中：{tgt.name}（-{dmg}，压制）。")
                        if not tgt.alive:
                            print(f"目标失去战斗力：{tgt.name} 被防空炮火力扫平。")
                            _record_enemy_destroyed(tgt, by="tank_ally")
                continue

            # 其他坦克：每回合一次射击，消耗 1 发弹药（命中与否都视作消耗）
            try:
                cur = int(getattr(tnk, "shells", 0) or 0)
            except Exception:
                cur = 0
            if cur <= 0:
                continue
            try:
                setattr(tnk, "shells", max(0, cur - 1))
                if hasattr(tnk, "clamp"):
                    tnk.clamp()
            except Exception:
                pass

            tidx = _pick_target(enemies, prefer_armored=True)
            if tidx is None:
                return
            tgt = enemies[tidx]
            if not tgt.alive:
                continue

            acc = int(tnk.accuracy)
            if smoke > 0:
                acc -= 6
            if maneuver > 0:
                acc += 2
            acc = max(12, min(85, acc))

            if s.rng.randint(1, 100) > acc:
                continue

            # 友军坦克默认优先使用“穿甲”对装甲目标；对软目标按“高爆”处理
            if tgt.armor >= 20:
                prof = _armor_profile_for_target(tgt)
                aspect = _roll_hit_aspect(maneuver=maneuver, target_suppressed=(tgt.status.get("压制", 0) > 0))
                base_armor = float(prof[aspect])
                eff_armor = _effective_armor(base=base_armor, slope_deg=float(prof["slope_deg"]), aspect=aspect)
                pen = _ally_ap_penetration(model)

                # 需求：斐迪南一炮击毁任何敌方单位（命中即击毁）
                if model == "斐迪南突击炮":
                    tgt.hp = 0
                    tgt.status["压制"] = max(2, tgt.status.get("压制", 0))
                    print(f"斐迪南命中：{tnk.name} 直接击毁 {tgt.name}（{aspect}）。")
                elif pen < eff_armor:
                    shock = s.rng.randint(4, 10)
                    tgt.hp -= shock
                    tgt.status["压制"] = max(1, tgt.status.get("压制", 0))
                    _apply_track_or_stun(tgt)
                    print(f"友军坦克命中：{tnk.name} 未贯穿 {tgt.name}（{aspect}，震击-{shock}）。")
                else:
                    dmg_rng = _ally_model_profile(model).get("dmg")
                    if isinstance(dmg_rng, tuple) and len(dmg_rng) == 2:
                        dmg = s.rng.randint(int(dmg_rng[0]), int(dmg_rng[1]))
                    else:
                        dmg = s.rng.randint(52, 86)
                    if str(prof.get("class")) == "heavy":
                        dmg = max(30, int(dmg * 0.85))
                    tgt.hp -= dmg
                    tgt.status["压制"] = max(2, tgt.status.get("压制", 0))
                    print(f"友军坦克贯穿：{tnk.name} 命中 {tgt.name}（{aspect}，-{dmg}）。")
            else:
                if model == "斐迪南突击炮":
                    dmg = tgt.hp + 999
                else:
                    dmg = tgt.hp + s.rng.randint(18, 40)
                tgt.hp -= dmg
                tgt.status["压制"] = max(2, tgt.status.get("压制", 0))
                print(f"友军坦克高爆：{tnk.name} 压垮 {tgt.name}（-{dmg}）。")

            tgt.clamp()
            if not tgt.alive:
                print(f"目标失去战斗力：{tgt.name} 被友军装甲击溃。")
                _record_enemy_destroyed(tgt, by="tank_ally")

    # 战斗状态
    smoke_turns = 0
    maneuver_turns = 0
    support_turns = 0

    # 距离档位：开局由地形决定；可通过战术机动调整为近/远。
    battle_terrain = _terrain_tag()
    range_tag = _initial_range_tag_from_terrain(battle_terrain)

    # 主炮“节奏”与弹种：不增加新菜单项，仅让切换弹种与故障更真实
    main_gun_delay = 0  # >0 表示本回合无法主炮射击（装填/卡滞）
    loaded_shell: Optional[str] = None  # "AP" / "HE" / None

    # 需求：上膛弹种取决于“上一次最后装填的是什么”。
    last = str(getattr(s, "last_loaded_shell", "") or "").upper()
    if last in ("AP", "HE"):
        loaded_shell = last
    else:
        # 兜底：开局默认已上膛（优先 HE）：第一回合可以直接开火，不再浪费一个“换弹回合”。
        if int(getattr(s, "he_shells", 0) or 0) > 0:
            loaded_shell = "HE"
        elif int(getattr(s, "ap_shells", 0) or 0) > 0:
            loaded_shell = "AP"

    # 章节分支：开局风格差异（小幅、可感知；不消耗物品）
    flags = dict(getattr(s, "story_flags", {}) or {})
    if bool(flags.get("ammo_conserve", False)):
        print("你们把高爆弹预置，但穿甲弹摆在手边：主炮切换更顺。")
    if bool(flags.get("camo_prepared", False)) and (not boss):
        smoke_turns = max(smoke_turns, 1)
        print("伪装与烟尘掩护让你们更难被锁定：开局烟幕+1。")
    if bool(flags.get("night_stealth", False)) and (not boss):
        maneuver_turns = max(maneuver_turns, 1)
        print("夜行规程发挥作用：开局机动+1。")
    if bool(flags.get("signal_net", False)) and (not boss):
        support_turns = max(support_turns, 1)
        print("你们的联络更稳定：开局掩护+1。")

    def _main_gun_status_text() -> str:
        shell = loaded_shell or "未装填"
        delay = f"延迟{main_gun_delay}" if main_gun_delay > 0 else "就绪"
        faults: List[str] = []
        if s.debuffs.get("gun_breech", 0) > 0:
            faults.append("炮闩")
        if s.debuffs.get("turret_jam", 0) > 0:
            faults.append("炮塔")
        if s.debuffs.get("engine_damage", 0) > 0:
            faults.append("动力")
        if s.debuffs.get("radio_damage", 0) > 0:
            faults.append("电台")
        if s.debuffs.get("optics_broken", 0) > 0:
            faults.append("观瞄")
        fault_text = (" 故障:" + "、".join(faults)) if faults else ""
        return f"主炮[{shell}/{delay}]{fault_text}"

    def _calc_main_gun_delay(*, switching_shell: bool) -> int:
        delay = 0
        loader = crew_role_state(s, "装填手")
        if loader == "missing":
            return 99
        lp = crew_effective_role_proficiency(s, "装填手")
        # 熟练度越高，迟滞概率越低（0 为基准不变）
        mult = 1.0 - lp * 0.0035
        mult = max(0.65, min(1.00, float(mult)))
        # 装填手负伤/高压力：更容易出现“迟滞”
        if loader == "wounded" and s.rng.random() < float(LOADER_WOUNDED_DELAY_CHANCE) * mult:
            delay += 1
        loaders = [m for m in s.crew if m.alive and m.role == "装填手"]
        if loaders:
            st = max(m.stress for m in loaders)
            if st >= 70 and s.rng.random() < float(LOADER_STRESS_DELAY_CHANCE) * mult:
                delay += 1
        if s.debuffs.get("gun_breech", 0) > 0:
            delay += 1
        # 若为换弹操作，使用弹种基线进一步决定最小冷却
        if switching_shell:
            # 默认至少 1 回合，若提供目标弹种则按历史基线加成
            delay = max(delay, 1)
            try:
                target = str(getattr(_calc_main_gun_delay, "_last_target_shell", "") or "")
            except Exception:
                target = ""
            # 若外部提供目标弹种（通过函数属性传递），使用基线
            if target in MAIN_GUN_SHELL_SWITCH_BASE:
                delay = max(delay, int(MAIN_GUN_SHELL_SWITCH_BASE[target]))
            # 清理临时标记
            try:
                setattr(_calc_main_gun_delay, "_last_target_shell", "")
            except Exception:
                pass
        return delay

    def _try_fire_main_gun(enemies: List[EnemyUnit], *, shell: str, target_idx: int, range_tag: str, smoke: int, maneuver: int) -> None:
        nonlocal main_gun_delay, loaded_shell

        loader = crew_role_state(s, "装填手")
        if loader == "missing":
            print("缺少装填手：主炮无法维持装填节奏，你只能用机枪/机动撑住。")
            return

        if main_gun_delay > 0:
            print("主炮尚未就绪：装填/卡滞让你错过了这个窗口。")
            return

        block_chance = float(GUN_BREECH_BLOCK_CHANCE)
        if bool(getattr(s, "story_flags", {}).get("maint_done", False)):
            block_chance *= 0.75
        if s.debuffs.get("gun_breech", 0) > 0 and s.rng.random() < block_chance:
            print("炮闩卡滞：你们只能用力排除故障，暂时无法开火。")
            main_gun_delay = max(main_gun_delay, 1)
            return

        # 弹种切换：花一回合“换弹”，不消耗弹药
        if loaded_shell != shell:
            loaded_shell = shell
            setattr(s, "last_loaded_shell", loaded_shell)
            # 记录希望切换到的弹种，供延迟计算参考
            try:
                setattr(_calc_main_gun_delay, "_last_target_shell", shell)
            except Exception:
                pass
            main_gun_delay = _calc_main_gun_delay(switching_shell=True)
            print(f"装填手更换弹种：将下一发准备为{shell}。")
            return

        # 弹药检查：只有真正开火才消耗
        if shell == "AP" and s.ap_shells <= 0:
            maybe_resupply_shells_from_inventory(ins, s, want_shell="AP")
            if s.ap_shells <= 0:
                print("AP炮弹见底：你只能短暂缩回，寻找更好的角度。")
                return
        if shell == "HE" and s.he_shells <= 0:
            maybe_resupply_shells_from_inventory(ins, s, want_shell="HE")
            if s.he_shells <= 0:
                print("HE炮弹见底：你只能短暂缩回，寻找更好的角度。")
                return

        # 开火
        if shell == "AP":
            s.ap_shells -= 1
        else:
            s.he_shells -= 1
        _player_attack(enemies, mode=shell, target_idx=target_idx, range_tag=range_tag, smoke=smoke, maneuver=maneuver)
        main_gun_delay = _calc_main_gun_delay(switching_shell=False)
        setattr(s, "last_loaded_shell", loaded_shell)

    # 章节/剧情可能会强制额外遭遇：这里让它不再跨事件残留
    s.buffs.pop("额外遭遇", None)

    # 将部分全局 buff 转为战斗内短效（并消费掉）
    if s.buffs.pop("求援", 0) > 0:
        support_turns = max(support_turns, 2)
        print("电台回音短促：你们得到更稳的火力协同。")

    # 战斗回合数：保持较短，避免拖沓
    max_turns = 4

    # --- Sd.Kfz.251 的“开局下车”已在上方处理

    # --- 现实机制：远程炮兵校射/炮火骚扰（不增加额外菜单）
    offmap_arty_turns: int = 0
    offmap_arty_intensity: int = 1
    terrain = str(MAP_META.get(s.location_key, {}).get("terrain", ""))
    base_risk = float(LOCATIONS.get(s.location_key, {}).get("risk", 0.0))
    # 提高炮兵出现基线与风险系数，令高风险地区更易遭遇炮火
    p_arty = 0.06 + base_risk * 0.12
    if bool(flags.get("intel_saved", False)) or bool(flags.get("escape_intel", False)) or bool(flags.get("signal_net", False)):
        p_arty = max(0.0, p_arty - 0.02)
    if terrain in ("公路", "郊区", "出口", "阵地", "堤坝"):
        p_arty += 0.05
        # 开阔地形更容易承受较强校射
        offmap_arty_intensity = 2
        # 极端开阔（堤坝/阵地）进一步加强
        if terrain in ("阵地", "堤坝"):
            offmap_arty_intensity = 3
    if any(x.alive and x.kind in ("迫击炮组", "反坦克炮") for x in enemies):
        p_arty += 0.05
    p_arty = max(0.02, min(0.18, p_arty))
    # 检查全局炮火冷却（历史化：炮兵需要时间重新集火）
    if s.counters.get("offmap_arty_cooldown", 0) > 0:
        # 仍在冷却中，禁止本战斗出现远程炮兵
        p_arty = 0.0
    if s.rng.random() < p_arty:
        # 强度越大，持续回合可能更多
        if offmap_arty_intensity >= 3:
            offmap_arty_turns = s.rng.randint(1, 3)
        else:
            offmap_arty_turns = s.rng.randint(1, 2)
        print("⚠️ 远处炮兵正在校射：本场战斗可能遭到炮火骚扰。")
        # 设置冷却：根据强度延长重现间隔
        try:
            s.counters["offmap_arty_cooldown"] = int(OFFMAP_ARTY_COOLDOWN_DEFAULT * max(1, offmap_arty_intensity))
        except Exception:
            s.counters["offmap_arty_cooldown"] = OFFMAP_ARTY_COOLDOWN_DEFAULT

    # --- 现实机制：敌方照明弹（削弱烟幕）
    illumination_turns: int = 0

    # --- 敌方空中支援：战斗中临时出现
    air_pending_kind: Optional[str] = None
    air_arrival_turn: Optional[int] = None
    air_kind: Optional[str] = None
    air_turns_left: int = 0
    # 自检：可强制指定空中支援（用于覆盖 IL-2 / 对空射击分支）
    forced_kind = _selftest_get("force_air_kind") if SELFTEST else None
    if isinstance(forced_kind, str) and forced_kind in {"Yak-3", "IL-2"}:
        air_pending_kind = forced_kind
        try:
            air_arrival_turn = int(_selftest_get("force_air_arrival_turn", 1))
        except Exception:
            air_arrival_turn = 1
        air_arrival_turn = max(1, min(max_turns, air_arrival_turn))
        if bool(_selftest_get("force_air_once", True)):
            _selftest_pop("force_air_kind", None)
            _selftest_pop("force_air_arrival_turn", None)
            _selftest_pop("force_air_once", None)
    else:
        # 根据区域风险与炮火骚扰提高空中支援出现概率
        spawn_p = AIR_SUPPORT_SPAWN_CHANCE + base_risk * 0.06
        if (
            bool(flags.get("night_stealth", False))
            or bool(flags.get("camo_prepared", False))
            or bool(flags.get("signal_net", False))
        ):
            spawn_p = max(0.0, float(spawn_p) * 0.78)
        # 检查空中支援冷却，若仍在冷却则不生成
        if s.counters.get("air_support_cooldown", 0) > 0:
            spawn_p = 0.0
        if s.rng.random() < spawn_p:
            # 风险高或炮火强度高时更倾向于出现 IL-2（对地打击）
            il2_bias = 0.42 + (0.18 if offmap_arty_intensity >= 2 else 0.0) + (0.12 * base_risk)
            il2_bias = max(0.25, min(0.92, il2_bias))
            air_pending_kind = "IL-2" if s.rng.random() < il2_bias else "Yak-3"
            air_arrival_turn = s.rng.randint(2, max_turns)
            # 设置空中支援冷却，减少连续出现
            try:
                s.counters["air_support_cooldown"] = int(AIR_SUPPORT_COOLDOWN_DEFAULT)
            except Exception:
                s.counters["air_support_cooldown"] = AIR_SUPPORT_COOLDOWN_DEFAULT

    def _aa_fire_try_drive_off() -> bool:
        if s.mg_ammo < AA_MG_COST:
            print("机枪弹不足：无法有效对空射击。")
            return False
        s.mg_ammo -= AA_MG_COST

        if SELFTEST and bool(_selftest_pop("force_aa_success", False)):
            print("对空射击奏效：敌机拉起转向，暂时脱离你们上空。")
            return True

        # 稍微降低对空射击驱离成功率，增加玩家权衡成本
        p = 0.48
        gunner = crew_role_state(s, "炮手")
        if gunner == "missing":
            p -= 0.18
        elif gunner == "wounded":
            p -= 0.10
        if s.debuffs.get("optics_broken", 0) > 0:
            p -= 0.06
        if s.morale <= 30:
            p -= 0.06
        if s.buffs.get("烟幕", 0) > 0:
            p -= 0.04
        p = max(0.15, min(0.80, p))

        if s.rng.random() < p:
            print("对空射击奏效：敌机拉起转向，暂时脱离你们上空。")
            return True
        print("对空射击未能驱离：敌机仍在盘旋寻找角度。")
        return False

    def _pick_offmap_ally_target(*, prefer_tank: bool = False) -> tuple[str, Optional[object]]:
        try:
            live_g = [u for u in allies if getattr(u, "alive", False)]
        except Exception:
            live_g = []
        try:
            live_t = [t for t in tank_allies if getattr(t, "alive", False)]
        except Exception:
            live_t = []

        if not live_g and not live_t:
            return ("player", None)

        w_player, w_g, w_t = 0.45, 0.30, 0.25
        if prefer_tank:
            w_t += 0.08
            w_player = max(0.30, w_player - 0.06)
            w_g = max(0.20, w_g - 0.02)

        if not live_g:
            w_player += w_g
            w_g = 0.0
        if not live_t:
            w_player += w_t
            w_t = 0.0

        choice = s.rng.choices(["player", "garrison", "tank"], weights=[w_player, w_g, w_t], k=1)[0]
        if choice == "garrison" and live_g:
            return ("garrison", s.rng.choice(live_g))
        if choice == "tank" and live_t:
            return ("tank", s.rng.choice(live_t))
        return ("player", None)

    def _apply_offmap_hit_to_garrison(u: GarrisonUnit, *, raw: int, tag: str) -> None:
        base = float(raw) * s.rng.uniform(1.05, 1.55)
        base -= float(getattr(u, "armor", 0)) * 0.45
        dmg = max(1, int(base))
        u.hp -= dmg
        u.clamp()
        print(f"{tag}：{u.name} 被波及（-{dmg}，HP{u.hp}）。")
        if not u.alive:
            print(f"友军被击毁：{u.name} 在爆炸中失去战斗力。")

    def _apply_offmap_hit_to_tank(t: TankAlly, *, raw: int, top_attack: bool, tag: str) -> None:
        base = float(raw) * s.rng.uniform(1.10, 1.70)
        if top_attack:
            base *= 1.10
        base -= float(getattr(t, "armor", 80)) * (0.20 if top_attack else 0.12)
        dmg = max(1, int(base))
        t.hp -= dmg
        t.clamp()
        print(f"{tag}：{t.name} 受到爆炸冲击（-{dmg}，HP{t.hp}）。")
        if not t.alive:
            print(f"友军坦克被击毁：{t.name} 冒烟停下，不再回应电台。")
            _sdkfz_infantry_return_or_flee(t)

    def _air_support_attack(*, smoke: int) -> None:
        nonlocal air_kind, air_turns_left, player_suppressed_turns
        if not air_kind or air_turns_left <= 0:
            return
        if air_kind == "Yak-3":
            player_suppressed_turns = max(player_suppressed_turns, 1)
            print("敌机掠过：雅克3 低空扫射（我方压制+1）。")
            target_kind, target_obj = _pick_offmap_ally_target(prefer_tank=False)
            if target_kind == "garrison" and isinstance(target_obj, GarrisonUnit):
                raw = s.rng.randint(3, 7)
                _apply_offmap_hit_to_garrison(target_obj, raw=raw, tag="空袭波及友军")
            elif target_kind == "tank" and isinstance(target_obj, TankAlly):
                raw = s.rng.randint(4, 8)
                _apply_offmap_hit_to_tank(target_obj, raw=raw, top_attack=False, tag="空袭波及友军装甲")
            elif s.rng.random() < 0.22:
                # 小概率造成轻度结构冲击
                raw = s.rng.randint(2, 5)
                eff = _apply_player_structure_damage(raw)
                print(f"机炮弹雨敲击车体（损伤+{eff}）。")
        else:  # IL-2
            print("⚠️ 敌方伊尔2 进入攻击航线：爆炸震响逼近。")
            # 顶攻/航弹：对装甲减伤更弱，但仍会被一定程度吸收
            # 增加强度与波动，空袭在无遮蔽时更致命
            raw = s.rng.randint(22, 44)
            if smoke > 0:
                raw = max(14, int(raw * 0.78))
            target_kind, target_obj = _pick_offmap_ally_target(prefer_tank=True)
            if target_kind == "garrison" and isinstance(target_obj, GarrisonUnit):
                _apply_offmap_hit_to_garrison(target_obj, raw=raw, tag="空袭误伤友军")
            elif target_kind == "tank" and isinstance(target_obj, TankAlly):
                _apply_offmap_hit_to_tank(target_obj, raw=raw, top_attack=True, tag="空袭命中友军装甲")
            else:
                eff = _apply_player_structure_damage(raw, top_attack=True)
                print(f"伊尔2 袭击命中：冲击波撕裂尘埃（损伤+{eff}）。")
                if s.rng.random() < 0.38:
                    candidates = [m for m in s.crew if m.alive and m.role != "车长"]
                    if candidates:
                        m = s.rng.choice(candidates)
                        hit = s.rng.randint(20, 40)
                        m.hp -= hit
                        m.stress += 16
                        if m.hp <= 0:
                            s.crew_lost += 1
                            print(f"乘员损失：{m.role} {m.name} 未能归队。")
                        else:
                            print(f"乘员受伤：{m.role} {m.name} 状态恶化。")

        air_turns_left = max(0, air_turns_left - 1)
        if air_turns_left <= 0:
            print("敌机掠过远去：上空暂时安静下来。")
            air_kind = None

    def _offmap_arty_attack(*, smoke: int, maneuver: int) -> None:
        nonlocal offmap_arty_turns, player_suppressed_turns
        if offmap_arty_turns <= 0:
            return

        # 烟幕/机动能显著降低被校射命中的概率
        p_hit = 0.55
        if smoke > 0:
            p_hit -= 0.18
        if maneuver > 0:
            p_hit -= 0.12
        if s.debuffs.get("engine_damage", 0) > 0:
            p_hit += 0.06
        p_hit = max(0.15, min(0.75, p_hit))

        if s.rng.random() < p_hit:
            raw = s.rng.randint(6, 10) + (2 if offmap_arty_intensity >= 2 else 0)
            target_kind, target_obj = _pick_offmap_ally_target(prefer_tank=True)
            if target_kind == "garrison" and isinstance(target_obj, GarrisonUnit):
                _apply_offmap_hit_to_garrison(target_obj, raw=raw, tag="远程炮击波及友军")
            elif target_kind == "tank" and isinstance(target_obj, TankAlly):
                _apply_offmap_hit_to_tank(target_obj, raw=raw, top_attack=True, tag="远程炮击命中友军装甲")
            else:
                eff = _apply_player_structure_damage(raw, top_attack=True)
                player_suppressed_turns = max(player_suppressed_turns, 1)
                print(f"远程炮击落下：爆炸震动传入舱内（损伤+{eff}，我方压制+1）。")
                if s.rng.random() < 0.12:
                    candidates = [m for m in s.crew if m.alive and m.role != "车长"]
                    if candidates:
                        m = s.rng.choice(candidates)
                        hit = s.rng.randint(10, 22)
                        m.hp -= hit
                        m.stress += 10
                        if m.hp <= 0:
                            s.crew_lost += 1
                            print(f"乘员损失：{m.role} {m.name} 未能归队。")
                        else:
                            print(f"乘员受伤：{m.role} {m.name} 状态恶化。")
        else:
            print("远处炮弹落在街口外：这轮没有直接命中。")

        offmap_arty_turns = max(0, offmap_arty_turns - 1)
        if offmap_arty_turns <= 0:
            print("炮火停歇：校射暂时中断。")

    def _auto_mg_fire(enemies: List[EnemyUnit], *, smoke: int) -> None:
        # 每回合自动对“步兵/火力点”进行一次机枪压制（不占用玩家选择的行动）
        if s.mg_ammo < MG_FIRE_COST:
            return
        infantry_idxs = [i for i, e in enumerate(enemies) if e.alive and e.armor < 20]
        if not infantry_idxs:
            return
        s.mg_ammo -= MG_FIRE_COST
        tidx = s.rng.choice(infantry_idxs)
        print("自动机枪扫射：你让机枪持续压住步兵火力。")
        _player_attack(enemies, mode="MG", target_idx=tidx, range_tag=range_tag, smoke=smoke, maneuver=maneuver_turns)

    tno = 0
    while True:
        tno += 1
        # 每回合开始：衰减全局支援冷却计数
        for k in ("offmap_arty_cooldown", "air_support_cooldown"):
            if s.counters.get(k, 0) > 0:
                try:
                    s.counters[k] = max(0, int(s.counters.get(k, 0)) - 1)
                except Exception:
                    s.counters[k] = 0
        if s.ended:
            setattr(s, "last_loaded_shell", loaded_shell)
            return "ended"

        # 需求：如果只剩下“无法清除”的突击队（永久攀附），则视为其趁乱逃离，避免战斗僵死。
        alive_enemies = [e for e in enemies if getattr(e, "alive", False)]
        if alive_enemies and all(
            (e.kind == "突击队" and int(getattr(e, "status", {}).get("攀附永续", 0) or 0) > 0)
            for e in alive_enemies
        ):
            for e in alive_enemies:
                try:
                    if isinstance(getattr(e, "status", None), dict):
                        e.status["_escaped"] = 1
                except Exception:
                    pass
                e.alive = False
                tag = f"逃窜敌人-{s.round_number}-{s.rng.randint(10,99)}"
                s.fleeing_enemies.append(tag)
            print("\n⚠️ 只剩下无法清除的突击队：对方趁烟尘撤离，你们无法再追击。")
            break
        if all(not e.alive for e in enemies):
            break

        print("\n" + "=" * 50)
        if air_pending_kind and air_arrival_turn == tno:
            air_kind = air_pending_kind
            air_pending_kind = None
            air_turns_left = 2
            name = "雅克3" if air_kind == "Yak-3" else "伊尔2"
            print(f"\n✈️  空中威胁：敌方{name} 进入战区（可用机枪对空射击驱离）。")

        air_text = ""
        if air_kind:
            air_text = f" | 敌机{air_kind}({air_turns_left})"
        fortify_text = f" 稳固{player_fortify_turns}" if player_fortify_turns > 0 else ""
        dist_text = {"close": "近", "medium": "中", "long": "远"}.get(range_tag, str(range_tag))
        print(f"战斗回合 {tno}  | 距离{dist_text} 烟幕{smoke_turns} 机动{maneuver_turns} 掩护{support_turns} 照明{illumination_turns} 我方压制{player_suppressed_turns}{fortify_text}{air_text}")
        print(f"燃油{s.fuel} 机枪弹{s.mg_ammo} 炮弹AP{s.ap_shells} HE{s.he_shells} 士气{s.morale} 损伤{s.damage} 装甲{player_armor_rating(s)} | {_main_gun_status_text()}")

        # 每回合开始：清理“本回合被攻击”标记（用于突击队攀附判定）
        for e in enemies:
            if not e.alive or not e.status:
                continue
            e.status.pop("本回合被攻击", None)

        # 敌方状态倒计时
        for e in enemies:
            if not e.alive or not e.status:
                continue
            # 压制
            if e.status.get("压制", 0) > 0:
                e.status["压制"] = max(0, e.status["压制"] - 1)
                if e.status["压制"] == 0:
                    e.status.pop("压制", None)
            # 机动受限
            if e.status.get("机动受限", 0) > 0:
                e.status["机动受限"] = max(0, e.status["机动受限"] - 1)
                if e.status["机动受限"] == 0:
                    e.status.pop("机动受限", None)
            # IS-2 主炮冷却
            if e.status.get("主炮冷却", 0) > 0:
                e.status["主炮冷却"] = max(0, int(e.status["主炮冷却"]) - 1)
                if e.status["主炮冷却"] == 0:
                    e.status.pop("主炮冷却", None)

            # 突击队攀附延迟（宽限回合）
            if e.status.get("攀附延迟", 0) > 0:
                e.status["攀附延迟"] = max(0, int(e.status["攀附延迟"]) - 1)
                if e.status["攀附延迟"] == 0:
                    e.status.pop("攀附延迟", None)

        # 友军行动：驻军与你并肩作战（压制/牵制）
        _allies_act(enemies)

        # 友军坦克行动：与我方类似的火力窗口（自动）
        _tank_allies_act(enemies, range_tag=range_tag, smoke=smoke_turns, maneuver=maneuver_turns)

        # 自动机枪：每回合先对步兵/火力点压制一次
        _auto_mg_fire(enemies, smoke=smoke_turns)

        # 玩家行动
        while True:
            action_menu = {
                "1": "主炮穿甲(AP)（对装甲/火炮更有效，耗AP炮弹1）",
                "2": "主炮高爆(HE)（压制步兵火力，耗HE炮弹1）",
                "3": f"机枪扫射（额外一次；压制步兵，耗机枪弹{MG_FIRE_COST}）",
                "4": "机动/倒车（降低敌方命中，提高我方命中，耗油2）",
                "5": "投烟幕（降低敌方命中，耗烟幕弹或烟幕buff）",
                "6": "使用技能（战斗中也可用：鼓舞/紧急抢修/电台求援/观察/稳固阵位）",
                "7": "尝试撤离（成功则脱离接触）",
                "8": "移动（调整距离，耗油2）",
            }
            if air_kind:
                action_menu["9"] = f"机枪对空射击（尝试驱离敌机，耗机枪弹{AA_MG_COST}）"
            default_action = "1" if not ins.default_when_empty else "1"
            a = choose(ins, f"选择行动(0-{9 if air_kind else 8})：", action_menu, default=default_action)
            if a == "0":
                continue

            if a in ("1", "2", "3"):
                if a == "1":
                    target_idx = _select_target_menu(enemies, hint="选择目标编号：")
                    if target_idx < 0:
                        continue
                    _try_fire_main_gun(
                        enemies,
                        shell="AP",
                        target_idx=target_idx,
                        range_tag=range_tag,
                        smoke=smoke_turns,
                        maneuver=maneuver_turns,
                    )
                elif a == "2":
                    target_idx = _select_target_menu(enemies, hint="选择目标编号：")
                    if target_idx < 0:
                        continue
                    _try_fire_main_gun(
                        enemies,
                        shell="HE",
                        target_idx=target_idx,
                        range_tag=range_tag,
                        smoke=smoke_turns,
                        maneuver=maneuver_turns,
                    )
                else:
                    if s.mg_ammo < MG_FIRE_COST:
                        print("机枪弹不足：你只能压低炮口寻找掩体。")
                        maneuver_turns = max(maneuver_turns, 1)
                    else:
                        target_idx = _select_target_menu(enemies, hint="选择目标编号：")
                        if target_idx < 0:
                            continue
                        s.mg_ammo -= MG_FIRE_COST
                        _player_attack(
                            enemies,
                            mode="MG",
                            target_idx=target_idx,
                            range_tag=range_tag,
                            smoke=smoke_turns,
                            maneuver=maneuver_turns,
                        )
                break

            if a == "9" and air_kind:
                if _aa_fire_try_drive_off():
                    air_kind = None
                    air_turns_left = 0
                break

            if a == "4":
                if s.fuel <= 0:
                    print("燃油不足：你无法做出有效机动。")
                else:
                    cost = 2
                    eff = 2
                    fatigue = int(s.counters.get("fatigue", 0))
                    if fatigue >= 80:
                        cost += 2
                        eff = max(1, eff - 1)
                    elif fatigue >= 60:
                        cost += 1
                        eff = max(1, eff - 1)
                    driver_state = crew_role_state(s, "驾驶员")
                    if driver_state == "missing":
                        cost += 1
                        eff = 1
                    elif driver_state == "wounded":
                        eff = max(1, eff - 1)
                    else:
                        dp = crew_effective_role_proficiency(s, "驾驶员")
                        if dp >= 70:
                            eff = min(3, eff + 1)
                    if s.debuffs.get("engine_damage", 0) > 0:
                        cost += 1
                        eff = 1
                    _consume_fuel_with_allies(s, cost)
                    maneuver_turns = max(maneuver_turns, eff)
                    print("你下令短促机动：利用掩体与角度压低敌方命中，同时争取更好的射击窗口。")
                break

            if a == "8":
                if s.fuel <= 0:
                    print("燃油不足：你无法移动调整距离。")
                else:
                    cost = 2
                    fatigue = int(s.counters.get("fatigue", 0))
                    if fatigue >= 80:
                        cost += 2
                    elif fatigue >= 60:
                        cost += 1
                    driver_state = crew_role_state(s, "驾驶员")
                    if driver_state == "missing":
                        cost += 1
                    if s.debuffs.get("engine_damage", 0) > 0:
                        cost += 1
                    _consume_fuel_with_allies(s, cost)

                    dist_menu = {
                        "1": "贴近（近距离）",
                        "2": "保持（中距离）",
                        "3": "拉开（远距离）",
                    }
                    d = choose(ins, "移动意图：", dist_menu, default="2")
                    if d == "1":
                        range_tag = "close"
                        print("你下令移动：贴近到近距离交战。")
                    elif d == "3":
                        range_tag = "long"
                        print("你下令移动：拉开到远距离交战。")
                    else:
                        range_tag = "medium"
                        print("你下令移动：保持中距离交火。")
                break

            if a == "5":
                if s.buffs.get("烟幕", 0) > 0:
                    s.buffs.pop("烟幕", None)
                    smoke_turns = max(smoke_turns, 2)
                    print("你利用已准备的烟幕窗口，街口很快被灰色吞没。")
                elif spend_item(s, "烟幕弹", 1):
                    smoke_turns = max(smoke_turns, 2)
                    print("烟幕弹爆开：视野被切碎，你们争取到撤离空间。")
                else:
                    print("你想投烟幕，但没有烟幕弹。")
                break

            if a == "6":
                menu_skills(ins, s)
                # 战斗内技能的结果在下一轮体现
                break

            # 撤离判定（a == "7" 或其他）
            escape = 0.45
            escape += 0.25 if smoke_turns > 0 else 0.0
            escape += 0.12 if maneuver_turns > 0 else 0.0
            escape += 0.08 if s.morale >= 60 else 0.0
            escape -= 0.12 if s.damage >= 80 else 0.0
            fatigue = int(s.counters.get("fatigue", 0))
            if fatigue >= 80:
                escape -= 0.10
            elif fatigue >= 60:
                escape -= 0.06
            if crew_role_state(s, "驾驶员") == "missing":
                escape -= 0.12
            elif crew_role_state(s, "驾驶员") == "wounded":
                escape -= 0.06
            else:
                dp = crew_effective_role_proficiency(s, "驾驶员")
                # 0..100 -> 0..+10%
                escape += dp * 0.0010
            if s.debuffs.get("engine_damage", 0) > 0:
                escape -= 0.06
            escape = max(0.08, min(0.85, escape))
            if s.rng.random() < escape:
                print("你们抓住一个缝隙撤离，成功脱离接触。")
                setattr(s, "last_loaded_shell", loaded_shell)
                _consume_fuel_with_allies(s, 5 + (1 if fatigue >= 60 else 0))
                s.morale += 1
                sec = s.sectors.get(s.location_key)
                if sec is not None:
                    sec.fall += 3
                    sec.favor = max(0, sec.favor - 1)

                # 战后：医疗组处置（成功撤离同样视为“战后”）
                _post_battle_medic_support()

                # 驻军支援单位战斗结束后归队
                for origin, u in deployed_pairs:
                    if u.alive:
                        sec0 = s.sectors.get(origin)
                        if sec0 is not None:
                            sec0.garrison_units.append(u)
                s.clamp()
                return "escape"
            print("撤离失败：敌方火力封锁了你们的退路。")
            break

        # 敌方照明弹：我方烟幕存在时，小概率点亮以削弱遮蔽
        if smoke_turns > 0 and illumination_turns <= 0:
            flare_capable = any(
                e.alive and e.kind in ("迫击炮组", "反坦克炮", "反坦克组", "火箭筒组", "重机枪点", "装甲车") for e in enemies
            )
            if flare_capable:
                p_flare = 0.10
                if any(x.alive and x.kind == "装甲车" for x in enemies):
                    p_flare += 0.06
                if any(x.alive and getattr(x, "elite", False) and x.kind in ("迫击炮组", "反坦克炮", "反坦克组", "火箭筒组", "重机枪点", "装甲车") for x in enemies):
                    p_flare += 0.04
                if s.rng.random() < p_flare:
                    illumination_turns = 1
                    print("⚠️ 敌方打出照明弹：烟幕的遮蔽效果被削弱。")

        # 远程炮火/校射：在敌方单位行动前先结算一次
        _offmap_arty_attack(smoke=smoke_turns, maneuver=maneuver_turns)

        # 敌方空中支援：在敌方单位行动前先结算一次
        _air_support_attack(smoke=smoke_turns)

        # 敌方行动
        for e in enemies:
            ally_support = 1 if _alive_allies() else 0
            support_eff = max(support_turns, ally_support)
            if s.debuffs.get("radio_damage", 0) > 0 or crew_role_state(s, "通信员") == "missing":
                support_eff = max(0, support_eff - 1)
            _enemy_ai_step(e, enemies, range_tag=range_tag, smoke=smoke_turns, maneuver=maneuver_turns, support=support_eff, tno=tno, max_turns=max_turns)
            # 结局判定统一走 end_conditions：确保“燃油耗尽/自毁准备/士气崩溃”等分支在战斗内也能生效
            ended = end_conditions(s)
            if ended is not None:
                end_game(s, ended[0], ended[1], ended[2])
                return "ended"
            if s.damage >= 100:
                # 兜底：理论上 damage>=100 已会被 end_conditions 捕获
                end_game(
                    s,
                    "E05",
                    "钢铁的终点",
                    """
车体终于停下。你命令乘员离车，尽力在废墟里分散。
你不知道每个人能走多远，但你知道此刻唯一重要的是：别再让钢铁决定人的命。
""",
                )
                return "ended"

        # 回合结算：短效状态递减
        smoke_turns = max(0, smoke_turns - 1)
        maneuver_turns = max(0, maneuver_turns - 1)
        support_turns = max(0, support_turns - 1)
        illumination_turns = max(0, illumination_turns - 1)
        main_gun_delay = max(0, main_gun_delay - 1)
        player_suppressed_turns = max(0, player_suppressed_turns - 1)
        player_fortify_turns = max(0, player_fortify_turns - 1)
        if s.debuffs.get("mg_jam", 0) > 0:
            s.debuffs["mg_jam"] = max(0, int(s.debuffs.get("mg_jam", 0)) - 1)
            if s.debuffs.get("mg_jam", 0) <= 0:
                s.debuffs.pop("mg_jam", None)
        s.clamp()

    # 战斗结束：胜/负/僵持
    setattr(s, "last_loaded_shell", loaded_shell)
    sec = s.sectors.get(s.location_key)
    # 逃离单位不计入“击毁数”（用于收益强度），但仍会进入 fleeing_enemies。
    killed = sum(
        1
        for e in enemies
        if (not e.alive)
        and (not (isinstance(getattr(e, "status", None), dict) and int(e.status.get("_escaped", 0) or 0) > 0))
    )
    if all(not e.alive for e in enemies):
        gain = 2 + max(0, killed - 2)
        s.victory_points += gain
        s.morale += 4
        _consume_fuel_with_allies(s, 3)
        print(f"\n你们稳住了局面并脱离接触（+{gain}胜利点）。")
        s.counters["wins"] = s.counters.get("wins", 0) + 1
        if sec is not None:
            sec.fall = max(0, sec.fall - 3)
            sec.favor += 2

        # 小概率战利品
        if s.rng.random() < 0.18:
            add_item(s, "弹药箱", 1)
            print("你们在撤离前带走了一只弹药箱。")
        outcome = "cleared"
    else:
        # 僵持：默认按“短撤离”处理
        print("\n你们没有彻底清空火力点，但也没有被困住：你选择趁间隙撤离。")
        _consume_fuel_with_allies(s, 4)
        s.counters["losses"] = s.counters.get("losses", 0) + 1
        if sec is not None:
            sec.fall += 4
            sec.favor = max(0, sec.favor - 1)
        outcome = "withdraw"

    if s.fleeing_enemies and s.rng.random() < 0.20:
        s.fleeing_enemies.pop(0)
        print("你们切断了一条逃窜线路，后续风险稍降。")

    combat_ally_losses = [u for u in allies if id(u) in battle_start_alive_ally_ids and not u.alive]
    combat_tank_losses = [t for t in tank_allies if id(t) in battle_start_alive_tank_ids and not t.alive]

    # 友军消耗：最多 1 名友军可能在混乱中失联/伤亡
    live_allies = [u for u in _alive_allies() if not bool(getattr(u, "_from_sdkfz", False))]
    if live_allies and not combat_ally_losses:
        loss_p = 0.12 if outcome == "cleared" else 0.28
        loss_p = max(0.0, min(0.45, loss_p))
        if s.rng.random() < loss_p:
            victim = s.rng.choice(live_allies)
            victim.hp = 0
            victim.clamp()
            print(f"友军伤亡：{victim.name} 在火力与烟尘中失去联系。")

    # 友军坦克消耗：仅在未能肃清敌人、撤离的混乱中才可能失联。
    # 规则：若敌人被肃清（outcome == "cleared"），则不再产生“战后随机坦克损失”。
    live_tanks = [t for t in getattr(s, "tank_allies", []) if t.alive]
    if outcome != "cleared" and live_tanks and not combat_tank_losses:
        loss_p = 0.14
        loss_p = max(0.0, min(0.30, loss_p))
        if s.rng.random() < loss_p:
            victim = s.rng.choice(live_tanks)
            victim.hp = 0
            victim.clamp()
            print(f"友军坦克损失：{victim.name} 在火力与瓦砾中失去联系。")

    # 战后混乱损失结算后，再统计一次“本场总损失”（用于成就/记录）
    total_ally_losses = [u for u in allies if id(u) in battle_start_alive_ally_ids and not u.alive]
    total_tank_losses = [t for t in tank_allies if id(t) in battle_start_alive_tank_ids and not t.alive]

    if total_tank_losses:
        s.counters["ally_tanks_lost"] = int(s.counters.get("ally_tanks_lost", 0)) + len(total_tank_losses)
    if total_ally_losses:
        s.counters["ally_units_lost"] = int(s.counters.get("ally_units_lost", 0)) + len(total_ally_losses)

    # 零损失胜利：仅在清空敌人且战斗中无新增伤亡时计数
    if outcome == "cleared":
        no_ally_loss = (len(total_ally_losses) == 0) and (len(total_tank_losses) == 0)
        no_crew_loss = int(getattr(s, "crew_lost", 0)) <= crew_lost_start
        if no_ally_loss and no_crew_loss:
            s.counters["flawless_clears"] = int(s.counters.get("flawless_clears", 0)) + 1
            newly = check_and_unlock_achievements(s)
            if newly:
                print("\n【成就解锁】" + "、".join(newly))

    # 友军坦克士气波动：战斗不再导致士气下降（只允许上升或保持不变）
    for t in getattr(s, "tank_allies", []):
        if not t.alive:
            continue
        if outcome == "cleared":
            t.morale += 2
        # 战斗内提示标记重置（避免跨战斗永久不提示）
        try:
            setattr(t, "_no_shells_notice", False)
        except Exception:
            pass
        t.clamp()

    # 新机制：友军补给独立；用尽后索要（燃油不给则离队；弹药可不给但无法战斗）
    _handle_tank_ally_supply_requests(ins, s)

    # 友军士气波动：战斗不再导致士气下降（只允许上升或保持不变）
    for u in allies:
        if not u.alive:
            continue
        if outcome == "cleared":
            u.morale += 3
        u.clamp()

    # 战后：医疗组处置（治疗参战步兵与车组成员）
    _post_battle_medic_support()

    # 驻军支援单位归队（返回原辖区）
    for origin, u in deployed_pairs:
        if u.alive:
            sec0 = s.sectors.get(origin)
            if sec0 is not None:
                sec0.garrison_units.append(u)

    # 战后奖励性事件：让遭遇战结束后有明确收益
    if post_reward_event:
        post_encounter_reward_event(s, boss=boss, outcome=outcome)
    # 若本次遭遇是来源于搜刮动作（scavenge），则在战后额外触发一次搜刮掉落，燃油掉落权重临时提升
    try:
        if getattr(s, "_last_encounter_reason", "") == "scavenge":
            # 标记以便 event_reward_supply 增加燃油权重
            try:
                s._favor_fuel_for_post_scavenge = True
            except Exception:
                pass
            # 直接再调用一次搜索掉落事件
            try:
                event_reward_supply(s)
            except Exception:
                pass
            # 清理标记
            try:
                s._last_encounter_reason = ""
            except Exception:
                pass
    except Exception:
        pass
    # 战后熟练度：战斗越多越强
    post_encounter_proficiency_gain(s, boss=boss, outcome=outcome)

    # 提供一个战后出售物资的交互入口（可选）
    try:
        sell_choice = choose(ins, "是否在战后出售物资以换取金条？", {"1": "是", "0": "否"}, default="0")
        if sell_choice == "1":
            try:
                event_sell_resources(ins, s)
            except Exception:
                print("出售界面打开失败。")
    except Exception:
        # 若交互不可用或出错则忽略
        pass

    s.clamp()
    return outcome


def event_assist_evacuation(s: GameState) -> None:
    s.civilians_helped += 1
    s.morale += 6
    s.victory_points += 2
    _quest_progress(s, "Q1", 1)
    print("你用车体遮挡火线，引导一小群人穿过危险区。")
    sec = s.sectors.get(s.location_key)
    if sec is not None:
        sec.favor += 10
        sec.fall = max(0, sec.fall - 3)
    s.clamp()


def run_assist_evacuation(ins: InputStream, s: GameState) -> None:
    """支援撤离：几乎必然遭遇敌人，但回报更偏补给。"""
    # 先结算撤离/救援本身的正向收益
    event_assist_evacuation(s)

    # 几乎必然遇敌：这里使用“高概率强制遭遇”，而不是完全依赖 maybe_trigger_event 的区域风险。
    encounter_p = 0.80
    outcome: Optional[str] = None
    if s.rng.random() < encounter_p:
        print("\n⚠️ 撤离队伍在街口暴露：敌方火力逼近。")
        outcome = resolve_encounter(ins, s, boss=False)
        if outcome == "ended":
            return

    # 更多补给：默认给 2 次补给投放；若清空敌人则再额外 1 次
    rolls = 2
    if outcome == "cleared":
        rolls += 1
    print("\n（撤离队伍留下的补给/谢礼）")
    for _ in range(rolls):
        event_reward_supply(s)
    s.clamp()


def maybe_meet_friendly_infantry(s: GameState, *, reason: str) -> bool:
    """移动/搜索时小概率遇到友军步兵/支援（驻军单位加入本辖区并在下一场遭遇参与）。"""
    # 提高基础触发概率：移动时与搜刮时更容易遇到步兵支援
    p = 0.22 if reason == "move" else 0.15
    # 好感/章节/我方士气会提高概率
    sec = s.sectors.get(s.location_key)
    if sec is not None:
        p += max(0, sec.favor - 50) * 0.001
        p -= max(0, sec.fall - 55) * 0.0015
    if s.morale >= 70:
        p += 0.01
    # 限制概率上限（放宽上限以允许在高好感/高士气下更常见）
    p = max(0.0, min(0.50, p))
    if s.rng.random() >= p:
        return False

    # 生成一名驻军单位并放入 deployed_garrison（下一场遭遇会参与）
    try:
        terrain_now = MAP_META.get(getattr(s, "location_key", ""), {}).get("terrain")
        unit = _make_garrison_unit(s.rng, terrain=terrain_now)
        s.deployed_garrison.append((s.location_key, unit))
        print(f"\n🚩 在路上你们遇到一支友军队伍：{unit.name} 决定暂时加入行动。")
        s.morale += 1
        s.clamp()
        return True
    except Exception:
        return False


def maybe_meet_friendly_tank(s: GameState, *, reason: str) -> bool:
    """移动/搜索时小概率遇到我方坦克加入。返回 True 表示已触发。"""
    # 自检：强制加入一台友军坦克（用于覆盖其参战逻辑）
    if SELFTEST and bool(_selftest_get("force_meet_friendly_tank", False)):
        only_reason = _selftest_get("force_meet_friendly_tank_reason")
        if not isinstance(only_reason, str) or only_reason == reason:
            _selftest_pop("force_meet_friendly_tank", None)
            _selftest_pop("force_meet_friendly_tank_reason", None)
            suffix = s.rng.randint(11, 99)
            model = "豹式坦克"
            name = f"{model}-{suffix}"
            ally = TankAlly(
                model=model,
                name=name,
                hp=s.rng.randint(110, 155),
                armor=s.rng.randint(88, 112),
                accuracy=s.rng.randint(60, 74),
                morale=s.rng.randint(45, 72),
            )
            _randomize_tank_ally_supplies(s, ally)
            ally.clamp()
            s.tank_allies.append(ally)
            s.morale += 2
            print(f"\n🚩 你们在烟尘间遇到 {name}：对方决定暂时与你并肩推进。")
            s.clamp()
            return True

    def _pick_model() -> str:
        # 基础更常见豹式；也可能遇到装甲运兵车/防空坦克；后期/好感高时更可能遇到更重的突击炮
        sec = s.sectors.get(s.location_key)
        favor = int(sec.favor) if sec is not None else 50
        w_panther = 1.2
        w_pz4 = 2.0
        w_ferd = 0.35
        # 需求：Sd.Kfz 更常见
        w_sdkfz = 2.5
        # 需求：降低防空坦克生成权重
        w_aa = 0.22
        w_tiger = 0.3
        chap_thr = int(math.ceil(10.0 / float(CHAPTER_INTERVAL)))
        chapter_idx = max(1, min(40, (int(getattr(s, "round_number", 1) or 1) - 1) // int(CHAPTER_INTERVAL) + 1))
        if chapter_idx >= chap_thr:
            w_panther += 1.5
            w_ferd += 0.15
            w_aa += 0.04
            w_tiger += 0.05
        # 章节线性微增：随章节增加友军高性能型号出现概率
        w_panther += 0.03 * float(max(0, chapter_idx - 1))
        w_tiger += 0.015 * float(max(0, chapter_idx - 1))
        if favor >= 65:
            w_ferd += 0.20
            w_aa += 0.02
            w_sdkfz += 0.05
            w_tiger += 0.05
        if s.damage >= 75:
            w_ferd = max(0.10, w_ferd - 0.10)
        models = ["豹式坦克", "四号坦克", "斐迪南突击炮", "Sd.Kfz.251装甲运兵车", "防空坦克", "虎式坦克"]
        weights = [w_panther, w_pz4, w_ferd, w_sdkfz, w_aa, w_tiger]
        return s.rng.choices(models, weights=weights, k=1)[0]


    # (注意) maybe_meet_friendly_infantry 已提升为顶级函数，供全局调用。

    def _model_template(model: str) -> Dict[str, Tuple[int, int]]:
        # 范围：用于生成一辆“并肩作战”的友军装甲；不追求历史毫米，仅做相对差异
        if model == "豹式坦克":
            # 豹式：高速、射速与穿深良好，正面装甲中等
            return {"hp": (130, 170), "armor": (75, 100), "acc": (64, 80), "morale": (48, 78)}
        if model in ("四号坦克", "突击炮III"):
            # 四号/突击炮：通用、可靠，适配多场景
            return {"hp": (120, 155), "armor": (68, 92), "acc": (60, 74), "morale": (46, 76)}
        if model == "Sd.Kfz.251装甲运兵车":
            # 装甲运兵车：脆弱但灵活，载员配置合理
            return {"hp": (80, 110), "armor": (10, 24), "acc": (54, 74), "morale": (46, 78)}
        if model in ("防空坦克", "四号防空坦克"):
            # 防空坦克：机动与火力用于压制步兵与对空
            return {"hp": (90, 120), "armor": (14, 28), "acc": (58, 78), "morale": (46, 76)}
        if model == "虎式坦克":
            # 虎式：重装甲与高火力，命中略保守
            return {"hp": (165, 200), "armor": (110, 150), "acc": (58, 74), "morale": (48, 80)}
        # 斐迪南/重突：重装甲、火力强但机动与命中较差
        return {"hp": (160, 200), "armor": (120, 160), "acc": (48, 64), "morale": (42, 72)}

    sec = s.sectors.get(s.location_key)
    p = 0.0
    if reason == "move":
        p = 0.14
    elif reason == "scavenge":
        p = 0.10

    # 好感高、沦陷低时更可能遇到协同行动的己方装甲
    if sec is not None:
        p += max(0, sec.favor - 50) * 0.0015
        p -= max(0, sec.fall - 55) * 0.0020
    if s.morale >= 70:
        p += 0.01
    if s.damage >= 75:
        p -= 0.01

    p = max(0.0, min(0.21, p))
    if s.rng.random() >= p:
        return False

    suffix = s.rng.randint(11, 99)
    model = _pick_model()
    tmpl = _model_template(model)
    name = f"{model}-{suffix}"
    ally = TankAlly(
        model=model,
        name=name,
        hp=s.rng.randint(*tmpl["hp"]),
        armor=s.rng.randint(*tmpl["armor"]),
        accuracy=s.rng.randint(*tmpl["acc"]),
        morale=s.rng.randint(*tmpl["morale"]),
    )
    _randomize_tank_ally_supplies(s, ally)
    ally.clamp()
    s.tank_allies.append(ally)
    s.morale += 2
    print(f"\n🚩 你们在烟尘间遇到 {name}：对方决定暂时与你并肩推进。")
    s.clamp()
    return True


def action_move(ins: InputStream, s: GameState) -> None:
    if s.moves_this_round >= s.max_moves_per_round:
        print("本回合移动已达上限。")
        return

    if int(getattr(s, "fuel", 0)) <= 0:
        print("燃油已耗尽：你们无法再进行转移。")
        return

    # 移动耗油倍率：基础移动成本已会被“车型/地形/疲劳/天气”等倍率放大，
    # 这里不再额外整体倍增，否则会出现单次移动耗油过高。
    MOVE_FUEL_MULT = 1

    print("\n可前往区域：")
    # 优先显示相邻区域（使用 MAP_META 的邻接表），若无定义则回退到全部区域列表
    current = s.location_key
    meta = MAP_META.get(current, {})
    neigh = meta.get("adj") if isinstance(meta.get("adj"), list) else None
    if neigh:
        options: Dict[str, str] = {}
        for k in neigh:
            loc = LOCATIONS.get(k, {})
            if not loc:
                continue
            base_cost = int(MAP_META.get(k, {}).get("move_cost", 10))
            show_cost = max(1, int(base_cost) * MOVE_FUEL_MULT)
            options[k] = f"{loc['name']}（风险{int(loc['risk']*100)}%，耗油{show_cost}）"
        options["M"] = "查看整张地图"
    else:
        options = {k: f"{v['name']}（风险{int(v['risk']*100)}%）" for k, v in LOCATIONS.items()}
    # 解锁：地图碎片5张
    map_frag = 0
    for q in s.quests:
        if q.id == "Q3":
            map_frag = q.progress
    if map_frag >= 5:
        # 地图编号已扩充，避免与数字地点冲突，改用字母选项。
        options["e"] = "郊外缺口（尝试离开市区；需要燃油与士气）"

    options["0"] = "取消/返回"

    c = choose(ins, "选择目的地：", options, default=s.location_key)
    if c == "0":
        return
    if c == "e":
        attempt_escape(ins, s)
        return
    if c == "M":
        show_map(s)
        return

    # 本回合发生过“移动/探索”动作：用于肃清敌人类任务计数。
    s.counters["moved_this_round"] = 1

    # 移动消耗基于 MAP_META 中的 move_cost，并受“疲劳”与“天气”影响
    cost = int(MAP_META.get(c, {}).get("move_cost", 8))
    fatigue = int(s.counters.get("fatigue", 0))
    if fatigue >= 60:
        cost += 1
    try:
        eff = weather_effects(s)
        mult = float(eff.get("move_mult", 1.0))
        cost = int(max(1, round(cost * mult)))
    except Exception:
        pass

    # 在疲劳/天气修正之后应用移动倍率
    cost = int(max(1, int(cost) * MOVE_FUEL_MULT))

    terrain_now = MAP_META.get(c, {}).get("terrain")
    need = _calc_fuel_cost(s, cost, vehicle_model="虎式坦克", terrain=terrain_now, vehicles=1)
    if int(getattr(s, "fuel", 0)) < int(need):
        print(f"燃油不足：本次转移需要燃油{need}，当前仅剩{int(getattr(s, 'fuel', 0))}。")
        return

    s.location_key = c
    s.explored.add(c)
    s.counters["explore"] = s.counters.get("explore", 0) + 1
    s.moves_this_round += 1
    s.action_points -= 1
    # 移动会消耗燃油（受地图 move_cost、疲劳与天气影响）
    player_consumed = int(need)
    _consume_fuel_with_allies(s, cost, vehicle_model="虎式坦克", terrain=terrain_now)
    # 可选：向玩家显示本次消耗（玩家油箱）
    print(f"本次移动消耗燃油：{player_consumed}（当前剩余 {int(getattr(s, 'fuel', 0))}）")
    s.damage += 2
    print(f"你们转移到：{LOCATIONS[c]['name']}")
    s.clamp()

    # 移动后：若友军油箱见底，则触发索要（不给则离队）
    _handle_tank_ally_supply_requests(ins, s)
    # 增加遇到友军（坦克/步兵）的概率
    maybe_meet_friendly_infantry(s, reason="move")
    maybe_meet_friendly_tank(s, reason="move")
    maybe_trigger_event(ins, s, reason="move")
    # 友军在平时也可尝试搜刮补给（移动中/转移后触发），但每辆每回合仅尝试一次
    try:
        rnd = s.rng
        # 坦克友军尝试
        for t in getattr(s, "tank_allies", []):
            if not getattr(t, "alive", False):
                continue
            last = int(getattr(t, "last_scavenge_round", 0) or 0)
            if last == int(getattr(s, "round_number", 0) or 0):
                continue
            if int(getattr(t, "morale", 0) or 0) >= AUTO_RETAIN_SCAVENGE_MORALE and rnd.random() < ALLY_MOVE_SCAVENGE_P:
                print(f"{t.name} 车组在移动途中低调搜索补给...")
                event_reward_supply(s)
                setattr(t, "last_scavenge_round", int(getattr(s, "round_number", 0) or 0))
                s.clamp()
        # 步兵/杂项友军尝试（概率更低）
        for u in getattr(s, "allies", []):
            if not getattr(u, "alive", False):
                continue
            last = int(getattr(u, "last_scavenge_round", 0) or 0)
            if last == int(getattr(s, "round_number", 0) or 0):
                continue
            if int(getattr(u, "morale", 0) or 0) >= AUTO_RETAIN_SCAVENGE_MORALE and rnd.random() < INFANTRY_MOVE_SCAVENGE_P:
                print(f"友军小队 {getattr(u, 'name', 'Unknown')} 在移动间寻找补给...")
                event_reward_supply(s)
                setattr(u, "last_scavenge_round", int(getattr(s, "round_number", 0) or 0))
                s.clamp()
    except Exception:
        pass


def action_hold_position(ins: InputStream, s: GameState) -> None:
    """肃清敌人：本回合不移动，强制抗住两轮进攻。"""
    if s.action_points <= 0:
        return

    print("\n【肃清敌人】你选择在当前位置死守：敌人将发动两轮进攻。")

    # 肃清敌人视作占用本回合时间：直接结束本回合剩余行动点。
    s.action_points = 0

    wave_outcomes: List[str] = []
    for wave in (1, 2):
        if s.ended:
            return
        print(f"\n—— 肃清敌人进攻 第{wave}/2 轮 ——")
        out = resolve_encounter(
            ins,
            s,
            boss=False,
            encounter_mode="normal",
            ignore_battle_cap=True,
            post_reward_event=False,
        )
        wave_outcomes.append(str(out))
        if s.ended:
            return
        s.clamp()

    # 肃清敌人结束后的“结算奖励”：只触发一次奖励事件，并让辖区状态有显著变化。
    sec = s.sectors.get(s.location_key)
    held = ("escape" not in wave_outcomes) and ("ended" not in wave_outcomes) and ("skipped" not in wave_outcomes)
    cleared_both = (len(wave_outcomes) == 2) and all(o == "cleared" for o in wave_outcomes)

    if held:
        print("\n【肃清敌人结算】你们顶住了两轮进攻：街区里的人开始相信这条防线还能撑住。")
        if sec is not None:
            if cleared_both:
                sec.favor += 18
                sec.fall = max(0, sec.fall - 12)
            else:
                sec.favor += 10
                sec.fall = max(0, sec.fall - 7)
            sec.clamp()

        s.morale += 3
        s.counters["hold_success"] = int(s.counters.get("hold_success", 0) or 0) + 1

        # 触发一次奖励事件（作为肃清敌人整体奖励，而非每波都刷）
        post_encounter_reward_event(s, boss=False, outcome=("cleared" if cleared_both else "withdraw"))
        s.clamp()


def action_scavenge(ins: InputStream, s: GameState) -> None:
    s.action_points -= 1
    s.fuel -= 3
    s.damage += 1
    s.counters["scavenge"] = s.counters.get("scavenge", 0) + 1
    print("你命令乘员在周边快速搜索可用物资。")
    s.clamp()

    maybe_meet_friendly_tank(s, reason="scavenge")

    # 搜索可能触发遭遇
    battles_before = int(getattr(s, "battles_this_round", 0) or 0)
    # 若触发事件/遭遇则直接返回，避免连锁触发导致刷取收益
    if maybe_trigger_event(ins, s, reason="scavenge"):
        return
    # 搜索：提高“有收获/有事件”的体感
    s.buffs["搜索加成"] = 1
    random_event(ins, s)

    # 额外收获：有限概率获得一次额外补给（显著降低概率以避免刷取）
    if s.rng.random() < 0.10:
        print("\n（额外搜索收获）")
        event_reward_supply(s)


def action_assist(ins: InputStream, s: GameState) -> None:
    s.action_points -= 1
    s.fuel -= 6
    s.damage += 2
    s.counters["assist"] = s.counters.get("assist", 0) + 1
    print("你选择把行动用于支援撤离与救援。")

    # 若存在救援任务，提供一次“更像原作”的分支选择
    if s.rescue_missions:
        m = s.rescue_missions[0]
        print(f"\n【救援任务】{m.title}（剩余{max(0, m.expires_round - s.round_number)}回合）")
        print(m.desc)
        default_choice = "2" if ins.default_when_empty else "1"
        cc = choose(ins, "是否转为执行救援？(1-2)：", {"1": "执行救援", "2": "维持常规支援"}, default=default_choice)
        if cc == "1":
            # 救援比常规支援更危险，但收益更高
            sec = s.sectors.get(s.location_key)
            base_risk = 0.45 + m.difficulty
            if sec is not None:
                base_risk += (sec.fall - 50) * 0.004
                base_risk -= (sec.favor - 50) * 0.002
            if s.buffs.pop("观察", 0) > 0:
                base_risk -= 0.08
            if s.buffs.pop("求援", 0) > 0:
                base_risk -= 0.10
            base_risk = max(0.15, min(0.9, base_risk))

            if s.rng.random() < base_risk:
                s.damage += 10
                if sec is not None:
                    sec.fall += 6
                print("救援过程中遭遇阻击，你们只能撤回。")
            else:
                saved = s.rng.randint(1, 3)
                s.crew_saved += saved
                s.civilians_helped += 1
                s.victory_points += 4
                _quest_progress(s, "Q1", 1)
                if sec is not None:
                    sec.favor += 8
                    sec.fall = max(0, sec.fall - 3)
                print(f"救援成功：带回{saved}名人员。")

            # 尝试过就移除该任务（代表窗口关闭/已处理）
            s.rescue_missions.pop(0)
            s.clamp()
            return
    s.clamp()

    # 支援更容易遇到危险，但收益更稳定
    if maybe_trigger_event(ins, s, reason="assist"):
        return
    event_assist_evacuation(s)


def action_repair(ins: InputStream, s: GameState) -> None:
    def _terrain() -> str:
        return str(MAP_META.get(s.location_key, {}).get("terrain", "市区"))

    def _is_workshop() -> bool:
        # 工坊/修理厂等地点更适合检修
        return _terrain() in ("工坊", "修理厂")

    def _vehicle_faults() -> Dict[str, str]:
        # key -> 显示名
        return {
            "engine_damage": "发动机/传动故障",
            "turret_jam": "炮塔回转卡滞",
            "gun_breech": "主炮炮闩异常",
            "mg_jam": "机枪供弹卡滞",
            "radio_damage": "电台受扰",
            "optics_broken": "观瞄受损",
        }

    def _active_fault_keys() -> List[str]:
        keys = []
        for k in _vehicle_faults().keys():
            if s.debuffs.get(k, 0) > 0:
                keys.append(k)
        return keys

    def _show_faults() -> None:
        faults = _active_fault_keys()
        if not faults:
            print("车辆状态：未见明显关键故障。")
            return
        print("车辆故障：")
        names = _vehicle_faults()
        for k in faults:
            t = int(s.debuffs.get(k, 0))
            print(f"- {names.get(k, k)}（预计影响 {t} 回合）")

    def _repair_success_chance(*, use_toolbox: bool, workshop: bool) -> float:
        p = 0.82
        driver = crew_role_state(s, "驾驶员")
        if driver == "ok":
            p += 0.10
        elif driver == "wounded":
            p -= 0.06
        else:
            p -= 0.12
        if workshop:
            p += 0.14
        if use_toolbox:
            p += 0.08
        if s.damage >= 80:
            p -= 0.04
        if s.morale <= 30:
            p -= 0.03
        return max(0.35, min(0.98, p))

    def _apply_fault_fix(key: str, amount: int) -> bool:
        before = int(s.debuffs.get(key, 0))
        if before <= 0:
            return False
        after = max(0, before - amount)
        if after <= 0:
            s.debuffs.pop(key, None)
        else:
            s.debuffs[key] = after
        return True

    def _routine_maintenance() -> None:
        # 例行维护：结构修复为主，顺带压制一个故障
        if s.inventory.get("备件", 0) <= 0:
            s.morale -= 2
            print("没有备件，只能做些‘让人安心’的检查。")
            return
        spend_item(s, "备件", 1)
        s.damage = max(0, s.damage - 22)
        s.morale += 3

        faults = _active_fault_keys()
        if faults:
            p = 0.78
            if _is_workshop():
                p += 0.12
            driver = crew_role_state(s, "驾驶员")
            if driver == "ok":
                p += 0.08
            elif driver == "missing":
                p -= 0.08
            p = max(0.25, min(0.95, p))
            if s.rng.random() < p:
                k = s.rng.choice(faults)
                _apply_fault_fix(k, 2)
                print("例行维护：你们换下损坏部件，顺带把一处小故障压了下去。")
                return

        print("例行维护：你们换下损坏部件，暂时把问题压住。")

    def _targeted_field_repair(*, use_toolbox: bool, workshop: bool) -> None:
        faults = _active_fault_keys()
        if not faults:
            print("当前没有需要抢修的关键故障。")
            return

        # 消耗：优先备件；工具箱作为增益（可不消耗）
        if s.inventory.get("备件", 0) <= 0:
            print("没有备件：无法进行可靠的针对性抢修。")
            return

        names = _vehicle_faults()
        fmap: Dict[str, str] = {}
        for i, k in enumerate(faults, 1):
            fmap[str(i)] = f"{names.get(k, k)}（剩余{int(s.debuffs.get(k, 0))}回合）"
        raw = choose(ins, "选择要抢修的故障编号(回车取消)：", fmap, default="")
        if raw == "":
            return
        idx = int(raw)
        key = faults[idx - 1]

        spend_item(s, "备件", 1)
        p = _repair_success_chance(use_toolbox=use_toolbox, workshop=workshop)
        fix = 2
        if use_toolbox:
            fix += 1
        if workshop:
            fix += 1

        if s.rng.random() < p:
            _apply_fault_fix(key, fix)
            s.damage = max(0, s.damage - 6)
            s.morale += 2
            print("抢修成功：你们把故障暂时排除，车辆恢复了一些可靠性。")
        else:
            # 失败也可能“稍微缓解”，但会带来代价
            if s.rng.random() < 0.65:
                _apply_fault_fix(key, 2)
                print("抢修未完全成功：故障有所缓解，但仍未根治。")
            else:
                print("抢修失败：工具不趁手，问题被迫搁置。")
            s.morale -= 2
            s.damage = min(100, s.damage + 1)

    def _toolbox_overhaul(*, workshop: bool) -> None:
        if s.inventory.get("工具箱", 0) <= 0:
            print("没有工具箱。")
            return
        spend_item(s, "工具箱", 1)
        # 直接复用道具效果（并在此处额外给一点“检修收益”）
        apply_item_effect(s, "工具箱")
        if workshop:
            # 工坊里更容易把细小问题处理到位
            fixed = False
            for k in _vehicle_faults().keys():
                if s.debuffs.get(k, 0) > 0:
                    _apply_fault_fix(k, 1)
                    fixed = True
            if fixed:
                print("工坊条件加成：进一步排除了残余故障。")

    def _time_only_troubleshoot(*, workshop: bool) -> None:
        """仅耗时排障：不消耗备件/工具箱，但成功率较低。"""
        faults = _active_fault_keys()
        if not faults:
            print("当前没有需要排除的关键故障。")
            return

        names = _vehicle_faults()
        fmap: Dict[str, str] = {}
        for i, k in enumerate(faults, 1):
            fmap[str(i)] = f"{names.get(k, k)}（剩余{int(s.debuffs.get(k, 0))}回合）"
        raw = choose(ins, "选择要排除的故障编号(回车取消)：", fmap, default="")
        if raw == "":
            return
        idx = int(raw)
        key = faults[idx - 1]

        # 成功率：比“备件抢修”更低，但工坊与驾驶员状态仍有帮助
        p = 0.62
        driver = crew_role_state(s, "驾驶员")
        if driver == "ok":
            p += 0.08
        elif driver == "wounded":
            p -= 0.05
        else:
            p -= 0.09
        if workshop:
            p += 0.10
        if s.damage >= 80:
            p -= 0.03
        if s.morale <= 30:
            p -= 0.03
        p = max(0.20, min(0.90, p))

        if s.rng.random() < p:
            _apply_fault_fix(key, 1)
            s.morale += 1
            print("排障成功：你们花时间把故障暂时压了下去。")
        else:
            # 失败也可能“摸索出一点门道”
            if s.rng.random() < 0.45:
                _apply_fault_fix(key, 2)
                print("排障未完全成功：故障有所缓解，但仍会影响作战。")
            else:
                print("排障失败：缺少工具与备件，问题只能先搁置。")
            s.morale -= 1

    # 选择检修方式（允许取消，不消耗行动点）
    print("你们准备进行修理/维护。")
    _show_faults()
    workshop = _is_workshop()
    tag = "（此处更适合检修）" if workshop else ""
    print(f"地点：{LOCATIONS[s.location_key]['name']}｜地形:{_terrain()}{tag}")

    menu: Dict[str, str] = {
        "1": "例行维护：消耗1备件（损伤-22，士气+3；较高概率压制一处故障）",
        "2": "针对故障抢修：消耗1备件（选择一处故障，成功则显著缓解/解除）",
    }
    menu["9"] = "耗时排障：仅消耗1行动点（不消耗备件/工具箱；选择一处故障，低概率缓解）"
    if s.inventory.get("工具箱", 0) > 0:
        menu["3"] = "使用工具箱彻底检修：消耗1工具箱（损伤-20，士气+5，并压制多处故障）"
    if workshop:
        menu["4"] = "工坊大修：额外消耗1行动点（更高成功率/更强故障缓解）"
    menu["0"] = "取消"

    default_choice = "1" if not ins.default_when_empty else "0"
    c = choose(ins, "选择(0-4/9)：", menu, default=default_choice)
    if c == "0":
        return

    # 费用：基础1行动点；工坊大修额外+1
    cost_ap = 1
    if c == "4":
        cost_ap = 2
    if s.action_points < cost_ap:
        print("行动点不足。")
        return

    s.action_points -= cost_ap
    s.counters["repairs"] = s.counters.get("repairs", 0) + 1

    if c == "1":
        _routine_maintenance()
    elif c == "2":
        _targeted_field_repair(use_toolbox=False, workshop=False)
    elif c == "3":
        _toolbox_overhaul(workshop=False)
    elif c == "9":
        _time_only_troubleshoot(workshop=workshop)
    else:
        # 工坊大修：两种方式都加强（有工具箱则优先用工具箱，否则用备件抢修）
        print("你们把车开进更可靠的修理点，开始更彻底的检修。")
        if s.inventory.get("工具箱", 0) > 0:
            _toolbox_overhaul(workshop=True)
        else:
            _targeted_field_repair(use_toolbox=False, workshop=True)
            # 工坊里顺带做一点结构维护（若还有备件则再压一层）
            if s.inventory.get("备件", 0) > 0 and s.rng.random() < 0.75:
                spend_item(s, "备件", 1)
                s.damage = max(0, s.damage - 12)
                print("工坊顺带处理了几处结构松动：损伤-12。")

    s.clamp()


def action_rest(ins: InputStream, s: GameState) -> None:
    s.action_points -= 1
    print("你允许乘员短暂休整。")
    s.counters["rest"] = s.counters.get("rest", 0) + 1
    # 疲劳下降：让驾驶/机动的长期代价更现实
    s.counters["fatigue"] = max(0, int(s.counters.get("fatigue", 0)) - 10)
    s.morale += 4
    s.damage = max(0, s.damage - 2)
    s.fuel -= 1
    # 休整缓解乘员压力
    relieve_crew_stress(s, amount=4, mode="all", include_commander=True)
    s.clamp()
    # 休整也可能被打断
    maybe_trigger_event(ins, s, reason="rest")


def action_menu(ins: InputStream, s: GameState) -> None:
    while True:
        print("\n管理：")
        c = choose(
            ins,
            "选择(0-8)：",
            {
                "0": "退出/返回",
                "1": "查看/使用背包物品",
                "2": "使用技能",
                "3": "查看任务/委托/勋章",
                "4": "查看乘员",
                "5": "查看辖区",
                "9": "查看耗弹报告",
                "7": "委派任务/任务板",
                "8": "存档",
            },
            default="0",
        )
        if c == "1":
            enc_before = int(s.counters.get("encounters", 0) or 0)
            menu_inventory(ins, s)
            enc_after = int(s.counters.get("encounters", 0) or 0)
            if enc_after > enc_before:
                print("\n（混乱中带回一些补给）")
                event_reward_supply(s)
                event_reward_supply(s)
                s.clamp()
        elif c == "2":
            menu_skills(ins, s)
        elif c == "3":
            # 刷新：任务/任务板/委托在同一回合内也能即时更新
            try:
                ensure_side_quests(s, keep=2)
                sync_counter_quests(s)
                complete_quests_if_any(s)
                sync_commissions(s)
            except Exception:
                pass
            show_task_requirements()
            show_quests(s)
            show_commissions(s)
            show_rescue_missions(s)
            show_delegated_tasks(s)
            print(f"当前勋章：{get_rank(s.victory_points)}")
            show_achievements(s)
            print(f"{crew_status_summary(s)}")
            show_task_log(s)
        elif c == "4":
            show_crew(s)
        elif c == "5":
            show_sector_overview(s)
        elif c == "0":
            return
        elif c == "7":
            show_rescue_missions(s)
            show_delegated_tasks(s)
            menu_delegation(ins, s)
        elif c == "8":
            menu_save_game(ins, s)
        elif c == "9":
            show_ammo_usage_report(s)
        else:
            return


def can_call_garrison(s: GameState) -> Tuple[bool, str]:
    sec = s.sectors.get(s.location_key)
    if sec is None:
        return False, "无辖区数据"
    if len(sec.garrison_units) <= 0:
        return False, "无驻军单位"
    if sec.favor < 50:
        return False, "好感不足（需要≥50）"
    if sec.fall >= 70:
        return False, "沦陷过高（需要<70）"
    return True, ""


def can_recruit_crew(s: GameState) -> Tuple[bool, str]:
    sec = s.sectors.get(s.location_key)
    if sec is None:
        return False, "无辖区数据"
    missing = crew_missing_roles(s)
    if not missing:
        return False, "车组岗位齐全"
    if sec.favor < 55:
        return False, "好感不足（需要≥55）"
    if sec.fall >= 70:
        return False, "沦陷过高（需要<70）"
    return True, ""


def _pick_unique_crew_name(s: GameState) -> str:
    used = {m.name for m in s.crew}
    name = s.rng.choice(CREW_NAMES)
    tries = 0
    while name in used and tries < 50:
        name = s.rng.choice(CREW_NAMES)
        tries += 1
    if name in used:
        name = f"新兵{len(used) + 1}"
    return name


def action_recruit_crew(ins: InputStream, s: GameState, sec: SectorState) -> None:
    missing = crew_missing_roles(s)
    if not missing:
        print("车组岗位齐全，无需招募。")
        return

    options: Dict[str, str] = {str(i + 1): f"补齐：{role}" for i, role in enumerate(missing)}
    options["0"] = "返回"
    c = choose(ins, "选择要招募的岗位(0返回)：", options, default="0")
    if c == "0":
        return
    if c not in options:
        print("无效选择。")
        return

    role = missing[int(c) - 1]
    s.action_points -= 1

    # 成功率：以好感/沦陷/士气为主；有“电台电量”时略有加成（视作联络渠道更稳定）
    p = 0.45
    p += max(0, sec.favor - 55) * 0.006
    p -= max(0, sec.fall - 45) * 0.010
    p += (s.morale - 50) * 0.003
    if s.buffs.get("电台电量", 0) > 0:
        p += 0.08
    p = max(0.10, min(0.80, p))

    if s.rng.random() < p:
        name = _pick_unique_crew_name(s)
        hp = s.rng.randint(70, 100)
        stress = s.rng.randint(8, 28)
        # 新加入（招募）：熟练度初始为 0（原状态），后续通过战斗/事件成长
        s.crew.append(CrewMember(role=role, name=name, hp=hp, stress=stress, proficiency=0))
        sec.favor = min(100, sec.favor + 2)
        s.morale += 1
        print(f"招募成功：{role}-{name} 加入车组（HP{hp}，压力{stress}）。")
    else:
        sec.favor = max(0, sec.favor - 4)
        sec.fall = min(100, sec.fall + 1)
        s.morale -= 2
        print("招募失败：没人愿意上车，气氛也更紧张了。")

    sec.clamp()
    s.clamp()


def action_garrison(ins: InputStream, s: GameState) -> None:
    print("\n驻军/辖区操作：")
    sec = s.sectors.get(s.location_key)
    if sec is None:
        print("当前区域无辖区数据。")
        return
    print(f"当前辖区：好感{sec.favor} 沦陷{sec.fall} 驻军{len(sec.garrison_units)}")
    print(f"钱包：{wallet_text(s)}")
    if sec.garrison_units:
        preview = "、".join(u.name for u in sec.garrison_units[:6])
        if len(sec.garrison_units) > 6:
            preview += "…"
        print(f"驻军单位：{preview}")
    ok, why = can_call_garrison(s)
    call_text = "呼叫驻军支援（下一场遭遇战并肩作战）" if ok else f"呼叫驻军支援（不可用：{why}）"

    ok_r, why_r = can_recruit_crew(s)
    recruit_text = "招募车组成员（补齐缺员，消耗1行动）" if ok_r else f"招募车组成员（不可用：{why_r}）"

    has_medic = any(u.unit_type == "医疗组" for u in sec.garrison_units)
    medic_text = "医疗组协助（缓解乘员伤情/压力）" if has_medic else "医疗组协助（不可用：无医疗组）"

    c = choose(
        ins,
        "选择(0-7)：",
        {
            "0": "返回",
            "1": call_text,
            "2": "尝试招募驻军（需要好感≥60 且沦陷<55；成功+1单位）",
            "3": recruit_text,
            "4": medic_text,
            "5": "查看驻军详情",
            "6": "分发补给给驻军（提升好感，消耗1行动）",
            "7": "驻军交易（用金条/通行证换补给）",
        },
        default="0",
    )
    if c == "0":
        return
    if c == "1":
        ok2, why2 = can_call_garrison(s)
        if not ok2:
            print(f"无法呼叫：{why2}")
            return
        used = sec.garrison_units.pop(0)
        s.deployed_garrison.append((s.location_key, used))
        s.morale += 2
        print(f"{used.name} 将在下一次遭遇战与你并肩作战（战斗后返回辖区）。")
        s.action_points -= 1
        s.clamp()
    elif c == "2":
        if sec.favor < 60 or sec.fall >= 55:
            print("条件不足。")
            return
        s.action_points -= 1
        # 成功率受沦陷影响
        p = 0.55 - (sec.fall - 35) * 0.01
        p = max(0.15, min(0.75, p))
        if s.rng.random() < p:
            # 新招募单位也可能根据当前地形有所取向
            terrain = MAP_META.get(s.location_key, {}).get("terrain")
            sec.garrison_units.append(_make_garrison_unit(s.rng, terrain))
            sec.favor += 2
            print("你在街区里临时组织到一小队支援力量。")
        else:
            sec.favor = max(0, sec.favor - 3)
            sec.fall += 2
            print("招募失败：人心更散，街区更乱。")
        s.clamp()
    elif c == "3":
        ok3, why3 = can_recruit_crew(s)
        if not ok3:
            print(f"无法招募：{why3}")
            return
        action_recruit_crew(ins, s, sec)
        return
    elif c == "4":
        if not has_medic:
            print("没有医疗组。")
            return
        s.action_points -= 1
        idx = None
        for i, u in enumerate(sec.garrison_units):
            if u.unit_type == "医疗组":
                idx = i
                break
        if idx is None:
            print("没有医疗组。")
            return
        used = sec.garrison_units.pop(idx)
        healed = 0
        for m in s.crew:
            if m.alive and m.hp < 100:
                m.hp = min(100, m.hp + 10)
                healed += 1
            if m.alive:
                m.stress = max(0, m.stress - 6)
        s.morale += 3
        sec.favor += 3
        print(f"{used.name} 完成了紧急处置（影响乘员{healed}人）。")
        s.clamp()
    elif c == "5":
        if not sec.garrison_units:
            print("当前无驻军单位。")
            return
        print("\n驻军详情：")
        for u in sec.garrison_units:
            armor = f" 装甲{u.armor}" if getattr(u, "armor", 0) > 0 else ""
            print(f"- {u.name}：类型{u.unit_type} HP{u.hp}{armor} 战力{u.power} 士气{u.morale}")
        return
    elif c == "6":
        if s.action_points <= 0:
            print("行动点不足。")
            return

        # 可用于“提升好感”的物资：越偏救援/照顾，提升越高
        cand: List[Tuple[str, int]] = []
        for it, gain in (
            ("医疗包", 7),
            ("药品", 6),
            ("急救包", 5),
            ("弹药箱", 4),
            ("燃油桶", 3),
            ("香烟", 3),
            ("咖啡", 3),
        ):
            if s.inventory.get(it, 0) > 0:
                cand.append((it, gain))

        if not cand:
            print("你想分发补给以安抚驻军，但背包里没有合适的物资。")
            return

        menu: Dict[str, str] = {str(i + 1): f"{it}（好感+{gain}）" for i, (it, gain) in enumerate(cand)}
        menu["0"] = "取消"
        pick = choose(ins, "选择要分发的物资：", menu, default="0")
        if pick == "0":
            return
        try:
            idx = int(pick) - 1
        except ValueError:
            print("无效选择。")
            return
        if idx < 0 or idx >= len(cand):
            print("无效选择。")
            return

        it, gain = cand[idx]
        if not spend_item(s, it, 1):
            print("物资不足。")
            return

        s.action_points -= 1
        sec.favor = min(100, sec.favor + int(gain))
        sec.fall = max(0, sec.fall - 1)
        s.morale += 1
        print(f"你把 {it} 分给驻军与伤员：街区情绪明显缓和（好感+{gain}）。")
        s.clamp()
    elif c == "7":
        action_garrison_trade(ins, s, sec)
        return
    else:
        return


def action_garrison_trade(ins: InputStream, s: GameState, sec: SectorState) -> None:
    """驻军交易：用金条/通行证交换补给（不消耗行动点）。"""
    while True:
        print("\n驻军交易：")
        print(f"钱包：{wallet_text(s)}")

        # 好感越高可买的越多（通行证商品偏稀有）
        favor = int(sec.favor)
        goods: List[Dict[str, object]] = [
                {"key": "FUEL", "label": "燃油桶 x1", "gold": 1, "passes": 0, "give_item": ("燃油桶", 1), "need_favor": 0},
                {"key": "PURE_FUEL", "label": "纯燃料桶 x1", "gold": 3, "passes": 0, "give_item": ("纯燃料桶", 1), "need_favor": 30},
            {"key": "AMMO_BOX", "label": "弹药箱 x1", "gold": 1, "passes": 0, "give_item": ("弹药箱", 1), "need_favor": 45},
            {"key": "SPARE", "label": "备件 x1", "gold": 1, "passes": 0, "give_item": ("备件", 1), "need_favor": 45},
            {"key": "BAT", "label": "电台电池 x1", "gold": 1, "passes": 0, "give_item": ("电台电池", 1), "need_favor": 45},
            {"key": "SMOKE", "label": "烟幕弹 x1", "gold": 0, "passes": 1, "give_item": ("烟幕弹", 1), "need_favor": 60},
            {"key": "SHELL_BOX", "label": "炮弹箱 x1", "gold": 1, "passes": 0, "give_item": ("炮弹箱", 1), "need_favor": 55},
            {"key": "TOOL", "label": "工具箱 x1", "gold": 2, "passes": 0, "give_item": ("工具箱", 1), "need_favor": 55},
            {"key": "PLATE", "label": "装甲板 x1", "gold": 0, "passes": 1, "give_item": ("装甲板", 1), "need_favor": 65},
            {"key": "MEDS", "label": "药品 x1", "gold": 1, "passes": 0, "give_item": ("药品", 1), "need_favor": 55},
            {"key": "MEDKIT", "label": "医疗包 x1", "gold": 2, "passes": 0, "give_item": ("医疗包", 1), "need_favor": 60},
            {"key": "RATION", "label": "口粮 x1", "gold": 1, "passes": 0, "give_item": ("口粮", 1), "need_favor": 55},
            {"key": "OIL", "label": "润滑油 x1", "gold": 1, "passes": 0, "give_item": ("润滑油", 1), "need_favor": 60},
            {"key": "CAMO", "label": "伪装网 x1", "gold": 0, "passes": 1, "give_item": ("伪装网", 1), "need_favor": 70},
            {"key": "RECON", "label": "侦察设备 x1", "gold": 0, "passes": 1, "give_item": ("侦察设备", 1), "need_favor": 70},
            {"key": "COFFEE", "label": "咖啡 x1", "gold": 1, "passes": 0, "give_item": ("咖啡", 1), "need_favor": 62},
        ]

        menu: Dict[str, str] = {"0": "返回"}
        idx_to_good: Dict[str, Dict[str, object]] = {}
        n = 1
        for g in goods:
            need = int(g.get("need_favor", 0) or 0)
            gold_flag = int(g.get("gold", 0) or 0)
            pas = int(g.get("passes", 0) or 0)
            if favor < need:
                continue
            price = []
            # 若商品以金条标价（gold_flag>0），则用地区定价表获得基础单价并乘以标记系数
            if gold_flag > 0:
                give_item = g.get("give_item")
                base_name = None
                if isinstance(give_item, tuple) and len(give_item) >= 1:
                    base_name = give_item[0]
                if base_name:
                    item_price = get_item_price(s, base_name)
                else:
                    item_price = 1
                price_amount = max(1, int(item_price * max(1, gold_flag)))
                price.append(f"金条{price_amount}")
            if pas > 0:
                price.append(f"通行证{pas}")
            price_text = "、".join(price) if price else "免费"
            key = str(n)
            menu[key] = f"{str(g.get('label'))}（{price_text}）"
            # 把计算得到的价格写回商品结构，便于后续消费检查
            g["_computed_gold"] = price_amount if gold_flag > 0 else 0
            idx_to_good[key] = g
            n += 1

        if len(menu) <= 1:
            print("好感不足：当前没有可交易的物资。")
            return

        pick = choose(ins, "选择：", menu, default="0")
        if pick == "0":
            return
        good = idx_to_good.get(pick)
        if not good:
            print("无效选择。")
            continue

        gold = int(good.get("_computed_gold", int(good.get("gold", 0) or 0)) or 0)
        pas = int(good.get("passes", 0) or 0)
        # 检查本地区购买额度（出售无额度限制）
        remaining = region_purchase_remaining(s, s.location_key)
        if remaining <= 0:
            print("本地区购买额度已用尽。")
            continue

        if not spend_currency(s, gold=gold, passes=pas):
            print("货币不足。")
            continue

        give_item = good.get("give_item")
        if isinstance(give_item, tuple) and len(give_item) == 2:
            name, qty = give_item
            if isinstance(name, str):
                add_item(s, name, int(qty) if isinstance(qty, int) else 1)
                # 扣减地区购买额度（按每笔交易计一次）
                try:
                    region_consume_purchase(s, s.location_key, 1)
                    rem = region_purchase_remaining(s, s.location_key)
                    print(f"交易完成：获得 {name} x{int(qty) if isinstance(qty, int) else 1}。本地区剩余购买额度：{rem}")
                except Exception:
                    print(f"交易完成：获得 {name} x{int(qty) if isinstance(qty, int) else 1}。")
        s.clamp()


def _maybe_trigger_questline_q5(ins: InputStream, s: GameState, *, reason: str) -> bool:
    """任务线Q5：失真信号（多阶段分支）。

    返回 True 表示本次行动被剧情事件占用。
    设计约束：不新增菜单/页面；复用 story_choice；只在非自检模式触发。
    """
    if SELFTEST:
        return False

    q = next((q for q in getattr(s, "quests", []) if q.id == "Q5"), None)
    if q is None or q.done:
        return False

    stage = int(getattr(s, "story_vars", {}).get("q5_stage", 0) or 0)

    def _advance() -> None:
        s.story_vars["q5_stage"] = stage + 1
        try:
            _quest_progress(s, "Q5", 1)
        except Exception:
            pass

    def _default() -> str:
        return "2" if ins.default_when_empty else "1"

    # 阶段1：听到失真的广播（需要一定回合数；更偏“移动”时触发）
    if stage <= 0 and s.round_number >= 4 and reason in ("move", "scavenge"):
        def _record_with_battery() -> None:
            # “消耗电台电池x1”是承诺；若不足，则给出明确提示并回退为“笔记记录”
            if spend_item(s, "电台电池", 1):
                s.story_flags["q5_s1_used_battery"] = True
                s.buffs["侦察"] = max(1, int(s.buffs.get("侦察", 0) or 0))
            else:
                s.story_flags["q5_s1_used_battery"] = False
                s.story_flags["q5_s1_notes"] = True
                print("电台箱里已经找不到可用电池：你们只能靠耳朵与笔记把断续信号记下。")

        def _record_with_notes() -> None:
            s.story_flags["q5_s1_notes"] = True

        story_choice(
            ins,
            s,
            event_id="Q5_S1",
            title="失真信号",
            text=(
                "\n车内电台忽然钻出一段断续的广播：像坐标，又像口令。\n"
                "通信员说：如果把它记录下来，或许能换来一条‘更不坏的路’。\n"
            ),
            options={
                "1": (
                    "立刻用电池稳住频段并记录（消耗电台电池x1；获得一次侦察优势）",
                    _record_with_battery,
                ),
                "2": (
                    "只凭耳朵与笔记记下（不消耗；但信息更粗糙）",
                    _record_with_notes,
                ),
            },
            default=_default(),
        )

        # 已织密频段则更容易把记录保存得更好
        if bool(getattr(s, "story_flags", {}).get("signal_net", False)):
            s.morale += 1
        _advance()
        s.clamp()
        return True

    # 阶段2：如何处理记录（偏“支援/移动”触发，让玩家有选择权）
    if stage == 1 and s.round_number >= 6 and reason in ("assist", "move"):
        def _share() -> None:
            s.story_flags["q5_shared_garrison"] = True
            sec = s.sectors.get(s.location_key)
            if sec is not None:
                sec.favor += 6
                sec.fall = max(0, sec.fall - 1)
                sec.clamp()
            s.morale += 2

        def _trade() -> None:
            s.story_flags["q5_trade"] = True
            s.gold_bars += 1
            s.morale += 1

        story_choice(
            ins,
            s,
            event_id="Q5_S2",
            title="把坐标交给谁",
            text=(
                "\n你们把断续信号拼成一串坐标。\n"
                "通信员说：交给驻军，可能换来更多协助；留在手里，也许能换到关键补给。\n"
            ),
            options={
                "1": ("交给驻军：换取信任（好感↑；士气↑）", _share),
                "2": ("留作交换：换取筹码（金条+1；士气小幅↑）", _trade),
            },
            default=_default(),
        )
        _advance()
        s.clamp()
        return True

    # 阶段3：高潮点（建议在移动时触发；可选打一场“清理街口”的战斗）
    if stage == 2 and s.round_number >= 8 and reason == "move":
        def _fight_and_relay() -> None:
            # 触发一场遭遇作为“争取架设时间”的代价
            out = resolve_encounter(ins, s, boss=False)
            if out == "cleared":
                s.victory_points += 3
                s.morale += 3
                s.story_flags["escape_intel"] = True
                s.buffs["侦察"] = max(1, int(s.buffs.get("侦察", 0) or 0))
                print("你们趁街口安静把天线架起：一条更清晰的路线被记录下来。")
            else:
                s.victory_points += 1
                s.morale += 1
                s.story_flags["escape_intel"] = True
                print("你们只来得及做短暂联络：路线仍然模糊，但至少有了方向。")

        def _quick_ping() -> None:
            s.victory_points += 1
            s.morale += 2
            s.story_flags["escape_intel"] = True
            s.buffs["侦察"] = max(1, int(s.buffs.get("侦察", 0) or 0))
            print("你们只做了短促联络：把下一段路变得更可控一点。")

        story_choice(
            ins,
            s,
            event_id="Q5_S3",
            title="屋顶上的回声",
            text=(
                "\n你们抵达一处能勉强抬高天线的废楼。\n"
                "只要争取几分钟，就能把坐标发出去，换回更清晰的路。\n"
            ),
            options={
                "1": ("清理街口并架设天线（会触发一场战斗；回报更高）", _fight_and_relay),
                "2": ("只做短暂联络立刻撤离（无战斗；回报较小）", _quick_ping),
            },
            default=_default(),
        )

        _advance()
        s.clamp()
        return True

    return False


def maybe_trigger_event(ins: InputStream, s: GameState, *, reason: str) -> bool:
    """返回 True 表示本次行动被事件占用/已处理（比如战斗）。"""
    # 自检：强制触发一次遭遇战（用于覆盖战斗分支）
    if SELFTEST and bool(_selftest_pop("force_next_encounter", False)):
        print("\n[自检] 强制触发遭遇战。")
        resolve_encounter(ins, s, boss=False)
        return True

    # 剧情/选择导致的“额外遭遇”：强制触发一次
    if s.buffs.pop("额外遭遇", 0) > 0:
        resolve_encounter(ins, s, boss=False)
        return True

    # 侦察设备/侦察优势：仅对“移动”生效，避免一次随机遭遇
    if reason == "move" and s.buffs.pop("侦察", 0) > 0:
        return False

    # 当地驻军遇袭事件：与驻军并肩作战
    sec0 = s.sectors.get(s.location_key)
    if sec0 is not None and sec0.garrison_units:
        live_units = [u for u in sec0.garrison_units if u.alive]
        if live_units:
            # 沦陷越高越容易遇袭；休整时略低，外出行动略高
            attack_chance = 0.1 + max(0, sec0.fall - 45) * 0.008  # 大量增加基础概率
            if reason in ("move", "scavenge", "assist"):
                attack_chance += 0.02
            if reason == "rest":
                attack_chance -= 0.02
            attack_chance = max(0.0, min(0.40, attack_chance))  # 提高上限

            if s.rng.random() < attack_chance:
                print("\n⚠️ 当地驻军遭遇袭击！")
                # 询问是否参加
                choices = {
                    "1": "介入战斗，与驻军并肩作战",
                    "2": "不参加，让驻军自行应对"
                }
                choice = choose(ins, "选择行动：", choices)
                if choice == "1":
                    print("你们决定介入，与驻军并肩作战。")
                    picked = s.rng.sample(live_units, k=min(2, len(live_units)))
                    outcome = resolve_encounter(ins, s, boss=False, garrison_allies=picked)

                    # 额外后果：在“遇袭”情境下更可能出现驻军减员
                    if outcome in ("ended",):
                        return True

                    # 规则：只要当前敌人列表清空（outcome == "cleared"）就算战斗成功
                    if outcome == "cleared":
                        extra_loss_p = 0.10
                        extra_loss_p = max(0.0, min(0.55, extra_loss_p))
                        if s.rng.random() < extra_loss_p:
                            victim = s.rng.choice([u for u in picked if u.alive] or picked)
                            victim.alive = False
                            print(f"驻军损失：{victim.name} 在混乱中掉队。")
                        print("敌人被清除，辖区局势暂时稳定。")
                    else:
                        extra_loss_p = 0.30
                        extra_loss_p = max(0.0, min(0.55, extra_loss_p))
                        if s.rng.random() < extra_loss_p:
                            victim = s.rng.choice([u for u in picked if u.alive] or picked)
                            victim.alive = False
                            print(f"驻军损失：{victim.name} 在混乱中掉队。")
                        sec0.fall += 15  # 敌人未清空 -> 大量增加沦陷度
                        print("敌人未被完全清除，辖区沦陷度大幅上升：+15")
                else:
                    print("你们选择不介入，让驻军自行应对。")
                    # 不参加，但可能有后果，增加沦陷度
                    sec0.fall += 10  # 大量增加
                    print("未介入战斗，辖区沦陷度上升：+10")
                sec0.clamp()
                s.clamp()
                return True

    # 多阶段任务线：优先于随机遭遇触发（占用本次行动）
    if _maybe_trigger_questline_q5(ins, s, reason=reason):
        return True

    base = LOCATIONS[s.location_key]["risk"] * float(state_danger(s))
    if reason == "assist":
        base += 0.06
    elif reason == "scavenge":
        base += 0.04
    elif reason == "rest":
        base -= 0.14
    elif reason == "move":
        base += 0.01

    if s.buffs.get("观察", 0) > 0:
        base -= 0.10
    if s.damage >= 75:
        base += 0.04
    if s.fuel <= 8:
        base += 0.04

    # 伪装网：降低遭遇概率（按回合递减）
    if s.buffs.get("伪装", 0) > 0:
        base *= 0.80

    # 章节分支：关键抉择影响遭遇频率（轻量修正，避免数值膨胀）
    flags = dict(getattr(s, "story_flags", {}) or {})
    if bool(flags.get("trusted_signs", False)):
        base -= 0.030 if reason == "move" else 0.015
    if bool(flags.get("camo_prepared", False)):
        base *= 0.92
    if bool(flags.get("night_stealth", False)) and reason == "move":
        base -= 0.035
    if bool(flags.get("intel_saved", False)) or bool(flags.get("escape_intel", False)):
        base -= 0.012
    if bool(flags.get("signal_net", False)):
        base -= 0.010
    # 根据区域的事件修正（MAP_META 中的 event_mod）调整触发率
    region_mod = 0.0
    meta = MAP_META.get(s.location_key)
    if isinstance(meta, dict):
        region_mod = float(meta.get("event_mod", 0.0))
    # 使用乘法因子避免直接偏移基线过大
    base = base * (1.0 + region_mod)
    # 天气：对遭遇概率做轻量加法修正
    try:
        base += float(weather_effects(s).get("encounter_delta", 0.0))
    except Exception:
        pass
    base = max(0.05, min(0.85, base))

    # 强行推进：更容易撞上交火区
    if s.buffs.get("强行推进", 0) > 0:
        base = min(0.95, base + 0.08)

    if s.rng.random() < base:
        encounter_mode = "breakout" if s.difficulty_key == "突围" else "normal"
        # 如果此次遭遇源自搜刮行为，则标记以便战后触发一次额外的搜索掉落
        try:
            if reason == "scavenge":
                setattr(s, "_last_encounter_reason", "scavenge")
        except Exception:
            pass
        resolve_encounter(ins, s, boss=False, encounter_mode=encounter_mode)
        return True
    return False


def boss_check_and_run(ins: InputStream, s: GameState) -> None:
    # 设计变更：去除BOSS强制战斗/关键回合机制。
    return


def attempt_escape(ins: InputStream, s: GameState) -> None:
    narrate(
        """
【郊外缺口】
地图碎片拼合出一条不确定的路线：一处防线的空隙、一段还能通行的路。
你知道它不保证安全，但它至少提供‘离开市区’的可能。
"""
    )
    status_line(s)

    if s.fuel < 25:
        print("燃油不足以支撑突围，缺口对你来说还太远。")
        s.morale -= 2
        s.clamp()
        return
    if s.damage >= 85:
        print("车辆损伤过重，强行突围很可能半途抛锚。")

    c = choose(
        ins,
        "是否尝试突围？(1-2)：",
        {"1": "尝试突围", "2": "暂缓"},
        default="2",
    )
    if c != "1":
        return

    # 成功率：受士气/损伤/燃油影响，也受任务完成度与剧情分支影响
    success = 0.45
    success += 0.12 if s.morale >= 60 else 0.0
    success -= 0.18 if s.damage >= 80 else 0.0
    success += 0.10 if s.victory_points >= 18 else 0.0
    if s.buffs.pop("求援", 0) > 0:
        success += 0.10

    # 章节分支：情报/乘员优先会提高突围成功率
    if s.story_flags.get("escape_intel", False):
        success += 0.04
    if s.story_flags.get("intel_saved", False):
        success += 0.02
    if s.story_flags.get("crew_first", False):
        success += 0.03
    if s.story_flags.get("trusted_signs", False):
        success -= 0.01

    route = int(s.story_vars.get("breakout_route", 0) or 0)
    if route == 1:
        success += 0.07
    elif route == 2:
        success += 0.03
    elif route == 3:
        success += 0.05

    if s.story_flags.get("shared_fuel", False):
        # 你分出去的东西，可能换回一条路
        success += 0.04
    if s.story_flags.get("saved_orphans", False):
        success += 0.03
    success = max(0.1, min(0.8, success))

    # 代价
    s.fuel -= 22
    s.damage += 8
    s.action_points = 0
    s.clamp()

    print("\n 你们必须在火线上连续撕开四道缺口。")

    # 突围：连续四次大型战斗（全近卫，坦克比例显著提高）
    for i in range(1, 5):
        print(f"\n【突围战 {i}/4】")
        outcome = resolve_encounter(ins, s, boss=False, encounter_mode="breakout_large")
        if outcome == "ended":
            return
        if outcome != "cleared":
            break

    if outcome == "cleared" and i == 4:
        # 更佳结局：带着更多人、且代价更小
        if s.civilians_helped >= 3 and s.crew_saved >= 2 and s.crew_lost <= 0 and s.victory_points >= 100:
            end_game(
                s,
                "E01",
                "带着更多人走出",
                """
你们在灰色的边界线上前进，尽量避开主要交火区。
你知道这不是胜利，但你清楚自己做过一些‘不让人被钢铁吞掉’的选择。

天亮之前，你们离开了最密的市区。
你回头看了一眼废墟，心里只有一句话：我至少把更多人带出了那里。
""",
            )
        else:
            end_game(
                s,
                "E02",
                "穿过灰色边界",
                """
你们沿着废墟与树影的边界前进，避开主要交火区。
在天亮前，你们终于离开了最密的市区。

这不是胜利，也不是洗白，只是你对自己说：至少，我把几个人带出了那片瓦砾。
""",
            )
        return

    # 失败：被迫弃车或投降
    if s.damage >= 95 or s.fuel <= 0:
        if s.story_flags.get("scuttle_prepared", False):
            end_game(
                s,
                "E14",
                "抛锚在半路（已准备自毁）",
                """
你们接近缺口时，车辆抛锚。
你用最短的口令完成了最后的准备：别让它成为谁的战利品。

爆裂声隔在身后，你让乘员分散撤离——这一次，你至少少了一件需要背负的东西。
""",
            )
            return
        end_game(
            s,
            "E13",
            "抛锚在半路",
            """
你们接近缺口时，车辆抛锚。
你让乘员带上能带的东西离开，剩下的只能留给铁与火。

你并不确定所有人都能走出去，但你仍然把‘活下去’当作最后的命令。
""",
        )
        return

    end_game(
        s,
        "E03",
        "停火与余生",
        """
突围失败后，你判断继续抵抗只会带来更多无谓的死亡。
你选择停火、投降，尽力确保乘员生还。
""",
    )


def end_conditions(s: GameState) -> Optional[Tuple[str, str, str]]:
    # 车长阵亡：游戏立刻结束
    commander = next((m for m in s.crew if m.role == "车长"), None)
    if commander is not None and (not commander.alive or commander.hp <= 0):
        return (
            "E11",
            "车长阵亡",
            "当命令声停下，车内的协作也随之崩塌。\n你们再也无法把这台钢铁继续带出废墟。\n\n结局来得很快，也很冷。\n",
        )

    alive_crew = sum(1 for m in s.crew if m.alive)
    if alive_crew <= 1 and s.round_number >= 4:
        return (
            "E10",
            "只剩你",
            """
当你回头时，车内只剩下自己的呼吸声。
你终于明白：继续把命令喊下去，只会让‘钢铁’替你把最后一个人也带走。

你选择停火、离车、把自己藏进废墟——不是为了体面，只是为了结束。
""",
        )
    if s.morale <= 0:
        # 士气崩溃宽限：给玩家若干回合尝试恢复
        try:
            mr = int(s.counters.get("morale_zero_rounds", 0) or 0)
        except Exception:
            mr = 0
        if mr >= int(MORALE_ZERO_GRACE_ROUNDS) + 1:
            return (
                "E12",
                "士气崩溃",
                """
当每个人都说不出话时，你明白继续前进只会让更多人倒下。
你选择停火，尽力让乘员保住性命。
""",
            )
    if s.fuel <= 0:
        # 空油宽限：给玩家若干回合寻找燃料；用尽后才触发结局
        try:
            empty_rounds = int(s.counters.get("fuel_empty_rounds", 0) or 0)
        except Exception:
            empty_rounds = 0
        if empty_rounds >= int(FUEL_EMPTY_GRACE_ROUNDS) + 1:
            if s.story_flags.get("scuttle_prepared", False):
                return (
                    "E09",
                    "燃油耗尽（已准备自毁）",
                    """
发动机沉默。你们在瓦砾里推车，推不动未来。
你把最后的准备变成一个清晰的命令：别让它成为任何人的战利品。

你让乘员分散撤离。你不知道结局会不会更好，但至少，你不再让钢铁决定人的命。
""",
                )
            return (
                "E06",
                "燃油耗尽",
                """
发动机沉默。你们在瓦砾里推车，推不动未来。
你只能做出人的选择：弃车，分散，别让钢铁困死所有人。
""",
            )
    if s.damage >= 100:
        if s.story_flags.get("scuttle_prepared", False):
            return (
                "E09",
                "车辆报废（已准备自毁）",
                """
坦克不再响应。你们把舱盖推开，让空气涌进来。
你用最短的口令完成最后的准备：别让它落入他人之手。

爆裂声隔在身后，你让乘员分散撤离——这一次，你至少少了一件需要背负的东西。
""",
            )
        return (
            "E05",
            "车辆报废",
            """
坦克不再响应。你打开舱盖，让空气涌进来。
你第一次意识到：这段路的尽头从来不是‘胜利’，而是‘结束’。
""",
        )
    if s.city_collapse >= 100:
        # 城市崩溃宽限：给玩家若干回合做最后撤离安排
        try:
            cr = int(s.counters.get("collapse_max_rounds", 0) or 0)
        except Exception:
            cr = 0
        if cr >= int(CITY_COLLAPSE_GRACE_ROUNDS) + 1:
            return (
                "E08",
                "城市崩溃",
                """
街区的秩序彻底瓦解，任何路线都变成赌局。
你意识到继续行动只会把乘员推向更糟的结局，于是做出最后的选择：尽可能分散撤离。
""",
            )
    return None


def intro(ins: InputStream) -> Tuple[str, str, str, bool]:
    banner()
    narrate(
        """
1945年春，柏林。
你是一辆虎王坦克的车长。你并不掌握历史，只能在每一个路口选择：
让更多人活下来，或者让钢铁替你做决定。
"""
    )
    if SELFTEST:
        return ("卡尔", "K-21", "2", False)

    name = get_valid_input(ins, "给车长取个名字（回车默认“卡尔”）：", default="卡尔")
    callsign = get_valid_input(ins, "给坦克设定呼号（回车默认“K-21”）：", default="K-21")
    diff = choose(
        ins,
        "选择难度(1-4或突围)：",
        {
            "1": "公路旅行：拿到满额资源，战斗少见",
            "2": "1945：推荐",
            "3": "回到苏军总部：资源更少、遭遇更多",
            "突围": "随机7友方装甲+4步兵，进入无限轮次突围战",
            "4": "自定义：手动调整开局与遭遇强度",
        },
        default="2",
    )
    # 隐藏选项：输入"增援"以公路旅行难度下获得两个随机友军坦克
    breakout_mode = False
    if diff == "增援":
        diff = "1"  # 使用公路旅行设置
        breakout_mode = True
    return (name, callsign, diff, breakout_mode)


def setup_endless_breakout(s: GameState) -> None:
    """突围难度专用初始化：随机7友方装甲+4步兵。"""
    if not is_breakout_mode(s):
        return

    # 友方装甲（友军坦克）：若不足7则补齐
    desired_tanks = 7
    cur_tanks = [t for t in getattr(s, "tank_allies", []) if getattr(t, "alive", True)]
    if len(cur_tanks) < desired_tanks:
        models = ["豹式坦克", "四号坦克", "斐迪南突击炮", "防空坦克", "Sd.Kfz.251装甲运兵车", "虎式坦克"]

        # 需求：突围模式开局至少有三个重型单位
        heavy_models = ["虎式坦克", "斐迪南突击炮", "豹式坦克"]

        def _tmpl(m: str) -> Dict[str, Tuple[int, int]]:
            if m == "豹式坦克":
                return {"hp": (115, 150), "armor": (65, 85), "acc": (62, 76), "morale": (45, 74)}
            if m == "四号坦克":
                return {"hp": (105, 140), "armor": (55, 78), "acc": (58, 70), "morale": (45, 72)}
            if m == "Sd.Kfz.251装甲运兵车":
                return {"hp": (60, 85), "armor": (8, 18), "acc": (52, 70), "morale": (45, 74)}
            if m == "防空坦克":
                return {"hp": (65, 90), "armor": (8, 18), "acc": (56, 74), "morale": (45, 72)}
            if m == "虎式坦克":
                return {"hp": (145, 180), "armor": (95, 125), "acc": (60, 74), "morale": (45, 74)}
            return {"hp": (135, 170), "armor": (105, 130), "acc": (52, 66), "morale": (42, 70)}

        existing_heavy = sum(1 for t in cur_tanks if str(getattr(t, "model", "")) in set(heavy_models))
        need_heavy = max(0, 3 - int(existing_heavy))

        for i in range(desired_tanks - len(cur_tanks)):
            if need_heavy > 0:
                model = s.rng.choice(heavy_models)
                need_heavy -= 1
            else:
                model = s.rng.choice(models)
            t = _tmpl(model)
            ally = TankAlly(
                name=f"{model} 友军-{i+1}",
                model=model,
                hp=s.rng.randint(*t["hp"]),
                armor=s.rng.randint(*t["armor"]),
                accuracy=s.rng.randint(*t["acc"]),
                morale=s.rng.randint(*t["morale"]),
            )
            _randomize_tank_ally_supplies(s, ally)
            # 需求：开局单位油料弹药加满（突围开局装甲）
            try:
                setattr(ally, "fuel", 200)
                setattr(ally, "shells", 30)
            except Exception:
                pass
            ally.clamp()
            try:
                setattr(ally, "_joined_round", int(getattr(s, "round_number", 0) or 0))
            except Exception:
                pass
            s.tank_allies.append(ally)

    # 步兵单位：放到“当前辖区驻军”中，战斗时作为 garrison_allies 传入。
    infantry_types = ["国民冲锋队", "国防军", "党卫军", "工兵", "狙击组", "侦察组", "机枪队"]
    sec = s.sectors.get(s.location_key)
    if sec is not None:
        live_infantry = [u for u in sec.garrison_units if getattr(u, "alive", True) and u.unit_type in set(infantry_types)]
        need = max(0, 4 - len(live_infantry))
        if need > 0:
            terrain = str(MAP_META.get(s.location_key, {}).get("terrain", "市区"))
            for _ in range(need):
                t = s.rng.choice(infantry_types)
                u = _make_garrison_unit(s.rng, terrain=terrain, force_type=t)
                u.clamp()
                sec.garrison_units.append(u)

    s.counters["endless_breakout"] = 1
    s.counters["breakout_waves"] = int(s.counters.get("breakout_waves", 0) or 0)

    # 自动生成装填手，如果没有的话
    has_loader = any(m.role == '装填手' and m.alive for m in s.crew)
    if not has_loader:
        s.crew.append(CrewMember(role='装填手', name='亚历克斯', proficiency=200))

    # 设置所有成员熟练度为200
    for m in s.crew:
        m.proficiency = 200

    s.clamp()


def run_endless_breakout(ins: InputStream, s: GameState) -> None:
    """突围难度：无限轮次遭遇战（技能免物品消耗；友军坦克无限参战）。"""
    setup_endless_breakout(s)
    narrate(
        """
【突围】
你与临时集结的友军装甲、步兵编组一起，冲向尚未被封死的街区缺口。
这不是一次战斗，而是一轮又一轮的突围。
"""
    )

    while True:
        sec = s.sectors.get(s.location_key)
        garrison_allies: List[GarrisonUnit] = []
        if sec is not None:
            # 仅带入4个步兵单位（存活优先），符合“随机四个步兵单位”的编制约束
            garrison_allies = [u for u in sec.garrison_units if getattr(u, "alive", True)][:4]

        s.counters["breakout_waves"] = int(s.counters.get("breakout_waves", 0) or 0) + 1
        wave = int(s.counters.get("breakout_waves", 1) or 1)
        print(f"\n=== 突围战斗（第{wave}轮）===")

        outcome = resolve_encounter(
            ins,
            s,
            boss=False,
            garrison_allies=garrison_allies,
            encounter_mode="breakout",
            ignore_battle_cap=True,
            post_reward_event=False,
        )
        if outcome == "ended" or bool(getattr(s, "ended", False)):
            return

        raw = get_valid_input(ins, "继续突围：回车继续；输入0结束突围并返回主菜单：", default="")
        if raw.strip() == "0":
            return


def build_state(ins: InputStream, name: str, callsign: str, diff: str, rng: random.Random, breakout_mode: bool = False) -> GameState:
    if diff == "4":
        custom = _prompt_custom_difficulty(ins)
        preset = DIFFICULTY.get(str(custom.get("base_key", "2")), DIFFICULTY["2"])
        start0 = preset.get("start") if isinstance(preset.get("start"), dict) else {}
        # 自定义 start 覆盖基础模板
        start = dict(start0)
        if isinstance(custom.get("start"), dict):
            start.update(custom["start"])
        s = GameState(
            name=name,
            callsign=callsign,
            difficulty_key="4",
            custom_difficulty={
                "base_key": str(custom.get("base_key", "2")),
                "danger": float(custom.get("danger", preset.get("danger", 0.85))),
                "start": {
                    "fuel": int(start.get("fuel", 120)),
                    "morale": int(start.get("morale", 65)),
                    "damage": int(start.get("damage", 6)),
                    "ap_shells": int(start.get("ap_shells", 22)),
                    "he_shells": int(start.get("he_shells", 18)),
                    "mg_ammo": int(start.get("mg_ammo", 170)),
                    "base_armor": int(start.get("base_armor", 105)),
                },
            },
            rng=rng,
            fuel=int(start.get("fuel", 120)),
            mg_ammo=int(start.get("mg_ammo", 170)),
            ap_shells=int(start.get("ap_shells", 22)),
            he_shells=int(start.get("he_shells", 18)),
            morale=int(start.get("morale", 65)),
            damage=int(start.get("damage", 6)),
            base_armor=int(start.get("base_armor", 105)),
        )
    else:
        preset = DIFFICULTY[diff]
        shell_total = int(preset["start"].get("ammo", 18))
        if diff == "1":
            ap_shells = 40
            he_shells = 40
        else:
            ap_shells = max(0, int(round(shell_total * 0.55)))
            he_shells = max(0, shell_total - ap_shells)
        mg_start = {"1": 200, "2": 170, "3": 140}.get(diff, 170)
        base_armor = {"1": 112, "2": 105, "3": 98}.get(diff, 105)
        s = GameState(
            name=name,
            callsign=callsign,
            difficulty_key=diff,
            rng=rng,
            fuel=preset["start"]["fuel"],
            mg_ammo=mg_start,
            ap_shells=ap_shells,
            he_shells=he_shells,
            morale=preset["start"]["morale"],
            damage=preset["start"]["damage"],
            base_armor=base_armor,
        )

    # 需求：开局单位油料弹药都加满（玩家本车）
    try:
        s.fuel = 200
        s.mg_ammo = 400
        s.ap_shells = 40
        s.he_shells = 40
    except Exception:
        pass

    # 初始背包
    add_item(s, "烟幕弹", 1)
    add_item(s, "急救包", 1)
    add_item(s, "电台电池", 1)
    add_item(s, "备件", 1)
    maybe_add_initial_quests(s)
    init_counters_and_commissions(s)
    # 初始化任务板支线任务（可刷新）
    try:
        ensure_side_quests(s, keep=2)
        sync_counter_quests(s)
    except Exception:
        pass
    init_sectors(s)
    init_crew(s)

    # 开局额外：按需求给玩家指定起始支援（并带兜底标记，避免重复发放）
    _grant_initial_support_if_missing(s)
    # 突围模式：添加两个随机友军坦克
    if breakout_mode:
        ally_models = ["豹式坦克", "黑豹坦克", "虎式坦克", "四号坦克"]
        for i in range(2):
            model = rng.choice(ally_models)
            ally = TankAlly(
                name=f"{model} #{i+1}",
                model=model,
                hp=120 + rng.randint(0, 40),  # 120-160
                armor=80 + rng.randint(0, 20),  # 80-100
                accuracy=60 + rng.randint(0, 10),  # 60-70
                morale=50 + rng.randint(0, 20),  # 50-70
            )
            _randomize_tank_ally_supplies(s, ally)
            # 需求：开局单位油料弹药加满（增援模式开局装甲）
            try:
                setattr(ally, "fuel", 200)
                setattr(ally, "shells", 30)
            except Exception:
                pass
            ally.clamp()
            try:
                setattr(ally, "_joined_round", int(getattr(s, "round_number", 0) or 0))
            except Exception:
                pass
            s.tank_allies.append(ally)
    s.explored.add(s.location_key)
    s.clamp()
    return s


def main_loop(ins: InputStream, s: GameState) -> None:
    # 兜底：无论新开局还是读档，只要尚未发过开局支援且当前没有装甲友军，就补发一次。
    _grant_initial_support_if_missing(s)
    # 初始化弹药跟踪（用于统计每回合/累计耗弹量）
    try:
        init_ammo_tracking(s)
    except Exception:
        pass

    # 开局直接进入第一章（不改变回合计数；并避免后续章节节点重复触发）
    if s.round_number <= 1 and "chapter_01" not in s.shown_events:
        force_trigger_chapter(s, 1)
        narrate("\n你把手贴在炮塔内壁上，能感觉到金属的冷与颤。城市的噪声像一层灰。")

    # 旧存档兼容：进入主循环时补齐任务板支线任务
    try:
        ensure_side_quests(s, keep=2)
        sync_counter_quests(s)
    except Exception:
        pass

    while not s.ended:
        tick_round_start(s)
        ensure_side_quests(s, keep=2)
        sync_counter_quests(s)
        complete_quests_if_any(s)
        sync_commissions(s)
        boss_check_and_run(ins, s)
        if s.ended:
            break

        # 回合内行动
        while s.action_points > 0 and not s.ended:
            status_line(s)
            sync_counter_quests(s)
            show_quests(s)
            show_commissions(s)
            print(crew_status_summary(s))
            print("\n行动：")
            default_action = "9" if ins.default_when_empty else "1"
            c = choose(
                ins,
                "选择(0-10)：",
                {
                    "1": "移动/探索",
                    "2": "搜索补给",
                    "3": "支援撤离",
                    "4": "修理/维护",
                    "5": "休整",
                    "6": "背包/技能/状态",
                    "7": "驻军/辖区",
                    "8": "查看地图",
                    "9": "结束本回合",
                    "10": "肃清敌人（本回合抗住两轮进攻）",
                },
                default=default_action,
            )

            if c == "0":
                continue

            if c == "1":
                action_move(ins, s)
            elif c == "2":
                action_scavenge(ins, s)
            elif c == "3":
                action_assist(ins, s)
            elif c == "4":
                action_repair(ins, s)
            elif c == "5":
                action_rest(ins, s)
            elif c == "6":
                action_menu(ins, s)
            elif c == "7":
                action_garrison(ins, s)
            elif c == "8":
                map_menu(ins, s)
            elif c == "10":
                action_hold_position(ins, s)
            else:
                s.action_points = 0

            # 刷新：行动后立刻同步任务/任务板/委托进度与结算
            try:
                ensure_side_quests(s, keep=2)
                sync_counter_quests(s)
                complete_quests_if_any(s)
                sync_commissions(s)
            except Exception:
                pass

            # 成就：行动后统一检查（覆盖探索/收集/支援/修理等非战斗触发）
            try:
                newly = check_and_unlock_achievements(s)
                if newly:
                    print("\n【成就解锁】" + "、".join(newly))
            except Exception:
                pass

            s.clamp()
            ended = end_conditions(s)
            if ended is not None:
                end_game(s, ended[0], ended[1], ended[2])
                break

            # 自动存档：每次行动后尝试写入（失败不打断游戏）
            if not s.ended:
                try:
                    save_autosave(s)
                except Exception:
                    pass

        s.round_number += 1
        # 每3回合进入下一章（若尚未触发则显示并应用效果）
        try:
            idx = maybe_trigger_chapter(s)
            if isinstance(idx, int):
                maybe_trigger_story_for_chapter(ins, s, idx)
        except Exception:
            pass
        # 已移除“最大回合数”强制结算：游戏将由其他结局条件自然结束


def run_selftest() -> None:
    print("\n=== 自检模式激活 ===")
    rng = random.Random(12345)
    cases: List[Dict[str, object]] = [
        {
            "name": "用例1",
            "seq": ["2", "3", "2", "5", "1", "2", "3", "7"],
            "flags": {},
        },
        {
            "name": "用例2",
            "seq": ["1", "3", "4", "1", "2", "4", "7"],
            "flags": {},
        },
        # 覆盖：IL-2 空袭（顶攻/高伤害）
        {
            "name": "用例3_IL2",
            "seq": ["1", "3"],
            "flags": {
                "force_next_encounter": True,
                "force_air_kind": "IL-2",
                "force_air_arrival_turn": 1,
                "force_air_once": True,
            },
        },
        # 覆盖：友军坦克加入并参与战斗
        {
            "name": "用例4_友军坦克",
            "seq": ["1", "3"],
            "flags": {
                "force_meet_friendly_tank": True,
                "force_meet_friendly_tank_reason": "move",
                "force_next_encounter": True,
            },
        },
        # 覆盖：机枪对空射击分支（强制成功，避免随机失败导致不稳定）
        {
            "name": "用例5_对空射击",
            "seq": ["1", "3", "8"],
            "flags": {
                "force_next_encounter": True,
                "force_air_kind": "Yak-3",
                "force_air_arrival_turn": 1,
                "force_air_once": True,
                "force_aa_success": True,
            },
        },

        # 覆盖：电台事件链（验证 require_item/cost_item 与 tank_support）
        {
            "name": "用例6_电台事件链",
            "mode": "event",
            "seq": ["1", "2", "2"],
            "flags": {},
        },
        # 覆盖：医院事件链（验证 require_item/cost_item 与 quest 推进）
        {
            "name": "用例7_医院事件链",
            "mode": "event",
            "seq": ["1", "1", "2"],
            "flags": {},
        },
        # 覆盖：地铁事件链（验证阶段 1->2->0 与行动点消耗）
        {
            "name": "用例8_地铁事件链",
            "mode": "event",
            "seq": ["1", "1", "1"],
            "flags": {},
        },
    ]
    for i, case in enumerate(cases, 1):
        name0 = str(case.get("name", f"用例{i}"))
        print(f"\n--- {name0} ---")
        seq = list(case.get("seq", []) or [])
        flags = dict(case.get("flags", {}) or {})
        _selftest_reset_context(flags)
        mode = str(case.get("mode", "main"))
        # main 模式：沿用原有“跑主循环”覆盖
        # event 模式：定向触发事件链，校验选项解析与阶段推进
        ins = InputStream(scripted=list(seq), default_when_empty=True)
        try:
            if mode == "event":
                ins0 = InputStream(scripted=[], default_when_empty=True)
                name, callsign, diff, breakout_mode = intro(ins0)
                s = build_state(ins0, name, callsign, diff, rng, breakout_mode)
                s.round_number = 3
                s.action_points = 3

                if name0.endswith("电台事件链"):
                    s.location_key = "24"  # terrain=电台
                    # build_state 默认已给 1 个电台电池：这里确保数量为 1，便于验证 cost_item 确实消耗。
                    s.inventory["电台电池"] = 1
                    _selftest_reset_context({"force_event_id": "EV_CHAIN_RADIO_01"})
                    random_event(ins, s)  # 选1：花AP记录频率
                    assert int(s.story_vars.get("radio_chain_stage", 0) or 0) == 1

                    _selftest_reset_context({"force_event_id": "EV_CHAIN_RADIO_02"})
                    random_event(ins, s)  # 选2：求援（消耗电池）
                    assert int(s.story_vars.get("radio_chain_stage", 0) or 0) == 2
                    assert int(s.inventory.get("电台电池", 0)) == 0

                    _selftest_reset_context({"force_event_id": "EV_CHAIN_RADIO_03"})
                    random_event(ins, s)  # 选2：点亮电台（耗AP，给支援）
                    assert int(s.story_vars.get("radio_chain_stage", 0) or 0) == 0
                    assert len(getattr(s, "tank_allies", [])) >= 1
                    assert s.action_points == 1
                    print("自检：电台事件链 OK")

                elif name0.endswith("医院事件链"):
                    s.location_key = "22"  # terrain=医院
                    add_item(s, "药品", 1)
                    add_item(s, "医疗包", 1)
                    add_item(s, "咖啡", 1)
                    s.quests.append(
                        Quest(
                            id="Q_hospital",
                            title="援助地下医院",
                            desc="为临时医院筹措资源与时间。",
                            target=3,
                            reward_points=2,
                        )
                    )

                    _selftest_reset_context({"force_event_id": "EV_CHAIN_HOSPITAL_01"})
                    random_event(ins, s)  # 选1：捐药
                    _selftest_reset_context({"force_event_id": "EV_CHAIN_HOSPITAL_02"})
                    random_event(ins, s)  # 选1：捐医疗包
                    _selftest_reset_context({"force_event_id": "EV_CHAIN_HOSPITAL_03"})
                    random_event(ins, s)  # 选2：留下箱子

                    q = next((q for q in s.quests if q.id == "Q_hospital"), None)
                    assert q is not None and int(q.progress) >= 3
                    assert bool(q.done) is True
                    assert int(s.inventory.get("药品", 0)) == 0
                    assert int(s.inventory.get("医疗包", 0)) == 0
                    print("自检：医院事件链 OK")

                elif name0.endswith("地铁事件链"):
                    s.location_key = "5"  # terrain=地铁
                    _selftest_reset_context({"force_event_id": "EV_CHAIN_TUNNEL_01"})
                    random_event(ins, s)  # 选1：下去侦察（耗AP）
                    assert int(s.story_vars.get("tunnel_chain_stage", 0) or 0) == 1

                    _selftest_reset_context({"force_event_id": "EV_CHAIN_TUNNEL_02"})
                    random_event(ins, s)  # 选1：追补给方向（耗AP）
                    assert int(s.story_vars.get("tunnel_chain_stage", 0) or 0) == 2

                    _selftest_reset_context({"force_event_id": "EV_CHAIN_TUNNEL_03"})
                    random_event(ins, s)  # 选1：快速搬运并撤离
                    assert int(s.story_vars.get("tunnel_chain_stage", 0) or 0) == 0
                    assert s.action_points == 1
                    print("自检：地铁事件链 OK")

                else:
                    print("自检：未知 event 用例（已跳过）")
                continue

            name, callsign, diff, breakout_mode = intro(ins)
            s = build_state(ins, name, callsign, diff, rng, breakout_mode)

            # 章节按每5回合触发，运行时由主循环的触发器处理

            main_loop(ins, s)
            print(f"自检：结局={s.ending_id}")
        except RestartGame:
            print("自检：触发重开（可忽略）")
        except SystemExit:
            print("自检：退出")
        except Exception as e:
            print(f"自检失败：{e}")
            import traceback

            traceback.print_exc()
            raise
    print("\n=== 自检完成 ===")


def main() -> None:
    crash_count = 0
    last_crash_at = 0.0
    while True:
        try:
            rng = _rng_from_env()
            ins = InputStream()
            banner()
            if not SELFTEST:
                choice = get_valid_input(
                    ins,
                    "输入1新游戏，2读取存档，其他退出（或输入20070529进入后端调试）：",
                    default="1",
                )
                if choice.strip() == "20070529":
                    dbg = globals().get("backend_debug")
                    if callable(dbg):
                        dbg(ins, rng)
                    else:
                        print("后端调试功能不可用（backend_debug 未定义）。")
                    continue
                if choice.strip() == "2":
                    st = menu_load_game(ins)
                    if st is None:
                        continue
                    # 读取存档兜底：如果是很早期回合且没有装甲友军，则补发开局支援
                    try:
                        if int(getattr(st, "round_number", 0) or 0) <= 2:
                            _grant_initial_support_if_missing(st)
                    except Exception:
                        pass
                    main_loop(ins, st)
                    if SELFTEST:
                        return
                    again = get_valid_input(ins, "\n输入1回到主菜单，其他退出：", default="1")
                    if again != "1":
                        return
                    continue
                if choice != "1":
                    print("游戏退出。")
                    return
            name, callsign, diff, breakout_mode = intro(ins)
            s = build_state(ins, name, callsign, diff, rng, breakout_mode)
            if diff == "突围":
                run_endless_breakout(ins, s)
            else:
                main_loop(ins, s)
            if SELFTEST:
                return
            again = get_valid_input(ins, "\n输入1重玩，其他退出：", default="0")
            if again != "1":
                return
        except RestartGame:
            continue
        except SystemExit:
            print("\n已退出。")
            return
        except Exception as e:
            now = time.time()
            if now - last_crash_at < 20.0:
                crash_count += 1
            else:
                crash_count = 1
            last_crash_at = now

            print(f"游戏运行中发生错误：{e}")
            traceback.print_exc()
            report = _write_crash_report(e, where="main()")
            if report:
                print(f"已生成崩溃报告：{report}")

            # 避免因同一故障导致无限重启刷屏
            if crash_count >= 3:
                print("\n检测到短时间内重复崩溃，已停止自动重启。")
                print("你可以尝试：")
                print("- 运行：python 本脚本.py --repair  （修复本地数据文件）")
                print(f"- 或删除/重命名 {_EVENTS_BASENAME}（默认位置：{_EVENTS_FILE}）")
                return

            print("尝试重新启动游戏...")
            time.sleep(1.0)
            continue
def backend_debug(ins: InputStream, rng: random.Random) -> None:
    """后端调试菜单：修改数值与状态（模仿原作的后端入口风格）。"""
    print("\n=== 后端调试模式 ===")
    s = build_state(ins, "卡尔", "K-21", "2", rng)
    while True:
        status_line(s)
        print("\n调试菜单：")
        c = choose(
            ins,
            "选择(1-8)：",
            {
                "1": "修改资源（燃油/机枪弹/AP/HE/士气/损伤/胜利点/崩溃）",
                "2": "修改背包（添加物品）",
                "3": "修改辖区（好感/沦陷/驻军单位）",
                "4": "修改乘员（HP/阵亡复活）",
                "5": "快速完成任务/委托（加进度）",
                "6": "显示完整状态",
                "7": "退出后端调试",
                "8": "修复本地数据文件（events_shown.json）",
            },
            default="7",
        )
        if c == "1":
            try:
                s.fuel = int(get_valid_input(ins, "燃油(0-200)：", default=str(s.fuel), allow_restart=False))
                s.mg_ammo = int(get_valid_input(ins, "机枪弹(0-240)：", default=str(s.mg_ammo), allow_restart=False))
                s.ap_shells = int(get_valid_input(ins, "AP炮弹(0-40)：", default=str(s.ap_shells), allow_restart=False))
                s.he_shells = int(get_valid_input(ins, "HE炮弹(0-40)：", default=str(s.he_shells), allow_restart=False))
                s.morale = int(get_valid_input(ins, f"士气(0-{MORALE_MAX})：", default=str(s.morale), allow_restart=False))
                s.damage = int(get_valid_input(ins, "损伤(0-100)：", default=str(s.damage), allow_restart=False))
                s.victory_points = int(get_valid_input(ins, "胜利点(>=0)：", default=str(s.victory_points), allow_restart=False))
                s.city_collapse = int(get_valid_input(ins, "崩溃(0-100)：", default=str(s.city_collapse), allow_restart=False))
                s.clamp()
            except ValueError:
                print("输入无效。")
        elif c == "2":
            print("可添加物品：" + "、".join(ITEMS.keys()))
            name = get_valid_input(ins, "物品名：", allow_restart=False)
            if name not in ITEMS:
                print("未知物品。")
                continue
            try:
                cnt = int(get_valid_input(ins, "数量：", default="1", allow_restart=False))
            except ValueError:
                print("输入无效。")
                continue
            add_item(s, name, max(1, cnt))
            print("已添加。")
        elif c == "3":
            show_sector_overview(s)
            key = get_valid_input(ins, "选择区域编号(1-5)：", default=s.location_key, allow_restart=False)
            if key not in s.sectors:
                print("无效区域。")
                continue
            sec = s.sectors[key]
            try:
                sec.favor = int(get_valid_input(ins, "好感(0-100)：", default=str(sec.favor), allow_restart=False))
                sec.fall = int(get_valid_input(ins, "沦陷(0-100)：", default=str(sec.fall), allow_restart=False))
                s.clamp()
            except ValueError:
                print("输入无效。")

            # 驻军单位编辑
            print("\n驻军单位：" + ("、".join(u.name for u in sec.garrison_units) if sec.garrison_units else "(无)"))
            op = choose(
                ins,
                "驻军编辑(1-4)：",
                {
                    "1": "添加1个随机单位",
                    "2": "删除最前1个单位",
                    "3": "清空",
                    "4": "返回",
                },
                default="4",
                allow_restart=False,
            )
            if op == "1":
                unit = _make_garrison_unit(s.rng)
                sec.garrison_units.append(unit)
                print(f"已添加：{unit.name}")
            elif op == "2":
                if sec.garrison_units:
                    removed = sec.garrison_units.pop(0)
                    print(f"已删除：{removed.name}")
                else:
                    print("无单位可删。")
            elif op == "3":
                sec.garrison_units.clear()
                print("已清空。")
            s.clamp()
        elif c == "4":
            show_crew(s)
            raw = get_valid_input(ins, "选择乘员序号(1-5)：", default="1", allow_restart=False)
            try:
                idx = int(raw)
            except ValueError:
                print("输入无效。")
                continue
            if idx < 1 or idx > len(s.crew):
                print("无效序号。")
                continue
            m = s.crew[idx - 1]
            try:
                hp = int(get_valid_input(ins, "HP(0-100)：", default=str(m.hp), allow_restart=False))
            except ValueError:
                print("输入无效。")
                continue
            m.hp = hp
            m.alive = hp > 0
            if m.alive:
                m.stress = max(0, m.stress - 10)
            s.clamp()
        elif c == "5":
            for q in s.quests:
                q.add(999)
            for k in list(s.counters.keys()):
                s.counters[k] += 5
            complete_quests_if_any(s)
            sync_commissions(s)
            print("已推进任务/委托进度。")
        elif c == "6":
            show_crew(s)
            show_sector_overview(s)
            show_quests(s)
            show_commissions(s)
            print(f"背包：{s.inventory}")
        elif c == "8":
            repair_local_files(verbose=True)
        else:
            print("=== 退出后端调试 ===\n")
            return


if __name__ == "__main__":
    _configure_stdio_utf8()
    if any(arg in ("repair", "--repair") for arg in sys.argv[1:]):
        ok = repair_local_files(verbose=True)
        raise SystemExit(0 if ok else 2)
    if SELFTEST:
        run_selftest()
    else:
        main()
