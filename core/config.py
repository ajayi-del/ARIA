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
    "HOOD-USD":      0.001,
    "LITE-USD":      0.001,
    "SKHX-USD":      0.001,
    "SAMSUNG-USD":   0.001,
    "SMCI-USD":      0.001,
    "UNITREE-USD":   0.001,
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
    "HOOD-USD":      3,
    "LITE-USD":      3,
    "SKHX-USD":      3,
    "SAMSUNG-USD":   3,
    "SMCI-USD":      3,
    "UNITREE-USD":   3,
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
    "HOOD-USD":    1.5,
    "LITE-USD":    1.5,
    "SMCI-USD":    1.5,
    "SAMSUNG-USD": 1.5,
    "SKHX-USD":    1.5,
    # Thinnest book of the 2026-09-11 adds (~$450 depth at 10bps) — wider stop
    # floor so a stop-exit sweep doesn't gift the whole visible book.
    "UNITREE-USD": 2.0,
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
        # ── 2026-09-11 equity-perp expansion (Governor "add a few more coins")
        # SoDEX L4-probed: spreads 0.2-2.5bps, depth@10bps $0.4-11.6K; maker-
        # first single-name doctrine binds (TRADFI_SINGLE_NAMES). 24h turnover
        # is micro ($2-9K/day) but books are MM-quoted — depth, not turnover,
        # is the liquidity that matters at $80-750 notional.
        "HOOD-USD",       # Robinhood — retail flow / crypto-equity proxy
        "LITE-USD",       # Lumentum — optical/AI infra; deepest book of the adds
        "SMCI-USD",       # Super Micro — AI server momentum
        "SAMSUNG-USD",    # Samsung (005930.KS) — memory cycle / HBM
        "SKHX-USD",       # SK Hynix (000660.KS) — HBM / AI memory leader
        "UNITREE-USD",    # Unitree — humanoid robotics; no Yahoo underlying
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
        # ── 2026-09-05 expansion (operator directive + CEO staged endorsement,
        # proposals.jsonl universe-expansion-2026-09). Driver: 0/8 of the day's
        # top gainers were in-universe — the Saturday-mover class was invisible.
        # Staged: FLOCK/FF carry Aster execution NOW (aster_assets — both clear
        # the ≥$390K/24h bar); TRIA/NOM/ZEN/ICX are data-plane incubation
        # (bybit_assets — signals + shadow scoring under gate "no_venue",
        # execution deferred to the requalification loop). Volumes verified
        # live 2026-09-05 (Aster 24h quoteVolume / Bybit turnover24h).
        "FLOCK-USD",      # AI agents — Aster $5.5M, Bybit $58.4M (execution)
        "FF-USD",         # Small-cap — Aster $1.7M, Bybit $10.1M, OI $13.3M (execution)
        "TRIA-USD",       # Small-cap — Aster $332K, Bybit $11.3M; operator +177% manual trade (incubation)
        "NOM-USD",        # Small-cap — Aster $343K, Bybit $16.1M (incubation)
        "ZEN-USD",        # Privacy L1 — Aster $318K, Bybit $12.3M (incubation)
        "ICX-USD",        # Alt L1 — no Aster listing; Bybit $10.2M, data-plane only (incubation)
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
        "HOOD-USD", "LITE-USD", "SMCI-USD",
        "SAMSUNG-USD", "SKHX-USD", "UNITREE-USD",
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
        "HOOD-USD", "LITE-USD", "SMCI-USD",
        "SAMSUNG-USD", "SKHX-USD", "UNITREE-USD",
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
        "HOOD-USD": {
            "tick_size": 0.01,
            "min_size": 0.001,
            "max_leverage": 7,
            "preferred_leverage": 7,
            "category": "equity",
            "market_hours": "24h"
        },
        "LITE-USD": {
            "tick_size": 0.01,
            "min_size": 0.001,
            "max_leverage": 7,
            "preferred_leverage": 7,
            "category": "equity",
            "market_hours": "24h"
        },
        "SMCI-USD": {
            "tick_size": 0.01,
            "min_size": 0.001,
            "max_leverage": 7,
            "preferred_leverage": 7,
            "category": "equity",
            "market_hours": "24h"
        },
        "SAMSUNG-USD": {
            "tick_size": 0.01,
            "min_size": 0.001,
            "max_leverage": 7,
            "preferred_leverage": 7,
            "category": "equity",
            "market_hours": "24h"
        },
        "SKHX-USD": {
            "tick_size": 0.01,
            "min_size": 0.001,
            "max_leverage": 7,
            "preferred_leverage": 7,
            "category": "equity",
            "market_hours": "24h"
        },
        "UNITREE-USD": {
            "tick_size": 0.01,
            "min_size": 0.001,
            "max_leverage": 5,
            "preferred_leverage": 5,
            "category": "equity",
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
        # 2026-09-05 Saturday-mover expansion (see config.assets comment)
        "FLOCK-USD": {
            "tick_size": 0.00001,
            "min_size": 10,
            "max_leverage": 5,
            "category": "defi_infra",
            "market_hours": "24h"
        },
        "FF-USD": {
            "tick_size": 0.0001,
            "min_size": 1,
            "max_leverage": 5,
            "category": "meme",
            "market_hours": "24h"
        },
        "TRIA-USD": {
            "tick_size": 0.0000001,
            "min_size": 100,
            "max_leverage": 5,
            "category": "meme",
            "market_hours": "24h"
        },
        "NOM-USD": {
            "tick_size": 0.000001,
            "min_size": 100,
            "max_leverage": 5,
            "category": "meme",
            "market_hours": "24h"
        },
        "ZEN-USD": {
            "tick_size": 0.001,
            "min_size": 0.1,
            "max_leverage": 5,
            "category": "alt_l1",
            "market_hours": "24h"
        },
        "ICX-USD": {
            "tick_size": 0.00001,
            "min_size": 10,
            "max_leverage": 5,
            "category": "alt_l1",
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
        # 2026-09-05 DATA-PLANE INCUBATION (operator + CEO staged endorsement):
        # NOT a Bybit-execution add — bybit_enabled=False, so these resolve to
        # SoDEX, hold no SoDEX ID, and their approved signals die at
        # order_blocked_no_symbol_id while the shadow journal scores them
        # under gate "no_venue". WARNING: flipping bybit_enabled=true would
        # route these to LIVE Bybit execution — re-vet before any such flip.
        # Graduation path: the universe-requalification loop promotes to
        # aster_assets on liquidity evidence (TRIA/NOM/ZEN are Aster TRADING
        # below the volume bar; ICX has no Aster listing).
        "TRIA-USD", "NOM-USD", "ZEN-USD", "ICX-USD",
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
    # ── Hedge mandate P1 (2026-09-19 Governor-approved hedge venue) ──────────
    # Hedge-mode order marking: when True, orders carry positionIdx 1 (Buy/long
    # position side) or 2 (Sell/short side) so the account can hold BOTH
    # directions on a symbol simultaneously — the precondition for the hedge
    # mandate. False = legacy one-way marking (positionIdx 0, byte-identical
    # payloads). The account itself must be in hedge mode — detect_position_mode
    # reads it via /v5/position/list and warns loud on mismatch; the client
    # never flips the account (mode flips are Governor-lane ops at a flat
    # book). The client remains INERT:
    # bybit_enabled stays False until the Governor arms the venue.
    bybit_hedge_mode: bool = False
    # Exchange-side trailing stop (POST /v5/position/trading-stop trailingStop,
    # ABSOLUTE price distance). Only meaningful when bybit_hedge_mode is on —
    # a hedge leg runs under a native trail that survives restarts while the
    # software stack manages the primary leg. Default True so arming the hedge
    # path is a one-flag flip; inert while bybit_enabled is False.
    bybit_native_trailing_enabled: bool = True
    # Hedge sub-account (2026-09-19 Governor directive): a dedicated Bybit
    # SUB-ACCOUNT carries the hedge book — isolated margin (a hedge
    # liquidation can never cascade), independent per-UID rate budget, and
    # the account balance IS the structural S_max backstop beneath the
    # brain's entry-gate S_max. Keys live in .env only (never git). Empty =
    # the hedge wrapper binds the legacy single client bit-for-bit.
    bybit_hedge_api_key: str = ""
    bybit_hedge_api_secret: str = ""
    hedge_account_low_margin_usd: float = 30.0   # alert floor; top-up is Governor-manual

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
    # 2026-09-16 Governor re-arm: sleeve re-funded (~$121, deposit 06:55Z)
    # after one day of the 09-15 $0-sleeve consolidation — full inventory
    # restored from git history (03c2692^). Data planes unchanged.
    aster_assets: list[str] = [
        "HYPE-USD", "ADA-USD", "UNI-USD", "ONDO-USD", "TAO-USD", "ENA-USD",
        "KAITO-USD", "WIF-USD", "ZEC-USD", "VIRTUAL-USD", "AAVE-USD",
        "1000BONK-USD", "SEI-USD", "PENGU-USD", "INJ-USD", "TIA-USD", "APT-USD",
        "TRX-USD", "BCH-USD", "XLM-USD", "FARTCOIN-USD",
        "VELVET-USD", "AKE-USD", "CYS-USD", "ASTER-USD",
        "ACE-USD", "MUBARAK-USD", "DOS-USD", "SNXX-USD",
        "HEMI-USD", "AIO-USD", "ARIA-USD",
        "XAUT-USD", "CL-USD",
        "TSM-USD", "ORCL-USD",
        "DOGE-USD",
        "XRP-USD", "1000PEPE-USD", "SUI-USD", "AVAX-USD", "LINK-USD",
        "LTC-USD", "NEAR-USD",
        "WLD-USD", "BOME-USD", "ICP-USD", "XMR-USD", "ORDI-USD",
        "WLFI-USD", "LIT-USD", "PAXG-USD",
        "FLOCK-USD", "FF-USD",
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
    # 2026-09-11 (Governor directive "migrate tradfi to sedex klines
    # immediately"): ALL remaining Yahoo-owned tradfi perps migrate. The
    # wound (overnight census): Yahoo dies at US close → signal_stale_data
    # all night (ORCL ×3,193/7d, candles 5h old) while the perps trade 24/7 —
    # months of tradfi silence was a dark data plane, not gates. Post-
    # migration the 15m ATR measures the perp's true 24/7 vol (incl. the
    # overnight moves) instead of session-only underlying vol.
    sodex_kline_assets: list[str] = [
        "SILVER-USD", "COPPER-USD",
        "TSM-USD", "ORCL-USD", "NVDA-USD", "MSFT-USD", "AAPL-USD",
        "AMZN-USD", "GOOGL-USD", "META-USD", "TSLA-USD",
        "USTECH100-USD", "SPCX-USD",
        # 2026-09-11 equity-perp expansion (same directive session): the 6
        # adds are kline-owned from birth; COIN/CRCL were in-universe but
        # dark (no seed, no ownership) — the identical overnight wound.
        "HOOD-USD", "LITE-USD", "SMCI-USD",
        "SAMSUNG-USD", "SKHX-USD", "UNITREE-USD",
        "COIN-USD", "CRCL-USD",
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
    # 2026-09-17 Governor order: "the money on aster can be used in full" —
    # 0.80 → 0.95; the retained 5% is purely a fee/funding buffer.
    aster_margin_pct: float = 0.95
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
    aster_max_positions: int = 12   # 2026-09-18 Governor: 5->12 — cap blocked 3 UNI re-entries (8.74-8.76) + post-boot INJ 7.49
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

    # ── Hedge venue (P0 spine, 2026-09-19 Governor-approved) ─────────────────
    # WHY: a third venue (ByBit) will run intentional hedge SHORTS against
    # Aster/SoDEX longs. P0 builds the inert spine every later phase depends
    # on: role tagging on Position ("primary"|"hedge"), risk/hedge_registry.py
    # (hedge legs NEVER enter the PositionManager — it nets opposite sides by
    # symbol, so a hedge short would annihilate the long's record),
    # venue-grouped reconciliation, and role guards across the portfolio loops.
    # hedge_enabled=False is the MASTER kill switch: no hedge positions can
    # exist and the primary book behaves bit-for-bit as before. hedge_venue
    # names the venue whose exchange-position rows have no primary-book
    # meaning — they route to the (P0 no-op, P2-filled) hedge-reconcile hook
    # instead of primary matching.
    hedge_enabled: bool = False
    hedge_venue: str = "bybit"

    # ── Hedge P2 knobs (2026-09-19 evidence review, Governor-approved) ───────
    # WHY each knob exists is cited to the 2026-09-19 review of 1,035 closes:
    #  - Rescue mode was REFUTED by counterfactual economics (a naive
    #    −15%/100%/8%-trail rescue is negative-EV in 3 of 4 scenarios) → it is
    #    SHADOW ONLY: hedge_rescue_shadow_enabled measures, hedge_rescue_live
    #    stays False and no live rescue path exists in v1.
    #  - Leverage is DERIVED, never assumed (2026-09-19 Governor formula):
    #    min_clearance = 1.25 × stop_frac + mmr − free_equity/notional;
    #    ≤ 0 → hedge_leverage_max; else lev = int(1/min_clearance) clamped to
    #    [hedge_leverage_min, hedge_leverage_max], below the floor → the plan
    #    is skipped as unhedgeable. The cross buffer is what makes the 18%
    #    stop viable — and the pair is only safe because the exchange-side
    #    catastrophic stop survives restarts.
    #  - The basis gate (not stressed AND basis ≥ hedge_min_basis_bp) exists
    #    because a hedge short opened into a dislocated-down hedge-venue mark
    #    locks in cross-venue bleed instead of profit.
    # Profit-lock rungs: (house-ROE % on the roe_ratchet basis → hedge ratio
    # of the long qty). Highest rung wins; ratchets UP only.
    hedge_profit_lock_enabled: bool = True
    hedge_profit_lock_rungs: list = [(0.30, 0.03), (0.50, 0.06),
                                     (0.70, 0.10), (1.00, 0.15)]
    hedge_min_notional: float = 6.0       # ByBit min order ≈ 5 USDT + buffer
    hedge_min_basis_bp: float = -2.0      # basis floor for arming (bp)
    hedge_leverage_min: int = 5           # floor of the leverage derivation
    hedge_leverage_max: int = 15          # ceiling of the leverage derivation
    hedge_trail_pct: float = 0.08         # trailing stop distance × entry (abs)
    hedge_catastrophic_stop_pct: float = 0.18   # fixed stop at entry×1.18
    hedge_chase_max_steps: int = 3        # amends before one market conversion
    hedge_chase_reprice_pct: float = 0.0025   # reprice trigger floor (0.25%)
    hedge_chase_min_amend_s: float = 15.0 # min seconds between amends
    hedge_chase_abandon_atr_mult: float = 1.0  # mark < arm_mark − mult×arm_atr → abort
    hedge_slippage_cap: float = 0.001     # market-conversion abort cap (0.1%)
    hedge_tick_est_pct: float = 0.0005    # fallback tick estimate (fraction)
    hedge_spread_est_pct: float = 0.0005  # fallback spread estimate (fraction)
    hedge_whipsaw_cooloff_s: float = 7200.0   # per-symbol after a trail cover
    # Dynamic short limit: S_max = max(floor, frac × Σ max(0, upnl_long_i)) —
    # the hedge book is funded by open-long profits, never by margin hope.
    hedge_short_floor_usd: float = 20.0
    hedge_short_upnl_frac: float = 0.5
    # Rescue (SHADOW ONLY — refuted economics; see above).
    hedge_rescue_shadow_enabled: bool = True
    hedge_rescue_live: bool = False       # HARD False in v1 — zero live rescue
    hedge_rescue_min_time_s: float = 3600.0
    hedge_rescue_roe_trigger_pct: float = -10.0
    hedge_rescue_giveback_frac: float = 0.5   # de-hedge: 50% giveback from peak

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

    # S1 — ETH 4h OI-pullback swing (Governor 2026-09-15, BYBIT-MAP register).
    # Live gate: this flag OR env ARIA_S1_ETH_OI=1. The loop runs and
    # shadow-scores from birth regardless — the gate only controls whether
    # SIGNAL_READY is published. Spec constants pin in
    # intelligence/s1_oi_pullback.py (knob defaults; these are wiring only).
    s1_oi_pullback_enabled: bool = False
    s1_oi_pullback_loop_s: float = 300.0
    s1_oi_pullback_refire_s: float = 8 * 3600.0

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
    max_deployed_pct: float = 0.60   # Governor 2026-09-14 (was 0.40) — trades must fire pre-funding to surface bugs
    min_trade_notional_usd: float = 75.0   # SoDEX hard floor $10 notional. Strategy floor $75 (Governor 2026-09-14, was $80)
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

    # Clamp-RR gate (Governor 2026-09-18): _clamp_tp_to_sodex_range runs AFTER
    # the build_candidate min-RR gate, so entries at the 24h extreme reached
    # the exchange with inverted R:R (~0.02-0.07:1). The clamp now returns a
    # verdict and all 3 bracket call sites refuse the entry when the
    # post-clamp TP2/stop R:R is below the floor. False = pre-gate bit-for-bit.
    sodex_clamp_rr_gate_enabled: bool = True
    sodex_clamp_min_rr: float = 1.0   # post-clamp TP2/stop R:R floor

    # Cooldown hardening (Governor 2026-09-18): loss_cut_cooloff armed only on
    # conviction-decay abandons + treasury loss-cuts — plain stop-loss closes
    # never armed it, and the strike streak reset after a hardcoded 2h gap, so
    # spaced-out losses each read as strike 1 = a 5-min cooldown.
    loss_cooloff_on_any_loss_enabled: bool = True   # False = legacy armers only
    direction_loss_strike_decay_s: float = 21600.0  # strike streak reset window (was 7200)

    # Cross-sleeve veto (Governor 2026-09-18): one process, one PositionManager —
    # never open against our own book on any venue (opposing fill nets away).
    cross_sleeve_veto_enabled: bool = True          # False = pre-gate bit-for-bit
    cross_sleeve_max_gross_usd_per_symbol: float = 0.0  # 0 = off; SHADOW-only when >0

    # SoDEX direction gate (Governor 2026-09-18 sleeve audit — ETH shorted
    # into a 7% bull day, OP shorted at 2.4:1 whale-long positioning).
    # Counter-trend + >=(min_agree-1) confirming extremes (funding carry,
    # whale L/S) = block. SHADOW-first: enabled accrues would-block evidence,
    # entries proceed until _live flips on shadow proof. enabled=False =
    # pass-through, zero code runs.
    sodex_direction_gate_enabled: bool = True
    sodex_direction_gate_live: bool = False   # promote only on shadow evidence
    sodex_gate_funding_extreme_bp: float = 8.0
    sodex_gate_whale_ratio: float = 2.0
    sodex_gate_min_agree: int = 2

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

    # Balance-failure strictness. WHY (2026-09-18): SoDEX perps balance
    # endpoints went dark 08:06-10:36Z (1,337 balance_fetch_failed events);
    # get_account_balance swallowed the failure into 0.0, the venue guard
    # only treats EXCEPTIONS as leg failure, and the combined read collapsed
    # to the Aster-only sleeve — a phantom 61.15% session DD halted ALL
    # entries 08:10→13:40Z. True = raise on total fetch failure (venue marks
    # the leg FAILED, last-good substituted) + 0.0-leg belt-and-braces in the
    # balance loop. False = pre-change bit-for-bit (0.0 return, exception-only
    # substitution).
    balance_failure_strict_enabled: bool = True

    # Fixed floor position sizing — replaces Kelly on small accounts
    # Set base_trade_usd > 0 to use conviction-scaled notional instead of risk_pct × balance.
    # Mainnet: $200 base, conviction × [1.0, 1.5, 2.0], capped at max_notional_usd.
    # Balance safety cap (50% of balance) applied before returning from build_candidate.
    # Temporal/DD multipliers applied AFTER build_candidate — min_trade_notional_usd is
    # the post-multiplier SoDEX floor (50).
    # Operator directive 2026-08-24: 200→600 base, 250→750 ceiling — 3× capital
    # step-up (with aster_margin_pct 0.25→0.50). ~$102 typical / $150 max margin
    # per trade ≈ 13-20% of a $763 book; Chancellor 60% total ceiling unchanged.
    # Operator directive 2026-09-17: 600→900 base, 750→1125 ceiling (+50%),
    # kingdom exposure ceiling 0.60→0.90 — deploy more of the book per
    # approved trade after the sizing-compression autopsy ($600→$4.21 margin).
    base_trade_usd: float = 900.0    # Base notional per trade
    min_trade_usd: float = 200.0     # Hard $200 minimum per trade — never build below this
    max_trade_usd: float = 1125.0    # Hard ceiling notional; balance safety cap may reduce below this
    max_notional_usd: float = 1125.0  # Alias for max_trade_usd — used in sizing formula

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
        "HOOD-USD":    0.3,
        "LITE-USD":    0.3,
        "SMCI-USD":    0.3,
        "SAMSUNG-USD": 0.3,
        "SKHX-USD":    0.3,
        "UNITREE-USD": 0.3,
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
    # ── regime-engine-v1 (Governor msg-186, filing regime-engine-v1 2026-09-10;
    # endorsed 2026-09-11 "make thus fix live and all fixes") ────────────────
    # Wounds repaired: HYPE shorted 40s after LOCKED trend/UP (conflict
    # fail-open abstained the veto); OP 4 longs into a -6.49% breakdown
    # (day_type != trend gated away ALL direction evidence); day classifier
    # anti-tape 98.3% trend rows at 42.4% accuracy; long share rose INTO the
    # 09-07/08 selldown. P0 EMA-slope regime + trend-alignment filter on BOTH
    # planes; P1 widen stops in trend + trend-hold mode. All refusals shadow-
    # scored from birth (gate counter_trend, event signal_rejected_counter_trend).
    trend_guard_locked_orb_wins: bool = True   # locked ORB breakout outranks stale 24h conflicts (HYPE fix)
    trend_guard_strong_move_mult: float = 2.0  # |day_move| >= mult x threshold votes even when day_type != trend (OP fix); 0 = off
    ema_regime_enabled: bool = True            # EMA-slope second plane (kill switch; False = ORB guard alone, bit-for-bit)
    ema_regime_fast: int = 8                   # 15m bars
    ema_regime_slow: int = 21
    ema_regime_slope_lookback: int = 3
    ema_regime_min_sep_atr: float = 0.15       # min |fast-slow| separation in ATR units for a directional read
    trend_veto_fastpath_enabled: bool = True   # bind the veto on explosive + whale probe guard chains (both planes)
    trend_stop_widen_enabled: bool = True      # P1: aligned entries on a locked trend day get wider stops
    trend_stop_widen_mult: float = 1.25        # storm-mode idiom (×1.25)
    trend_hold_mode_enabled: bool = True       # P1: aligned positions on a locked trend day are not abandoned by the conviction clock
    # 2026-09-11 — pair_meanrev SHADOW gate (queue #66, pair pipeline step 2).
    # Scores screened cointegrated pairs (logs/pair_screen.json) counter-
    # factually from birth into logs/pair_shadow.jsonl. SHADOW-ONLY: no live
    # orders until n>=50 AND EV>+0.15R AND CI>0 (whale_absorption doctrine).
    pair_meanrev_shadow_enabled: bool = True   # kill switch; False = loop stands down
    pair_z_entry: float = 2.0                  # |z| to open a shadow position
    pair_z_exit: float = 0.5                   # |z| to close as mean_reverted
    pair_z_stop: float = 3.5                   # adverse |z| excursion stop
    pair_max_open: int = 3                     # concurrent shadow slot budget
    pair_cost_bps_rt: float = 16.0             # round-trip taker cost, both legs
    # 2026-09-11 — canon execution-formula measurement plane (Governor
    # directive "tune this live" = instruments live as SHADOW telemetry only;
    # no live gate/sizing changes until >=200 counterfactuals + Bonferroni
    # alpha=0.01 screen). intelligence/exec_formulas.py -> logs/exec_formulas.jsonl.
    exec_formulas_enabled: bool = True    # kill switch; False = loop stands down
    exec_formulas_window: int = 60        # rolling 1m bars for the estimators
    exec_formulas_publish_s: int = 300    # per-symbol publish cadence
    # 2026-09-15 — Hurst/regime classification plane (Governor spec "no
    # strategy should fire until Hurst is computed", shipped SHADOW-from-birth:
    # classification + shadow scoring ON, live gating OFF). Brain:
    # intelligence/hurst_regime.py (raw-series R/S Hurst + realized-vol rank ->
    # chaotic/trending/mean_reverting/random/unknown). States mirror to
    # logs/regime_states.json + regime_states.jsonl; the shadow gate
    # "regime_gate" counterfactually scores would-have-blocked entries.
    regime_classify_enabled: bool = True    # kill switch; False = loop stands down
    regime_gate_live_enabled: bool = False  # MUST stay False until the shadow
                                            # census (n>=20/cell) argues otherwise
    regime_loop_interval_s: int = 300       # classification cadence
    regime_cache_ttl_s: int = 1800          # 4h bars move slowly — REST cache TTL
    regime_min_bars: int = 100              # Governor floor: Hurst abstains below
    # 2026-09-15 — stock-carry shadow plane (B4 register consumer, register
    # Stocks 1 & 2; closes audit P0-1/P0-2/P1-4). Runs the ORCL basis-episode
    # + META regime-shift brains (intelligence/stock_carry.py — SHADOW_ONLY
    # by declaration, evidence-thin) as paper shadows in
    # logs/stock_carry_shadow.json + lifecycle rows in
    # logs/stock_carry_shadow.jsonl. ZERO execution wiring.
    stock_carry_shadow_enabled: bool = True   # kill switch; False = loop stands down
    stock_carry_orcl_symbol: str = "ORCL-USD"
    stock_carry_meta_symbol: str = "META-USD"
    # 2026-09-19 — fee actuals at close (forensic audit: fee_usd was always
    # null in trade records and fee_est_usd was never subtracted, making
    # fee-drag audits impossible). On a SoDEX close the close path queries
    # GET /accounts/{addr}/trades (open→now) and books ADDITIVE fields
    # (fee_actual_usd / fee_maker_usd / fee_taker_usd / fee_fill_count) onto
    # the trade record — net_pnl / fee_est_usd semantics unchanged. One query
    # per close, fail-open (any error → null fields + fee_actuals_unavailable
    # event, close accounting never blocked). False = skip the query entirely
    # (pre-change behavior).
    fee_actuals_enabled: bool = True
    # 2026-09-15 — vol-stop cybernetics (Governor order, September exit
    # census: stops ~0.41% fire inside 1-sigma of 4h noise; 91.6% of stopped
    # trades went green first). ATR(14,4h) stop floor + R:R TP1 floor at
    # bracket creation, frozen at entry, widen-only. intelligence/vol_stop.py
    # is the zero-I/O brain; main.py splices fetch/cache + floors at the 3
    # place_bracket sites. Env kill switch VOL_STOP_ENABLED wins when
    # explicitly "false"; disabled = pre-module geometry bit-for-bit.
    vol_stop_enabled: bool = True
    vol_stop_atr_period: int = 14
    vol_stop_tp_rr: float = 2.5            # TP1 >= 2.5 x final stop distance
    vol_stop_cache_s: int = 300            # per-symbol 4h-plane cache TTL
    # P1b (Governor 2026-09-15, option B): the floor lands AFTER risk-parity
    # sizing, so a widened stop would silently multiply USD risk ~6.5x.
    # Re-size proportional to the widening (constant-risk), floored at the
    # venue min notional (bounded expansion, never abstains for size).
    vol_stop_resize_enabled: bool = True
    # FIX A (Governor 2026-09-18, UNI +9.88% runner autopsy): the constant-
    # risk resize amputated notional by the full floor ratio (median 4.8x
    # floor -> ~0.2x size -> dust entries; UNI paid $0.77 on a +9.88% run).
    # The shrink ratio is clamped at this floor so floored entries keep
    # meaningful size. Governor directive: 0.75 (75% of balance stays
    # deployable). Residual risk overshoot = ratio x floor_ratio (median
    # ~3.6x intended at 0.75) — bounded by the 5% daily-loss veto.
    # 0.0 = legacy constant-risk resize bit-for-bit.
    vol_stop_resize_min_ratio: float = 0.75
    # FIX C shadow gate (OFF): when > 0, a floored stop whose floor ratio
    # (floored_dist / orig_dist) exceeds this emits
    # signal_rejected_vol_stop_regime (shadow gate vol_stop_regime) — the
    # entry proceeds; the cohort answers whether rejecting extreme-floor
    # entries would pay. 0.0 = event never fires.
    vol_stop_max_floor_ratio: float = 0.0
    # Floor-tighten fallback (2026-09-19 OP/ARB audit): when the constant-risk
    # re-size target lands below the venue min-notional floor, the floored
    # size comes out >= the current size, the re-size guard `0 < new < orig`
    # fails, and the re-size SILENTLY skipped — the full position rode the
    # wide vol stop at 2.3-3.1x intended USD risk (OP 2026-09-19: 7.71% stop,
    # 2.27x; ARB 2026-09-17: 10.51% stop, 3.1x; both `resized=false` with zero
    # telemetry). Instead tighten the stop to the distance that holds the
    # intended constant-dollar risk at the CURRENT size (tighten-only, never
    # wider than the live stop) and emit vol_stop_floor_tightened /
    # vol_stop_resize_blocked. False = pre-fix bit-for-bit (silent skip).
    vol_stop_floor_tighten_enabled: bool = True

    # ── Pyramid layer (Governor directive 2026-09-18; spec /tmp/pyramid_spec.md) ──
    # Staircase adds into proven moves, legs as sub-allocations of ONE netted
    # exchange position (aster_swing precedent). LIVE from day one per Governor
    # order ("enabled all new builds from day one" — no shadow phase); the
    # TP1 parent gate (main.py aster_swing_add_gate family) binds every add.
    # Watchdog MUST-NOT tune any pyramid_* knob.
    pyramid_enabled: bool = True
    pyramid_shadow: bool = False
    pyramid_scalp_leg_weights: str = "0.5,0.5"
    pyramid_swing_leg_weights: str = "0.405,0.25,0.20,0.145"
    pyramid_scalp_trigger_atr: float = 0.3
    pyramid_swing_trigger_atr: float = 1.0
    pyramid_trigger_mode: str = "step"          # "step" | "cumulative" (shadow cohort)
    pyramid_max_concurrent: int = 2
    pyramid_max_exposure_pct: float = 0.30
    pyramid_funding_extreme_pct: float = 0.10
    pyramid_rv_rank_kill: float = 90.0          # IVR>=90 adaptation (rv_rank proxy)
    pyramid_oi_delta_kill_pct: float = -2.0
    pyramid_add_coherence_min: float = 5.0
    pyramid_reentry_coherence_min: float = 6.5
    pyramid_reentry_window_s: int = 86400
    pyramid_reentry_retrace_min: float = 0.30
    pyramid_reentry_retrace_max: float = 0.65
    pyramid_be_buffer_pct: float = 0.004
    pyramid_max_add_attempts: int = 2
    pyramid_scalp_leg_window_s: int = 1800
    pyramid_swing_leg_window_s: int = 14400
    pyramid_atr_period: int = 14
    pyramid_atr_timeframe: str = "15m"
    # Restart orphan seam (2026-09-19): startup-sync-adopted positions get a
    # TERMINAL-state track (staircase complete at birth — adds never fire,
    # kill-switch HARD_EXIT still covers, no SCALE_OUT). False = legacy
    # bit-for-bit (restarts orphan pyramid coverage on the live book).
    pyramid_boot_rebuild_enabled: bool = True
    # 2026-09-19 naked-stop incident (ETH short + ARB long left UNPROTECTED on
    # SoDEX): pyramid SCALE_OUT halved both positions via _record_partial_close
    # but the resting native stops kept the OLD full size; the immune purge then
    # cancelled both live stops as orphan_reduce_only on the size mismatch while
    # the trail/roe-ratchet loops were paused (pyramid owns stops mid-build).
    # pyramid_scaleout_stop_resize_enabled: after a successful scale-out partial
    #   close, replace the native stop at the SAME price for the REMAINING size
    #   (tighten size only, never move the price; fail-open on error — the
    #   software stop guardian still covers the position).
    # orphan_purge_protective_exempt_enabled: a reduce-only order whose side
    #   OPPOSES a live tracked position's side is protective — never purge it
    #   for size mismatch alone (the exchange caps reduce-only fills at
    #   position size, so an oversize RO stop is still fully protective).
    # False on either = pre-fix behavior bit-for-bit.
    pyramid_scaleout_stop_resize_enabled: bool = True
    orphan_purge_protective_exempt_enabled: bool = True
    # 2026-09-19 stuck-BUILDING incident: XMR/HYPE pyramid tracks sat in the
    # BUILDING phase all night, so owns_stop paused the roe_ratchet on both —
    # ~$1+ of locked profit forgone while ladder targets went unratcheted.
    # WHY safe: both systems are tighten-only (the ratchet caller is hardened
    # to be provably tighten-only — the 1bp mark-side cap is clamped against
    # the live stop), so the tighter stop always wins and they compose. True =
    # the roe_ratchet loop ignores pyramid stop-ownership (emits
    # roe_ratchet_pyramid_exempt per stop improvement on an owned symbol); the
    # trailing-stop loop stays paused (the trail can loosen). False =
    # pre-change skip bit-for-bit.
    pyramid_roe_ratchet_exempt_enabled: bool = True
    # 2026-09-19 stuck-pause incident (UNI/ETH/ARB scale-out'ed and stayed
    # paused FOREVER): SCALE_OUT set unwind_mode but never advanced the phase,
    # so the tracks sat in PAUSE_PHASES and trail/software_tp/time_stop never
    # resumed on the remaining position (the roe_ratchet exemption above
    # already shipped; the rest of the exit stack stayed paused all night).
    # pyramid_scaleout_unpause_enabled: after a successful scale-out, advance
    #   the phase to UNWINDING — pause_exits/owns_stop release, add-leg logic
    #   stays dead (an unwinding pyramid never re-adds), pyramid_closed
    #   journaling still fires at the final close.
    # pyramid_track_qty_sync_enabled: when a size sync adopts a new exchange
    #   size for a symbol with a pyramid track, re-anchor the track's
    #   current_qty (UNI tonight: track believed 5.5, book was 6.0 — future
    #   leg/unwind math read the ghost size). base_qty (the registration
    #   anchor for leg ratios) is never touched.
    # False on either = pre-fix behavior bit-for-bit.
    pyramid_scaleout_unpause_enabled: bool = True
    pyramid_track_qty_sync_enabled: bool = True
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
    # 2026-09-06 (CEO spec D11): ATR floor on the ROE ratchet — a ratchet stop
    # may never sit closer than this × ATR from the mark. INHERITED from
    # trail_distance_atr (1.0). CLOSED to tuning until shadow gate
    # roe_ratchet_atr_floor has n≥30. Env kill switch ROE_RATCHET_ATR_FLOOR.
    roe_ratchet_min_stop_dist_atr: float = 1.0
    # 2026-09-15 (Governor directive, Bybit AI model part 5): time-stop loser
    # cutoff SKIPS positions whose symbol is in the _roe_ratchet_owned registry
    # (identity-keyed on opened_at_ms — the ratchet has locked a stop at/above
    # breakeven, so the loser clock is moot). Max-hold still binds. False =
    # pre-bypass system bit-for-bit.
    time_stop_ratchet_bypass_enabled: bool = True
    # 2026-09-04 (watchdog cycle-25 P0): the cascade fast paths bypass the
    # interpreter, so the Gate -1 macro-print calendar block never bound them —
    # three momentum entries fired INTO the NFP print (-$5.53 in 77s). Prints
    # CAUSE liquidation cascades; the fast paths must stand down on BLOCK.
    cascade_calendar_block_enabled: bool = True
    # 2026-09-16 (Governor-endorsed, cascade-post-print-settle-hole-0916):
    # BLOCK stands down DURING the print; the measured leak is the first 30
    # min AFTER — post-print cascade entries [0-30m) n=11 net -$7.57 avg
    # -$0.688 WR 27.3% vs no-event control n=188 avg -$0.149 WR 40.4%; the
    # [30-120m) and [2-12h) arms are POSITIVE, so the band is exactly 0.5h,
    # never wider. False = pre-change system bit-for-bit.
    cascade_settle_band_enabled: bool = True
    cascade_settle_band_hours: float = 0.5
    # 2026-09-17 Governor-approved Phase 1: 0-for-8, -$6.32 pooled post-print
    # T+0-3min census — calendar regime stays BLOCK until event_time + dwell.
    post_print_block_seconds: int = 180
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
    # WHY (2026-09-19 22:03Z defect): the treasury ledger excluded pyramid-
    # paused symbols outright; with 6 of 7 open positions mid-staircase the
    # managed value fell below the activation floor and profit-taking stood
    # down book-wide (treasury_deactivated on a full book). True = the
    # ACTIVATION floor counts ALL open positions (paused included) while the
    # per-symbol MANAGEMENT skip is unchanged (treasury never trades/adjusts
    # a symbol mid-staircase — pyramid owns their exits). False restores the
    # pre-change behavior bit-for-bit.
    treasury_activation_ignores_pyramid_pause_enabled: bool = True
    # Fallback/Legacy Aliases (for Pydantic validation)
    risk_pct: float = 0.03              # 3% risk per trade
    min_coherence: float = 3.5  # Gate 5: lowered for small-account signal flow
    auto_adj_enabled: bool = False  # Enable auto-adjustment position closes (set True after validation)
    funding_carry_threshold: float = 1.5    # min |funding_rate|% to activate carry veto (Gap 4)
    regime_stability_window_s: float = 180.0  # seconds in transitioning before suppression (Gap 6)
    alpha_floor_min_trades: int = 10        # minimum trades before alpha floor applies (Gap 3)
    aftermath_session_bypass_min_coherence: float = 5.0  # min coherence for aftermath to bypass session exclusion
    # 2026-09-15 (Governor directive, Bybit AI structural model): the aftermath
    # two-condition entry gate (intelligence/aftermath_gate.py) — tier-scaled
    # minimum delay + L4 depth recovery + entry-side imbalance before a
    # post-cascade fade entry is eligible. Fail-open on dark data; exception
    # inside the gate = allow. False = pre-gate system bit-for-bit.
    aftermath_gate_enabled: bool = True
    aftermath_gate_imbalance_floor: float = 1.20  # entry-side top-5 ratio floor
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
    chancellor_emergency_halt_balance: float = 0.0    # REMOVED (Governor 2026-09-09): floor was
    # anchored to combined equity but balance is per-venue since 2026-08-16 — $150 floored the
    # $110 Aster sleeve into a total halt. 0.0 disables; dd/daily-loss/exposure guards still bind.
    chancellor_veto_drawdown_pct: float = 8.0         # session DD (percent) → VETO
    chancellor_max_daily_loss_pct: float = 0.05       # realized daily loss fraction → VETO
    chancellor_max_symbol_exposure_pct: float = 0.15  # margin per symbol / balance → clamp
    chancellor_max_kingdom_exposure_pct: float = 0.90 # total margin / balance → clamp (Governor 2026-09-17: 0.60→0.90)
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
