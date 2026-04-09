import requests
import time
import logging
import re
from bs4 import BeautifulSoup
from config import *

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

def get_h2h_data(p1_name, p2_name):
    """Va chercher les stats réelles sur score-tennis pour ces deux joueurs"""
    try:
        # On simule la recherche de la page H2H simplifiée
        search_url = f"https://score-tennis.com/up-games/"
        r = requests.get(search_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        
        # On cherche le bloc qui contient les deux noms
        for block in soup.find_all("div", class_="game_block"): # Sélecteur générique
            text = block.get_text()
            if p1_name.split()[-1] in text and p2_name.split()[-1] in text:
                # Extraction H2H
                h2h_m = re.search(r'(\d+)\s*:\s*(\d+)\s*score of recent face-to-face', text)
                if not h2h_m: continue
                
                h2h_p1, h2h_p2 = int(h2h_m.group(1)), int(h2h_m.group(2))
                total = h2h_p1 + h2h_p2
                if total < 1: continue

                # Extraction dernier set (première ligne du tableau)
                rows = block.select("table tr")
                for row in rows:
                    cols = [td.get_text(strip=True) for td in row.find_all("td")]
                    if len(cols) < 6 or not re.match(r'\d{2}\.\d{2}', cols[0]): continue
                    return {
                        "h2h_p1": h2h_p1, "h2h_p2": h2h_p2, "total": total,
                        "last_s1_p1": int(cols[2]), "last_s1_p2": int(cols[3])
                    }
    except: pass
    return None

def run():
    log.info("=== BOT ANALYSEUR 60% + DERNIER MATCH + SET 1 ===")
    seen_ids = set()

    while True:
        matches = requests.get(f"https://api.the-odds-api.com/v4/sports/upcoming/odds/?apiKey={ODDS_API_KEY}&regions=eu").json()
        
        for m in matches:
            mid = m['id']
            if mid in seen_ids or "table tennis" not in m.get('sport_title', '').lower(): continue
            
            p1, p2 = m['home_team'], m['away_team']
            stats = get_h2h_data(p1, p2)
            
            if stats:
                # Logique : Favori 60% + Gagné dernier match + Gagné dernier Set 1
                winrate_p1 = stats["h2h_p1"] / stats["total"]
                is_p1_fav = winrate_p1 >= 0.60
                is_p2_fav = winrate_p1 <= 0.40
                
                # Le favori a-t-il gagné le dernier match (donc le dernier Set 1 dans notre flux) ?
                success = False
                if is_p1_fav and stats["last_s1_p1"] > stats["last_s1_p2"]: success = True
                if is_p2_fav and stats["last_s1_p2"] > stats["last_s1_p1"]: success = True

                if success:
                    fav = p1 if is_p1_fav else p2
                    wr = winrate_p1 if is_p1_fav else (stats["h2h_p2"]/stats["total"])
                    msg = (f"<b>🎯 PRONO : {fav} - {SET1_ALERT_LABEL}</b>\n"
                           f"━━━━━━━━━━━━━━━━━━━━\n"
                           f"🔥 {p1} vs {p2}\n\n"
                           f"• Winrate H2H : {wr*100:.0f}%\n"
                           f"• Dernier match & Set 1 : ✅ GAGNÉ")
                    
                    for dest in ALERT_DESTINATIONS:
                        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", 
                                      json={"chat_id": dest, "text": msg, "parse_mode": "HTML"})
                    seen_ids.add(mid)

        time.sleep(300)

if __name__ == "__main__":
    run()
