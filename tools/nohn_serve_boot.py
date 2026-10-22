# -*- coding: utf-8 -*-
"""在**本机**起一个可被另一台电脑联通的 system.World（HTTP + WS）。

它做三件事：
  1. 用持久化数据目录打开世界（重启不丢）
  2. 生成本机操作者灵魂（密钥只落在本机 data 目录，永不上传），
     并把本世界名片导出到 world_card.json（给对方注册用）
  3. 0.0.0.0 监听，进入阻塞服务

用法：
  python tools/nohn_serve_boot.py --world nohn-world-a --data D:\\nohn_world_a --port 8000

安全边界（务必知悉）：
  · 明文 HTTP，无 TLS —— 只在可信局域网内跑，不要映射到公网
  · 任何"已认证灵魂"都能调用 /keys/rotate、/keys/revoke（无角色模型）
  · data 目录含世界私钥与操作者私钥，等于本机身份本体，永不外发
"""

import argparse
import base64
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from system.runtime import World                                     # noqa: E402
from system.keys import (                                            # noqa: E402
    generate_user_keypair, build_genesis_proof, derive_soul_hash_from_pubkey,
)
from system.api import serve                                         # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="启动可跨机联通的 system.World")
    ap.add_argument("--world", default="nohn-world-a")
    ap.add_argument("--data", default=os.path.join(_REPO, ".world_data", "nohn_world_a"))
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--card-out", default="world_card.json")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    os.makedirs(args.data, exist_ok=True)
    world = World(args.world, data_dir=args.data)

    # ---- 本机操作者灵魂：密钥只存本机 ----
    key_path = os.path.join(args.data, "operator_key.b64")
    if os.path.exists(key_path):
        secret = base64.b64decode(open(key_path, "r").read().strip())
    else:
        kp = generate_user_keypair()
        secret = kp["secret"]
        with open(key_path, "w") as f:
            f.write(base64.b64encode(secret).decode("ascii"))
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass

    from system.keys import public_from_private
    pub = public_from_private(secret)
    soul_hash = derive_soul_hash_from_pubkey(pub)

    if not world.soul_ledger.exists(soul_hash):
        proof = build_genesis_proof(secret, {
            "world": args.world, "purpose": "local-operator",
        })
        agent = world.spawn_agent(soul_hash=soul_hash, genesis_proof=proof)
        if agent is None:
            print("[boot] 操作者灵魂注册失败", file=sys.stderr)
            return 1
    print("[boot] 世界: %s" % args.world)
    print("[boot] 操作者灵魂: %s" % soul_hash)
    print("[boot] 数据目录: %s" % os.path.abspath(args.data))

    # ---- 本世界名片（交给对方注册）----
    card = world.memory_channel.export_world_card()
    with open(args.card_out, "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False, indent=2)
    print("[boot] 名片已写出: %s (world=%s)" % (
        os.path.abspath(args.card_out),
        card.get("card", {}).get("world_id", "?")))

    print("[boot] 监听 %s:%d （明文 HTTP，仅限可信局域网）" % (args.host, args.port))
    serve(world, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
