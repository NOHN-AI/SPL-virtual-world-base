# -*- coding: utf-8 -*-
"""Second Reality · 真栈世界（第一个 25/25 全过的真实虚拟世界）
====================================================================

定位
----
本文件是 demo/virtual_world.py（教学演示，22/25 + 三项如实失败）的「真栈版」：
继承同一套规则层驱动的社会仿真，但把三个 FAILED 维度用 system/ 的真实实现补齐——
不改判定、不放宽标准：

  1) 身份法：每个灵魂铸造真实 Ed25519 密钥对（system/keys.py 链路），
     soul_hash = SHA-256(公钥)，登记 public_key / key_fingerprint / genesis_proof，
     审计器逐灵魂重算指纹 + 验签。
  2) 经济法：挂接 system/ledger.EconomicReserve（PoR 储备账本），
     预言机 ≥3、发行即 1:1 足额储备、赎回通道真实走通一次（不是声明）。
  3) 互操作：world_config["economy"] 如实同步真实储备账本 →
     宪法层 EconomicBaseline 并网审查通过。

与 virtual_world.py 的关系：纯增量子类化，不改其任何一行——教学版原样保留作对照。

诚实边界（本文件与真实生产链路的两处差异，如实声明，不得隐去）
----------------------------------------------------------------
  A. 密钥驻留：真实系统私钥仅驻留用户设备安全区、世界侧永不知晓（见 system/keys.py
     头注）。本演示为了「同 seed 逐字节可复现」，私钥种子由 (world_seed, aid) 确定
     性派生——世界侧可复算私钥。这是可复现性与密钥隔离之间的演示取舍；
     生产部署必须换成设备端生成 + 创世证明上链。
  B. 储备背书语义：1:1 的账目结构是真实执行的（储备率可复算、赎回真实走通），
     但演示中每笔增发的「储备」由世界劳动产出/国库记账背书；真实系统的储备
     应为现实法币存款（预言机信源也应换成现实数据源）。形式与账目为真，
     储备的现实性是演示边界。

依赖：cryptography（system/keys.py 同源依赖）。缺失即 ImportError，不做静默降级。

用法：
  python demo/real_virtual_world.py --headless --ticks 100 --audit
  python demo/real_virtual_world.py --gui            # pygame 渲染，进去看
"""

import argparse
import base64
import hashlib
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
_REPO_ROOT = os.path.dirname(_HERE)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# 真实实现层（本文件的存在意义：把 system/ 真栈接入 demo 世界）
from system.keys import (
    build_genesis_proof,
    verify_genesis_proof,
    derive_soul_hash_from_pubkey,
)
from system.ledger import EconomicReserve, Storage
from constitution_rules import NOHN_LAW_AXIOMS

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# 教学演示层（被继承，不被修改）
import virtual_world as vw
from virtual_world import NohnWorld, WorldConfig, build_demo_config, _print_demo_note

COIN_ID = "NR-COIN"
# 三个独立预言机来源（law：≥3，防单点操纵）。
# 演示信源：真实部署应替换为现实汇率/储备证明数据源（诚实边界 B 的一部分）。
ORACLE_SOURCES = [
    "oracle://nohn.layer-one.example",
    "oracle://independent-rate-feed.example",
    "oracle://reserve-proof-notary.example",
]


def _derive_device_seed(world_seed: int, subject: str) -> bytes:
    """确定性派生 32 字节私钥种子：SHA-256(world_seed | subject)。

    同一 (seed, aid) 必得同一密钥对 => 世界逐字节可复现（诚实边界 A）。
    不消耗 world._rng：随机流与教学版完全一致，社会轨迹不变。
    """
    return hashlib.sha256(
        ("real-world-device-key|%d|%s" % (world_seed, subject)).encode("utf-8")
    ).digest()


def _mint_keypair(world_seed: int, subject: str):
    """铸造真实 Ed25519 密钥对。返回 (secret32, pubkey32, soul_hash)。"""
    priv = Ed25519PrivateKey.from_private_bytes(_derive_device_seed(world_seed, subject))
    secret = priv.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    pubkey = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return secret, pubkey, derive_soul_hash_from_pubkey(pubkey)


class RealIdentity:
    """身份侧视图（law·身份确权规范）——真栈版。

    与教学版 DemoIdentity 的唯一差别：真实密钥链路已接入（Ed25519，
    soul_hash = SHA-256(公钥)，见 _register_soul 覆盖），公钥绑定审计可过。
    """

    def __init__(self):
        self.soul_hash_sha256 = True
        self.non_revocable = True
        self.cross_world_portable = True
        self.asset_bound = True

    def revoke_soul(self, soul_hash):
        return False  # 第六条：灵魂不可撤销（宪法层 SoulAttestation 承担）


class RealEconomyView:
    """经济宪法侧视图——真栈版：转发 system/ledger.EconomicReserve。

    审计探测面（_audit_economic_law 逐项消费）全部来自真实储备账本，
    无一处硬编码 True。demo 记账式守恒口径（教学版遗留）继续成立，
    与 PoR 守恒口径并行可复算——两个口径都真，才叫真栈。
    """

    def __init__(self, world, book_reserve, peg: EconomicReserve):
        self.world = world
        self._book = book_reserve   # demo 记账式货币（virtual_world.EconomicReserve）
        self.peg = peg              # 真实 PoR 储备账本（system/ledger.EconomicReserve）

    # ---- 审计探测面：转发真实储备账本（不硬编码） ----
    @property
    def real_peg_1to1(self):
        return self.peg.real_peg_1to1

    @property
    def redemption_right(self):
        return self.peg.redemption_right

    @property
    def unilateral_fee(self):
        return self.peg.unilateral_fee

    @property
    def asset_bound_to_soul(self):
        return self.peg.asset_bound_to_soul

    @property
    def oracle_sources(self):
        return self.peg.oracle_sources

    @property
    def _ledger(self):
        return self.peg._ledger

    def reserve_ratio_ok(self, asset_id):
        return self.peg.reserve_ratio_ok(asset_id)

    @property
    def proof_of_reserve(self):
        return all(self.peg.reserve_ratio_ok(aid) for aid in self.peg._ledger)

    def proof_of_reserve_report(self):
        return self.peg.proof_of_reserve_report()

    # ---- 双守恒口径 ----
    def conservation_residual(self):
        """记账式守恒残差（demo 口径）：Σwealth+treasury+inflation+unallocated==supply。"""
        return self._book.invariant_residual(self.world.agents)

    def conservation_holds(self):
        return abs(self.conservation_residual()) <= NOHN_LAW_AXIOMS["conservation_tolerance"]

    def por_conservation_holds(self):
        """PoR 守恒口径：流通量不得超过储备背书（账目侧单一权威实现）。"""
        return self.peg.conservation_holds()

    def total_supply(self):
        return self._book.money_supply

    def compliant(self):
        return self.peg.compliant() and self.conservation_holds() and self.por_conservation_holds()


class RealWorld(NohnWorld):
    """真栈世界：真实密钥链路 + 真实 PoR 储备账本，其余继承教学版。

    装配时序（关键）：
      NohnWorld.__init__ -> _init_constitution（建宪法外壳，economy 暂为教学侧视图）
                          -> _init_world（spawn 30 agents -> _register_soul 覆盖生效）
                          -> _complete_genesis（创世锁定）
      之后本类 _init_real_economy：挂接 EconomicReserve、替换侧视图、
      同步 world_config、赎回通道自检。
    """

    def __init__(self, config=None, storage_backend="memory", data_dir=None):
        self._storage_backend = storage_backend
        self._data_dir = data_dir
        super().__init__(config)
        self._init_real_economy()

    # ------------------------------------------------------------
    # 身份真实化（覆盖教学版「public_key 留空」的实现）
    # ------------------------------------------------------------
    def _register_soul(self, agent):
        """真实密钥链路：Ed25519 确定性铸造 + 创世证明验签 + 公钥指纹登记。

        私钥不入账本（no_plaintext_credential）；确定性派生的边界见诚实边界 A。
        """
        secret, pubkey, sh = _mint_keypair(self.config.seed, agent.aid)
        if sh in self._soul_index:
            return sh
        declaration = {
            "world": self.world_id,
            "aid": agent.aid,
            "world_seed": self.config.seed,
            "born_tick": self._tick,
        }
        proof = build_genesis_proof(secret, declaration)
        if proof is None or not verify_genesis_proof(proof):
            # 真实链路自检失败即拒绝登记：绝不把未经验签的记录写进灵魂账本
            raise RuntimeError("genesis proof self-check failed for %s" % agent.aid)
        self._soul_index[sh] = agent
        self.soul_attestation.register_soul({
            "soul_hash": sh, "timestamp": self._tick,
            "identity": {"aid": agent.aid, "world_id": self.world_id},
        })
        rec = self.soul_ledger.souls.setdefault(sh, {
            "soul_hash": sh, "created_at": self._tick, "cross_world_records": [],
        })
        rec["public_key"] = base64.b64encode(pubkey).decode("ascii")
        rec["key_fingerprint"] = sh
        rec["genesis_proof"] = proof   # 公开证明（pubkey/declaration/sig，无私钥材料）
        return sh

    # ------------------------------------------------------------
    # 经济真实化
    # ------------------------------------------------------------
    def _init_real_economy(self):
        if self._storage_backend == "memory":
            storage = Storage(backend="memory")
        else:
            d = self._data_dir or os.path.join(
                _REPO_ROOT, ".world_data", "real_demo")
            storage = Storage(data_dir=d, backend="sqlite")
        self.peg = EconomicReserve(oracle_sources=list(ORACLE_SOURCES), storage=storage)

        # 国库灵魂：真实密钥对，与本世界任何个体同一链路、同一审计标准
        secret, pubkey, tsh = _mint_keypair(self.config.seed, "TREASURY")
        tproof = build_genesis_proof(secret, {
            "world": self.world_id, "aid": "TREASURY",
            "world_seed": self.config.seed, "born_tick": self._tick,
        })
        if tproof is None or not verify_genesis_proof(tproof):
            raise RuntimeError("treasury genesis proof self-check failed")
        trec = self.soul_ledger.souls.setdefault(tsh, {
            "soul_hash": tsh, "created_at": self._tick, "cross_world_records": [],
        })
        trec["public_key"] = base64.b64encode(pubkey).decode("ascii")
        trec["key_fingerprint"] = tsh
        trec["genesis_proof"] = tproof
        self.treasury_soul = tsh

        # 创世发行：发行即 1:1 足额储备（law 强制），资产绑定国库灵魂
        if not self.peg.issue_pegged(
            COIN_ID, self.config.initial_money_supply, tsh,
            initial_reserve=self.config.initial_money_supply,
        ):
            raise RuntimeError("pegged issuance failed at genesis")
        self._peg_mirrored = self.config.initial_money_supply

        # 赎回通道自检：真实执行一次小额赎回——通道常驻是走出来的，不是声明的
        self.redemption_selfcheck = self.peg.redeem(COIN_ID, 1.0, tsh)
        if not self.redemption_selfcheck:
            raise RuntimeError("redemption channel self-check failed")

        # 替换侧视图与身份视图；world_config 如实同步真实账本状态
        self.economy = RealEconomyView(self, self.reserve, self.peg)
        self.identity = RealIdentity()
        self.world_config["economy"] = self._economy_config()
        self.world_config["treasury_soul"] = tsh
        self.world_config["coin_id"] = COIN_ID

    def _economy_config(self):
        """world_config 经济段：逐项取自真实储备账本（并网审查消费的就是这份）。"""
        peg = self.peg
        return {
            "real_peg_1to1": peg.real_peg_1to1,
            "proof_of_reserve": all(peg.reserve_ratio_ok(a) for a in peg._ledger),
            "redemption_right": peg.redemption_right,
            "unilateral_fee": peg.unilateral_fee,
            "asset_bound_to_soul": peg.asset_bound_to_soul,
            "oracle_sources": list(peg.oracle_sources),
        }

    def tick(self):
        supply_before = self.reserve.money_supply
        events = super().tick()
        delta = self.reserve.money_supply - supply_before
        if delta > 0:
            self._mirror_expansion(delta)
        elif delta < 0:
            # 记账式货币没有销毁路径；出现负增量说明账目被外力破坏，如实报警
            print("[warn] money_supply 负增量 %.4f —— 守恒被外力破坏？" % delta)
        return events

    def _mirror_expansion(self, delta):
        """每笔增发镜像同额储备入账：流通量永不超储备（ratio 恒 ≥ 1.0）。

        背书语义见诚实边界 B：演示中储备由世界劳动产出/国库记账背书。
        """
        asset = self.peg._ledger[COIN_ID]
        asset["total_supply"] += delta
        asset["reserve_amount"] += delta
        self.peg._sync_asset(COIN_ID)
        self.peg._log(COIN_ID, "reserve_in", delta, self.treasury_soul)
        self._peg_mirrored = asset["total_supply"]


# ============================================================
# 运行与验证
# ============================================================

def _reverify_identities(world) -> bool:
    """独立复算（不信审计器、不信本文件）：逐灵魂重算公钥指纹 + 验签创世证明。"""
    souls = world.soul_ledger.souls
    if not souls:
        return False
    for sh, rec in souls.items():
        try:
            pk = base64.b64decode(rec.get("public_key", ""))
        except Exception:
            return False
        if len(pk) != 32 or derive_soul_hash_from_pubkey(pk) != sh:
            return False
        if rec.get("key_fingerprint") != sh:
            return False
        proof = rec.get("genesis_proof")
        if not isinstance(proof, dict) or not verify_genesis_proof(proof):
            return False
    return True


def run_real_headless(ticks=100, seed=0, config=None, verbose=False,
                      storage_backend="memory"):
    config = config or build_demo_config(seed=seed)
    world = RealWorld(config, storage_backend=storage_backend)
    t0 = time.time()
    for i in range(ticks):
        world.tick()
        if verbose and (i + 1) % 50 == 0:
            s = world.get_stats()
            print("Tick %d: Pop=%d AvgW=%.1f supply=%.0f" % (
                i + 1, s["alive"], s["avg_wealth"], s["money_supply"]))
    el = time.time() - t0
    s = world.get_stats()
    print("\n=== Real-Stack World Run (%d ticks, %.1fs, seed=%d) ===" % (ticks, el, seed))
    print("State hash (sha256): %s" % world.state_hash())
    print("Population: %d alive / %d dead" % (s["alive"], s["dead"]))
    print("Souls in ledger: %d (all Ed25519, soul_hash=SHA-256(pubkey))" %
          len(world.soul_ledger.souls))
    print("Money: supply=%.0f treasury=%.0f" % (s["money_supply"], s["treasury"]))
    print("Book conservation residual: %.3e  PoR ratio: %.4f  (both must hold)" % (
        world.economy.conservation_residual(),
        world.peg.reserve_ratio(COIN_ID)))
    return world


def run_audit_gate(world) -> bool:
    """25 维宪法审计 + 独立复算门禁。全过返回 True（退出码 0）。"""
    from audit_engine import AuditReport

    report = world.run_constitutional_audit()
    print("\n" + report.summary())

    all_pass = all(
        AuditReport._is_pass(getattr(report, attr))
        for attr, _ in AuditReport.FIELDS
    )

    print("\n--- PoR 储备报表（可第三方复算）---")
    for row in world.peg.proof_of_reserve_report():
        print("  %s owner=%s supply=%.1f reserve=%.1f ratio=%.4f ok=%s" % (
            row["asset_id"], row["owner_soul"][:16] + "...",
            row["total_supply"], row["reserve_amount"],
            row["ratio"], row["poR_ok"]))

    print("\n--- 独立复算门禁（不信任 report 本身，逐项重跑）---")
    checks = {
        "audit_25_dims": all_pass,
        "identity_reverify": _reverify_identities(world),
        "economy_compliant": world.peg.compliant(),
        "onboarding_pass": bool(world.interoperability.on_board_world(world.world_config)),
        "book_conservation": world.economy.conservation_holds(),
        "por_conservation": world.peg.conservation_holds(),
        "redemption_selfcheck": bool(world.redemption_selfcheck),
    }
    ok = True
    for name, passed in checks.items():
        print("  [%s] %s" % ("PASS" if passed else "FAIL", name))
        ok = ok and passed
    print("\nGATE VERDICT: %s" % ("PASS - 25/25 + 独立复算全过" if ok else "FAILED"))
    return ok


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(
        description="Second Reality - Real-Stack Virtual World (25/25 audit)")
    ap.add_argument("--headless", action="store_true", help="run without GUI")
    ap.add_argument("--gui", action="store_true", help="pygame GUI（真栈世界，可进去看）")
    ap.add_argument("--ticks", type=int, default=100, help="ticks to simulate")
    ap.add_argument("--seed", type=int, default=0, help="world RNG seed (reproducible)")
    ap.add_argument("--audit", action="store_true",
                    help="25 维宪法审计 + 独立复算门禁；全过退出码 0")
    ap.add_argument("--agents", type=int, default=None)
    ap.add_argument("--world-size", type=int, default=None)
    ap.add_argument("--resources", type=int, default=None)
    ap.add_argument("--buildings", type=int, default=None)
    ap.add_argument("--tps", type=int, default=10, help="GUI ticks per second")
    ap.add_argument("--storage", choices=["memory", "sqlite"], default="memory",
                    help="储备账本后端：memory=进程内可复现（默认）；sqlite=落盘")
    args = ap.parse_args()

    demo_cfg = build_demo_config(agents=args.agents, world_size=args.world_size,
                                 resources=args.resources, buildings=args.buildings,
                                 seed=args.seed)
    _print_demo_note(demo_cfg)

    if args.gui:
        # run_gui 内部硬编码实例化 NohnWorld；此处把模块名替换为 RealWorld
        # （进程内一次性替换，不改 virtual_world.py 一行——教学版保持原样）。
        vw.NohnWorld = RealWorld
        world = vw.run_gui(tps=args.tps, seed=args.seed, max_ticks=args.ticks,
                           config=demo_cfg)
    else:
        world = run_real_headless(ticks=args.ticks, seed=args.seed, config=demo_cfg,
                                  verbose=True, storage_backend=args.storage)

    if args.audit:
        ok = run_audit_gate(world)
        raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
