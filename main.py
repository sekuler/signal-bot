import time
import json
import requests
from datetime import datetime
from pathlib import Path

# ─── AYARLAR ───────────────────────────────────────────────
TELEGRAM_TOKEN = "8892190725:AAFmzgGnH5L-ZDmepaIA1uYpxM5Bbzy7X4A"
CHAT_ID        = "1590986571"
SCAN_INTERVAL  = 90
SEEN_FILE      = "seen_tokens.json"
SOLANA_RPC     = "https://api.mainnet-beta.solana.com"

MAX_TOP10_HOLDER_PCT = 35
MAX_BUNDLE_PCT       = 20
MAX_CREATOR_RUGS     = 2
MIN_LIQUIDITY_USD    = 20000
MIN_VOLUME_5M        = 2000
MIN_CHANGE_5M        = 3.0
MAX_AGE_MINUTES      = 360
WAIT_SECONDS         = 60
MIN_RISK_SCORE       = 40
MIN_MOMENTUM_SCORE   = 30
# ───────────────────────────────────────────────────────────

def load_seen():
    try:
        if Path(SEEN_FILE).exists():
            with open(SEEN_FILE, "r") as f:
                return set(json.load(f))
    except:
        pass
    return set()

def save_seen(seen):
    try:
        with open(SEEN_FILE, "w") as f:
            json.dump(list(seen)[-5000:], f)
    except:
        pass

seen    = load_seen()
pending = {}


def solana_rpc(method, params):
    try:
        r = requests.post(SOLANA_RPC, json={
            "jsonrpc": "2.0", "id": 1,
            "method": method, "params": params
        }, timeout=10)
        if r.ok:
            return r.json().get("result")
    except Exception as e:
        print(f"RPC hata: {e}")
    return None


def check_mint_freeze(token_address):
    warnings = []
    penalty  = 0
    try:
        result = solana_rpc("getAccountInfo", [token_address, {"encoding": "jsonParsed"}])
        if not result:
            return warnings, penalty
        data       = (result.get("value") or {}).get("data") or {}
        token_info = (data.get("parsed") or {}).get("info") or {}
        if token_info.get("mintAuthority"):
            warnings.append("🚨 Mint Authority AÇIK!")
            penalty += 60
        if token_info.get("freezeAuthority"):
            warnings.append("🚨 Freeze Authority AÇIK!")
            penalty += 60
    except Exception as e:
        print(f"Mint/Freeze hata: {e}")
    return warnings, penalty


def rugcheck_solana(token_address):
    warnings = []
    penalty  = 0
    bonus    = 0
    try:
        r = requests.get(f"https://api.rugcheck.xyz/v1/tokens/{token_address}/report", timeout=12)
        if not r.ok:
            return warnings, penalty, bonus
        data = r.json()

        creator_tokens = (data.get("creator") or {}).get("tokens") or []
        rug_count      = sum(1 for t in creator_tokens if t.get("rugged", False))
        total_created  = len(creator_tokens)
        if rug_count >= MAX_CREATOR_RUGS:
            warnings.append(f"🚨 Dev {rug_count} rug yapmış!")
            penalty += 100
        elif rug_count == 1:
            warnings.append("⚠️ Dev 1 rug geçmişi")
            penalty += 30
        if total_created > 15:
            warnings.append(f"⚠️ Dev {total_created} token çıkarmış")
            penalty += 15

        top_holders = data.get("topHolders") or []
        if top_holders:
            top10_pct = sum(h.get("pct", 0) for h in top_holders[:10])
            if top10_pct > MAX_TOP10_HOLDER_PCT:
                warnings.append(f"🚨 Top10 holder %{top10_pct:.0f}!")
                penalty += 50
            elif top10_pct > 25:
                warnings.append(f"⚠️ Top10 holder %{top10_pct:.0f}")
                penalty += 20

        insider_pct = sum(h.get("pct", 0) for h in top_holders if h.get("insider", False))
        if insider_pct > MAX_BUNDLE_PCT:
            warnings.append(f"🚨 Bundle/insider %{insider_pct:.0f}!")
            penalty += 60
        elif insider_pct > 10:
            warnings.append(f"⚠️ Insider %{insider_pct:.0f}")
            penalty += 20

        markets   = data.get("markets") or []
        lp_burned = any(m.get("lp", {}).get("burned") for m in markets)
        lp_locked = any(m.get("lp", {}).get("locked") for m in markets)
        if lp_burned:
            bonus += 25
        elif lp_locked:
            bonus += 10
        else:
            warnings.append("⚠️ LP kilitli/yakılmamış")
            penalty += 20

        rc_score = data.get("score", 0)
        if rc_score < 300:
            warnings.append(f"🚨 RugCheck: {rc_score}")
            penalty += 30
        elif rc_score < 600:
            warnings.append(f"⚠️ RugCheck: {rc_score}")
            penalty += 10
        elif rc_score >= 800:
            bonus += 10

        for risk in (data.get("risks") or []):
            lvl  = risk.get("level", "")
            name = risk.get("name", "")
            if lvl == "danger":
                warnings.append(f"🚨 {name}"); penalty += 25
            elif lvl == "warn":
                warnings.append(f"⚠️ {name}"); penalty += 8

    except Exception as e:
        print(f"RugCheck hata: {e}")
    return warnings, penalty, bonus


def tokensniffer_base(token_address):
    warnings = []
    penalty  = 0
    bonus    = 0
    try:
        r = requests.get(
            f"https://tokensniffer.com/api/v2/tokens/8453/{token_address}?apikey=free&include_metrics=true",
            timeout=12
        )
        if not r.ok:
            return warnings, penalty, bonus
        data = r.json()
        if data.get("is_honeypot"):
            warnings.append("🚨 HONEYPOT!"); penalty += 100
        if data.get("rugged"):
            warnings.append("🚨 Daha önce rug!"); penalty += 100
        ts = data.get("score", 100)
        if ts < 30:
            warnings.append(f"🚨 TokenSniffer: {ts}/100"); penalty += 50
        elif ts < 60:
            warnings.append(f"⚠️ TokenSniffer: {ts}/100"); penalty += 20
        else:
            bonus += 10
        top10 = (data.get("holders") or {}).get("top10_percent", 0) or 0
        if top10 > MAX_TOP10_HOLDER_PCT:
            warnings.append(f"🚨 Top10 %{top10:.0f}!"); penalty += 40
        elif top10 > 25:
            warnings.append(f"⚠️ Top10 %{top10:.0f}"); penalty += 15
    except Exception as e:
        print(f"TokenSniffer hata: {e}")
    return warnings, penalty, bonus


def check_socials(pair):
    info     = pair.get("info") or {}
    socials  = info.get("socials") or []
    websites = info.get("websites") or []
    has_tw   = any(s.get("type") == "twitter"  for s in socials)
    has_tg   = any(s.get("type") == "telegram" for s in socials)
    has_web  = len(websites) > 0
    if not has_tw and not has_tg and not has_web:
        return -999, []
    bonus    = (8 if has_tw else 0) + (5 if has_tg else 0) + (7 if has_web else 0)
    warnings = []
    if not has_tw:  warnings.append("⚠️ Twitter yok")
    if not has_tg:  warnings.append("⚠️ Telegram yok")
    if not has_web: warnings.append("⚠️ Website yok")
    return bonus, warnings


def calc_risk_score(rug_penalty, rug_bonus, mf_penalty, social_bonus):
    score = 100 - (rug_penalty + mf_penalty) + rug_bonus + max(0, social_bonus)
    return max(0, min(100, score))


def calc_momentum_score(pair):
    score   = 0
    reasons = []
    liq      = (pair.get("liquidity")   or {}).get("usd", 0) or 0
    vol5m    = (pair.get("volume")      or {}).get("m5",  0) or 0
    change5m = (pair.get("priceChange") or {}).get("m5",  0) or 0
    change1h = (pair.get("priceChange") or {}).get("h1",  0) or 0
    buys5m   = ((pair.get("txns") or {}).get("m5") or {}).get("buys",  0) or 0
    sells5m  = ((pair.get("txns") or {}).get("m5") or {}).get("sells", 0) or 0
    age_min  = pair.get("_age_minutes", 60)

    if liq >= 500000:
        score += 20; reasons.append("💰 $500K+ likidite")
    elif liq >= 200000:
        score += 15; reasons.append("💰 $200K+ likidite")
    elif liq >= 100000:
        score += 10; reasons.append("💰 $100K+ likidite")
    elif liq >= 50000:
        score += 6;  reasons.append("💰 $50K+ likidite")
    elif liq >= 20000:
        score += 3;  reasons.append("💰 $20K+ likidite")

    vol_ratio = vol5m / liq if liq > 0 else 0
    if vol_ratio > 0.50:
        score += 20; reasons.append("🔥 Çok yüksek hacim/liq")
    elif vol_ratio > 0.20:
        score += 15; reasons.append("📈 Güçlü hacim/liq")
    elif vol_ratio > 0.05:
        score += 8;  reasons.append("📊 Normal hacim")

    if change5m > 30:
        score += 15; reasons.append("🚀 +%30 fiyat")
    elif change5m > 15:
        score += 10; reasons.append("📈 +%15 fiyat")
    elif change5m > 5:
        score += 5;  reasons.append("📈 Pozitif momentum")
    elif change5m < -10:
        score -= 10; reasons.append("⚠️ Sert düşüş")

    if change1h > 50:
        score += 10; reasons.append("📈 +%50 1sa trend")
    elif change1h > 20:
        score += 5;  reasons.append("📈 Pozitif 1sa trend")

    total_tx = buys5m + sells5m
    if total_tx > 0:
        ratio = buys5m / total_tx
        if ratio > 0.80:
            score += 15; reasons.append(f"👥 Çok güçlü alım (%{ratio*100:.0f})")
        elif ratio > 0.65:
            score += 8;  reasons.append(f"👥 Güçlü alım (%{ratio*100:.0f})")
        elif ratio < 0.40:
            score -= 10; reasons.append("⚠️ Satış baskısı")

    if buys5m > 100:
        score += 15; reasons.append("🔥 100+ alım tx/5dk")
    elif buys5m > 50:
        score += 10; reasons.append("🔥 50+ alım tx/5dk")
    elif buys5m > 20:
        score += 5;  reasons.append("📈 20+ alım tx/5dk")

    if age_min < 10:
        score += 8;  reasons.append("⚡ <10dk yeni")
    elif age_min < 30:
        score += 5;  reasons.append("⚡ <30dk yeni")
    elif age_min < 60:
        score += 3;  reasons.append("🕐 <60dk taze")

    return max(0, min(100, score)), reasons


def fetch_pairs():
    results = []
    for chain in ["solana", "base"]:
        try:
            r = requests.get("https://api.dexscreener.com/token-profiles/latest/v1", timeout=10)
            if not r.ok:
                continue
            profiles = [p for p in (r.json() or []) if p.get("chainId") == chain and p.get("tokenAddress")]
            addrs    = [p.get("tokenAddress") for p in profiles]
            for i in range(0, len(addrs), 30):
                batch = addrs[i:i+30]
                try:
                    r2 = requests.get(
                        f"https://api.dexscreener.com/latest/dex/tokens/{','.join(batch)}",
                        timeout=10
                    )
                    if r2.ok:
                        pairs = r2.json().get("pairs") or []
                        for p in pairs:
                            p["_chain"] = chain
                        results.extend(pairs)
                    time.sleep(0.3)
                except Exception as e:
                    print(f"[{chain}] batch hata: {e}")
        except Exception as e:
            print(f"[{chain}] fetch hata: {e}")
    print(f"Toplam {len(results)} pair çekildi")
    return results


def send_telegram(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10
        )
    except Exception as e:
        print(f"TG hata: {e}")


def fmt(n):
    if not n: return "?"
    if n >= 1_000_000: return f"${n/1_000_000:.1f}M"
    if n >= 1_000:     return f"${n/1_000:.0f}K"
    return f"${n:.0f}"


def scan():
    now_ms = time.time() * 1000
    now_s  = time.time()
    found  = 0
    pairs  = fetch_pairs()

    for pair in pairs:
        addr  = pair.get("pairAddress", "")
        chain = pair.get("_chain", "")
        if not addr or addr in seen:
            continue

        created  = pair.get("pairCreatedAt") or 0
        age_min  = (now_ms - created) / 60000 if created else 999
        liq      = (pair.get("liquidity")   or {}).get("usd", 0) or 0
        vol5m    = (pair.get("volume")      or {}).get("m5",  0) or 0
        change5m = (pair.get("priceChange") or {}).get("m5",  0) or 0

        if age_min  > MAX_AGE_MINUTES:   continue
        if liq      < MIN_LIQUIDITY_USD: continue
        if vol5m    < MIN_VOLUME_5M:     continue
        if change5m < MIN_CHANGE_5M:     continue

        social_bonus, social_warnings = check_socials(pair)
        if social_bonus == -999:
            seen.add(addr)
            print(f"[SKIP-SOSYAL] {(pair.get('baseToken') or {}).get('symbol')}")
            continue

        if addr not in pending:
            pending[addr] = now_s
            print(f"[PENDING] {(pair.get('baseToken') or {}).get('symbol')}")
            continue
        if now_s - pending[addr] < WAIT_SECONDS:
            continue

        token_ca = (pair.get("baseToken") or {}).get("address", "")
        pair["_age_minutes"] = age_min

        mf_warnings, mf_penalty = [], 0
        if chain == "solana" and token_ca:
            mf_warnings, mf_penalty = check_mint_freeze(token_ca)
            if mf_penalty >= 60:
                seen.add(addr); pending.pop(addr, None)
                print(f"[SKIP-AUTH] {(pair.get('baseToken') or {}).get('symbol')}")
                continue

        rug_warnings, rug_penalty, rug_bonus = [], 0, 0
        if chain == "solana" and token_ca:
            rug_warnings, rug_penalty, rug_bonus = rugcheck_solana(token_ca)
        elif chain == "base" and token_ca:
            rug_warnings, rug_penalty, rug_bonus = tokensniffer_base(token_ca)

        if (mf_penalty + rug_penalty) >= 80:
            seen.add(addr); pending.pop(addr, None)
            print(f"[SKIP-RUG] {(pair.get('baseToken') or {}).get('symbol')}")
            continue

        risk_score             = calc_risk_score(rug_penalty, rug_bonus, mf_penalty, social_bonus)
        mom_score, mom_reasons = calc_momentum_score(pair)

        if risk_score < MIN_RISK_SCORE or mom_score < MIN_MOMENTUM_SCORE:
            seen.add(addr); pending.pop(addr, None); continue

        final_score = int((risk_score + mom_score) / 2)
        seen.add(addr); pending.pop(addr, None); save_seen(seen)
        found += 1

        if final_score >= 75:   grade, emoji = "S", "🏆"
        elif final_score >= 60: grade, emoji = "A", "🥇"
        elif final_score >= 45: grade, emoji = "B", "🥈"
        else:                   grade, emoji = "C", "🥉"

        symbol      = (pair.get("baseToken") or {}).get("symbol", "?")
        dex_url     = pair.get("url", "")
        chain_label = "🟣 Solana" if "sol" in chain.lower() else "🔵 Base"
        buys5m      = ((pair.get("txns") or {}).get("m5") or {}).get("buys",  0) or 0
        sells5m     = ((pair.get("txns") or {}).get("m5") or {}).get("sells", 0) or 0

        all_warnings = mf_warnings + rug_warnings + social_warnings
        mom_txt      = "\n".join(f"  • {r}" for r in mom_reasons[:5])
        warn_txt     = ("\n\n⚠️ <b>Uyarılar:</b>\n" + "\n".join(f"  {w}" for w in all_warnings[:5])
                       ) if all_warnings else "\n\n✅ <b>Tüm kontroller temiz</b>"

        info     = pair.get("info") or {}
        socials  = info.get("socials") or []
        websites = info.get("websites") or []
        links    = ""
        for s in socials:
            if s.get("type") == "twitter":
                links += f"🐦<a href='{s.get('url')}'>TW</a>  "
            elif s.get("type") == "telegram":
                links += f"✈️<a href='{s.get('url')}'>TG</a>  "
        if websites:
            links += f"🌐<a href='{websites[0].get('url')}'>WEB</a>"

        msg = (
            f"{emoji} <b>Grade {grade} — {final_score}/100</b>\n"
            f"🛡 Risk: {risk_score}/100  |  ⚡ Momentum: {mom_score}/100\n\n"
            f"🪙 <b>{symbol}</b> · {chain_label}\n"
            f"📋 <code>{token_ca}</code>\n"
            f"⏱ Yaş: {age_min:.0f} dk\n\n"
            f"💰 Likidite: {fmt(liq)}\n"
            f"📊 5dk Hacim: {fmt(vol5m)}\n"
            f"📈 5dk Değişim: %{change5m:+.2f}\n"
            f"👥 Buy/Sell: {buys5m} / {sells5m}\n\n"
            f"<b>Momentum:</b>\n{mom_txt}"
            f"{warn_txt}\n\n"
            f"{links}\n"
            f"🔗 <a href='{dex_url}'>DexScreener</a>"
        )

        send_telegram(msg)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ {symbol} {grade} Risk:{risk_score} Mom:{mom_score}")

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Tarama bitti — {found} yeni token")


def main():
    print("=" * 55)
    print("  SIGNAL Bot v9 — Çift Skor Sistemi")
    print("=" * 55)
    send_telegram(
        "🤖 <b>SIGNAL Bot v9 başlatıldı</b>\n\n"
        "<b>Yenilikler:</b>\n"
        "  🛡 Risk Skoru (Mint/Freeze/LP/Holder/Creator)\n"
        "  ⚡ Momentum Skoru (Hacim/Fiyat/Buy-Sell)\n"
        "  📦 Tüm tokenler 30'luk gruplar halinde\n"
        "  💾 Seen listesi dosyaya kaydediliyor\n\n"
        "Solana + Base 🚀"
    )
    while True:
        try:
            scan()
        except Exception as e:
            print(f"Ana hata: {e}")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
