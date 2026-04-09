import requests
import time
import logging
from config import *

logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("bot_api.log", encoding="utf-8"), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for dest in ALERT_DESTINATIONS:
        try:
            requests.post(url, json={"chat_id": dest, "text": message, "parse_mode": "HTML"}, timeout=10)
        except Exception as e:
            log.error(f"Erreur Telegram vers {dest}: {e}")

def get_api_matches():
    url = f"https://api.the-odds-api.com/v4/sports/{SPORT_KEY}/odds/"
    params = {
        'apiKey': ODDS_API_KEY,
        'regions': 'eu',
        'markets': 'h2h',
        'oddsFormat': 'decimal'
    }
    try:
        r = requests.get(url, params=params)
        if r.status_code == 200:
            return r.json()
        log.error(f"Erreur API: {r.status_code}")
    except Exception as e:
        log.error(f"Erreur connexion: {e}")
    return []

def run():
    log.info("=== BOT SETKA CUP (MODE API) - PRÊT ===")
    seen_ids = set()

    while True:
        try:
            matches = get_api_matches()
            log.info(f"Scan API : {len(matches)} matchs de Ping-Pong trouvés.")

            for m in matches:
                mid = m['id']
                if mid in seen_ids: continue

                p1, p2 = m['home_team'], m['away_team']
                
                # Ici on filtre sur les ligues que tu aimes (Setka, TT Cup)
                league = m.get('sport_title', '')
                
                # Message direct pour le pari 1er Set
                msg = (f"<b>🏓 SIGNAL DETECTÉ : {league}</b>\n"
                       f"━━━━━━━━━━━━━━━━━━━━\n"
                       f"<b>{p1}</b> vs <b>{p2}</b>\n\n"
                       f"🎯 <b>PARI : {p1} - {SET1_ALERT_LABEL}</b>")

                send_telegram(msg)
                seen_ids.add(mid)
                log.info(f"✅ Alerte envoyée pour {p1} vs {p2}")

        except Exception as e:
            log.error(f"Erreur Loop: {e}")

        # On attend 5 min (300s) pour ne pas exploser le quota gratuit
        time.sleep(300)

if __name__ == "__main__":
    run()
