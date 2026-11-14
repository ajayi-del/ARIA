"""Signal tap — the harness's one-way read of ARIA's prop_outbox.jsonl.

CEO prop-readonly-bridge-1106 contract: pull-only, effectively-once
(durable idempotency via deterministic source ids), TTL quarantine
(stale signals are dead on arrival), gap quarantine (file shrink or
rotation resets the cursor; dedup absorbs the replay). Symbol filter:
SYMBOL_MAP — crypto + commodities + indices + stocks (Governor
2026-11-07; the funded-program sleeve partition lives in the account
layer, not the tap).

Graph 7 (2026-11-08): REFUSE_SYMBOLS stamps explicit refuse reasons for
census-driven unmapped emits (no route invention); sleeve-aware
tradfi_ttl_s keeps xyz: rows alive through Monday RTH boot/poll lag;
arm_boot_replay() hot-cursors the tradfi owner without inventing
signals (dedup + TTL still bind).

Byte-offset doctrine: the cursor only ever advances past COMPLETE
lines. A trailing partial line is left unconsumed and re-read on the
next poll, so a mid-write read can never corrupt a record.

Transports: local file (same-host future, tests, demos) and ssh
tail-pull against the ARIA VM (current cross-machine reality).
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from .journal import atomic_write_json, read_json
from ..families import family_of

# Symbol universe (Governor 2026-11-07: all three classes on the demo
# account — sleeve-B-only filter superseded; the 1:1 account↔sleeve
# partition still governs the FUNDED multi-account program). Every asset
# needs a SPECS row in bracket.py (xyz: assets fall back to
# XYZ_DEFAULT_SPEC) and a venue margin config set at provisioning.
SYMBOL_MAP = {
    # crypto
    "BTC-USD": "BTC", "ETH-USD": "ETH", "SOL-USD": "SOL",
    "DOGE-USD": "DOGE", "XRP-USD": "XRP", "AVAX-USD": "AVAX",
    "LINK-USD": "LINK", "AAVE-USD": "AAVE",
    "ZEC-USD": "ZEC", "BCH-USD": "BCH",
    # crypto wave 2 (venue WS listing ∩ ARIA universe, 2026-11-07)
    "BNB-USD": "BNB", "OP-USD": "OP", "ARB-USD": "ARB", "SUI-USD": "SUI",
    "NEAR-USD": "NEAR", "HBAR-USD": "HBAR", "TRUMP-USD": "TRUMP",
    "LTC-USD": "LTC", "HYPE-USD": "HYPE", "ADA-USD": "ADA",
    "UNI-USD": "UNI", "ONDO-USD": "ONDO", "TAO-USD": "TAO",
    "ENA-USD": "ENA", "VIRTUAL-USD": "VIRTUAL", "SEI-USD": "SEI",
    "PENGU-USD": "PENGU", "INJ-USD": "INJ", "TIA-USD": "TIA",
    "APT-USD": "APT", "TRX-USD": "TRX", "XLM-USD": "XLM",
    "FARTCOIN-USD": "FARTCOIN", "ASTER-USD": "ASTER", "WLD-USD": "WLD",
    "ICP-USD": "ICP", "XMR-USD": "XMR", "WLFI-USD": "WLFI",
    "LIT-USD": "LIT", "PAXG-USD": "PAXG",
    "1000PEPE-USD": "kPEPE", "1000BONK-USD": "kBONK",
    # equities (ARIA SoDEX names → venue xyz: listings)
    "LITE-USD": "xyz:LITE", "SAMSUNG-USD": "xyz:SMSN",
    "SKHX-USD": "xyz:SKHX", "UNITREE-USD": "xyz:UNITREE",
    "DRAM-USD": "xyz:DRAM",
    # commodities (XAUT is ARIA's gold vehicle)
    "XAUT-USD": "xyz:GOLD", "SILVER-USD": "xyz:SILVER",
    "COPPER-USD": "xyz:COPPER", "CL-USD": "xyz:CL",
    # indices
    "SPCX-USD": "xyz:SP500", "USTECH100-USD": "xyz:XYZ100",
    # single-name stocks (venue-probed listed 2026-11-07)
    "NVDA-USD": "xyz:NVDA", "TSLA-USD": "xyz:TSLA", "AMD-USD": "xyz:AMD",
    "MSFT-USD": "xyz:MSFT", "META-USD": "xyz:META", "AAPL-USD": "xyz:AAPL",
    "AMZN-USD": "xyz:AMZN", "GOOGL-USD": "xyz:GOOGL", "COIN-USD": "xyz:COIN",
    "ORCL-USD": "xyz:ORCL", "TSM-USD": "xyz:TSM", "HOOD-USD": "xyz:HOOD",
    "MSTR-USD": "xyz:MSTR",
    # venue-probed listed + margin-provisioned 2026-11-08
    "SMCI-USD": "xyz:SMCI", "CRCL-USD": "xyz:CRCL",
    # ── wave 3 (2026-11-08, Governor: "ensure equities, fx preipo tokens are
    # added on the prop legs"). Propr executes on Hyperliquid (margin-config
    # exchange field), so the binding listing constraint is the HL xyz dex
    # (143 assets probed live today); the propr program's leverage-limits
    # endpoint confirms fx (25x), pre_ipo (4x), equity (4x) as first-class
    # instrument classes. SPECS: all xyz: assets ride XYZ_DEFAULT_SPEC.
    # fx
    "EUR-USD": "xyz:EUR", "JPY-USD": "xyz:JPY", "GBP-USD": "xyz:GBP",
    "DXY-USD": "xyz:DXY", "KRW-USD": "xyz:KRW",
    # indices + country ETFs
    "JP225-USD": "xyz:JP225", "KR200-USD": "xyz:KR200",
    "NIFTY-USD": "xyz:NIFTY", "MAGS-USD": "xyz:MAGS",
    "IBOV-USD": "xyz:IBOV", "EWJ-USD": "xyz:EWJ", "EWT-USD": "xyz:EWT",
    "EWY-USD": "xyz:EWY", "EWZ-USD": "xyz:EWZ",
    # rates
    "TLT-USD": "xyz:TLT",
    # semis / hardware
    "AVGO-USD": "xyz:AVGO", "QCOM-USD": "xyz:QCOM", "MU-USD": "xyz:MU",
    "INTC-USD": "xyz:INTC", "MRVL-USD": "xyz:MRVL", "AMAT-USD": "xyz:AMAT",
    "LRCX-USD": "xyz:LRCX", "ASML-USD": "xyz:ASML", "SNDK-USD": "xyz:SNDK",
    "WDC-USD": "xyz:WDC", "STX-USD": "xyz:STX", "UMC-USD": "xyz:UMC",
    "ARM-USD": "xyz:ARM", "AAOI-USD": "xyz:AAOI", "SOXL-USD": "xyz:SOXL",
    "SMH-USD": "xyz:SMH",
    # 2026-11-10: CBRS HL-listed (xyz:CBRS); SoDEX CBRS-USD absent —
    # map enables turbo2 tap when sibling HL marks plane scores it.
    "CBRS-USD": "xyz:CBRS",
    # megacap ex-semis
    "NFLX-USD": "xyz:NFLX", "IBM-USD": "xyz:IBM", "COST-USD": "xyz:COST",
    "LLY-USD": "xyz:LLY", "NOW-USD": "xyz:NOW",
    # software / cloud
    "SNOW-USD": "xyz:SNOW", "SHOP-USD": "xyz:SHOP", "NET-USD": "xyz:NET",
    "CRWD-USD": "xyz:CRWD", "ZM-USD": "xyz:ZM", "TWST-USD": "xyz:TWST",
    "DELL-USD": "xyz:DELL", "ACN-USD": "xyz:ACN",
    # ai / datacenter
    "PLTR-USD": "xyz:PLTR", "IREN-USD": "xyz:IREN", "NBIS-USD": "xyz:NBIS",
    "CRWV-USD": "xyz:CRWV",
    # consumer / misc single-names
    "GME-USD": "xyz:GME", "RDDT-USD": "xyz:RDDT", "SNAP-USD": "xyz:SNAP",
    "RIVN-USD": "xyz:RIVN", "MRNA-USD": "xyz:MRNA", "HIMS-USD": "xyz:HIMS",
    "DKNG-USD": "xyz:DKNG", "EBAY-USD": "xyz:EBAY", "MELI-USD": "xyz:MELI",
    "TTWO-USD": "xyz:TTWO", "NOK-USD": "xyz:NOK",
    # china
    "BABA-USD": "xyz:BABA",
    # fintech / crypto-adjacent equity
    "BMNR-USD": "xyz:BMNR", "NU-USD": "xyz:NU", "BX-USD": "xyz:BX",
    # energy equity
    "CVX-USD": "xyz:CVX", "XLE-USD": "xyz:XLE",
    # commodities: uranium / metals / energy / ags
    "URANIUM-USD": "xyz:URANIUM", "URNM-USD": "xyz:URNM",
    "PLATINUM-USD": "xyz:PLATINUM", "PALLADIUM-USD": "xyz:PALLADIUM",
    "ALUMINIUM-USD": "xyz:ALUMINIUM",
    "NATGAS-USD": "xyz:NATGAS", "HO-USD": "xyz:HO", "TTF-USD": "xyz:TTF",
    "WHEAT-USD": "xyz:WHEAT", "CORN-USD": "xyz:CORN",
    # pre-IPO / private-company tokens (HL xyz; OPENAI/ANTHROPIC are
    # Variational-only — NOT tradeable via propr's HL execution)
    "MINIMAX-USD": "xyz:MINIMAX", "ZHIPU-USD": "xyz:ZHIPU",
    "H100-USD": "xyz:H100", "CAMBRICON-USD": "xyz:CAMBRICON",
    "CXMT-USD": "xyz:CXMT", "INNOLIGHT-USD": "xyz:INNOLIGHT",
    "IBIDEN-USD": "xyz:IBIDEN", "KIOXIA-USD": "xyz:KIOXIA",
    "GIGADEV-USD": "xyz:GIGADEV", "YMTC-USD": "xyz:YMTC",
    "SHEIN-USD": "xyz:SHEIN", "OURA-USD": "xyz:OURA",
    "RELIANCE-USD": "xyz:RELIANCE", "JIO-USD": "xyz:JIO",
    "SOFTBANK-USD": "xyz:SOFTBANK", "CATL-USD": "xyz:CATL",
    "HYUNDAI-USD": "xyz:HYUNDAI",
}

# Turbo crypto sleeve denylist (ATS-102 / week census Mon 2026-11-09
# Europe/Berlin → now). Applies ONLY when asset_class=="crypto" so
# turbo2 stocks and demo asset_class=all stay bit-for-bit. Kill:
# PROP_CRYPTO_DENY=0 or crypto_deny_enabled=False. Override list:
# PROP_CRYPTO_DENY_SYMBOLS=comma ARIA symbols (empty = this default).
# Evidence (turbo journal reconcile_closed_external): WLD −$8.00, APT
# −$7.09, SOL −$6.09, BCH −$5.58, XMR −$3.75, SUI −$1.83 (Σ −$32.34 =
# ~94% of week crypto loser PnL). Stocks/xyz: never listed here.
DEFAULT_CRYPTO_DENY_SYMBOLS: frozenset[str] = frozenset({
    "WLD-USD", "APT-USD", "SOL-USD", "BCH-USD", "XMR-USD", "SUI-USD",
})


def parse_crypto_deny_symbols(raw: str | None) -> frozenset[str]:
    """Empty/None → DEFAULT; non-empty comma list overrides."""
    if raw is None:
        return DEFAULT_CRYPTO_DENY_SYMBOLS
    text_ = str(raw).strip()
    if not text_:
        return DEFAULT_CRYPTO_DENY_SYMBOLS
    return frozenset(s.strip() for s in text_.split(",") if s.strip())


# Graph 7 / infra-profit §4 P1 — census-driven refuse table.
# Recurring ARIA emits that must NOT invent a propr route (venue-dark on
# HL / Aster-only incubation / Arcus sleeve names outside SYMBOL_MAP).
# Explicit reason > bare tap_filtered. Emit-side hygiene reads this too.
REFUSE_SYMBOLS: dict[str, str] = {
    # Aster incubation / explosive watchlist — propr HL unlisted
    "KAITO-USD": "venue_unlisted",
    "WIF-USD": "venue_unlisted",
    "VELVET-USD": "venue_unlisted",
    "AKE-USD": "venue_unlisted",
    "CYS-USD": "venue_unlisted",
    "ACE-USD": "venue_unlisted",
    "MUBARAK-USD": "venue_unlisted",
    "DOS-USD": "venue_unlisted",
    "SNXX-USD": "venue_unlisted",
    "HEMI-USD": "venue_unlisted",
    "AIO-USD": "venue_unlisted",
    "ARIA-USD": "venue_unlisted",
    "QNT-USD": "venue_unlisted",
    "BOME-USD": "venue_unlisted",
    "ORDI-USD": "venue_unlisted",
    "FLOCK-USD": "venue_unlisted",
    "FF-USD": "venue_unlisted",
    "TRIA-USD": "venue_unlisted",
    "NOM-USD": "venue_unlisted",
    "ZEN-USD": "venue_unlisted",
    "ICX-USD": "venue_unlisted",
    # Arcus assets deliberately outside harness map (broadcast filter)
    "SPY-USD": "arcus_unmapped",
    "GLD-USD": "arcus_unmapped",
    "USO-USD": "arcus_unmapped",
    "PUMP-USD": "arcus_unmapped",
    "W-USD": "arcus_unmapped",
}

_SIDE_MAP = {"long": "buy", "buy": "buy", "short": "sell", "sell": "sell"}
REQUIRED_FIELDS = ("id", "symbol", "side", "entry_ref", "stop_price", "ts_ms")
DEFAULT_REMOTE_OUTBOX = "/home/dayodapper/ARIA/signals/prop_outbox.jsonl"
# management-layer-schema §7 — unlabeled omit = legacy aria_exec.
BLOCK_EXEC = "aria_exec"
BLOCK_CAPITAL = "aria_capital_block"
BLOCK_RISK = "aria_risk_block"
# Monday-RTH enablement default for xyz: rows (kill: tradfi_ttl_s<=0 → legacy).
DEFAULT_TRADFI_TTL_S = 3600.0


def refuse_reason(symbol: str) -> str:
    """Explicit refuse reason for an unmapped symbol (census table or generic)."""
    return REFUSE_SYMBOLS.get(symbol, "unmapped")


class SignalTap:
    def __init__(self, cursor_path: Path, ttl_s: float = 300.0,
                 dedup_cap: int = 2000, asset_class: str = "all",
                 families: frozenset[str] | None = None,
                 capital_surplus_enabled: bool = True,
                 tradfi_ttl_s: float | None = DEFAULT_TRADFI_TTL_S,
                 crypto_deny_enabled: bool = True,
                 crypto_deny_symbols: frozenset[str] | None = None):
        # asset_class (Governor 2026-11-07 sleeve split): "crypto" keeps only
        # non-xyz: assets, "tradfi" keeps only xyz: assets, "all" = bit-for-bit.
        if asset_class not in ("all", "crypto", "tradfi"):
            raise ValueError(f"bad asset_class {asset_class!r}")
        self.asset_class = asset_class
        # families (Governor 2026-11-08 two-turbo partition): when set, only
        # assets whose theme family is listed pass — each family lives on
        # exactly one account so the family cap's one-representative rule
        # doubles effective capacity without weakening it. Unknown assets map
        # to solo:ASSET families, so they pass only if explicitly listed.
        self.families = families
        # §7 S3: capital_block actionable only when surplus enabled; risk_block
        # always dropped. False = ignore surplus rows (legacy exec-only tap).
        self.capital_surplus_enabled = bool(capital_surplus_enabled)
        # Turbo crypto toxic-cohort denylist (sleeve-scoped). None symbols →
        # DEFAULT_CRYPTO_DENY_SYMBOLS. Disabled / non-crypto class → no-op.
        self.crypto_deny_enabled = bool(crypto_deny_enabled)
        self.crypto_deny_symbols = (
            DEFAULT_CRYPTO_DENY_SYMBOLS if crypto_deny_symbols is None
            else frozenset(crypto_deny_symbols))
        self.cursor_path = Path(cursor_path)
        self.ttl_ms = ttl_s * 1000.0
        # Graph 7: sleeve-aware TTL. tradfi_ttl_s<=0 → legacy (crypto TTL for
        # all). Default 3600s so Monday RTH equity rows survive boot/poll lag
        # instead of dying as tap_expired under the shared 300s cursor.
        if tradfi_ttl_s is None or float(tradfi_ttl_s) <= 0:
            self.tradfi_ttl_ms = self.ttl_ms
        else:
            self.tradfi_ttl_ms = float(tradfi_ttl_s) * 1000.0
        self.dedup_cap = dedup_cap
        state = read_json(self.cursor_path, {})
        self.offset = int(state.get("offset", 0))
        self.seen: dict[str, float] = dict(state.get("seen", {}))

    def _ttl_ms_for(self, is_tradfi: bool) -> float:
        return self.tradfi_ttl_ms if is_tradfi else self.ttl_ms

    def arm_boot_replay(self) -> dict | None:
        """One-shot hot-cursor: rewind offset→0 so the next poll rescans.

        In-TTL rows become actionable again; dedup + TTL quarantine still
        bind (no signal invention, no double-fire of seen ids). Returns the
        journal event, or None when already at head (nothing to rewind).
        """
        if self.offset <= 0:
            return None
        prev = self.offset
        self.offset = 0
        self._save()
        return {"type": "tap_boot_replay", "prev_offset": prev,
                "asset_class": self.asset_class,
                "tradfi_ttl_ms": self.tradfi_ttl_ms,
                "ttl_ms": self.ttl_ms}

    def _save(self) -> None:
        if len(self.seen) > self.dedup_cap:
            keep = list(self.seen.items())[-self.dedup_cap // 2:]
            self.seen = dict(keep)
        atomic_write_json(self.cursor_path,
                          {"offset": self.offset, "seen": self.seen})

    def consume(self, size: int, chunk: str,
                now_ms: int | None = None) -> tuple[list[dict], list[dict]]:
        """Consume bytes read from self.offset. Returns (signals, events)."""
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        events: list[dict] = []
        signals: list[dict] = []

        if size < self.offset:
            events.append({"type": "tap_gap_reset",
                           "offset": self.offset, "size": size})
            self.offset = 0
            self._save()
            return signals, events  # caller refetches from 0 next poll

        last_nl = chunk.rfind("\n")
        complete = chunk[:last_nl + 1] if last_nl >= 0 else ""
        self.offset += len(complete.encode("utf-8"))

        for line in complete.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                events.append({"type": "tap_malformed", "line": line[:120]})
                continue
            if not isinstance(rec, dict) or any(k not in rec for k in REQUIRED_FIELDS):
                events.append({"type": "tap_malformed", "line": line[:120]})
                continue
            side = _SIDE_MAP.get(str(rec["side"]).lower())
            if side is None:
                events.append({"type": "tap_malformed", "line": line[:120]})
                continue
            if rec["symbol"] not in SYMBOL_MAP:
                events.append({"type": "tap_filtered",
                               "reason": refuse_reason(rec["symbol"]),
                               "symbol": rec["symbol"], "id": rec["id"]})
                continue
            asset = SYMBOL_MAP[rec["symbol"]]
            is_tradfi = asset.startswith("xyz:")
            if ((self.asset_class == "crypto" and is_tradfi)
                    or (self.asset_class == "tradfi" and not is_tradfi)):
                events.append({"type": "tap_filtered", "reason": "asset_class",
                               "symbol": rec["symbol"], "id": rec["id"]})
                continue
            # Crypto-sleeve denylist ONLY — demo (all) / turbo2 (tradfi) skip.
            if (self.asset_class == "crypto" and self.crypto_deny_enabled
                    and rec["symbol"] in self.crypto_deny_symbols):
                events.append({"type": "tap_filtered", "reason": "crypto_deny",
                               "symbol": rec["symbol"], "id": rec["id"]})
                continue
            if self.families is not None and family_of(asset) not in self.families:
                events.append({"type": "tap_filtered", "reason": "family_subset",
                               "family": family_of(asset),
                               "symbol": rec["symbol"], "id": rec["id"]})
                continue
            ttl_ms = self._ttl_ms_for(is_tradfi)
            age_ms = now_ms - int(rec["ts_ms"])
            if age_ms > ttl_ms:
                events.append({"type": "tap_expired", "id": rec["id"],
                               "symbol": rec["symbol"], "tradfi": is_tradfi,
                               "age_ms": age_ms, "ttl_ms": ttl_ms})
                continue
            if rec["id"] in self.seen:
                events.append({"type": "tap_duplicate", "id": rec["id"]})
                continue
            # §7 block_class gate (S3): risk never actionable; capital only
            # when surplus kill switch allows; omit/aria_exec = today path.
            block_class = str(rec.get("block_class") or BLOCK_EXEC)
            if block_class == BLOCK_RISK:
                events.append({"type": "risk_block_dropped", "id": rec["id"],
                               "symbol": rec["symbol"]})
                self.seen[rec["id"]] = now_ms  # consume once — census not replay
                continue
            if block_class == BLOCK_CAPITAL and not self.capital_surplus_enabled:
                events.append({"type": "capital_block_standby", "id": rec["id"],
                               "symbol": rec["symbol"],
                               "capital_reason": rec.get("capital_reason")})
                self.seen[rec["id"]] = now_ms
                continue
            if block_class not in (BLOCK_EXEC, BLOCK_CAPITAL):
                events.append({"type": "tap_malformed", "line": line[:120]})
                continue
            self.seen[rec["id"]] = now_ms
            signals.append({
                "id": rec["id"],
                "symbol": rec["symbol"],
                "asset": SYMBOL_MAP[rec["symbol"]],
                "side": side,
                "entry_ref": float(rec["entry_ref"]),
                "stop_price": float(rec["stop_price"]),
                "tp1_price": (float(rec["tp1_price"])
                              if rec.get("tp1_price") else None),
                "coherence": rec.get("coherence"),
                "ts_ms": int(rec["ts_ms"]),
                "block_class": block_class,
                "capital_reason": rec.get("capital_reason"),
            })

        if complete or events:
            self._save()
        return signals, events


def fetch_local(path: Path, offset: int) -> tuple[int, str] | None:
    """Read from offset. Returns (file_size, chunk) or None if missing."""
    p = Path(path)
    if not p.is_file():
        return None
    size = p.stat().st_size
    if size < offset:
        return (size, "")
    with open(p, "rb") as f:
        f.seek(offset)
        return (size, f.read().decode("utf-8", "replace"))


def fetch_ssh(host: str, zone: str, remote_path: str, offset: int,
              timeout: int = 20) -> tuple[int, str]:
    """Pull new bytes from the ARIA VM. One ssh round-trip per poll."""
    cmd = (f'f="{remote_path}"; s=$(stat -c %s "$f" 2>/dev/null || echo 0); '
           f'echo "PROP_SIZE:$s"; '
           f'if [ "$s" -gt {offset} ]; then tail -c +{offset + 1} "$f"; fi')
    out = subprocess.run(
        ["gcloud", "compute", "ssh", host, f"--zone={zone}", f"--command={cmd}"],
        capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(f"ssh pull failed: {out.stderr.strip()[:200]}")
    first, _, rest = out.stdout.partition("\n")
    if not first.startswith("PROP_SIZE:"):
        raise RuntimeError(f"bad transport header: {first[:80]}")
    return (int(first.split(":", 1)[1]), rest)
