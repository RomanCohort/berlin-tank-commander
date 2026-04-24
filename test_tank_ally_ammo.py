import importlib.util
import sys
from pathlib import Path
p = Path(r"c:\Users\LENOVO\Desktop\新建文件夹 (3)\柏林1945_虎王车长_系统版.py")
spec = importlib.util.spec_from_file_location('game_mod', str(p))
mod = importlib.util.module_from_spec(spec)
loader = spec.loader
if loader is None:
    print('无法加载模块')
    sys.exit(1)
sys.modules['game_mod'] = mod
loader.exec_module(mod)

rng = mod._rng_from_env()
s = mod.GameState(name='test', callsign='T', difficulty_key='2', rng=rng)
mod._grant_initial_support_if_missing(s)
print('\n友军列表：')
for t in s.tank_allies:
    print(f"- {t.name} model={t.model} fuel={t.fuel} shells={t.shells} mg_ammo={getattr(t,'mg_ammo',None)}")

# 模拟一次射击消耗：优先找 Sd.Kfz 或 防空坦克
for t in s.tank_allies:
    if t.model in ('Sd.Kfz.251装甲运兵车','防空坦克','四号防空坦克'):
        before = getattr(t, 'mg_ammo', 0)
        dec = min(before, mod.MG_FIRE_COST)
        t.mg_ammo = max(0, before - dec)
        print(f"模拟射击：{t.name} mg -{dec} -> {t.mg_ammo}")
        break

print('\n测试完成')
