#!/usr/bin/env python3
"""
Setka Cup Betting Bot - Source: score-tennis.com
Signaux : H2H + Set1 dernier H2H + Forme + Fatigue
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
    IGNORE_ODDS_FILTER, REQUIRE_FAVORITE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 60

BILAN_FILE = Path("bilan.json")

def load_bilan() -> dict:
    if BILAN_FILE.exists():
        try:
            return json.loads(BILAN_FILE.read_text())
        except:
            pass
    return {
        "set1":  {"won": 0, "lost": 0},
        "match": {"won": 0, "lost": 0},
        "week":  {"set1": {"won": 0, "lost": 0}, "match": {"won": 0, "lost": 0}},
    }

def save_bilan(b: dict):
    BILAN_FILE.write_text(json.dumps(b, indent=2))

alerted  = set()
tracking = {}
seen     = set()

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

        # Noms depuis liens /players/
        player_links = block_soup.find_all(
            "a", href=re.compile(r"score-tennis\.com/players/|^/players/")
        )
        if len(player_links) < 2 or "face-to-face" not in block_text:
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

        # URLs joueurs pour forme + fatigue
        p1_url = player_links[0].get("href", "")
        p2_url = player_links[1].get("href", "")
        if not p1_url.startswith("http"):
            p1_url = f"{BASE_URL}{p1_url}"
        if not p2_url.startswith("http"):
            p2_url = f"{BASE_URL}{p2_url}"

        # URL page du match H2H (pour set 1 dernier H2H)
        # Elle apparaît dans les lignes du tableau historique
        match_links = block_soup.find_all("a", href=re.compile(r"/games/"))
        h2h_match_url = None
        for ml in match_links:
            href = ml.get("href", "")
            if href and p1.split()[0].lower() in href.lower():
                h2h_match_url = href if href.startswith("http") else f"{BASE_URL}{href}"
                break

        mid = f"{p1}_{p2}_{match_date}_{match_time}"
        matches.append({
            "id":            mid,
            "player1":       p1,
            "player2":       p2,
            "time":          match_time,
            "date":          match_date,
            "competition":   competition_key,
            "odds":          [w1, w2],
            "h2h":           {"p1_wins": h2h_p1, "p2_wins": h2h_p2, "total": h2h_p1 + h2h_p2},
            "p1_url":        p1_url,
            "p2_url":        p2_url,
            "h2h_match_url": h2h_match_url,
        })
        log.info(f"✅ {p1} vs {p2} | W1={w1} W2={w2} | H2H {h2h_p1}:{h2h_p2}")
        i += 2

    log.info(f"[{competition_key}] {len(matches)} matchs")
    return matches

# ─── Données joueur : forme + fatigue ────────────────────────────────────────

def get_player_data(player_url: str, today_str: str) -> dict:
    """
    Scrape la page joueur pour extraire :
    - Forme : V/D sur les matchs visibles
    - Matchs joués aujourd'hui
    """
    empty = {"wins": 0, "losses": 0, "form_str": "?", "matches_today": 0}
    if not player_url:
        return empty

    soup = fetch_page(player_url)
    if not soup:
        return empty

    try:
        rows = soup.select("table tr")
        wins = losses = matches_today = 0
        form_chars = []

        for row in rows:
            cols = [c.get_text(strip=True) for c in row.find_all("td")]
            if len(cols) < 2:
                continue

            date_col  = cols[0]
            score_col = cols[1]

            if not re.match(r'\d{2}\.\d{2}', date_col):
                continue

            # Compter matchs aujourd'hui
            row_date = date_col[:5]  # "DD.MM"
            if row_date == today_str:
                matches_today += 1

            # Score global visible (pas BASE)
            if score_col and score_col != "BASE":
                score_m = re.match(r'(\d+)\s*:\s*(\d+)', score_col)
                if score_m:
                    s1, s2 = int(score_m.group(1)), int(score_m.group(2))
                    if s1 > s2:
                        wins += 1
                        form_chars.append("V")
                    else:
                        losses += 1
                        form_chars.append("D")

        form_str = "".join(form_chars[:5]) if form_chars else "?"
        return {
            "wins":          wins,
            "losses":        losses,
            "form_str":      form_str,
            "matches_today": matches_today,
        }
    except Exception as e:
        log.debug(f"Player data error [{player_url}]: {e}")
        return empty

# ─── Set 1 du dernier H2H ────────────────────────────────────────────────────

def get_last_h2h_set1(match_url: str, p1_name: str) -> str | None:
    """
    Scrape la page du dernier match H2H pour extraire le score du set 1.
    Retourne "p1" si P1 a gagné le set 1, "p2" sinon, None si indispo.
    """
    if not match_url:
        return None

    soup = fetch_page(match_url)
    if not soup:
        return None

    try:
        # Chercher le score dans la page du match
        # Format : "3 : 1 (11:9 8:11 11:7 11:6)"
        text = soup.get_text()

        # Score complet avec sets entre parenthèses
        sets_m = re.search(r'\((\d+:\d+)', text)
        if sets_m:
            set1 = sets_m.group(1)
            parts = set1.split(":")
            if len(parts) == 2:
                s1, s2 = int(parts[0]), int(parts[1])
                # Déterminer si P1 (home) a gagné le set 1
                home_link = soup.find("a", href=re.compile(r"/players/"))
                if home_link:
                    home_name = " ".join(home_link.get_text().split())
                    p1_is_home = p1_name.split()[0].lower() in home_name.lower()
                    if p1_is_home:
                        return "p1" if s1 > s2 else "p2"
                    else:
                        return "p2" if s1 > s2 else "p1"
        return None
    except Exception as e:
        log.debug(f"Set1 H2H error: {e}")
        return None

# ─── Analyse principale ───────────────────────────────────────────────────────

def analyze_match(match: dict) -> dict | None:
    """
    Signal principal : H2H dominant
    Données contextuelles : set 1 dernier H2H, forme, fatigue
    Ces données enrichissent le message mais ne bloquent pas encore
    (phase d'observation pour calibrer les filtres)
    """
    h2h = match.get("h2h", {"p1_wins": 0, "p2_wins": 0, "total": 0})

    # ── Signal H2H ───────────────────────────────────────────────
    if h2h["total"] < 1:
        log.debug(f"Pas de H2H pour {match['player1']} vs {match['player2']}")
        return None

    win_rate_p1 = h2h["p1_wins"] / h2h["total"]

    if win_rate_p1 >= 0.60:
        bet_on    = "player1"
        fav_name  = match["player1"]
        und_name  = match["player2"]
        fav_is_p1 = True
        h2h_wins  = h2h["p1_wins"]
    elif win_rate_p1 <= 0.40:
        bet_on    = "player2"
        fav_name  = match["player2"]
        und_name  = match["player1"]
        fav_is_p1 = False
        h2h_wins  = h2h["p2_wins"]
    else:
        log.debug(f"H2H trop équilibré ({win_rate_p1:.0%}) pour {match['player1']} vs {match['player2']}")
        return None

    today_str = match["date"]

    # ── Données contextuelles ────────────────────────────────────
    fav_url = match["p1_url"] if fav_is_p1 else match["p2_url"]
    und_url = match["p2_url"] if fav_is_p1 else match["p1_url"]

    fav_data = get_player_data(fav_url, today_str)
    und_data = get_player_data(und_url, today_str)

    # Set 1 du dernier H2H
    set1_winner = get_last_h2h_set1(match.get("h2h_match_url"), match["player1"])
    if set1_winner:
        set1_fav_won = (set1_winner == "p1") if fav_is_p1 else (set1_winner == "p2")
    else:
        set1_fav_won = None

    # Type de pari : SET1 par défaut
    # Si set1_fav_won == False (a perdu le dernier set 1 H2H) → WIN MATCH
    if set1_fav_won is False:
        bet_type = "MATCH"
    else:
        bet_type = "SET1"

    log.info(f"Analyse: {fav_name} | H2H {h2h_wins}/{h2h['total']} | Set1={set1_fav_won} | Today={fav_data['matches_today']} matchs | Forme={fav_data['form_str']}")

    return {
        "bet_on":          bet_on,
        "bet_type":        bet_type,
        "favorite":        fav_name,
        "underdog":        und_name,
        "fav_is_p1":       fav_is_p1,
        "h2h_wins":        h2h_wins,
        "h2h_total":       h2h["total"],
        "set1_fav_won":    set1_fav_won,
        "fav_form":        fav_data["form_str"],
        "fav_today":       fav_data["matches_today"],
        "und_today":       und_data["matches_today"],
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

    # Option 3 : filtre d'entrée — match avec favori marqué ?
    if REQUIRE_FAVORITE and min(o1, o2) > MAX_FAVORITE_ODDS:
        log.debug(f"Pas de favori clair: {match['player1']} {o1} vs {match['player2']} {o2}")
        return

    # Analyse
    verdict = analyze_match(match)
    if not verdict:
        return

    # Cotes
    fav_odds = o1 if verdict["fav_is_p1"] else o2
    und_odds = o2 if verdict["fav_is_p1"] else o1

    # Option 1 : validation cote après analyse
    odds_in_window = MIN_FAVORITE_ODDS <= fav_odds <= MAX_FAVORITE_ODDS
    if not IGNORE_ODDS_FILTER and not odds_in_window:
        log.info(f"❌ Joueur désigné ({verdict['favorite']}) cote {fav_odds} hors fenêtre — divergence analyse/marché")
        return

    analysis = {
        "match_id":      mid,
        "player1":       match["player1"],
        "player2":       match["player2"],
        "favorite":      verdict["favorite"],
        "underdog":      verdict["underdog"],
        "fav_odds":      fav_odds,
        "und_odds":      und_odds,
        "bet_type":      verdict["bet_type"],
        "h2h_wins":      verdict["h2h_wins"],
        "h2h_total":     verdict["h2h_total"],
        "set1_fav_won":  verdict["set1_fav_won"],
        "fav_form":      verdict["fav_form"],
        "fav_today":     verdict["fav_today"],
        "und_today":     verdict["und_today"],
        "time":          match["time"],
        "competition":   match["competition"],
        "odds_in_window": odds_in_window,
    }

    send_telegram(format_alert(analysis), ALERT_DESTINATIONS)
    alerted.add(mid)
    tracking[mid] = analysis
    log.info(f"✅ Alerte: {verdict['favorite']} | {verdict['bet_type']} | H2H {verdict['h2h_wins']}/{verdict['h2h_total']}")

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

        # Score final : format "3:1" ou "1:3" etc.
        final_m = re.search(r'\b([0-4])\s*:\s*([0-4])\b', snippet)
        if not final_m:
            continue

        s1, s2 = int(final_m.group(1)), int(final_m.group(2))
        if s1 + s2 < 2 or s1 + s2 > 7:
            continue

        fav_won_match = (s1 > s2) if fav_is_p1 else (s2 > s1)

        # Score set 1 dans les parenthèses après le score global
        after = snippet[final_m.end():]
        set1_m = re.search(r'\(?\s*(\d{1,2})\s*:\s*(\d{1,2})', after)
        if set1_m:
            ss1, ss2   = int(set1_m.group(1)), int(set1_m.group(2))
            fav_won_s1 = (ss1 > ss2) if fav_is_p1 else (ss2 > ss1)
        else:
            fav_won_s1 = fav_won_match

        if a["bet_type"] == "SET1":
            key = "won" if fav_won_s1 else "lost"
            bilan["set1"][key] += 1
            bilan["week"]["set1"][key] += 1
            log.info(f"📊 SET1 {key}: {a['favorite']} (set1={ss1 if set1_m else '?'}:{ss2 if set1_m else '?'})")
        else:
            key = "won" if fav_won_match else "lost"
            bilan["match"][key] += 1
            bilan["week"]["match"][key] += 1
            log.info(f"📊 MATCH {key}: {a['favorite']} ({s1}:{s2})")

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
        f"🏓 WIN 1er SET : ✅ {s1['won']} / ❌ {s1['lost']}  ({pct(s1['won'], s1['lost'])})\n"
        f"🏆 WIN MATCH   : ✅ {ma['won']} / ❌ {ma['lost']}  ({pct(ma['won'], ma['lost'])})\n"
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
            bilan["week"] = {"set1": {"won": 0, "lost": 0}, "match": {"won": 0, "lost": 0}}
            save_bilan(bilan)
            last_weekly = week_str
            log.info("📆 Bilan hebdomadaire envoyé")

        time.sleep(60)

# ─── Format message ───────────────────────────────────────────────────────────

def format_alert(a: dict) -> str:
    comp = COMP_LABELS.get(a["competition"], a["competition"])

    # Cotes sur les noms
    if a["favorite"] == a["player1"]:
        p1_display = f"{a['player1']} ({a['fav_odds']})"
        p2_display = f"{a['player2']} ({a['und_odds']})"
    else:
        p1_display = f"{a['player1']} ({a['und_odds']})"
        p2_display = f"{a['player2']} ({a['fav_odds']})"

    # Type de pari
    pari_str = "WIN 1er SET" if a["bet_type"] == "SET1" else "WIN MATCH"

    # Set 1 dernier H2H
    if a["set1_fav_won"] is True:
        set1_str = "✅ gagné"
    elif a["set1_fav_won"] is False:
        set1_str = "❌ perdu"
    else:
        set1_str = "?"

    # Fatigue
    today_str = ""
    if a["fav_today"] >= 3:
        today_str = f" ⚠️ {a['fav_today']}e match aujourd'hui"
    elif a["fav_today"] >= 2:
        today_str = f" ({a['fav_today']}e match)"

    # Note si hors fenêtre
    odds_note = "\n⚠️ <i>Cote hors fenêtre — signal analyse seul</i>" if (IGNORE_ODDS_FILTER and not a["odds_in_window"]) else ""

    return (
        f"🏓 <b>{comp}</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"⚔️  {p1_display} vs {p2_display}\n"
        f"🕐  {a['time']} UTC{today_str}{odds_note}\n\n"
        f"✅ <b>PARI : {a['favorite']} — {pari_str}</b>\n\n"
        f"📊 H2H : {a['h2h_wins']}/{a['h2h_total']} matchs\n"
        f"📊 Set 1 dernier H2H : {set1_str}\n"
        f"📊 Forme : {a['fav_form']}"
    )

# ─── Boucle principale ───────────────────────────────────────────────────────

def run():
    log.info("🚀 Bot démarré")
    opts = [
        f"• Filtre favori (cote ≤ {MAX_FAVORITE_ODDS}) : {'✅' if REQUIRE_FAVORITE else '❌'}",
        f"• Validation cote après analyse : {'❌ désactivée' if IGNORE_ODDS_FILTER else '✅'}",
        f"• Compétitions : {', '.join(COMPETITIONS)}",
        f"• Scan toutes les {CHECK_INTERVAL_SECONDS}s",
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

        log.info(f"⏳ {CHECK_INTERVAL_SECONDS}s...")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
