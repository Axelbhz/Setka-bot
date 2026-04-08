#!/usr/bin/env python3
"""
Setka Cup Betting Bot - Version Optimisée "Friction Zéro"
Source: score-tennis.com
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
    STARTUP_MESSAGE_ENABLED, ENABLE_DAILY_RECAP
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

# --- GESTION DU BILAN (Sauvegarde sur Railway) ---
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
}

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

# --- FONCTIONS DE BASE ---
def send_telegram(message: str, destinations: list):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for dest in destinations:
        try:
            r = requests.post(url, json={
                "chat_id": dest, "text": message, "parse_mode": "HTML"
            }, timeout=10)
        except Exception as e:
            log.error(f"Erreur Telegram: {e}")

def fetch_page(url: str) -> BeautifulSoup | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        log.error(f"Erreur Fetch: {e}")
        return None

# --- ANALYSE ET FILTRE DE DOMINATION ---
def analyze_match(match: dict) -> dict | None:
    h2h = match["h2h"]
    if h2h["total"] < 1:
        return None

    win_rate_p1 = h2h["p1_wins"] / h2h["total"]

    if win_rate_p1 >= 0.60:
        fav_name, und_name = match["player1"], match["player2"]
        fav_is_p1 = True
        h2h_wins  = h2h["p1_wins"]
        fav_url   = match["p1_url"]
    elif win_rate_p1 <= 0.40:
        fav_name, und_name = match["player2"], match["player1"]
        fav_is_p1 = False
        h2h_wins  = h2h["p2_wins"]
        fav_url   = match["p2_url"]
    else:
        return None

    s1p1 = match["set1_p1"]
    s1p2 = match["set1_p2"]
    fav_won_set1 = None
    set1_str_fav = None
    point_diff = 0

    if s1p1 is not None and s1p2 is not None:
        fav_won_set1 = (s1p1 > s1p2) if fav_is_p1 else (s1p2 > s1p1)
        set1_score = f"{s1p1}:{s1p2}" if fav_is_p1 else f"{s1p2}:{s1p1}"
        set1_str_fav = set1_score
        point_diff = abs(s1p1 - s1p2)

    # 🎯 APPLICATION DU FILTRE : Écart de points
    if STRICT_DOMINATION_FILTER and fav_won_set1 is True:
        if point_diff < MIN_POINT_DIFF_LAST_SET1:
            log.info(f"FILTRE : {fav_name} écart trop faible ({set1_str_fav}) -> SKIP")
            return None

    # Fatigue
    today_str = match["date"]
    fav_today = get_matches_today(fav_url, today_str)

    # Type de pari
    bet_type = "MATCH" if fav_won_set1 is False else "SET1"

    return {
        "fav_name": fav_name, "und_name": und_name, "fav_is_p1": fav_is_p1,
        "h2h_wins": h2h_wins, "h2h_total": h2h["total"],
        "fav_won_set1": fav_won_set1, "set1_score": set1_str_fav,
        "fav_today": fav_today, "bet_type": bet_type,
    }
    # --- PARSING DES MATCHS (CORRECTION BUG PRO LEAGUE) ---
def parse_match_block(block_soup: BeautifulSoup, block_text: str,
                      match_date: str, match_time: str,
                      competition_key: str, section_name: str) -> dict | None:
    # 1. Vérification stricte du nom de section
    if section_name not in block_text[:200]:
        return None

    if "face-to-face" not in block_text:
        return None

    # 2. Cotes
    w1_m = re.search(r'W1[^:]*:\s*([\d.]+)', block_text)
    w2_m = re.search(r'W2[^:]*:\s*([\d.]+)', block_text)
    if not w1_m or not w2_m:
        return None

    w1, w2 = float(w1_m.group(1)), float(w2_m.group(1))

    # 3. Joueurs
    player_links = block_soup.find_all("a", href=re.compile(r"score-tennis\.com/players/|^/players/"))
    if len(player_links) < 2:
        return None

    p1 = " ".join(player_links[0].get_text().split())
    p2 = " ".join(player_links[1].get_text().split())

    # 4. H2H Global
    h2h_m = re.search(r'(\d+)\s*:\s*(\d+)\s*\n?\s*score of recent face-to-face', block_text)
    h2h_p1 = h2h_p2 = 0
    if h2h_m:
        h2h_p1, h2h_p2 = int(h2h_m.group(1)), int(h2h_m.group(2))

    # 5. Score Set 1 dernier H2H
    set1_p1 = set1_p2 = None
    rows = block_soup.select("table tr")
    for row in rows:
        cols = [td.get_text(strip=True) for td in row.find_all("td")]
        if len(cols) < 6 or not re.match(r'\d{2}\.\d{2}', cols[0]):
            continue
        try:
            set1_p1, set1_p2 = int(cols[2]), int(cols[3])
            break
        except:
            continue

    p1_url = f"{BASE_URL}{player_links[0].get('href', '')}"
    p2_url = f"{BASE_URL}{player_links[1].get('href', '')}"

    return {
        "id": f"{p1}_{p2}_{match_date}_{match_time}",
        "player1": p1, "player2": p2, "time": match_time, "date": match_date,
        "competition": competition_key, "odds": [w1, w2],
        "h2h": {"p1_wins": h2h_p1, "p2_wins": h2h_p2, "total": h2h_p1 + h2h_p2},
        "set1_p1": set1_p1, "set1_p2": set1_p2, "p1_url": p1_url, "p2_url": p2_url,
    }

def fetch_matches_today(competition_key: str) -> list:
    try:
        r = requests.get(f"{BASE_URL}/up-games/", headers=HEADERS, timeout=15)
        if r.status_code != 200: return []
    except: return []

    section_name = COMP_SECTION_NAMES.get(competition_key, "")
    today_str = datetime.now(tz=timezone.utc).strftime("%d.%m")
    matches = []
    blocks = re.split(r'(\d{2}\.\d{2}\s+\d{2}:\d{2})', r.text)

    i = 1
    while i < len(blocks) - 1:
        header, block_html = blocks[i], blocks[i + 1]
        date_m = re.match(r'(\d{2}\.\d{2})\s+(\d{2}:\d{2})', header)
        if not date_m or date_m.group(1) != today_str:
            i += 2
            continue

        block_soup = BeautifulSoup(block_html, "html.parser")
        block_text = block_soup.get_text(separator="\n").strip()
        
        # SÉCURITÉ : On vérifie que la ligue est bien en haut du bloc
        if section_name.lower() not in block_text.split('\n')[0].lower():
            i += 2
            continue

        match = parse_match_block(block_soup, block_text, date_m.group(1), date_m.group(2), competition_key, section_name)
        if match: matches.append(match)
        i += 2
    return matches

def get_matches_today(player_url: str, today_str: str) -> int:
    soup = fetch_page(player_url)
    if not soup: return 0
    count = 0
    for row in soup.select("table tr"):
        cols = [td.get_text(strip=True) for td in row.find_all("td")]
        if cols and re.match(r'\d{2}\.\d{2}\.\d{2}', cols[0]) and cols[0][:5] == today_str:
            count += 1
    return count

def process_alert(match: dict):
    if match["id"] in alerted: return
    o1, o2 = match["odds"]
    if REQUIRE_FAVORITE and min(o1, o2) > MAX_FAVORITE_ODDS: return

    verdict = analyze_match(match)
    if not verdict: return

    fav_odds = o1 if verdict["fav_is_p1"] else o2
    odds_in_window = MIN_FAVORITE_ODDS <= fav_odds <= MAX_FAVORITE_ODDS
    if not IGNORE_ODDS_FILTER and not odds_in_window: return

    analysis = {
        "match_id": match["id"], "player1": match["player1"], "player2": match["player2"],
        "favorite": verdict["fav_name"], "underdog": verdict["und_name"],
        "fav_odds": fav_odds, "und_odds": o2 if verdict["fav_is_p1"] else o1,
        "bet_type": verdict["bet_type"], "h2h_wins": verdict["h2h_wins"],
        "h2h_total": verdict["h2h_total"], "fav_won_set1": verdict["fav_won_set1"],
        "set1_score": verdict["set1_score"], "fav_today": verdict["fav_today"],
        "time": match["time"], "competition": match["competition"], "odds_in_window": odds_in_window,
    }
    send_telegram(format_alert(analysis), ALERT_DESTINATIONS)
    alerted.add(match["id"])
    tracking[match["id"]] = analysis

def check_results():
    if not tracking: return
    bilan, changed = load_bilan(), False
    soup = fetch_page(f"{BASE_URL}/games/")
    if not soup: return
    content = soup.get_text(separator="\n").lower()

    for mid, a in list(tracking.items()):
        if mid in seen: continue
        p1_f, p2_f = a["player1"].split()[0].lower(), a["player2"].split()[0].lower()
        idx = content.find(p1_f)
        if idx == -1: continue
        snip = content[idx:idx+500]
        if p2_f not in snip: continue

        final_m = re.search(r'\b([0-4])\s*:\s*([0-4])\b', snip)
        if not final_m: continue
        s1, s2 = int(final_m.group(1)), int(final_m.group(2))
        fav_is_p1 = a["favorite"] == a["player1"]
        fav_won_m = (s1 > s2) if fav_is_p1 else (s2 > s1)

        set1_m = re.search(r'\(?\s*(\d{1,2})\s*:\s*(\d{1,2})', snip[final_m.end():])
        fav_won_s1 = ((int(set1_m.group(1)) > int(set1_m.group(2))) if fav_is_p1 else (int(set1_m.group(2)) > int(set1_m.group(1)))) if set1_m else fav_won_m

        key = "won" if (fav_won_s1 if a["bet_type"] == "SET1" else fav_won_m) else "lost"
        bilan["set1" if a["bet_type"] == "SET1" else "match"][key] += 1
        bilan["week"]["set1" if a["bet_type"] == "SET1" else "match"][key] += 1
        seen.add(mid)
        changed = True

    if changed: save_bilan(bilan)

def format_alert(a: dict) -> str:
    comp = COMP_LABELS.get(a["competition"], a["competition"])
    p1 = f"<b>{a['player1']} ({a['fav_odds'] if a['favorite']==a['player1'] else a['und_odds']})</b>"
    p2 = f"<b>{a['player2']} ({a['fav_odds'] if a['favorite']==a['player2'] else a['und_odds']})</b>"
    s1_str = f"Gagne {a['set1_score']}" if a["fav_won_set1"] else f"Perdu {a['set1_score']}" if a["fav_won_set1"] is False else "N/A"
    fatigue = f"\n{a['favorite']} - {a['fav_today']}e match" if a["fav_today"] >= 2 else ""
    return f"<b>{comp}</b>\n━━━━━━━━━━━━━━━━━━━━\n{p1} vs {p2}\n{a['time']} UTC{fatigue}\n\n<b>PARI : {a['favorite']} - {a['bet_type']}</b>\n\nH2H : {a['h2h_wins']}/{a['h2h_total']}\nSet 1 dernier H2H : {s1_str}"

def bilan_thread():
    last_daily = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    while True:
        now = datetime.now(tz=timezone.utc)
        date_str = now.strftime("%Y-%m-%d")
        if date_str != last_daily and ENABLE_DAILY_RECAP:
            b = load_bilan()
            if (b["set1"]["won"] + b["set1"]["lost"]) > 0:
                s1, ma = b["set1"], b["match"]
                t1, tm = s1['won']+s1['lost'], ma['won']+ma['lost']
                p1, pm = f"{int(s1['won']/t1*100)}%" if t1>0 else "N/A", f"{int(ma['won']/tm*100)}%" if tm>0 else "N/A"
                msg = f"<b>📊 BILAN QUOTIDIEN</b>\nWIN 1er SET : {s1['won']}G/{s1['lost']}P ({p1})\nWIN MATCH : {ma['won']}G/{ma['lost']}P ({pm})"
                send_telegram(msg, ALERT_DESTINATIONS)
                b["set1"], b["match"] = {"won": 0, "lost": 0}, {"won": 0, "lost": 0}
                save_bilan(b)
            last_daily = date_str
        time.sleep(300)

def run():
    if STARTUP_MESSAGE_ENABLED:
        send_telegram(f"<b>🚀 Bot Setka Cup Ready</b>\nFiltre Domination: {STRICT_DOMINATION_FILTER}\nLigues: {', '.join(COMPETITIONS)}", ALERT_DESTINATIONS)
    threading.Thread(target=bilan_thread, daemon=True).start()
    while True:
        try:
            for comp in COMPETITIONS:
                for match in fetch_matches_today(comp): process_alert(match)
            check_results()
        except Exception as e: log.error(f"Erreur: {e}")
        time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    run()
