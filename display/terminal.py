import time
import asyncio
import structlog
from collections import deque
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.live import Live
from datetime import datetime, timedelta, timezone
from typing import Callable
from memory.performance import PerformanceTracker
from memory.trade_journal import TradeJournal
import traceback

from core.config import Settings
from core.market_engine import MarketEngine
from data.sodex_feed import SoDEXFeed
from memory.performance import SessionDrawdownTracker

logger = structlog.get_logger(__name__)


class TerminalDisplay:
    def __init__(
        self,
        config: Settings,
        orderbook_stores: dict,
        mark_price_stores: dict,
        candle_buffers: dict,
        trade_flow_stores: dict,
        health_check: Callable[[], dict],
        market_engine: MarketEngine = None,
        calendar_engine=None,  # CalendarEngine
        journal: TradeJournal = None,
        perf: PerformanceTracker = None,
        system_state=None,  # SystemStateManager
        position_manager=None,
        interpreter=None,  # IntelligenceInterpreter
        ws_manager=None,
        dd_tracker: SessionDrawdownTracker = None,
        cascade_tracker=None,    # CascadeTracker
        adaptive_calibrator=None,  # AdaptiveCalibrator
        bybit_ticker_stores: dict = None,   # Bybit OI + funding (real market reference)
        signal_price_stores: dict = None,   # SSI spot prices {symbol: {price, ts_ms, drift_1h}}
    ):
        self.config = config
        self.orderbook_stores = orderbook_stores
        self.mark_price_stores = mark_price_stores
        self.candle_buffers = candle_buffers
        self.trade_flow_stores = trade_flow_stores
        self.health_check = health_check
        self.market_engine = market_engine
        self.calendar_engine = calendar_engine
        self._journal = journal
        self._perf = perf
        self.system_state = system_state
        self._position_manager = position_manager
        self.interpreter = interpreter
        self._ws_manager = ws_manager
        self._dd_tracker = dd_tracker
        self._cascade_tracker = cascade_tracker
        self._adaptive_calibrator = adaptive_calibrator
        self._bybit_ticker_stores = bybit_ticker_stores or {}
        self._signal_price_stores = signal_price_stores or {}
        # SLP vault tracker — set post-init via display._slp_tracker = slp_tracker
        self._slp_tracker = None
        # Sovereign portfolio agent — set post-init via display._sovereign_agent = agent
        self._sovereign_agent = None
        # Per-agent win/loss tracker — set post-init via display._agent_wr = wr
        self._agent_wr = None
        # Phase 11: OutcomeRecorder — set post-init via display._outcome_recorder = recorder
        self._outcome_recorder = None
        # Per-asset previous last price for activity blink detection
        # Using last_price (Bybit trade) not mark_price (SoDEX) — SoDEX marks are
        # infrequent on thin books, Bybit trades stream every second.
        self._prev_mark: dict = {}    # legacy name kept for compat
        self._prev_last: dict = {}    # last_price comparison for activity blink

        # Phase 4.5 Data
        self._funding_snapshots = {}
        self._open_arbs = []

        # v1.4 On-chain + True Arb data
        self._vc_status: dict = {}
        self._true_arb_positions: list = []

        # v1.3 Cached Async Data
        self._calendar_states = {}
        self._upcoming_events = []

        self._equity_history = []  # List of (timestamp, perps balance)
        self._spot_balance: float = 0.0
        self._fee_data: dict = {}
        self.start_time = time.time()
        self._task = None

        # Render-time performance cache — expensive ops throttled to avoid
        # blocking the event loop on every 500ms display tick.
        self._perf_cache: object = None          # last PerformanceStats result
        self._perf_cache_ts: float = 0.0         # epoch time of last compute()
        self._perf_cache_ttl: float = 5.0        # recompute every 5 s

        # Trade candidate log — gates-passed submissions and SoDEX outcomes
        # Increased to 20 so the unified Intelligence Feed has depth.
        self._trade_candidate_log: deque = deque(maxlen=20)

        # Intelligence Feed — active bet from prediction market + regime events
        self._active_bet: dict = None             # current open bet (None = no bet)
        self._regime_events: deque = deque(maxlen=20)  # regime shift events for feed
        self._last_regime: str = ""               # tracks prev regime for change detection

        # v2.0 MarketContext — updated each signal tick from main.py
        self._market_context = None

        # ── Display cache ─────────────────────────────────────────────────────
        # All panel builders read ONLY from here. Populated by _populate_cache()
        # before each render. O(1) render, zero external calls in panel builders.
        self._display_cache: dict = {
            "assets": {},           # symbol → {last_price, mark_price, divergence_pct, imbalance, ob_age, buy_delta, warmup_count, warmup_target, warmup_phase}
            "signals": {},          # symbol → {weighted_score, direction, atr, atr_ratio, coherence_mult, freshness_mult, calendar_mult, sweep, vpin}
            "flow": {},             # symbol → {buy_vol, sell_vol, delta, aggressor_ratio}
            "funding": {},          # funding snapshots dict
            "cascade": None,        # cascade_tracker.get_summary()
            "market_mode": "normal",
            "positions": [],        # open directional positions
            "arb_legs": [],         # true arb positions
            "calendar": {},         # calendar states per asset
            "calendar_events": [],  # upcoming events list
            "session": {},          # perf stats + balance + drawdown
            "equity": [],           # equity history
            "context": None,        # MarketContext
            "system_state": {},     # global phase + counts
            "calibrator": {},       # calibration summary
            "chain": {},            # vc_status
            "mag7": {},             # mag7 state
            "macro": {},            # macro intelligence state
            "fee": {},              # fee engine summary
            "stuck_positions": {},  # symbol → {"count": int, "last_err": str} for unclosed orders
            "active_bet": None,     # current prediction market bet for Intelligence Feed
            "ssi_signals": {},      # {symbol: {price, drift_1h, regime_signal}} from SSI spot feed
            "slp_vault": None,      # SLPSnapshot from SLPVaultTracker
            # Phase 11: Agent accountability
            "agent_states": {},     # {agent_name: AgentOutput | None} — most recent perception
            "agent_accuracy": {},   # {agent_name: AgentAccuracy} from OutcomeRecorder
            "agent_total_trades": 0,  # total closed trades recorded
            "outcome_feed": [],     # last 5 TradeOutcome dicts for display
            "calibration_alerts": [],  # list[str] from OutcomeRecorder.get_calibration_recommendations()
            "last_updated_ms": 0,
        }

    # ── Public update methods ─────────────────────────────────────────────────

    def update_cache(self, key: str, value) -> None:
        """Direct cache key update — used by intelligence loop in main.py."""
        self._display_cache[key] = value

    def update_stuck_positions(self, stuck: dict) -> None:
        """
        Called by stop_guardian in main.py after every failure handling cycle.
        stuck = _stop_close_fails = {symbol: {"count": int, "backoff_until": float, "last_err": str}}
        Header shows ⚠ UNCLOSED alert when any entries remain.
        """
        self._display_cache["stuck_positions"] = dict(stuck)

    def push_trade_candidate(
        self,
        *,
        symbol: str,
        direction: str,
        score: float,
        entry: float,
        stop: float,
        tp1: float,
        size: float,
        leverage: int,
        rr: float,
        status: str,            # "SUBMITTED" | "PLACED" | "REJECTED"
        error: str = None,
        personality: str = None,   # agent personality — feeds Intelligence Feed
        reason: str = None,        # human-readable rejection/context note
    ) -> None:
        """Record a gate-passed trade candidate or its SoDEX outcome."""
        ts       = datetime.now(timezone.utc).strftime("%H:%M:%S")
        ts_epoch = time.time()
        if (self._trade_candidate_log
                and self._trade_candidate_log[-1]["sym"] == symbol
                and self._trade_candidate_log[-1]["status"] == "SUBMITTED"
                and status in ("PLACED", "REJECTED")):
            # Update the existing SUBMITTED entry in-place
            self._trade_candidate_log[-1]["status"] = status
            self._trade_candidate_log[-1]["error"]  = error
            self._trade_candidate_log[-1]["ts"]      = ts
            if reason:
                self._trade_candidate_log[-1]["reason"] = reason
        else:
            self._trade_candidate_log.append({
                "type": "decision",
                "ts": ts, "ts_epoch": ts_epoch,
                "sym": symbol, "dir": direction,
                "score": score, "entry": entry, "stop": stop,
                "tp1": tp1, "size": size, "lev": leverage,
                "rr": rr, "status": status, "error": error,
                "personality": personality,
                "reason": reason or (error[:60] if error else None),
            })

    def update_equity(self, balance: float) -> None:
        self._equity_history.append((int(time.time() * 1000), balance))
        if len(self._equity_history) > 200:
            self._equity_history = self._equity_history[-200:]

    def update_spot_balance(self, balance: float) -> None:
        self._spot_balance = float(balance) if balance is not None else 0.0

    def update_fee_data(self, fee_summary: dict) -> None:
        self._fee_data = fee_summary

    def update_funding(self, snapshots: dict) -> None:
        self._funding_snapshots = snapshots

    def update_arbs(self, arbs: list) -> None:
        self._open_arbs = arbs

    def update_vc_status(self, status: dict) -> None:
        self._vc_status = status

    def update_true_arb_positions(self, positions: list) -> None:
        self._true_arb_positions = positions

    def update_market_context(self, ctx) -> None:
        self._market_context = ctx

    def push_agent_state(self, agent_name: str, output) -> None:
        """
        Called by signal agent perceive() completions to update live agent state.
        output is an AgentOutput instance.
        """
        self._display_cache["agent_states"][agent_name] = output

    def push_outcome(self, outcome_row: dict) -> None:
        """
        Called by OutcomeRecorder after each trade close.
        outcome_row is a flat dict matching the outcomes SQLite schema.
        Keeps last 5 entries for the outcome feed panel.
        """
        feed = self._display_cache.get("outcome_feed", [])
        feed.insert(0, outcome_row)
        self._display_cache["outcome_feed"] = feed[:5]

    def update_active_bet(self, bet: dict | None) -> None:
        """
        Called by prediction market when a bet is placed or resolved.
        bet = {
          agent_a, conf_a, budget_a,
          agent_b, conf_b, budget_b,
          symbol, direction, p_joint, combined, size_mult,
          opened_at (epoch), live_pnl (float|None), resolved (bool)
        }
        Pass None or bet with resolved=True to clear the display.
        """
        self._active_bet = bet

    def push_regime_event(self, from_regime: str, to_regime: str, conf: float = 0.0) -> None:
        """Inject a regime shift event into the Intelligence Feed timeline."""
        ts_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self._regime_events.appendleft({
            "type":     "regime_shift",
            "ts_epoch": time.time(),
            "ts":       ts_str,
            "from":     from_regime.upper().replace("_", " "),
            "to":       to_regime.upper().replace("_", " "),
            "conf":     conf,
        })

    def push_cascade_phase_event(
        self,
        from_phase: str,
        to_phase: str,
        direction: str = "",
        summary: dict = None,
    ) -> None:
        """Inject a cascade phase transition into the Intelligence Feed timeline."""
        ts_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self._regime_events.appendleft({
            "type":      "cascade_shift",
            "ts_epoch":  time.time(),
            "ts":        ts_str,
            "from":      from_phase.upper(),
            "to":        to_phase.upper(),
            "direction": direction.upper() if direction else "",
            "summary":   summary or {},
        })

    def push_bet_event(
        self,
        symbol: str,
        agent_a: str,
        agent_b: str,
        p_joint: float,
        size_mult: float,
    ) -> None:
        """Inject a cross-agent bet confirmation into the Intelligence Feed."""
        ts_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self._regime_events.appendleft({
            "type":      "bet_placed",
            "ts_epoch":  time.time(),
            "ts":        ts_str,
            "sym":       symbol,
            "agent_a":   agent_a[:8],
            "agent_b":   agent_b[:8],
            "p_joint":   round(p_joint, 3),
            "size_mult": round(size_mult, 2),
        })

    # ── Cache population ──────────────────────────────────────────────────────

    def _populate_cache(self) -> None:
        """
        Collect all display data into _display_cache in one pass.
        Called once per render cycle BEFORE generate_layout().
        No external store calls are allowed inside panel builders.
        """
        # ── Per-asset: price, OB, flow, signals ───────────────────────────────
        assets_cache: dict = {}
        flow_cache: dict = {}
        signals_cache: dict = {}

        for asset in self.config.assets:
            a: dict = {
                "last_price": 0.0,
                "mark_price": 0.0,
                "divergence_pct": 0.0,
                "imbalance": 0.0,
                "ob_age": 999999,
                "buy_delta": 0.0,
                "warmup_count": 0,
                "warmup_target": 50,
                "warmup_phase": "WARMING_UP",
            }

            if asset in self.trade_flow_stores:
                store = self.trade_flow_stores[asset]
                lp = store.latest_price()
                if lp is not None:
                    a["last_price"] = lp
                a["buy_delta"] = store.delta()

            if asset in self.mark_price_stores:
                mp_data = self.mark_price_stores[asset].get()
                if mp_data:
                    a["mark_price"] = mp_data.get("mark_price", 0.0)
                    if mp_data.get("last_price", 0) != 0:
                        a["last_price"] = mp_data["last_price"]
                    a["divergence_pct"] = mp_data.get("divergence_pct", 0.0)

            if asset in self.orderbook_stores:
                a["imbalance"] = self.orderbook_stores[asset].imbalance()
                a["ob_age"] = self.orderbook_stores[asset].age_ms()

            # Bybit intelligence — funding rate, OI, 24h change for display
            _bt = self._bybit_ticker_stores.get(asset, {})
            a["bybit_funding"] = _bt.get("funding_rate", 0.0)
            a["bybit_oi"] = _bt.get("open_interest", 0.0)
            a["bybit_oi_prev"] = _bt.get("prev_open_interest", 0.0)
            # Price activity: compare Bybit last_price between renders → blink/color.
            # Using last_price (Bybit L2 trade) not mark_price (SoDEX M) because SoDEX
            # marks update infrequently on thin books — trade prices stream every second.
            _cur_last = a.get("last_price", 0.0)
            _prev_last = self._prev_last.get(asset, _cur_last)
            a["price_up"] = _cur_last > _prev_last and _cur_last > 0
            a["price_down"] = _cur_last < _prev_last and _cur_last > 0
            if _cur_last > 0:
                self._prev_last[asset] = _cur_last
            # Keep mark comparison for divergence (SoDEX vs Bybit spread)
            _cur_mark = a.get("mark_price", 0.0)
            if _cur_mark > 0:
                self._prev_mark[asset] = _cur_mark

            if self.system_state:
                try:
                    status = self.system_state.get_warmup_status().get(asset, {})
                    a["warmup_count"] = status.get("count", 0)
                    a["warmup_target"] = status.get("target", 50)
                    from core.system_state import SystemPhase
                    phase_enum = self.system_state._symbol_phase.get(asset, SystemPhase.WARMING_UP)
                    a["warmup_phase"] = phase_enum.name
                except Exception:
                    pass

            assets_cache[asset] = a

            # Flow volumes
            f: dict = {"buy_vol": 0.0, "sell_vol": 0.0, "delta": 0.0, "aggressor_ratio": 0.0}
            if asset in self.trade_flow_stores:
                store = self.trade_flow_stores[asset]
                f["buy_vol"] = store.buy_volume()
                f["sell_vol"] = store.sell_volume()
                f["delta"] = store.delta()
                f["aggressor_ratio"] = store.aggressor_ratio()
            flow_cache[asset] = f

            # Signal state
            state = None
            if self.interpreter:
                state = self.interpreter.get_market_state(asset)
            elif self.market_engine:
                state = self.market_engine.get_market_state(asset)

            signals_cache[asset] = {
                "weighted_score": getattr(state, "weighted_score", 0.0) if state else 0.0,
                "direction": getattr(state, "trade_direction", "none") if state else "none",
                "atr": getattr(state, "atr", 0.0) if state else 0.0,
                "mark_price": getattr(state, "mark_price", 0.0) if state else 0.0,
                "atr_ratio": getattr(state, "atr_vs_baseline", 1.0) if state else 1.0,
                "coherence_mult": getattr(state, "coherence_mult", 0.0) if state else 0.0,
                "freshness_mult": getattr(state, "freshness_mult", 1.0) if state else 1.0,
                "calendar_mult": getattr(state, "calendar_mult", 1.0) if state else 1.0,
                "sweep": getattr(state, "sweep", "none") if state else "none",
                "vpin": getattr(state, "vpin", 0.0) if state else 0.0,
            }

        # ── Positions ─────────────────────────────────────────────────────────
        positions = []
        if self._position_manager:
            try:
                positions = list(self._position_manager.get_all())
            except Exception:
                pass

        # ── Session / performance stats ────────────────────────────────────────
        balance = self._equity_history[-1][1] if self._equity_history else 0.0
        deployed = sum(getattr(p, "initial_margin", 0.0) for p in positions)
        session: dict = {
            "win_rate": 0.0, "total_pnl": 0.0, "sqn": 0.0, "closed": 0,
            "balance": balance,
            "spot_balance": self._spot_balance or 0.0,
            "deployed": deployed,
            "dd_pct": 0.0, "dd_regime": "normal", "dd_streak": 0,
            "uptime_s": int(time.time() - self.start_time),
        }
        if self._perf and self._journal:
            try:
                # Recompute at most every 5 s — journal.compute() iterates all
                # closed entries and is expensive at 4 Hz on 60+ trade journals.
                _now_pc = time.time()
                if self._perf_cache is None or (_now_pc - self._perf_cache_ts) >= self._perf_cache_ttl:
                    self._perf_cache = self._perf.compute(self._journal)
                    self._perf_cache_ts = _now_pc
                stats = self._perf_cache
                if stats is not None:
                    session["win_rate"] = stats.win_rate * 100
                    session["total_pnl"] = stats.total_pnl_usd
                    session["sqn"] = stats.sqn
                    session["closed"] = stats.closed_trades
            except Exception:
                pass
        if self._dd_tracker:
            try:
                session["dd_pct"] = self._dd_tracker.session_drawdown_pct
                session["dd_regime"] = self._dd_tracker.drawdown_regime
                session["dd_streak"] = self._dd_tracker.consecutive_losses
            except Exception:
                pass

        # ── System state ──────────────────────────────────────────────────────
        system_data: dict = {"global_phase": "OFFLINE", "active_signals": 0, "live_trades": 0}
        if self.system_state:
            try:
                system_data["global_phase"] = self.system_state.get_global_phase().value.upper()
            except Exception:
                pass
        # Count only signals that are both directional AND above the coherence floor.
        # A raw direction ≠ "none" is insufficient — low-conviction signals (score < 3.0)
        # pollute the header count with noise.  3.0 matches the risk-engine minimum for
        # most regime states.  This keeps the header in sync with the signal engine panel.
        _sig_floor = max(getattr(self.config, "live_min_coherence", 1.0), 3.0)
        system_data["active_signals"] = sum(
            1 for s in signals_cache.values()
            if s["direction"] != "none"
               and s["weighted_score"] >= _sig_floor
               and s.get("atr", 0.0) > 0
               and s.get("mark_price", 0.0) > 0
        )
        system_data["live_trades"] = len(positions)

        # ── Calibrator ────────────────────────────────────────────────────────
        calibrator_data: dict = {}
        if self._adaptive_calibrator is not None:
            try:
                calibrator_data = self._adaptive_calibrator.get_calibration_summary()
            except Exception:
                pass

        # ── Cascade ───────────────────────────────────────────────────────────
        cascade_data = None
        if self._cascade_tracker is not None:
            try:
                cascade_data = self._cascade_tracker.get_summary()
            except Exception:
                pass

        # ── Chain / macro / mag7 ─────────────────────────────────────────────
        chain_data = dict(self._vc_status) if self._vc_status else {}

        mag7_data: dict = {}
        if self.interpreter and hasattr(self.interpreter, "_mag7"):
            try:
                _m = self.interpreter._mag7
                mag7_data = {
                    "stale": _m.is_stale(),
                    "direction": _m.direction,
                    "strength": _m.strength,
                }
            except Exception:
                pass

        macro_data: dict = {}
        if self.interpreter and hasattr(self.interpreter, "_macro"):
            try:
                _ms = self.interpreter._macro.state
                macro_data = {
                    "macro_direction": _ms.macro_direction,
                    "assets_confirming": _ms.assets_confirming,
                    "assets_total_active": _ms.assets_total_active,
                    "capitulation_detected": _ms.capitulation_detected,
                    "post_event_active": _ms.post_event_active,
                    "funding_regime": _ms.funding_regime,
                    "xaut_direction": _ms.xaut_direction,
                    "xaut_confirms_regime": _ms.xaut_confirms_regime,
                    "xaut_macro_mult": _ms.xaut_macro_mult,
                    "volume_quality_mult": _ms.volume_quality_mult,
                }
            except Exception:
                pass

        # ── SSI signal prices — from SSI spot feed ────────────────────────────
        ssi_data: dict = {}
        _SSI_LABELS = {
            "MAG7SSI-USD": "MAG7ssi",
            "DEFISSI-USD": "DEFIssi",
            "MEMESSI-USD": "MEMEssi",
            "USSI-USD":    "USSI   ",
        }
        for _sym, _label in _SSI_LABELS.items():
            _sp = self._signal_price_stores.get(_sym, {})
            _price    = float(_sp.get("price", 0.0))
            _drift    = float(_sp.get("drift_1h", 0.0))
            # Derive a brief regime label from drift magnitude + direction
            if _sym == "MAG7SSI-USD":
                _sig = ("tech_inflow ↑" if _drift > 0.008 else
                        "tech_outflow ↓" if _drift < -0.008 else "equity_flat →")
            elif _sym == "DEFISSI-USD":
                _sig = ("defi_flow ↑" if _drift > 0.010 else
                        "defi_stress ↓" if _drift < -0.010 else "defi_neutral →")
            elif _sym == "MEMESSI-USD":
                _sig = ("meme_watch ↑" if _drift > 0.015 else
                        "meme_fade ↓" if _drift < -0.015 else "meme_quiet →")
            else:  # USSI
                _sig = ("equity_bid ↑" if _drift > 0.005 else
                        "equity_soft ↓" if _drift < -0.005 else "equity_flat →")
            ssi_data[_sym] = {
                "label":   _label,
                "price":   _price,
                "drift_1h": _drift,
                "regime_signal": _sig,
            }

        # ── SLP vault snapshot ────────────────────────────────────────────────
        slp_snap = None
        if self._slp_tracker is not None:
            try:
                slp_snap = self._slp_tracker.get_snapshot()
            except Exception:
                pass

        # ── Phase 11: Signal agent states + outcome recorder ─────────────────
        _agent_states: dict = self._display_cache.get("agent_states", {})
        _agent_accuracy: dict = self._display_cache.get("agent_accuracy", {})
        _agent_total_trades: int = self._display_cache.get("agent_total_trades", 0)
        _outcome_feed: list = self._display_cache.get("outcome_feed", [])
        _calibration_alerts: list = self._display_cache.get("calibration_alerts", [])

        # ── Sovereign portfolio agent snapshot ────────────────────────────────
        _sovereign_portfolio_data = None
        if self._sovereign_agent is not None:
            try:
                _sovereign_portfolio_data = self._sovereign_agent.get_display_data()
            except Exception:
                pass

        # ── Regime change detection — auto-push shift events to feed ─────────
        _cur_regime = getattr(self._market_context, "regime", "") if self._market_context else ""
        if _cur_regime and _cur_regime != self._last_regime and self._last_regime:
            _cur_conf = getattr(self._market_context, "regime_confidence", 0.0)
            self.push_regime_event(self._last_regime, _cur_regime, _cur_conf)
        if _cur_regime:
            self._last_regime = _cur_regime

        # ── Commit to cache atomically ────────────────────────────────────────
        self._display_cache.update({
            "assets": assets_cache,
            "signals": signals_cache,
            "flow": flow_cache,
            "funding": dict(self._funding_snapshots),
            "cascade": cascade_data,
            "market_mode": self._market_context.market_mode if self._market_context else "normal",
            "positions": positions,
            "arb_legs": list(self._true_arb_positions or []),
            "calendar": dict(self._calendar_states),
            "calendar_events": list(self._upcoming_events),
            "session": session,
            "equity": list(self._equity_history),
            "context": self._market_context,
            "system_state": system_data,
            "calibrator": calibrator_data,
            "chain": chain_data,
            "mag7": mag7_data,
            "macro": macro_data,
            "fee": dict(self._fee_data) if self._fee_data else {},
            "active_bet": self._active_bet,
            "ssi_signals":        ssi_data,
            "slp_vault":          slp_snap,
            "sovereign_portfolio": _sovereign_portfolio_data,
            # Phase 11 — carry forward (updated externally via update_cache)
            "agent_states":       _agent_states,
            "agent_accuracy":     _agent_accuracy,
            "agent_total_trades": _agent_total_trades,
            "outcome_feed":       _outcome_feed,
            "calibration_alerts": _calibration_alerts,
            "last_updated_ms":    int(time.monotonic() * 1000),
        })

    # ── Safety wrapper ─────────────────────────────────────────────────────────

    def _safe_panel(self, builder_method, title: str) -> Panel:
        """Prevent panel builder exceptions from crashing the UI."""
        try:
            return builder_method()
        except Exception as e:
            structlog.get_logger(__name__).error(f"panel_build_error_{title}", error=str(e))
            return Panel(
                f"[red]Error: {str(e)}[/red]\n{traceback.format_exc() if self.config.debug else ''}",
                title=f"[red]{title}[/red]",
                border_style="red"
            )

    # ── Render loop ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Consolidated rendering loop with screen takeover."""
        if self.calendar_engine:
            try:
                self._calendar_states = await self.calendar_engine.get_states_all(self.config.assets)
                self._upcoming_events = await self.calendar_engine.event_store.get_upcoming(hours_ahead=72)
            except Exception:
                pass

        # Purge any stdout that leaked before Rich takes over (pre-init logs,
        # shell echos) — ANSI clear + cursor-home so nothing bleeds through
        # the alternate screen buffer when screen=True switches to it.
        import sys as _sys_live
        _sys_live.stdout.write("\033[2J\033[H")
        _sys_live.stdout.flush()

        # 2 Hz render — trading terminal needs responsiveness, not animation.
        # At 4 Hz the display consumed ~76 % of its 250 ms budget just rendering;
        # 2 Hz (500 ms) halves that load and keeps the event loop free for execution.
        with Live(self.generate_layout(), refresh_per_second=2, screen=True) as live:
            _cal_tick = 0  # calendar refresh counter
            while True:
                try:
                    # Calendar refresh every 10 ticks = every 5 s.
                    # get_states_all() hits SQLite; calling it every 250 ms was
                    # a significant hidden bottleneck on journals with 60+ entries.
                    _cal_tick += 1
                    if self.calendar_engine and _cal_tick >= 10:
                        _cal_tick = 0
                        self._calendar_states = await self.calendar_engine.get_states_all(self.config.assets)
                        if int(time.time()) % 60 == 0:
                            self._upcoming_events = await self.calendar_engine.event_store.get_upcoming(hours_ahead=72)

                    # Populate cache — one data-collection pass before rendering
                    self._populate_cache()

                    # Render timing — skip frame if layout takes > 100ms to avoid blocking event loop
                    try:
                        async with asyncio.timeout(0.1):
                            _t_render = time.monotonic()
                            layout = self.generate_layout()
                            _render_ms = (time.monotonic() - _t_render) * 1000
                            if _render_ms > 250:
                                logger.warning("slow_render", elapsed_ms=round(_render_ms, 1))
                            live.update(layout)
                    except asyncio.TimeoutError:
                        logger.debug("render_skipped_timeout")
                except Exception as _e:
                    import traceback as _tb
                    logger.error("display_render_error",
                                 error=str(_e),
                                 traceback=_tb.format_exc().strip())
                await asyncio.sleep(0.5)

    def _build_ssi_signals_panel(self) -> Panel:
        """
        SSI Regime Signals — live spot prices and 1h drift for the four SSI tokens.
        Drives mag7_led, defi_active, meme_euphoria, equity_led regime states.
        Latency: O(4). Reads from _display_cache["ssi_signals"] only.
        """
        ssi = self._display_cache.get("ssi_signals") or {}
        ctx = self._display_cache.get("context")
        regime = getattr(ctx, "regime", "").lower() if ctx else ""

        _SSI_ORDER = ["MAG7SSI-USD", "DEFISSI-USD", "MEMESSI-USD", "USSI-USD"]
        _REGIME_MATCH = {
            "MAG7SSI-USD": "mag7_led",
            "DEFISSI-USD": "defi_active",
            "MEMESSI-USD": "meme_euphoria",
            "USSI-USD":    "equity_led",
        }

        header = (
            f" [dim]{'Token':<9}{'Price':>8}  {'1h Drift':>8}  Signal[/]"
        )
        lines = [header, " [dim]" + "─" * 44 + "[/]"]

        for sym in _SSI_ORDER:
            d = ssi.get(sym)
            if d is None:
                lines.append(f" [dim]{sym.replace('-USD',''):<9}{'—':>8}  {'—':>8}  warming up[/]")
                continue

            label    = d["label"]
            price    = d["price"]
            drift    = d["drift_1h"]
            sig      = d["regime_signal"]
            is_active = _REGIME_MATCH.get(sym, "") == regime

            price_str = f"${price:.4f}" if price > 0 else "—"
            drift_str = f"{drift*100:+.2f}%" if price > 0 else "—"
            drift_col = "#00d4aa" if drift >= 0 else "#ff3d5a"
            sig_col   = "#f5c842" if is_active else "#555566"
            label_col = "#e8edf2" if is_active else "dim"

            lines.append(
                f" [{label_col}]{label}[/]"
                f" [#888899]{price_str:>8}[/]"
                f"  [{drift_col}]{drift_str:>8}[/]"
                f"  [{sig_col}]{sig}[/]"
            )

        return Panel(
            Text.from_markup("\n".join(lines)),
            title="[bold #4d9fff]◈ SSI REGIME SIGNALS[/]",
            style="#e8edf2 on #080809",
            border_style="#4d9fff",
            padding=(0, 1),
        )

    def _build_slp_vault_panel(self) -> Panel:
        """
        SLP Vault + SOSO Staking — yield accounting for the SLP vault position.
        All values read from SLPVaultTracker (which reads from env vars — never hardcoded).
        Latency: O(1). Reads from _display_cache["slp_vault"] only.
        """
        snap = self._display_cache.get("slp_vault")

        if snap is None:
            lines = [
                " [dim]SLP Vault not configured.[/]",
                " [dim]Set SLP_VAULT_SMAG7_DEPOSITED in .env[/]",
            ]
            return Panel(
                Text.from_markup("\n".join(lines)),
                title="[bold #9b6dff]◆ SLP VAULT[/]",
                style="#e8edf2 on #080809",
                border_style="#3a3a4a",
                padding=(0, 1),
            )

        deposited  = snap.smag7_deposited
        entry_usd  = snap.vault_usd_at_entry
        curr_usd   = snap.vault_usd_current
        days_held  = snap.days_held
        idx_yield  = snap.index_yield_usd
        mm_yield   = snap.mm_revenue_usd
        total_yld  = snap.total_yield_30d_usd
        apy        = snap.annualised_apy
        funding    = snap.funding_collected_usd
        hedge      = snap.hedge_status
        entry_px   = snap.mag7ssi_price_entry
        curr_px    = snap.mag7ssi_price

        apy_pct    = apy * 100
        apy_col    = "#00d4aa" if apy_pct >= 15 else ("#f5c842" if apy_pct >= 5 else "#ff3d5a")
        yld_col    = "#00d4aa" if total_yld >= 0 else "#ff3d5a"
        hedge_col  = "#ff3d5a" if "SHORT" in hedge else "#888899"

        vault_lines = [
            "[bold #9b6dff]SLP VAULT[/]",
            f" [dim]Deposited[/]  [#e8edf2]{deposited:.4f} sMAG7[/]"
            + (f" [dim](${entry_usd:.2f} entry)[/]" if entry_usd > 0 else ""),
        ]
        if curr_px > 0 and entry_px > 0:
            px_chg_pct = (curr_px - entry_px) / entry_px * 100
            px_col = "#00d4aa" if px_chg_pct >= 0 else "#ff3d5a"
            vault_lines.append(
                f" [dim]Price[/]     [#888899]${entry_px:.4f}[/] → "
                f"[{px_col}]${curr_px:.4f} ({px_chg_pct:+.2f}%)[/]"
            )
        if curr_usd > 0:
            vault_lines.append(f" [dim]Value now[/]  [#e8edf2]${curr_usd:.2f}[/]  [dim]{days_held}d held[/]")
        vault_lines += [
            f" [dim]Idx yield[/]  [{yld_col}]+${idx_yield:.4f}[/] [dim](30d)[/]",
            f" [dim]MM rev[/]    [{yld_col}]+${mm_yield:.4f}[/] [dim](30d)[/]",
            f" [dim]Total[/]     [{yld_col}]+${total_yld:.4f}[/]  [{apy_col}]{apy_pct:.1f}% APY[/]",
        ]
        if funding > 0:
            vault_lines.append(f" [dim]Funding[/]   [#00d4aa]+${funding:.4f}[/]")
        vault_lines.append(f" [dim]Hedge[/]     [{hedge_col}]{hedge}[/]")

        staking_lines = [
            "",
            "[bold #9b6dff]SOSO STAKING[/]",
            f" [dim]Staked[/]    [#e8edf2]{snap.soso_staked:.0f} SOSO[/]",
            f" [dim]Discount[/]  [{apy_col}]{snap.soso_discount_pct:.1f}%[/] [dim]off all fees[/]",
        ]
        if snap.soso_saved_30d_usd > 0:
            staking_lines.append(
                f" [dim]Saved[/]     [#00d4aa]+${snap.soso_saved_30d_usd:.4f}[/] [dim](30d)[/]"
            )

        border = "#9b6dff" if deposited > 0 else "#3a3a4a"
        return Panel(
            Text.from_markup("\n".join(vault_lines + staking_lines)),
            title="[bold #9b6dff]◆ SLP VAULT + SOSO[/]",
            style="#e8edf2 on #080809",
            border_style=border,
            padding=(0, 1),
        )

    def generate_layout(self) -> Layout:
        layout = Layout()
        layout.split(
            Layout(name="header", size=3),
            Layout(name="body")
        )
        layout["body"].split_row(
            Layout(name="left", ratio=3),
            Layout(name="center", ratio=4),
            Layout(name="right", ratio=3),
        )

        layout["header"].update(self._safe_panel(self._build_header, "Header"))

        # ── LEFT: scanner → agents → compact positions (replaces trade candidates) ──
        layout["left"].split(
            Layout(name="market_scanner",    ratio=4),
            Layout(name="agents_panel",      ratio=3),
            Layout(name="compact_positions", ratio=2),
        )
        layout["left"]["market_scanner"].update(self._safe_panel(self._build_assets_panel, "Assets"))
        layout["left"]["agents_panel"].update(self._safe_panel(self._build_agents_panel, "Agents"))
        layout["left"]["compact_positions"].update(self._safe_panel(self._build_compact_positions_panel, "Positions"))

        # ── CENTER: [news | chain] → equity → intelligence feed → open positions ──
        layout["center"].split(
            Layout(name="center_top",     size=8),
            Layout(name="equity_curve",   size=7),
            Layout(name="intelligence",   ratio=3),
            Layout(name="open_positions", ratio=1),
        )
        layout["center"]["center_top"].split_row(
            Layout(name="market_mode",        ratio=1),
            Layout(name="chain_intelligence", ratio=1),
        )
        layout["center"]["market_mode"].update(self._safe_panel(self._build_context_panel, "Market News"))
        layout["center"]["chain_intelligence"].update(self._safe_panel(self._build_chain_intelligence_panel, "Chain Intelligence"))
        layout["center"]["equity_curve"].update(self._safe_panel(self._build_equity_curve_panel, "Equity Curve"))
        layout["center"]["intelligence"].update(self._safe_panel(self._build_intelligence_feed_panel, "Intelligence Feed"))
        layout["center"]["open_positions"].update(self._safe_panel(self._build_open_positions_panel, "Open Positions"))

        # ── RIGHT: sovereign → arb → signal agents (Phase 11) → SSI → SLP vault ─
        layout["right"].split(
            Layout(name="sovereign_panel",    ratio=4),
            Layout(name="true_arb_positions", ratio=1),
            Layout(name="signal_agents",      ratio=2),
            Layout(name="ssi_signals",        size=8),
            Layout(name="slp_vault",          size=11),
        )
        layout["right"]["sovereign_panel"].update(self._safe_panel(self._build_sovereign_panel, "SOVEREIGN"))
        layout["right"]["true_arb_positions"].update(self._safe_panel(self._build_true_arb_panel, "True Arb Positions"))
        layout["right"]["signal_agents"].update(self._safe_panel(self._build_signal_agents_panel, "Signal Agents"))
        layout["right"]["ssi_signals"].update(self._safe_panel(self._build_ssi_signals_panel, "SSI Signals"))
        layout["right"]["slp_vault"].update(self._safe_panel(self._build_slp_vault_panel, "SLP Vault"))

        return layout

    # ── Panel builders — read ONLY from _display_cache ────────────────────────

    def _build_header(self) -> Panel:
        now  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        mode = self.config.mode.upper()

        if mode == "LIVE":
            mode_badge  = "[bold #ff6b2b] LIVE [/]"
            border      = "#ff6b2b"
        elif mode == "TESTNET":
            mode_badge  = "[bold #f5c842] TESTNET [/]"
            border      = "#f5c842"
        else:
            mode_badge  = "[#888899] PAPER [/]"
            border      = "#3a3a4a"

        sys_d        = self._display_cache.get("system_state", {})
        global_phase = sys_d.get("global_phase", "OFFLINE")
        live_trades  = sys_d.get("live_trades", 0)
        active_sigs  = sys_d.get("active_signals", 0)
        stuck        = self._display_cache.get("stuck_positions", {})

        phase_col  = "#00d4aa" if global_phase in ("TRADING", "READY") else "#ff3d5a"
        trade_col  = "#f5c842 blink" if live_trades > 0 else "dim"
        sig_col    = "#f5c842" if active_sigs > 0 else "dim"

        if stuck:
            _parts = [f"{s}({(v.get('count',1) if isinstance(v,dict) else 1)})" for s, v in stuck.items()]
            _sc    = "bold #ff3d5a blink" if any((v.get("count",1) if isinstance(v,dict) else 1) >= 5 for v in stuck.values()) else "bold #f5c842 blink"
            order_seg = f"[{_sc}]⚠ UNCLOSED: {' '.join(_parts)}[/]"
        else:
            order_seg = "[dim]✓ ORDERS CLEAN[/]"

        header_text = Text.from_markup(
            f"[bold #ff6b2b]ARIA[/] [dim #888899]v1.3[/]  "
            f"[bold]{mode_badge}[/]  "
            f"[dim]│[/]  [{phase_col}]{global_phase}[/]  "
            f"[dim]│[/]  [dim]{now}[/]  "
            f"[dim]│[/]  [{trade_col}]{live_trades} TRADE{'S' if live_trades != 1 else ''}[/]  "
            f"[{sig_col}]{active_sigs} SIG{'S' if active_sigs != 1 else ''}[/]  "
            f"[dim]│[/]  [bold #00d4aa]● SoDEX MAINNET[/]  "
            f"[dim]│[/]  {order_seg}"
        )
        header_text.justify = "center"
        return Panel(header_text, style="on #080809", border_style=border, padding=(0, 0))

    def _build_context_panel(self) -> Panel:
        """
        Market News panel — most visually prominent element in the UI.
        Two display states:
          CASCADE: phase, cascade direction, notional, aftermath signals, weight overrides
          NORMAL:  regime, top flow biases, funding spread summary, next event
        """
        ctx = self._display_cache.get("context")
        if ctx is None:
            return Panel(
                "[dim]Awaiting market context...[/dim]",
                title="[dim]◈ MARKET NEWS[/dim]",
                border_style="dim"
            )

        # Override market_mode from live cascade_tracker on every render tick.
        # _display_cache["context"] is only updated when on_signal_ready() fires —
        # after a cascade, the quiet market filter blocks signals, leaving mode="normal"
        # even though cascade_tracker is BLOCKED. Read the tracker directly so the
        # panel reflects the real cascade phase regardless of signal activity.
        mode = ctx.market_mode
        if self._cascade_tracker is not None:
            try:
                _live_phase = self._cascade_tracker.get_phase().value
                if _live_phase == "blocked":
                    mode = "cascade_blocked"
                elif _live_phase == "momentum":
                    mode = "cascade_momentum"
                elif _live_phase == "primed":
                    mode = "cascade_primed"
            except Exception:
                pass

        _MODE_STYLE = {
            "cascade_blocked":  ("bold #ff4444 blink", "⛔ CASCADE BLOCKED",  "red"),
            "cascade_momentum": ("bold #ff8c00 blink", "⚡ CASCADE MOMENTUM", "#ff8c00"),
            "cascade_primed":   ("bold #00d084",       "◈ CASCADE PRIMED",    "#00d084"),
            "calendar_caution": ("bold #f5a623",       "⚠ CALENDAR CAUTION", "#f5a623"),
            "defensive":        ("bold #ff6b6b",       "🛡 DEFENSIVE",        "#ff6b6b"),
            "normal":           ("bold #00aaff",       "● NORMAL",            "#00aaff"),
        }
        text_style, label, border = _MODE_STYLE.get(
            mode, ("bold #00aaff", "● NORMAL", "#00aaff")
        )

        t = Text()
        t.append(f" {label} ", style=text_style)

        if mode in ("cascade_blocked", "cascade_momentum", "cascade_primed"):
            # Pull live cascade data directly from tracker (ctx may be stale when
            # quiet market filter suppresses signals after a cascade fires).
            _c_direction = ctx.cascade_direction
            _c_notional  = ctx.cascade_notional
            _c_type      = ctx.cascade_type
            _c_aftermath = ctx.cascade_aftermath_count
            if self._cascade_tracker is not None:
                try:
                    _snap = self._cascade_tracker.get_snapshot()
                    _aft  = self._cascade_tracker.get_aftermath_signals()
                    if _snap is not None:
                        _c_direction = _snap.batch_direction
                        _c_notional  = _snap.batch_notional_usd
                        _c_type      = "momentum" if self._cascade_tracker._is_momentum_cascade(
                            _snap.velocity, _snap.batch_notional_usd) else "exhaustion"
                    if _aft:
                        _c_aftermath = sum(1 for v in _aft.values() if v)
                except Exception:
                    pass

            _is_bear_dir = _c_direction in ("bearish", "short")
            _is_bull_dir = _c_direction in ("bullish", "long")
            direction_char = "▼ BEAR" if _is_bear_dir else ("▲ BULL" if _is_bull_dir else "? N/A")
            dir_color = "#ff4444" if _is_bear_dir else ("#00d084" if _is_bull_dir else "#888888")
            notional_str = (
                f"${_c_notional / 1_000_000:.1f}M"
                if _c_notional >= 1_000_000
                else f"${_c_notional / 1_000:.0f}k"
                if _c_notional >= 1_000
                else f"${_c_notional:.0f}"
            )
            t.append(f"\n Cascade: ", style="dim")
            t.append(direction_char, style=dir_color)
            t.append(f"  Notional: {notional_str}", style="bold")
            t.append(f"  Type: {_c_type.upper()}", style="dim")
            t.append(f"\n Aftermath signals: ", style="dim")
            t.append(f"{_c_aftermath}/5", style=(
                "bold #00d084" if _c_aftermath >= 3 else
                "bold #f5a623" if _c_aftermath >= 1 else "dim"
            ))
            if ctx.signal_weights:
                weight_str = "  ".join(
                    f"{k[:4]}:{v:.1f}×" for k, v in sorted(ctx.signal_weights.items())
                    if v != 1.0
                )
                t.append(f"\n Weights: {weight_str}", style="#888888")
        else:
            conf_pct = f"{ctx.regime_confidence * 100:.0f}%"
            t.append(f"\n Regime: {ctx.regime.replace('_', ' ').upper()}  ", style="dim")
            t.append(f"conf {conf_pct}", style="dim")

            buy_syms = [s for s, b in ctx.flow_bias.items() if b == "buy"]
            sell_syms = [s for s, b in ctx.flow_bias.items() if b == "sell"]
            if buy_syms or sell_syms:
                flow_parts = []
                if buy_syms:
                    flow_parts.append(f"[#00d084]▲ {' '.join(s.replace('-USD', '') for s in buy_syms[:3])}[/]")
                if sell_syms:
                    flow_parts.append(f"[#ff4444]▼ {' '.join(s.replace('-USD', '') for s in sell_syms[:3])}[/]")
                t.append("\n Flow:  ", style="dim")
                t.append_text(Text.from_markup("  ".join(flow_parts)))

            if ctx.calendar_hours is not None:
                hrs = ctx.calendar_hours
                cal_color = "#ff4444" if hrs < 1 else "#f5a623" if hrs < 4 else "#888888"
                t.append(f"\n Event: ", style="dim")
                t.append(f"{hrs:.1f}h away  ({ctx.calendar_regime})", style=cal_color)
            else:
                t.append(f"\n Calendar: clear", style="dim #888888")

            # ── Leading SSI Leg — which capital leg is dominating ──────────────
            _ssi_data = self._display_cache.get("ssi_signals", {})
            _SSI_LEG_LABELS = {
                "MAG7SSI-USD": ("MAG7", "#00d4aa"),
                "DEFISSI-USD": ("DEFI", "#4d9fff"),
                "MEMESSI-USD": ("MEME", "#f5c842"),
                "USSI-USD":    ("USSI", "#9b6dff"),
            }
            # Find the leg with highest absolute 1h drift
            _best_leg = max(
                ((sym, d) for sym, d in _ssi_data.items() if d.get("price", 0) > 0),
                key=lambda x: abs(x[1].get("drift_1h", 0.0)),
                default=(None, None),
            )
            if _best_leg[0] is not None:
                _leg_sym, _leg_d = _best_leg
                _leg_label, _leg_col = _SSI_LEG_LABELS.get(_leg_sym, ("?", "#888899"))
                _leg_drift = _leg_d.get("drift_1h", 0.0)
                _drift_sign = "▲" if _leg_drift >= 0 else "▼"
                _drift_col  = "#00d084" if _leg_drift >= 0 else "#ff4757"
                t.append(f"\n Lead:  ", style="dim")
                t.append(f"[{_leg_col}]{_leg_label}[/]", style=_leg_col)
                t.append(f"  [{_drift_col}]{_drift_sign}{abs(_leg_drift)*100:.2f}%[/]",
                         style=_drift_col)
                _leg_sig = _leg_d.get("regime_signal", "")
                if _leg_sig:
                    t.append(f"  {_leg_sig}", style="dim #666666")

            # ── Time Regime Overlay ────────────────────────────────────────────
            _tr_phase = getattr(ctx, "time_regime_phase", "")
            _tr_notes = getattr(ctx, "time_regime_notes", "")
            if _tr_phase or _tr_notes:
                _tr_color = "#f5a623" if "event" in _tr_phase or "block" in _tr_phase else "#888888"
                t.append(f"\n Phase: ", style="dim")
                t.append(f"{_tr_phase}", style=_tr_color)
                if _tr_notes:
                    # Show first note segment only to keep display compact
                    _first_note = _tr_notes.split(" | ")[0]
                    t.append(f"  {_first_note}", style="dim #666666")

        return Panel(t, title=f"[bold {border}]◈ MARKET NEWS[/]", border_style=border, padding=(0, 1))

    def _build_assets_panel(self) -> Panel:
        """
        Compact market scanner — one row per asset.

        Columns (execution-critical only, no decorative padding):
          SYM | PRICE | DIV% | OB | SCORE | DIR | FR% | OI | STATUS

        Replaces 15 individual mini-panels (one per coin). A single Rich table
        renders in ~1ms vs ~15ms for 15 nested panels; critical at 2 Hz.
        """
        table = Table(
            expand=True,
            style="#e8edf2 on #080809",
            border_style="#1a1a24",
            show_lines=False,
            padding=(0, 0),
        )
        table.add_column("SYM",    min_width=7,  no_wrap=True)
        table.add_column("PRICE",  min_width=9,  justify="right", no_wrap=True)
        table.add_column("DIV",    min_width=5,  justify="right", no_wrap=True)
        table.add_column("SCR",    min_width=4,  justify="right", no_wrap=True)
        table.add_column("DIR",    min_width=5,  no_wrap=True)
        table.add_column("OB",     min_width=5,  justify="right", no_wrap=True)
        table.add_column("STATUS", min_width=9,  no_wrap=True)

        assets_cache  = self._display_cache.get("assets", {})
        signals_cache = self._display_cache.get("signals", {})
        calendar_cache = self._display_cache.get("calendar", {})
        positions = self._display_cache.get("positions", [])
        open_syms  = {getattr(p, "symbol", "") for p in positions}
        open_sides = {getattr(p, "symbol", ""): getattr(p, "side", "long") for p in positions}

        for asset in self.config.assets:
            a   = assets_cache.get(asset, {})
            sig = signals_cache.get(asset, {})

            sym_short = asset.replace("-USD", "")

            # ── Price ──────────────────────────────────────────────────────────
            price     = a.get("last_price", 0.0) or a.get("mark_price", 0.0)
            price_up  = a.get("price_up", False)
            price_dn  = a.get("price_down", False)
            _px_color = "#00d084" if price_up else ("#ff4757" if price_dn else "#aaaaaa")
            _blink    = " blink" if (price_up or price_dn) else ""
            if price >= 1000:
                price_str = f"{price:,.0f}"
            elif price >= 1:
                price_str = f"{price:.3f}"
            elif price > 0:
                price_str = f"{price:.5f}"
            else:
                price_str = "—"

            # ── Divergence % ───────────────────────────────────────────────────
            div = a.get("divergence_pct", 0.0)
            div_color = "#f5a623" if abs(div) > 0.05 else "dim"
            div_str   = f"{div:+.2f}" if div != 0 else "—"

            # ── Orderbook age ──────────────────────────────────────────────────
            ob_age = a.get("ob_age", 999999)
            if ob_age > 10000:
                ob_str   = "—"
                ob_color = "dim"
            elif ob_age > 1000:
                ob_str   = f"{ob_age // 1000}s"
                ob_color = "#f5a623"
            else:
                ob_str   = f"{ob_age}ms"
                ob_color = "#00d084"

            # ── Signal score + direction ────────────────────────────────────────
            w_score   = sig.get("weighted_score", 0.0)
            direction = sig.get("direction", "none").upper()
            scr_color = "#00d084" if w_score >= 5.0 else ("#f5a623" if w_score >= 3.5 else "dim")
            scr_str   = f"{w_score:.1f}" if w_score > 0 else "—"

            if direction == "LONG":
                dir_cell = "[bold #00d084]▲ L[/]"
            elif direction == "SHORT":
                dir_cell = "[bold #ff4757]▼ S[/]"
            else:
                dir_cell = "[dim]—[/]"

            # ── Funding rate ───────────────────────────────────────────────────
            fr      = a.get("bybit_funding", 0.0)
            fr_pct  = fr * 100.0
            fr_color = "#ff4757" if fr_pct > 0.01 else ("#00d084" if fr_pct < -0.01 else "dim")
            fr_str   = f"{fr_pct:+.4f}" if fr != 0 else "—"

            # ── Open Interest ──────────────────────────────────────────────────
            oi      = a.get("bybit_oi", 0.0)
            oi_prev = a.get("bybit_oi_prev", 0.0)
            oi_delta = oi - oi_prev if oi_prev > 0 else 0.0
            if oi_delta > 0:
                oi_color, oi_arrow = "#00d084", "↑"
            elif oi_delta < 0:
                oi_color, oi_arrow = "#ff4757", "↓"
            else:
                oi_color, oi_arrow = "dim", "─"
            oi_str = f"{oi_arrow}{oi/1e6:.1f}M" if oi > 0 else "—"

            # ── Status cell — execution readiness, highest priority first ──────
            warmup_phase = a.get("warmup_phase", "WARMING_UP")
            warmup_count = a.get("warmup_count", 0)
            cal = calendar_cache.get(asset)
            cal_regime = getattr(cal, "regime", "CLEAR") if cal else "CLEAR"
            cal_hours  = getattr(cal, "hours_to_event", None) if cal else None
            cal_event  = getattr(cal, "nearest_event_name", "") if cal else ""

            in_trade = asset in open_syms
            if in_trade:
                _pos_side = open_sides.get(asset, "long")
                _px_color = "#00d084" if _pos_side == "long" else "#ff4757"

            if in_trade:
                status_str  = "● OPEN"
                status_color = "#f5a623"
            elif warmup_phase == "WARMING_UP":
                status_str  = f"~{warmup_count}/50"
                status_color = "#555555"
            elif cal_regime == "BLOCK":
                _mkt_closed = any(x in (cal_event or "").upper()
                                  for x in ("MARKET_CLOSED", "WEEKEND", "EQUITY_MARKET",
                                            "COMMODITY_MARKET", "CLOSED"))
                if _mkt_closed:
                    status_str = "CLOSED"
                else:
                    tag = cal_event[:6] if cal_event else "EVT"
                    status_str = f"BLK {tag}"
                status_color = "#ff4757"
            elif cal_hours is not None and 0 < cal_hours < 1.0:
                tag = cal_event[:6] if cal_event else "EVT"
                _is_wkd = "WEEKEND" in (cal_event or "").upper()
                status_str  = f"WKD {cal_hours*60:.0f}m" if _is_wkd else f"⚡{tag} {cal_hours*60:.0f}m"
                status_color = "#ff4757"
            elif cal_hours is not None and 0 < cal_hours < 4.0:
                status_str  = f"~{cal_hours:.1f}h"
                status_color = "#f5a623"
            elif w_score >= 4.5 and direction not in ("NONE", "none"):
                arrow = "▲" if direction == "LONG" else "▼"
                status_str  = f"{arrow}STRONG {w_score:.1f}"
                status_color = "#00d084" if direction == "LONG" else "#ff4757"
            else:
                status_str  = "READY"
                status_color = "#00d084"

            # ── Highlight row if this coin has an open position ────────────────
            row_style = "bold" if in_trade else ""

            table.add_row(
                f"[bold {_px_color}]{sym_short}[/]",
                f"[{_px_color}{_blink}]{price_str}[/]",
                f"[{div_color}]{div_str}[/]",
                f"[{scr_color}]{scr_str}[/]",
                dir_cell,
                f"[{ob_color}]{ob_str}[/]",
                f"[{status_color}]{status_str}[/]",
                style=row_style,
            )

        return Panel(
            table,
            title="[bold #4d9fff]◆ MARKET SCANNER[/]",
            style="#e8edf2 on #080809",
            border_style="#1a1a24",
            padding=(0, 0),
        )

    def _build_intelligence_panel(self) -> Panel:
        table = Table(expand=True, style="#e8edf2 on #080809", border_style="#1a1a24", show_lines=False)
        table.add_column("SYM",   min_width=8)
        table.add_column("WTD",   justify="right", min_width=5)
        table.add_column("DIR",   min_width=6)
        table.add_column("ATR",   justify="right", min_width=7)
        table.add_column("ATR×",  justify="right", min_width=5)
        table.add_column("COH×",  justify="right", min_width=5)
        table.add_column("FRSH×", justify="right", min_width=5)

        signals = self._display_cache.get("signals", {})

        for asset in self.config.assets:
            sig       = signals.get(asset, {})
            wtd       = sig.get("weighted_score", 0.0)
            direction = sig.get("direction", "none").upper()
            atr       = sig.get("atr", 0.0)
            atr_ratio = sig.get("atr_ratio", 1.0)
            coh_mult  = sig.get("coherence_mult", 0.0)
            frsh_mult = sig.get("freshness_mult", 1.0)

            scr_col = (
                "bold white"   if wtd >= 9.0 else   # elite — full alignment
                "bold #ff6b2b" if wtd >= 7.0 else   # exceptional
                "#ff6b2b"      if wtd >= 5.0 else   # institutional
                "#f5c842"      if wtd >= 4.0 else   # active
                "#00d4aa"      if wtd >= 3.0 else "dim"
            )
            dir_col = "#00d4aa" if direction == "LONG" else ("#ff3d5a" if direction == "SHORT" else "dim")
            atr_col = "#ff3d5a" if atr_ratio > 1.5 else ("#f5c842" if atr_ratio > 1.2 else "#00d4aa")
            coh_col = "#00d4aa" if coh_mult >= 1.0 else ("dim" if coh_mult == 0.0 else "#f5c842")

            if atr >= 1000:
                atr_str = f"{atr:,.0f}"
            elif atr >= 1:
                atr_str = f"{atr:.2f}"
            elif atr > 0:
                atr_str = f"{atr:.4f}"
            else:
                atr_str = "—"

            dir_cell = (
                f"[bold {dir_col}]▲ LONG[/]"  if direction == "LONG"  else
                f"[bold {dir_col}]▼ SHORT[/]" if direction == "SHORT" else
                f"[dim]—[/]"
            )

            table.add_row(
                f"[bold]{asset.replace('-USD', '')}[/]",
                f"[{scr_col}]{wtd:.1f}[/]" if wtd > 0 else "[dim]—[/]",
                dir_cell,
                atr_str,
                f"[{atr_col}]{atr_ratio:.2f}[/]",
                f"[{coh_col}]{coh_mult:.2f}[/]",
                f"{frsh_mult:.2f}",
            )

        mag7 = self._display_cache.get("mag7", {})
        mag7_sfx = ""
        if mag7:
            if mag7.get("stale"):
                mag7_sfx = "  [dim]MAG7: STALE[/]"
            elif mag7.get("direction") == "bullish":
                mag7_sfx = f"  [#00d4aa]MAG7: BULL {mag7.get('strength', 0.0):.2f}[/]"
            elif mag7.get("direction") == "bearish":
                mag7_sfx = f"  [#ff3d5a]MAG7: BEAR {mag7.get('strength', 0.0):.2f}[/]"
            else:
                mag7_sfx = "  [dim]MAG7: NEUT[/]"

        return Panel(
            table,
            title=f"[bold #ff6b2b]► SIGNAL ENGINE — LIVE TIER ANALYSIS[/]{mag7_sfx}",
            style="#e8edf2 on #080809",
            border_style="#2a2a3a",
        )

    def _build_trade_flow(self) -> Panel:
        table = Table(expand=True, style="#e8edf2 on #0d1014", border_style="#4a5a6a")
        table.add_column("Asset")
        table.add_column("Buy Vol", justify="right")
        table.add_column("Sell Vol", justify="right")
        table.add_column("Delta", justify="right")
        table.add_column("Ratio", justify="right")

        flow = self._display_cache.get("flow", {})
        for asset in self.config.assets:
            f = flow.get(asset, {})
            bv = f.get("buy_vol", 0.0)
            sv = f.get("sell_vol", 0.0)
            delta = f.get("delta", 0.0)
            ratio = f.get("aggressor_ratio", 0.0)
            delta_color = "#00d084" if delta >= 0 else "#ff4757"
            table.add_row(
                asset,
                f"{bv:,.2f}",
                f"{sv:,.2f}",
                f"[{delta_color}]{delta:+,.2f}[/]",
                f"{ratio:.2f}"
            )

        return Panel(table, title="Trade Flow (60s)", style="#e8edf2 on #0d1014", border_style="#4a5a6a")

    def _build_calendar_panel(self) -> Panel:
        """
        Unified calendar panel — single source of truth for all calendar data.
        Top section: per-asset regime/multipliers from CalendarEngine.get_states_all().
        Bottom section: upcoming events (72h) from CalendarEngine.event_store.get_upcoming().
        Both read from _display_cache["calendar"] and _display_cache["calendar_events"] —
        populated once per cycle by _populate_cache(), never fetched inside the panel builder.
        """
        from rich.console import Group as RichGroup

        # ── Per-asset regime table ─────────────────────────────────────────────
        status_table = Table(
            show_header=True, expand=True,
            style="#e8edf2 on #0d1014", border_style="#4a5a6a",
            title="[bold #aaaaaa]Asset Status[/]",
        )
        status_table.add_column("Asset", min_width=8)
        status_table.add_column("Regime", justify="center", min_width=8)
        status_table.add_column("Size", justify="right", min_width=5)
        status_table.add_column("Stop", justify="right", min_width=5)
        status_table.add_column("Note", no_wrap=False)

        states = self._display_cache.get("calendar", {})
        _cal_signals = self._display_cache.get("signals", {})
        # Time regime phase — same source as Market News; integrated per-asset note
        _ctx = self._display_cache.get("context")
        _tr_phase = getattr(_ctx, "time_regime_phase", "") if _ctx else ""
        _tr_notes = getattr(_ctx, "time_regime_notes", "") if _ctx else ""
        # Abbreviate time regime for per-asset note (keep it short)
        _tr_abbrev = ""
        if _tr_phase:
            _phase_map = {
                "early_week": "early wk",
                "mid_week":   "mid wk",
                "late_week":  "late wk",
                "mid_month_tue_wed": "mid-mo chop",
                "mid_month":  "mid-mo",
                "event_block": "event⚡",
                "pre_open":   "pre-open",
                "lunch_lull": "lunch",
                "power_hour": "power hr",
            }
            _tr_abbrev = _phase_map.get(_tr_phase, _tr_phase.replace("_", " "))

        if states:
            for asset, s in states.items():
                reason_str = (s.reason or "").replace("_", " ")
                if s.regime == "BLOCK":
                    regime_color = "#ff4757"
                    asset_str = f"[bold #ff4757]{asset}[/]"
                elif s.regime == "CAUTION":
                    regime_color = "#f5a623"
                    asset_str = f"[#f5a623]{asset}[/]"
                else:
                    regime_color = "#4a5a6a"
                    asset_str = f"[dim]{asset}[/dim]"

                # Signal direction prefix — shows real-time market bias
                _sig = _cal_signals.get(asset, {})
                _dir = _sig.get("direction", "none")
                _score = _sig.get("weighted_score", 0.0)
                if _dir == "long" and _score > 0:
                    _dir_prefix = "[#00d084]↑[/] "
                elif _dir == "short" and _score > 0:
                    _dir_prefix = "[#ff4757]↓[/] "
                else:
                    _dir_prefix = ""

                evt_name = (s.nearest_event_name or "")[:16] or "—"
                hrs = s.hours_to_event
                note_body = f"{evt_name} {hrs:.1f}h" if hrs is not None and hrs > 0 else reason_str or "clear"
                # Append time regime for CLEAR assets so context is visible per-row
                if _tr_abbrev and s.regime == "CLEAR":
                    note_body = f"{note_body} | [dim #666666]{_tr_abbrev}[/]"
                elif _tr_abbrev and s.regime != "BLOCK":
                    note_body = f"{note_body} [{_tr_abbrev}]"
                note = f"{_dir_prefix}{note_body}"
                status_table.add_row(
                    asset_str,
                    f"[{regime_color}]{s.regime}[/]",
                    f"{float(s.size_multiplier):.2f}×",
                    f"{float(s.stop_atr_multiplier):.1f}×",
                    f"[dim]{note}[/dim]" if s.regime == "CLEAR" and not _dir_prefix else note,
                )
        else:
            status_table.add_row("[dim]Loading…[/dim]", "—", "—", "—", "—")

        # ── Upcoming events table ──────────────────────────────────────────────
        events_table = Table(
            show_header=True, expand=True,
            style="#e8edf2 on #0d1014", border_style="#4a5a6a",
            title="[bold #aaaaaa]Upcoming (72h)[/]",
        )
        events_table.add_column("Event", no_wrap=False)
        events_table.add_column("Type", min_width=6)
        events_table.add_column("In", justify="right", min_width=5)

        events = self._display_cache.get("calendar_events", [])
        if events:
            now = datetime.now(timezone.utc)
            for ev in events[:8]:   # cap at 8 rows to fit panel
                delta = ev.event_time - now
                total_s = delta.total_seconds()
                hours = total_s / 3600.0
                if hours > 1:
                    countdown = f"{hours:.1f}h"
                else:
                    countdown = f"{total_s / 60:.0f}m"
                color = "#ff4757" if hours < 6 else ("#f5a623" if hours < 24 else "white")
                events_table.add_row(ev.name[:22], ev.event_type, f"[{color}]{countdown}[/]")
        else:
            events_table.add_row("[dim]No events scheduled[/dim]", "—", "—")

        # ── Time regime overlay + signal weights ──────────────────────────────────
        # Mirror the same time-regime and weight info shown in Market News so the
        # calendar panel is self-contained (day-of-week, time-of-month, event block,
        # plus any active signal tier-weight overrides).
        ctx = self._display_cache.get("context")
        _group_items = [status_table, events_table]
        if ctx is not None:
            tr_phase = getattr(ctx, "time_regime_phase", "")
            tr_notes = getattr(ctx, "time_regime_notes", "")
            sw       = getattr(ctx, "signal_weights", {}) or {}

            _overlay_parts: list = []
            if tr_phase:
                _is_event = "event" in tr_phase or "block" in tr_phase
                _ph_color = "#f5a623" if _is_event else "#888888"
                _overlay_parts.append(
                    f"[dim]Time:[/dim] [{_ph_color}]{tr_phase}[/]"
                )
            if tr_notes:
                # Show all notes, pipe-separated, truncated for space
                _first_note = tr_notes.split(" | ")[0]
                _overlay_parts.append(f"[dim #666666]{_first_note}[/]")
            non_default_w = {k: v for k, v in sw.items() if v != 1.0}
            if non_default_w:
                _wstr = "  ".join(
                    f"[#888888]{k[:5]}:[/][bold]{v:.1f}×[/]"
                    for k, v in sorted(non_default_w.items())
                )
                _overlay_parts.append(f"[dim]Wts:[/dim] {_wstr}")

            if _overlay_parts:
                from rich.text import Text as RichText
                _overlay_text = RichText.from_markup("  ".join(_overlay_parts))
                _group_items.append(_overlay_text)

        return Panel(
            RichGroup(*_group_items),
            title="[bold]CALENDAR[/bold]",
            style="#e8edf2 on #0d1014",
            border_style="#4a5a6a",
        )

    def _build_funding_radar(self) -> Panel:
        table = Table(expand=True, style="#e8edf2 on #0d1014", border_style="#4a5a6a")
        table.add_column("Asset")
        table.add_column("Rate", justify="right")
        table.add_column("Carry", justify="right")
        table.add_column("Signal", justify="center")
        table.add_column("Direction")

        funding = self._display_cache.get("funding", {})
        # Augment with live Bybit funding rates for any symbol without a FundingRadar snap
        _bybit_rates_for_display = {
            s: d.get("funding_rate", 0.0)
            for s, d in self._bybit_ticker_stores.items()
            if d.get("funding_rate", 0.0) != 0.0
        }
        for asset, snap in funding.items():
            try:
                rate = getattr(snap, "rate", 0.0)
                score = getattr(snap, "carry_score", 0.0)
                signal = getattr(snap, "arb_signal", False)
                direction = getattr(snap, "direction", "none")
                rate_color = "#00d084" if rate > 0 else "#ff4757"
                table.add_row(
                    str(asset),
                    f"[{rate_color}]{rate * 100:.4f}%[/]" if isinstance(rate, float) else str(rate),
                    f"{score:.2f}" if isinstance(score, float) else str(score),
                    "✓" if signal else "✗",
                    str(direction or "—")
                )
            except Exception as e:
                logger.error("funding_row_render_error", asset=str(asset), error=str(e))
                table.add_row(str(asset), "ERROR", "ERROR", "ERROR", "ERROR")

        return Panel(table, title="FUNDING RADAR", style="#e8edf2 on #0d1014", border_style="#4a5a6a")

    def _build_open_positions_panel(self) -> Panel:
        import time as _time
        table = Table(expand=True, style="#e8edf2 on #0d1014", border_style="#00aaff", show_lines=False)
        table.add_column("Sym", min_width=5)
        table.add_column("Dir", min_width=5)
        table.add_column("Entry", justify="right", min_width=9)
        table.add_column("Mark", justify="right", min_width=9)
        table.add_column("uPnL", justify="right", min_width=8)
        table.add_column("Stop", justify="right", min_width=10)
        table.add_column("TPs", justify="right", min_width=14)
        table.add_column("Lev", justify="right", min_width=4)
        table.add_column("Age", justify="right", min_width=7)
        table.add_column("Liq%", justify="right", min_width=5)

        positions = self._display_cache.get("positions", [])
        assets_cache = self._display_cache.get("assets", {})
        now_ms = int(_time.time() * 1000)

        if not positions:
            table.add_row("[dim]—[/]", "[dim]No open positions[/]", "", "", "", "", "", "", "", "")
        else:
            for pos in positions:
                sym = getattr(pos, "symbol", "?")
                side = getattr(pos, "side", "long")
                entry = getattr(pos, "entry_price", 0.0)
                stop = getattr(pos, "stop_price", 0.0)
                tp1 = getattr(pos, "tp1_price", 0.0)
                tp2 = getattr(pos, "tp2_price", 0.0)
                tp3 = getattr(pos, "tp3_price", 0.0)
                size = getattr(pos, "size", 0.0)
                lev = getattr(pos, "leverage", 1)
                liq = getattr(pos, "liq_price", 0.0)
                tp1_hit = getattr(pos, "tp1_hit", False)
                tp2_hit = getattr(pos, "tp2_hit", False)
                opened_at = getattr(pos, "opened_at_ms", now_ms)

                # Use cached mark price (no live store call)
                mark = assets_cache.get(sym, {}).get("mark_price", entry) or entry
                if mark <= 0:
                    mark = entry

                upnl = (mark - entry) * size if side == "long" else (entry - mark) * size
                dir_color = "#00d084" if side == "long" else "#ff4757"
                pnl_color = "#00d084" if upnl >= 0 else "#ff4757"
                sym_short = sym.replace("-USD", "")

                def _fmt_price(p: float) -> str:
                    if p >= 1000:
                        return f"{p:,.1f}"
                    elif p >= 1:
                        return f"{p:.3f}"
                    return f"{p:.5f}"

                stop_str = _fmt_price(stop) if stop > 0 else "[bold #ff4444]NO STOP[/]"

                def _tp_label(price: float, hit: bool, label: str) -> str:
                    if price <= 0:
                        return f"[dim]{label}:—[/]"
                    marker = "[#00d084]✓[/]" if hit else ""
                    return f"{marker}{label}:{_fmt_price(price)}"

                tp_str = " ".join([
                    _tp_label(tp1, tp1_hit, "T1"),
                    _tp_label(tp2, tp2_hit, "T2"),
                    _tp_label(tp3, False, "T3"),
                ])

                age_ms = max(0, now_ms - opened_at)
                age_s = age_ms // 1000
                if age_s < 60:
                    age_str = f"{age_s}s"
                elif age_s < 3600:
                    age_str = f"{age_s // 60}m{age_s % 60:02d}s"
                else:
                    age_str = f"{age_s // 3600}h{(age_s % 3600) // 60:02d}m"

                if liq > 0 and mark > 0:
                    liq_pct = abs(mark - liq) / mark * 100
                    liq_color = "#ff4444" if liq_pct < 5 else ("#f5a623" if liq_pct < 15 else "#888888")
                    liq_str = f"[{liq_color}]{liq_pct:.1f}%[/]"
                else:
                    liq_str = "[dim]—[/]"

                # Glow indicator — pulses based on PnL direction, settles once closed
                _glow = "◉" if upnl >= 0 else "◎"
                _glow_col = "#00d084" if upnl >= 0 else "#ff4757"

                table.add_row(
                    f"[bold {_glow_col}]{_glow} {sym_short}[/]",
                    f"[{dir_color}]{side.upper()}[/]",
                    _fmt_price(entry),
                    _fmt_price(mark),
                    f"[bold {pnl_color}]{upnl:+.2f}[/]",
                    stop_str,
                    tp_str,
                    f"{lev}x",
                    age_str,
                    liq_str,
                )

        mode        = self.config.mode.upper()
        title_color = "#ff3d5a" if mode == "LIVE" else "#888899"
        # Border glows when positions are live and unsettled
        _border_col = "#00aaff" if positions else "#2a2a3a"
        _pos_count  = len(positions)
        _count_str  = f" {_pos_count} LIVE" if _pos_count > 0 else ""
        return Panel(
            table,
            title=f"[bold {title_color}]◉ OPEN BETS ({mode}){_count_str}[/]",
            style="#e8edf2 on #080809",
            border_style=_border_col,
        )

    def _build_trade_candidates_panel(self) -> Panel:
        table = Table(expand=True, style="#e8edf2 on #0d1014", border_style="#f5a623", show_lines=False)
        table.add_column("Time", min_width=8)
        table.add_column("Sym", min_width=6)
        table.add_column("Dir", min_width=5)
        table.add_column("Scr", justify="right", min_width=4)
        table.add_column("Entry", justify="right", min_width=8)
        table.add_column("Stop", justify="right", min_width=7)
        table.add_column("RR", justify="right", min_width=4)
        table.add_column("Status", min_width=10)

        if not self._trade_candidate_log:
            table.add_row("[dim]—[/]", "[dim]No candidates yet[/]", "", "", "", "", "", "")
        else:
            for entry in reversed(self._trade_candidate_log):
                status = entry["status"]
                if status == "PLACED":
                    status_str = "[bold #00d084]✓ PLACED[/]"
                elif status == "REJECTED":
                    err_short = (entry.get("error") or "unknown")[:20]
                    status_str = f"[bold #ff4757]✗ REJECTED[/] [dim]{err_short}[/]"
                else:
                    status_str = "[bold #f5a623]⟳ SENT[/]"

                dir_color = "#00d084" if entry["dir"] == "long" else "#ff4757"
                sym_short = entry["sym"].replace("-USD", "")

                def _fp(p: float) -> str:
                    if p >= 1000:
                        return f"{p:,.1f}"
                    elif p >= 1:
                        return f"{p:.3f}"
                    return f"{p:.5f}"

                table.add_row(
                    f"[dim]{entry['ts']}[/]",
                    f"[bold]{sym_short}[/]",
                    f"[{dir_color}]{entry['dir'].upper()[:5]}[/]",
                    f"{entry['score']:.1f}",
                    _fp(entry["entry"]),
                    _fp(entry["stop"]),
                    f"{entry['rr']:.1f}R",
                    status_str,
                )

        return Panel(
            table,
            title="[bold #f5a623]▶ TRADE CANDIDATES[/]",
            style="#e8edf2 on #0d1014",
            border_style="#f5a623"
        )

    def _build_arb_positions(self) -> Panel:
        table = Table(expand=True, style="#e8edf2 on #0d1014", border_style="#4a5a6a")
        table.add_column("Asset")
        table.add_column("Direction")
        table.add_column("Size", justify="right")
        table.add_column("Entry", justify="right")
        table.add_column("Current", justify="right")
        table.add_column("Notional", justify="right")
        table.add_column("P&L", justify="right")

        assets_cache = self._display_cache.get("assets", {})
        for pos in self._open_arbs:
            try:
                symbol = getattr(pos, "symbol", "unknown")
                direction = getattr(pos, "direction", "none")
                size = getattr(pos, "size", 0.0)
                entry = getattr(pos, "entry_price", 0.0)
                pnl = getattr(pos, "current_pnl", 0.0)
                current_price = assets_cache.get(symbol, {}).get("mark_price", 0.0)
                notional = size * current_price
                pnl_color = "green" if pnl >= 0 else "red"
                table.add_row(
                    str(symbol),
                    str(direction),
                    f"{size:.4f}",
                    f"${entry:,.2f}" if entry > 0 else "—",
                    f"${current_price:,.2f}",
                    f"${notional:,.2f}",
                    f"[{pnl_color}]${pnl:+.4f}[/]"
                )
            except Exception as e:
                logger.error("arb_row_render_error", error=str(e))
                table.add_row("ERROR", "...", "0.0000", "$0.00", "$0.00", "$0.00", "$0.0000")

        return Panel(table, title="ACTIVE ARB LEGS", style="#e8edf2 on #0d1014", border_style="#4a5a6a")

    def _build_allocation_panel(self) -> Panel:
        total_slots = 30
        session = self._display_cache.get("session", {})
        balance = session.get("balance", 0.0)
        spot_balance = session.get("spot_balance", 0.0)
        deployed = session.get("deployed", 0.0)

        total_capital = deployed + balance
        deploy_pct = min(1.0, deployed / total_capital) if total_capital > 0 else 0.0
        filled = int(deploy_pct * total_slots)
        free_slots = total_slots - filled
        deploy_color = "#00ff88" if deploy_pct < 0.25 else "#ffcc00" if deploy_pct < 0.60 else "#ff4455"
        bar = f"[{deploy_color}]{'█' * filled}[/][dim]{'░' * free_slots}[/dim]"

        spot_line = (
            f"  [dim]Spot: [bold]${spot_balance:,.2f}[/bold][/dim]"
            if spot_balance > 0 else ""
        )
        content = (
            f"[dim]Alloc:[/dim] [{deploy_color}]{deploy_pct * 100:.1f}%[/] deployed  "
            f"[dim]|[/dim]  [bold]${deployed:,.2f}[/bold] / [dim]Perps ${balance:,.2f}[/dim]{spot_line}\n"
            f"[{bar}]"
        )
        return Panel(Text.from_markup(content), style="#e8edf2 on #0d1014", border_style="#4a5a6a")

    def _build_stats_panel(self) -> Panel:
        session = self._display_cache.get("session", {})
        uptime_s = session.get("uptime_s", 0)
        hours, rem = divmod(uptime_s, 3600)
        mins, secs = divmod(rem, 60)
        uptime_str = f"{hours:02d}:{mins:02d}:{secs:02d}"

        win_rate = session.get("win_rate", 0.0)
        total_pnl = session.get("total_pnl", 0.0)
        sqn = session.get("sqn", 0.0)
        closed = session.get("closed", 0)
        balance = session.get("balance", 0.0)
        spot_balance = session.get("spot_balance", 0.0)
        deployed = session.get("deployed", 0.0)
        dd_pct = session.get("dd_pct", 0.0)
        dd_regime = session.get("dd_regime", "normal")
        dd_streak = session.get("dd_streak", 0)

        _total_cap = deployed + balance
        deploy_pct = (deployed / _total_cap * 100) if _total_cap > 0 else 0.0

        mode = self.config.mode.upper()
        mode_color = "#ff4444" if mode == "LIVE" else ("#f5a623" if mode == "TESTNET" else "#888888")
        pnl_color = "#00d084" if total_pnl >= 0 else "#ff4757"
        sqn_color = "#00d084" if sqn >= 2.0 else ("#f5a623" if sqn >= 1.0 else "dim")
        _dd_regime_color = {
            "normal": "#00d084",
            "caution": "#f5a623",
            "defensive": "#ff4757",
            "halt": "bold #ff0000",
        }.get(dd_regime, "dim")

        grid = Table.grid(expand=True, padding=(0, 1))
        grid.add_column()
        grid.add_column()
        grid.add_column()
        grid.add_column()

        bal_label = (
            f"[bold]Perps[/] ${balance:,.2f}  [dim]Spot ${spot_balance:,.2f}[/]"
            if spot_balance > 0
            else f"[bold]Bal[/] ${balance:,.2f}"
        )
        grid.add_row(
            bal_label,
            f"[bold]Dep[/] ${deployed:,.2f} ({deploy_pct:.1f}%)",
            f"[bold]WR[/] [yellow]{win_rate:.1f}%[/] ({closed}T)",
            f"[bold]P&L[/] [{pnl_color}]${total_pnl:+.2f}[/]"
        )
        grid.add_row(
            f"[bold]SQN[/] [{sqn_color}]{sqn:.2f}[/]",
            f"[{mode_color}][bold]{mode}[/][/]",
            f"[dim]Up {uptime_str}[/]",
            f"[bold]DD[/] [{_dd_regime_color}]{dd_pct:.1f}% {dd_regime.upper()}[/]  [dim]L-Str {dd_streak}[/]"
        )

        cal = self._display_cache.get("calibrator", {})
        if cal:
            try:
                coh_min = cal.get("coherence_min", 0.0)
                loss_str = cal.get("loss_streak", 0)
                fast_wr = cal.get("fast_wr", 0.0)
                med_wr = cal.get("medium_wr", 0.0)
                coh_color = "#ff4757" if coh_min > 3.5 else ("#f5a623" if coh_min > 2.5 else "#00d084")
                grid.add_row(
                    f"[bold]Coh-Min[/] [{coh_color}]{coh_min:.1f}[/]",
                    f"[dim]L-Str[/] {loss_str}",
                    f"[dim]WR5[/] {fast_wr * 100:.0f}%  [dim]WR10[/] {med_wr * 100:.0f}%",
                    f"[dim]Adaptive[/]",
                )
            except Exception:
                pass

        return Panel(grid, title="[bold #888899]■ SESSION[/]",
                     style="#e8edf2 on #080809", border_style="#2a2a3a")

    def _build_chain_intelligence_panel(self) -> Panel:
        chain = self._display_cache.get("chain", {})
        healthy = chain.get("healthy", False)
        last_block = chain.get("last_block", 0)
        events_60s = chain.get("events_60s", 0)
        cascade_active = chain.get("cascade_active", False)
        rpc = chain.get("rpc_endpoint", "—")
        failures = chain.get("consecutive_failures", 0)

        if cascade_active:
            status_str = "[bold red blink]⚠ CASCADE — DO NOT TRADE[/]"
            border_style = "bold red"
        elif healthy:
            status_str = "[bold #00d084]● LIVE[/]"
            border_style = "#00d084"
        else:
            status_str = f"[bold #f5a623]◌ RECONNECTING ({failures}×)[/]"
            border_style = "#f5a623"

        rpc_short = rpc.replace("https://", "").split("/")[0][:28]
        block_str = f"{last_block:,}" if last_block else "—"
        events_color = "#ff4757" if events_60s >= 3 else ("#f5a623" if events_60s >= 1 else "dim")

        content = (
            f"[dim]Chain:[/dim]  SoDEX EVM (ID 286623)   {status_str}\n"
            f"[dim]RPC:[/dim]    [dim]{rpc_short}[/dim]\n"
            f"[dim]Block:[/dim]  {block_str}   "
            f"[dim]Liqs/60s:[/dim] [{events_color}]{events_60s}[/]"
        )

        signals = chain.get("active_signals", [])
        if signals:
            sig_lines = []
            for s in signals[:3]:
                sym = s.get("symbol", "?")[:4]
                src = s.get("source", "?")[:10]
                d = s.get("direction", "?")[0].upper()
                str_ = s.get("strength", 0.0)
                age = s.get("age_s", 0)
                clr = "#00d084" if d == "L" else "#ff4757"
                sig_lines.append(f"[{clr}]{sym:5} {src:12} {d} {str_:.2f}[/]  [dim]{age}s ago[/]")
            content += "\n" + "\n".join(sig_lines)

        # Cascade state from cache
        cascade = self._display_cache.get("cascade")
        if cascade is not None:
            try:
                phase = cascade.get("phase", "idle").upper()
                phase_colors = {
                    "IDLE":      "dim",
                    "DETECTING": "#f5a623",
                    "BLOCKED":   "bold #ff4757 blink",
                    "PRIMED":    "bold #00d084 blink",
                    "MOMENTUM":  "bold #ffcc00 blink",
                }
                p_clr = phase_colors.get(phase, "dim")
                snap = cascade.get("snapshot") or {}
                snap_dir = snap.get("direction", "")
                snap_note = f"${snap.get('notional_usd', 0):,.0f}" if snap.get("notional_usd") else ""
                primed_dir = cascade.get("primed_direction", "") or cascade.get("momentum_direction", "")
                vel = cascade.get("velocity", 0.0)
                aftermath = cascade.get("aftermath_signals", {})
                aft_str = " ".join(
                    f"[#00d084]{k[:4]}[/]" if v else f"[dim]{k[:4]}[/]"
                    for k, v in aftermath.items()
                ) if aftermath else "[dim]—[/]"
                cascade_line = (
                    f"\n[dim]─────────── CASCADE ───────────[/]"
                    f"\n[{p_clr}]{phase}[/]"
                    + (f"  [{('#00d084' if primed_dir == 'long' else '#ff4757')}]{primed_dir.upper()}[/]" if primed_dir else "")
                    + (f"  {snap_dir[:4].upper()}" if snap_dir else "")
                    + (f"  {snap_note}" if snap_note else "")
                    + (f"  vel={vel:.2f}" if vel else "")
                    + (f"\n{aft_str}" if aftermath else "")
                )
                content += cascade_line
            except Exception:
                pass

        # Macro intelligence from cache
        macro = self._display_cache.get("macro", {})
        if macro:
            try:
                _mdir = macro.get("macro_direction", "neutral")
                _mclr = "#00d084" if _mdir == "long" else ("#ff4757" if _mdir == "short" else "dim")
                _at = macro.get("assets_total_active", 0)
                _confirming = f"{macro.get('assets_confirming', 0)}/{_at}" if _at > 0 else "—"
                _cap = "[bold #ff4757]CAP[/]" if macro.get("capitulation_detected") else "[dim]—[/]"
                _pe = "[bold #f5a623]POST-EVT[/]" if macro.get("post_event_active") else "[dim]—[/]"
                _fr = macro.get("funding_regime", "").replace("crowded_", "CR:").upper()
                _fr_clr = "#f5a623" if "CR:" in _fr else "dim"
                _xaut_dir = macro.get("xaut_direction", "")
                _xaut_conf = macro.get("xaut_confirms_regime", False)
                _xaut_mult = macro.get("xaut_macro_mult", 1.0)
                _xaut_str = (
                    f"[#00d084]AU↑ {_xaut_mult:.2f}×[/]" if _xaut_dir == "long" and _xaut_conf
                    else f"[#ff4757]AU↓ {_xaut_mult:.2f}×[/]" if _xaut_dir == "short" and _xaut_conf
                    else "[dim]AU—[/]"
                )
                content += (
                    f"\n[dim]────────────── MACRO ──────────────[/]"
                    f"\n[{_mclr}]{_mdir.upper():5}[/] {_confirming}  [{_fr_clr}]{_fr}[/]  {_xaut_str}"
                    f"  {_cap}  {_pe}"
                    f"  [dim]vol {macro.get('volume_quality_mult', 1.0):.2f}×[/]"
                )
            except Exception:
                pass

        # SSI / DeFi stress signals
        ssi_cache = self._display_cache.get("ssi_signals", {})
        if ssi_cache:
            try:
                _defi = ssi_cache.get("DEFISSI-USD", {})
                _mag7 = ssi_cache.get("MAG7SSI-USD", {})
                _meme = ssi_cache.get("MEMESSI-USD", {})
                _ussi = ssi_cache.get("USSI-USD", {})
                def _ssi_row(label: str, data: dict, clr_pos: str, clr_neg: str) -> str:
                    sig   = data.get("regime_signal", "")
                    drift = data.get("drift_1h", 0.0)
                    price = data.get("price", 0.0)
                    if not sig or price <= 0:
                        return f"[dim]{label} —[/]"
                    is_neg = "stress" in sig or "outflow" in sig or "fade" in sig or "soft" in sig
                    col = clr_neg if is_neg else (clr_pos if drift != 0.0 else "dim")
                    return (
                        f"[{col}]{label}[/]"
                        f" [dim]{sig}[/]"
                        f"  [{col}]{drift*100:+.2f}%[/]"
                    )
                _d_row = _ssi_row("DEFI", _defi, "#4d9fff", "#ff4757")
                _m_row = _ssi_row("MAG7", _mag7, "#00d4aa", "#ff3d5a")
                _e_row = _ssi_row("MEME", _meme, "#f5c842", "#888899")
                _u_row = _ssi_row("USSI", _ussi, "#9b6dff", "#888899")
                content += (
                    f"\n[dim]──────────── SSI SIGNALS ───────────[/]"
                    f"\n{_d_row}"
                    f"\n{_m_row}   {_e_row}"
                    f"\n{_u_row}"
                )
            except Exception:
                pass

        return Panel(
            Text.from_markup(content),
            title="[bold #00e5ff]# CHAIN INTELLIGENCE[/]",
            style="#e8edf2 on #080809",
            border_style=border_style,
        )

    def _build_true_arb_panel(self) -> Panel:
        table = Table(expand=True, style="#e8edf2 on #0d1014",
                      border_style="#aa77ff", show_lines=False)
        table.add_column("Sym", min_width=5)
        table.add_column("Dir", min_width=10)
        table.add_column("Qty", justify="right", min_width=7)
        table.add_column("Entry$", justify="right", min_width=8)
        table.add_column("Held", justify="right", min_width=6)
        table.add_column("Fund$", justify="right", min_width=7)

        positions = self._display_cache.get("arb_legs", [])
        if not positions:
            table.add_row("[dim]—[/]", "[dim]No true arb positions[/]", "", "", "", "")
        else:
            now_t = time.time()
            for pos in positions:
                sym = getattr(pos, "symbol", "?").replace("-USD", "")
                direction = getattr(pos, "direction", "?")
                qty = getattr(pos, "spot_qty", 0.0)
                entry = getattr(pos, "spot_entry", 0.0)
                opened_at = getattr(pos, "opened_at", now_t)
                hold_h = (now_t - opened_at) / 3600
                funding = getattr(pos, "funding_collected_usd", 0.0)

                dir_short = "L↑S↓" if "long_spot" in direction else "S↓L↑"
                dir_color = "#00d084" if "long_spot" in direction else "#ff4757"
                hold_color = "#00d084" if hold_h >= 8 else "#f5a623"

                table.add_row(
                    f"[bold]{sym}[/]",
                    f"[{dir_color}]{dir_short}[/]",
                    f"{qty:.4f}",
                    f"${entry:,.2f}" if entry > 0 else "—",
                    f"[{hold_color}]{hold_h:.1f}h[/]",
                    f"[bold #00d084]${funding:.4f}[/]" if funding > 0 else "[dim]$0.0000[/]",
                )

        return Panel(
            table,
            title="[bold #00e5ff]⇌ TRUE ARB — SPOT+PERP[/]",
            style="#e8edf2 on #080809",
            border_style="#00e5ff",
        )

    def _build_fee_intelligence_panel(self) -> Panel:
        fee = self._display_cache.get("fee", {})
        if not fee:
            return Panel(
                Text.from_markup("[dim]Fee data loading…[/dim]"),
                title="[bold #9b6dff]◈ FEE INTELLIGENCE[/]",
                style="#e8edf2 on #080809",
                border_style="#9b6dff",
            )

        tier = fee.get("tier", 0)
        max_tier = 4
        vol_14d = fee.get("weighted_14d_volume", 0.0)
        gap = fee.get("volume_to_next_tier", 0.0)
        soso = fee.get("soso_staked", 0.0)
        staking_pct = fee.get("staking_discount_pct", 0.0)

        tier_bar = ""
        for i in range(max_tier + 1):
            if i < tier:
                tier_bar += "[bold #00d084]█[/]"
            elif i == tier:
                tier_bar += "[bold #ffcc00]█[/]"
            else:
                tier_bar += "[dim]░[/dim]"

        tier_color = "#00d084" if tier >= 3 else ("#ffcc00" if tier >= 1 else "dim")
        next_str = f"[dim]${gap:,.0f} to Tier {tier + 1}[/dim]" if gap > 0 else "[bold #00d084]MAX TIER[/]"

        staking_str = (
            f"[bold]{soso:,.0f}[/bold] SOSO → [bold #00d084]{staking_pct:.0f}%[/] off"
            if soso > 0
            else "[dim]0 SOSO staked[/dim]"
        )

        perp_t = fee.get("perps_taker_pct", 0.0)
        perp_m = fee.get("perps_maker_pct", 0.0)
        spot_t = fee.get("spot_taker_pct", 0.0)
        spot_m = fee.get("spot_maker_pct", 0.0)
        arb_rt = fee.get("arb_round_trip_maker_pct", 0.0)
        arb_be = fee.get("arb_break_even_3periods_maker_pct", 0.0)

        content = (
            f"[dim]Tier[/] [{tier_color}]{tier}[/]  {tier_bar}  {next_str}\n"
            f"[dim]14D vol:[/dim] [bold]${vol_14d:,.0f}[/]  {staking_str}\n"
            f"[dim]Perp[/]  T:[bold]{perp_t:.4f}%[/] M:[bold]{perp_m:.4f}%[/]  "
            f"[dim]Spot[/] T:[bold]{spot_t:.4f}%[/] M:[bold]{spot_m:.4f}%[/]\n"
            f"[dim]Arb RT[/] [bold]{arb_rt:.4f}%[/]  "
            f"[dim]BE/3prd[/] [bold]{arb_be:.4f}%[/]"
        )
        return Panel(
            Text.from_markup(content),
            title="[bold #9b6dff]◈ FEE INTELLIGENCE[/]",
            style="#e8edf2 on #080809",
            border_style="#9b6dff",
        )

    def _build_intelligence_feed_panel(self) -> Panel:
        """
        Unified Intelligence Feed — Agent Log + Prediction Market on one time axis.

        Design insight (from React prototype):
          Agent Log   = WHAT ARIA JUST DID  (past → present, last 2-30 seconds)
          Pred Market = WHAT ARIA IS BETTING (present → future, open bets)
          They are sequential in time — one river, not two panels fighting for space.

        Structure:
          1. Regime state header   — replaces separate regime strip
          2. Active bet row        — present position, gold-bordered
          3. Timeline              — agent decisions + regime shift events, newest first,
                                     fade by age, personality-coloured

        Latency: O(n_candidates + n_regime_events). No external I/O.
        """
        _A_COL = {
            "SHIELD":    "#ff3d5a",
            "SOVEREIGN": "#9b6dff",
            "AFTERMATH": "#f5c842",
            "APEX":      "#ff6b2b",
            "FLOW":      "#00d4aa",
            "COIL":      "#4d9fff",
            "SCOUT":     "#888899",
        }
        _A_SYM = {
            "SHIELD":    "■",
            "SOVEREIGN": "◆",
            "AFTERMATH": "◈",
            "APEX":      "▲",
            "FLOW":      "≈",
            "COIL":      "⊙",
            "SCOUT":     "∘",
        }
        _REGIME_COL = {
            "risk_on":    "#00d4aa",
            "risk off":   "#00d4aa",  # normalised
            "risk_off":   "#ff3d5a",
            "rotational": "#4d9fff",
        }
        # Opacity fade: blend #e8e8f0 toward #0a0f1a at 6 stops
        _FADE = ["#e8e8f0", "#c6c8d3", "#9899a7", "#6b6d7b", "#484b58", "#343643"]

        ctx       = self._display_cache.get("context")
        pmap      = self._display_cache.get("personality_map") or {}
        session   = self._display_cache.get("session", {})
        bet       = self._display_cache.get("active_bet")

        regime_raw = getattr(ctx, "regime", "") if ctx else ""
        regime     = regime_raw.upper().replace("_", " ") if regime_raw else "—"
        conf       = getattr(ctx, "regime_confidence", 0.0) if ctx else 0.0
        flow_bias  = getattr(ctx, "flow_bias", {}) or {} if ctx else {}
        buy_syms   = [s.replace("-USD","") for s,b in flow_bias.items() if b=="buy"][:4]
        sell_syms  = [s.replace("-USD","") for s,b in flow_bias.items() if b=="sell"][:4]
        regime_col = _REGIME_COL.get(regime_raw.lower(), "#888899")

        # ── 1. Regime state header ─────────────────────────────────────────────
        flow_parts: list[str] = []
        if buy_syms:
            flow_parts.append(f"[#00d4aa]▲ {' '.join(buy_syms)}[/]")
        if sell_syms:
            flow_parts.append(f"[#ff3d5a]▼ {' '.join(sell_syms)}[/]")
        flow_str = "  ".join(flow_parts) if flow_parts else "[dim]—[/]"

        header_line = (
            f" [bold {regime_col}]{regime}[/]"
            f"  [dim]conf[/] [{regime_col}]{conf*100:.0f}%[/]"
            f"  [dim]│[/]  {flow_str}"
        )

        # ── 2. Active bet ──────────────────────────────────────────────────────
        bet_lines: list[str] = []
        if bet and not bet.get("resolved", True):
            agent_a  = bet.get("agent_a", "?")
            agent_b  = bet.get("agent_b", "?")
            col_a    = _A_COL.get(agent_a, "#888899")
            col_b    = _A_COL.get(agent_b, "#888899")
            sym_a    = _A_SYM.get(agent_a, "∘")
            sym_b    = _A_SYM.get(agent_b, "∘")
            conf_a   = bet.get("conf_a", 0.0)
            conf_b   = bet.get("conf_b", 0.0)
            budget_a = bet.get("budget_a", 0.0)
            budget_b = bet.get("budget_b", 0.0)
            p_joint  = bet.get("p_joint", 0.0)
            combined = bet.get("combined", 0.0)
            size_m   = bet.get("size_mult", 1.0)
            sym      = bet.get("symbol", "?").replace("-USD","")
            dirn     = bet.get("direction", "?").upper()
            live_pnl = bet.get("live_pnl")
            dir_col  = "#00d4aa" if dirn == "LONG" else "#ff3d5a"
            pj_col   = "#f5c842" if p_joint >= 0.7 else ("#888899" if p_joint >= 0.5 else "#ff3d5a")

            pnl_str = ""
            if live_pnl is not None:
                pnl_c = "#00d4aa" if live_pnl >= 0 else "#ff3d5a"
                pnl_str = f"  [dim]PnL[/] [{pnl_c}]{live_pnl:+.2f}[/]"

            BAR_W = 10
            def _bar(pct: float, col: str) -> str:
                filled = max(0, int(min(1.0, pct) * BAR_W))
                return f"[{col}]{'█'*filled}{'░'*(BAR_W-filled)}[/]"

            bet_lines = [
                f" [bold #f5c842]◆ BET ACTIVE[/]"
                f"  [{col_a}]{sym_a}{agent_a}[/] + [{col_b}]{sym_b}{agent_b}[/]"
                f"  [dim]→[/]  [bold]{sym}[/] [{dir_col}]{dirn}[/]"
                f"  [bold {pj_col}]P={p_joint*100:.0f}%[/]"
                f"  [dim]size[/] [#f5c842]{size_m:.1f}×[/]"
                f"  [dim]pool[/] ${combined:.2f}"
                f"{pnl_str}",

                f"   [{col_a}]{_bar(conf_a, col_a)}[/]"
                f" [{col_a}]{conf_a*100:.0f}%[/] ${budget_a:.2f}"
                f"    [{col_b}]{_bar(conf_b, col_b)}[/]"
                f" [{col_b}]{conf_b*100:.0f}%[/] ${budget_b:.2f}",
            ]

        # ── 3. Unified timeline — merge decisions + regime events ──────────────
        events: list[dict] = []
        for entry in self._trade_candidate_log:
            if "ts_epoch" not in entry:
                entry["ts_epoch"] = 0.0   # legacy entries without epoch
            events.append(entry)
        for ev in self._regime_events:
            events.append(ev)
        events.sort(key=lambda e: e.get("ts_epoch", 0.0), reverse=True)

        feed_lines: list[str] = []
        if not events:
            feed_lines.append("[#484b58] Agents scanning — no decisions this session[/]")
        else:
            for idx, ev in enumerate(events[:14]):
                fade = _FADE[min(idx, len(_FADE) - 1)]

                if ev.get("type") == "regime_shift":
                    from_r  = ev.get("from", "?")
                    to_r    = ev.get("to", "?")
                    conf_r  = ev.get("conf", 0.0)
                    to_col  = _REGIME_COL.get(to_r.lower().replace(" ","_"), "#888899")
                    ts_str  = ev.get("ts", "")
                    feed_lines.append(
                        f" [{fade}]{ts_str}[/]"
                        f"  [dim]── REGIME SHIFT ──[/]"
                        f" [{fade}]{from_r}[/] [dim]→[/]"
                        f" [bold {to_col}]{to_r}[/]"
                        f"  [dim]conf {conf_r*100:.0f}%[/]"
                    )
                    continue

                if ev.get("type") == "cascade_shift":
                    _cf = ev.get("from", "?")
                    _ct = ev.get("to", "?")
                    _cd = ev.get("direction", "")
                    _phase_colors = {
                        "BLOCKED":  "#ff4757",
                        "PRIMED":   "#00d084",
                        "MOMENTUM": "#ffcc00",
                        "IDLE":     "#888899",
                    }
                    _to_col  = _phase_colors.get(_ct, "#888899")
                    _dir_str = (
                        f"  [{'#00d084' if _cd == 'LONG' else '#ff4757'}]{_cd}[/]" if _cd else ""
                    )
                    feed_lines.append(
                        f" [{fade}]{ev.get('ts','')}[/]"
                        f"  [dim]── CASCADE ──[/]"
                        f" [dim]{_cf}[/] [dim]→[/]"
                        f" [bold {_to_col}]{_ct}[/]"
                        f"{_dir_str}"
                    )
                    continue

                if ev.get("type") == "bet_placed":
                    _bsym = ev.get("sym", "?").replace("-USD", "")
                    _ba   = ev.get("agent_a", "?")
                    _bb   = ev.get("agent_b", "?")
                    _pj   = ev.get("p_joint", 0.0)
                    _sm   = ev.get("size_mult", 1.0)
                    feed_lines.append(
                        f" [{fade}]{ev.get('ts','')}[/]"
                        f"  [dim]── BET ──[/]"
                        f" [bold #ffcc00]{_bsym}[/]"
                        f"  [dim]{_ba}+{_bb}[/]"
                        f"  [bold #00d084]P={_pj:.0%}[/]"
                        f"  [dim]{_sm:.1f}×[/]"
                    )
                    continue

                # Agent decision row
                ts_str  = ev.get("ts", "")
                sym_s   = ev.get("sym", "?").replace("-USD","")
                dir_s   = ev.get("dir", "?").upper()
                score   = ev.get("score", 0.0)
                status  = ev.get("status", "?").upper()
                reason  = ev.get("reason") or ev.get("error", "")
                # Personality: stored in entry, else fall back to pmap
                agent   = ev.get("personality") or pmap.get(ev.get("sym",""), "SCOUT")

                a_col   = _A_COL.get(agent, "#888899") if idx == 0 else fade
                a_sym   = _A_SYM.get(agent, "∘")
                dir_col = ("#00d4aa" if dir_s == "LONG" else "#ff3d5a") if idx == 0 else fade
                sym_col = "#e8e8f0" if idx == 0 else fade
                ts_col  = "#555566" if idx == 0 else _FADE[min(idx+1, len(_FADE)-1)]

                if status in ("PLACED", "FILLED"):
                    st_col = "#00d4aa"
                elif status == "SUBMITTED":
                    st_col = "#f5c842" if idx == 0 else fade
                elif status in ("REJECTED", "BLOCKED"):
                    st_col = "#ff4444" if idx == 0 else "#5a1515"
                else:
                    st_col = fade

                feed_lines.append(
                    f" [{ts_col}]{ts_str}[/]"
                    f"  [{a_col}]{a_sym}{agent[:4]:<4}[/]"
                    f"  [{sym_col}]{sym_s:<6}[/]"
                    f"  [{dir_col}]{dir_s:<5}[/]"
                    f"  [{fade}]s={score:.1f}[/]"
                    f"  [{st_col}]{status}[/]"
                )
                # Sub-row: reason for rejections / brief context — top 4 entries only
                if reason and idx < 4:
                    reason_col = "#5a1515" if status in ("REJECTED","BLOCKED") else "#444455"
                    reason_short = str(reason)[:56]
                    feed_lines.append(
                        f"           [{reason_col}]{reason_short}[/]"
                    )

        # ── Assemble ───────────────────────────────────────────────────────────
        sep = f" [dim]{'─' * 54}[/]"
        parts: list[str] = [header_line, sep]
        if bet_lines:
            parts.extend(bet_lines)
            parts.append(sep)
        parts.extend(feed_lines)

        # Title: dominant agent + session stats + bet badge
        _counts: dict = {}
        for _sym, _p in pmap.items():
            _counts[_p] = _counts.get(_p, 0) + 1
        _priority = ["SHIELD","SOVEREIGN","AFTERMATH","APEX","FLOW","COIL","SCOUT"]
        dominant  = next((p for p in _priority if _counts.get(p,0) > 0), "SCOUT")
        dom_col   = _A_COL.get(dominant, "#888899")

        wr      = session.get("win_rate", 0.0)
        t_cnt   = session.get("closed", 0)
        pnl     = session.get("total_pnl", 0.0)
        pnl_col = "#00d4aa" if pnl >= 0 else "#ff3d5a"
        bet_badge = ""
        if bet and not bet.get("resolved", True):
            bet_badge = "  [bold #f5c842]◆ BET LIVE[/]"

        title = (
            f"[bold {dom_col}]● INTELLIGENCE FEED[/]"
            f"  [dim]WR[/] [#f5c842]{wr:.0f}%[/] [dim]T:{t_cnt}[/]"
            f"  [{pnl_col}]{pnl:+.2f}[/]"
            f"{bet_badge}"
        )

        return Panel(
            Text.from_markup("\n".join(parts)),
            title=title,
            style="#e8e8f0 on #0a0f1a",
            border_style="#4d9fff",
            padding=(0, 0),
        )

    def _build_agents_panel(self) -> Panel:
        """
        ARIA Agent Performance Board — 7 specialist agents running your SoDEX account.

        Design principles:
          - Mature, institutional symbols. No playful emojis.
          - User-centric: which agent is dominant, how many assets each runs.
          - SOVEREIGN gets its own section: territory + yield budget.
          - Session P&L and win rate surfaced at the top.

        Latency: O(n_assets + n_candidates). No external I/O.
        """
        _A: dict = {
            # name: (hex_color, glyph, tagline)
            "SHIELD":    ("#ff3d5a", "■", "capital protection"),
            "SOVEREIGN": ("#9b6dff", "◆", "yield-funded MAG7 alpha"),
            "AFTERMATH": ("#f5c842", "◈", "reads the battlefield"),
            "APEX":      ("#ff6b2b", "▲", "cavalry at peak momentum"),
            "FLOW":      ("#00d4aa", "≈", "river logic — trend"),
            "COIL":      ("#4d9fff", "⊙", "siege patience — arb"),
            "SCOUT":     ("#888899", "∘", "advance guard — reduced"),
        }

        pmap      = self._display_cache.get("personality_map") or {}
        sovereign = self._display_cache.get("sovereign") or {}
        session   = self._display_cache.get("session") or {}

        # Tally assignments
        counts:  dict = {p: 0 for p in _A}
        buckets: dict = {p: [] for p in _A}
        for sym, p in pmap.items():
            if p in counts:
                counts[p] += 1
                buckets[p].append(sym.replace("-USD", ""))

        # Dominant agent (highest-priority non-idle)
        _priority = ["SHIELD", "SOVEREIGN", "AFTERMATH", "APEX", "FLOW", "COIL", "SCOUT"]
        dominant = next((p for p in _priority if counts.get(p, 0) > 0), "SCOUT")
        dom_col, dom_glyph, _ = _A[dominant]

        wr_pct   = session.get("win_rate", 0.0)
        t_closed = session.get("closed", 0)
        pnl      = session.get("total_pnl", 0.0)
        live     = session.get("deployed", 0.0)
        pnl_col  = "#00d4aa" if pnl >= 0 else "#ff3d5a"
        wr_col   = "#00d4aa" if wr_pct >= 55 else ("#f5c842" if wr_pct >= 45 else "#ff3d5a")

        lines: list[str] = [
            f" WR [{wr_col}]{wr_pct:.0f}%[/] · T:{t_closed} · "
            f"P&L [{pnl_col}]{pnl:+.2f}[/] · Margin ${live:.0f}\n"
        ]

        # Per-agent win rate records (persistent across restarts)
        _wr_all: dict = {}
        if self._agent_wr is not None:
            try:
                _wr_all = {a: rec for a, rec in self._agent_wr.all().items()}
            except Exception:
                pass

        # ── Agent roster ──────────────────────────────────────────────────────
        for p_name in _priority:
            col, glyph, tagline = _A[p_name]
            cnt  = counts.get(p_name, 0)
            syms = buckets.get(p_name, [])
            _rec = _wr_all.get(p_name)
            _wr_pct  = _rec.win_rate if _rec else 0.0
            _wr_t    = _rec.trades  if _rec else 0
            _streak  = _rec.streak  if _rec else 0
            _wr_col  = "#00d4aa" if _wr_pct >= 55 else ("#f5c842" if _wr_pct >= 45 else ("#888899" if _wr_t == 0 else "#4f8ef7"))
            _str_col = "#00d4aa" if _streak > 0 else ("#ff3d5a" if _streak < 0 else "#888899")
            _str_str = f"[{_str_col}]{_streak:+d}[/]" if _wr_t > 0 else "[dim]—[/]"
            _wr_str  = f"[{_wr_col}]{_wr_pct:.0f}%[/][dim]/{_wr_t}[/]" if _wr_t > 0 else "[dim]─%[/]"

            if p_name == "SOVEREIGN":
                sov_budget = sovereign.get("budget_usd", 0.0)
                sov_active = sovereign.get("is_active", False)
                sov_col    = "#00d4aa" if sov_active else "#888899"
                sov_label  = "ACTIVE" if sov_active else "COIL  "
                lines.append(
                    f" [{col}]{glyph} {p_name:<10}[/]"
                    f" [{sov_col}]{sov_label}[/]"
                    f"  [dim]${sov_budget:.2f}[/]"
                    f"  WR {_wr_str} {_str_str}"
                )
                continue

            if cnt > 0:
                bar_str = "█" * min(cnt, 8) + "░" * (8 - min(cnt, 8))
                sym_str = " ".join(syms[:4]) + ("+" if len(syms) > 4 else "")
                lines.append(
                    f" [{col}]{glyph} {p_name:<10}[/]"
                    f" [{col}]{bar_str}[/]"
                    f" [bold]{cnt:2}[/]"
                    f"  WR {_wr_str} {_str_str}"
                    f"  [dim]{sym_str}[/]"
                )
            else:
                lines.append(
                    f" [dim]{glyph} {p_name:<10} {'░'*8}  0[/]"
                    f"  WR {_wr_str} {_str_str}"
                )

        # ── SOVEREIGN territory strip ─────────────────────────────────────────
        sov_stake = sovereign.get("stake_usd", 0.0)
        sov_yield = sovereign.get("yield_accrued", 0.0)
        best_sym  = sovereign.get("best_sym", "")
        best_z    = sovereign.get("best_z", 0.0)
        best_dir  = sovereign.get("best_dir", "")

        lines.append("\n [dim]─── SOVEREIGN TERRITORY ───[/]")
        lines.append(
            f" [#9b6dff]◆[/] Staked [bold]${sov_stake:.0f}[/] sMAG7  "
            f"Yield [#9b6dff]${sov_yield:.4f}[/]"
        )
        if best_sym:
            z_col   = "#ff3d5a" if best_z < 0 else "#00d4aa"
            dir_col = "#ff3d5a" if best_dir == "short" else "#00d4aa"
            lines.append(
                f" Signal [{z_col}]{best_sym.replace('-USD', '')} z={best_z:+.1f}[/]"
                f"  [{dir_col}]{best_dir.upper()}[/]"
            )
        else:
            lines.append(" [dim]Divergence signal: warming up[/]")

        return Panel(
            Text.from_markup("\n".join(lines)),
            title=f"[bold {dom_col}]{dom_glyph} AGENTS — {dominant} DOMINANT[/]",
            style="#e8edf2 on #080809",
            border_style=dom_col,
        )

    def _build_sovereign_panel(self) -> Panel:
        """
        SOVEREIGN kingdom intelligence panel.

        Shows the MAG7 territory (staked position), yield income, campaign budget,
        and per-component z-scores (spread divergence from rolling index return).

        SOVEREIGN trades from yield — the territory is never consumed.
        Latency: O(7 components). No external I/O.
        """
        sovereign = self._display_cache.get("sovereign") or {}
        z_scores    = sovereign.get("z_scores") or {}
        stake_usd   = sovereign.get("stake_usd", 0.0)
        budget_usd  = sovereign.get("budget_usd", 0.0)
        reserve_usd = sovereign.get("reserve_usd", 0.0)
        is_active   = sovereign.get("is_active", False)
        yield_acc   = sovereign.get("yield_accrued", 0.0)
        best_sym    = sovereign.get("best_sym", "")
        best_z      = sovereign.get("best_z", 0.0)
        best_dir    = sovereign.get("best_dir", "")

        _W = {"NVDA":0.25,"MSFT":0.18,"AAPL":0.15,"AMZN":0.14,"GOOGL":0.12,"META":0.10,"TSLA":0.06}

        sov_col   = "#00d4aa" if is_active else "#888899"
        sov_label = "ACTIVE" if is_active else "COIL"

        lines: list[str] = [
            f" [dim]Territory[/] [bold #9b6dff]${stake_usd:.0f}[/] sMAG7"
            f"   [{sov_col}]{sov_label}[/]",
            f" [dim]Yield[/] [bold #9b6dff]${yield_acc:.4f}[/]   "
            f"[dim]Budget[/] [{sov_col}]${budget_usd:.4f}[/]   [dim]Reserve ${reserve_usd:.4f}[/]",
        ]

        if best_sym:
            z_col   = "#ff3d5a" if best_z < 0 else "#00d4aa"
            dir_col = "#ff3d5a" if best_dir == "short" else "#00d4aa"
            lines.append(
                f" [dim]Signal[/] [{z_col}]{best_sym.replace('-USD', '')} z={best_z:+.2f}[/]"
                f"  [{dir_col}]{best_dir.upper()} candidate[/]"
            )

        lines.append("\n [dim]─── FIELD INTELLIGENCE (MAG7 components) ───[/]")

        if not z_scores:
            lines.append(" [dim]Warming up — price feed required (15min cadence)[/]")
            lines.append(" [dim]Component z-scores will appear after first update.[/]")
            for sym_s, wt in _W.items():
                hedge = stake_usd * wt
                lines.append(f" [dim]{sym_s:<6} {wt*100:.0f}%  ${hedge:.0f}  ─────────────[/]")
        else:
            sorted_syms = sorted(z_scores.keys(), key=lambda s: abs(z_scores.get(s, 0)), reverse=True)
            for sym in sorted_syms:
                z     = z_scores.get(sym, 0.0)
                sym_s = sym.replace("-USD", "")
                wt    = _W.get(sym_s, 0.0)
                hedge = stake_usd * wt

                if abs(z) >= 2.0:
                    z_col, label, l_col = ("#ff3d5a" if z < 0 else "#00d4aa"), ("SHORT" if z < 0 else "LONG "), ("#ff3d5a" if z < 0 else "#00d4aa")
                    sym_style = f"bold {z_col}"
                elif abs(z) >= 1.5:
                    z_col = l_col = "#f5c842"; label = "watch"; sym_style = z_col
                else:
                    z_col = "#555566"; l_col = "#555566"; label = "hold "; sym_style = "dim"

                bar = ["─"] * 9
                bar[min(8, max(0, int(4 + z * 1.5)))] = "◆" if abs(z) >= 1.5 else "·"
                bar_str = "".join(bar)

                lines.append(
                    f" [{sym_style}]{sym_s:<6}[/] [dim]{bar_str}[/]"
                    f" [{z_col}]z={z:+.2f}[/]  [{l_col}]{label}[/]  [dim]${hedge:.0f}[/]"
                )

        # ── Sovereign Portfolio Agent section ────────────────────────────────
        sp = self._display_cache.get("sovereign_portfolio") or {}
        if sp:
            phase         = sp.get("phase", "?")
            phase_age     = sp.get("phase_age_h", 0.0)
            confidence    = sp.get("confidence", 0.0)
            hedge_active  = sp.get("hedge_active", False)
            portfolio     = sp.get("portfolio", {})
            total_usd     = portfolio.get("total_usd", 0.0)
            var_pct       = portfolio.get("var_pct", 0.0)
            yield_30d     = sp.get("yield_30d_usd", 0.0)
            ussi_apy      = sp.get("current_ussi_apy", 0.0)
            residual_pct  = sp.get("residual_basis_pct", 32.4)
            carry_score   = sp.get("avg_carry_score", 0.0)
            hedge_instrs  = sp.get("hedge_instructions", [])
            execute_on    = sp.get("execute_enabled", False)

            phase_col = {"BULL": "#00d4aa", "CAUTION": "#f5c842",
                         "TRANSITION": "#ff8c42", "BEAR": "#ff3d5a",
                         "RECOVERY": "#9b6dff"}.get(phase, "#888899")

            lines.append("\n [dim]─── SOVEREIGN PORTFOLIO ───[/]")
            exec_tag = "[bold #00d4aa]LIVE[/]" if execute_on else "[dim]ADVISORY[/]"
            lines.append(
                f" [{phase_col}]{phase}[/] {exec_tag}"
                f"  [dim]age[/] {phase_age:.1f}h  [dim]conf[/] {confidence:.0%}"
                f"  [dim]VaR[/] {var_pct:.1f}%"
            )
            if hedge_active and hedge_instrs:
                hedge_str = " ".join(
                    f"{h['symbol'].replace('-USD','')} ${h['notional_usd']:.0f}s"
                    for h in hedge_instrs[:4]
                )
                lines.append(f" [dim]Hedge:[/] [{phase_col}]{hedge_str}[/]")
                lines.append(f" [dim]Basis risk:[/] [#f5c842]{residual_pct:.1f}%[/] unhedgeable")
            elif not hedge_active:
                lines.append(f" [dim]Basis risk:[/] [#f5c842]{residual_pct:.1f}%[/]  [dim]no hedge active[/]")

            # Position table
            positions = portfolio.get("positions", {})
            for _sym, _pos in positions.items():
                if not isinstance(_pos, dict):
                    continue
                _disp   = _pos.get("display", _sym)
                _qty    = _pos.get("quantity", 0.0)
                _usd    = _pos.get("current_usd", 0.0)
                _tw     = _pos.get("target_weight", 0.0)
                _aw     = _pos.get("actual_weight", 0.0)
                _drift  = _pos.get("drift_1h", 0.0)
                _drift_s = f"[#00d4aa]+{_drift*100:.1f}%[/]" if _drift > 0 else (
                           f"[#ff3d5a]{_drift*100:.1f}%[/]" if _drift < 0 else "[dim] 0.0%[/]")
                _wdiff  = _tw - _aw
                _w_col  = "#00d4aa" if abs(_wdiff) < 0.02 else "#f5c842"
                lines.append(
                    f" [dim]{_disp:<9}[/] {_qty:>8.2f}  "
                    f"[bold]${_usd:>7.2f}[/]  "
                    f"[{_w_col}]{_aw:.0%}→{_tw:.0%}[/]  {_drift_s}"
                )

            lines.append(
                f" [dim]USSI APY[/] [#9b6dff]{ussi_apy:.1f}%[/]"
                f"  [dim]carry[/] {carry_score:+.2f}"
                f"  [dim]30d yield[/] [#00d4aa]${yield_30d:+.2f}[/]"
            )

        # ── Fee Intelligence section (merged) ────────────────────────────────
        fee = self._display_cache.get("fee") or {}
        if fee:
            tier     = fee.get("tier", 0)
            vol_14d  = fee.get("weighted_14d_volume", 0.0)
            gap      = fee.get("volume_to_next_tier", 0.0)
            soso     = fee.get("soso_staked", 0.0)
            s_pct    = fee.get("staking_discount_pct", 0.0)
            perp_t   = fee.get("perps_taker_pct", 0.0)
            spot_t   = fee.get("spot_taker_pct", 0.0)
            arb_be   = fee.get("arb_break_even_3periods_maker_pct", 0.0)
            max_tier = 4
            tier_bar = "".join(
                "[bold #00d084]█[/]" if i < tier else
                "[bold #ffcc00]█[/]" if i == tier else "[dim]░[/dim]"
                for i in range(max_tier + 1)
            )
            tier_color  = "#00d084" if tier >= 3 else ("#ffcc00" if tier >= 1 else "dim")
            next_str    = f"[dim]${gap:,.0f}→T{tier+1}[/]" if gap > 0 else "[bold #00d084]MAX[/]"
            soso_str    = (f"[bold]{soso:,.0f}[/] SOSO [{s_pct:.0f}%off]" if soso > 0 else "[dim]0 SOSO[/]")
            lines.append("\n [dim]─── FEE INTELLIGENCE ───[/]")
            lines.append(
                f" [dim]Tier[/] [{tier_color}]{tier}[/] {tier_bar} {next_str}  {soso_str}"
            )
            lines.append(
                f" [dim]14D vol[/] [bold]${vol_14d:,.0f}[/]  "
                f"[dim]Perp T[/] [bold]{perp_t:.4f}%[/]  "
                f"[dim]Spot T[/] [bold]{spot_t:.4f}%[/]  "
                f"[dim]ArbBE[/] [bold]{arb_be:.4f}%[/]"
            )
        else:
            lines.append("\n [dim]─── FEE INTELLIGENCE ─── loading…[/]")

        border       = "#9b6dff" if is_active else "#3a3a4a"
        status_title = "[bold #00d4aa]ACTIVE[/]" if is_active else "[dim]COIL — awaiting yield[/]"
        return Panel(
            Text.from_markup("\n".join(lines)),
            title=f"[bold #9b6dff]◆ SOVEREIGN — {status_title}[/]",
            style="#e8edf2 on #080809",
            border_style=border,
        )

    def _build_equity_curve_panel(self) -> Panel:
        """
        Equity sparkline — multi-row ASCII curve using block characters.
        Latency: O(n_equity_points). No external I/O.
        """
        equity  = self._display_cache.get("equity", [])
        session = self._display_cache.get("session", {})
        pnl     = session.get("total_pnl", 0.0)
        dd_pct  = session.get("dd_pct", 0.0)
        dd_reg  = session.get("dd_regime", "normal")

        if len(equity) < 2:
            return Panel(
                Text.from_markup("[dim]Collecting balance data points…[/]"),
                title="[bold #00d4aa]∿ EQUITY CURVE[/]",
                style="#e8edf2 on #080809", border_style="#00d4aa", padding=(0, 1),
            )

        balances = [b for _, b in equity[-80:]]
        mn  = min(balances)
        mx  = max(balances)
        cur = balances[-1]
        s0  = balances[0]
        pct_chg = (cur - s0) / s0 * 100 if s0 else 0.0
        spread  = mx - mn if mx != mn else 1.0

        ROWS  = 4
        width = min(len(balances), 60)
        pts   = balances[-width:]

        grid = [[" "] * width for _ in range(ROWS)]
        for i, val in enumerate(pts):
            y = min(ROWS - 1, int((val - mn) / spread * (ROWS - 1)))
            grid[ROWS - 1 - y][i] = "█" if i == width - 1 else "─"

        is_up  = cur >= s0
        c_line = "#00d4aa" if is_up else "#ff3d5a"
        c_dim  = "#004d3d" if is_up else "#5a0f1a"

        # Build markup string — gradient: top rows dark, bottom rows bright
        chart_lines: list[str] = []
        for row_i, row in enumerate(grid):
            col = c_line if row_i >= ROWS - 2 else c_dim
            chart_lines.append(f"[{col}]{''.join(row)}[/]")

        pct_col = "#00d4aa" if pct_chg >= 0 else "#ff3d5a"
        dd_col  = {"normal": "#00d4aa", "caution": "#f5c842", "defensive": "#ff3d5a", "halt": "bold #ff0000"}.get(dd_reg, "dim")

        footer = (
            f"[dim]T:{len(equity)}[/]  "
            f"Peak [#00d4aa]${mx:.2f}[/]  Trough [#ff3d5a]${mn:.2f}[/]  "
            f"[{pct_col}]{pct_chg:+.2f}%[/]  "
            f"DD [{dd_col}]{dd_pct:.1f}% {dd_reg.upper()}[/]"
        )
        content = "\n".join(chart_lines) + "\n" + footer

        pnl_col   = "#00d4aa" if pnl >= 0 else "#ff3d5a"
        title_sfx = f"[{pnl_col}]{pnl:+.2f} session[/]"
        return Panel(
            Text.from_markup(content),
            title=f"[bold #00d4aa]∿ EQUITY CURVE[/]  {title_sfx}",
            style="#e8edf2 on #080809",
            border_style="#00d4aa",
            padding=(0, 1),
        )

    def _build_regime_summary_panel(self) -> Panel:
        """
        Regime summary + personality budget allocation bars.
        One-stop view of market regime, phase, flow bias, and which agents
        own what percentage of the asset universe.
        Latency: O(n_assets). No external I/O.
        """
        ctx    = self._display_cache.get("context")
        pmap   = self._display_cache.get("personality_map") or {}
        session = self._display_cache.get("session", {})

        regime   = getattr(ctx, "regime", "—").upper().replace("_", " ") if ctx else "—"
        conf     = getattr(ctx, "regime_confidence", 0.0) if ctx else 0.0
        mode     = getattr(ctx, "market_mode", "normal") if ctx else "normal"
        tr_phase = getattr(ctx, "time_regime_phase", "") if ctx else ""
        buy_syms = [s.replace("-USD","") for s,b in (getattr(ctx,"flow_bias",{}) or {}).items() if b=="buy"][:3]
        sell_syms= [s.replace("-USD","") for s,b in (getattr(ctx,"flow_bias",{}) or {}).items() if b=="sell"][:3]

        _MODE_COL = {
            "cascade_blocked":  "#ff3d5a",
            "cascade_momentum": "#ff8c00",
            "cascade_primed":   "#00d4aa",
            "calendar_caution": "#f5c842",
            "defensive":        "#ff6b6b",
            "normal":           "#4d9fff",
        }
        mode_col = _MODE_COL.get(mode, "#4d9fff")

        lines: list[str] = [
            f" [dim]Regime[/]    [bold {mode_col}]{regime}[/]",
            f" [dim]Confident[/] [{mode_col}]{conf*100:.0f}%[/]",
        ]
        if tr_phase:
            _ph     = tr_phase.replace("_", " ")
            _ph_col = "#f5c842" if "event" in tr_phase or "block" in tr_phase else "#888899"
            lines.append(f" [dim]Phase[/]     [{_ph_col}]{_ph}[/]")
        if buy_syms:
            lines.append(f" [dim]Flow ▲[/]   [#00d4aa]{' '.join(buy_syms)}[/]")
        if sell_syms:
            lines.append(f" [dim]Flow ▼[/]   [#ff3d5a]{' '.join(sell_syms)}[/]")

        lines.append("\n [dim]──────── Personality Budget ────────[/]")

        _AGENTS = [
            ("SOVEREIGN", "#9b6dff", "◆"),
            ("AFTERMATH", "#f5c842", "◈"),
            ("APEX",      "#ff6b2b", "▲"),
            ("FLOW",      "#00d4aa", "≈"),
            ("COIL",      "#4d9fff", "⊙"),
            ("SCOUT",     "#888899", "∘"),
        ]

        total  = len(pmap) or 1
        counts: dict = {}
        for _sym, _p in pmap.items():
            counts[_p] = counts.get(_p, 0) + 1

        BAR_W = 16
        for p_name, col, glyph in _AGENTS:
            cnt    = counts.get(p_name, 0)
            pct    = cnt / total
            filled = int(pct * BAR_W)
            bar    = "█" * filled + "░" * (BAR_W - filled)
            lines.append(
                f" [{col}]{glyph} {p_name:<10}[/]"
                f" [{col}]{bar}[/]"
                f" [dim]{pct*100:.0f}%[/]"
            )

        wr    = session.get("win_rate", 0.0)
        t_cnt = session.get("closed", 0)
        bal   = session.get("balance", 0.0)
        dep   = session.get("deployed", 0.0)
        dep_pct = dep / bal * 100 if bal > 0 else 0
        dep_col = "#00d4aa" if dep_pct < 25 else ("#f5c842" if dep_pct < 60 else "#ff3d5a")
        lines.append(
            f"\n [dim]WR[/] [#f5c842]{wr:.1f}%[/]  [dim]T:[/]{t_cnt}  "
            f"[dim]Dep[/] [{dep_col}]{dep_pct:.0f}%[/]  [dim]Bal[/] ${bal:.2f}"
        )

        return Panel(
            Text.from_markup("\n".join(lines)),
            title=f"[bold {mode_col}]► REGIME SUMMARY[/]",
            style="#e8edf2 on #080809",
            border_style=mode_col,
            padding=(0, 1),
        )

    def _build_compact_positions_panel(self) -> Panel:
        """
        Compact open positions — designed for the small left-column slot.
        Replaces Trade Candidates. One row per position: Sym | Dir | uPnL | Age.
        Latency: O(n_positions). No external I/O.
        """
        import time as _time
        positions    = self._display_cache.get("positions", [])
        assets_cache = self._display_cache.get("assets", {})
        now_ms       = int(_time.time() * 1000)

        table = Table(
            expand=True, style="#e8edf2 on #080809",
            border_style="#2a2a3a", show_lines=False,
            padding=(0, 0),
        )
        table.add_column("Sym", min_width=6,  no_wrap=True)
        table.add_column("Dir", min_width=5,  no_wrap=True)
        table.add_column("uPnL", min_width=8, justify="right", no_wrap=True)
        table.add_column("Age",  min_width=6, justify="right", no_wrap=True)

        if not positions:
            table.add_row("[dim]—[/]", "[dim]flat[/]", "[dim]—[/]", "[dim]—[/]")
        else:
            for pos in positions:
                sym   = getattr(pos, "symbol", "?")
                side  = getattr(pos, "side", "long")
                entry = getattr(pos, "entry_price", 0.0)
                size  = getattr(pos, "size", 0.0)
                mark  = assets_cache.get(sym, {}).get("mark_price", entry) or entry
                upnl  = (mark - entry) * size if side == "long" else (entry - mark) * size
                opened_at = getattr(pos, "opened_at_ms", now_ms)
                age_s = max(0, (now_ms - opened_at)) // 1000
                age_str = (f"{age_s}s" if age_s < 60 else
                           f"{age_s//60}m{age_s%60:02d}s" if age_s < 3600 else
                           f"{age_s//3600}h{(age_s%3600)//60:02d}m")

                sym_s   = sym.replace("-USD", "")
                glow    = "◉" if upnl >= 0 else "◎"
                pnl_col = "#00d084" if upnl >= 0 else "#ff4757"
                dir_col = "#00d084" if side == "long" else "#ff4757"
                dir_lbl = "L" if side == "long" else "S"

                table.add_row(
                    f"[bold {pnl_col}]{glow} {sym_s}[/]",
                    f"[{dir_col}]{dir_lbl}[/]",
                    f"[bold {pnl_col}]{upnl:+.2f}[/]",
                    f"[dim]{age_str}[/]",
                )

        _border  = "#00aaff" if positions else "#2a2a3a"
        _n_live  = f" {len(positions)} LIVE" if positions else ""
        return Panel(
            table,
            title=f"[bold #4d9fff]◉ POSITIONS{_n_live}[/]",
            style="#e8edf2 on #080809",
            border_style=_border,
            padding=(0, 0),
        )

    def _build_signal_agents_panel(self) -> Panel:
        """
        Phase 11 — Signal Agents status panel.
        Shows the 6 core signal agents (macro/regime/structure/micro/funding/ssi)
        with current state (FIRED/QUIET), direction, confidence, and running accuracy.

        When calibration_alerts exist, appends them as a footer.
        When agent_total_trades < 10, accuracy column shows '—' (not enough data).

        Latency: O(6). No external I/O.
        """
        agent_states   = self._display_cache.get("agent_states", {})
        agent_accuracy = self._display_cache.get("agent_accuracy", {})
        total_trades   = self._display_cache.get("agent_total_trades", 0)
        cal_alerts     = self._display_cache.get("calibration_alerts", [])

        _AGENTS = [
            ("macro",     "#00d4aa", "M"),
            ("regime",    "#4d9fff", "R"),
            ("structure", "#f5c842", "T"),
            ("micro",     "#ff6b2b", "μ"),
            ("funding",   "#9b6dff", "F"),
            ("ssi",       "#00e5ff", "S"),
        ]
        _FREQ = {
            "macro":     "15m",
            "regime":    "15m",
            "structure": "1m",
            "micro":     "50ms",
            "funding":   "1h",
            "ssi":       "15m",
        }

        table = Table(
            expand=True, style="#e8edf2 on #080809",
            border_style="#1a1a2a", show_lines=False,
            padding=(0, 0),
        )
        table.add_column("Agent",   min_width=10, no_wrap=True)
        table.add_column("State",   min_width=6,  no_wrap=True)
        table.add_column("P(sig)",  min_width=6,  justify="right", no_wrap=True)
        table.add_column("Signal",  min_width=10, no_wrap=True)
        table.add_column("Acc",     min_width=6,  justify="right", no_wrap=True)

        has_any = False
        for agent_name, col, sym in _AGENTS:
            output   = agent_states.get(agent_name)
            accuracy = agent_accuracy.get(agent_name)
            freq     = _FREQ.get(agent_name, "?")

            if output is None:
                table.add_row(
                    f"[dim]{sym} {agent_name:<9}[/]",
                    f"[dim]─{freq}─[/]",
                    "[dim]—[/]",
                    "[dim]—[/]",
                    "[dim]—[/]",
                )
                continue

            has_any = True
            fired      = getattr(output, "fired", False)
            confidence = getattr(output, "confidence", 0.0)
            raw_data   = getattr(output, "raw_data", {}) or {}
            inv_reason = getattr(output, "invocation_reason", "") or ""

            # State badge
            state_str = f"[bold {col}]FIRED[/]" if fired else "[dim]quiet[/]"

            # P(signal) confidence — prediction probability
            if fired:
                conf_val = f"{confidence*100:.0f}%"
                conf_col = "#00d084" if confidence >= 0.75 else ("#f5c842" if confidence >= 0.60 else "#ff4757")
                conf_str = f"[bold {conf_col}]{conf_val}[/]"
            else:
                conf_col = "#888899"
                conf_str = "[dim]—[/]"

            # Signal descriptor: show what the agent detected (not direction)
            # Pull from raw_data fields or invocation_reason
            _sig_label = (
                raw_data.get("regime") or raw_data.get("signal_type") or
                raw_data.get("trigger") or raw_data.get("reason") or
                (inv_reason[:10] if inv_reason and inv_reason != "ssi_poll" else "")
            )
            if fired and _sig_label:
                _sig_col = col
                sig_str = f"[dim {_sig_col}]{str(_sig_label)[:10]}[/]"
            else:
                sig_str = "[dim]—[/]"

            # Accuracy
            if accuracy is None or total_trades < 10:
                acc_str = "[dim]—[/]"
            else:
                total_contrib = getattr(accuracy, "total_contributing_trades", 0)
                if total_contrib < 5:
                    acc_str = f"[dim]{total_contrib}T[/]"
                else:
                    acc_pct = getattr(accuracy, "accuracy_pct", 0.0)
                    acc_col = "#00d084" if acc_pct >= 60 else ("#f5c842" if acc_pct >= 45 else "#ff4757")
                    acc_str = f"[{acc_col}]{acc_pct:.0f}%[/]"

            table.add_row(
                f"[{col}]{sym} {agent_name:<9}[/]",
                state_str,
                conf_str,
                sig_str,
                acc_str,
            )

        # ── Calibration alerts footer ──────────────────────────────────────────
        if cal_alerts:
            lines_extra = ["\n [dim]─── CALIBRATION ALERTS ───[/]"]
            for alert in cal_alerts[:4]:
                # Truncate to fit panel width
                short = alert[:70] + "…" if len(alert) > 70 else alert
                lines_extra.append(f" [bold #ff4757]⚠[/] [dim]{short}[/]")
            alerts_text = "\n".join(lines_extra)
        else:
            alerts_text = ""

        # Build final content
        from rich.console import Group as RichGroup
        total_str = f" [dim]{total_trades}T recorded[/]" if total_trades > 0 else ""
        warm_note = "" if has_any else " [dim]warming up…[/]"

        # Include outcome_feed summary if recent closes exist
        outcome_feed = self._display_cache.get("outcome_feed", [])
        outcome_lines: list[str] = []
        if outcome_feed:
            outcome_lines.append(" [dim]─── RECENT OUTCOMES ───[/]")
            for oc in outcome_feed[:3]:
                _sym = str(oc.get("symbol", "?")).replace("-USD", "")
                _r   = float(oc.get("net_pnl_r", 0.0))
                _exit= str(oc.get("exit_reason", "?"))
                _r_col = "#00d084" if _r > 0 else "#ff4757"
                # Per-agent marks
                _marks = []
                for _ag in ("macro", "regime", "structure", "micro", "funding", "ssi"):
                    _correct = oc.get(f"{_ag}_correct", -1)
                    if _correct == 1:
                        _marks.append(f"[#00d084]✓[/]")
                    elif _correct == 0:
                        _marks.append(f"[#ff4757]✗[/]")
                    else:
                        _marks.append("[dim]·[/]")
                _marks_str = " ".join(_marks)
                outcome_lines.append(
                    f" [dim]{_sym}[/] [{_r_col}]{_r:+.2f}R[/]"
                    f" [dim]{_exit}[/]  {_marks_str}"
                )

        content_markup = "\n".join(outcome_lines) + alerts_text
        content_parts = [table]
        if content_markup.strip():
            content_parts.append(Text.from_markup(content_markup))

        border = "#00d4aa" if has_any else "#1a1a2a"
        title  = f"[bold #4d9fff]◉ SIGNAL AGENTS[/]{total_str}{warm_note}"
        return Panel(
            RichGroup(*content_parts),
            title=title,
            style="#e8edf2 on #080809",
            border_style=border,
            padding=(0, 0),
        )
