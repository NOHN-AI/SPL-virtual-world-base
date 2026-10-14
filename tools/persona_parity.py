#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
人格层非侵入性对照测试（parity）
================================================================================
要证明的命题：人格层是「纯观测层」——挂载它对世界状态轨迹零影响。

方法：同一 (config, seed) 跑两个世界各 N tick，
      A 组 persona_mode="affect"，B 组 persona_mode="off"，
      比对两者 export_state()，仅在比对前从 config 里剔除 persona_mode 这一个新增键。
      其余部分必须逐字节相同。

结论口径：
  PASS = 人格层不改动账本态/人口态/经济态/随机流 → 可安全挂载；
  FAIL = 人格层影响到了世界轨迹 → 必须回滚挂载点，不得带着此状态提交。

同时输出两组的 state_hash：二者可以不同，差异只应来自 config 多出的
persona_mode 键（export_state 会把 config.__dict__ 一并哈希）。
"""

import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from demo.virtual_world import NohnWorld, WorldConfig  # noqa: E402

TICKS = 300
SEED = 42


def run_once(persona_mode: str):
    cfg = WorldConfig(world_size=60, initial_agents=30, initial_resources=80,
                      initial_buildings=8, seed=SEED, persona_mode=persona_mode)
    w = NohnWorld(cfg)
    for _ in range(TICKS):
        w.tick()
    return w


def strip_config_key(state: dict, key: str) -> dict:
    s = copy.deepcopy(state)
    s.get("config", {}).pop(key, None)
    return s


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    wa = run_once("affect")
    wb = run_once("off")

    sa = strip_config_key(wa.export_state(), "persona_mode")
    sb = strip_config_key(wb.export_state(), "persona_mode")

    ja = json.dumps(sa, ensure_ascii=False, sort_keys=True, default=str)
    jb = json.dumps(sb, ensure_ascii=False, sort_keys=True, default=str)
    ok = (ja == jb)

    print("=== persona parity (seed=%d, %d ticks) ===" % (SEED, TICKS))
    print("  A affect  state_hash: %s" % wa.state_hash())
    print("  B off     state_hash: %s" % wb.state_hash())
    print("  export_state (config.persona_mode excluded) identical: %s" % ("YES" if ok else "NO"))
    if not ok:
        # 定位首个分歧字段，避免"只报失败不报原因"
        for k in sorted(set(sa) | set(sb)):
            if json.dumps(sa.get(k), sort_keys=True, default=str) != \
               json.dumps(sb.get(k), sort_keys=True, default=str):
                print("      first differing top-level key: %r" % k)
                break
    print("  persona attached (A): %d agents" % sum(
        1 for a in wa.agents if a.persona is not None))
    print("  persona attached (B): %d agents" % sum(
        1 for a in wb.agents if a.persona is not None))
    rep = wa.persona_report()
    print("  persona_report (A): ticks=%s agents=%s" % (
        rep.get("ticks_advanced"), rep.get("agents_with_persona")))
    af = rep.get("avg_affect", {})
    if af:
        print("  avg affect: 喜悦=%.4f 恐惧=%.4f 信任=%.4f 自尊=%.4f" % (
            af.get("fluid.喜悦", -1), af.get("fluid.恐惧", -1),
            af.get("fluid.信任", -1), af.get("self_esteem", -1)))
    print("\nVERDICT: %s" % ("PASS - persona layer is non-invasive" if ok
                             else "FAIL - persona layer altered world trajectory"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
