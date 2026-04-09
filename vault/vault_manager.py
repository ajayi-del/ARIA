import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import List, Dict, Optional
from pathlib import Path

@dataclass
class Depositor:
    wallet: str
    deposited_usdc: float
    share_pct: float
    entry_nav: float
    deposited_at: str
    telegram_id: Optional[str] = None

class VaultManager:
    """
    Tracks depositors, shares, and vault NAV.
    Persists to logs/vault.json
    """
    
    def __init__(self, log_dir: str = "./logs"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)
        self.vault_file = self.log_dir / "vault.json"
        self.depositors: List[Depositor] = []
        self.total_deposited = 0.0
        self.high_water_mark = 0.0
        
    def add_depositor(self, wallet: str, amount: float, current_nav: float) -> str:
        """
        Add a new depositor to the vault.
        Calculates share percentage based on contribution.
        """
        # Calculate entry NAV (current total value / total shares)
        # For simplicity, if first depositor, entry_nav is 1.0
        entry_nav = current_nav if current_nav > 0 else 1.0
        
        # Calculate share percentage
        # share = amount / (total_deposited + amount)
        self.total_deposited += amount
        share_pct = (amount / self.total_deposited) * 100
        
        # Update existing shares
        for d in self.depositors:
            d.share_pct = (d.deposited_usdc / self.total_deposited) * 100
            
        new_depositor = Depositor(
            wallet=wallet,
            deposited_usdc=amount,
            share_pct=share_pct,
            entry_nav=entry_nav,
            deposited_at=datetime.now(timezone.utc).isoformat()
        )
        
        self.depositors.append(new_depositor)
        self.save()
        return f"DEP_{wallet[:6]}"

    def get_total_nav(self, current_balance: float) -> float:
        """
        Current total vault value in USDC.
        """
        return current_balance

    def get_share_value(self, wallet: str, current_nav: float) -> float:
        """
        Current value of a depositor's share.
        """
        for d in self.depositors:
            if d.wallet == wallet:
                # Value = (share_pct / 100) * total_nav
                return (d.share_pct / 100) * current_nav
        return 0.0

    def generate_report(self, wallet: str, current_nav: float) -> Optional[Dict]:
        """
        Returns full report for a depositor.
        """
        for d in self.depositors:
            if d.wallet == wallet:
                current_val = self.get_share_value(wallet, current_nav)
                pnl = current_val - d.deposited_usdc
                pnl_pct = (pnl / d.deposited_usdc) * 100 if d.deposited_usdc > 0 else 0
                
                return {
                    "wallet": d.wallet,
                    "deposit_amount": d.deposited_usdc,
                    "current_value": current_val,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "share_pct": d.share_pct,
                    "entry_date": d.deposited_at,
                    "entry_nav": d.entry_nav
                }
        return None

    def save(self):
        data = {
            "total_deposited": self.total_deposited,
            "high_water_mark": self.high_water_mark,
            "depositors": [asdict(d) for d in self.depositors]
        }
        with open(self.vault_file, 'w') as f:
            json.dump(data, f, indent=2)

    def load(self):
        if self.vault_file.exists():
            try:
                with open(self.vault_file, 'r') as f:
                    data = json.load(f)
                    self.total_deposited = data.get("total_deposited", 0.0)
                    self.high_water_mark = data.get("high_water_mark", 0.0)
                    self.depositors = [Depositor(**d) for d in data.get("depositors", [])]
            except (json.JSONDecodeError, FileNotFoundError):
                pass
