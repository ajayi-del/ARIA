import asyncio
import httpx
import structlog
from datetime import datetime
from typing import Optional

logger = structlog.get_logger(__name__)

class AlertSystem:
    """
    Sends alerts to Terminal and Telegram for critical events.
    """
    
    def __init__(self, config):
        self.config = config
        self.bot_token = config.telegram_bot_token
        self.chat_id = config.telegram_chat_id
        self.client = httpx.AsyncClient(timeout=5.0)
        
    async def send(self, message: str, level: str = "INFO"):
        """
        Main entry point for sending alerts.
        Always logs to terminal, sends to telegram if configured.
        """
        # 1. Log to terminal/structlog
        log_func = logger.info if level == "INFO" else logger.warning if level == "WARNING" else logger.error
        log_func("alert", message=message, level=level)
        
        # 2. Send to Telegram if configured
        if self.bot_token and self.chat_id:
            try:
                # Fire and forget telegram message to not block the main loop
                asyncio.create_task(self._send_telegram(message))
            except Exception as e:
                logger.error("telegram_alert_failed", error=str(e))

    async def _send_telegram(self, message: str):
        """
        Low-level telegram sender.
        """
        if not self.bot_token or not self.chat_id:
            return
            
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": f"🤖 ARIA Alert\n\n{message}",
            "parse_mode": "Markdown"
        }
        
        try:
            resp = await self.client.post(url, json=payload)
            if resp.status_code != 200:
                logger.error("telegram_http_error", status=resp.status_code, text=resp.text)
        except Exception as e:
            logger.error("telegram_transport_error", error=str(e))

    def notify_trade_placed(self, symbol: str, side: str, price: float, stop: float, size: float, rr: float):
        msg = (
            f"🚀 *TRADE_PLACED*\n"
            f"ARIA placed *{side.upper()}* {symbol}\n"
            f"Entry: {price:.2f} | Stop: {stop:.2f}\n"
            f"Size: {size:.4f} | R:R {rr:.2f}"
        )
        asyncio.create_task(self.send(msg))

    def notify_trade_closed(self, symbol: str, outcome: str, pnl: float, r_multiple: float, total_pnl: float):
        indicator = "✅" if pnl > 0 else "❌"
        msg = (
            f"{indicator} *TRADE_CLOSED*\n"
            f"ARIA closed {symbol} — *{outcome.upper()}*\n"
            f"P&L: ${pnl:.2f} | R: {r_multiple:.2f}R\n"
            f"Total Session P&L: ${total_pnl:.2f}"
        )
        asyncio.create_task(self.send(msg))

    def notify_tp1_hit(self, symbol: str):
        msg = (
            f"🎯 *TP1_HIT*\n"
            f"TP1 hit on {symbol}\n"
            f"Stop moved to breakeven."
        )
        asyncio.create_task(self.send(msg))

    def notify_stopped_out(self, symbol: str, loss: float):
        msg = (
            f"🛑 *STOPPED_OUT*\n"
            f"Stopped out on {symbol}\n"
            f"Loss: -${abs(loss):.2f} (-1R as expected)"
        )
        asyncio.create_task(self.send(msg))

    def notify_stop_fix_failed(self, symbol: str, error: str):
        msg = (
            f"⚠️ *STOP_FIX_FAILED*\n"
            f"ARIA failed to update stop on {symbol} after 3 retries.\n"
            f"Last error: {error}\n"
            f"Manual intervention may be needed."
        )
        asyncio.create_task(self.send(msg, level="WARNING"))

    def notify_daily_limit_hit(self, pnl: float):
        msg = (
            f"⚠️ *DAILY_LIMIT_HIT*\n"
            f"Daily loss limit hit (${pnl:.2f}).\n"
            f"ARIA pausing until tomorrow."
        )
        asyncio.create_task(self.send(msg, level="WARNING"))

    def notify_balance_floor_hit(self, balance: float):
        msg = (
            f"🚨 *BALANCE_FLOOR_HIT*\n"
            f"Balance below $500 (${balance:.2f}).\n"
            f"ARIA stopped. Manual restart needed."
        )
        asyncio.create_task(self.send(msg, level="ERROR"))

    def notify_arb_opened(self, symbol: str, rate: float, direction: str, amount: float):
        msg = (
            f"⚖️ *ARB_OPENED*\n"
            f"Funding arb opened on {symbol}\n"
            f"Rate: {rate:.4f}% | {direction.upper()}\n"
            f"Capital deployed: ${amount:.2f}"
        )
        asyncio.create_task(self.send(msg))

    def notify_arb_closed(self, symbol: str, collected: float, reason: str):
        msg = (
            f"📦 *ARB_CLOSED*\n"
            f"Funding arb closed on {symbol}\n"
            f"Collected: ${collected:.4f}\n"
            f"Reason: {reason}"
        )
        asyncio.create_task(self.send(msg))

    def notify_leverage_unlocked(self, level: int, trades: int, wr: float, pf: float):
        msg = (
            f"🔓 *LEVERAGE_UNLOCKED*\n"
            f"{trades} trades complete.\n"
            f"Leverage unlocked to {level}x.\n"
            f"Win Rate: {wr*100:.1f}% | PF: {pf:.2f}"
        )
        asyncio.create_task(self.send(msg))

    async def stop(self):
        await self.client.aclose()
