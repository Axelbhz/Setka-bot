# ===============================================================
#   CONFIGURATION — SETKA CUP BETTING BOT
# ===============================================================

# ── Telegram ────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = "8689450824:AAE9NAsDGI9CdLP6qcOvQ3pM5kMnN_6jb-Y"

ALERT_DESTINATIONS = [
    "406477026",
    "-1003521219534",
]

# ── Competitions surveillees ────────────────────────────────────
COMPETITIONS = [
    "setka_cup_cz",
    "setka_cup",
    # "pro_league_cz",
    # "tt_cup_cz",
]

# ── Filtre cotes favori ─────────────────────────────────────────
MIN_FAVORITE_ODDS = 1.25
MAX_FAVORITE_ODDS = 1.70

# ── Options de filtrage ─────────────────────────────────────────
IGNORE_ODDS_FILTER = True
REQUIRE_FAVORITE   = False

# Filtre marge de securite (Friction Zero)
# True  = ignore les matchs ou le favori a gagne le set1 avec ecart faible
# False = desactive (mode observation)
STRICT_DOMINATION_FILTER  = False
MIN_POINT_DIFF_LAST_SET1  = 3  # Ecart minimal en points (3 = 11-8 ou mieux)

# ── Parametres H2H ─────────────────────────────────────────────
MIN_H2H_MATCHES = 1
MIN_WIN_RATE    = 0.60

# ── Labels ──────────────────────────────────────────────────────
SET1_ALERT_LABEL  = "WIN 1er SET"
MATCH_ALERT_LABEL = "WIN MATCH"

# ── Systemes & Bilans ───────────────────────────────────────────
STARTUP_MESSAGE_ENABLED = True
ENABLE_DAILY_RECAP      = True
CHECK_INTERVAL_MINUTES  = 1

# ── Compatibilite ───────────────────────────────────────────────
SET2_ALERT_LABEL      = "WIN 2eme SET"
DISABLE_SET2_RECOVERY = True
RECAP_INTERVAL_HOURS  = 24
RECAP_WINDOW_HOURS    = 2
RECAP_DESTINATIONS    = ALERT_DESTINATIONS
