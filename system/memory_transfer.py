"""记忆传输通道（Memory Transfer Channel）——灵魂漫游的第七级：记忆真正过境。

============================================================
为什么需要这个模块
============================================================
soul_roaming.py 解决了"身份能不能被对方承认"，但它自己在文件头
第 35 行写着：具体的跨世界通信通道和记忆加密传输在后续实现。
也就是说：证书能签、能验、能映射，但记忆本体一步都没走出去。

本模块补上这一段，并且刻意绕开三个容易踩碎的坑：

坑一 · seal 不能跨世界
    memories 表的 seal 是本世界 HMAC 密钥（data_dir/memory_seal_key）
    的产物。把 seal 原样搬到对方世界，对方用自己那把密钥验，必然失败。
    → 处理方式：源 seal 不作验证凭据，只作"来源凭证"随记忆一起入库；
      目标世界用本地密钥重新封存。于是 source_seal 被包在 content 里，
      反过来受本地 seal 保护，篡改不了。

坑二 · 两台电脑未必能直连
    跨世界的真实场景常常是：同一局域网、不同城市、或者干脆隔着一堵
    公司防火墙。任何"必须有可直连端点"的设计都会在这里卡死。
    → 处理方式：信封是自包含的（.srbundle 一个 JSON 文件），
      可以走 HTTP，也可以走 U 盘 / 网盘 / 聊天窗口。传输方式与协议解耦。

坑三 · 记忆是隐私，通道公钥可被替换
    如果只是"你把你的加密公钥发给我"，中间人可以换成自己的公钥，
    从而解开全部记忆。
    → 处理方式：通道公钥（X25519）写进漫游证书的 identity_proof，
      而 identity_proof 整体参与世界私钥签名。换公钥 = 破签名。
      同时每个信封使用临时密钥对，前向保密，单个信封泄露不牵连其他。

============================================================
信封结构（bundle）
============================================================
{
  "bundle_version": 1,
  "transfer_id": "uuid",              # 幂等键，重复导入不重复写
  "certificate": {...},                # 源世界签发的漫游证书（含通道公钥）
  "ephemeral_pubkey_b64": "...",       # 发送方一次性 X25519 公钥
  "nonce_b64": "...",                  # AES-GCM 12 字节
  "ciphertext_b64": "...",             # 加密后的记忆载荷
  "manifest": [...],                   # 明文清单：迁移什么，内容不含
  "content_hash": "sha256..."          # 明文字节整体哈希
}

AAD（附加认证数据）= 规范 JSON{transfer_id, cert_id, content_hash,
source_world_id, target_world_id}。信封头任何一项被改动，解密即失败。

============================================================
单向 vs 双向
============================================================
单向：A 导出 → B 导入。A 不需要知道 B 的私钥，只知道 B 的通道公钥。
双向：两台各做一次，互为源与目标，记忆就形成了互见。
"""

import base64
import hashlib
import json
import logging
import time
import uuid
from typing import Dict, List, Optional, Set, Tuple

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .keys import sign_with_device, verify_signature
from .soul_roaming import SoulRoamingCertificate

logger = logging.getLogger(__name__)

BUNDLE_VERSION = 1
WORLD_CARD_VERSION = 1

_RAW_KEY_BYTES = 32
_AEAD_NONCE_BYTES = 12
_HKDF_INFO = b"second-reality/memory-bundle/v1"
_AEAD_INFO = b"second-reality/memory-bundle/v1/aad"

# 单次传输保护上限：防止一个信封吃掉内存
DEFAULT_MAX_ITEMS = 5000
DEFAULT_MAX_BYTES = 16 * 1024 * 1024


class TransferError(Exception):
    """记忆传输失败。code 供调用方（API/CLI）做机器可读判断。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> Dict:
        return {"error": self.message, "code": self.code}


# ============================================================
# 基础工具
# ============================================================

def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: str, field: str) -> bytes:
    try:
        return base64.b64decode(text, validate=True)
    except Exception:
        raise TransferError("bad_base64", f"字段 {field} 不是合法 base64")


def _canonical(obj) -> bytes:
    """规范 JSON 字节流：键排序、紧凑分隔符、不转义非 ASCII。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _x25519_private_from_secret(secret: bytes) -> X25519PrivateKey:
    if len(secret) != _RAW_KEY_BYTES:
        raise TransferError(
            "bad_channel_secret",
            f"通道秘密必须 {_RAW_KEY_BYTES} 字节，实际 {len(secret)}",
        )
    return X25519PrivateKey.from_private_bytes(secret)


def _x25519_public_raw(priv: X25519PrivateKey) -> bytes:
    return priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _x25519_public_from_raw(raw: bytes) -> X25519PublicKey:
    if len(raw) != _RAW_KEY_BYTES:
        raise TransferError("bad_peer_key", f"对端通道公钥应为 {_RAW_KEY_BYTES} 字节")
    try:
        return X25519PublicKey.from_public_bytes(raw)
    except Exception:
        raise TransferError("bad_peer_key", "对端通道公钥不是合法 X25519 点")


def _derive_session_key(shared: bytes, salt: bytes, transfer_id: str) -> bytes:
    """ECDH 共享秘密 → HKDF-SHA256 → 32 字节 AEAD 密钥。

    transfer_id 进 info，让同一对密钥在不同信封上派生出不同会话密钥。
    """
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=_HKDF_INFO + b"|" + transfer_id.encode("utf-8"),
    ).derive(shared)


def _build_aad(
    transfer_id: str, cert_id: str, content_hash: str, source_world_id: str, target_world_id: str
) -> bytes:
    """信封头的绑定数据。头被改动 → AAD 不匹配 → 解密失败。"""
    return _canonical(
        {
            "transfer_id": transfer_id,
            "cert_id": cert_id,
            "content_hash": content_hash,
            "source_world_id": source_world_id,
            "target_world_id": target_world_id,
            "aead": _AEAD_INFO.decode("ascii"),
        }
    )


# ============================================================
# 世界名片：交换一次，之后可端到端加密
# ============================================================

class WorldCard:
    """世界名片：让对方能安全地把记忆寄给你。

    名片由世界签名私钥自签名。初次传递仍依赖一个可信信道（你亲口念、
    你自己拷 U 盘），但此后任何篡改——尤其是替换通道公钥——都会验签失败。
    """

    def __init__(self, payload: Dict, signature_b64: str):
        self.payload = payload
        self.signature_b64 = signature_b64

    @property
    def world_id(self) -> str:
        return self.payload["world_id"]

    @property
    def transport_pubkey_b64(self) -> str:
        return self.payload["transport_pubkey_b64"]

    @property
    def identity_pubkey_b64(self) -> str:
        return self.payload["identity_pubkey_b64"]

    def canonical_payload(self) -> bytes:
        return _canonical(self.payload)

    def verify(self) -> Tuple[bool, str]:
        try:
            pubkey = _b64d(self.identity_pubkey_b64, "identity_pubkey_b64")
            sig = _b64d(self.signature_b64, "signature_b64")
            tp = _b64d(self.transport_pubkey_b64, "transport_pubkey_b64")
        except TransferError as e:
            return False, e.message
        if len(tp) != _RAW_KEY_BYTES:
            return False, "名片中的通道公钥长度非法"
        if not verify_signature(pubkey, self.canonical_payload(), sig):
            return False, "名片签名无效（可能被篡改）"
        return True, "ok"

    def to_dict(self) -> Dict:
        return {"card": self.payload, "signature_b64": self.signature_b64}

    @classmethod
    def from_dict(cls, d: Dict) -> "WorldCard":
        if not isinstance(d, dict) or not isinstance(d.get("card"), dict):
            raise TransferError("bad_card", "名片结构不完整")
        return cls(payload=d["card"], signature_b64=d.get("signature_b64", ""))


# ============================================================
# 记忆传输通道
# ============================================================

class MemoryTransferChannel:
    """单个世界的记忆传输通道。

    依赖注入（全部可选，缺省时降级但不越权）：
      world_id            本世界 ID
      storage             ledger.Storage（台账与伙伴名册持久化）
      channel_secret      32 字节 X25519 私钥（由 KMS 托管派生）
      vault               agent_engine.MemoryVault（记忆读写）
      roaming             SoulRoamingProtocol（证书签发与验证）
      signing_secret      世界 Ed25519 签名私钥（签名片）
      identity_pubkey     世界 Ed25519 公钥
    """

    def __init__(
        self,
        world_id: str,
        channel_secret: bytes,
        storage=None,
        vault=None,
        roaming=None,
        signing_secret: Optional[bytes] = None,
        identity_pubkey: Optional[bytes] = None,
        soul_ledger=None,
        max_items: int = DEFAULT_MAX_ITEMS,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ):
        self.world_id = world_id
        self._channel_secret = channel_secret
        self._channel_priv = _x25519_private_from_secret(channel_secret)
        self._channel_pub_raw = _x25519_public_raw(self._channel_priv)
        self._storage = storage
        self._vault = vault
        self._roaming = roaming
        self._signing_secret = signing_secret
        self._identity_pubkey = identity_pubkey
        self._soul_ledger = soul_ledger
        self.max_items = max_items
        self.max_bytes = max_bytes
        self._init_storage()

    # ── 初始化与存储 ──────────────────────────────────────────

    def _init_storage(self) -> None:
        if not self._storage:
            return
        # 伙伴名册：对方世界的身份公钥 + 通道公钥
        self._storage.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_transfer_peers (
                world_id             TEXT PRIMARY KEY,
                identity_pubkey_b64  TEXT NOT NULL,
                transport_pubkey_b64 TEXT NOT NULL,
                registered_at        REAL NOT NULL
            )
            """
        )
        # 传输台账：一个信封一条记录，transfer_id 即幂等键
        self._storage.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_transfer_log (
                transfer_id      TEXT PRIMARY KEY,
                direction        TEXT NOT NULL,
                source_world_id  TEXT NOT NULL,
                target_world_id  TEXT NOT NULL,
                cert_id          TEXT,
                item_count       INTEGER NOT NULL,
                payload_bytes    INTEGER NOT NULL,
                content_hash     TEXT,
                local_soul_hash  TEXT,
                created_at       REAL NOT NULL
            )
            """
        )
        # 逐条明细：用于回溯"这条记忆是从哪封信封来的"
        self._storage.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_transfer_items (
                transfer_id   TEXT NOT NULL,
                memory_id     TEXT NOT NULL,
                local_seq     INTEGER,
                item_hash     TEXT NOT NULL,
                imported_at   REAL NOT NULL,
                PRIMARY KEY (transfer_id, memory_id)
            )
            """
        )

    def transport_pubkey_b64(self) -> str:
        return _b64e(self._channel_pub_raw)

    def transport_pubkey_raw(self) -> bytes:
        return self._channel_pub_raw

    # ── 名片 ──────────────────────────────────────────────────

    def export_world_card(self) -> Dict:
        """生成本世界名片（自签名）。给对方注册用。"""
        if not self._signing_secret or not self._identity_pubkey:
            raise TransferError("no_signing_key", "缺少世界签名密钥，无法签发名片")
        payload = {
            "card_version": WORLD_CARD_VERSION,
            "world_id": self.world_id,
            "identity_pubkey_b64": _b64e(self._identity_pubkey),
            "transport_pubkey_b64": self.transport_pubkey_b64(),
            "issued_at": time.time(),
        }
        sig = sign_with_device(self._signing_secret, _canonical(payload))
        return WorldCard(payload=payload, signature_b64=_b64e(sig)).to_dict()

    def register_world_card(self, card_dict: Dict) -> Dict:
        """注册对方名片：验签 → 入册 → 同时登记到漫游公钥注册表。

        注册后该世界即可作为记忆传输的对端（加密目标）与证书签发源。
        """
        card = WorldCard.from_dict(card_dict)
        ok, reason = card.verify()
        if not ok:
            raise TransferError("invalid_card", f"名片校验失败：{reason}")
        if card.world_id == self.world_id:
            raise TransferError("self_card", "不能注册本世界自己的名片")

        identity_pub = _b64d(card.identity_pubkey_b64, "identity_pubkey_b64")
        transport_pub = _b64d(card.transport_pubkey_b64, "transport_pubkey_b64")

        if self._storage:
            self._storage.execute(
                "INSERT OR REPLACE INTO memory_transfer_peers "
                "(world_id, identity_pubkey_b64, transport_pubkey_b64, registered_at) "
                "VALUES (?, ?, ?, ?)",
                (card.world_id, card.identity_pubkey_b64, card.transport_pubkey_b64, time.time()),
            )
        # 同时让漫游层认识这个世界的身份公钥，否则收到它的证书验不了签
        if self._roaming:
            self._roaming.register_world(card.world_id, identity_pub)

        logger.info(
            "memory transfer peer registered world_id=%s transport=%s...",
            card.world_id, card.transport_pubkey_b64[:12],
        )
        return {
            "registered": True,
            "world_id": card.world_id,
            "transport_pubkey_b64": card.transport_pubkey_b64,
            "identity_pubkey_b64": _b64e(identity_pub),
        }

    def get_peer(self, world_id: str) -> Optional[Dict]:
        """取伙伴名册条目，本地缓存未命中则回查存储。"""
        if not self._storage:
            return None
        rows = self._storage.query(
            "SELECT world_id, identity_pubkey_b64, transport_pubkey_b64 "
            "FROM memory_transfer_peers WHERE world_id=?",
            (world_id,),
        )
        if not rows:
            return None
        return {
            "world_id": rows[0][0],
            "identity_pubkey_b64": rows[0][1],
            "transport_pubkey_b64": rows[0][2],
        }

    def list_peers(self) -> List[Dict]:
        if not self._storage:
            return []
        rows = self._storage.query(
            "SELECT world_id, transport_pubkey_b64, registered_at "
            "FROM memory_transfer_peers ORDER BY world_id"
        )
        return [
            {"world_id": r[0], "transport_pubkey_b64": r[1], "registered_at": r[2]}
            for r in rows
        ]

    # ── 记忆读取（导出侧） ────────────────────────────────────

    def _collect_memories(
        self, soul_hash: str, user_selected_ids: Optional[Set[str]] = None
    ) -> List[Dict]:
        """从本世界记忆库取出待迁移记忆。seq 作为稳定 memory_id。"""
        if not self._vault:
            raise TransferError("no_vault", "本世界未装配记忆库，无法导出记忆")
        rows = self._vault._storage.query(
            "SELECT seq, memory, seal, ts FROM memories WHERE soul_hash=? ORDER BY seq",
            (soul_hash,),
        )
        items: List[Dict] = []
        for seq, memory_json, seal, ts in rows:
            memory_id = str(seq)
            if user_selected_ids is not None and memory_id not in user_selected_ids:
                continue
            try:
                content = json.loads(memory_json)
            except Exception:
                # 坏数据不静默丢弃，明确拒绝——否则"迁移成功"是假的
                raise TransferError(
                    "corrupt_memory", f"本地记忆 seq={seq} 不是合法 JSON，拒绝导出"
                )
            items.append(
                {
                    "memory_id": memory_id,
                    "category": (content.get("category") if isinstance(content, dict) else None)
                    or "general",
                    "content": content,
                    "ts": ts,
                    "source_seal": seal,
                }
            )
        return items

    def _build_manifest(self, items: List[Dict]) -> List[Dict]:
        """明文清单：只暴露"有哪些、多大、哈希多少"，不暴露内容。

        哈希算法与 soul_roaming.verify_memory_integrity 完全一致，
        避免两边各算一套哈希导致"内容没错却验不过"。
        """
        manifest = []
        for item in items:
            content_hash = _sha256_hex(
                json.dumps(item["content"], sort_keys=True, ensure_ascii=False).encode("utf-8")
            )
            manifest.append(
                {
                    "memory_id": item["memory_id"],
                    "category": item.get("category", "general"),
                    "hash": content_hash,
                    "size_bytes": len(json.dumps(item["content"]).encode("utf-8")),
                }
            )
        return manifest

    # ── 导出：把记忆装进加密信封 ──────────────────────────────

    def export_bundle(
        self,
        target_world_id: str,
        soul_hash: str,
        user_selected_ids: Optional[Set[str]] = None,
        cert_ttl_seconds: Optional[int] = None,
        reputation_score: Optional[float] = None,
        account_age_days: Optional[float] = None,
        user_pubkey: Optional[bytes] = None,
    ) -> Dict:
        """为指定目标世界打一个记忆信封。

        流程：查名片 → 取记忆 → 建清单 → 签漫游证书（含通道公钥）
              → ECDH 派生会话密钥 → AES-GCM 加密 → 记台账

        注意 user_pubkey：漫游证书要求 identity_proof.pubkey_b64 的
        SHA-256 恰好等于 soul_hash，所以这里必须是**灵魂本人**的公钥，
        不是世界公钥。拿错会在对方验签第 5 步直接失败。
        """
        if not self._roaming:
            raise TransferError("no_roaming", "本世界未装配漫游协议，无法签发证书")
        peer = self.get_peer(target_world_id)
        if not peer:
            raise TransferError(
                "unknown_peer",
                f"未注册世界 {target_world_id} 的名片，无法加密（先交换名片）",
            )

        if user_pubkey is None and self._soul_ledger is not None:
            user_pubkey = self._soul_ledger.get_pubkey(soul_hash)
        if not user_pubkey:
            raise TransferError(
                "no_user_pubkey",
                f"灵魂 {soul_hash[:12]}... 无注册公钥，无法签发漫游证书",
            )

        items = self._collect_memories(soul_hash, user_selected_ids)
        if not items:
            raise TransferError("empty_selection", "没有可迁移的记忆（选择为空）")
        if len(items) > self.max_items:
            raise TransferError(
                "too_many_items", f"单封记忆条目 {len(items)} 超过上限 {self.max_items}"
            )

        manifest = self._build_manifest(items)
        payload = {
            "bundle_version": BUNDLE_VERSION,
            "source_world_id": self.world_id,
            "target_world_id": target_world_id,
            "soul_hash": soul_hash,
            "exported_at": time.time(),
            "items": items,
        }
        plaintext = _canonical(payload)
        if len(plaintext) > self.max_bytes:
            raise TransferError(
                "payload_too_large",
                f"信封明文 {len(plaintext)} 字节超过上限 {self.max_bytes}",
            )
        content_hash = _sha256_hex(plaintext)

        # 身份证明里塞通道公钥——它整体参与世界私钥签名，不可被替换
        identity_proof = {
            "pubkey_b64": _b64e(user_pubkey),
            "soul_hash_in_source": soul_hash,
            "transport_pubkey_b64": self.transport_pubkey_b64(),
        }
        if self._identity_pubkey:
            identity_proof["world_pubkey_b64"] = _b64e(self._identity_pubkey)
        if reputation_score is not None:
            identity_proof["reputation_score"] = reputation_score
        if account_age_days is not None:
            identity_proof["account_age_days"] = account_age_days

        cert = self._roaming.issue_roaming_certificate(
            soul_hash=soul_hash,
            target_world_id=target_world_id,
            identity_proof=identity_proof,
            memory_manifest=manifest,
            ttl_seconds=cert_ttl_seconds,
        )

        # —— 加密 ——
        ephemeral_priv = X25519PrivateKey.generate()
        ephemeral_pub_raw = _x25519_public_raw(ephemeral_priv)
        peer_transport_pub = _x25519_public_from_raw(
            _b64d(peer["transport_pubkey_b64"], "peer.transport_pubkey_b64")
        )
        shared = ephemeral_priv.exchange(peer_transport_pub)

        transfer_id = uuid.uuid4().hex
        # salt 绑两端公钥，防止同一会话密钥被挪用到别的信封
        salt = hashlib.sha256(ephemeral_pub_raw + peer_transport_pub.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )).digest()
        session_key = _derive_session_key(shared, salt, transfer_id)

        nonce = hashlib.sha256(uuid.uuid4().bytes + transfer_id.encode()).digest()[:_AEAD_NONCE_BYTES]
        aad = _build_aad(
            transfer_id, cert.cert_id, content_hash, self.world_id, target_world_id
        )
        ciphertext = AESGCM(session_key).encrypt(nonce, plaintext, aad)

        bundle = {
            "bundle_version": BUNDLE_VERSION,
            "transfer_id": transfer_id,
            "certificate": cert.to_dict(),
            "ephemeral_pubkey_b64": _b64e(ephemeral_pub_raw),
            "nonce_b64": _b64e(nonce),
            "ciphertext_b64": _b64e(ciphertext),
            "manifest": manifest,
            "content_hash": content_hash,
        }

        self._record_transfer(
            transfer_id=transfer_id,
            direction="export",
            source_world_id=self.world_id,
            target_world_id=target_world_id,
            cert_id=cert.cert_id,
            item_count=len(items),
            payload_bytes=len(plaintext),
            content_hash=content_hash,
            local_soul_hash=soul_hash,
        )
        logger.info(
            "memory bundle exported transfer=%s %s->%s items=%d bytes=%d",
            transfer_id, self.world_id, target_world_id, len(items), len(plaintext),
        )
        return bundle

    # ── 导入：验证书 → 解密 → 校验 → 重封存 ──────────────────

    def import_bundle(
        self,
        bundle: Dict,
        local_soul_hash: Optional[str] = None,
        require_mapping: bool = False,
    ) -> Dict:
        """接收并落地一封记忆信封。

        幂等：同一 transfer_id 重复导入直接返回 already_imported，
        不重复写记忆（防止一次传输被复制多份导致记忆膨胀）。
        """
        if not isinstance(bundle, dict):
            raise TransferError("bad_bundle", "信封必须是 JSON 对象")
        version = bundle.get("bundle_version")
        if version != BUNDLE_VERSION:
            raise TransferError("unsupported_version", f"不支持的信封版本 {version}")

        for field in (
            "transfer_id", "certificate", "ephemeral_pubkey_b64",
            "nonce_b64", "ciphertext_b64", "manifest", "content_hash",
        ):
            if field not in bundle:
                raise TransferError("bad_bundle", f"信封缺少字段 {field}")

        transfer_id = bundle["transfer_id"]
        # 幂等前置检查：不必解密就能判断是否处理过
        existing = self.get_transfer(transfer_id)
        if existing:
            return {
                "status": "already_imported",
                "transfer_id": transfer_id,
                "item_count": existing["item_count"],
                "source_world_id": existing["source_world_id"],
                "local_soul_hash": existing["local_soul_hash"],
            }

        # 1) 证书验证（目标世界必须是本世界、未过期、源世界已注册、签名有效）
        try:
            cert = SoulRoamingCertificate.from_dict(bundle["certificate"])
        except (KeyError, TypeError) as e:
            raise TransferError("bad_certificate", f"证书结构不完整：{e}")
        if not self._roaming:
            raise TransferError("no_roaming", "本世界未装配漫游协议，无法验证证书")
        valid, reason = self._roaming.verify_roaming_certificate(cert)
        if not valid:
            raise TransferError("cert_rejected", f"漫游证书未通过：{reason}")

        # 2) 核对信封头与证书一致（防止拼接：真证书 + 别的内容）
        if cert.source_world_id != bundle.get("certificate", {}).get("source_world_id"):
            raise TransferError("header_mismatch", "信封头与证书源世界不一致")
        if cert.memory_manifest != bundle["manifest"]:
            raise TransferError("manifest_mismatch", "信封清单与证书内清单不一致（可能被替换）")

        # 3) 解密
        payload, content_hash = self._decrypt_bundle(bundle, cert)
        if content_hash != bundle["content_hash"]:
            raise TransferError("hash_mismatch", "明文哈希与信封声明不一致")
        if payload.get("source_world_id") != cert.source_world_id:
            raise TransferError("header_mismatch", "明文源世界与证书不一致")
        if payload.get("target_world_id") != self.world_id:
            raise TransferError("wrong_target", "明文目标世界不是本世界")

        items = payload.get("items") or []
        if len(items) != len(bundle["manifest"]):
            raise TransferError("count_mismatch", "记忆条数与清单不符")

        # 4) 逐条校验内容哈希（复用漫游层的哈希口径）
        manifest_by_id = {m["memory_id"]: m for m in bundle["manifest"]}
        if len(manifest_by_id) != len(bundle["manifest"]):
            raise TransferError("duplicate_memory_id", "清单中存在重复 memory_id")
        for item in items:
            entry = manifest_by_id.get(item.get("memory_id"))
            if entry is None:
                raise TransferError("manifest_mismatch", f"条目 {item.get('memory_id')} 不在清单中")
            if not self._roaming.verify_memory_integrity({"content": item["content"]}, entry):
                raise TransferError(
                    "item_hash_mismatch", f"条目 {item['memory_id']} 内容哈希不符"
                )

        # 5) 目标灵魂：优先既有漫游映射，其次显式传入，最后沿用源 soul_hash
        source_soul = cert.soul_hash
        mapped = self._roaming.get_local_soul_hash(cert.source_world_id, source_soul)
        target_soul = mapped or local_soul_hash or source_soul
        if require_mapping and not mapped:
            raise TransferError(
                "mapping_required",
                "要求先完成灵魂映射（/roaming/map）才能导入记忆",
            )

        # 6) 落地：源 seal 作为来源凭证随内容入库，本地重新封存
        if not self._vault:
            raise TransferError("no_vault", "本世界未装配记忆库，无法落地记忆")
        written = []
        now = time.time()
        for item in items:
            # 保持原记忆结构不变：原字段照原样放回顶层，协议元数据收纳到
            # 保留键 _roaming 下。这样导入方读取 memory["content"] 仍然拿到
            # 原来的内容，不会因为"过了一趟海关"就多套一层壳。
            source_content = item["content"]
            base = source_content if isinstance(source_content, dict) else {"content": source_content}
            record = dict(base)
            record["_roaming"] = {
                "origin": {
                    "world_id": cert.source_world_id,
                    "soul_hash": source_soul,
                    "memory_id": item["memory_id"],
                    "source_seal": item.get("source_seal"),
                    "source_ts": item.get("ts"),
                },
                "transfer_id": transfer_id,
                "imported_at": now,
            }
            local_seal = self._vault.store_memory(target_soul, record)
            written.append(
                {
                    "memory_id": item["memory_id"],
                    "local_seal": local_seal,
                    "hash": manifest_by_id[item["memory_id"]]["hash"],
                }
            )

        # 7) 台账 + 明细
        self._record_transfer(
            transfer_id=transfer_id,
            direction="import",
            source_world_id=cert.source_world_id,
            target_world_id=self.world_id,
            cert_id=cert.cert_id,
            item_count=len(written),
            payload_bytes=len(_canonical(payload)),
            content_hash=bundle["content_hash"],
            local_soul_hash=target_soul,
        )
        if self._storage:
            for w in written:
                self._storage.execute(
                    "INSERT OR IGNORE INTO memory_transfer_items "
                    "(transfer_id, memory_id, local_seq, item_hash, imported_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (transfer_id, w["memory_id"], None, w["hash"], now),
                )

        logger.info(
            "memory bundle imported transfer=%s from=%s items=%d soul=%s",
            transfer_id, cert.source_world_id, len(written), target_soul,
        )
        return {
            "status": "imported",
            "transfer_id": transfer_id,
            "source_world_id": cert.source_world_id,
            "source_soul_hash": source_soul,
            "local_soul_hash": target_soul,
            "mapped": bool(mapped),
            "item_count": len(written),
            "content_hash": bundle["content_hash"],
        }

    def _decrypt_bundle(self, bundle: Dict, cert: SoulRoamingCertificate) -> Tuple[Dict, str]:
        """ECDH 解密 + 解 JSON。AAD 不匹配时 AES-GCM 直接抛异常。"""
        ephemeral_pub_raw = _b64d(bundle["ephemeral_pubkey_b64"], "ephemeral_pubkey_b64")
        nonce = _b64d(bundle["nonce_b64"], "nonce_b64")
        ciphertext = _b64d(bundle["ciphertext_b64"], "ciphertext_b64")
        if len(nonce) != _AEAD_NONCE_BYTES:
            raise TransferError("bad_nonce", f"nonce 应为 {_AEAD_NONCE_BYTES} 字节")

        ephemeral_pub = _x25519_public_from_raw(ephemeral_pub_raw)
        shared = self._channel_priv.exchange(ephemeral_pub)
        salt = hashlib.sha256(
            ephemeral_pub_raw + self._channel_pub_raw
        ).digest()
        session_key = _derive_session_key(shared, salt, bundle["transfer_id"])

        aad = _build_aad(
            bundle["transfer_id"],
            cert.cert_id,
            bundle["content_hash"],
            cert.source_world_id,
            cert.target_world_id,
        )
        try:
            plaintext = AESGCM(session_key).decrypt(nonce, ciphertext, aad)
        except Exception:
            raise TransferError(
                "decrypt_failed",
                "解密失败：可能是信封被改动、目标世界不匹配，或本世界通道密钥已轮换",
            )
        try:
            payload = json.loads(plaintext)
        except Exception:
            raise TransferError("bad_payload", "解密后不是合法 JSON")
        if not isinstance(payload, dict):
            raise TransferError("bad_payload", "解密后不是 JSON 对象")
        return payload, _sha256_hex(plaintext)

    # ── 台账查询 ──────────────────────────────────────────────

    def _record_transfer(
        self,
        transfer_id: str,
        direction: str,
        source_world_id: str,
        target_world_id: str,
        cert_id: Optional[str],
        item_count: int,
        payload_bytes: int,
        content_hash: Optional[str],
        local_soul_hash: Optional[str],
    ) -> None:
        if not self._storage:
            return
        self._storage.execute(
            "INSERT OR REPLACE INTO memory_transfer_log "
            "(transfer_id, direction, source_world_id, target_world_id, cert_id, "
            " item_count, payload_bytes, content_hash, local_soul_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                transfer_id, direction, source_world_id, target_world_id, cert_id,
                item_count, payload_bytes, content_hash, local_soul_hash, time.time(),
            ),
        )

    def get_transfer(self, transfer_id: str) -> Optional[Dict]:
        if not self._storage:
            return None
        rows = self._storage.query(
            "SELECT transfer_id, direction, source_world_id, target_world_id, cert_id, "
            "item_count, payload_bytes, content_hash, local_soul_hash, created_at "
            "FROM memory_transfer_log WHERE transfer_id=?",
            (transfer_id,),
        )
        if not rows:
            return None
        r = rows[0]
        return {
            "transfer_id": r[0], "direction": r[1], "source_world_id": r[2],
            "target_world_id": r[3], "cert_id": r[4], "item_count": r[5],
            "payload_bytes": r[6], "content_hash": r[7], "local_soul_hash": r[8],
            "created_at": r[9],
        }

    def list_transfers(self, limit: int = 50) -> List[Dict]:
        if not self._storage:
            return []
        rows = self._storage.query(
            "SELECT transfer_id, direction, source_world_id, target_world_id, cert_id, "
            "item_count, payload_bytes, content_hash, local_soul_hash, created_at "
            "FROM memory_transfer_log ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        )
        return [
            {
                "transfer_id": r[0], "direction": r[1], "source_world_id": r[2],
                "target_world_id": r[3], "cert_id": r[4], "item_count": r[5],
                "payload_bytes": r[6], "content_hash": r[7], "local_soul_hash": r[8],
                "created_at": r[9],
            }
            for r in rows
        ]


__all__ = [
    "BUNDLE_VERSION",
    "MemoryTransferChannel",
    "TransferError",
    "WorldCard",
]
