#!/usr/bin/env python3
"""
Setka Cup Betting Bot
Source de données : flashscore (scraping public) + sofascore API publique
"""

import time
import logging
import requests
from bs4 import BeautifulSoup
from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_DESTINATIONS,
    COMPETITIONS, MIN_FAVORITE_ODDS, MAX_FAVORITE_ODDS,
    MIN_H2H_MATCHES, MIN_WIN_RATE, CHECK_INTERVAL_MINUTES,
    SET1_ALERT_LABEL, SET2_ALERT_LABEL,
    IGNORE_ODDS_FILTER, DISABLE_SET2_RECOVERY, REQUIRE_FAVORITE
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

alerted_set1  = set()
alerted_set2  = set()
live_tracking = {}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.oddsportal.com/",
}

# Mapping compétition → ID Sofascore (API publique gratuite)
SOFASCORE_TOURNAMENT_IDS = {
    "setka_cup_cz":      {"id": 2388525, "name": "Setka Cup CZ"},
    "setka_cup_ukraine": {"id": 2388526, "name": "Setka Cup Ukraine"},
    "setka_cup_intl":    {"id": 1733171, "name": "Setka Cup International"},
    "liga_pro_russia":   {"id": 2388527, "name": "Liga Pro Russia"},
    "tt_star_series":    {"id": 2388528, "name": "TT Star Series"},
    "pro_league_cz":     {"id": 2095165, "name": "Pro League CZ"},
}

SOFASCORE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://www.sofascore.com/",
    "Origin": "https://www.sofascore.com",
}

# ─── Telegram ─────────────────────────────────────────────────────────────────

def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for dest in TELEGRAM_DESTINATIONS:
        payload = {"chat_id": dest, "text": message, "parse_mode": "HTML"}
        try:
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code == 200:
                log.info(f"✅ Envoyé → {dest}")
            else:
                log.warning(f"Telegram [{dest}]: {r.status_code} {r.text[:100]}")
        except Exception as e:
            log.error(f"Telegram exception [{dest}]: {e}")


# ─── Scraping matchs via Sofascore API publique ───────────────────────────────

def fetch_upcoming_matches(competition_key: str) -> list:
    """Récupère les matchs à venir via l'API publique Sofascore."""
    info = SOFASCORE_TOURNAMENT_IDS.get(competition_key)
    if not info:
        log.warning(f"Compétition inconnue: {competition_key}")
        return []

    tournament_id = info["id"]
    matches = []

    try:
        # Sofascore API publique — événements du jour
        url = f"https://api.sofascore.com/api/v1/sport/table-tennis/scheduled-events/today"
        r = requests.get(url, headers=SOFASCORE_HEADERS, timeout=15)

        if r.status_code != 200:
            log.warning(f"Sofascore today events: {r.status_code}")
            return fetch_upcoming_matches_fallback(competition_key)

        data = r.json()
        events = data.get("events", [])

        for event in events:
            try:
                # Filtrer par tournoi
                t_id = event.get("tournament", {}).get("uniqueTournament", {}).get("id")
                if t_id != tournament_id:
                    continue

                # Statut — on veut uniquement les matchs pas encore joués
                status = event.get("status", {}).get("type", "")
                if status in ["finished", "inprogress"]:
                    continue

                home = event.get("homeTeam", {}).get("name", "?")
                away = event.get("awayTeam", {}).get("name", "?")
                event_id = str(event.get("id", f"{home}_{away}"))

                # Heure
                start_ts = event.get("startTimestamp", 0)
                from datetime import datetime, timezone
                match_time = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%H:%M") if start_ts else "?"

                # Cotes via Sofascore odds endpoint
                odds = fetch_sofascore_odds(event_id)

                matches.append({
                    "id": event_id,
                    "player1": home,
                    "player2": away,
                    "time": match_time,
                    "competition": competition_key,
                    "odds": odds
                })

            except Exception as e:
                log.debug(f"Event parse error: {e}")
                continue

        log.info(f"[{competition_key}] {len(matches)} matchs à venir")
        return matches

    except Exception as e:
        log.error(f"Sofascore error [{competition_key}]: {e}")
        return fetch_upcoming_matches_fallback(competition_key)


def fetch_sofascore_odds(event_id: str) -> list:
    """Récupère les cotes match winner depuis Sofascore."""
    try:
        url = f"https://api.sofascore.com/api/v1/event/{event_id}/odds/1/all"
        r = requests.get(url, headers=SOFASCORE_HEADERS, timeout=10)
        if r.status_code != 200:
            return []
        data = r.json()
        markets = data.get("markets", [])
        for market in markets:
            if market.get("marketName") in ["Full time", "Match winner", "Winner"]:
                choices = market.get("choices", [])
                o1 = o2 = None
                for c in choices:
                    if c.get("name") in ["1", "Home"]:
                        o1 = float(c.get("fractionalValue") or c.get("decimalValue") or 0)
                    elif c.get("name") in ["2", "Away"]:
                        o2 = float(c.get("fractionalValue") or c.get("decimalValue") or 0)
                if o1 and o2:
                    return [o1, o2]
        return []
    except Exception as e:
        log.debug(f"Odds error event {event_id}: {e}")
        return []


def fetch_upcoming_matches_fallback(competition_key: str) -> list:
    """Fallback : scraping oddsportal si Sofascore échoue."""
    fallback_urls = {
        "setka_cup_cz":      "https://www.oddsportal.com/table-tennis/czech-republic/setka-cup/",
        "setka_cup_ukraine": "https://www.oddsportal.com/table-tennis/ukraine/setka-cup/",
        "setka_cup_intl":    "https://www.oddsportal.com/table-tennis/world/setka-cup/",
        "liga_pro_russia":   "https://www.oddsportal.com/table-tennis/russia/liga-pro/",
        "tt_star_series":    "https://www.oddsportal.com/table-tennis/world/tt-star-series/",
        "pro_league_cz":     "https://www.oddsportal.com/table-tennis/czech-republic/pro-league/",
    }
    url = fallback_urls.get(competition_key)
    if not url:
        return []

    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        matches = []

        for row in soup.select("div[data-event-id], tr[data-event-id]"):
            try:
                event_id = row.get("data-event-id", "")
                players  = row.select(".participant-name, .table-participant")
                if len(players) < 2:
                    continue
                p1 = players[0].get_text(strip=True)
                p2 = players[1].get_text(strip=True)

                odds_els = row.select("[data-odd], .odds-nowrp")
                odds = []
                for o in odds_els[:2]:
                    try:
                        odds.append(float(o.get("data-odd") or o.get_text(strip=True)))
                    except:
                        pass

                time_el = row.select_one(".table-time, [data-start-time]")
                match_time = time_el.get_text(strip=True) if time_el else "?"

                matches.append({
                    "id": event_id or f"{p1}_{p2}",
                    "player1": p1,
                    "player2": p2,
                    "time": match_time,
                    "competition": competition_key,
                    "odds": odds
                })
            except Exception as e:
                log.debug(f"Fallback row error: {e}")
                continue

        log.info(f"[{competition_key}] fallback: {len(matches)} matchs")
        return matches

    except Exception as e:
        log.error(f"Fallback error [{competition_key}]: {e}")
        return []


# ─── H2H via Sofascore ────────────────────────────────────────────────────────

def fetch_h2h_sofascore(event_id: str) -> dict:
    """H2H des deux joueurs via Sofascore."""
    try:
        url = f"https://api.sofascore.com/api/v1/event/{event_id}/h2h"
        r = requests.get(url, headers=SOFASCORE_HEADERS, timeout=10)
        if r.status_code != 200:
            return {"p1_wins": 0, "p2_wins": 0, "total": 0}

        data   = r.json()
        events = data.get("previousEvents", [])
        p1_wins = p2_wins = 0

        for e in events:
            winner_code = e.get("winnerCode")  # 1 = home wins, 2 = away wins
            if winner_code == 1:
                p1_wins += 1
            elif winner_code == 2:
                p2_wins += 1

        return {"p1_wins": p1_wins, "p2_wins": p2_wins, "total": p1_wins + p2_wins}

    except Exception as e:
        log.debug(f"H2H sofascore error: {e}")
        return {"p1_wins": 0, "p2_wins": 0, "total": 0}


# ─── Forme récente via Sofascore ─────────────────────────────────────────────

def fetch_recent_form_sofascore(team_id: str, is_home: bool) -> dict:
    """Forme récente d'un joueur via Sofascore."""
    try:
        url = f"https://api.sofascore.com/api/v1/team/{team_id}/events/last/0"
        r = requests.get(url, headers=SOFASCORE_HEADERS, timeout=10)
        if r.status_code != 200:
            return {"wins": 0, "losses": 0, "last5": "?????"}

        data   = r.json()
        events = data.get("events", [])[:5]
        results = []

        for e in events:
            home_id = e.get("homeTeam", {}).get("id")
            winner  = e.get("winnerCode")
            if str(home_id) == str(team_id):
                results.append("V" if winner == 1 else "D")
            else:
                results.append("V" if winner == 2 else "D")

        wins   = results.count("V")
        losses = results.count("D")
        return {"wins": wins, "losses": losses, "last5": "".join(results)}

    except Exception as e:
        log.debug(f"Form error team {team_id}: {e}")
        return {"wins": 0, "losses": 0, "last5": "?????"}


# ─── Analyse ─────────────────────────────────────────────────────────────────

def analyze_match(match: dict) -> dict | None:
    p1   = match["player1"]
    p2   = match["player2"]
    odds = match.get("odds", [])

    if len(odds) < 2:
        log.debug(f"Pas de cotes: {p1} vs {p2}")
        return None

    o1, o2 = odds[0], odds[1]

    # ── Option 3 : favori clair requis ──────────────────────────
    has_favorite = abs(o1 - o2) >= 0.05
    if REQUIRE_FAVORITE and not has_favorite:
        log.debug(f"Pas de favori clair ({p1} {o1} vs {p2} {o2})")
        return None

    # Identifier le favori
    if o1 <= o2:
        favorite, underdog = p1, p2
        fav_odds, und_odds = o1, o2
        fav_label = "player1"
    else:
        favorite, underdog = p2, p1
        fav_odds, und_odds = o2, o1
        fav_label = "player2"

    # ── Option 1 : filtre fenêtre de cotes ──────────────────────
    odds_confirmed = MIN_FAVORITE_ODDS <= fav_odds <= MAX_FAVORITE_ODDS
    if not IGNORE_ODDS_FILTER and not odds_confirmed:
        log.debug(f"{favorite} cote {fav_odds} hors fenêtre")
        return None

    # ── H2H ─────────────────────────────────────────────────────
    h2h = fetch_h2h_sofascore(match["id"])
    fav_h2h_wins = h2h["p1_wins"] if fav_label == "player1" else h2h["p2_wins"]
    total_h2h    = h2h["total"]

    # ── Forme récente ────────────────────────────────────────────
    # On passe un ID fictif basé sur le nom si pas d'ID Sofascore dispo
    form = fetch_recent_form_sofascore(match["id"], fav_label == "player1")

    # ── Calcul confiance ─────────────────────────────────────────
    confidence = 50
    if total_h2h >= MIN_H2H_MATCHES:
        win_rate = fav_h2h_wins / total_h2h
        if win_rate >= MIN_WIN_RATE:
            confidence += int(win_rate * 30)
    confidence += form["wins"] * 4
    confidence -= form["losses"] * 4
    confidence  = max(0, min(confidence, 95))

    if confidence < 55:
        log.debug(f"Confiance {confidence}% trop basse: {favorite}")
        return None

    return {
        "match_id":       match["id"],
        "player1":        p1,
        "player2":        p2,
        "favorite":       favorite,
        "underdog":       underdog,
        "fav_odds":       fav_odds,
        "und_odds":       und_odds,
        "odds_confirmed": odds_confirmed,
        "h2h_fav_wins":   fav_h2h_wins,
        "h2h_total":      total_h2h,
        "form_last5":     form["last5"],
        "form_wins":      form["wins"],
        "confidence":     confidence,
        "time":           match["time"],
        "competition":    match["competition"]
    }


# ─── Messages ────────────────────────────────────────────────────────────────

COMP_LABELS = {
    "setka_cup_cz":      "Setka Cup 🇨🇿",
    "setka_cup_ukraine": "Setka Cup 🇺🇦",
    "setka_cup_intl":    "Setka Cup 🌍",
    "liga_pro_russia":   "Liga Pro 🇷🇺",
    "tt_star_series":    "TT Star Series ⭐",
    "pro_league_cz":     "Pro League 🇨🇿",
}

def format_set1_alert(a: dict) -> str:
    comp    = COMP_LABELS.get(a["competition"], a["competition"])
    h2h_str = f"{a['h2h_fav_wins']}/{a['h2h_total']}" if a["h2h_total"] > 0 else "N/A"
    odds_note = "\n⚠️ <i>Mode analyse : cote hors fenêtre normale</i>" if (IGNORE_ODDS_FILTER and not a["odds_confirmed"]) else ""
    return (
        f"🏓 <b>{comp}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚔️  {a['player1']} vs {a['player2']}\n"
        f"🕐  {a['time']}\n\n"
        f"📊 <b>ANALYSE</b>\n"
        f"• Favori : <b>{a['favorite']}</b>\n"
        f"• H2H : {h2h_str} victoires\n"
        f"• Forme (5 derniers) : {a['form_last5']} ({a['form_wins']}V)\n"
        f"• Confiance : {a['confidence']}%\n"
        f"{odds_note}\n"
        f"✅ <b>PARI : {SET1_ALERT_LABEL} — {a['favorite']}</b>\n"
        f"💰 Cote : <b>{a['fav_odds']}</b>"
    )

def format_set2_alert(a: dict) -> str:
    comp = COMP_LABELS.get(a["competition"], a["competition"])
    return (
        f"⚠️ <b>SET 1 PERDU — {comp}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚔️  {a['player1']} vs {a['player2']}\n"
        f"Score : 0-1 pour {a['underdog']}\n\n"
        f"📊 Favori ({a['favorite']}) toujours solide\n"
        f"• Forme : {a['form_last5']}\n"
        f"• H2H global en sa faveur\n\n"
        f"🔄 <b>OPTION : {SET2_ALERT_LABEL} — {a['favorite']}</b>\n"
        f"💰 Cote : vérifier sur 1xbet\n\n"
        f"⚡ <i>Si perd encore → on laisse</i>"
    )


# ─── Live : set 1 perdu ? ────────────────────────────────────────────────────

def check_live_set1_lost(event_id: str, favorite: str) -> bool:
    try:
        url = f"https://api.sofascore.com/api/v1/event/{event_id}"
        r   = requests.get(url, headers=SOFASCORE_HEADERS, timeout=10)
        if r.status_code != 200:
            return False
        data  = r.json()
        event = data.get("event", {})

        # Statut en cours ?
        if event.get("status", {}).get("type") != "inprogress":
            return False

        # Score par période
        home_score = event.get("homeScore", {})
        away_score = event.get("awayScore", {})
        p1_set1 = home_score.get("period1", 0)
        p2_set1 = away_score.get("period1", 0)

        # Set 1 terminé ?
        if p1_set1 == 0 and p2_set1 == 0:
            return False

        home_name = event.get("homeTeam", {}).get("name", "")
        fav_is_home = favorite.lower() in home_name.lower()

        if fav_is_home:
            return p1_set1 < p2_set1
        else:
            return p2_set1 < p1_set1

    except Exception as e:
        log.debug(f"Live check error: {e}")
        return False


# ─── Boucle principale ───────────────────────────────────────────────────────

def run():
    log.info("🚀 Bot démarré")
    opts = [
        f"• Favori clair requis : {'✅' if REQUIRE_FAVORITE else '❌ désactivé'}",
        f"• Filtre cotes [{MIN_FAVORITE_ODDS}-{MAX_FAVORITE_ODDS}] : {'❌ désactivé' if IGNORE_ODDS_FILTER else '✅'}",
        f"• Récupération set 2 : {'❌ désactivée' if DISABLE_SET2_RECOVERY else '✅'}",
        f"• Compétitions : {', '.join(COMPETITIONS)}",
        f"• Scan toutes les {CHECK_INTERVAL_MINUTES} min",
    ]
    send_telegram("🤖 <b>Bot Setka Cup démarré</b>\n━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(opts))

    while True:
        try:
            for comp in COMPETITIONS:
                log.info(f"🔍 Scan: {comp}")
                matches = fetch_upcoming_matches(comp)

                for match in matches:
                    mid = match["id"]

                    if mid not in alerted_set1:
                        analysis = analyze_match(match)
                        if analysis:
                            send_telegram(format_set1_alert(analysis))
                            alerted_set1.add(mid)
                            live_tracking[mid] = analysis
                            log.info(f"✅ Alerte set1: {analysis['favorite']}")

                    elif (
                        not DISABLE_SET2_RECOVERY
                        and mid in live_tracking
                        and mid not in alerted_set2
                    ):
                        a    = live_tracking[mid]
                        lost = check_live_set1_lost(mid, a["favorite"])
                        if lost:
                            send_telegram(format_set2_alert(a))
                            alerted_set2.add(mid)
                            log.info(f"⚠️ Alerte set2: {a['favorite']}")

        except Exception as e:
            log.error(f"Erreur boucle: {e}")

        log.info(f"⏳ Pause {CHECK_INTERVAL_MINUTES} min...")
        time.sleep(CHECK_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    run()
