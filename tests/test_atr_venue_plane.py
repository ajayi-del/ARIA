"""Venue-plane ATR repair (2026-09-01, operator directive "ultrathink and ship").

USTECH100 was structurally blocked: candidate.atr comes from Yahoo-QQQ-plane
candles (0.357) while the entry is the rebased synthetic perp (29112) — the
atr_sanity ratio read 1.2e-5 < 1e-4 and 28/31 candidates died at the gate.
Same plane-mismatch class as the 6d1a7c3 sentinel defect. The repair maps
the same % vol onto the entry plane via the rebase factor, spliced BEFORE
the atr_sanity gate so the gate stays armed. Healthy-ratio symbols are
untouched bit-for-bit; wrong-plane/missing inputs fail closed (legacy
reject). Kill switch ATR_VENUE_PLANE_FIX_ENABLED=false = legacy."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import venue_plane_atr  # noqa: E402


class TestVenuePlaneAtr:
    def test_ustech100_live_values_corrected(self):
        # The exact 16:33 UTC rejection: atr 0.357549, entry 29112, QQQ ~716
        fixed = venue_plane_atr(0.357549, 29112.0, 716.0)
        assert fixed is not None and fixed != 0.357549
        assert abs(fixed - 0.357549 * 29112.0 / 716.0) < 1e-9
        assert fixed / 29112.0 >= 0.0001          # now passes the gate
        # % vol preserved exactly across the plane mapping
        assert abs(fixed / 29112.0 - 0.357549 / 716.0) < 1e-12

    def test_healthy_ratio_untouch(self):
        # SPCX-shaped: SPY-plane atr 0.5 vs entry 141 → ratio 0.0035 healthy
        assert venue_plane_atr(0.5, 141.0, 767.0) == 0.5

    def test_boundary_ratio_is_healthy(self):
        assert venue_plane_atr(0.0001, 1.0, 100.0) == 0.0001

    def test_wrong_plane_close_fails_closed(self):
        # Close already on the venue plane (rebase ~1) → correction cannot
        # fix the ratio → None = caller keeps the legacy reject
        assert venue_plane_atr(0.357, 29112.0, 29100.0) is None

    def test_nonpositive_inputs_fail_closed(self):
        assert venue_plane_atr(0.0, 29112.0, 716.0) is None
        assert venue_plane_atr(-1.0, 29112.0, 716.0) is None
        assert venue_plane_atr(0.357, 0.0, 716.0) is None
        assert venue_plane_atr(0.357, 29112.0, 0.0) is None
        assert venue_plane_atr(0.357, 29112.0, -716.0) is None

    def test_garbage_inputs_fail_closed(self):
        assert venue_plane_atr("x", 29112.0, 716.0) is None
        assert venue_plane_atr(0.357, None, 716.0) is None
        assert venue_plane_atr(0.357, 29112.0, "y") is None


class TestMainWiring:
    @staticmethod
    def _src():
        return open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "main.py")).read()

    def test_repair_before_gate(self):
        src = self._src()
        i_fix = src.index("Venue-plane ATR repair")
        i_gate = src.index('logger.info("signal_rejected_atr_sanity"')
        assert i_fix < i_gate

    def test_kill_switch_and_predicate(self):
        src = self._src()
        assert "ATR_VENUE_PLANE_FIX_ENABLED" in src
        assert "_sentinel_venue_ref_symbol(symbol, _apf_vk)" in src
        assert "sodex_kline_assets" in src and "aster_kline_assets" in src

    def test_telemetry_and_throttle(self):
        src = self._src()
        assert "atr_venue_plane_corrected" in src
        assert "_atr_plane_fix_last: dict = {}" in src
        assert "_atr_plane_fix_last.get(symbol, 0.0) >= 300.0" in src

    def test_underlying_close_from_own_buffer(self):
        src = self._src()
        assert '_apf_buf = (candle_buffers.get(symbol) or {}).get("1m")' in src
        assert "_apf_buf.closes(1)" in src

    def test_gate_itself_untouched(self):
        src = self._src()
        assert ("if candidate.atr <= 0 or candidate.atr / candidate.entry_price "
                "< 0.0001:") in src
