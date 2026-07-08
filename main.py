import time
import requests
from datetime import datetime

TELEGRAM_TOKEN = "8892190725:AAFmzgGnH5L-ZDmepaIA1uYpxM5Bbzy7X4A"
CHAT_ID = "1590986571"
SCAN_INTERVAL = 90
MIN_SCORE = 40
seen = set()
pending = {}  # 60 saniye bekleme için


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

        # 1. Dev cüzdan geçmişi
        creator = data.get("creator", {}) or {}
        creator_tokens = creator.get("tokens", []) or []
        rug_count = sum(1 for t in creator_tokens if t.get("rugged", False))
        total_created = len(creator_tokens)

        if rug_count > 0:
            warnings.append(f"🚨 Dev daha önce {rug_count} rug yapmış!")
            penalty += 50
        if total_created > 10:
            warnings.append(f"⚠️ Dev bugüne kadar {total_created} token çıkarmış")
            penalty += 20

        # 2. Holder dağılımı
        top_holders = data.get("topHolders", []) or []
        if top_holders:
            top10_pct = sum(h.get("pct", 0) for h in top_holders[:10])
            if top10_pct > 50:
                warnings.append(f"🚨 Top 10 holder arzın %{top10_pct:.0f}'ini tutuyor!")
                penalty += 40
            elif top10_pct > 25:
                warnings.append(f"⚠️ Top 10 holder %{top10_pct:.0f} tutuyor")
                penalty += 20

        # 3. Bundle wallet tespiti
        insider_pct = 0
        for h in top_holders:
            if h.get("insider", False):
                insider_pct += h.get("pct", 0)
        if insider_pct > 10:
            warnings.append(f"🚨 Bundle/insider cüzdanlar arzın %{insider_pct:.0f}'ini tutuyor!")
            penalty += 40

        # 4. LP kilidi
        markets = data.get("markets", []) or []
        lp_locked = any(m.get("lp", {}).get("locked", False) for m in markets)
        if not lp_locked:
            warnings.append("⚠️ LP kilidi yok")
            penalty += 15
        else:
            score_bonus += 10

        # 5. Genel RugCheck skoru
        rugcheck_score = data.get("score", 0)
        if rugcheck_score < 500:
            warnings.append(f"⚠️ RugCheck skoru düşük: {rugcheck_score}")
            penalty += 15

        # 6. Risk listesi
        risks = data.get("risks", []) or []
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
        is_honeypot = data.get("is_honeypot", False)
        rugged = data.get("rugged", False)
        ts_score = data.get("score", 100)

        if is_honeypot:
            warnings.append("🚨 HONEYPOT — satış engellenmiş!")
            penalty += 100
        if rugged:
            warnings.append("🚨 Bu kontrat daha önce rug yapmış!")
            penalty += 100
        if ts_score < 30:
            warnings.append(f"🚨 Token Sniffer skoru: {ts_score}/100")
            penalty += 40
        elif ts_score < 60:
            warnings.append(f"⚠️ Token Sniffer skoru: {ts_score}/100")
            penalty += 15
        else:
            score_bonus += 10

    except Exception as e:
        print(f"TokenSniffer hata: {e}")

    return warnings, penalty, score_bonus


def check_momentum(pair):
    """Holder ve işlem sayısı artıyor mu kontrol et"""
    bonus = 0
    notes = []
    buys5m = ((pair.get("txns") or {}).get("m5") or {}).get("buys", 0) or 0
    buys1h = ((pair.get("txns") or {}).get("h1") or {}).get("buys", 0) or 0
    sells5m = ((pair.get("txns") or {}).get("m5") or {}).get("sells", 0) or 0

    # Son 5 dakikada işlem yoğunluğu
    if buys5m > 50:
        bonus += 10
        notes.append("🔥 Çok yoğun alım (50+ tx/5dk)")
    elif buys5m > 20:
        bonus += 5
        notes.append("📈 Yoğun alım (20+ tx/5dk)")

    # Buy/sell oranı
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
    change1h = (pair.get("priceChange") or {}).get("h1", 0) or 0
    age_min = pair.get("_age_minutes", 60)

    # Likidite (min 15 SOL ~ $2000, ideal $50K+)
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

    # Hacim/likidite oranı
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

    # Fiyat hareketi
    if change5m > 20:
        score += 15
        reasons.append("🚀 +%20 fiyat hareketi")
    elif change5m > 10:
        score += 10
        reasons.append("📈 +%10 fiyat hareketi")
    elif change5m > 3:
        score += 5
        reasons.append("📈 Pozitif momentum")

    # Yaş bonusu (60sn bekleme sonrası)
    if age_min < 5:
        score += 5
        reasons.append("⚡ Çok yeni (<5dk)")
    elif age_min < 15:
        score += 8
        reasons.append("⚡ Yeni (<15dk)")
    elif age_min < 30:
        score += 5
        reasons.append("🕐 Taze (<30dk)")

    score = max(0, min(100, score))
    return score, reasons


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

        # Temel filtreler
        if age_min > 360:
            continue
        if liq < 20000:
            continue
        if vol5m < 2000:
            continue
        if change5m < 1.0:
            continue

        # 60 saniye bekleme — ilk rug dalgasını atlatmak
        if addr not in pending:
            pending[addr] = {"first_seen": now_s, "pair": pair}
            print(f"[PENDING] {(pair.get('baseToken') or {}).get('symbol')} — 60sn bekleniyor")
            continue

        wait_time = now_s - pending[addr]["first_seen"]
        if wait_time < 60:
            continue

        # 60 saniye geçti, analiz et
        pair["_age_minutes"] = age_min
        score, reasons = score_token(pair)

        # Momentum kontrolü
        mom_bonus, mom_notes = check_momentum(pair)
        score += mom_bonus
        reasons.extend(mom_notes)

        token_ca = (pair.get("baseToken") or {}).get("address", "")

        # Rug kontrolleri
        rug_warnings = []
        penalty = 0
        rug_bonus = 0

        if chain == "solana" and token_ca:
            rug_warnings, penalty, rug_bonus = rugcheck_solana(token_ca)
        elif chain == "base" and token_ca:
            rug_warnings, penalty, rug_bonus = tokensniffer_base(token_ca)

        # Honeypot veya kritik rug varsa direkt atla
        if any("HONEYPOT" in w or ("🚨" in w and "rug" in w.lower()) for w in rug_warnings):
            seen.add(addr)
            pending.pop(addr, None)
            print(f"[SKIP] Kritik rug riski: {(pair.get('baseToken') or {}).get('symbol')}")
            continue

        if penalty >= 60:
            seen.add(addr)
            pending.pop(addr, None)
            print(f"[SKIP] Yüksek penalty ({penalty}): {(pair.get('baseToken') or {}).get('symbol')}")
            continue

        # Final skor
        score = max(0, min(100, score - penalty + rug_bonus))

        if score < MIN_SCORE:
            seen.add(addr)
            pending.pop(addr, None)
            continue

        seen.add(addr)
        pending.pop(addr, None)
        found += 1

        # Grade
        if score >= 70:
            grade = "S"
            grade_emoji = "🏆"
        elif score >= 55:
            grade = "A"
            grade_emoji = "🥇"
        elif score >= 40:
            grade = "B"
            grade_emoji = "🥈"
        else:
            grade = "C"
            grade_emoji = "🥉"

        symbol = (pair.get("baseToken") or {}).get("symbol", "?")
        dex_url = pair.get("url", "")
        chain_label = "🟣 Solana" if "sol" in chain.lower() else "🔵 Base"
        buys5m = ((pair.get("txns") or {}).get("m5") or {}).get("buys", 0) or 0
        sells5m = ((pair.get("txns") or {}).get("m5") or {}).get("sells", 0) or 0
        reasons_txt = "\n".join(f"  • {r}" for r in reasons[:5])

        if rug_warnings:
            rug_txt = "\n\n⚠️ <b>Rug Uyarıları:</b>\n" + "\n".join(f"  {w}" for w in rug_warnings[:4])
        else:
            rug_txt = "\n\n✅ <b>Rug kontrolleri temiz</b>"

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
            f"🔗 <a href='{dex_url}'>DexScreener'da gör</a>"
        )

        send_telegram(msg)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ALARM → {symbol} ({chain}) Grade:{grade} Skor:{score}")

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Tarama bitti — {found} yeni token")


def main():
    print("=" * 50)
    print("  SIGNAL Bot v6 — Gelişmiş Rug Filtreli")
    print("=" * 50)
    send_telegram(
        "🤖 <b>SIGNAL Bot v6 başlatıldı</b>\n\n"
        "Yeni filtreler:\n"
        "  ✅ Dev geçmişi kontrolü\n"
        "  ✅ Holder dağılımı (%25+ uyarısı)\n"
        "  ✅ Bundle wallet tespiti\n"
        "  ✅ LP kilit kontrolü\n"
        "  ✅ 60 saniye bekleme\n"
        "  ✅ Momentum analizi\n\n"
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