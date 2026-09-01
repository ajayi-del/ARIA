"""Mark-scale quarantine (Workstream B): sentinel state machine, consumer
helper, phantom firewall, and wiring pins."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence.mark_scale import (  # noqa: E402
    MarkScaleSentinel, LOW_RATIO, HIGH_RATIO, PERSIST_N, HEAL_N)
from intelligence.shadow_journal import REJECTION_EVENTS  # noqa: E402
import main as _main  # noqa: E402


# ── Sentinel: band + persistence ─────────────────────────────────────────────

def test_in_band_never_arms():
    s = MarkScaleSentinel()
    for _ in range(10):
        q, tr, r = s.observe("BTC-USD", 100.0, 100.5)
        assert q is False and tr is None
    assert not s.quarantined("BTC-USD")


def test_band_boundaries_are_in_band():
    s = MarkScaleSentinel()
    for _ in range(PERSIST_N + 2):
        q, tr, _ = s.observe("A", LOW_RATIO * 100.0, 100.0)     # exactly 0.70
        assert tr is None and q is False
        q, tr, _ = s.observe("B", HIGH_RATIO * 100.0, 100.0)    # exactly 1.43
        assert tr is None and q is False


def test_split_arms_after_persist_n_consecutive():
    s = MarkScaleSentinel()
    for i in range(PERSIST_N):
        q, tr, r = s.observe("SPCX-USD", 769.35, 140.5)   # 5.48x split
        if i < PERSIST_N - 1:
            assert tr is None and q is False
        else:
            assert tr == "armed" and q is True
            assert abs(r - 769.35 / 140.5) < 1e-9
    assert s.quarantined("SPCX-USD")


def test_single_split_does_not_arm():
    s = MarkScaleSentinel()
    q, tr, _ = s.observe("X", 200.0, 100.0)   # 2.0x — one observation
    assert tr is None and q is False


def test_counter_resets_on_in_band():
    s = MarkScaleSentinel()
    s.observe("X", 200.0, 100.0)
    s.observe("X", 200.0, 100.0)              # 2 splits
    s.observe("X", 100.0, 100.0)              # in-band → reset
    s.observe("X", 200.0, 100.0)
    q, tr, _ = s.observe("X", 200.0, 100.0)   # only 2 consecutive
    assert tr is None and q is False


def test_fail_open_on_invalid_inputs():
    s = MarkScaleSentinel()
    for m, k in ((0.0, 100.0), (100.0, 0.0), (-5.0, 100.0), (None, 100.0),
                 (100.0, None), ("bad", 100.0)):
        q, tr, r = s.observe("X", m, k)
        assert tr is None and q is False
    # State untouched: a fresh split still needs PERSIST_N real observations
    s.observe("X", 200.0, 100.0)
    q, tr, _ = s.observe("X", 0.0, 100.0)     # invalid — counter must NOT reset
    s.observe("X", 200.0, 100.0)
    q, tr, _ = s.observe("X", 200.0, 100.0)
    assert tr == "armed" and q is True


def test_heal_after_heal_n_in_band():
    s = MarkScaleSentinel()
    for _ in range(PERSIST_N):
        s.observe("X", 200.0, 100.0)
    assert s.quarantined("X")
    for i in range(HEAL_N):
        q, tr, _ = s.observe("X", 100.0, 100.0)
        if i < HEAL_N - 1:
            assert tr is None and q is True     # still armed while healing
        else:
            assert tr == "healed" and q is False
    assert not s.quarantined("X")


def test_heal_counter_resets_on_split():
    s = MarkScaleSentinel()
    for _ in range(PERSIST_N):
        s.observe("X", 200.0, 100.0)
    s.observe("X", 100.0, 100.0)
    s.observe("X", 100.0, 100.0)              # 2 in-band
    q, tr, _ = s.observe("X", 200.0, 100.0)   # split again → heal resets
    assert tr is None and q is True
    s.observe("X", 100.0, 100.0)
    q, tr, _ = s.observe("X", 100.0, 100.0)   # only 2 consecutive in-band
    assert tr is None and q is True


def test_per_symbol_isolation():
    s = MarkScaleSentinel()
    for _ in range(PERSIST_N):
        s.observe("SPCX-USD", 769.0, 140.0)
    assert s.quarantined("SPCX-USD")
    assert not s.quarantined("BTC-USD")


# ── Consumer helper + firewall ────────────────────────────────────────────────

class _FakePS:
    def __init__(self, val=None, raises=False, quarantined_sym="SPCX-USD"):
        self._val = val
        self._raises = raises
        self._key = f"mark_scale_quarantined:{quarantined_sym}"

    def get_ai_param(self, key, default=None):
        if self._raises:
            raise RuntimeError("disk gone")
        if key == self._key:
            return self._val if self._val is not None else default
        return default


def test_quarantine_helper_reads_param(monkeypatch):
    monkeypatch.delenv("MARK_SCALE_QUARANTINE_ENABLED", raising=False)
    assert _main._mark_scale_quarantined("SPCX-USD", ps=_FakePS({"ratio": 5.5})) is True
    assert _main._mark_scale_quarantined("SPCX-USD", ps=_FakePS(None)) is False
    assert _main._mark_scale_quarantined("SPCX-USD", ps=None) is False
    assert _main._mark_scale_quarantined("SPCX-USD", ps=_FakePS(raises=True)) is False


def test_quarantine_helper_kill_switch(monkeypatch):
    monkeypatch.setenv("MARK_SCALE_QUARANTINE_ENABLED", "false")
    assert _main._mark_scale_quarantined("SPCX-USD", ps=_FakePS({"ratio": 5.5})) is False
    pnl, suppressed = _main.apply_phantom_firewall("SPCX-USD", 792.0, ps=_FakePS({"ratio": 5.5}))
    assert pnl == 792.0 and suppressed is False


def test_phantom_firewall(monkeypatch):
    monkeypatch.delenv("MARK_SCALE_QUARANTINE_ENABLED", raising=False)
    pnl, suppressed = _main.apply_phantom_firewall("SPCX-USD", 792.0, ps=_FakePS({"ratio": 5.5}))
    assert pnl == 0.0 and suppressed is True
    pnl, suppressed = _main.apply_phantom_firewall("SPCX-USD", -649.78, ps=_FakePS({"ratio": 5.5}))
    assert pnl == 0.0 and suppressed is True
    # Unquarantined symbol flows through untouched
    pnl, suppressed = _main.apply_phantom_firewall("BTC-USD", -0.63, ps=_FakePS({"ratio": 5.5}))
    assert pnl == -0.63 and suppressed is False
    # No param_store → fail open
    pnl, suppressed = _main.apply_phantom_firewall("SPCX-USD", 792.0, ps=None)
    assert pnl == 792.0 and suppressed is False


# ── Wiring pins ───────────────────────────────────────────────────────────────

def test_shadow_gate_registered():
    assert REJECTION_EVENTS.get("entry_blocked_mark_scale") == "mark_scale_quarantine"


def test_main_source_wiring():
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "main.py")).read()
    # Five entry-path blocks: standard, momentum, aftermath, campaign, explosive
    assert src.count("entry_blocked_mark_scale") >= 5
    for path in ('path="standard"', 'path="cascade_momentum"',
                 'path="cascade_aftermath"', 'path="campaign_heartbeat"',
                 'path="explosive"'):
        assert path in src, f"missing {path}"
    # Phantom firewall + telemetry
    assert "apply_phantom_firewall(sym, pnl, ps=_param_store)" in src
    assert "phantom_close_suppressed" in src
    # Detector loop registered in the gather list
    assert '_supervise(_mark_scale_sentinel_loop,       "mark_scale_sentinel")' in src
    # Close-path skips
    assert src.count("_mark_scale_quarantined(_sym, ps=_param_store)") >= 2   # time_stop + trailing
    assert "_mark_scale_quarantined(_pp.symbol, ps=_param_store)" in src      # pnl_attribution
    assert "_mark_scale_quarantined(_adl_pp.symbol, ps=_param_store)" in src  # ADL
    # Venue-native reference plane for Yahoo-owned symbols (2026-08-31)
    assert "_sentinel_venue_ref_symbol(_sym, _vk_owned)" in src
    assert "await _venue_kline_1m_close(_sym, _vk_base)" in src


# ── Cascade-momentum inflight guard (2026-08-31 double-fill repair) ──────────

def test_momentum_inflight_guard_wiring():
    """N symbol cascade events spawn N momentum tasks that all select the same
    preferred symbol (2× BTC long 12:41 UTC 2026-08-31, $247 vs $124). The
    guard must be check-and-set BEFORE the first await (asyncio-atomic), keyed
    on direction, released in finally on every path, with blocked telemetry."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "main.py")).read()
    i_def = src.index("async def _execute_cascade_momentum")
    i_aftermath = src.index("async def _execute_cascade_aftermath")
    body = src[i_def:i_aftermath]
    # Guard at the head, before the first await in the body
    i_guard = body.index("if direction in _cascade_momentum_inflight:")
    assert body.index("_cascade_momentum_inflight.add(direction)") > i_guard
    assert "cascade_momentum_inflight_blocked" in body
    assert body.index("await ") > body.index("_cascade_momentum_inflight.add(direction)")
    # Released in finally AFTER the except (every path, including exceptions)
    i_except = body.index("except Exception as _cm_ex:")
    i_finally = body.index("finally:")
    assert i_finally > i_except
    assert "_cascade_momentum_inflight.discard(direction)" in body[i_finally:]
    # Registry co-located with _pending_entry_symbols
    assert "_cascade_momentum_inflight: set = set()" in src


# ── Venue-reference routing (2026-08-31 false-quarantine repair) ─────────────

_VK_OWNED = ("SILVER-USD", "COPPER-USD", "XAUT-USD", "CL-USD")


def test_venue_ref_symbol_membership():
    # Yahoo-owned candles → venue kline reference required
    assert _main._sentinel_venue_ref_symbol("SPCX-USD", _VK_OWNED) is True
    assert _main._sentinel_venue_ref_symbol("USTECH100-USD", _VK_OWNED) is True
    assert _main._sentinel_venue_ref_symbol("NVDA-USD", _VK_OWNED) is True
    # Venue/Aster-owned candles already sit on an execution-venue plane
    assert _main._sentinel_venue_ref_symbol("SILVER-USD", _VK_OWNED) is False
    assert _main._sentinel_venue_ref_symbol("COPPER-USD", _VK_OWNED) is False
    assert _main._sentinel_venue_ref_symbol("XAUT-USD", _VK_OWNED) is False
    assert _main._sentinel_venue_ref_symbol("CL-USD", _VK_OWNED) is False
    # Crypto buffers (Bybit-owned) untouched; unknown symbols safe
    assert _main._sentinel_venue_ref_symbol("BTC-USD", _VK_OWNED) is False
    assert _main._sentinel_venue_ref_symbol("NOPE-USD", _VK_OWNED) is False
    assert _main._sentinel_venue_ref_symbol("SPCX-USD", None) is True


class _FakeKlineResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _FakeKlineClient:
    payload = {"data": [{"t": 1788190000000, "c": "142.72"}]}
    status = 200
    raises = False

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None):
        if self.raises:
            raise RuntimeError("gateway down")
        return _FakeKlineResp(self.status, self.payload)


def test_venue_kline_close_parses_newest_first(monkeypatch):
    import asyncio
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeKlineClient)
    close, open_ms = asyncio.run(
        _main._venue_kline_1m_close("SPCX-USD", "https://example"))
    assert close == 142.72 and open_ms == 1788190000000


def test_venue_kline_close_fail_open(monkeypatch):
    import asyncio
    import httpx

    class _Bad(_FakeKlineClient):
        payload = {"data": []}
    monkeypatch.setattr(httpx, "AsyncClient", _Bad)
    assert asyncio.run(_main._venue_kline_1m_close("SPCX-USD", "https://x")) is None

    class _HttpErr(_FakeKlineClient):
        status = 500
        payload = {"data": [{"t": 1, "c": "142.0"}]}
    monkeypatch.setattr(httpx, "AsyncClient", _HttpErr)
    assert asyncio.run(_main._venue_kline_1m_close("SPCX-USD", "https://x")) is None

    class _ZeroRow(_FakeKlineClient):
        payload = {"data": [{"t": 0, "c": "0"}]}
    monkeypatch.setattr(httpx, "AsyncClient", _ZeroRow)
    assert asyncio.run(_main._venue_kline_1m_close("SPCX-USD", "https://x")) is None

    class _Down(_FakeKlineClient):
        raises = True
    monkeypatch.setattr(httpx, "AsyncClient", _Down)
    assert asyncio.run(_main._venue_kline_1m_close("SPCX-USD", "https://x")) is None
