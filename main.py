import time
import json
import requests
from datetime import datetime
from pathlib import Path

TELEGRAM_TOKEN = "8892190725:AAFmzgGnH5L-ZDmepaIA1uYpxM5Bbzy7X4A"
CHAT_ID        = "1590986571"
SCAN_INTERVAL  = 60
SEEN_FILE      = "seen_tokens.json"

MIN_LIQUIDITY  = 25000
MIN_VOLUME_5M  = 3000
MIN_CHANGE_5M  = 3.0
MIN_BUY_RATIO  = 0.55
MAX_AGE_MIN    = 360
WAIT_SECONDS   = 60

seen    = set()
pending = {}

def load_seen():
    try:
        if Path(SEEN_FILE).exists():
            with open(SEEN_FILE, "r") as f:
                return set(json.load(f))
    except:
        pass
    return set()

def save_seen():
    try:
        with open(SEEN_FILE, "w") as f:
            json.dump(list(seen)[-5000:], f)
    except:
        pass

seen = load_seen()

def check_socials(pair):
    info    = pair.get("info") or {}
    socials = info.get("socials") or []
    webs    = info.get("websites") or []
    has_tw  = any(s.get("type") == "twitter" for s in socials)
    has_tg  = any(s.get("type") == "telegram" for s in socials)
    has_web = len(webs) > 0
    if not has_tw:
        return False
    return True

def fetch_pairs():
    results = []
    for chain in ["solana", "base"]:
        try:
            r = requests.get("https://api.dexscreener.com/token-profiles/latest/v1", timeout=10)
            if r.ok:
                profiles = [p for p in (r.json() or []) if p.get("chainId") == chain and p.get("tokenAddress")]
                addrs = [p.get("tokenAddress") for p in profiles]
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
                        time.sleep(0.2)
                    except:
                        pass
        except Exception as e:
            print(f"[{chain}] hata: {e}")

        for kw in ["pump", "new", "pepe", "dog", "cat", "ai", "sol"]:
            try:
                r = requests.get(
                    f"https://api.dexscreener.com/latest/dex/search?q={kw}&chainId={chain}",
                    timeout=10
                )
                if r.ok:
                    pairs = r.json().get("pairs") or []
                    for p in pairs:
                        p["_chain"] = chain
                    results.extend(pairs)
                time.sleep(0.2)
            except:
                pass

    seen_pairs = set()
    unique = []
    for p in results:
        addr = p.get("pairAddress", "")
        if addr and addr not in seen_pairs:
            seen_pairs.add(addr)
            unique.append(p)

    print(f"Toplam {len(unique)} pair çekildi")
    return unique

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
        change1h = (pair.get("priceChange") or {}).get("h1",  0) or 0
        buys5m   = ((pair.get("txns") or {}).get("m5") or {}).get("buys",  0) or 0
        sells5m  = ((pair.get("txns") or {}).get("m5") or {}).get("sells", 0) or 0

        if age_min  > MAX_AGE_MIN:    continue
        if liq      < MIN_LIQUIDITY:  continue
        if vol5m    < MIN_VOLUME_5M:  continue
        if change5m < MIN_CHANGE_5M:  continue

        total_tx  = buys5m + sells5m
        buy_ratio = buys5m / total_tx if total_tx > 0 else 0
        if buy_ratio < MIN_BUY_RATIO:
            continue

        if not check_socials(pair):
            seen.add(addr)
            continue

        if addr not in pending:
            pending[addr] = now_s
            print(f"[PENDING] {(pair.get('baseToken') or {}).get('symbol')}")
            continue
        if now_s - pending[addr] < WAIT_SECONDS:
            continue

        seen.add(addr)
        pending.pop(addr, None)
        save_seen()
        found += 1

        token_ca    = (pair.get("baseToken") or {}).get("address", "")
        symbol      = (pair.get("baseToken") or {}).get("symbol", "?")
        dex_url     = pair.get("url", "")
        chain_label = "🟣 Solana" if "sol" in chain.lower() else "🔵 Base"

        info    = pair.get("info") or {}
        socials = info.get("socials") or []
        webs    = info.get("websites") or []
        links   = ""
        for s in socials:
            if s.get("type") == "twitter":
                links += f"🐦<a href='{s.get('url')}'>TW</a>  "
            elif s.get("type") == "telegram":
                links += f"✈️<a href='{s.get('url')}'>TG</a>  "
        if webs:
            links += f"🌐<a href='{webs[0].get('url')}'>WEB</a>"

        msg = (
            f"🪙 <b>{symbol}</b> · {chain_label}\n"
            f"📋 <code>{token_ca}</code>\n"
            f"⏱ Yaş: {age_min:.0f} dk\n\n"
            f"💰 Likidite: {fmt(liq)}\n"
            f"📊 5dk Hacim: {fmt(vol5m)}\n"
            f"📈 5dk: %{change5m:+.2f}  |  1sa: %{change1h:+.2f}\n"
            f"👥 Buy/Sell: {buys5m}/{sells5m} (%{buy_ratio*100:.0f} buy)\n\n"
            f"{links}\n"
            f"🔗 <a href='{dex_url}'>DexScreener</a>"
        )

        send_telegram(msg)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ {symbol} ({chain})")

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Tarama bitti — {found} yeni token")

def main():
    print("=" * 50)
    print("  SIGNAL Bot — Sade Versiyon")
    print("=" * 50)
    send_telegram("🤖 <b>SIGNAL Bot başlatıldı</b>\n\nSolana + Base 🚀")
    while True:
        try:
            scan()
        except Exception as e:
            print(f"Hata: {e}")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()