#!/usr/bin/env python3
"""
Setka Cup Betting Bot - Version Corrigée (Parsing Sectionné)
"""

import re
import time
import json
import logging
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
    PHPSESSID
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

# Noms de sections tels qu'ils apparaissent sur la page
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

# ─────────────────────────────────────────────────────────────
# BILAN
# ─────────────────────────────────────────────────────────────

def load_bilan() -> dict:
    if BILAN_FILE.exists():
        try:
            return json.loads(BILAN_FILE.read_text())
        except Exception:
            pass
    return {
        "set1":  {"won": 0, "lost": 0},
        "match": {"won": 0, "lost": 0},
        "week":  {"set1": {"won": 0, "lost": 0}, "match": {"won": 0, "lost": 0}}
    }

def save_bilan(b: dict):
    BILAN_FILE.write_text(json.dumps(b, indent=2))

# ─────────────────────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────────────────────

alerted: set  = set()   # match IDs déjà alertés
tracking: dict = {}     # match IDs en suivi de résultat
seen: set     = set()   # match IDs dont le résultat a été enregistré

UPGAMES_URL = "https://score-tennis.com/up-games/?champ=all"

def _make_headers() -> dict:
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Cache-Control": "max-age=0",
        "Referer": "https://score-tennis.com/up-games/?champ=all",
        "Upgrade-Insecure-Requests": "1",
        "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "Cookie": f"PHPSESSID={PHPSESSID}",
    }

# ─────────────────────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────────────────────

def send_telegram(message: str, destinations: list):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for dest in destinations:
        try:
            requests.post(
                url,
                json={"chat_id": dest, "text": message, "parse_mode": "HTML"},
                timeout=10
            )
        except Exception as e:
            log.error(f"Erreur Telegram ({dest}): {e}")

# ─────────────────────────────────────────────────────────────
# HTTP
# ─────────────────────────────────────────────────────────────

def fetch_page(url: str) -> BeautifulSoup | None:
    try:
        r = requests.get(url, headers=_make_headers(), timeout=15)
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        log.error(f"fetch_page({url}): {e}")
        return None

# ─────────────────────────────────────────────────────────────
# PARSING — NOYAU CORRIGÉ
# ─────────────────────────────────────────────────────────────

def extract_section_html(full_html: str, section_name: str) -> str | None:
    """
    Extrait le bloc HTML brut compris entre le titre de la section `section_name`
    et le prochain titre de section (ou la fin du document).

    La page score-tennis.com présente les compétitions sous forme de blocs
    successifs séparés par leurs titres. On cherche le titre exact, puis on
    prend tout jusqu'au prochain titre de même niveau.
    """
    # Titre de section tel qu'il apparaît dans le HTML (texte visible)
    # On utilise BeautifulSoup pour trouver le nœud titre, puis on récupère
    # son parent et ses siblings jusqu'au prochain titre de même type.
    soup = BeautifulSoup(full_html, "html.parser")

    # Chercher dans tous les éléments texte le titre exact
    # Les titres de ligue sont souvent dans <h2>, <h3>, <div class="..."> etc.
    # On cherche par texte, insensible à la casse, dans n'importe quel tag.
    title_node = None
    for tag in soup.find_all(string=re.compile(re.escape(section_name), re.IGNORECASE)):
        title_node = tag.parent
        break

    if title_node is None:
        log.debug(f"Section introuvable: '{section_name}'")
        return None

    # Remonter au parent significatif (bloc de compétition)
    # On collecte tous les siblings suivants jusqu'au prochain bloc de même type
    section_parts = [str(title_node)]
    for sibling in title_node.find_next_siblings():
        # On s'arrête si on rencontre un autre titre de compétition
        # (heuristique : un sibling qui contient l'un des noms de section connus)
        sibling_text = sibling.get_text()
        is_new_section = any(
            name.lower() in sibling_text.lower()
            for name in COMP_SECTION_NAMES.values()
            if name.lower() != section_name.lower()
        )
        if is_new_section:
            break
        section_parts.append(str(sibling))

    return "".join(section_parts)


def parse_match_block(block_soup: BeautifulSoup, block_text: str,
                      match_date: str, match_time: str,
                      competition_key: str) -> dict | None:
    """Extrait les données d'un bloc match individuel."""

    # Garde-fou : le bloc doit contenir face-to-face
    if "face-to-face" not in block_text.lower():
        return None

    # Cotes W1 / W2
    w1_m = re.search(r'W1[^:]*:\s*([\d.]+)', block_text)
    w2_m = re.search(r'W2[^:]*:\s*([\d.]+)', block_text)
    if not w1_m or not w2_m:
        return None
    w1, w2 = float(w1_m.group(1)), float(w2_m.group(1))

    # Noms des joueurs
    player_links = block_soup.find_all("a", href=re.compile(r"/players/"))
    if len(player_links) < 2:
        return None
    p1, p2 = [" ".join(p.get_text().split()) for p in player_links[:2]]

    # H2H Global
    h2h_m = re.search(r'(\d+)\s*:\s*(\d+)\s*\n?\s*score of recent face-to-face', block_text)
    h2h_p1, h2h_p2 = (int(h2h_m.group(1)), int(h2h_m.group(2))) if h2h_m else (0, 0)

    # Score Set 1 du dernier H2H (ignorer la ligne BASE)
    set1_p1 = set1_p2 = None
    for row in block_soup.select("table tr"):
        cols = [td.get_text(strip=True) for td in row.find_all("td")]
        if len(cols) < 6:
            continue
        if not re.match(r'\d{2}\.\d{2}', cols[0]):
            continue
        if "BASE" in cols:
            continue
        try:
            set1_p1, set1_p2 = int(cols[2]), int(cols[3])
            break
        except (ValueError, IndexError):
            continue

    return {
        "id": f"{p1}_{p2}_{match_date}_{match_time}",
        "player1": p1,
        "player2": p2,
        "time": match_time,
        "date": match_date,
        "competition": competition_key,
        "odds": [w1, w2],
        "h2h": {"p1_wins": h2h_p1, "p2_wins": h2h_p2, "total": h2h_p1 + h2h_p2},
        "set1_p1": set1_p1,
        "set1_p2": set1_p2,
        "p1_url": f"{BASE_URL}{player_links[0].get('href', '')}",
        "p2_url": f"{BASE_URL}{player_links[1].get('href', '')}",
    }


def fetch_matches_today(competition_key: str) -> list:
    """
    Stratégie en deux passes :
      1. Récupérer la page complète.
      2. Isoler le bloc HTML propre à la section `competition_key`.
      3. À l'intérieur de ce bloc uniquement, chercher les matchs du jour.

    Cela garantit qu'un match d'une autre compétition ne peut jamais
    être labellisé comme appartenant à la compétition courante.
    """
    soup_full = fetch_page(UPGAMES_URL)
    if not soup_full:
        return []

    section_name = COMP_SECTION_NAMES.get(competition_key)
    if not section_name:
        log.warning(f"Compétition inconnue : {competition_key}")
        return []

    full_html = str(soup_full)
    section_html = extract_section_html(full_html, section_name)
    if not section_html:
        log.info(f"Section '{section_name}' absente de la page aujourd'hui.")
        return []

    # Travailler uniquement dans le HTML de la section isolée
    section_soup = BeautifulSoup(section_html, "html.parser")
    today_str = datetime.now(tz=timezone.utc).strftime("%d.%m")

    matches = []

    # Découper la section par blocs timestamp  (DD.MM HH:MM)
    section_raw = section_soup.decode_contents()
    blocks = re.split(r'(\d{2}\.\d{2}\s+\d{2}:\d{2})', section_raw)

    i = 1
    while i < len(blocks) - 1:
        header     = blocks[i]
        block_html = blocks[i + 1]
        i += 2

        # Filtrer sur la date du jour
        if today_str not in header:
            continue

        date_part = header.split()[0]   # "DD.MM"
        time_part = header.split()[1]   # "HH:MM"

        block_soup = BeautifulSoup(block_html, "html.parser")
        block_text = block_soup.get_text(separator="\n").strip()

        match = parse_match_block(
            block_soup, block_text,
            date_part, time_part,
            competition_key
        )
        if match:
            matches.append(match)
            log.info(f"[{COMP_LABELS.get(competition_key)}] Match trouvé : {match['player1']} vs {match['player2']} à {time_part}")

    return matches

# ─────────────────────────────────────────────────────────────
# ANALYSE
# ─────────────────────────────────────────────────────────────

def analyze_match(match: dict) -> dict | None:
    h2h = match["h2h"]
    if h2h["total"] < 1:
        return None

    win_rate_p1 = h2h["p1_wins"] / h2h["total"]

    if win_rate_p1 >= 0.60:
        fav_name, und_name, fav_is_p1 = match["player1"], match["player2"], True
        h2h_wins = h2h["p1_wins"]
        fav_url  = match["p1_url"]
    elif win_rate_p1 <= 0.40:
        fav_name, und_name, fav_is_p1 = match["player2"], match["player1"], False
        h2h_wins = h2h["p2_wins"]
        fav_url  = match["p2_url"]
    else:
        return None

    s1p1, s1p2 = match["set1_p1"], match["set1_p2"]

    if s1p1 is not None and s1p2 is not None:
        fav_won_set1 = (s1p1 > s1p2) if fav_is_p1 else (s1p2 > s1p1)
        point_diff   = abs(s1p1 - s1p2)
    else:
        fav_won_set1 = None
        point_diff   = 0

    # Filtre domination stricte
    if STRICT_DOMINATION_FILTER and fav_won_set1 is True and point_diff < MIN_POINT_DIFF_LAST_SET1:
        return None

    # Score set1 du point de vue du favori
    if fav_is_p1:
        set1_score = f"{s1p1}:{s1p2}" if s1p1 is not None else "N/A"
    else:
        set1_score = f"{s1p2}:{s1p1}" if s1p1 is not None else "N/A"

    bet_type = "MATCH" if fav_won_set1 is False else "SET1"

    return {
        "fav_name":    fav_name,
        "und_name":    und_name,
        "fav_is_p1":   fav_is_p1,
        "h2h_wins":    h2h_wins,
        "h2h_total":   h2h["total"],
        "fav_won_set1": fav_won_set1,
        "set1_score":  set1_score,
        "bet_type":    bet_type,
    }

# ─────────────────────────────────────────────────────────────
# FORMATAGE ALERTE
# ─────────────────────────────────────────────────────────────

def format_alert(a: dict) -> str:
    comp = COMP_LABELS.get(a["competition"], a["competition"])

    if a["favorite"] == a["player1"]:
        fav_odds_str = str(a["fav_odds"])
        und_odds_str = str(a["und_odds"])
    else:
        fav_odds_str = str(a["fav_odds"])
        und_odds_str = str(a["und_odds"])

    p1_odds = fav_odds_str if a["favorite"] == a["player1"] else und_odds_str
    p2_odds = fav_odds_str if a["favorite"] == a["player2"] else und_odds_str

    p1_str = f"<b>{a['player1']} ({p1_odds})</b>"
    p2_str = f"<b>{a['player2']} ({p2_odds})</b>"

    pari_label = SET1_ALERT_LABEL if a["bet_type"] == "SET1" else MATCH_ALERT_LABEL

    return (
        f"<b>{comp}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{p1_str} vs {p2_str}\n"
        f"{a['time']} UTC\n\n"
        f"<b>PARI : {a['favorite']} - {pari_label}</b>\n\n"
        f"H2H : {a['h2h_wins']}/{a['h2h_total']}\n"
        f"Set 1 dernier H2H : {a['set1_score']}"
    )

# ─────────────────────────────────────────────────────────────
# TRAITEMENT ALERTE
# ─────────────────────────────────────────────────────────────

def process_alert(match: dict):
    if match["id"] in alerted:
        return

    verdict = analyze_match(match)
    if not verdict:
        return

    o1, o2 = match["odds"]
    fav_odds = o1 if verdict["fav_is_p1"] else o2
    und_odds = o2 if verdict["fav_is_p1"] else o1

    if not IGNORE_ODDS_FILTER and not (MIN_FAVORITE_ODDS <= fav_odds <= MAX_FAVORITE_ODDS):
        log.info(f"Cote hors filtre ({fav_odds}) — match ignoré : {match['player1']} vs {match['player2']}")
        return

    analysis = {
        **match,
        "favorite":   verdict["fav_name"],
        "fav_odds":   fav_odds,
        "und_odds":   und_odds,
        "bet_type":   verdict["bet_type"],
        "h2h_wins":   verdict["h2h_wins"],
        "h2h_total":  verdict["h2h_total"],
        "set1_score": verdict["set1_score"],
    }

    send_telegram(format_alert(analysis), ALERT_DESTINATIONS)
    alerted.add(match["id"])
    tracking[match["id"]] = analysis
    log.info(f"Alerte envoyée : {match['player1']} vs {match['player2']} — {verdict['bet_type']}")

# ─────────────────────────────────────────────────────────────
# VÉRIFICATION RÉSULTATS
# ─────────────────────────────────────────────────────────────

def check_results():
    if not tracking:
        return
    bilan   = load_bilan()
    soup    = fetch_page(f"{BASE_URL}/games/")
    if not soup:
        return
    content = soup.get_text().lower()
    changed = False

    for mid, a in list(tracking.items()):
        if mid in seen:
            continue
        if a["player1"].split()[0].lower() not in content:
            continue
        # Logique de résultat à compléter selon structure de la page /games/
        seen.add(mid)
        changed = True

    if changed:
        save_bilan(bilan)

# ─────────────────────────────────────────────────────────────
# BOUCLE PRINCIPALE
# ─────────────────────────────────────────────────────────────

def run():
    if STARTUP_MESSAGE_ENABLED:
        send_telegram("<b>🚀 Bot Setka Cup Ready</b>", ALERT_DESTINATIONS)

    while True:
        try:
            for comp in COMPETITIONS:
                matches = fetch_matches_today(comp)
                log.info(f"[{COMP_LABELS.get(comp, comp)}] {len(matches)} match(s) trouvé(s) aujourd'hui")
                for match in matches:
                    process_alert(match)
            check_results()
        except Exception as e:
            log.error(f"Loop Error: {e}", exc_info=True)

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
