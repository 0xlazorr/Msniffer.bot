import os
import sys
import html
import time
import socket
import logging
from typing import Dict, Any, Optional

# Force IPv4 resolution to eliminate broken IPv6 route timeouts on Linux
orig_getaddrinfo = socket.getaddrinfo
def getaddrinfo_ipv4(host, port, family=0, type=0, proto=0, flags=0):
    return orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
socket.getaddrinfo = getaddrinfo_ipv4

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

import asyncio
import math

from config import BOT_TOKEN, FEE_RESERVE_SOL, ADMIN_USER_ID, USDT_MINT, DEFAULT_SLIPPAGE_BPS
from wallet_manager import WalletManager
from sniffer import MarketSniffer, format_currency
from jupiter_swap import JupiterSwapEngine
from copy_engine import SettingsManager, CopyTradingEngine
from ai_engine import AITradingEngine

# Logging configuration
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# State tracking for user inputs (custom buy, withdraw, custom swap, import wallet, etc.)
USER_STATES: Dict[int, Dict[str, Any]] = {}

# Active admin ID
ACTIVE_ADMIN_ID = ADMIN_USER_ID


def parse_positive_float(val: str, max_val: float = 1_000_000.0) -> Optional[float]:
    """Strictly parses positive floats, rejecting NaN, Inf, zero, negatives, and overflow"""
    try:
        amt = float(val.strip().replace(",", ""))
        if math.isnan(amt) or math.isinf(amt) or amt <= 0.0 or amt > max_val:
            return None
        return amt
    except Exception:
        return None


def format_main_menu(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Builds clean, simple introduction and main dashboard with active mode badge"""
    pubkey, _ = WalletManager.get_or_create_wallet(user_id)
    sol_bal = WalletManager.get_sol_balance(pubkey)
    usdt_bal = WalletManager.get_usdt_balance(pubkey)

    settings = SettingsManager.get_user_settings(user_id)
    mode = settings.get("mode", "MANUAL")

    if mode == "COPY":
        target = settings.get("target_wallet", "")
        target_display = f"<code>{target[:6]}...{target[-4:]}</code>" if target else "<i>(target not set)</i>"
        mode_badge = f"🎯 <b>Copy Trading</b> ({target_display})"
    elif mode == "AI":
        strategy = settings.get("ai_strategy", "BALANCED")
        mode_badge = f"🤖 <b>AI Autopilot</b> (<code>{strategy}</code>)"
    else:
        mode_badge = "⏸️ <b>Manual Sniper</b>"

    text = (
        "🐕🔎 <b>Market Sniffer</b>\n"
        "<i>Solana Sniper, Copy & AI Auto-Trader</i>\n\n"
        "Paste any <b>Token Contract Address (CA)</b> to audit safety and trade instantly.\n\n"
        "📬 <b>Your Trading Wallet:</b>\n"
        f"<code>{pubkey}</code> <i>(tap to copy)</i>\n\n"
        f"💰 <b>Balance:</b> <code>{sol_bal:.4f} SOL</code> | <code>{usdt_bal:.2f} USDT</code>\n"
        f"🎮 <b>Active Mode:</b> {mode_badge}\n"
    )

    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎯 Copy Trading", callback_data="copy_menu"),
            InlineKeyboardButton("🤖 AI Autopilot", callback_data="ai_menu"),
        ],
        [
            InlineKeyboardButton("⚡ Swap SOL ⇄ USDT", callback_data="swap_menu"),
            InlineKeyboardButton("🎒 My Positions", callback_data="positions_menu"),
        ],
        [
            InlineKeyboardButton("💳 Wallet & Funds", callback_data="wallet_menu"),
            InlineKeyboardButton("🔄 Refresh", callback_data="refresh_balance"),
        ]
    ])
    return text, markup


def format_swap_menu(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Builds the SOL <-> USDT quick swap interface"""
    pubkey = WalletManager.get_public_key(user_id)
    sol_bal = WalletManager.get_sol_balance(pubkey)
    usdt_bal = WalletManager.get_usdt_balance(pubkey)

    text = (
        "⚡ <b>Quick Swap (SOL ⇄ USDT)</b>\n\n"
        f"💰 <b>SOL Balance:</b> <code>{sol_bal:.4f} SOL</code>\n"
        f"💵 <b>USDT Balance:</b> <code>{usdt_bal:.2f} USDT</code>\n\n"
        "Choose your swap direction below:"
    )

    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Swap SOL ➡️ USDT", callback_data="swap_sol_to_usdt_menu"),
        ],
        [
            InlineKeyboardButton("🔄 Swap USDT ➡️ SOL", callback_data="swap_usdt_to_sol_menu"),
        ],
        [
            InlineKeyboardButton("🔙 Back to Home", callback_data="home_menu"),
        ]
    ])
    return text, markup


def format_copy_menu(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Builds Copy Trading Dashboard"""
    settings = SettingsManager.get_user_settings(user_id)
    mode = settings.get("mode", "MANUAL")
    is_active = (mode == "COPY")
    target = settings.get("target_wallet", "")
    target_str = f"<code>{target}</code>" if target else "<i>None (Tap button below to set)</i>"
    amount = settings.get("copy_buy_amount_sol", 0.1)
    copy_sells = settings.get("copy_sells", True)
    slippage = settings.get("slippage_bps", 300) / 100.0

    status_str = "🟢 <b>ACTIVE (Copying Target)</b>" if is_active else "⏸️ <b>PAUSED (Manual Mode)</b>"

    text = (
        "🎯 <b>COPY TRADING DASHBOARD</b>\n"
        "<i>Automatically mirror on-chain whale & insider trades</i>\n\n"
        f"📊 <b>Engine Status:</b> {status_str}\n"
        f"👤 <b>Target Wallet to Copy:</b>\n{target_str}\n\n"
        f"💸 <b>Copied Buy Size:</b> <code>{amount:.2f} SOL</code>\n"
        f"🔴 <b>Mirror Target Sells:</b> {'🟢 Yes (Auto-Sell 100%)' if copy_sells else '⚪ No (Manual Sell)'}\n"
        f"⚡ <b>Slippage Tolerance:</b> <code>{slippage:.1f}%</code>\n\n"
        "💡 <i>When active, the bot detects new swap transactions by the target wallet on Raydium, Pump.fun, or Jupiter and automatically executes the mirrored trade for you.</i>"
    )

    toggle_btn_text = "⏸️ Pause Copy Mode" if is_active else "▶️ Activate Copy Mode"
    toggle_sells_text = "🔴 Auto-Sells: ON" if copy_sells else "⚪ Auto-Sells: OFF"

    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(toggle_btn_text, callback_data="toggle_copy_mode"),
        ],
        [
            InlineKeyboardButton("👤 Set Target Wallet", callback_data="prompt_set_target_wallet"),
            InlineKeyboardButton("💸 Set Buy Amount", callback_data="prompt_set_copy_amount"),
        ],
        [
            InlineKeyboardButton(toggle_sells_text, callback_data="toggle_copy_sells"),
            InlineKeyboardButton("⚡ Slippage", callback_data="prompt_copy_slippage"),
        ],
        [
            InlineKeyboardButton("🔙 Back to Home", callback_data="home_menu"),
        ]
    ])
    return text, markup


def format_ai_menu(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Builds AI Autopilot Dashboard"""
    settings = SettingsManager.get_user_settings(user_id)
    mode = settings.get("mode", "MANUAL")
    is_active = (mode == "AI")
    strategy = settings.get("ai_strategy", "BALANCED")
    trade_amt = settings.get("ai_trade_amount_sol", 0.1)
    tp_pct = settings.get("ai_take_profit_pct", 30.0)
    sl_pct = settings.get("ai_stop_loss_pct", 15.0)

    ai_status = AITradingEngine.get_status()
    status_str = "🟢 <b>ACTIVE (AI Autopilot Engaged)</b>" if is_active else "⏸️ <b>PAUSED</b>"

    text = (
        "🤖 <b>AI AUTONOMOUS TRADING DASHBOARD</b>\n"
        "<i>Autonomous market analysis, sniper execution & risk exit management</i>\n\n"
        f"📊 <b>Engine Status:</b> {status_str}\n"
        f"🧠 <b>AI Core:</b> <code>{ai_status['description']}</code>\n"
        f"🎯 <b>Strategy:</b> <b>{strategy}</b>\n"
        f"💸 <b>Max Trade Size:</b> <code>{trade_amt:.2f} SOL</code>\n"
        f"📈 <b>Take Profit:</b> <code>+{tp_pct:.0f}%</code> | <b>Stop Loss:</b> <code>-{sl_pct:.0f}%</code>\n\n"
        "🤖 <b>Strategy Profiles:</b>\n"
        "• <b>Balanced:</b> High-momentum breakouts with verified liquidity & revoked authorities.\n"
        "• <b>Degen:</b> Aggressive pump.fun curve volume scalping.\n"
        "• <b>Conservative:</b> Ultra-safe $50K+ liquidity with zero warnings.\n\n"
        "💡 <i>You can add your OpenAI, Grok, or Claude API key in <code>.env</code> anytime for custom LLM decision making.</i>"
    )

    toggle_btn_text = "⏸️ Pause AI Mode" if is_active else "▶️ Activate AI Mode"

    strat_buttons = [
        InlineKeyboardButton(("✅ " if strategy == "BALANCED" else "") + "Balanced", callback_data="set_ai_strat:BALANCED"),
        InlineKeyboardButton(("✅ " if strategy == "DEGEN" else "") + "Degen", callback_data="set_ai_strat:DEGEN"),
        InlineKeyboardButton(("✅ " if strategy == "CONSERVATIVE" else "") + "Conservative", callback_data="set_ai_strat:CONSERVATIVE"),
    ]

    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(toggle_btn_text, callback_data="toggle_ai_mode"),
        ],
        strat_buttons,
        [
            InlineKeyboardButton("💸 Set Trade Size", callback_data="prompt_set_ai_amount"),
            InlineKeyboardButton("📈 Set TP / SL", callback_data="prompt_set_ai_tpsl"),
        ],
        [
            InlineKeyboardButton("🔙 Back to Home", callback_data="home_menu"),
        ]
    ])
    return text, markup


def format_positions_menu(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Builds portfolio and token positions dashboard"""
    pubkey = WalletManager.get_public_key(user_id)
    if not pubkey:
        pubkey, _ = WalletManager.get_or_create_wallet(user_id)

    sol_bal = WalletManager.get_sol_balance(pubkey)
    all_positions = WalletManager.get_all_positions(pubkey)

    # Filter out dust (ui_amount < 0.000001)
    active_positions = [p for p in all_positions if p.get("ui_amount", 0.0) > 0.000001]

    lines = [
        "🎒 <b>MY PORTFOLIO & POSITIONS</b>\n",
        f"📬 <b>Wallet:</b> <code>{pubkey}</code>",
        f"💰 <b>SOL Balance:</b> <code>{sol_bal:.4f} SOL</code>\n",
    ]

    keyboard = []

    if not active_positions:
        lines.append("<i>You currently hold no open SPL token positions.</i>")
        lines.append("\n💡 <i>Paste any Token CA or DexScreener link to sniff and buy!</i>")
    else:
        lines.append("📊 <b>Open Token Holdings:</b>")
        for pos in active_positions[:6]:
            mint = pos["mint"]
            ui_amt = pos["ui_amount"]

            market_info = MarketSniffer.get_market_data(mint)
            symbol = market_info.get("symbol", "TOKEN") if market_info else "TOKEN"
            price_usd = market_info.get("price_usd", 0.0) if market_info else 0.0
            val_usd = ui_amt * price_usd

            sym_esc = html.escape(symbol)
            val_str = f" (~${val_usd:,.2f})" if val_usd > 0 else ""
            lines.append(
                f"• <b>${sym_esc}</b>: <code>{ui_amt:,.4f}</code>{val_str}\n  <code>{mint}</code>"
            )

            keyboard.append([
                InlineKeyboardButton(f"🔴 Sell 50% ${sym_esc}", callback_data=f"sell:{mint}:50"),
                InlineKeyboardButton(f"🔴 Sell 100% ${sym_esc}", callback_data=f"sell:{mint}:100"),
            ])
            keyboard.append([
                InlineKeyboardButton(f"🔎 Sniff ${sym_esc}", callback_data=f"refresh:{mint}"),
            ])

    keyboard.append([
        InlineKeyboardButton("🔄 Refresh Positions", callback_data="positions_menu"),
        InlineKeyboardButton("🔙 Back to Home", callback_data="home_menu"),
    ])

    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


def build_audit_markup(
    mint: str,
    pair_url: str,
    has_token_balance: bool = False
) -> InlineKeyboardMarkup:
    """Builds interactive buy/sell buttons for a token audit"""
    keyboard = [
        [
            InlineKeyboardButton("⚡ Buy 0.1 SOL", callback_data=f"buy:{mint}:0.1"),
            InlineKeyboardButton("⚡ Buy 0.25 SOL", callback_data=f"buy:{mint}:0.25"),
        ],
        [
            InlineKeyboardButton("⚡ Buy 0.5 SOL", callback_data=f"buy:{mint}:0.5"),
            InlineKeyboardButton("⚡ Buy 1.0 SOL", callback_data=f"buy:{mint}:1.0"),
        ],
        [
            InlineKeyboardButton("✏️ Custom Buy", callback_data=f"custom_buy:{mint}"),
            InlineKeyboardButton("🤖 AI Scan", callback_data=f"ai_scan:{mint}"),
            InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh:{mint}"),
        ],
    ]

    if has_token_balance:
        keyboard.append([
            InlineKeyboardButton("🔴 Sell 25%", callback_data=f"sell:{mint}:25"),
            InlineKeyboardButton("🔴 Sell 50%", callback_data=f"sell:{mint}:50"),
            InlineKeyboardButton("🔴 Sell 100%", callback_data=f"sell:{mint}:100"),
        ])

    keyboard.append([
        InlineKeyboardButton("📈 DexScreener", url=pair_url),
        InlineKeyboardButton("❌ Close", callback_data="close"),
    ])

    return InlineKeyboardMarkup(keyboard)


def format_audit_message(report: dict, sol_balance: float, token_balance: float = 0.0) -> str:
    """Formats comprehensive market and safety audit card"""
    mint = report["mint"]
    market = report["market"]
    sec = report["security"]

    name_esc = html.escape(market["name"])
    symbol_esc = html.escape(market["symbol"])

    lines = [
        "🐕🔎 <b>MARKET SNIFFER AUDIT</b>\n",
        f"🪙 <b>{name_esc}</b> (<code>${symbol_esc}</code>)",
        f"📝 <code>{mint}</code>\n",
        "📊 <b>Market Metrics:</b>",
        f"• <b>Price:</b> {format_currency(market['price_usd'])} ({market['price_native']:.8f} SOL)",
        f"• <b>Market Cap:</b> {format_currency(market['fdv'])}",
        f"• <b>Liquidity:</b> {format_currency(market['liquidity_usd'])} ({market['dex']})",
        f"• <b>Changes:</b> 5m: <code>{market['m5']:+.1f}%</code> | 1h: <code>{market['h1']:+.1f}%</code> | 24h: <code>{market['h24']:+.1f}%</code>\n",
        "🛡️ <b>Security & Honeypot Sniffer:</b>",
        f"• <b>Freeze Authority:</b> {'🟢 REVOKED (Safe)' if sec['freeze_authority'] == 'REVOKED' else '🚨 ACTIVE (DANGER: Can freeze wallet!)'}",
        f"• <b>Mint Authority:</b> {'🟢 REVOKED (Safe)' if sec['mint_authority'] == 'REVOKED' else '🚨 ACTIVE (DANGER: Infinite mint!)'}",
        f"• <b>Top 10 Concentration:</b> {sec['top10_pct']:.1f}%",
        f"• <b>Safety Rating:</b> <b>{sec['risk_level']}</b>",
    ]

    if sec["warnings"]:
        lines.append("\n⚠️ <b>Detected Warnings:</b>")
        for w in sec["warnings"]:
            lines.append(f"• {html.escape(w)}")

    lines.append(f"\n💳 <b>Your SOL Balance:</b> <code>{sol_balance:.4f} SOL</code>")
    if token_balance > 0:
        lines.append(f"🎒 <b>You Own:</b> <code>{token_balance:,.2f} ${symbol_esc}</code>")

    lines.append("\n<i>Tap a button below to execute instant swap:</i>")
    return "\n".join(lines)


# ----------------------------------------------------
# COMMAND HANDLERS
# ----------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /start command"""
    user_id = update.effective_user.id
    USER_STATES.pop(user_id, None)
    text, markup = format_main_menu(user_id)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def swap_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /swap command"""
    user_id = update.effective_user.id
    USER_STATES.pop(user_id, None)
    text, markup = format_swap_menu(user_id)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /positions command"""
    user_id = update.effective_user.id
    USER_STATES.pop(user_id, None)
    text, markup = format_positions_menu(user_id)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /cancel command"""
    user_id = update.effective_user.id
    USER_STATES.pop(user_id, None)
    text, markup = format_main_menu(user_id)
    await update.message.reply_text("❌ Action cancelled.\n\n" + text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def copy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /copy command"""
    user_id = update.effective_user.id
    USER_STATES.pop(user_id, None)
    text, markup = format_copy_menu(user_id)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /ai command"""
    user_id = update.effective_user.id
    USER_STATES.pop(user_id, None)
    text, markup = format_ai_menu(user_id)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def import_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /import command"""
    if update.effective_chat.type != "private":
        await update.message.reply_text("⚠️ For your security, wallet import can only be done in a direct private 1-on-1 chat with the bot.")
        return
    user_id = update.effective_user.id
    USER_STATES[user_id] = {"type": "import_wallet"}
    prompt_text = (
        "📥 <b>IMPORT EXISTING WALLET</b>\n\n"
        "Reply with your Phantom or Solflare <b>Base58 Private Key</b> (or JSON byte array):\n\n"
        "🔒 <b>Security Notice:</b>\n"
        "• Your key is encrypted with AES-256 Fernet immediately upon receipt.\n"
        "• The bot will automatically attempt to delete your message from Telegram.\n"
        "• Type /cancel anytime to abort."
    )
    await update.message.reply_text(prompt_text, parse_mode=ParseMode.HTML)


async def wallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays user wallet details, balance, export PK, and withdraw options"""
    if update.effective_chat.type != "private":
        await update.message.reply_text("⚠️ For your security, wallet details and keys can only be managed in direct private chat with the bot.")
        return

    user_id = update.effective_user.id
    USER_STATES.pop(user_id, None)
    pubkey, _ = WalletManager.get_or_create_wallet(user_id)
    sol_bal = WalletManager.get_sol_balance(pubkey)
    usdt_bal = WalletManager.get_usdt_balance(pubkey)

    msg_text = (
        "💳 <b>Market Sniffer Wallet</b>\n\n"
        "📬 <b>Your Public Address:</b>\n"
        f"<code>{pubkey}</code> <i>(tap to copy)</i>\n\n"
        f"💰 <b>SOL Balance:</b> <code>{sol_bal:.4f} SOL</code>\n"
        f"💵 <b>USDT Balance:</b> <code>{usdt_bal:.2f} USDT</code>\n\n"
        "📥 <b>Deposit:</b> Send SOL or USDT directly to the address above.\n"
        "📤 <b>Withdraw:</b> Tap <b>Withdraw SOL</b> to send funds to any wallet.\n"
        "🔑 <b>Import:</b> Tap <b>Import Wallet</b> to connect an existing Phantom key.\n"
        "🔐 <b>Export:</b> Tap <b>Export Private Key</b> to backup your wallet."
    )

    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="refresh_balance"),
            InlineKeyboardButton("💸 Withdraw SOL", callback_data="withdraw_prompt"),
        ],
        [
            InlineKeyboardButton("📥 Import Wallet (Phantom / Solflare)", callback_data="import_wallet_prompt"),
        ],
        [
            InlineKeyboardButton("🔑 Export Private Key", callback_data="export_pk_confirm"),
            InlineKeyboardButton("🔄 Generate New Wallet", callback_data="reset_wallet_prompt"),
        ],
        [
            InlineKeyboardButton("🔙 Back to Home", callback_data="home_menu"),
        ]
    ])

    if update.callback_query:
        await update.callback_query.edit_message_text(msg_text, parse_mode=ParseMode.HTML, reply_markup=markup)
    else:
        await update.message.reply_text(msg_text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def alpha_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Exclusive Admin Command: View & Manage the permanent Alpha Wallet
    Collects automatic gas fee deposits from bot trades.
    """
    global ACTIVE_ADMIN_ID
    # Strictly disallow running /alpha in group chats
    if update.effective_chat.type != "private":
        return

    user_id = update.effective_user.id

    # Auto-claim admin role on first call if ADMIN_USER_ID is not configured
    if ACTIVE_ADMIN_ID == 0:
        ACTIVE_ADMIN_ID = user_id
        try:
            import re
            from config import BASE_DIR
            env_file = BASE_DIR / ".env"
            if env_file.exists():
                txt = env_file.read_text()
                if "ADMIN_USER_ID=" in txt:
                    txt = re.sub(r"ADMIN_USER_ID=\d+", f"ADMIN_USER_ID={user_id}", txt)
                else:
                    txt += f"\nADMIN_USER_ID={user_id}\n"
                env_file.write_text(txt)
        except Exception:
            pass

    if user_id != ACTIVE_ADMIN_ID:
        # Silently ignore unauthorized users
        return

    USER_STATES.pop(user_id, None)
    alpha_data = WalletManager.get_or_create_alpha_wallet()
    alpha_pub = alpha_data["public_key"]
    balance = WalletManager.get_sol_balance(alpha_pub)

    text = (
        "👑 <b>ALPHA WALLET DASHBOARD</b>\n"
        "<i>Permanent fee accumulator wallet</i>\n\n"
        "📬 <b>Alpha Address:</b>\n"
        f"<code>{alpha_pub}</code> <i>(tap to copy)</i>\n\n"
        f"💰 <b>Accumulated Gas Fee Deposits:</b> <code>{balance:.5f} SOL</code>\n\n"
        "🔒 This wallet receives the automatic gas fee percentage on all trades. "
        "It is completely isolated from user trading wallets."
    )

    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Refresh Balance", callback_data="alpha_refresh"),
            InlineKeyboardButton("💸 Withdraw to Personal Phantom", callback_data="alpha_withdraw_prompt"),
        ],
        [
            InlineKeyboardButton("🔑 Export Alpha Key", callback_data="alpha_export_pk"),
            InlineKeyboardButton("❌ Close", callback_data="close"),
        ]
    ])

    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


# ----------------------------------------------------
# MESSAGE & TEXT HANDLER
# ----------------------------------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes incoming text: Solana CAs, cancel commands, or pending state inputs"""
    user_id = update.effective_user.id
    text = update.message.text.strip()

    # 1. Handle explicit cancel
    if text.lower() in ("/cancel", "cancel", "stop", "exit"):
        USER_STATES.pop(user_id, None)
        text_out, markup_out = format_main_menu(user_id)
        await update.message.reply_text("❌ Action cancelled.\n\n" + text_out, parse_mode=ParseMode.HTML, reply_markup=markup_out)
        return

    # 2. Extract potential Solana address
    extracted_ca = MarketSniffer.extract_solana_address(text)

    # 3. State machine (waiting for user inputs)
    if user_id in USER_STATES:
        state = USER_STATES[user_id]
        state_type = state.get("type")

        # If user was in a numeric prompt but pasted a new CA, discard prompt and sniff CA
        if extracted_ca and state_type in ("custom_buy", "custom_sol_to_usdt", "custom_usdt_to_sol"):
            USER_STATES.pop(user_id, None)
            # Will proceed to sniffing extracted_ca below
        elif state_type == "custom_buy":
            mint = state["mint"]
            del USER_STATES[user_id]
            amount = parse_positive_float(text, max_val=500.0)
            if amount is None:
                await update.message.reply_text("❌ Please enter a valid SOL amount (e.g. 0.35). Type /cancel to abort.")
                return
            await execute_buy_action(update, context, user_id, mint, amount)
            return

        # Withdraw
        elif state_type == "withdraw":
            del USER_STATES[user_id]
            parts = text.split()
            if len(parts) < 1:
                await update.message.reply_text("Usage: <code>&lt;destination_address&gt; [amount_or_all]</code>", parse_mode=ParseMode.HTML)
                return

            dest_addr = parts[0].strip()
            if not MarketSniffer.is_valid_solana_address(dest_addr):
                await update.message.reply_text("❌ Invalid destination Solana address.")
                return

            pubkey = WalletManager.get_public_key(user_id)
            balance = WalletManager.get_sol_balance(pubkey)
            if balance <= FEE_RESERVE_SOL:
                await update.message.reply_text(f"❌ Insufficient balance to cover gas fee reserve ({balance:.4f} SOL).")
                return

            if len(parts) >= 2 and parts[1].lower() != "all":
                withdraw_amount = parse_positive_float(parts[1], max_val=balance)
                if withdraw_amount is None:
                    await update.message.reply_text("❌ Invalid withdrawal amount.")
                    return
            else:
                withdraw_amount = balance - FEE_RESERVE_SOL

            if withdraw_amount <= 0 or withdraw_amount > (balance - FEE_RESERVE_SOL):
                await update.message.reply_text(f"❌ Max withdrawable amount is {balance - FEE_RESERVE_SOL:.4f} SOL.")
                return

            status_msg = await update.message.reply_text(f"⏳ <i>Withdrawing {withdraw_amount:.4f} SOL to {dest_addr[:6]}...{dest_addr[-4:]}...</i>", parse_mode=ParseMode.HTML)
            kp = WalletManager.get_keypair(user_id)
            success, sig_or_err = WalletManager.transfer_sol(kp, dest_addr, withdraw_amount)

            if success:
                await status_msg.edit_text(
                    f"✅ <b>Withdrawal Successful!</b>\n\n"
                    f"💸 Sent <code>{withdraw_amount:.4f} SOL</code>\n"
                    f"📬 To: <code>{dest_addr}</code>\n\n"
                    f"🔗 <a href='https://solscan.io/tx/{sig_or_err}'>View on Solscan</a>",
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            else:
                await status_msg.edit_text(f"❌ Withdrawal failed: {html.escape(sig_or_err)}")
            return

        # Alpha Wallet Withdraw
        elif state_type == "alpha_withdraw":
            del USER_STATES[user_id]
            if user_id != ACTIVE_ADMIN_ID:
                return

            parts = text.split()
            dest_addr = parts[0].strip()
            if not MarketSniffer.is_valid_solana_address(dest_addr):
                await update.message.reply_text("❌ Invalid Solana address.")
                return

            alpha_data = WalletManager.get_or_create_alpha_wallet()
            balance = WalletManager.get_sol_balance(alpha_data["public_key"])
            if balance <= FEE_RESERVE_SOL:
                await update.message.reply_text(f"❌ Insufficient Alpha balance ({balance:.4f} SOL).")
                return

            withdraw_amount = balance - FEE_RESERVE_SOL
            if len(parts) >= 2 and parts[1].lower() != "all":
                amt_parsed = parse_positive_float(parts[1], max_val=balance)
                if amt_parsed is not None:
                    withdraw_amount = amt_parsed

            kp = WalletManager.get_alpha_keypair()
            status_msg = await update.message.reply_text(f"⏳ <i>Sending {withdraw_amount:.4f} SOL from Alpha wallet...</i>", parse_mode=ParseMode.HTML)
            success, sig_or_err = WalletManager.transfer_sol(kp, dest_addr, withdraw_amount)
            if success:
                await status_msg.edit_text(
                    f"👑 <b>Alpha Funds Withdrawn!</b>\n\n"
                    f"💸 Sent <code>{withdraw_amount:.4f} SOL</code>\n"
                    f"📬 To: <code>{dest_addr}</code>\n\n"
                    f"🔗 <a href='https://solscan.io/tx/{sig_or_err}'>View on Solscan</a>",
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            else:
                await status_msg.edit_text(f"❌ Alpha withdrawal failed: {html.escape(sig_or_err)}")
            return

        # Custom SOL to USDT
        elif state_type == "custom_sol_to_usdt":
            del USER_STATES[user_id]
            amt = parse_positive_float(text, max_val=500.0)
            if amt is None:
                await update.message.reply_text("❌ Please enter a valid positive SOL amount.")
                return
            await execute_sol_to_usdt_action(update, context, user_id, amt)
            return

        # Custom USDT to SOL
        elif state_type == "custom_usdt_to_sol":
            del USER_STATES[user_id]
            amt = parse_positive_float(text, max_val=50000.0)
            if amt is None:
                await update.message.reply_text("❌ Please enter a valid positive USDT amount.")
                return
            await execute_usdt_to_sol_action(update, context, user_id, amt)
            return

        # Import Existing Wallet
        elif state_type == "import_wallet":
            del USER_STATES[user_id]
            try:
                # Security: Delete raw private key message from chat immediately
                await update.message.delete()
            except Exception:
                pass

            success, pubkey_imported, msg = WalletManager.import_wallet(user_id, text)
            if success:
                sol_bal = WalletManager.get_sol_balance(pubkey_imported)
                usdt_bal = WalletManager.get_usdt_balance(pubkey_imported)
                succ_text = (
                    "🎉 <b>WALLET IMPORTED SUCCESSFULLY!</b>\n\n"
                    "📬 <b>Connected Address:</b>\n"
                    f"<code>{pubkey_imported}</code> <i>(tap to copy)</i>\n\n"
                    f"💰 <b>Balance:</b> <code>{sol_bal:.4f} SOL</code> | <code>{usdt_bal:.2f} USDT</code>\n\n"
                    "🔒 <i>Your private key is secured with AES-256 encryption. Your Telegram message containing the key was deleted automatically.</i>"
                )
                _, markup = format_main_menu(user_id)
                await update.effective_chat.send_message(succ_text, parse_mode=ParseMode.HTML, reply_markup=markup)
            else:
                await update.effective_chat.send_message(f"❌ <b>Import Failed:</b> {html.escape(msg)}\n\nTry again via /import or type /cancel to abort.", parse_mode=ParseMode.HTML)
            return

        # Set Target Wallet for Copy Trading
        elif state_type == "set_target_wallet":
            del USER_STATES[user_id]
            target_addr = MarketSniffer.extract_solana_address(text) or text.strip()
            if not MarketSniffer.is_valid_solana_address(target_addr):
                await update.message.reply_text("❌ Invalid Solana wallet address. Please provide a valid Base58 address.")
                return

            SettingsManager.update_user_settings(user_id, {"target_wallet": target_addr, "last_seen_sig": ""})
            await update.message.reply_text(f"✅ <b>Target Wallet Set!</b>\n<code>{target_addr}</code>\n\nReturning to Copy Trading menu...", parse_mode=ParseMode.HTML)
            text_out, markup_out = format_copy_menu(user_id)
            await update.message.reply_text(text_out, parse_mode=ParseMode.HTML, reply_markup=markup_out)
            return

        # Set Copy Buy Amount
        elif state_type == "set_copy_amount":
            del USER_STATES[user_id]
            amt = parse_positive_float(text, max_val=100.0)
            if amt is None:
                await update.message.reply_text("❌ Please enter a valid SOL amount (e.g. <code>0.2</code>).")
                return

            SettingsManager.update_user_settings(user_id, {"copy_buy_amount_sol": amt})
            await update.message.reply_text(f"✅ <b>Copy Buy Size set to {amt} SOL!</b>", parse_mode=ParseMode.HTML)
            text_out, markup_out = format_copy_menu(user_id)
            await update.message.reply_text(text_out, parse_mode=ParseMode.HTML, reply_markup=markup_out)
            return

        # Set Copy Slippage
        elif state_type == "set_copy_slippage":
            del USER_STATES[user_id]
            pct = parse_positive_float(text, max_val=50.0)
            if pct is None:
                await update.message.reply_text("❌ Please enter a valid slippage percentage (e.g. <code>3.5</code>).")
                return

            bps = int(pct * 100)
            SettingsManager.update_user_settings(user_id, {"slippage_bps": bps})
            await update.message.reply_text(f"✅ <b>Slippage set to {pct:.1f}% ({bps} BPS)!</b>", parse_mode=ParseMode.HTML)
            text_out, markup_out = format_copy_menu(user_id)
            await update.message.reply_text(text_out, parse_mode=ParseMode.HTML, reply_markup=markup_out)
            return

        # Set AI Trade Amount
        elif state_type == "set_ai_amount":
            del USER_STATES[user_id]
            amt = parse_positive_float(text, max_val=100.0)
            if amt is None:
                await update.message.reply_text("❌ Please enter a valid SOL amount (e.g. <code>0.1</code>).")
                return

            SettingsManager.update_user_settings(user_id, {"ai_trade_amount_sol": amt})
            await update.message.reply_text(f"✅ <b>AI Trade Size set to {amt} SOL!</b>", parse_mode=ParseMode.HTML)
            text_out, markup_out = format_ai_menu(user_id)
            await update.message.reply_text(text_out, parse_mode=ParseMode.HTML, reply_markup=markup_out)
            return

        # Set AI TP / SL
        elif state_type == "set_ai_tpsl":
            del USER_STATES[user_id]
            parts = text.split()
            if len(parts) < 2:
                await update.message.reply_text("Usage: <code>&lt;take_profit_pct&gt; &lt;stop_loss_pct&gt;</code> (e.g. <code>40 15</code>)", parse_mode=ParseMode.HTML)
                return

            tp = parse_positive_float(parts[0], max_val=1000.0)
            sl = parse_positive_float(parts[1], max_val=99.0)
            if tp is None or sl is None:
                await update.message.reply_text("❌ Invalid TP or SL percentages.")
                return

            SettingsManager.update_user_settings(user_id, {"ai_take_profit_pct": tp, "ai_stop_loss_pct": sl})
            await update.message.reply_text(f"✅ <b>AI Take Profit set to +{tp:.0f}%, Stop Loss set to -{sl:.0f}%!</b>", parse_mode=ParseMode.HTML)
            text_out, markup_out = format_ai_menu(user_id)
            await update.message.reply_text(text_out, parse_mode=ParseMode.HTML, reply_markup=markup_out)
            return

    # 4. Check if text contains a valid Solana CA (direct CA, "CA: ...", DexScreener/Pump URLs)
    if extracted_ca:
        mint = extracted_ca
        status_msg = await update.message.reply_text(
            f"🔎 <i>Sniffing token security and market metrics for:</i>\n<code>{mint}</code>...",
            parse_mode=ParseMode.HTML
        )

        report = MarketSniffer.sniff(mint)
        pubkey, _ = WalletManager.get_or_create_wallet(user_id)
        sol_bal = WalletManager.get_sol_balance(pubkey)
        token_bal, _ = WalletManager.get_token_balance(pubkey, mint)

        msg_text = format_audit_message(report, sol_bal, token_bal)
        markup = build_audit_markup(mint, report["market"]["pair_url"], has_token_balance=(token_bal > 0))

        await status_msg.edit_text(msg_text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True)
        return

    # Fallback
    text_out, markup_out = format_main_menu(user_id)
    await update.message.reply_text(text_out, parse_mode=ParseMode.HTML, reply_markup=markup_out)


# ----------------------------------------------------
# SWAP EXECUTION ACTIONS
# ----------------------------------------------------
async def execute_buy_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    mint: str,
    amount_sol: float,
    callback_query = None
):
    """Executes a buy order via Jupiter Swap and reports transaction result"""
    pubkey = WalletManager.get_public_key(user_id)
    balance = WalletManager.get_sol_balance(pubkey)

    needed_sol = amount_sol + FEE_RESERVE_SOL
    if balance < needed_sol:
        err_msg = (
            f"❌ <b>Insufficient SOL Balance!</b>\n\n"
            f"You need at least <code>{needed_sol:.4f} SOL</code> (including network fee reserve).\n"
            f"Current Balance: <code>{balance:.4f} SOL</code>\n\n"
            f"Deposit SOL to: <code>{pubkey}</code>"
        )
        if callback_query:
            await callback_query.message.reply_text(err_msg, parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(err_msg, parse_mode=ParseMode.HTML)
        return

    progress_text = (
        f"⏳ <b>Executing Buy Order...</b>\n\n"
        f"🪙 Token: <code>{mint}</code>\n"
        f"💸 Amount: <code>{amount_sol} SOL</code>\n"
        "🛡️ <i>Routing via Jupiter Aggregator with priority fee...</i>"
    )

    if callback_query:
        status_msg = await callback_query.message.reply_text(progress_text, parse_mode=ParseMode.HTML)
    else:
        status_msg = await update.message.reply_text(progress_text, parse_mode=ParseMode.HTML)

    keypair = WalletManager.get_keypair(user_id)
    if not keypair:
        await status_msg.edit_text("❌ Error loading wallet keys. Please check /wallet.")
        return

    success, sig_or_err, quote = JupiterSwapEngine.execute_buy(keypair, mint, amount_sol)

    if success:
        try:
            m_data = MarketSniffer.get_market_data(mint)
            if m_data and m_data.get("price_usd"):
                u_settings = SettingsManager.get_user_settings(user_id)
                e_prices = u_settings.get("entry_prices", {})
                e_prices[mint] = m_data["price_usd"]
                SettingsManager.update_user_settings(user_id, {"entry_prices": e_prices})
        except Exception:
            pass

        success_msg = (
            "🎉 <b>BUY SUCCESSFUL!</b>\n\n"
            f"💸 <b>Spent:</b> <code>{amount_sol} SOL</code>\n"
            f"🪙 <b>Token:</b> <code>{mint}</code>\n"
            f"🔗 <a href='https://solscan.io/tx/{sig_or_err}'>View on Solscan</a>\n\n"
            "<i>Manage your position below:</i>"
        )
        sell_markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔴 Sell 25%", callback_data=f"sell:{mint}:25"),
                InlineKeyboardButton("🔴 Sell 50%", callback_data=f"sell:{mint}:50"),
                InlineKeyboardButton("🔴 Sell 100%", callback_data=f"sell:{mint}:100"),
            ],
            [
                InlineKeyboardButton("🎒 My Positions", callback_data="positions_menu"),
                InlineKeyboardButton("🔎 Resniff Token", callback_data=f"refresh:{mint}"),
            ],
            [
                InlineKeyboardButton("💳 Wallet", callback_data="wallet_menu"),
                InlineKeyboardButton("🔙 Home", callback_data="home_menu"),
            ]
        ])
        await status_msg.edit_text(success_msg, parse_mode=ParseMode.HTML, reply_markup=sell_markup, disable_web_page_preview=True)
    else:
        await status_msg.edit_text(
            f"❌ <b>Buy Failed:</b>\n\n"
            f"<i>{html.escape(str(sig_or_err))}</i>\n\n"
            "💡 Try checking if the pool has sufficient liquidity.",
            parse_mode=ParseMode.HTML
        )


async def execute_sol_to_usdt_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    amount_sol: float,
    callback_query = None
):
    """Swaps SOL to USDT"""
    pubkey = WalletManager.get_public_key(user_id)
    balance = WalletManager.get_sol_balance(pubkey)

    needed_sol = amount_sol + FEE_RESERVE_SOL
    if balance < needed_sol:
        err_msg = f"❌ Insufficient SOL balance ({balance:.4f} SOL available)."
        if callback_query:
            await callback_query.message.reply_text(err_msg)
        else:
            await update.message.reply_text(err_msg)
        return

    status_text = f"⏳ <i>Swapping {amount_sol} SOL ➡️ USDT via Jupiter...</i>"
    if callback_query:
        status_msg = await callback_query.message.reply_text(status_text, parse_mode=ParseMode.HTML)
    else:
        status_msg = await update.message.reply_text(status_text, parse_mode=ParseMode.HTML)

    keypair = WalletManager.get_keypair(user_id)
    success, sig_or_err, quote = JupiterSwapEngine.swap_sol_to_usdt(keypair, amount_sol)

    if success:
        out_usdt = int(quote.get("outAmount", 0)) / 1_000_000.0 if quote else 0.0
        await status_msg.edit_text(
            "🎉 <b>SWAP SUCCESSFUL!</b>\n\n"
            f"💸 Swapped <code>{amount_sol} SOL</code>\n"
            f"💵 Received <code>~{out_usdt:.2f} USDT</code>\n\n"
            f"🔗 <a href='https://solscan.io/tx/{sig_or_err}'>View on Solscan</a>",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚡ Back to Swap Menu", callback_data="swap_menu")]])
        )
    else:
        await status_msg.edit_text(f"❌ Swap failed: {html.escape(str(sig_or_err))}")


async def execute_usdt_to_sol_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    amount_usdt: float,
    callback_query = None
):
    """Swaps USDT to SOL"""
    pubkey = WalletManager.get_public_key(user_id)
    usdt_bal = WalletManager.get_usdt_balance(pubkey)

    if usdt_bal < amount_usdt:
        err_msg = f"❌ Insufficient USDT balance ({usdt_bal:.2f} USDT available)."
        if callback_query:
            await callback_query.message.reply_text(err_msg)
        else:
            await update.message.reply_text(err_msg)
        return

    status_text = f"⏳ <i>Swapping {amount_usdt:.2f} USDT ➡️ SOL via Jupiter...</i>"
    if callback_query:
        status_msg = await callback_query.message.reply_text(status_text, parse_mode=ParseMode.HTML)
    else:
        status_msg = await update.message.reply_text(status_text, parse_mode=ParseMode.HTML)

    keypair = WalletManager.get_keypair(user_id)
    success, sig_or_err, quote = JupiterSwapEngine.swap_usdt_to_sol(keypair, amount_usdt)

    if success:
        out_sol = int(quote.get("outAmount", 0)) / 1_000_000_000.0 if quote else 0.0
        await status_msg.edit_text(
            "🎉 <b>SWAP SUCCESSFUL!</b>\n\n"
            f"💵 Swapped <code>{amount_usdt:.2f} USDT</code>\n"
            f"💸 Received <code>~{out_sol:.4f} SOL</code>\n\n"
            f"🔗 <a href='https://solscan.io/tx/{sig_or_err}'>View on Solscan</a>",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚡ Back to Swap Menu", callback_data="swap_menu")]])
        )
    else:
        await status_msg.edit_text(f"❌ Swap failed: {html.escape(str(sig_or_err))}")


# ----------------------------------------------------
# CALLBACK QUERY HANDLER
# ----------------------------------------------------
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles button clicks"""
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = update.effective_user.id

    # 1. Close
    if data == "close":
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    # 2. Navigation
    if data == "home_menu":
        text, markup = format_main_menu(user_id)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    if data == "swap_menu":
        text, markup = format_swap_menu(user_id)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    if data == "wallet_menu":
        await wallet_command(update, context)
        return

    if data == "refresh_balance":
        pubkey = WalletManager.get_public_key(user_id)
        balance = WalletManager.get_sol_balance(pubkey)
        await query.answer(f"Balance: {balance:.4f} SOL", show_alert=True)
        text, markup = format_main_menu(user_id)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    if data == "positions_menu":
        text, markup = format_positions_menu(user_id)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True)
        return

    # 3. Copy Trading Callbacks
    if data == "copy_menu":
        text, markup = format_copy_menu(user_id)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    if data == "toggle_copy_mode":
        s = SettingsManager.get_user_settings(user_id)
        new_mode = "MANUAL" if s.get("mode") == "COPY" else "COPY"
        if new_mode == "COPY" and not s.get("target_wallet"):
            await query.answer("Please set a Target Wallet first!", show_alert=True)
            USER_STATES[user_id] = {"type": "set_target_wallet"}
            await query.message.reply_text("👤 <b>Set Target Wallet:</b>\nReply with the Solana wallet address you want to copy trade:", parse_mode=ParseMode.HTML)
            return
        SettingsManager.update_user_settings(user_id, {"mode": new_mode})
        await query.answer(f"Copy Trading {'Activated!' if new_mode == 'COPY' else 'Paused.'}", show_alert=True)
        text, markup = format_copy_menu(user_id)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    if data == "toggle_copy_sells":
        s = SettingsManager.get_user_settings(user_id)
        new_val = not s.get("copy_sells", True)
        SettingsManager.update_user_settings(user_id, {"copy_sells": new_val})
        text, markup = format_copy_menu(user_id)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    if data == "prompt_set_target_wallet":
        USER_STATES[user_id] = {"type": "set_target_wallet"}
        await query.message.reply_text("👤 <b>Set Target Wallet:</b>\nReply with the Solana wallet address you wish to copy trade:", parse_mode=ParseMode.HTML)
        return

    if data == "prompt_set_copy_amount":
        USER_STATES[user_id] = {"type": "set_copy_amount"}
        await query.message.reply_text("💸 <b>Set Copy Buy Size:</b>\nReply with the SOL amount to spend per trade (e.g. <code>0.2</code>):", parse_mode=ParseMode.HTML)
        return

    if data == "prompt_copy_slippage":
        USER_STATES[user_id] = {"type": "set_copy_slippage"}
        await query.message.reply_text("⚡ <b>Set Slippage:</b>\nReply with your slippage tolerance percentage (e.g. <code>3.0</code>):", parse_mode=ParseMode.HTML)
        return

    # 4. AI Trading Callbacks
    if data == "ai_menu":
        text, markup = format_ai_menu(user_id)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    if data == "toggle_ai_mode":
        s = SettingsManager.get_user_settings(user_id)
        new_mode = "MANUAL" if s.get("mode") == "AI" else "AI"
        SettingsManager.update_user_settings(user_id, {"mode": new_mode})
        await query.answer(f"AI Autopilot {'Engaged!' if new_mode == 'AI' else 'Paused.'}", show_alert=True)
        text, markup = format_ai_menu(user_id)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    if data.startswith("set_ai_strat:"):
        strat = data.split(":", 1)[1]
        SettingsManager.update_user_settings(user_id, {"ai_strategy": strat})
        text, markup = format_ai_menu(user_id)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    if data == "prompt_set_ai_amount":
        USER_STATES[user_id] = {"type": "set_ai_amount"}
        await query.message.reply_text("💸 <b>Set AI Trade Size:</b>\nReply with max SOL per AI trade (e.g. <code>0.15</code>):", parse_mode=ParseMode.HTML)
        return

    if data == "prompt_set_ai_tpsl":
        USER_STATES[user_id] = {"type": "set_ai_tpsl"}
        await query.message.reply_text("📈 <b>Set Take Profit & Stop Loss:</b>\nReply with: <code>&lt;TP%&gt; &lt;SL%&gt;</code> (e.g. <code>40 15</code> for +40% TP, -15% SL):", parse_mode=ParseMode.HTML)
        return

    # 5. Import Wallet Prompt Callback
    if data == "import_wallet_prompt":
        if update.effective_chat.type != "private":
            await query.answer("⚠️ Security Alert: Wallet import can only be accessed in private chat with the bot.", show_alert=True)
            return
        USER_STATES[user_id] = {"type": "import_wallet"}
        prompt_text = (
            "📥 <b>IMPORT EXISTING WALLET</b>\n\n"
            "Reply with your Phantom or Solflare <b>Base58 Private Key</b> (or JSON byte array):\n\n"
            "🔒 <b>Security Notice:</b>\n"
            "• Your key is encrypted with AES-256 Fernet immediately upon receipt.\n"
            "• The bot will automatically delete your message from Telegram.\n"
            "• Type /cancel anytime to abort."
        )
        await query.message.reply_text(prompt_text, parse_mode=ParseMode.HTML)
        return

    # 3. Swap SOL -> USDT sub-menu
    if data == "swap_sol_to_usdt_menu":
        pubkey = WalletManager.get_public_key(user_id)
        sol_bal = WalletManager.get_sol_balance(pubkey)
        text = (
            "🔄 <b>Swap SOL ➡️ USDT</b>\n\n"
            f"💰 Available: <code>{sol_bal:.4f} SOL</code>\n\n"
            "Select amount to swap:"
        )
        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("0.1 SOL", callback_data="do_swap_sol_usdt:0.1"),
                InlineKeyboardButton("0.25 SOL", callback_data="do_swap_sol_usdt:0.25"),
                InlineKeyboardButton("0.5 SOL", callback_data="do_swap_sol_usdt:0.5"),
            ],
            [
                InlineKeyboardButton("1.0 SOL", callback_data="do_swap_sol_usdt:1.0"),
                InlineKeyboardButton("✏️ Custom Amount", callback_data="prompt_custom_sol_usdt"),
            ],
            [
                InlineKeyboardButton("🔙 Back", callback_data="swap_menu"),
            ]
        ])
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    # 4. Swap USDT -> SOL sub-menu
    if data == "swap_usdt_to_sol_menu":
        pubkey = WalletManager.get_public_key(user_id)
        usdt_bal = WalletManager.get_usdt_balance(pubkey)
        text = (
            "🔄 <b>Swap USDT ➡️ SOL</b>\n\n"
            f"💵 Available: <code>{usdt_bal:.2f} USDT</code>\n\n"
            "Select amount to swap:"
        )
        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("25%", callback_data="do_swap_usdt_sol:25pct"),
                InlineKeyboardButton("50%", callback_data="do_swap_usdt_sol:50pct"),
                InlineKeyboardButton("100%", callback_data="do_swap_usdt_sol:100pct"),
            ],
            [
                InlineKeyboardButton("✏️ Custom USDT", callback_data="prompt_custom_usdt_sol"),
            ],
            [
                InlineKeyboardButton("🔙 Back", callback_data="swap_menu"),
            ]
        ])
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    # Execute quick SOL -> USDT
    if data.startswith("do_swap_sol_usdt:"):
        amt = float(data.split(":", 1)[1])
        await execute_sol_to_usdt_action(update, context, user_id, amt, callback_query=query)
        return

    # Execute quick USDT -> SOL
    if data.startswith("do_swap_usdt_sol:"):
        code = data.split(":", 1)[1]
        pubkey = WalletManager.get_public_key(user_id)
        usdt_bal = WalletManager.get_usdt_balance(pubkey)
        if code == "25pct":
            amt = usdt_bal * 0.25
        elif code == "50pct":
            amt = usdt_bal * 0.5
        elif code == "100pct":
            amt = usdt_bal
        else:
            amt = float(code)

        if amt <= 0:
            await query.answer("USDT balance is 0.", show_alert=True)
            return

        await execute_usdt_to_sol_action(update, context, user_id, amt, callback_query=query)
        return

    if data == "prompt_custom_sol_usdt":
        USER_STATES[user_id] = {"type": "custom_sol_to_usdt"}
        await query.message.reply_text("✏️ Reply with the amount of SOL to swap to USDT (e.g. <code>0.45</code>):", parse_mode=ParseMode.HTML)
        return

    if data == "prompt_custom_usdt_sol":
        USER_STATES[user_id] = {"type": "custom_usdt_to_sol"}
        await query.message.reply_text("✏️ Reply with the amount of USDT to swap to SOL (e.g. <code>25.0</code>):", parse_mode=ParseMode.HTML)
        return

    # 5. Export Private Key
    if data == "export_pk_confirm":
        if update.effective_chat.type != "private":
            await query.answer("⚠️ Security Alert: Private keys can only be revealed in direct private chat with the bot.", show_alert=True)
            return

        warning_text = (
            "⚠️ <b>CRITICAL SECURITY WARNING</b>\n\n"
            "Never share your private key with anyone!\n"
            "Anyone who gains access to this key will have complete control over all funds in this wallet.\n\n"
            "Are you sure you want to reveal your Phantom-compatible private key?"
        )
        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Yes, Reveal Key", callback_data="reveal_pk_now"),
                InlineKeyboardButton("❌ Cancel", callback_data="wallet_menu"),
            ]
        ])
        await query.edit_message_text(warning_text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    if data == "reveal_pk_now":
        if update.effective_chat.type != "private":
            await query.answer("⚠️ Security Alert: Private keys can only be revealed in direct private chat with the bot.", show_alert=True)
            return
        b58_key = WalletManager.export_private_key_b58(user_id)
        reveal_text = (
            "🔑 <b>Your Phantom / Solflare Import Key:</b>\n\n"
            f"<code>{b58_key}</code> <i>(tap to copy)</i>\n\n"
            "<b>How to Import into Phantom:</b>\n"
            "1. Open Phantom app/extension\n"
            "2. Click Menu → <b>Add / Connect Wallet</b>\n"
            "3. Select <b>Import Private Key</b>\n"
            "4. Paste the key above and confirm.\n\n"
            "⚠️ <i>Please delete or hide this message after importing!</i>"
        )
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔒 Hide / Back to Wallet", callback_data="wallet_menu")]])
        await query.edit_message_text(reveal_text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    # 6. Withdraw Prompt
    if data == "withdraw_prompt":
        USER_STATES[user_id] = {"type": "withdraw"}
        await query.message.reply_text(
            "💸 <b>Withdraw SOL</b>\n\n"
            "Please reply with your destination Solana address and amount:\n"
            "Example:\n"
            "<code>&lt;YOUR_PHANTOM_ADDRESS&gt; all</code>\n"
            "or\n"
            "<code>&lt;YOUR_PHANTOM_ADDRESS&gt; 0.5</code>",
            parse_mode=ParseMode.HTML
        )
        return

    # 7. Reset Wallet
    if data == "reset_wallet_prompt":
        reset_text = (
            "⚠️ <b>RESET WALLET:</b>\n"
            "Generating a new wallet will overwrite your current active key.\n"
            "Ensure you have exported your private key before resetting!"
        )
        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("💥 Yes, Generate New Wallet", callback_data="do_reset_wallet"),
                InlineKeyboardButton("❌ Cancel", callback_data="wallet_menu"),
            ]
        ])
        await query.edit_message_text(reset_text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    if data == "do_reset_wallet":
        new_pub = WalletManager.reset_wallet(user_id)
        await query.answer("New wallet generated!", show_alert=True)
        await wallet_command(update, context)
        return

    # 8. Token Refresh
    if data.startswith("refresh:"):
        mint = data.split(":", 1)[1]
        report = MarketSniffer.sniff(mint)
        pubkey = WalletManager.get_public_key(user_id)
        sol_bal = WalletManager.get_sol_balance(pubkey)
        token_bal, _ = WalletManager.get_token_balance(pubkey, mint)

        msg_text = format_audit_message(report, sol_bal, token_bal)
        markup = build_audit_markup(mint, report["market"]["pair_url"], has_token_balance=(token_bal > 0))
        await query.edit_message_text(msg_text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True)
        return

    # 8.5 AI Scan Callback
    if data.startswith("ai_scan:"):
        mint = data.split(":", 1)[1]
        settings = SettingsManager.get_user_settings(user_id)
        strategy = settings.get("ai_strategy", "BALANCED")
        eval_res = AITradingEngine.evaluate_token(mint, strategy=strategy)
        decision = eval_res.get("decision", "SKIP")
        confidence = eval_res.get("confidence", 0.0) * 100
        reason = eval_res.get("reason", "N/A")
        badge = "🟢 BUY RECOMMENDED" if decision == "BUY" else "🔴 PASS / CAUTION"
        ai_text = (
            f"🤖 <b>AI TOKEN ANALYSIS ({strategy})</b>\n\n"
            f"🪙 <b>Token:</b> <code>{mint}</code>\n"
            f"🎯 <b>Recommendation:</b> <b>{badge}</b>\n"
            f"📊 <b>Confidence:</b> <code>{confidence:.0f}%</code>\n"
            f"💬 <b>Reasoning:</b> <i>{html.escape(reason)}</i>"
        )
        await query.message.reply_text(ai_text, parse_mode=ParseMode.HTML)
        return

    # 9. Custom Buy
    if data.startswith("custom_buy:"):
        mint = data.split(":", 1)[1]
        USER_STATES[user_id] = {"type": "custom_buy", "mint": mint}
        await query.message.reply_text("✏️ <b>Custom Buy:</b> Reply with the amount of SOL to spend (e.g. <code>0.35</code>):", parse_mode=ParseMode.HTML)
        return

    # 10. Buy Execution
    if data.startswith("buy:"):
        _, mint, amount_str = data.split(":", 2)
        amount = float(amount_str)
        await execute_buy_action(update, context, user_id, mint, amount, callback_query=query)
        return

    # 11. Sell Execution
    if data.startswith("sell:"):
        _, mint, pct_str = data.split(":", 2)
        pct = float(pct_str)

        status_msg = await query.message.reply_text(
            f"⏳ <b>Executing Sell Order ({pct:.0f}%)...</b>\n"
            "🛡️ <i>Routing via Jupiter Aggregator back to native SOL...</i>",
            parse_mode=ParseMode.HTML
        )

        keypair = WalletManager.get_keypair(user_id)
        success, sig_or_err, quote = JupiterSwapEngine.execute_sell(keypair, mint, percentage=pct)

        if success:
            if pct >= 100:
                try:
                    u_settings = SettingsManager.get_user_settings(user_id)
                    e_prices = u_settings.get("entry_prices", {})
                    if mint in e_prices:
                        del e_prices[mint]
                        SettingsManager.update_user_settings(user_id, {"entry_prices": e_prices})
                except Exception:
                    pass

            out_sol = int(quote.get("outAmount", 0)) / 1_000_000_000.0 if quote else 0.0
            await status_msg.edit_text(
                "🎉 <b>SELL SUCCESSFUL!</b>\n\n"
                f"💰 <b>Received:</b> <code>~{out_sol:.4f} SOL</code>\n"
                f"🔗 <a href='https://solscan.io/tx/{sig_or_err}'>View on Solscan</a>",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        else:
            await status_msg.edit_text(f"❌ <b>Sell Failed:</b>\n\n<i>{html.escape(str(sig_or_err))}</i>", parse_mode=ParseMode.HTML)
        return

    # 12. ALPHA WALLET CALLBACKS (Admin only)
    if data.startswith("alpha_"):
        global ACTIVE_ADMIN_ID
        if ACTIVE_ADMIN_ID != 0 and user_id != ACTIVE_ADMIN_ID:
            await query.answer("Unauthorized.", show_alert=True)
            return

    if data == "alpha_refresh":
        await alpha_command(update, context)
        return

    if data == "alpha_export_pk":
        if update.effective_chat.type != "private":
            await query.answer("⚠️ Admin actions are only available in direct private chat with the bot.", show_alert=True)
            return
        alpha_data = WalletManager.get_or_create_alpha_wallet()
        pk = alpha_data["private_key_b58"]
        text = (
            "👑 <b>Alpha Wallet Private Key (Phantom Import):</b>\n\n"
            f"<code>{pk}</code> <i>(tap to copy)</i>\n\n"
            "⚠️ <i>Keep this secret! Import into Phantom to access accumulated fees anytime.</i>"
        )
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("Back to Alpha", callback_data="alpha_refresh")]])
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    if data == "alpha_withdraw_prompt":
        USER_STATES[user_id] = {"type": "alpha_withdraw"}
        await query.message.reply_text(
            "💸 <b>Withdraw Alpha Earnings</b>\n\n"
            "Reply with your personal Solana address:\n"
            "<code>&lt;YOUR_PHANTOM_ADDRESS&gt; all</code>",
            parse_mode=ParseMode.HTML
        )
        return


async def background_trader_worker(application: Application):
    """
    Continuous background loop that monitors:
    1. Active copy-trading target wallets on-chain (sub-3s polling)
    2. Active AI positions for Take Profit / Stop Loss thresholds
    """
    logger.info("🚀 Background Copy Trading & AI Autopilot worker loop started.")
    initialized_targets = set()
    last_market_scan_time = 0.0

    while True:
        try:
            now = time.time()

            # --- 1. COPY TRADING MONITOR ---
            active_copy = SettingsManager.get_active_copy_traders()
            for user_id, settings in active_copy:
                target = settings.get("target_wallet", "").strip()
                if not target:
                    continue

                last_sig = settings.get("last_seen_sig", "")

                # If target was never initialized, record current head signature without trading
                if target not in initialized_targets and not last_sig:
                    try:
                        recent = CopyTradingEngine.get_recent_swaps(target)
                        if recent:
                            latest_sig = recent[-1][0]
                            SettingsManager.update_user_settings(user_id, {"last_seen_sig": latest_sig})
                        initialized_targets.add(target)
                    except Exception as e:
                        logger.warning(f"Failed to initialize copy target {target}: {e}")
                    continue

                initialized_targets.add(target)

                # Fetch any new swaps since last_seen_sig
                try:
                    new_swaps = CopyTradingEngine.get_recent_swaps(target, last_seen_sig=last_sig)
                except Exception as e:
                    logger.warning(f"Error querying swaps for target {target}: {e}")
                    new_swaps = []

                if new_swaps:
                    for sig, swap_info in new_swaps:
                        SettingsManager.update_user_settings(user_id, {"last_seen_sig": sig})

                        trade_res = CopyTradingEngine.execute_mirrored_trade(user_id, swap_info, settings)
                        if trade_res:
                            action = trade_res.get("action", "SWAP")
                            mint = trade_res.get("mint", "")

                            if trade_res.get("success"):
                                tx_sig = trade_res.get("sig", "")
                                amt_info = f"<code>{trade_res.get('amount_sol', '')} SOL</code>" if action == "BUY" else "100%"
                                alert_msg = (
                                    "🎯 <b>COPY TRADE EXECUTED!</b>\n\n"
                                    f"👤 <b>Target Wallet:</b> <code>{target[:6]}...{target[-4:]}</code>\n"
                                    f"⚡ <b>Action:</b> <b>{action}</b> {amt_info}\n"
                                    f"🪙 <b>Token:</b> <code>{mint}</code>\n"
                                    f"🔗 <a href='https://solscan.io/tx/{tx_sig}'>View on Solscan</a>"
                                )
                                try:
                                    await application.bot.send_message(
                                        chat_id=user_id,
                                        text=alert_msg,
                                        parse_mode=ParseMode.HTML,
                                        disable_web_page_preview=True
                                    )
                                except Exception as err:
                                    logger.error(f"Failed to send copy alert to {user_id}: {err}")
                            else:
                                err = trade_res.get("error", "Unknown error")
                                alert_msg = (
                                    "⚠️ <b>Copy Trade Skipped / Failed:</b>\n\n"
                                    f"👤 <b>Target:</b> <code>{target[:6]}...{target[-4:]}</code>\n"
                                    f"⚡ <b>Attempted:</b> {action} <code>{mint}</code>\n"
                                    f"❌ <b>Reason:</b> <i>{html.escape(str(err))}</i>"
                                )
                                try:
                                    await application.bot.send_message(
                                        chat_id=user_id,
                                        text=alert_msg,
                                        parse_mode=ParseMode.HTML
                                    )
                                except Exception:
                                    pass

            # --- 2. AI POSITION EXIT MONITOR (TP / SL) ---
            active_ai = SettingsManager.get_active_ai_traders()
            for user_id, settings in active_ai:
                entry_prices = settings.get("entry_prices", {})
                if not entry_prices:
                    continue

                tp = settings.get("ai_take_profit_pct", 30.0)
                sl = settings.get("ai_stop_loss_pct", 15.0)

                exits = AITradingEngine.check_position_exits(
                    user_id=user_id,
                    entry_prices=entry_prices,
                    take_profit_pct=tp,
                    stop_loss_pct=sl
                )

                for ex in exits:
                    mint = ex.get("mint", "")
                    symbol = ex.get("symbol", "TOKEN")
                    if ex.get("success"):
                        entry_prices.pop(mint, None)
                        SettingsManager.update_user_settings(user_id, {"entry_prices": entry_prices})
                        action_title = "🎉 <b>TAKE PROFIT HIT!</b>" if ex["action"] == "TAKE_PROFIT" else "🛑 <b>STOP LOSS TRIGGERED!</b>"
                        pct_val = ex.get("gain_pct", ex.get("loss_pct", 0.0))
                        exit_msg = (
                            f"{action_title}\n\n"
                            f"🪙 <b>Token:</b> ${html.escape(symbol)} (<code>{mint}</code>)\n"
                            f"📊 <b>P&L:</b> <code>{'+' if ex['action'] == 'TAKE_PROFIT' else '-'}{pct_val:.1f}%</code>\n"
                            f"🔗 <a href='https://solscan.io/tx/{ex.get('sig')}'>View on Solscan</a>"
                        )
                        try:
                            await application.bot.send_message(
                                chat_id=user_id,
                                text=exit_msg,
                                parse_mode=ParseMode.HTML,
                                disable_web_page_preview=True
                            )
                        except Exception:
                            pass

            # --- 3. AUTONOMOUS ALGORITHMIC MARKET SCANNER & AUTO-TRADER ---
            if active_ai and (now - last_market_scan_time) >= 40.0:
                last_market_scan_time = now
                for user_id, settings in active_ai:
                    # Cooldown check: 10 minutes between automated buys per user
                    last_buy_time = settings.get("last_ai_trade_time", 0.0)
                    if (now - last_buy_time) < 600.0:
                        continue

                    pubkey = WalletManager.get_public_key(user_id)
                    if not pubkey:
                        continue

                    # Balance check: ensure user has enough SOL to cover trade + gas reserve
                    trade_amt = float(settings.get("ai_trade_amount_sol", 0.1))
                    sol_bal = WalletManager.get_sol_balance(pubkey)
                    if sol_bal < (trade_amt + FEE_RESERVE_SOL + 0.01):
                        continue

                    # Position count limit: max 3 open positions simultaneously
                    positions = WalletManager.get_all_positions(pubkey)
                    active_pos = [p for p in positions if p.get("ui_amount", 0.0) > 0.0001]
                    if len(active_pos) >= 3:
                        continue

                    owned_mints = [p["mint"] for p in active_pos]
                    strategy = settings.get("ai_strategy", "BALANCED")

                    # Run Pattern Recognition Scanner across live Solana market
                    candidate = AITradingEngine.scan_and_select_trade_candidate(
                        strategy=strategy,
                        user_positions=owned_mints
                    )

                    if candidate:
                        mint = candidate["mint"]
                        market = candidate.get("market", {})
                        symbol = market.get("symbol", "TOKEN")
                        price_usd = market.get("price_usd", 0.0)
                        pattern = candidate.get("pattern", "Volume Surge & Breakout")
                        score = candidate.get("score", 75.0)

                        keypair = WalletManager.get_keypair(user_id)
                        slippage = int(settings.get("slippage_bps", DEFAULT_SLIPPAGE_BPS))

                        success, sig_or_err, quote = JupiterSwapEngine.execute_buy(
                            keypair, mint, trade_amt, slippage_bps=slippage
                        )

                        if success:
                            e_prices = settings.get("entry_prices", {})
                            e_prices[mint] = price_usd
                            SettingsManager.update_user_settings(user_id, {
                                "entry_prices": e_prices,
                                "last_ai_trade_time": now,
                            })

                            tp_pct = settings.get("ai_take_profit_pct", 30.0)
                            sl_pct = settings.get("ai_stop_loss_pct", 15.0)

                            alert_text = (
                                "🤖 <b>ALGORITHMIC AUTOPILOT BUY EXECUTED!</b>\n\n"
                                f"🪙 <b>Token:</b> ${html.escape(symbol)} (<code>{mint}</code>)\n"
                                f"📊 <b>Pattern Detected:</b> <i>{html.escape(pattern)}</i>\n"
                                f"🎯 <b>Strategy:</b> <b>{strategy}</b> (Pattern Score: <b>{score:.0f}/100</b>)\n"
                                f"💸 <b>Amount Spent:</b> <code>{trade_amt} SOL</code>\n"
                                f"📈 <b>Take Profit:</b> <code>+{tp_pct:.0f}%</code> | <b>Stop Loss:</b> <code>-{sl_pct:.0f}%</code>\n\n"
                                f"🔗 <a href='https://solscan.io/tx/{sig_or_err}'>View on Solscan</a>"
                            )
                            try:
                                await application.bot.send_message(
                                    chat_id=user_id,
                                    text=alert_text,
                                    parse_mode=ParseMode.HTML,
                                    disable_web_page_preview=True
                                )
                            except Exception as err:
                                logger.error(f"Failed to send auto-trade alert to {user_id}: {err}")
                        else:
                            logger.warning(f"Auto-trade buy failed for {user_id} on {mint}: {sig_or_err}")

        except Exception as e:
            logger.error(f"Error in background trader worker: {e}", exc_info=True)

        await asyncio.sleep(2.5)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Global error handler"""
    logger.error("Exception handling update:", exc_info=context.error)


async def post_init(application: Application):
    """Executed once bot authenticates with Telegram"""
    me = application.bot
    print(f"✅ Market Sniffer Bot is live! Logged in as: {me.first_name} (@{me.username})")
    asyncio.create_task(background_trader_worker(application))


def main():
    if not BOT_TOKEN:
        print("\n" + "=" * 60)
        print("❌ ERROR: TELEGRAM_BOT_TOKEN is not configured for Market Sniffer!")
        print("Please edit /home/lazorr/market_sniffer_bot/.env and set:")
        print("BOT_TOKEN=your_telegram_bot_token_here")
        print("=" * 60 + "\n")
        sys.exit(1)

    print("🚀 Initializing Market Sniffer Bot...")
    request = HTTPXRequest(
        connection_pool_size=16,
        connect_timeout=30.0,
        read_timeout=60.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .post_init(post_init)
        .build()
    )

    # Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("wallet", wallet_command))
    app.add_handler(CommandHandler("swap", swap_command))
    app.add_handler(CommandHandler("positions", positions_command))
    app.add_handler(CommandHandler("copy", copy_command))
    app.add_handler(CommandHandler("ai", ai_command))
    app.add_handler(CommandHandler("import", import_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("alpha", alpha_command))

    # Text message handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Callback query handler
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    # Error handler
    app.add_error_handler(error_handler)

    print("📡 Connecting Market Sniffer to Telegram...")
    app.run_polling(
        drop_pending_updates=True,
        bootstrap_retries=-1,
        timeout=20,
        poll_interval=1.0,
    )


if __name__ == "__main__":
    main()
