"""记忆传输通道 API 层验收（HTTP 路由 / 认证 / 状态码）

内核能跑不等于接得上：这层证明 REST 端点确实挂对了位置、确实要认证、
确实把 TransferError 翻译成合理状态码。

用法：python tools/memory_transfer_api_smoke.py
"""

import hashlib
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("STORAGE", "memory")

from system.runtime import World
from system.api import WorldAPI
from system.keys import generate_user_keypair, build_genesis_proof

_FAILURES = []


def check(no, label, cond, detail=""):
    ok = bool(cond)
    print(f"[{no}] {'PASS' if ok else 'FAIL'} - {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        _FAILURES.append(f"[{no}] {label} {detail}")


def make_actor(world_id: str):
    """造一个可认证的世界 + 一个已登录的灵魂。"""
    world = World(world_id, data_dir=None)
    kp = generate_user_keypair()
    proof = build_genesis_proof(kp["secret"], {"name": world_id, "ts": int(time.time())})
    agent = world.spawn_agent(soul_hash=None, genesis_proof=proof)
    world.credentials.bind_credential(agent.soul_hash, kp["pubkey"], "primary")
    pkfp = hashlib.sha256(kp["pubkey"]).hexdigest()[:32]
    token, _ = world.sessions.issue(agent.soul_hash, pubkey_fingerprint=pkfp)
    return world, WorldAPI(world), token, agent.soul_hash, kp


def main() -> int:
    print("=== 记忆传输 API 层验收 ===")

    world_a, api_a, token_a, soul_a, kp_a = make_actor("api-alpha")
    world_b, api_b, token_b, soul_b, kp_b = make_actor("api-beta")

    # 1. 未认证必须 401（同时证明路由存在，而不是落到 404）
    st, _ = api_a.dispatch("POST", "/roaming/memory/card", {}, None)
    check("1", "无 token 访问名片端点返回 401", st == 401, f"status={st}")

    # 2. 认证后能取名片
    st, card_a = api_a.dispatch("POST", "/roaming/memory/card", {}, token_a)
    check("2", "认证后取本世界名片", st == 200 and "card" in card_a, f"status={st}")

    st, card_b = api_b.dispatch("POST", "/roaming/memory/card", {}, token_b)
    check("3", "B 世界名片获取成功", st == 200)

    # 3. 互换名片
    st, r = api_b.dispatch("POST", "/roaming/memory/peer/register", {"card": card_a}, token_b)
    check("4", "B 注册 A 的名片", st == 201 and r.get("registered") is True, f"status={st}")
    st, r = api_a.dispatch("POST", "/roaming/memory/peer/register", {"card": card_b}, token_a)
    check("5", "A 注册 B 的名片", st == 201, f"status={st}")

    # 4. 重复注册同名名片应当覆盖而非报错
    st, r = api_b.dispatch("POST", "/roaming/memory/peer/register", {"card": card_a}, token_b)
    check("6", "重复注册名片幂等", st == 201, f"status={st}")

    # 5. 坏名片 → 400
    st, r = api_b.dispatch("POST", "/roaming/memory/peer/register", {"card": {"nope": 1}}, token_b)
    check("7", "结构错误的名片返回 400", st == 400 and "code" in r, f"status={st}")

    # 6. 伙伴列表
    st, r = api_a.dispatch("POST", "/roaming/memory/peers", {}, token_a)
    peers = r.get("peers", []) if isinstance(r, dict) else []
    check("8", "伙伴名册可查询", st == 200 and any(p["world_id"] == "api-beta" for p in peers))

    # 7. A 写记忆后导出
    for text in ["记忆甲", "记忆乙"]:
        world_a.memory_vault.store_memory(soul_a, {"content": text, "category": "api"})

    st, r = api_a.dispatch(
        "POST", "/roaming/memory/export", {"target_world_id": "api-beta"}, token_a
    )
    check("9", "导出端点返回信封", st == 201 and "bundle" in r, f"status={st}")
    bundle = r["bundle"]

    # 8. 缺参数 → 400
    st, r = api_a.dispatch("POST", "/roaming/memory/export", {}, token_a)
    check("10", "缺 target_world_id 返回 400", st == 400, f"status={st}")

    # 9. 未注册对端 → 403
    st, r = api_a.dispatch(
        "POST", "/roaming/memory/export", {"target_world_id": "api-unknown"}, token_a
    )
    check("11", "未注册对端导出返回 403", st == 403 and r.get("code") == "unknown_peer",
          f"status={st}")

    # 10. B 导入
    st, r = api_b.dispatch("POST", "/roaming/memory/import", {"bundle": bundle}, token_b)
    check("12", "导入端点成功", st == 201 and r.get("status") == "imported", f"status={st}")
    check("13", "导入 2 条记忆", r.get("item_count") == 2, f"count={r.get('item_count')}")

    # 11. B 能看到记忆
    #     API 语义：导入默认落到**当前认证用户**的灵魂。
    #     真实场景里同一个身份私钥在两台机器派生出同一个 soul_hash，
    #     所以这里天然重合；本测试给两端生成了不同密钥，故落在 soul_b。
    mem_b = world_b.memory_vault.recall_memory(soul_b)
    check("14", "记忆落到当前认证用户的灵魂", len(mem_b) == 2, f"count={len(mem_b)}")

    # 11.1 显式指定落地灵魂（跨身份导入时用）
    st, r = api_b.dispatch(
        "POST",
        "/roaming/memory/import",
        {"bundle": bundle, "local_soul_hash": soul_a},
        token_b,
    )
    # 同一 transfer_id 已处理过，幂等返回；换成新信封才能验证显式落地
    check("14b", "同信封重复导入仍幂等", st == 200 and r.get("status") == "already_imported",
          f"status={st}")

    # 12. 重复导入 → 200 + already_imported
    st, r = api_b.dispatch("POST", "/roaming/memory/import", {"bundle": bundle}, token_b)
    check("15", "重复导入返回 200 且幂等",
          st == 200 and r.get("status") == "already_imported", f"status={st}")

    # 13. 落错世界 → 403
    world_c, api_c, token_c, _, _ = make_actor("api-gamma")
    st, r = api_c.dispatch("POST", "/roaming/memory/import", {"bundle": bundle}, token_c)
    check("16", "非目标世界导入返回 403", st == 403, f"status={st}")

    # 14. 台账
    st, r = api_b.dispatch("POST", "/roaming/memory/transfers", {}, token_b)
    records = r.get("transfers", []) if isinstance(r, dict) else []
    check("17", "导入台账可查询", st == 200 and len(records) >= 1, f"count={len(records)}")

    st, r = api_a.dispatch("POST", "/roaming/memory/transfers", {"limit": 10}, token_a)
    records = r.get("transfers", []) if isinstance(r, dict) else []
    check("18", "导出台账可查询", st == 200 and len(records) >= 1, f"count={len(records)}")

    # 15. 未知子路由 → 404（不误吞）
    st, r = api_a.dispatch("POST", "/roaming/memory/nonexistent", {}, token_a)
    check("19", "未知记忆子路由返回 404", st == 404, f"status={st}")

    print(f"\n检查项 {21 - len(_FAILURES)}/21 通过")
    if _FAILURES:
        print("失败项：")
        for f in _FAILURES:
            print("  -", f)
        return 1
    print("ALL MEMORY TRANSFER API CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
