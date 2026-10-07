# 🐕🔎 Market Sniffer — Solana Sniper, Copy & Algorithmic AI Auto-Trader

A high-speed, institutional-grade Solana trading bot built for Telegram. Audit token contracts, mirror insider whale trades on-chain, or run autonomous algorithmic trading using real-time volume breakout & momentum pattern recognition (with zero external AI API required, plus pluggable OpenAI/Grok/Claude support).

---

## 🌟 Key Features

### 1. 🐕 Token Sniffer & Security Auditor
- **Instant Contract Sniffing:** Paste any Solana Token Contract Address (CA), pump.fun URL, or DexScreener link.
- **Honeypot & Freeze Authority Gate:** Strictly identifies tokens with active freeze authority or mint permissions.
- **Holder Distribution & RugCheck:** Verifies top 10 concentration, LP lock status, and safety risk ratings.
- **1-Tap Swap Execution:** Instant buy buttons (`0.1`, `0.25`, `0.5`, `1.0 SOL`, or custom) routed through **Jupiter Aggregator v1**.

### 2. 🎯 On-Chain Copy Trading Engine (`/copy`)
- **Universal Swap Detection:** Compares Solana `preTokenBalances` vs `postTokenBalances` on target wallets. Detects swaps across **Raydium**, **Pump.fun**, **Jupiter**, and **Orca** DEX programs.
- **Automated Mirroring:** Automatically executes matching buys and sells in real-time with sub-3-second polling.
- **Customizable Execution:** Customize copy buy size in SOL, slippage tolerance, and toggle auto-selling on/off.

### 3. ⚡ Autonomous Algorithmic & Pattern Recognition Engine (`/ai`)
- **Zero-API Key Required:** Operates 100% autonomously out-of-the-box using quantitative technical pattern recognition:
  - **Volume Acceleration Pattern:** Detects 5-minute volume surges relative to hourly moving averages.
  - **Buy Pressure Ratio:** Tracks net buyer accumulation (`buys_m5` vs `sells_m5`).
  - **Momentum & Trend Alignment:** Validates positive velocity without buying overextended parabolic tops.
  - **Safety Gate:** Filters out honeypots, active mint authorities, and high-risk contracts.
- **Strategy Profiles:**
  - **Balanced:** High-momentum breakouts with verified liquidity ($15K+) and revoked authorities.
  - **Degen:** Aggressive pump.fun curve volume scalping ($4K+ liquidity).
  - **Conservative:** Ultra-safe $50K+ liquidity with zero audit warnings.
- **Pluggable AI APIs (Optional):** Add `AI_API_KEY` for **OpenAI** (`gpt-4o-mini`), **Grok (xAI)** (`grok-beta`), or **Anthropic Claude** to get deep LLM reasoning on top of quant metrics.
- **Automated Risk Management (TP / SL):** Automatically monitors positions and executes Take Profit (`+30%`) or Stop Loss (`-15%`) exits.
- **Safety Safeguards:** 10-minute auto-buy cooldown, maximum 3 simultaneous active positions, and automatic gas balance floor protection.

### 4. 💳 Wallet Management & Phantom / Solflare Import (`/wallet`, `/import`)
- **Direct Wallet Import:** Connect an existing Phantom or Solflare wallet via Base58 private key or JSON byte array.
- **Zero Plaintext Trace:** Messages containing private keys are automatically deleted from Telegram chat history immediately upon receipt.
- **Encrypted Storage:** Keystore and settings are encrypted at rest with AES-256 Fernet (`chmod 0600`).
- **Withdraw & Export:** Easily withdraw native SOL or export keys at any time.

---

## 🛡️ Security Architecture

1. **Local AES-256 Encrypted Keystore**: Keys are encrypted at rest with AES-256 (`data/wallets.enc`, `chmod 0600`) and only decrypted in memory when signing.
2. **Private Chat Isolation**: Sensitive commands (`/wallet`, `/import`, `/alpha`) are strictly forbidden in public groups.
3. **MEV-Resistant Routing**: Dynamic compute unit limits and priority fees prevent sandwich attacks.
4. **Gas Overdraft Protection**: Preserves a minimum `0.005 SOL` safety reserve so wallets never become bricked.

---

## ⚡ Telegram Commands

| Command | Action |
| :--- | :--- |
| `/start` | Open the main dashboard with active mode badge (`🎯 Copy`, `🤖 AI`, or `⏸️ Manual`) |
| `/copy` | Open Copy Trading settings (set target wallet, copy buy size, toggle auto-sells) |
| `/ai` | Open AI / Algorithmic Autopilot settings (strategy, trade size, TP/SL targets) |
| `/import` | Securely import an existing Phantom or Solflare wallet |
| `/wallet` | View SOL & USDT balances, deposit address, withdraw funds, export key |
| `/positions` | View open token holdings with quick-sell buttons |
| `/swap` | Quick swap SOL ⇄ USDT |
| `/alpha` | Access the hidden Alpha Wallet dashboard *(admin only)* |
| `/cancel` | Cancel any pending input prompt |

---

## 🚀 Setup & Installation

### 1. Requirements
Python 3.10+ on Linux / macOS / Windows.
```bash
pip install -r requirements.txt
```

### 2. Configuration
Create `.env` based on `.env.example`:
```env
BOT_TOKEN=your_telegram_bot_token_from_botfather
RPC_ENDPOINT=https://api.mainnet-beta.solana.com
DEFAULT_SLIPPAGE_BPS=300
FEE_PERCENT=0.01

# Optional: Add external AI API key (OpenAI, Grok, Anthropic)
# If omitted, the bot runs via the built-in Algorithmic Pattern Recognition Engine
AI_PROVIDER=openai
AI_API_KEY=
AI_MODEL=
```

### 3. Run the Bot
```bash
python3 bot.py
# or
./start.sh
```

---

## 📦 Project Structure

```
├── bot.py              # Telegram bot UI, command handlers, and background trader loop
├── sniffer.py          # DexScreener market analytics, trending scanner & RugCheck auditor
├── ai_engine.py        # Algorithmic pattern recognition & pluggable LLM auto-trader
├── copy_engine.py      # On-chain target wallet swap parser & mirrored execution engine
├── wallet_manager.py   # AES-256 encrypted keystore, wallet import, transfers, and export
├── jupiter_swap.py     # Jupiter Aggregator v1 swap quotes, fee routing & transaction signing
├── config.py           # Environment variables, token constants, and storage paths
├── requirements.txt    # Python dependencies
└── start.sh            # Service launcher script
```
