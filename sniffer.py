import re
import requests
from typing import Optional, Dict, Any, List
from solders.pubkey import Pubkey

DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/tokens/{}"
RUGCHECK_URL = "https://api.rugcheck.xyz/v1/tokens/{}/report"


def format_currency(val: Optional[float], is_usd: bool = True) -> str:
    """Formats numbers to human readable strings ($1.2M, $45.3K, etc.)"""
    if val is None:
        return "N/A"
    prefix = "$" if is_usd else ""
    if abs(val) >= 1_000_000_000:
        return f"{prefix}{val / 1_000_000_000:.2f}B"
    elif abs(val) >= 1_000_000:
        return f"{prefix}{val / 1_000_000:.2f}M"
    elif abs(val) >= 1_000:
        return f"{prefix}{val / 1_000:.2f}K"
    elif abs(val) < 0.0001:
        return f"{prefix}{val:.8f}"
    else:
        return f"{prefix}{val:.4f}"


class MarketSniffer:
    @staticmethod
    def is_valid_solana_address(address: str) -> bool:
        """Validates if a string is a valid Solana Base58 public key"""
        try:
            Pubkey.from_string(address.strip())
            return True
        except Exception:
            return False

    @staticmethod
    def extract_solana_address(text: str) -> Optional[str]:
        """
        Extracts a valid Solana Base58 public key from raw text,
        handling direct CAs, 'CA: <addr>', DexScreener URLs, Pump.fun URLs, etc.
        """
        if not text:
            return None
        cleaned = text.strip()
        try:
            Pubkey.from_string(cleaned)
            return cleaned
        except Exception:
            pass

        tokens = re.findall(r"[1-9A-HJ-NP-Za-km-z]{32,44}", cleaned)
        for tok in tokens:
            try:
                Pubkey.from_string(tok)
                return tok
            except Exception:
                continue
        return None

    @staticmethod
    def get_market_data(mint: str) -> Optional[Dict[str, Any]]:
        """
        Fetches live market metrics from DexScreener.
        """
        try:
            url = DEXSCREENER_URL.format(mint.strip())
            res = requests.get(url, timeout=8)
            if res.status_code != 200:
                return None
            data = res.json()
            pairs = data.get("pairs")
            if not pairs:
                return None

            # Pick the most liquid Solana pair (usually the first or highest liquidity)
            best_pair = None
            highest_liq = -1.0
            for p in pairs:
                if p.get("chainId") == "solana":
                    liq_usd = float(p.get("liquidity", {}).get("usd", 0.0) or 0.0)
                    if liq_usd > highest_liq:
                        highest_liq = liq_usd
                        best_pair = p

            if not best_pair:
                best_pair = pairs[0]

            base = best_pair.get("baseToken", {})
            name = base.get("name", "Unknown Token")
            symbol = base.get("symbol", "TOKEN")
            price_usd = float(best_pair.get("priceUsd") or 0.0)
            price_native = float(best_pair.get("priceNative") or 0.0)
            fdv = float(best_pair.get("fdv") or 0.0)
            liq_usd = float(best_pair.get("liquidity", {}).get("usd") or 0.0)

            # Price changes
            price_change = best_pair.get("priceChange", {})
            m5 = float(price_change.get("m5") or 0.0)
            h1 = float(price_change.get("h1") or 0.0)
            h24 = float(price_change.get("h24") or 0.0)

            volume = best_pair.get("volume", {})
            vol_24h = float(volume.get("h24") or 0.0)

            pair_url = best_pair.get("url") or f"https://dexscreener.com/solana/{mint}"

            return {
                "name": name,
                "symbol": symbol,
                "price_usd": price_usd,
                "price_native": price_native,
                "fdv": fdv,
                "liquidity_usd": liq_usd,
                "m5": m5,
                "h1": h1,
                "h24": h24,
                "vol_24h": vol_24h,
                "pair_url": pair_url,
                "dex": best_pair.get("dexId", "Raydium"),
            }
        except Exception as e:
            print(f"Error fetching DexScreener market data: {e}")
            return None

    @staticmethod
    def get_security_audit(mint: str) -> Dict[str, Any]:
        """
        Audits token security via RugCheck API.
        Verifies freeze authority, mint authority, top holders, and risk factors.
        """
        audit = {
            "mint_authority": "Unknown",
            "freeze_authority": "Unknown",
            "top10_pct": 0.0,
            "lp_locked_pct": 0.0,
            "score": 0,
            "risk_level": "UNKNOWN",
            "warnings": [],
        }

        try:
            url = RUGCHECK_URL.format(mint.strip())
            res = requests.get(url, timeout=8)
            if res.status_code == 200:
                data = res.json()
                token_info = data.get("token", {})

                # 1. Mint Authority
                mint_auth = token_info.get("mintAuthority")
                if mint_auth is None:
                    audit["mint_authority"] = "REVOKED"
                else:
                    audit["mint_authority"] = "ACTIVE"
                    audit["warnings"].append("🚨 Mint Authority is active (Owner can mint infinite tokens)")

                # 2. Freeze Authority
                freeze_auth = token_info.get("freezeAuthority")
                if freeze_auth is None:
                    audit["freeze_authority"] = "REVOKED"
                else:
                    audit["freeze_authority"] = "ACTIVE"
                    audit["warnings"].append("🚨 Freeze Authority is active (Owner can freeze your wallet!)")

                # 3. Top Holders
                top_holders = data.get("topHolders", [])
                if top_holders:
                    top10 = top_holders[:10]
                    top10_sum = sum(float(h.get("pct") or 0.0) for h in top10)
                    audit["top10_pct"] = top10_sum
                    if top10_sum > 50.0:
                        audit["warnings"].append(f"⚠️ Top 10 holders own {top10_sum:.1f}% (High dump risk)")

                # 4. RugCheck Risk Score & Metadata
                score = data.get("score", 0)
                audit["score"] = score
                audit["token_meta"] = data.get("tokenMeta", {})

                for r in data.get("risks", []):
                    desc = r.get("description") or r.get("name")
                    if desc:
                        warn = f"⚠️ {desc}"
                        if warn not in audit["warnings"]:
                            audit["warnings"].append(warn)

                # Determine overall risk rating
                if audit["freeze_authority"] == "ACTIVE" or audit["mint_authority"] == "ACTIVE":
                    audit["risk_level"] = "🚨 HIGH DANGER (HONEYPOT RISK)"
                elif score > 1000 or audit["top10_pct"] > 60:
                    audit["risk_level"] = "⚠️ CAUTION / RISKY"
                else:
                    audit["risk_level"] = "🟢 LOW RISK (SAFE)"
            else:
                audit["risk_level"] = "⚠️ UNVERIFIED (RugCheck API Busy)"
                audit["warnings"].append("⚠️ Security audit temporarily unavailable. Trade with caution!")

        except Exception as e:
            audit["risk_level"] = "⚠️ UNVERIFIED"
            audit["warnings"].append("⚠️ Could not reach security auditor. Verify CA manually before trading.")

        return audit

    @classmethod
    def sniff(cls, mint: str) -> Dict[str, Any]:
        """
        Combines market data and security audit into a unified report.
        """
        mint = mint.strip()
        market = cls.get_market_data(mint) or {
            "name": "Unknown Token",
            "symbol": "TOKEN",
            "price_usd": 0.0,
            "price_native": 0.0,
            "fdv": 0.0,
            "liquidity_usd": 0.0,
            "m5": 0.0,
            "h1": 0.0,
            "h24": 0.0,
            "vol_24h": 0.0,
            "pair_url": f"https://dexscreener.com/solana/{mint}",
            "dex": "DEX",
        }
        security = cls.get_security_audit(mint)

        # Fallback to RugCheck token metadata if DexScreener has no pair info
        if market["name"] == "Unknown Token" and security.get("token_meta"):
            meta = security["token_meta"]
            if meta.get("name"):
                market["name"] = meta["name"]
            if meta.get("symbol"):
                market["symbol"] = meta["symbol"]

        # Liquidity analysis
        liq = market.get("liquidity_usd", 0.0)
        if 0.0 < liq < 5000.0:
            security["warnings"].append(f"⚠️ Low Liquidity (${liq:,.0f}). Severe price impact possible.")
            if security["risk_level"] == "🟢 LOW RISK (SAFE)":
                security["risk_level"] = "⚠️ LOW LIQUIDITY RISK"
        elif liq == 0.0 and not security.get("token_meta"):
            security["warnings"].append("🚨 Zero Liquidity detected! Swaps may fail.")
            security["risk_level"] = "🚨 ZERO LIQUIDITY"

        if "pump" in market.get("dex", "").lower():
            security["warnings"].append("🟢 Pump.fun Curve: Mint & Freeze disabled by program.")

        return {
            "mint": mint,
            "market": market,
            "security": security,
        }
