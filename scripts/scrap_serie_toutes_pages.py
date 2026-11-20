"""
Scrap des SÉRIES de fs01.lol : parcourt la liste des séries page par page
(page 1 = index.php?do=cat&category=s-tv ; pages 2... = index.php?cstart=N&do=cat&category=s-tv).

Script AUTONOME (séries uniquement, indépendant de scrap_toutes_pages.py).

Chaque page du site = UNE saison ("Rivals - Saison 2"). Pour chaque saison, le script :
  - lit le titre / l'année, cherche la série sur TMDB (fiche minimale si introuvable),
  - clique sur chaque épisode (VF / TRUEFRENCH / VOSTFR) puis sur Vidzy et Uqload pour récupérer les iframes,
  - enregistre la saison dans ta base (création, ou mise à jour des épisodes manquants).

Comment ça marche :
  1) Au lancement, il lit TOUTE ta base (films + séries).
  2) Il scrape page 1, 2, 3... jusqu'à la dernière (détectée automatiquement).
  3) La progression est sauvegardée (après chaque saison et chaque page) : si le script s'arrête,
     il reprend à la page où il s'était arrêté au prochain lancement.

Lancement :
    py scrap_series_toutes_pages.py
"""
import os
import logging
import time
import re
import json
import unicodedata
import difflib
import requests
from urllib.parse import urlparse

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

SITE_BASE = "https://fs01.lol"

# Page 1 = index.php?do=cat&category=s-tv (sans cstart) ; pages suivantes = index.php?cstart=N
FIRST_PAGE_URL = "https://fs01.lol/index.php?do=cat&category=s-tv"
PAGE_URL = "https://fs01.lol/index.php?cstart={n}&do=cat&category=s-tv"

START_PAGE = 1
# None = la dernière page est détectée automatiquement dans la pagination du site.
# Tu peux aussi mettre un nombre fixe (ex : 250).
END_PAGE = None
END_PAGE_IF_UNKNOWN = 500   # utilisé seulement si la détection automatique échoue

# True = reprend à la page où le script s'est arrêté au lancement suivant
RESUME = True

# Si une page ne s'affiche pas (blocage anti-bot, site en panne, fin des pages), le script la retente
# puis s'arrête après N essais. La progression est sauvegardée : relance et il reprend ici.
MAX_EMPTY_PAGES = 3

# True = à la fin, retente une fois les saisons en échec
RETRY_FAILED_AT_END = True

# True  = si la saison est déjà en base, on l'ignore (rapide).
# False = mode SYNCHRONISATION : on ouvre la saison déjà en base et on ajoute les épisodes / liens manquants.
SKIP_ALREADY_IN_DB = True

PAGE_LOAD_TIMEOUT = 30
DELAY_BETWEEN_SERIES = 2  # secondes

# Lecture de ta base : nombre de fiches demandées par requête (la pagination est gérée si besoin)
DB_PAGE_SIZE = 50000
DB_MAX_PAGES = 100

# Page « Vérification de votre navigateur... » : temps max d'attente de la redirection automatique
ANTIBOT_WAIT = 60  # secondes

# Profil Chrome dédié au script : une fois la vérification validée, Chrome garde le cookie.
# (Même profil que le script films. Ne lance pas les deux scripts en même temps.)
PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chrome_profile_scraper")

# Si la vérification ne passe pas toute seule, le script se met en pause :
# tu valides à la main dans la fenêtre Chrome, puis tu appuies sur Entrée dans le terminal.
MANUAL_VERIF_FALLBACK = True

# Fichiers de sauvegarde (à côté du script), propres aux séries. Pour tout recommencer : supprime-les.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROGRESS_FILE = os.path.join(BASE_DIR, "progress_series.json")   # dernière page terminée
DONE_FILE = os.path.join(BASE_DIR, "done_series.txt")            # saisons déjà traitées (1 identifiant par ligne)
FAILED_FILE = os.path.join(BASE_DIR, "failed_series.json")       # saisons en échec à retenter
DEBUG_FILE = os.path.join(BASE_DIR, "debug_serie.html")          # page sauvegardée si rien n'est trouvé

# Identifiants (le script marche tout seul, sans rien configurer)
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "Admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# =====================================================================
# GENRES : PLATEFORMES / STUDIOS
# =====================================================================
# Noms EXACTEMENT identiques aux cases de ton filtre (la casse compte)
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
}


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

    t = (title or "").lower()
    if any(w in t for w in ["spider-man", "spiderman", "marvel"]):
        add("Marvel")

    return found


# =====================================================================
# FONCTIONS UTILITAIRES & TMDB
# =====================================================================
def get_text(el):
    """Lit le texte d'un élément via textContent (marche même fenêtre réduite / en arrière-plan)."""
    try:
        t = (el.get_attribute("textContent") or "").strip()
    except Exception:
        return ""
    return re.sub(r"\s+", " ", t)


def clean_text_for_comparison(text):
    if not text:
        return ""
    text = str(text).lower()
    text = unicodedata.normalize('NFD', text).encode('ascii', 'ignore').decode("utf-8")
    return re.sub(r'[^a-z0-9]', '', text)


def extract_season_number(title):
    if not title:
        return None
    patterns = [
        r'saisons?\s*0*(\d+)',
        r'seasons?\s*0*(\d+)',
        r'\bs0*(\d+)\b',
        r'-\s*0*(\d+)\s*$',
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
    t = re.sub(r'\s*-?\s*saisons?\s*0*\d+', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\s*-?\s*seasons?\s*0*\d+', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\bs0*\d+\b', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\s*-\s*0*\d+\s*$', '', t)
    return t.strip()


def build_match_key(title, year=None):
    season_num = extract_season_number(title) or 1
    base_clean = clean_text_for_comparison(get_base_title(title))
    if year:
        return f"{base_clean}_s{season_num}_y{year}"
    return f"{base_clean}_s{season_num}"


def _norm(s):
    return re.sub(r'[^a-z0-9 ]', '', (s or '').lower().replace("’", "'").replace("'", " ")).strip()


def title_similarity(a, b):
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def extract_year_hint(driver, raw_title=""):
    def plausible(y):
        try:
            return 1900 <= int(y) <= 2029
        except Exception:
            return False

    # 1) Année directement présente dans le titre : "Dark Matter (2015)"
    m = re.search(r'\((\d{4})\)', raw_title)
    if m and plausible(m.group(1)):
        return m.group(1)

    # 1b) Année dans l'URL de la page : ".../derry-saison-1-2025.html"
    try:
        m = re.search(r'-(\d{4})\.html?(?:$|\?)', driver.current_url or "")
        if m and plausible(m.group(1)):
            logger.info(f"   🔎 Année {m.group(1)} trouvée dans l'URL")
            return m.group(1)
    except Exception:
        pass

    # 1c) FrenchStream : ligne "-16 - 2024 - Science-Fiction & Fantastique, Drame - 54 min"
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text
        m = re.search(r'(?<!\d)((?:19|20)\d{2})\s*-\s*[^\n]{3,80}?-\s*\d+\s*min', body_text)
        if m and plausible(m.group(1)):
            logger.info(f"   🔎 Année {m.group(1)} trouvée via la ligne d'infos (genre + durée)")
            return m.group(1)
    except Exception:
        pass

    # 2) Balises meta standard
    try:
        for sel in ['meta[itemprop="datePublished"]', 'meta[property="video:release_date"]']:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                content = el.get_attribute("content") or ""
                m = re.search(r'(\d{4})', content)
                if m and plausible(m.group(1)):
                    logger.info(f"   🔎 Année {m.group(1)} trouvée via la balise meta '{sel}'")
                    return m.group(1)
    except Exception:
        pass

    # 3) Sélecteurs FR classiques
    for sel in [".s-annee", ".release-year", ".annee", "span.year", ".data span", "#info", ".sgeneros"]:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                m = re.search(r'\b(19|20)\d{2}\b', el.text)
                if m and plausible(m.group(0)):
                    logger.info(f"   🔎 Année {m.group(0)} trouvée via le sélecteur '{sel}' : {el.text[:80]!r}")
                    return m.group(0)
        except Exception:
            continue

    # 4) Dernier recours dans le texte body
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text
        m = re.search(r'(?:Ann[ée]e|Year|Sortie)\D{0,10}(\d{4})', body_text, flags=re.IGNORECASE)
        if m and plausible(m.group(1)):
            logger.info(f"   🔎 Année {m.group(1)} trouvée via le texte du body : {m.group(0)!r}")
            return m.group(1)
    except Exception:
        pass

    return None


# =====================================================================
# API / BASE DE DONNÉES
# =====================================================================
def get_admin_token():
    if not ADMIN_PASSWORD or not TMDB_API_KEY:
        logger.error("❌ Variables d'environnement manquantes : ADMIN_PASSWORD et/ou TMDB_API_KEY.")
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


def fetch_all_items(base_url, headers):
    """Toutes les fiches de ta base (pagine si le serveur limite le nombre de résultats)."""
    items, seen = [], set()
    got_answer = False
    for page in range(1, DB_MAX_PAGES + 1):
        url = f"{base_url}?limit={DB_PAGE_SIZE}&page={page}"
        try:
            res = requests.get(url, headers=headers, timeout=180)
        except Exception as e:
            logger.warning(f"⚠️ {base_url} inaccessible : {str(e)[:120]}")
            return items if got_answer else None
        if res.status_code != 200:
            return items if got_answer else None
        try:
            data = res.json()
        except Exception:
            return items if got_answer else None
        batch = data if isinstance(data, list) else (
            data.get("movies") or data.get("data") or data.get("results") or []
        )
        got_answer = True
        fresh = 0
        for m in batch:
            if not isinstance(m, dict):
                continue
            key = str(m.get("id") or m.get("_id") or f"{m.get('title')}|{m.get('year')}|{m.get('type')}")
            if key in seen:
                continue
            seen.add(key)
            items.append(m)
            fresh += 1
        if fresh == 0:
            break
    return items


def get_existing_database(token):
    """
    Lit TOUTE ta base et renvoie {"by_tmdb": {...}, "by_key": {...}} pour les SÉRIES (une fiche = une saison),
    ou None si la base est illisible (on s'arrête alors pour ne pas créer de doublons).
    """
    logger.info("🔍 Lecture de ta base de données (films + séries)...")
    headers = {"authorization": token}

    for base in (f"{API_URL}/api/admin/movies", f"{API_URL}/api/movies"):
        items = fetch_all_items(base, headers)
        if items is None:
            continue
        by_tmdb, by_key = {}, {}
        n_series = 0
        for m in items:
            if str(m.get("type") or "film").lower() == "film":
                continue
            if not m.get("title"):
                continue
            n_series += 1
            by_key[build_match_key(m["title"], m.get("year"))] = m
            if m.get("tmdb_id"):
                s_num_existing = extract_season_number(m["title"]) or 1
                by_tmdb[f"{m['tmdb_id']}_s{s_num_existing}"] = m
        logger.info(f"✅ {len(items)} fiches en base dont {n_series} saisons de séries.")
        return {"by_tmdb": by_tmdb, "by_key": by_key}
    return None


def push_to_mongodb(payload, token, is_update=False):
    headers = {"Content-Type": "application/json", "authorization": token}
    try:
        payload.pop("is_japanese_anime", None)
        res = requests.post(f"{API_URL}/api/admin/movies", json=payload, headers=headers, timeout=60)
        if res.status_code == 200:
            action = "Mise à jour (épisodes / liens) réussie" if is_update else "Création réussie"
            logger.info(f"      [MONGODB] 🚀 {action} : {payload.get('title')}")
            return True
        logger.warning(f"      [MONGODB] ⚠️ Erreur sauvegarde : {res.text}")
    except Exception as e:
        logger.error(f"      [MONGODB] ❌ Erreur réseau : {e}")
    return False


# =====================================================================
# TMDB (SÉRIES / SAISONS)
# =====================================================================
def get_youtube_trailer_for_season(movie_id, season_num):
    season_url = f"https://api.themoviedb.org/3/tv/{movie_id}/season/{season_num}/videos"
    tv_url = f"https://api.themoviedb.org/3/tv/{movie_id}/videos"

    def fetch_video(url, lang):
        try:
            res = requests.get(url, params={"api_key": TMDB_API_KEY, "language": lang}, timeout=15).json()
            videos = res.get("results", [])
            trailer = next((v for v in videos if v.get("type") == "Trailer" and v.get("site") == "YouTube"), None)
            if not trailer:
                trailer = next((v for v in videos if v.get("site") == "YouTube"), None)
            if trailer and trailer.get("key"):
                return f"https://www.youtube.com/embed/{trailer['key']}"
        except Exception:
            pass
        return ""

    vid = fetch_video(season_url, "fr-FR")
    if not vid:
        vid = fetch_video(season_url, "en-US")
    if not vid:
        vid = fetch_video(tv_url, "fr-FR")
    if not vid:
        vid = fetch_video(tv_url, "en-US")
    if not vid:
        vid = fetch_video(tv_url, "")
    return vid


def fallback_metadata(clean_title, season_num, year_hint=None):
    """Fiche minimale utilisée quand TMDB ne trouve rien : à compléter à la main ensuite."""
    return {
        "tmdb_id": "",
        "title": f"{clean_title} - Saison {season_num}",
        "type": "serie",
        "year": str(year_hint or ""),
        "rating": "",
        "genre": "",
        "desc": "",
        "dir": "",
        "cast": "",
        "poster": "",
        "trailer": "",
        "is_japanese_anime": False
    }


def fetch_tmdb_season_metadata(title, season_num, year_hint=None):
    try:
        clean_search_title = re.sub(r'\s+(saison\s*\d+|season\s*\d+|\d+)$', '', title, flags=re.IGNORECASE).strip()
        clean_search_title = clean_search_title.replace("’", "'").replace("'", " ")
        if not clean_search_title:
            clean_search_title = title

        # --- 1. RECHERCHE EN FRANÇAIS ---
        search_url_fr = f"https://api.themoviedb.org/3/search/tv?api_key={TMDB_API_KEY}&language=fr-FR&query={requests.utils.quote(clean_search_title)}"
        results = requests.get(search_url_fr, timeout=15).json().get("results") or []

        # --- 2. RECHERCHE EN ANGLAIS (Fallback si rien trouvé en FR) ---
        if not results:
            logger.info(f"   🔍 Aucun résultat TMDB en FR pour '{clean_search_title}', tentative en EN...")
            search_url_en = f"https://api.themoviedb.org/3/search/tv?api_key={TMDB_API_KEY}&language=en-US&query={requests.utils.quote(clean_search_title)}"
            results = requests.get(search_url_en, timeout=15).json().get("results") or []

        if not results:
            return None

        # --- 3. FILTRAGE STRICT SUR L'ANNÉE ---
        movie = None
        if year_hint:
            for r in results:
                air_date = r.get("first_air_date") or ""
                if air_date[:4] == str(year_hint):
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
                    logger.info(f"   📅 Année approchante trouvée: {best.get('first_air_date')[:4]} pour la demande {year_hint}")
                else:
                    # L'année de la page est peut-être fausse : on se rabat sur le titre
                    strong = [
                        r for r in results
                        if max(title_similarity(clean_search_title, r.get('name')),
                               title_similarity(clean_search_title, r.get('original_name'))) >= 0.85
                    ]
                    if len(strong) == 1:
                        movie = strong[0]
                        logger.warning(
                            f"   ⚠️ Année de la page ({year_hint}) incohérente, mais un seul titre correspond : "
                            f"{movie.get('name')} ({movie.get('first_air_date')}). On le retient."
                        )
                    else:
                        logger.warning(
                            f"   ⚠️ REJET : année {year_hint} incohérente et {len(strong)} titres candidats "
                            f"(Meilleur par année : {best.get('name')} / {best.get('first_air_date')}). "
                            f"Recherche annulée pour éviter les erreurs de mapping."
                        )
                        return None
        else:
            # Pas d'année : on ne devine plus si plusieurs séries portent le même titre
            strong = [
                r for r in results
                if max(title_similarity(clean_search_title, r.get('name')),
                       title_similarity(clean_search_title, r.get('original_name'))) >= 0.85
            ]
            if len(strong) > 1:
                logger.warning(f"   ⚠️ REJET : pas d'année et {len(strong)} séries portent ce titre. Abandon pour éviter une mauvaise fiche.")
                return None
            movie = strong[0] if strong else results[0]

        movie_id = movie["id"]

        # --- 4. RÉCUPÉRATION DES DÉTAILS (TOUJOURS EN FRANÇAIS) ---
        details_url = f"https://api.themoviedb.org/3/tv/{movie_id}?api_key={TMDB_API_KEY}&language=fr-FR&append_to_response=credits"
        det_res = requests.get(details_url, timeout=15).json()

        # La saison demandée existe-t-elle sur TMDB ? Si non, on garde les infos générales de la série.
        season_exists = True
        try:
            season_exists = any(s.get("season_number") == int(season_num) for s in det_res.get("seasons", []))
        except Exception:
            season_exists = True

        if not season_exists:
            logger.warning(
                f"   ⚠️ TMDB n'a pas (encore) la saison {season_num} de '{det_res.get('name')}'. "
                f"On utilise les infos générales de la série."
            )

        origin_countries = det_res.get("origin_country", [])
        genres = [g["name"] for g in det_res.get("genres", [])]

        if "Animation" in genres and "JP" in origin_countries:
            return {"is_japanese_anime": True}

        directors = []
        if "created_by" in det_res and det_res["created_by"]:
            directors = [c["name"] for c in det_res["created_by"]]
        if det_res.get("credits") and det_res["credits"].get("crew"):
            dir_jobs = ["Director", "Series Director", "Creator", "Réalisation"]
            for c in det_res["credits"]["crew"]:
                if c.get("job") in dir_jobs or c.get("department") == "Directing":
                    if c["name"] not in directors:
                        directors.append(c["name"])
        if not directors and "production_companies" in det_res and det_res["production_companies"]:
            directors = [det_res["production_companies"][0]["name"]]

        cast = [c["name"] for c in det_res.get("credits", {}).get("cast", [])[:8]]

        # Plateformes (Netflix, Prime, HBO...) et studios (Pixar, DC, Warner...)
        for label in detect_platform_genres(det_res, clean_search_title):
            if label not in genres:
                genres.append(label)

        # Infos de la saison : seulement si elle existe sur TMDB
        season_res = {}
        if season_exists:
            season_url = f"https://api.themoviedb.org/3/tv/{movie_id}/season/{season_num}?api_key={TMDB_API_KEY}&language=fr-FR"
            try:
                season_res = requests.get(season_url, timeout=15).json() or {}
            except Exception:
                season_res = {}

        poster_path = season_res.get('poster_path') or det_res.get('poster_path')
        overview = season_res.get('overview') or det_res.get("overview", "")
        backdrop_path = det_res.get('backdrop_path')

        poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else ""
        backdrop_url = f"https://image.tmdb.org/t/p/original{backdrop_path}" if backdrop_path else ""
        final_poster = f"{poster_url}|||{backdrop_url}" if backdrop_url else poster_url

        trailer_link = get_youtube_trailer_for_season(movie_id, season_num)

        base_title = det_res.get("name") or clean_search_title
        final_title = f"{base_title} - Saison {season_num}"

        # Année : celle de la saison si dispo, sinon l'année détectée sur la page, sinon celle de la série
        final_year = (
            (season_res.get("air_date") or "")[:4]
            or (str(year_hint) if (year_hint and not season_exists) else "")
            or (det_res.get("first_air_date") or "")[:4]
            or "2026"
        )

        return {
            "tmdb_id": str(movie_id),
            "title": final_title,
            "type": "serie",
            "year": final_year,
            "rating": str(round(det_res.get("vote_average", 8.0), 1)),
            "genre": ", ".join(genres),
            "desc": overview,
            "dir": ", ".join(directors) if directors else "Inconnu",
            "cast": ", ".join(cast),
            "poster": final_poster,
            "trailer": trailer_link,
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
            if new_ep.get("iframe_vidzy"):
                ep_dict[title]["iframe_vidzy"] = new_ep["iframe_vidzy"]
            if new_ep.get("iframe_uqload"):
                ep_dict[title]["iframe_uqload"] = new_ep["iframe_uqload"]
            if new_ep.get("iframe_vidzy_vostfr"):
                ep_dict[title]["iframe_vidzy_vostfr"] = new_ep["iframe_vidzy_vostfr"]
            if new_ep.get("iframe_uqload_vostfr"):
                ep_dict[title]["iframe_uqload_vostfr"] = new_ep["iframe_uqload_vostfr"]
        else:
            ep_dict[title] = new_ep

    def extract_nums(t):
        nums = re.findall(r'\b\d+\b', str(t))
        return int(nums[-1]) if nums else 0

    return sorted(list(ep_dict.values()), key=lambda x: extract_nums(x.get("title", "")))


def build_keys_and_id(tmdb_data, season_full_title, clean_title, s_num, year_hint):
    """Clés de correspondance + ID unifié, valables même sans tmdb_id (fiche de secours)."""
    tmdb_id = tmdb_data.get("tmdb_id") or ""
    tmdb_key = f"{tmdb_id}_s{s_num}" if tmdb_id else None
    match_key = build_match_key(season_full_title, tmdb_data.get("year") or year_hint)
    slug = tmdb_id or clean_text_for_comparison(clean_title)
    unified_id = f"cinelk10_serie_{slug}_s{s_num}"
    return tmdb_key, match_key, unified_id


def apply_tmdb_to_existing(matched_db_season, tmdb_data):
    """Met à jour titre/tmdb_id d'une fiche existante uniquement si TMDB a vraiment répondu.
    Les genres TMDB manquants sont AJOUTÉS sans rien supprimer de ce que tu as mis à la main."""
    if tmdb_data.get("tmdb_id"):
        matched_db_season["title"] = tmdb_data["title"]
        matched_db_season["tmdb_id"] = tmdb_data["tmdb_id"]

        old_genres = [g.strip() for g in (matched_db_season.get("genre") or "").split(",") if g.strip()]
        new_genres = [g.strip() for g in (tmdb_data.get("genre") or "").split(",") if g.strip()]
        old_lower = {g.lower() for g in old_genres}
        for g in new_genres:
            if g.lower() not in old_lower:
                old_genres.append(g)
                old_lower.add(g.lower())
        matched_db_season["genre"] = ", ".join(old_genres)


# =====================================================================
# NAVIGATION SELENIUM
# =====================================================================
ANTIBOT_MARKERS = ("verification de votre navigateur", "vérification de votre navigateur", "protection anti-robot")


def is_antibot_page(driver):
    """True si la page affichée est l'écran « Vérification de votre navigateur... »."""
    try:
        txt = driver.execute_script(
            "return ((document.body && document.body.textContent) || '').slice(0, 800);"
        ) or ""
    except Exception:
        return False
    txt = txt.lower()
    return any(m in txt for m in ANTIBOT_MARKERS)


def wait_for_antibot(driver, timeout=ANTIBOT_WAIT):
    """Attend la fin de l'écran de vérification (redirection automatique). Renvoie True si passé."""
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
    """driver.get avec timeout court + attente de l'écran de vérification s'il apparaît."""
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


def save_debug_page(driver):
    try:
        with open(DEBUG_FILE, "w", encoding="utf-8") as f:
            f.write(driver.page_source or "")
        logger.info(f"   🧪 Page sauvegardée dans {os.path.basename(DEBUG_FILE)} (pour diagnostiquer).")
    except Exception as e:
        logger.warning(f"   ⚠️ Impossible de sauvegarder la page de debug : {e}")


# =====================================================================
# LISTE DES SAISONS D'UNE PAGE
# =====================================================================
# Chaque affiche : <div class="short"> ... <a class="short-poster" href="/index.php?newsid=123"> ...
#   <div class="short-title">Titre</div> ... <div class="short-year">2026</div>
CARDS_JS = r"""
var list = document.querySelectorAll('#dle-content .short');
if (!list.length) { list = document.querySelectorAll('.short'); }
var out = [];
list.forEach(function(c){
  var a = c.querySelector('a.short-poster[href]') || c.querySelector('a[href*="newsid="]') || c.querySelector('a[href]');
  if (!a) { return; }
  var t = c.querySelector('.short-title');
  var y = c.querySelector('.short-year a') || c.querySelector('.short-year') || c.querySelector('.fss-yr');
  out.push({href: a.href, title: t ? t.textContent : '', year: y ? y.textContent : '', id: c.getAttribute('data-fss-id') || ''});
});
return out;
"""


def extract_newsid(url):
    m = re.search(r'[?&]newsid=(\d+)', url or "")
    if m:
        return m.group(1)
    m = re.search(r'/(\d+)-[^/]*\.html', urlparse(url or "").path)
    return m.group(1) if m else None


def collect_cards(driver):
    """Saisons affichées sur la page : liste de {id, url, title, year}."""
    raw = driver.execute_script(CARDS_JS) or []
    cards, seen = [], set()
    for r in raw:
        nid = extract_newsid(r.get("href") or "") or str(r.get("id") or "").strip()
        if not nid or nid in seen:
            continue
        seen.add(nid)
        cards.append({
            "id": nid,
            "url": f"{SITE_BASE}/index.php?newsid={nid}",
            "title": (r.get("title") or "").strip(),
            "year": (r.get("year") or "").strip(),
        })
    return cards


def wait_for_cards(driver, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if is_antibot_page(driver):
                wait_for_antibot(driver)
            cards = collect_cards(driver)
        except Exception:
            cards = []
        if cards:
            return cards
        time.sleep(1)
    return []


def page_url(page):
    """Page 1 = index.php?do=cat&category=s-tv ; pages suivantes = index.php?cstart=N."""
    return FIRST_PAGE_URL if page == 1 else PAGE_URL.format(n=page)


def load_listing_page(driver, page):
    """Ouvre la page `page` de la liste des séries (2 essais) et renvoie ses saisons."""
    url = page_url(page)
    for attempt in range(2):
        safe_get(driver, url)
        cards = wait_for_cards(driver)
        if cards:
            return cards
        if attempt == 0:
            logger.info("   🔄 Aucune série affichée, je recharge la page...")
    return []


def detect_last_page(driver):
    """Dernier numéro de page, lu dans la pagination du site (liens ...cstart=N...)."""
    try:
        hrefs = driver.execute_script(
            "return Array.from(document.querySelectorAll('.pagi-nav a[href], #bottom-nav a[href]'))"
            ".map(function(a){return a.getAttribute('href');});"
        ) or []
    except Exception:
        return None
    nums = []
    for h in hrefs:
        m = re.search(r'cstart=(\d+)', h or "")
        if m:
            nums.append(int(m.group(1)))
    return max(nums) if nums else None


def card_in_db(card, ctx):
    """True si la saison de cette affiche (titre + année) est déjà en base."""
    year = (card.get("year") or "").strip()
    if not re.fullmatch(r"(19|20)\d{2}", year):
        return False
    return build_match_key(card.get("title"), year) in ctx["by_key"]


# =====================================================================
# EXTRACTION DES ÉPISODES (pages avec #serie-config)
# =====================================================================
def detect_site_type(driver):
    try:
        if driver.find_elements(By.ID, "serie-config"):
            return "frenchstream"
    except Exception:
        pass
    return "autre"


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
        iframe_el = driver.find_element(By.ID, "seriePlayer")
        src = iframe_el.get_attribute("src")
        if src and server_name in src.lower():
            if server_name == "vidzy":
                src = re.sub(r'vidzy\.[a-z]+', 'vidzy.lol', src, flags=re.IGNORECASE)
            elif server_name == "uqload":
                src = re.sub(r'uqload\.[a-z]+', 'uqload.vc', src, flags=re.IGNORECASE)
            return f'<IFRAME SRC="{src}" FRAMEBORDER=0 MARGINWIDTH=0 MARGINHEIGHT=0 SCROLLING=NO WIDTH=640 HEIGHT=360 allowfullscreen></IFRAME>'
    except Exception:
        pass
    return ""


def get_episode_numbers(driver, ep_type):
    selector = f"#{ep_type}-episodes .ep-title"
    try:
        nums = []
        for el in driver.find_elements(By.CSS_SELECTOR, selector):
            num = el.get_attribute("data-num")
            if num:
                nums.append(num)
        return nums
    except Exception:
        return []


def click_episode(driver, ep_type, ep_num):
    try:
        selector = f'.ep-title[data-type="{ep_type}"][data-num="{ep_num}"]'
        el = driver.find_element(By.CSS_SELECTOR, selector)
        driver.execute_script("arguments[0].click();", el)
        time.sleep(3)
        return True
    except Exception as e:
        logger.warning(f"      Impossible de cliquer sur l'épisode {ep_type} {ep_num} : {str(e)[:100]}")
        return False


def process_season_episodes(driver, existing_eps_dict):
    episodes_data = []

    vf_nums = get_episode_numbers(driver, "vf")
    truefrench_nums = get_episode_numbers(driver, "truefrench")
    vostfr_nums = get_episode_numbers(driver, "vostfr")

    all_nums = sorted(set(vf_nums) | set(truefrench_nums) | set(vostfr_nums), key=lambda x: int(x))
    logger.info(f"    {len(all_nums)} épisodes détectés (VF: {len(vf_nums)} | TRUEFRENCH: {len(truefrench_nums)} | VOSTFR: {len(vostfr_nums)}).")

    for num in all_nums:
        ep_title = f"Épisode {num}"
        old_ep = existing_eps_dict.get(ep_title, {})
        has_vf_v = bool(old_ep.get("iframe_vidzy"))
        has_vf_u = bool(old_ep.get("iframe_uqload"))
        has_vost_v = bool(old_ep.get("iframe_vidzy_vostfr"))
        has_vost_u = bool(old_ep.get("iframe_uqload_vostfr"))

        vf_vidzy, vf_uqload = "", ""
        vostfr_vidzy, vostfr_uqload = "", ""

        # Traitement VF (Vidzy + Uqload)
        if not (has_vf_v and has_vf_u):
            if num in truefrench_nums or num in vf_nums:
                logger.info(f"      🔍 [VF] {ep_title} en cours de scrape (Vidzy/Uqload)...")
                ep_type = "truefrench" if num in truefrench_nums else "vf"
                if click_episode(driver, ep_type, num):
                    if not has_vf_v and select_server(driver, "vidzy"):
                        vf_vidzy = extract_current_src(driver, "vidzy")
                    if not has_vf_u and select_server(driver, "uqload"):
                        vf_uqload = extract_current_src(driver, "uqload")

        # Traitement VOSTFR (Vidzy + Uqload)
        if num in vostfr_nums and not (has_vost_v and has_vost_u):
            logger.info(f"      🔍 [VOSTFR] {ep_title} en cours de scrape (Vidzy/Uqload)...")
            if click_episode(driver, "vostfr", num):
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
                "iframe_uqload_vostfr": vostfr_uqload or old_ep.get("iframe_uqload_vostfr", "")
            })

    return episodes_data


# =====================================================================
# TRAITEMENT D'UNE SAISON
# =====================================================================
def read_raw_title(driver):
    for sel in ("#s-title", "h1"):
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                t = get_text(el)
                if t:
                    return t
        except Exception:
            continue
    return ""


def wait_for_title(driver, timeout=10):
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#s-title, h1"))
        )
    except Exception:
        pass


def process_serie(driver, serie_url, token, ctx):
    """
    Renvoie : "added" (créée ou mise à jour), "skipped" (déjà en base / déjà à jour),
              "ignored" (anime japonais), "failed".
    """
    safe_get(driver, serie_url)

    raw_title, site_type = "", "autre"
    # 2 tentatives : on laisse la page finir de charger (titre + #serie-config) puis on réessaie
    for attempt in range(2):
        wait_for_title(driver)
        site_type = detect_site_type(driver)
        raw_title = read_raw_title(driver)
        if raw_title and site_type == "frenchstream":
            break
        if attempt == 0:
            logger.info("   🔄 Page incomplète, je laisse la page charger et je réessaie...")
            time.sleep(4)

    if not raw_title:
        logger.warning("   ⚠️ Titre vide, on passe.")
        return "failed"
    if site_type != "frenchstream":
        logger.warning("   ⚠️ Page non reconnue (pas de #serie-config) : voir debug_serie.html.")
        save_debug_page(driver)
        return "failed"

    time.sleep(1)  # laisse la liste des épisodes (JS) s'afficher

    clean_title = re.sub(r'\s*-?\s*saisons?\s*0*\d+\s*$', '', raw_title, flags=re.IGNORECASE).strip()
    s_num = extract_season_number(raw_title) or 1
    season_full_title = f"{clean_title} - Saison {s_num}"

    year_hint = extract_year_hint(driver, raw_title)
    if year_hint:
        logger.info(f"   📅 Année détectée sur la page : {year_hint}")
    else:
        logger.warning("   ⚠️ Aucune année détectée sur la page — désambiguïsation TMDB moins fiable.")

    logger.info(f"   🎬 TRAITEMENT : {season_full_title}")

    tmdb_data = fetch_tmdb_season_metadata(clean_title, s_num, year_hint=year_hint)
    if not tmdb_data:
        logger.warning("   ⚠️ TMDB introuvable : création d'une fiche minimale (à compléter à la main).")
        tmdb_data = fallback_metadata(clean_title, s_num, year_hint)

    if tmdb_data.get("is_japanese_anime"):
        logger.info(f"   🇯🇵 '{clean_title}' est un anime japonais : ignoré.")
        return "ignored"

    tmdb_key, match_key, unified_id = build_keys_and_id(tmdb_data, season_full_title, clean_title, s_num, year_hint)
    matched = (ctx["by_tmdb"].get(tmdb_key) if tmdb_key else None) or ctx["by_key"].get(match_key)

    if matched and SKIP_ALREADY_IN_DB:
        logger.info("   ⏭️ NEXT : déjà en base.")
        return "skipped"

    if not matched:
        logger.info(f"      ✨ [NOUVEAU] '{season_full_title}' ({tmdb_data.get('year')}) n'existe pas. Création intégrale...")
    else:
        logger.info("      🛠️ [SYNCHRONISATION] Fiche existante trouvée. Recherche de nouveautés...")

    existing_eps_dict = {}
    if matched:
        existing_eps_dict = {ep["title"]: ep for ep in (matched.get("episodes") or []) if isinstance(ep, dict) and ep.get("title")}

    extracted = process_season_episodes(driver, existing_eps_dict)

    if not extracted:
        if matched:
            logger.info(f"      ✅ '{season_full_title}' est déjà à jour à 100%.")
            return "skipped"
        logger.warning("   ⚠️ Aucun lien valide (Vidzy/Uqload) pour cette saison.")
        save_debug_page(driver)
        return "failed"

    if matched:
        apply_tmdb_to_existing(matched, tmdb_data)
        matched["episodes"] = merge_episodes(matched.get("episodes", []), extracted)
        ok = push_to_mongodb(matched, token, is_update=True)
        if ok:
            if tmdb_key:
                ctx["by_tmdb"][tmdb_key] = matched
            ctx["by_key"][match_key] = matched
        return "added" if ok else "failed"

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
        "episodes": merge_episodes([], extracted)
    }
    if not tmdb_data["tmdb_id"]:
        logger.warning(f"      ✍️ Fiche créée SANS données TMDB (id: {unified_id}) : complète-la à la main dans l'admin.")

    if push_to_mongodb(payload, token):
        if tmdb_key:
            ctx["by_tmdb"][tmdb_key] = payload
        ctx["by_key"][match_key] = payload
        return "added"
    return "failed"


# =====================================================================
# SAUVEGARDE LOCALE DE LA PROGRESSION
# =====================================================================
def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    """Écriture sécurisée (fichier temporaire puis remplacement) : pas de fichier corrompu si ça plante."""
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning(f"⚠️ Impossible d'écrire {os.path.basename(path)} : {e}")


def load_done_ids():
    try:
        with open(DONE_FILE, "r", encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip()}
    except Exception:
        return set()


def append_done(nid):
    try:
        with open(DONE_FILE, "a", encoding="utf-8") as f:
            f.write(str(nid) + "\n")
    except Exception as e:
        logger.warning(f"⚠️ Impossible d'écrire {os.path.basename(DONE_FILE)} : {e}")


def is_dead_browser(err):
    msg = str(err).lower()
    return any(k in msg for k in ("invalid session", "disconnected", "no such window", "chrome not reachable", "target window already closed"))


def run_serie(driver, url, token, ctx):
    """Traite une saison. Relance Chrome s'il a planté. Renvoie (résultat, driver)."""
    try:
        return process_serie(driver, url, token, ctx), driver
    except Exception as e:
        logger.warning(f"   ⚠️ Erreur sur cette saison, on passe à la suivante : {str(e)[:150]}")
        if is_dead_browser(e):
            logger.warning("   🔄 Chrome a planté, je le relance...")
            try:
                driver.quit()
            except Exception:
                pass
            driver = build_driver()
        else:
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
        return "failed", driver


# =====================================================================
# MAIN
# =====================================================================
def main():
    token = get_admin_token()
    if not token:
        return

    # 1) Avant tout : on récupère toute ta base pour éviter les doublons
    ctx = get_existing_database(token)
    if ctx is None:
        logger.error("❌ Base de données illisible : j'arrête pour ne pas créer de doublons. Relance dans une minute (Render met parfois du temps à se réveiller).")
        return

    done_ids = load_done_ids()
    failed_urls = load_json(FAILED_FILE, [])
    if not isinstance(failed_urls, list):
        failed_urls = []

    # 2) Reprise là où le script s'était arrêté
    progress = load_json(PROGRESS_FILE, {})
    start_page = START_PAGE
    if RESUME and isinstance(progress, dict):
        last_done = progress.get("last_page_done")
        if progress.get("finished"):
            logger.info("✅ Le dernier passage était terminé : je repars de la page 1 pour récupérer les nouveautés.")
        elif isinstance(last_done, int) and last_done >= START_PAGE:
            start_page = last_done + 1
            logger.info(f"▶️ Reprise à la page {start_page} (dernière page terminée : {last_done}).")

    try:
        driver = build_driver()
    except Exception as e:
        logger.error(f"❌ Impossible de lancer Chrome : {e}")
        return

    stats = {"added": 0, "skipped": 0, "ignored": 0, "failed": 0}
    last_page = END_PAGE
    page = start_page
    empty_pages = 0
    stopped_early = False
    finished = False

    try:
        # 3) On scrape directement : page par page, jusqu'à la dernière
        while True:
            if last_page and page > last_page:
                finished = True
                break

            logger.info(f"\n==================== PAGE {page}/{last_page or '?'} ====================")

            try:
                cards = load_listing_page(driver, page)
            except Exception as e:
                if is_dead_browser(e):
                    logger.warning("   🔄 Chrome a planté, je le relance...")
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    driver = build_driver()
                else:
                    logger.warning(f"   ⚠️ Erreur de chargement de la page {page} : {str(e)[:120]}")
                cards = []

            if not cards:
                empty_pages += 1
                logger.warning(f"   ⚠️ Page {page} illisible ({empty_pages}/{MAX_EMPTY_PAGES}).")
                if empty_pages >= MAX_EMPTY_PAGES:
                    logger.error(f"   ⛔ Je m'arrête (blocage, site en panne ou fin des pages). Relance plus tard : le script reprendra à la page {page}.")
                    stopped_early = True
                    break
                time.sleep(10 * empty_pages)
                continue    # on retente la MÊME page (on n'en saute aucune)
            empty_pages = 0

            if END_PAGE is None:
                detected = detect_last_page(driver)
                if detected and detected != last_page:
                    last_page = detected
                    logger.info(f"   📚 Dernière page du site : {last_page}")
            if last_page is None:
                last_page = END_PAGE_IF_UNKNOWN

            # Retire de la liste les saisons déjà traitées ou déjà présentes dans ta base
            todo = []
            for c in cards:
                if c["id"] in done_ids:
                    stats["skipped"] += 1
                    continue
                if SKIP_ALREADY_IN_DB and card_in_db(c, ctx):
                    stats["skipped"] += 1
                    done_ids.add(c["id"])
                    append_done(c["id"])
                    continue
                todo.append(c)

            logger.info(f"   📄 {len(cards)} saisons sur la page → {len(todo)} à scraper "
                        f"({len(cards) - len(todo)} déjà en base / traitées).")

            page_failed = 0
            for i, c in enumerate(todo, 1):
                logger.info(f"\n   [p{page} · {i}/{len(todo)}] ➡️ {c['title'] or c['url']}")
                result, driver = run_serie(driver, c["url"], token, ctx)
                stats[result] += 1

                if result in ("added", "skipped", "ignored"):
                    done_ids.add(c["id"])
                    append_done(c["id"])          # sauvegarde immédiate après CHAQUE saison
                    if c["url"] in failed_urls:
                        failed_urls.remove(c["url"])
                        save_json(FAILED_FILE, failed_urls)
                else:
                    page_failed += 1
                    if c["url"] not in failed_urls:
                        failed_urls.append(c["url"])
                        save_json(FAILED_FILE, failed_urls)

                if result == "added":
                    time.sleep(DELAY_BETWEEN_SERIES)

            # Page terminée : sauvegarde de la progression
            save_json(PROGRESS_FILE, {"last_page_done": page, "last_page": last_page, "finished": False})
            logger.info(f"   💾 Page {page} terminée et sauvegardée ({page_failed} échec(s)). "
                        f"Total → ajoutées/mises à jour : {stats['added']} | déjà en base : {stats['skipped']} | échecs : {stats['failed']}")
            page += 1

        if finished:
            save_json(PROGRESS_FILE, {"last_page_done": last_page, "last_page": last_page, "finished": True})
            logger.info("\n🏁 Toutes les pages ont été parcourues.")

        # 4) Deuxième chance pour les saisons en échec (titre vide, vérification, lecteurs lents...)
        if RETRY_FAILED_AT_END and failed_urls and not stopped_early:
            logger.info(f"\n🔁 Nouvelle tentative pour {len(failed_urls)} saison(s) en échec...")
            for url in list(failed_urls):
                logger.info(f"\n   ➡️ {url}")
                result, driver = run_serie(driver, url, token, ctx)
                if result in ("added", "skipped", "ignored"):
                    stats["failed"] = max(0, stats["failed"] - 1)
                    stats[result] += 1
                    nid = extract_newsid(url)
                    if nid:
                        done_ids.add(nid)
                        append_done(nid)
                    failed_urls.remove(url)
                    save_json(FAILED_FILE, failed_urls)
                if result == "added":
                    time.sleep(DELAY_BETWEEN_SERIES)

    except KeyboardInterrupt:
        logger.info("\n⏹️ Arrêt demandé (Ctrl+C). La progression est sauvegardée : au prochain lancement, je reprends à la page "
                    f"{page}.")
    except Exception as e:
        logger.error(f"Une erreur est survenue : {e} — la progression est sauvegardée, relance le script.")
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    logger.info("\n==========================================")
    logger.info(f"✅ Ajoutées/mises à jour : {stats['added']} | ⏭️ Déjà en base : {stats['skipped']} | "
                f"🚫 Ignorées (anime JP) : {stats['ignored']} | ❌ Échecs : {stats['failed']}")
    if failed_urls:
        logger.info(f"Saisons en échec (retentées au prochain lancement) : {len(failed_urls)}")
        for u in failed_urls[:50]:
            logger.info(f"   - {u}")
        if len(failed_urls) > 50:
            logger.info(f"   ... et {len(failed_urls) - 50} autres")
    logger.info("==========================================")


if __name__ == "__main__":
    main()