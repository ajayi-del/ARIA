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
    # wave 3 (2026-11-08): fx / indices / rates / wider equities /
    # commodities / pre-IPO — same doctrine, families = shared risk factor
    # in a selloff, one representative per direction.
    "xyz:EUR": "fx", "xyz:JPY": "fx", "xyz:GBP": "fx", "xyz:DXY": "fx",
    "xyz:KRW": "fx",
    "xyz:JP225": "index", "xyz:KR200": "index", "xyz:NIFTY": "index",
    "xyz:MAGS": "index", "xyz:IBOV": "index", "xyz:EWJ": "index",
    "xyz:EWT": "index", "xyz:EWY": "index", "xyz:EWZ": "index",
    "xyz:TLT": "rates",
    "xyz:AVGO": "equity_semis", "xyz:QCOM": "equity_semis",
    "xyz:MU": "equity_semis", "xyz:INTC": "equity_semis",
    "xyz:MRVL": "equity_semis", "xyz:AMAT": "equity_semis",
    "xyz:LRCX": "equity_semis", "xyz:ASML": "equity_semis",
    "xyz:SNDK": "equity_semis", "xyz:WDC": "equity_semis",
    "xyz:STX": "equity_semis", "xyz:UMC": "equity_semis",
    "xyz:ARM": "equity_semis", "xyz:AAOI": "equity_semis",
    "xyz:SOXL": "equity_semis", "xyz:SMH": "equity_semis",
    "xyz:NFLX": "equity_megacap", "xyz:IBM": "equity_megacap",
    "xyz:COST": "equity_megacap", "xyz:LLY": "equity_megacap",
    "xyz:NOW": "equity_megacap",
    "xyz:SNOW": "equity_sw", "xyz:SHOP": "equity_sw",
    "xyz:NET": "equity_sw", "xyz:CRWD": "equity_sw", "xyz:ZM": "equity_sw",
    "xyz:TWST": "equity_sw", "xyz:DELL": "equity_sw", "xyz:ACN": "equity_sw",
    "xyz:PLTR": "equity_ai", "xyz:IREN": "equity_ai",
    "xyz:NBIS": "equity_ai", "xyz:CRWV": "equity_ai",
    "xyz:GME": "equity_consumer", "xyz:RDDT": "equity_consumer",
    "xyz:SNAP": "equity_consumer", "xyz:RIVN": "equity_consumer",
    "xyz:MRNA": "equity_consumer", "xyz:HIMS": "equity_consumer",
    "xyz:DKNG": "equity_consumer", "xyz:EBAY": "equity_consumer",
    "xyz:MELI": "equity_consumer", "xyz:TTWO": "equity_consumer",
    "xyz:NOK": "equity_consumer",
    "xyz:BABA": "equity_china",
    "xyz:BMNR": "equity_fintech", "xyz:NU": "equity_fintech",
    "xyz:BX": "equity_fintech",
    "xyz:CVX": "energy", "xyz:XLE": "energy",
    "xyz:URANIUM": "uranium", "xyz:URNM": "uranium",
    "xyz:PLATINUM": "metal", "xyz:PALLADIUM": "metal",
    "xyz:ALUMINIUM": "metal",
    "xyz:NATGAS": "energy", "xyz:HO": "energy", "xyz:TTF": "energy",
    "xyz:WHEAT": "ags", "xyz:CORN": "ags",
    "xyz:MINIMAX": "pre_ipo_ai", "xyz:ZHIPU": "pre_ipo_ai",
    "xyz:H100": "pre_ipo_ai", "xyz:CAMBRICON": "pre_ipo_ai",
    "xyz:CXMT": "pre_ipo_ai", "xyz:INNOLIGHT": "pre_ipo_ai",
    "xyz:IBIDEN": "pre_ipo_ai", "xyz:KIOXIA": "pre_ipo_ai",
    "xyz:GIGADEV": "pre_ipo_ai", "xyz:YMTC": "pre_ipo_ai",
    "xyz:SHEIN": "pre_ipo", "xyz:OURA": "pre_ipo",
    "xyz:RELIANCE": "pre_ipo", "xyz:JIO": "pre_ipo",
    "xyz:SOFTBANK": "pre_ipo", "xyz:CATL": "pre_ipo",
    "xyz:HYUNDAI": "pre_ipo",
}


def family_of(asset: str) -> str:
    """Unknown assets are their own family — never blocked by the cap."""
    return _FAMILIES.get(asset, f"solo:{asset}")


def family_assets() -> tuple[str, ...]:
    """Stable tuple of every asset the rotation map knows — boot provision
    iterates this (sleeve-filtered) so wave-3 names get venue leverage pins
    without duplicating the map into `_LEV_OVERRIDES`."""
    return tuple(_FAMILIES)
