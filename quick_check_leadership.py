# -*- coding: utf-8 -*-
"""快速验证：车长熟练度是否会提高其他岗位的 effective 熟练度。

运行：
    python quick_check_leadership.py
"""

from __future__ import annotations

import importlib.util
import sys
from types import SimpleNamespace


def load_game_module():
    path = r"C:\Users\LENOVO\Desktop\新建文件夹 (3)\柏林1945_虎王车长_系统版.py"
    spec = importlib.util.spec_from_file_location("game", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    # Python 3.13: dataclasses 在处理注解时会从 sys.modules 取模块命名空间。
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    m = load_game_module()

    # 车长熟练度 80，炮手 50
    s = SimpleNamespace(
        crew=[
            m.CrewMember(role="车长", name="玩家", proficiency=80),
            m.CrewMember(role="炮手", name="炮手A", proficiency=50),
        ]
    )

    base = m.crew_role_proficiency(s, "炮手")
    eff = m.crew_effective_role_proficiency(s, "炮手")
    print(f"炮手 base={base} -> effective={eff} (车长经验联动应让 effective > base)")

    # 车长低熟练度 0，不应影响
    s2 = SimpleNamespace(
        crew=[
            m.CrewMember(role="车长", name="玩家", proficiency=0),
            m.CrewMember(role="炮手", name="炮手A", proficiency=50),
        ]
    )
    base2 = m.crew_role_proficiency(s2, "炮手")
    eff2 = m.crew_effective_role_proficiency(s2, "炮手")
    print(f"炮手 base={base2} -> effective={eff2} (车长为0时应相等)")


if __name__ == "__main__":
    main()
