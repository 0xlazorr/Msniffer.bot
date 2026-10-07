import os
import json
import requests
from typing import Optional, Dict, Any, List, Tuple

from config import (
    AI_PROVIDER,
    AI_API_KEY,
    AI_MODEL,
)
from sniffer import MarketSniffer
from wallet_manager import WalletManager
from jupiter_swap import JupiterSwapEngine


class AITradingEngine:
    """
    Intelligent Autonomous Trading Engine.
    Supports OpenAI, Grok (xAI), Anthropic Claude, and Gemini,
    with an institutional Quantitative Rule-Based fallback.
    """

    @classmethod
    def get_status(cls) -> Dict[str, Any]:
        """Returns current AI configuration state"""
        has_key = bool(AI_API_KEY)
        provider = AI_PROVIDER.capitalize() if AI_PROVIDER else "OpenAI"
        model = AI_MODEL or ("gpt-4o-mini" if provider.lower() == "openai" else "grok-beta")
        mode_desc = f"{provider} ({model})" if has_key else "Smart Quantitative Algorithmic AI"
        return {
            "has_api_key": has_key,
            "provider": provider,
            "model": model,
            "description": mode_desc,
        }

    @classmethod
    def consult_llm(cls, market: dict, security: dict, strategy: str) -> Optional[dict]:
        """
        Sends structured market telemetry to configured LLM (OpenAI, Grok, Anthropic).
        Returns parsed JSON trade signal or None.
        """
        if not AI_API_KEY:
            return None

        prompt = f"""
You are an expert Solana algorithmic crypto trading agent.
Analyze the following live token metrics and security audit to determine if an automated scalp buy is warranted.

STRATEGY: {strategy}
TOKEN NAME: {market.get('name')} (${market.get('symbol')})
PRICE: ${market.get('price_usd')}
LIQUIDITY: ${market.get('liquidity_usd'):,.0f}
MARKET CAP: ${market.get('fdv'):,.0f}
CHANGES: 5m: {market.get('m5')}%, 1h: {market.get('h1')}%, 24h: {market.get('h24')}%
FREEZE AUTHORITY: {security.get('freeze_authority')}
MINT AUTHORITY: {security.get('mint_authority')}
TOP 10 HOLDERS: {security.get('top10_pct')}%
SECURITY RISK: {security.get('risk_level')}
DETECTED WARNINGS: {', '.join(security.get('warnings', []))}

Respond STRICTLY in JSON format with no markdown wrappers:
{{
  "decision": "BUY" or "SKIP",
  "confidence": 0.0 to 1.0,
  "reason": "short explanation under 15 words"
}}
"""
        try:
            provider = AI_PROVIDER.lower()
            if provider in ("openai", "grok", "xai"):
                url = (
                    "https://api.x.ai/v1/chat/completions"
                    if provider in ("grok", "xai")
                    else "https://api.openai.com/v1/chat/completions"
                )
                headers = {
                    "Authorization": f"Bearer {AI_API_KEY}",
                    "Content-Type": "application/json",
                }
                model_name = AI_MODEL or ("grok-beta" if "grok" in provider else "gpt-4o-mini")
                payload = {
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": "You are a quantitative Solana sniper bot."},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.2,
                }
                res = requests.post(url, json=payload, headers=headers, timeout=10)
                if res.status_code == 200:
                    content = res.json()["choices"][0]["message"]["content"].strip()
                    if content.startswith("```"):
                        content = content.split("```")[1]
                        if content.startswith("json"):
                            content = content[4:]
                    return json.loads(content)

            elif provider == "anthropic":
                url = "https://api.anthropic.com/v1/messages"
                headers = {
                    "x-api-key": AI_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                }
                payload = {
                    "model": AI_MODEL or "claude-3-5-sonnet-20241022",
                    "max_tokens": 150,
                    "messages": [{"role": "user", "content": prompt}],
                }
                res = requests.post(url, json=payload, headers=headers, timeout=10)
                if res.status_code == 200:
                    content = res.json()["content"][0]["text"].strip()
                    return json.loads(content)

        except Exception as e:
            print(f"LLM consultation error: {e}")

        return None

    @classmethod
    def evaluate_quant_rules(cls, market: dict, security: dict, strategy: str) -> dict:
        """
        Rule-based high-winrate algorithmic evaluator.
        Enforces strict safety: honeypots rejected, high concentration rejected.
        """
        # Hard safety filters (applies to all strategies)
        if security.get("freeze_authority") == "ACTIVE":
            return {"decision": "SKIP", "confidence": 0.0, "reason": "Honeypot: Freeze authority active"}
        if security.get("mint_authority") == "ACTIVE":
            return {"decision": "SKIP", "confidence": 0.0, "reason": "Danger: Mint authority active"}

        liq = market.get("liquidity_usd", 0.0)
        top10 = security.get("top10_pct", 0.0)
        m5 = market.get("m5", 0.0)
        h1 = market.get("h1", 0.0)

        if strategy == "CONSERVATIVE":
            if liq < 50_000:
                return {"decision": "SKIP", "confidence": 0.2, "reason": "Liquidity under $50K threshold"}
            if top10 > 45.0:
                return {"decision": "SKIP", "confidence": 0.3, "reason": "Top 10 concentration > 45%"}
            if m5 < 1.0 or h1 < 3.0:
                return {"decision": "SKIP", "confidence": 0.4, "reason": "Insufficient momentum"}
            return {"decision": "BUY", "confidence": 0.88, "reason": "High liquidity & steady positive momentum"}

        elif strategy == "DEGEN":
            if liq < 4_000:
                return {"decision": "SKIP", "confidence": 0.1, "reason": "Liquidity too thin (<$4K)"}
            if m5 > 8.0 and top10 < 65.0:
                return {"decision": "BUY", "confidence": 0.85, "reason": "Strong 5m breakout momentum"}
            return {"decision": "SKIP", "confidence": 0.5, "reason": "Waiting for breakout spike"}

        else:  # BALANCED
            if liq < 15_000:
                return {"decision": "SKIP", "confidence": 0.2, "reason": "Liquidity under $15K threshold"}
            if top10 > 55.0:
                return {"decision": "SKIP", "confidence": 0.3, "reason": "Top 10 concentration > 55%"}
            if m5 > 2.0 or (h1 > 5.0 and m5 >= 0.0):
                return {"decision": "BUY", "confidence": 0.82, "reason": "Favorable volume & clean safety audit"}
            return {"decision": "SKIP", "confidence": 0.4, "reason": "Consolidation phase"}

    @classmethod
    def evaluate_token(cls, mint: str, strategy: str = "BALANCED") -> dict:
        """Evaluates token using LLM if configured, else Quant rules"""
        report = MarketSniffer.sniff(mint)
        market = report.get("market", {})
        security = report.get("security", {})

        # Try LLM first if API key configured
        if AI_API_KEY:
            llm_result = cls.consult_llm(market, security, strategy)
            if llm_result and "decision" in llm_result:
                llm_result["market"] = market
                llm_result["security"] = security
                return llm_result

        # Quant fallback
        res = cls.evaluate_quant_rules(market, security, strategy)
        res["market"] = market
        res["security"] = security
        return res

    @classmethod
    def check_position_exits(
        cls,
        user_id: int,
        entry_prices: Dict[str, float],
        take_profit_pct: float = 30.0,
        stop_loss_pct: float = 15.0,
    ) -> List[dict]:
        """
        Monitors open token positions for Take Profit / Stop Loss conditions.
        Returns list of executed sell orders.
        """
        keypair = WalletManager.get_keypair(user_id)
        if not keypair:
            return []

        pubkey = str(keypair.pubkey())
        positions = WalletManager.get_all_positions(pubkey)
        exits = []

        for p in positions:
            mint = p["mint"]
            ui_amt = p["ui_amount"]
            if ui_amt <= 0.0001:
                continue

            entry = entry_prices.get(mint)
            if not entry or entry <= 0:
                continue

            market = MarketSniffer.get_market_data(mint)
            if not market:
                continue

            current_price = market.get("price_usd", 0.0)
            if current_price <= 0:
                continue

            pct_change = ((current_price - entry) / entry) * 100.0

            # Take Profit Trigger
            if pct_change >= take_profit_pct:
                success, sig_or_err, _ = JupiterSwapEngine.execute_sell(
                    keypair, mint, percentage=100.0
                )
                exits.append({
                    "action": "TAKE_PROFIT",
                    "mint": mint,
                    "symbol": market.get("symbol", "TOKEN"),
                    "gain_pct": pct_change,
                    "success": success,
                    "sig": sig_or_err if success else "",
                })

            # Stop Loss Trigger
            elif pct_change <= -stop_loss_pct:
                success, sig_or_err, _ = JupiterSwapEngine.execute_sell(
                    keypair, mint, percentage=100.0
                )
                exits.append({
                    "action": "STOP_LOSS",
                    "mint": mint,
                    "symbol": market.get("symbol", "TOKEN"),
                    "loss_pct": abs(pct_change),
                    "success": success,
                    "sig": sig_or_err if success else "",
                })

        return exits
