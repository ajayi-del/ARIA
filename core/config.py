from typing import Literal, Dict, Any, List
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator


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
    "XAUT-USD":      0.0001,
    "XRP-USD":       0.1,
    "DOGE-USD":      1.0,
    "HBAR-USD":      1.0,
    "TRUMP-USD":     0.01,
    "BASED-USD":     1.0,
    "LTC-USD":       0.01,
    "CL-USD":        0.001,
    "COPPER-USD":    0.01,
    "SILVER-USD":    0.01,
    "CRCL-USD":      0.001,
    "TSM-USD":       0.001,
    "ORCL-USD":      0.001,
    "NVDA-USD":      0.001,
    "MSFT-USD":      0.001,
    "AAPL-USD":      0.001,
    "AMZN-USD":      0.001,
    "GOOGL-USD":     0.001,
    "META-USD":      0.001,
    "TSLA-USD":      0.001,
    "USTECH100-USD": 0.0001,
    "SPCX-USD":      0.0001,
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
    "XAUT-USD":      4,
    "XRP-USD":       1,
    "DOGE-USD":      0,
    "HBAR-USD":      0,
    "TRUMP-USD":     2,
    "BASED-USD":     0,
    "CL-USD":        3,
    "COPPER-USD":    2,
    "CRCL-USD":      3,
    "TSM-USD":       3,
    "ORCL-USD":      3,
    "NVDA-USD":      3,
    "MSFT-USD":      3,
    "AAPL-USD":      3,
    "AMZN-USD":      3,
    "GOOGL-USD":     3,
    "META-USD":      3,
    "TSLA-USD":      3,
    "USTECH100-USD": 4,
    "SPCX-USD":      4,
}


# ── Per-symbol minimum coherence floors (evidence-based, Apr-2026 audit) ─────
# Symbols with no demonstrated edge at low conviction — only trade on high
# certainty signals.  Falls back to global live_min_coherence when not listed.
SYMBOL_MIN_COHERENCE: Dict[str, float] = {
    "TRUMP-USD": 6.5,   # Meme volatility traps without strong directional signal
    "BASED-USD": 6.0,   # Meme — require conviction, not noise
    # Evidence-based floors from Jun 22-23 trade audit (0% WR symbols):
    # Quant overhaul Jul-16: equities raised to 6.0 (was 5.0) — oracle-driven
    # SoDEX equity perps have 0-25% WR below 6.0 coherence.
    "AAPL-USD":  6.0,
    "GOOGL-USD": 6.0,
    "AMZN-USD":  6.0,
    "NVDA-USD":  6.0,
    "MSFT-USD":  6.0,
    "TSLA-USD":  6.0,
    "META-USD":  5.5,   # Only marginally profitable equity — softer floor
    "BTC-USD":   4.5,   # 0W/1L, major symbol — weak signals = noise
    "SOL-USD":   4.0,   # 2W/7L (22% WR) — needs cleaner directional signal
}

# ── Per-symbol minimum stop distance (% from reference price) ────────────────
# SoDEX rejects stops placed too close to mark/entry ("stopPrice is invalid").
# Enforced in sodex_client before order submission.
MIN_STOP_DISTANCE_PCT: Dict[str, float] = {
    "BTC-USD":   0.5,
    "ETH-USD":   0.5,
    "SOL-USD":   0.5,
    "BNB-USD":   0.5,
    "LINK-USD":  0.5,
    "AVAX-USD":  0.5,
    "SUI-USD":   0.5,
    "NEAR-USD":  0.5,
    "ARB-USD":   0.5,
    "OP-USD":    0.5,
    "XRP-USD":   0.5,
    "DOGE-USD":  0.5,
    "HBAR-USD":  0.5,
    "COIN-USD":  0.5,
    "LTC-USD":   0.5,
    "XAUT-USD":  0.5,
    "CL-USD":    0.5,
    "COPPER-USD": 0.5,
    "SILVER-USD": 0.5,
    "CRCL-USD": 0.5,
    "USTECH100-USD": 0.5,
    "SPCX-USD": 0.5,
    # Equities: wider minimum (observed rejections at 1.5-1.6%)
    "NVDA-USD":  1.5,
    "MSFT-USD":  1.5,
    "AAPL-USD":  1.5,
    "AMZN-USD":  2.0,
    "GOOGL-USD": 1.5,
    "META-USD":  1.5,
    "TSLA-USD":  1.5,
    "TSM-USD":   1.5,
    "ORCL-USD":  1.5,
}
DEFAULT_MIN_STOP_DISTANCE_PCT: float = 1.0


class Settings(BaseSettings):
    # Mode — mainnet live only
    mode: Literal["live"] = "live"
    data_source: Literal["synthetic", "sodex", "bybit"] = "sodex"

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
        "DOGE-USD",       # Large-cap meme — retail sentiment + liquidity
        "HBAR-USD",       # Enterprise L1 — governing council narrative
        "1000PEPE-USD",   # Meme — high liquidity, strong momentum vol
        "XRP-USD",        # Large-cap alt — payments narrative, high liquidity
        "TRUMP-USD",      # Meme / political — high volatility event coin
        # BASED-USD removed: exchange rejects updateLeverage (id 78) — dead market
        "CRCL-USD",       # Circle — stablecoin infra, crypto equity proxy
        "COIN-USD",       # Coinbase — crypto exchange equity proxy
        # ── Legacy L1 ───────────────────────────────────────
        "LTC-USD",        # Litecoin — legacy payment crypto, high liquidity
        # ── Binary event / macro (SoDEX-only) ─────────────
        "CL-USD",         # Crude Oil — binary event / geopolitical catalyst
        "COPPER-USD",     # Copper — macro/industrial demand signal
        "SILVER-USD",     # Silver — precious metal, industrial demand + macro hedge
        "TSM-USD",        # TSMC — AI chip / semiconductor momentum
        "ORCL-USD",       # Oracle — AI cloud momentum
        # ── Equities (SoDEX perps, 24/7) ──────────────────
        "NVDA-USD",       # Nvidia — AI hardware cycle leader
        "MSFT-USD",       # Microsoft — AI/cloud bellwether
        "AAPL-USD",       # Apple — consumer cycle / risk barometer
        "AMZN-USD",       # Amazon — cloud + consumer macro
        "GOOGL-USD",      # Alphabet — AI/search revenue proxy
        "META-USD",       # Meta — digital ad cycle + AI infra
        "TSLA-USD",       # Tesla — EV cycle + retail sentiment
        "USTECH100-USD",  # Nasdaq 100 — tech macro regime proxy
        "SPCX-USD",       # S&P 500 — broad market equity index proxy
        # ── Bybit venue (routed via execution/venue.py; candles/OI/funding ────
        # from data/bybit_feed.py — same deep-market signal source as crypto).
        "HYPE-USD",       # Perp DEX ecosystem — deepest Bybit-only book ($189M/24h)
        "ADA-USD",        # Large-cap alt L1
        "UNI-USD",        # DeFi blue chip
        "ONDO-USD",       # RWA narrative
        "TAO-USD",        # AI — deep perp liquidity
        "ENA-USD",        # DeFi infra / stablecoin adoption
        "KAITO-USD",      # AI data — price discovery phase
        "WIF-USD",        # High-beta meme — cascade material
        "ZEC-USD",        # Privacy — strong OI ($69M)
        "VIRTUAL-USD",    # AI agents infrastructure
        "AAVE-USD",       # DeFi lending blue chip
        "1000BONK-USD",   # Meme — 1000-denominated (like 1000PEPE)
        "SEI-USD",        # High-throughput L1
        "PENGU-USD",      # Meme / NFT ecosystem
        "INJ-USD",        # DeFi L1
        "TIA-USD",        # Modular L1
        "APT-USD",        # Alt L1 — Move ecosystem
        # ── Aster-expansion incubation universe (2026-08-15) ──────────────
        # In config.assets so signals + shadow-journal scoring run NOW;
        # execution is blocked at order_blocked_no_symbol_id (gate "no_venue")
        # until ASTER_ENABLED=true routes them. Dual-verified: Aster TRADING
        # + real quoteVolume + Bybit perp data path (BYBIT_SYMBOL_MAP).
        "TRX-USD",        # Legacy L1 — payments, deep liquidity
        "BCH-USD",        # Legacy L1 — payment crypto
        "XLM-USD",        # Legacy L1 — payments narrative
        "FARTCOIN-USD",   # Meme — high-beta Solana narrative
        "VELVET-USD",     # DeFi asset management ($1.9M/day Aster)
        "AKE-USD",        # Gaming/AI narrative ($25.6M/day Aster)
        "CYS-USD",        # ZK infra narrative ($12.8M/day Aster)
        "ASTER-USD",      # Aster DEX token — venue-native ($6.4M/day)
        "ACE-USD",        # Gaming L1 narrative ($6.1M/day Aster)
        "MUBARAK-USD",    # Meme — BNB-chain community narrative
        "DOS-USD",        # Small-cap narrative — explosive-alt watchlist
        "SNXX-USD",       # Small-cap narrative — explosive-alt watchlist
        "HEMI-USD",       # Modular BTC L2 (2026-08-16 operator add — Aster+Bybit verified)
        "AIO-USD",        # Small-cap narrative (2026-08-16 operator add — Aster+Bybit verified)
        "ARIA-USD",       # Small-cap narrative (2026-08-16 operator add — Aster+Bybit verified)
        # ── 2026-08-21 expansion (operator: toward 70 aster symbols, tempered
        # by cluster families — quality bars: Aster vol ≥$300K/24h AND Bybit
        # perp data path AND family diversity). Volumes verified same-day.
        "WLD-USD",        # Worldcoin — AI/identity narrative (Aster $480K, Bybit $55M)
        "BOME-USD",       # Meme — Book of Meme (Aster $936K, Bybit $82.6M)
        "ICP-USD",        # Alt L1 — Internet Computer (Aster $555K, Bybit $10.7M)
        "XMR-USD",        # Privacy L1 — Monero (Aster $439K, Bybit $17M)
        "ORDI-USD",       # BTC-ecosystem narrative — BRC-20 (Aster $390K, Bybit $19M)
        "WLFI-USD",       # DeFi governance — World Liberty Fi (Aster $1.95M, Bybit $18M)
        "LIT-USD",        # CEX ecosystem — Lighter perp DEX (Aster $5.2M, Bybit $51.5M)
        "PAXG-USD",       # Commodity — PAX Gold token (Aster $529K, Bybit $5.7M)
    ]

    # ── Core assets: subscribed at WS connect, before display starts ─────────────
    # All other assets stagger in (3/batch, 2s apart) to prevent the initial
    # data burst that corrupts the Rich terminal display.
    core_assets: list[str] = [
        "BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD",
        "XAUT-USD", "OP-USD", "ARB-USD",
        "SPCX-USD",  # Campaign priority — immediate WS subscription
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

    @field_validator("assets", "core_assets", "signal_assets", "bybit_assets",
                     "aster_assets", "aster_shadow_assets", "aster_kline_assets",
                     mode="before")
    @classmethod
    def _universe_is_code_only(cls, v, info):
        # .env is for secrets, not universe config (issue #17; regression
        # 2026-07-28 when a stale ASSETS= line resurrected delisted BASED-USD).
        # Any env-supplied universe is discarded — the code list is the only
        # source of truth.
        return cls.model_fields[info.field_name].default

    # ── Asset category classification ────────────────────────────────────────────
    MACRO_SYNTHETIC_ASSETS: List[str] = []  # Removed — no index products in universe
    COMMODITY_ASSETS: List[str] = [
        "XAUT-USD",    # Gold
        "SILVER-USD",  # Silver
    ]
    MAG7_STOCK_ASSETS: List[str] = []  # Removed — not listed on SoDEX perps

    # Assets that use their OWN price structure for HTF bias.
    # BTC HTF direction is irrelevant for gold/oil/equities — they move on different macro drivers.
    # The HTF counter-trend gate is skipped entirely for these symbols.
    TRADFI_ASSETS: List[str] = [
        "XAUT-USD",       # Gold — inverse to BTC during risk-off
        "SILVER-USD",     # Silver — precious metal + industrial demand
        "CL-USD",         # Crude Oil — geopolitical/supply driven
        "COPPER-USD",     # Copper — industrial demand signal
        "USTECH100-USD",  # Nasdaq 100 — tech macro regime proxy
        "TSM-USD",        # Taiwan Semi — AI chip cycle
        "ORCL-USD",       # Oracle — AI cloud
        "NVDA-USD",       # Nvidia — AI hardware
        "MSFT-USD",       # Microsoft — AI/cloud
        "AAPL-USD",       # Apple — consumer cycle
        "AMZN-USD",       # Amazon — cloud/consumer
        "GOOGL-USD",      # Google — AI/search
        "META-USD",       # Meta — digital advertising
        "TSLA-USD",       # Tesla — EV cycle
        "SPCX-USD",       # S&P 500 — broad market index
    ]
    TIER_A_ASSETS: List[str] = [
        "BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD",
    ]
    TIER_B_ASSETS: List[str] = [
        "XAUT-USD",
        "AVAX-USD", "LINK-USD", "SUI-USD",
        "ARB-USD", "OP-USD", "NEAR-USD",
        "1000PEPE-USD",
        "XRP-USD", "DOGE-USD", "HBAR-USD",
        "TRUMP-USD",
        "CRCL-USD", "COIN-USD",
        "LTC-USD",
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
            "max_leverage": 7,
            "preferred_leverage": 7,
            "category": "large_cap",
            "market_hours": "24h"
        },
        "ETH-USD":  {
            "tick_size": 0.1,
            "min_size": 0.0001,
            "max_leverage": 8,
            "preferred_leverage": 7,
            "category": "large_cap",
            "market_hours": "24h"
        },
        "SOL-USD":  {
            "tick_size": 0.01,
            "min_size": 0.001,
            "max_leverage": 8,
            "preferred_leverage": 7,
            "category": "alt_l1",
            "market_hours": "24h"
        },
        "BNB-USD":  {
            "tick_size": 0.1,
            "min_size": 0.001,
            "max_leverage": 8,
            "category": "cex_ecosystem",
            "market_hours": "24h"
        },
        # ── Crypto mid-cap ────────────────────────────────────────────────────
        "LINK-USD": {
            "tick_size": 0.001,
            "min_size": 0.1,
            "max_leverage": 5,
            "category": "defi_infra",
            "market_hours": "24h"
        },
        "AVAX-USD": {
            "tick_size": 0.001,
            "min_size": 1,
            "max_leverage": 5,
            "category": "alt_l1",
            "market_hours": "24h"
        },
        "SUI-USD":  {
            "tick_size": 0.0001,
            "min_size": 0.1,
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
            "max_leverage": 5,
            "category": "alt_l1",
            "market_hours": "24h"
        },
        # ── Commodities ───────────────────────────────────────────────────────
        "XAUT-USD": {
            "tick_size": 0.1,
            "min_size": 0.0001,
            "max_leverage": 7,
            "preferred_leverage": 7,
            "category": "commodity",
            "market_hours": "24h"
        },
        # ── Meme / high vol ───────────────────────────────────────────────────
        "1000PEPE-USD": {
            "tick_size": 0.000001,
            "min_size": 100,
            "max_leverage": 5,
            "category": "meme",
            "market_hours": "24h"
        },
        # ── Legacy L1 ─────────────────────────────────────────────────────────
        "LTC-USD": {
            "tick_size": 0.01,
            "min_size": 0.01,
            "max_leverage": 5,
            "preferred_leverage": 5,
            "category": "crypto",
            "market_hours": "24h"
        },
        # ── High-vol alts / meme ──────────────────────────────────────────────
        "XRP-USD": {
            "tick_size": 0.0001,
            "min_size": 1,
            "max_leverage": 5,
            "category": "alt_l1",
            "market_hours": "24h"
        },
        "TRUMP-USD": {
            "tick_size": 0.0001,
            "min_size": 0.01,
            "max_leverage": 5,
            "category": "meme",
            "market_hours": "24h"
        },
        "BASED-USD": {
            "tick_size": 0.0001,
            "min_size": 1,
            "max_leverage": 5,
            "category": "meme",
            "market_hours": "24h"
        },
        "CRCL-USD": {
            "tick_size": 0.001,
            "min_size": 0.001,
            "max_leverage": 8,
            "preferred_leverage": 7,
            "category": "crypto",
            "market_hours": "24h"
        },
        "COIN-USD": {
            "tick_size": 0.001,
            "min_size": 0.001,
            "max_leverage": 8,
            "preferred_leverage": 7,
            "category": "crypto",
            "market_hours": "24h"
        },
        "DOGE-USD": {
            "tick_size": 1,
            "min_size": 1,
            "max_leverage": 8,
            "preferred_leverage": 5,
            "category": "meme",
            "market_hours": "24h"
        },
        "HBAR-USD": {
            "tick_size": 1,
            "min_size": 1,
            "max_leverage": 5,
            "preferred_leverage": 5,
            "category": "alt_l1",
            "market_hours": "24h"
        },
        # ── Binary event / macro (SoDEX-only) ────────────────────────────────
        # Tick/step sizes are best-estimates — verify against SoDEX /markets/symbols on first run.
        "CL-USD": {
            "tick_size": 0.001,   # live API confirmed (sodex_client: 0.001)
            "min_size": 0.001,
            "max_leverage": 7,
            "preferred_leverage": 7,
            "category": "commodity",
            "market_hours": "24h"
        },
        "COPPER-USD": {
            "tick_size": 0.0001,  # live API confirmed (sodex_client: 0.0001)
            "min_size": 0.01,
            "max_leverage": 7,
            "preferred_leverage": 5,
            "category": "commodity",
            "market_hours": "24h"
        },
        "SILVER-USD": {
            "tick_size": 0.001,
            "min_size": 0.01,
            "max_leverage": 7,
            "preferred_leverage": 5,
            "category": "commodity",
            "market_hours": "24h"
        },
        "TSM-USD": {
            "tick_size": 0.01,
            "min_size": 0.001,
            "max_leverage": 7,
            "preferred_leverage": 7,
            "category": "equity",
            "market_hours": "24h"
        },
        "ORCL-USD": {
            "tick_size": 0.01,
            "min_size": 0.001,
            "max_leverage": 7,
            "preferred_leverage": 7,
            "category": "equity",
            "market_hours": "24h"
        },
        "NVDA-USD": {
            "tick_size": 0.01,
            "min_size": 0.001,
            "max_leverage": 5,
            "preferred_leverage": 5,
            "category": "equity",
            "market_hours": "24h"
        },
        "MSFT-USD": {
            "tick_size": 0.01,
            "min_size": 0.001,
            "max_leverage": 5,
            "preferred_leverage": 5,
            "category": "equity",
            "market_hours": "24h"
        },
        "AAPL-USD": {
            "tick_size": 0.01,
            "min_size": 0.001,
            "max_leverage": 5,
            "preferred_leverage": 5,
            "category": "equity",
            "market_hours": "24h"
        },
        "AMZN-USD": {
            "tick_size": 0.01,
            "min_size": 0.001,
            "max_leverage": 7,
            "preferred_leverage": 7,
            "category": "equity",
            "market_hours": "24h"
        },
        "GOOGL-USD": {
            "tick_size": 0.01,
            "min_size": 0.001,
            "max_leverage": 5,
            "preferred_leverage": 5,
            "category": "equity",
            "market_hours": "24h"
        },
        "META-USD": {
            "tick_size": 0.01,
            "min_size": 0.001,
            "max_leverage": 5,
            "preferred_leverage": 5,
            "category": "equity",
            "market_hours": "24h"
        },
        "TSLA-USD": {
            "tick_size": 0.01,
            "min_size": 0.001,
            "max_leverage": 5,
            "preferred_leverage": 5,
            "category": "equity",
            "market_hours": "24h"
        },
        "USTECH100-USD": {
            "tick_size": 0.1,
            "min_size": 0.0001,
            "max_leverage": 8,
            "preferred_leverage": 5,
            "category": "equity_index",
            "market_hours": "24h"
        },
        "SPCX-USD": {
            "tick_size": 0.1,
            "min_size": 0.0001,
            "max_leverage": 8,
            "preferred_leverage": 8,
            "category": "equity_index",
            "market_hours": "24h"
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
        # ── Bybit-venue symbols (execution/venue.py routes these to BybitClient) ──
        # NOT in config.assets — activation happens by appending to bybit_assets
        # once keys are live. Registered here so category/risk classification works
        # from day 1. Trade only on Bybit (no SoDEX perp exists for these).
        # Seed set selected 2026-07-30 against live Bybit turnover/OI:
        # HYPE $189M, UNI $60M, ADA $54M, ONDO $41M, ENA $33M, KAITO $26M,
        # TAO $18M, WIF $13M 24h turnover — all deep enough for clean signals.
        "HYPE-USD": {
            "tick_size": 0.001,
            "min_size": 0.1,
            "max_leverage": 5,
            "category": "cex_ecosystem",
            "market_hours": "24h"
        },
        "ADA-USD": {
            "tick_size": 0.0001,
            "min_size": 1,
            "max_leverage": 5,
            "category": "alt_l1",
            "market_hours": "24h"
        },
        "UNI-USD": {
            "tick_size": 0.001,
            "min_size": 0.1,
            "max_leverage": 5,
            "category": "defi_infra",
            "market_hours": "24h"
        },
        "ONDO-USD": {
            "tick_size": 0.0001,
            "min_size": 1,
            "max_leverage": 5,
            "category": "defi_infra",
            "market_hours": "24h"
        },
        "TAO-USD": {
            "tick_size": 0.01,
            "min_size": 0.01,
            "max_leverage": 5,
            "category": "alt_l1",
            "market_hours": "24h"
        },
        "ENA-USD": {
            "tick_size": 0.0001,
            "min_size": 1,
            "max_leverage": 5,
            "category": "defi_infra",
            "market_hours": "24h"
        },
        "KAITO-USD": {
            "tick_size": 0.0001,
            "min_size": 1,
            "max_leverage": 5,
            "category": "crypto",
            "market_hours": "24h"
        },
        "WIF-USD": {
            "tick_size": 0.00001,
            "min_size": 1,
            "max_leverage": 5,
            "category": "meme",
            "market_hours": "24h"
        },
        # Extended bench (added 2026-07-30) — ZEC $48M turnover/$69M OI leads.
        "ZEC-USD": {
            "tick_size": 0.01,
            "min_size": 0.01,
            "max_leverage": 5,
            "category": "crypto",
            "market_hours": "24h"
        },
        "VIRTUAL-USD": {
            "tick_size": 0.0001,
            "min_size": 1,
            "max_leverage": 5,
            "category": "crypto",
            "market_hours": "24h"
        },
        "AAVE-USD": {
            "tick_size": 0.01,
            "min_size": 0.01,
            "max_leverage": 5,
            "category": "defi_infra",
            "market_hours": "24h"
        },
        "1000BONK-USD": {
            "tick_size": 0.000001,
            "min_size": 100,
            "max_leverage": 5,
            "category": "meme",
            "market_hours": "24h"
        },
        "SEI-USD": {
            "tick_size": 0.0001,
            "min_size": 1,
            "max_leverage": 5,
            "category": "alt_l1",
            "market_hours": "24h"
        },
        "PENGU-USD": {
            "tick_size": 0.000001,
            "min_size": 10,
            "max_leverage": 5,
            "category": "meme",
            "market_hours": "24h"
        },
        # Operator list 2026-07-30 (TRIA/SPACE rejected: <$1M turnover).
        "INJ-USD": {
            "tick_size": 0.001,
            "min_size": 0.1,
            "max_leverage": 5,
            "category": "alt_l1",
            "market_hours": "24h"
        },
        "TIA-USD": {
            "tick_size": 0.0001,
            "min_size": 0.1,
            "max_leverage": 5,
            "category": "alt_l1",
            "market_hours": "24h"
        },
        "APT-USD": {
            "tick_size": 0.0001,
            "min_size": 0.1,
            "max_leverage": 5,
            "category": "alt_l1",
            "market_hours": "24h"
        },
        # ── Aster-expansion incubation universe (2026-08-15) ──────────────
        # Registered so category/risk classification works during incubation.
        # Authoritative specs come from Aster exchangeInfo at boot (spec
        # sync); these mirror the Bybit-entry pattern for signal-side math.
        "TRX-USD": {
            "tick_size": 0.0001,
            "min_size": 1,
            "max_leverage": 5,
            "category": "large_cap",
            "market_hours": "24h"
        },
        "BCH-USD": {
            "tick_size": 0.01,
            "min_size": 0.01,
            "max_leverage": 5,
            "category": "large_cap",
            "market_hours": "24h"
        },
        "XLM-USD": {
            "tick_size": 0.0001,
            "min_size": 1,
            "max_leverage": 5,
            "category": "large_cap",
            "market_hours": "24h"
        },
        "FARTCOIN-USD": {
            "tick_size": 0.0001,
            "min_size": 1,
            "max_leverage": 5,
            "category": "meme",
            "market_hours": "24h"
        },
        "VELVET-USD": {
            "tick_size": 0.0001,
            "min_size": 1,
            "max_leverage": 5,
            "category": "defi_infra",
            "market_hours": "24h"
        },
        "AKE-USD": {
            "tick_size": 0.0001,
            "min_size": 1,
            "max_leverage": 5,
            "category": "meme",
            "market_hours": "24h"
        },
        "CYS-USD": {
            "tick_size": 0.0001,
            "min_size": 1,
            "max_leverage": 5,
            "category": "defi_infra",
            "market_hours": "24h"
        },
        "ASTER-USD": {
            "tick_size": 0.0001,
            "min_size": 1,
            "max_leverage": 5,
            "category": "cex_ecosystem",
            "market_hours": "24h"
        },
        "ACE-USD": {
            "tick_size": 0.0001,
            "min_size": 1,
            "max_leverage": 5,
            "category": "meme",
            "market_hours": "24h"
        },
        "MUBARAK-USD": {
            "tick_size": 0.00001,
            "min_size": 10,
            "max_leverage": 5,
            "category": "meme",
            "market_hours": "24h"
        },
        "DOS-USD": {
            "tick_size": 0.0001,
            "min_size": 1,
            "max_leverage": 5,
            "category": "meme",
            "market_hours": "24h"
        },
        "SNXX-USD": {
            "tick_size": 0.0001,
            "min_size": 1,
            "max_leverage": 5,
            "category": "meme",
            "market_hours": "24h"
        },
        "HEMI-USD": {
            "tick_size": 0.0001,
            "min_size": 1,
            "max_leverage": 5,
            "category": "defi",
            "market_hours": "24h"
        },
        "AIO-USD": {
            "tick_size": 0.0001,
            "min_size": 1,
            "max_leverage": 5,
            "category": "meme",
            "market_hours": "24h"
        },
        "ARIA-USD": {
            "tick_size": 0.0001,
            "min_size": 1,
            "max_leverage": 5,
            "category": "meme",
            "market_hours": "24h"
        },
        # ── 2026-08-21 expansion (Aster + Bybit dual-verified) ──────────────
        "WLD-USD": {
            "tick_size": 0.0001,
            "min_size": 1,
            "max_leverage": 5,
            "category": "alt_l1",
            "market_hours": "24h"
        },
        "BOME-USD": {
            "tick_size": 0.000001,
            "min_size": 1,
            "max_leverage": 5,
            "category": "meme",
            "market_hours": "24h"
        },
        "ICP-USD": {
            "tick_size": 0.001,
            "min_size": 1,
            "max_leverage": 5,
            "category": "alt_l1",
            "market_hours": "24h"
        },
        "XMR-USD": {
            "tick_size": 0.01,
            "min_size": 0.01,
            "max_leverage": 5,
            "category": "alt_l1",
            "market_hours": "24h"
        },
        "ORDI-USD": {
            "tick_size": 0.001,
            "min_size": 1,
            "max_leverage": 5,
            "category": "meme",
            "market_hours": "24h"
        },
        "WLFI-USD": {
            "tick_size": 0.0001,
            "min_size": 1,
            "max_leverage": 5,
            "category": "defi_infra",
            "market_hours": "24h"
        },
        "LIT-USD": {
            "tick_size": 0.0001,
            "min_size": 1,
            "max_leverage": 5,
            "category": "cex_ecosystem",
            "market_hours": "24h"
        },
        "PAXG-USD": {
            "tick_size": 0.1,
            "min_size": 0.001,
            "max_leverage": 5,
            "category": "commodity",
            "market_hours": "24h"
        },
    }

    # ── Bybit venue (execution/bybit_client.py + execution/venue.py) ──────────
    # Symbol-partition routing: bybit_assets trade on Bybit, everything else on
    # SoDEX. Defaults are INERT — enabled=False and empty bybit_assets mean the
    # dispatch resolves every call to the SoDEX client (zero behavior change).
    # Keys go in .env (secrets), never here. MAINNET ONLY — no testnet path.
    bybit_enabled: bool = False
    bybit_api_key: str = ""
    bybit_api_secret: str = ""
    # Endpoint switch — .env flips this one flag (BYBIT_TESTNET=true/false).
    # Keys must match the environment: testnet keys are created at
    # testnet.bybit.com, mainnet keys at bybit.com — they are NOT interchangeable.
    bybit_testnet: bool = False
    # Symbols routed to Bybit. Activation list — venue.py assigns these to the
    # BybitClient at boot; everything else stays on SoDEX. Env overrides are
    # NOT honored for universe config (issue #17) — code-only like config.assets.
    bybit_assets: list[str] = [
        "HYPE-USD", "ADA-USD", "UNI-USD", "ONDO-USD", "TAO-USD", "ENA-USD",
        "KAITO-USD", "WIF-USD", "ZEC-USD", "VIRTUAL-USD", "AAVE-USD",
        "1000BONK-USD", "SEI-USD", "PENGU-USD", "INJ-USD", "TIA-USD", "APT-USD",
    ]
    # Live-day-1 sizing: pct-of-venue-equity so the chain works at $50 and
    # scales linearly as balance grows. margin = equity * bybit_margin_pct,
    # notional = margin * leverage (5x default; hard clamp 10x).
    # $100 equity → $10 margin → $50 notional per trade; 5 slots → ≤50%
    # margin utilization (operator-capped venue, withdrawals disabled).
    # Swing + scalp both supported: native position-level stops and GTC
    # reduce-only TPs persist across restarts (swing), taker entries and
    # software-stop/time-stop paths dispatch by symbol (scalp).
    bybit_margin_pct: float = 0.10
    bybit_leverage: int = 5
    bybit_max_leverage: int = 10
    bybit_max_positions: int = 5             # concurrent Bybit position cap
    # Chancellor venue partition: sleeve halts ITSELF at 30% sleeve drawdown
    # (≈5.6% of combined equity at $100/$533) so a Bybit bleed can never
    # reach the 8% kingdom veto. SoDEX operation unaffected. Session-scoped
    # (restart resets the baseline; top-ups lift equity back above the halt).
    bybit_sleeve_halt_dd_pct: float = 0.30
    # Bybit V5 linear taker/maker fee rates (fraction, not bps) — used for cost
    # accounting; SoDEX rates stay untouched.
    bybit_taker_fee: float = 0.00055
    bybit_maker_fee: float = 0.0002

    # ── Aster venue (execution/aster_client.py + data/aster_feed.py) ─────────
    # Second execution venue (Binance-protocol). Hooks SoDEX lacks: $1 min
    # notional (kills issue #14 dust class), maker fee 0% on ALL contracts,
    # native STOP_MARKET/TP_MARKET/TRAILING_STOP_MARKET on MARK_PRICE (issue
    # #10), hedge mode (dual positionSide), auto-cancel-all dead-man switch,
    # ADL quantile endpoint (issue #8). Defaults INERT: enabled=False + empty
    # aster_assets → every dispatch resolves exactly as before. Keys in .env
    # (ASTER_API_KEY / ASTER_API_SECRET), never here. MAINNET ONLY.
    aster_enabled: bool = False
    aster_api_key: str = ""
    aster_api_secret: str = ""
    # SoSoValue market-data API (2026-08-29) — institutional flow gauge.
    # Key from env (SOSOVALUE_API_KEY), demo plan 10k calls/mo 10rpm — the
    # feed spends ~6/day by design (ETF flows are daily-cadence data).
    sosovalue_api_key: str = ""
    sosovalue_enabled: bool = True
    sosovalue_symbols: list = ["BTC", "ETH", "SOL"]
    # Consumer kill switches (2026-08-29). False = pre-SoSoValue system
    # bit-for-bit. Defaults ON per operator directive (sharper offense);
    # every consumer is bounded and shadow-scored from birth.
    etf_flow_sizing_enabled: bool = True      # ±10% size tilt, majors only
    etf_aftermath_haircut_enabled: bool = True  # opposed-tide ×0.5 on cascades
    etf_tide_veto_enabled: bool = True  # opposed-tide entries blocked (journal: WR 27%)
    # Symbols routed to Aster (canonical form). Code-only like config.assets
    # (issue #17 — env universe overrides are not honored).
    # Two groups (2026-08-15):
    #   1. Migration — the 17 bybit_assets. Bybit execution is 401-dead (IP
    #      whitelist); Aster revives them. Symbols present in BOTH lists route
    #      to Aster when active (registration order wins); bybit_assets stays
    #      intact for instant revert. Boot spec-sync gates: anything Aster
    #      doesn't list is skipped with a warning and keeps its old routing.
    #   2. Expansion — 12 symbols dual-verified 2026-08-15 (Aster TRADING
    #      status + Aster 24h quoteVolume + Bybit perp for the signal-data
    #      path): TRX/BCH/XLM/FARTCOIN/VELVET/AKE/CYS/ASTER/ACE/MUBARAK/
    #      DOS/SNXX. They sit in config.assets from the incubation commit —
    #      the fetch_symbol_ids exemption keeps them in the universe with no
    #      SoDEX ID, so approved signals die at order_blocked_no_symbol_id
    #      and the shadow journal scores their counterfactual edge under
    #      gate "no_venue" BEFORE capital commits. Rejected with data: ETC
    #      ($457/day Aster), 1000SHIB (no Bybit perp), SPACE ($6K/day),
    #      COOKIE (not Aster-listed), MOODENG ($6.6K OI).
    # SoDEX-listed symbols (BTC/ETH/SOL/majors) are NOT here on purpose:
    # campaigns + SoDEX-native funding edge keep them home until router v2.
    aster_assets: list[str] = [
        "HYPE-USD", "ADA-USD", "UNI-USD", "ONDO-USD", "TAO-USD", "ENA-USD",
        "KAITO-USD", "WIF-USD", "ZEC-USD", "VIRTUAL-USD", "AAVE-USD",
        "1000BONK-USD", "SEI-USD", "PENGU-USD", "INJ-USD", "TIA-USD", "APT-USD",
        "TRX-USD", "BCH-USD", "XLM-USD", "FARTCOIN-USD",
        "VELVET-USD", "AKE-USD", "CYS-USD", "ASTER-USD",
        "ACE-USD", "MUBARAK-USD", "DOS-USD", "SNXX-USD",
        # 2026-08-16 operator directive: HEMI/AIO/ARIA added (Aster TRADING +
        # Bybit perp data path dual-verified). H-USD REJECTED — no Bybit
        # perp → no candle/OI data path, would starve the interpreter.
        "HEMI-USD", "AIO-USD", "ARIA-USD",
        # XAUT/CL migrated off SoDEX same directive (zero SoDEX fills ever;
        # Aster $1 min notional + 0.009% commodity taker + deeper book).
        # Candle path unchanged: tradfi_feed Yahoo GC=F/CL=F + Bybit XAUTUSDT.
        "XAUT-USD", "CL-USD",
        # TSM/ORCL same migration (operator, same directive) — Aster stock
        # perps trade near-24/7 with EWMA-smoothed marks off-hours.
        "TSM-USD", "ORCL-USD",
        # 2026-08-21 operator directive: DOGE migrates SoDEX → Aster (dual-
        # verified same-day: Aster DOGEUSDT TRADING, $34.5M/24h, 60k trades;
        # Bybit perp $341M turnover for the candle/signal path). Aster's book
        # is ~100x deeper than SoDEX's ($11.9K 24h). Evaluated and REJECTED
        # with data: PUMP (no Bybit perp — signal path blind), NEIRO (no
        # Bybit perp + $303K/day Aster), ATOM (Aster $83K/day, 243 trades —
        # dead book). HYPE/ASTER already routed here (migration/expansion).
        "DOGE-USD",
        # 2026-08-21 operator directive (toward 70, tempered by cluster
        # families — landed at 45): 7 SoDEX→Aster migrations where the Aster
        # book is mechanically better (0% maker, $1 min notional, native
        # trailing, deeper book) and the Bybit candle path already exists:
        # XRP $95.1M / 1000PEPE $4.0M / SUI $1.4M / AVAX $1.35M / LINK $1.0M /
        # LTC $0.91M / NEAR $0.67M (Aster 24h, verified same-day).
        "XRP-USD", "1000PEPE-USD", "SUI-USD", "AVAX-USD", "LINK-USD",
        "LTC-USD", "NEAR-USD",
        # 8 new symbols (in config.assets same commit): family-diverse, all
        # Aster vol ≥$390K/24h + Bybit perp path. See config.assets comments.
        "WLD-USD", "BOME-USD", "ICP-USD", "XMR-USD", "ORDI-USD",
        "WLFI-USD", "LIT-USD", "PAXG-USD",
    ]
    # Shadow-dual (2026-08-16): SoDEX keeps LIVE routing for these — this list
    # is NEVER passed to venue.assign_symbols. It only (a) unions into the
    # Aster WS feed symbols + spec sync so mark/book data flows, and (b) arms
    # fill-time venue snapshots (shadow_journal.record_venue_snapshot) and the
    # router v2 shadow scorer. Zero margin, zero routing change.
    aster_shadow_assets: list[str] = [
        "BTC-USD", "ETH-USD", "SOL-USD",
    ]
    # Aster-owned candles (2026-08-18): aster-routed TradFi symbols whose old
    # candle source (Yahoo GC=F/CL=F 1m) lags ~10 min overnight → the 90s
    # interpreter staleness guard vetoed every signal (23.7k signal_stale_data).
    # These symbols get kline_1m from the execution venue itself (AsterFeed
    # writes candle_buffers + CANDLE_CLOSED; tradfi_feed yields their candles).
    aster_kline_assets: list[str] = [
        "XAUT-USD", "CL-USD",
    ]
    # SoDEX-owned candles (2026-08-24): same Yahoo-futures ~10-min lag defect
    # as XAUT/CL, but SILVER/COPPER have no Aster listing — the execution
    # venue's own klines are the only timely source (verified fresh, ~1-3 min
    # bar closes with real volume). SoDEX kline_1m owns candle_buffers +
    # CANDLE_CLOSED; tradfi_feed keeps polling Yahoo for the basis-divergence
    # guard but never writes their candles. The list IS the kill switch.
    sodex_kline_assets: list[str] = [
        "SILVER-USD", "COPPER-USD",
    ]
    # Sizing mirrors the Bybit sleeve: margin = venue equity * aster_margin_pct,
    # notional = margin * leverage. Works at $50, scales linearly.
    # Operator directive 2026-08-20: 0.10 → 0.40 — Aster executions were the
    # day's only clean winners; size them up.
    # Operator directive 2026-08-21: 0.40 → 0.25 — ENA drawdown (-$5 on a
    # $203 sleeve) showed 40% per trade swings the sleeve too hard.
    # Operator directive 2026-08-24: 0.25 → 0.50 — 3× capital step-up with
    # base_trade_usd 200→600; ~15% book margin per trade, 3-loss streak ≈ −9%.
    # Operator directive 2026-08-26: 0.50 → 0.80 — with risk-parity sizing live
    # the stop distance (not the margin budget) is the risk governor; the
    # margin budget is a ceiling, and 80% of the sleeve lets high-conviction
    # tight-stop trades reach their risk-parity size (~$45 margin ≈ 0.7% sleeve
    # risk at a 1% stop on a $336 sleeve).
    aster_margin_pct: float = 0.80
    # Operator directive (2026-08-16): commodities/equities on Aster carry
    # HIGHER margin — their moves are slower and cleaner than alt-crypto.
    # 2026-08-20: raised 0.20 → 0.40 with the base so the tradfi tier never
    # sizes below crypto (the "tradfi ≥ base" ordering is deliberate).
    # 2026-08-24: 0.40 → 0.50 — ordering preserved at the new base.
    # 2026-08-26: 0.50 → 0.80 with the base — ordering preserved.
    aster_tradfi_margin_pct: float = 0.80
    # Fix B (2026-08-21): standard-path build_candidate sizes aster-routed
    # symbols off the sleeve's own equity (base = cap/2, cap = pct × equity
    # × lev, min = $1 exchange floor) instead of the SoDEX $200/$500/$80
    # chain. False restores the legacy SoDEX chain on Aster.
    aster_standard_path_fixed_fraction: bool = True
    # Operator directive 2026-08-29 ("9usd is not efficient margin use"):
    # 1.0-conviction base = cap × this fraction (was hardcoded cap/2). 0.75
    # lifts a standard aster trade +50% (HYPE-class fill $62.5 → ~$94
    # notional); 2.0 conviction still hits the cap, never exceeds (Vince).
    # 0.5 reproduces the legacy ladder bit-for-bit.
    aster_conviction_base_frac: float = 0.75
    aster_max_leverage: int = 8   # 2026-09-04 operator: 10->8, more margin / less early stop-out
    aster_max_positions: int = 5
    # Chancellor venue partition — same invariant as Bybit: sleeve self-halts
    # at 30% sleeve drawdown so an Aster bleed never reaches the 8% kingdom veto.
    aster_sleeve_halt_dd_pct: float = 0.30
    # Dead-man switch: refresh countdown every N seconds (0 = off). When on,
    # a dead ARIA process means Aster auto-cancels all open orders — SoDEX has
    # no equivalent (its stale-order purges were manual, see 07-26).
    aster_deadman_seconds: int = 0
    # Fee schedule from docs (fraction): maker 0% everywhere; taker 0.04%
    # USDT-margined crypto, 0.009% stock perps, 0.005% USD1-margined.
    # 5% further discount paying fees in $ASTER (not wired — needs token ops).
    aster_taker_fee: float = 0.0004
    aster_maker_fee: float = 0.0

    # Explosive breakout path (2026-08-16): Dreamer's ExplosiveScanner fires
    # live on aster-routed symbols when score >= explosive_min_score (of 4
    # precursors). Entry MARKET, native STOP_MARKET at trigger-candle low
    # (capped at -explosive_max_stop_pct — wick deeper than that is ignored),
    # TRAILING_STOP_MARKET (callback %, activates at +activation %) — the
    # trailing stop is the weapon SoDEX lacks for vertical alt moves.
    # Long-only live (shorts shadow-scored by the journal for calibration).
    # All guards fail-closed. Operator-set caps (2026-08-16): max 3 at a time,
    # up to 10/day — high enough that bugs surface early, capped enough that
    # a mistake is catchable.
    explosive_enabled: bool = True
    explosive_min_score: float = 3.0
    explosive_max_concurrent: int = 3
    explosive_daily_cap: int = 10
    explosive_trail_callback_pct: float = 5.0   # Aster venue max (10 rejected live 2026-08-16)
    explosive_trail_activation_pct: float = 15.0
    explosive_max_stop_pct: float = 5.0
    explosive_time_stop_hours: float = 4.0

    # ── Aster swing class + pyramid (2026-08-20, operator directive) ─────────
    # The pyramid carrier on Aster. Aftermath entries whose direction is
    # trend-day ALIGNED (the guard's verdict, not merely "not counter") tag
    # trade_type="aster_swing": no loser time-stop (breakout semantics — the
    # native trailing loop owns the exit), pyramid-eligible after TP1.
    # Anti-martingale only: adds require a banked TP1, are sized off the BASE
    # (never equity), one add ever, floor at combined VWAP breakeven - buffer.
    # aster_swing_enabled=False reproduces the pre-swing system exactly.
    aster_swing_enabled: bool = True
    aster_swing_pyramid_frac: float = 0.40     # add = frac x base size (coh-taper is majors-only)
    aster_swing_pyramid_window_s: float = 1800.0  # add within 30min of TP1 (alts move faster than majors' 15min window allows for)
    aster_swing_max_day_move_pct: float = 8.0   # no NEW swing entries into an exhausted day move (FOMO guard)
    aster_swing_add_max_day_move_pct: float = 10.0  # adds tolerate a slightly more extended day than fresh entries
    aster_swing_l4_spread_cap_bps: float = 25.0   # no add into a gapping book

    # Graduation registry (2026-08-16) — AUTONOMOUS privilege grant/lapse.
    # Operator directive: "stay outside the loop — the machine evolves
    # cybernetically, makes its own mistakes, recalibrates itself."
    # A shadow subsystem that accumulates enough live-forward evidence holds
    # a TTL'd privilege key (param_store grad_<name>), re-earned every
    # evaluation while criteria hold, lapsed automatically on decay. No
    # permanent grants; consumers act on is_graduated() without asking.
    # Chancellor/Kant/Nietzsche stay absolute — graduation can only relax
    # THIS table's knobs, never the risk engines.
    graduation_enabled: bool = True
    graduation_min_samples: int = 30
    graduation_min_span_days: float = 7.0
    graduation_min_shrunk_wr: float = 0.5   # shrinkage k=20 toward 0.5
    graduation_ttl_hours: int = 72
    # Explosive privileges while graduated: lower entry bar + wider caps.
    explosive_graduated_min_score: float = 2.5
    explosive_graduated_max_concurrent: int = 4
    explosive_graduated_daily_cap: int = 15
    # Leverage privilege (operator directive 2026-08-16): graduated symbols
    # (rally graduation or a graduated venue subsystem) earn +bonus leverage
    # up to the ceiling. Earned, TTL'd, lapses with the key.
    explosive_graduated_leverage: int = 7
    graduation_leverage_bonus: int = 2
    graduation_leverage_ceiling: int = 10

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

    # Set to a non-zero value to apply a manual balance adjustment on startup.
    # Negative for withdrawals, positive for deposits. Resets to 0 after application.
    # pydantic-settings reads MANUAL_BALANCE_ADJUSTMENT env var automatically.
    manual_balance_adjustment: float = 0.0

    live_risk_pct: float = 0.03  # 3% risk per trade
    live_min_coherence: float = 3.5  # lowered for small-account signal flow
    default_leverage: int = 5   # 5x: margin=$40 per $200 trade, liq ~20% away.
    arb_capital_pct: float = 0.2  # 20% of balance for arb capital
    live_mode_confirmed: bool = Field(default=False, description="Must be True for live mode")

    # Mainnet Limits
    balance_floor: float = 50.0          # Minimum account balance to permit trading
    # Shadow journal — counterfactual scoring of gate refusals (Nine Questions
    # + lucky-gate persistence test). Structlog processor only; never touches
    # the trade path. Set SHADOW_JOURNAL_ENABLED=false to disable.
    shadow_journal_enabled: bool = True
    daily_loss_limit_pct: float = 0.05   # Gate 8: 5% daily loss circuit breaker
    max_daily_loss_pct: float = 0.05     # Alias for risk_engine gate lookup
    # Mark/entry scale-split guard (SPCX phantom 2026-08-22): close triggers
    # skip any position whose mark diverges from its own entry by more than
    # this fraction — a persistent rebase scale split is not a tick jump, so
    # the discontinuity quarantine cannot catch it.
    mark_entry_scale_guard_pct: float = 0.30
    max_deployed_pct: float = 0.40
    min_trade_notional_usd: float = 80.0   # SoDEX hard floor $10 notional. Strategy floor raised to $80
                                            # so post-multiplier trades stay executable (0.45x crush → $36).
                                            # minimum so drawdown-reduced sizes still execute. Execution layer
                                            # bumps dust up by 1 step if rounding lands just under $10.
    # Venue-aware dynamic floor (operator directive 2026-08-29: "that 80 usd
    # cap is a bug it should be dynamic and grow with account"). The $80
    # strategy floor is SoDEX-calibrated; applied to Aster (exchange min $1)
    # it rejected standard-path winners (UNI $69.06 → nietzsche_min_notional_
    # fail) while the aster ladder slipped sub-floor (HYPE $62.5 → $9 margin).
    # Floor = max(venue minimum, sleeve × dynamic_pct) — grows with the
    # account, never below the venue's own exchange floor.
    min_notional_dynamic_pct: float = 0.02
    aster_min_notional_usd: float = 3.0   # 3 bracket legs × $1 exchange min

    # Gate 1 — Portfolio VaR limit
    max_portfolio_var_pct: float = 0.40  # 40% — sized for leveraged crypto; updates dynamically with balance

    # Gate 2 — Symbol concentration cap
    max_symbol_concentration: float = 0.20  # 20% of balance per symbol

    # SoDEX mainnet thin-market thresholds (Gate B)
    # SoDEX books are thin — $100 depth / 50bps spread is CEX-calibrated and blocks all trades.
    # $25 depth = realistic for SoDEX; 150bps spread = 1.5% which is still tradeable at 10x.
    min_ob_depth_usd: float = 25.0     # Minimum USD depth within 0.5% of entry
    max_spread_bps: float = 150.0      # Maximum bid-ask spread in basis points (1.5%)

    # Confidence-based order-type override (Phase 1: 0.75 threshold, Phase 2: 0.60)
    # High-confidence signals prefer LIMIT/GTC (maker) to preserve edge.
    # Only applies when spread < 15 bps to avoid adverse selection in wide books.
    confidence_limit_threshold: float = 0.75
    confidence_limit_max_spread_bps: float = 15.0

    # Maker entries (GTX post-only at the touch). L4 "limit" verdicts are
    # converted to post-only orders with a short fill window + one taker
    # fallback (place_bracket). Kill-switch: set False to revert to GTC limits.
    maker_entries_enabled: bool = True

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
    # Operator directive 2026-08-24: 200→600 base, 250→750 ceiling — 3× capital
    # step-up (with aster_margin_pct 0.25→0.50). ~$102 typical / $150 max margin
    # per trade ≈ 13-20% of a $763 book; Chancellor 60% total ceiling unchanged.
    base_trade_usd: float = 600.0    # Base notional per trade
    min_trade_usd: float = 200.0     # Hard $200 minimum per trade — never build below this
    max_trade_usd: float = 750.0     # Hard ceiling notional; balance safety cap may reduce below this
    max_notional_usd: float = 750.0  # Alias for max_trade_usd — used in sizing formula

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
        "ARB-USD": 1.0, "OP-USD": 1.0, "NEAR-USD": 1.0,
        "1000PEPE-USD": 1.0,
        # Binary event / macro — lower threshold: move hasn't happened yet
        "CL-USD":     0.5,
        "COPPER-USD": 0.6,
        "TSM-USD":    0.7,
        "ORCL-USD":   0.7,
        # US equities — gate requires at least 0.3% 1m ATR (screens out dead oracle candles)
        "NVDA-USD":   0.3,
        "TSLA-USD":   0.3,
        "META-USD":   0.3,
        "AMZN-USD":   0.3,
        "MSFT-USD":   0.3,
        "AAPL-USD":   0.3,
        "GOOGL-USD":  0.3,
        "SPCX-USD":   0.3,
    }

    stop_atr_mult: float = 1.5           # Stop buffer: 1.5×ATR. Floor: max(1.5×ATR, 0.8% of price).
                                         # 0.5% floor was too tight — AVAX/LINK/SOL noise hits it in seconds.
                                         # 0.8% gives ~60% more breathing room; at 6x = 4.8% margin loss max.
    max_hold_minutes: int = 30           # Time stop: exit flat/losing trades after 30 min
    max_concurrent_positions: int = 7    # Global position cap across all symbols
    # Operator directive 2026-08-25: 3 → 7 — the alt_season clamp was the
    # binding cap ("active 3, cap 3" in replacement-eviction events); the book
    # never held more than 3. Now matches max_concurrent_positions.
    alt_season_max_positions: int = 7   # Cap during alt_season (was concentration clamp 3)
    max_margin_per_trade_pct: float = 0.20  # Cap single-trade margin at 20% of balance ($60 on $300)
    small_account_balance_threshold: float = 150.0  # Balance below this → small-account mode
    small_account_max_margin_pct: float = 0.30      # Raised margin cap for small accounts
    trail_activation_atr: float = 2.0   # Trail activates after 2.0×ATR favorable move
    trail_distance_atr: float = 1.0     # Trail distance: stop = best ± 1.0×ATR
    # 2026-08-18 Phase 2b: trend-day TP room (Livermore sitting organ). Digest
    # hold-asymmetry: winners cut at 6-38min, basket_harvest=0 all-time — the
    # 7% winner-escape valve clips trend-day runners. On trend day-types the
    # escape threshold is widened by this conservative multiplier; the
    # small-account basket TP caps are untouched.
    trend_day_tp_room_enabled: bool = True
    trend_day_winner_escape_mult: float = 1.5   # 7% → 10.5% on trend days
    # 2026-08-20 trend-day direction guard (operator directive, trend-day
    # autopsy): day_type=trend fired all through the 08-17→19 rally while
    # mean-reversion shorts entered into +8-20% moves — direction reached
    # exits, never entries. Locked trend + known direction → counter-trend
    # signals rejected (shadow gate "counter_trend" measures the refusals);
    # aligned signals get coherence relief. Fail-open on missing/conflicting
    # direction evidence; campaign + aftermath bypass.
    trend_day_direction_guard_enabled: bool = True
    trend_day_momentum_threshold_pct: float = 5.0   # |24h change| needed when ORB direction unknown
    # 2026-08-20 (7-book bundle, Link/Carver): third direction source — move
    # from today's 00:00 UTC open. Fresher than the 24h window (which still
    # carried overnight drift on 08-20: BTC +3.7% from midnight by 08:00 while
    # change_24h read <5% and breakout was ""). Lower threshold than the 24h
    # source: an intraday move is the stronger tell per percent.
    trend_day_move_threshold_pct: float = 3.0
    trend_day_aligned_coherence_boost: float = 0.5  # aligned-signal relief (graduation precedent)
    # 2026-09-01 (watchdog proposal coherence-floor-trend-day-conditional,
    # operator-shipped): the Kant coherence floor + c_tier gate earn their
    # 86% accuracy on RANGE days but amputate the trend-day right tail
    # (coherence_floor x trend n=244 +992.8% 7d missed; c_tier x trend n=114
    # +423.1%). Aligned candidates on a locked trend day get a bounded Kant
    # floor relief (never below 2.5) and a c_tier bypass. Recovery suppresses
    # (capital preservation outranks); counter/unknown fail closed.
    trend_day_coherence_relief_enabled: bool = True
    trend_day_coherence_relief: float = 0.5   # Kant floor 3.0 - relief, clamped >= 2.5
    trend_day_c_tier_bypass_enabled: bool = True
    # 2026-09-03 (operator directive, the missed-rally autopsy): the locked
    # 3%-from-midnight verdict is structurally late in the first half of a
    # rally leg — SOL rallied +3.4% in 90min while every trend instrument
    # read 'unknown', the base-rate veto blocked ~46 aligned majors longs
    # 12:00-16:00 UTC, and two cascade shorts fired into the turn. The
    # EMERGING-trend predicate (symbol participates >= sym threshold AND BTC
    # confirms >= btc threshold, same direction) is the leading read:
    # 'aligned' releases the base-rate veto (tide-accel precedent), 'opposed'
    # blocks counter-direction cascade entries + denies the elite override.
    # Crypto-only; tradfi abstains neutral (BTC is the wrong plane).
    emerging_trend_sym_move_pct: float = 1.0
    emerging_trend_btc_move_pct: float = 1.5
    base_rate_veto_emerging_trend_exempt_enabled: bool = True
    emerging_trend_cascade_veto_enabled: bool = True
    # 2026-09-04 (operator directive — bull-run capital utilization): aligned
    # candidates earn a bounded size boost (same x1.25 class as the whale
    # single-direct boost); recovery suppresses. And the peak-ROE stop
    # ratchet (intelligence/roe_ratchet.py): >=3% peak ROE moves the stop to
    # breakeven+buffer, then locks 45/60/70% of peak at 6/9/15% — mechanical
    # give-back control, tighten-only, treasury/Hugo-owned positions skipped.
    emerging_trend_size_boost_enabled: bool = True
    emerging_trend_size_boost: float = 1.25
    roe_ratchet_be_rung_pct: float = 3.0
    roe_ratchet_be_buffer_pct: float = 0.15
    # 2026-09-04 (watchdog cycle-25 P0): the cascade fast paths bypass the
    # interpreter, so the Gate -1 macro-print calendar block never bound them —
    # three momentum entries fired INTO the NFP print (-$5.53 in 77s). Prints
    # CAUSE liquidation cascades; the fast paths must stand down on BLOCK.
    cascade_calendar_block_enabled: bool = True
    # 2026-08-22 Trend Offensive ("Hugo", intelligence/trend_offensive.py):
    # confirmed trend day (N>=entry_n of 6 evidences aligned, day_move required)
    # flips doctrine for the aligned direction — size up, base-rate veto
    # downgraded to a size discount, fixed TP ladder + treasury harvest
    # suspended (the trail owns the exit), eviction immunity, conviction grace
    # x grace_mult, pyramid on strength. trend_offensive_enabled=False = the
    # brain never leaves "off" = pre-module system bit-for-bit.
    trend_offensive_enabled: bool = True
    trend_offensive_entry_n: int = 4          # aligned evidences to arm
    trend_offensive_exit_n: int = 3           # hysteresis floor while active
    trend_offensive_confirm_evals: int = 2    # consecutive qualifying evals to arm
    trend_offensive_decay_s: float = 900.0    # evidence below exit_n this long → off
    trend_offensive_size_boost: float = 2.0   # same doctrine as rally graduation (max, never stacked)
    trend_offensive_veto_discount: float = 0.35  # base-rate veto → this size mult
    trend_offensive_grace_mult: float = 4.0   # conviction-review aligned grace
    trend_offensive_pyramid_min_roe: float = 2.0  # aligned runner ROE floor for pre-TP1 adds
    trend_offensive_trail_dist_mult: float = 2.0  # LeBeau Chandelier: aligned runners trail wide
    # 2026-08-27 alt-breadth day_move tiebreak: the majors EW vote reads 0 on
    # an alt-led day (BTC +1.7% while 5+ alts run +10%) and Hugo stays silent
    # because day_move is a required vote. When the majors vote is 0 and >=
    # _min crypto alts have same-direction day moves >= _move_pct, day_move
    # votes that direction. Never overrides a majors vote; both directions
    # qualifying = abstain (divergent tape). False = pre-extension bit-for-bit.
    trend_offensive_alt_breadth_enabled: bool = True
    trend_offensive_alt_breadth_min: int = 5
    trend_offensive_alt_breadth_move_pct: float = 5.0
    # 2026-08-19 Treasury (the accounting department): single owner of profit
    # realization — venue-aware ledger, correlated-cluster harvests, runaway
    # trims, margin recycling. treasury_enabled=False reverts profit-taking to
    # individual software TPs (treasury never activates, nothing suppressed).
    treasury_enabled: bool = True
    treasury_runaway_trim_ratio: float = 0.5   # bank half a runaway, rest runs
    treasury_recycle_enabled: bool = True
    treasury_recycle_margin_util: float = 0.75  # recycle only under margin pressure
    treasury_recycle_min_age_s: float = 2700.0  # 45min stale-flat threshold
    treasury_recycle_flat_roe_band: float = 1.5 # |ROE| <= band = dead capital
    # Fallback/Legacy Aliases (for Pydantic validation)
    risk_pct: float = 0.03              # 3% risk per trade
    min_coherence: float = 3.5  # Gate 5: lowered for small-account signal flow
    auto_adj_enabled: bool = False  # Enable auto-adjustment position closes (set True after validation)
    funding_carry_threshold: float = 1.5    # min |funding_rate|% to activate carry veto (Gap 4)
    regime_stability_window_s: float = 180.0  # seconds in transitioning before suppression (Gap 6)
    alpha_floor_min_trades: int = 10        # minimum trades before alpha floor applies (Gap 3)
    aftermath_session_bypass_min_coherence: float = 5.0  # min coherence for aftermath to bypass session exclusion
    oracle_enabled: bool = True             # ORACLE pre-cascade smart money cluster detector
    oracle_min_subs: int = 3               # sub-signals required to fire oracle cluster signal
    oracle_coherence_boost_strong: float = 1.5   # boost when 4/4 subs align
    oracle_coherence_boost_moderate: float = 0.8  # boost when 3/4 subs align
    sovereign_capital_pct: float = 0.20  # fraction of perp balance allocated to Sovereign perp trades per session
    sovereign_enabled: bool = False      # DISABLED Jul-16: unvalidated 5-min divergence signal caused catastrophic equity losses

    # ── The Chancellor — final capital governance (risk/chancellor.py) ────────
    # Absolute last gate AFTER all sizing, BEFORE every order submission.
    # Nothing overrides a Chancellor veto — not campaign, rally, cascade, or APEX.
    # Drawdown here is PERCENT scale (8.0 = 8%) per Hard Rule 7.
    chancellor_emergency_halt_balance: float = 150.0  # balance below → VETO all trading
    chancellor_veto_drawdown_pct: float = 8.0         # session DD (percent) → VETO
    chancellor_max_daily_loss_pct: float = 0.05       # realized daily loss fraction → VETO
    chancellor_max_symbol_exposure_pct: float = 0.15  # margin per symbol / balance → clamp
    chancellor_max_kingdom_exposure_pct: float = 0.60 # total margin / balance → clamp
    chancellor_min_margin_usd: float = 2.0            # post-clamp floor → VETO if below

    # ── Per-symbol daily trade cap — prevents churn (ETH 35 trades in 5 days)
    max_trades_per_symbol_per_day: int = 4

    # ── Capacity governor (2026-08-23, HYPE/MUBARAK autopsies) ─────────────
    # The cap is a churn guard; these legs exempt evidence-gated TREND
    # participation (intelligence/capacity_governor.py — book grounding in
    # module docstring). All legs suppressed in recovery; all bounded by the
    # per-symbol daily risk budget (Carver: constrain risk, not count).
    daily_cap_day_move_exempt_enabled:  bool = True   # symbol day-move >= trend_day_move_threshold_pct in signal direction
    daily_cap_journal_evidence_enabled: bool = True   # shadow-journal measured cap accuracy per symbol
    daily_cap_journal_evidence_min_n:   int = 10      # Aronson: no verdict on noise
    daily_cap_journal_evidence_max_accuracy: float = 0.35  # relax when the cap is mostly WRONG here
    daily_symbol_risk_budget_pct: float = 1.0         # per-symbol daily stop-risk budget, % of combined balance
    rally_max_graduated_per_direction: int = 2        # HYPE: single slot was contended (slot_taken x141)
    # Mover radar: public-24h-move vs participation, feed-independent
    # (MUBARAK class = silent pipe, HYPE class = blocked pipe — one detector).
    mover_radar_enabled:       bool = True
    mover_radar_threshold_pct: float = 10.0           # |24h move| that names a big mover
    mover_radar_poll_s:        int = 300
    mover_relief_ttl_s:        int = 3600             # blocked-class relief param TTL

    # ── Whale mirror (Deploy 5, 2026-08-29) — fresh-flow detection, LIVE from
    # day one (operator directive): SIZE is the differentiator — the mirror
    # never creates or vetoes an entry, it boosts size on gated candidates
    # when fresh whale flow agrees. Gate deliberately NOT overfit: two fixed
    # boost steps, accuracy review at n≥10 (not 30) slicing boosted vs not.
    # Watched addresses. aster: leaderboard pnl-delta inference (campaign-
    # scoped — dark while the pro campaign is off; detected + abstained,
    # never traded). sodex: unsigned positions snapshots, direct diffs.
    whale_mirror_enabled:        bool = True          # master: polling loop
    whale_mirror_live_enabled:   bool = True          # the size boost alone
    whale_registry: List[Dict[str, str]] = [
        {"address": "0xb79C80a503bf3c62F90A06593fBD7cCefEAb5c8C",
         "venue": "aster", "label": "aster_pro_30"},
        {"address": "0xE1d71a56367736Caa42E3740f1C8a553458dDefd",
         "venue": "aster", "label": "aster_770k_btc_eth_50x"},
        {"address": "0xb79C809AaE7FE060a772C2d4D5a6303cC74D95E6",
         "venue": "aster", "label": "aster_86k_btc75x"},
        {"address": "0x4ea29DE91ac9fbDA5A52EaE81fbA1cbD246124dD",
         "venue": "aster", "label": "aster_146k_eth50x_fresh"},
        {"address": "0xc8F703e16515Dc5F626714ee8A1330DF12aCa38a",
         "venue": "aster", "label": "aster_118k_ena15x"},
        {"address": "0xefe1272b0A0B25f3e7Baa4B04e04b2E28E38a8fF",
         "venue": "sodex", "label": "sodex_whale_1"},
    ]
    whale_aster_poll_s:          int = 300            # leaderboard cadence
    whale_sodex_poll_s:          int = 60             # positions cadence
    whale_aster_symbols: List[str] = [  # leaderboard poll set (operator: DOGE/
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "NEARUSDT", "DOGEUSDT", "TRUMPUSDT",
        "XRPUSDT", "SUIUSDT", "AVAXUSDT", "LINKUSDT", "WLDUSDT", "XMRUSDT",
        "TAOUSDT", "ENAUSDT", "AAVEUSDT", "ZECUSDT", "VIRTUALUSDT",
        "1000PEPEUSDT", "AKEUSDT"]      # TRUMP + alts with potential)
    whale_flow_min_pnl_delta_usd: float = 50.0        # noise floor for Δpnl legs
    whale_flow_min_price_move_pct: float = 0.05       # min |Δprice| for direction inference
    whale_consensus_window_s:    int = 1800           # ≥2 whales same sym+dir inside this
    whale_mirror_single_boost:    float = 1.25        # one DIRECT-leg whale agrees
    whale_mirror_consensus_boost: float = 1.5         # ≥2 independent whales agree
    # Tide-Aligned Consensus (2026-08-30 spec audit): bounded placeholder
    # ladder 1.00/1.05/1.15/1.25 over EFFECTIVE breadth (40% leviathan cap,
    # venue-cluster sqrt(n) deflation — correlated whales = one risk factor).
    # ETF tide amplifies strong consensus or abstains the boost when opposed
    # (never vetoes). tide_consensus_enabled=false = legacy fixed ladder
    # bit-for-bit. Rung economics learned from shadow before any rung moves.
    tide_consensus_enabled:        bool = True
    tac_strong_breadth_floor:      float = 2.0        # eff. breadth for "strong"
    # Whale Position Plane (2026-08-30): address-scoped position resolution
    # — Aster RPC aster_getBalance (tapi.asterdex.com) + Hyperliquid
    # clearinghouseState. Delta engine emits WhaleMirror-contract flows;
    # upgrades the dark Aster inferred leg to DIRECT. Data accrual default.
    whale_positions_enabled:       bool = True
    whale_positions_poll_s:        int = 60
    whale_min_notional_delta_usd:  float = 10_000.0   # WPP emission floor
    # Whale Absorption Signal (2026-08-30): SHADOW-ONLY — true/false
    # absorption discrimination (forced liq window × whale identity × L4
    # refill × stabilization). Emits shadow gate "whale_absorption"; ZERO
    # live orders until graduation (n≥50, EV>+0.15R, CI>0, PF>1.15, OOS).
    whale_absorption_enabled:      bool = True        # shadow accrual
    whale_absorption_min_forced_usd: float = 250_000.0
    whale_absorption_symbols: List[str] = []          # [] = probe symbols
    # Exit side (O'Hara PIN): a DIRECT-leg whale closing the side we hold ends
    # the mirrored thesis → greedy partial harvest while green (Freeman-Shor).
    whale_reversal_harvest_enabled: bool = True
    whale_reversal_harvest_min_roe_pct: float = 1.5   # harvest only while green
    whale_reversal_harvest_fraction: float = 0.5      # bank half, keep the runner
    # Conviction support: fresh DIRECT-leg whale agreement = an informed
    # same-direction signal (thesis ALIVE) for the conviction review — stops
    # the 30-min-clock abandons from churning whale-confirmed names.
    whale_conviction_support_enabled: bool = True
    # 50x consensus probe (Thorp/Vince: leverage ≠ risk — risk = notional ×
    # stop). Margin is equity-SCALED (5% floor $15 cap $50) so the class grows
    # with the book; on a $600 book = $30 margin, $1,500 notional, stop risk
    # ≈ $9+fees ≈ 1.6% — material enough that a runner matters (operator:
    # "$4.50 is too small"). n≥2 consensus only, Aster-routed only.
    whale_probe_enabled:           bool = True
    whale_probe_margin_pct:        float = 0.10       # of aster sleeve equity
    whale_probe_margin_floor_usd:  float = 15.0
    whale_probe_margin_cap_usd:    float = 50.0
    whale_probe_leverage:          float = 50.0
    whale_probe_stop_pct:          float = 0.6
    whale_probe_tp1_pct:           float = 0.8        # R 1.33 vs the stop
    whale_probe_tp2_pct:           float = 1.2
    whale_probe_time_stop_s:       int = 900          # Hasbrouck: ignition decays in minutes
    whale_probe_daily_cap:         int = 3
    whale_probe_max_concurrent:    int = 1
    whale_probe_symbols: List[str] = ["BTC-USD", "ETH-USD", "SOL-USD"]
    # Runner conversion (the 110% mechanism — operator 2026-08-29): at TP2, if
    # the whale consensus is still ALIVE (n≥2, no direct-leg exit), bank the
    # majority and convert the rest to a trailing runner with NO time-stop —
    # the whales hold for weeks at 20-75x; the runner exits on the trail or
    # on a direct-leg whale exit (O'Hara: thesis over).
    whale_probe_runner_enabled:    bool = True
    whale_probe_runner_bank_fraction: float = 0.5     # bank half at TP2, run the rest
    whale_probe_runner_trail_callback_pct: float = 2.5

    # ── SoDEX Campaign Mode ────────────────────────────────────────────────────
    # Activated for exchange trading tournaments / volume campaigns.
    # Prioritizes campaign_symbol with relaxed gates + larger size for volume
    # generation while keeping non-campaign assets on normal rules.
    #
    # CRITICAL: trades held < 1 minute are EXCLUDED from eligible volume.
    # Campaign tuning ensures minimum 2-minute holds + wider stops so
    # exchange bracket orders don't fire on noise in the first 60s.
    # Points = eligible_volume × SOSO_boost.  Maximize both.
    # 2026-09-02 (operator directive): campaign OFF — the relaxed-gate volume
    # path was the day's dominant bleed (SPCX heartbeat fills 23:29/07:07/13:29,
    # -$6.4 combined; conviction_decay cohort). All entries now flow the
    # standard path under Kant/Nietzsche/Chancellor. Re-enable is a one-line flip.
    campaign_mode_enabled: bool = False
    campaign_symbol: str = "SPCX-USD"
    campaign_coherence_floor: float = 1.5       # was 2.5 — SPCX sparse candle data rarely hits 2.5;
                                                  # 1.5 lets any real directional signal through
    campaign_size_boost: float = 2.5             # 2.5× notional ($500/trade)
    campaign_leverage: int = 10                  # max allowed for SPCX
    campaign_signal_throttle_s: float = 30.0     # was 90s — match heartbeat interval for max throughput
    campaign_off_hours_allowed: bool = True      # bypass US-hours gate for volume
    campaign_tp_tighten: float = 1.0             # NO tighten — normal TPs
    campaign_max_hold_min: int = 10              # was 30m — faster turnover = more volume = more points
    campaign_min_hold_min: int = 2               # 2m minimum — volume eligibility
    campaign_stop_widen: float = 1.5             # 1.5× normal stop — survive noise
    campaign_min_notional_usd: float = 250.0     # floor aligned with actual sizing
                                                   # ($260-300 post-multiplier on $435 balance)
    # 2026-08-18 Phase 2a: conviction-proportional floor. The flat $250 floor
    # inverted sizing — SPCX coh 3.5 floored to $250 while ETH coh 9.69 was
    # crushed to $40 mid-chain. Scale the floor by the same coherence bands
    # that drive conv_mult (≥4.5 → 1.0×, ≥3.0 → 0.75×, else 0.5×) so a 3.5
    # never out-sizes a 9.7 on the same account state. False restores the
    # legacy flat floor.
    campaign_conviction_floor_enabled: bool = True
    # 2026-08-18 churn choke: heartbeat re-entered both directions within
    # seconds of every stop (70 trades/3d, 26% WR, -$2.23) — the directional
    # Livermore block is evaded by ping-pong. Any losing close on the symbol
    # suppresses heartbeat entries for this long.
    campaign_loss_cooloff_s: float = 7200.0

    # ── Campaign Pyramid Engine (SpaceX tournament) ───────────────────────────
    campaign_pyramid_enabled: bool = True          # MFE-based anti-martingale layers
    campaign_pyramid_max_layers: int = 3           # base + 2 adds = 3 total
    campaign_pyramid_min_layer_gap_s: float = 180.0  # 3 min min between layers
    campaign_pyramid_volatility_cap: float = 1.5   # no pyramid if atr/baseline > 1.5
    campaign_pyramid_l1_stop_buffer: float = 0.006  # 0.6% L1 stop (wider than normal)
    campaign_pyramid_breakeven_buffer: float = 0.002  # 0.2% below breakeven for L2/L3

    # ── Execution Alpha Patch feature flags ───────────────────────────────────
    signal_tier_enabled:     bool = True   # SignalTier classification + C-tier skip + tier size mult
    trade_type_enabled:      bool = True   # TradeType tagging (drives time-stop and TP structure)
    dispersion_gate_enabled: bool = True   # DispersionGate: block alts in low-dispersion regimes
    regime_sizing_enabled:   bool = True   # Regime-aware size multiplier table
    streak_sizing_enabled:   bool = True   # Streak compounding: consecutive wins → 1.1x/1.2x/1.3x
    coherence_decay_enabled: bool = True   # CoherenceDecayMonitor: close/trim on signal evaporation
    # Conviction Review v2 (2026-08-22): thesis-tested, regime-conditional abandon.
    # False → v1 bit-for-bit (age>1800s AND ROE<-2% → close).
    conviction_review_v2_enabled:       bool = True
    conviction_decay_aligned_grace_mult: float = 4.0   # Lo: trend-aligned grace = 1800s × this
    conviction_atr_noise_mult:          float = 0.5    # Carver: bleeding = adverse ≥ max(0.4%, this×ATR15)
    conviction_inversion_enabled:       bool = True    # Raschke: counter-verdict + fresh opp signal = thesis dead
    conviction_winner_inversion_enabled: bool = True   # Frazzini mirror: green + inversion = bank the winner early
    conviction_mr_grace_mult:           float = 0.75   # Lo-MacKinlay: mean-reverting path → shorter grace
    volatility_estimators_enabled:      bool = True    # YZ noise band + VR path class in conviction review
    price_discovery_enabled:            bool = True    # Hasbrouck IS sampler on shadow-dual majors
    lppl_enabled:                       bool = True    # Sornette dragon-king boost in explosive readiness
    coherence_decay_trim_winner_enabled: bool = True   # Freeman-Shor: trim 50% of decaying winners (False = log-only)
    aster_book_anchor_enabled:          bool = True    # anchor aster entries to ≤250ms L4 mid, not the 1Hz mark
    aster_maker_first_enabled:          bool = True    # Aster entries attempt GTX at touch first (maker 0% vs taker 0.04%)
    aster_maker_timeout_s:              float = 8.0    # fill window before cancel + one taker retry
    asymmetric_tps_enabled:  bool = True   # Asymmetric TP engine (Phase 2 — replaces fixed TPs)
    dynamic_stops_enabled:   bool = True   # Dynamic ATR stops per trade-type (Phase 2)

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

    def effective_base_trade(
        self,
        balance: float,
        drawdown_pct: float = 0.0,
        win_streak: int = 0,
        loss_streak: int = 0,
    ) -> float:
        """Dynamic base trade notional scaled to account size and performance.

        Small accounts (<$150) get proportionally scaled base trades so capital
        actually deploys. Large accounts keep the fixed $200 base. Drawdown and
        streak convexity prevent runaway risk.
        """
        if balance <= 0:
            return self.base_trade_usd

        # Start with the configured base trade ($200)
        base = self.base_trade_usd

        # Small-account override: if fixed base exceeds what balance can support,
        # scale down to margin-based capacity so trades remain executable.
        if balance < self.small_account_balance_threshold:
            _margin_pct = self.small_account_max_margin_pct
            raw = balance * _margin_pct * self.default_leverage
            base = min(base, max(self.min_trade_notional_usd, raw))

        # Drawdown penalty: deeper hole = smaller trades
        if drawdown_pct < 0.10:
            dd_penalty = 1.0
        elif drawdown_pct < 0.20:
            dd_penalty = 0.70
        elif drawdown_pct < 0.25:
            dd_penalty = 0.50
        else:
            dd_penalty = 0.35

        # Streak boost: winning streaks earn larger size; losing streaks suppressed
        streak_boost = 1.0 + (win_streak * 0.10) - (loss_streak * 0.05)
        streak_boost = max(0.5, min(1.5, streak_boost))

        effective = base * dd_penalty * streak_boost
        return max(self.min_trade_notional_usd, min(effective, self.max_trade_usd))

    def effective_max_margin_pct(self, balance: float) -> float:
        """Return max margin percentage for a given balance tier."""
        if balance < self.small_account_balance_threshold:
            return self.small_account_max_margin_pct
        return self.max_margin_per_trade_pct

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )
