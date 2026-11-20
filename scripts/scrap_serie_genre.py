"""
Scrap des SÉRIES de fs01.lol par mot-clé, avec SYNCHRONISATION saison par saison.

Fusion de :
  - la recherche par mot-clé + écran anti-bot (ancien scrap_serie_genre.py)
  - la logique "1 fiche = 1 saison" + mise à jour des fiches existantes (script de test d'une série)

Comportement :
  - chaque saison est une fiche "Titre - Saison N" (id : cinelk10_serie_<tmdb>_sN)
  - si la fiche existe déjà : seuls les épisodes / liens manquants sont ajoutés (fusion)
  - si elle n'existe pas : création complète

Définis tes identifiants dans PowerShell, puis lance :
    $env:TMDB_API_KEY="ta_cle"
    $env:ADMIN_USERNAME="Admin"
    $env:ADMIN_PASSWORD="ton_mot_de_passe"
    py scrap_serie_genre.py
"""
import os
import re
import time
import difflib
import logging
import unicodedata
import requests
from urllib.parse import urlparse, quote_plus

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


# =====================================================================
# CONFIG
# =====================================================================
API_URL = "https://backend-cin-lk10.onrender.com"
PUSH_ENDPOINT = "/api/admin/movies"

SITE_BASE = "https://fs01.lol"
SEARCH_URL_TEMPLATE = SITE_BASE + "/?search={q}"

# Mots-clés recherchés
SEARCH_KEYWORDS = ["noel", "christmas"]

# Une fiche est considérée comme une SÉRIE si son URL contient un de ces mots.
SERIES_URL_HINTS = ("serie", "saison", "episode")
# True  = on ne garde que les liens avec ces mots (rapide).
# False = on ouvre tous les résultats et on détecte la série sur la page (plus lent).
ONLY_URLS_WITH_HINTS = True

EXCLUDE_IN_URL = ("anime", "manga")

# None = tout traiter. Mets par ex. 3 pour tester sur 3 pages seulement.
MAX_SERIES = None
# None = tous les épisodes. Mets par ex. 2 pour tester vite.
MAX_EPISODES_PER_SEASON = None

PAGE_LOAD_TIMEOUT = 30
DELAY_BETWEEN_SERIES = 2  # secondes

ANTIBOT_WAIT = 60  # secondes
PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chrome_profile_scraper")
MANUAL_VERIF_FALLBACK = True

# Secrets : à définir dans PowerShell avant de lancer le script (aucune valeur par défaut en clair)
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "Admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")


# =====================================================================
# GENRES : PLATEFORMES / STUDIOS
# =====================================================================
NETWORK_PATTERNS = {
    "Netflix":   r"netflix",
    "Prime":     r"prime video|amazon",
    "Disney":    r"disney",
    "Apple TV":  r"apple tv",
    "HBO":       r"\bhbo\b|\bmax\b",
    "Starz":     r"starz",
    "Paramount": r"paramount",
    "Mtv":       r"\bmtv\b",
    "MGM+":      r"mgm\+|\bepix\b",
}

STUDIO_PATTERNS = {
    "Pixar":            r"pixar",
    "Marvel":           r"marvel",
    "DC":               r"\bdc (comics|entertainment|studios|films)\b|^dc$",
    "Warner":           r"warner",
    "Dreamworks":       r"dreamworks",
    "20th Century Fox": r"20th century|twentieth century",
    "Paramount":        r"paramount",
    "MGM+":             r"metro-goldwyn|\bmgm\b",
    "Disney":           r"walt disney",
    "HBO":              r"\bhbo\b",
    "Mtv":              r"\bmtv\b",
    "Starz":            r"starz",
    "Netflix":          r"netflix",
    "Prime":            r"amazon (studios|mgm)|prime video",
    "Apple TV":         r"apple (studios|original)",
}

USE_WATCH_PROVIDERS = True
WATCH_REGIONS = ("FR",)


def detect_provider_genres(det_res):
    found = []
    results = (det_res.get("watch/providers") or {}).get("results") or {}
    for region in WATCH_REGIONS:
        for prov in (results.get(region) or {}).get("flatrate") or []:
            name = (prov.get("provider_name") or "").lower()
            for label, pat in NETWORK_PATTERNS.items():
                if re.search(pat, name) and label not in found:
                    found.append(label)
    return found


def detect_platform_genres(det_res, title=""):
    found = []

    def add(label):
        if label not in found:
            found.append(label)

    for net in det_res.get("networks", []) or []:
        name = (net.get("name") or "").lower()
        for label, pat in NETWORK_PATTERNS.items():
            if re.search(pat, name):
                add(label)

    for comp in det_res.get("production_companies", []) or []:
        name = (comp.get("name") or "").lower()
        for label, pat in STUDIO_PATTERNS.items():
            if re.search(pat, name):
                add(label)

    if USE_WATCH_PROVIDERS:
        for label in detect_provider_genres(det_res):
            add(label)

    t = (title or "").lower()
    if any(w in t for w in ["spider-man", "spiderman", "marvel"]):
        add("Marvel")
    if "batman" in t or "scooby" in t:
        add("DC")
        add("Warner")

    return found


# =====================================================================
# OUTILS TEXTE
# =====================================================================
def get_text(el):
    """Texte via textContent (marche même fenêtre réduite / en arrière-plan)."""
    try:
        t = (el.get_attribute("textContent") or "").strip()
    except Exception:
        return ""
    return re.sub(r"\s+", " ", t)


def page_text(driver):
    """Texte du body avec les retours à la ligne (innerText)."""
    try:
        return driver.execute_script("return document.body ? document.body.innerText : '';") or ""
    except Exception:
        return ""


def clean_text_for_comparison(text):
    if not text:
        return ""
    text = str(text).lower()
    text = unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode("utf-8")
    return re.sub(r"[^a-z0-9]", "", text)


def extract_season_number(title):
    if not title:
        return None
    patterns = [
        r"saisons?\s*0*(\d+)",
        r"seasons?\s*0*(\d+)",
        r"\bs0*(\d+)\b",
        r"-\s*0*(\d+)\s*$",
    ]
    for p in patterns:
        m = re.search(p, title, flags=re.IGNORECASE)
        if m:
            return int(m.group(1))
    return None


def get_base_title(title):
    if not title:
        return ""
    t = title
    t = re.sub(r"\s*-?\s*saisons?\s*0*\d+", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*-?\s*seasons?\s*0*\d+", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\bs0*\d+\b", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*-\s*0*\d+\s*$", "", t)
    return t.strip()


def build_match_key(title, year=None):
    season_num = extract_season_number(title) or 1
    base_clean = clean_text_for_comparison(get_base_title(title))
    if year:
        return f"{base_clean}_s{season_num}_y{year}"
    return f"{base_clean}_s{season_num}"


def _norm(s):
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower().replace("’", "'").replace("'", " ")).strip()


def title_similarity(a, b):
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def extract_year_hint(driver, raw_title=""):
    """Devine l'année affichée sur la page pour départager les homonymes."""
    def plausible(y):
        try:
            return 1900 <= int(y) <= 2029
        except Exception:
            return False

    # 1) Dans le titre : "Dark Matter (2015)"
    m = re.search(r"\((\d{4})\)", raw_title)
    if m and plausible(m.group(1)):
        return m.group(1)

    # 1b) Dans l'URL : ".../derry-saison-1-2025.html"
    try:
        m = re.search(r"-(\d{4})\.html?(?:$|\?)", driver.current_url or "")
        if m and plausible(m.group(1)):
            logger.info(f"   🔎 Année {m.group(1)} trouvée dans l'URL")
            return m.group(1)
    except Exception:
        pass

    # 1c) FrenchStream : ligne "-16 - 2024 - Science-Fiction, Drame - 54 min"
    body_text = page_text(driver)
    m = re.search(r"(?<!\d)((?:19|20)\d{2})\s*-\s*[^\n]{3,80}?-\s*\d+\s*min", body_text)
    if m and plausible(m.group(1)):
        logger.info(f"   🔎 Année {m.group(1)} trouvée via la ligne d'infos (genre + durée)")
        return m.group(1)

    # 2) Balises meta
    try:
        for sel in ['meta[itemprop="datePublished"]', 'meta[property="video:release_date"]']:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                mm = re.search(r"(\d{4})", el.get_attribute("content") or "")
                if mm and plausible(mm.group(1)):
                    logger.info(f"   🔎 Année {mm.group(1)} trouvée via la balise meta '{sel}'")
                    return mm.group(1)
    except Exception:
        pass

    # 3) Sélecteurs FR classiques
    for sel in [".s-annee", ".release-year", ".annee", "span.year", ".data span", "#info", ".sgeneros"]:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                mm = re.search(r"\b(19|20)\d{2}\b", get_text(el))
                if mm and plausible(mm.group(0)):
                    logger.info(f"   🔎 Année {mm.group(0)} trouvée via '{sel}'")
                    return mm.group(0)
        except Exception:
            continue

    # 4) Dernier recours : texte du body
    m = re.search(r"(?:Ann[ée]e|Year|Sortie)\D{0,10}(\d{4})", body_text, flags=re.IGNORECASE)
    if m and plausible(m.group(1)):
        logger.info(f"   🔎 Année {m.group(1)} trouvée via le texte du body")
        return m.group(1)

    return None


# =====================================================================
# API / BASE DE DONNÉES
# =====================================================================
def get_admin_token():
    if not ADMIN_PASSWORD or not TMDB_API_KEY:
        logger.error("❌ Variables d'environnement manquantes : ADMIN_PASSWORD et/ou TMDB_API_KEY.")
        logger.info('💡 PowerShell : $env:TMDB_API_KEY="ta_cle" ; $env:ADMIN_PASSWORD="ton_mdp"')
        return None
    logger.info("Connexion à l'API Render en tant qu'Admin...")
    try:
        res = requests.post(f"{API_URL}/api/auth/login", json={
            "username": ADMIN_USERNAME,
            "password": ADMIN_PASSWORD
        }, timeout=60)
        try:
            data = res.json()
        except Exception:
            data = {}
        if data.get("success"):
            logger.info("✅ Authentification admin réussie !")
            return data.get("token")
        logger.error(f"❌ Login refusé : HTTP {res.status_code} - {res.text[:200]}")
    except Exception as e:
        logger.error(f"❌ Erreur réseau : {e}")
    return None


def get_existing_database(token):
    """Renvoie la liste des fiches en base, ou None si illisible (on s'arrête alors, pour éviter les doublons)."""
    logger.info("🔍 Lecture de ta base de données pour la synchronisation...")
    headers = {"authorization": token}
    endpoints = [f"{API_URL}/api/admin/movies?limit=5000", f"{API_URL}/api/movies?limit=5000"]

    for endpoint in endpoints:
        try:
            res = requests.get(endpoint, headers=headers, timeout=60)
            if res.status_code == 200:
                data = res.json()
                db_list = data if isinstance(data, list) else (
                    data.get("movies") or data.get("data") or data.get("results") or []
                )
                logger.info(f"✅ {len(db_list)} fiches récupérées. Mode synchronisation activé !")
                if len(db_list) >= 5000:
                    logger.warning("⚠️ Limite de 5000 fiches atteinte : la liste est peut-être tronquée "
                                   "(risque de doublons).")
                return db_list
        except Exception as e:
            logger.warning(f"⚠️ Endpoint {endpoint} inaccessible : {e}")
    return None


def push_to_mongodb(payload, token, is_update=False):
    headers = {"Content-Type": "application/json", "authorization": token}
    try:
        res = requests.post(f"{API_URL}{PUSH_ENDPOINT}", json=payload, headers=headers, timeout=60)
        if res.status_code == 200:
            action = "Mise à jour réussie" if is_update else "Création réussie"
            logger.info(f"      [MONGODB] 🚀 {action} : {payload.get('title')}")
            return True
        logger.warning(f"      [MONGODB] ⚠️ Erreur sauvegarde : {res.text}")
    except Exception as e:
        logger.error(f"      [MONGODB] ❌ Erreur réseau : {e}")
    return False


def build_indexes(existing):
    """Index des séries déjà en base : par (tmdb_id, saison) et par clé titre/saison/année."""
    db_by_tmdb, db_by_key = {}, {}
    for m in existing:
        if "title" not in m:
            continue
        if str(m.get("type") or "").lower() == "film":
            continue
        db_by_key[build_match_key(m["title"], m.get("year"))] = m
        if m.get("tmdb_id"):
            s = extract_season_number(m["title"]) or 1
            db_by_tmdb[f"{m['tmdb_id']}_s{s}"] = m
    return db_by_tmdb, db_by_key


# =====================================================================
# TMDB (SÉRIES / SAISONS)
# =====================================================================
def tmdb_get(path, **params):
    params = {"api_key": TMDB_API_KEY, **{k: v for k, v in params.items() if v}}
    return requests.get(f"https://api.themoviedb.org/3{path}", params=params, timeout=15).json()


def get_youtube_trailer_for_season(tv_id, season_num):
    def fetch_video(path, lang):
        try:
            res = tmdb_get(path, language=lang)
            videos = res.get("results", [])
            trailer = next((v for v in videos if v.get("type") == "Trailer" and v.get("site") == "YouTube"), None)
            if not trailer:
                trailer = next((v for v in videos if v.get("site") == "YouTube"), None)
            if trailer and trailer.get("key"):
                return f"https://www.youtube.com/embed/{trailer['key']}"
        except Exception:
            pass
        return ""

    season_path = f"/tv/{tv_id}/season/{season_num}/videos"
    tv_path = f"/tv/{tv_id}/videos"
    for path, lang in [(season_path, "fr-FR"), (season_path, "en-US"),
                       (tv_path, "fr-FR"), (tv_path, "en-US"), (tv_path, "")]:
        vid = fetch_video(path, lang)
        if vid:
            return vid
    return ""


def fallback_metadata(clean_title, season_num, year_hint=None):
    """Fiche minimale quand TMDB ne trouve rien : à compléter à la main ensuite."""
    return {
        "tmdb_id": "", "title": f"{clean_title} - Saison {season_num}", "type": "serie",
        "year": str(year_hint or ""), "rating": "", "genre": "", "desc": "", "dir": "",
        "cast": "", "poster": "", "trailer": "", "is_japanese_anime": False
    }


def fetch_tmdb_season_metadata(title, season_num, year_hint=None):
    try:
        clean_search_title = re.sub(r"\s+(saison\s*\d+|season\s*\d+|\d+)$", "", title, flags=re.IGNORECASE).strip()
        clean_search_title = clean_search_title.replace("’", "'").replace("'", " ")
        if not clean_search_title:
            clean_search_title = title

        results = tmdb_get("/search/tv", language="fr-FR", query=clean_search_title).get("results") or []
        if not results:
            logger.info(f"   🔍 Aucun résultat TMDB en FR pour '{clean_search_title}', tentative en EN...")
            results = tmdb_get("/search/tv", language="en-US", query=clean_search_title).get("results") or []
        if not results:
            return None

        def strong_matches():
            return [
                r for r in results
                if max(title_similarity(clean_search_title, r.get("name")),
                       title_similarity(clean_search_title, r.get("original_name"))) >= 0.85
            ]

        movie = None
        if year_hint:
            for r in results:
                if (r.get("first_air_date") or "")[:4] == str(year_hint):
                    movie = r
                    break

            if not movie:
                def year_diff(r):
                    ad = r.get("first_air_date") or ""
                    if not ad:
                        return 999
                    try:
                        return abs(int(ad[:4]) - int(year_hint))
                    except Exception:
                        return 999

                best = min(results, key=year_diff)
                if year_diff(best) <= 2:
                    movie = best
                    logger.info(f"   📅 Année approchante : {best.get('first_air_date')[:4]} (demandé : {year_hint})")
                else:
                    strong = strong_matches()
                    if len(strong) == 1:
                        movie = strong[0]
                        logger.warning(f"   ⚠️ Année de la page ({year_hint}) incohérente, mais un seul titre "
                                       f"correspond : {movie.get('name')} ({movie.get('first_air_date')}).")
                    else:
                        logger.warning(f"   ⚠️ REJET : année {year_hint} incohérente et {len(strong)} titres candidats "
                                       f"(meilleur par année : {best.get('name')} / {best.get('first_air_date')}).")
                        return None
        else:
            strong = strong_matches()
            if len(strong) > 1:
                logger.warning(f"   ⚠️ REJET : pas d'année et {len(strong)} séries portent ce titre.")
                return None
            movie = strong[0] if strong else results[0]

        movie_id = movie["id"]

        det_res = tmdb_get(f"/tv/{movie_id}", language="fr-FR", append_to_response="credits,watch/providers")

        season_exists = True
        try:
            season_exists = any(s.get("season_number") == int(season_num) for s in det_res.get("seasons", []))
        except Exception:
            season_exists = True
        if not season_exists:
            logger.warning(f"   ⚠️ TMDB n'a pas (encore) la saison {season_num} de '{det_res.get('name')}'. "
                           f"On utilise les infos générales de la série.")

        genres = [g["name"] for g in det_res.get("genres", [])]
        if "Animation" in genres and "JP" in (det_res.get("origin_country") or []):
            return {"is_japanese_anime": True}

        directors = [c["name"] for c in (det_res.get("created_by") or []) if c.get("name")]
        if det_res.get("credits") and det_res["credits"].get("crew"):
            dir_jobs = ["Director", "Series Director", "Creator", "Réalisation"]
            for c in det_res["credits"]["crew"]:
                if c.get("job") in dir_jobs or c.get("department") == "Directing":
                    if c["name"] not in directors:
                        directors.append(c["name"])
        if not directors and det_res.get("production_companies"):
            directors = [det_res["production_companies"][0]["name"]]

        cast = [c["name"] for c in (det_res.get("credits") or {}).get("cast", [])[:8]]

        for label in detect_platform_genres(det_res, clean_search_title):
            if label not in genres:
                genres.append(label)

        season_res = {}
        if season_exists:
            try:
                season_res = tmdb_get(f"/tv/{movie_id}/season/{season_num}", language="fr-FR") or {}
            except Exception:
                season_res = {}

        poster_path = season_res.get("poster_path") or det_res.get("poster_path")
        overview = season_res.get("overview") or det_res.get("overview", "")
        backdrop_path = det_res.get("backdrop_path")

        poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else ""
        backdrop_url = f"https://image.tmdb.org/t/p/original{backdrop_path}" if backdrop_path else ""
        final_poster = f"{poster_url}|||{backdrop_url}" if backdrop_url else poster_url

        base_title = det_res.get("name") or clean_search_title
        final_year = (
            (season_res.get("air_date") or "")[:4]
            or (str(year_hint) if (year_hint and not season_exists) else "")
            or (det_res.get("first_air_date") or "")[:4]
            or "2026"
        )

        return {
            "tmdb_id": str(movie_id),
            "title": f"{base_title} - Saison {season_num}",
            "type": "serie",
            "year": final_year,
            "rating": str(round(det_res.get("vote_average", 8.0), 1)),
            "genre": ", ".join(genres),
            "desc": overview,
            "dir": ", ".join(directors) if directors else "Inconnu",
            "cast": ", ".join(cast),
            "poster": final_poster,
            "trailer": get_youtube_trailer_for_season(movie_id, season_num),
            "is_japanese_anime": False
        }
    except Exception as e:
        logger.warning(f"⚠️ Erreur TMDB pour {title} Saison {season_num}: {e}")
    return None


def merge_episodes(old_eps, new_eps):
    old_eps = old_eps or []
    ep_dict = {ep.get("title", ""): ep for ep in old_eps if isinstance(ep, dict)}

    for new_ep in new_eps:
        title = new_ep["title"]
        if title in ep_dict:
            for field in ("iframe_vidzy", "iframe_uqload", "iframe_vidzy_vostfr", "iframe_uqload_vostfr"):
                if new_ep.get(field):
                    ep_dict[title][field] = new_ep[field]
        else:
            ep_dict[title] = new_ep

    def extract_nums(t):
        nums = re.findall(r"\b\d+\b", str(t))
        return int(nums[-1]) if nums else 0

    return sorted(ep_dict.values(), key=lambda x: extract_nums(x.get("title", "")))


def build_keys_and_id(tmdb_data, season_full_title, clean_title, s_num, year_hint):
    """Clés de correspondance + ID unifié, valables même sans tmdb_id (fiche de secours)."""
    tmdb_id = tmdb_data.get("tmdb_id") or ""
    tmdb_key = f"{tmdb_id}_s{s_num}" if tmdb_id else None
    match_key = build_match_key(season_full_title, tmdb_data.get("year") or year_hint)
    slug = tmdb_id or clean_text_for_comparison(clean_title)
    unified_id = f"cinelk10_serie_{slug}_s{s_num}"
    return tmdb_key, match_key, unified_id


def apply_tmdb_to_existing(matched, tmdb_data):
    """Met à jour titre/tmdb_id d'une fiche existante seulement si TMDB a répondu, et AJOUTE
    les genres manquants (plateformes, studios...) sans rien supprimer de tes réglages manuels."""
    if tmdb_data.get("tmdb_id"):
        matched["title"] = tmdb_data["title"]
        matched["tmdb_id"] = tmdb_data["tmdb_id"]

        old_genres = [g.strip() for g in (matched.get("genre") or "").split(",") if g.strip()]
        new_genres = [g.strip() for g in (tmdb_data.get("genre") or "").split(",") if g.strip()]
        old_lower = {g.lower() for g in old_genres}
        for g in new_genres:
            if g.lower() not in old_lower:
                old_genres.append(g)
                old_lower.add(g.lower())
        matched["genre"] = ", ".join(old_genres)


# =====================================================================
# NAVIGATION SELENIUM + ANTI-BOT
# =====================================================================
ANTIBOT_MARKERS = ("verification de votre navigateur", "vérification de votre navigateur", "protection anti-robot")


def is_antibot_page(driver):
    try:
        txt = driver.execute_script(
            "return ((document.body && document.body.textContent) || '').slice(0, 800);"
        ) or ""
    except Exception:
        return False
    return any(m in txt.lower() for m in ANTIBOT_MARKERS)


def wait_for_antibot(driver, timeout=ANTIBOT_WAIT):
    if not is_antibot_page(driver):
        return True
    logger.info("   🛡️ Écran de vérification du navigateur, j'attends la redirection automatique...")
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(2)
        if not is_antibot_page(driver):
            logger.info("   ✅ Vérification terminée.")
            time.sleep(2)
            return True
    logger.warning(f"   ⏱️ Toujours sur l'écran de vérification après {timeout}s.")
    return False


def safe_get(driver, url):
    """driver.get avec timeout court + attente de l'écran de vérification."""
    try:
        driver.get(url)
    except TimeoutException:
        logger.warning(f"   ⏱️ Chargement trop long (> {PAGE_LOAD_TIMEOUT}s), j'arrête et je continue.")
        try:
            driver.execute_script("window.stop();")
        except Exception:
            pass
    time.sleep(2)

    if not wait_for_antibot(driver):
        logger.info("   🔄 Je recharge la page...")
        try:
            driver.get(url)
        except TimeoutException:
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
        time.sleep(2)
        if not wait_for_antibot(driver) and MANUAL_VERIF_FALLBACK:
            input("   👉 Valide la vérification dans la fenêtre Chrome, puis appuie sur Entrée ici... ")
            time.sleep(2)


def build_driver():
    options = webdriver.ChromeOptions()
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-notifications")
    options.add_argument("--mute-audio")
    options.add_argument("--window-size=1280,900")
    if PROFILE_DIR:
        options.add_argument(f"--user-data-dir={PROFILE_DIR}")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--disable-renderer-backgrounding")
    options.add_argument("--disable-background-timer-throttling")
    options.page_load_strategy = "eager"
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    driver.set_script_timeout(PAGE_LOAD_TIMEOUT)
    return driver


# =====================================================================
# RECHERCHE : RÉCUPÉRATION DES LIENS
# =====================================================================
LINK_RE = re.compile(r"^/(?:[^/]+/)?\d+-[^/]+\.html$")

LINKS_JS_SEARCH = r"""
var out = [];
document.querySelectorAll('.search-item').forEach(function(e){
  var m = (e.getAttribute('onclick') || '').match(/location\.href\s*=\s*['"]([^'"]+)['"]/);
  if (m) { try { out.push(new URL(m[1], location.href).href); } catch(x) {} }
  else {
    var a = e.querySelector('a[href]');
    if (a) { out.push(a.href); }
  }
});
return out;
"""

SHOW_MORE_JS = r"""
var root = document.getElementById('search-results-top') || document.body;
var all = root.querySelectorAll('*');
var target = null;
for (var i = 0; i < all.length; i++) {
  var e = all[i];
  if (e.children.length === 0 && /afficher\s+la\s+suite/i.test(e.textContent || '')) {
    if (e.offsetParent !== null) { target = e; break; }
  }
}
if (!target) { return false; }
target.scrollIntoView({block: 'center'});
target.click();
return true;
"""


def _filter_series_links(hrefs):
    links = []
    site_host = urlparse(SITE_BASE).netloc
    for href in hrefs or []:
        p = urlparse(href or "")
        if p.netloc and p.netloc != site_host:
            continue
        if not LINK_RE.match(p.path):
            continue
        low = p.path.lower()
        if any(w in low for w in EXCLUDE_IN_URL):
            continue
        if ONLY_URLS_WITH_HINTS and not any(w in low for w in SERIES_URL_HINTS):
            continue
        clean = f"{p.scheme}://{p.netloc}{p.path}"
        if clean not in links:
            links.append(clean)
    return links


def collect_search_links(driver):
    return _filter_series_links(driver.execute_script(LINKS_JS_SEARCH))


def wait_for_search_results(driver, timeout=25):
    end = time.time() + timeout
    while time.time() < end:
        try:
            links = collect_search_links(driver)
        except Exception:
            links = []
        if links:
            return links
        time.sleep(1)
    return []


def click_show_more(driver):
    try:
        return bool(driver.execute_script(SHOW_MORE_JS))
    except Exception:
        return False


def scroll_search_results(driver, max_rounds=400):
    """Clique sur 'Afficher la suite →' tant qu'il apporte de nouveaux résultats."""
    count = len(collect_search_links(driver))
    no_progress = 0
    clicks = 0

    for _ in range(max_rounds):
        if click_show_more(driver):
            clicks += 1

        new_count = count
        for _ in range(6):
            time.sleep(1)
            try:
                new_count = len(collect_search_links(driver))
            except Exception:
                new_count = count
            if new_count > count:
                break

        if new_count > count:
            logger.info(f"   ➕ {new_count} résultats chargés (clics 'Afficher la suite' : {clicks})")
            count = new_count
            no_progress = 0
            continue

        no_progress += 1
        try:
            driver.execute_script("""
                var c = document.getElementById('search-results-top');
                if (c) { c.scrollTop = c.scrollHeight; }
                window.scrollTo(0, document.body.scrollHeight);
            """)
        except Exception:
            pass
        if no_progress >= 3:
            break

    if clicks == 0:
        logger.info("   (aucun bouton 'Afficher la suite' : tous les résultats étaient déjà affichés)")
    return collect_search_links(driver)


def find_search_box(driver):
    xpaths = [
        "//input[@id='story']",
        "//input[@name='story']",
        "//input[contains(translate(@placeholder,'TITRE','titre'),'titre')]",
        "//input[@type='search']",
    ]
    for xp in xpaths:
        try:
            for el in driver.find_elements(By.XPATH, xp):
                if el.is_displayed():
                    return el
        except Exception:
            continue
    return None


def search_by_typing(driver, keyword):
    """Plan B : remplit la barre de recherche et envoie le formulaire en JavaScript."""
    safe_get(driver, SITE_BASE)
    time.sleep(2)
    box = find_search_box(driver)
    if not box:
        logger.error("   ❌ Barre de recherche introuvable sur la page d'accueil.")
        return False
    try:
        driver.execute_script("""
            var box = arguments[0];
            box.focus();
            box.value = arguments[1];
            box.dispatchEvent(new Event('input', {bubbles: true}));
            box.dispatchEvent(new Event('keyup', {bubbles: true}));
            var f = box.form;
            if (f) { if (f.requestSubmit) { f.requestSubmit(); } else { f.submit(); } }
        """, box, keyword)
        logger.info("   ⌨️ Mot-clé saisi dans la barre, envoi du formulaire.")
    except Exception as e:
        logger.error(f"   ❌ Impossible de remplir la barre de recherche : {str(e)[:100]}")
        return False
    return True


def search_all_series_links(driver, keyword):
    logger.info(f"\n🔎 RECHERCHE : '{keyword}'")

    safe_get(driver, SEARCH_URL_TEMPLATE.format(q=quote_plus(keyword)))
    links = wait_for_search_results(driver, timeout=25)

    if not links:
        logger.info("   ↩️ Pas de résultats via l'URL, j'essaie via la barre de recherche...")
        if search_by_typing(driver, keyword):
            links = wait_for_search_results(driver, timeout=25)

    if not links:
        logger.error(f"   ❌ Aucun résultat pour '{keyword}'. Je saute ce mot-clé.")
        return []

    logger.info(f"   📄 {len(links)} résultats visibles, je clique sur 'Afficher la suite' pour tout charger...")
    links = scroll_search_results(driver)
    logger.info(f"   ✅ '{keyword}' : {len(links)} pages de séries trouvées.")
    return links


# =====================================================================
# SITE "FRENCHSTREAM" (1 page = 1 saison, lecteur #seriePlayer)
# =====================================================================
def detect_site_type(driver):
    try:
        if driver.find_elements(By.ID, "serie-config"):
            return "frenchstream"
    except Exception:
        pass
    return "filmoflix"


def fmt_iframe(src, server_name):
    if server_name == "vidzy":
        src = re.sub(r"vidzy\.[a-z]+", "vidzy.lol", src, flags=re.IGNORECASE)
    elif server_name == "uqload":
        src = re.sub(r"uqload\.[a-z]+", "uqload.vc", src, flags=re.IGNORECASE)
    return (f'<IFRAME SRC="{src}" FRAMEBORDER=0 MARGINWIDTH=0 MARGINHEIGHT=0 '
            f'SCROLLING=NO WIDTH=640 HEIGHT=360 allowfullscreen></IFRAME>')


def select_server(driver, server_name):
    try:
        tabs = driver.find_elements(By.CSS_SELECTOR, f"a.fsctab[data-player='{server_name}']")
        if tabs:
            driver.execute_script("arguments[0].click();", tabs[0])
            time.sleep(2)
            return True
    except Exception:
        pass
    return False


def extract_current_src(driver, server_name):
    try:
        src = driver.find_element(By.ID, "seriePlayer").get_attribute("src")
        if src and server_name in src.lower():
            return fmt_iframe(src, server_name)
    except Exception:
        pass
    return ""


def get_new_site_episode_numbers(driver, ep_type):
    try:
        nums = []
        for el in driver.find_elements(By.CSS_SELECTOR, f"#{ep_type}-episodes .ep-title"):
            num = el.get_attribute("data-num")
            if num:
                nums.append(num)
        return nums
    except Exception:
        return []


def click_episode_new_site(driver, ep_type, ep_num):
    try:
        el = driver.find_element(By.CSS_SELECTOR, f'.ep-title[data-type="{ep_type}"][data-num="{ep_num}"]')
        driver.execute_script("arguments[0].click();", el)
        time.sleep(3)
        return True
    except Exception as e:
        logger.warning(f"      Impossible de cliquer sur l'épisode {ep_type} {ep_num} : {str(e)[:80]}")
        return False


def sort_nums(nums):
    def k(x):
        try:
            return int(x)
        except Exception:
            return 0
    return sorted(nums, key=k)


def process_season_episodes_new_site(driver, existing_eps_dict):
    episodes_data = []

    vf_nums = get_new_site_episode_numbers(driver, "vf")
    truefrench_nums = get_new_site_episode_numbers(driver, "truefrench")
    vostfr_nums = get_new_site_episode_numbers(driver, "vostfr")

    all_nums = sort_nums(set(vf_nums) | set(truefrench_nums) | set(vostfr_nums))
    logger.info(f"    {len(all_nums)} épisodes détectés "
                f"(VF: {len(vf_nums)} | TRUEFRENCH: {len(truefrench_nums)} | VOSTFR: {len(vostfr_nums)}).")
    if MAX_EPISODES_PER_SEASON:
        all_nums = all_nums[:MAX_EPISODES_PER_SEASON]

    for num in all_nums:
        ep_title = f"Épisode {num}"
        old_ep = existing_eps_dict.get(ep_title, {})
        has_vf_v = bool(old_ep.get("iframe_vidzy"))
        has_vf_u = bool(old_ep.get("iframe_uqload"))
        has_vost_v = bool(old_ep.get("iframe_vidzy_vostfr"))
        has_vost_u = bool(old_ep.get("iframe_uqload_vostfr"))

        vf_vidzy = vf_uqload = vostfr_vidzy = vostfr_uqload = ""

        if not (has_vf_v and has_vf_u) and (num in truefrench_nums or num in vf_nums):
            logger.info(f"      🔍 [VF] {ep_title} en cours de scrape (Vidzy/Uqload)...")
            ep_type = "truefrench" if num in truefrench_nums else "vf"
            if click_episode_new_site(driver, ep_type, num):
                if not has_vf_v and select_server(driver, "vidzy"):
                    vf_vidzy = extract_current_src(driver, "vidzy")
                if not has_vf_u and select_server(driver, "uqload"):
                    vf_uqload = extract_current_src(driver, "uqload")

        if num in vostfr_nums and not (has_vost_v and has_vost_u):
            logger.info(f"      🔍 [VOSTFR] {ep_title} en cours de scrape (Vidzy/Uqload)...")
            if click_episode_new_site(driver, "vostfr", num):
                if not has_vost_v and select_server(driver, "vidzy"):
                    vostfr_vidzy = extract_current_src(driver, "vidzy")
                if not has_vost_u and select_server(driver, "uqload"):
                    vostfr_uqload = extract_current_src(driver, "uqload")

        if vf_vidzy or vf_uqload or vostfr_vidzy or vostfr_uqload:
            episodes_data.append({
                "title": ep_title,
                "iframe_premium": "",
                "iframe_vidzy": vf_vidzy or old_ep.get("iframe_vidzy", ""),
                "iframe_uqload": vf_uqload or old_ep.get("iframe_uqload", ""),
                "iframe_premium_vostfr": "",
                "iframe_vidzy_vostfr": vostfr_vidzy or old_ep.get("iframe_vidzy_vostfr", ""),
                "iframe_uqload_vostfr": vostfr_uqload or old_ep.get("iframe_uqload_vostfr", ""),
            })

    return episodes_data


# =====================================================================
# SITE "FILMOFLIX" (page série → pages saison → pages épisode)
# =====================================================================
def extract_both_iframes(driver):
    iframes_data = {"vf_vidzy": "", "vf_uqload": "", "vostfr_vidzy": "", "vostfr_uqload": ""}
    css = "ul.list-liens li, .player-list .lien-bis, table tbody tr"
    try:
        count = len(driver.find_elements(By.CSS_SELECTOR, css))
        for i in range(count):
            rows = driver.find_elements(By.CSS_SELECTOR, css)
            if i >= len(rows):
                break
            row = rows[i]

            try:
                row_text = get_text(row).lower()
                is_vidzy = "vidzy" in row_text
                is_uqload = "uqload" in row_text
                if not is_vidzy and not is_uqload:
                    continue
                html_content = (row.get_attribute("innerHTML") or "").lower()
            except Exception:
                continue

            mots_vostfr = ['en.png', 'uk.png', 'us.png', 'gb.png', 'en.svg', 'uk.svg', 'gb.svg',
                           'flag-gb', 'flag-uk', 'vostfr', 'vost', 'english', 'anglais']
            is_vostfr = any(x in html_content for x in mots_vostfr)

            if is_vostfr and is_vidzy and iframes_data["vostfr_vidzy"]:
                continue
            if is_vostfr and is_uqload and iframes_data["vostfr_uqload"]:
                continue
            if not is_vostfr and is_vidzy and iframes_data["vf_vidzy"]:
                continue
            if not is_vostfr and is_uqload and iframes_data["vf_uqload"]:
                continue

            driver.execute_script("arguments[0].click();", row)
            time.sleep(3)

            lang = "vostfr" if is_vostfr else "vf"
            for iframe in driver.find_elements(By.TAG_NAME, "iframe"):
                src = iframe.get_attribute("src")
                if not src:
                    continue
                low = src.lower()
                if "vidzy" in low and is_vidzy:
                    iframes_data[f"{lang}_vidzy"] = fmt_iframe(src, "vidzy")
                    logger.info(f"        Lien {lang.upper()} Vidzy extrait")
                    break
                if "uqload" in low and is_uqload:
                    iframes_data[f"{lang}_uqload"] = fmt_iframe(src, "uqload")
                    logger.info(f"        Lien {lang.upper()} Uqload extrait")
                    break

            if all(iframes_data.values()):
                break
    except Exception as e:
        logger.warning(f"      Erreur extraction iframe : {str(e)[:120]}")
    return iframes_data


def process_season_episodes(driver, season_url, season_num, existing_eps_dict):
    safe_get(driver, season_url)
    episodes_data = []

    try:
        hrefs = [el.get_attribute("href") for el in driver.find_elements(By.TAG_NAME, "a") if el.get_attribute("href")]
        ep_urls = {}

        for href in hrefs:
            m = re.search(rf"{season_num}-saison/(\d+)-episode", href)
            if m:
                ep_urls.setdefault(m.group(1), href)

        if not ep_urls:
            for href in hrefs:
                m = re.search(r"(\d+)-episode", href)
                if m:
                    ep_urls.setdefault(m.group(1), href)

        items = sorted(ep_urls.items(), key=lambda kv: int(kv[0]))
        logger.info(f"    Saison {season_num}: {len(items)} épisodes détectés.")
        if MAX_EPISODES_PER_SEASON:
            items = items[:MAX_EPISODES_PER_SEASON]

        for ep_num, url in items:
            ep_title = f"Épisode {ep_num}"
            old_ep = existing_eps_dict.get(ep_title, {})

            if all(old_ep.get(k) for k in ("iframe_vidzy", "iframe_uqload",
                                           "iframe_vidzy_vostfr", "iframe_uqload_vostfr")):
                logger.info(f"      ⏭️ [COMPLET] {ep_title} a déjà ses 4 liens. On passe.")
                continue

            logger.info(f"      🔍 [INCOMPLET] {ep_title} → scrape Vidzy et Uqload...")
            safe_get(driver, url)
            extracted = extract_both_iframes(driver)

            if any(extracted.values()):
                episodes_data.append({
                    "title": ep_title,
                    "iframe_premium": "",
                    "iframe_vidzy": extracted["vf_vidzy"] or old_ep.get("iframe_vidzy", ""),
                    "iframe_uqload": extracted["vf_uqload"] or old_ep.get("iframe_uqload", ""),
                    "iframe_premium_vostfr": "",
                    "iframe_vidzy_vostfr": extracted["vostfr_vidzy"] or old_ep.get("iframe_vidzy_vostfr", ""),
                    "iframe_uqload_vostfr": extracted["vostfr_uqload"] or old_ep.get("iframe_uqload_vostfr", ""),
                })
    except Exception as e:
        logger.error(f"      Erreur traitement de la saison {season_num}: {str(e)[:150]}")
    return episodes_data


# =====================================================================
# SYNCHRONISATION D'UNE SAISON
# =====================================================================
def sync_season(token, clean_title, s_num, year_hint, db_by_tmdb, db_by_key, extract_fn):
    """
    Crée ou met à jour la fiche d'UNE saison.
    extract_fn(existing_eps_dict) -> liste des épisodes nouveaux / complétés.
    Renvoie : "added", "updated", "skipped" (déjà à jour), "ignored" (anime japonais), "failed".
    """
    season_full_title = f"{clean_title} - Saison {s_num}"
    logger.info(f"\n   🎬 TRAITEMENT : {season_full_title}")

    tmdb_data = fetch_tmdb_season_metadata(clean_title, s_num, year_hint=year_hint)
    if not tmdb_data:
        logger.warning("   ⚠️ TMDB introuvable : création d'une fiche minimale (à compléter à la main).")
        tmdb_data = fallback_metadata(clean_title, s_num, year_hint)

    if tmdb_data.get("is_japanese_anime"):
        logger.info(f"   🇯🇵 [FILTRE] '{clean_title}' est un anime japonais. Ignoré.")
        return "ignored"

    tmdb_key, match_key, unified_id = build_keys_and_id(tmdb_data, season_full_title, clean_title, s_num, year_hint)
    matched = (db_by_tmdb.get(tmdb_key) if tmdb_key else None) or db_by_key.get(match_key)

    if not matched:
        logger.info(f"      ✨ [NOUVEAU] '{season_full_title}' ({tmdb_data.get('year')}) : création intégrale...")
    else:
        logger.info("      🛠️ [SYNCHRONISATION] Fiche existante trouvée. Recherche de nouveautés...")

    existing_eps_dict = {}
    if matched:
        existing_eps_dict = {ep["title"]: ep for ep in (matched.get("episodes") or [])
                             if isinstance(ep, dict) and ep.get("title")}

    extracted = extract_fn(existing_eps_dict)
    if not extracted:
        logger.info(f"      ✅ '{season_full_title}' est déjà à jour (ou aucun lecteur Vidzy/Uqload trouvé).")
        return "skipped"

    if matched:
        apply_tmdb_to_existing(matched, tmdb_data)
        matched["episodes"] = merge_episodes(matched.get("episodes", []), extracted)
        ok = push_to_mongodb(matched, token, is_update=True)
        record, result = matched, "updated"
    else:
        payload = {
            "id": unified_id,
            "tmdb_id": tmdb_data["tmdb_id"],
            "type": "serie",
            "title": tmdb_data["title"],
            "year": tmdb_data["year"],
            "poster": tmdb_data["poster"],
            "rating": tmdb_data["rating"],
            "genre": tmdb_data["genre"],
            "desc": tmdb_data["desc"],
            "dir": tmdb_data["dir"],
            "cast": tmdb_data["cast"],
            "trailer_iframe": tmdb_data["trailer"],
            "iframe_premium": "",
            "iframe_vidzy": "",
            "iframe_uqload": "",
            "iframe_premium_vostfr": "",
            "iframe_vidzy_vostfr": "",
            "iframe_uqload_vostfr": "",
            "episodes": merge_episodes([], extracted),
        }
        if not tmdb_data["tmdb_id"]:
            logger.warning(f"      ✍️ Fiche créée SANS données TMDB (id: {unified_id}) : complète-la à la main.")
        ok = push_to_mongodb(payload, token)
        record, result = payload, "added"

    if not ok:
        return "failed"

    # On garde les index à jour : une même saison peut réapparaître via un autre mot-clé
    if tmdb_key:
        db_by_tmdb[tmdb_key] = record
    db_by_key[match_key] = record
    return result


# =====================================================================
# TRAITEMENT D'UNE PAGE DE RÉSULTAT
# =====================================================================
def wait_for_title(driver, timeout=10):
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#s-title, h1"))
        )
    except Exception:
        pass


def get_raw_title(driver, site_type):
    selectors = ["#s-title", "h1"] if site_type == "frenchstream" else ["h1"]
    for sel in selectors:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                t = get_text(el)
                if t:
                    return t
        except Exception:
            continue
    return ""


def looks_like_series(driver, url, site_type):
    if any(w in urlparse(url).path.lower() for w in SERIES_URL_HINTS):
        return True
    if site_type == "frenchstream":
        return True
    try:
        hrefs = [el.get_attribute("href") or "" for el in driver.find_elements(By.TAG_NAME, "a")]
        return any(re.search(r"\d+-(saison|episode)", h) for h in hrefs)
    except Exception:
        return False


def process_page(driver, url, token, db_by_tmdb, db_by_key):
    """Traite une page de résultat. Renvoie la liste des résultats (un par saison)."""
    safe_get(driver, url)

    raw_title, site_type = "", "filmoflix"
    for attempt in range(2):
        wait_for_title(driver)
        site_type = detect_site_type(driver)
        raw_title = get_raw_title(driver, site_type)
        if raw_title:
            break
        if attempt == 0:
            logger.info("   🔄 Titre vide, je laisse la page charger et je réessaie...")
            time.sleep(4)

    if not raw_title:
        logger.warning("   ⚠️ Titre vide, on passe.")
        return ["failed"]

    if not looks_like_series(driver, url, site_type):
        logger.info(f"   ⏭️ '{raw_title}' n'a pas l'air d'une série, ignorée.")
        return ["ignored"]

    logger.info(f"   🌐 Type de site détecté : {site_type}")

    # ---------------- FRENCHSTREAM : la page = 1 saison ----------------
    if site_type == "frenchstream":
        clean_title = re.sub(r"\s*-?\s*saisons?\s*0*\d+\s*$", "", raw_title, flags=re.IGNORECASE).strip()
        s_num = extract_season_number(raw_title) or 1
        year_hint = extract_year_hint(driver, raw_title)
        if year_hint:
            logger.info(f"   📅 Année détectée sur la page : {year_hint}")
        else:
            logger.warning("   ⚠️ Aucune année détectée — désambiguïsation TMDB moins fiable.")

        return [sync_season(
            token, clean_title, s_num, year_hint, db_by_tmdb, db_by_key,
            extract_fn=lambda eps: process_season_episodes_new_site(driver, eps),
        )]

    # ---------------- FILMOFLIX : page série → saisons ----------------
    clean_title = raw_title.replace("VOIR", "").replace("SÉRIE EN STREAMING", "").strip()
    clean_title = re.sub(r"\(\d{4}\)", "", clean_title).strip()
    clean_title = re.sub(r"\s*-?\s*saisons?\s*0*\d+\s*$", "", clean_title, flags=re.IGNORECASE).strip()

    year_hint = extract_year_hint(driver, raw_title)
    if year_hint:
        logger.info(f"   📅 Année détectée sur la page : {year_hint}")
    else:
        logger.warning("   ⚠️ Aucune année détectée — désambiguïsation TMDB moins fiable.")

    hrefs = [el.get_attribute("href") for el in driver.find_elements(By.TAG_NAME, "a") if el.get_attribute("href")]
    seasons = {}
    for href in hrefs:
        m = re.search(r"(\d+)-saison", href)
        if m:
            seasons.setdefault(int(m.group(1)), href)

    if not seasons and any(re.search(r"\d+-episode", h) for h in hrefs):
        seasons[1] = safe_url_for_season_one(driver, url)

    if not seasons:
        logger.warning(f"   ⚠️ Aucune saison/épisode trouvée pour {clean_title}")
        return ["failed"]

    results = []
    for s_num in sorted(seasons):
        s_url = seasons[s_num]
        results.append(sync_season(
            token, clean_title, s_num, year_hint, db_by_tmdb, db_by_key,
            extract_fn=lambda eps, u=s_url, n=s_num: process_season_episodes(driver, u, n, eps),
        ))
    return results


def safe_url_for_season_one(driver, fallback_url):
    """Quand la page série contient directement les épisodes, la saison 1 = la page elle-même."""
    try:
        return driver.current_url or fallback_url
    except Exception:
        return fallback_url


# =====================================================================
# BOUCLE PRINCIPALE
# =====================================================================
def main():
    token = get_admin_token()
    if not token:
        return

    existing = get_existing_database(token)
    if existing is None:
        logger.error("❌ Base de données illisible : j'arrête pour ne pas créer de doublons. "
                     "Relance dans une minute (Render met parfois du temps à se réveiller).")
        return
    db_by_tmdb, db_by_key = build_indexes(existing)

    try:
        driver = build_driver()
    except Exception as e:
        logger.error(f"❌ Impossible de lancer Chrome : {e}")
        return

    stats = {"added": 0, "updated": 0, "skipped": 0, "ignored": 0, "failed": 0}
    failed_urls = []

    try:
        # 1) Collecte de toutes les pages (tous mots-clés, sans doublons)
        links_all = []
        for kw in SEARCH_KEYWORDS:
            for link in search_all_series_links(driver, kw):
                if link not in links_all:
                    links_all.append(link)

        logger.info(f"\n📚 TOTAL : {len(links_all)} pages de séries uniques trouvées sur le site.")
        if not links_all and ONLY_URLS_WITH_HINTS:
            logger.info("💡 Aucune série trouvée : si le site en a, mets ONLY_URLS_WITH_HINTS = False.")

        if MAX_SERIES:
            links_all = links_all[:MAX_SERIES]
            logger.info(f"   (limité à {MAX_SERIES} pages pour le test)")

        # 2) Traitement page par page (synchronisation saison par saison)
        for i, link in enumerate(links_all, 1):
            logger.info(f"\n[{i}/{len(links_all)}] ➡️ {link}")
            try:
                results = process_page(driver, link, token, db_by_tmdb, db_by_key)
            except Exception as e:
                logger.warning(f"   ⚠️ Erreur sur cette page, on passe à la suivante : {str(e)[:150]}")
                try:
                    driver.execute_script("window.stop();")
                except Exception:
                    pass
                results = ["failed"]

            for r in results:
                stats[r] += 1
            if "failed" in results:
                failed_urls.append(link)
            if any(r in ("added", "updated") for r in results):
                time.sleep(DELAY_BETWEEN_SERIES)

    except Exception as e:
        logger.error(f"Une erreur est survenue : {e}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    logger.info("\n==========================================")
    logger.info(f"✅ Créées : {stats['added']} | 🛠️ Mises à jour : {stats['updated']} | "
                f"⏭️ Déjà à jour : {stats['skipped']} | 🚫 Ignorées : {stats['ignored']} | ❌ Échecs : {stats['failed']}")
    if failed_urls:
        logger.info("Pages en échec (à revoir) :")
        for u in failed_urls:
            logger.info(f"   - {u}")
    logger.info("==========================================")


if __name__ == "__main__":
    main()