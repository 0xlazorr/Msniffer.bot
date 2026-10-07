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
from telegram.constants import ParseMode, ChatAction
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from config import BOT_TOKEN, FEE_RESERVE_SOL, DEFAULT_SLIPPAGE_BPS
from wallet_manager import WalletManager
from sniffer import MarketSniffer, format_currency
from jupiter_swap import JupiterSwapEngine

# Logging configuration
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# State tracking for pending user inputs (e.g. custom buy amount, withdraw)
USER_STATES: Dict[int, Dict[str, Any]] = {}


def build_audit_markup(
    mint: str,
    pair_url: str,
    has_token_balance: bool = False
) -> InlineKeyboardMarkup:
    """Builds interactive buy/sell and navigation buttons for a token audit"""
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
            InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh:{mint}"),
        ],
    ]

    # If user already holds this token, show sell buttons
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


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /start command: displays welcome, creates/shows wallet"""
    user_id = update.effective_user.id
    pubkey, is_new = WalletManager.get_or_create_wallet(user_id)
    balance = WalletManager.get_sol_balance(pubkey)

    welcome_text = (
        "🐕🔎 <b>Welcome to Market Sniffer!</b>\n"
        "<i>The fastest & safest Solana Degen Trading Bot.</i>\n\n"
        "🛡️ <b>Built-in Capital Protection:</b>\n"
        "• Automatic Honeypot & Freeze Authority detector\n"
        "• Mint Authority revocation check\n"
        "• Top 10 Holder concentration alerts\n"
        "• MEV-resistant routing via Jupiter Aggregator\n\n"
        "💳 <b>Your Dedicated Trading Wallet:</b>\n"
        f"<code>{pubkey}</code> <i>(tap to copy)</i>\n\n"
        f"💰 <b>Balance:</b> <code>{balance:.4f} SOL</code>\n\n"
        "🚀 <b>How to Trade:</b>\n"
        "1. Deposit a small amount of SOL into your address above.\n"
        "2. <b>Paste any Token Contract Address (CA)</b> into this chat.\n"
        "3. Review the security audit and tap <b>Buy</b>!\n\n"
        "💡 <i>You can export this wallet's private key into Phantom or Solflare anytime via /wallet.</i>"
    )

    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💳 Wallet Settings", callback_data="wallet_menu"),
            InlineKeyboardButton("🔄 Refresh Balance", callback_data="refresh_balance"),
        ],
        [
            InlineKeyboardButton("🎒 My Positions", callback_data="positions_menu"),
        ]
    ])

    await update.message.reply_text(welcome_text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def wallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays user wallet details, balance, export PK, and withdraw options"""
    user_id = update.effective_user.id
    pubkey, _ = WalletManager.get_or_create_wallet(user_id)
    balance = WalletManager.get_sol_balance(pubkey)

    msg_text = (
        "💳 <b>Market Sniffer Wallet</b>\n\n"
        "📬 <b>Your Public Address:</b>\n"
        f"<code>{pubkey}</code>\n\n"
        f"💰 <b>SOL Balance:</b> <code>{balance:.4f} SOL</code>\n\n"
        "🔒 <b>Security Note:</b>\n"
        "Your private key is encrypted locally on your server using AES-256. "
        "You can import this wallet into Phantom or Solflare by clicking <b>Export Private Key</b> below."
    )

    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="refresh_balance"),
            InlineKeyboardButton("💸 Withdraw SOL", callback_data="withdraw_prompt"),
        ],
        [
            InlineKeyboardButton("🔑 Export Private Key (Phantom)", callback_data="export_pk_confirm"),
        ],
        [
            InlineKeyboardButton("🔄 Generate New Wallet", callback_data="reset_wallet_prompt"),
            InlineKeyboardButton("❌ Close", callback_data="close"),
        ]
    ])

    if update.callback_query:
        await update.callback_query.edit_message_text(msg_text, parse_mode=ParseMode.HTML, reply_markup=markup)
    else:
        await update.message.reply_text(msg_text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes incoming text: Solana CAs or pending state inputs"""
    user_id = update.effective_user.id
    text = update.message.text.strip()

    # 1. Check if user is in a state (e.g. waiting for withdraw address or custom buy)
    if user_id in USER_STATES:
        state = USER_STATES[user_id]
        state_type = state.get("type")

        # Custom Buy Amount Input
        if state_type == "custom_buy":
            mint = state["mint"]
            del USER_STATES[user_id]
            try:
                amount = float(text)
                if amount <= 0:
                    await update.message.reply_text("❌ Amount must be greater than 0.")
                    return
                await execute_buy_action(update, context, user_id, mint, amount)
            except ValueError:
                await update.message.reply_text("❌ Invalid number. Please enter a valid SOL amount (e.g. 0.35).")
            return

        # Withdraw Input
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
                await update.message.reply_text(f"❌ Insufficient balance to cover gas fee ({balance:.4f} SOL).")
                return

            if len(parts) >= 2 and parts[1].lower() != "all":
                try:
                    withdraw_amount = float(parts[1])
                except ValueError:
                    await update.message.reply_text("❌ Invalid amount.")
                    return
            else:
                withdraw_amount = balance - FEE_RESERVE_SOL

            if withdraw_amount <= 0 or withdraw_amount > (balance - FEE_RESERVE_SOL):
                await update.message.reply_text(f"❌ Max withdrawable amount is {balance - FEE_RESERVE_SOL:.4f} SOL.")
                return

            status_msg = await update.message.reply_text(f"⏳ <i>Withdrawing {withdraw_amount:.4f} SOL to {dest_addr[:6]}...{dest_addr[-4:]}...</i>", parse_mode=ParseMode.HTML)
            kp = WalletManager.get_keypair(user_id)
            success, sig_or_err = WalletManager.withdraw_sol(kp, dest_addr, withdraw_amount)

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

    # 2. Check if message is a Solana Token Contract Address (CA)
    if MarketSniffer.is_valid_solana_address(text):
        mint = text
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

    # Fallback for unrecognized text
    await update.message.reply_text(
        "💡 <b>Market Sniffer</b>\n"
        "Send any <b>Solana Token Contract Address (CA)</b> to audit security and execute swaps!\n"
        "Or use /wallet to manage your keys and funds.",
        parse_mode=ParseMode.HTML
    )


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

    # Send progress notification
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

    # Decrypt Keypair in memory
    keypair = WalletManager.get_keypair(user_id)
    if not keypair:
        await status_msg.edit_text("❌ Error loading wallet keys. Please check /wallet.")
        return

    # Execute Swap
    success, sig_or_err, quote = JupiterSwapEngine.execute_buy(keypair, mint, amount_sol)

    if success:
        out_amount = quote.get("outAmount", "N/A") if quote else "N/A"
        success_msg = (
            "🎉 <b>BUY SUCCESSFUL!</b>\n\n"
            f"💸 <b>Spent:</b> <code>{amount_sol} SOL</code>\n"
            f"🪙 <b>Token:</b> <code>{mint}</code>\n"
            f"🔗 <a href='https://solscan.io/tx/{sig_or_err}'>View on Solscan</a>\n\n"
            "<i>Manage your position with the buttons below:</i>"
        )
        sell_markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔴 Sell 25%", callback_data=f"sell:{mint}:25"),
                InlineKeyboardButton("🔴 Sell 50%", callback_data=f"sell:{mint}:50"),
                InlineKeyboardButton("🔴 Sell 100%", callback_data=f"sell:{mint}:100"),
            ],
            [
                InlineKeyboardButton("🔎 Resniff Token", callback_data=f"refresh:{mint}"),
                InlineKeyboardButton("💳 Wallet", callback_data="wallet_menu"),
            ]
        ])
        await status_msg.edit_text(success_msg, parse_mode=ParseMode.HTML, reply_markup=sell_markup, disable_web_page_preview=True)
    else:
        await status_msg.edit_text(
            f"❌ <b>Buy Failed:</b>\n\n"
            f"<i>{html.escape(str(sig_or_err))}</i>\n\n"
            "💡 Try increasing slippage in /settings or check if liquidity is sufficient.",
            parse_mode=ParseMode.HTML
        )


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles all button interactions (Buy, Sell, Wallet, Export PK, Refresh)"""
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

    # 2. Wallet Menu
    if data == "wallet_menu":
        await wallet_command(update, context)
        return

    # 3. Refresh Balance
    if data == "refresh_balance":
        pubkey = WalletManager.get_public_key(user_id)
        balance = WalletManager.get_sol_balance(pubkey)
        await query.answer(f"Balance updated: {balance:.4f} SOL", show_alert=True)
        await wallet_command(update, context)
        return

    # 4. Export Private Key Warning Prompt
    if data == "export_pk_confirm":
        warning_text = (
            "⚠️ <b>CRITICAL SECURITY WARNING</b>\n\n"
            "Never share your private key with anyone! "
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

    # 5. Reveal Private Key
    if data == "reveal_pk_now":
        b58_key = WalletManager.export_private_key_b58(user_id)
        if not b58_key:
            await query.edit_message_text("❌ Key not found.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="wallet_menu")]]))
            return

        reveal_text = (
            "🔑 <b>Your Phantom / Solflare Import Key:</b>\n\n"
            f"<code>{b58_key}</code> <i>(tap to copy)</i>\n\n"
            "<b>How to Import into Phantom:</b>\n"
            "1. Open Phantom app/extension\n"
            "2. Click Menu (top left) → <b>Add / Connect Wallet</b>\n"
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

    # 7. Reset Wallet Prompt
    if data == "reset_wallet_prompt":
        reset_text = (
            "⚠️ <b>RESET WALLET WARNING:</b>\n\n"
            "Generating a new wallet will overwrite your current active key.\n"
            "Make sure you have withdrawn or exported your private key before resetting!"
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
        new_pubkey = WalletManager.reset_wallet(user_id)
        await query.answer("New wallet generated!", show_alert=True)
        await wallet_command(update, context)
        return

    # 8. Refresh Token Audit
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

    # 9. Custom Buy Prompt
    if data.startswith("custom_buy:"):
        mint = data.split(":", 1)[1]
        USER_STATES[user_id] = {"type": "custom_buy", "mint": mint}
        await query.message.reply_text(
            f"✏️ <b>Custom Buy:</b>\nReply with the amount of SOL you want to spend (e.g. <code>0.35</code>):",
            parse_mode=ParseMode.HTML
        )
        return

    # 10. Execute Buy: buy:<mint>:<amount>
    if data.startswith("buy:"):
        _, mint, amount_str = data.split(":", 2)
        amount = float(amount_str)
        await execute_buy_action(update, context, user_id, mint, amount, callback_query=query)
        return

    # 11. Execute Sell: sell:<mint>:<pct>
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
            out_sol = int(quote.get("outAmount", 0)) / 1_000_000_000.0 if quote else 0.0
            await status_msg.edit_text(
                "🎉 <b>SELL SUCCESSFUL!</b>\n\n"
                f"💰 <b>Received:</b> <code>~{out_sol:.4f} SOL</code>\n"
                f"🔗 <a href='https://solscan.io/tx/{sig_or_err}'>View on Solscan</a>",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        else:
            await status_msg.edit_text(
                f"❌ <b>Sell Failed:</b>\n\n<i>{html.escape(str(sig_or_err))}</i>",
                parse_mode=ParseMode.HTML
            )
        return


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Global error handler to ensure bot never crashes"""
    logger.error("Exception handling update:", exc_info=context.error)


async def post_init(application: Application):
    """Executed once bot authenticates with Telegram"""
    me = application.bot
    print(f"✅ Market Sniffer Bot is live! Logged in as: {me.first_name} (@{me.username})")


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
    app.add_handler(CommandHandler("positions", wallet_command))

    # Text message handler (Solana CAs and state responses)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Interactive button callback handler
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
