# Scraper LK10 — panneau de contrôle

1. Mets ce dossier dans un dépôt GitHub **privé**.
2. Render → New → Web Service → ce dépôt → Runtime **Docker** → plan **Standard** (2 Go de RAM).
3. Variables d'environnement : MONGODB_URI, DASH_PASSWORD, TMDB_API_KEY, ADMIN_USERNAME, ADMIN_PASSWORD.
4. Atlas → Network Access → autoriser 0.0.0.0/0.
5. Ouvre l'URL Render sur ton téléphone → « Ajouter à l'écran d'accueil ».

Script films : dépose `scrap_toutes_pages.py` dans `scripts/` (il apparaît tout seul dans l'interface).
