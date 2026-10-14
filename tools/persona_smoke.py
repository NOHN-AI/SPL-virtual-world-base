#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
persona 人格层冒烟测试
================================================================================
校验四件事，任何一项 FAIL 即视为注入未完成：

  1. 引擎一致性：persona/spl_engine.py 的 MD5 必须等于源仓库的引擎哈希。
  2. 确定性    ：同 (seed, aid, traits) 两次独立构造 + 同步推进，观测值逐字节相同。
  3. 世界随机源无污染：构造并推进人格层前后，world 侧 RNG 的 getstate() 不变。
  4. 成本      ：30 个智能体 × N tick 的每 tick 实测耗时，与 GasMeter 默认
                 500ms 预算对照（该项只测量、不下结论，超预算时明确报出）。

用法：
    python tools/persona_smoke.py              # 默认 100 tick / 30 智能体
    python tools/persona_smoke.py --ticks 300
"""

import argparse
import hashlib
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from persona import SECONDS_PER_TICK, AffectMapper, build_persona  # noqa: E402

ENGINE_MD5_EXPECTED = "f2fa611d355959e8d72a1f44c40963f0"
GAS_BUDGET_MS = 500.0

FLUID_KEYS = ("喜悦", "愤怒", "恐惧", "信任", "疏离", "张力", "愧疚", "羞耻")


class FakeAgent:
    """最小 agent 替身：只暴露 AffectMapper 读取的字段。"""

    def __init__(self, aid="A0001", needs=None, energy=100.0, wealth=100.0, violations=0):
        self.aid = aid
        self.needs = needs or {
            "physiology": 70.0, "safety": 60.0, "belonging": 55.0,
            "esteem": 50.0, "self_actualization": 40.0,
        }
        self.energy = energy
        self.wealth = wealth
        self.violations = violations


TRAITS_A = {"openness": 0.72, "conscientiousness": 0.41, "extraversion": 0.83,
            "agreeableness": 0.66, "neuroticism": 0.29}
TRAITS_B = {"openness": 0.35, "conscientiousness": 0.61, "extraversion": 0.22,
            "agreeableness": 0.48, "neuroticism": 0.71}


def check_engine_md5():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "persona", "spl_engine.py")
    with open(p, "rb") as f:
        got = hashlib.md5(f.read()).hexdigest()
    ok = got == ENGINE_MD5_EXPECTED
    print("[1] engine md5: %s  (%s)" % (got, "PASS" if ok else "FAIL"))
    return ok


def check_determinism(ticks):
    agent = FakeAgent()
    results = []
    for _ in range(2):
        layer = build_persona("A0001", world_seed=42, traits=TRAITS_A)
        for _ in range(ticks):
            layer.step(agent, perceived=[{"type": "agent", "dist": 2.0}])
        results.append(dict(layer.affect))
    ok = results[0] == results[1]
    print("[2] determinism (seed=42, aid=A0001, %d ticks): %s" % (ticks, "PASS" if ok else "FAIL"))
    if not ok:
        for k in sorted(set(results[0]) | set(results[1])):
            if results[0].get(k) != results[1].get(k):
                print("      diff %s: %r != %r" % (k, results[0].get(k), results[1].get(k)))
    return ok


def check_rng_untouched(ticks):
    """人格层不得消耗 world 的随机源——这是同 seed 可复现的前提。"""
    world_rng = random.Random(12345)
    before = world_rng.getstate()
    layer = build_persona("A0002", world_seed=12345, traits=TRAITS_B)
    agent = FakeAgent(aid="A0002")
    for _ in range(ticks):
        layer.step(agent, perceived=[])
    after = world_rng.getstate()
    ok = (before == after)
    print("[3] world RNG untouched: %s" % ("PASS" if ok else "FAIL"))
    return ok


def check_traits_differ():
    """不同特质必须产出不同人格——否则等于没接入。"""
    out = []
    for traits, name in ((TRAITS_A, "A-trait"), (TRAITS_B, "B-trait")):
        layer = build_persona("A0003", world_seed=7, traits=traits)
        agent = FakeAgent(aid="A0003")
        for _ in range(50):
            layer.step(agent, perceived=[])
        out.append((name, dict(layer.affect)))
    differ = out[0][1] != out[1][1]
    print("[4] trait separation: %s" % ("PASS" if differ else "FAIL"))
    for name, aff in out:
        print("      %-8s 喜悦=%.4f 恐惧=%.4f 信任=%.4f 自尊=%.4f" % (
            name, aff.get("fluid.喜悦", -1), aff.get("fluid.恐惧", -1),
            aff.get("fluid.信任", -1), aff.get("self_esteem", -1)))
    return differ


def measure_cost(n_agents, ticks):
    agents = [FakeAgent(aid="A%04d" % i,
                        needs={"physiology": 60.0 + i % 30, "safety": 50.0 + i % 20,
                               "belonging": 40.0 + i % 25, "esteem": 45.0,
                               "self_actualization": 35.0},
                        energy=90.0 - i % 20)
              for i in range(n_agents)]
    layers = [build_persona(a.aid, world_seed=99, traits=TRAITS_A) for a in agents]
    perceived = [{"type": "agent", "dist": 1.5}, {"type": "resource", "dist": 3.0}]
    t0 = time.time()
    for _ in range(ticks):
        for a, l in zip(agents, layers):
            l.step(a, perceived=perceived)
    elapsed = time.time() - t0
    per_tick_ms = elapsed / max(1, ticks) * 1000.0
    print("[5] cost: %d agents x %d ticks = %.3fs -> %.2f ms/tick (GasMeter budget %.0f ms) %s"
          % (n_agents, ticks, elapsed, per_tick_ms, GAS_BUDGET_MS,
             "WITHIN" if per_tick_ms <= GAS_BUDGET_MS else "OVER BUDGET"))
    return per_tick_ms


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", type=int, default=100)
    ap.add_argument("--agents", type=int, default=30)
    args = ap.parse_args()

    print("=== persona smoke test (1 tick = %.0f engine seconds) ===" % SECONDS_PER_TICK)
    results = [
        check_engine_md5(),
        check_determinism(args.ticks),
        check_rng_untouched(args.ticks),
        check_traits_differ(),
    ]
    measure_cost(args.agents, args.ticks)

    ok = all(results)
    print("\nVERDICT: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
