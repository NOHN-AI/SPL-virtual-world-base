"""记忆传输通道验收测试（跨世界记忆过境）

验证的不是"函数能跑"，而是"这封信封能不能被伪造和重放"：

  正常路径   A 世界导出 → B 世界导入 → 记忆可读、可验封
  隐私      信封里搜不到记忆明文
  防篡改    改密文 / 改清单 → 必须拒收
  防重放    同一信封导两次 → 只落一次，条目数不膨胀
  防错投    第三个世界拿着别人的信封 → 必须拒收
  防误发    没交换名片就导出 → 必须拒绝
  选择性授权 只选 1 条就只走 1 条

用法：python tools/memory_transfer_smoke.py
退出码 0 = 全部通过。
"""

import base64
import copy
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("STORAGE", "memory")

from system.runtime import World
from system.keys import generate_user_keypair, build_genesis_proof
from system.memory_transfer import TransferError

_FAILURES = []
_CHECKS = 0


def check(no: str, label: str, condition, detail: str = "") -> None:
    global _CHECKS
    _CHECKS += 1
    ok = bool(condition)
    print(f"[{no}] {'PASS' if ok else 'FAIL'} - {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        _FAILURES.append(f"[{no}] {label} {detail}")


def expect_error(no: str, label: str, code: str, fn) -> None:
    """断言某个操作必须以指定 code 失败。"""
    try:
        fn()
    except TransferError as e:
        check(no, label, e.code == code, f"code={e.code} expected={code}")
    except Exception as e:  # 非预期异常也算失败
        check(no, label, False, f"unexpected={type(e).__name__}: {e}")
    else:
        check(no, label, False, "no error raised")


def main() -> int:
    print("=== 记忆传输通道验收 ===")
    t0 = time.time()

    # ---- 1. 两个独立世界 ----
    world_a = World("world-alpha", data_dir=None)
    world_b = World("world-beta", data_dir=None)
    world_c = World("world-gamma", data_dir=None)
    check("1", "三个独立世界创世完成",
          world_a.world_id == "world-alpha" and world_b.world_id == "world-beta")

    # ---- 2. 交换名片（这是唯一的带外信任点）----
    card_a = world_a.memory_channel.export_world_card()
    card_b = world_b.memory_channel.export_world_card()
    world_b.memory_channel.register_world_card(card_a)
    world_a.memory_channel.register_world_card(card_b)
    check("2", "名片互换并验签通过",
          world_b.memory_channel.get_peer("world-alpha") is not None
          and world_a.memory_channel.get_peer("world-beta") is not None)

    # 名片被篡改必须认出来（换掉通道公钥 = 中间人攻击）
    forged = copy.deepcopy(card_a)
    forged["card"]["transport_pubkey_b64"] = base64.b64encode(
        os.urandom(32)
    ).decode("ascii")
    expect_error("3", "被替换通道公钥的名片被拒", "invalid_card",
                 lambda: world_b.memory_channel.register_world_card(forged))

    # ---- 3. A 世界注册灵魂并写入记忆 ----
    kp = generate_user_keypair()
    proof = build_genesis_proof(kp["secret"], {"name": "roamer", "ts": int(time.time())})
    agent = world_a.spawn_agent(soul_hash=None, genesis_proof=proof)
    soul = agent.soul_hash
    for i, text in enumerate(["第一次看见海", "学会说谎", "决定原谅"], start=1):
        world_a.memory_vault.store_memory(
            soul, {"content": text, "category": "life", "index": i}
        )
    recalled_a = world_a.memory_vault.recall_memory(soul)
    check("4", f"A 世界写入记忆 {len(recalled_a)} 条", len(recalled_a) == 3)

    # ---- 4. 未交换名片就导出 → 必须拒绝 ----
    expect_error("5", "未注册对端名片时拒绝导出", "unknown_peer",
                 lambda: world_a.memory_channel.export_bundle("world-gamma", soul, user_pubkey=kp["pubkey"]))

    # ---- 5. 正常导出 ----
    bundle = world_a.memory_channel.export_bundle(
        target_world_id="world-beta", soul_hash=soul, user_pubkey=kp["pubkey"]
    )
    check("6", "信封生成成功",
          bundle["bundle_version"] == 1 and len(bundle["manifest"]) == 3,
          f"transfer={bundle['transfer_id'][:12]}")

    # ---- 6. 隐私：信封里不能有明文记忆 ----
    bundle_text = json.dumps(bundle, ensure_ascii=False)
    leaked = [w for w in ["第一次看见海", "学会说谎", "决定原谅"] if w in bundle_text]
    check("7", "信封内不含记忆明文", not leaked, f"泄露={leaked}")

    # ---- 7. B 世界导入 ----
    result = world_b.memory_channel.import_bundle(bundle)
    check("8", "B 世界导入成功", result["status"] == "imported" and result["item_count"] == 3)

    recalled_b = world_b.memory_vault.recall_memory(soul)
    check("9", f"B 世界读到 {len(recalled_b)} 条记忆", len(recalled_b) == 3)

    contents = [r["memory"]["content"] for r in recalled_b]
    check("10", "记忆内容逐条一致（结构未被套壳）",
          contents == ["第一次看见海", "学会说谎", "决定原谅"], f"{contents}")

    # ---- 8. 跨世界 seal 问题：本地必须重新封存且验得过 ----
    check("11", "B 世界本地重新封存可验证",
          world_b.memory_vault.verify_all(soul) is True)
    origin = recalled_b[0]["memory"].get("_roaming", {}).get("origin", {})
    check("12", "源世界 seal 作为来源凭证保留",
          origin.get("world_id") == "world-alpha" and len(origin.get("source_seal") or "") == 64,
          f"from={origin.get('world_id')}")

    # 源 seal 在 A 世界可验、在 B 世界不可验（证明"不能直接搬 seal"这个判断成立）
    a_seal_ok = world_a.memory_vault.guardian.verify_memory(
        world_a.memory_vault.recall_memory(soul)[0]["memory"],
        world_a.memory_vault.recall_memory(soul)[0]["seal"],
        soul,
    )
    check("13", "A 世界自己的 seal 可在 A 验证", a_seal_ok is True)

    # ---- 9. 防重放：同一个信封再导一次 ----
    replay = world_b.memory_channel.import_bundle(bundle)
    count_after = len(world_b.memory_vault.recall_memory(soul))
    check("14", "重放同一信封幂等（不重复写入）",
          replay["status"] == "already_imported" and count_after == 3,
          f"status={replay['status']} count={count_after}")

    # ---- 10. 防篡改：改密文 ----
    tampered = copy.deepcopy(bundle)
    raw = bytearray(base64.b64decode(tampered["ciphertext_b64"]))
    raw[10] ^= 0xFF
    tampered["ciphertext_b64"] = base64.b64encode(bytes(raw)).decode("ascii")
    tampered["transfer_id"] = "tamper-ct-001"
    expect_error("15", "篡改密文被拒（AES-GCM 认证失败）", "decrypt_failed",
                 lambda: world_b.memory_channel.import_bundle(tampered))

    # ---- 11. 防篡改：改清单（把大小改掉）----
    # 清单整体在证书签名范围内，所以第一道拦截是验签（cert_rejected）；
    # 显式比对（manifest_mismatch）是第二道纵深防御，实现走样时才用得上。
    tampered2 = copy.deepcopy(bundle)
    tampered2["transfer_id"] = "tamper-manifest-001"
    tampered2["manifest"][0]["size_bytes"] = 999999
    try:
        world_b.memory_channel.import_bundle(tampered2)
    except TransferError as e:
        check("16", "篡改清单被拒（签名层拦截）",
              e.code in ("cert_rejected", "manifest_mismatch"),
              f"code={e.code}")
    else:
        check("16", "篡改清单被拒（签名层拦截）", False, "no error raised")

    # ---- 12. 防篡改：改 content_hash ----
    tampered3 = copy.deepcopy(bundle)
    tampered3["transfer_id"] = "tamper-hash-001"
    tampered3["content_hash"] = "0" * 64
    expect_error("17", "篡改明文哈希被拒", "decrypt_failed",
                 lambda: world_b.memory_channel.import_bundle(tampered3))

    # ---- 13. 防错投：第三个世界拿着别人的信封 ----
    world_c.memory_channel.register_world_card(card_a)  # C 认识 A，但信封不是寄给 C 的
    expect_error("18", "非目标世界导入被拒", "cert_rejected",
                 lambda: world_c.memory_channel.import_bundle(bundle))

    # ---- 14. 选择性授权：只带走 1 条 ----
    partial = world_a.memory_channel.export_bundle(
        target_world_id="world-beta",
        soul_hash=soul,
        user_selected_ids={"2"},
        user_pubkey=kp["pubkey"],
    )
    check("19", "选择性导出只含 1 条", len(partial["manifest"]) == 1,
          f"ids={[m['memory_id'] for m in partial['manifest']]}")

    # 换一个独立灵魂接收，避免与前面 3 条混在一起
    kp2 = generate_user_keypair()
    proof2 = build_genesis_proof(kp2["secret"], {"name": "partial-receiver", "ts": int(time.time())})
    agent2 = world_a.spawn_agent(soul_hash=None, genesis_proof=proof2)
    world_a.memory_vault.store_memory(
        agent2.soul_hash, {"content": "另一个灵魂的唯一记忆", "category": "misc"}
    )
    partial2 = world_a.memory_channel.export_bundle(
        target_world_id="world-beta",
        soul_hash=agent2.soul_hash,
        user_pubkey=kp2["pubkey"],
    )
    res2 = world_b.memory_channel.import_bundle(partial2)
    check("20", "独立灵魂的记忆单独传输成功",
          res2["status"] == "imported" and res2["item_count"] == 1)

    # ---- 15. 灵魂映射：先 map 再导入，记忆落到本地灵魂 ----
    kp3 = generate_user_keypair()
    proof3 = build_genesis_proof(kp3["secret"], {"name": "local-self", "ts": int(time.time())})
    local_agent = world_b.spawn_agent(soul_hash=None, genesis_proof=proof3)
    local_soul = local_agent.soul_hash

    bundle3 = world_a.memory_channel.export_bundle(
        target_world_id="world-beta", soul_hash=soul, user_pubkey=kp["pubkey"]
    )
    from system.soul_roaming import SoulRoamingCertificate

    cert3 = SoulRoamingCertificate.from_dict(bundle3["certificate"])
    valid, reason = world_b.roaming.verify_roaming_certificate(cert3)
    check("21", "B 验证 A 的漫游证书通过", valid, reason)

    world_b.roaming.map_roaming_soul(cert3, local_soul)
    res3 = world_b.memory_channel.import_bundle(bundle3)
    check("22", "映射后记忆落到本地灵魂",
          res3["local_soul_hash"] == local_soul and res3["mapped"] is True)
    local_memories = world_b.memory_vault.recall_memory(local_soul)
    check("23", f"本地灵魂持有 {len(local_memories)} 条漫游记忆", len(local_memories) == 3)

    # ---- 16. 台账 ----
    transfers_b = world_b.memory_channel.list_transfers()
    imports = [t for t in transfers_b if t["direction"] == "import"]
    check("24", f"B 世界导入台账 {len(imports)} 条", len(imports) >= 3)

    transfers_a = world_a.memory_channel.list_transfers()
    exports = [t for t in transfers_a if t["direction"] == "export"]
    check("25", f"A 世界导出台账 {len(exports)} 条", len(exports) == 4)

    # ---- 汇总 ----
    elapsed = time.time() - t0
    print(f"\n检查项 {_CHECKS - len(_FAILURES)}/{_CHECKS} 通过，用时 {elapsed:.2f}s")
    if _FAILURES:
        print("失败项：")
        for f in _FAILURES:
            print("  -", f)
        return 1
    print("ALL MEMORY TRANSFER CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
