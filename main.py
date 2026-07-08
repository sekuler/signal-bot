import time
import requests
from datetime import datetime

TELEGRAM_TOKEN = "8892190725:AAFmzgGnH5L-ZDmepaIA1uYpxM5Bbzy7X4A"
CHAT_ID = "1590986571"
SCAN_INTERVAL = 60
MIN_GRADE = "C"
seen = set()

def score_token(pair):
    score = 0
    reasons = []
    liq = (pair.get("liquidity") or {}).get("usd", 0) or 0
    vol5m = (pair.get("volume") or {}).get("m5", 0) or 0
    change5m = (pair.get("priceChange") or {}).get("m5", 0) or 0
    buys5m = ((pair.get("txns") or {}).get("m5") or {}).get("buys", 0) or 0
    sells5m = ((pair.get("txns") or {}).get("m5") or {}).get("sells", 0) or 0
    age_min = pair.get("_age_minutes", 60)

    if liq >= 500000:
        score += 25
        reasons.append("💰 Yüksek likidite ($500K+)")
    elif liq >= 100000:
        score += 15
        reasons.append("💰 İyi likidite ($100K+)")
    elif liq >= 10000:
        score += 8
        reasons.append("💰 Düşük likidite ($10K+)")

    vol_ratio = vol5m / liq if liq > 0 else 0
    if vol_ratio > 0.30:
        score += 25
        reasons.append("🔥 Çok yüksek hacim")
    elif vol_ratio > 0.10:
        score += 15
        reasons.append("📈 Güçlü hacim")
    elif vol_ratio > 0.02:
        score += 8
        reasons.append("📊 Normal hacim")

    if change5m > 20:
        score += 20
        reasons.append("🚀 +%20 fiyat hareketi")
    elif change5m > 10:
        score += 15
        reasons.append("📈 +%10 fiyat hareketi")
    elif change5m > 1:
        score += 8
        reasons.append("📈 Pozitif momentum")

    total_tx = buys5m + sells5m
    buy_ratio = buys5m / total_tx if total_tx > 0 else 0
    if buy_ratio > 0.7 and total_tx > 10:
        score += 20
        reasons.append("👥 Güçlü alım baskısı")
    elif buy_ratio > 0.5:
        score += 10
        reasons.append("👥 Alımlar baskın")

    if age_min < 10:
        score += 10
        reasons.append("⚡ Çok yeni (<10dk)")
    elif age_min < 30:
        score += 6
        reasons.append("⚡ Yeni (<30dk)")
    elif age_min < 60:
        score += 3
        reasons.append("🕐 Taze (<60dk)")

    score = max(0, min(100, score))
    if score >= 70:
        grade = "S"
    elif score >= 55:
        grade = "A"
    elif score >= 40:
        grade = "B"
    elif score >= 25:
        grade = "C"
    else:
        grade = "D"
    return score, grade, reasons


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
        if liq < 10000:
            continue
        if vol5m < 1000:
            continue
        if change5m < 1.0:
            continue

        pair["_age_minutes"] = age_min
        score, grade, reasons = score_token(pair)

        grade_order = {"S": 4, "A": 3, "B": 2, "C": 1, "D": 0}
        if grade_order.get(grade, 0) < grade_order.get(MIN_GRADE, 0):
            seen.add(addr)
            continue

        seen.add(addr)
        found += 1

        symbol = (pair.get("baseToken") or {}).get("symbol", "?")
        token_ca = (pair.get("baseToken") or {}).get("address", "?")
        dex_url = pair.get("url", "")
        chain_label = "🟣 Solana" if "sol" in chain.lower() else "🔵 Base"
        buys5m = ((pair.get("txns") or {}).get("m5") or {}).get("buys", 0) or 0
        sells5m = ((pair.get("txns") or {}).get("m5") or {}).get("sells", 0) or 0
        grade_emoji = {"S": "🏆", "A": "🥇", "B": "🥈", "C": "🥉"}.get(grade, "")
        reasons_txt = "\n".join(f"  • {r}" for r in reasons[:4])

        msg = (
            f"{grade_emoji} <b>YENİ TOKEN — Grade {grade} ({score}/100)</b>\n\n"
            f"🪙 <b>{symbol}</b> · {chain_label}\n"
            f"📋 <code>{token_ca}</code>\n"
            f"⏱ Yaş: {age_min:.0f} dk\n\n"
            f"💰 Likidite: {fmt(liq)}\n"
            f"📊 5dk Hacim: {fmt(vol5m)}\n"
            f"📈 5dk Değişim: %{change5m:+.2f}\n"
            f"👥 Buy/Sell: {buys5m} / {sells5m}\n\n"
            f"<b>Sinyal nedenleri:</b>\n{reasons_txt}\n\n"
            f"🔗 <a href='{dex_url}'>DexScreener'da gör</a>"
        )

        send_telegram(msg)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ALARM → {symbol} ({chain}) Grade:{grade} Skor:{score}")

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Tarama bitti — {found} yeni token")


def main():
    print("=" * 50)
    print("  SIGNAL Bot v4")
    print("=" * 50)
    send_telegram("🤖 <b>SIGNAL Bot v4 başlatıldı</b>\n\nSolana + Base takip ediliyor 🚀")
    while True:
        try:
            scan()
        except Exception as e:
            print(f"Hata: {e}")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()