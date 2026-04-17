from typing import Literal, Dict, Any, List
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


# ── Per-symbol minimum order quantity (live API 2026-04-17) ───────────────────
# minQty == stepSize for all SoDEX symbols. Used to floor Nietzsche output.
SYMBOL_MIN_QUANTITY: Dict[str, float] = {
    "BTC-USD":       0.00001,
    "ETH-USD":       0.0001,
    "SOL-USD":       0.001,
    "LINK-USD":      0.1,
    "AVAX-USD":      1.0,
    "OP-USD":        0.1,
    "ARB-USD":       0.1,
    "SUI-USD":       0.1,
    "NEAR-USD":      0.1,
    "BNB-USD":       0.001,
    "1000PEPE-USD":  1.0,
    "MNT-USD":       1.0,
    "XAUT-USD":      0.0001,
    "XRP-USD":       0.1,
    "TRUMP-USD":     0.01,
    "BASED-USD":     1.0,
    "CL-USD":        0.001,
    "COPPER-USD":    0.01,
    "TSM-USD":       0.001,
    "ORCL-USD":      0.001,
    "NVDA-USD":      0.001,
    "MSFT-USD":      0.001,
    "AAPL-USD":      0.001,
    "AMZN-USD":      0.001,
    "GOOGL-USD":     0.001,
    "META-USD":      0.001,
    "TSLA-USD":      0.001,
}

# ── Per-symbol quantity precision (decimal places for formatting) ─────────────
SYMBOL_QTY_PRECISION: Dict[str, int] = {
    "BTC-USD":       5,
    "ETH-USD":       4,
    "SOL-USD":       3,
    "LINK-USD":      1,
    "AVAX-USD":      0,
    "OP-USD":        1,
    "ARB-USD":       1,
    "SUI-USD":       1,
    "NEAR-USD":      1,
    "BNB-USD":       3,
    "1000PEPE-USD":  0,
    "MNT-USD":       0,
    "XAUT-USD":      4,
    "XRP-USD":       1,
    "TRUMP-USD":     2,
    "BASED-USD":     0,
    "CL-USD":        3,
    "COPPER-USD":    2,
    "TSM-USD":       3,
    "ORCL-USD":      3,
    "NVDA-USD":      3,
    "MSFT-USD":      3,
    "AAPL-USD":      3,
    "AMZN-USD":      3,
    "GOOGL-USD":     3,
    "META-USD":      3,
    "TSLA-USD":      3,
}


class Settings(BaseSettings):
    # Mode — mainnet live only
    mode: Literal["live"] = "live"
    data_source: Literal["synthetic", "sodex", "bybit"] = "bybit"

    # ── Asset universe v2.0 — 14-coin, 6 market families ────────────────────────
    # Balanced across correlation clusters. Core 7 subscribe at startup;
    # watchlist 7 stagger in (3 per batch, 2s apart) to protect the display.
    assets: list[str] = [
        # ── Core (subscribed immediately) ──────────────────
        "BTC-USD",        # Large-cap crypto — price discovery anchor
        "ETH-USD",        # Large-cap crypto — smart contract benchmark
        "SOL-USD",        # Large-cap crypto — high-throughput L1
        "BNB-USD",        # Large-cap crypto — CEX ecosystem
        "XAUT-USD",       # Commodity / gold — uncorrelated to crypto
        "OP-USD",         # L2 ecosystem — Optimism
        "ARB-USD",        # L2 ecosystem — Arbitrum
        # ── Watchlist (staggered after startup) ────────────
        "AVAX-USD",       # Alt L1 — avalanche ecosystem
        "SUI-USD",        # Alt L1 — high-throughput Move chain
        "LINK-USD",       # DeFi infra — oracle network
        "NEAR-USD",       # Alt L1 — AI + chain abstraction narrative
        "MNT-USD",        # L2 — Mantle ecosystem
        "1000PEPE-USD",   # Meme — high liquidity, strong momentum vol
        "XRP-USD",        # Large-cap alt — payments narrative, high liquidity
        "TRUMP-USD",      # Meme / political — high volatility event coin
        "BASED-USD",      # Meme — Base chain native, momentum driven
        # ── Binary event / macro (SoDEX-only) ─────────────
        "CL-USD",         # Crude Oil — binary event / geopolitical catalyst
        "COPPER-USD",     # Copper — macro/industrial demand signal
        "TSM-USD",        # TSMC — AI chip / semiconductor momentum
        "ORCL-USD",       # Oracle — AI cloud momentum
    ]

    # ── Core assets: subscribed at WS connect, before display starts ─────────────
    # All other assets stagger in (3/batch, 2s apart) to prevent the initial
    # data burst that corrupts the Rich terminal display.
    core_assets: list[str] = [
        "BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD",
        "XAUT-USD", "OP-USD", "ARB-USD",
    ]

    # ── Signal-only assets: read-only price feeds for regime classification ────────
    # These are SPOT tokens on SoDEX — no perp contract exists.
    # NEVER added to config.assets (tradeable universe).
    # NEVER passed to fetch_symbol_ids() or the perp order path.
    # candle_buffers and signal_price_stores are built for these; execution layer skips them.
    signal_assets: list[str] = [
        "MAG7SSI-USD",   # MAG7 index SSI — index_tech regime; institutional inflow signal
        "DEFISSI-USD",   # DeFi SSI basket — index_defi regime; DeFi flow direction
        "MEMESSI-USD",   # Meme SSI basket — index_meme regime; retail euphoria indicator
        "USSI-USD",      # Universal SSI — index_equity regime; broad TradFi vs crypto
    ]

    # ── Asset category classification ────────────────────────────────────────────
    MACRO_SYNTHETIC_ASSETS: List[str] = []  # Removed — no index products in universe
    COMMODITY_ASSETS: List[str] = [
        "XAUT-USD",    # Gold
    ]
    MAG7_STOCK_ASSETS: List[str] = []  # Removed — not listed on SoDEX perps
    TIER_A_ASSETS: List[str] = [
        "BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD",
    ]
    TIER_B_ASSETS: List[str] = [
        "XAUT-USD",
        "AVAX-USD", "LINK-USD", "SUI-USD",
        "ARB-USD", "OP-USD", "NEAR-USD",
        "MNT-USD", "1000PEPE-USD",
        "XRP-USD", "TRUMP-USD", "BASED-USD",
        "CL-USD", "COPPER-USD", "TSM-USD", "ORCL-USD",
    ]

    def get_asset_category(self, symbol: str) -> str:
        if symbol in self.MACRO_SYNTHETIC_ASSETS:
            return "macro_synthetic"
        if symbol in self.COMMODITY_ASSETS:
            return "commodity"
        if symbol in self.MAG7_STOCK_ASSETS:
            return "mag7_stock"
        if symbol in self.TIER_A_ASSETS:
            return "crypto_large"
        if symbol in self.TIER_B_ASSETS:
            return "crypto_mid"
        return "crypto_mid"

    ASSET_CONFIG: Dict[str, Dict[str, Any]] = {
        # ── Crypto large-cap ──────────────────────────────────────────────────
        "BTC-USD":  {
            "tick_size": 1,
            "min_size": 0.00001,
            "max_leverage": 25,
            "category": "large_cap",
            "market_hours": "24h"
        },
        "ETH-USD":  {
            "tick_size": 0.1,
            "min_size": 0.0001,
            "max_leverage": 20,
            "category": "large_cap",
            "market_hours": "24h"
        },
        "SOL-USD":  {
            "tick_size": 0.01,
            "min_size": 0.001,
            "max_leverage": 20,
            "category": "alt_l1",
            "market_hours": "24h"
        },
        "BNB-USD":  {
            "tick_size": 0.1,
            "min_size": 0.001,
            "max_leverage": 20,
            "category": "cex_ecosystem",
            "market_hours": "24h"
        },
        # ── Crypto mid-cap ────────────────────────────────────────────────────
        "LINK-USD": {
            "tick_size": 0.001,
            "min_size": 0.1,
            "max_leverage": 10,
            "category": "defi_infra",
            "market_hours": "24h"
        },
        "AVAX-USD": {
            "tick_size": 0.001,
            "min_size": 1,
            "max_leverage": 20,
            "category": "alt_l1",
            "market_hours": "24h"
        },
        "SUI-USD":  {
            "tick_size": 0.0001,
            "min_size": 0.1,
            "max_leverage": 10,
            "category": "alt_l1",
            "market_hours": "24h"
        },
        "ARB-USD":  {
            "tick_size": 0.00001,
            "min_size": 0.1,
            "max_leverage": 5,
            "category": "l2",
            "market_hours": "24h"
        },
        "OP-USD":   {
            "tick_size": 0.00001,
            "min_size": 0.1,
            "max_leverage": 5,
            "category": "l2",
            "market_hours": "24h"
        },
        "NEAR-USD": {
            "tick_size": 0.0001,
            "min_size": 0.1,
            "max_leverage": 10,
            "category": "alt_l1",
            "market_hours": "24h"
        },
        # ── Commodities ───────────────────────────────────────────────────────
        "XAUT-USD": {
            "tick_size": 0.1,
            "min_size": 0.0001,
            "max_leverage": 25,
            "category": "commodity",
            "market_hours": "gold_hours"
        },
        # ── L2 — Mantle ───────────────────────────────────────────────────────
        "MNT-USD": {
            "tick_size": 0.0001,
            "min_size": 1,
            "max_leverage": 10,
            "category": "l2",
            "market_hours": "24h"
        },
        # ── Meme / high vol ───────────────────────────────────────────────────
        "1000PEPE-USD": {
            "tick_size": 0.000001,
            "min_size": 100,
            "max_leverage": 10,
            "category": "meme",
            "market_hours": "24h"
        },
        # ── High-vol alts / meme ──────────────────────────────────────────────
        "XRP-USD": {
            "tick_size": 0.0001,
            "min_size": 1,
            "max_leverage": 20,
            "category": "alt_l1",
            "market_hours": "24h"
        },
        "TRUMP-USD": {
            "tick_size": 0.001,
            "min_size": 0.1,
            "max_leverage": 10,
            "category": "meme",
            "market_hours": "24h"
        },
        "BASED-USD": {
            "tick_size": 0.0001,
            "min_size": 10,
            "max_leverage": 10,
            "category": "meme",
            "market_hours": "24h"
        },
        # ── Binary event / macro (SoDEX-only) ────────────────────────────────
        # Tick/step sizes are best-estimates — verify against SoDEX /markets/symbols on first run.
        "CL-USD": {
            "tick_size": 0.01,
            "min_size": 0.01,
            "max_leverage": 10,
            "category": "commodity",
            "market_hours": "gold_hours"
        },
        "COPPER-USD": {
            "tick_size": 0.001,
            "min_size": 0.1,
            "max_leverage": 10,
            "category": "commodity",
            "market_hours": "gold_hours"
        },
        "TSM-USD": {
            "tick_size": 0.01,
            "min_size": 0.01,
            "max_leverage": 5,
            "category": "equity",
            "market_hours": "ustech_hours"
        },
        "ORCL-USD": {
            "tick_size": 0.01,
            "min_size": 0.01,
            "max_leverage": 5,
            "category": "equity",
            "market_hours": "ustech_hours"
        },
        "NVDA-USD": {
            "tick_size": 0.01,
            "min_size": 0.01,
            "max_leverage": 5,
            "category": "equity",
            "market_hours": "ustech_hours"
        },
        "MSFT-USD": {
            "tick_size": 0.01,
            "min_size": 0.01,
            "max_leverage": 5,
            "category": "equity",
            "market_hours": "ustech_hours"
        },
        "AAPL-USD": {
            "tick_size": 0.01,
            "min_size": 0.01,
            "max_leverage": 5,
            "category": "equity",
            "market_hours": "ustech_hours"
        },
        "AMZN-USD": {
            "tick_size": 0.01,
            "min_size": 0.01,
            "max_leverage": 5,
            "category": "equity",
            "market_hours": "ustech_hours"
        },
        "GOOGL-USD": {
            "tick_size": 0.01,
            "min_size": 0.01,
            "max_leverage": 5,
            "category": "equity",
            "market_hours": "ustech_hours"
        },
        "META-USD": {
            "tick_size": 0.01,
            "min_size": 0.01,
            "max_leverage": 5,
            "category": "equity",
            "market_hours": "ustech_hours"
        },
        "TSLA-USD": {
            "tick_size": 0.01,
            "min_size": 0.01,
            "max_leverage": 5,
            "category": "equity",
            "market_hours": "ustech_hours"
        },
        # ── SSI signal tokens (read-only price feeds — no perp, not tradeable) ──
        "MAG7SSI-USD": {
            "tick_size": 0.0001,
            "min_size": 1.0,
            "max_leverage": 1,
            "category": "index_tech",
            "market_hours": "24h",
            "tradeable": False,         # ← execution layer skips this asset
            "spot_ws_symbol": "MAG7SSI_USDC",
        },
        "DEFISSI-USD": {
            "tick_size": 0.0001,
            "min_size": 1.0,
            "max_leverage": 1,
            "category": "index_defi",
            "market_hours": "24h",
            "tradeable": False,
            "spot_ws_symbol": "DEFISSI_USDC",
        },
        "MEMESSI-USD": {
            "tick_size": 0.0001,
            "min_size": 1.0,
            "max_leverage": 1,
            "category": "index_meme",
            "market_hours": "24h",
            "tradeable": False,
            "spot_ws_symbol": "MEMESSI_USDC",
        },
        "USSI-USD": {
            "tick_size": 0.0001,
            "min_size": 1.0,
            "max_leverage": 1,
            "category": "index_equity",
            "market_hours": "24h",
            "tradeable": False,
            "spot_ws_symbol": "USSI_USDC",
        },
    }

    # SoDEX WebSocket endpoints
    mainnet_ws_spot: str = "wss://mainnet-gw.sodex.dev/ws/spot"
    mainnet_ws_perps: str = "wss://mainnet-gw.sodex.dev/ws/perps"

    # Data settings
    orderbook_max_age_ms: int = 500
    candle_buffer_size: int = 200
    loop_interval_ms: int = 1000
    
    # REST Endpoint
    mainnet_rest_url: str = "https://mainnet-gw.sodex.dev/api/v1"

    # Logging & Monitoring
    log_level: str = "INFO"
    log_dir: str = "./logs"
    telegram_bot_token: str = Field(default="", description="Telegram Bot Token")
    telegram_chat_id: str = Field(default="", description="Telegram Chat ID")
    deepseek_api_key: str = Field(default="", description="DeepSeek API Key")
    debug: bool = False

    # SoDEX Credentials (v1.3 Primary)
    sodex_private_key: str = Field(default="", description="Private key for EIP-712 signing")
    sodex_account_id: str = Field(default="", description="SoDEX account ID")
    sodex_mainnet: bool = True

    # Execution layer settings (Legacy/Fallback)
    private_key: str = Field(default="", description="Private key for EIP-712 signing")
    account_id: str = Field(default="", description="SoDEX account ID")
    chain_id_mainnet: int = 286623

    live_risk_pct: float = 0.01  # 1% risk per trade in mainnet
    live_min_coherence: float = 5.0  # Raised from 3.0 — WR evidence: trades at 2.15-2.5 effective coherence losing. 5.0 = institutional floor.
    default_leverage: int = 6   # 6x: margin=$33 per $200 trade, liq ~16.7% away. Safer than 10x on thin SoDEX books.
    arb_capital_pct: float = 0.2  # 20% of balance for arb capital
    live_mode_confirmed: bool = Field(default=False, description="Must be True for live mode")

    # Mainnet Limits
    balance_floor: float = 50.0          # Minimum account balance to permit trading
    daily_loss_limit_pct: float = 0.05   # Gate 8: 5% daily loss circuit breaker
    max_daily_loss_pct: float = 0.05     # Alias for risk_engine gate lookup
    max_deployed_pct: float = 0.40
    min_trade_notional_usd: float = 50.0   # Post-multiplier floor: SoDEX minimum (~$50). Temporal/DD multipliers already
                                            # reduce size — don't additionally gate valid signals on balance math.

    # Gate 1 — Portfolio VaR limit
    max_portfolio_var_pct: float = 0.40  # 40% — sized for leveraged crypto; updates dynamically with balance

    # Gate 2 — Symbol concentration cap
    max_symbol_concentration: float = 0.20  # 20% of balance per symbol

    # SoDEX mainnet thin-market thresholds (Gate B)
    # SoDEX books are thin — $100 depth / 50bps spread is CEX-calibrated and blocks all trades.
    # $25 depth = realistic for SoDEX; 150bps spread = 1.5% which is still tradeable at 10x.
    min_ob_depth_usd: float = 25.0     # Minimum USD depth within 0.5% of entry
    max_spread_bps: float = 150.0      # Maximum bid-ask spread in basis points (1.5%)

    # DrawdownManager thresholds (used by risk/drawdown_manager.py)
    max_weekly_drawdown: float = 0.15          # 15% weekly → reduce size
    max_total_drawdown: float = 0.25           # 25% total → halt directional
    drawdown_recovery_threshold: float = 0.10  # 10% gain from low watermark to resume

    # Fixed floor position sizing — replaces Kelly on small accounts
    # Set base_trade_usd > 0 to use conviction-scaled notional instead of risk_pct × balance.
    # Mainnet: $200 base, conviction × [1.0, 1.5, 2.0], capped at max_notional_usd.
    # Balance safety cap (50% of balance) applied before returning from build_candidate.
    # Temporal/DD multipliers applied AFTER build_candidate — min_trade_notional_usd is
    # the post-multiplier SoDEX floor (50).
    base_trade_usd: float = 200.0    # Base notional per trade
    min_trade_usd: float = 200.0     # Hard $200 minimum per trade — never build below this
    max_trade_usd: float = 500.0     # Hard ceiling notional; balance safety cap may reduce below this
    max_notional_usd: float = 500.0  # Alias for max_trade_usd — used in sizing formula

    # Cascade intelligence thresholds
    cascade_min_coherence: float = 3.0        # Coherence floor for cascade-primed entries
    momentum_velocity_threshold: float = 3.0  # Events/s² above which cascade is classified momentum
    momentum_notional_threshold: float = 50000.0  # Min notional (USD) for momentum cascade

    # Trade activity targets (informational — not enforced as a gate)
    max_daily_trades: int = 40
    target_daily_trades: int = 20

    # Capital efficiency — $300 / 5 trades / 30-min cycle
    # Per-asset minimum ATR-as-% of price required for entry.
    # Crypto stays at 1.0% (losers 0.7%, winners 1.4% — source: live trade analysis).
    # Equities/commodities have lower baseline vol so use lower thresholds.
    # CL-USD binary event gets 0.5% — pre-event entry before catalyst fires.
    atr_min_pct: Dict[str, float] = {
        "BTC-USD": 1.0, "ETH-USD": 1.0, "SOL-USD": 1.0, "BNB-USD": 1.0,
        "XAUT-USD": 0.8, "LINK-USD": 1.0, "AVAX-USD": 1.0, "SUI-USD": 1.0,
        "ARB-USD": 1.0, "OP-USD": 1.0, "NEAR-USD": 1.0, "MNT-USD": 1.0,
        "1000PEPE-USD": 1.0,
        # Binary event / macro — lower threshold: move hasn't happened yet
        "CL-USD":     0.5,
        "COPPER-USD": 0.6,
        "TSM-USD":    0.7,
        "ORCL-USD":   0.7,
    }

    stop_atr_mult: float = 1.5           # Stop buffer: 1.5×ATR. Floor: max(1.5×ATR, 0.8% of price).
                                         # 0.5% floor was too tight — AVAX/LINK/SOL noise hits it in seconds.
                                         # 0.8% gives ~60% more breathing room; at 6x = 4.8% margin loss max.
    max_hold_minutes: int = 30           # Time stop: exit flat/losing trades after 30 min
    max_concurrent_positions: int = 5    # Global position cap across all symbols
    max_margin_per_trade_pct: float = 0.20  # Cap single-trade margin at 20% of balance ($60 on $300)
    trail_activation_atr: float = 0.5   # Trail activates after 0.5×ATR favorable move
    trail_distance_atr: float = 0.5     # Trail distance: stop = best ± 0.5×ATR

    # Fallback/Legacy Aliases (for Pydantic validation)
    risk_pct: float = 0.015             # 1.5% risk per trade (half-Kelly for $300 account)
    min_coherence: float = 3.0  # Gate 5: raised from 1.0 — need WR ≥40% for positive EV at 2:1 RR

    # Computed properties
    @property
    def sodex_chain_id(self) -> int:
        return 286623 if self.sodex_mainnet else 138565

    @property
    def sodex_ws_perps(self) -> str:
        base = "mainnet-gw.sodex.dev" if self.sodex_mainnet else "testnet-gw.sodex.dev"
        return f"wss://{base}/ws/perps"

    @property
    def sodex_rest_perps(self) -> str:
        base = "mainnet-gw.sodex.dev" if self.sodex_mainnet else "testnet-gw.sodex.dev"
        return f"https://{base}/api/v1/perps"

    # WebSocket URL properties — always use mainnet (sodex_mainnet=True enforced by .env)
    @property
    def ws_spot_url(self) -> str:
        return self.mainnet_ws_spot

    @property
    def ws_perps_url(self) -> str:
        return self.mainnet_ws_perps

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )
