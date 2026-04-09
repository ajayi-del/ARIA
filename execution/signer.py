"""
SoDEX EIP-712 Signer

Handles EIP-712 typed signature creation for all SoDEX write operations.
"""

import json
from typing import Dict, Any
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3
from execution.schemas import PerpsOrderItem


class SoDEXSigner:
    """
    Handles EIP-712 typed signature creation for SoDEX operations.
    """
    
    def __init__(self, private_key: str, chain_id: int, app_chain: str):
        self.private_key = private_key
        self.chain_id = chain_id
        self.app_chain = app_chain  # "spot" or "futures"
        
    def sign_payload(self, payload: Dict[str, Any], nonce: int) -> str:
        """
        Signs payload with EIP-712 structured data hashing.
        """
        
        # 1. Compact JSON of payload
        payload_json = json.dumps(
            payload, separators=(',',':')
        ).encode('utf-8')
        
        # 2. Keccak256 hash
        payload_hash = Web3.keccak(payload_json)
        
        # 3. Build the EIP-712 domain manually
        domain_separator = Web3.keccak(
            Web3.solidity_keccak(
                ['bytes32','bytes32','bytes32','uint256','address'],
                [
                    Web3.keccak(text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
                    Web3.keccak(text=self.app_chain),
                    Web3.keccak(text="1"),
                    self.chain_id,
                    "0x0000000000000000000000000000000000000000"
                ]
            )
        )
        
        # 4. Create the struct hash
        struct_hash = Web3.keccak(
            Web3.solidity_keccak(
                ['bytes32','bytes32','uint64'],
                [
                    Web3.keccak(text="ExchangeAction(bytes32 payloadHash,uint64 nonce)"),
                    payload_hash,
                    nonce
                ]
            )
        )
        
        # 5. Create the final digest
        digest = Web3.keccak(
            Web3.solidity_keccak(
                ['bytes32','bytes32'],
                [domain_separator, struct_hash]
            )
        )
        
        # 6. Use Account.sign_message for the typed hash, then prepend 0x01
        signable_hash = encode_defunct(digest)
        signed = Account.sign_message(signable_hash, self.private_key)
        
        # 7. Prepend 0x01 byte
        sig = signed.signature.hex()
        return "0x01" + sig[2:]
    
    def get_address(self) -> str:
        """
        Returns the EVM address derived from private key.
        Used as account identifier.
        """
        account = Account.from_key(self.private_key)
        return account.address


def build_perps_order_payload(order_item: PerpsOrderItem) -> Dict[str, Any]:
    """
    Builds SoDEX perps order payload with exact field order.
    CRITICAL FIELD ORDER for PerpsOrderItem:
    clOrdID, modifier, side, type, timeInForce,
    price, quantity, funds, stopPrice, stopType,
    triggerType, reduceOnly, positionSide
    
    Fields must appear in this exact order in JSON payload.
    Wrong order = signature verification failure on SoDEX servers.
    """
    return {
        "type": "newOrder",
        "params": {
            "orders": [{
                "clOrdID": order_item.clOrdID,
                "modifier": order_item.modifier,    # 1=post-only limit
                "side": order_item.side,            # 1=buy, 2=sell
                "type": order_item.type,            # 1=market, 2=limit
                "timeInForce": order_item.timeInForce,  # 1=GTC, 2=IOC
                "price": order_item.price,           # DecimalString
                "quantity": order_item.quantity,       # DecimalString
                "funds": order_item.funds,           # DecimalString "0"
                "stopPrice": order_item.stopPrice,     # DecimalString "0"
                "stopType": order_item.stopType,       # 0 if not stop
                "triggerType": order_item.triggerType,   # 0 if not trigger
                "reduceOnly": order_item.reduceOnly,
                "positionSide": order_item.positionSide   # 1=long, 2=short
            }]
        }
    }
