#!/usr/bin/env python3
"""
Setka Cup Betting Bot
Analyse les matchs à venir, détecte les favoris et envoie des alertes Telegram.
Envoi simultané vers toutes les destinations configurées (privé + canal).
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

# ─── Mémoire des alertes déjà envoyées ───────────────────────────────────────
alerted_set1  = set()
alerted_set2  = set()
live_tracking = {}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}

# ─── Telegram ─────────────────────────────────────────────────────────────────

def send_telegram(message: str):
    """Envoie le message à TOUTES les destinations configurées simultanément."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for dest in TELEGRAM_DESTINATIONS:
        payload = {
            "chat_id": dest,
            "text": message,
            "parse_mode": "HTML"
        }
        try:
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code == 200:
                log.info(f"✅ Message envoyé → {dest}")
            else:
                log.warning(f"Telegram error [{dest}]: {r.status_code} {r.text}")
        except Exception as e:
            log.error(f"Telegram exception [{dest}]: {e}")


# ─── Scraping matchs à venir ──────────────────────────────────────────────────

def fetch_upcoming_matches(competition_key: str) -> list:
    urls = {
        "setka_cup_cz":      "https://www.betexplorer.com/table-tennis/czech-republic/setka-cup/",
        "setka_cup_ukraine": "https://www.betexplorer.com/table-tennis/ukraine/setka-cup/",
        "setka_cup_intl":    "https://www.betexplorer.com/table-tennis/world/setka-cup/",
        "liga_pro_russia":   "https://www.betexplorer.com/table-tennis/russia/liga-pro/",
        "tt_star_series":    "https://www.betexplorer.com/table-tennis/world/tt-star-series/",
        "pro_league_cz":     "https://www.betexplorer.com/table-tennis/czech-republic/pro-league/",
    }
    url = urls.get(competition_key)
    if not url:
        log.warning(f"Compétition inconnue: {competition_key}")
        return []

    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        matches = []

        rows = soup.select("tr.in-match, tr[data-eventid]")
        for row in rows:
            try:
                event_id = row.get("data-eventid", "")
                cols = row.find_all("td")
                if len(cols) < 3:
                    continue

                participants = row.select(".in-match__name, .table-main__participants")
                if participants:
                    names = participants[0].get_text(separator=" - ").split(" - ")
                else:
                    name_col = cols[0].get_text(strip=True)
                    names = name_col.split(" - ") if " - " in name_col else name_col.split(" v ")

                if len(names) < 2:
                    continue

                player1 = names[0].strip()
                player2 = names[1].strip()

                time_col = row.select_one(".table-main__time, .in-match__time")
                match_time = time_col.get_text(strip=True) if time_col else "?"

                odd_cells = row.select("td.table-main__odds, td[data-odd]")
                odds = []
                for oc in odd_cells[:2]:
                    try:
                        odds.append(float(oc.get("data-odd") or oc.get_text(strip=True)))
                    except:
                        pass

                if len(odds) < 2:
                    spans = row.select("span.table-main__odds")
                    for s in spans[:2]:
                        try:
                            odds.append(float(s.get_text(strip=True)))
                        except:
                            pass

                matches.append({
                    "id": event_id or f"{player1}_{player2}_{match_time}",
                    "player1": player1,
                    "player2": player2,
                    "time": match_time,
                    "competition": competition_key,
                    "odds": odds
                })

            except Exception as e:
                log.debug(f"Row parse error: {e}")
                continue

        log.info(f"[{competition_key}] {len(matches)} matchs trouvés")
        return matches

    except Exception as e:
        log.error(f"Fetch error [{competition_key}]: {e}")
        return []


# ─── H2H ─────────────────────────────────────────────────────────────────────

def fetch_h2h(player1: str, player2: str) -> dict:
    try:
        search_url = (
            f"https://www.betexplorer.com/results/table-tennis/?stage=1"
            f"&player1={requests.utils.quote(player1)}"
            f"&player2={requests.utils.quote(player2)}"
        )
        r = requests.get(search_url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        p1_wins = p2_wins = 0

        for row in soup.select("tr[data-eventid]"):
            result_col = row.select_one(".table-main__result")
            if not result_col:
                continue
            parts = result_col.get_text(strip=True).split(":")
            if len(parts) == 2:
                try:
                    s1, s2 = int(parts[0]), int(parts[1])
                    if s1 > s2:
                        p1_wins += 1
                    else:
                        p2_wins += 1
                except:
                    pass

        return {"p1_wins": p1_wins, "p2_wins": p2_wins, "total": p1_wins + p2_wins}

    except Exception as e:
        log.debug(f"H2H error: {e}")
        return {"p1_wins": 0, "p2_wins": 0, "total": 0}


# ─── Forme récente ────────────────────────────────────────────────────────────

def fetch_recent_form(player: str) -> dict:
    try:
        url = f"https://www.betexplorer.com/results/table-tennis/?player={requests.utils.quote(player)}"
        r = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        results = []

        for row in soup.select("tr[data-eventid]")[:5]:
            name_col   = row.select_one(".table-main__participants")
            result_col = row.select_one(".table-main__result")
            if not name_col or not result_col:
                continue
            names_text = name_col.get_text(separator="|")
            parts = result_col.get_text(strip=True).split(":")
            if len(parts) == 2:
                try:
                    s1, s2 = int(parts[0]), int(parts[1])
                    is_p1  = player.lower() in names_text.split("|")[0].lower()
                    won    = (s1 > s2) if is_p1 else (s2 > s1)
                    results.append("V" if won else "D")
                except:
                    pass

        wins   = results.count("V")
        losses = results.count("D")
        return {"wins": wins, "losses": losses, "last5": "".join(results)}

    except Exception as e:
        log.debug(f"Form error for {player}: {e}")
        return {"wins": 0, "losses": 0, "last5": "?????"}


# ─── Analyse & détection favori ──────────────────────────────────────────────

def analyze_match(match: dict) -> dict | None:
    p1   = match["player1"]
    p2   = match["player2"]
    odds = match.get("odds", [])

    if len(odds) < 2:
        log.debug(f"Pas de cotes pour {p1} vs {p2}")
        return None

    o1, o2 = odds[0], odds[1]

    # ── Option 3 : vérifier qu'il existe un favori clair ────────
    # C'est le premier filtre — avant toute analyse coûteuse
    has_favorite = abs(o1 - o2) >= 0.05
    if REQUIRE_FAVORITE and not has_favorite:
        log.debug(f"Match sans favori clair ({p1} {o1} vs {p2} {o2}) — ignoré")
        return None

    # Identifier le favori
    if o1 < o2:
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
        log.debug(f"{favorite} cote {fav_odds} hors fenêtre [{MIN_FAVORITE_ODDS}-{MAX_FAVORITE_ODDS}]")
        return None

    # ── Analyse H2H (seulement si passé les filtres rapides) ────
    if fav_label == "player1":
        h2h = fetch_h2h(favorite, underdog)
        fav_h2h_wins = h2h["p1_wins"]
    else:
        h2h = fetch_h2h(underdog, favorite)
        fav_h2h_wins = h2h["p2_wins"]

    total_h2h = h2h["total"]

    # ── Forme récente ────────────────────────────────────────────
    form = fetch_recent_form(favorite)

    # ── Calcul confiance ─────────────────────────────────────────
    confidence = 50
    if total_h2h >= MIN_H2H_MATCHES:
        win_rate = fav_h2h_wins / total_h2h
        if win_rate >= MIN_WIN_RATE:
            confidence += int(win_rate * 30)
    if form["wins"] >= 3:
        confidence += form["wins"] * 4
    if form["losses"] >= 3:
        confidence -= form["losses"] * 4
    confidence = min(confidence, 95)

    if confidence < 55:
        log.debug(f"Confiance {confidence}% trop basse pour {favorite}")
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


# ─── Formats des messages ─────────────────────────────────────────────────────

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
    odds_note = ""
    if IGNORE_ODDS_FILTER and not a["odds_confirmed"]:
        odds_note = "\n⚠️ <i>Mode analyse : cote hors fenêtre normale</i>"

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


# ─── Surveillance live set 1 ──────────────────────────────────────────────────

def check_live_set1_lost(match_id: str, favorite: str) -> bool:
    try:
        url  = "https://scores24.live/en/table-tennis"
        r    = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        fav_lower = favorite.lower()

        for row in soup.find_all(string=lambda t: fav_lower in t.lower() if t else False):
            parent   = row.parent
            score_el = parent.find_next(class_=lambda c: c and "score" in c.lower())
            if score_el:
                score_text = score_el.get_text(strip=True)
                if "0:1" in score_text or "0-1" in score_text:
                    return True
        return False

    except Exception as e:
        log.debug(f"Live check error: {e}")
        return False


# ─── Boucle principale ───────────────────────────────────────────────────────

def run():
    log.info("🚀 Bot démarré")

    opts = [
        f"• Exiger un favori clair : {'✅ Actif' if REQUIRE_FAVORITE else '❌ Désactivé (mode analyse)'}",
        f"• Filtre fenêtre de cotes : {'❌ Désactivé (mode analyse)' if IGNORE_ODDS_FILTER else '✅ Actif'}",
        f"• Récupération set 2 : {'❌ Désactivée (mode analyse)' if DISABLE_SET2_RECOVERY else '✅ Active'}",
        f"• Compétitions : {', '.join(COMPETITIONS)}",
        f"• Scan toutes les {CHECK_INTERVAL_MINUTES} min",
    ]

    send_telegram(
        "🤖 <b>Bot Setka Cup démarré</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        + "\n".join(opts)
    )

    while True:
        try:
            for comp in COMPETITIONS:
                log.info(f"🔍 Scan: {comp}")
                matches = fetch_upcoming_matches(comp)

                for match in matches:
                    mid = match["id"]

                    # ── Alerte set 1 ─────────────────────────────
                    if mid not in alerted_set1:
                        analysis = analyze_match(match)
                        if analysis:
                            send_telegram(format_set1_alert(analysis))
                            alerted_set1.add(mid)
                            live_tracking[mid] = analysis
                            log.info(f"✅ Alerte set1: {analysis['favorite']}")

                    # ── Alerte set 2 (si option active) ──────────
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
                            log.info(f"⚠️ Alerte set2: {a['favorite']} a perdu set1")

        except Exception as e:
            log.error(f"Erreur boucle principale: {e}")

        log.info(f"⏳ Pause {CHECK_INTERVAL_MINUTES} min...")
        time.sleep(CHECK_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    run()
