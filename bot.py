#!/usr/bin/env python3
"""
Setka Cup Betting Bot - Version Optimisee "Friction Zero"
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

from config import (
    TELEGRAM_BOT_TOKEN, ALERT_DESTINATIONS, COMPETITIONS,
    MIN_FAVORITE_ODDS, MAX_FAVORITE_ODDS, IGNORE_ODDS_FILTER,
    REQUIRE_FAVORITE, STRICT_DOMINATION_FILTER, MIN_POINT_DIFF_LAST_SET1,
    STARTUP_MESSAGE_ENABLED, ENABLE_DAILY_RECAP,
    SET1_ALERT_LABEL, MATCH_ALERT_LABEL,
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
    "setka_cup_cz":  "Setka Cup. Czech Republic",
    "pro_league_cz": "Pro League. Czech Republic",
    "tt_cup_cz":     "TT-Cup. Czech Republic",
}

COMP_LABELS = {
    "setka_cup_cz":  "Setka Cup CZ",
    "pro_league_cz": "Pro League CZ",
    "tt_cup_cz":     "TT Cup CZ",
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
                log.info(f"Envoye -> {dest}")
            else:
                log.warning(f"Telegram [{dest}]: {r.status_code}")
        except Exception as e:
            log.error(f"Erreur Telegram: {e}")

def fetch_page(url: str) -> BeautifulSoup | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        log.error(f"Erreur Fetch [{url}]: {e}")
        return None

# ─── Parsing matchs ───────────────────────────────────────────────────────────

def parse_match_block(block_soup: BeautifulSoup, block_text: str,
                      match_date: str, match_time: str,
                      competition_key: str, section_name: str) -> dict | None:

    # 1. Verifier section dans les 200 premiers caracteres
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

    # 3. Joueurs depuis liens /players/
    player_links = block_soup.find_all(
        "a", href=re.compile(r"score-tennis\.com/players/|^/players/")
    )
    if len(player_links) < 2:
        return None

    p1 = " ".join(player_links[0].get_text().split())
    p2 = " ".join(player_links[1].get_text().split())
    if not p1 or not p2 or p1 == p2:
        return None

    # 4. H2H global
    h2h_m = re.search(r'(\d+)\s*:\s*(\d+)\s*\n?\s*score of recent face-to-face', block_text)
    h2h_p1 = h2h_p2 = 0
    if h2h_m:
        h2h_p1, h2h_p2 = int(h2h_m.group(1)), int(h2h_m.group(2))

    # 5. Score set 1 du dernier H2H
    # CORRECTION : verifier explicitement que la ligne n'est pas BASE
    set1_p1 = set1_p2 = None
    rows = block_soup.select("table tr")
    for row in rows:
        cols = [td.get_text(strip=True) for td in row.find_all("td")]
        if len(cols) < 6:
            continue
        # Verifier format date
        if not re.match(r'\d{2}\.\d{2}', cols[0]):
            continue
        # Verifier score global present (ex: "3:0")
        if not re.match(r'\d\s*:\s*\d', cols[1]):
            continue
        # CORRECTION CRITIQUE : ignorer les lignes BASE
        if any(c == "BASE" for c in cols):
            continue
        # Verifier que les colonnes set1 sont des entiers
        try:
            v1 = int(cols[2])
            v2 = int(cols[3])
            set1_p1 = v1
            set1_p2 = v2
            break
        except (ValueError, IndexError):
            continue

    p1_href = player_links[0].get("href", "")
    p2_href = player_links[1].get("href", "")
    p1_url  = p1_href if p1_href.startswith("http") else f"{BASE_URL}{p1_href}"
    p2_url  = p2_href if p2_href.startswith("http") else f"{BASE_URL}{p2_href}"

    return {
        "id":          f"{p1}_{p2}_{match_date}_{match_time}",
        "player1":     p1,
        "player2":     p2,
        "time":        match_time,
        "date":        match_date,
        "competition": competition_key,
        "odds":        [w1, w2],
        "h2h":         {"p1_wins": h2h_p1, "p2_wins": h2h_p2, "total": h2h_p1 + h2h_p2},
        "set1_p1":     set1_p1,
        "set1_p2":     set1_p2,
        "p1_url":      p1_url,
        "p2_url":      p2_url,
    }


def fetch_matches_today(competition_key: str) -> list:
    try:
        r = requests.get(f"{BASE_URL}/up-games/", headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return []
    except:
        return []

    section_name = COMP_SECTION_NAMES.get(competition_key, "")
    today_str    = datetime.now(tz=timezone.utc).strftime("%d.%m")
    matches      = []
    blocks       = re.split(r'(\d{2}\.\d{2}\s+\d{2}:\d{2})', r.text)

    i = 1
    while i < len(blocks) - 1:
        header, block_html = blocks[i], blocks[i + 1]
        date_m = re.match(r'(\d{2}\.\d{2})\s+(\d{2}:\d{2})', header)
        if not date_m or date_m.group(1) != today_str:
            i += 2
            continue

        block_soup = BeautifulSoup(block_html, "html.parser")
        block_text = block_soup.get_text(separator="\n").strip()

        # CORRECTION BUG PRO LEAGUE :
        # Chercher le nom de section dans les divs de type "tag" du bloc
        # C'est le seul endroit fiable car le nom de section est dans un div.tag
        tag_divs = block_soup.find_all("div", class_=lambda c: c and "tag" in c)
        comp_names_in_block = [d.get_text(strip=True) for d in tag_divs]
        if section_name not in comp_names_in_block:
            i += 2
            continue

        match = parse_match_block(
            block_soup, block_text,
            date_m.group(1), date_m.group(2),
            competition_key, section_name
        )
        if match:
            log.info(
                f"Match: {match['player1']} vs {match['player2']} | "
                f"W1={match['odds'][0]} W2={match['odds'][1]} | "
                f"H2H {match['h2h']['p1_wins']}:{match['h2h']['p2_wins']} | "
                f"Set1 {match['set1_p1']}:{match['set1_p2']}"
            )
            matches.append(match)
        i += 2

    log.info(f"[{competition_key}] {len(matches)} matchs")
    return matches

# ─── Fatigue ──────────────────────────────────────────────────────────────────

def get_matches_today(player_url: str, today_str: str) -> int:
    soup = fetch_page(player_url)
    if not soup:
        return 0
    count = 0
    for row in soup.select("table tr"):
        cols = [td.get_text(strip=True) for td in row.find_all("td")]
        if cols and re.match(r'\d{2}\.\d{2}\.\d{2}', cols[0]) and cols[0][:5] == today_str:
            count += 1
    return count

# ─── Analyse ─────────────────────────────────────────────────────────────────

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
    point_diff   = None

    if s1p1 is not None and s1p2 is not None:
        fav_won_set1 = (s1p1 > s1p2) if fav_is_p1 else (s1p2 > s1p1)
        set1_score   = f"{s1p1}:{s1p2}" if fav_is_p1 else f"{s1p2}:{s1p1}"
        set1_str_fav = set1_score
        point_diff   = abs(s1p1 - s1p2)

    # Filtre marge de securite
    # Si le favori a gagne le set1 mais avec ecart insuffisant -> No Bet
    if STRICT_DOMINATION_FILTER and fav_won_set1 is True and point_diff is not None:
        if point_diff < MIN_POINT_DIFF_LAST_SET1:
            log.info(
                f"FILTRE domination: {fav_name} ecart set1={point_diff} "
                f"< {MIN_POINT_DIFF_LAST_SET1} ({set1_str_fav}) -> SKIP"
            )
            return None

    # Fatigue
    fav_today = get_matches_today(fav_url, match["date"])

    # Type de pari
    bet_type = "MATCH" if fav_won_set1 is False else "SET1"

    log.info(
        f"Analyse: {fav_name} | H2H {h2h_wins}/{h2h['total']} | "
        f"Set1={set1_str_fav} ecart={point_diff} | Today={fav_today} | -> {bet_type}"
    )

    return {
        "fav_name":     fav_name,
        "und_name":     und_name,
        "fav_is_p1":    fav_is_p1,
        "h2h_wins":     h2h_wins,
        "h2h_total":    h2h["total"],
        "fav_won_set1": fav_won_set1,
        "set1_score":   set1_str_fav,
        "point_diff":   point_diff,
        "fav_today":    fav_today,
        "bet_type":     bet_type,
    }

# ─── Traitement alerte ────────────────────────────────────────────────────────

def process_alert(match: dict):
    if match["id"] in alerted:
        return

    o1, o2 = match["odds"]
    if REQUIRE_FAVORITE and min(o1, o2) > MAX_FAVORITE_ODDS:
        return

    verdict = analyze_match(match)
    if not verdict:
        return

    fav_odds = o1 if verdict["fav_is_p1"] else o2
    und_odds = o2 if verdict["fav_is_p1"] else o1

    odds_in_window = MIN_FAVORITE_ODDS <= fav_odds <= MAX_FAVORITE_ODDS
    if not IGNORE_ODDS_FILTER and not odds_in_window:
        log.info(f"Cote {fav_odds} hors fenetre pour {verdict['fav_name']}")
        return

    analysis = {
        "match_id":      match["id"],
        "player1":       match["player1"],
        "player2":       match["player2"],
        "favorite":      verdict["fav_name"],
        "underdog":      verdict["und_name"],
        "fav_odds":      fav_odds,
        "und_odds":      und_odds,
        "bet_type":      verdict["bet_type"],
        "h2h_wins":      verdict["h2h_wins"],
        "h2h_total":     verdict["h2h_total"],
        "fav_won_set1":  verdict["fav_won_set1"],
        "set1_score":    verdict["set1_score"],
        "point_diff":    verdict["point_diff"],
        "fav_today":     verdict["fav_today"],
        "time":          match["time"],
        "competition":   match["competition"],
        "odds_in_window": odds_in_window,
    }

    send_telegram(format_alert(analysis), ALERT_DESTINATIONS)
    alerted.add(match["id"])
    tracking[match["id"]] = analysis
    log.info(f"Alerte: {verdict['fav_name']} | {verdict['bet_type']}")

# ─── Resultats + bilan ───────────────────────────────────────────────────────

def check_results():
    if not tracking:
        return
    bilan   = load_bilan()
    changed = False

    soup = fetch_page(f"{BASE_URL}/games/")
    if not soup:
        return
    content = soup.get_text(separator="\n").lower()

    for mid, a in list(tracking.items()):
        if mid in seen:
            continue

        p1_f = a["player1"].split()[0].lower()
        p2_f = a["player2"].split()[0].lower()
        idx  = content.find(p1_f)
        if idx == -1:
            continue
        snip = content[idx:idx + 500]
        if p2_f not in snip:
            continue

        final_m = re.search(r'\b([0-4])\s*:\s*([0-4])\b', snip)
        if not final_m:
            continue

        s1, s2    = int(final_m.group(1)), int(final_m.group(2))
        if s1 + s2 < 2 or s1 + s2 > 7:
            continue

        fav_is_p1 = a["favorite"] == a["player1"]
        fav_won_m = (s1 > s2) if fav_is_p1 else (s2 > s1)

        set1_m = re.search(r'\(?\s*(\d{1,2})\s*:\s*(\d{1,2})', snip[final_m.end():])
        if set1_m:
            ss1, ss2   = int(set1_m.group(1)), int(set1_m.group(2))
            fav_won_s1 = (ss1 > ss2) if fav_is_p1 else (ss2 > ss1)
        else:
            fav_won_s1 = fav_won_m

        cat = "set1" if a["bet_type"] == "SET1" else "match"
        key = "won" if (fav_won_s1 if a["bet_type"] == "SET1" else fav_won_m) else "lost"
        bilan[cat][key] += 1
        bilan["week"][cat][key] += 1
        seen.add(mid)
        changed = True
        log.info(f"Bilan {cat} {key}: {a['favorite']}")

    if changed:
        save_bilan(bilan)

# ─── Format message ───────────────────────────────────────────────────────────

def format_alert(a: dict) -> str:
    comp = COMP_LABELS.get(a["competition"], a["competition"])

    # Cotes sur les noms, favori en gras
    if a["favorite"] == a["player1"]:
        p1_str = f"<b>{a['player1']} ({a['fav_odds']})</b>"
        p2_str = f"{a['player2']} ({a['und_odds']})"
    else:
        p1_str = f"{a['player1']} ({a['und_odds']})"
        p2_str = f"<b>{a['player2']} ({a['fav_odds']})</b>"

    # Label pari
    pari_label = SET1_ALERT_LABEL if a["bet_type"] == "SET1" else MATCH_ALERT_LABEL

    # Set 1
    if a["fav_won_set1"] is True:
        diff_str = f" (ecart: {a['point_diff']})" if a["point_diff"] is not None else ""
        s1_str   = f"Gagne {a['set1_score']}{diff_str}"
    elif a["fav_won_set1"] is False:
        s1_str = f"Perdu {a['set1_score']}"
    else:
        s1_str = "N/A"

    # Fatigue
    fatigue = ""
    if a["fav_today"] >= 3:
        fatigue = f"\n{a['favorite']} - {a['fav_today']}e match aujourd'hui"
    elif a["fav_today"] == 2:
        fatigue = f"\n{a['favorite']} - 2e match aujourd'hui"

    # Note hors fenetre
    odds_note = "\nCote hors fenetre" if (IGNORE_ODDS_FILTER and not a["odds_in_window"]) else ""

    return (
        f"<b>{comp}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{p1_str} vs {p2_str}\n"
        f"{a['time']} UTC{fatigue}{odds_note}\n\n"
        f"<b>PARI : {a['favorite']} - {pari_label}</b>\n\n"
        f"H2H : {a['h2h_wins']}/{a['h2h_total']}\n"
        f"Set 1 dernier H2H : {s1_str}"
    )

# ─── Bilan thread ─────────────────────────────────────────────────────────────

def bilan_thread():
    last_daily = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    while True:
        now      = datetime.now(tz=timezone.utc)
        date_str = now.strftime("%Y-%m-%d")

        if date_str != last_daily and ENABLE_DAILY_RECAP:
            b  = load_bilan()
            s1 = b["set1"]
            ma = b["match"]
            t1 = s1["won"] + s1["lost"]
            tm = ma["won"] + ma["lost"]

            if t1 > 0 or tm > 0:
                p1 = f"{int(s1['won']/t1*100)}%" if t1 > 0 else "N/A"
                pm = f"{int(ma['won']/tm*100)}%" if tm > 0 else "N/A"
                msg = (
                    f"<b>BILAN QUOTIDIEN</b>\n"
                    f"WIN 1er SET : {s1['won']}G/{s1['lost']}P ({p1})\n"
                    f"WIN MATCH   : {ma['won']}G/{ma['lost']}P ({pm})"
                )
                send_telegram(msg, ALERT_DESTINATIONS)
                b["set1"]  = {"won": 0, "lost": 0}
                b["match"] = {"won": 0, "lost": 0}
                save_bilan(b)

            last_daily = date_str

        time.sleep(300)

# ─── Boucle principale ───────────────────────────────────────────────────────

def run():
    log.info("Bot demarre")

    if STARTUP_MESSAGE_ENABLED:
        opts = [
            f"Filtre domination : {'OUI (ecart >= ' + str(MIN_POINT_DIFF_LAST_SET1) + ' pts)' if STRICT_DOMINATION_FILTER else 'NON'}",
            f"Filtre favori (cote <= {MAX_FAVORITE_ODDS}) : {'OUI' if REQUIRE_FAVORITE else 'NON'}",
            f"Validation cote : {'NON' if IGNORE_ODDS_FILTER else 'OUI'}",
            f"Ligues : {', '.join(COMPETITIONS)}",
            f"Source : score-tennis.com",
        ]
        send_telegram(
            "<b>Bot Setka Cup demarre</b>\n" + "\n".join(opts),
            ALERT_DESTINATIONS
        )

    threading.Thread(target=bilan_thread, daemon=True).start()

    while True:
        try:
            for comp in COMPETITIONS:
                for match in fetch_matches_today(comp):
                    process_alert(match)
            check_results()
        except Exception as e:
            log.error(f"Erreur: {e}")

        log.info(f"Pause {CHECK_INTERVAL_SECONDS}s...")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
