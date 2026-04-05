#!/usr/bin/env python3
"""
Setka Cup Betting Bot
Source : score-tennis.com (scraping gratuit)
─────────────────────────────────────────────
Section A : Alertes paris (toutes les 3 min)
Section B : Récap toutes les 2h
Bilan      : Quotidien et hebdomadaire à minuit
"""

import re
import time
import json
import logging
import threading
from datetime import datetime, timezone, timedelta, date
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
live_tracking = {}  # {match_id: analysis_dict}
results_seen  = set()  # matchs dont on a déjà enregistré le résultat

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

BASE_URL = "https://score-tennis.com"

# Mapping compétition → nom sur score-tennis.com
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

# ─── Scraping score-tennis.com ────────────────────────────────────────────────

def fetch_page(url: str) -> BeautifulSoup | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        log.error(f"Fetch error [{url}]: {e}")
        return None

def parse_odds(text: str) -> float:
    """Extrait une cote depuis un texte comme 'W1: 1.35'"""
    try:
        m = re.search(r'[\d]+\.[\d]+', text)
        return float(m.group()) if m else 0.0
    except:
        return 0.0

def parse_upcoming_matches(competition_key: str) -> list:
    """
    Scrape score-tennis.com/up-games/ et retourne les matchs à venir
    pour la compétition demandée.
    """
    soup = fetch_page(f"{BASE_URL}/up-games/")
    if not soup:
        return []

    section_name = COMP_SECTION_NAMES.get(competition_key, "")
    matches = []
    now_utc = datetime.now(tz=timezone.utc)
    today_str = now_utc.strftime("%d.%m")

    # La page liste les matchs sous forme de blocs texte
    # On cherche les blocs qui contiennent le nom de la compétition
    content = soup.get_text(separator="\n")
    lines   = content.split("\n")
    lines   = [l.strip() for l in lines if l.strip()]

    i = 0
    while i < len(lines):
        line = lines[i]

        # Cherche une ligne de date/heure comme "05.04 08:25"
        time_match = re.match(r'(\d{2}\.\d{2})\s+(\d{2}:\d{2})', line)
        if time_match:
            match_date = time_match.group(1)  # "05.04"
            match_time = time_match.group(2)  # "08:25"

            # Vérifie que c'est aujourd'hui
            if match_date != today_str:
                i += 1
                continue

            # Cherche le nom de la compétition dans les lignes suivantes
            comp_found = False
            w1 = w2 = 0.0
            p1 = p2 = ""
            h2h_score = ""

            j = i + 1
            while j < min(i + 30, len(lines)):
                l = lines[j]

                if section_name and section_name.lower() in l.lower():
                    comp_found = True

                if "W1:" in l:
                    w1 = parse_odds(l)
                if "W2:" in l:
                    w2 = parse_odds(l)

                # H2H score comme "5 : 0" ou "1 : 4"
                h2h_m = re.match(r'^(\d+)\s*:\s*(\d+)$', l)
                if h2h_m and not h2h_score:
                    h2h_score = l

                # Noms joueurs — lignes après les cotes qui ne sont pas des nombres
                # Les noms apparaissent après les cotes W1/W2
                if w1 > 0 and w2 > 0 and not p1:
                    # Ligne joueur 1 : nom propre (pas de chiffres dominants)
                    if re.search(r'[A-Za-zÀ-ÿ]{3,}', l) and not re.match(r'^[\d\.\s:]+$', l) and "W" not in l and "Total" not in l and "Handi" not in l and "Ind." not in l:
                        p1 = l.strip()
                elif p1 and not p2:
                    if re.search(r'[A-Za-zÀ-ÿ]{3,}', l) and not re.match(r'^[\d\.\s:]+$', l) and "W" not in l and "Total" not in l and "Handi" not in l and "Ind." not in l and "score" not in l.lower():
                        p2 = l.strip()

                # Fin du bloc (nouvelle date/heure)
                if re.match(r'\d{2}\.\d{2}\s+\d{2}:\d{2}', l) and j > i + 1:
                    break

                j += 1

            if comp_found and p1 and p2 and w1 > 0 and w2 > 0:
                # H2H depuis le score affiché "X : Y"
                h2h_p1 = h2h_p2 = 0
                if h2h_score:
                    parts = h2h_score.split(":")
                    if len(parts) == 2:
                        try:
                            h2h_p1 = int(parts[0].strip())
                            h2h_p2 = int(parts[1].strip())
                        except:
                            pass

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
                log.debug(f"Match trouvé: {p1} vs {p2} | {w1}/{w2} | H2H {h2h_p1}:{h2h_p2}")

        i += 1

    log.info(f"[{competition_key}] {len(matches)} matchs à venir")
    return matches

def parse_recent_form(player_url: str) -> dict:
    """
    Scrape la page d'un joueur pour récupérer sa forme récente.
    Retourne wins, losses, last_match_won, won_after_losing_set, wins_excluding_last.
    """
    empty = {
        "wins": 0, "losses": 0, "last5": "?????",
        "last_match_won": False, "won_after_losing_set": False,
        "wins_excluding_last": 0, "matches_today": 0,
    }
    if not player_url:
        return empty

    soup = fetch_page(player_url)
    if not soup:
        return empty

    try:
        results = []
        today   = date.today().strftime("%d.%m")
        matches_today = 0
        won_after_losing_set = False

        # Cherche les lignes de résultats dans le tableau
        rows = soup.select("table tr")
        for row in rows[:8]:
            cols = row.find_all("td")
            if len(cols) < 3:
                continue

            date_col   = cols[0].get_text(strip=True) if cols else ""
            score_col  = cols[1].get_text(strip=True) if len(cols) > 1 else ""
            sets_text  = " ".join(c.get_text(strip=True) for c in cols[2:])

            if not re.match(r'\d{2}\.\d{2}', date_col):
                continue

            if date_col.startswith(today):
                matches_today += 1

            # Score global ex: "3:1" ou "1:3"
            score_m = re.match(r'^(\d+)\s*:\s*(\d+)$', score_col)
            if not score_m:
                continue

            s1, s2 = int(score_m.group(1)), int(score_m.group(2))
            won    = s1 > s2
            results.append("V" if won else "D")

            # Résilience : a-t-il remporté en ayant perdu un set ?
            if won:
                sets_lost = sets_text.count("loss") or (s2 >= 1)
                if sets_lost:
                    won_after_losing_set = True

        last5  = results[:5]
        wins   = last5.count("V")
        losses = last5.count("D")

        return {
            "wins":                wins,
            "losses":              losses,
            "last5":               "".join(last5),
            "last_match_won":      results[0] == "V" if results else False,
            "won_after_losing_set": won_after_losing_set,
            "wins_excluding_last": last5[1:].count("V") if len(last5) > 1 else 0,
            "matches_today":       matches_today,
        }
    except Exception as e:
        log.debug(f"Form parse error [{player_url}]: {e}")
        return empty

# ─── Logique d'analyse (Axel) ─────────────────────────────────────────────────

def analyze_match_logic(match: dict) -> dict | None:
    """
    Analyse structurelle indépendante des filtres de cotes.
    Utilise le H2H déjà dans le match + forme récente si disponible.
    """
    h2h    = match.get("h2h", {"p1_wins": 0, "p2_wins": 0, "total": 0})
    score_p1 = 50

    # ── PILIER A : Ascendant psychologique (H2H) ────────────────
    if h2h["total"] >= 1:
        win_rate_p1  = h2h["p1_wins"] / h2h["total"]
        score_p1    += int((win_rate_p1 - 0.5) * 60)  # -30 à +30

    # ── PILIERS B/C/D/E : Forme (simplifiée sans URL joueur) ────
    # score-tennis.com affiche le H2H directement sur la page up-games
    # La forme détaillée nécessite de visiter la page joueur (coûteux)
    # On l'active uniquement si le H2H est insuffisant

    score_p1 = max(0, min(score_p1, 100))

    if score_p1 >= 75:
        return {"bet_on": "player1", "name": match["player1"], "confidence": score_p1}
    elif score_p1 <= 25:
        return {"bet_on": "player2", "name": match["player2"], "confidence": 100 - score_p1}

    return None

# ─── Traitement alerte paris (Section A) ──────────────────────────────────────

def process_alert(match: dict):
    mid = match["id"]
    if mid in alerted_set1:
        return

    odds = match.get("odds", [])
    if len(odds) < 2:
        return

    o1, o2 = odds[0], odds[1]

    # ── Option 3 : match avec favori requis ─────────────────────
    if REQUIRE_FAVORITE and min(o1, o2) > MAX_FAVORITE_ODDS:
        log.debug(f"Pas de favori clair: {match['player1']} ({o1}) vs {match['player2']} ({o2})")
        return

    # ── Analyse structurelle ─────────────────────────────────────
    verdict = analyze_match_logic(match)
    if not verdict:
        log.debug(f"Pas de signal structurel: {match['player1']} vs {match['player2']}")
        return

    # Cote du joueur désigné
    if verdict["bet_on"] == "player1":
        fav_name, und_name = match["player1"], match["player2"]
        fav_odds = o1
    else:
        fav_name, und_name = match["player2"], match["player1"]
        fav_odds = o2

    # ── Option 1 : filtre fenêtre de cotes ──────────────────────
    odds_in_window = MIN_FAVORITE_ODDS <= fav_odds <= MAX_FAVORITE_ODDS
    if not IGNORE_ODDS_FILTER and not odds_in_window:
        log.debug(f"Cote {fav_odds} hors fenêtre pour {fav_name}")
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
    log.info(f"✅ Alerte set1: {fav_name} vs {und_name} | cote {fav_odds}")

# ─── Surveillance live (Section A) ────────────────────────────────────────────

def check_live_and_results():
    """
    Scrape score-tennis.com/live_v2/ pour détecter set 1 perdu
    et score-tennis.com/games/ pour enregistrer les résultats finaux.
    """
    if not live_tracking:
        return

    bilan = load_bilan()
    changed = False

    # ── Résultats finaux ─────────────────────────────────────────
    soup = fetch_page(f"{BASE_URL}/games/")
    if soup:
        content = soup.get_text(separator="\n")
        for mid, a in list(live_tracking.items()):
            if mid in results_seen:
                continue
            fav = a["favorite"].split()[0].lower()  # premier mot du nom
            if fav in content.lower():
                # Chercher le score final dans le contexte
                idx = content.lower().find(fav)
                snippet = content[idx:idx+200]
                score_m = re.search(r'(\d+)\s*:\s*(\d+)', snippet)
                if score_m:
                    s1, s2 = int(score_m.group(1)), int(score_m.group(2))
                    # Déduire si le favori a gagné (approximation)
                    fav_is_p1 = a["favorite"] == a["player1"]
                    fav_won_set1 = (s1 > s2) if fav_is_p1 else (s2 > s1)

                    if mid in alerted_set1 and mid not in alerted_set2:
                        if fav_won_set1:
                            bilan["set1"]["won"] += 1
                            bilan["week"]["set1"]["won"] += 1
                        else:
                            bilan["set1"]["lost"] += 1
                            bilan["week"]["set1"]["lost"] += 1
                        results_seen.add(mid)
                        changed = True

                    if mid in alerted_set2:
                        if fav_won_set1:
                            bilan["set2"]["won"] += 1
                            bilan["week"]["set2"]["won"] += 1
                        else:
                            bilan["set2"]["lost"] += 1
                            bilan["week"]["set2"]["lost"] += 1
                        results_seen.add(mid)
                        changed = True

    # ── Live : set 1 perdu ? ─────────────────────────────────────
    if not DISABLE_SET2_RECOVERY:
        soup_live = fetch_page(f"{BASE_URL}/live_v2/")
        if soup_live:
            content_live = soup_live.get_text(separator="\n")
            for mid, a in live_tracking.items():
                if mid in alerted_set2:
                    continue
                fav = a["favorite"].split()[0].lower()
                if fav in content_live.lower():
                    idx = content_live.lower().find(fav)
                    snippet = content_live[idx:idx+150]
                    # Cherche un score de sets comme "0 : 1" indiquant set 1 perdu
                    set_score = re.search(r'(\d+)\s*:\s*(\d+)', snippet)
                    if set_score:
                        ss1, ss2 = int(set_score.group(1)), int(set_score.group(2))
                        fav_is_p1 = a["favorite"] == a["player1"]
                        fav_lost_set1 = (ss1 < ss2) if fav_is_p1 else (ss2 < ss1)
                        if fav_lost_set1 and (ss1 + ss2) == 1:
                            send_telegram(format_set2_alert(a), ALERT_DESTINATIONS)
                            alerted_set2.add(mid)
                            log.info(f"⚠️ Alerte set2: {a['favorite']}")

    if changed:
        save_bilan(bilan)

# ─── Section B : Récap toutes les 2h ─────────────────────────────────────────

def send_recap(all_matches: list):
    now     = datetime.now(tz=timezone.utc)
    horizon = now + timedelta(hours=RECAP_WINDOW_HOURS)
    grouped = {}

    for match in all_matches:
        # Convertir l'heure du match
        try:
            t = datetime.strptime(match["time"], "%H:%M").replace(
                year=now.year, month=now.month, day=now.day, tzinfo=timezone.utc
            )
        except:
            continue

        if not (now <= t <= horizon):
            continue

        odds = match.get("odds", [])
        if len(odds) < 2:
            continue

        o1, o2 = odds[0], odds[1]

        # Filtre favori
        if REQUIRE_FAVORITE and min(o1, o2) > MAX_FAVORITE_ODDS:
            continue

        # Identifier le favori
        if o1 <= o2:
            fav_name, fav_odds = match["player1"], o1
        else:
            fav_name, fav_odds = match["player2"], o2

        # Filtre cotes si actif
        if not IGNORE_ODDS_FILTER and not (MIN_FAVORITE_ODDS <= fav_odds <= MAX_FAVORITE_ODDS):
            continue

        label = COMP_LABELS.get(match["competition"], match["competition"])
        grouped.setdefault(label, []).append(
            f"  🕐 {match['time']} — {match['player1']} vs {match['player2']} → <b>{fav_name}</b> ({fav_odds})"
        )

    if not grouped:
        log.info("Récap : aucun match filtré dans les 2h à venir")
        return

    total = sum(len(v) for v in grouped.values())
    msg   = f"📋 <b>MATCHS À VENIR — {now.strftime('%H:%M')} UTC</b>\n━━━━━━━━━━━━━━━━━━━━\n"
    for label, lines in grouped.items():
        msg += f"\n🏆 <b>{label}</b>\n" + "\n".join(lines) + "\n"
    msg += f"\n━━━━━━━━━━━━━━━━━━━━\n{total} opportunité(s) dans les {RECAP_WINDOW_HOURS}h à venir"

    send_telegram(msg, RECAP_DESTINATIONS)
    log.info(f"📋 Récap envoyé : {total} matchs")

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
        now  = datetime.now(tz=timezone.utc)
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
        f"💰 Cote : <b>{a['fav_odds']}</b>"
    )

def format_set2_alert(a: dict) -> str:
    comp = COMP_LABELS.get(a["competition"], a["competition"])
    return (
        f"⚠️ <b>SET 1 PERDU — {comp}</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"⚔️  {a['player1']} vs {a['player2']}\n\n"
        f"🔄 <b>OPTION : {SET2_ALERT_LABEL} — {a['favorite']}</b>\n"
        f"💰 Cote : vérifier sur 1xbet\n\n"
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
        f"• Alertes toutes les {CHECK_INTERVAL_MINUTES} min",
        f"• Récap toutes les {RECAP_INTERVAL_HOURS}h",
        f"• Source : score-tennis.com",
    ]
    send_telegram(
        "🤖 <b>Bot Setka Cup démarré</b>\n━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(opts),
        ALERT_DESTINATIONS
    )

    threading.Thread(target=bilan_thread, daemon=True).start()
    last_recap = datetime.now(tz=timezone.utc) - timedelta(hours=RECAP_INTERVAL_HOURS)

    while True:
        try:
            all_matches = []

            for comp in COMPETITIONS:
                matches = parse_upcoming_matches(comp)
                all_matches.extend(matches)

                # Section A : alertes paris
                for match in matches:
                    process_alert(match)

            # Section A : check live + résultats
            check_live_and_results()

            # Section B : récap toutes les 2h
            now = datetime.now(tz=timezone.utc)
            if (now - last_recap).total_seconds() >= RECAP_INTERVAL_HOURS * 3600:
                send_recap(all_matches)
                last_recap = now

        except Exception as e:
            log.error(f"Erreur boucle: {e}")

        log.info(f"⏳ Pause {CHECK_INTERVAL_MINUTES} min...")
        time.sleep(CHECK_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    run()
