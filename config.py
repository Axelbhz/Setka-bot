# ═══════════════════════════════════════════════════════════════
#  CONFIGURATION — SETKA CUP BETTING BOT
# ═══════════════════════════════════════════════════════════════

# ── Telegram ────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = "METS_TON_BOT_TOKEN_ICI"

ALERT_DESTINATIONS = [
    "METS_TON_CHAT_ID_PERSONNEL_ICI",
    "@nom_de_ton_canal_prive",
]

# ── Compétitions surveillées ────────────────────────────────────
COMPETITIONS = [
    "setka_cup_cz",
    # "setka_cup_ukraine",
    # "setka_cup_intl",
    # "liga_pro_russia",
    # "pro_league_cz",
    # "tt_cup_cz",
]

# ── Filtre cotes favori ─────────────────────────────────────────
MIN_FAVORITE_ODDS = 1.25
MAX_FAVORITE_ODDS = 1.70

# ── Option 1 : IGNORER le filtre de cotes ──────────────────────
# False = cote entre MIN et MAX requise
# True  = alerte même si hors fenêtre (mode analyse)
IGNORE_ODDS_FILTER = True

# ── Option 2 : EXIGER UN FAVORI CLAIR ──────────────────────────
# True  = ignore les matchs sans favori (cote ≤ MAX_FAVORITE_ODDS)
# False = analyse tous les matchs
REQUIRE_FAVORITE = True

# ── Paramètres H2H ─────────────────────────────────────────────
MIN_H2H_MATCHES = 1
MIN_WIN_RATE    = 0.60

# ── Labels ──────────────────────────────────────────────────────
SET1_ALERT_LABEL  = "WIN 1er SET"
MATCH_ALERT_LABEL = "WIN MATCH"

# ── Paramètres inutilisés (compatibilité) ───────────────────────
CHECK_INTERVAL_MINUTES = 1
RECAP_INTERVAL_HOURS   = 2
RECAP_WINDOW_HOURS     = 2
SET2_ALERT_LABEL       = "WIN 2ème SET"
DISABLE_SET2_RECOVERY  = True
RECAP_DESTINATIONS     = []
