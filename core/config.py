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

    # Logging
    log_level: str = "INFO"
    log_dir: str = "./logs"

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
