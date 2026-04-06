#!/usr/bin/env python3
"""
Setka Cup Betting Bot - Source: score-tennis.com
Scan toutes les minutes. Analyse H2H global + H2H set 1.
Propose WIN 1er SET ou WIN MATCH selon l'analyse.
"""

import re
import time
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from config import (
    TELEGRAM_BOT_TOKEN,
    ALERT_DESTINATIONS,
    COMPETITIONS, MIN_FAVORITE_ODDS, MAX_FAVORITE_ODDS,
    MIN_H2H_MATCHES, MIN_WIN_RATE,
    SET1_ALERT_LABEL,
    IGNORE_ODDS_FILTER, REQUIRE_FAVORITE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 60  # scan toutes les minutes

BILAN_FILE = Path("bilan.json")

def load_bilan() -> dict:
    if BILAN_FILE.exists():
        try:
            return json.loads(BILAN_FILE.read_text())
        except:
            pass
    return {
        "set1": {"won": 0, "lost": 0},
        "match": {"won": 0, "lost": 0},
        "week": {
            "set1":  {"won": 0, "lost": 0},
            "match": {"won": 0, "lost": 0},
        },
    }

def save_bilan(b: dict):
    BILAN_FILE.write_text(json.dumps(b, indent=2))

alerted   = set()   # match_ids déjà alertés
tracking  = {}      # {mid: analysis_dict}
seen      = set()   # résultats déjà enregistrés

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

BASE_URL = "https://score-tennis.com"

COMP_SECTION_NAMES = {
    "setka_cup_cz":      "Setka Cup. Czech Republic",
    "setka_cup_ukraine": "Setka Cup",
    "setka_cup_intl":    "Setka Cup",
    "liga_pro_russia":   "Pro League",
    "pro_league_cz":     "Pro League. Czech Republic",
    "tt_cup_cz":         "TT-Cup. Czech Republic",
}

COMP_LABELS = {
    "setka_cup_cz":      "Setka Cup 🇨🇿",
    "setka_cup_ukraine": "Setka Cup 🇺🇦",
    "setka_cup_intl":    "Setka Cup 🌍",
    "liga_pro_russia":   "Liga Pro 🇷🇺",
    "pro_league_cz":     "Pro League 🇨🇿",
    "tt_cup_cz":         "TT Cup 🇨🇿",
}

# ─── Telegram ─────────────────────────────────────────────────────────────────

def send_telegram(message: str, destinations: list):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for dest in destinations:
        try:
            r = requests.post(url, json={
                "chat_id": dest, "text": message, "parse_mode": "HTML"
            }, timeout=10)
            if r.status_code == 200:
                log.info(f"✅ Envoyé → {dest}")
            else:
                log.warning(f"Telegram [{dest}]: {r.status_code} {r.text[:100]}")
        except Exception as e:
            log.error(f"Telegram exception [{dest}]: {e}")

# ─── Fetch ────────────────────────────────────────────────────────────────────

def fetch_page(url: str) -> BeautifulSoup | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        log.error(f"Fetch error [{url}]: {e}")
        return None

# ─── Parsing matchs à venir ───────────────────────────────────────────────────

def parse_upcoming_matches(competition_key: str) -> list:
    try:
        r = requests.get(f"{BASE_URL}/up-games/", headers=HEADERS, timeout=15)
        if r.status_code != 200:
            log.warning(f"up-games: {r.status_code}")
            return []
    except Exception as e:
        log.error(f"up-games error: {e}")
        return []

    soup         = BeautifulSoup(r.text, "html.parser")
    section_name = COMP_SECTION_NAMES.get(competition_key, "")
    today_str    = datetime.now(tz=timezone.utc).strftime("%d.%m")
    matches      = []

    # Découper en blocs par date/heure
    raw_html    = r.text
    text_blocks = re.split(r'(\d{2}\.\d{2}\s+\d{2}:\d{2})', raw_html)

    i = 1
    while i < len(text_blocks) - 1:
        header     = text_blocks[i]
        block_html = text_blocks[i + 1]

        date_m = re.match(r'(\d{2}\.\d{2})\s+(\d{2}:\d{2})', header)
        if not date_m:
            i += 2
            continue

        match_date = date_m.group(1)
        match_time = date_m.group(2)

        if match_date != today_str:
            i += 2
            continue

        if section_name and section_name not in block_html:
            i += 2
            continue

        block_soup = BeautifulSoup(block_html, "html.parser")
        block_text = block_soup.get_text(separator="\n")

        # Cotes
        w1_m = re.search(r'W1[^:]*:\s*([\d.]+)', block_text)
        w2_m = re.search(r'W2[^:]*:\s*([\d.]+)', block_text)
        if not w1_m or not w2_m:
            i += 2
            continue

        w1 = float(w1_m.group(1))
        w2 = float(w2_m.group(1))

        # Noms depuis liens /players/ dans ce bloc uniquement
        player_links = block_soup.find_all(
            "a", href=re.compile(r"score-tennis\.com/players/|^/players/")
        )

        if len(player_links) < 2:
            i += 2
            continue

        # Vérification : les deux liens doivent être dans le même bloc
        # et avoir "face-to-face" entre eux dans le texte
        if "face-to-face" not in block_text:
            i += 2
            continue

        p1 = " ".join(player_links[0].get_text().split())
        p2 = " ".join(player_links[1].get_text().split())

        if not p1 or not p2 or p1 == p2:
            i += 2
            continue

        # H2H global
        h2h_m  = re.search(r'(\d+)\s*:\s*(\d+)\s*\n?\s*score of recent face-to-face', block_text)
        h2h_p1 = h2h_p2 = 0
        if h2h_m:
            h2h_p1 = int(h2h_m.group(1))
            h2h_p2 = int(h2h_m.group(2))

        # URL de la page H2H détaillée (pour analyse set 1)
        h2h_url = None
        stats_link = block_soup.find("a", href=re.compile(r"score-tennis\.com/stats/|^/stats/"))
        if stats_link:
            href = stats_link.get("href", "")
            h2h_url = href if href.startswith("http") else f"{BASE_URL}{href}"

        mid = f"{p1}_{p2}_{match_date}_{match_time}"
        matches.append({
            "id":          mid,
            "player1":     p1,
            "player2":     p2,
            "time":        match_time,
            "date":        match_date,
            "competition": competition_key,
            "odds":        [w1, w2],
            "h2h":         {"p1_wins": h2h_p1, "p2_wins": h2h_p2, "total": h2h_p1 + h2h_p2},
            "h2h_url":     h2h_url,
        })
        log.info(f"✅ {p1} vs {p2} | W1={w1} W2={w2} | H2H {h2h_p1}:{h2h_p2}")
        i += 2

    log.info(f"[{competition_key}] {len(matches)} matchs")
    return matches

# ─── Analyse set 1 depuis page H2H ───────────────────────────────────────────

def analyze_set1_from_h2h(h2h_url: str, fav_is_p1: bool) -> dict:
    """
    Scrape la page H2H détaillée pour calculer le taux de victoire
    du favori au set 1 spécifiquement.
    Retourne {"set1_wins": int, "set1_total": int, "set1_rate": float}
    """
    empty = {"set1_wins": 0, "set1_total": 0, "set1_rate": 0.0}
    if not h2h_url:
        return empty

    soup = fetch_page(h2h_url)
    if not soup:
        return empty

    try:
        set1_wins = 0
        set1_total = 0

        # Chercher les lignes de matchs H2H avec scores par set
        # Format : | DD.MM | Score | set1_p1 | set1_p2 | ...
        rows = soup.select("table tr")
        for row in rows:
            cols = [c.get_text(strip=True) for c in row.find_all("td")]
            if len(cols) < 4:
                continue

            # Colonnes set 1 : généralement cols[2] et cols[3]
            try:
                s1_p1 = int(cols[2]) if cols[2].isdigit() else None
                s1_p2 = int(cols[3]) if cols[3].isdigit() else None
                if s1_p1 is not None and s1_p2 is not None:
                    set1_total += 1
                    fav_won_s1 = (s1_p1 > s1_p2) if fav_is_p1 else (s1_p2 > s1_p1)
                    if fav_won_s1:
                        set1_wins += 1
            except:
                continue

        rate = set1_wins / set1_total if set1_total > 0 else 0.0
        return {"set1_wins": set1_wins, "set1_total": set1_total, "set1_rate": rate}

    except Exception as e:
        log.debug(f"Set1 analysis error: {e}")
        return empty

# ─── Logique d'analyse ────────────────────────────────────────────────────────

def analyze_match(match: dict) -> dict | None:
    """
    Analyse complète :
    - H2H global → confiance générale
    - H2H set 1 → propose WIN 1er SET ou WIN MATCH
    """
    h2h      = match.get("h2h", {"p1_wins": 0, "p2_wins": 0, "total": 0})
    score_p1 = 50

    if h2h["total"] >= 1:
        win_rate = h2h["p1_wins"] / h2h["total"]
        score_p1 += int((win_rate - 0.5) * 60)

    score_p1 = max(0, min(score_p1, 100))
    log.info(f"📊 {match['player1']} vs {match['player2']} | Score P1: {score_p1}")

    if score_p1 >= 60:
        bet_on   = "player1"
        fav_name = match["player1"]
        und_name = match["player2"]
        confidence = score_p1
        fav_is_p1  = True
    elif score_p1 <= 40:
        bet_on   = "player2"
        fav_name = match["player2"]
        und_name = match["player1"]
        confidence = 100 - score_p1
        fav_is_p1  = False
    else:
        return None

    # Analyse set 1 spécifique
    set1_data = analyze_set1_from_h2h(match.get("h2h_url"), fav_is_p1)
    log.info(f"Set1 data: {set1_data}")

    # Décision WIN SET 1 vs WIN MATCH
    if set1_data["set1_total"] >= 2:
        if set1_data["set1_rate"] >= 0.65:
            bet_type = "SET1"   # fort en set 1 → on joue le set 1
        elif set1_data["set1_rate"] >= 0.50:
            bet_type = "MATCH"  # domine globalement mais pas toujours au set 1
        else:
            bet_type = "MATCH"  # perd souvent le set 1 mais gagne le match
    else:
        bet_type = "SET1"  # pas assez de données set 1 → on reste sur set 1 par défaut

    return {
        "bet_on":      bet_on,
        "bet_type":    bet_type,  # "SET1" ou "MATCH"
        "favorite":    fav_name,
        "underdog":    und_name,
        "confidence":  confidence,
        "fav_is_p1":   fav_is_p1,
        "set1_data":   set1_data,
    }

# ─── Traitement alerte ────────────────────────────────────────────────────────

def process_alert(match: dict):
    mid = match["id"]
    if mid in alerted:
        return

    odds = match.get("odds", [])
    if len(odds) < 2:
        return

    o1, o2 = odds[0], odds[1]
    log.info(f"🔍 {match['player1']} ({o1}) vs {match['player2']} ({o2})")

    # Option 3 : favori requis
    if REQUIRE_FAVORITE and min(o1, o2) > MAX_FAVORITE_ODDS:
        log.info(f"❌ Pas de favori clair")
        return

    verdict = analyze_match(match)
    if not verdict:
        return

    # Cote du favori désigné
    fav_odds = o1 if verdict["fav_is_p1"] else o2

    # Option 1 : filtre fenêtre de cotes
    odds_in_window = MIN_FAVORITE_ODDS <= fav_odds <= MAX_FAVORITE_ODDS
    if not IGNORE_ODDS_FILTER and not odds_in_window:
        log.info(f"❌ Cote {fav_odds} hors fenêtre [{MIN_FAVORITE_ODDS}-{MAX_FAVORITE_ODDS}]")
        return

    h2h = match.get("h2h", {})
    analysis = {
        "match_id":       mid,
        "player1":        match["player1"],
        "player2":        match["player2"],
        "favorite":       verdict["favorite"],
        "underdog":       verdict["underdog"],
        "fav_odds":       fav_odds,
        "und_odds":       o2 if verdict["fav_is_p1"] else o1,
        "confidence":     verdict["confidence"],
        "bet_type":       verdict["bet_type"],
        "h2h_wins":       h2h.get("p1_wins") if verdict["fav_is_p1"] else h2h.get("p2_wins"),
        "h2h_total":      h2h.get("total", 0),
        "set1_data":      verdict["set1_data"],
        "time":           match["time"],
        "competition":    match["competition"],
        "odds_in_window": odds_in_window,
    }

    send_telegram(format_alert(analysis), ALERT_DESTINATIONS)
    alerted.add(mid)
    tracking[mid] = analysis
    log.info(f"✅ Alerte: {verdict['favorite']} | {verdict['bet_type']}")

# ─── Résultats + bilan ────────────────────────────────────────────────────────

def check_results():
    if not tracking:
        return

    bilan   = load_bilan()
    changed = False

    soup = fetch_page(f"{BASE_URL}/games/")
    if not soup:
        return

    content = soup.get_text(separator="\n")

    for mid, a in list(tracking.items()):
        if mid in seen:
            continue

        p1_first  = a["player1"].split()[0].lower()
        p2_first  = a["player2"].split()[0].lower()
        fav_is_p1 = a["favorite"] == a["player1"]

        idx = content.lower().find(p1_first)
        if idx == -1:
            continue

        snippet = content[idx:idx+400]
        if p2_first not in snippet.lower():
            continue

        # Score final du match ex: "3:1" "3:0" "2:3"
        final_m = re.search(r'\b([0-4])\s*:\s*([0-4])\b', snippet)
        if not final_m:
            continue

        s1, s2 = int(final_m.group(1)), int(final_m.group(2))
        # Vérifier que c'est bien un score de match (somme >= 2, max <= 7)
        if s1 + s2 < 2 or s1 + s2 > 7:
            continue

        fav_won_match = (s1 > s2) if fav_is_p1 else (s2 > s1)

        # Chercher score set 1 après le score global
        # Format page résultats : score global puis (set1_p1:set1_p2, ...)
        set1_section = snippet[final_m.end():]
        set1_m = re.search(r'\(?\s*(\d+)\s*:\s*(\d+)', set1_section)

        if set1_m:
            ss1, ss2    = int(set1_m.group(1)), int(set1_m.group(2))
            fav_won_s1  = (ss1 > ss2) if fav_is_p1 else (ss2 > ss1)
        else:
            fav_won_s1 = fav_won_match  # fallback

        # Enregistrer selon le type de pari
        if a["bet_type"] == "SET1":
            key = "won" if fav_won_s1 else "lost"
            bilan["set1"][key] += 1
            bilan["week"]["set1"][key] += 1
            log.info(f"📊 Bilan SET1: {key} — {a['favorite']}")
        else:
            key = "won" if fav_won_match else "lost"
            bilan["match"][key] += 1
            bilan["week"]["match"][key] += 1
            log.info(f"📊 Bilan MATCH: {key} — {a['favorite']}")

        seen.add(mid)
        changed = True

    if changed:
        save_bilan(bilan)

# ─── Bilans ───────────────────────────────────────────────────────────────────

def pct(won, lost):
    total = won + lost
    return f"{int(won/total*100)}%" if total > 0 else "N/A"

def format_bilan(b: dict, period: str) -> str:
    s1    = b["set1"]  if period == "daily" else b["week"]["set1"]
    ma    = b["match"] if period == "daily" else b["week"]["match"]
    label = "📅 BILAN QUOTIDIEN" if period == "daily" else "📆 BILAN HEBDOMADAIRE"
    return (
        f"{label}\n━━━━━━━━━━━━━━━━━━━━\n"
        f"🏓 <b>WIN 1er SET</b>\n"
        f"  ✅ {s1['won']} gagné  ({pct(s1['won'], s1['lost'])})\n"
        f"  ❌ {s1['lost']} perdu\n\n"
        f"🏆 <b>WIN MATCH</b>\n"
        f"  ✅ {ma['won']} gagné  ({pct(ma['won'], ma['lost'])})\n"
        f"  ❌ {ma['lost']} perdu\n"
    )

def bilan_thread():
    last_daily  = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    last_weekly = datetime.now(tz=timezone.utc).strftime("%Y-W%W")
    while True:
        now      = datetime.now(tz=timezone.utc)
        date_str = now.strftime("%Y-%m-%d")
        week_str = now.strftime("%Y-W%W")

        if now.hour == 0 and now.minute == 0 and date_str != last_daily:
            bilan = load_bilan()
            send_telegram(format_bilan(bilan, "daily"), ALERT_DESTINATIONS)
            bilan["set1"]  = {"won": 0, "lost": 0}
            bilan["match"] = {"won": 0, "lost": 0}
            save_bilan(bilan)
            last_daily = date_str
            log.info("📅 Bilan quotidien envoyé")

        if now.weekday() == 6 and now.hour == 0 and now.minute == 0 and week_str != last_weekly:
            bilan = load_bilan()
            send_telegram(format_bilan(bilan, "weekly"), ALERT_DESTINATIONS)
            bilan["week"] = {
                "set1":  {"won": 0, "lost": 0},
                "match": {"won": 0, "lost": 0},
            }
            save_bilan(bilan)
            last_weekly = week_str
            log.info("📆 Bilan hebdomadaire envoyé")

        time.sleep(60)

# ─── Format message ───────────────────────────────────────────────────────────

def format_alert(a: dict) -> str:
    comp     = COMP_LABELS.get(a["competition"], a["competition"])
    h2h_str  = f"{a['h2h_wins']}/{a['h2h_total']}" if a.get("h2h_total") else "N/A"
    s1       = a.get("set1_data", {})
    s1_str   = f"{s1['set1_wins']}/{s1['set1_total']} set 1" if s1.get("set1_total") else ""

    # Type de pari
    if a["bet_type"] == "SET1":
        pari_str = f"✅ <b>PARI : {a['favorite']} — WIN 1er SET</b>"
    else:
        pari_str = f"✅ <b>PARI : {a['favorite']} — WIN MATCH</b>"

    # Indicateur de confiance
    conf = a["confidence"]
    if conf >= 80:
        conf_emoji = "🔥"
    elif conf >= 65:
        conf_emoji = "💪"
    else:
        conf_emoji = "📈"

    # Note si hors fenêtre normale
    odds_note = "\n⚠️ <i>Cote hors fenêtre normale</i>" if (IGNORE_ODDS_FILTER and not a["odds_in_window"]) else ""

    # Ligne analyse H2H
    h2h_line = f"📊 H2H : {h2h_str}"
    if s1_str:
        h2h_line += f" | {s1_str}"

    now_utc = datetime.now(tz=timezone.utc).strftime("%H:%M UTC")

    return (
        f"🏓 <b>{comp}</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"⚔️  {a['player1']} ({a['fav_odds'] if a['favorite']==a['player1'] else a['und_odds']}) "
        f"vs {a['player2']} ({a['und_odds'] if a['favorite']==a['player1'] else a['fav_odds']})\n"
        f"🕐  {a['time']} UTC{odds_note}\n\n"
        f"{pari_str}\n"
        f"{h2h_line}\n"
        f"{conf_emoji} Confiance : {conf}%"
    )

# ─── Boucle principale ───────────────────────────────────────────────────────

def run():
    log.info("🚀 Bot démarré")
    opts = [
        f"• Favori requis (cote ≤ {MAX_FAVORITE_ODDS}) : {'✅' if REQUIRE_FAVORITE else '❌'}",
        f"• Filtre cotes [{MIN_FAVORITE_ODDS}-{MAX_FAVORITE_ODDS}] : {'❌ désactivé' if IGNORE_ODDS_FILTER else '✅'}",
        f"• Compétitions : {', '.join(COMPETITIONS)}",
        f"• Scan toutes les {CHECK_INTERVAL_SECONDS}s",
        f"• Source : score-tennis.com",
    ]
    send_telegram(
        "🤖 <b>Bot Setka Cup démarré</b>\n━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(opts),
        ALERT_DESTINATIONS
    )

    threading.Thread(target=bilan_thread, daemon=True).start()

    while True:
        try:
            for comp in COMPETITIONS:
                matches = parse_upcoming_matches(comp)
                for match in matches:
                    process_alert(match)
            check_results()

        except Exception as e:
            log.error(f"Erreur boucle: {e}")

        log.info(f"⏳ Pause {CHECK_INTERVAL_SECONDS}s...")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
