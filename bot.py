import requests
import time
import logging
from config import *

# Configuration des Logs
logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot_api.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

def send_telegram(message: str):
    """Envoie l'alerte aux destinations définies dans config.py"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for dest in ALERT_DESTINATIONS:
        try:
            requests.post(url, json={
                "chat_id": dest, 
                "text": message, 
                "parse_mode": "HTML"
            }, timeout=10)
        except Exception as e:
            log.error(f"Erreur Telegram vers {dest}: {e}")

def get_api_matches():
    """Récupère les matchs via The Odds API (clé 'upcoming' pour éviter la 404)"""
    url = f"https://api.the-odds-api.com/v4/sports/upcoming/odds/"
    params = {
        'apiKey': ODDS_API_KEY,
        'regions': 'eu',
        'markets': 'h2h',
        'oddsFormat': 'decimal'
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code == 200:
            return r.json()
        log.error(f"Erreur API ({r.status_code}): {r.text}")
    except Exception as e:
        log.error(f"Erreur connexion API: {e}")
    return []

def run():
    log.info("=== 🚀 DEMARRAGE DU BOT (MODE API) ===")
    seen_ids = set()

    while True:
        try:
            matches = get_api_matches()
            log.info(f"Scan API : {len(matches)} matchs totaux trouvés.")

            for m in matches:
                mid = m['id']
                if mid in seen_ids: continue

                # Filtre : On ne garde que le Tennis de Table
                sport_title = m.get('sport_title', '')
                log.info(f"Sport détecté : {sport_title}")
                if "table tennis" in sport_title.lower():
                    p1, p2 = m['home_team'], m['away_team']
                    
                    msg = (f"<b>🏓 SIGNAL DÉTECTÉ : {sport_title}</b>\n"
                           f"━━━━━━━━━━━━━━━━━━━━\n"
                           f"<b>{p1}</b> vs <b>{p2}</b>\n\n"
                           f"🎯 <b>PARI : {p1} - {SET1_ALERT_LABEL}</b>")

                    send_telegram(msg)
                    seen_ids.add(mid)
                    log.info(f"✅ Alerte envoyée : {p1} vs {p2}")

        except Exception as e:
            log.error(f"Erreur dans la boucle: {e}")

        time.sleep(300)

if __name__ == "__main__":
    run()
