# -*- coding: utf-8 -*-
"""《红尘泪》世界层验收（tools 级冒烟）

不信任 demo/hongchenlei.py 的自述，逐项重跑：

  武道侧（真气守恒 / 境界状态机 / 吸收上限 / 丹田剥夺 / 榜单排除）
  跨世界侧（名片互注 → 记忆加密过境 → 篡改拒 → 重放幂等）

运行：python tools/hongchenlei_smoke.py
退出码 0 = 全部通过。
"""

import copy
import hashlib
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
for p in (_REPO, os.path.join(_REPO, "demo")):
    if p not in sys.path:
        sys.path.insert(0, p)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from constitution_rules import NOHN_LAW_AXIOMS                     # noqa: E402
from demo.real_virtual_world import RealWorld                       # noqa: E402
from demo.hongchenlei import (                                      # noqa: E402
    QiLedger, HongchenleiRealm, rank_name, try_promote,
    build_board, eligible_for_board, RANK_INDEX, WUDAO_LADDER,
)
from system.memory_transfer import TransferError                    # noqa: E402

_TOL = float(NOHN_LAW_AXIOMS["conservation_tolerance"])
_RESULTS = []


def check(name, ok, detail=""):
    _RESULTS.append((name, bool(ok), detail))
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  " + detail) if detail else ""))
    return bool(ok)


# ============================================================
# 一、真气守恒账本（直接用账本，不依赖世界）
# ============================================================

def _fake(name="X", qi=0.0, rank=0):
    class _A:
        pass
    a = _A()
    a.name = name
    a.qi = qi
    a.rank_idx = rank
    a.dantian_intact = True
    a.faction = "无门无派"
    a.role = "江湖"
    a.retired = False
    a.age = 30
    return a


def test_qi_ledger():
    print("\n=== 一、真气守恒账本 ===")
    led = QiLedger(genesis_qi=10000.0)
    a, b = _fake("a"), _fake("b")
    agents = [a, b]
    check("创世：全部真气在灵气池", abs(led.residual(agents)) <= _TOL,
          "residual=%.3e" % led.residual(agents))

    led.allocate(a, 3000.0)
    check("入门分配守恒（池→人，非凭空创造）",
          abs(led.residual(agents)) <= _TOL and a.qi == 3000.0,
          "residual=%.3e" % led.residual(agents))

    led.consume(a, 500.0)
    check("施放消耗守恒（人→消散，非凭空消失）",
          abs(led.residual(agents)) <= _TOL and led.dissipated == 500.0,
          "dissipated=%.1f" % led.dissipated)

    before = a.qi + b.qi          # a=2500（3000 入门 - 500 施放），b=0
    expect_moved = round(before * 0.3, 10)
    led.absorb(a, b, ratio=0.3)
    check("《千秋内经》吸收=转移（总量不变）",
          abs((a.qi + b.qi) - before) <= _TOL and abs(b.qi - expect_moved) <= _TOL,
          "src=%.1f dst=%.1f (转移 %.1f，总量 %.1f 不变)" % (a.qi, b.qi, b.qi, before))
    check("吸收守恒式仍闭合", abs(led.residual(agents)) <= _TOL,
          "residual=%.3e" % led.residual(agents))

    # 边界条款 2：吸收上限
    c, d = _fake("c", qi=1000.0), _fake("d", qi=0.0)
    led2 = QiLedger(10000.0)
    led2.allocate(c, 0.0)
    c.qi = 1000.0
    got = led2.absorb(c, d, ratio=0.3)
    check("吸收上限生效（单次 ≤ 对方 30%）", abs(got - 300.0) <= _TOL,
          "absorbed=%.1f / src had 1000" % got)

    # 边界条款 1：丹田被废 = 剥夺
    e = _fake("e", qi=2000.0, rank=5)
    led3 = QiLedger(10000.0)
    e.qi = 2000.0
    led3.unallocated = 8000.0
    lost = led3.shatter(e)
    check("丹田被废：真气尽归消散、境界归零",
          e.qi == 0.0 and e.rank_idx == 0 and lost == 2000.0,
          "lost=%.1f rank=%s" % (lost, rank_name(e.rank_idx)))
    check("丹田被废后守恒仍闭合", abs(led3.residual([e])) <= _TOL,
          "residual=%.3e" % led3.residual([e]))
    check("被废者不得再晋升", try_promote(e) == 0 and e.rank_idx == 0)


# ============================================================
# 二、境界状态机（单调 / 门槛 / 连升）
# ============================================================

def test_ranks():
    print("\n=== 二、境界状态机 ===")
    check("阶梯次序正确（九品最低、仙绝最高）",
          WUDAO_LADDER[0] == "九品" and WUDAO_LADDER[-1] == "仙绝")
    a = _fake("p")
    a.qi = 50000.0            # 越过极道门槛
    n = try_promote(a)
    check("按门槛连升至极道", rank_name(a.rank_idx) == "极道" and n > 0,
          "promoted=%d rank=%s" % (n, rank_name(a.rank_idx)))
    r_before = a.rank_idx
    a.qi = 10.0               # 真气暴跌
    try_promote(a)
    check("境界只升不降（时间不可逆）", a.rank_idx == r_before,
          "rank=%s" % rank_name(a.rank_idx))
    a.qi = 200000.0
    try_promote(a)
    check("仙绝为阶梯顶端（不越界）",
          a.rank_idx == RANK_INDEX["仙绝"], "rank=%s" % rank_name(a.rank_idx))


# ============================================================
# 三、榜单排除规则（边界条款 3）
# ============================================================

def test_boards():
    print("\n=== 三、江湖三榜 ===")
    court = _fake("朝臣", qi=99999.0)
    court.role = "朝臣"
    court.rank_idx = RANK_INDEX["化境"]
    retired = _fake("退隐", qi=99999.0)
    retired.retired = True
    retired.rank_idx = RANK_INDEX["化境"]
    hero = _fake("江湖客", qi=5000.0)
    hero.rank_idx = RANK_INDEX["一品·下位"]
    pool = [court, retired, hero]
    board = build_board(pool, limit=10)
    check("朝臣不入榜", court not in board)
    check("退隐者不入榜", retired not in board)
    check("江湖客入榜且居首", board[0] is hero, "榜首=%s" % board[0].name)
    young = _fake("少年", qi=100.0, rank=3)
    young.age = 15
    old = _fake("老辈", qi=900.0, rank=6)
    old.age = 60
    shaoyou = build_board([young, old], limit=50, max_age=20)
    check("少君榜按年龄截断（二十岁以下）", shaoyou == [young])
    di = build_board([young, old], limit=20, max_age=50)
    check("地星榜按年龄截断（五十岁以下）", di == [young])


# ============================================================
# 四、挂到真世界：跑 tick，守恒须始终成立
# ============================================================

def test_world_run():
    print("\n=== 四、真栈世界运行（100 tick）===")
    from demo.virtual_world import WorldConfig
    cfg = WorldConfig(world_size=40, initial_agents=20, initial_resources=60,
                      initial_buildings=4, seed=7)
    w = RealWorld(cfg, world_id="world-alpha")
    worst = 0.0
    for _ in range(100):
        w.tick()
        worst = max(worst, abs(w.realm.ledger.residual(list(w.agents))))
    s = w.realm.summary()
    check("真气守恒全程成立（100 tick 最大残差 ≤ 容差）", worst <= _TOL,
          "worst=%.3e tol=%g" % (worst, _TOL))
    check("世界层守恒判定为真", s["conservation_holds"] is True)
    check("确有人晋升（体系在跑，不是死数据）", s["promotions"] > 0,
          "promotions=%d" % s["promotions"])
    return w


# ============================================================
# 五、跨世界记忆加密过境（端到端）
# ============================================================

def test_memory_transfer():
    print("\n=== 五、跨世界记忆加密过境 ===")
    from demo.virtual_world import WorldConfig
    cfg_a = WorldConfig(world_size=30, initial_agents=8, initial_resources=40,
                        initial_buildings=2, seed=11)
    cfg_b = WorldConfig(world_size=30, initial_agents=8, initial_resources=40,
                        initial_buildings=2, seed=12)
    A = RealWorld(cfg_a, world_id="world-alpha")
    B = RealWorld(cfg_b, world_id="world-beta")
    for _ in range(30):
        A.tick()
        B.tick()

    # 名片互换并验签
    card_a = A.memory_channel.export_world_card()
    card_b = B.memory_channel.export_world_card()
    r1 = B.memory_channel.register_world_card(card_a)
    r2 = A.memory_channel.register_world_card(card_b)
    check("双向名片注册通过",
          r1.get("ok", True) and r2.get("ok", True),
          "A->B=%s B->A=%s" % (r1.get("ok"), r2.get("ok")))

    # 归档并导出
    soul = next(iter(A._soul_pubkeys), None)
    n = A.mirror_memories(soul)
    check("记忆归档进真实记忆库", n > 0, "memories=%d" % n)
    bundle = A.memory_channel.export_bundle(
        target_world_id="world-beta", soul_hash=soul,
        user_pubkey=A._soul_pubkeys[soul])
    check("信封生成", bool(bundle.get("transfer_id")),
          "transfer=%s" % bundle.get("transfer_id"))

    # 信封内不得含记忆明文
    raw = json.dumps(bundle, ensure_ascii=False)
    leak = [m for m in ["第一次看见海", "决定原谅", "学会说谎"] if m in raw]
    # 通用做法：比对明文是否出现在密文里
    plain_seen = False
    rows = A.soul_memory_vault._storage.query(
        "SELECT memory FROM memories WHERE soul_hash=?", (soul,))
    for (mem,) in rows:
        try:
            content = json.loads(mem).get("content", "")
        except Exception:
            content = str(mem)
        if content and content in raw:
            plain_seen = True
            break
    check("信封内不含记忆明文", not plain_seen)

    # 导入
    res = B.memory_channel.import_bundle(bundle)
    check("B 世界导入成功",
          res.get("status") in ("ok", "imported", "success") or res.get("item_count", 0) > 0,
          json.dumps(res, ensure_ascii=False)[:120])

    # 重放幂等
    res2 = B.memory_channel.import_bundle(bundle)
    check("重放同一信封幂等", res2.get("status") == "already_imported",
          str(res2.get("status")))

    # 篡改密文 → 必须拒绝。
    # 注意：必须用**新信封**——上一步已导入的 transfer_id 会命中幂等前置检查
    # （未解密即返回 already_imported），那样测不到 AES-GCM 的认证失败。
    fresh = A.memory_channel.export_bundle(
        target_world_id="world-beta", soul_hash=soul,
        user_pubkey=A._soul_pubkeys[soul])
    check("篡改用例使用全新 transfer_id",
          fresh.get("transfer_id") != bundle.get("transfer_id"),
          "fresh=%s" % fresh.get("transfer_id"))
    bad = copy.deepcopy(fresh)
    ct = bad["ciphertext_b64"]
    bad["ciphertext_b64"] = ("A" if ct[0] != "A" else "B") + ct[1:]
    rejected = False
    detail = ""
    try:
        B.memory_channel.import_bundle(bad)
    except TransferError as exc:
        rejected = True
        detail = getattr(exc, "code", "")
    except Exception:
        rejected = True
        detail = "raised"
    check("篡改密文被拒（AES-GCM 认证失败）", rejected, "code=%s" % detail)

    # 非目标世界导入被拒
    C = RealWorld(WorldConfig(world_size=30, initial_agents=6,
                              initial_resources=30, initial_buildings=2, seed=13),
                  world_id="world-gamma")
    C.memory_channel.register_world_card(card_a)
    wrong = False
    try:
        C.memory_channel.import_bundle(bundle)
    except Exception:
        wrong = True
    check("非目标世界导入被拒", wrong)


def main():
    print("=== 《红尘泪》世界层验收 ===")
    test_qi_ledger()
    test_ranks()
    test_boards()
    test_world_run()
    test_memory_transfer()
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    total = len(_RESULTS)
    print("\n检查项 %d/%d 通过" % (passed, total))
    if passed == total:
        print("ALL HONGCHENLEI CHECKS PASSED")
        raise SystemExit(0)
    print("FAILED: %s" % ", ".join(n for n, ok, _ in _RESULTS if not ok))
    raise SystemExit(1)


if __name__ == "__main__":
    main()
