import requests
import time
import logging
from config import *

# Logging pour voir ce qui se passe dans la console
logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

def send_telegram(message: str):
    """Envoie le message à toutes les destinations configurées"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chat_id in ALERT_DESTINATIONS:
        try:
            payload = {
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML"
            }
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code == 200:
                log.info(f"✅ Message envoyé à {chat_id}")
            else:
                log.error(f"❌ Erreur Telegram sur {chat_id}: {r.text}")
        except Exception as e:
            log.error(f"⚠️ Erreur de connexion Telegram: {e}")

def get_api_matches():
    """Récupère les matchs et les cotes via The Odds API"""
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
        log.error(f"Erreur API Odds: {r.status_code}")
    except Exception as e:
        log.error(f"Erreur de connexion API: {e}")
    return []

def run_bot():
    log.info("🚀 Bot en ligne (Mode API + Multi-Destinations)")
    alerted_matches = set()

    while True:
        matches = get_api_matches()
        log.info(f"📡 Scan API : {len(matches)} matchs trouvés.")

        for m in matches:
            match_id = m['id']
            if match_id in alerted_matches:
                continue

            p1 = m['home_team']
            p2 = m['away_team']
            
            # --- BLOC ANALYSE (Simulation pour le test) ---
            # Ici on forcera le signal pour vérifier que l'envoi double marche
            log.info(f"Analyse en cours : {p1} vs {p2}")
            
            # Simulation : On génère un signal pour tester les deux destinations
            msg = (f"<b>🔔 NOUVEAU SIGNAL</b>\n"
                   f"━━━━━━━━━━━━━━━━━━━━\n"
                   f"<b>{p1}</b> vs <b>{p2}</b>\n\n"
                   f"🎯 Pari : {p1} - {SET1_ALERT_LABEL}")
            
            send_telegram(msg)
            alerted_matches.add(match_id)

        # On attend 5 minutes pour ne pas dépasser le quota gratuit
        log.info("⏳ En attente du prochain cycle (5 min)...")
        time.sleep(300)

if __name__ == "__main__":
    run_bot()
