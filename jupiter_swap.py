import base64
import time
import requests
from typing import Optional, Dict, Any, Tuple
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from config import JUPITER_QUOTE_API, JUPITER_SWAP_API, SOL_MINT, RPC_ENDPOINT, DEFAULT_SLIPPAGE_BPS
from wallet_manager import WalletManager


class JupiterSwapEngine:
    @staticmethod
    def get_quote(
        input_mint: str,
        output_mint: str,
        amount: int,
        slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
    ) -> Optional[Dict[str, Any]]:
        """
        Fetches best swap route from Jupiter v1 quote API.
        """
        try:
            params = {
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(amount),
                "slippageBps": str(slippage_bps),
            }
            res = requests.get(JUPITER_QUOTE_API, params=params, timeout=10)
            if res.status_code == 200:
                return res.json()
            else:
                print(f"Jupiter quote error ({res.status_code}): {res.text}")
        except Exception as e:
            print(f"Exception during Jupiter quote: {e}")
        return None

    @staticmethod
    def build_and_sign_swap(
        keypair: Keypair,
        quote_response: Dict[str, Any],
        priority_fee: str = "auto",
    ) -> Optional[str]:
        """
        Sends quote to Jupiter /swap endpoint, receives base64 transaction,
        signs it locally with the keypair, and returns base64 signed transaction.
        """
        try:
            payload = {
                "quoteResponse": quote_response,
                "userPublicKey": str(keypair.pubkey()),
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": priority_fee,
            }
            res = requests.post(JUPITER_SWAP_API, json=payload, timeout=12)
            if res.status_code != 200:
                print(f"Jupiter swap build error: {res.text}")
                return None

            swap_tx_b64 = res.json().get("swapTransaction")
            if not swap_tx_b64:
                return None

            # Deserialize transaction
            raw_tx_bytes = base64.b64decode(swap_tx_b64)
            raw_tx = VersionedTransaction.from_bytes(raw_tx_bytes)

            # Sign transaction in memory with user's keypair
            signed_tx = VersionedTransaction(raw_tx.message, [keypair])

            # Return serialized signed base64 transaction
            return base64.b64encode(bytes(signed_tx)).decode("utf-8")

        except Exception as e:
            print(f"Error building and signing swap: {e}")
            return None

    @staticmethod
    def send_and_confirm_transaction(signed_tx_b64: str) -> Tuple[bool, str]:
        """
        Submits the signed transaction to Solana RPC and confirms.
        Returns (success, signature_or_error_message).
        """
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendTransaction",
                "params": [
                    signed_tx_b64,
                    {
                        "encoding": "base64",
                        "skipPreflight": False,
                        "preflightCommitment": "confirmed",
                        "maxRetries": 3,
                    },
                ],
            }
            res = requests.post(RPC_ENDPOINT, json=payload, timeout=15)
            data = res.json()

            if "result" in data:
                sig = data["result"]
                return True, sig
            elif "error" in data:
                err_msg = data["error"].get("message", "Transaction failed")
                return False, err_msg
            else:
                return False, "Unknown RPC response"

        except Exception as e:
            return False, str(e)

    @classmethod
    def execute_buy(
        cls,
        keypair: Keypair,
        token_mint: str,
        sol_amount: float,
        slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Executes a Buy order: swaps SOL -> token_mint.
        Returns (success, signature_or_error, quote_info).
        """
        lamports = int(sol_amount * 1_000_000_000)

        # 1. Fetch Quote
        quote = cls.get_quote(
            input_mint=SOL_MINT,
            output_mint=token_mint,
            amount=lamports,
            slippage_bps=slippage_bps,
        )
        if not quote:
            return False, "Could not find a swap route or liquidity pool for this token.", None

        # 2. Build and Sign Transaction
        signed_tx_b64 = cls.build_and_sign_swap(keypair, quote)
        if not signed_tx_b64:
            return False, "Failed to compile and sign swap transaction.", quote

        # 3. Broadcast to Solana
        success, sig_or_err = cls.send_and_confirm_transaction(signed_tx_b64)
        return success, sig_or_err, quote

    @classmethod
    def execute_sell(
        cls,
        keypair: Keypair,
        token_mint: str,
        percentage: float = 100.0,
        slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Executes a Sell order: swaps token_mint -> SOL.
        Returns (success, signature_or_error, quote_info).
        """
        # 1. Get user's current token balance
        ui_balance, raw_balance = WalletManager.get_token_balance(
            str(keypair.pubkey()), token_mint
        )
        if raw_balance <= 0:
            return False, "You do not hold any balance of this token in your bot wallet.", None

        # Calculate amount to sell
        sell_raw = int(raw_balance * (percentage / 100.0))
        if sell_raw <= 0:
            return False, "Calculated sell amount is 0.", None

        # 2. Fetch Quote (Token -> SOL)
        quote = cls.get_quote(
            input_mint=token_mint,
            output_mint=SOL_MINT,
            amount=sell_raw,
            slippage_bps=slippage_bps,
        )
        if not quote:
            return False, "Could not find liquidity route to sell this token.", None

        # 3. Build and Sign Transaction
        signed_tx_b64 = cls.build_and_sign_swap(keypair, quote)
        if not signed_tx_b64:
            return False, "Failed to compile and sign sell transaction.", quote

        # 4. Broadcast to Solana
        success, sig_or_err = cls.send_and_confirm_transaction(signed_tx_b64)
        return success, sig_or_err, quote
