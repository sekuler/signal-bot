import time
import requests
from datetime import datetime

TELEGRAM_TOKEN = "8892190725:AAFmzgGnH5L-ZDmepaIA1uYpxM5Bbzy7X4A"
CHAT_ID = "1590986571"
SCAN_INTERVAL = 90
MIN_SCORE = 40
seen = set()
pending = {}


def check_socials(pair):
    """DexScreener'dan sosyal medya kontrolü"""
    bonus = 0
    warnings = []
    info = pair.get("info") or {}
    socials = info.get("socials") or []
    websites = info.get("websites") or []

    has_twitter = any(s.get("type") == "twitter" for s in socials)
    has_telegram = any(s.get("type") == "telegram" for s in socials)
    has_website = len(websites) > 0

    if not has_twitter and not has_telegram and not has_website:
        warnings.append("🚨 Sosyal medya yok — yüksek rug riski!")
        return -20, warnings

    if has_twitter:
        bonus += 8
    if has_telegram:
        bonus += 5
    if has_website:
        bonus += 7

    if not has_twitter:
        warnings.append("⚠️ Twitter/X hesabı yok")
    if not has_telegram:
        warnings.append("⚠️ Telegram yok")
    if not has_website:
        warnings.append("⚠️ Website yok")

    return bonus, warnings


def rugcheck_solana(token_address):
    warnings = []
    penalty = 0
    score_bonus = 0
    try:
        url = f"https://api.rugcheck.xyz/v1/tokens/{token_address}/report"
        r = requests.get(url, timeout=10)
        if not r.ok:
            return warnings, penalty, score_bonus

        data = r.json()

        creator = data.get("creator") or {}
        creator_tokens = creator.get("tokens") or []
        rug_count = sum(1 for t in creator_tokens if t.get("rugged", False))
        total_created = len(creator_tokens)

        if rug_count > 0:
            warnings.append(f"🚨 Dev daha önce {rug_count} rug yapmış!")
            penalty += 50
        if total_created > 10:
            warnings.append(f"⚠️ Dev {total_created} token çıkarmış")
            penalty += 20

        top_holders = data.get("topHolders") or []
        if top_holders:
            top10_pct = sum(h.get("pct", 0) for h in top_holders[:10])
            if top10_pct > 50:
                warnings.append(f"🚨 Top 10 holder %{top10_pct:.0f} tutuyor!")
                penalty += 40
            elif top10_pct > 25:
                warnings.append(f"⚠️ Top 10 holder %{top10_pct:.0f} tutuyor")
                penalty += 20

        insider_pct = sum(h.get("pct", 0) for h in top_holders if h.get("insider", False))
        if insider_pct > 10:
            warnings.append(f"🚨 Bundle/insider %{insider_pct:.0f} tutuyor!")
            penalty += 40

        markets = data.get("markets") or []
        lp_locked = any(m.get("lp", {}).get("locked", False) for m in markets)
        if not lp_locked:
            warnings.append("⚠️ LP kilidi yok")
            penalty += 15
        else:
            score_bonus += 10

        rugcheck_score = data.get("score", 0)
        if rugcheck_score < 500:
            warnings.append(f"⚠️ RugCheck skoru: {rugcheck_score}")
            penalty += 15

        risks = data.get("risks") or []
        for risk in risks:
            level = risk.get("level", "")
            name = risk.get("name", "")
            if level == "danger":
                warnings.append(f"🚨 {name}")
                penalty += 25
            elif level == "warn":
                warnings.append(f"⚠️ {name}")
                penalty += 8

    except Exception as e:
        print(f"RugCheck hata: {e}")

    return warnings, penalty, score_bonus


def tokensniffer_base(token_address):
    warnings = []
    penalty = 0
    score_bonus = 0
    try:
        url = f"https://tokensniffer.com/api/v2/tokens/8453/{token_address}?apikey=free&include_metrics=true"
        r = requests.get(url, timeout=10)
        if not r.ok:
            return warnings, penalty, score_bonus

        data = r.json()
        if data.get("is_honeypot"):
            warnings.append("🚨 HONEYPOT — satış engellenmiş!")
            penalty += 100
        if data.get("rugged"):
            warnings.append("🚨 Daha önce rug yapmış!")
            penalty += 100
        ts_score = data.get("score", 100)
        if ts_score < 30:
            warnings.append(f"🚨 Token Sniffer: {ts_score}/100")
            penalty += 40
        elif ts_score < 60:
            warnings.append(f"⚠️ Token Sniffer: {ts_score}/100")
            penalty += 15
        else:
            score_bonus += 10

    except Exception as e:
        print(f"TokenSniffer hata: {e}")

    return warnings, penalty, score_bonus


def check_momentum(pair):
    bonus = 0
    notes = []
    buys5m = ((pair.get("txns") or {}).get("m5") or {}).get("buys", 0) or 0
    sells5m = ((pair.get("txns") or {}).get("m5") or {}).get("sells", 0) or 0

    if buys5m > 50:
        bonus += 10
        notes.append("🔥 Çok yoğun alım (50+ tx/5dk)")
    elif buys5m > 20:
        bonus += 5
        notes.append("📈 Yoğun alım (20+ tx/5dk)")

    total = buys5m + sells5m
    if total > 0:
        ratio = buys5m / total
        if ratio > 0.8:
            bonus += 10
            notes.append(f"👥 Çok güçlü alım baskısı (%{ratio*100:.0f} buy)")
        elif ratio > 0.65:
            bonus += 5
            notes.append(f"👥 Güçlü alım baskısı (%{ratio*100:.0f} buy)")

    return bonus, notes


def score_token(pair):
    score = 0
    reasons = []
    liq = (pair.get("liquidity") or {}).get("usd", 0) or 0
    vol5m = (pair.get("volume") or {}).get("m5", 0) or 0
    change5m = (pair.get("priceChange") or {}).get("m5", 0) or 0
    age_min = pair.get("_age_minutes", 60)

    if liq >= 500000:
        score += 25
        reasons.append("💰 Yüksek likidite ($500K+)")
    elif liq >= 100000:
        score += 20
        reasons.append("💰 İyi likidite ($100K+)")
    elif liq >= 50000:
        score += 15
        reasons.append("💰 Orta likidite ($50K+)")
    elif liq >= 20000:
        score += 8
        reasons.append("💰 Düşük likidite ($20K+)")

    vol_ratio = vol5m / liq if liq > 0 else 0
    if vol_ratio > 0.30:
        score += 20
        reasons.append("🔥 Çok yüksek hacim/liq oranı")
    elif vol_ratio > 0.10:
        score += 12
        reasons.append("📈 Güçlü hacim")
    elif vol_ratio > 0.02:
        score += 6
        reasons.append("📊 Normal hacim")

    if change5m > 20:
        score += 15
        reasons.append("🚀 +%20 fiyat hareketi")
    elif change5m > 10:
        score += 10
        reasons.append("📈 +%10 fiyat hareketi")
    elif change5m > 3:
        score += 5
        reasons.append("📈 Pozitif momentum")

    if age_min < 5:
        score += 5
        reasons.append("⚡ Çok yeni (<5dk)")
    elif age_min < 15:
        score += 8
        reasons.append("⚡ Yeni (<15dk)")
    elif age_min < 30:
        score += 5
        reasons.append("🕐 Taze (<30dk)")

    return max(0, min(100, score)), reasons


def fetch_pairs():
    results = []
    for chain in ["solana", "base"]:
        try:
            r = requests.get("https://api.dexscreener.com/token-profiles/latest/v1", timeout=10)
            if r.ok:
                profiles = r.json() or []
                addrs = [p.get("tokenAddress") for p in profiles if p.get("chainId") == chain and p.get("tokenAddress")]
                if addrs:
                    addr_str = ",".join(addrs[:30])
                    r2 = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{addr_str}", timeout=10)
                    if r2.ok:
                        pairs = r2.json().get("pairs") or []
                        for p in pairs:
                            p["_chain"] = chain
                        results.extend(pairs)
        except Exception as e:
            print(f"[{chain}] hata: {e}")
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
    if not n:
        return "?"
    if n >= 1000000:
        return f"${n/1000000:.1f}M"
    if n >= 1000:
        return f"${n/1000:.0f}K"
    return f"${n:.0f}"


def scan():
    now_ms = time.time() * 1000
    now_s = time.time()
    found = 0
    pairs = fetch_pairs()

    for pair in pairs:
        addr = pair.get("pairAddress", "")
        chain = pair.get("_chain", "")
        if not addr or addr in seen:
            continue

        created = pair.get("pairCreatedAt") or 0
        age_min = (now_ms - created) / 60000 if created else 999
        liq = (pair.get("liquidity") or {}).get("usd", 0) or 0
        vol5m = (pair.get("volume") or {}).get("m5", 0) or 0
        change5m = (pair.get("priceChange") or {}).get("m5", 0) or 0

        if age_min > 360:
            continue
        if liq < 20000:
            continue
        if vol5m < 2000:
            continue
        if change5m < 1.0:
            continue

        # Sosyal medya kontrolü — sosyal medyası hiç yoksa direkt atla
        social_bonus, social_warnings = check_socials(pair)
        if social_bonus == -20:
            seen.add(addr)
            print(f"[SKIP] Sosyal medya yok: {(pair.get('baseToken') or {}).get('symbol')}")
            continue

        # 60 saniye bekleme
        if addr not in pending:
            pending[addr] = {"first_seen": now_s, "pair": pair}
            print(f"[PENDING] {(pair.get('baseToken') or {}).get('symbol')} — 60sn bekleniyor")
            continue

        if time.time() - pending[addr]["first_seen"] < 60:
            continue

        pair["_age_minutes"] = age_min
        score, reasons = score_token(pair)

        # Momentum
        mom_bonus, mom_notes = check_momentum(pair)
        score += mom_bonus
        reasons.extend(mom_notes)

        # Sosyal medya bonusu
        score += social_bonus
        if social_warnings:
            reasons.extend(social_warnings)

        token_ca = (pair.get("baseToken") or {}).get("address", "")

        # Rug kontrolleri
        rug_warnings = []
        penalty = 0
        rug_bonus = 0

        if chain == "solana" and token_ca:
            rug_warnings, penalty, rug_bonus = rugcheck_solana(token_ca)
        elif chain == "base" and token_ca:
            rug_warnings, penalty, rug_bonus = tokensniffer_base(token_ca)

        if any("HONEYPOT" in w or ("🚨" in w and "rug" in w.lower()) for w in rug_warnings):
            seen.add(addr)
            pending.pop(addr, None)
            print(f"[SKIP] Kritik rug: {(pair.get('baseToken') or {}).get('symbol')}")
            continue

        if penalty >= 60:
            seen.add(addr)
            pending.pop(addr, None)
            print(f"[SKIP] Yüksek penalty ({penalty}): {(pair.get('baseToken') or {}).get('symbol')}")
            continue

        score = max(0, min(100, score - penalty + rug_bonus))

        if score < MIN_SCORE:
            seen.add(addr)
            pending.pop(addr, None)
            continue

        seen.add(addr)
        pending.pop(addr, None)
        found += 1

        if score >= 70:
            grade, grade_emoji = "S", "🏆"
        elif score >= 55:
            grade, grade_emoji = "A", "🥇"
        elif score >= 40:
            grade, grade_emoji = "B", "🥈"
        else:
            grade, grade_emoji = "C", "🥉"

        symbol = (pair.get("baseToken") or {}).get("symbol", "?")
        dex_url = pair.get("url", "")
        chain_label = "🟣 Solana" if "sol" in chain.lower() else "🔵 Base"
        buys5m = ((pair.get("txns") or {}).get("m5") or {}).get("buys", 0) or 0
        sells5m = ((pair.get("txns") or {}).get("m5") or {}).get("sells", 0) or 0
        reasons_txt = "\n".join(f"  • {r}" for r in reasons[:5])

        info = pair.get("info") or {}
        socials = info.get("socials") or []
        websites = info.get("websites") or []
        social_links = ""
        for s in socials:
            if s.get("type") == "twitter":
                social_links += f"  🐦 <a href='{s.get('url')}'>Twitter</a>\n"
            elif s.get("type") == "telegram":
                social_links += f"  ✈️ <a href='{s.get('url')}'>Telegram</a>\n"
        if websites:
            social_links += f"  🌐 <a href='{websites[0].get('url')}'>Website</a>\n"

        if rug_warnings:
            rug_txt = "\n⚠️ <b>Rug Uyarıları:</b>\n" + "\n".join(f"  {w}" for w in rug_warnings[:4])
        else:
            rug_txt = "\n✅ <b>Rug kontrolleri temiz</b>"

        msg = (
            f"{grade_emoji} <b>YENİ TOKEN — Grade {grade} ({score}/100)</b>\n\n"
            f"🪙 <b>{symbol}</b> · {chain_label}\n"
            f"📋 <code>{token_ca}</code>\n"
            f"⏱ Yaş: {age_min:.0f} dk\n\n"
            f"💰 Likidite: {fmt(liq)}\n"
            f"📊 5dk Hacim: {fmt(vol5m)}\n"
            f"📈 5dk Değişim: %{change5m:+.2f}\n"
            f"👥 Buy/Sell: {buys5m} / {sells5m}\n\n"
            f"<b>Sinyal nedenleri:</b>\n{reasons_txt}"
            f"{rug_txt}\n\n"
            f"<b>Sosyal Medya:</b>\n{social_links if social_links else '  Bulunamadı'}\n"
            f"🔗 <a href='{dex_url}'>DexScreener'da gör</a>"
        )

        send_telegram(msg)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ALARM → {symbol} ({chain}) Grade:{grade} Skor:{score}")

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Tarama bitti — {found} yeni token")


def main():
    print("=" * 50)
    print("  SIGNAL Bot v7 — Sosyal Medya Filtreli")
    print("=" * 50)
    send_telegram(
        "🤖 <b>SIGNAL Bot v7 başlatıldı</b>\n\n"
        "Yeni filtreler:\n"
        "  ✅ Sosyal medya kontrolü\n"
        "  ✅ Twitter/Telegram/Website linki\n"
        "  ✅ Sosyal medyası olmayan tokenler eleniyor\n\n"
        "Solana + Base takip ediliyor 🚀"
    )
    while True:
        try:
            scan()
        except Exception as e:
            print(f"Hata: {e}")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()