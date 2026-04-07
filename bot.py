#!/usr/bin/env python3
"""
Setka Cup Betting Bot - Source: score-tennis.com
Signaux : H2H + Set1 dernier H2H + Fatigue
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
    "setka_cup_ukraine": "Setka Cup. Ukraine",
    "liga_pro_russia":   "Pro League. Russia",
    "pro_league_cz":     "Pro League. Czech Republic",
    "tt_cup_cz":         "TT-Cup. Czech Republic",
}

COMP_LABELS = {
    "setka_cup_cz":      "Setka Cup 🇨🇿",
    "setka_cup_ukraine": "Setka Cup 🇺🇦",
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

        # H2H global depuis "X : Y score of recent face-to-face"
        h2h_m  = re.search(r'(\d+)\s*:\s*(\d+)\s*\n?\s*score of recent face-to-face', block_text)
        h2h_p1 = h2h_p2 = 0
        if h2h_m:
            h2h_p1 = int(h2h_m.group(1))
            h2h_p2 = int(h2h_m.group(2))

        # URL du dernier match H2H — chercher lien /games/ dans le bloc
        # Les liens /games/ dans up-games/ pointent vers les derniers matchs H2H
        game_links = block_soup.find_all("a", href=re.compile(r"score-tennis\.com/games/|^/games/"))
        last_h2h_url = None
        for gl in game_links:
            href = gl.get("href", "")
            if not href:
                continue
            # Vérifier que le lien contient le nom d'un des joueurs (translittéré)
            href_lower = href.lower()
            p1_first = p1.split()[0].lower()
            p2_first = p2.split()[0].lower()
            if p1_first[:4] in href_lower or p2_first[:4] in href_lower:
                last_h2h_url = href if href.startswith("http") else f"{BASE_URL}{href}"
                break

        # URLs joueurs pour fatigue
        p1_href = player_links[0].get("href", "")
        p2_href = player_links[1].get("href", "")
        p1_url  = p1_href if p1_href.startswith("http") else f"{BASE_URL}{p1_href}"
        p2_url  = p2_href if p2_href.startswith("http") else f"{BASE_URL}{p2_href}"

        mid = f"{p1}_{p2}_{match_date}_{match_time}"
        matches.append({
            "id":           mid,
            "player1":      p1,
            "player2":      p2,
            "time":         match_time,
            "date":         match_date,
            "competition":  competition_key,
            "odds":         [w1, w2],
            "h2h":          {"p1_wins": h2h_p1, "p2_wins": h2h_p2, "total": h2h_p1 + h2h_p2},
            "last_h2h_url": last_h2h_url,
            "p1_url":       p1_url,
            "p2_url":       p2_url,
        })
        log.info(f"✅ {p1} vs {p2} | W1={w1} W2={w2} | H2H {h2h_p1}:{h2h_p2} | h2h_url={last_h2h_url}")
        i += 2

    log.info(f"[{competition_key}] {len(matches)} matchs")
    return matches

# ─── Données depuis page match H2H ───────────────────────────────────────────

def get_h2h_page_data(h2h_url: str, fav_is_p1: bool) -> dict:
    """
    Scrape la page du dernier match H2H pour extraire :
    - Score du set 1 (visible gratuitement dans le titre)
    - H2H global depuis le tableau (ex: "7 : 3")
    - Matchs aujourd'hui pour chaque joueur
    Format titre : "3 : 2 (11:9 11:6 8:11 5:11 11:6)"
    """
    result = {
        "set1_winner":    None,   # "fav" ou "und" ou None
        "set1_score":     None,   # ex: "11:9"
        "fav_today":      0,
        "und_today":      0,
    }
    if not h2h_url:
        return result

    soup = fetch_page(h2h_url)
    if not soup:
        return result

    try:
        text = soup.get_text(separator="\n")
        today_str = datetime.now(tz=timezone.utc).strftime("%d.%m")

        # Score complet : chercher "(11:9 11:6 ...)" dans le texte
        sets_m = re.search(r'\((\d+:\d+)(?:\s+\d+:\d+)*\)', text)
        if sets_m:
            first_set = sets_m.group(1)  # ex: "11:9"
            parts = first_set.split(":")
            if len(parts) == 2:
                s1, s2 = int(parts[0]), int(parts[1])
                # Qui a gagné le set 1 ?
                # P1 est le joueur affiché en premier (home)
                home_links = soup.find_all("a", href=re.compile(r"/players/"))
                if home_links:
                    # Le premier lien joueur = P1 (home)
                    # fav_is_p1 = True → le favori est P1
                    fav_won_s1 = (s1 > s2) if fav_is_p1 else (s2 > s1)
                    result["set1_winner"] = "fav" if fav_won_s1 else "und"
                    result["set1_score"]  = first_set

        # Compter matchs aujourd'hui pour chaque joueur
        # depuis les tableaux "last games"
        date_pattern = re.compile(r'\b(\d{2}\.\d{2}\.\d{2})\b')
        dates = date_pattern.findall(text)

        # Les dates sont du format "DD.MM.YY"
        today_short = today_str  # "DD.MM"

        # Compter par section joueur — approximation par position dans le texte
        p1_section = text.find("last games")
        if p1_section > 0:
            p1_block = text[p1_section:p1_section + 800]
            p2_section_offset = p1_block.find("last games", 10)
            if p2_section_offset > 0:
                p2_block = p1_block[p2_section_offset:]
                p1_block = p1_block[:p2_section_offset]
            else:
                p2_block = ""

            for date_str_found in re.findall(r'(\d{2}\.\d{2})\.\d{2}', p1_block):
                if date_str_found == today_short:
                    result["fav_today"] += 1
            for date_str_found in re.findall(r'(\d{2}\.\d{2})\.\d{2}', p2_block):
                if date_str_found == today_short:
                    result["und_today"] += 1

        log.info(f"H2H page: set1={result['set1_score']} winner={result['set1_winner']} | fav_today={result['fav_today']} und_today={result['und_today']}")
        return result

    except Exception as e:
        log.debug(f"H2H page error: {e}")
        return result

# ─── Analyse principale ───────────────────────────────────────────────────────

def analyze_match(match: dict) -> dict | None:
    h2h = match.get("h2h", {"p1_wins": 0, "p2_wins": 0, "total": 0})

    if h2h["total"] < 1:
        return None

    win_rate_p1 = h2h["p1_wins"] / h2h["total"]

    if win_rate_p1 >= 0.60:
        fav_name, und_name = match["player1"], match["player2"]
        fav_is_p1 = True
        h2h_wins  = h2h["p1_wins"]
    elif win_rate_p1 <= 0.40:
        fav_name, und_name = match["player2"], match["player1"]
        fav_is_p1 = False
        h2h_wins  = h2h["p2_wins"]
    else:
        return None

    # Données H2H page détaillée
    h2h_data = get_h2h_page_data(match.get("last_h2h_url"), fav_is_p1)

    # Type de pari
    # Si le favori a perdu le set 1 du dernier H2H → WIN MATCH
    # Sinon → WIN 1er SET
    if h2h_data["set1_winner"] == "und":
        bet_type = "MATCH"
    else:
        bet_type = "SET1"

    return {
        "fav_name":     fav_name,
        "und_name":     und_name,
        "fav_is_p1":    fav_is_p1,
        "h2h_wins":     h2h_wins,
        "h2h_total":    h2h["total"],
        "set1_winner":  h2h_data["set1_winner"],
        "set1_score":   h2h_data["set1_score"],
        "fav_today":    h2h_data["fav_today"],
        "und_today":    h2h_data["und_today"],
        "bet_type":     bet_type,
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

    # Filtre d'entrée : match avec favori marqué ?
    if REQUIRE_FAVORITE and min(o1, o2) > MAX_FAVORITE_ODDS:
        return

    verdict = analyze_match(match)
    if not verdict:
        return

    fav_odds = o1 if verdict["fav_is_p1"] else o2
    und_odds = o2 if verdict["fav_is_p1"] else o1

    # Validation cote après analyse
    odds_in_window = MIN_FAVORITE_ODDS <= fav_odds <= MAX_FAVORITE_ODDS
    if not IGNORE_ODDS_FILTER and not odds_in_window:
        log.info(f"❌ {verdict['fav_name']} cote {fav_odds} hors fenêtre — divergence")
        return

    analysis = {
        "match_id":      mid,
        "player1":       match["player1"],
        "player2":       match["player2"],
        "favorite":      verdict["fav_name"],
        "underdog":      verdict["und_name"],
        "fav_odds":      fav_odds,
        "und_odds":      und_odds,
        "bet_type":      verdict["bet_type"],
        "h2h_wins":      verdict["h2h_wins"],
        "h2h_total":     verdict["h2h_total"],
        "set1_winner":   verdict["set1_winner"],
        "set1_score":    verdict["set1_score"],
        "fav_today":     verdict["fav_today"],
        "und_today":     verdict["und_today"],
        "time":          match["time"],
        "competition":   match["competition"],
        "odds_in_window": odds_in_window,
    }

    send_telegram(format_alert(analysis), ALERT_DESTINATIONS)
    alerted.add(mid)
    tracking[mid] = analysis
    log.info(f"✅ {verdict['fav_name']} | {verdict['bet_type']} | H2H {verdict['h2h_wins']}/{verdict['h2h_total']}")

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

        snippet = content[idx:idx+500]
        if p2_first not in snippet.lower():
            continue

        # Score final
        final_m = re.search(r'\b([0-4])\s*:\s*([0-4])\b', snippet)
        if not final_m:
            continue

        s1, s2 = int(final_m.group(1)), int(final_m.group(2))
        if s1 + s2 < 2 or s1 + s2 > 7:
            continue

        fav_won_match = (s1 > s2) if fav_is_p1 else (s2 > s1)

        # Score set 1
        after  = snippet[final_m.end():]
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
            log.info(f"📊 SET1 {key}: {a['favorite']}")
        else:
            key = "won" if fav_won_match else "lost"
            bilan["match"][key] += 1
            bilan["week"]["match"][key] += 1
            log.info(f"📊 MATCH {key}: {a['favorite']}")

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
            log.info("📅 Bilan quotidien")

        if now.weekday() == 6 and now.hour == 0 and now.minute == 0 and week_str != last_weekly:
            bilan = load_bilan()
            send_telegram(format_bilan(bilan, "weekly"), ALERT_DESTINATIONS)
            bilan["week"] = {"set1": {"won": 0, "lost": 0}, "match": {"won": 0, "lost": 0}}
            save_bilan(bilan)
            last_weekly = week_str
            log.info("📆 Bilan hebdomadaire")

        time.sleep(60)

# ─── Format message ───────────────────────────────────────────────────────────

def format_alert(a: dict) -> str:
    comp = COMP_LABELS.get(a["competition"], a["competition"])

    # Cotes sur les noms
    if a["favorite"] == a["player1"]:
        p1_str = f"{a['player1']} ({a['fav_odds']})"
        p2_str = f"{a['player2']} ({a['und_odds']})"
    else:
        p1_str = f"{a['player1']} ({a['und_odds']})"
        p2_str = f"{a['player2']} ({a['fav_odds']})"

    pari_str = "WIN 1er SET" if a["bet_type"] == "SET1" else "WIN MATCH"

    # Set 1 dernier H2H
    if a["set1_winner"] == "fav":
        s1_str = f"✅ {a['favorite']} ({a['set1_score']})"
    elif a["set1_winner"] == "und":
        s1_str = f"❌ {a['underdog']} ({a['set1_score']})"
    else:
        s1_str = "N/A"

    # Fatigue
    fatigue_str = ""
    if a["fav_today"] >= 3:
        fatigue_str = f"\n⚠️ {a['favorite']} — {a['fav_today']}e match aujourd'hui"
    elif a["fav_today"] == 2:
        fatigue_str = f"\n⚡ {a['favorite']} — 2e match aujourd'hui"

    # Note hors fenêtre
    odds_note = "\n⚠️ <i>Cote hors fenêtre</i>" if (IGNORE_ODDS_FILTER and not a["odds_in_window"]) else ""

    return (
        f"🏓 <b>{comp}</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"⚔️  {p1_str} vs {p2_str}\n"
        f"🕐  {a['time']} UTC{fatigue_str}{odds_note}\n\n"
        f"✅ <b>PARI : {a['favorite']} — {pari_str}</b>\n\n"
        f"📊 H2H : {a['h2h_wins']}/{a['h2h_total']}\n"
        f"📊 Set 1 dernier H2H : {s1_str}"
    )

# ─── Boucle principale ───────────────────────────────────────────────────────

def run():
    log.info("🚀 Bot démarré")
    opts = [
        f"• Filtre favori (cote ≤ {MAX_FAVORITE_ODDS}) : {'✅' if REQUIRE_FAVORITE else '❌'}",
        f"• Validation cote : {'❌ désactivée' if IGNORE_ODDS_FILTER else '✅'}",
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
