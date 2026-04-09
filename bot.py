#!/usr/bin/env python3
"""
Setka Cup Betting Bot - Version Optimisée
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

# Import des réglages depuis config.py
from config import (
    TELEGRAM_BOT_TOKEN, ALERT_DESTINATIONS, COMPETITIONS, 
    MIN_FAVORITE_ODDS, MAX_FAVORITE_ODDS, IGNORE_ODDS_FILTER, 
    REQUIRE_FAVORITE, STRICT_DOMINATION_FILTER, MIN_POINT_DIFF_LAST_SET1,
    STARTUP_MESSAGE_ENABLED, ENABLE_DAILY_RECAP,
    SET1_ALERT_LABEL, MATCH_ALERT_LABEL
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 60
BILAN_FILE = Path("bilan.json")
BASE_URL = "https://score-tennis.com"

COMP_SECTION_NAMES = {
    "setka_cup_cz":   "Setka Cup. Czech Republic",
    "pro_league_cz":  "Pro League. Czech Republic",
    "tt_cup_cz":      "TT-Cup. Czech Republic",
}

COMP_LABELS = {
    "setka_cup_cz":  "Setka Cup CZ",
    "pro_league_cz": "Pro League CZ",
    "tt_cup_cz":     "TT Cup CZ",
}

def load_bilan() -> dict:
    if BILAN_FILE.exists():
        try: return json.loads(BILAN_FILE.read_text())
        except: pass
    return {"set1": {"won": 0, "lost": 0}, "match": {"won": 0, "lost": 0}, 
            "week": {"set1": {"won": 0, "lost": 0}, "match": {"won": 0, "lost": 0}}}

def save_bilan(b: dict):
    BILAN_FILE.write_text(json.dumps(b, indent=2))

alerted, tracking, seen = set(), {}, set()
HEADERS = {"User-Agent": "Mozilla/5.0"}

# --- FONCTIONS UTILITAIRES ---
def send_telegram(message: str, destinations: list):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for dest in destinations:
        try: requests.post(url, json={"chat_id": dest, "text": message, "parse_mode": "HTML"}, timeout=10)
        except Exception as e: log.error(f"Erreur Telegram: {e}")

def fetch_page(url: str) -> BeautifulSoup | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        return BeautifulSoup(r.text, "html.parser")
    except: return None

# --- PARSING ET ANALYSE ---
def parse_match_block(block_soup: BeautifulSoup, block_text: str, match_date: str, match_time: str, competition_key: str, section_name: str) -> dict | None:
    # 1. Protection contre les mauvais blocs
    if "face-to-face" not in block_text: return None

    # 2. Extraction des cotes
    w1_m, w2_m = re.search(r'W1[^:]*:\s*([\d.]+)', block_text), re.search(r'W2[^:]*:\s*([\d.]+)', block_text)
    if not w1_m or not w2_m: return None
    w1, w2 = float(w1_m.group(1)), float(w2_m.group(1))

    # 3. Extraction des noms
    player_links = block_soup.find_all("a", href=re.compile(r"/players/"))
    if len(player_links) < 2: return None
    p1, p2 = [" ".join(p.get_text().split()) for p in player_links[:2]]

    # 4. H2H Global
    h2h_m = re.search(r'(\d+)\s*:\s*(\d+)\s*\n?\s*score of recent face-to-face', block_text)
    h2h_p1, h2h_p2 = (int(h2h_m.group(1)), int(h2h_m.group(2))) if h2h_m else (0, 0)

    # 5. Score Set 1 (CORRECTION "BASE")
    set1_p1 = set1_p2 = None
    rows = block_soup.select("table tr")
    for row in rows:
        cols = [td.get_text(strip=True) for td in row.find_all("td")]
        if len(cols) < 6 or not re.match(r'\d{2}\.\d{2}', cols[0]): continue
        if "BASE" in cols: continue  # ← CORRECTION CRITIQUE
        try:
            set1_p1, set1_p2 = int(cols[2]), int(cols[3])
            break
        except: continue

    return {
        "id": f"{p1}_{p2}_{match_date}_{match_time}", "player1": p1, "player2": p2,
        "time": match_time, "date": match_date, "competition": competition_key, "odds": [w1, w2],
        "h2h": {"p1_wins": h2h_p1, "p2_wins": h2h_p2, "total": h2h_p1 + h2h_p2},
        "set1_p1": set1_p1, "set1_p2": set1_p2, 
        "p1_url": f"{BASE_URL}{player_links[0].get('href', '')}", "p2_url": f"{BASE_URL}{player_links[1].get('href', '')}"
    }

def analyze_match(match: dict) -> dict | None:
    h2h = match["h2h"]
    if h2h["total"] < 1: return None
    win_rate_p1 = h2h["p1_wins"] / h2h["total"]

    if win_rate_p1 >= 0.60:
        fav_name, und_name, fav_is_p1 = match["player1"], match["player2"], True
        h2h_wins, fav_url = h2h["p1_wins"], match["p1_url"]
    elif win_rate_p1 <= 0.40:
        fav_name, und_name, fav_is_p1 = match["player2"], match["player1"], False
        h2h_wins, fav_url = h2h["p2_wins"], match["p2_url"]
    else: return None

    s1p1, s1p2 = match["set1_p1"], match["set1_p2"]
    fav_won_set1 = (s1p1 > s1p2) if (fav_is_p1 and s1p1 is not None) else (s1p2 > s1p1) if (not fav_is_p1 and s1p1 is not None) else None
    point_diff = abs(s1p1 - s1p2) if s1p1 is not None else 0

    if STRICT_DOMINATION_FILTER and fav_won_set1 is True and point_diff < MIN_POINT_DIFF_LAST_SET1:
        return None

    return {"fav_name": fav_name, "und_name": und_name, "fav_is_p1": fav_is_p1, "h2h_wins": h2h_wins, 
            "h2h_total": h2h["total"], "fav_won_set1": fav_won_set1, "set1_score": f"{s1p1}:{s1p2}" if fav_is_p1 else f"{s1p2}:{s1p1}",
            "bet_type": "MATCH" if fav_won_set1 is False else "SET1"}

def fetch_matches_today(competition_key: str) -> list:
    r = fetch_page(f"{BASE_URL}/up-games/")
    if not r: return []
    
    target_section = COMP_SECTION_NAMES.get(competition_key, "")
    today_str = datetime.now(tz=timezone.utc).strftime("%d.%m")
    matches = []

    # On cherche tous les blocs de matchs (ils sont souvent dans des divs ou séparés par des hr/br)
    # Ici on utilise une recherche par texte pour isoler uniquement ce qui appartient à la ligue
    content_html = r.decode_contents()
    
    # On découpe la page par ligue d'abord
    # On cherche l'endroit où commence la section cible (ex: Setka Cup Ukraine)
    start_idx = content_html.find(target_section)
    if start_idx == -1:
        return []

    # On prend le contenu à partir de là jusqu'à la prochaine section ou fin de page
    # Cela évite de lire les matchs des autres ligues qui sont plus bas
    sub_content = content_html[start_idx:]
    next_section_idx = 1000000 # Valeur par défaut
    
    # On cherche si une autre ligue commence après pour s'arrêter avant
    for other_section in COMP_SECTION_NAMES.values():
        if other_section != target_section:
            idx = sub_content.find(other_section, 50) # On ignore les 50 premiers caractères
            if idx != -1 and idx < next_section_idx:
                next_section_idx = idx
    
    # On a maintenant le morceau de code qui ne contient QUE notre ligue
    league_html = sub_content[:next_section_idx]
    
    # On découpe maintenant par date/heure à l'intérieur de cette ligue
    blocks = re.split(r'(\d{2}\.\d{2}\s+\d{2}:\d{2})', league_html)
    
    i = 1
    while i < len(blocks) - 1:
        header, block_html = blocks[i], blocks[i + 1]
        if today_str not in header:
            i += 2; continue
            
        block_soup = BeautifulSoup(block_html, "html.parser")
        block_text = block_soup.get_text(separator="\n").strip()
        
        # On vérifie qu'on a bien un H2H sinon c'est un bloc vide
        if "face-to-face" not in block_text.lower():
            i += 2; continue

        match = parse_match_block(block_soup, block_text, today_str, header.split()[1], competition_key, target_section)
        if match:
            matches.append(match)
        i += 2
        
    return matches

def format_alert(a: dict) -> str:
    comp = COMP_LABELS.get(a["competition"], a["competition"])
    p1 = f"<b>{a['player1']} ({a['fav_odds'] if a['favorite']==a['player1'] else a['und_odds']})</b>"
    p2 = f"<b>{a['player2']} ({a['fav_odds'] if a['favorite']==a['player2'] else a['und_odds']})</b>"
    # CORRECTION LABELS
    pari_label = SET1_ALERT_LABEL if a["bet_type"] == "SET1" else MATCH_ALERT_LABEL
    return f"<b>{comp}</b>\n━━━━━━━━━━━━━━━━━━━━\n{p1} vs {p2}\n{a['time']} UTC\n\n<b>PARI : {a['favorite']} - {pari_label}</b>\n\nH2H : {a['h2h_wins']}/{a['h2h_total']}\nSet 1 dernier H2H : {a['set1_score']}"

def process_alert(match: dict):
    if match["id"] in alerted: return
    o1, o2 = match["odds"]
    verdict = analyze_match(match)
    if not verdict: return
    fav_odds = o1 if verdict["fav_is_p1"] else o2
    if not IGNORE_ODDS_FILTER and not (MIN_FAVORITE_ODDS <= fav_odds <= MAX_FAVORITE_ODDS): return

    analysis = {**match, "favorite": verdict["fav_name"], "fav_odds": fav_odds, "und_odds": o2 if verdict["fav_is_p1"] else o1,
                "bet_type": verdict["bet_type"], "h2h_wins": verdict["h2h_wins"], "h2h_total": verdict["h2h_total"], "set1_score": verdict["set1_score"]}
    send_telegram(format_alert(analysis), ALERT_DESTINATIONS)
    alerted.add(match["id"])
    tracking[match["id"]] = analysis

def check_results():
    if not tracking: return
    bilan, changed = load_bilan(), False
    soup = fetch_page(f"{BASE_URL}/games/")
    if not soup: return
    content = soup.get_text().lower()
    for mid, a in list(tracking.items()):
        if mid in seen or a["player1"].split()[0].lower() not in content: continue
        # Logique de résultat simplifiée pour la stabilité
        seen.add(mid); changed = True
    if changed: save_bilan(bilan)

def run():
    if STARTUP_MESSAGE_ENABLED: send_telegram("<b>🚀 Bot Setka Cup Ready</b>", ALERT_DESTINATIONS)
    while True:
        try:
            for comp in COMPETITIONS:
                for match in fetch_matches_today(comp): process_alert(match)
            check_results()
        except Exception as e: log.error(f"Loop Error: {e}")
        time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__": run()
