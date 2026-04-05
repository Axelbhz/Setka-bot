#!/usr/bin/env python3
"""
Setka Cup Betting Bot
Source : score-tennis.com
─────────────────────────
Section A : Alertes paris (toutes les 3 min)
Section B : Récap matin (08h UTC) + après-midi (14h UTC)
Bilan      : Quotidien + hebdomadaire à minuit
"""

import re
import time
import json
import logging
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from config import (
    TELEGRAM_BOT_TOKEN,
    ALERT_DESTINATIONS, RECAP_DESTINATIONS,
    COMPETITIONS, MIN_FAVORITE_ODDS, MAX_FAVORITE_ODDS,
    MIN_H2H_MATCHES, MIN_WIN_RATE, CHECK_INTERVAL_MINUTES,
    SET1_ALERT_LABEL, SET2_ALERT_LABEL,
    IGNORE_ODDS_FILTER, DISABLE_SET2_RECOVERY, REQUIRE_FAVORITE,
    RECAP_INTERVAL_HOURS, RECAP_WINDOW_HOURS,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ─── Persistance bilan ────────────────────────────────────────────────────────
BILAN_FILE = Path("bilan.json")

def load_bilan() -> dict:
    if BILAN_FILE.exists():
        try:
            return json.loads(BILAN_FILE.read_text())
        except:
            pass
    return {
        "set1": {"won": 0, "lost": 0},
        "set2": {"won": 0, "lost": 0},
        "week": {"set1": {"won": 0, "lost": 0}, "set2": {"won": 0, "lost": 0}},
    }

def save_bilan(b: dict):
    BILAN_FILE.write_text(json.dumps(b, indent=2))

# ─── Mémoire session ──────────────────────────────────────────────────────────
alerted_set1  = set()
alerted_set2  = set()
live_tracking = {}
results_seen  = set()

# Heures UTC des récaps fixes
RECAP_HOURS_UTC = [8, 14]

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

# ─── Fetch page ───────────────────────────────────────────────────────────────

def fetch_page(url: str) -> BeautifulSoup | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        log.error(f"Fetch error [{url}]: {e}")
        return None

def fetch_raw(url: str) -> str | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return r.text
    except Exception as e:
        log.error(f"Fetch raw error [{url}]: {e}")
        return None

# ─── Scraping matchs à venir ──────────────────────────────────────────────────

def parse_upcoming_matches(competition_key: str) -> list:
    """
    Parse score-tennis.com/up-games/ en utilisant BeautifulSoup
    pour extraire les noms depuis les liens /players/ directement.
    Structure : bloc par match contenant :
      - date/heure dans le texte
      - nom compétition dans un div.tag
      - W1/W2 dans le texte
      - deux liens <a href="/players/..."> pour les joueurs
      - score H2H "X : Y" + "score of recent face-to-face"
    """
    html = fetch_raw(f"{BASE_URL}/up-games/")
    if not html:
        return []

    soup         = BeautifulSoup(html, "html.parser")
    section_name = COMP_SECTION_NAMES.get(competition_key, "")
    today_str    = datetime.now(tz=timezone.utc).strftime("%d.%m")
    matches      = []

    # On cherche tous les liens joueurs
    player_links = soup.find_all("a", href=re.compile(r"^https?://score-tennis\.com/players/"))

    i = 0
    while i < len(player_links) - 1:
        a1 = player_links[i]
        a2 = player_links[i + 1]

        p1 = " ".join(a1.get_text(strip=True).split())
        p2 = " ".join(a2.get_text(strip=True).split())

        if not p1 or not p2:
            i += 1
            continue

        # Chercher le conteneur parent commun
        # Remonter depuis a1 pour trouver le bloc du match
        parent = a1.parent
        for _ in range(8):
            if parent is None:
                break
            parent_text = parent.get_text()
            if "face-to-face" in parent_text and p2 in parent_text:
                break
            parent = parent.parent

        if parent is None:
            i += 1
            continue

        block_text = parent.get_text(separator="\n")

        # Vérifier que P2 est bien dans ce bloc
        if p2 not in block_text:
            i += 1
            continue

        # Date/heure
        time_m = re.search(r'(\d{2}\.\d{2})\s+(\d{2}:\d{2})', block_text)
        if not time_m:
            i += 2
            continue

        match_date = time_m.group(1)
        match_time = time_m.group(2)

        if match_date != today_str:
            i += 2
            continue

        # Compétition
        if section_name and section_name not in block_text:
            i += 2
            continue

        # Cotes
        w1_m = re.search(r'W1[^:]*:\s*([\d.]+)', block_text)
        w2_m = re.search(r'W2[^:]*:\s*([\d.]+)', block_text)
        if not w1_m or not w2_m:
            i += 2
            continue

        w1 = float(w1_m.group(1))
        w2 = float(w2_m.group(1))

        # H2H : chercher "X : Y" avant "face-to-face"
        h2h_m  = re.search(r'(\d+)\s*:\s*(\d+)\s*\n?\s*score of recent face-to-face', block_text)
        h2h_p1 = h2h_p2 = 0
        if h2h_m:
            h2h_p1 = int(h2h_m.group(1))
            h2h_p2 = int(h2h_m.group(2))

        mid = f"{p1}_{p2}_{match_date}_{match_time}"
        matches.append({
            "id":          mid,
            "player1":     p1,
            "player2":     p2,
            "time":        match_time,
            "competition": competition_key,
            "odds":        [w1, w2],
            "h2h":         {"p1_wins": h2h_p1, "p2_wins": h2h_p2, "total": h2h_p1 + h2h_p2},
        })
        log.info(f"✅ {p1} vs {p2} | W1={w1} W2={w2} | H2H {h2h_p1}:{h2h_p2}")
        i += 2

    log.info(f"[{competition_key}] {len(matches)} matchs")
    return matches

# ─── Logique d'analyse ────────────────────────────────────────────────────────

def analyze_match_logic(match: dict) -> dict | None:
    h2h      = match.get("h2h", {"p1_wins": 0, "p2_wins": 0, "total": 0})
    score_p1 = 50

    if h2h["total"] >= 1:
        win_rate_p1  = h2h["p1_wins"] / h2h["total"]
        score_p1    += int((win_rate_p1 - 0.5) * 60)

    score_p1 = max(0, min(score_p1, 100))
    log.info(f"📊 {match['player1']} vs {match['player2']} | Score P1: {score_p1}")

    if score_p1 >= 60:
        return {"bet_on": "player1", "name": match["player1"], "confidence": score_p1}
    elif score_p1 <= 40:
        return {"bet_on": "player2", "name": match["player2"], "confidence": 100 - score_p1}

    return None

# ─── Traitement alerte paris ──────────────────────────────────────────────────

def process_alert(match: dict):
    mid = match["id"]
    if mid in alerted_set1:
        return

    odds = match.get("odds", [])
    if len(odds) < 2:
        return

    o1, o2 = odds[0], odds[1]
    log.info(f"🔍 {match['player1']} ({o1}) vs {match['player2']} ({o2}) | H2H: {match.get('h2h')}")

    if REQUIRE_FAVORITE and min(o1, o2) > MAX_FAVORITE_ODDS:
        log.info(f"❌ Pas de favori clair")
        return

    verdict = analyze_match_logic(match)
    if not verdict:
        return

    if verdict["bet_on"] == "player1":
        fav_name, und_name = match["player1"], match["player2"]
        fav_odds = o1
    else:
        fav_name, und_name = match["player2"], match["player1"]
        fav_odds = o2

    odds_in_window = MIN_FAVORITE_ODDS <= fav_odds <= MAX_FAVORITE_ODDS
    if not IGNORE_ODDS_FILTER and not odds_in_window:
        log.info(f"❌ Cote {fav_odds} hors fenêtre [{MIN_FAVORITE_ODDS}-{MAX_FAVORITE_ODDS}]")
        return

    h2h = match.get("h2h", {})
    analysis = {
        "match_id":       mid,
        "player1":        match["player1"],
        "player2":        match["player2"],
        "favorite":       fav_name,
        "underdog":       und_name,
        "fav_odds":       fav_odds,
        "confidence":     verdict["confidence"],
        "h2h_wins":       h2h.get("p1_wins") if verdict["bet_on"] == "player1" else h2h.get("p2_wins"),
        "h2h_total":      h2h.get("total", 0),
        "time":           match["time"],
        "competition":    match["competition"],
        "odds_in_window": odds_in_window,
    }

    send_telegram(format_set1_alert(analysis), ALERT_DESTINATIONS)
    alerted_set1.add(mid)
    live_tracking[mid] = analysis
    log.info(f"✅ Alerte set1: {fav_name} vs {und_name}")

# ─── Surveillance live + résultats ───────────────────────────────────────────

def check_live_and_results():
    if not live_tracking:
        return

    bilan   = load_bilan()
    changed = False

    # Scrape la page résultats du jour
    soup = fetch_page(f"{BASE_URL}/games/")
    if not soup:
        return

    content = soup.get_text(separator="\n")

    for mid, a in list(live_tracking.items()):
        if mid in results_seen:
            continue

        p1_first  = a["player1"].split()[0].lower()
        p2_first  = a["player2"].split()[0].lower()
        fav_is_p1 = a["favorite"] == a["player1"]

        # Chercher le match dans les résultats
        # Format sur games/ : "Nom1 – Nom2" puis score "3:1"
        idx = content.lower().find(p1_first)
        if idx == -1:
            continue

        snippet = content[idx:idx+300]

        # Vérifier que P2 est aussi dans le snippet
        if p2_first not in snippet.lower():
            continue

        # Chercher le score final ex: "3:1" ou "3:2"
        score_m = re.search(r'\b([0-3])\s*:\s*([0-3])\b', snippet)
        if not score_m:
            # Match pas encore terminé — chercher score set 1 en live
            # Format live : "0 : 1" entre les noms
            set_score_m = re.search(r'\b0\s*:\s*1\b|\b1\s*:\s*0\b', snippet)
            if set_score_m and mid not in alerted_set2 and not DISABLE_SET2_RECOVERY:
                score_txt = set_score_m.group()
                parts     = re.findall(r'\d+', score_txt)
                ss1, ss2  = int(parts[0]), int(parts[1])
                fav_lost  = (ss1 < ss2) if fav_is_p1 else (ss2 < ss1)
                if fav_lost:
                    send_telegram(format_set2_alert(a), ALERT_DESTINATIONS)
                    alerted_set2.add(mid)
                    log.info(f"⚠️ Alerte set2: {a['favorite']}")
            continue

        # Match terminé — enregistrer le bilan
        s1, s2   = int(score_m.group(1)), int(score_m.group(2))
        fav_won  = (s1 > s2) if fav_is_p1 else (s2 > s1)

        if mid in alerted_set1 and mid not in alerted_set2:
            key = "won" if fav_won else "lost"
            bilan["set1"][key] += 1
            bilan["week"]["set1"][key] += 1
            results_seen.add(mid)
            changed = True
            log.info(f"📊 Bilan set1: {key} — {a['favorite']}")

        elif mid in alerted_set2:
            key = "won" if fav_won else "lost"
            bilan["set2"][key] += 1
            bilan["week"]["set2"][key] += 1
            results_seen.add(mid)
            changed = True
            log.info(f"📊 Bilan set2: {key} — {a['favorite']}")

    if changed:
        save_bilan(bilan)

# ─── Section B : Récap à heures fixes ────────────────────────────────────────

def build_recap(all_matches: list) -> str | None:
    grouped = {}

    for match in all_matches:
        odds = match.get("odds", [])
        if len(odds) < 2:
            continue
        o1, o2 = odds[0], odds[1]

        # Option 3 : favori requis
        if REQUIRE_FAVORITE and min(o1, o2) > MAX_FAVORITE_ODDS:
            continue

        # Option 1 : filtre fenêtre de cotes
        if not IGNORE_ODDS_FILTER:
            if not (MIN_FAVORITE_ODDS <= min(o1, o2) <= MAX_FAVORITE_ODDS):
                continue

        label = COMP_LABELS.get(match["competition"], match["competition"])
        grouped.setdefault(label, []).append(
            f"  🕐 {match['time']} — {match['player1']} vs {match['player2']}"
        )

    if not grouped:
        return None

    now   = datetime.now(tz=timezone.utc)
    total = sum(len(v) for v in grouped.values())
    msg   = f"📋 <b>MATCHS DU JOUR — {now.strftime('%H:%M')} UTC</b>\n━━━━━━━━━━━━━━━━━━━━\n"
    for label, lines in grouped.items():
        msg += f"\n🏆 <b>{label}</b>\n" + "\n".join(lines) + "\n"
    msg += f"\n━━━━━━━━━━━━━━━━━━━━\n{total} match(s)"
    return msg

def recap_thread():
    """Thread qui envoie le récap à heures fixes."""
    sent_hours = set()  # {(date_str, hour)}

    while True:
        now  = datetime.now(tz=timezone.utc)
        key  = (now.strftime("%Y-%m-%d"), now.hour)

        if now.hour in RECAP_HOURS_UTC and key not in sent_hours:
            try:
                all_matches = []
                for comp in COMPETITIONS:
                    all_matches.extend(parse_upcoming_matches(comp))

                msg = build_recap(all_matches)
                if msg:
                    send_telegram(msg, RECAP_DESTINATIONS)
                    log.info(f"📋 Récap {now.hour}h envoyé")
                else:
                    log.info(f"📋 Récap {now.hour}h : aucun match filtré")

                sent_hours.add(key)
                # Garder seulement les 10 dernières clés
                if len(sent_hours) > 10:
                    sent_hours.pop()
            except Exception as e:
                log.error(f"Recap thread error: {e}")

        time.sleep(60)

# ─── Bilans ───────────────────────────────────────────────────────────────────

def pct(won, lost):
    total = won + lost
    return f"{int(won/total*100)}%" if total > 0 else "N/A"

def format_bilan(b: dict, period: str) -> str:
    s1    = b["set1"] if period == "daily" else b["week"]["set1"]
    s2    = b["set2"] if period == "daily" else b["week"]["set2"]
    label = "📅 BILAN QUOTIDIEN" if period == "daily" else "📆 BILAN HEBDOMADAIRE"
    return (
        f"{label}\n━━━━━━━━━━━━━━━━━━━━\n"
        f"🏓 <b>SET 1</b>\n"
        f"  ✅ Gagné : {s1['won']}  ({pct(s1['won'], s1['lost'])})\n"
        f"  ❌ Perdu : {s1['lost']}  ({pct(s1['lost'], s1['won'])})\n\n"
        f"🔄 <b>SET 2 (récupération)</b>\n"
        f"  ✅ Gagné : {s2['won']}  ({pct(s2['won'], s2['lost'])})\n"
        f"  ❌ Perdu : {s2['lost']}  ({pct(s2['lost'], s2['won'])})\n"
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
            send_telegram(format_bilan(bilan, "daily"), ALERT_DESTINATIONS + RECAP_DESTINATIONS)
            bilan["set1"] = {"won": 0, "lost": 0}
            bilan["set2"] = {"won": 0, "lost": 0}
            save_bilan(bilan)
            last_daily = date_str
            log.info("📅 Bilan quotidien envoyé")

        if now.weekday() == 6 and now.hour == 0 and now.minute == 0 and week_str != last_weekly:
            bilan = load_bilan()
            send_telegram(format_bilan(bilan, "weekly"), ALERT_DESTINATIONS + RECAP_DESTINATIONS)
            bilan["week"] = {"set1": {"won": 0, "lost": 0}, "set2": {"won": 0, "lost": 0}}
            save_bilan(bilan)
            last_weekly = week_str
            log.info("📆 Bilan hebdomadaire envoyé")

        time.sleep(60)

# ─── Formats messages ─────────────────────────────────────────────────────────

def format_set1_alert(a: dict) -> str:
    comp      = COMP_LABELS.get(a["competition"], a["competition"])
    h2h_str   = f"{a['h2h_wins']}/{a['h2h_total']}" if a.get("h2h_total") else "N/A"
    odds_note = "\n⚠️ <i>Mode analyse : cote hors fenêtre normale</i>" if (IGNORE_ODDS_FILTER and not a["odds_in_window"]) else ""
    return (
        f"🏓 <b>{comp}</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"⚔️  {a['player1']} vs {a['player2']}\n"
        f"🕐  {a['time']}\n\n"
        f"📊 <b>ANALYSE</b>\n"
        f"• Favori : <b>{a['favorite']}</b>\n"
        f"• H2H : {h2h_str} victoires\n"
        f"• Confiance : {a['confidence']}%\n"
        f"{odds_note}\n"
        f"✅ <b>PARI : {SET1_ALERT_LABEL} — {a['favorite']}</b>\n"
        f"💰 Cote set 1 : vérifier sur 1xbet"
    )

def format_set2_alert(a: dict) -> str:
    comp = COMP_LABELS.get(a["competition"], a["competition"])
    return (
        f"⚠️ <b>SET 1 PERDU — {comp}</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"⚔️  {a['player1']} vs {a['player2']}\n\n"
        f"🔄 <b>OPTION : {SET2_ALERT_LABEL} — {a['favorite']}</b>\n"
        f"💰 Cote set 2 : vérifier sur 1xbet\n\n"
        f"⚡ <i>Si perd encore → on laisse</i>"
    )

# ─── Boucle principale ───────────────────────────────────────────────────────

def run():
    log.info("🚀 Bot démarré")
    opts = [
        f"• Favori requis (cote ≤ {MAX_FAVORITE_ODDS}) : {'✅' if REQUIRE_FAVORITE else '❌'}",
        f"• Filtre cotes [{MIN_FAVORITE_ODDS}-{MAX_FAVORITE_ODDS}] : {'❌ désactivé' if IGNORE_ODDS_FILTER else '✅'}",
        f"• Récupération set 2 : {'❌ désactivée' if DISABLE_SET2_RECOVERY else '✅'}",
        f"• Compétitions : {', '.join(COMPETITIONS)}",
        f"• Récap à 08h00 et 14h00 UTC",
        f"• Source : score-tennis.com",
    ]
    send_telegram(
        "🤖 <b>Bot Setka Cup démarré</b>\n━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(opts),
        ALERT_DESTINATIONS
    )

    threading.Thread(target=bilan_thread, daemon=True).start()
    threading.Thread(target=recap_thread, daemon=True).start()

    while True:
        try:
            for comp in COMPETITIONS:
                matches = parse_upcoming_matches(comp)
                for match in matches:
                    process_alert(match)

            check_live_and_results()

        except Exception as e:
            log.error(f"Erreur boucle: {e}")

        log.info(f"⏳ Pause {CHECK_INTERVAL_MINUTES} min...")
        time.sleep(CHECK_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    run()
