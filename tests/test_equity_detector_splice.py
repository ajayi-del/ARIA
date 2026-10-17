"""Equity detector splice pins (2026-10-17 Governor directive "build live and
own shadow if wrong we tweak execution live from today" — shadow-first
OVERRIDDEN; the five zero-I/O detector brains ACTUATE on the live signal
score from day one).

Textual pins on main.py:
  - all five brain event-name constants referenced at the splice (and the
    resolved values equal the brains' contract names),
  - every actuation env knob present with its documented default,
  - EQUITY_DETECTOR_BONUS_CAP present,
  - the helper is called in the on_signal_ready scoring path BEFORE the Kant
    gate evaluation point (kant_engine.assess),
  - the raw score is preserved in the splice log payload (score_raw /
    detector_bonus).

Behavioral pins (real brains, injected ctx planes, monkeypatched env):
  - direction disagreement → 0 bonus,
  - per-detector kill switch off → that detector contributes 0, others
    unaffected,
  - the cap binds at EQUITY_DETECTOR_BONUS_CAP,
  - all bonus knobs off → 0.0 (legacy score bit-for-bit),
  - fire paths: ETF flow direction agreement, funding fade direction
    agreement, directionless amplifiers (oi_velocity + pre_earnings_oi).
"""

import datetime as _dt
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as _main  # noqa: E402
from data.candle_buffer import Candle, CandleBuffer  # noqa: E402
from intelligence import equity_outliers as _eo  # noqa: E402
from intelligence import earnings_watch as _ew  # noqa: E402
from intelligence import equity_open_lead as _eol  # noqa: E402
from intelligence import etf_flow_ratio as _efr  # noqa: E402
from intelligence import mag7_pairs as _m7  # noqa: E402

_MAIN_SRC = open(_main.__file__).read()

_ALL_KNOBS = (
    "EQUITY_OUTLIER_OI_BONUS", "EQUITY_OUTLIER_ROT_BONUS",
    "EQUITY_OUTLIER_FUND_BONUS", "EQUITY_OUTLIER_EARN_BONUS",
    "EQUITY_EARNINGS_BONUS", "EQUITY_OPEN_LEAD_BONUS",
    "ETF_FLOW_BONUS", "MAG7_PAIR_BONUS", "EQUITY_DETECTOR_BONUS_CAP",
)
_ALL_KILL_SWITCHES = (
    "EQUITY_OUTLIER_ENABLED", "EARNINGS_WATCH_ENABLED",
    "EQUITY_OPEN_LEAD_ENABLED", "ETF_FLOW_RATIO_ENABLED", "MAG7_PAIRS_ENABLED",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for _k in _ALL_KNOBS + _ALL_KILL_SWITCHES:
        monkeypatch.delenv(_k, raising=False)
    # The earnings calendar plane reads logs/*.jsonl from disk — keep every
    # test hermetic unless it explicitly re-arms the brain.
    monkeypatch.setenv("EARNINGS_WATCH_ENABLED", "false")
    yield


# ── ctx builders ─────────────────────────────────────────────────────────────

def _ctx(**over):
    c = {"candle_buffers": {}, "funding_rates": {}, "etf_verdict_fn": None,
         "etf_rows_fn": None, "oi_series_fn": None,
         "category_fn": lambda _s: None}
    c.update(over)
    return c


def _buf(closes, interval="15m"):
    b = CandleBuffer(symbol="X", interval=interval, maxlen=400)
    for i, c in enumerate(closes):
        b.add(Candle(open_time=i * 60000, open=c, high=c, low=c,
                     close=c, volume=1.0, close_time=(i + 1) * 60000))
    return b


def _etf_ctx(direction_flow=-1.0):
    """BTC ETF plane that FIRES: today's |ratio| is top-decile vs 20 days of
    dust history. direction_flow < 0 → outflow day."""
    today = {"date": "2026-10-16",
             "total_net_inflow": direction_flow * 1.0e9,
             "total_net_assets": 1.0e10}
    hist = [{"date": f"2026-09-{d:02d}", "total_net_inflow": 1.0e4,
             "total_net_assets": 1.0e10} for d in range(26, 6, -1)]
    rows = [today] + hist                       # newest-first, 21 rows
    verdict = {"rows": len(rows),
               "last_inflow_usd": today["total_net_inflow"],
               "net_assets_usd": today["total_net_assets"]}
    return _ctx(etf_verdict_fn=lambda _s: (verdict, 5.0),
                etf_rows_fn=lambda _a: rows)


def _lead_now():
    """A Thursday inside the 09:30-10:30 ET window (EDT = UTC-4)."""
    return _dt.datetime(2026, 10, 15, 13, 45,
                        tzinfo=_dt.timezone.utc).timestamp()


# ── textual pins ─────────────────────────────────────────────────────────────

class TestTextualPins:
    def test_brain_event_constants_referenced_at_splice(self):
        for ref in ("_eq_outliers.EVENT_OI_VELOCITY",
                    "_eq_outliers.EVENT_ROTATION_RESIDUAL",
                    "_eq_outliers.EVENT_FUNDING_DIVERGENCE",
                    "_eq_outliers.EVENT_PRE_EARNINGS_OI",
                    "_eq_earnings_watch.EVENT_NAME",
                    "_eq_open_lead.EVENT_NAME",
                    "_eq_etf_flow_ratio.ETF_FLOW_RATIO_EVENT",
                    "_eq_mag7_pairs.MAG7_PAIR_EVENT"):
            assert ref in _MAIN_SRC, f"missing splice reference {ref}"

    def test_brain_event_names_match_contract(self):
        assert _eo.EVENT_OI_VELOCITY == "equity_outlier_oi_velocity"
        assert _eo.EVENT_ROTATION_RESIDUAL == "equity_outlier_rotation_residual"
        assert _eo.EVENT_FUNDING_DIVERGENCE == "equity_outlier_funding_divergence"
        assert _eo.EVENT_PRE_EARNINGS_OI == "equity_outlier_pre_earnings_oi"
        assert _ew.EVENT_NAME == "earnings_watch_window"
        assert _eol.EVENT_NAME == "equity_open_lead_score"
        assert _efr.ETF_FLOW_RATIO_EVENT == "etf_flow_ratio_signal"
        assert _m7.MAG7_PAIR_EVENT == "mag7_pair_divergence"

    def test_env_knobs_present_with_documented_defaults(self):
        expected = {
            "EQUITY_OUTLIER_OI_BONUS": "0.3",
            "EQUITY_OUTLIER_ROT_BONUS": "0.3",
            "EQUITY_OUTLIER_FUND_BONUS": "0.3",
            "EQUITY_OUTLIER_EARN_BONUS": "0.3",
            "EQUITY_EARNINGS_BONUS": "0.3",
            "EQUITY_OPEN_LEAD_BONUS": "0.4",
            "ETF_FLOW_BONUS": "0.4",
            "MAG7_PAIR_BONUS": "0.3",
            "EQUITY_DETECTOR_BONUS_CAP": "1.0",
        }
        for knob, default in expected.items():
            assert knob in _MAIN_SRC, f"missing knob {knob}"
            assert f'"{knob}", {default}' in _MAIN_SRC, \
                f"knob {knob} missing documented default {default}"

    def test_helper_called_before_kant_gate(self):
        call = _MAIN_SRC.index("_equity_detector_bonus(symbol, _sig_dir)")
        kant = _MAIN_SRC.index("_kant_frame = kant_engine.assess(")
        assert call < kant, "detector bonus must land BEFORE the Kant gate"

    def test_raw_score_preserved_in_payload(self):
        call = _MAIN_SRC.index("_equity_detector_bonus(symbol, _sig_dir)")
        window = _MAIN_SRC[call:call + 1200]
        assert "score_raw=" in window
        assert "detector_bonus=" in window
        assert "equity_detector_scored" in window

    def test_helper_added_to_score_additively(self):
        call = _MAIN_SRC.index("_equity_detector_bonus(symbol, _sig_dir)")
        window = _MAIN_SRC[call:call + 900]
        assert "_effective_coherence + _det_bonus" in window


# ── behavioral pins ──────────────────────────────────────────────────────────

class TestDirectionAgreement:
    def test_etf_outflow_agrees_with_short(self):
        c = _etf_ctx(direction_flow=-1.0)
        b = _main._equity_detector_bonus("BTC-USD", "short", ctx=c,
                                         now_ts=1760710000.0)
        assert b == pytest.approx(0.4)

    def test_direction_disagreement_zero_bonus(self):
        c = _etf_ctx(direction_flow=-1.0)       # outflow → SHORT only
        assert _main._equity_detector_bonus(
            "BTC-USD", "long", ctx=c, now_ts=1760710000.0) == 0.0

    def test_etf_inflow_agrees_with_long_only(self):
        c = _etf_ctx(direction_flow=1.0)
        assert _main._equity_detector_bonus(
            "BTC-USD", "long", ctx=c, now_ts=1760710000.0) == pytest.approx(0.4)
        assert _main._equity_detector_bonus(
            "BTC-USD", "short", ctx=c, now_ts=1760710000.0) == 0.0


class TestKillSwitchIndependence:
    def _mag7_ctx(self):
        # NVDA +5% vs AMD flat over the 5h window → 5% spread, NVDA rich.
        nvda = _buf([100.0] * 20 + [105.0])
        amd = _buf([50.0] * 21)
        return _ctx(candle_buffers={"NVDA-USD": {"15m": nvda},
                                    "AMD-USD": {"15m": amd}})

    def test_mag7_fires_convergence_directions(self):
        c = self._mag7_ctx()
        # rich leg = NVDA (leg_a) → SHORT NVDA / LONG AMD earn the bonus
        assert _main._equity_detector_bonus(
            "NVDA-USD", "short", ctx=c, now_ts=1760710000.0) == pytest.approx(0.3)
        assert _main._equity_detector_bonus(
            "AMD-USD", "long", ctx=c, now_ts=1760710000.0) == pytest.approx(0.3)
        assert _main._equity_detector_bonus(
            "NVDA-USD", "long", ctx=c, now_ts=1760710000.0) == 0.0

    def test_etf_kill_switch_off_others_unaffected(self, monkeypatch):
        monkeypatch.setenv("ETF_FLOW_RATIO_ENABLED", "false")
        c_etf = _etf_ctx(direction_flow=-1.0)
        assert _main._equity_detector_bonus(
            "BTC-USD", "short", ctx=c_etf, now_ts=1760710000.0) == 0.0
        # MAG7 brain unaffected by the ETF kill switch
        c_mag = self._mag7_ctx()
        assert _main._equity_detector_bonus(
            "NVDA-USD", "short", ctx=c_mag, now_ts=1760710000.0) == pytest.approx(0.3)

    def test_mag7_kill_switch_off_others_unaffected(self, monkeypatch):
        monkeypatch.setenv("MAG7_PAIRS_ENABLED", "false")
        c_mag = self._mag7_ctx()
        assert _main._equity_detector_bonus(
            "NVDA-USD", "short", ctx=c_mag, now_ts=1760710000.0) == 0.0
        c_etf = _etf_ctx(direction_flow=-1.0)
        assert _main._equity_detector_bonus(
            "BTC-USD", "short", ctx=c_etf, now_ts=1760710000.0) == pytest.approx(0.4)


class TestCapAndLegacy:
    def _double_fire_ctx(self):
        """ETF outflow fire (0.4 short) + equity-open lead fire (0.4 short)
        on BTC-USD inside the 09:30-10:30 ET window."""
        c = _etf_ctx(direction_flow=-1.0)
        # USTECH100 -0.30% / BTC -0.10% over 15min → leader "equity", short.
        u_closes = [100.0] * 15 + [99.70]
        b_closes = [100.0] * 15 + [99.90]
        c["candle_buffers"] = {"USTECH100-USD": {"1m": _buf(u_closes, "1m")},
                               "BTC-USD": {"1m": _buf(b_closes, "1m")}}
        return c

    def test_two_detectors_stack_additively(self):
        c = self._double_fire_ctx()
        b = _main._equity_detector_bonus("BTC-USD", "short", ctx=c,
                                         now_ts=_lead_now())
        assert b == pytest.approx(0.4 + 0.4)

    def test_cap_binds(self, monkeypatch):
        monkeypatch.setenv("EQUITY_DETECTOR_BONUS_CAP", "0.5")
        c = self._double_fire_ctx()
        b = _main._equity_detector_bonus("BTC-USD", "short", ctx=c,
                                         now_ts=_lead_now())
        assert b == pytest.approx(0.5)

    def test_all_knobs_off_legacy_bit_for_bit(self, monkeypatch):
        for _k in _ALL_KNOBS:
            monkeypatch.setenv(_k, "0")
        c = self._double_fire_ctx()
        assert _main._equity_detector_bonus(
            "BTC-USD", "short", ctx=c, now_ts=_lead_now()) == 0.0
        # Zero bonus = the splice adds nothing → the pre-module score stands.
        c2 = _etf_ctx(direction_flow=-1.0)
        assert _main._equity_detector_bonus(
            "BTC-USD", "short", ctx=c2, now_ts=1760710000.0) == 0.0


class TestEquityClassDetectors:
    def _equity_category(self, sym):
        return {"TSLA-USD": "equity"}.get(sym, "large_cap")

    def test_funding_fade_directional(self):
        rates = {"TSLA-USD": 0.002, "BTC-USD": 0.0001, "ETH-USD": 0.0001,
                 "SOL-USD": 0.0001}      # TSLA 19bps rich vs crypto median
        c = _ctx(funding_rates=rates, category_fn=self._equity_category)
        assert _main._equity_detector_bonus(
            "TSLA-USD", "short", ctx=c, now_ts=1760710000.0) == pytest.approx(0.3)
        c2 = _ctx(funding_rates=rates, category_fn=self._equity_category)
        assert _main._equity_detector_bonus(
            "TSLA-USD", "long", ctx=c2, now_ts=1760710000.0) == 0.0

    def test_crypto_symbol_never_reads_equity_detectors(self):
        rates = {"BTC-USD": 0.002, "ETH-USD": 0.0001, "SOL-USD": 0.0001}
        c = _ctx(funding_rates=rates, category_fn=self._equity_category)
        # BTC is not equity-class → funding_divergence must not apply to it
        assert _main._equity_detector_bonus(
            "BTC-USD", "short", ctx=c, now_ts=1760710000.0) == 0.0

    def test_oi_amplifiers_directionless(self, monkeypatch):
        monkeypatch.setenv("EARNINGS_WATCH_ENABLED", "false")
        now = 1760710000.0
        day_ms = 86400 * 1000
        base = now * 1000 - 9 * day_ms
        # 9 daily points: alternating ±1 baseline deltas (non-zero variance —
        # a zero-variance baseline is a documented degenerate-baseline abstain),
        # then a huge newest delta → oi_velocity z fires; OI also +60% over
        # the window → build fires.
        vals = [100.0, 101.0, 100.0, 101.0, 100.0, 101.0, 100.0, 101.0, 160.0]
        oi = [(base + i * day_ms, v) for i, v in enumerate(vals)]
        monkeypatch.setattr(_main, "_eq_det_earnings_ts",
                            lambda _s, _n: now + 3 * 86400.0)
        c = _ctx(category_fn=self._equity_category,
                 oi_series_fn=lambda _s: oi)
        long_b = _main._equity_detector_bonus("TSLA-USD", "long", ctx=c,
                                              now_ts=now)
        assert long_b == pytest.approx(0.3 + 0.3)   # oi + pre_earnings build
        c2 = _ctx(category_fn=self._equity_category,
                  oi_series_fn=lambda _s: oi)
        short_b = _main._equity_detector_bonus("TSLA-USD", "short", ctx=c2,
                                               now_ts=now)
        assert short_b == pytest.approx(0.3 + 0.3)  # directionless amplifiers

    def test_missing_planes_abstain_silently(self):
        # Equity symbol, no OI plane, no funding, no buffers, earnings off:
        # every detector abstains → 0.0, never a fabricated score.
        c = _ctx(category_fn=self._equity_category)
        assert _main._equity_detector_bonus(
            "TSLA-USD", "long", ctx=c, now_ts=1760710000.0) == 0.0

    def test_helper_fail_closed_on_garbage(self):
        assert _main._equity_detector_bonus("TSLA-USD", "sideways") == 0.0
        assert _main._equity_detector_bonus("", "long") == 0.0
