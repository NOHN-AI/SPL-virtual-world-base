#!/usr/bin/env python3
"""跨机记忆漫游 CLI —— 两台电脑之间搬记忆的命令行工具。

为什么要有这个东西
------------------
两台电脑未必能直连：可能隔着不同城市、不同网络，甚至一堵公司防火墙。
所以这里不假定任何网络，只假定"你能把一个文件搬到对面"——U 盘、
网盘、微信、邮件，随便。协议负责加密与校验，搬运用什么方式不重要。

典型流程（以 A 机 → B 机 为例）
------------------------------
  # ① 两边各生成一次"世界名片"（一张 JSON，含世界公钥与通道公钥）
  A> python tools/roam_memory.py --world my-world --data ./data card -o cardA.json
  B> python tools/roam_memory.py --world my-world --data ./data card -o cardB.json

  # ② 互相注册（这一步是唯一的带外信任点：你得确认真拿到的是对方的名片）
  A> python tools/roam_memory.py --world my-world --data ./data register cardB.json
  B> python tools/roam_memory.py --world my-world --data ./data register cardA.json

  # ③ A 导出记忆（端到端加密，直接寄给 B）
  A> python tools/roam_memory.py --world my-world --data ./data souls
  A> python tools/roam_memory.py --world my-world --data ./data export \
         --to <B的世界ID> --soul <你的soul_hash> -o memory_A.srbundle

  # ④ 把 memory_A.srbundle 拷到 B 机，导入
  B> python tools/roam_memory.py --world my-world --data ./data import memory_A.srbundle

  # ⑤ 看台账
  B> python tools/roam_memory.py --world my-world --data ./data transfers

安全边界（请认真看）
--------------------
- 信封本身是加密的，丢了也不泄露内容；但**名片必须亲手核对**。
  名片被换 = 通道公钥被换 = 记忆寄给了别人。
- 世界签名私钥与通道私钥都在 data 目录下的密钥库里。data 目录等于你的
  身份，别随便外发。
- 导入是幂等的：同一个信封重复导入只落一次。

退出码：0 成功 / 1 失败
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _open_world(args):
    from system.runtime import World

    return World(args.world, data_dir=args.data)


def _channel(world):
    channel = getattr(world, "memory_channel", None)
    if channel is None:
        print("错误：本世界未装配记忆传输通道", file=sys.stderr)
        sys.exit(1)
    return channel


def _dump(obj, out_path):
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"已写入 {out_path}")
    else:
        print(text)


def _load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------- 子命令 ----------------

def cmd_card(args):
    world = _open_world(args)
    card = _channel(world).export_world_card()
    _dump(card, args.out)


def cmd_register(args):
    world = _open_world(args)
    channel = _channel(world)
    card = _load_json(args.file)
    try:
        result = channel.register_world_card(card)
    except Exception as e:
        print(f"注册失败：{getattr(e, 'message', e)}", file=sys.stderr)
        return 1
    print(f"已注册世界 {result['world_id']}")
    print(f"  身份公钥 : {result['identity_pubkey_b64'][:24]}...")
    print(f"  通道公钥 : {result['transport_pubkey_b64'][:24]}...")
    print("提示：请确认这两个公钥来自对方本人，而不是中间转发者。")
    return 0


def cmd_peers(args):
    world = _open_world(args)
    peers = _channel(world).list_peers()
    if not peers:
        print("（尚未注册任何伙伴世界）")
        return 0
    print(f"{'世界ID':<24} {'通道公钥':<28} 注册时间")
    for p in peers:
        stamp = p.get("registered_at") or 0
        import datetime

        when = datetime.datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M")
        print(f"{p['world_id']:<24} {p['transport_pubkey_b64'][:26]:<28} {when}")
    return 0


def cmd_souls(args):
    world = _open_world(args)
    rows = world.storage.query("SELECT soul_hash FROM souls ORDER BY created_at")
    if not rows:
        print("（本世界还没有注册任何灵魂）")
        return 0
    print(f"本世界灵魂共 {len(rows)} 个：")
    for (soul_hash,) in rows:
        memories = world.memory_vault.recall_memory(soul_hash)
        print(f"  {soul_hash}   记忆 {len(memories)} 条")
    return 0


def cmd_memories(args):
    world = _open_world(args)
    rows = world.storage.query(
        "SELECT seq, memory, ts FROM memories WHERE soul_hash=? ORDER BY seq",
        (args.soul,),
    )
    if not rows:
        print(f"（{args.soul[:16]}... 没有记忆）")
        return 0
    print(f"灵魂 {args.soul[:16]}... 共 {len(rows)} 条记忆：")
    for seq, memory_json, ts in rows[: args.limit]:
        try:
            content = json.loads(memory_json)
        except Exception:
            content = {"<无法解析>": memory_json[:40]}
        preview = json.dumps(content, ensure_ascii=False)[:90]
        print(f"  [seq={seq}] {preview}")
    if len(rows) > args.limit:
        print(f"  ...（仅显示前 {args.limit} 条，用 --limit 调整）")
    return 0


def cmd_export(args):
    world = _open_world(args)
    channel = _channel(world)
    selected = set(args.ids.split(",")) if args.ids else None
    try:
        bundle = channel.export_bundle(
            target_world_id=args.to,
            soul_hash=args.soul,
            user_selected_ids=selected,
            cert_ttl_seconds=args.ttl,
        )
    except Exception as e:
        print(f"导出失败：{getattr(e, 'message', e)}", file=sys.stderr)
        return 1
    out = args.out or f"memory_{args.to}_{bundle['transfer_id'][:8]}.srbundle"
    _dump(bundle, out)
    print(f"信封 transfer_id = {bundle['transfer_id']}")
    print(f"记忆条目 {len(bundle['manifest'])} 条，已加密寄往 {args.to}")
    print("把这个文件用任何方式送到对方电脑即可，它自身不含明文。")
    return 0


def cmd_import(args):
    world = _open_world(args)
    channel = _channel(world)
    bundle = _load_json(args.file)
    try:
        result = channel.import_bundle(bundle, local_soul_hash=args.soul)
    except Exception as e:
        print(f"导入失败：{getattr(e, 'message', e)}", file=sys.stderr)
        return 1
    if result["status"] == "already_imported":
        print(f"这封信封之前已导入过（{result['transfer_id'][:12]}），未重复写入。")
        return 0
    print(f"导入成功：来自 {result['source_world_id']} 的 {result['item_count']} 条记忆")
    print(f"  落地灵魂 : {result['local_soul_hash']}")
    print(f"  源灵魂   : {result['source_soul_hash']}")
    print(f"  transfer : {result['transfer_id']}")
    if result.get("mapped"):
        print("  （已按漫游映射落到本地灵魂）")
    return 0


def cmd_transfers(args):
    world = _open_world(args)
    records = _channel(world).list_transfers(args.limit)
    if not records:
        print("（还没有任何传输记录）")
        return 0
    import datetime

    print(f"{'方向':<8}{'来源世界':<18}{'目标世界':<18}{'条数':<6}{'时间'}")
    for r in records:
        when = datetime.datetime.fromtimestamp(r["created_at"]).strftime("%m-%d %H:%M")
        print(
            f"{r['direction']:<8}{r['source_world_id']:<18}"
            f"{r['target_world_id']:<18}{r['item_count']:<6}{when}"
        )
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="跨机记忆漫游 CLI（加密信封 + 文件中转）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="先用 card 导出名片、register 互相注册，再 export / import。",
    )
    parser.add_argument("--world", required=True, help="本世界 ID（两台机器必须不同）")
    parser.add_argument("--data", default=None, help="世界数据目录（含密钥，勿外发）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("card", help="导出本世界名片")
    p.add_argument("-o", "--out", help="写入文件（默认打印到终端）")
    p.set_defaults(func=cmd_card)

    p = sub.add_parser("register", help="注册对方世界名片")
    p.add_argument("file", help="对方名片 JSON")
    p.set_defaults(func=cmd_register)

    p = sub.add_parser("peers", help="列出已注册伙伴世界")
    p.set_defaults(func=cmd_peers)

    p = sub.add_parser("souls", help="列出本世界灵魂")
    p.set_defaults(func=cmd_souls)

    p = sub.add_parser("memories", help="列出某个灵魂的记忆")
    p.add_argument("--soul", required=True)
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_memories)

    p = sub.add_parser("export", help="导出加密记忆信封")
    p.add_argument("--to", required=True, help="目标世界 ID（须已注册）")
    p.add_argument("--soul", required=True, help="要迁移的灵魂 hash")
    p.add_argument("--ids", help="只迁移指定 memory_id，逗号分隔（seq 编号）")
    p.add_argument("--ttl", type=int, default=None, help="证书有效期（秒）")
    p.add_argument("-o", "--out", help="输出文件（.srbundle）")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("import", help="导入记忆信封")
    p.add_argument("file", help=".srbundle 文件")
    p.add_argument("--soul", help="落地到指定灵魂（默认认证身份/源灵魂）")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("transfers", help="查看传输台账")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_transfers)

    args = parser.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
