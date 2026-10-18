"""
orchestrator_receiver.py - Multi-Agent AI Orchestrator Receiver & Listener Interface for Market Sniffer

Accepts, validates, and automatically executes trade consensus signals from:
1. Telegram Group Chat messages and commands (/signal, /execute, /trade, /agent_trade, JSON payloads)
2. Lightweight local HTTP Webhook endpoint (POST /trade or POST /signal)
"""

import asyncio
import html
import json
import logging
import math
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes

import config
from copy_engine import SettingsManager
from jupiter_swap import JupiterSwapEngine
from sniffer import MarketSniffer
from wallet_manager import WalletManager

logger = logging.getLogger("orchestrator_receiver")


@dataclass
class TradeSignal:
    action: str  # "BUY" or "SELL"
    mint: str  # Validated Solana Mint Address
    amount: float  # SOL amount for BUY; percentage (0.1 - 100) for SELL
    symbol: str = ""  # Ticker symbol (e.g., "$BONK")
    slippage_bps: int = 300  # Basis points (10 - 1500)
    reason: str = ""  # Consensus justification / vote tally
    secret: str = ""  # Shared auth secret if provided
    sender_desc: str = ""  # Sender identity for audit logging
    raw_payload: Optional[Dict[str, Any]] = None


class SignalParser:
    """Parses trade execution signals from JSON objects, markdown blocks, or compact text commands."""

    @staticmethod
    def _extract_json_dict(text: str) -> Optional[Dict[str, Any]]:
        """Finds and parses a JSON object from raw text or markdown code blocks."""
        text_clean = text.strip()

        # Direct JSON string
        if text_clean.startswith("{") and text_clean.endswith("}"):
            try:
                data = json.loads(text_clean)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass

        # Markdown ```json ... ``` block
        code_block = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if code_block:
            try:
                data = json.loads(code_block.group(1))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass

        # Any embedded JSON object
        brace_match = re.search(r"(\{[\s\S]*\})", text)
        if brace_match:
            try:
                data = json.loads(brace_match.group(1))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass

        return None

    @classmethod
    def parse(cls, text: str, sender_desc: str = "") -> Tuple[Optional[TradeSignal], Optional[str]]:
        """
        Parses text into a TradeSignal.
        Returns: (signal, error_reason)
        """
        if not text:
            return None, "Empty signal text"

        # 1. Try JSON payload
        json_data = cls._extract_json_dict(text)
        if json_data:
            return cls._parse_json(json_data, sender_desc)

        # 2. Try Command / Tagged Text Format
        return cls._parse_compact_text(text, sender_desc)

    @classmethod
    def _parse_json(cls, data: Dict[str, Any], sender_desc: str = "") -> Tuple[Optional[TradeSignal], Optional[str]]:
        # Map action
        raw_action = str(
            data.get("action") or data.get("type") or data.get("decision") or data.get("order") or ""
        ).strip().upper()
        if raw_action not in ("BUY", "SELL"):
            return None, f"Invalid or missing action '{raw_action}'. Must be 'BUY' or 'SELL'."

        # Map token mint
        raw_mint = str(
            data.get("mint")
            or data.get("token")
            or data.get("ca")
            or data.get("address")
            or data.get("token_address")
            or ""
        ).strip()
        ca = MarketSniffer.extract_solana_address(raw_mint) or raw_mint
        if not MarketSniffer.is_valid_solana_address(ca):
            return None, f"Invalid Solana mint address: '{raw_mint}'"

        if ca == getattr(config, "SOL_MINT", "So11111111111111111111111111111111111111112"):
            return None, "Target mint cannot be native SOL (So11111111111111111111111111111111111111112)."

        # Map amount
        raw_amt = (
            data.get("amount")
            or data.get("amount_sol")
            or data.get("sol_amount")
            or data.get("sol")
            or data.get("pct")
            or data.get("percent")
            or data.get("percentage")
        )
        try:
            amt = float(raw_amt)
            if math.isnan(amt) or math.isinf(amt) or amt <= 0.0:
                raise ValueError()
        except (ValueError, TypeError):
            return None, f"Invalid amount '{raw_amt}'. Must be a positive number."

        # Bounds validation
        if raw_action == "BUY":
            if amt > 1000.0:
                return None, f"Buy amount {amt} SOL exceeds safety maximum of 1,000 SOL."
            if amt < 0.001:
                return None, f"Buy amount {amt} SOL is below minimum trade threshold of 0.001 SOL."
        else:
            if amt > 100.0:
                return None, f"Sell percentage {amt}% cannot exceed 100%."
            if amt <= 0.0:
                return None, "Sell percentage must be greater than 0%."

        # Map slippage
        raw_slip = data.get("slippage_bps") or data.get("slippage")
        default_slippage = getattr(config, "DEFAULT_SLIPPAGE_BPS", 300)
        slippage_bps = default_slippage
        if raw_slip is not None:
            try:
                slip_val = float(raw_slip)
                # If entered as percent e.g. 3.0 -> 300 bps
                if slip_val < 15.0 and isinstance(raw_slip, float):
                    slip_val = slip_val * 100.0
                slippage_bps = max(10, min(int(slip_val), 1500))
            except Exception:
                slippage_bps = default_slippage

        symbol = str(data.get("symbol") or data.get("ticker") or "").strip().upper()
        reason = str(data.get("reason") or data.get("consensus") or data.get("rationale") or "").strip()
        secret = str(data.get("secret") or data.get("api_key") or "").strip()

        sig = TradeSignal(
            action=raw_action,
            mint=ca,
            amount=amt,
            symbol=symbol,
            slippage_bps=slippage_bps,
            reason=reason,
            secret=secret,
            sender_desc=sender_desc,
            raw_payload=data,
        )
        return sig, None

    @classmethod
    def _parse_compact_text(cls, text: str, sender_desc: str = "") -> Tuple[Optional[TradeSignal], Optional[str]]:
        """
        Parses commands like:
        /signal BUY <mint> <amount_sol> [slippage_bps] [secret]
        /signal SELL <mint> <percentage> [slippage_bps] [secret]
        !SIGNAL BUY <mint> <amount>
        [AGENT_SIGNAL] ACTION=BUY MINT=<mint> AMOUNT=<amount>
        """
        line = text.strip()

        # Handle tagged key-value format e.g. ACTION=BUY MINT=... AMOUNT=...
        if "ACTION=" in line.upper() and ("MINT=" in line.upper() or "CA=" in line.upper()):
            kv_pairs = {}
            for match in re.finditer(r"([A-Za-z_]+)\s*=\s*([^\s]+)", line):
                kv_pairs[match.group(1).lower()] = match.group(2)
            return cls._parse_json(kv_pairs, sender_desc)

        # Remove leading trigger prefixes
        prefixes = [
            "/signal",
            "/execute",
            "/trade",
            "/agent_trade",
            "!signal",
            "!execute",
            "[agent_signal]",
            "[orchestrator]",
        ]
        lowered = line.lower()
        matched_prefix = None
        for p in prefixes:
            if lowered.startswith(p):
                matched_prefix = p
                break

        if not matched_prefix:
            return None, "Not an orchestrator signal command."

        params_str = line[len(matched_prefix) :].strip()
        # If parameters start with JSON, recurse
        if params_str.startswith("{"):
            json_data = cls._extract_json_dict(params_str)
            if json_data:
                return cls._parse_json(json_data, sender_desc)

        parts = params_str.split()
        if len(parts) < 3:
            return None, (
                "Invalid signal syntax. Usage: "
                "<code>/signal &lt;BUY|SELL&gt; &lt;mint&gt; &lt;amount_or_pct&gt; [slippage_bps] [secret]</code>"
            )

        action = parts[0].upper()
        if action not in ("BUY", "SELL"):
            return None, f"Invalid action '{action}'. Must be BUY or SELL."

        mint = parts[1].strip()
        ca = MarketSniffer.extract_solana_address(mint) or mint
        if not MarketSniffer.is_valid_solana_address(ca):
            return None, f"Invalid Solana address: '{mint}'"

        if ca == getattr(config, "SOL_MINT", "So11111111111111111111111111111111111111112"):
            return None, "Target mint cannot be native SOL."

        amt_str = parts[2].replace("%", "").strip()
        try:
            amt = float(amt_str)
            if math.isnan(amt) or math.isinf(amt) or amt <= 0.0:
                raise ValueError()
        except ValueError:
            return None, f"Invalid amount '{parts[2]}'."

        if action == "BUY":
            if amt > 1000.0:
                return None, "Buy amount exceeds 1,000 SOL safety cap."
            if amt < 0.001:
                return None, "Buy amount is below minimum 0.001 SOL threshold."
        if action == "SELL":
            if amt > 100.0:
                return None, "Sell percentage cannot exceed 100%."
            if amt <= 0.0:
                return None, "Sell percentage must be greater than 0%."

        default_slippage = getattr(config, "DEFAULT_SLIPPAGE_BPS", 300)
        slippage_bps = default_slippage
        secret = ""
        if len(parts) >= 4:
            if parts[3].isdigit():
                slippage_bps = max(10, min(int(parts[3]), 1500))
                if len(parts) >= 5:
                    secret = parts[4].strip()
            else:
                secret = parts[3].strip()

        sig = TradeSignal(
            action=action,
            mint=ca,
            amount=amt,
            symbol="",
            slippage_bps=slippage_bps,
            reason="Triggered via Telegram Command",
            secret=secret,
            sender_desc=sender_desc,
            raw_payload={"action": action, "mint": ca, "amount": amt},
        )
        return sig, None


class SecurityValidator:
    """Verifies that an incoming signal comes from an authorized admin, orchestrator bot, or group."""

    @staticmethod
    def is_authorized(
        user_id: int,
        chat_id: int,
        payload_secret: str = "",
    ) -> Tuple[bool, str]:
        """
        Validates authorization.
        Returns: (is_authorized, reason)
        """
        admin_id = getattr(config, "ADMIN_USER_ID", 0)
        orch_secret = getattr(config, "ORCHESTRATOR_SECRET", "")
        orch_chat_id = getattr(config, "ORCHESTRATOR_CHAT_ID", 0)
        orch_user_ids = getattr(config, "ORCHESTRATOR_USER_IDS", set())

        # 1. Admin is unconditionally authorized
        if admin_id and user_id == admin_id:
            return True, "Admin authorized"

        # 2. Shared secret check (constant-time comparison)
        if orch_secret and payload_secret:
            if secrets.compare_digest(payload_secret.strip(), orch_secret):
                return True, "Shared secret key authorized"

        # 3. Designated Group Chat check
        if orch_chat_id and chat_id == orch_chat_id:
            # If specific orchestrator user/bot IDs are configured, sender must be one of them
            if orch_user_ids:
                if user_id in orch_user_ids:
                    return True, "Authorized orchestrator bot in designated group chat"
                return False, f"Sender user ID {user_id} not in authorized ORCHESTRATOR_USER_IDS"
            # Otherwise, any member/bot in the designated group chat is accepted
            return True, "Designated group chat authorized"

        # 4. Authorized Orchestrator user/bot ID check
        if orch_user_ids and user_id in orch_user_ids:
            return True, "Authorized orchestrator bot user ID"

        return (
            False,
            "Unauthorized sender: This bot only accepts execution signals from the Bot Admin, "
            "designated ORCHESTRATOR_CHAT_ID, authorized ORCHESTRATOR_USER_IDS, or with a valid ORCHESTRATOR_SECRET.",
        )


class SignalExecutor:
    """Executes trade signals on-chain using the Admin's / Orchestrator's keypair and logs results."""

    @staticmethod
    async def execute(
        signal: TradeSignal,
        user_id_override: Optional[int] = None,
    ) -> Tuple[bool, str, Optional[Dict[str, Any]], float]:
        """
        Executes a TradeSignal.
        Returns: (success, sig_or_err, quote_dict, elapsed_seconds)
        """
        start_time = time.time()
        admin_id = getattr(config, "ADMIN_USER_ID", 0)
        exec_id = getattr(config, "ORCHESTRATOR_EXECUTION_USER_ID", 0)
        exec_user_id = user_id_override or exec_id or admin_id

        if not exec_user_id:
            return False, "No execution user ID or ADMIN_USER_ID configured.", None, 0.0

        fee_reserve = getattr(config, "FEE_RESERVE_SOL", 0.005)

        # Import lock from bot to ensure global thread/trade safety
        from bot import UserTradeLock

        async with UserTradeLock(exec_user_id) as acquired:
            if not acquired:
                return (
                    False,
                    "Trade in progress for this wallet. Previous transaction is still confirming.",
                    None,
                    time.time() - start_time,
                )

            keypair = WalletManager.get_keypair(exec_user_id)
            if not keypair:
                # Attempt to generate or load
                await asyncio.to_thread(WalletManager.get_or_create_wallet, exec_user_id)
                keypair = WalletManager.get_keypair(exec_user_id)
                if not keypair:
                    return False, "Could not load Solana keypair for execution wallet.", None, time.time() - start_time

            pubkey = str(keypair.pubkey())

            # BUY Execution
            if signal.action == "BUY":
                current_bal = await asyncio.to_thread(WalletManager.get_sol_balance, pubkey)
                needed = signal.amount + fee_reserve
                if current_bal < needed:
                    return (
                        False,
                        f"Insufficient SOL balance. Available: {current_bal:.4f} SOL, "
                        f"Needed: {needed:.4f} SOL (includes {fee_reserve} SOL fee reserve).",
                        None,
                        time.time() - start_time,
                    )

                logger.info(
                    f"Executing Orchestrator BUY: {signal.amount} SOL -> {signal.mint} (slippage: {signal.slippage_bps} bps)"
                )
                success, sig_or_err, quote = await asyncio.to_thread(
                    JupiterSwapEngine.execute_buy,
                    keypair,
                    signal.mint,
                    signal.amount,
                    slippage_bps=signal.slippage_bps,
                )

                if success:
                    # Update entry price for position tracker
                    try:
                        m_data = await asyncio.to_thread(MarketSniffer.get_market_data, signal.mint)
                        if m_data and m_data.get("price_usd"):
                            u_settings = SettingsManager.get_user_settings(exec_user_id)
                            e_prices = u_settings.get("entry_prices", {})
                            e_prices[signal.mint] = m_data["price_usd"]
                            SettingsManager.update_user_settings(exec_user_id, {"entry_prices": e_prices})
                    except Exception:
                        pass

                duration = time.time() - start_time
                return success, sig_or_err, quote, duration

            # SELL Execution
            elif signal.action == "SELL":
                ui_bal, raw_bal = await asyncio.to_thread(WalletManager.get_token_balance, pubkey, signal.mint)
                if raw_bal <= 0:
                    return (
                        False,
                        f"Wallet holds 0 balance for token {signal.mint[:6]}...{signal.mint[-4:]}.",
                        None,
                        time.time() - start_time,
                    )

                logger.info(
                    f"Executing Orchestrator SELL: {signal.amount}% of {signal.mint} (slippage: {signal.slippage_bps} bps)"
                )
                success, sig_or_err, quote = await asyncio.to_thread(
                    JupiterSwapEngine.execute_sell,
                    keypair,
                    signal.mint,
                    percentage=signal.amount,
                    slippage_bps=signal.slippage_bps,
                )

                if success and signal.amount >= 100.0:
                    try:
                        u_settings = SettingsManager.get_user_settings(exec_user_id)
                        e_prices = u_settings.get("entry_prices", {})
                        if signal.mint in e_prices:
                            del e_prices[signal.mint]
                            SettingsManager.update_user_settings(exec_user_id, {"entry_prices": e_prices})
                    except Exception:
                        pass

                duration = time.time() - start_time
                return success, sig_or_err, quote, duration

            else:
                return False, f"Unsupported action: {signal.action}", None, time.time() - start_time


# -----------------------------------------------------------------------------
# FEEDBACK MESSAGE FORMATTERS
# -----------------------------------------------------------------------------
def format_signal_ack(signal: TradeSignal) -> str:
    """Builds initial Telegram acknowledgement message while trade is routing."""
    ticker_display = f" (<b>{html.escape(signal.symbol)}</b>)" if signal.symbol else ""
    reason_display = (
        f"\n💬 <b>Consensus:</b> <i>{html.escape(signal.reason)}</i>\n" if signal.reason else ""
    )

    if signal.action == "BUY":
        action_text = f"🟢 <b>BUY</b> <code>{signal.amount:.4f} SOL</code>"
    else:
        action_text = f"🔴 <b>SELL</b> <code>{signal.amount:.1f}%</code>"

    return (
        "🤖 <b>AI Orchestrator Signal Received!</b>\n\n"
        f"🎯 <b>Action:</b> {action_text}\n"
        f"🪙 <b>Token:</b> <code>{signal.mint}</code>{ticker_display}\n"
        f"🛡️ <b>Slippage:</b> <code>{signal.slippage_bps / 100:.1f}%</code>"
        f"{reason_display}\n"
        "⏳ <i>Routing and signing swap via Jupiter Aggregator...</i>"
    )


def format_signal_result(
    signal: TradeSignal,
    success: bool,
    sig_or_err: str,
    quote: Optional[Dict[str, Any]],
    duration: float,
) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    """Builds confirmation message and interactive controls after execution."""
    ticker_display = f" (<b>{html.escape(signal.symbol)}</b>)" if signal.symbol else ""

    if success:
        if signal.action == "BUY":
            out_amount_str = ""
            if quote and quote.get("outAmount"):
                out_amount_str = f"\n📦 <b>Tokens Received:</b> <code>{quote.get('outAmount')}</code> (raw)"

            msg = (
                "🎉 <b>AI ORCHESTRATOR TRADE EXECUTED!</b>\n\n"
                "✅ <b>Status:</b> Confirmed on Solana\n"
                f"🎯 <b>Action:</b> 🟢 BUY <code>{signal.amount:.4f} SOL</code>\n"
                f"🪙 <b>Token:</b> <code>{signal.mint}</code>{ticker_display}"
                f"{out_amount_str}\n"
                f"⚡ <b>Execution Speed:</b> <code>{duration:.2f}s</code>\n"
                f"🔗 <a href='https://solscan.io/tx/{sig_or_err}'>View Transaction on Solscan</a>\n\n"
                "<i>Position is now tracked in your wallet portfolio.</i>"
            )
        else:
            received_sol = 0.0
            if quote and quote.get("outAmount"):
                received_sol = int(quote["outAmount"]) / 1_000_000_000.0

            msg = (
                "🎉 <b>AI ORCHESTRATOR SELL EXECUTED!</b>\n\n"
                "✅ <b>Status:</b> Confirmed on Solana\n"
                f"🎯 <b>Action:</b> 🔴 SELL <code>{signal.amount:.1f}%</code>\n"
                f"🪙 <b>Token:</b> <code>{signal.mint}</code>{ticker_display}\n"
                f"💰 <b>SOL Received:</b> <code>~{received_sol:.4f} SOL</code>\n"
                f"⚡ <b>Execution Speed:</b> <code>{duration:.2f}s</code>\n"
                f"🔗 <a href='https://solscan.io/tx/{sig_or_err}'>View Transaction on Solscan</a>"
            )

        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔴 Sell 50%", callback_data=f"sell:{signal.mint}:50"),
                InlineKeyboardButton("🔴 Sell 100%", callback_data=f"sell:{signal.mint}:100"),
            ],
            [
                InlineKeyboardButton("🎒 Positions", callback_data="positions_menu"),
                InlineKeyboardButton("🔎 Resniff", callback_data=f"refresh:{signal.mint}"),
            ],
        ])
        return msg, markup

    else:
        err_clean = html.escape(str(sig_or_err))
        msg = (
            "❌ <b>AI Orchestrator Trade Failed</b>\n\n"
            f"🎯 <b>Action:</b> {signal.action} {signal.amount}\n"
            f"🪙 <b>Token:</b> <code>{signal.mint}</code>{ticker_display}\n"
            f"⚠️ <b>Reason:</b> <i>{err_clean}</i>\n"
            f"⚡ <b>Elapsed:</b> <code>{duration:.2f}s</code>"
        )
        return msg, None


# -----------------------------------------------------------------------------
# TELEGRAM HANDLERS
# -----------------------------------------------------------------------------
async def orchestrator_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles commands: /signal, /execute, /trade, /agent_trade
    Triggered by AI group chat orchestrators or the Bot Admin.
    """
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id if update.effective_user else 0
    chat_id = update.effective_chat.id if update.effective_chat else 0
    text = update.message.text.strip()

    # Pre-parse payload to extract any supplied secret
    signal, parse_err = SignalParser.parse(text, sender_desc=f"User {user_id} in Chat {chat_id}")
    secret = signal.secret if signal else ""

    # Authorization Check
    is_auth, auth_reason = SecurityValidator.is_authorized(user_id, chat_id, payload_secret=secret)
    if not is_auth:
        logger.warning(f"Unauthorized orchestrator signal attempt by user={user_id} in chat={chat_id}: {text}")
        await update.message.reply_text(
            f"⛔ <b>Access Denied:</b> {html.escape(auth_reason)}",
            parse_mode=ParseMode.HTML,
        )
        return

    if not signal:
        await update.message.reply_text(
            f"⚠️ <b>Invalid Signal Format:</b>\n<i>{html.escape(parse_err or 'Unknown parsing error')}</i>\n\n"
            "<b>Supported JSON Format:</b>\n"
            "<code>/signal {\"action\": \"BUY\", \"mint\": \"...\", \"amount\": 0.2, \"symbol\": \"BONK\"}</code>\n\n"
            "<b>Supported Compact Format:</b>\n"
            "<code>/signal BUY &lt;mint&gt; &lt;amount_sol&gt; [slippage_bps]</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    # Acknowledge immediately to the group chat
    ack_text = format_signal_ack(signal)
    status_msg = await update.message.reply_text(
        ack_text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

    # Execute trade
    success, sig_or_err, quote, duration = await SignalExecutor.execute(signal)

    # Send final feedback
    result_text, markup = format_signal_result(signal, success, sig_or_err, quote, duration)
    try:
        await status_msg.edit_text(
            result_text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    except Exception:
        await update.message.reply_text(
            result_text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            disable_web_page_preview=True,
        )


async def try_process_group_signal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Called from `handle_message` when a plain text message arrives.
    Checks if message is an automated consensus signal from the orchestrator.
    Returns True if the message was processed as an orchestrator signal, False otherwise.
    """
    if not update.message or not update.message.text:
        return False

    user_id = update.effective_user.id if update.effective_user else 0
    chat_id = update.effective_chat.id if update.effective_chat else 0
    text = update.message.text.strip()

    orch_chat_id = getattr(config, "ORCHESTRATOR_CHAT_ID", 0)
    orch_user_ids = getattr(config, "ORCHESTRATOR_USER_IDS", set())

    # Fast heuristics: only parse if in designated chat or has signal keywords
    is_orch_chat = (orch_chat_id and chat_id == orch_chat_id)
    is_orch_user = (orch_user_ids and user_id in orch_user_ids)
    has_keywords = any(k in text.lower() for k in ("!signal", "[agent_signal]", "[orchestrator]", '"action":', '"mint":'))

    if not (is_orch_chat or is_orch_user or has_keywords):
        return False

    signal, _ = SignalParser.parse(text, sender_desc=f"Group Chat {chat_id}")
    if not signal:
        return False

    # Verify authorization
    is_auth, _ = SecurityValidator.is_authorized(user_id, chat_id, payload_secret=signal.secret)
    if not is_auth:
        return False

    # Process signal
    ack_text = format_signal_ack(signal)
    status_msg = await update.message.reply_text(
        ack_text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

    success, sig_or_err, quote, duration = await SignalExecutor.execute(signal)
    result_text, markup = format_signal_result(signal, success, sig_or_err, quote, duration)
    try:
        await status_msg.edit_text(
            result_text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    except Exception:
        await update.message.reply_text(
            result_text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            disable_web_page_preview=True,
        )

    return True


# -----------------------------------------------------------------------------
# LIGHTWEIGHT LOCAL HTTP WEBHOOK SERVER
# -----------------------------------------------------------------------------
class OrchestratorHttpServer:
    """
    Lightweight local HTTP webhook listener allowing orchestrators running locally
    or on internal networks to dispatch trade execution signals via HTTP POST.
    """

    def __init__(self, application: Application):
        self.application = application
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None
        self._cached_bal: float = 0.0
        self._cached_bal_time: float = 0.0

    async def handle_trade_post(self, request: web.Request) -> web.Response:
        """Endpoint: POST /trade or POST /signal"""
        try:
            body = await request.json()
            if not isinstance(body, dict):
                return web.json_response({"error": "Payload must be a JSON object."}, status=400)
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        # Header or payload secret check
        header_secret = request.headers.get("X-Orchestrator-Secret", "").strip()
        payload_secret = str(body.get("secret", "")).strip()
        secret = header_secret or payload_secret

        client_ip = request.remote or "unknown"
        is_local = client_ip in ("127.0.0.1", "::1", "localhost")

        orch_secret = getattr(config, "ORCHESTRATOR_SECRET", "")
        # Security check
        if orch_secret:
            if not secret or not secrets.compare_digest(secret, orch_secret):
                return web.json_response({"error": "Unauthorized: Invalid orchestrator secret key."}, status=401)
        elif not is_local:
            return web.json_response({"error": "Unauthorized: Remote access prohibited without ORCHESTRATOR_SECRET configured."}, status=403)

        signal, parse_err = SignalParser.parse(json.dumps(body), sender_desc=f"HTTP Webhook ({client_ip})")
        if not signal:
            return web.json_response({"error": f"Invalid signal parameters: {parse_err}"}, status=422)

        # Notify Telegram group or Admin that HTTP signal arrived
        orch_chat_id = getattr(config, "ORCHESTRATOR_CHAT_ID", 0)
        admin_id = getattr(config, "ADMIN_USER_ID", 0)
        target_chat = orch_chat_id or admin_id
        tg_status_msg = None
        if target_chat and self.application and getattr(self.application, "bot", None):
            try:
                ack_text = format_signal_ack(signal)
                tg_status_msg = await self.application.bot.send_message(
                    chat_id=target_chat,
                    text=f"🌐 <i>[HTTP Webhook Trigger]</i>\n{ack_text}",
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.warning(f"Could not send Telegram HTTP signal ack to chat {target_chat}: {e}")

        # Execute trade
        success, sig_or_err, quote, duration = await SignalExecutor.execute(signal)

        # Update Telegram notification
        if tg_status_msg and target_chat and self.application and getattr(self.application, "bot", None):
            try:
                result_text, markup = format_signal_result(signal, success, sig_or_err, quote, duration)
                await tg_status_msg.edit_text(
                    f"🌐 <i>[HTTP Webhook Result]</i>\n{result_text}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.warning(f"Could not update Telegram status msg: {e}")

        resp_payload = {
            "success": success,
            "action": signal.action,
            "mint": signal.mint,
            "amount": signal.amount,
            "symbol": signal.symbol,
            "tx_hash": sig_or_err if success else None,
            "solscan_url": f"https://solscan.io/tx/{sig_or_err}" if success else None,
            "error": None if success else sig_or_err,
            "duration_seconds": round(duration, 3),
        }
        return web.json_response(resp_payload, status=200 if success else 500)

    async def handle_health_get(self, request: web.Request) -> web.Response:
        """Endpoint: GET /health or GET /status"""
        admin_id = getattr(config, "ADMIN_USER_ID", 0)
        exec_id = getattr(config, "ORCHESTRATOR_EXECUTION_USER_ID", 0)
        exec_user = exec_id or admin_id
        pubkey = WalletManager.get_public_key(exec_user)
        now = time.time()
        if (now - self._cached_bal_time) < 10.0:
            sol_bal = self._cached_bal
        else:
            self._cached_bal_time = now
            sol_bal = await asyncio.to_thread(WalletManager.get_sol_balance, pubkey) if pubkey else 0.0
            self._cached_bal = sol_bal

        orch_secret = getattr(config, "ORCHESTRATOR_SECRET", "")
        orch_chat_id = getattr(config, "ORCHESTRATOR_CHAT_ID", 0)
        orch_user_ids = getattr(config, "ORCHESTRATOR_USER_IDS", set())

        return web.json_response({
            "status": "online",
            "bot": "MarketSniffer",
            "execution_user_id": exec_user,
            "execution_wallet": pubkey,
            "sol_balance": round(sol_bal, 4),
            "orchestrator_chat_id": orch_chat_id,
            "auth_configured": bool(orch_secret or orch_chat_id or orch_user_ids),
        })

    async def start(self):
        """Starts the aiohttp web server"""
        host = getattr(config, "ORCHESTRATOR_HTTP_HOST", "127.0.0.1")
        port = getattr(config, "ORCHESTRATOR_HTTP_PORT", 8088)

        app = web.Application()
        app.router.add_post("/trade", self.handle_trade_post)
        app.router.add_post("/signal", self.handle_trade_post)
        app.router.add_get("/health", self.handle_health_get)
        app.router.add_get("/status", self.handle_health_get)

        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, host, port)
        await self.site.start()
        logger.info(f"🚀 Orchestrator HTTP Webhook listener running at http://{host}:{port}")

    async def stop(self):
        """Cleanly stops the aiohttp web server"""
        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()
        logger.info("Orchestrator HTTP Webhook listener stopped.")


_GLOBAL_HTTP_SERVER: Optional[OrchestratorHttpServer] = None


async def start_orchestrator_http_server(application: Application):
    """Initializes and runs the HTTP webhook server if enabled"""
    global _GLOBAL_HTTP_SERVER
    enabled = getattr(config, "ORCHESTRATOR_HTTP_ENABLED", True)
    if not enabled:
        logger.info("Orchestrator HTTP listener is disabled via configuration.")
        return

    port = getattr(config, "ORCHESTRATOR_HTTP_PORT", 8088)
    try:
        _GLOBAL_HTTP_SERVER = OrchestratorHttpServer(application)
        await _GLOBAL_HTTP_SERVER.start()
    except Exception as e:
        logger.error(f"Failed to start Orchestrator HTTP server on port {port}: {e}", exc_info=True)


async def stop_orchestrator_http_server():
    """Stops the global HTTP webhook server"""
    global _GLOBAL_HTTP_SERVER
    if _GLOBAL_HTTP_SERVER:
        try:
            await _GLOBAL_HTTP_SERVER.stop()
        except Exception as e:
            logger.warning(f"Error stopping Orchestrator HTTP server: {e}")
        _GLOBAL_HTTP_SERVER = None
