# -*- coding: utf-8 -*-
"""《红尘泪》世界层 —— 大央帝朝武道体系，接入 Second Reality 真栈世界
====================================================================

本模块把《红尘泪》的武道设定落成**可执行的机制**，而不是叙事文本。
它遵守宪法第负一章的 M1 入章纪律：每条机制都必须有

    statement  陈述（人话）
    producer   世界内部的执行点（本模块的方法）
    consumer   消费并判定它的检查（tools/hongchenlei_smoke.py）
    recompute  第三方可独立重跑的算式（只用账本与公开输入）

**核心是守恒**：真气不是"蓝条"，是可计量、可转移、可剥夺的量。

守恒式（任意 tick 均可校验，residual 应为 0）：

    Σ(所有 agent 的真气) + dissipated（离体消散） + unallocated（天地未分配）
        == genesis_qi（创世真气总量）

三条操作各自对应守恒的一段：
  - 修炼 / 入门分配：unallocated → agent（划转，不新增）
  - 施放消耗：agent → dissipated（离体消散，不消失）
  - 《千秋内经》吸收：agent → agent（**转移**，总量不变）

**边界条款（硬，与 M1 并列强制）**
  1. 境界只升不降（时间不可逆的武道投影）；唯一例外是「丹田被废」——
     那不是降级，是**剥夺**：真气尽数归入 dissipated，境界归零。
  2. 《千秋内经》吸收**设有上限**（默认单次不超过对方真气的 30%）：
     无上限的吸收等于允许一人独吞全世界的真气，守恒虽成立、公平已死
     ——这与天道框架「不可变的公平形式」直接冲突。
  3. 榜单**排除朝臣、皇族与退隐者**（原稿规则）：庙堂与江湖是两套评价体系，
     混榜会让权力直接兑换成江湖排名，那是中心化的另一种形态。
  4. 仙绝之后即飞升——飞升意味着实体离开本世界。**本模块不实现飞升**：
     离开世界的实体如何处理其因果链，属未决问题（见世界观文档第十节缺口 1），
     实现它等于擅自定义一个未定的世界边界。

用户裁决（2026-10-22）：凌化尘按**极道**算——已成就领域、一呼一吸分海摧城，
剑圣级。境界判据以此为准，不再采用"真气量仍一品下位"的模糊表述。

依赖：仅标准库 + constitution_rules（单一权威常量源）。
"""

import hashlib
import random
from typing import Dict, List, Optional, Sequence, Tuple

from constitution_rules import NOHN_LAW_AXIOMS

# ============================================================
# 一、境界阶梯（低 → 高）
# ============================================================

# 九品至七品为武者，六品至四品为武师，三品至一品为宗师；
# 一品之上另有化境、极道、仙绝三境。
WUDAO_LADDER: Tuple[str, ...] = (
    "九品", "八品", "七品",                          # 武者
    "六品", "五品", "四品",                          # 武师
    "三品", "二品",                                  # 准宗师 / 小宗师
    "一品·下位", "一品·中位", "一品·上位（金身）",    # 大宗师
    "化境", "极道", "仙绝",
)
RANK_INDEX: Dict[str, int] = {name: i for i, name in enumerate(WUDAO_LADDER)}
TOP_RANK = len(WUDAO_LADDER) - 1
RANK_JIDAO = RANK_INDEX["极道"]        # 用户裁决：凌化尘所处境界
RANK_XIANJUE = RANK_INDEX["仙绝"]

# 江湖俗谓（供展示，不参与判定）
RANK_ALIAS = {
    "三品": "准宗师", "二品": "小宗师",
    "一品·下位": "大宗师", "一品·中位": "大宗师", "一品·上位（金身）": "大宗师",
    "化境": "剑仙", "极道": "剑圣", "仙绝": "陆地神仙",
}

# 各阶真气门槛：晋升的确定性判据（达到即晋升，无随机）
# 数值取自"一品上位可活百五十年、化境延寿五十"的层级差距所隐含的量级，
# 仅作演示标度；真实标定权归公投（天道：阈值不随形式一并冻结）。
QI_THRESHOLD: Tuple[float, ...] = (
    0.0, 10.0, 25.0,          # 九品 / 八品 / 七品
    60.0, 140.0, 320.0,       # 六品 / 五品 / 四品
    700.0, 1500.0,            # 三品 / 二品
    3000.0, 6000.0, 12000.0,  # 一品下位 / 中位 / 上位（金身）
    25000.0, 50000.0,         # 化境 / 极道
    100000.0,                 # 仙绝
)

# ============================================================
# 二、势力与身份（榜单排除规则的依据）
# ============================================================

FACTIONS: Tuple[str, ...] = ("林家", "江家", "萧家", "杀殿", "道门", "无门无派")

# 朝臣与皇族：不入江湖三榜（边界条款 3）
COURT_ROLES = ("皇族", "朝臣")


# ============================================================
# 三、真气守恒账本（轮回框架在武道侧的账目实现）
# ============================================================

class QiLedger:
    """真气守恒账本。

    守恒式：Σ agent.qi + dissipated + unallocated == genesis_qi
    容差取自 NOHN_LAW_AXIOMS['conservation_tolerance']（M2 单一权威源，不重复硬编码）。

    可复算性：第三方只需读 (各 agent 的真气, dissipated, unallocated, genesis_qi)
    即可重跑残差算式，无需信任本进程。
    """

    def __init__(self, genesis_qi: float = 100000.0):
        self.genesis_qi = float(genesis_qi)
        self.unallocated = float(genesis_qi)   # 天地灵气池中尚未分配的部分
        self.dissipated = 0.0                  # 离体消散（施放、丹田被废）
        self.total_absorbed = 0.0              # 千秋内经累计转移量（记账，不进守恒式）
        self.total_cultivated = 0.0

    # ---- 划转：天地灵气池 → 人 ----
    def allocate(self, agent, amount: float) -> float:
        """入门分配 / 修炼所得：从灵气池划入个体。总量不变。"""
        amount = min(max(0.0, float(amount)), self.unallocated)
        if amount <= 0:
            return 0.0
        agent.qi = getattr(agent, "qi", 0.0) + amount
        self.unallocated -= amount
        self.total_cultivated += amount
        return amount

    # ---- 消耗：人 → 消散 ----
    def consume(self, agent, amount: float) -> float:
        """施放武功：真气离体消散（进入 dissipated，不凭空消失）。"""
        have = getattr(agent, "qi", 0.0)
        amount = min(max(0.0, float(amount)), have)
        if amount <= 0:
            return 0.0
        agent.qi = have - amount
        self.dissipated += amount
        return amount

    # ---- 转移：人 → 人（千秋内经） ----
    def absorb(self, src, dst, ratio: float = 0.3) -> float:
        """《千秋内经》：吸收任何内力化为己用。

        这是**转移算子**，不是创造算子：src 减多少，dst 加多少，总量不变。
        边界条款 2：单次吸收不超过对方当前真气的 ratio（默认 30%）。
        """
        src_qi = getattr(src, "qi", 0.0)
        amount = min(max(0.0, src_qi * ratio), src_qi)
        if amount <= 0:
            return 0.0
        src.qi = src_qi - amount
        dst.qi = getattr(dst, "qi", 0.0) + amount
        self.total_absorbed += amount
        return amount

    # ---- 剥夺：丹田被废 ----
    def shatter(self, agent) -> float:
        """震碎丹田：真气尽数归入 dissipated，境界归零。

        这不是"降级"（时间不可逆，境界只升不降），而是**剥夺**——
        被废者的真气从个体账户转入消散，守恒式依然闭合。
        """
        lost = getattr(agent, "qi", 0.0)
        agent.qi = 0.0
        agent.rank_idx = 0
        agent.dantian_intact = False
        self.dissipated += lost
        return lost

    # ---- 守恒判定 ----
    def residual(self, agents: Sequence) -> float:
        total = sum(float(getattr(a, "qi", 0.0)) for a in agents)
        return total + self.dissipated + self.unallocated - self.genesis_qi

    def holds(self, agents: Sequence) -> bool:
        return abs(self.residual(agents)) <= float(
            NOHN_LAW_AXIOMS["conservation_tolerance"]
        )


# ============================================================
# 四、境界晋升（确定性状态机）
# ============================================================

def rank_name(idx: int) -> str:
    return WUDAO_LADDER[max(0, min(int(idx), TOP_RANK))]


def try_promote(agent) -> int:
    """按真气门槛晋升，可连升。返回晋升级数。

    单调不可逆（边界条款 1）：只升不降。丹田已废者不得晋升。
    """
    if not getattr(agent, "dantian_intact", True):
        return 0
    promoted = 0
    idx = int(getattr(agent, "rank_idx", 0))
    qi = float(getattr(agent, "qi", 0.0))
    while idx < TOP_RANK and qi >= QI_THRESHOLD[idx + 1]:
        idx += 1
        promoted += 1
    if promoted:
        agent.rank_idx = idx
    return promoted


# ============================================================
# 五、江湖三榜（百晓生）
# ============================================================

def _board_key(a) -> Tuple[int, float]:
    """排名键：先比境界阶位，再比真气量（全确定性，无随机）。"""
    return (int(getattr(a, "rank_idx", 0)), float(getattr(a, "qi", 0.0)))


def eligible_for_board(agent) -> bool:
    """边界条款 3：朝臣、皇族与退隐者不入江湖三榜。"""
    if getattr(agent, "retired", False):
        return False
    if getattr(agent, "role", None) in COURT_ROLES:
        return False
    return True


def build_board(agents: Sequence, limit: int = 10, max_age: Optional[float] = None):
    """榜单：天耀榜（老辈）/ 地星榜（五十岁以下）/ 少君榜（二十岁以下）。

    max_age 为 None 时不限年龄（即天耀榜口径）。
    """
    pool = [a for a in agents if eligible_for_board(a)]
    if max_age is not None:
        pool = [a for a in pool if float(getattr(a, "age", 0)) <= float(max_age)]
    pool.sort(key=_board_key, reverse=True)
    return pool[:limit]


# ============================================================
# 六、世界装配：把武道体系挂到既有智能体上
# ============================================================

class HongchenleiRealm:
    """《红尘泪》世界层：给既有智能体挂上境界 / 真气 / 势力，并逐 tick 推进。

    producer（世界内执行点）：__init__ 分配、tick 推进、cultivate / absorb / shatter
    consumer（判定方）      ：tools/hongchenlei_smoke.py
    recompute（第三方重跑） ：QiLedger.residual(agents)

    随机流纪律：本层使用**独立 rng**（由 world_seed 派生），绝不消耗
    world._rng —— 否则既有社会轨迹的随机流会整体错位，state_hash 漂移。
    """

    #: 每 tick 的修炼产出（占灵气池余量之比，确定性）
    CULTIVATE_RATE = 0.002
    #: 每 tick 的施放消耗（占自身真气之比）
    CONSUME_RATE = 0.001

    def __init__(self, world, genesis_qi: float = 100000.0,
                 seed_salt: str = "hongchenlei"):
        self.world = world
        self.ledger = QiLedger(genesis_qi)
        self.rng = random.Random(
            int(hashlib.sha256(
                ("%s|%d" % (seed_salt, getattr(world.config, "seed", 0))).encode("utf-8")
            ).hexdigest()[:16], 16)
        )
        self.promotions = 0
        self.shattered = 0
        self._assign_all()

    # ---- 创世分配：确定性 ----
    def _assign_all(self) -> None:
        agents = list(self.world.agents)
        for i, a in enumerate(agents):
            a.qi = 0.0
            a.rank_idx = 0
            a.dantian_intact = True
            a.faction = FACTIONS[i % len(FACTIONS)]
            # 势力与身份：林家/江家为朝堂侧，杀殿/道门为江湖侧
            a.role = "朝臣" if a.faction in ("林家", "江家", "萧家") else "江湖"
            a.retired = False
        # 入门分配：按确定性权重分配灵气池的一部分（不超过池子）
        weights = [0.5 + self.rng.random() for _ in agents]
        total_w = sum(weights) or 1.0
        budget = self.ledger.genesis_qi * 0.3      # 创世时仅分配三成，余留天地
        for a, w in zip(agents, weights):
            self.ledger.allocate(a, budget * (w / total_w))
            try_promote(a)

    # ---- 逐 tick 推进 ----
    def tick(self) -> Dict:
        """推进一 tick：修炼 → 消耗 → 晋升。全确定性。"""
        agents = [a for a in self.world.agents if getattr(a, "alive", True)]
        promoted = 0
        for a in agents:
            if not getattr(a, "dantian_intact", True):
                continue
            self.ledger.allocate(a, self.ledger.unallocated * self.CULTIVATE_RATE)
            self.ledger.consume(a, getattr(a, "qi", 0.0) * self.CONSUME_RATE)
            promoted += try_promote(a)
        self.promotions += promoted
        return {
            "promoted": promoted,
            "unallocated": round(self.ledger.unallocated, 4),
            "dissipated": round(self.ledger.dissipated, 4),
            "residual": self.ledger.residual(list(self.world.agents)),
        }

    # ---- 公开操作（供世界与测试调用） ----
    def cultivate(self, agent, amount: float) -> float:
        got = self.ledger.allocate(agent, amount)
        try_promote(agent)
        return got

    def cast(self, agent, amount: float) -> float:
        return self.ledger.consume(agent, amount)

    def qianqiu_absorb(self, src, dst, ratio: float = 0.3) -> float:
        got = self.ledger.absorb(src, dst, ratio)
        try_promote(dst)
        return got

    def shatter_dantian(self, agent) -> float:
        self.shattered += 1
        return self.ledger.shatter(agent)

    # ---- 榜单 ----
    def boards(self) -> Dict[str, List]:
        agents = list(self.world.agents)
        return {
            "天耀榜": build_board(agents, limit=10),
            "地星榜": build_board(agents, limit=20, max_age=50),
            "少君榜": build_board(agents, limit=50, max_age=20),
        }

    def summary(self) -> Dict:
        agents = list(self.world.agents)
        dist: Dict[str, int] = {}
        for a in agents:
            n = rank_name(getattr(a, "rank_idx", 0))
            dist[n] = dist.get(n, 0) + 1
        return {
            "agents": len(agents),
            "genesis_qi": self.ledger.genesis_qi,
            "unallocated": round(self.ledger.unallocated, 4),
            "dissipated": round(self.ledger.dissipated, 4),
            "absorbed_total": round(self.ledger.total_absorbed, 4),
            "conservation_residual": self.ledger.residual(agents),
            "conservation_holds": self.ledger.holds(agents),
            "promotions": self.promotions,
            "shattered": self.shattered,
            "rank_distribution": dict(sorted(dist.items(), key=lambda kv: -kv[1])),
        }


__all__ = [
    "WUDAO_LADDER", "RANK_INDEX", "RANK_JIDAO", "RANK_XIANJUE", "QI_THRESHOLD",
    "FACTIONS", "COURT_ROLES", "QiLedger", "HongchenleiRealm",
    "rank_name", "try_promote", "build_board", "eligible_for_board",
]
