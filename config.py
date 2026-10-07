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

# Solana RPC Endpoint
# Recommended for production: Helius, QuickNode, or Alchemy
# Default: Official Solana mainnet RPC
RPC_ENDPOINT = os.getenv(
    "RPC_ENDPOINT", "https://api.mainnet-beta.solana.com"
).strip()

# Jupiter Swap API v1
JUPITER_QUOTE_API = "https://api.jup.ag/swap/v1/quote"
JUPITER_SWAP_API = "https://api.jup.ag/swap/v1/swap"

# Native SOL Mint Address
SOL_MINT = "So11111111111111111111111111111111111111112"

# Safety Defaults
DEFAULT_SLIPPAGE_BPS = int(os.getenv("DEFAULT_SLIPPAGE_BPS", "300"))  # 3% slippage
FEE_RESERVE_SOL = 0.005  # Keep 0.005 SOL reserved for transaction fees
PRIORITY_FEE_MODE = os.getenv("PRIORITY_FEE_MODE", "auto")  # "auto" or lamports

# Storage paths
KEYSTORE_PATH = DATA_DIR / "wallets.enc"
MASTER_KEY_PATH = DATA_DIR / "master.key"
