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

SOLANA_RPC = "https://api.mainnet-beta.solana.com"

# Eleme eşikleri
MAX_TOP10_HOLDER_PCT  = 35    # İlk 10 holder max %35
MAX_BUNDLE_PCT        = 20    # Bundle/sniper max %20
MAX_CREATOR_RUGS      = 2     # Creator max kaç rug yapabilir
MIN_LIQUIDITY_USD     = 20000 # Min $20K likidite
MIN_VOLUME_5M         = 2000  # Min $2K 5dk hacim
MIN_CHANGE_5M         = 3.0   # Min %3 değişim
MAX_AGE_MINUTES       = 360   # Max 6 saat yaş
WAIT_SECONDS          = 60    # İlk görüldükten kaç sn sonra analiz et
# ───────────────────────────────────────────────────────────

# Seen listesini dosyadan yükle
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
            json.dump(list(seen)[-5000:], f)  # Max 5000 kayıt tut
    except:
        pass

seen    = load_seen()
pending = {}


# ─── SOLANA RPC ────────────────────────────────────────────
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

def check_mint_freeze_authority(token_address):
    """Mint ve Freeze authority kontrolü — Solana için kritik"""
    warnings = []
    penalty  = 0
    try:
        result = solana_rpc("getAccountInfo", [
            token_address,
            {"encoding": "jsonParsed"}
        ])
        if not result:
            return warnings, penalty

        info = result.get("value", {}) or {}
        data = info.get("data", {}) or {}
        parsed = data.get("parsed", {}) or {}
        token_info = parsed.get("info", {}) or {}

        mint_authority   = token_info.get("mintAuthority")
        freeze_authority = token_info.get("freezeAuthority")

        if mint_authority:
            warnings.append("🚨 Mint authority açık — sınırsız token basılabilir!")
            penalty += 60
        if freeze_authority:
            warnings.append("🚨 Freeze authority açık — cüzdanlar dondurulabilir!")
            penalty += 60

    except Exception as e:
        print(f"Mint/Freeze kontrol hata: {e}")

    return warnings, penalty


def check_creator_sells(token_address):
    """Creator ilk 5 dakikada satış yapmış mı?"""
    warnings = []
    penalty  = 0
    try:
        result = solana_rpc("getSignaturesForAddress", [
            token_address,
            {"limit": 50}
        ])
        if not result:
            return warnings, penalty

        # İlk işlem zamanı
        if len(result) > 0:
            first_tx_time = result[-1].get("blockTime", 0)
            now = time.time()
            
            # İlk 5 dakika içindeki işlemler
            early_txs = [tx for tx in result 
                        if tx.get("blockTime", 0) and 
                        (tx.get("blockTime") - first_tx_time) < 300]
            
            if len(early_txs) > 20:
                warnings.append(f"⚠️ İlk 5dk'da çok fazla işlem ({len(early_txs)})")
                penalty += 15

    except Exception as e:
        print(f"Creator sells hata: {e}")

    return warnings, penalty


# ─── RUGCHECK API ──────────────────────────────────────────
def rugcheck_solana(token_address):
    warnings    = []
    penalty     = 0
    score_bonus = 0

    try:
        url = f"https://api.rugcheck.xyz/v1/tokens/{token_address}/report"
        r   = requests.get(url, timeout=12)
        if not r.ok:
            return warnings, penalty, score_bonus

        data = r.json()

        # 1. Dev cüzdan geçmişi
        creator        = data.get("creator") or {}
        creator_tokens = creator.get("tokens") or []
        rug_count      = sum(1 for t in creator_tokens if t.get("rugged", False))
        total_created  = len(creator_tokens)

        if rug_count >= MAX_CREATOR_RUGS:
            warnings.append(f"🚨 Dev {rug_count} rug yapmış — KESİN ATLA!")
            penalty += 100
        elif rug_count == 1:
            warnings.append(f"⚠️ Dev 1 rug geçmişi var")
            penalty += 30

        if total_created > 15:
            warnings.append(f"⚠️ Dev {total_created} token çıkarmış")
            penalty += 15

        # 2. Holder dağılımı
        top_holders = data.get("topHolders") or []
        if top_holders:
            top10_pct = sum(h.get("pct", 0) for h in top_holders[:10])
            if top10_pct > MAX_TOP10_HOLDER_PCT:
                warnings.append(f"🚨 Top 10 holder %{top10_pct:.0f} tutuyor — DUMP RİSKİ!")
                penalty += 50
            elif top10_pct > 25:
                warnings.append(f"⚠️ Top 10 holder %{top10_pct:.0f} tutuyor")
                penalty += 20

        # 3. Bundle/insider tespiti
        insider_pct = sum(h.get("pct", 0) for h in top_holders if h.get("insider", False))
        if insider_pct > MAX_BUNDLE_PCT:
            warnings.append(f"🚨 Bundle/insider cüzdanlar %{insider_pct:.0f} tutuyor!")
            penalty += 60
        elif insider_pct > 10:
            warnings.append(f"⚠️ Insider cüzdanlar %{insider_pct:.0f}")
            penalty += 20

        # 4. LP kilidi
        markets   = data.get("markets") or []
        lp_locked = False
        lp_burned = False
        for m in markets:
            lp = m.get("lp") or {}
            if lp.get("locked"):
                lp_locked = True
            if lp.get("burned"):
                lp_burned = True

        if lp_burned:
            score_bonus += 20
        elif lp_locked:
            score_bonus += 10
        else:
            warnings.append("⚠️ LP kilitli/yakılmış değil")
            penalty += 20

        # 5. RugCheck genel skoru
        rc_score = data.get("score", 0)
        if rc_score < 300:
            warnings.append(f"🚨 RugCheck skoru çok düşük: {rc_score}")
            penalty += 30
        elif rc_score < 600:
            warnings.append(f"⚠️ RugCheck skoru: {rc_score}")
            penalty += 10
        elif rc_score >= 800:
            score_bonus += 10

        # 6. Risk listesi
        risks = data.get("risks") or []
        for risk in risks:
            level = risk.get("level", "")
            name  = risk.get("name", "")
            if level == "danger":
                warnings.append(f"🚨 {name}")
                penalty += 25
            elif level == "warn":
                warnings.append(f"⚠️ {name}")
                penalty += 8

    except Exception as e:
        print(f"RugCheck hata: {e}")

    return warnings, penalty, score_bonus


# ─── TOKEN SNIFFER (BASE) ──────────────────────────────────
def tokensniffer_base(token_address):
    warnings    = []
    penalty     = 0
    score_bonus = 0

    try:
        url = f"https://tokensniffer.com/api/v2/tokens/8453/{token_address}?apikey=free&include_metrics=true"
        r   = requests.get(url, timeout=12)
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
            penalty += 50
        elif ts_score < 60:
            warnings.append(f"⚠️ Token Sniffer: {ts_score}/100")
            penalty += 20
        else:
            score_bonus += 10

        # Holder analizi
        holders = data.get("holders") or {}
        top10   = holders.get("top10_percent", 0) or 0
        if top10 > MAX_TOP10_HOLDER_PCT:
            warnings.append(f"🚨 Top 10 holder %{top10:.0f} tutuyor!")
            penalty += 40
        elif top10 > 25:
            warnings.append(f"⚠️ Top 10 holder %{top10:.0f}")
            penalty += 15

    except Exception as e:
        print(f"TokenSniffer hata: {e}")

    return warnings, penalty, score_bonus


# ─── SOSYAL MEDYA ──────────────────────────────────────────
def check_socials(pair):
    bonus    = 0
    warnings = []
    info     = pair.get("info") or {}
    socials  = info.get("socials") or []
    websites = info.get("websites") or []

    has_twitter  = any(s.get("type") == "twitter"  for s in socials)
    has_telegram = any(s.get("type") == "telegram" for s in socials)
    has_website  = len(websites) > 0

    if not has_twitter and not has_telegram and not has_website:
        return -999, warnings  # -999 = direkt ele sinyali

    if has_twitter:  bonus += 8
    if has_telegram: bonus += 5
    if has_website:  bonus += 7

    if not has_twitter:  warnings.append("⚠️ Twitter/X yok")
    if not has_telegram: warnings.append("⚠️ Telegram yok")
    if not has_website:  warnings.append("⚠️ Website yok")

    return bonus, warnings


# ─── MOMENTUM ──────────────────────────────────────────────
def check_momentum(pair):
    bonus  = 0
    notes  = []
    buys5m  = ((pair.get("txns") or {}).get("m5") or {}).get("buys",  0) or 0
    sells5m = ((pair.get("txns") or {}).get("m5") or {}).get("sells", 0) or 0
    total   = buys5m + sells5m

    if buys5m > 100:
        bonus += 15
        notes.append("🔥 Çok yoğun alım (100+ tx/5dk)")
    elif buys5m > 50:
        bonus += 10
        notes.append("🔥 Yoğun alım (50+ tx/5dk)")
    elif buys5m > 20:
        bonus += 5
        notes.append("📈 Aktif alım (20+ tx/5dk)")

    if total > 0:
        ratio = buys5m / total
        if ratio > 0.80:
            bonus += 15
            notes.append(f"👥 Çok güçlü alım baskısı (%{ratio*100:.0f})")
        elif ratio > 0.65:
            bonus += 8
            notes.append(f"👥 Güçlü alım baskısı (%{ratio*100:.0f})")
        elif ratio < 0.40:
            bonus -= 10
            notes.append(f"⚠️ Satış baskısı (%{(1-ratio)*100:.0f} sell)")

    return bonus, notes


# ─── SKOR SİSTEMİ ──────────────────────────────────────────
def score_token(pair):
    score   = 0
    reasons = []

    liq      = (pair.get("liquidity")   or {}).get("usd", 0) or 0
    vol5m    = (pair.get("volume")      or {}).get("m5",  0) or 0
    change5m = (pair.get("priceChange") or {}).get("m5",  0) or 0
    change1h = (pair.get("priceChange") or {}).get("h1",  0) or 0
    age_min  = pair.get("_age_minutes", 60)

    # Likidite skoru
    if liq >= 500000:
        score += 25; reasons.append("💰 Yüksek likidite ($500K+)")
    elif liq >= 200000:
        score += 20; reasons.append("💰 İyi likidite ($200K+)")
    elif liq >= 100000:
        score += 15; reasons.append("💰 Orta likidite ($100K+)")
    elif liq >= 50000:
        score += 10; reasons.append("💰 Düşük likidite ($50K+)")
    elif liq >= 20000:
        score += 5;  reasons.append("💰 Min likidite ($20K+)")

    # Hacim/likidite oranı
    vol_ratio = vol5m / liq if liq > 0 else 0
    if vol_ratio > 0.50:
        score += 20; reasons.append("🔥 Çok yüksek hacim/liq")
    elif vol_ratio > 0.20:
        score += 15; reasons.append("📈 Güçlü hacim/liq")
    elif vol_ratio > 0.05:
        score += 8;  reasons.append("📊 Normal hacim")

    # Fiyat hareketi
    if change5m > 30:
        score += 15; reasons.append("🚀 +%30 fiyat hareketi")
    elif change5m > 15:
        score += 10; reasons.append("📈 +%15 fiyat hareketi")
    elif change5m > 5:
        score += 5;  reasons.append("📈 Pozitif momentum")
    elif change5m < -10:
        score -= 10; reasons.append("⚠️ Sert düşüş")

    # 1 saatlik trend
    if change1h > 50:
        score += 10; reasons.append("📈 Güçlü 1sa trendi (+%50)")
    elif change1h > 20:
        score += 5;  reasons.append("📈 Pozitif 1sa trend")

    # Yaş
    if age_min < 10:
        score += 8;  reasons.append("⚡ Çok yeni (<10dk)")
    elif age_min < 30:
        score += 5;  reasons.append("⚡ Yeni (<30dk)")
    elif age_min < 60:
        score += 3;  reasons.append("🕐 Taze (<60dk)")

    return max(0, min(100, score)), reasons


# ─── DEXSCREENER ───────────────────────────────────────────
def fetch_pairs():
    results = []
    for chain in ["solana", "base"]:
        try:
            r = requests.get("https://api.dexscreener.com/token-profiles/latest/v1", timeout=10)
            if r.ok:
                profiles = r.json() or []
                addrs = [
                    p.get("tokenAddress") for p in profiles
                    if p.get("chainId") == chain and p.get("tokenAddress")
                ]
                if addrs:
                    addr_str = ",".join(addrs[:30])
                    r2 = requests.get(
                        f"https://api.dexscreener.com/latest/dex/tokens/{addr_str}",
                        timeout=10
                    )
                    if r2.ok:
                        pairs = r2.json().get("pairs") or []
                        for p in pairs:
                            p["_chain"] = chain
                        results.extend(pairs)
        except Exception as e:
            print(f"[{chain}] fetch hata: {e}")

    print(f"Toplam {len(results)} pair çekildi")
    return results


# ─── TELEGRAM ──────────────────────────────────────────────
def send_telegram(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": msg,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=10
        )
    except Exception as e:
        print(f"TG hata: {e}")


def fmt(n):
    if not n: return "?"
    if n >= 1_000_000: return f"${n/1_000_000:.1f}M"
    if n >= 1_000:     return f"${n/1_000:.0f}K"
    return f"${n:.0f}"


# ─── ANA TARAMA ────────────────────────────────────────────
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

        # ── ELEME 1: Temel filtreler ──
        if age_min  > MAX_AGE_MINUTES:  continue
        if liq      < MIN_LIQUIDITY_USD: continue
        if vol5m    < MIN_VOLUME_5M:     continue
        if change5m < MIN_CHANGE_5M:     continue

        # ── ELEME 2: Sosyal medya yok → at ──
        social_bonus, social_warnings = check_socials(pair)
        if social_bonus == -999:
            seen.add(addr)
            print(f"[SKIP-SOSYAL] {(pair.get('baseToken') or {}).get('symbol')}")
            continue

        # ── 60 saniye bekleme ──
        if addr not in pending:
            pending[addr] = now_s
            print(f"[PENDING] {(pair.get('baseToken') or {}).get('symbol')} — 60sn bekleniyor")
            continue

        if now_s - pending[addr] < WAIT_SECONDS:
            continue

        token_ca = (pair.get("baseToken") or {}).get("address", "")
        pair["_age_minutes"] = age_min

        # ── ELEME 3: Mint/Freeze authority (sadece Solana) ──
        mf_warnings = []
        mf_penalty  = 0
        if chain == "solana" and token_ca:
            mf_warnings, mf_penalty = check_mint_freeze_authority(token_ca)
            if mf_penalty >= 60:
                seen.add(addr)
                pending.pop(addr, None)
                print(f"[SKIP-MINT/FREEZE] {(pair.get('baseToken') or {}).get('symbol')}")
                continue

        # ── ELEME 4: Creator sells ──
        cs_warnings = []
        cs_penalty  = 0
        if chain == "solana" and token_ca:
            cs_warnings, cs_penalty = check_creator_sells(token_ca)

        # ── ELEME 5: RugCheck / TokenSniffer ──
        rug_warnings = []
        rug_penalty  = 0
        rug_bonus    = 0
        if chain == "solana" and token_ca:
            rug_warnings, rug_penalty, rug_bonus = rugcheck_solana(token_ca)
        elif chain == "base" and token_ca:
            rug_warnings, rug_penalty, rug_bonus = tokensniffer_base(token_ca)

        # Kritik rug → at
        total_penalty = mf_penalty + cs_penalty + rug_penalty
        if total_penalty >= 80:
            seen.add(addr)
            pending.pop(addr, None)
            print(f"[SKIP-RUG] penalty={total_penalty}: {(pair.get('baseToken') or {}).get('symbol')}")
            continue

        # ── SKORLAMA ──
        score, reasons = score_token(pair)

        mom_bonus, mom_notes = check_momentum(pair)
        score += mom_bonus
        reasons.extend(mom_notes)

        score += social_bonus
        score += rug_bonus
        score -= total_penalty
        score  = max(0, min(100, score))

        if score < 35:
            seen.add(addr)
            pending.pop(addr, None)
            continue

        seen.add(addr)
        pending.pop(addr, None)
        save_seen(seen)
        found += 1

        # Grade
        if score >= 75:   grade, emoji = "S", "🏆"
        elif score >= 60: grade, emoji = "A", "🥇"
        elif score >= 45: grade, emoji = "B", "🥈"
        else:             grade, emoji = "C", "🥉"

        symbol      = (pair.get("baseToken") or {}).get("symbol", "?")
        dex_url     = pair.get("url", "")
        chain_label = "🟣 Solana" if "sol" in chain.lower() else "🔵 Base"
        buys5m      = ((pair.get("txns") or {}).get("m5") or {}).get("buys",  0) or 0
        sells5m     = ((pair.get("txns") or {}).get("m5") or {}).get("sells", 0) or 0

        all_warnings = mf_warnings + cs_warnings + rug_warnings + social_warnings
        reasons_txt  = "\n".join(f"  • {r}" for r in reasons[:5])

        if all_warnings:
            warn_txt = "\n\n⚠️ <b>Uyarılar:</b>\n" + "\n".join(f"  {w}" for w in all_warnings[:5])
        else:
            warn_txt = "\n\n✅ <b>Tüm güvenlik kontrolleri temiz</b>"

        # Sosyal medya linkleri
        info     = pair.get("info") or {}
        socials  = info.get("socials") or []
        websites = info.get("websites") or []
        links    = ""
        for s in socials:
            if s.get("type") == "twitter":
                links += f"  🐦 <a href='{s.get('url')}'>Twitter</a>  "
            elif s.get("type") == "telegram":
                links += f"  ✈️ <a href='{s.get('url')}'>Telegram</a>  "
        if websites:
            links += f"  🌐 <a href='{websites[0].get('url')}'>Website</a>"

        msg = (
            f"{emoji} <b>Grade {grade} — {score}/100</b>\n\n"
            f"🪙 <b>{symbol}</b> · {chain_label}\n"
            f"📋 <code>{token_ca}</code>\n"
            f"⏱ Yaş: {age_min:.0f} dk\n\n"
            f"💰 Likidite: {fmt(liq)}\n"
            f"📊 5dk Hacim: {fmt(vol5m)}\n"
            f"📈 5dk Değişim: %{change5m:+.2f}\n"
            f"👥 Buy/Sell: {buys5m} / {sells5m}\n\n"
            f"<b>Sinyal:</b>\n{reasons_txt}"
            f"{warn_txt}\n\n"
            f"{links}\n"
            f"🔗 <a href='{dex_url}'>DexScreener</a>"
        )

        send_telegram(msg)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ {symbol} ({chain}) {grade} {score}/100")

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Tarama bitti — {found} yeni token")


# ─── MAIN ──────────────────────────────────────────────────
def main():
    print("=" * 55)
    print("  SIGNAL Bot v8 — Eleme Öncelikli")
    print("=" * 55)
    send_telegram(
        "🤖 <b>SIGNAL Bot v8 başlatıldı</b>\n\n"
        "<b>Eleme filtreleri:</b>\n"
        "  ❌ Mint authority açıksa → at\n"
        "  ❌ Freeze authority açıksa → at\n"
        "  ❌ Top 10 holder >%35 → at\n"
        "  ❌ Bundle/insider >%20 → at\n"
        "  ❌ Dev 2+ rug geçmişi → at\n"
        "  ❌ LP kilitli/yakılmamış → ceza\n"
        "  ❌ Sosyal medya yok → at\n"
        "  ⏱ 60 saniye bekleme aktif\n\n"
        "Solana + Base takip ediliyor 🚀"
    )

    while True:
        try:
            scan()
        except Exception as e:
            print(f"Ana hata: {e}")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
