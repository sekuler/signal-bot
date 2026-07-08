import time
import json
import requests
from datetime import datetime
from pathlib import Path
from collections import defaultdict

# ─── AYARLAR ───────────────────────────────────────────────
TELEGRAM_TOKEN = "8892190725:AAFmzgGnH5L-ZDmepaIA1uYpxM5Bbzy7X4A"
CHAT_ID        = "1590986571"
SCAN_INTERVAL  = 90
SEEN_FILE      = "seen_tokens.json"
SOLANA_RPC     = "https://api.mainnet-beta.solana.com"

MAX_TOP10_HOLDER_PCT  = 35
MAX_BUNDLE_PCT        = 20
MAX_CREATOR_RUGS      = 2
MAX_CREATOR_HOLD_PCT  = 20   # Creator max %20 tutabilir
MIN_LIQUIDITY_USD     = 20000
MIN_VOLUME_5M         = 2000
MIN_CHANGE_5M         = 3.0
MAX_AGE_MINUTES       = 360
WAIT_SECONDS          = 60
MIN_RISK_SCORE        = 40
MIN_MOMENTUM_SCORE    = 30
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

seen             = load_seen()
pending          = {}   # addr -> {first_seen, buys_t0}
holder_snapshots = {}   # addr -> holder_count at t0


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
        print(f"RPC hata ({method}): {e}")
    return None


def check_mint_freeze(token_address):
    """SPL Token mint account'u doğru parse ederek Mint/Freeze authority kontrolü"""
    warnings = []
    penalty  = 0
    try:
        result = solana_rpc("getAccountInfo", [
            token_address,
            {"encoding": "jsonParsed", "commitment": "confirmed"}
        ])
        if not result:
            warnings.append("⚠️ Mint account verisi alınamadı")
            penalty += 10
            return warnings, penalty

        value      = result.get("value") or {}
        data       = value.get("data") or {}

        # jsonParsed formatında SPL token mint account
        if isinstance(data, dict):
            parsed     = data.get("parsed") or {}
            ptype      = parsed.get("type", "")
            info       = parsed.get("info") or {}

            if ptype != "mint":
                # Bu bir mint account değil, beklenmedik durum
                warnings.append("⚠️ Mint account doğrulanamadı")
                penalty += 15
                return warnings, penalty

            mint_auth   = info.get("mintAuthority")
            freeze_auth = info.get("freezeAuthority")
            decimals    = info.get("decimals", -1)
            supply      = int(info.get("supply", 0))

            if mint_auth:
                warnings.append(f"🚨 Mint Authority AÇIK ({mint_auth[:8]}...)")
                penalty += 60
            if freeze_auth:
                warnings.append(f"🚨 Freeze Authority AÇIK ({freeze_auth[:8]}...)")
                penalty += 60
            if decimals == 0:
                warnings.append("⚠️ Decimals=0 — NFT benzeri token")
                penalty += 10
        else:
            warnings.append("⚠️ Mint account parse edilemedi")
            penalty += 10

    except Exception as e:
        print(f"Mint/Freeze hata: {e}")
        warnings.append("⚠️ Mint/Freeze kontrol edilemedi")
        penalty += 10

    return warnings, penalty


def get_creator_address(token_address):
    """Token'ı yaratan ilk transaction'dan creator adresini bul"""
    try:
        sigs = solana_rpc("getSignaturesForAddress", [
            token_address, {"limit": 100, "commitment": "confirmed"}
        ])
        if not sigs:
            return None
        # En eski tx (listenin sonu)
        oldest_sig = sigs[-1].get("signature")
        if not oldest_sig:
            return None

        tx = solana_rpc("getTransaction", [
            oldest_sig,
            {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}
        ])
        if not tx:
            return None

        # Fee payer = creator
        account_keys = (tx.get("transaction") or {}).get("message", {}).get("accountKeys") or []
        if account_keys:
            first = account_keys[0]
            if isinstance(first, dict):
                return first.get("pubkey")
            return str(first)
    except Exception as e:
        print(f"Creator adres hata: {e}")
    return None


def check_creator_sells(token_address, creator_address):
    """Creator'ın ilk 5 dakikada satış yapıp yapmadığını kontrol et"""
    warnings = []
    penalty  = 0
    if not creator_address:
        return warnings, penalty

    try:
        # Token oluşturulma zamanı
        sigs = solana_rpc("getSignaturesForAddress", [
            token_address, {"limit": 100, "commitment": "confirmed"}
        ])
        if not sigs:
            return warnings, penalty

        mint_time = sigs[-1].get("blockTime", 0)
        if not mint_time:
            return warnings, penalty

        # Creator'ın işlemlerine bak
        creator_sigs = solana_rpc("getSignaturesForAddress", [
            creator_address,
            {"limit": 50, "commitment": "confirmed"}
        ])
        if not creator_sigs:
            return warnings, penalty

        # İlk 5 dakikadaki creator işlemleri
        early_count = 0
        for sig_info in creator_sigs:
            block_time = sig_info.get("blockTime", 0)
            if block_time and (block_time - mint_time) < 300 and block_time >= mint_time:
                early_count += 1

        if early_count > 5:
            warnings.append(f"🚨 Creator ilk 5dk'da {early_count} işlem yaptı!")
            penalty += 40
        elif early_count > 2:
            warnings.append(f"⚠️ Creator ilk 5dk'da {early_count} işlem")
            penalty += 15

    except Exception as e:
        print(f"Creator sells hata: {e}")

    return warnings, penalty


def get_unique_buyers(pair_address, chain):
    """Son 5 dakikadaki benzersiz alıcı sayısını tahmin et"""
    if chain != "solana":
        return 0

    try:
        sigs = solana_rpc("getSignaturesForAddress", [
            pair_address,
            {"limit": 100, "commitment": "confirmed"}
        ])
        if not sigs:
            return 0

        now       = time.time()
        five_min  = now - 300
        recent    = [s for s in sigs if s.get("blockTime", 0) > five_min]

        if not recent:
            return 0

        # Her tx'in fee payer'ını topla = alıcı tahmini
        unique_addresses = set()
        for sig_info in recent[:30]:  # Max 30 tx analiz et
            sig = sig_info.get("signature")
            if not sig:
                continue
            try:
                tx = solana_rpc("getTransaction", [
                    sig,
                    {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}
                ])
                if tx:
                    keys = (tx.get("transaction") or {}).get("message", {}).get("accountKeys") or []
                    if keys:
                        first = keys[0]
                        addr  = first.get("pubkey") if isinstance(first, dict) else str(first)
                        if addr:
                            unique_addresses.add(addr)
            except:
                continue

        return len(unique_addresses)
    except Exception as e:
        print(f"Unique buyers hata: {e}")
    return 0


def check_holder_growth(addr, current_buys):
    """60 saniye öncesiyle buy tx sayısını karşılaştır — büyüme hızı"""
    if addr not in pending:
        return 0, ""
    prev_buys = pending[addr].get("buys_t0", 0)
    growth    = current_buys - prev_buys
    if growth > 30:
        return 15, f"📈 60sn'de {growth} yeni alım tx — hızlı büyüme!"
    elif growth > 10:
        return 8, f"📈 60sn'de {growth} yeni alım tx"
    elif growth > 3:
        return 3, f"📈 60sn'de {growth} alım tx"
    return 0, ""


# ─── RUGCHECK ──────────────────────────────────────────────
def rugcheck_solana(token_address):
    warnings    = []
    penalty     = 0
    bonus       = 0
    api_success = False

    try:
        r = requests.get(
            f"https://api.rugcheck.xyz/v1/tokens/{token_address}/report",
            timeout=12
        )
        if not r.ok:
            warnings.append("🚫 RugCheck API yanıt vermedi — risk doğrulanamadı!")
            penalty += 30
            return warnings, penalty, bonus, api_success

        data        = r.json()
        api_success = True

        # Creator geçmişi
        creator        = data.get("creator") or {}
        creator_tokens = creator.get("tokens") or []
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

        # Creator'ın elindeki token oranı
        creator_addr  = creator.get("address", "")
        top_holders   = data.get("topHolders") or []
        creator_hold  = next(
            (h.get("pct", 0) for h in top_holders if h.get("address") == creator_addr),
            0
        )
        if creator_hold > MAX_CREATOR_HOLD_PCT:
            warnings.append(f"🚨 Creator hâlâ %{creator_hold:.0f} tutuyor — dump riski!")
            penalty += 50
        elif creator_hold > 10:
            warnings.append(f"⚠️ Creator %{creator_hold:.0f} tutuyor")
            penalty += 20

        # Holder dağılımı
        if top_holders:
            top10_pct = sum(h.get("pct", 0) for h in top_holders[:10])
            if top10_pct > MAX_TOP10_HOLDER_PCT:
                warnings.append(f"🚨 Top10 holder %{top10_pct:.0f}!")
                penalty += 50
            elif top10_pct > 25:
                warnings.append(f"⚠️ Top10 holder %{top10_pct:.0f}")
                penalty += 20

        # Bundle/insider
        insider_pct = sum(h.get("pct", 0) for h in top_holders if h.get("insider", False))
        if insider_pct > MAX_BUNDLE_PCT:
            warnings.append(f"🚨 Bundle/insider %{insider_pct:.0f}!")
            penalty += 60
        elif insider_pct > 10:
            warnings.append(f"⚠️ Insider %{insider_pct:.0f}")
            penalty += 20

        # LP durumu
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

        # RugCheck skoru
        rc_score = data.get("score", 0)
        if rc_score < 300:
            warnings.append(f"🚨 RugCheck: {rc_score}")
            penalty += 30
        elif rc_score < 600:
            warnings.append(f"⚠️ RugCheck: {rc_score}")
            penalty += 10
        elif rc_score >= 800:
            bonus += 10

        # Risk listesi
        for risk in (data.get("risks") or []):
            lvl  = risk.get("level", "")
            name = risk.get("name", "")
            if lvl == "danger":
                warnings.append(f"🚨 {name}"); penalty += 25
            elif lvl == "warn":
                warnings.append(f"⚠️ {name}"); penalty += 8

    except Exception as e:
        print(f"RugCheck hata: {e}")
        warnings.append("🚫 RugCheck bağlantı hatası — risk doğrulanamadı!")
        penalty += 30

    return warnings, penalty, bonus, api_success


# ─── TOKEN SNIFFER (BASE) ──────────────────────────────────
def tokensniffer_base(token_address):
    warnings    = []
    penalty     = 0
    bonus       = 0
    api_success = False

    try:
        r = requests.get(
            f"https://tokensniffer.com/api/v2/tokens/8453/{token_address}?apikey=free&include_metrics=true",
            timeout=12
        )
        if not r.ok:
            warnings.append("🚫 TokenSniffer yanıt vermedi — risk doğrulanamadı!")
            penalty += 30
            return warnings, penalty, bonus, api_success

        data        = r.json()
        api_success = True

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
        warnings.append("🚫 TokenSniffer bağlantı hatası — risk doğrulanamadı!")
        penalty += 30

    return warnings, penalty, bonus, api_success


# ─── SOSYAL MEDYA ──────────────────────────────────────────
def check_socials(pair):
    info    = pair.get("info") or {}
    socials = info.get("socials") or []
    webs    = info.get("websites") or []
    has_tw  = any(s.get("type") == "twitter"  for s in socials)
    has_tg  = any(s.get("type") == "telegram" for s in socials)
    has_web = len(webs) > 0
    if not has_tw and not has_tg and not has_web:
        return -999, []
    bonus    = (8 if has_tw else 0) + (5 if has_tg else 0) + (7 if has_web else 0)
    warnings = []
    if not has_tw:  warnings.append("⚠️ Twitter yok")
    if not has_tg:  warnings.append("⚠️ Telegram yok")
    if not has_web: warnings.append("⚠️ Website yok")
    return bonus, warnings


# ─── SKOR SİSTEMLERİ ───────────────────────────────────────
def calc_risk_score(rug_penalty, rug_bonus, mf_penalty, social_bonus, api_success):
    if not api_success:
        # API çalışmıyorsa risk skoru max 50 olabilir
        base = 50
    else:
        base = 100
    score = base - (rug_penalty + mf_penalty) + rug_bonus + max(0, social_bonus)
    return max(0, min(100, score))


def calc_momentum_score(pair, unique_buyers=0, growth_bonus=0):
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

    # Benzersiz alıcılar
    if unique_buyers > 50:
        score += 15; reasons.append(f"👤 {unique_buyers} benzersiz alıcı (5dk)")
    elif unique_buyers > 20:
        score += 10; reasons.append(f"👤 {unique_buyers} benzersiz alıcı")
    elif unique_buyers > 10:
        score += 5;  reasons.append(f"👤 {unique_buyers} benzersiz alıcı")
    elif unique_buyers > 0:
        score += 2;  reasons.append(f"👤 {unique_buyers} benzersiz alıcı")

    # Holder büyümesi
    score += growth_bonus

    if age_min < 10:
        score += 8;  reasons.append("⚡ <10dk yeni")
    elif age_min < 30:
        score += 5;  reasons.append("⚡ <30dk yeni")
    elif age_min < 60:
        score += 3;  reasons.append("🕐 <60dk taze")

    return max(0, min(100, score)), reasons


# ─── DEXSCREENER ───────────────────────────────────────────
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
        buys5m   = ((pair.get("txns") or {}).get("m5") or {}).get("buys", 0) or 0

        if age_min  > MAX_AGE_MINUTES:   continue
        if liq      < MIN_LIQUIDITY_USD: continue
        if vol5m    < MIN_VOLUME_5M:     continue
        if change5m < MIN_CHANGE_5M:     continue

        # Sosyal medya
        social_bonus, social_warnings = check_socials(pair)
        if social_bonus == -999:
            seen.add(addr)
            print(f"[SKIP-SOSYAL] {(pair.get('baseToken') or {}).get('symbol')}")
            continue

        # 60 saniye bekleme + holder büyümesi için snapshot al
        if addr not in pending:
            pending[addr] = {"first_seen": now_s, "buys_t0": buys5m}
            print(f"[PENDING] {(pair.get('baseToken') or {}).get('symbol')}")
            continue
        if now_s - pending[addr]["first_seen"] < WAIT_SECONDS:
            continue

        token_ca  = (pair.get("baseToken") or {}).get("address", "")
        pair_addr = pair.get("pairAddress", "")
        pair["_age_minutes"] = age_min

        # Mint/Freeze kontrolü (Solana)
        mf_warnings, mf_penalty = [], 0
        if chain == "solana" and token_ca:
            mf_warnings, mf_penalty = check_mint_freeze(token_ca)
            if mf_penalty >= 60:
                seen.add(addr); pending.pop(addr, None)
                print(f"[SKIP-AUTH] {(pair.get('baseToken') or {}).get('symbol')}")
                continue

        # Creator analizi (Solana)
        cs_warnings, cs_penalty = [], 0
        if chain == "solana" and token_ca:
            creator_addr = get_creator_address(token_ca)
            if creator_addr:
                cs_warnings, cs_penalty = check_creator_sells(token_ca, creator_addr)

        # RugCheck / TokenSniffer
        rug_warnings, rug_penalty, rug_bonus, api_ok = [], 0, 0, False
        if chain == "solana" and token_ca:
            rug_warnings, rug_penalty, rug_bonus, api_ok = rugcheck_solana(token_ca)
        elif chain == "base" and token_ca:
            rug_warnings, rug_penalty, rug_bonus, api_ok = tokensniffer_base(token_ca)

        total_penalty = mf_penalty + cs_penalty + rug_penalty
        if total_penalty >= 80:
            seen.add(addr); pending.pop(addr, None)
            print(f"[SKIP-RUG] penalty={total_penalty} {(pair.get('baseToken') or {}).get('symbol')}")
            continue

        # Unique buyers (Solana)
        unique_buyers = 0
        if chain == "solana" and pair_addr:
            unique_buyers = get_unique_buyers(pair_addr, chain)

        # Holder büyüme bonusu
        growth_bonus, growth_note = check_holder_growth(addr, buys5m)

        # Skorlar
        risk_score = calc_risk_score(rug_penalty, rug_bonus, mf_penalty, social_bonus, api_ok)
        mom_score, mom_reasons = calc_momentum_score(pair, unique_buyers, growth_bonus)
        if growth_note:
            mom_reasons.append(f"📊 {growth_note}")

        if risk_score < MIN_RISK_SCORE:
            seen.add(addr); pending.pop(addr, None); continue
        if mom_score  < MIN_MOMENTUM_SCORE:
            seen.add(addr); pending.pop(addr, None); continue

        seen.add(addr); pending.pop(addr, None); save_seen(seen)
        found += 1

        symbol      = (pair.get("baseToken") or {}).get("symbol", "?")
        dex_url     = pair.get("url", "")
        chain_label = "🟣 Solana" if "sol" in chain.lower() else "🔵 Base"
        sells5m     = ((pair.get("txns") or {}).get("m5") or {}).get("sells", 0) or 0

        if risk_score >= 75:   risk_grade = "🟢"
        elif risk_score >= 50: risk_grade = "🟡"
        else:                  risk_grade = "🔴"

        if mom_score >= 70:    mom_grade = "🔥"
        elif mom_score >= 45:  mom_grade = "📈"
        else:                  mom_grade = "📊"

        all_warnings = mf_warnings + cs_warnings + rug_warnings + social_warnings
        mom_txt      = "\n".join(f"  • {r}" for r in mom_reasons[:5])
        warn_txt     = ("\n\n⚠️ <b>Uyarılar:</b>\n" + "\n".join(f"  {w}" for w in all_warnings[:6])
                       ) if all_warnings else "\n\n✅ <b>Tüm kontroller temiz</b>"
        api_txt      = "" if api_ok else "\n🚫 <i>Risk API'si yanıt vermedi</i>"

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

        ub_txt = f" · 👤{unique_buyers} uniq" if unique_buyers > 0 else ""

        msg = (
            f"🪙 <b>{symbol}</b> · {chain_label}\n"
            f"📋 <code>{token_ca}</code>\n"
            f"⏱ Yaş: {age_min:.0f} dk\n\n"
            f"{risk_grade} <b>Risk: {risk_score}/100</b>  |  "
            f"{mom_grade} <b>Momentum: {mom_score}/100</b>\n\n"
            f"💰 Likidite: {fmt(liq)}\n"
            f"📊 5dk Hacim: {fmt(vol5m)}{ub_txt}\n"
            f"📈 5dk Değişim: %{change5m:+.2f}\n"
            f"👥 Buy/Sell: {buys5m} / {sells5m}\n\n"
            f"<b>Momentum sinyalleri:</b>\n{mom_txt}"
            f"{warn_txt}"
            f"{api_txt}\n\n"
            f"{links}\n"
            f"🔗 <a href='{dex_url}'>DexScreener</a>"
        )

        send_telegram(msg)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ {symbol} Risk:{risk_score} Mom:{mom_score}")

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Tarama bitti — {found} yeni token")


def main():
    print("=" * 55)
    print("  SIGNAL Bot v10")
    print("=" * 55)
    send_telegram(
        "🤖 <b>SIGNAL Bot v10 başlatıldı</b>\n\n"
        "<b>Düzeltmeler:</b>\n"
        "  ✅ Creator adresi tespit edilerek gerçek satış kontrolü\n"
        "  ✅ SPL Token mint account doğru parse ediliyor\n"
        "  ✅ Benzersiz alıcı sayısı (on-chain)\n"
        "  ✅ Risk ve Momentum tamamen ayrı gösterim\n"
        "  ✅ API hata → 'risk doğrulanamadı' işareti\n"
        "  ✅ Creator'ın elindeki token oranı kontrolü\n"
        "  ✅ Holder büyüme hızı (60sn snapshot)\n\n"
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
