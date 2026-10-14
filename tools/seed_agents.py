#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
种群转储器 —— 把 virtual world 里的智能体栈导出为可复核的 JSON
================================================================================
作用：证明"1000 个智能体各不相同"是可验证的，而不是口头声明。

做法：直接构造 NohnWorld（不推进 tick），因为智能体栈的生成已经内建在
      spawn 路径上（persona.build_persona / build_persona_stack），
      两处共用同一确定性源，所以本工具导出的内容与实跑世界逐字段一致。
      不另起一套生成逻辑，避免"工具输出与世界里的不是同一批人"。

用法：
    python tools/seed_agents.py                          # 1000 人 / seed 0
    python tools/seed_agents.py --agents 200 --world-size 120
    python tools/seed_agents.py --agents 1000 --no-dump  # 只打分布，不落盘
"""

import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from demo.virtual_world import NohnWorld, build_demo_config  # noqa: E402


def summarise(world):
    roles = collections.Counter()
    modes = collections.Counter()
    goals = collections.Counter()
    values = collections.Counter()
    biases = collections.Counter()
    for a in world.agents:
        if a.stack is None:
            continue
        roles[a.stack.identity_role] += 1
        modes[a.stack.language_mode] += 1
        goals[a.stack.goal_title] += 1
        values[max(a.stack.values, key=lambda k: (a.stack.values[k], k))] += 1
        biases[max(a.stack.biases, key=lambda k: (a.stack.biases[k], k))] += 1
    return roles, modes, goals, values, biases


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", type=int, default=1000)
    ap.add_argument("--world-size", type=int, default=None)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out-dir", default="seed")
    ap.add_argument("--no-dump", action="store_true", help="只打印分布，不写文件")
    args = ap.parse_args()

    cfg = build_demo_config(agents=args.agents, world_size=args.world_size, seed=args.seed)
    note = getattr(cfg, "_demo_note", None)
    print("=== 种群生成 (agents=%d, world=%dx%d, seed=%d) ==="
          % (cfg.initial_agents, cfg.world_size, cfg.world_size, args.seed))
    print("  资源 %d / 建筑 %d%s" % (
        cfg.initial_resources, cfg.initial_buildings,
        "（受 size²×8%% 上限截断）" if note and note.get("capped") else ""))

    world = NohnWorld(cfg)

    # 唯一性检查：1000 人若共用同一模板，这里会立刻暴露
    names = [a.name for a in world.agents]
    signatures = set()
    for a in world.agents:
        if a.stack is None:
            continue
        sig = (round(a.personality["openness"], 6), round(a.personality["neuroticism"], 6),
               a.stack.identity_role, a.stack.language_mode,
               round(a.stack.values["freedom"], 6), round(a.stack.biases["negativity_bias"], 6))
        signatures.add(sig)
    print("  姓名唯一: %d/%d" % (len(set(names)), len(names)))
    print("  人格签名唯一（特质+身份+语言档+2 项档案）: %d/%d"
          % (len(signatures), len(world.agents)))

    roles, modes, goals, values, biases = summarise(world)
    for label, ctr in (("身份", roles), ("语言档", modes), ("长期目标", goals),
                       ("主导价值观", values), ("最强偏见", biases)):
        top = ", ".join("%s=%d" % (k, v) for k, v in ctr.most_common(8))
        print("  %-10s %s" % (label + ":", top))

    if not args.no_dump:
        os.makedirs(args.out_dir, exist_ok=True)
        path = os.path.join(args.out_dir,
                            "agents-%d-seed%d.json" % (cfg.initial_agents, args.seed))
        payload = {
            "generator": "virtual_world.NohnWorld spawn path (persona.build_persona_stack)",
            "agents_requested": cfg.initial_agents,
            "world_size": cfg.world_size,
            "resources": cfg.initial_resources,
            "buildings": cfg.initial_buildings,
            "seed": args.seed,
            "population": [
                {
                    "aid": a.aid, "name": a.name, "age": a.age,
                    "traits": a.personality, "skills": a.skills,
                    "identity_role": a.stack.identity_role if a.stack else None,
                    "goal": a.stack.goal_title if a.stack else None,
                    "language_mode": a.stack.language_mode if a.stack else None,
                    "values": a.stack.values if a.stack else None,
                    "biases": a.stack.biases if a.stack else None,
                    "world_beliefs": a.stack.world_beliefs if a.stack else None,
                    "baseline_affect": a.affect,
                }
                for a in world.agents
            ],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        size_mb = os.path.getsize(path) / 1048576.0
        print("\n已写出 %s (%.2f MB)" % (path, size_mb))

    print("\n注：世界每次启动都按同一 seed 重放同一批智能体，本文件是转储而非输入，"
          "\n    两者若不一致即为确定性被破坏（可用 --no-dump 复跑比对）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
