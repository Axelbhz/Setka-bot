# ═══════════════════════════════════════════════════════════════
#  CONFIGURATION — SETKA CUP BETTING BOT
# ═══════════════════════════════════════════════════════════════

# ── Telegram Bot Token ──────────────────────────────────────────
TELEGRAM_BOT_TOKEN = "8689450824:AAE9NAsDGI9CdLP6qcOvQ3pM5kMnN_6jb-Y"

# ── Section A : Destinations des ALERTES PARIS ─────────────────
ALERT_DESTINATIONS = [
    "406477026",   # Ton ID perso
    "-1003521219534",           # Ton canal privé
]

# ── Section B : Destinations du RÉCAP TOUTES LES 2H ────────────
RECAP_DESTINATIONS = [
    "406477026",   # Ton ID perso
    # "@nom_de_ton_canal_prive",         # Décommente si tu veux aussi dans le canal
]
RECAP_INTERVAL_HOURS = 2   # Récap toutes les 2 heures
RECAP_WINDOW_HOURS   = 2   # Matchs dans les X heures à venir

# ── Compétitions surveillées ────────────────────────────────────
COMPETITIONS = [
    "setka_cup_cz",        # Setka Cup République Tchèque ✅
    # "setka_cup_ukraine",
    # "setka_cup_intl",
    # "liga_pro_russia",
    # "tt_star_series",
    # "pro_league_cz",
]

# ── Filtre cotes favori ─────────────────────────────────────────
MIN_FAVORITE_ODDS = 1.25
MAX_FAVORITE_ODDS = 1.70

# ── Option 1 : IGNORER le filtre de cotes ──────────────────────
# False (défaut) = cote entre MIN et MAX requise
# True           = alerte même si hors fenêtre (mode analyse)
IGNORE_ODDS_FILTER = True

# ── Option 2 : DÉSACTIVER la récupération sur le 2ème set ──────
# False (défaut) = alerte set 2 si set 1 perdu
# True           = jamais d'alerte set 2 (mode analyse)
DISABLE_SET2_RECOVERY = False

# ── Option 3 : EXIGER UN FAVORI CLAIR ──────────────────────────
# True  (défaut) = ignore les matchs sans favori 
# False          = analyse tous les matchs même sans favori marqué
REQUIRE_FAVORITE = True

# ── Filtre H2H ──────────────────────────────────────────────────
MIN_H2H_MATCHES = 1
MIN_WIN_RATE    = 0.60

# ── Intervalle de scan alertes ──────────────────────────────────
CHECK_INTERVAL_MINUTES = 3

# ── Labels des alertes ──────────────────────────────────────────
SET1_ALERT_LABEL = "WIN 1er SET"
SET2_ALERT_LABEL = "WIN 2ème SET"
