# -*- coding: utf-8 -*-
"""
persona —— second-reality 的拟人心理层（人格层）
================================================================================
来源：Anthropomorphic-Agent-Engine 的 SPL Pure Core V8.0
      spl_engine.py 为原文逐字节复制件，MD5 f2fa611d355959e8d72a1f44c40963f0
      （与仓库根 SPL-anthropic-engine.py 同一份；ENGINE_VERSION = "SPL-V8.0"）
      feature/ 为同源六个可组合模块（identity / goal / bias / value / world /
      language_style）。

三条不可越的边界（违反任一条都会推翻既有设计目标）
--------------------------------------------------------------------------------
1. 本层只提供「人格与内感受」，不实现宪法契约。
   MemoryVault(708) / IndependentWill(782) / MemoryInalienability(903) /
   MemoryGuardian(924) 仍由 system/agent_engine.py 实现，审计依赖它们。
   本层若被用来替换那些类，宪法审计直接 FAILED。

2. 本层不消耗 world._rng。
   世界随机源必须保持全局唯一，否则同 seed 世界不再逐字节可复现，
   HLD §1.2「同 seed + 同 config → 同世界」与 state_hash 资产锚定同时失效。
   本层需要的一切"个体差异"均来自 (world_seed, aid, 五维特质) 的确定性派生，
   不使用任何 random 调用。

3. 本层不读墙上时钟。
   引擎的 last_time 默认取 time.time()，是确定性漏点；本层一律用
   set_clock() 接管，时间由 tick 序号线性映射，tick → 秒的换算见
   SECONDS_PER_TICK。世界不同跑两遍，时钟序列完全相同。

观测口径：人格层状态不写进 NohnAgent.to_dict()，因此不改变 world.state_hash()。
人格层自身状态经 World.persona_report() 单独读出，与审计口径不混称。
================================================================================
"""

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .spl_engine import SPLPureCoreV7_3, NarrativeMapper
from .feature import (
    BiasProfile, GoalEngine, GoalNode, IdentityEngine, IdentityNode,
    LanguagePersona, StyleProfile, ValueProfile, WorldModel,
)

__all__ = [
    "SPLPureCoreV7_3",
    "NarrativeMapper",
    "PersonaLayer",
    "PersonaStack",
    "AffectMapper",
    "build_persona",
    "build_persona_stack",
    "PERSONA_MODES",
    "SECONDS_PER_TICK",
    "VALUE_KEYS",
    "BIAS_KEYS",
    "WORLD_KEYS",
    "LANGUAGE_MODES",
]

# ---------------------------------------------------------------------------
# 时间映射：1 world tick = 60 引擎秒（= 1 模拟分钟）
# 理由：mood（心境）是小时-天级慢变量、fluid 是秒-分钟级快变量，
#       1 秒/tick 会让 1000 tick 只推进 17 分钟，心境几乎不动；
#       60 秒/tick 时 1000 tick = 16.7 模拟小时，快慢两层都可见。
# 该常量只影响情绪演化速率，不影响世界状态哈希（人格层不入 to_dict）。
# ---------------------------------------------------------------------------
SECONDS_PER_TICK = 60.0

PERSONA_MODES = ("off", "affect")

_FLUID_KEYS = ("喜悦", "愤怒", "恐惧", "信任", "疏离", "张力", "愧疚", "羞耻")


def _stable_unit(*parts: Any) -> float:
    """由给定成分派生 [0,1) 的确定性小数。

    用 sha256 而非 random：跨进程、跨运行、跨 Python 小版本都稳定；
    random.Random(str) 依赖解释器对字符串种子的实现细节，不适合做取证基准。
    """
    raw = "nohn-persona|" + "|".join(str(p) for p in parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) / 0x100000000


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else (hi if x > hi else x)


# ---------------------------------------------------------------------------
# 感知 → 内感受向量
# ---------------------------------------------------------------------------
class AffectMapper:
    """把 world 侧的状态与视野，翻译成引擎能消化的内感受向量。

    对应引擎设计里的「叙事映射器」位置：同一份引擎，换一个 Mapper
    就换一套"什么算威胁、什么算归属"的世界观口径。
    本 Mapper 的口径：需求满足度是主信号，社交密度是归属修正项。
    """

    @staticmethod
    def to_vector(agent: Any, perceived: Optional[List[Dict]] = None):
        perceived = perceived or []
        needs = getattr(agent, "needs", {}) or {}

        # 需求满足度 [0,100] → 有符号信号 [-1,1]
        safety = _clamp(needs.get("safety", 50.0) / 100.0)
        belonging_need = _clamp(needs.get("belonging", 50.0) / 100.0)
        esteem_need = _clamp(needs.get("esteem", 50.0) / 100.0)
        actualization = _clamp(needs.get("self_actualization", 50.0) / 100.0)

        # 视野内可见智能体数量：社交密度对归属的作用（饱和在 5 人）
        visible_agents = sum(1 for p in perceived if p.get("type") == "agent")
        density = min(1.0, visible_agents / 5.0)

        # 威胁：安全缺口为主，叠加违规计数的声誉压力
        threat = -(safety * 2.0 - 1.0) + min(0.4, 0.1 * float(getattr(agent, "violations", 0)))
        # 归属：需求满足 + 社交密度；朋友多寡不再叠加，避免与 friends 双计
        belonging = (belonging_need * 2.0 - 1.0) + 0.3 * density
        # 自主：自我实现需求 + 财富带来的行动自由（饱和在 500）
        wealth = float(getattr(agent, "wealth", 0.0))
        autonomy = (actualization * 2.0 - 1.0) * 0.7 + min(1.0, max(0.0, wealth / 500.0)) * 0.6
        # 疲劳：能量亏空 [0,1]
        fatigue = 1.0 - _clamp(float(getattr(agent, "energy", 100.0)) / 100.0)
        # 羞耻触发：尊重需求缺口 + 违规
        shame_trigger = (1.0 - esteem_need) * 0.6 + min(0.4, 0.1 * float(getattr(agent, "violations", 0)))

        vector = {
            "threat": round(threat, 6),
            "belonging": round(belonging, 6),
            "autonomy": round(autonomy, 6),
            "fatigue": round(fatigue, 6),
            "shame_trigger": round(shame_trigger, 6),
        }
        norm = sum(v * v for v in vector.values()) ** 0.5
        intensity = round(min(1.0, norm / 2.0), 6)
        return vector, intensity


# ---------------------------------------------------------------------------
# 人格层主体
# ---------------------------------------------------------------------------
@dataclass
class PersonaLayer:
    """单个智能体的拟人心理层实例（引擎 + 确定性时钟 + 观测缓存）。"""

    aid: str
    world_seed: int = 0
    core: SPLPureCoreV7_3 = field(default=None, repr=False)
    tick_count: int = 0
    affect: Dict[str, float] = field(default_factory=dict)
    last_vector: Dict[str, float] = field(default_factory=dict)

    def __post_init__(self):
        if self.core is None:
            raise ValueError("PersonaLayer requires an SPLPureCoreV7_3 instance")

    # -- 推进 ---------------------------------------------------------------
    def step(self, agent: Any, perceived: Optional[List[Dict]] = None) -> Dict[str, float]:
        """推进一个 tick 的情绪演化，并把观测缓存回写自身。

        确定性：时钟由 tick_count 线性决定；向量由 agent 状态决定；
        引擎自身声明「无 LLM，无随机，可完全重放」。
        """
        self.tick_count += 1
        self.core.set_clock(self.tick_count * SECONDS_PER_TICK)
        vector, intensity = AffectMapper.to_vector(agent, perceived)
        self.last_vector = vector
        self.core.process_vector(
            vector, raw_intensity=intensity, event_id="tick%d" % self.tick_count
        )
        self.affect = self.observe()
        return self.affect

    # -- 观测 ---------------------------------------------------------------
    def observe(self) -> Dict[str, float]:
        """人格层可对外读出的标量视图（全部 float，便于 JSON 与哈希比对）。"""
        fluid = self.core.fluid
        mood = getattr(self.core, "mood", {}) or {}
        out: Dict[str, float] = {}
        for k in _FLUID_KEYS:
            out["fluid." + k] = round(float(fluid.get(k, 0.0)), 6)
        for k in sorted(mood.keys()):
            out["mood." + k] = round(float(mood.get(k, 0.0)), 6)
        out["self_esteem"] = round(float(getattr(self.core, "self_esteem", 0.0)), 6)
        out["excitation"] = round(float(getattr(self.core, "excitation", 0.0)), 6)
        out["latent_pressure"] = round(float(getattr(self.core, "latent_pressure", 0.0)), 6)
        out["cognitive_dissonance"] = round(float(getattr(self.core, "cognitive_dissonance", 0.0)), 6)
        out["memory_traces"] = float(len(self.core.memory_traces))
        return out

    def snapshot(self) -> Dict[str, Any]:
        """引擎原生快照（含 fluid/mood/自尊/能量/创伤等完整键集），供深度排查。"""
        return self.core.snapshot()


def build_persona(
    aid: str,
    world_seed: int,
    traits: Optional[Dict[str, float]] = None,
    audit_enabled: bool = False,
) -> PersonaLayer:
    """由 (world_seed, aid, 五维特质) 确定性地构造一个人格层实例。

    五维特质来自 world 侧既有的 Big Five 抽样（make_personality），
    因此人格层的个体差异与世界 RNG 流一一对应，且不额外消耗随机数。

    audit_enabled 默认 False：引擎的 AuditLogger 会写本地 JSONL 文件，
    30 个智能体 × 每 tick 一条会造成无意义的磁盘写入；需要取证时显式开启。
    """
    t = traits or {}
    openness = _clamp(float(t.get("openness", 0.5)))
    conscientiousness = _clamp(float(t.get("conscientiousness", 0.5)))
    extraversion = _clamp(float(t.get("extraversion", 0.5)))
    agreeableness = _clamp(float(t.get("agreeableness", 0.5)))
    neuroticism = _clamp(float(t.get("neuroticism", 0.5)))

    jitter = _stable_unit(world_seed, aid, "baseline")

    # 特质 → 引擎参数（映射规则写死，可审计，不做拟合）
    resilience = _clamp(0.5 * conscientiousness + 0.5 * (1.0 - neuroticism))
    affinity = agreeableness
    self_esteem = _clamp(0.3 + 0.4 * extraversion + 0.1 * (jitter - 0.5))

    baseline = {
        "喜悦": round(0.05 + 0.30 * extraversion, 6),
        "愤怒": round(0.05 * neuroticism, 6),
        "恐惧": round(0.05 + 0.35 * neuroticism, 6),
        "信任": round(0.30 + 0.45 * agreeableness, 6),
        "疏离": round(0.10 + 0.35 * (1.0 - agreeableness), 6),
        "张力": round(0.10 + 0.30 * neuroticism + 0.05 * jitter, 6),
        "愧疚": 0.0,
        "羞耻": round(0.05 * neuroticism, 6),
    }

    core = SPLPureCoreV7_3(
        psychological_resilience=resilience,
        affinity=affinity,
        self_esteem=self_esteem,
        excitation=round(0.20 + 0.30 * extraversion + 0.05 * jitter, 6),
        audit_enabled=audit_enabled,
    )
    # 情绪流体三层对齐同一基线：起点即"这个人的底色"，而非全场统一的 0
    core.fluid = dict(baseline)
    core.fluid_baseline = dict(baseline)
    core.fluid_target = dict(baseline)
    core.set_clock(0.0)   # 接管时钟，杜绝 time.time() 泄漏进演化
    # last_time 默认由 time.time() 初始化（构造期），会与虚拟时钟产生巨大负 dt
    # （引擎对 dt<=0 的处理是"跳过本次演化"），故显式归零，使第 1 tick 即开始演化。
    core.last_time = 0.0

    layer = PersonaLayer(aid=aid, world_seed=world_seed, core=core)
    layer.affect = layer.observe()
    return layer


# ===========================================================================
# 智能体栈：persona/feature 六模块的确定性实例化
# ---------------------------------------------------------------------------
# 对应 Anthropomorphic-Agent-Engine 的引擎设计哲学——「核心不动，角色全在外部自设」：
#   ① 叙事映射器（AffectMapper）② feature/ 六模块 ③ 核心参数（build_persona）
#   ④ Agent 封装
# 本函数的每个字段取值 = 特质相关项 + 独立抖动项，两项都来自确定性哈希流，
# 因此 1000 个个体的差异真实存在，且换一台机器重跑逐字节相同。
# ===========================================================================

VALUE_KEYS = ("freedom", "responsibility", "dignity", "intimacy", "security",
              "justice", "loyalty", "honesty", "competence", "growth",
              "creativity", "legacy")
BIAS_KEYS = ("negativity_bias", "threat_vigilance", "positivity_offset",
             "confirmation_bias", "hostile_attribution", "benevolent_attribution",
             "mood_congruent_memory", "peak_end_rule", "rosy_retrospection",
             "self_serving_bias", "fundamental_attribution", "in_group_bias",
             "authority_bias", "projection_bias")
WORLD_KEYS = ("trust_people", "relationship_stability", "effort_pays_off",
              "authority_reliability", "conflict_danger", "ambiguity_intolerance",
              "locus_of_control", "openness_to_experience")
LANGUAGE_MODES = ("direct", "restrained", "confrontational", "evasive", "intimate")

# 身份取自"最强技能"而非凭空命名：身份必须与个体在世界里的实际能力一致，
# 否则身份引擎算出的冲突与自尊变化会与现实脱钩。
SKILL_IDENTITY = {
    "gathering": ("采集者", "以采集供养群体"),
    "crafting": ("工匠", "以手艺换取报酬"),
    "social": ("社交者", "以关系维系共识"),
    "trading": ("商人", "以交易流通物资"),
    "learning": ("学者", "以学习积累知识"),
    "creating": ("创作者", "以创造产生新物"),
}
# 主导需求 → 生存级目标（仅当缺口压过个人偏好时使用）
NEED_GOAL = {
    "physiology": "活下去",
    "safety": "求安稳",
    "belonging": "被接纳",
    "esteem": "被尊重",
    "self_actualization": "成为想成为的人",
}
# 主导价值观 → 长期目标。首版只按需求缺口派生，结果是 1000 人目标全同
# （初始需求一致 ⇒ 缺口最大项恒为 self_actualization），由 tools/seed_agents.py
# 的唯一性检查抓出。改为：极端缺口（≥60）压过个人偏好，否则按本人最强价值观定目标。
VALUE_GOAL = {
    "dignity": "证明自己",
    "competence": "把手上的事做到最好",
    "creativity": "成为想成为的人",
    "growth": "走得更远",
    "intimacy": "守护身边的人",
    "loyalty": "不负所托",
    "justice": "让规则对所有人一样",
    "security": "求安稳",
    "freedom": "不被安排",
    "honesty": "活得坦荡",
    "legacy": "留下点东西",
    "responsibility": "撑住这个摊子",
}

# 字段派生表：field -> (基础值, 特质权重, 特质名, 抖动权重)
_VALUE_SPEC = {
    "freedom": (0.30, 0.30, "openness", 0.30),
    "responsibility": (0.30, 0.40, "conscientiousness", 0.30),
    "dignity": (0.40, 0.30, "inv_neuroticism", 0.30),
    "intimacy": (0.30, 0.40, "agreeableness", 0.30),
    "security": (0.30, 0.30, "neuroticism", 0.30),
    "justice": (0.30, 0.40, "agreeableness", 0.30),
    "loyalty": (0.30, 0.40, "conscientiousness", 0.30),
    "honesty": (0.40, 0.30, "agreeableness", 0.30),
    "competence": (0.30, 0.40, "conscientiousness", 0.30),
    "growth": (0.30, 0.40, "openness", 0.30),
    "creativity": (0.30, 0.35, "openness", 0.35),
    "legacy": (0.20, 0.30, "conscientiousness", 0.50),
}
_BIAS_SPEC = {
    "negativity_bias": (0.45, 0.45, "neuroticism", 0.10),
    "threat_vigilance": (0.30, 0.50, "neuroticism", 0.20),
    "positivity_offset": (0.45, 0.30, "extraversion", 0.20),
    "confirmation_bias": (0.45, 0.30, "inv_openness", 0.25),
    "hostile_attribution": (0.15, 0.45, "inv_agreeableness", 0.30),
    "benevolent_attribution": (0.25, 0.50, "agreeableness", 0.25),
    "mood_congruent_memory": (0.40, 0.30, "neuroticism", 0.30),
    "peak_end_rule": (0.40, 0.30, "neuroticism", 0.30),
    "rosy_retrospection": (0.20, 0.35, "inv_neuroticism", 0.35),
    "self_serving_bias": (0.35, 0.30, "neuroticism", 0.35),
    "fundamental_attribution": (0.40, 0.30, "neuroticism", 0.30),
    "in_group_bias": (0.25, 0.30, "inv_openness", 0.35),
    "authority_bias": (0.20, 0.40, "conscientiousness", 0.40),
    "projection_bias": (0.30, 0.30, "inv_openness", 0.30),
}
_WORLD_SPEC = {
    "trust_people": (0.35, 0.50, "agreeableness", 0.15),
    "relationship_stability": (0.35, 0.40, "conscientiousness", 0.20),
    "effort_pays_off": (0.45, 0.40, "inv_neuroticism", 0.15),
    "authority_reliability": (0.30, 0.40, "conscientiousness", 0.30),
    "conflict_danger": (0.35, 0.50, "neuroticism", 0.15),
    "ambiguity_intolerance": (0.25, 0.40, "inv_openness", 0.35),
    "locus_of_control": (0.35, 0.40, "conscientiousness", 0.30),
    "openness_to_experience": (0.30, 0.55, "openness", 0.15),
}


def _trait_values(traits: Dict[str, float]) -> Dict[str, float]:
    t = traits or {}
    o = _clamp(float(t.get("openness", 0.5)))
    c = _clamp(float(t.get("conscientiousness", 0.5)))
    e = _clamp(float(t.get("extraversion", 0.5)))
    a = _clamp(float(t.get("agreeableness", 0.5)))
    n = _clamp(float(t.get("neuroticism", 0.5)))
    return {"openness": o, "conscientiousness": c, "extraversion": e,
            "agreeableness": a, "neuroticism": n,
            "inv_openness": 1.0 - o, "inv_agreeableness": 1.0 - a,
            "inv_neuroticism": 1.0 - n}


def _derive(spec: Dict[str, tuple], tv: Dict[str, float],
            world_seed: int, aid: str) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for field_name, (base, weight, trait_name, jitter_w) in spec.items():
        j = _stable_unit(world_seed, aid, field_name)
        out[field_name] = round(_clamp(base + weight * tv[trait_name] + jitter_w * j), 6)
    return out


def _pick_language_mode(tv: Dict[str, float], j: float) -> str:
    """五档表达模式，由外向性/宜人性/神经质分档选定，j 只用于档内边界抖动。"""
    e, a, n = tv["extraversion"], tv["agreeableness"], tv["neuroticism"]
    score = e * 0.45 + a * 0.35 - n * 0.35 + (j - 0.5) * 0.20
    if score >= 0.55:
        return "intimate" if a >= 0.5 else "direct"
    if score >= 0.35:
        return "restrained" if a >= 0.5 else "direct"
    if score >= 0.18:
        return "evasive" if a < 0.5 else "restrained"
    return "confrontational" if a < 0.5 else "evasive"


@dataclass
class PersonaStack:
    """一个智能体的完整外部人格层：六模块实例 + 可读标量视图。"""

    aid: str
    identity_role: str = ""
    goal_title: str = ""
    language_mode: str = "direct"
    values: Dict[str, float] = field(default_factory=dict)
    biases: Dict[str, float] = field(default_factory=dict)
    world_beliefs: Dict[str, float] = field(default_factory=dict)
    identity: Optional[IdentityEngine] = field(default=None, repr=False, compare=False)
    goals: Optional[GoalEngine] = field(default=None, repr=False, compare=False)
    language: Optional[LanguagePersona] = field(default=None, repr=False, compare=False)
    style: Optional[StyleProfile] = field(default=None, repr=False, compare=False)

    def observe(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for k, v in self.values.items():
            out["value." + k] = v
        for k, v in self.biases.items():
            out["bias." + k] = v
        for k, v in self.world_beliefs.items():
            out["world." + k] = v
        mode_idx = LANGUAGE_MODES.index(self.language_mode) if self.language_mode in LANGUAGE_MODES else -1
        out["language.mode_index"] = float(mode_idx)
        return out


def build_persona_stack(
    aid: str,
    world_seed: int,
    traits: Optional[Dict[str, float]] = None,
    skills: Optional[Dict[str, float]] = None,
    needs: Optional[Dict[str, float]] = None,
) -> PersonaStack:
    """由 (world_seed, aid, 五维特质, 技能, 需求) 确定性构造智能体栈。

    skills 决定身份（最强技能），needs 决定长期目标（最缺口的需求），
    因此栈里的内容与世界里的实际处境一致，不是装饰性标签。
    """
    tv = _trait_values(traits)
    values = _derive(_VALUE_SPEC, tv, world_seed, aid)
    biases = _derive(_BIAS_SPEC, tv, world_seed, aid)
    belief = _derive(_WORLD_SPEC, tv, world_seed, aid)

    sk = skills or {}
    top_skill = max(sk, key=lambda k: (sk[k], k)) if sk else "gathering"
    role, role_meaning = SKILL_IDENTITY.get(top_skill, ("居民", "在社会中自处"))

    nd = needs or {}
    deficit = {k: 100.0 - float(nd.get(k, 100.0)) for k in NEED_GOAL}
    top_need = max(deficit, key=lambda k: (deficit[k], k))
    top_value = max(values, key=lambda k: (values[k], k))
    if deficit.get(top_need, 0.0) >= 60.0:
        # 生存级缺口压过个人偏好：快饿死的人不该去追"证明自己"
        goal_title = NEED_GOAL.get(top_need, "活下去")
    else:
        goal_title = VALUE_GOAL.get(top_value, NEED_GOAL.get(top_need, "活下去"))

    mode = _pick_language_mode(tv, _stable_unit(world_seed, aid, "language_mode"))

    identity = IdentityEngine()
    identity.identities.append(IdentityNode(
        name=role,
        strength=round(_clamp(0.45 + 0.4 * tv["conscientiousness"]), 6),
        priority=round(_clamp(0.40 + 0.4 * values["dignity"]), 6),
        satisfaction=round(_clamp(values["dignity"]), 6),
    ))
    goals = GoalEngine()
    goals.goals.append(GoalNode(
        name=goal_title,
        importance=round(_clamp(deficit.get(top_need, 0.0) / 100.0), 6),
        progress=0.0,
        urgency=round(_clamp(deficit.get(top_need, 0.0) / 100.0), 6),
    ))
    style = StyleProfile(
        base_verbosity=round(_clamp(0.25 + 0.5 * tv["extraversion"]), 6),
        formality=round(_clamp(0.35 + 0.5 * tv["conscientiousness"]), 6),
        sarcasm_tendency=round(_clamp(0.15 + 0.55 * (1.0 - tv["agreeableness"])), 6),
    )
    language = LanguagePersona(mode=mode, silence_policy=False)

    return PersonaStack(
        aid=aid, identity_role=role, goal_title=goal_title, language_mode=mode,
        values=values, biases=biases, world_beliefs=belief,
        identity=identity, goals=goals, language=language, style=style,
    )
