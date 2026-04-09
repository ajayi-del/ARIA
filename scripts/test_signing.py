import asyncio
import os
import sys
from dotenv import load_dotenv

# Add parent directory to path to allow imports from core/execution
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.signer import SoDEXSigner
from execution.nonce_manager import NonceManager
from execution.sodex_client import SoDEXClient
from core.config import Settings

load_dotenv()

async def test():
    config = Settings()
    
    private_key = os.getenv("PRIVATE_KEY")
    account_id = os.getenv("ACCOUNT_ID")
    
    if not private_key or "your_evm" in private_key:
        print("Error: PRIVATE_KEY not set in .env")
        return
        
    signer = SoDEXSigner(
        private_key=private_key,
        chain_id=config.chain_id_testnet,
        app_chain="futures"
    )
    
    # NonceManager requires an identifier (using private_key as in main.py)
    nonce_mgr = NonceManager(private_key)
    client = SoDEXClient(config, signer, nonce_mgr)
    
    print(f"--- ARIA Testnet Verification ---")
    print(f"Mode:   {config.mode}")
    print(f"Wallet: {signer.get_address()}")
    
    try:
        # Test public endpoint
        print("Fetching BTC mark price...")
        price = await client.get_mark_price("BTC")
        print(f"BTC mark price: {price}")
        
        # Test authenticated endpoint
        if account_id and "your_sodex" not in account_id:
            print(f"Fetching balance for Account ID: {account_id}...")
            balance = await client.get_account_balance(account_id)
            print(f"Balance: {balance} USDC")
            print("\n✓ Authenticated signing verified. Ready for testnet.")
        else:
            print("\n! ACCOUNT_ID not set. Skipping authenticated test.")
            print("✓ Public connectivity verified.")
            
    except Exception as e:
        print(f"\n✕ Error during verification: {e}")

if __name__ == "__main__":
    asyncio.run(test())
