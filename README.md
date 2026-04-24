# 柏林车长 - Berlin 1945 Tiger II Commander

二战柏林战役背景的文字冒险游戏，玩家扮演虎王（Tiger II）坦克车长。

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Genre](https://img.shields.io/badge/Genre-Text%20Adventure-red)

## 游戏简介

1945 年柏林战役，你是一名虎王坦克车长。在城市废墟中指挥你的战车，做出战术决策，经历历史的最后时刻。

## 运行

```bash
python 柏林1945_虎王车长_系统版.py
```

## 项目结构

```
├── 柏林1945_虎王车长_系统版.py   # 主游戏程序
├── 文字冒险游戏_fixed8.py       # 文字冒险游戏引擎
├── sim_batch.py                 # 批量战斗模拟
├── sim_run.py                   # 单次战斗模拟
├── is2_test.py                  # IS-2 坦克测试
├── test_tank_ally_ammo.py       # 弹药系统测试
├── quick_check_leadership.py    # 指挥系统检查
├── build_exe.py                 # PyInstaller 打包脚本
├── __analyze_globals.py         # 代码分析工具
└── _extract_models.py           # 模型提取工具
```

## 游戏系统

- **坦克指挥**：驾驶虎王坦克参与城市战
- **战术决策**：多分支剧情选择
- **战斗模拟**：基于数据的坦克对抗系统
- **历史还原**：参考真实战役背景

## 打包

```bash
python build_exe.py
```

## 注意

本项目仅供学习娱乐用途。

## 许可证

MIT License
