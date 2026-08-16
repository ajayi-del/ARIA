"""Graduation registry — shrinkage math, thresholds, TTL set/lapse,
autonomous transitions (2026-08-16)."""
import time

from intelligence.graduation import GraduationRegistry


class FakeParamStore:
    def __init__(self):
        self._d = {}

    def set_ai_param(self, key, value, ttl_seconds=None):
        exp = time.time() + ttl_seconds if ttl_seconds is not None else None
        self._d[key] = (value, exp)

    def get_ai_param(self, key, default=None):
        item = self._d.get(key)
        if item is None:
            return default
        val, exp = item
        if exp is not None and time.time() > exp:
            del self._d[key]
            return default
        return val


def _outcomes(n, wins, span_days, end=None):
    end = end if end is not None else time.time()
    if n == 1:
        return [(end, True)]
    start = end - span_days * 86400.0
    step = (end - start) / (n - 1)
    return [(start + i * step, i < wins) for i in range(n)]


def _reg(ps, **kw):
    kw.setdefault("min_samples", 30)
    kw.setdefault("min_span_days", 7.0)
    kw.setdefault("min_shrunk_wr", 0.5)
    kw.setdefault("ttl_s", 72 * 3600)
    return GraduationRegistry(ps, **kw)


def test_shrinkage_math():
    assert GraduationRegistry.shrunk_wr(0, 0) == 0.0
    assert abs(GraduationRegistry.shrunk_wr(8, 10) - 0.6) < 1e-9
    # small n pulled toward the 0.5 prior: 3/4 raw = 0.75, shrunk ≈ 0.5417
    assert abs(GraduationRegistry.shrunk_wr(3, 4) - 13.0 / 24.0) < 1e-9
    # 8/10 lucky streak must NOT clear the bar
    assert GraduationRegistry.shrunk_wr(8, 10) > 0.5  # math clears…
    # …but n=10 < min_samples gates it in evaluate, covered below


def test_no_param_store_never_graduates():
    reg = _reg(None)
    st = reg.evaluate("explosive", _outcomes(40, 30, 10))
    assert st["graduated"] is False
    assert reg.is_graduated("explosive") is False


def test_below_min_samples_no_key():
    ps = FakeParamStore()
    reg = _reg(ps)
    st = reg.evaluate("explosive", _outcomes(10, 9, 10))
    assert st["graduated"] is False
    assert reg.is_graduated("explosive") is False
    assert ps.get_ai_param("grad_explosive") is None


def test_short_span_no_key():
    ps = FakeParamStore()
    reg = _reg(ps)
    st = reg.evaluate("explosive", _outcomes(40, 30, 2.0))
    assert st["graduated"] is False
    assert reg.is_graduated("explosive") is False


def test_coin_flip_wr_no_key():
    ps = FakeParamStore()
    reg = _reg(ps)
    # 20/40 over 10d shrinks to exactly 0.5 — must be strictly above
    st = reg.evaluate("explosive", _outcomes(40, 20, 10))
    assert st["graduated"] is False
    assert reg.is_graduated("explosive") is False


def test_meets_criteria_sets_ttl_key():
    ps = FakeParamStore()
    reg = _reg(ps)
    st = reg.evaluate("explosive", _outcomes(40, 30, 10))
    assert st["graduated"] is True
    assert st["n"] == 40 and st["wins"] == 30
    assert reg.is_graduated("explosive") is True
    payload = ps.get_ai_param("grad_explosive")
    assert payload["wins"] == 30 and "earned_at" in payload


def test_ttl_lapse_revokes_privilege():
    ps = FakeParamStore()
    reg = _reg(ps, ttl_s=-1)   # key expires the instant it's written
    st = reg.evaluate("explosive", _outcomes(40, 30, 10))
    assert st["graduated"] is True        # criteria met at evaluation time
    assert reg.is_graduated("explosive") is False   # …but the key lapsed


def test_lapse_transition_announced_once():
    ps = FakeParamStore()
    reg = _reg(ps)
    reg.evaluate("explosive", _outcomes(40, 30, 10))
    assert "explosive" in reg._announced
    ps._d.clear()   # simulate TTL purge
    st = reg.evaluate("explosive", _outcomes(5, 1, 1.0))
    assert st["graduated"] is False
    assert "explosive" not in reg._announced


def test_subsystems_isolated():
    ps = FakeParamStore()
    reg = _reg(ps)
    reg.evaluate("explosive", _outcomes(40, 30, 10))
    reg.evaluate("router_v2", _outcomes(10, 9, 10))
    assert reg.is_graduated("explosive") is True
    assert reg.is_graduated("router_v2") is False
