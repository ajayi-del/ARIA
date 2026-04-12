from typing import Literal, Dict, Any, List
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

class Settings(BaseSettings):
    # Mode — mainnet live only
    mode: Literal["live"] = "live"
    data_source: Literal["synthetic", "sodex", "bybit"] = "bybit"

    # ── Asset universe (v1.8 — full SoDEX discovery 2026-04-13) ─────────────────
    # Active trading universe. USTECH100-USD is the MAG7SSI Tier 1 proxy.
    assets: list[str] = [
        # Core crypto
        "BTC-USD", "ETH-USD", "SOL-USD", "XAUT-USD",
        "BNB-USD", "LINK-USD", "AVAX-USD",
        # US indices and synthetics (MAG7SSI proxy + S&P 500)
        "USTECH100-USD", "US500-USD",
        # Precious metals
        "SILVER-USD",
        # Mag7 individual stocks
        "NVDA-USD", "AAPL-USD", "MSFT-USD", "META-USD",
        "AMZN-USD", "GOOGL-USD", "TSLA-USD",
        # Crypto mid-cap
        "SUI-USD", "APT-USD", "ARB-USD", "OP-USD", "NEAR-USD",
    ]

    # ── Asset category classification ────────────────────────────────────────────
    MACRO_SYNTHETIC_ASSETS: List[str] = [
        "USTECH100-USD",  # Nasdaq 100 — MAG7SSI proxy (Tier 1 signal source)
        "US500-USD",      # S&P 500
    ]
    COMMODITY_ASSETS: List[str] = [
        "XAUT-USD",    # Gold
        "SILVER-USD",  # Silver
    ]
    MAG7_STOCK_ASSETS: List[str] = [
        "AAPL-USD", "AMZN-USD", "GOOGL-USD",
        "META-USD", "MSFT-USD", "NVDA-USD", "TSLA-USD",
    ]
    TIER_A_ASSETS: List[str] = [
        "BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD",
    ]
    TIER_B_ASSETS: List[str] = [
        "AVAX-USD", "LINK-USD", "SUI-USD", "APT-USD",
        "ARB-USD", "OP-USD", "NEAR-USD",
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
        "APT-USD":  {
            "tick_size": 0.0001,
            "min_size": 0.01,
            "max_leverage": 5,
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
        "SILVER-USD": {
            "tick_size": 0.001,
            "min_size": 0.01,
            "max_leverage": 20,
            "category": "commodity",
            "market_hours": "gold_hours"
        },
        # ── US indices / macro synthetics ─────────────────────────────────────
        "USTECH100-USD": {
            "tick_size": 1,
            "min_size": 0.0001,
            "max_leverage": 25,
            "category": "macro_synthetic",
            "market_hours": "ustech_hours"
        },
        "US500-USD": {
            "tick_size": 0.1,
            "min_size": 0.001,
            "max_leverage": 20,
            "category": "macro_synthetic",
            "market_hours": "ustech_hours"
        },
        # ── Mag7 individual stocks ─────────────────────────────────────────────
        "NVDA-USD": {
            "tick_size": 0.01,
            "min_size": 0.001,
            "max_leverage": 10,
            "category": "mag7_stock",
            "market_hours": "ustech_hours"
        },
        "AAPL-USD": {
            "tick_size": 0.01,
            "min_size": 0.001,
            "max_leverage": 10,
            "category": "mag7_stock",
            "market_hours": "ustech_hours"
        },
        "MSFT-USD": {
            "tick_size": 0.01,
            "min_size": 0.001,
            "max_leverage": 10,
            "category": "mag7_stock",
            "market_hours": "ustech_hours"
        },
        "META-USD": {
            "tick_size": 0.01,
            "min_size": 0.001,
            "max_leverage": 10,
            "category": "mag7_stock",
            "market_hours": "ustech_hours"
        },
        "AMZN-USD": {
            "tick_size": 0.01,
            "min_size": 0.001,
            "max_leverage": 10,
            "category": "mag7_stock",
            "market_hours": "ustech_hours"
        },
        "GOOGL-USD": {
            "tick_size": 0.01,
            "min_size": 0.001,
            "max_leverage": 10,
            "category": "mag7_stock",
            "market_hours": "ustech_hours"
        },
        "TSLA-USD": {
            "tick_size": 0.01,
            "min_size": 0.001,
            "max_leverage": 10,
            "category": "mag7_stock",
            "market_hours": "ustech_hours"
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
    live_min_coherence: float = 1.0  # SoDEX thin market floor (calibrates upward after 50 trades)
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

    # Trade activity targets (informational — not enforced as a gate)
    max_daily_trades: int = 40
    target_daily_trades: int = 20

    # Capital efficiency — $300 / 5 trades / 30-min cycle
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
    min_coherence: float = 1.0  # Gate 5: SoDEX thin market floor, calibrates upward after 50 trades

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
