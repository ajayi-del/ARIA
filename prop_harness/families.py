"""Theme families for the rotation policy (Rule B: one representative
per correlated cluster, direction-aware).

Static, conservative grouping — the harness has no price plane, so
downside-convergence is encoded structurally: assets that collapse into
one risk factor in a selloff share a family. A hedge (opposite sides in
one family) stays legal; a second same-direction entry in a held family
is the churn the policy exists to kill (BCH+XRP+ZEC shorts at once was
the 2026-11-07 live example).
"""

from __future__ import annotations

_FAMILIES: dict[str, str] = {
    # crypto
    "BTC": "majors", "ETH": "majors",
    "SOL": "large_alt", "XRP": "large_alt", "ADA": "large_alt",
    "LTC": "large_alt", "BCH": "large_alt", "TRX": "large_alt",
    "XLM": "large_alt", "HBAR": "large_alt", "AVAX": "large_alt",
    "LINK": "large_alt", "BNB": "large_alt",
    "NEAR": "l1_alt", "SUI": "l1_alt", "APT": "l1_alt", "INJ": "l1_alt",
    "TIA": "l1_alt", "SEI": "l1_alt", "OP": "l1_alt", "ARB": "l1_alt",
    "ICP": "l1_alt",
    "AAVE": "defi", "UNI": "defi", "ONDO": "defi", "ENA": "defi",
    "DOGE": "meme", "TRUMP": "meme", "kPEPE": "meme", "kBONK": "meme",
    "FARTCOIN": "meme", "PENGU": "meme", "WLFI": "meme", "LIT": "meme",
    "HYPE": "exchange", "ASTER": "exchange",
    "ZEC": "privacy", "XMR": "privacy",
    "TAO": "ai", "VIRTUAL": "ai", "WLD": "ai",
    "PAXG": "metal",
    # xyz: tradfi
    "xyz:SP500": "index", "xyz:XYZ100": "index",
    "xyz:GOLD": "metal", "xyz:SILVER": "metal", "xyz:COPPER": "metal",
    "xyz:CL": "energy", "xyz:BRENTOIL": "energy",
    "xyz:NVDA": "equity_semis", "xyz:AMD": "equity_semis",
    "xyz:TSM": "equity_semis", "xyz:SMCI": "equity_semis",
    "xyz:SMSN": "equity_semis", "xyz:SKHX": "equity_semis",
    "xyz:MSFT": "equity_megacap", "xyz:META": "equity_megacap",
    "xyz:AAPL": "equity_megacap", "xyz:AMZN": "equity_megacap",
    "xyz:GOOGL": "equity_megacap", "xyz:ORCL": "equity_megacap",
    "xyz:COIN": "equity_fintech", "xyz:HOOD": "equity_fintech",
    "xyz:CRCL": "equity_fintech",
    "xyz:TSLA": "equity_auto", "xyz:MSTR": "equity_other",
    "xyz:LITE": "equity_other", "xyz:UNITREE": "equity_other",
    "xyz:DRAM": "equity_other",
}


def family_of(asset: str) -> str:
    """Unknown assets are their own family — never blocked by the cap."""
    return _FAMILIES.get(asset, f"solo:{asset}")
