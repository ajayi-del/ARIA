from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

class Settings(BaseSettings):
    # Mode
    mode: Literal["paper", "testnet", "live"] = "paper"

    # Assets
    assets: list[str] = ["BTC", "ETH", "SOL", "XAUT"]

    # SoDEX endpoints (read from env, fallback to defaults)
    testnet_ws_spot: str = "wss://testnet-gw.sodex.dev/ws/spot"
    testnet_ws_perps: str = "wss://testnet-gw.sodex.dev/ws/perps"
    mainnet_ws_spot: str = "wss://mainnet-gw.sodex.dev/ws/spot"
    mainnet_ws_perps: str = "wss://mainnet-gw.sodex.dev/ws/perps"

    # Data settings
    orderbook_max_age_ms: int = 500
    candle_buffer_size: int = 200
    loop_interval_ms: int = 1000

    # Logging & Monitoring
    log_level: str = "INFO"
    log_dir: str = "./logs"
    telegram_bot_token: str = Field(default="", description="Telegram Bot Token")
    telegram_chat_id: str = Field(default="", description="Telegram Chat ID")
    deepseek_api_key: str = Field(default="", description="DeepSeek API Key")

    # Execution layer settings
    private_key: str = Field(default="", description="Private key for EIP-712 signing")
    account_id: str = Field(default="", description="SoDEX account ID")
    chain_id_testnet: int = 138565
    chain_id_mainnet: int = 286623
    live_risk_pct: float = 0.01  # 1% risk per trade in mainnet
    live_min_coherence: int = 5  # Minimum coherence for mainnet
    min_rr_ratio: float = 2.0  # Minimum risk/reward ratio
    default_leverage: int = 4  # Default leverage for mainnet
    arb_capital_pct: float = 0.2  # 20% of balance for arb capital
    live_mode_confirmed: bool = Field(default=False, description="Must be True for live mode")

    # Mainnet Limits
    balance_floor: float = 500.0
    daily_loss_limit_pct: float = 0.03
    max_deployed_pct: float = 0.40

    # Computed properties
    @property
    def ws_spot_url(self) -> str:
        # paper mode uses testnet urls
        if self.mode == "live":
            return self.mainnet_ws_spot
        return self.testnet_ws_spot
    
    @property  
    def ws_perps_url(self) -> str:
        if self.mode == "live":
            return self.mainnet_ws_perps
        return self.testnet_ws_perps

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8"
    )
