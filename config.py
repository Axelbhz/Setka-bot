# ═══════════════════════════════════════════════════════════════
#  CONFIGURATION — SETKA CUP BETTING BOT
# ═══════════════════════════════════════════════════════════════

# ── Telegram ────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = "METS_TON_BOT_TOKEN_ICI"

TELEGRAM_DESTINATIONS = [
    "METS_TON_CHAT_ID_PERSONNEL_ICI",   # Ton ID perso
    "@nom_de_ton_canal_prive",           # Ton canal privé
]

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
IGNORE_ODDS_FILTER = False

# ── Option 2 : DÉSACTIVER la récupération sur le 2ème set ──────
# False (défaut) = alerte set 2 si set 1 perdu
# True           = jamais d'alerte set 2 (mode analyse)
DISABLE_SET2_RECOVERY = False

# ── Option 3 : EXIGER UN FAVORI CLAIR ──────────────────────────
# True  (défaut) = ignore les matchs sans favori (écart cotes < 0.05)
#                  → ne perd pas de temps à analyser les 50/50
# False          = analyse tous les matchs même sans favori marqué
REQUIRE_FAVORITE = True

# ── Filtre H2H ──────────────────────────────────────────────────
MIN_H2H_MATCHES = 3
MIN_WIN_RATE    = 0.60

# ── Intervalle de scan ──────────────────────────────────────────
CHECK_INTERVAL_MINUTES = 3

# ── Labels des alertes ──────────────────────────────────────────
SET1_ALERT_LABEL = "WIN 1er SET"
SET2_ALERT_LABEL = "WIN 2ème SET"
