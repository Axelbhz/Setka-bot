#!/usr/bin/env python3
"""
Setka Cup Betting Bot
─────────────────────
Section A : Alertes paris (continu, toutes les 3 min)
Section B : Récap matchs filtrés (toutes les 2h)
Bilan      : Quotidien et hebdomadaire à minuit
"""

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

# ─── Logging ──────────────────────────────────────────────────────────────────
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
        "last_reset_daily": "",
        "last_reset_weekly": "",
    }

def save_bilan(b: dict):
    BILAN_FILE.write_text(json.dumps(b, indent=2))

# ─── Mémoire session ──────────────────────────────────────────────────────────
alerted_set1  = set()
alerted_set2  = set()
live_tracking = {}   # {match_id: analysis_dict}

SOFASCORE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://www.sofascore.com/",
    "Origin": "https://www.sofascore.com",
}

SOFASCORE_TOURNAMENT_IDS = {
    "setka_cup_cz":      15006,   # Setka Cup Czech Republic
    "setka_cup_ukraine": 15004,   # Setka Cup Men (Ukraine)
    "setka_cup_intl":    15004,   # Setka Cup International
    "liga_pro_russia":   15003,   # Liga Pro Russia
    "tt_star_series":    15008,   # TT Star Series
    "pro_league_cz":     15005,   # TT Cup / Pro League CZ
}

COMP_LABELS = {
    "setka_cup_cz":      "Setka Cup 🇨🇿",
    "setka_cup_ukraine": "Setka Cup 🇺🇦",
    "setka_cup_intl":    "Setka Cup 🌍",
    "liga_pro_russia":   "Liga Pro 🇷🇺",
    "tt_star_series":    "TT Star Series ⭐",
    "pro_league_cz":     "Pro League 🇨🇿",
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

# ─── Sofascore : matchs du jour ───────────────────────────────────────────────

def fetch_all_today_events() -> dict:
    """Retourne tous les événements du jour groupés par tournament_id."""
    try:
        url = "https://api.sofascore.com/api/v1/sport/table-tennis/scheduled-events/today"
        r = requests.get(url, headers=SOFASCORE_HEADERS, timeout=15)
        if r.status_code != 200:
            log.warning(f"Sofascore today: {r.status_code}")
            return {}
        events = r.json().get("events", [])
        grouped = {}
        for e in events:
            tid = e.get("tournament", {}).get("uniqueTournament", {}).get("id")
            if tid:
                grouped.setdefault(tid, []).append(e)
        return grouped
    except Exception as ex:
        log.error(f"fetch_all_today_events: {ex}")
        return {}

def parse_event(event: dict, competition_key: str) -> dict:
    """Transforme un event Sofascore en dict standard."""
    home = event.get("homeTeam", {}).get("name", "?")
    away = event.get("awayTeam", {}).get("name", "?")
    eid  = str(event.get("id", f"{home}_{away}"))
    ts   = event.get("startTimestamp", 0)
    match_time = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M") if ts else "?"
    odds = fetch_sofascore_odds(eid)
    return {
        "id": eid,
        "player1": home,
        "player2": away,
        "time": match_time,
        "timestamp": ts,
        "competition": competition_key,
        "odds": odds,
        "status": event.get("status", {}).get("type", "notstarted"),
    }

def fetch_upcoming_matches(competition_key: str, all_events: dict) -> list:
    tid     = SOFASCORE_TOURNAMENT_IDS.get(competition_key)
    events  = all_events.get(tid, [])
    matches = []
    for e in events:
        status = e.get("status", {}).get("type", "")
        if status in ("finished", "inprogress"):
            continue
        matches.append(parse_event(e, competition_key))
    log.info(f"[{competition_key}] {len(matches)} matchs à venir")
    return matches

# ─── Sofascore : cotes ────────────────────────────────────────────────────────

def fetch_sofascore_odds(event_id: str) -> list:
    try:
        url = f"https://api.sofascore.com/api/v1/event/{event_id}/odds/1/all"
        r   = requests.get(url, headers=SOFASCORE_HEADERS, timeout=10)
        if r.status_code != 200:
            return []
        for market in r.json().get("markets", []):
            if market.get("marketName") in ["Full time", "Match winner", "Winner"]:
                o1 = o2 = None
                for c in market.get("choices", []):
                    if c.get("name") in ["1", "Home"]:
                        try: o1 = float(c.get("decimalValue") or c.get("fractionalValue") or 0)
                        except: pass
                    elif c.get("name") in ["2", "Away"]:
                        try: o2 = float(c.get("decimalValue") or c.get("fractionalValue") or 0)
                        except: pass
                if o1 and o2:
                    return [o1, o2]
        return []
    except Exception as e:
        log.debug(f"Odds error {event_id}: {e}")
        return []

# ─── Sofascore : H2H ─────────────────────────────────────────────────────────

def fetch_h2h_sofascore(event_id: str) -> dict:
    try:
        url = f"https://api.sofascore.com/api/v1/event/{event_id}/h2h"
        r   = requests.get(url, headers=SOFASCORE_HEADERS, timeout=10)
        if r.status_code != 200:
            return {"p1_wins": 0, "p2_wins": 0, "total": 0}
        p1_wins = p2_wins = 0
        for e in r.json().get("previousEvents", []):
            wc = e.get("winnerCode")
            if wc == 1: p1_wins += 1
            elif wc == 2: p2_wins += 1
        return {"p1_wins": p1_wins, "p2_wins": p2_wins, "total": p1_wins + p2_wins}
    except Exception as e:
        log.debug(f"H2H error {event_id}: {e}")
        return {"p1_wins": 0, "p2_wins": 0, "total": 0}

# ─── Sofascore : Forme récente ────────────────────────────────────────────────

def fetch_recent_form_sofascore(event_id: str, is_home: bool) -> dict:
    """
    Retourne :
      wins, losses, last5, last_match_won,
      won_after_losing_set, wins_excluding_last, matches_today
    """
    empty = {
        "wins": 0, "losses": 0, "last5": "?????",
        "last_match_won": False, "won_after_losing_set": False,
        "wins_excluding_last": 0, "matches_today": 0,
    }
    try:
        # Récupérer l'ID de l'équipe depuis l'event
        url_event = f"https://api.sofascore.com/api/v1/event/{event_id}"
        r = requests.get(url_event, headers=SOFASCORE_HEADERS, timeout=10)
        if r.status_code != 200:
            return empty
        event_data = r.json().get("event", {})
        team = event_data.get("homeTeam" if is_home else "awayTeam", {})
        team_id = team.get("id")
        if not team_id:
            return empty

        # Derniers matchs du joueur
        url_last = f"https://api.sofascore.com/api/v1/team/{team_id}/events/last/0"
        r2 = requests.get(url_last, headers=SOFASCORE_HEADERS, timeout=10)
        if r2.status_code != 200:
            return empty

        events  = r2.json().get("events", [])[:6]  # 6 pour exclure le dernier
        results = []
        today   = datetime.now(tz=timezone.utc).date()
        matches_today = 0
        won_after_losing_set = False

        for ev in events:
            home_id = ev.get("homeTeam", {}).get("id")
            wc      = ev.get("winnerCode")
            ts      = ev.get("startTimestamp", 0)
            ev_date = datetime.fromtimestamp(ts, tz=timezone.utc).date() if ts else None

            if ev_date == today:
                matches_today += 1

            is_home_ev = (home_id == team_id)
            won = (wc == 1) if is_home_ev else (wc == 2)
            results.append("V" if won else "D")

            # Résilience : a-t-il gagné après avoir perdu un set ?
            hs = ev.get("homeScore", {})
            as_ = ev.get("awayScore", {})
            if is_home_ev:
                p1s = [hs.get(f"period{i}", 0) for i in range(1, 4)]
                p2s = [as_.get(f"period{i}", 0) for i in range(1, 4)]
            else:
                p1s = [as_.get(f"period{i}", 0) for i in range(1, 4)]
                p2s = [hs.get(f"period{i}", 0) for i in range(1, 4)]

            sets_lost = sum(1 for a, b in zip(p1s, p2s) if b > a)
            if won and sets_lost >= 1:
                won_after_losing_set = True

        last5  = results[:5]
        wins   = last5.count("V")
        losses = last5.count("D")
        last_match_won      = results[0] == "V" if results else False
        wins_excluding_last = last5[1:].count("V") if len(last5) > 1 else 0

        return {
            "wins": wins,
            "losses": losses,
            "last5": "".join(last5),
            "last_match_won": last_match_won,
            "won_after_losing_set": won_after_losing_set,
            "wins_excluding_last": wins_excluding_last,
            "matches_today": matches_today,
        }

    except Exception as e:
        log.debug(f"Form error {event_id} is_home={is_home}: {e}")
        return empty

# ─── Logique d'analyse (Axel) ─────────────────────────────────────────────────

def analyze_match_logic(match: dict) -> dict | None:
    """
    Logique d'analyse structurelle indépendante des filtres de cotes.
    Retourne le parieur potentiel et la confiance, ou None si pas de signal.
    """
    p1_form = fetch_recent_form_sofascore(match["id"], True)
    p2_form = fetch_recent_form_sofascore(match["id"], False)
    h2h     = fetch_h2h_sofascore(match["id"])

    score_p1 = 50

    # ── PILIER A : Ascendant psychologique (H2H) ────────────────
    if h2h["total"] >= 1:
        win_rate_p1 = h2h["p1_wins"] / h2h["total"]
        score_p1 += int((win_rate_p1 - 0.5) * 60)  # -30 à +30

    # ── PILIER B : Momentum (dernier match) ─────────────────────
    if p1_form["last_match_won"]:  score_p1 += 10
    if p2_form["last_match_won"]:  score_p1 -= 10

    # ── PILIER C : Résilience (gagné après avoir perdu un set) ──
    if p1_form.get("won_after_losing_set"): score_p1 += 5
    if p2_form.get("won_after_losing_set"): score_p1 -= 5

    # ── PILIER D : Forme globale (hors dernier match) ───────────
    score_p1 += (p1_form["wins_excluding_last"] * 4)
    score_p1 -= (p2_form["wins_excluding_last"] * 4)

    # ── PILIER E : Fatigue (matchs joués aujourd'hui) ───────────
    diff = p1_form.get("matches_today", 0) - p2_form.get("matches_today", 0)
    score_p1 -= (diff * 5)

    score_p1 = max(0, min(score_p1, 100))

    if score_p1 >= 75:
        return {"bet_on": "player1", "name": match["player1"], "confidence": score_p1}
    elif score_p1 <= 25:
        return {"bet_on": "player2", "name": match["player2"], "confidence": 100 - score_p1}

    return None

# ─── Filtres rapides (communs Section A et B) ─────────────────────────────────

def quick_filter(match: dict) -> tuple[bool, float, float, str, str]:
    """
    Applique les filtres rapides (favori clair + fenêtre cotes).
    Retourne (passed, fav_odds, und_odds, favorite_name, underdog_name).
    """
    odds = match.get("odds", [])
    if len(odds) < 2:
        return False, 0, 0, "", ""

    o1, o2 = odds[0], odds[1]

    # Option 3 : favori clair requis
    if REQUIRE_FAVORITE and min(o1, o2) > MAX_FAVORITE_ODDS:
        return False, 0, 0, "", ""

    if o1 <= o2:
        fav_name, und_name = match["player1"], match["player2"]
        fav_odds, und_odds = o1, o2
    else:
        fav_name, und_name = match["player2"], match["player1"]
        fav_odds, und_odds = o2, o1

    # Option 1 : filtre fenêtre de cotes
    if not IGNORE_ODDS_FILTER:
        if not (MIN_FAVORITE_ODDS <= fav_odds <= MAX_FAVORITE_ODDS):
            return False, 0, 0, "", ""

    return True, fav_odds, und_odds, fav_name, und_name

# ─── Section A : Analyse complète pour alertes paris ─────────────────────────

def process_alert(match: dict):
    mid = match["id"]
    if mid in alerted_set1:
        return

    odds = match.get("odds", [])
    if len(odds) < 2:
        return

    o1, o2 = odds[0], odds[1]

    # ── Option 3 : filtre d'entrée — match avec favori ? ────────
    if REQUIRE_FAVORITE and min(o1, o2) > MAX_FAVORITE_ODDS:
        return

    # ── Analyse structurelle (indépendante des cotes) ────────────
    verdict = analyze_match_logic(match)
    if not verdict:
        return

    # Cote du joueur désigné par l'analyse
    if verdict["bet_on"] == "player1":
        fav_name, und_name = match["player1"], match["player2"]
        fav_odds = o1
    else:
        fav_name, und_name = match["player2"], match["player1"]
        fav_odds = o2

    # ── Option 1 : filtre de sortie — cote du gagnant désigné ───
    odds_in_window = MIN_FAVORITE_ODDS <= fav_odds <= MAX_FAVORITE_ODDS
    if not IGNORE_ODDS_FILTER and not odds_in_window:
        return

    analysis = {
        "match_id":       mid,
        "player1":        match["player1"],
        "player2":        match["player2"],
        "favorite":       fav_name,
        "underdog":       und_name,
        "fav_odds":       fav_odds,
        "confidence":     verdict["confidence"],
        "time":           match["time"],
        "competition":    match["competition"],
        "odds_in_window": odds_in_window,
    }

    send_telegram(format_set1_alert(analysis), ALERT_DESTINATIONS)
    alerted_set1.add(mid)
    live_tracking[mid] = analysis
    log.info(f"✅ Alerte set1: {fav_name} vs {und_name}")

# ─── Section A : Surveillance set 1 perdu ────────────────────────────────────

def check_live_results(all_events: dict):
    """Vérifie les matchs en cours pour set1 perdu et bilan."""
    bilan = load_bilan()

    for comp in COMPETITIONS:
        tid    = SOFASCORE_TOURNAMENT_IDS.get(comp)
        events = all_events.get(tid, [])

        for e in events:
            eid    = str(e.get("id", ""))
            status = e.get("status", {}).get("type", "")

            # ── Bilan : match terminé ────────────────────────────
            if status == "finished" and eid in live_tracking:
                a  = live_tracking[eid]
                wc = e.get("winnerCode")
                home_name = e.get("homeTeam", {}).get("name", "")
                fav_is_home = a["favorite"].lower() in home_name.lower()
                fav_won = (wc == 1) if fav_is_home else (wc == 2)

                hs  = e.get("homeScore", {})
                as_ = e.get("awayScore", {})
                p1s1 = hs.get("period1", 0)
                p2s1 = as_.get("period1", 0)
                fav_won_set1 = (p1s1 > p2s1) if fav_is_home else (p2s1 > p1s1)

                if eid in alerted_set1 and eid not in alerted_set2:
                    if fav_won_set1:
                        bilan["set1"]["won"] += 1
                        bilan["week"]["set1"]["won"] += 1
                    else:
                        bilan["set1"]["lost"] += 1
                        bilan["week"]["set1"]["lost"] += 1

                if eid in alerted_set2:
                    fav_won_set2 = fav_won  # simplification
                    if fav_won_set2:
                        bilan["set2"]["won"] += 1
                        bilan["week"]["set2"]["won"] += 1
                    else:
                        bilan["set2"]["lost"] += 1
                        bilan["week"]["set2"]["lost"] += 1

                live_tracking.pop(eid, None)

            # ── Alerte set 2 ─────────────────────────────────────
            if (
                status == "inprogress"
                and eid in live_tracking
                and eid not in alerted_set2
                and not DISABLE_SET2_RECOVERY
            ):
                a  = live_tracking[eid]
                hs = e.get("homeScore", {})
                as_ = e.get("awayScore", {})
                home_name   = e.get("homeTeam", {}).get("name", "")
                fav_is_home = a["favorite"].lower() in home_name.lower()
                p1s1 = hs.get("period1", 0)
                p2s1 = as_.get("period1", 0)
                set1_done = (p1s1 + p2s1) > 0
                fav_lost_set1 = set1_done and ((p1s1 < p2s1) if fav_is_home else (p2s1 < p1s1))

                if fav_lost_set1:
                    send_telegram(format_set2_alert(a), ALERT_DESTINATIONS)
                    alerted_set2.add(eid)
                    log.info(f"⚠️ Alerte set2: {a['favorite']}")

    save_bilan(bilan)

# ─── Section B : Récap toutes les 2h ─────────────────────────────────────────

def send_recap(all_events: dict):
    """Envoie la liste des matchs à venir dans les 2h qui passent les filtres."""
    now      = datetime.now(tz=timezone.utc)
    horizon  = now + timedelta(hours=RECAP_WINDOW_HOURS)
    grouped  = {}  # {comp_label: [lines]}

    for comp in COMPETITIONS:
        tid    = SOFASCORE_TOURNAMENT_IDS.get(comp)
        events = all_events.get(tid, [])
        label  = COMP_LABELS.get(comp, comp)
        lines  = []

        for e in events:
            status = e.get("status", {}).get("type", "")
            if status in ("finished", "inprogress"):
                continue

            ts = e.get("startTimestamp", 0)
            if not ts:
                continue
            match_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            if not (now <= match_dt <= horizon):
                continue

            match = parse_event(e, comp)
            passed, fav_odds, _, fav_name, _ = quick_filter(match)
            if not passed:
                continue

            match_time = match_dt.strftime("%H:%M")
            lines.append(f"  🕐 {match_time} — {match['player1']} vs {match['player2']} → <b>{fav_name}</b> ({fav_odds})")

        if lines:
            grouped[label] = lines

    if not grouped:
        log.info("Récap : aucun match filtré dans les 2h à venir")
        return

    total = sum(len(v) for v in grouped.values())
    msg   = f"📋 <b>MATCHS À VENIR — {now.strftime('%H:%M')} UTC</b>\n"
    msg  += f"━━━━━━━━━━━━━━━━━━━━\n"
    for label, lines in grouped.items():
        msg += f"\n🏆 <b>{label}</b>\n"
        msg += "\n".join(lines) + "\n"
    msg += f"\n━━━━━━━━━━━━━━━━━━━━\n{total} opportunité(s) dans les {RECAP_WINDOW_HOURS}h à venir"

    send_telegram(msg, RECAP_DESTINATIONS)
    log.info(f"📋 Récap envoyé : {total} matchs")

# ─── Bilans ───────────────────────────────────────────────────────────────────

def pct(won, lost):
    total = won + lost
    return f"{int(won/total*100)}%" if total > 0 else "N/A"

def format_bilan(b: dict, period: str) -> str:
    s1 = b["set1"] if period == "daily" else b["week"]["set1"]
    s2 = b["set2"] if period == "daily" else b["week"]["set2"]
    label = "📅 BILAN QUOTIDIEN" if period == "daily" else "📆 BILAN HEBDOMADAIRE"
    return (
        f"{label}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🏓 <b>SET 1</b>\n"
        f"  ✅ Gagné  : {s1['won']}  ({pct(s1['won'], s1['lost'])})\n"
        f"  ❌ Perdu  : {s1['lost']}  ({pct(s1['lost'], s1['won'])})\n\n"
        f"🔄 <b>SET 2 (récupération)</b>\n"
        f"  ✅ Gagné  : {s2['won']}  ({pct(s2['won'], s2['lost'])})\n"
        f"  ❌ Perdu  : {s2['lost']}  ({pct(s2['lost'], s2['won'])})\n"
    )

def bilan_thread():
    """Thread dédié aux bilans — tourne en parallèle."""
    last_daily  = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    last_weekly = datetime.now(tz=timezone.utc).strftime("%Y-W%W")

    while True:
        now  = datetime.now(tz=timezone.utc)
        date = now.strftime("%Y-%m-%d")
        week = now.strftime("%Y-W%W")

        # Bilan quotidien à minuit
        if now.hour == 0 and now.minute == 0 and date != last_daily:
            bilan = load_bilan()
            send_telegram(format_bilan(bilan, "daily"), ALERT_DESTINATIONS + RECAP_DESTINATIONS)
            bilan["set1"] = {"won": 0, "lost": 0}
            bilan["set2"] = {"won": 0, "lost": 0}
            bilan["last_reset_daily"] = date
            save_bilan(bilan)
            last_daily = date
            log.info("📅 Bilan quotidien envoyé")

        # Bilan hebdomadaire le dimanche à minuit
        if now.weekday() == 6 and now.hour == 0 and now.minute == 0 and week != last_weekly:
            bilan = load_bilan()
            send_telegram(format_bilan(bilan, "weekly"), ALERT_DESTINATIONS + RECAP_DESTINATIONS)
            bilan["week"] = {"set1": {"won": 0, "lost": 0}, "set2": {"won": 0, "lost": 0}}
            bilan["last_reset_weekly"] = week
            save_bilan(bilan)
            last_weekly = week
            log.info("📆 Bilan hebdomadaire envoyé")

        time.sleep(60)

# ─── Formats messages alertes ─────────────────────────────────────────────────

def format_set1_alert(a: dict) -> str:
    comp      = COMP_LABELS.get(a["competition"], a["competition"])
    odds_note = "\n⚠️ <i>Mode analyse : cote hors fenêtre normale</i>" if (IGNORE_ODDS_FILTER and not a["odds_in_window"]) else ""
    return (
        f"🏓 <b>{comp}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚔️  {a['player1']} vs {a['player2']}\n"
        f"🕐  {a['time']}\n\n"
        f"📊 <b>ANALYSE</b>\n"
        f"• Favori structural : <b>{a['favorite']}</b>\n"
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
        f"⚔️  {a['player1']} vs {a['player2']}\n\n"
        f"🔄 <b>OPTION : {SET2_ALERT_LABEL} — {a['favorite']}</b>\n"
        f"💰 Cote : vérifier sur 1xbet\n\n"
        f"⚡ <i>Si perd encore → on laisse</i>"
    )

# ─── Boucle principale ───────────────────────────────────────────────────────

def run():
    log.info("🚀 Bot démarré")

    opts = [
        f"• Favori clair requis : {'✅' if REQUIRE_FAVORITE else '❌ désactivé'}",
        f"• Filtre cotes [{MIN_FAVORITE_ODDS}-{MAX_FAVORITE_ODDS}] : {'❌ désactivé' if IGNORE_ODDS_FILTER else '✅'}",
        f"• Récupération set 2 : {'❌ désactivée' if DISABLE_SET2_RECOVERY else '✅'}",
        f"• Compétitions : {', '.join(COMPETITIONS)}",
        f"• Alertes toutes les {CHECK_INTERVAL_MINUTES} min",
        f"• Récap toutes les {RECAP_INTERVAL_HOURS}h",
        f"• Bilans : quotidien + hebdomadaire à minuit",
    ]
    send_telegram(
        "🤖 <b>Bot Setka Cup démarré</b>\n━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(opts),
        ALERT_DESTINATIONS
    )

    # Thread bilans en parallèle
    t = threading.Thread(target=bilan_thread, daemon=True)
    t.start()

    last_recap = datetime.now(tz=timezone.utc) - timedelta(hours=RECAP_INTERVAL_HOURS)

    while True:
        try:
            all_events = fetch_all_today_events()
            now        = datetime.now(tz=timezone.utc)

            # ── Section A : alertes paris ─────────────────────────
            for comp in COMPETITIONS:
                matches = fetch_upcoming_matches(comp, all_events)
                for match in matches:
                    process_alert(match)

            # ── Section A : check live (set2 + bilan) ────────────
            check_live_results(all_events)

            # ── Section B : récap toutes les 2h ──────────────────
            if (now - last_recap).total_seconds() >= RECAP_INTERVAL_HOURS * 3600:
                send_recap(all_events)
                last_recap = now

        except Exception as e:
            log.error(f"Erreur boucle: {e}")

        log.info(f"⏳ Pause {CHECK_INTERVAL_MINUTES} min...")
        time.sleep(CHECK_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    run()
