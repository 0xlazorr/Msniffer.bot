# 🐕🔎 Market Sniffer — Solana Degen Trading Bot

A high-speed, security-first Solana trading bot built for Telegram. Simply paste any **Token Contract Address (CA)** into the chat to immediately audit its safety (Honeypot, Freeze Authority, Mint Authority, Top Holders) and execute swaps via **Jupiter Aggregator** with one tap.

---

## 🛡️ Financial & Security Architecture

1. **Local AES-256 Encrypted Keystore**:
   - Each user receives an auto-generated Ed25519 Solana wallet keypair.
   - Private keys are encrypted at rest with AES-256 (Fernet) in `data/wallets.enc` (`chmod 600`).
   - Private keys are **never** logged, **never** transmitted to any external server, and only decrypted in volatile memory when signing a transaction.
2. **Phantom / Solflare Integration**:
   - Easily export your bot wallet's Base58 private key with `/wallet` → **Export Private Key** to manage, track, or trade inside Phantom or Solflare.
3. **Automated "Sniffer" Security Audits**:
   - **Freeze Authority Check**: Flags tokens where the creator can freeze trading or blacklist your wallet.
   - **Mint Authority Check**: Detects if infinite tokens can be minted and dumped.
   - **Top 10 Holder Concentration**: Calculates percentage of supply held by top wallets.
   - **RugCheck Risk Score**: Instant rating: 🟢 LOW RISK | 🟡 CAUTION | 🚨 HIGH DANGER.
4. **MEV-Resistant Swap Execution**:
   - Powered by **Jupiter Aggregator v1 API** (routes through Raydium, Orca, Meteora, Pump.fun).
   - Dynamic compute unit limits & automatic priority fees to land transactions quickly without sandwich attacks.
5. **Fee Reserve Buffer**:
   - Enforces a `0.005 SOL` safety reserve so your wallet never gets bricked with zero balance for gas fees.

---

## ⚡ Commands & Interactions

| Action | Description |
|---|---|
| `/start` | Displays welcome screen, your bot wallet address, and balance. |
| `/wallet` | View address, live SOL balance, export Phantom key, or withdraw funds. |
| `Paste any CA` | Instantly audits token metrics & security, offering `[ Buy 0.1 SOL ]`, `[ Buy 0.5 SOL ]`, etc. |
| `[ 🔴 Sell % ]` | Instant 1-tap sell buttons (25%, 50%, 100%) to swap tokens back to native SOL. |

---

## 🚀 Setup & Installation

### 1. Requirements
Ensure Python 3.10+ is installed:
```bash
pip install -r requirements.txt
```

### 2. Configure Telegram Bot Token
1. Open [@BotFather](https://t.me/BotFather) on Telegram and run `/newbot`.
2. Name your bot `Market Sniffer` and choose a username.
3. Paste the token into `.env`:
```env
BOT_TOKEN=your_token_from_botfather
RPC_ENDPOINT=https://api.mainnet-beta.solana.com
DEFAULT_SLIPPAGE_BPS=300
```

### 3. Start the Bot
```bash
./start.sh
```

---

## 📦 Project Structure

```
/home/lazorr/market_sniffer_bot/
├── bot.py              # Telegram bot UI, command handlers, and interactive menus
├── sniffer.py          # DexScreener analytics & RugCheck security audit engine
├── wallet_manager.py   # Encrypted keystore, Phantom export, balance checks, transfers
├── jupiter_swap.py     # Jupiter v1 quote routing, transaction signing, and swap execution
├── config.py           # Configuration and safety parameters
├── requirements.txt    # Python dependencies
├── start.sh            # Quick execution script
├── .gitignore          # Prevents keys, credentials, and data/ from being tracked
└── data/               # AES-256 encrypted wallet storage (restricted permissions)
```
