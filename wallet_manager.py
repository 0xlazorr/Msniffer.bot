import os
import json
import base58
import base64
import requests
from typing import Optional, Dict, Tuple
from cryptography.fernet import Fernet
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.message import MessageV0
from solders.transaction import VersionedTransaction
from solders.hash import Hash

from config import KEYSTORE_PATH, MASTER_KEY_PATH, RPC_ENDPOINT


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
        """Loads and decrypts keystore JSON"""
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
        """Encrypts and persists keystore JSON to disk with 0600 permissions"""
        cipher = cls._get_cipher()
        raw_json = json.dumps(data).encode("utf-8")
        enc_data = cipher.encrypt(raw_json)
        KEYSTORE_PATH.write_bytes(enc_data)
        KEYSTORE_PATH.chmod(0o600)

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

        # Generate new Solana Ed25519 Keypair
        kp = Keypair()
        pubkey_str = str(kp.pubkey())
        priv_bytes = bytes(kp)
        priv_b58 = base58.b58encode(priv_bytes).decode("utf-8")

        keystore[uid] = {
            "public_key": pubkey_str,
            "private_key_b58": priv_b58,
            "created_at": str(os.path.getmtime(MASTER_KEY_PATH) if MASTER_KEY_PATH.exists() else 0),
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
    def get_token_balance(cls, wallet_pubkey: str, token_mint: str) -> Tuple[float, int]:
        """
        Fetches balance and decimals for a specific SPL token in user's wallet.
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
                if accounts:
                    info = accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]
                    return float(info.get("uiAmount") or 0.0), int(info.get("amount") or 0)
        except Exception as e:
            print(f"Error fetching token balance: {e}")
        return 0.0, 0

    @classmethod
    def withdraw_sol(
        cls, sender_keypair: Keypair, recipient_address: str, amount_sol: float
    ) -> Tuple[bool, str]:
        """
        Withdraws SOL from bot wallet to external address (e.g. user's Phantom).
        Returns (success, signature_or_error_msg).
        """
        try:
            # 1. Validate recipient address
            recipient_pubkey = Pubkey.from_string(recipient_address)
            lamports = int(amount_sol * 1_000_000_000)

            # 2. Fetch latest blockhash
            bh_payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getLatestBlockhash",
                "params": [{"commitment": "confirmed"}],
            }
            bh_res = requests.post(RPC_ENDPOINT, json=bh_payload, timeout=8)
            bh_data = bh_res.json()
            recent_blockhash_str = bh_data["result"]["value"]["blockhash"]
            recent_blockhash = Hash.from_string(recent_blockhash_str)

            # 3. Create transfer instruction and sign transaction
            ix = transfer(
                TransferParams(
                    from_pubkey=sender_keypair.pubkey(),
                    to_pubkey=recipient_pubkey,
                    lamports=lamports,
                )
            )
            msg = MessageV0.try_compile(sender_keypair.pubkey(), [ix], [], recent_blockhash)
            tx = VersionedTransaction(msg, [sender_keypair])

            # 4. Submit raw transaction to Solana RPC
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
                return True, sig
            elif "error" in res_json:
                return False, res_json["error"].get("message", "RPC error")
            else:
                return False, "Unknown RPC response"

        except Exception as e:
            return False, str(e)
