"""
SoDEX EIP-712 Signer — ARIA edition.

Signing spec (ValueChain L1, Chain ID 286623):
  1. payloadHash = keccak256(compact_json_bytes)           (full {type,params} wrapper)
  2. structHash   = keccak256(ACTION_TYPE_HASH || payloadHash || ABI32(nonce))
                    ABI32 = uint64 left-padded to 32 bytes (EIP-712, NOT tight)
  3. digest       = keccak256(b"\\x19\\x01" || domainSep || structHash)
  4. sig          = ECDSA.sign(digest, private_key)
  5. typed_sig    = "0x01" + sig_hex   (SoDEX typed-signature prefix)
"""

import json
from typing import Dict, Any

from eth_account import Account
from eth_account.messages import SignableMessage
from web3 import Web3

# ── Domain constants ──────────────────────────────────────────────────────────
_CHAIN_ID = 286623

# ── Static type hashes ───────────────────────────────────────────────────────
_DOMAIN_TYPE_HASH: bytes = Web3.keccak(
    text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)
_ACTION_TYPE_HASH: bytes = Web3.keccak(
    text="ExchangeAction(bytes32 payloadHash,uint64 nonce)"
)

# ── Domain separator — all fields ABI-encoded (32B each) ─────────────────────
_DOMAIN_SEP: bytes = Web3.keccak(
    _DOMAIN_TYPE_HASH                         # bytes32 → 32B as-is
    + Web3.keccak(text="futures")             # string  → keccak256 → 32B
    + Web3.keccak(text="1")                   # string  → keccak256 → 32B
    + _CHAIN_ID.to_bytes(32, "big")           # uint256 → 32B ABI (left-padded)
    + b"\x00" * 32                            # address(0) → 32B ABI (left-padded)
)


class SoDEXSigner:
    """EIP-712 signer for SoDEX perps."""

    def __init__(self, private_key: str, chain_id: int = _CHAIN_ID, app_chain: str = "futures"):
        self.private_key = private_key
        # chain_id / app_chain kept for API compatibility — domain sep is module-level const
        self._chain_id = chain_id
        self._app_chain = app_chain

    def sign_payload(self, payload: Dict[str, Any], nonce: int) -> str:
        """
        EIP-712 sign a SoDEX perps payload.

        Args:
            payload: full payload dict {"type": "newOrder", "params": {...}}
            nonce:   millisecond-precision uint64 nonce

        Returns:
            "0x01<130 hex chars>" for X-API-Sign header.
        """
        # 1. payloadHash = keccak256(compact JSON bytes)
        payload_json: bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        payload_hash: bytes = Web3.keccak(payload_json)

        # 2. structHash — ABI encoding: every field padded to 32B
        struct_encoded: bytes = (
            _ACTION_TYPE_HASH
            + payload_hash
            + nonce.to_bytes(32, "big")        # uint64 → 32B (left-padded)
        )
        struct_hash: bytes = Web3.keccak(struct_encoded)

        # 3. EIP-712 digest = keccak256("\x19\x01" || domainSep || structHash)
        signable = SignableMessage(
            version=b"\x01",
            header=_DOMAIN_SEP,
            body=struct_hash,
        )
        signed = Account.sign_message(signable, self.private_key)

        # 4. Normalise v byte: go-ethereum's crypto.Ecrecover expects v = 0 or 1
        #    (raw secp256k1 recovery ID). Python eth_account produces v = 27 or 28
        #    (Ethereum convention). Subtract 27 if needed so SoDEX can verify.
        sig_bytes = bytearray(signed.signature)
        if sig_bytes[-1] >= 27:
            sig_bytes[-1] -= 27
        # 5. Prepend 0x01 typed-signature prefix
        return "0x01" + bytes(sig_bytes).hex()

    def get_address(self) -> str:
        """Return checksummed EVM address for the private key."""
        return Account.from_key(self.private_key).address


def build_perps_order_payload(order: Dict[str, Any]) -> Dict[str, Any]:
    """
    Wraps an order params dict in the {type, params} envelope for signing.
    Body sent to the API must be params only (not this full payload).
    """
    return {"type": "newOrder", "params": order}
