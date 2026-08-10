"""
多账号隔离与指纹种子稳定化 回归测试。

覆盖：
  - P0-1 isolate_pool：不同 adv_id 隔离互不污染、同 adv_id 内 C段/ASN/指纹互斥；
  - P0-4 adv_isolation：不同 adv_id 隔离、同 adv_id 内 device×IP 互斥；
  - 弱约束回归：adv_id 为空/占位时隔离判定依然严格生效（不再直接放行）；
  - P2-2 get_stable_canvas_seed：同 fp_id 稳定、不同 fp_id 不同。
"""
import uuid

import pytest

from risk_control_enhancements import (
    _AdvIsolation,
    _IsolatePool,
    adv_isolation,
    fingerprint_seed,
    get_stable_canvas_seed,
    isolate_pool,
)


def _fresh_pool() -> _IsolatePool:
    """构造一个清空共享状态的隔离池实例，避免污染/被污染。"""
    p = _IsolatePool()
    p._c.clear()
    p._asn.clear()
    p._fp.clear()
    return p


def _fresh_iso() -> _AdvIsolation:
    """构造一个清空共享状态的隔离实例。"""
    i = _AdvIsolation()
    i._di.clear()
    i._ia.clear()
    i._ad.clear()
    return i


def _unique(suffix: str) -> str:
    return f"{suffix}-{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------------------- #
#  P0-1 isolate_pool：不同 adv_id 隔离
# --------------------------------------------------------------------------- #
def test_isolate_pool_different_adv_not_polluted():
    pool = _fresh_pool()
    adv_a = _unique("advA")
    adv_b = _unique("advB")
    ok_a, _ = pool.allow(adv_a, "9.9.9.5", "fp-same", "ua-same", asn="AS100-a", persist=False)
    ok_b, _ = pool.allow(adv_b, "9.9.9.5", "fp-same", "ua-same", asn="AS100-a", persist=False)
    assert ok_a and ok_b, "不同 adv_id 复用同一 C段/ASN/指纹 应互不污染、均放行"


def test_isolate_pool_same_adv_mutex():
    pool = _fresh_pool()
    adv = _unique("adv")
    ok1, _ = pool.allow(adv, "8.8.8.7", "fp-x", "ua-x", asn="AS200-x", persist=False)
    ok2, reason = pool.allow(adv, "8.8.8.7", "fp-x", "ua-x", asn="AS200-x", persist=False)
    assert ok1 is True
    assert ok2 is False, "同 adv_id 复用同一 C段/指纹 应被互斥拒绝"
    assert "重复" in reason


def test_isolate_pool_empty_adv_still_enforced():
    # 弱约束回归：adv_id 为空也绝不直接放行
    pool = _fresh_pool()
    ok1, _ = pool.allow("", "7.7.7.7", "fp-e", "ua-e", asn="AS300-e", persist=False)
    ok2, reason = pool.allow("", "7.7.7.7", "fp-e", "ua-e", asn="AS300-e", persist=False)
    assert ok1 is True
    assert ok2 is False, "adv_id 为空时隔离判定也应严格生效"
    assert "重复" in reason


def test_isolate_pool_derive_keys_namespace_disjoint():
    k_a = _IsolatePool.derive_keys("adv-A", "1.2.3.4", "fp", "ua")
    k_default_b = _IsolatePool.derive_keys("adv-B", "1.2.3.4", "fp", "ua")
    assert k_a[0] != k_default_b[0], "不同 adv_id 的命名空间必须不相交"
    # 空 adv_id 落回统一 default 命名空间，且与显式命名空间不同
    k_default = _IsolatePool.derive_keys("", "1.2.3.4", "fp", "ua")
    assert k_default[0] == "__default_adv__"
    assert k_default[0] != k_a[0]
    # 同 adv_id 同资源 → 键一致
    k_a2 = _IsolatePool.derive_keys("adv-A", "1.2.3.4", "fp", "ua")
    assert k_a == k_a2


# --------------------------------------------------------------------------- #
#  P0-4 adv_isolation：不同 adv_id 隔离、同 adv_id 互斥
# --------------------------------------------------------------------------- #
def test_adv_isolation_different_adv_not_polluted():
    iso = _fresh_iso()
    adv_a = _unique("advA")
    adv_b = _unique("advB")
    # 同一 IP，但两个 adv_id 使用各自的设备 → (ip,adv)/(adv,device) 键空间互不污染
    ok_a, _ = iso.can_acquire(adv_a, "devA", "10.1.1.1", ua="ua", persist=False)
    ok_b, _ = iso.can_acquire(adv_b, "devB", "10.1.1.1", ua="ua", persist=False)
    assert ok_a and ok_b, "不同 adv_id 复用同一 IP（各自独立设备）应互不污染"


def test_adv_isolation_same_device_shared_across_adv_global_mutex():
    # (device,ip) 是设备级全局互斥（原设计）：同一设备被两个账户复用应被拒绝
    iso = _fresh_iso()
    adv_a = _unique("advA")
    adv_b = _unique("advB")
    ok_a, _ = iso.can_acquire(adv_a, "devShared", "10.9.9.9", ua="ua", persist=False)
    ok_b, _ = iso.can_acquire(adv_b, "devShared", "10.9.9.9", ua="ua", persist=False)
    assert ok_a is True
    assert ok_b is False, "同一物理设备不应被两个广告账户复用"


def test_adv_isolation_same_adv_mutex():
    iso = _fresh_iso()
    adv = _unique("adv")
    ok1, _ = iso.can_acquire(adv, "dev2", "10.2.2.2", ua="ua", persist=False)
    ok2, reason = iso.can_acquire(adv, "dev2", "10.2.2.2", ua="ua", persist=False)
    assert ok1 is True
    assert ok2 is False, "同 adv_id 复用同一 device×IP 应被互斥拒绝"
    assert "冲突" in reason


def test_adv_isolation_empty_adv_still_enforced():
    # 弱约束回归：adv_id 为空时隔离判定依然严格生效
    iso = _fresh_iso()
    ok1, _ = iso.can_acquire("", "dev3", "10.3.3.3", ua="ua", persist=False)
    ok2, reason = iso.can_acquire("", "dev3", "10.3.3.3", ua="ua", persist=False)
    assert ok1 is True
    assert ok2 is False, "adv_id 为空时隔离判定也应严格生效"
    assert "冲突" in reason


def test_adv_isolation_derive_keys_namespace_disjoint():
    k_a = _AdvIsolation.derive_keys("adv-A", "dev", "1.1.1.1", "ua")
    k_b = _AdvIsolation.derive_keys("adv-B", "dev", "1.1.1.1", "ua")
    # (ip,adv) 与 (adv,device) 键空间必须不同
    assert k_a[0] != k_b[0]
    k_default = _AdvIsolation.derive_keys("", "dev", "1.1.1.1", "ua")
    assert k_default[0] == "__default_adv__"
    assert _AdvIsolation.derive_keys("adv-A", "dev", "1.1.1.1", "ua") == k_a


# --------------------------------------------------------------------------- #
#  P2-2 fingerprint_seed：get_stable_canvas_seed 稳定性
# --------------------------------------------------------------------------- #
def test_get_stable_canvas_seed_same_fp_stable():
    fp = _unique("fp")
    s1 = get_stable_canvas_seed(fp)
    s2 = get_stable_canvas_seed(fp)
    assert s1 == s2, "同 fp_id 的 canvas 种子必须稳定一致"
    assert isinstance(s1, int)
    assert s1 > 0, "种子必须为正整数，避免退化为固定基线"


def test_get_stable_canvas_seed_diff_fp_diff():
    fp1 = _unique("fp")
    fp2 = _unique("fp")
    s1 = get_stable_canvas_seed(fp1)
    s2 = get_stable_canvas_seed(fp2)
    assert s1 != s2, "不同 fp_id 的 canvas 种子应该不同"


def test_get_stable_canvas_seed_uses_fingerprint_seed():
    fp = _unique("fp")
    assert get_stable_canvas_seed(fp) == ((fingerprint_seed.get(fp) % 2 ** 31) or 1)


# --------------------------------------------------------------------------- #
#  共享单例基本可用（不抛异常）
# --------------------------------------------------------------------------- #
def test_shared_singletons_usable():
    adv = _unique("adv")
    ok1, _ = isolate_pool.allow(adv, "5.5.5.5", "fp-s", "ua-s", persist=False)
    ok2, _ = adv_isolation.can_acquire(adv, "dev-s", "5.5.5.5", ua="ua", persist=False)
    assert ok1 and ok2
    assert get_stable_canvas_seed(_unique("fp")) > 0