import re
from pathlib import Path

p = Path(r"c:\Users\LENOVO\Desktop\新建文件夹 (3)\柏林1945_虎王车长_系统版.py")
s = p.read_text(encoding="utf-8")

vals = set(re.findall(r'"model"\s*:\s*"([^"]+)"', s))
vals |= set(re.findall(r'\bmodel\s*=\s*"([^"]+)"', s))
vals |= set(re.findall(r'\bvehicle_model\s*=\s*"([^"]+)"', s))
vals = {v.strip() for v in vals if v and v.strip()}

print("COUNT", len(vals))
for v in sorted(vals):
    print(v)
