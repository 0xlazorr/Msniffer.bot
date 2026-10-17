import os
import json
import time
import shutil
import fcntl
import threading
import requests
from collections import deque
from typing import Optional, Dict, Any, List, Tuple
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from config import (
    SETTINGS_PATH,
    RPC_ENDPOINT,
    DEFAULT_SLIPPAGE_BPS,
    SOL_MINT,
    USDT_MINT,
    USDC_MINT,
    FEE_RESERVE_SOL,
)
from wallet_manager import WalletManager
from jupiter_swap import JupiterSwapEngine


class SettingsManager:
    """Manages encrypted user modes and settings (Copy Trading & AI Autopilot) atomically"""
    _lock = threading.Lock()

    @classmethod
    def _load_all(cls) -> Dict[str, dict]:
        if not SETTINGS_PATH.exists():
            return {}
        try:
            cipher = WalletManager._get_cipher()
            enc = SETTINGS_PATH.read_bytes()
            if not enc:
                return {}
            dec = cipher.decrypt(enc)
            return json.loads(dec.decode("utf-8"))
        except Exception as e:
            backup_corrupt = SETTINGS_PATH.with_suffix(f".corrupt_{int(time.time())}.bak")
            try:
                shutil.copy2(SETTINGS_PATH, backup_corrupt)
                print(f"CRITICAL: Settings decrypt error: {e}. Preserved backup at {backup_corrupt}")
            except Exception:
                pass
            raise RuntimeError(f"Failed to decrypt settings file! Existing file preserved: {e}")

    @classmethod
    def _save_all(cls, data: Dict[str, dict]):
        with cls._lock:
            cipher = WalletManager._get_cipher()
            raw = json.dumps(data, indent=2).encode("utf-8")
            enc = cipher.encrypt(raw)

            # Preserve previous backup before overwriting
            if SETTINGS_PATH.exists():
                try:
                    shutil.copy2(SETTINGS_PATH, SETTINGS_PATH.with_suffix(".bak"))
                except Exception:
                    pass

            # Write atomically with tempfile
            temp_path = SETTINGS_PATH.with_suffix(".tmp")
            with open(temp_path, "wb") as f:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    f.write(enc)
                    f.flush()
                    os.fsync(f.fileno())
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

            temp_path.chmod(0o600)
            os.replace(temp_path, SETTINGS_PATH)
            SETTINGS_PATH.chmod(0o600)

    @classmethod
    def get_user_settings(cls, user_id: int) -> dict:
        uid = str(user_id)
        try:
            all_data = cls._load_all()
        except Exception as e:
            print(f"Error loading settings: {e}")
            all_data = {}
        default_settings = {
            "mode": "MANUAL",  # "MANUAL", "COPY", "AI"
            "target_wallet": "",
            "copy_buy_amount_sol": 0.1,
            "copy_sells": True,
            "slippage_bps": DEFAULT_SLIPPAGE_BPS,
            "ai_strategy": "BALANCED",  # "CONSERVATIVE", "BALANCED", "DEGEN"
            "ai_trade_amount_sol": 0.1,
            "ai_take_profit_pct": 30.0,
            "ai_stop_loss_pct": 15.0,
            "entry_prices": {},
            "last_seen_sig": "",
            "processed_sigs": [],
        }
        user_settings = all_data.get(uid, {})
        # Merge defaults
        for k, v in default_settings.items():
            if k not in user_settings:
                user_settings[k] = v
        return user_settings

    @classmethod
    def update_user_settings(cls, user_id: int, updates: dict):
        uid = str(user_id)
        all_data = cls._load_all()
        current = cls.get_user_settings(user_id)
        current.update(updates)
        # Limit processed_sigs to last 50 to prevent file bloat
        if len(current.get("processed_sigs", [])) > 50:
            current["processed_sigs"] = current["processed_sigs"][-50:]
        all_data[uid] = current
        cls._save_all(all_data)

    @classmethod
    def get_active_copy_traders(cls) -> List[Tuple[int, dict]]:
        """Returns list of (user_id, settings) where mode == 'COPY' and target_wallet is set"""
        try:
            all_data = cls._load_all()
        except Exception:
            return []
        active = []
        for uid_str, s in all_data.items():
            if s.get("mode") == "COPY" and s.get("target_wallet"):
                active.append((int(uid_str), s))
        return active

    @classmethod
    def get_active_ai_traders(cls) -> List[Tuple[int, dict]]:
        """Deprecated: Returns empty list. AI Autopilot is managed exclusively by Chatters AI Council."""
        return []


class CopyTradingEngine:
    """Detects on-chain swaps for target wallets and mirrors orders with strict FIFO deduplication"""

    # FIFO cache of recent signatures to prevent duplicate trade executions
    _seen_signatures_set = set()
    _seen_signatures_order = deque(maxlen=600)
    _lock = threading.Lock()

    @classmethod
    def parse_target_swap(cls, target_wallet: str, tx_data: dict) -> Optional[dict]:
        """
        Analyzes preTokenBalances vs postTokenBalances for the target_wallet.
        Detects whether target bought or sold an SPL token.
        """
        meta = tx_data.get("meta")
        if not meta or meta.get("err") is not None:
            return None  # Transaction failed on-chain

        pre_balances = meta.get("preTokenBalances", [])
        post_balances = meta.get("postTokenBalances", [])

        # Map mint -> uiAmount for target_wallet
        pre_map: Dict[str, float] = {}
        post_map: Dict[str, float] = {}

        for b in pre_balances:
            if b.get("owner") == target_wallet:
                mint = b.get("mint")
                ui_amt = float(b.get("uiTokenAmount", {}).get("uiAmount") or 0.0)
                if mint:
                    pre_map[mint] = ui_amt

        for b in post_balances:
            if b.get("owner") == target_wallet:
                mint = b.get("mint")
                ui_amt = float(b.get("uiTokenAmount", {}).get("uiAmount") or 0.0)
                if mint:
                    post_map[mint] = ui_amt

        # Check all unique mints (excluding base SOL, USDT, and USDC to avoid stablecoin copy buys)
        all_mints = set(pre_map.keys()) | set(post_map.keys())
        all_mints.discard(SOL_MINT)
        all_mints.discard(USDT_MINT)
        all_mints.discard(USDC_MINT)

        for mint in all_mints:
            pre_amt = pre_map.get(mint, 0.0)
            post_amt = post_map.get(mint, 0.0)
            diff = post_amt - pre_amt

            if diff > 0.000001:
                # Target BOUGHT this token
                return {
                    "action": "BUY",
                    "mint": mint,
                    "target_wallet": target_wallet,
                    "amount_change": diff,
                }
            elif diff < -0.000001:
                # Target SOLD this token
                sell_ratio = (abs(diff) / pre_amt * 100.0) if pre_amt > 0 else 100.0
                return {
                    "action": "SELL",
                    "mint": mint,
                    "target_wallet": target_wallet,
                    "amount_change": abs(diff),
                    "sell_pct": min(100.0, max(1.0, sell_ratio)),
                }

        return None

    @classmethod
    def get_recent_swaps(cls, target_wallet: str, last_seen_sig: str = "") -> List[Tuple[str, dict]]:
        """
        Polls recent signatures for target_wallet and parses any executed swaps with FIFO deduplication.
        Returns list of (sig, swap_info).
        """
        swaps = []
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSignaturesForAddress",
                "params": [
                    target_wallet,
                    {"limit": 4},
                ],
            }
            res = requests.post(RPC_ENDPOINT, json=payload, timeout=8)
            if res.status_code != 200:
                return []

            sigs = [s["signature"] for s in res.json().get("result", [])]
            for sig in reversed(sigs):  # Process chronologically
                if sig == last_seen_sig:
                    continue

                with cls._lock:
                    if sig in cls._seen_signatures_set:
                        continue

                    # Strict FIFO eviction ensures recently seen transactions are NEVER prematurely evicted
                    if len(cls._seen_signatures_order) >= 600:
                        oldest = cls._seen_signatures_order.popleft()
                        cls._seen_signatures_set.discard(oldest)

                    cls._seen_signatures_set.add(sig)
                    cls._seen_signatures_order.append(sig)

                # Fetch parsed transaction
                tx_payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTransaction",
                    "params": [
                        sig,
                        {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
                    ],
                }
                tx_res = requests.post(RPC_ENDPOINT, json=tx_payload, timeout=8)
                if tx_res.status_code == 200:
                    tx_data = tx_res.json().get("result")
                    if tx_data:
                        swap_info = cls.parse_target_swap(target_wallet, tx_data)
                        if swap_info:
                            swaps.append((sig, swap_info))

        except Exception as e:
            print(f"Error checking target {target_wallet}: {e}")

        return swaps

    @classmethod
    def execute_mirrored_trade(
        cls,
        user_id: int,
        swap_info: dict,
        settings: dict,
    ) -> Optional[dict]:
        """
        Executes mirrored buy or sell for user using Jupiter Swap Engine.
        Accurately records entry prices in SettingsManager.
        """
        action = swap_info["action"]
        mint = swap_info["mint"]
        keypair = WalletManager.get_keypair(user_id)
        if not keypair:
            return {"success": False, "error": "User wallet key not found"}

        if action == "BUY":
            amount_sol = float(settings.get("copy_buy_amount_sol", 0.1))
            slippage = int(settings.get("slippage_bps", DEFAULT_SLIPPAGE_BPS))

            pubkey = str(keypair.pubkey())
            user_sol = WalletManager.get_sol_balance(pubkey)
            needed = amount_sol + FEE_RESERVE_SOL
            if user_sol < needed:
                return {
                    "success": False,
                    "action": "BUY",
                    "mint": mint,
                    "error": f"Insufficient SOL ({user_sol:.4f} SOL available, need {needed:.4f} SOL)",
                }

            success, sig_or_err, quote = JupiterSwapEngine.execute_buy(
                keypair, mint, amount_sol, slippage_bps=slippage
            )
            if success:
                try:
                    from sniffer import MarketSniffer
                    m_data = MarketSniffer.get_market_data(mint)
                    if m_data and m_data.get("price_usd"):
                        u_settings = SettingsManager.get_user_settings(user_id)
                        e_prices = u_settings.get("entry_prices", {})
                        e_prices[mint] = m_data["price_usd"]
                        SettingsManager.update_user_settings(user_id, {"entry_prices": e_prices})
                except Exception as e:
                    print(f"Failed to record copy-trade entry price for user {user_id}: {e}")

            return {
                "success": success,
                "action": "BUY",
                "mint": mint,
                "amount_sol": amount_sol,
                "sig": sig_or_err if success else "",
                "error": sig_or_err if not success else "",
            }

        elif action == "SELL":
            if not settings.get("copy_sells", True):
                return None  # User disabled auto-sells

            pubkey = str(keypair.pubkey())
            ui_bal, raw_bal = WalletManager.get_token_balance(pubkey, mint)
            if raw_bal <= 0:
                return None  # User holds none of this token

            slippage = int(settings.get("slippage_bps", DEFAULT_SLIPPAGE_BPS))
            sell_pct = float(swap_info.get("sell_pct", 100.0))
            success, sig_or_err, quote = JupiterSwapEngine.execute_sell(
                keypair, mint, percentage=sell_pct, slippage_bps=slippage
            )
            if success:
                if sell_pct >= 99.0:
                    try:
                        u_settings = SettingsManager.get_user_settings(user_id)
                        e_prices = u_settings.get("entry_prices", {})
                        if mint in e_prices:
                            del e_prices[mint]
                            SettingsManager.update_user_settings(user_id, {"entry_prices": e_prices})
                    except Exception as e:
                        print(f"Failed to clear entry price on copy sell for user {user_id}: {e}")

            return {
                "success": success,
                "action": "SELL",
                "mint": mint,
                "sell_pct": sell_pct,
                "sig": sig_or_err if success else "",
                "error": sig_or_err if not success else "",
            }

        return None

