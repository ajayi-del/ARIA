"""
debug_signing.py — EIP-712 signing hypothesis tester for SoDEX.

Tests 7 different signing approaches against the live SoDEX API using
`updateLeverage` (idempotent — safe to call, doesn't place orders).

A code:0 response means the signature is VALID.
A code:-1 means the signature is still wrong.
Any other non-zero code (e.g. "leverage already set") means signature is valid.

Run: python debug_signing.py
"""

import asyncio
import json
import os
import sys
import time

import certifi
import httpx
from dotenv import load_dotenv
from eth_account import Account
from eth_account.messages import SignableMessage
from web3 import Web3

load_dotenv()

# ── Credentials from ARIA .env ────────────────────────────────────────────────
PRIVATE_KEY = os.getenv("SODEX_PRIVATE_KEY") or os.getenv("PRIVATE_KEY", "")
ACCOUNT_ADDR = os.getenv("SODEX_ACCOUNT_ID") or os.getenv("ACCOUNT_ID", "")

if not PRIVATE_KEY or not ACCOUNT_ADDR:
    print("ERROR: SODEX_PRIVATE_KEY and SODEX_ACCOUNT_ID must be set in .env")
    sys.exit(1)

BASE_URL = "https://mainnet-gw.sodex.dev/api/v1/perps"
CHAIN_ID = 286623

# ── Test payload: updateLeverage(BTC, 5x cross) — idempotent ─────────────────
# Numeric account_id (2905 for ARIA)
NUMERIC_ACCOUNT_ID = 2905

TEST_PARAMS = {
    "accountID": NUMERIC_ACCOUNT_ID,
    "symbolID": 1,         # BTC-USD
    "leverage": 5,
    "marginMode": 2,       # CROSS
}
TEST_ACTION = "updateLeverage"
TEST_ENDPOINT = "/trade/leverage"


def _v_normalize(sig_bytes: bytearray) -> bytearray:
    if sig_bytes[-1] >= 27:
        sig_bytes[-1] -= 27
    return sig_bytes


def _sign(private_key: str, payload_json: bytes, nonce: int, domain_sep: bytes, action_type_hash: bytes) -> str:
    """Core EIP-712 signing: hash payload → structHash → digest → ECDSA."""
    payload_hash = Web3.keccak(payload_json)
    struct_hash = Web3.keccak(action_type_hash + payload_hash + nonce.to_bytes(32, "big"))
    signable = SignableMessage(version=b"\x01", header=domain_sep, body=struct_hash)
    signed = Account.sign_message(signable, private_key)
    sig_bytes = _v_normalize(bytearray(signed.signature))
    return "0x01" + bytes(sig_bytes).hex()


def _make_domain_sep(name: str, version: str, chain_id: int, verifying_contract: bytes) -> bytes:
    domain_type_hash = Web3.keccak(
        text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
    )
    return Web3.keccak(
        domain_type_hash
        + Web3.keccak(text=name)
        + Web3.keccak(text=version)
        + chain_id.to_bytes(32, "big")
        + verifying_contract
    )


def _make_domain_sep_no_verifying(name: str, version: str, chain_id: int) -> bytes:
    """4-field domain (no verifyingContract)."""
    domain_type_hash = Web3.keccak(
        text="EIP712Domain(string name,string version,uint256 chainId)"
    )
    return Web3.keccak(
        domain_type_hash
        + Web3.keccak(text=name)
        + Web3.keccak(text=version)
        + chain_id.to_bytes(32, "big")
    )


# ── Pre-compute domain separators to test ─────────────────────────────────────
HYPOTHESES = []

# A. Current: name="futures", version="1", verifyingContract=address(0), uint64 nonce
_dom_A = _make_domain_sep("futures", "1", CHAIN_ID, b"\x00" * 32)
_ath_uint64 = Web3.keccak(text="ExchangeAction(bytes32 payloadHash,uint64 nonce)")
_ath_uint256 = Web3.keccak(text="ExchangeAction(bytes32 payloadHash,uint256 nonce)")

HYPOTHESES.append({
    "id": "A",
    "desc": 'name="futures", verifyingContract=address(0), uint64 nonce, type-first JSON',
    "domain_sep": _dom_A,
    "action_type_hash": _ath_uint64,
    "json_order": "type_first",
})

# B. Same domain, but uint256 nonce type
HYPOTHESES.append({
    "id": "B",
    "desc": 'name="futures", verifyingContract=address(0), uint256 nonce, type-first JSON',
    "domain_sep": _dom_A,
    "action_type_hash": _ath_uint256,
    "json_order": "type_first",
})

# C. Same domain/action hash, but params-first JSON (Go map alphabetical)
HYPOTHESES.append({
    "id": "C",
    "desc": 'name="futures", verifyingContract=address(0), uint64 nonce, params-first JSON (sort_keys)',
    "domain_sep": _dom_A,
    "action_type_hash": _ath_uint64,
    "json_order": "sort_keys",
})

# D. uint256 + params-first JSON
HYPOTHESES.append({
    "id": "D",
    "desc": 'name="futures", verifyingContract=address(0), uint256 nonce, params-first JSON (sort_keys)',
    "domain_sep": _dom_A,
    "action_type_hash": _ath_uint256,
    "json_order": "sort_keys",
})

# E. 4-field domain (no verifyingContract), uint64
_dom_E = _make_domain_sep_no_verifying("futures", "1", CHAIN_ID)
HYPOTHESES.append({
    "id": "E",
    "desc": 'name="futures", NO verifyingContract (4-field domain), uint64 nonce',
    "domain_sep": _dom_E,
    "action_type_hash": _ath_uint64,
    "json_order": "type_first",
})

# F. 4-field domain, uint256
HYPOTHESES.append({
    "id": "F",
    "desc": 'name="futures", NO verifyingContract (4-field domain), uint256 nonce',
    "domain_sep": _dom_E,
    "action_type_hash": _ath_uint256,
    "json_order": "type_first",
})

# G. name="Exchange" (some exchanges use generic name), address(0), uint64
_dom_G = _make_domain_sep("Exchange", "1", CHAIN_ID, b"\x00" * 32)
HYPOTHESES.append({
    "id": "G",
    "desc": 'name="Exchange", verifyingContract=address(0), uint64 nonce, type-first JSON',
    "domain_sep": _dom_G,
    "action_type_hash": _ath_uint64,
    "json_order": "type_first",
})


async def test_hypothesis(h: dict, api_key_name: str, http: httpx.AsyncClient) -> dict:
    """Send one signed updateLeverage request and return the result."""
    nonce = int(time.time() * 1000)

    full_payload = {"type": TEST_ACTION, "params": TEST_PARAMS}
    if h["json_order"] == "sort_keys":
        payload_json = json.dumps(full_payload, sort_keys=True, separators=(",", ":")).encode()
    else:
        payload_json = json.dumps(full_payload, separators=(",", ":")).encode()

    sig = _sign(PRIVATE_KEY, payload_json, nonce, h["domain_sep"], h["action_type_hash"])

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-API-Key": api_key_name,
        "X-API-Sign": sig,
        "X-API-Nonce": str(nonce),
    }

    try:
        resp = await http.post(
            f"{BASE_URL}{TEST_ENDPOINT}",
            json=TEST_PARAMS,
            headers=headers,
            timeout=8.0,
        )
        data = resp.json()
        return {
            "hypothesis": h["id"],
            "desc": h["desc"],
            "http_status": resp.status_code,
            "code": data.get("code"),
            "msg": data.get("msg") or data.get("message") or data.get("error") or "—",
            "payload_json": payload_json.decode(),
        }
    except Exception as e:
        return {
            "hypothesis": h["id"],
            "desc": h["desc"],
            "http_status": -1,
            "code": -99,
            "msg": str(e),
            "payload_json": payload_json.decode(),
        }


async def resolve_api_key(http: httpx.AsyncClient) -> str:
    """Fetch registered API key name for this private key."""
    signing_addr = Account.from_key(PRIVATE_KEY).address.lower()
    resp = await http.get(
        f"{BASE_URL}/accounts/{ACCOUNT_ADDR}/api-keys",
        timeout=8.0,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"API key list error: {data}")
    for key in data.get("data", []):
        pub = key.get("publicKey") or key.get("public_key") or key.get("address") or ""
        if pub.lower() == signing_addr:
            print(f"  API key name: {key['name']}")
            return key["name"]
    raise RuntimeError(f"No registered API key found for {signing_addr} — keys: {data}")


async def main():
    print("=" * 70)
    print("SoDEX EIP-712 Signing Hypothesis Tester")
    print(f"Endpoint: POST {BASE_URL}{TEST_ENDPOINT}")
    print(f"Payload:  updateLeverage(BTC, 5x cross, accountID={NUMERIC_ACCOUNT_ID})")
    print("=" * 70)

    async with httpx.AsyncClient(verify=certifi.where()) as http:
        print("\nResolving API key name...")
        api_key_name = await resolve_api_key(http)

        print(f"\nRunning {len(HYPOTHESES)} hypotheses...\n")

        results = []
        for h in HYPOTHESES:
            await asyncio.sleep(0.3)   # brief gap between attempts to avoid nonce collisions
            r = await test_hypothesis(h, api_key_name, http)
            results.append(r)
            status = "✓ VALID SIGNATURE" if r["code"] == 0 else (
                "✓ VALID SIG (non-zero biz error)" if r["code"] not in (-1, -99, None) else "✗ SIG FAIL"
            )
            print(f"[{r['hypothesis']}] {status}")
            print(f"     code={r['code']} msg={r['msg']}")
            print(f"     {h['desc']}")
            print()

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    valid = [r for r in results if r["code"] not in (-1, -99, None)]
    if valid:
        print(f"\n✓ Working hypothesis/hypotheses: {[r['hypothesis'] for r in valid]}")
        for r in valid:
            print(f"  [{r['hypothesis']}] {r['desc']}")
            print(f"           payload_json: {r['payload_json']}")
    else:
        print("\n✗ ALL hypotheses failed — signature is wrong in all 7 tested approaches.")
        print("  Next steps:")
        print("  1. Check the verifyingContract address on ValueChain L1 explorer")
        print("     (chain 286623 — look for a deployed 'ExchangeAction' or 'Perps' contract)")
        print("  2. Confirm exact ACTION_TYPE_HASH string with SoDEX team/SDK")
        print("  3. Try name='sodex' or name='SoDEX' (case-sensitive)")


if __name__ == "__main__":
    asyncio.run(main())
