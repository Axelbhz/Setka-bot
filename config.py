# ═══════════════════════════════════════════════════════════════
#   CONFIGURATION — SETKA CUP BETTING BOT (V2 Évolutive)
# ═══════════════════════════════════════════════════════════════

# ── Telegram ────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = "8689450824:AAE9NAsDGI9CdLP6qcOvQ3pM5kMnN_6jb-Y"

ALERT_DESTINATIONS = [
    "406477026",
    "-1003521219534",
]

# ── Compétitions surveillées ────────────────────────────────────
# Il suffit de décommenter (#) pour activer une compétition.
# La protection contre les mauvais matchs sera gérée dans le code principal.
COMPETITIONS = [
    "setka_cup_cz",
     "setka_cup_ukraine",
    # "setka_cup_intl",
    # "liga_pro_russia",
    # "pro_league_cz",
    # "tt_cup_cz",
]

# ── Filtre cotes favori ─────────────────────────────────────────
MIN_FAVORITE_ODDS = 1.25
MAX_FAVORITE_ODDS = 1.70

# ── Options de Filtrage ─────────────────────────────────────────
IGNORE_ODDS_FILTER = False
REQUIRE_FAVORITE = True

# 🎯 CRITÈRE DE DOMINATION RÉELLE (Friction Zéro)
# Évite les matchs où le dernier Set 1 était trop serré (ex: 12-10)
STRICT_DOMINATION_FILTER = False
MIN_POINT_DIFF_LAST_SET1 = 3 # Écart minimal (3 = 11-8 ou mieux)

# ── Paramètres H2H ─────────────────────────────────────────────
MIN_H2H_MATCHES = 1
MIN_WIN_RATE    = 0.60

# ── Labels ──────────────────────────────────────────────────────
SET1_ALERT_LABEL  = "WIN 1er SET"
MATCH_ALERT_LABEL = "WIN MATCH"

# ── Systèmes & Bilans ───────────────────────────────────────────
STARTUP_MESSAGE_ENABLED = True  # Pour corriger ton bug de démarrage
ENABLE_DAILY_RECAP = True       # Pour corriger ton bug de bilan
CHECK_INTERVAL_MINUTES = 1

# ── Paramètres de compatibilité (À garder pour éviter les crashs) 
SET2_ALERT_LABEL       = "WIN 2ème SET"
DISABLE_SET2_RECOVERY  = True
RECAP_INTERVAL_HOURS   = 24
RECAP_WINDOW_HOURS     = 2
RECAP_DESTINATIONS     = ALERT_DESTINATIONS
