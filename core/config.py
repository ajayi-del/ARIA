from typing import Literal, Dict, Any
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

class Settings(BaseSettings):
    # Mode
    mode: Literal["paper", "testnet", "live"] = "paper"
    data_source: Literal["synthetic", "testnet", "live", "binance", "bybit", "sodex"] = "synthetic"

    # Assets
    assets: list[str] = ["BTC-USD", "ETH-USD", "SOL-USD", "XAUT-USD", "BNB-USD", "LINK-USD", "AVAX-USD", "USTECH100-USD"]

    ASSET_CONFIG: Dict[str, Dict[str, Any]] = {
        "BTC-USD":  {
            "tick_size": 0.5,
            "min_size": 0.001,
            "max_leverage": 25,
            "category": "large_cap",
            "market_hours": "24h"
        },
        "ETH-USD":  {
            "tick_size": 0.05,
            "min_size": 0.01,
            "max_leverage": 20,
            "category": "large_cap",
            "market_hours": "24h"
        },
        "SOL-USD":  {
            "tick_size": 0.01,
            "min_size": 0.1,
            "max_leverage": 20,
            "category": "alt_l1",
            "market_hours": "24h"
        },
        "XAUT-USD": {
            "tick_size": 0.1,
            "min_size": 0.001,
            "max_leverage": 25,
            "category": "commodity",
            "market_hours": "gold_hours"
        },
        "BNB-USD":  {
            "tick_size": 0.01,
            "min_size": 0.01,
            "max_leverage": 20,
            "category": "cex_ecosystem",
            "market_hours": "24h"
        },
        "LINK-USD": {
            "tick_size": 0.001,
            "min_size": 0.1,
            "max_leverage": 20,
            "category": "defi_infra",
            "market_hours": "24h"
        },
        "AVAX-USD": {
            "tick_size": 0.01,
            "min_size": 0.1,
            "max_leverage": 20,
            "category": "alt_l1",
            "market_hours": "24h"
        },
        "USTECH100-USD": {
            "tick_size": 1.0,
            "min_size": 0.01,
            "max_leverage": 10,
            "category": "index",
            "market_hours": "ustech_hours"
        }
    }

    # SoDEX endpoints (read from env, fallback to defaults)
    testnet_ws_spot: str = "wss://testnet-gw.sodex.dev/ws/spot"
    testnet_ws_perps: str = "wss://testnet-gw.sodex.dev/ws/perps"
    mainnet_ws_spot: str = "wss://mainnet-gw.sodex.dev/ws/spot"
    mainnet_ws_perps: str = "wss://mainnet-gw.sodex.dev/ws/perps"

    # Data settings
    orderbook_max_age_ms: int = 500
    candle_buffer_size: int = 200
    loop_interval_ms: int = 1000
    
    # REST Endpoints (for symbol discovery / depth)
    testnet_rest_url: str = "https://testnet-gw.sodex.dev/api/v1"
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
    chain_id_testnet: int = 138565
    chain_id_mainnet: int = 286623

    # Bybit Credentials
    bybit_api_key: str = ""
    bybit_api_secret: str = ""
    bybit_testnet: bool = False

    live_risk_pct: float = 0.01  # 1% risk per trade in mainnet
    live_min_coherence: int = 5  # Minimum coherence for mainnet
    min_rr_ratio: float = 2.0  # Minimum risk/reward ratio
    default_leverage: int = 4  # Default leverage for mainnet
    arb_capital_pct: float = 0.2  # 20% of balance for arb capital
    live_mode_confirmed: bool = Field(default=False, description="Must be True for live mode")
    paper_starting_balance: float = 10000.0

    # Mainnet Limits
    balance_floor: float = 50.0          # Minimum account balance to permit trading
    daily_loss_limit_pct: float = 0.03
    max_deployed_pct: float = 0.40
    min_trade_notional_usd: float = 10.0  # Skip trades below this notional
    max_trade_notional_usd: float = 500.0 # Hard cap on single trade notional

    # Fallback/Legacy Aliases (for Pydantic validation)
    risk_pct: float = 0.01
    min_coherence: int = 4

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

    # Legacy properties
    @property
    def ws_spot_url(self) -> str:
        source = self.data_source
        if source == "live":
            return self.mainnet_ws_spot
        return self.testnet_ws_spot
    
    @property  
    def ws_perps_url(self) -> str:
        source = self.data_source
        if source == "live":
            return self.mainnet_ws_perps
        return self.testnet_ws_perps

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )
