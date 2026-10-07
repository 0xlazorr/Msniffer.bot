import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Load environment
load_dotenv(BASE_DIR / ".env")

# Telegram Bot Token
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Admin Telegram ID (for exclusive access to /alpha wallet dashboard)
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))

# Solana RPC Endpoint
RPC_ENDPOINT = os.getenv(
    "RPC_ENDPOINT", "https://api.mainnet-beta.solana.com"
).strip()

# Jupiter Swap API v1
JUPITER_QUOTE_API = "https://api.jup.ag/swap/v1/quote"
JUPITER_SWAP_API = "https://api.jup.ag/swap/v1/swap"

# Token Mint Addresses
SOL_MINT = "So11111111111111111111111111111111111111112"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

# Safety Defaults
DEFAULT_SLIPPAGE_BPS = int(os.getenv("DEFAULT_SLIPPAGE_BPS", "300"))  # 3% slippage
FEE_RESERVE_SOL = 0.005  # Keep 0.005 SOL reserved for transaction fees
FEE_PERCENT = float(os.getenv("FEE_PERCENT", "0.01"))  # 1.0% fee routed to Alpha wallet

# AI Autonomous Trading Configurations (openai, grok, anthropic, gemini)
AI_PROVIDER = os.getenv("AI_PROVIDER", "openai").lower()
AI_API_KEY = os.getenv("AI_API_KEY", "").strip()
AI_MODEL = os.getenv("AI_MODEL", "").strip()

# Storage paths
KEYSTORE_PATH = DATA_DIR / "wallets.enc"
MASTER_KEY_PATH = DATA_DIR / "master.key"
ALPHA_WALLET_PATH = DATA_DIR / "alpha_wallet.enc"
SETTINGS_PATH = DATA_DIR / "trader_settings.enc"
