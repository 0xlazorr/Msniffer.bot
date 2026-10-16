import os
import re
import json
import time
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
    Operates via full Algorithmic & Pattern Recognition by default (zero API key needed),
    and seamlessly plugs in OpenAI, Grok (xAI), or Anthropic Claude when configured.
    """

    # Memory cache to throttle repeated evaluations of recently audited mints
    _recently_checked_mints: Dict[str, float] = {}

    @classmethod
    def get_status(cls) -> Dict[str, Any]:
        """Returns current AI / Algorithmic configuration state"""
        has_key = bool(AI_API_KEY)
        provider = AI_PROVIDER.capitalize() if AI_PROVIDER else "OpenAI"
        model = AI_MODEL or ("gpt-4o-mini" if provider.lower() == "openai" else "grok-beta")
        mode_desc = f"{provider} ({model})" if has_key else "⚡ Autonomous Algorithmic & Pattern Recognition"
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

        clean_name = re.sub(r"[^\w\s\.-]", "", str(market.get("name", "Unknown")))[:30]
        clean_symbol = re.sub(r"[^\w\s\.-]", "", str(market.get("symbol", "TOKEN")))[:10]

        prompt = f"""
You are an expert Solana algorithmic crypto trading agent.
Analyze the following live token metrics and security audit to determine if an automated scalp buy is warranted.

STRATEGY: {strategy}
TOKEN NAME: {clean_name} (${clean_symbol})
PRICE: ${market.get('price_usd')}
LIQUIDITY: ${market.get('liquidity_usd', 0.0):,.0f}
MARKET CAP: ${market.get('fdv', 0.0):,.0f}
CHANGES: 5m: {market.get('m5') or 0.0}%, 1h: {market.get('h1') or 0.0}%, 24h: {market.get('h24') or 0.0}%
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
                    data = res.json()
                    choices = data.get("choices") or []
                    if choices:
                        raw_content = choices[0].get("message", {}).get("content", "").strip()
                        match = re.search(r"\{.*\}", raw_content, re.DOTALL)
                        if match:
                            parsed = json.loads(match.group(0))
                            decision = str(parsed.get("decision", "SKIP")).upper()
                            conf = max(0.0, min(float(parsed.get("confidence") or 0.5), 1.0))
                            return {
                                "decision": "BUY" if decision == "BUY" else "SKIP",
                                "confidence": conf,
                                "reason": str(parsed.get("reason", "LLM decision")),
                            }

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
                    data = res.json()
                    content_blocks = data.get("content") or []
                    if content_blocks:
                        raw_content = content_blocks[0].get("text", "").strip()
                        match = re.search(r"\{.*\}", raw_content, re.DOTALL)
                        if match:
                            parsed = json.loads(match.group(0))
                            decision = str(parsed.get("decision", "SKIP")).upper()
                            conf = max(0.0, min(float(parsed.get("confidence") or 0.5), 1.0))
                            return {
                                "decision": "BUY" if decision == "BUY" else "SKIP",
                                "confidence": conf,
                                "reason": str(parsed.get("reason", "LLM decision")),
                            }

        except Exception as e:
            print(f"LLM consultation error: {e}")

        return None

    @classmethod
    def evaluate_quant_rules(cls, market: dict, security: dict, strategy: str) -> dict:
        """
        Institutional Pattern Recognition & Rule-Based Algorithmic Evaluator.
        Operates fully autonomously without needing external AI API keys.
        """
        # 1. Hard safety filters (applies to all strategies)
        if security.get("freeze_authority") == "ACTIVE":
            return {"decision": "SKIP", "confidence": 0.0, "score": 0, "reason": "Honeypot: Freeze authority active", "pattern": "Honeypot Trap"}
        if security.get("mint_authority") == "ACTIVE":
            return {"decision": "SKIP", "confidence": 0.0, "score": 0, "reason": "Danger: Mint authority active", "pattern": "Inflation Trap"}

        risk_tag = str(security.get("risk_tag", "")).upper()
        risk_str = str(security.get("risk_level", "")).upper()
        if risk_tag == "DANGER" or any(d in risk_str for d in ("DANGER", "HIGH RISK", "HONEYPOT", "ZERO LIQUIDITY")):
            return {"decision": "SKIP", "confidence": 0.0, "score": 0, "reason": "Security: High risk warnings detected", "pattern": "Malicious Rug"}

        liq = market.get("liquidity_usd", 0.0)
        top10 = security.get("top10_pct", 0.0)
        m5 = market.get("m5", 0.0)
        h1 = market.get("h1", 0.0)
        buys_m5 = market.get("buys_m5", 0)
        sells_m5 = market.get("sells_m5", 0)

        # Pattern scoring: 0 - 100
        score = 50
        pattern_name = "Neutral Momentum"

        # Pattern 1: Buy Pressure vs Sell Pressure
        if buys_m5 > (sells_m5 * 1.3) and buys_m5 >= 5:
            score += 20
            pattern_name = "Bullish Accumulation & Buy Pressure"
        elif buys_m5 >= sells_m5 and buys_m5 >= 2:
            score += 10
            pattern_name = "Positive Momentum Flow"
        elif sells_m5 > (buys_m5 * 1.5):
            score -= 20
            pattern_name = "Sell Distribution / Dumping"

        # Pattern 2: Short-term price breakout
        if m5 > 2.5:
            score += 15
        elif m5 >= 0:
            score += 5
        else:
            score -= 10

        # Pattern 3: Medium-term trend (1h)
        if 2.0 <= h1 <= 80.0:
            score += 15
        elif h1 > 80.0:
            score -= 5  # Overextended pump risk
        elif h1 < -10.0:
            score -= 15

        # Pattern 4: Holder decentralization
        if top10 < 30.0:
            score += 15
        elif top10 < 50.0:
            score += 5
        elif top10 > 65.0:
            score -= 20

        # Strategy threshold enforcement
        if strategy == "CONSERVATIVE":
            if liq < 50_000:
                return {"decision": "SKIP", "confidence": 0.2, "score": score, "reason": "Liquidity under $50K threshold", "pattern": pattern_name}
            if top10 > 40.0:
                return {"decision": "SKIP", "confidence": 0.3, "score": score, "reason": "Top 10 concentration > 40%", "pattern": pattern_name}
            if score >= 75 and m5 > 0.5:
                return {"decision": "BUY", "confidence": min(0.95, score / 100.0), "score": score, "reason": "High liquidity & steady breakout pattern", "pattern": pattern_name}
            return {"decision": "SKIP", "confidence": 0.4, "score": score, "reason": "Did not meet conservative pattern score", "pattern": pattern_name}

        elif strategy == "DEGEN":
            if liq < 4_000:
                return {"decision": "SKIP", "confidence": 0.1, "score": score, "reason": "Liquidity too thin (<$4K)", "pattern": pattern_name}
            if score >= 60 and m5 > 1.5:
                return {"decision": "BUY", "confidence": min(0.92, score / 100.0), "score": score, "reason": "Aggressive early momentum breakout", "pattern": pattern_name}
            return {"decision": "SKIP", "confidence": 0.3, "score": score, "reason": "Waiting for volume spike", "pattern": pattern_name}

        else:  # BALANCED
            if liq < 15_000:
                return {"decision": "SKIP", "confidence": 0.2, "score": score, "reason": "Liquidity under $15K threshold", "pattern": pattern_name}
            if top10 > 55.0:
                return {"decision": "SKIP", "confidence": 0.3, "score": score, "reason": "Top 10 concentration > 55%", "pattern": pattern_name}
            if score >= 70 and m5 > 0.5:
                return {"decision": "BUY", "confidence": min(0.90, score / 100.0), "score": score, "reason": "Strong volume accumulation & clean safety pattern", "pattern": pattern_name}
            return {"decision": "SKIP", "confidence": 0.4, "score": score, "reason": "Did not meet balanced pattern threshold", "pattern": pattern_name}

    @classmethod
    def evaluate_token(
        cls,
        mint: str,
        strategy: str = "BALANCED",
        prefetched_market: Optional[dict] = None,
    ) -> dict:
        """Evaluates token using LLM if configured, else Quant rules"""
        if prefetched_market:
            market = prefetched_market
            security = MarketSniffer.get_security_audit(mint)
        else:
            report = MarketSniffer.sniff(mint)
            market = report.get("market", {})
            security = report.get("security", {})

        # Try LLM first if API key configured
        if AI_API_KEY:
            llm_result = cls.consult_llm(market, security, strategy)
            if llm_result and "decision" in llm_result:
                # Enforce hard safety rules even if LLM said BUY
                if llm_result["decision"] == "BUY":
                    risk_tag = str(security.get("risk_tag", "")).upper()
                    risk_str = str(security.get("risk_level", "")).upper()
                    if (
                        security.get("freeze_authority") == "ACTIVE"
                        or security.get("mint_authority") == "ACTIVE"
                        or risk_tag == "DANGER"
                        or any(d in risk_str for d in ("DANGER", "HONEYPOT", "ZERO LIQUIDITY"))
                    ):
                        llm_result["decision"] = "SKIP"
                        llm_result["reason"] = "Honeypot / Security risk override"
                llm_result["market"] = market
                llm_result["security"] = security
                return llm_result

        # Algorithmic pattern recognition fallback
        res = cls.evaluate_quant_rules(market, security, strategy)
        res["market"] = market
        res["security"] = security
        return res

    @classmethod
    def scan_and_select_trade_candidate(
        cls,
        strategy: str = "BALANCED",
        user_positions: Optional[List[str]] = None
    ) -> Optional[dict]:
        """
        Scans live Solana market for tokens matching breakout & volume patterns.
        Filters candidates through fast market pre-filters, safety gates, and strategy criteria.
        Returns top trade candidate or None.
        """
        user_positions = user_positions or []
        mints = MarketSniffer.get_trending_solana_mints(limit=15)
        best_candidate = None
        highest_score = -1.0

        min_liq = 50_000 if strategy == "CONSERVATIVE" else (15_000 if strategy == "BALANCED" else 4_000)

        now = time.time()
        for mint in mints:
            if mint in user_positions:
                continue

            # Don't re-audit same token if evaluated in last 3 minutes
            last_checked = cls._recently_checked_mints.get(mint, 0)
            if (now - last_checked) < 180:
                continue

            cls._recently_checked_mints[mint] = now
            if len(cls._recently_checked_mints) > 200:
                cls._recently_checked_mints.pop(next(iter(cls._recently_checked_mints)))

            try:
                # Fast pre-filter: Check DexScreener market first
                m_data = MarketSniffer.get_market_data(mint)
                if not m_data or m_data.get("liquidity_usd", 0.0) < min_liq or m_data.get("m5", -1.0) < 0.0:
                    continue

                # Pass pre-fetched market data to avoid duplicate DexScreener calls
                eval_res = cls.evaluate_token(mint, strategy=strategy, prefetched_market=m_data)
                if eval_res.get("decision") == "BUY":
                    score = float(eval_res.get("score") or (eval_res.get("confidence", 0.0) * 100))
                    if score > highest_score:
                        highest_score = score
                        best_candidate = {
                            "mint": mint,
                            "market": eval_res.get("market", {}),
                            "security": eval_res.get("security", {}),
                            "confidence": eval_res.get("confidence", 0.0),
                            "score": score,
                            "pattern": eval_res.get("pattern", "Volume Surge & Breakout"),
                            "reason": eval_res.get("reason", "Pattern confirmed"),
                        }
                # Short throttle to be polite to external APIs
                time.sleep(0.1)
            except Exception as e:
                print(f"Error evaluating candidate {mint}: {e}")

        return best_candidate

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
        Returns list of executed sell orders, handling each position independently.
        """
        keypair = WalletManager.get_keypair(user_id)
        if not keypair:
            return []

        pubkey = str(keypair.pubkey())
        positions = WalletManager.get_all_positions(pubkey)
        exits = []

        for p in positions:
            try:
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
            except Exception as e:
                print(f"Error checking position exit for user {user_id}, mint {p.get('mint')}: {e}")

        return exits

