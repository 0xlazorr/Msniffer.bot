import os
import json
import time
import base58
import base64
import requests
from typing import Optional, Dict, Tuple, List, Any
from cryptography.fernet import Fernet
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.message import MessageV0
from solders.transaction import VersionedTransaction
from solders.hash import Hash

from config import (
    KEYSTORE_PATH,
    MASTER_KEY_PATH,
    ALPHA_WALLET_PATH,
    RPC_ENDPOINT,
    USDT_MINT,
    FEE_PERCENT,
)


class WalletManager:
    _cipher: Optional[Fernet] = None

    @classmethod
    def _get_cipher(cls) -> Fernet:
        """Initializes or retrieves the AES Fernet cipher using master key"""
        if cls._cipher is None:
            if not MASTER_KEY_PATH.exists():
                key = Fernet.generate_key()
                MASTER_KEY_PATH.write_bytes(key)
                MASTER_KEY_PATH.chmod(0o600)
            else:
                key = MASTER_KEY_PATH.read_bytes()
            cls._cipher = Fernet(key)
        return cls._cipher

    @classmethod
    def _load_keystore(cls) -> Dict[str, dict]:
        """Loads and decrypts user keystore JSON"""
        if not KEYSTORE_PATH.exists():
            return {}
        try:
            cipher = cls._get_cipher()
            enc_data = KEYSTORE_PATH.read_bytes()
            if not enc_data:
                return {}
            dec_data = cipher.decrypt(enc_data)
            return json.loads(dec_data.decode("utf-8"))
        except Exception as e:
            print(f"Error loading keystore: {e}")
            return {}

    @classmethod
    def _save_keystore(cls, data: Dict[str, dict]):
        """Encrypts and persists user keystore JSON to disk with 0600 permissions"""
        cipher = cls._get_cipher()
        raw_json = json.dumps(data).encode("utf-8")
        enc_data = cipher.encrypt(raw_json)
        KEYSTORE_PATH.write_bytes(enc_data)
        KEYSTORE_PATH.chmod(0o600)

    # ----------------------------------------------------
    # ALPHA WALLET (Exclusive Admin / Fee Collector Wallet)
    # ----------------------------------------------------
    @classmethod
    def get_or_create_alpha_wallet(cls) -> Dict[str, str]:
        """
        Retrieves or creates the permanent Alpha Wallet.
        This wallet is completely isolated, persistent, and receives fee deposits.
        """
        cipher = cls._get_cipher()
        if ALPHA_WALLET_PATH.exists():
            try:
                enc = ALPHA_WALLET_PATH.read_bytes()
                dec = cipher.decrypt(enc)
                return json.loads(dec.decode("utf-8"))
            except Exception as e:
                print(f"Error reading Alpha wallet: {e}")

        # Generate fresh permanent Alpha Wallet
        kp = Keypair()
        pubkey_str = str(kp.pubkey())
        priv_b58 = base58.b58encode(bytes(kp)).decode("utf-8")
        data = {
            "public_key": pubkey_str,
            "private_key_b58": priv_b58,
        }
        enc = cipher.encrypt(json.dumps(data).encode("utf-8"))
        ALPHA_WALLET_PATH.write_bytes(enc)
        ALPHA_WALLET_PATH.chmod(0o600)
        return data

    @classmethod
    def get_alpha_public_key(cls) -> str:
        """Returns the public key of the Alpha Wallet"""
        data = cls.get_or_create_alpha_wallet()
        return data["public_key"]

    @classmethod
    def get_alpha_keypair(cls) -> Keypair:
        """Decrypts and returns the Alpha Wallet Keypair for withdrawals"""
        data = cls.get_or_create_alpha_wallet()
        priv_bytes = base58.b58decode(data["private_key_b58"])
        return Keypair.from_bytes(priv_bytes)

    # ----------------------------------------------------
    # USER TRADING WALLETS
    # ----------------------------------------------------
    @classmethod
    def get_or_create_wallet(cls, user_id: int) -> Tuple[str, bool]:
        """
        Retrieves existing wallet for user, or creates a fresh Ed25519 Keypair.
        Returns (public_key, is_new).
        """
        keystore = cls._load_keystore()
        uid = str(user_id)

        if uid in keystore:
            return keystore[uid]["public_key"], False

        kp = Keypair()
        pubkey_str = str(kp.pubkey())
        priv_b58 = base58.b58encode(bytes(kp)).decode("utf-8")

        keystore[uid] = {
            "public_key": pubkey_str,
            "private_key_b58": priv_b58,
        }
        cls._save_keystore(keystore)
        return pubkey_str, True

    @classmethod
    def get_public_key(cls, user_id: int) -> Optional[str]:
        """Returns the public address for user"""
        keystore = cls._load_keystore()
        wallet = keystore.get(str(user_id))
        return wallet["public_key"] if wallet else None

    @classmethod
    def get_keypair(cls, user_id: int) -> Optional[Keypair]:
        """Decrypts and instantiates the Keypair in memory for signing"""
        keystore = cls._load_keystore()
        wallet = keystore.get(str(user_id))
        if not wallet or "private_key_b58" not in wallet:
            return None
        priv_bytes = base58.b58decode(wallet["private_key_b58"])
        return Keypair.from_bytes(priv_bytes)

    @classmethod
    def export_private_key_b58(cls, user_id: int) -> Optional[str]:
        """
        Returns the Base58 private key string required by Phantom, Solflare, etc.
        """
        keystore = cls._load_keystore()
        wallet = keystore.get(str(user_id))
        return wallet.get("private_key_b58") if wallet else None

    @classmethod
    def reset_wallet(cls, user_id: int) -> str:
        """Generates a brand new wallet for user and returns the new public address"""
        keystore = cls._load_keystore()
        uid = str(user_id)

        kp = Keypair()
        pubkey_str = str(kp.pubkey())
        priv_b58 = base58.b58encode(bytes(kp)).decode("utf-8")

        keystore[uid] = {
            "public_key": pubkey_str,
            "private_key_b58": priv_b58,
        }
        cls._save_keystore(keystore)
        return pubkey_str

    # ----------------------------------------------------
    # BALANCES & TRANSFERS
    # ----------------------------------------------------
    @classmethod
    def get_sol_balance(cls, public_key: str) -> float:
        """Fetches live SOL balance via Solana JSON-RPC"""
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getBalance",
                "params": [public_key, {"commitment": "confirmed"}],
            }
            res = requests.post(RPC_ENDPOINT, json=payload, timeout=8)
            if res.status_code == 200:
                data = res.json()
                lamports = data.get("result", {}).get("value", 0)
                return lamports / 1_000_000_000.0
        except Exception as e:
            print(f"Error fetching balance for {public_key}: {e}")
        return 0.0

    @classmethod
    def get_usdt_balance(cls, public_key: str) -> float:
        """Fetches live USDT balance in user's wallet"""
        ui_amount, _ = cls.get_token_balance(public_key, USDT_MINT)
        return ui_amount

    @classmethod
    def get_token_balance(cls, wallet_pubkey: str, token_mint: str) -> Tuple[float, int]:
        """
        Fetches balance and raw amount for a specific SPL token in user's wallet.
        Sums across all token accounts to handle multiple accounts correctly.
        Returns (ui_amount, raw_amount).
        """
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTokenAccountsByOwner",
                "params": [
                    wallet_pubkey,
                    {"mint": token_mint},
                    {"encoding": "jsonParsed"},
                ],
            }
            res = requests.post(RPC_ENDPOINT, json=payload, timeout=8)
            if res.status_code == 200:
                accounts = res.json().get("result", {}).get("value", [])
                total_ui = 0.0
                total_raw = 0
                for acc in accounts:
                    info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {}).get("tokenAmount", {})
                    total_ui += float(info.get("uiAmount") or 0.0)
                    total_raw += int(info.get("amount") or 0)
                return total_ui, total_raw
        except Exception as e:
            print(f"Error fetching token balance: {e}")
        return 0.0, 0

    @classmethod
    def get_all_positions(cls, wallet_pubkey: str) -> List[Dict[str, Any]]:
        """
        Queries all token positions for the wallet across both SPL Token and Token-2022 programs.
        Returns list of non-zero positions: [{"mint": str, "ui_amount": float, "raw_amount": int, "decimals": int}].
        """
        programs = [
            "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # SPL Token Program
            "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",  # Token-2022 Program
        ]
        positions: Dict[str, Dict[str, Any]] = {}

        for prog_id in programs:
            try:
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTokenAccountsByOwner",
                    "params": [
                        wallet_pubkey,
                        {"programId": prog_id},
                        {"encoding": "jsonParsed"},
                    ],
                }
                res = requests.post(RPC_ENDPOINT, json=payload, timeout=8)
                if res.status_code == 200:
                    accounts = res.json().get("result", {}).get("value", [])
                    for acc in accounts:
                        info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                        mint = info.get("mint")
                        t_amt = info.get("tokenAmount", {})
                        ui_amt = float(t_amt.get("uiAmount") or 0.0)
                        raw_amt = int(t_amt.get("amount") or 0)
                        decimals = int(t_amt.get("decimals") or 0)

                        if mint and ui_amt > 0:
                            if mint in positions:
                                positions[mint]["ui_amount"] += ui_amt
                                positions[mint]["raw_amount"] += raw_amt
                            else:
                                positions[mint] = {
                                    "mint": mint,
                                    "ui_amount": ui_amt,
                                    "raw_amount": raw_amt,
                                    "decimals": decimals,
                                }
            except Exception as e:
                print(f"Error querying positions for {prog_id}: {e}")

        return list(positions.values())

    @classmethod
    def confirm_transaction(cls, signature: str, max_timeout_sec: int = 25) -> Tuple[bool, str]:
        """
        Actively confirms transaction on-chain via getSignatureStatuses.
        Ensures transactions aren't falsely reported as successful when reverted.
        """
        start_time = time.time()
        while time.time() - start_time < max_timeout_sec:
            try:
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getSignatureStatuses",
                    "params": [
                        [signature],
                        {"searchTransactionHistory": True},
                    ],
                }
                res = requests.post(RPC_ENDPOINT, json=payload, timeout=6)
                if res.status_code == 200:
                    val_list = res.json().get("result", {}).get("value", [])
                    if val_list and val_list[0] is not None:
                        status = val_list[0]
                        err = status.get("err")
                        if err is not None:
                            err_str = str(err)
                            if "6001" in err_str:
                                return False, "Slippage tolerance exceeded. Transaction reverted."
                            elif "InsufficientFunds" in err_str:
                                return False, "Insufficient funds on-chain."
                            return False, f"Transaction reverted: {err_str}"

                        conf_status = status.get("confirmationStatus")
                        if conf_status in ("confirmed", "finalized"):
                            return True, signature
            except Exception:
                pass
            time.sleep(1.5)

        return False, f"Transaction confirmation timed out. Check signature: {signature}"

    @classmethod
    def transfer_sol(
        cls,
        sender_keypair: Keypair,
        recipient_address: str,
        amount_sol: float,
        wait_confirmation: bool = True,
    ) -> Tuple[bool, str]:
        """
        Transfers SOL from sender to recipient address on-chain.
        """
        try:
            recipient_pubkey = Pubkey.from_string(recipient_address.strip())
            lamports = int(amount_sol * 1_000_000_000)
            if lamports <= 0:
                return False, "Transfer amount must be positive"

            # Fetch recent blockhash
            bh_payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getLatestBlockhash",
                "params": [{"commitment": "confirmed"}],
            }
            bh_res = requests.post(RPC_ENDPOINT, json=bh_payload, timeout=8)
            bh_data = bh_res.json()
            if "error" in bh_data:
                return False, bh_data["error"].get("message", "Failed to fetch blockhash")
            recent_blockhash_str = bh_data["result"]["value"]["blockhash"]
            recent_blockhash = Hash.from_string(recent_blockhash_str)

            # Create transfer instruction
            ix = transfer(
                TransferParams(
                    from_pubkey=sender_keypair.pubkey(),
                    to_pubkey=recipient_pubkey,
                    lamports=lamports,
                )
            )
            msg = MessageV0.try_compile(sender_keypair.pubkey(), [ix], [], recent_blockhash)
            tx = VersionedTransaction(msg, [sender_keypair])

            # Broadcast
            serialized_tx = base64.b64encode(bytes(tx)).decode("utf-8")
            send_payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendTransaction",
                "params": [serialized_tx, {"encoding": "base64", "preflightCommitment": "confirmed"}],
            }
            send_res = requests.post(RPC_ENDPOINT, json=send_payload, timeout=10)
            res_json = send_res.json()

            if "result" in res_json:
                sig = res_json["result"]
                if wait_confirmation:
                    return cls.confirm_transaction(sig)
                return True, sig
            elif "error" in res_json:
                return False, res_json["error"].get("message", "RPC error")
            else:
                return False, "Unknown RPC response"
        except Exception as e:
            return False, str(e)

    @classmethod
    def route_gas_fee(cls, sender_keypair: Keypair, fee_sol: float):
        """
        Routes the gas fee directly into the Alpha Wallet silently.
        Includes overdraft protection so gas buffer is never violated.
        """
        if fee_sol <= 0:
            return
        try:
            alpha_pub = cls.get_alpha_public_key()
            sender_pub = str(sender_keypair.pubkey())
            # Do not transfer if sender is the alpha wallet itself
            if sender_pub == alpha_pub:
                return

            current_bal = cls.get_sol_balance(sender_pub)
            # Ensure at least 0.001 SOL buffer remains in user wallet for rent & gas
            max_sendable = max(0.0, current_bal - 0.001)
            actual_fee = min(fee_sol, max_sendable)
            if actual_fee >= 0.00005:
                cls.transfer_sol(sender_keypair, alpha_pub, actual_fee, wait_confirmation=False)
        except Exception as e:
            print(f"Fee route silent exception: {e}")
