import os
import json
import time
import requests
from typing import Optional, Dict, Any, List, Tuple
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from config import (
    SETTINGS_PATH,
    RPC_ENDPOINT,
    DEFAULT_SLIPPAGE_BPS,
    SOL_MINT,
    USDT_MINT,
)
from wallet_manager import WalletManager
from jupiter_swap import JupiterSwapEngine


class SettingsManager:
    """Manages encrypted user modes and settings (Copy Trading & AI Autopilot)"""

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
            print(f"Error loading trader settings: {e}")
            return {}

    @classmethod
    def _save_all(cls, data: Dict[str, dict]):
        cipher = WalletManager._get_cipher()
        raw = json.dumps(data).encode("utf-8")
        enc = cipher.encrypt(raw)
        SETTINGS_PATH.write_bytes(enc)
        SETTINGS_PATH.chmod(0o600)

    @classmethod
    def get_user_settings(cls, user_id: int) -> dict:
        uid = str(user_id)
        all_data = cls._load_all()
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
        all_data = cls._load_all()
        active = []
        for uid_str, s in all_data.items():
            if s.get("mode") == "COPY" and s.get("target_wallet"):
                active.append((int(uid_str), s))
        return active

    @classmethod
    def get_active_ai_traders(cls) -> List[Tuple[int, dict]]:
        """Returns list of (user_id, settings) where mode == 'AI'"""
        all_data = cls._load_all()
        active = []
        for uid_str, s in all_data.items():
            if s.get("mode") == "AI":
                active.append((int(uid_str), s))
        return active


class CopyTradingEngine:
    """Detects on-chain swaps for target wallets and mirrors orders"""

    # Memory cache of recent signatures to avoid repeated RPC parsing
    _seen_signatures = set()

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

        # Check all unique mints (excluding base SOL / USDT wrappers)
        all_mints = set(pre_map.keys()) | set(post_map.keys())
        all_mints.discard(SOL_MINT)

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
                return {
                    "action": "SELL",
                    "mint": mint,
                    "target_wallet": target_wallet,
                    "amount_change": abs(diff),
                }

        return None

    @classmethod
    def get_recent_swaps(cls, target_wallet: str, last_seen_sig: str = "") -> List[Tuple[str, dict]]:
        """
        Polls recent signatures for target_wallet and parses any executed swaps.
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
                if sig in cls._seen_signatures:
                    continue

                cls._seen_signatures.add(sig)
                if len(cls._seen_signatures) > 300:
                    cls._seen_signatures.pop()

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
            if user_sol < (amount_sol + 0.005):
                return {
                    "success": False,
                    "action": "BUY",
                    "mint": mint,
                    "error": f"Insufficient SOL ({user_sol:.4f} SOL available, need {amount_sol + 0.005:.4f} SOL)",
                }

            success, sig_or_err, quote = JupiterSwapEngine.execute_buy(
                keypair, mint, amount_sol, slippage_bps=slippage
            )
            if success:
                try:
                    from sniffer import MarketSniffer
                    m_data = MarketSniffer.get_market_data(mint)
                    if m_data and m_data.get("price_usd"):
                        u_settings = cls.get_user_settings(user_id)
                        e_prices = u_settings.get("entry_prices", {})
                        e_prices[mint] = m_data["price_usd"]
                        cls.update_user_settings(user_id, {"entry_prices": e_prices})
                except Exception:
                    pass
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
            success, sig_or_err, quote = JupiterSwapEngine.execute_sell(
                keypair, mint, percentage=100.0, slippage_bps=slippage
            )
            if success:
                try:
                    u_settings = cls.get_user_settings(user_id)
                    e_prices = u_settings.get("entry_prices", {})
                    if mint in e_prices:
                        del e_prices[mint]
                        cls.update_user_settings(user_id, {"entry_prices": e_prices})
                except Exception:
                    pass
            return {
                "success": success,
                "action": "SELL",
                "mint": mint,
                "sig": sig_or_err if success else "",
                "error": sig_or_err if not success else "",
            }

        return None
