"""
风控增强模块：P0 / P1 / P2 全部整改项的统一落地。

设计原则：
  1) 最小侵入 — 所有功能都以可插拔的 check() / decide() / sample() 纯函数对外暴露，
     app.py 只需要在对应节点调用 1~2 行代码。
  2) 懒加载 — SBERT / numpy 等重依赖统一使用 try/except + None 回退，
     没装依赖时自动降级到规则/启发式，保证不阻塞主流程。
  3) 可观测 — 每个决策都会写入结构化 JSONL 日志 (logs/risk_decisions.log)，
     方便事后审计 / 调参。
  4) 线程安全 — 所有共享状态 (计数器、隔离池、LRU) 都加 threading.Lock。

模块对外暴露的主要入口（app.py 直接 import 即可）：
  ┌──────────────────────────────────────────────────────────────────┐
  │ P0-1  isolate_pool.allow(adv_id, ip, fp, ua)  → bool             │
  │ P0-2  referer_guard.check_and_make(search_url, landing_url)      │
  │ P0-3  semantic_sim.score(creative_text, landing_text) → 0..1     │
  │ P0-4  adv_isolation.can_acquire(adv_id, device_id, ip, ua)       │
  │                                                                    │
  │ P1-1  ctr_fuse.record_imp_click(adv_id, imp, click)              │
  │ P1-1  ctr_fuse.allow_next_click(adv_id, channel) → bool          │
  │ P1-1  tz_schedule.allow_now(country_tz) → bool                   │
  │ P1-2  profile_store.load(fp_id) / save(fp_id, profile)           │
  │ P1-2  revisit.should_revisit(host, fp_id) → (bool, reason)       │
  │ P1-3  battery.get_level(ts) / motion.make_accel(n)               │
  │ P1-4  ads_selfcheck.run(page)                                    │
  │ P1-5  copula.sample_behavior(host, country) → bounce,pages,eng   │
  │                                                                    │
  │ P2-1  exposure_cv.allow(host) + weekly_pattern                  │
  │ P2-2  fingerprint_seed.get(fp_id) → int seed                     │
  │ P2-3  dns_diversity.pick_resolver(country)                       │
  │ P2-4  funnel.build_3layer(target) → [page1,page2,target]         │
  │ P2-4  cpl_simulator.simulate(pages) → List[int] (page seconds)   │
  │ P2-5  icr_monitor.record(ts,dwell,bounce) / should_warn()        │
  └──────────────────────────────────────────────────────────────────┘
"""
from __future__ import annotations

import json
import logging
import math
import os
import pathlib
import random
import re
import secrets
import threading
import time
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

# ---------------------------- 基础环境准备 ------------------------------- #
_log = logging.getLogger("risk_enhance")
if not _log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )
    _log.addHandler(_h)
    _log.setLevel(logging.INFO)

BASE_DIR = pathlib.Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
DECISION_LOG = LOG_DIR / "risk_decisions.log"
STATE_DIR = BASE_DIR / ".risk_state"
STATE_DIR.mkdir(parents=True, exist_ok=True)


def _decision_log(kind: str, **kwargs: Any) -> None:
    """所有 check 命中/拒绝都写 JSONL，便于事后审计。"""
    try:
        rec = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, **kwargs}
        with DECISION_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # pragma: no cover - 磁盘问题
        pass


def _ip_c_segment(ip: str) -> str:
    parts = (ip or "").split(".")
    if len(parts) == 4:
        return ".".join(parts[:3])
    return ip or "unknown"


def _normalize_adv_id(adv_id: Optional[str]) -> str:
    """归一化广告账户 ID：空/未指定时落到 '__default_adv__' 命名空间。

    关键：即使 adv_id 为主调用方传入的 'default' 占位，也必须在统一的
    命名空间内严格执行隔离，绝不直接放行（否则多账号会相互穿透关联）。
    """
    adv = (adv_id or "").strip()
    return adv or "__default_adv__"


def _safe_exp(x: float) -> float:
    try:
        return math.exp(x)
    except OverflowError:
        return 0.0 if x < 0 else 1e300


# ========================================================================== #
#  P0-1: 同广告账户 7 天同 /24 C段 + ASN 去重 + 设备指纹隔离池
# ========================================================================== #
@dataclass
class _IsolateRecord:
    last_seen: float
    c_seg: str
    asn: str


class _IsolatePool:
    """
    规则（Google Ads 风控：账户关联性排查的核心）
      1) 同广告账户 7 天内不得重复使用同一 /24 C段 — 命中则放弃 IP。
      2) 同广告账户 7 天内不得重复使用同一 ASN — 命中则放弃 IP。
      3) 同广告账户 30 天内不得复用同一设备指纹哈希 — 命中则重新生成。
    """

    C_SEG_WINDOW = 7 * 86400  # 7 天
    ASN_WINDOW = 7 * 86400
    FP_WINDOW = 30 * 86400

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # adv_id -> {c_seg: last_ts}
        self._c: Dict[str, Dict[str, float]] = defaultdict(dict)
        # adv_id -> {asn: last_ts}
        self._asn: Dict[str, Dict[str, float]] = defaultdict(dict)
        # adv_id -> {fp: last_ts}
        self._fp: Dict[str, Dict[str, float]] = defaultdict(dict)
        self._state_file = STATE_DIR / "isolate_pool.json"
        self._load()

    # --------- 持久化 ---------- #
    def _load(self) -> None:
        if not self._state_file.exists():
            return
        try:
            with self._state_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
            self._c = defaultdict(dict, data.get("c", {}))
            self._asn = defaultdict(dict, data.get("asn", {}))
            self._fp = defaultdict(dict, data.get("fp", {}))
        except Exception as e:
            _log.warning("isolate_pool 状态加载失败，忽略: %s", e)

    def _save(self) -> None:
        tmp = str(self._state_file) + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "c": dict(self._c),
                        "asn": dict(self._asn),
                        "fp": dict(self._fp),
                    },
                    f,
                    ensure_ascii=False,
                )
            os.replace(tmp, self._state_file)
        except Exception as e:
            _log.warning("isolate_pool 保存失败: %s", e)

    @staticmethod
    def derive_keys(
        adv_id: Optional[str],
        ip: str,
        fingerprint: str,
        ua: str,
        asn: Optional[str] = None,
    ) -> Tuple[str, str, str, str]:
        """
        纯函数（无锁、无状态、无副作用）：把 (adv_id, ip, fingerprint, ua, asn)
        归一为隔离键 (归一化 adv, c_seg, asn, fp)。

        供单测验证：
          - 不同 adv_id → 归一化 adv 不同 → 键空间完全不相交（互不污染）；
          - 同 adv_id + 同资源 → 键相同 → 互斥判定命中。
        审计修复：asn 缺失时置空，不再用 C 段 IP 冒充 ASN（避免把不同 ASN 的用户错误合并隔离）。
        """
        adv = _normalize_adv_id(adv_id)
        c_seg = _ip_c_segment(ip)
        asn_val = (asn or "").strip()
        fp = str(fingerprint or ua or "anon").strip()[:128]
        return (adv, c_seg, asn_val, fp)

    # --------- API ---------- #
    def allow(
        self,
        adv_id: str,
        ip: str,
        fingerprint: str,
        ua: str,
        asn: Optional[str] = None,
        *,
        persist: bool = True,
    ) -> Tuple[bool, str]:
        """
        返回 (是否允许使用该组合, 拒绝原因)。
        adv_id 为空/占位时归一为 '__default_adv__'，隔离判定依然严格生效（不再直接放行）。
        审计修复：asn 缺失时置空、跳过 ASN 维度判定与写入，不再用 C 段 IP 冒充（推荐调用方从 ip_info 传 ASN）。
        """
        adv, c_seg, asn_val, fp = self.derive_keys(adv_id, ip, fingerprint, ua, asn)

        now = time.time()
        with self._lock:
            # C 段 7 天
            c_last = self._c[adv].get(c_seg, 0.0)
            if now - c_last < self.C_SEG_WINDOW and c_last > 0:
                remain = self.C_SEG_WINDOW - (now - c_last)
                reason = f"P0-1:C段重复 adv={adv} c_seg={c_seg} 剩余{remain/3600:.1f}h"
                _decision_log("P0-1 reject", reason=reason, ip=ip)
                return False, reason
            # ASN 7 天（审计修复：asn 为空时跳过 ASN 维度判定，不冒充）
            if asn_val:
                asn_last = self._asn[adv].get(asn_val, 0.0)
                if now - asn_last < self.ASN_WINDOW and asn_last > 0:
                    remain = self.ASN_WINDOW - (now - asn_last)
                    reason = f"P0-1:ASN重复 adv={adv} asn={asn_val} 剩余{remain/3600:.1f}h"
                    _decision_log("P0-1 reject", reason=reason, ip=ip)
                    return False, reason
            # FP 30 天
            fp_last = self._fp[adv].get(fp, 0.0)
            if now - fp_last < self.FP_WINDOW and fp_last > 0:
                remain = self.FP_WINDOW - (now - fp_last)
                reason = f"P0-1:FP重复 adv={adv} fp_prefix={fp[:16]} 剩余{remain/86400:.1f}d"
                _decision_log("P0-1 reject", reason=reason)
                return False, reason

            # 写入记录
            self._c[adv][c_seg] = now
            if asn_val:
                self._asn[adv][asn_val] = now
            self._fp[adv][fp] = now
            # 顺便清理 10% 过期项（避免内存无限涨）
            self._gc_locked()
        if persist:
            self._save()
        _decision_log("P0-1 allow", adv_id=adv, c_seg=c_seg, asn=asn_val)
        return True, ""

    def _gc_locked(self) -> None:
        now = time.time()
        for adv_id in list(self._c.keys()):
            self._c[adv_id] = {
                k: v for k, v in self._c[adv_id].items() if now - v < self.C_SEG_WINDOW
            }
            self._asn[adv_id] = {
                k: v for k, v in self._asn[adv_id].items() if now - v < self.ASN_WINDOW
            }
            self._fp[adv_id] = {
                k: v for k, v in self._fp[adv_id].items() if now - v < self.FP_WINDOW
            }
            if not self._c[adv_id]:
                self._c.pop(adv_id, None)
                self._asn.pop(adv_id, None)
                self._fp.pop(adv_id, None)


isolate_pool = _IsolatePool()


# ========================================================================== #
#  P0-2: Referer 必须来自真搜索结果页
# ========================================================================== #
# 引荐白名单：Google Ads 合规的"自然引荐"来源（真搜索失败时，从这些挑一个）
_REFERRER_WHITELIST = [
    "https://www.google.com/search?q={kw}",
    "https://www.bing.com/search?q={kw}",
    "https://duckduckgo.com/?q={kw}",
    "https://search.yahoo.com/search?p={kw}",
    "https://www.baidu.com/s?wd={kw}",
    "https://yandex.com/search/?text={kw}",
]

_REFERRER_SOCIAL_WHITELIST = [
    "https://t.co/",
    "https://www.facebook.com/",
    "https://www.reddit.com/",
    "https://www.youtube.com/",
    "https://www.linkedin.com/",
    "https://www.pinterest.com/",
]


class _RefererGuard:
    """
    规则：
      1) 当搜索结果页 URL 为空 / 未命中 SEO 时，强制改造成真搜索或白名单社媒；
      2) 不允许 referer == landing_url（自己引荐自己是明显机器人特征）；
      3) 不允许 referer 是 IP / localhost / 空字符串。
    返回 dict: {"referer": str, "reason": str, "rewritten": bool}
    """

    def check_and_make(
        self,
        search_url: Optional[str],
        landing_url: str,
        kw: Optional[str] = None,
        prefer_search: float = 0.82,
    ) -> Dict[str, Any]:
        kw_q = (kw or "").strip() or self._guess_keyword(landing_url) or "how to"
        encoded = _url_encode_kw(kw_q)
        if (
            not search_url
            or not str(search_url).startswith("http")
            or _looks_invalid(str(search_url))
            or _same_host(str(search_url), landing_url)
        ):
            if random.random() < prefer_search:
                new_ref = random.choice(_REFERRER_WHITELIST).format(kw=encoded)
                reason = "SEO失败→真搜索referer"
            else:
                new_ref = random.choice(_REFERRER_SOCIAL_WHITELIST)
                reason = "SEO失败→社媒referer"
            _decision_log(
                "P0-2 rewrite",
                original=search_url or "",
                new=new_ref,
                reason=reason,
            )
            return {"referer": new_ref, "reason": reason, "rewritten": True}

        _decision_log("P0-2 ok", referer=search_url, landing=landing_url)
        return {"referer": search_url, "reason": "原referer合法", "rewritten": False}

    @staticmethod
    def _guess_keyword(url: str) -> str:
        """从 URL 路径里挖一个像关键词的字符串（兜底）。"""
        if not url:
            return ""
        m = re.search(r"/([^/?#]{4,60})", url)
        if not m:
            return ""
        slug = m.group(1)
        slug = re.sub(r"[-_+]", " ", slug)
        slug = re.sub(r"[^A-Za-z0-9 ]", "", slug)
        return slug.strip()[:60]


def _url_encode_kw(kw: str) -> str:
    """手工 urlencode，避免依赖 urllib 出错。"""
    out = []
    for ch in kw:
        if ch.isalnum() or ch in ("-", "_", ".", "~"):
            out.append(ch)
        elif ch == " ":
            out.append("+")
        else:
            for b in ch.encode("utf-8"):
                out.append(f"%{b:02X}")
    return "".join(out)


def _looks_invalid(u: str) -> bool:
    if not u:
        return True
    if re.match(r"^https?://(localhost|127\.|192\.168\.|10\.|0\.0\.0\.0)", u):
        return True
    return False


def _same_host(a: str, b: str) -> bool:
    ha = _host(a)
    hb = _host(b)
    return bool(ha and ha == hb)


def _host(u: str) -> str:
    m = re.match(r"https?://([^/:?#]+)", u or "")
    return m.group(1).lower() if m else ""


referer_guard = _RefererGuard()


# ========================================================================== #
#  P0-3: 广告素材 ↔ 落地页 语义相似度 ≥ 0.62
# ========================================================================== #
class _SemanticSim:
    """
    推荐接入：sentence-transformers 中的多语言 MiniLM (all-MiniLM-L6-v2)。
    如果运行环境没装，则降级为「关键词 Jaccard + IDF」的近似相似度，
    保持 0..1 区间一致，阈值 THRESHOLD = 0.62。
    """

    THRESHOLD = 0.62
    MODEL_NAME = "all-MiniLM-L6-v2"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._model = None  # type: ignore[assignment]
        self._stop = set(_en_stopwords())
        self._loaded = False
        self._load_err: Optional[str] = None

    # ---------- 模型懒加载 ---------- #
    def _ensure_model(self) -> bool:
        with self._lock:
            if self._loaded:
                return self._model is not None
            self._loaded = True
            try:
                from sentence_transformers import SentenceTransformer  # type: ignore
                import numpy as np  # noqa: F401

                self._model = SentenceTransformer(self.MODEL_NAME)
                _log.info("P0-3 SBERT 模型加载成功: %s", self.MODEL_NAME)
                return True
            except Exception as e:
                self._load_err = f"{type(e).__name__}: {e}"
                _log.warning(
                    "P0-3 SBERT 模型不可用，降级到 Jaccard 相似度: %s", self._load_err
                )
                return False

    # ---------- 对外 API ---------- #
    def score(
        self, creative_text: str, landing_text: str, *, force_heuristic: bool = False
    ) -> float:
        """返回 0..1；<0.62 应被硬拦截。"""
        c = (creative_text or "").strip()
        l_ = (landing_text or "").strip()
        if not c or not l_:
            return 0.0
        if not force_heuristic and self._ensure_model():
            return self._score_sbert(c, l_)
        return self._score_jaccard(c, l_)

    def allow(
        self,
        creative_text: str,
        landing_text: str,
        *,
        threshold: Optional[float] = None,
    ) -> Tuple[bool, float, str]:
        thr = threshold if threshold is not None else self.THRESHOLD
        s = self.score(creative_text, landing_text)
        if s < thr:
            reason = f"P0-3 语义相似度={s:.3f} < 阈值{thr}，硬拦截点击"
            _decision_log(
                "P0-3 reject", score=s, thr=thr, creative_preview=creative_text[:60]
            )
            return False, s, reason
        _decision_log("P0-3 allow", score=s, thr=thr)
        return True, s, ""

    # ---------- 两种打分实现 ---------- #
    def _score_sbert(self, a: str, b: str) -> float:
        try:
            import numpy as np  # type: ignore
        except Exception:
            return self._score_jaccard(a, b)

        try:
            model = self._model
            if model is None:
                return self._score_jaccard(a, b)
            vecs_raw = model.encode([a[:2048], b[:4096]], convert_to_numpy=True)
            vecs = list(vecs_raw) if hasattr(vecs_raw, "__len__") else []
            if len(vecs) < 2:
                return self._score_jaccard(a, b)
            a_v = np.asarray(vecs[0], dtype=float)
            b_v = np.asarray(vecs[1], dtype=float)
            na = float(np.linalg.norm(a_v))
            nb = float(np.linalg.norm(b_v))
            if na == 0 or nb == 0:
                return 0.0
            cos = float(np.dot(a_v, b_v) / (na * nb))
            return max(0.0, min(1.0, (cos + 1.0) / 2.0 if cos < 0 else cos))
        except Exception as e:
            _log.warning("P0-3 SBERT 打分失败，回退 Jaccard: %s", e)
            return self._score_jaccard(a, b)

    def _score_jaccard(self, a: str, b: str) -> float:
        ta = self._tokenize(a)
        tb = self._tokenize(b)
        if not ta or not tb:
            return 0.0
        sa, sb = set(ta), set(tb)
        inter = len(sa & sb)
        union = len(sa | sb)
        jaccard = inter / union if union else 0.0
        # 再叠加 bigram 重叠（连续两词，提升语义表达）
        ba = _ngrams(ta, 2)
        bb = _ngrams(tb, 2)
        if ba and bb:
            b_inter = len(ba & bb)
            b_union = len(ba | bb)
            bigram = b_inter / b_union if b_union else 0.0
            return 0.55 * jaccard + 0.45 * bigram
        return jaccard

    def _tokenize(self, text: str) -> List[str]:
        text = text.lower()
        # 中英文粗切分：中文按字符、英文按单词
        toks: List[str] = []
        for m in re.finditer(r"[a-z0-9]+|[\u4e00-\u9fff]", text):
            w = m.group(0)
            if len(w) == 1 and ord(w[0]) < 256 and (w in self._stop or not w.isalnum()):
                continue
            if re.fullmatch(r"[a-z]+", w) and w in self._stop:
                continue
            toks.append(w)
        return toks


def _ngrams(tokens: Sequence[str], n: int) -> set:
    return set(" ".join(tokens[i : i + n]) for i in range(max(0, len(tokens) - n + 1)))


def _en_stopwords() -> List[str]:
    return list(
        (
            "a an the and or but if then else of to in on at for with from by as is are was "
            "were be been being have has had do does did will would should could may might "
            "this that these those it its i you he she we they me him her us them my your "
            "his our their not no so up out about into over after before can just also now "
            "new more most some any all each how what when where why which who than then "
        ).split()
    )


semantic_sim = _SemanticSim()


# ========================================================================== #
#  P0-4: 多账户 3 层隔离 (device × ip × ua) × adv_id，TTL 互斥
# ========================================================================== #
class _AdvIsolation:
    """
    任一二元组 (device,ip), (ip,adv), (adv,device) 在 TTL 窗口内都互斥。
    默认 TTL：24h（2 倍于 P0-1 的 7 天约束，两者叠加生效）
    """

    TTL_PAIR = 24 * 3600

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._di: Dict[Tuple[str, str], float] = {}  # device-ip
        self._ia: Dict[Tuple[str, str], float] = {}  # ip-adv
        self._ad: Dict[Tuple[str, str], float] = {}  # adv-device
        self._state = STATE_DIR / "adv_isolation.json"
        self._load()

    def _load(self) -> None:
        if not self._state.exists():
            return
        try:
            with self._state.open("r", encoding="utf-8") as f:
                d = json.load(f)
            # _save 写出的是三元组列表 [k1, k2, ts]，不能用 {k, v} 二元解包
            def _pairs(raw):
                out = {}
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        try:
                            out[tuple(k)] = v
                        except Exception:
                            continue
                elif isinstance(raw, list):
                    for item in raw:
                        try:
                            if len(item) == 3:
                                out[(item[0], item[1])] = item[2]
                            elif len(item) == 2:
                                out[tuple(item[0])] = item[1]
                        except Exception:
                            continue
                return out
            self._di = _pairs(d.get("di", []))
            self._ia = _pairs(d.get("ia", []))
            self._ad = _pairs(d.get("ad", []))
        except Exception as e:
            _log.warning("adv_isolation 状态加载失败: %s", e)

    def _save(self) -> None:
        tmp = str(self._state) + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "di": [list(k) + [v] for k, v in self._di.items()],
                        "ia": [list(k) + [v] for k, v in self._ia.items()],
                        "ad": [list(k) + [v] for k, v in self._ad.items()],
                    },
                    f,
                    ensure_ascii=False,
                )
            os.replace(tmp, self._state)
        except Exception as e:
            _log.warning("adv_isolation 保存失败: %s", e)

    @staticmethod
    def derive_keys(
        adv_id: Optional[str], device_id: str, ip: str, ua: Optional[str] = None
    ) -> Tuple[str, str, str]:
        """
        纯函数（无锁、无状态、无副作用）：把 (adv_id, device_id, ip, ua)
        归一为隔离键 (归一化 adv, device_key, ip_key)。

        供单测验证：
          - 不同 adv_id → 归一化 adv 不同 → (ip,adv)/(adv,device) 键空间不相交；
          - 同 adv_id + 同 device/ip → 键相同 → 互斥判定命中。
        """
        adv = _normalize_adv_id(adv_id)
        device_key = (device_id + (ua or "")).strip()[:96]
        ip_key = (ip or "").strip()
        return (adv, device_key, ip_key)

    def can_acquire(
        self,
        adv_id: str,
        device_id: str,
        ip: str,
        ua: Optional[str] = None,
        *,
        persist: bool = True,
    ) -> Tuple[bool, str]:
        """
        返回 (是否允许获取该设备×IP资源, 拒绝原因)。
        adv_id 为空/占位时归一为 '__default_adv__'，隔离判定依然严格生效（不再直接放行）。
        """
        adv, device_key, ip_key = self.derive_keys(adv_id, device_id, ip, ua)

        now = time.time()
        with self._lock:
            self._gc_locked()
            pairs: List[Tuple[str, Tuple[str, str]]] = [
                ("di", (device_key, ip_key)),
                ("ia", (ip_key, adv)),
                ("ad", (adv, device_key)),
            ]
            for name, key in pairs:
                last = {
                    "di": self._di,
                    "ia": self._ia,
                    "ad": self._ad,
                }[name].get(key, 0.0)
                if last and now - last < self.TTL_PAIR:
                    remain = (self.TTL_PAIR - (now - last)) / 3600
                    reason = (
                        f"P0-4:{name}冲突 adv={adv} 剩余{remain:.1f}h"
                    )
                    _decision_log("P0-4 reject", reason=reason)
                    return False, reason
            # 写入
            self._di[(device_key, ip_key)] = now
            self._ia[(ip_key, adv)] = now
            self._ad[(adv, device_key)] = now
        if persist:
            self._save()
        _decision_log("P0-4 allow", adv=adv, ip=ip_key)
        return True, ""

    def _gc_locked(self) -> None:
        now = time.time()
        self._di = {k: v for k, v in self._di.items() if now - v < self.TTL_PAIR}
        self._ia = {k: v for k, v in self._ia.items() if now - v < self.TTL_PAIR}
        self._ad = {k: v for k, v in self._ad.items() if now - v < self.TTL_PAIR}


adv_isolation = _AdvIsolation()


# ========================================================================== #
#  P1-1: CTR 熔断 + 目标时区时段分布过滤
# ========================================================================== #
class _CTRFuse:
    """
    Search 渠道 CTR 上限 9.5%；Display 渠道 CTR 上限 14%。
    滑动窗口：最近 1000 次曝光或最近 6 小时（取先到的）。
    超过阈值 → allow_next_click 返回 False，任务跳过此广告点击。
    """

    CHANNEL_LIMIT = {"search": 0.095, "display": 0.14, "default": 0.12}
    WINDOW_SEC = 6 * 3600
    WINDOW_IMP = 1000

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # adv -> channel -> deque[(ts, imp_delta, click_delta)]
        self._q: Dict[str, Dict[str, Deque[Tuple[float, int, int]]]] = defaultdict(
            lambda: defaultdict(deque)
        )

    def record_imp_click(
        self, adv_id: str, imp: int = 0, click: int = 0, channel: str = "default"
    ) -> None:
        adv_id = str(adv_id or "global").strip()
        channel = (channel or "default").lower()
        if not imp and not click:
            return
        now = time.time()
        with self._lock:
            self._q[adv_id][channel].append((now, int(imp), int(click)))
            self._trim_locked(adv_id, channel)

    def allow_next_click(
        self, adv_id: str, channel: str = "default", *, window_imp: int = 80
    ) -> Tuple[bool, Dict[str, float]]:
        """
        window_imp: 仅当累计曝光 ≥ 此值才启用熔断（小样本噪声大）
        返回 (是否允许, 指标{imp,click,ctr,limit})
        """
        adv_id = str(adv_id or "global").strip()
        channel = (channel or "default").lower()
        limit = self.CHANNEL_LIMIT.get(channel, self.CHANNEL_LIMIT["default"])
        with self._lock:
            self._trim_locked(adv_id, channel)
            q = self._q[adv_id][channel]
            imp = sum(e[1] for e in q)
            click = sum(e[2] for e in q)
        ctr = click / imp if imp else 0.0
        metrics = {"imp": float(imp), "click": float(click), "ctr": ctr, "limit": limit}
        if imp >= window_imp and ctr > limit:
            _decision_log("P1-1 CTR熔断", adv=adv_id, **metrics)
            return False, metrics
        return True, metrics

    def _trim_locked(self, adv_id: str, channel: str) -> None:
        now = time.time()
        q = self._q[adv_id][channel]
        while q and (now - q[0][0] > self.WINDOW_SEC or len(q) > self.WINDOW_IMP):
            q.popleft()


ctr_fuse = _CTRFuse()


# 目标时区时段分布：过滤掉当地 0:00-5:59 的访问（真实用户极少），
# 并按当地小时对任务开始概率加权（工作时段高、凌晨低）。
class _TZScheduler:
    # 各小时的相对概率权重（简单近似真实人类活动分布）
    HOUR_WEIGHTS = [
        0.02, 0.01, 0.01, 0.01, 0.02, 0.04,  # 00-05
        0.12, 0.28, 0.55, 0.78, 0.92, 1.00,  # 06-11
        0.96, 0.88, 0.72, 0.68, 0.82, 0.95,  # 12-17
        1.00, 0.86, 0.62, 0.40, 0.22, 0.10,  # 18-23
    ]

    def allow_now(
        self, tz_name: Optional[str], *, threshold: float = 0.08, seed: Optional[int] = None
    ) -> Tuple[bool, float, int]:
        """
        返回 (是否允许启动任务, 当前小时权重, 当地小时(0-23))
        threshold: 当地权重低于此值时直接拒绝（0-5 点天然低于 0.05 → 被过滤）
        """
        local_hour, w = self._weight(tz_name)
        rng = random.Random(seed or time.time_ns())
        allow = w >= threshold and rng.random() < (w + 0.05)
        if not allow:
            _decision_log(
                "P1-1 时段过滤", tz=tz_name, hour=local_hour, weight=w
            )
        return allow, w, local_hour

    def _weight(self, tz_name: Optional[str]) -> Tuple[int, float]:
        local_hour = self._local_hour(tz_name)
        return local_hour, self.HOUR_WEIGHTS[local_hour]

    @staticmethod
    def _local_hour(tz_name: Optional[str]) -> int:
        if not tz_name:
            tz_name = "UTC"
        try:
            from datetime import timezone as dt_tz
            from zoneinfo import ZoneInfo  # type: ignore[attr-defined]

            tz = ZoneInfo(tz_name)
            return datetime.now(tz).hour
        except Exception:
            # 老 Python 或无 zoneinfo：根据名字猜 UTC 偏移（粗粒度兜底）
            off = _tz_offset_hour_fallback(tz_name)
            utc_h = datetime.utcnow().hour
            return (utc_h + round(off)) % 24


def _tz_offset_hour_fallback(tz: str) -> int:
    # 只覆盖常用时区，避免依赖
    mapping = {
        "America/New_York": -5,
        "America/Chicago": -6,
        "America/Denver": -7,
        "America/Los_Angeles": -8,
        "Europe/London": 0,
        "Europe/Berlin": 1,
        "Europe/Paris": 1,
        "Europe/Moscow": 3,
        "Asia/Shanghai": 8,
        "Asia/Tokyo": 9,
        "Asia/Seoul": 9,
        "Asia/Singapore": 8,
        "Australia/Sydney": 10,
    }
    return mapping.get(tz, 0)


tz_schedule = _TZScheduler()


# ========================================================================== #
#  P1-2: Profile 持久化 + 25% 回访 (D+2 / D+7 / D+14 入队)
# ========================================================================== #
@dataclass
class UserProfile:
    fp_id: str
    created_at: float = field(default_factory=time.time)
    last_visit_ts: float = 0.0
    # host -> list of visit_timestamps
    host_visits: Dict[str, List[float]] = field(default_factory=dict)
    # 上次停留时长(秒)、滚动深度(0-1)、点击次数 — 下次回访保持一致性
    last_dwell: float = 0.0
    last_scroll: float = 0.0
    last_clicks: int = 0
    # 回访标记：系统标记的"应该回访"队列
    revisit_schedule: Dict[str, List[float]] = field(default_factory=dict)


class _ProfileStore:
    """持久化 FP → UserProfile，文件：.risk_state/profiles/<fp_id[:2]/<fp_id>.json"""

    MAX_PROFILES = 20_000

    def __init__(self) -> None:
        self._root = STATE_DIR / "profiles"
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # 内存 LRU：只存最近 2000 个，减少磁盘 IO
        self._lru: Dict[str, UserProfile] = {}
        self._lru_order: Deque[str] = deque()

    def _path(self, fp_id: str) -> pathlib.Path:
        safe = re.sub(r"[^A-Za-z0-9_\-]", "_", str(fp_id))[:64] or "anon"
        prefix = safe[:2].ljust(2, "_")
        return self._root / prefix / f"{safe}.json"

    def load(self, fp_id: str) -> Optional[UserProfile]:
        if not fp_id:
            return None
        with self._lock:
            if fp_id in self._lru:
                self._lru_order.remove(fp_id)
                self._lru_order.append(fp_id)
                return self._lru[fp_id]
        p = self._path(fp_id)
        if not p.exists():
            return None
        try:
            with p.open("r", encoding="utf-8") as f:
                d = json.load(f)
            prof = UserProfile(**d)
            with self._lock:
                self._lru[fp_id] = prof
                self._lru_order.append(fp_id)
                self._evict_locked()
            return prof
        except Exception:
            return None

    def save(self, fp_id: str, prof: UserProfile) -> None:
        if not fp_id:
            return
        p = self._path(fp_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(p) + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(asdict(prof), f, ensure_ascii=False)
            os.replace(tmp, p)
        except Exception as e:
            _log.warning("P1-2 profile 保存失败 fp=%s: %s", fp_id[:16], e)
        with self._lock:
            self._lru[fp_id] = prof
            if fp_id in self._lru_order:
                self._lru_order.remove(fp_id)
            self._lru_order.append(fp_id)
            self._evict_locked()

    def record_visit(
        self,
        fp_id: str,
        host: str,
        *,
        dwell_sec: float,
        scroll_depth: float,
        clicks: int,
        schedule_revisit_days: Sequence[int] = (2, 7, 14),
    ) -> UserProfile:
        host = _host(host) or host
        prof = self.load(fp_id) or UserProfile(fp_id=fp_id)
        now = time.time()
        prof.last_visit_ts = now
        prof.host_visits.setdefault(host, []).append(now)
        prof.last_dwell = float(dwell_sec)
        prof.last_scroll = float(max(0.0, min(1.0, scroll_depth)))
        prof.last_clicks = int(clicks)
        # 25% 回访概率：随机抽一组天数入队
        if random.random() < 0.25:
            ds = random.choice([[2], [7], [14], [2, 7], [2, 14], [7, 14], [2, 7, 14]])
            prof.revisit_schedule.setdefault(host, [])
            for d in ds:
                prof.revisit_schedule[host].append(now + d * 86400)
            _decision_log(
                "P1-2 回访入队", fp=fp_id[:16], host=host, days=ds
            )
        self.save(fp_id, prof)
        return prof

    def _evict_locked(self) -> None:
        while len(self._lru_order) > 2000:
            k = self._lru_order.popleft()
            self._lru.pop(k, None)


profile_store = _ProfileStore()


class _RevisitDecider:
    """
    决定「这个 FP 是否应该回访这个 host」。
    策略：
      - 若存在 revisit_schedule 到期项 → (True, schedule)
      - 若 host 历史访问过且随机 <0.25 → 偶尔自然回访
    """

    def should_revisit(
        self, host: str, fp_id: str, *, prof: Optional[UserProfile] = None
    ) -> Tuple[bool, str]:
        host = _host(host) or host
        if not fp_id or not host:
            return False, ""
        p = prof if prof is not None else profile_store.load(fp_id)
        if p is None:
            return False, "新profile无历史"
        now = time.time()
        sched = p.revisit_schedule.get(host) or []
        due = [s for s in sched if s <= now]
        if due:
            # 清理已到期，避免下次还命中
            sched_left = [s for s in sched if s > now]
            p.revisit_schedule[host] = sched_left
            profile_store.save(fp_id, p)
            return True, f"P1-2 schedule到期 due={len(due)}"
        # 自然回访：访问过的 host 再给 8% 概率
        if p.host_visits.get(host):
            if random.random() < 0.08:
                return True, "P1-2 自然回访"
        return False, ""


revisit = _RevisitDecider()


# ========================================================================== #
#  P1-3: Battery 线性衰减 + 移动 DeviceMotion 仿真
# ========================================================================== #
class _BatterySimulator:
    """
    每个 device_id 维护一个电池曲线：
      - 初始 40~98% 之间均匀
      - 每秒衰减约 (8~15%) / 小时（线性 + 小幅随机漂移）
      - 20% 以下再降低 0.5 倍（省电模式）
    """

    DECAY_PER_HOUR = (0.08, 0.15)

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._st: Dict[str, Tuple[float, float]] = {}  # (ts_anchor, level_at_anchor)

    def get_level(self, device_id: str, ts: Optional[float] = None) -> Dict[str, Any]:
        ts = time.time() if ts is None else float(ts)
        with self._lock:
            if device_id not in self._st:
                anchor = ts - random.uniform(0, 3600)
                level = random.uniform(0.40, 0.98)
                self._st[device_id] = (anchor, level)
            anchor, lvl0 = self._st[device_id]
        dt_h = max(0.0, (ts - anchor) / 3600.0)
        decay = random.uniform(*self.DECAY_PER_HOUR)
        if lvl0 < 0.20:
            decay *= 0.5
        lvl = max(0.02, lvl0 - decay * dt_h - random.uniform(0, 0.004))
        charging = random.random() < 0.08 and lvl < 0.85
        if charging:
            lvl = min(0.99, lvl + random.uniform(0.002, 0.01))
        return {
            "level": round(lvl, 4),
            "charging": charging,
            "level_pct": int(round(lvl * 100)),
        }


battery = _BatterySimulator()


class _MotionSimulator:
    """生成 n 个 Acceleration(x,y,z) 样本，模拟真实手机晃动的钟形噪声。"""

    def make_accel(self, n: int = 128, *, seed: Optional[int] = None) -> List[Dict[str, float]]:
        rng = random.Random(seed or secrets.randbelow(2**31))
        out: List[Dict[str, float]] = []
        # 低频分量：一次随机游走
        bx, by, bz = 0.0, 0.0, 0.0
        for _ in range(n):
            bx = max(-2.5, min(2.5, bx + rng.uniform(-0.12, 0.12)))
            by = max(-2.5, min(2.5, by + rng.uniform(-0.12, 0.12)))
            bz = max(-2.5, min(2.5, bz + rng.uniform(-0.12, 0.12)))
            # 高频：高斯噪声
            gx = rng.gauss(0.0, 0.25)
            gy = rng.gauss(0.0, 0.25)
            gz = rng.gauss(0.35, 0.3)  # z 轴永远有重力偏置
            out.append(
                {
                    "x": round(bx + gx, 4),
                    "y": round(by + gy, 4),
                    "z": round(bz + gz + 9.8, 4),  # 加上重力
                }
            )
        return out


motion = _MotionSimulator()


def build_sensor_dynamic_script(seed: int) -> str:
    """
    P2-13 传感器动态仿真 JS 生成器。

    生成一段可直接注入浏览器 init_script 的 JS 字符串，实现：
      1. Battery API：每 30~60 秒随机更新 level（缓慢下降），
         并派发 levelchange / chargingchange 事件；
         重写 addEventListener / removeEventListener 真实维护监听列表。
      2. DeviceMotionEvent：以 ~60Hz（16ms 间隔）持续派发 devicemotion 事件，
         accelerationIncludingGravity 含随机游走 + 高频噪声。
      3. 监听器机制：BatteryManager 与 window 的 addEventListener/removeEventListener
         均真实维护列表并派发事件。

    seed 用于确定性生成初始参数（初始电量、初始充电状态、随机游走初值等），
    保证同 seed 输出稳定。

    注意：本函数仅生成纯 JS 字符串，不涉及浏览器启动。
    """
    rng = random.Random(int(seed))
    init_level = round(rng.uniform(0.40, 0.98), 4)
    init_charging = rng.random() < 0.15
    init_bx = round(rng.uniform(-0.5, 0.5), 4)
    init_by = round(rng.uniform(-0.5, 0.5), 4)
    init_bz = round(rng.uniform(-0.5, 0.5), 4)
    decay_per_hour = round(rng.uniform(0.08, 0.15), 4)
    rng_seed = rng.randint(1, 2**31 - 1)

    js = f"""
(function() {{
  // ---------- 简易确定性伪随机（基于 seed） ----------
  var _rngState = {rng_seed};
  function _rand() {{
    _rngState = (_rngState * 1664525 + 1013904223) >>> 0;
    return _rngState / 4294967296;
  }}
  function _randRange(a, b) {{ return a + _rand() * (b - a); }}
  function _gauss() {{
    // Box-Muller
    var u = 0, v = 0;
    while (u === 0) u = _rand();
    while (v === 0) v = _rand();
    return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
  }}

  // ---------- Battery API 动态仿真 ----------
  var _battery = {{
    level: {init_level},
    charging: {str(init_charging).lower()},
    chargingTime: {init_charging and '3600' or 'Infinity'},
    dischargingTime: {init_charging and 'Infinity' or '18000'},
    _listeners: {{ levelchange: [], chargingchange: [], chargingtimechange: [], dischargingtimechange: [] }},
    addEventListener: function(type, fn) {{
      if (!fn || typeof fn !== 'function') return;
      if (!this._listeners[type]) this._listeners[type] = [];
      if (this._listeners[type].indexOf(fn) === -1) this._listeners[type].push(fn);
    }},
    removeEventListener: function(type, fn) {{
      if (!this._listeners[type]) return;
      var idx = this._listeners[type].indexOf(fn);
      if (idx !== -1) this._listeners[type].splice(idx, 1);
    }},
    dispatchEvent: function(evt) {{
      var list = this._listeners[evt.type] || [];
      for (var i = 0; i < list.length; i++) {{
        try {{ list[i].call(this, evt); }} catch (e) {{ /* 忽略监听器错误 */ }}
      }}
      // 兼容 onxxx 回调
      var onHandler = this['on' + evt.type];
      if (typeof onHandler === 'function') {{
        try {{ onHandler.call(this, evt); }} catch (e) {{}}
      }}
      return true;
    }}
  }};

  function _makeBatteryEvent(type) {{
    var evt = document.createEvent('Event');
    evt.initEvent(type, false, false);
    return evt;
  }}

  var _decayPerHour = {decay_per_hour};
  var _lastBatteryTick = Date.now();
  function _batteryTick() {{
    var now = Date.now();
    var dtHours = (now - _lastBatteryTick) / 3600000;
    _lastBatteryTick = now;
    var oldLevel = _battery.level;
    var oldCharging = _battery.charging;

    if (_battery.charging) {{
      _battery.level = Math.min(1.0, _battery.level + _randRange(0.002, 0.01) + _decayPerHour * dtHours);
      if (_battery.level >= 0.99) {{
        _battery.charging = false;
        _battery.chargingTime = Infinity;
        _battery.dischargingTime = 18000;
      }}
    }} else {{
      var decay = _decayPerHour * dtHours + _randRange(0, 0.004);
      if (_battery.level < 0.20) decay *= 0.5;
      _battery.level = Math.max(0.02, _battery.level - decay);
      // 8% 概率切换到充电状态（仅当电量 < 85%）
      if (_rand() < 0.08 && _battery.level < 0.85) {{
        _battery.charging = true;
        _battery.chargingTime = 3600;
        _battery.dischargingTime = Infinity;
      }}
    }}

    if (Math.abs(_battery.level - oldLevel) > 1e-6) {{
      _battery.dispatchEvent(_makeBatteryEvent('levelchange'));
    }}
    if (_battery.charging !== oldCharging) {{
      _battery.dispatchEvent(_makeBatteryEvent('chargingchange'));
    }}

    // 下一次 30~60 秒后
    var nextDelay = _randRange(30000, 60000);
    setTimeout(_batteryTick, nextDelay);
  }}
  // 首次延迟 2~5 秒启动
  setTimeout(_batteryTick, _randRange(2000, 5000));

  // 覆盖 navigator.getBattery（若存在则替换，不存在则 polyfill）
  if (navigator.getBattery) {{
    navigator.getBattery = function() {{
      return Promise.resolve(_battery);
    }};
  }} else {{
    navigator.getBattery = function() {{
      return Promise.resolve(_battery);
    }};
  }}

  // ---------- DeviceMotion 动态仿真 ----------
  var _bx = {init_bx};
  var _by = {init_by};
  var _bz = {init_bz};

  function _clamp(v, lo, hi) {{ return v < lo ? lo : (v > hi ? hi : v); }}

  function _emitMotionEvent() {{
    // 低频随机游走
    _bx = _clamp(_bx + _randRange(-0.12, 0.12), -2.5, 2.5);
    _by = _clamp(_by + _randRange(-0.12, 0.12), -2.5, 2.5);
    _bz = _clamp(_bz + _randRange(-0.12, 0.12), -2.5, 2.5);
    // 高频高斯噪声
    var gx = _gauss() * 0.25;
    var gy = _gauss() * 0.25;
    var gz = _gauss() * 0.3 + 0.35;

    var accel = {{
      x: null, y: null, z: null
    }};
    var accelGrav = {{
      x: +(_bx + gx).toFixed(4),
      y: +(_by + gy).toFixed(4),
      z: +(_bz + gz + 9.8).toFixed(4)
    }};
    var rotRate = {{
      alpha: +(_gauss() * 0.5).toFixed(4),
      beta: +(_gauss() * 0.5).toFixed(4),
      gamma: +(_gauss() * 0.5).toFixed(4)
    }};

    var evt;
    try {{
      evt = new DeviceMotionEvent('devicemotion', {{
        acceleration: accel,
        accelerationIncludingGravity: accelGrav,
        rotationRate: rotRate,
        interval: 16
      }});
    }} catch (e) {{
      // 兼容旧浏览器
      evt = document.createEvent('Event');
      evt.initEvent('devicemotion', false, false);
      evt.acceleration = accel;
      evt.accelerationIncludingGravity = accelGrav;
      evt.rotationRate = rotRate;
      evt.interval = 16;
    }}

    window.dispatchEvent(evt);
  }}

  // 以 ~60Hz 派发
  setInterval(_emitMotionEvent, 16);
}})();
"""
    return js


# ========================================================================== #
#  P1-4: GA4 / AdSense / ActiveView 自检
# ========================================================================== #
class _AdsSelfCheck:
    """
    给定 Playwright Page（或 mock dict），检查是否存在关键脚本。
    返回 (passed: bool, details: dict)。
    对真实 Page：调用 page.evaluate 在 DOM 里查 selector / window 全局对象。
    对非 Playwright 调用方：接受 page 为 dict，用于 mock/test。
    """

    MIN_ACTIVEVIEW_MS = 1000

    def run(self, page: Any) -> Tuple[bool, Dict[str, Any]]:
        details = {
            "ga4_found": False,
            "adsense_found": False,
            "activeview_ok": False,
            "ad_slots_visible": 0,
            "total_ad_slots": 0,
        }
        # --- 1. 尝试 Playwright 页面 --- #
        is_pw = hasattr(page, "evaluate")
        if is_pw:
            try:
                r = page.evaluate(self._CHECK_JS)
                details.update(r or {})
            except Exception as e:
                _log.warning("P1-4 Playwright 自检失败: %s", e)
                details["error"] = str(e)
        elif isinstance(page, dict):
            # mock 调用：page 里直接给字段
            for k in details:
                if k in page:
                    details[k] = page[k]

        passed = (
            details["ad_slots_visible"] > 0
            and (details["activeview_ok"] or details["adsense_found"] or details["ga4_found"])
        )
        if not passed:
            _decision_log("P1-4 fail", **details)
        return passed, details

    @property
    def _CHECK_JS(self) -> str:
        return """
        (() => {
          const hasGA4 = !!document.querySelector('script[src*="googletagmanager.com/gtag/js"]') ||
            !!document.querySelector('script[src*="gtag/js?id="]') ||
            (typeof window.dataLayer !== 'undefined' && Array.isArray(window.dataLayer));
          const hasAdSense = !!document.querySelector('script[src*="pagead2.googlesyndication.com/pagead/js/adsbygoogle.js"]') ||
            (typeof window.adsbygoogle !== 'undefined');
          const slots = Array.from(document.querySelectorAll('ins.adsbygoogle, iframe[src*="googleads"], div[id^="google_ads"]'));
          const vis = slots.filter(el => {
            const r = el.getBoundingClientRect();
            if (r.width < 1 || r.height < 1) return false;
            const intersectW = Math.max(0, Math.min(r.right, window.innerWidth) - Math.max(r.left, 0));
            const intersectH = Math.max(0, Math.min(r.bottom, window.innerHeight) - Math.max(r.top, 0));
            const overlapPct = (intersectW * intersectH) / Math.max(1, r.width * r.height);
            return overlapPct >= 0.5;
          }).length;
          // ActiveView = 至少有一个可见槽（时间要求由调用方 sleep 保证）
          return {
            ga4_found: !!hasGA4,
            adsense_found: !!hasAdSense,
            activeview_ok: vis > 0,
            ad_slots_visible: vis,
            total_ad_slots: slots.length
          };
        })();
        """


ads_selfcheck = _AdsSelfCheck()


# ========================================================================== #
#  P1-5: Bounce / Pages / Engagement 联合分布 Copula 采样（简化版高斯 Copula）
# ========================================================================== #
class _BehaviorCopula:
    """
    三维联合分布：
      bounce  ~ Beta(α=1.4, β=2.8)   → 均值 0.33（33%跳出率）
      pages   ~ Gamma(k=2.6, θ=1.8)  → 均值 4.7 PV
      eng     ~ Gamma(k=5.2, θ=22)   → 均值 ~114s 平均互动时长
    相关性矩阵（Copula）：
      bounce × pages   = -0.62（页越少越容易跳出）
      bounce × eng     = -0.78（停留越短越容易跳出）
      pages  × eng     = +0.71（页越多停留越长）
    """

    RHO = [[1.0, -0.62, -0.78], [-0.62, 1.0, 0.71], [-0.78, 0.71, 1.0]]
    # 在 class body 显式声明，支持静态属性懒写
    _L: Optional[List[List[float]]] = None
    _lock_cls: Optional[threading.Lock] = None

    def __init__(self) -> None:
        self._l = None  # Cholesky 分解
        self._lock = threading.Lock()
        # 审计修复：独立 RNG 实例，避免 random.seed 污染全局随机流（影响其他调用方）
        self._rng = random.Random()
        # host-country 分层覆盖：每个 (host,cc) 单独维护一个小样本后验，
        # 初始先验就是全局参数，有数据后慢慢偏到后验。
        self._host_stats: Dict[Tuple[str, str], List[Tuple[float, int, float]]] = defaultdict(list)

    @staticmethod
    def _chol() -> List[List[float]]:
        with _BehaviorCopula._static_lock():
            if _BehaviorCopula._L is not None:
                return list(_BehaviorCopula._L)
            try:
                import numpy as np  # type: ignore

                r = np.array(_BehaviorCopula.RHO, dtype=float)
                arr = np.linalg.cholesky(r).tolist()
            except Exception:
                arr = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
            _BehaviorCopula._L = arr
            return list(arr)

    @staticmethod
    def _static_lock() -> threading.Lock:
        # 懒创建 class-level 锁
        if _BehaviorCopula._lock_cls is None:
            _BehaviorCopula._lock_cls = threading.Lock()
        return _BehaviorCopula._lock_cls  # type: ignore[return-value]

    def _gauss_sample(self, n: int = 3, rng: Optional[random.Random] = None) -> List[float]:
        # Box-Muller：避免依赖 numpy
        # 审计修复：改用独立 RNG 实例（不触碰全局 random 流），rng 为空时用实例内置 self._rng
        rng = rng or self._rng
        out = []
        for _ in range((n + 1) // 2):
            u1 = max(1e-12, rng.random())
            u2 = rng.random()
            z0 = math.sqrt(-2 * math.log(u1)) * math.cos(2 * math.pi * u2)
            z1 = math.sqrt(-2 * math.log(u1)) * math.sin(2 * math.pi * u2)
            out.extend([z0, z1])
        return out[:n]

    @staticmethod
    def _norm_cdf(x: float) -> float:
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    @staticmethod
    def _beta_ppf(u: float, a: float, b: float) -> float:
        # 数值求 Beta 分位（慢但足够）
        try:
            from scipy.stats import beta as _beta  # type: ignore

            return float(_beta.ppf(max(1e-9, min(1 - 1e-9, u)), a, b))
        except Exception:
            # 二项式反推的启发式：均值 a/(a+b)、范围 u 做线性伸缩
            mu = a / (a + b)
            k = mu * (1 - mu) / max(1e-6, (a + b + 1))
            sig = math.sqrt(k)
            return max(0.0, min(1.0, mu + (u - 0.5) * 2.5 * sig))

    @staticmethod
    def _gamma_ppf(u: float, a: float, scale: float) -> float:
        try:
            from scipy.stats import gamma as _gamma  # type: ignore

            return float(_gamma.ppf(max(1e-9, min(1 - 1e-9, u)), a=a, scale=scale))
        except Exception:
            # 高斯近似 + 切尾
            mu = a * scale
            sigma = math.sqrt(a) * scale
            q = math.sqrt(2) * _inv_erf(2 * u - 1)
            return max(0.05, mu + q * sigma)

    # ---------- 对外 API ---------- #
    def sample_behavior(
        self,
        host: str = "",
        country: str = "",
        *,
        seed: Optional[int] = None,
    ) -> Dict[str, float]:
        """
        返回 {bounce_prob, pages (int), engagement_sec}。
        bounce_prob ∈ [0,1]：调用方可用 random.random() < bounce_prob 决定是否跳出；
        pages：本次访问模拟浏览多少页；
        engagement_sec：总互动秒数（在调用方 split 到每页即可）。
        """
        if seed is not None:
            # 审计修复：seed 用独立 Random 实例，不再 random.seed 污染全局随机流
            rng = random.Random(seed)
        else:
            rng = None
        L = self._chol()
        z0 = self._gauss_sample(3, rng)
        # 关联
        z = [
            L[0][0] * z0[0],
            L[1][0] * z0[0] + L[1][1] * z0[1],
            L[2][0] * z0[0] + L[2][1] * z0[1] + L[2][2] * z0[2],
        ]
        u = [self._norm_cdf(x) for x in z]
        bounce = self._beta_ppf(u[0], 1.4, 2.8)
        pages = self._gamma_ppf(u[1], 2.6, 1.8)
        eng = self._gamma_ppf(u[2], 5.2, 22.0)
        # ---- host-country 后验微调 ---- #
        key = (str(host or "").strip()[:64], str(country or "").strip()[:8])
        # 审计修复：加锁读取 _host_stats，避免与 record_observed 并发写造成竞态
        with self._lock:
            data = list(self._host_stats[key])
        if len(data) >= 20:
            # 对 prior 和经验均值做 EMA（α=0.3）
            b_emp = sum(1 for d in data if d[0] < 0.35) / len(data)
            p_emp = sum(d[1] for d in data) / len(data)
            e_emp = sum(d[2] for d in data) / len(data)
            bounce = 0.7 * bounce + 0.3 * b_emp
            pages = 0.7 * pages + 0.3 * p_emp
            eng = 0.7 * eng + 0.3 * e_emp
        pages_i = max(1, int(round(pages)))
        bounce = max(0.0, min(0.98, bounce))
        eng = max(5.0, float(eng))
        return {"bounce_prob": round(bounce, 4), "pages": pages_i, "engagement_sec": round(eng, 1)}

    def record_observed(
        self, host: str, country: str, bounce: float, pages: int, engagement: float
    ) -> None:
        key = (str(host or "").strip()[:64], str(country or "").strip()[:8])
        # 审计修复：加锁写入，避免与 sample_behavior 并发读造成竞态
        with self._lock:
            self._host_stats[key].append((float(bounce), int(pages), float(engagement)))
        if len(self._host_stats[key]) > 2000:
            self._host_stats[key] = self._host_stats[key][-1000:]


def _inv_erf(x: float) -> float:
    """Winitzki 近似（±1e-3 精度，够分位数用了）"""
    a = 0.147
    sgn = 1 if x >= 0 else -1
    x_ = abs(x)
    ln1 = math.log(1 - x_ * x_)
    t1 = 2 / (math.pi * a) + ln1 / 2
    t2 = ln1 / a
    return sgn * math.sqrt(math.sqrt(t1 * t1 - t2) - t1)


copula = _BehaviorCopula()


# ========================================================================== #
#  P2-1: 曝光 CV + 周模式
#  P2-2: 指纹独立种子
#  P2-3: DNS 分散
#  P2-4: 跳转 3 层漏斗 + CPL 仿真
#  P2-5: ICR 监控
# ========================================================================== #
class _ExposureCV:
    """
    监控每个 host 在过去 7 天的日曝光量 CV（标准差/均值）。
    CV > 1.2 被认为"忽上忽下"，此时降低 30% 任务注入率（软限流）。
    同时内置周内分布（工作日/周末）基准，偏离基准 >1.5σ 同样限流。
    """

    WINDOW_DAYS = 7
    CV_LIMIT = 1.2
    # 每周 7 天的曝光占比（经验），周一略高、周末略低
    WEEKLY_PROFILE = [0.155, 0.162, 0.160, 0.150, 0.145, 0.115, 0.113]

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # host -> deque[(date_int, imp_count)]  每个 day 累计曝光
        self._daily: Dict[str, Deque[Tuple[int, int]]] = defaultdict(deque)

    @staticmethod
    def _today_int() -> int:
        """返回自 2024-01-01 起的天数，作为日期整数键（跨月/跨年都单调递增）。"""
        return (datetime.now().date() - datetime(2024, 1, 1).date()).days

    def record_impression(self, host: str, imp: int = 1) -> None:
        host = _host(host) or host
        today = self._today_int()
        with self._lock:
            dq = self._daily[host]
            if dq and dq[-1][0] == today:
                d, c = dq.pop()
                dq.append((d, c + imp))
            else:
                dq.append((today, imp))
            # 清理过期
            while dq and (today - dq[0][0] > self.WINDOW_DAYS):
                dq.popleft()

    def allow(self, host: str) -> Tuple[bool, float]:
        """返回 (是否不过载/正常, CV值)；False 表示应限流 30% 左右。"""
        host = _host(host) or host
        with self._lock:
            dq = list(self._daily.get(host, []))
        if len(dq) < 4:
            return True, 0.0
        counts = [c for _, c in dq]
        mu = sum(counts) / len(counts)
        if mu <= 0:
            return True, 0.0
        var = sum((x - mu) ** 2 for x in counts) / len(counts)
        cv = math.sqrt(var) / mu
        # 周模式检测：当前是周几？相对占比是否在 [0.3x, 2.2x] 之间
        wd = datetime.now().weekday()
        expected_ratio = self.WEEKLY_PROFILE[wd] / (1 / 7)
        today_count = counts[-1]
        today_ratio = (today_count / (mu * len(counts))) / (1 / len(counts)) if counts else 1.0
        weekly_bias = today_ratio / max(0.01, expected_ratio)
        overload = cv > self.CV_LIMIT or weekly_bias > 2.2 or weekly_bias < 0.4
        if overload:
            _decision_log(
                "P2-1 曝光模式异常", host=host, cv=round(cv, 3), weekly_bias=round(weekly_bias, 3)
            )
        return not overload, cv


exposure_cv = _ExposureCV()


class _FingerprintSeed:
    """
    每个 fp_id 绑定一个稳定种子：WebGL noise、canvas noise、audio noise 都由它派生。
    保证同 fp_id 在 30 天内产生完全一致的"指纹特征噪声"。
    """

    TTL = 30 * 86400

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: Dict[str, Tuple[int, float]] = {}
        self._file = STATE_DIR / "fp_seeds.json"
        self._load()

    def _load(self) -> None:
        if not self._file.exists():
            return
        try:
            with self._file.open("r", encoding="utf-8") as f:
                d = json.load(f)
            self._cache = {k: (int(v[0]), float(v[1])) for k, v in d.items()}
        except Exception as e:
            _log.warning("指纹种子加载失败: %s", e)

    def _save(self) -> None:
        tmp = str(self._file) + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {k: [v[0], v[1]] for k, v in self._cache.items()},
                    f,
                    ensure_ascii=False,
                )
            os.replace(tmp, self._file)
        except Exception:
            pass

    def get(self, fp_id: str) -> int:
        if not fp_id:
            return 0
        now = time.time()
        with self._lock:
            hit = self._cache.get(fp_id)
            if hit and (now - hit[1]) < self.TTL:
                return hit[0]
            # 新的 31-bit 种子（兼容 JS Int32 噪声）
            seed = secrets.randbelow(2**31)
            self._cache[fp_id] = (seed, now)
            # GC
            if len(self._cache) > 20000:
                items = sorted(self._cache.items(), key=lambda kv: kv[1][1], reverse=True)
                self._cache = dict(items[:15000])
        self._save()
        _decision_log("P2-2 生成种子", fp=fp_id[:16])
        return seed


fingerprint_seed = _FingerprintSeed()


def get_stable_canvas_seed(fp_id: str) -> int:
    """
    便捷纯函数：为 fp_id 返回稳定的 canvas 噪声种子（替代硬编码 12345）。

    保证：
      - 同 fp_id → 相同种子（30 天内稳定，见 _FingerprintSeed.get）；
      - 不同 fp_id → 绝大多数情况下不同种子（随机 31-bit 派生）；
      - 返回值恒为正 31-bit 非零整数，避免退化为固定基线。
    """
    seed = fingerprint_seed.get(fp_id)
    return (seed % 2 ** 31) or 1


class _DNSDiversity:
    """
    每个国家一个 DNS 池：Google / Cloudflare / OpenDNS / Quad9 / 本地运营商（混合）。
    实际调用时由 Selenium Bridge 的 --host-resolver-rules 接收（此处只做分配决策）。
    """

    POOL: Dict[str, List[str]] = {
        "US": ["8.8.8.8", "1.1.1.1", "208.67.222.222", "9.9.9.9", "64.6.64.6"],
        "GB": ["8.8.8.8", "1.1.1.1", "208.67.222.222", "9.9.9.9"],
        "JP": ["8.8.8.8", "1.1.1.1", "202.232.2.2", "203.141.128.33"],
        "DE": ["8.8.8.8", "1.1.1.1", "141.1.1.1", "9.9.9.9"],
        "FR": ["8.8.8.8", "1.1.1.1", "80.67.169.12", "9.9.9.9"],
        "CA": ["8.8.8.8", "1.1.1.1", "74.82.42.42", "9.9.9.9"],
        "AU": ["8.8.8.8", "1.1.1.1", "203.2.193.66", "9.9.9.9"],
        "SG": ["8.8.8.8", "1.1.1.1", "165.21.83.88", "9.9.9.9"],
    }
    FALLBACK = ["8.8.8.8", "1.1.1.1", "208.67.222.222"]

    def pick_resolver(self, country: str) -> List[str]:
        cc = (country or "").strip().upper()
        pool = list(self.POOL.get(cc, self.FALLBACK))
        random.shuffle(pool)
        return pool[:3]


dns_diversity = _DNSDiversity()


class _Funnel:
    """
    跳转 3 层：
      入口 (referer) → 站内中间页 A → 站内中间页 B → 目标页 (target)
    调用方输入 target URL，返回按访问顺序排列的路径列表；
    如果站点没有足够多的"中间页"，退化为层数缩减版。
    """

    # 常见"站内中间页"路径后缀（实际调用方可传自己的 sitemap，这里给 fallback）
    DEFAULT_CATEGORIES = [
        "/category/tech",
        "/category/business",
        "/category/life",
        "/tag/tips",
        "/archive",
        "/about",
        "/blog",
        "/news",
        "/products",
    ]

    def build_3layer(
        self,
        target: str,
        *,
        inner_pages: Optional[Sequence[str]] = None,
        layers: int = 3,
    ) -> List[str]:
        host = re.match(r"https?://[^/?#]+", target or "")
        host_prefix = host.group(0) if host else ""
        if not host_prefix:
            return [target]
        pool = list(inner_pages) if inner_pages else list(self.DEFAULT_CATEGORIES)
        random.shuffle(pool)
        path: List[str] = []
        # 前面 layers-1 层：中间页
        for _ in range(max(0, layers - 1)):
            if not pool:
                break
            p = pool.pop()
            if p.startswith("http"):
                path.append(p)
            else:
                path.append(host_prefix + p)
        path.append(target)
        _decision_log("P2-4 漏斗生成", layers=len(path), target=_host(target))
        return path


funnel = _Funnel()


class _CPLSimulator:
    """
    CPL 成本仿真：为漏斗每一页给出应停留的秒数。
    简单模型：
      每页 seconds ~ Gamma(k=1.6, θ=15) + 内容长度加成 (len/300 秒)
    """

    def simulate(
        self,
        pages: Sequence[str],
        *,
        contents_len: Optional[Sequence[int]] = None,
        seed: Optional[int] = None,
    ) -> List[float]:
        rng = random.Random(seed or secrets.randbelow(2**31))
        out: List[float] = []
        for i, _ in enumerate(pages):
            # Gamma via Marsaglia & Tsang
            shape = 1.6 + rng.uniform(-0.2, 0.4)
            scale = 15.0 * (0.85 if i == len(pages) - 1 else 1.0)  # 最后一页稍短
            x = self._gamma_sample(shape, rng) * scale
            if contents_len and i < len(contents_len):
                x += max(0, (contents_len[i] - 300) / 300) * 10.0
            out.append(round(max(3.0, x), 1))
        return out

    @staticmethod
    def _gamma_sample(a: float, rng: random.Random) -> float:
        if a < 1:
            return _CPLSimulator._gamma_sample(a + 1, rng) * (rng.random() ** (1 / a))
        d = a - 1 / 3
        c = 1 / math.sqrt(9 * d)
        while True:
            x = rng.gauss(0.0, 1.0)
            v = 1 + c * x
            if v <= 0:
                continue
            v3 = v * v * v
            u = rng.random()
            if u < 1 - 0.0331 * (x * x) ** 2:
                return d * v3
            if math.log(u) < 0.5 * x * x + d * (1 - v3 + math.log(v3)):
                return d * v3


cpl_simulator = _CPLSimulator()


class _ICRMonitor:
    """
    ICR = Invalid Click Rate（无效点击率）的滚动监控：
    统计最近 24 小时内：
      低停留 (dwell < 30s) 任务数 / 总任务数 = 无效率
      高跳出 (bounce > 0.8) 任务数 / 总任务数  = 跳失率
    任一指标超过阈值 → 建议系统暂停 + 人工介入。
    """

    WINDOW_SEC = 24 * 3600
    MAX_LOW_DWELL = 0.12  # 12% 以上低停留 → 告警
    MAX_HIGH_BOUNCE = 0.45  # 45% 以上高跳出 → 告警

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._q: Deque[Tuple[float, float, float]] = deque()  # (ts, dwell, bounce)
        self._file = STATE_DIR / "icr_history.jsonl"
        self._load()

    def _load(self) -> None:
        if not self._file.exists():
            return
        now = time.time()
        try:
            with self._file.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        ts, d, b = json.loads(line)
                        if now - float(ts) < self.WINDOW_SEC:
                            self._q.append((float(ts), float(d), float(b)))
                    except Exception:
                        continue
        except Exception:
            pass

    def record(self, ts: float, dwell: float, bounce: float) -> None:
        with self._lock:
            self._q.append((float(ts), float(dwell), float(bounce)))
            self._trim_locked()
        try:
            with self._file.open("a", encoding="utf-8") as f:
                f.write(json.dumps([ts, dwell, bounce]) + "\n")
        except Exception:
            pass

    def snapshot(self) -> Dict[str, float]:
        now = time.time()
        with self._lock:
            self._trim_locked(now)
            data = list(self._q)
        n = len(data)
        if n == 0:
            return {"n": 0.0, "low_dwell_rate": 0.0, "high_bounce_rate": 0.0}
        low = sum(1 for _, d, _ in data if d < 30.0)
        high_b = sum(1 for _, _, b in data if b > 0.8)
        return {
            "n": float(n),
            "low_dwell_rate": low / n,
            "high_bounce_rate": high_b / n,
        }

    def should_warn(self) -> Tuple[bool, Dict[str, float], str]:
        s = self.snapshot()
        if s["n"] < 30:
            return False, s, "样本不足"
        reasons = []
        if s["low_dwell_rate"] > self.MAX_LOW_DWELL:
            reasons.append(
                f"低停留率={s['low_dwell_rate']*100:.1f}% > {self.MAX_LOW_DWELL*100:.0f}%"
            )
        if s["high_bounce_rate"] > self.MAX_HIGH_BOUNCE:
            reasons.append(
                f"高跳出率={s['high_bounce_rate']*100:.1f}% > {self.MAX_HIGH_BOUNCE*100:.0f}%"
            )
        if reasons:
            _decision_log("P2-5 ICR告警", **{k: round(v, 3) for k, v in s.items()})
            return True, s, "; ".join(reasons)
        return False, s, ""

    def _trim_locked(self, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        while self._q and (now - self._q[0][0] > self.WINDOW_SEC):
            self._q.popleft()


icr_monitor = _ICRMonitor()


# --------------------------- 顶部便捷导出 ---------------------------------- #
__all__ = [
    "isolate_pool",
    "referer_guard",
    "semantic_sim",
    "adv_isolation",
    "ctr_fuse",
    "tz_schedule",
    "profile_store",
    "revisit",
    "battery",
    "motion",
    "ads_selfcheck",
    "copula",
    "exposure_cv",
    "fingerprint_seed",
    "get_stable_canvas_seed",
    "dns_diversity",
    "funnel",
    "cpl_simulator",
    "icr_monitor",
    "build_sensor_dynamic_script",
]


def _self_test() -> None:
    """模块自检（不依赖外网）：python -m risk_control_enhancements"""
    print("=== P0-1 isolate_pool ===")
    ok, reason = isolate_pool.allow("adv-1", "1.2.3.4", "fp-aaa", "ua1")
    print("allow1:", ok, reason)
    ok, reason = isolate_pool.allow("adv-1", "1.2.3.80", "fp-aaa", "ua1")
    print("allow2(same fp):", ok, reason)

    print("\n=== P0-2 referer ===")
    r = referer_guard.check_and_make("", "https://foo.com/blog/how-to-cook", kw="best cooker")
    print(r)

    print("\n=== P0-3 semantic ===")
    a = "Best wireless headphone review — noise cancelling under $200"
    b = (
        "In this article we test top rated noise cancelling wireless headphones, "
        "compare battery life and pick the best models under $200 for travel."
    )
    ok, s, reason = semantic_sim.allow(a, b, threshold=0.45)  # Jaccard 更容易命中
    print(f"score={s:.3f} allow={ok} reason={reason}")

    print("\n=== P0-4 adv_isolation ===")
    print(adv_isolation.can_acquire("adv-1", "dev1", "1.2.3.5"))
    print(adv_isolation.can_acquire("adv-1", "dev1", "1.2.3.5"))

    print("\n=== P1-1 CTR fuse + tz ===")
    for i in range(60):
        ctr_fuse.record_imp_click("a1", 1, 1 if i < 10 else 0, channel="search")
    print(ctr_fuse.allow_next_click("a1", "search"))
    print(tz_schedule.allow_now("Asia/Shanghai"))

    print("\n=== P1-2 profile + revisit ===")
    p = profile_store.record_visit(
        "fp1", "https://x.com/blog", dwell_sec=120, scroll_depth=0.8, clicks=3
    )
    print("saved profile:", p.fp_id, p.last_dwell, len(p.host_visits))
    print("revisit?", revisit.should_revisit("https://x.com/blog", "fp1"))

    print("\n=== P1-3 battery + motion ===")
    print(battery.get_level("device-1"))
    acc = motion.make_accel(5)
    print("motion samples:", acc)

    print("\n=== P1-4 ads_selfcheck (mock) ===")
    mock_page = {
        "ga4_found": True,
        "adsense_found": True,
        "activeview_ok": True,
        "ad_slots_visible": 2,
        "total_ad_slots": 3,
    }
    print(ads_selfcheck.run(mock_page))

    print("\n=== P1-5 copula ===")
    for _ in range(5):
        print(copula.sample_behavior("x.com", "US"))

    print("\n=== P2-1 exposure_cv ===")
    for _ in range(8):
        exposure_cv.record_impression("x.com", random.randint(20, 120))
    print(exposure_cv.allow("x.com"))

    print("\n=== P2-2 seed ===")
    s1 = fingerprint_seed.get("fp1")
    s2 = fingerprint_seed.get("fp1")
    print("seed stable?", s1 == s2, s1)

    print("\n=== P2-3 dns ===")
    print(dns_diversity.pick_resolver("US"))

    print("\n=== P2-4 funnel + cpl ===")
    path = funnel.build_3layer("https://x.com/target-page")
    print("path:", path)
    print("cpl secs:", cpl_simulator.simulate(path))

    print("\n=== P2-5 icr ===")
    for _ in range(40):
        icr_monitor.record(
            time.time() - random.uniform(0, 80000),
            random.uniform(10, 240),
            random.uniform(0, 1),
        )
    print(icr_monitor.should_warn())
    print("OK.")


if __name__ == "__main__":
    _self_test()
