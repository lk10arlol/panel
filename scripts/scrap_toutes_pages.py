"""
Scrap des FILMS de fs01.lol : parcourt la liste des films page par page
(page 1 = https://fs01.lol/films/ ; pages 2 à 1337 = index.php?cstart=N&do=cat&category=films).

Script AUTONOME (films uniquement, pas les séries).

Comment ça marche :
  1) Au lancement, il lit TOUTE ta base (films + séries) pour éviter les doublons.
  2) Il commence directement à scraper : page 1, 2, 3... jusqu'à la page 1337.
  3) Pour chaque page, il retire les films déjà en base et ne scrape que les nouveaux.
  4) Dès qu'une page est terminée (et après chaque film), la progression est sauvegardée.
     Si le script s'arrête (bug, blocage anti-bot, Ctrl+C...), au prochain lancement
     il reprend à la page où il s'était arrêté.

Lancement :
    py scrap_toutes_pages.py
(les identifiants sont dans la section CONFIG ci-dessous)
"""
import os
import logging
import time
import re
import json
import hashlib
import unicodedata
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

# Page 1 = index.php?do=cat&category=films (sans cstart) ; pages suivantes = index.php?cstart=N (N = numéro de page)
FIRST_PAGE_URL = "https://fs01.lol/index.php?do=cat&category=films"
PAGE_URL = "https://fs01.lol/index.php?cstart={n}&do=cat&category=films"

START_PAGE = 1
END_PAGE = 1337              # dernière page fixée (pas de détection automatique)
END_PAGE_IF_UNKNOWN = 1337   # utilisé seulement si END_PAGE = None et que la détection échoue

# True = reprend à la page où le script s'est arrêté au lancement suivant
RESUME = True

# Si une page ne s'affiche pas (blocage anti-bot, site en panne), le script la retente
# puis s'arrête après N essais. La progression est sauvegardée : relance et il reprend ici.
MAX_EMPTY_PAGES = 3

# True = à la fin, retente une fois les films en échec
RETRY_FAILED_AT_END = True

PAGE_LOAD_TIMEOUT = 30
DELAY_BETWEEN_FILMS = 2  # secondes

# True = sur chaque page, retire les films déjà en base AVANT de les ouvrir (comparaison titre + année
# lus sur l'affiche). Mets False pour tout ouvrir (plus lent, aucun risque d'homonyme).
PREFILTER_BY_TITLE = True

# Lecture de ta base : nombre de fiches demandées par requête (la pagination est gérée si besoin)
DB_PAGE_SIZE = 50000
DB_MAX_PAGES = 100

# Page « Vérification de votre navigateur... » : temps max d'attente de la redirection automatique
ANTIBOT_WAIT = 60  # secondes

# Profil Chrome dédié au script : une fois la vérification validée, Chrome garde le cookie
# et ne la redemande plus (ou beaucoup moins) aux lancements suivants.
# Ferme les autres fenêtres Chrome ouvertes avec ce profil avant de lancer le script.
PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chrome_profile_scraper")

# Si la vérification ne passe pas toute seule, le script se met en pause :
# tu valides à la main dans la fenêtre Chrome, puis tu appuies sur Entrée dans le terminal.
MANUAL_VERIF_FALLBACK = True

# Fichiers de sauvegarde (à côté du script). Pour tout recommencer de zéro : supprime-les.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROGRESS_FILE = os.path.join(BASE_DIR, "progress_films.json")   # dernière page terminée
DONE_FILE = os.path.join(BASE_DIR, "done_films.txt")            # films déjà traités (1 identifiant par ligne)
FAILED_FILE = os.path.join(BASE_DIR, "failed_films.json")       # films en échec à retenter

# Identifiants (le script marche tout seul, sans rien configurer)
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


# =====================================================================
# OUTILS
# =====================================================================
def get_text(el):
    """
    Lit le texte d'un élément via textContent (fonctionne même si la fenêtre
    est réduite / en arrière-plan, contrairement à .text qui ne voit que le visible).
    """
    try:
        t = (el.get_attribute("textContent") or "").strip()
    except Exception:
        return ""
    return re.sub(r"\s+", " ", t)


def detect_provider_genres(det_res):
    found = []
    results = (det_res.get("watch/providers") or {}).get("results") or {}
    for region in WATCH_REGIONS:
        flatrate = (results.get(region) or {}).get("flatrate") or []
        for prov in flatrate:
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
    Lit TOUTE ta base (films + séries) et renvoie (ids, tmdb_ids_des_films, clés titre|année des films),
    ou None si la base est illisible (on s'arrête alors pour ne pas créer de doublons).
    Les ids TMDB des séries sont ignorés : un film et une série peuvent avoir le même numéro TMDB.
    """
    logger.info("🔍 Lecture de ta base de données (films + séries)...")
    headers = {"authorization": token}

    for base in (f"{API_URL}/api/admin/movies", f"{API_URL}/api/movies"):
        items = fetch_all_items(base, headers)
        if items is None:
            continue
        ids, tmdb_ids, keys = set(), set(), set()
        n_films = n_other = 0
        for m in items:
            if m.get("id"):
                ids.add(str(m["id"]))
            is_film = str(m.get("type") or "film").lower() == "film"
            if is_film:
                n_films += 1
                if m.get("tmdb_id"):
                    tmdb_ids.add(str(m["tmdb_id"]))
                if m.get("title"):
                    keys.add(film_key(m["title"], m.get("year")))
            else:
                n_other += 1
        logger.info(f"✅ {len(items)} fiches en base : {n_films} films, {n_other} séries/autres.")
        return ids, tmdb_ids, keys
    return None


def push_to_mongodb(movie_data, token):
    headers = {"Content-Type": "application/json", "authorization": token}
    try:
        res = requests.post(f"{API_URL}/api/admin/movies", json=movie_data, headers=headers, timeout=30)
        if res.status_code == 200:
            logger.info(f"   [MONGODB] 🚀 Film enregistré : {movie_data.get('title')}")
            return True
        logger.warning(f"   [MONGODB] ⚠️ Erreur sauvegarde : {res.text}")
    except Exception as e:
        logger.error(f"   [MONGODB] ❌ Erreur réseau : {e}")
    return False


# =====================================================================
# TMDB
# =====================================================================
def get_youtube_trailer(movie_id, language="fr-FR"):
    videos_url = f"https://api.themoviedb.org/3/movie/{movie_id}/videos"
    videos_res = requests.get(videos_url, params={"api_key": TMDB_API_KEY, "language": language}, timeout=15).json()
    videos = videos_res.get("results") or []

    if not videos and language != "en-US":
        videos_res = requests.get(videos_url, params={"api_key": TMDB_API_KEY, "language": "en-US"}, timeout=15).json()
        videos = videos_res.get("results") or []

    trailer = next((v for v in videos if v.get("type") == "Trailer" and v.get("site") == "YouTube"), None)
    if not trailer:
        trailer = next((v for v in videos if v.get("site") == "YouTube"), None)

    return trailer["key"] if trailer else ""


def extract_year_hint(driver, raw_title=""):
    """Devine l'année de sortie affichée sur la page pour départager les homonymes."""
    def plausible(y):
        try:
            return 1900 <= int(y) <= 2029
        except Exception:
            return False

    m = re.search(r'\((\d{4})\)', raw_title)
    if m and plausible(m.group(1)):
        return m.group(1)

    m = re.search(r'-\s*(\d{4})\s*$', raw_title)
    if m and plausible(m.group(1)):
        return m.group(1)

    try:
        for sel in ['meta[itemprop="datePublished"]', 'meta[property="video:release_date"]']:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                content = el.get_attribute("content") or ""
                m = re.search(r'(\d{4})', content)
                if m and plausible(m.group(1)):
                    return m.group(1)
    except Exception:
        pass

    for sel in [".s-annee", ".release-year", ".annee", "span.year", ".data span", "#info", ".sgeneros"]:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                m = re.search(r'\b(19|20)\d{2}\b', get_text(el))
                if m and plausible(m.group(0)):
                    return m.group(0)
        except Exception:
            continue

    try:
        body_text = get_text(driver.find_element(By.TAG_NAME, "body"))
        m = re.search(r'(?:Ann[ée]e|Year|Sortie)\D{0,10}(\d{4})', body_text, flags=re.IGNORECASE)
        if m and plausible(m.group(1)):
            return m.group(1)
    except Exception:
        pass

    return None


def fetch_tmdb_metadata(title, year_hint=None):
    try:
        clean_query = title.replace("&", "et").strip()

        search_url_fr = f"https://api.themoviedb.org/3/search/movie?api_key={TMDB_API_KEY}&language=fr-FR&query={requests.utils.quote(clean_query)}"
        results = requests.get(search_url_fr, timeout=15).json().get("results") or []

        if not results:
            search_url_en = f"https://api.themoviedb.org/3/search/movie?api_key={TMDB_API_KEY}&language=en-US&query={requests.utils.quote(clean_query)}"
            results = requests.get(search_url_en, timeout=15).json().get("results") or []

        if not results:
            logger.warning(f"   Aucun film trouvé sur TMDB pour '{title}'")
            return None

        movie = None
        if year_hint:
            for r in results:
                if (r.get("release_date") or "")[:4] == str(year_hint):
                    movie = r
                    break

            if not movie:
                def year_diff(r):
                    rd = r.get("release_date") or ""
                    if not rd:
                        return 999
                    try:
                        return abs(int(rd[:4]) - int(year_hint))
                    except Exception:
                        return 999

                best = min(results, key=year_diff)
                if year_diff(best) <= 2:
                    movie = best
                    logger.info(f"   📅 Année approchante : {best.get('release_date')[:4]} (demandé : {year_hint})")
                else:
                    logger.warning(
                        f"   ⚠️ REJET : aucun résultat TMDB pour l'année {year_hint} "
                        f"(meilleur : {best.get('title')} / {best.get('release_date')})."
                    )
                    return None
        else:
            movie = results[0]

        movie_id = movie["id"]

        details_url = f"https://api.themoviedb.org/3/movie/{movie_id}?api_key={TMDB_API_KEY}&language=fr-FR&append_to_response=credits,watch/providers"
        det_res = requests.get(details_url, timeout=15).json()

        found_title = det_res.get("title") or movie.get("title") or title

        directors = []
        if det_res.get("credits") and det_res["credits"].get("crew"):
            directors = [c["name"] for c in det_res["credits"]["crew"] if c["job"] == "Director"]

        cast = []
        if det_res.get("credits") and det_res["credits"].get("cast"):
            cast = [c["name"] for c in det_res["credits"]["cast"][:6]]

        genres = [g["name"] for g in det_res.get("genres", [])]
        for label in detect_platform_genres(det_res, title):
            if label not in genres:
                genres.append(label)

        trailer_key = get_youtube_trailer(movie_id)

        return {
            "tmdb_id": str(movie_id),
            "title": found_title,
            "year": (det_res.get("release_date") or "2026")[:4],
            "rating": str(round(det_res.get("vote_average", 7.5), 1)),
            "genre": ", ".join(genres) if genres else "Comédie",
            "desc": det_res.get("overview", ""),
            "dir": ", ".join(directors) if directors else "Inconnu",
            "cast": ", ".join(cast) if cast else "",
            "poster": f"https://image.tmdb.org/t/p/w500{det_res.get('poster_path')}" if det_res.get('poster_path') else "",
            "backdrop": f"https://image.tmdb.org/t/p/original{det_res.get('backdrop_path')}" if det_res.get('backdrop_path') else "",
            "trailer": trailer_key
        }
    except Exception as e:
        logger.warning(f"   ⚠️ Erreur TMDB : {e}")
    return None


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
        # Un seul nouvel essai : on recharge la page
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
    # Empêche Chrome de ralentir/geler la page quand la fenêtre est cachée
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--disable-renderer-backgrounding")
    options.add_argument("--disable-background-timer-throttling")
    options.page_load_strategy = "eager"
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    driver.set_script_timeout(PAGE_LOAD_TIMEOUT)
    return driver


# =====================================================================
# LISTE DES FILMS D'UNE PAGE
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
    """Films affichés sur la page : liste de {id, url, title, year}."""
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
    """Page 1 = /films/ ; pages suivantes = index.php?cstart=N."""
    return FIRST_PAGE_URL if page == 1 else PAGE_URL.format(n=page)


def load_listing_page(driver, page):
    """Ouvre la page `page` de la liste des films (2 essais) et renvoie ses films."""
    url = page_url(page)
    for attempt in range(2):
        safe_get(driver, url)
        cards = wait_for_cards(driver)
        if cards:
            return cards
        if attempt == 0:
            logger.info("   🔄 Aucun film affiché, je recharge la page...")
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


# =====================================================================
# DOUBLONS : COMPARAISON AVEC TA BASE (titre + année)
# =====================================================================
def norm_title(t):
    t = unicodedata.normalize("NFKD", (t or "").lower())
    t = t.encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", t)


def film_key(title, year):
    return f"{norm_title(title)}|{str(year or '').strip()[:4]}"


def card_in_db(card, existing_keys):
    """True si un film de ta base a le même titre ET la même année que l'affiche."""
    year = (card.get("year") or "").strip()
    if not re.fullmatch(r"(19|20)\d{2}", year):
        return False
    return film_key(card.get("title"), year) in existing_keys


# =====================================================================
# EXTRACTION DES IFRAMES
# =====================================================================
def fmt_iframe(src, server_name):
    if server_name == "vidzy":
        src = re.sub(r'vidzy\.[a-z]+', 'vidzy.lol', src, flags=re.IGNORECASE)
    else:
        src = re.sub(r'uqload\.[a-z]+', 'uqload.vc', src, flags=re.IGNORECASE)
    return f'<IFRAME SRC="{src}" FRAMEBORDER=0 MARGINWIDTH=0 MARGINHEIGHT=0 SCROLLING=NO WIDTH=640 HEIGHT=360 allowfullscreen></IFRAME>'


def detect_site_type_film(driver):
    try:
        if driver.find_elements(By.ID, "player-options"):
            return "frenchstream"
    except Exception:
        pass
    return "filmoflix"


def _extract_iframe_from_option(driver, opt, server_name):
    driver.execute_script("arguments[0].click();", opt)
    time.sleep(3)
    try:
        iframe = driver.find_element(By.ID, "video-iframe")
        src = iframe.get_attribute("src")
        if src and server_name in src.lower():
            return fmt_iframe(src, server_name)
    except Exception:
        pass
    return ""


FRENCHSTREAM_SERVERS_CSS = "button.player-option"
FILMOFLIX_ROWS_CSS = "ul.list-liens li, .player-list .lien-bis, table tbody tr"


def extract_frenchstream_film_iframes(driver):
    iframes_data = {"vf_vidzy": "", "vf_uqload": "", "vostfr_vidzy": "", "vostfr_uqload": ""}
    try:
        count = len(driver.find_elements(By.CSS_SELECTOR, FRENCHSTREAM_SERVERS_CSS))

        for i in range(count):
            # On ré-récupère les boutons à chaque tour : la page change après un clic
            # (sinon StaleElementReferenceException).
            servers = driver.find_elements(By.CSS_SELECTOR, FRENCHSTREAM_SERVERS_CSS)
            if i >= len(servers):
                break
            s = servers[i]

            s_text = get_text(s).lower()
            data_player = (s.get_attribute("data-player") or "").lower()

            server_name = None
            if "vidzy" in data_player or "vidzy" in s_text:
                server_name = "vidzy"
            elif "uqload" in data_player or "uqload" in s_text:
                server_name = "uqload"

            if not server_name:
                continue

            driver.execute_script("arguments[0].click();", s)
            time.sleep(2)

            # Après le clic, on re-trouve le bouton pour lire ses versions
            servers = driver.find_elements(By.CSS_SELECTOR, FRENCHSTREAM_SERVERS_CSS)
            if i >= len(servers):
                continue
            s = servers[i]

            version_options = s.find_elements(By.CSS_SELECTOR, ".version-option")

            if version_options:
                opts_by_lang = {"vostfr": None, "french": None, "truefrench": None}
                for opt in version_options:
                    lang_text = (opt.get_attribute("data-version") or "").upper() + " " + get_text(opt).upper()
                    if "TRUEFRENCH" in lang_text:
                        opts_by_lang["truefrench"] = opt
                    elif "VOSTFR" in lang_text:
                        opts_by_lang["vostfr"] = opt
                    elif "FRENCH" in lang_text or "VF" in lang_text:
                        opts_by_lang["french"] = opt

                vf_opt = opts_by_lang["truefrench"] or opts_by_lang["french"]
                if vf_opt and not iframes_data[f"vf_{server_name}"]:
                    iframes_data[f"vf_{server_name}"] = _extract_iframe_from_option(driver, vf_opt, server_name)
                    if iframes_data[f"vf_{server_name}"]:
                        logger.info(f"      🇫🇷 Lien VF {server_name.capitalize()} extrait")

                if opts_by_lang["vostfr"] and not iframes_data[f"vostfr_{server_name}"]:
                    try:
                        iframes_data[f"vostfr_{server_name}"] = _extract_iframe_from_option(driver, opts_by_lang["vostfr"], server_name)
                    except Exception:
                        # L'option a pu devenir périmée après l'extraction VF : on la retrouve
                        fresh = driver.find_elements(By.CSS_SELECTOR, FRENCHSTREAM_SERVERS_CSS)
                        if i < len(fresh):
                            for opt in fresh[i].find_elements(By.CSS_SELECTOR, ".version-option"):
                                lt = (opt.get_attribute("data-version") or "").upper() + " " + get_text(opt).upper()
                                if "VOSTFR" in lt:
                                    iframes_data[f"vostfr_{server_name}"] = _extract_iframe_from_option(driver, opt, server_name)
                                    break
                    if iframes_data[f"vostfr_{server_name}"]:
                        logger.info(f"      🇺🇸 Lien VOSTFR {server_name.capitalize()} extrait")
            else:
                if not iframes_data[f"vf_{server_name}"]:
                    try:
                        iframe = driver.find_element(By.ID, "video-iframe")
                        src = iframe.get_attribute("src")
                        if src and server_name in src.lower():
                            iframes_data[f"vf_{server_name}"] = fmt_iframe(src, server_name)
                            logger.info(f"      🇫🇷 Lien VF {server_name.capitalize()} extrait (direct)")
                    except Exception:
                        pass

    except Exception as e:
        logger.warning(f"   Erreur extraction Frenchstream : {str(e)[:150]}")

    return iframes_data


def extract_filmoflix_film_iframes(driver):
    iframes_data = {"vf_vidzy": "", "vf_uqload": "", "vostfr_vidzy": "", "vostfr_uqload": ""}
    try:
        count = len(driver.find_elements(By.CSS_SELECTOR, FILMOFLIX_ROWS_CSS))

        for i in range(count):
            rows = driver.find_elements(By.CSS_SELECTOR, FILMOFLIX_ROWS_CSS)
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

            mots_cles_vostfr = ['en.png', 'uk.png', 'us.png', 'gb.png', 'en.svg', 'uk.svg', 'gb.svg', 'flag-gb', 'flag-uk', 'vostfr', 'vost', 'english', 'anglais']
            is_vostfr = any(x in html_content for x in mots_cles_vostfr)

            if is_vostfr and is_vidzy and iframes_data["vostfr_vidzy"]: continue
            if is_vostfr and is_uqload and iframes_data["vostfr_uqload"]: continue
            if not is_vostfr and is_vidzy and iframes_data["vf_vidzy"]: continue
            if not is_vostfr and is_uqload and iframes_data["vf_uqload"]: continue

            driver.execute_script("arguments[0].click();", row)
            time.sleep(4)

            for iframe in driver.find_elements(By.TAG_NAME, "iframe"):
                src = iframe.get_attribute("src")
                if not src:
                    continue
                src_lower = src.lower()
                lang = "vostfr" if is_vostfr else "vf"

                if "vidzy" in src_lower and is_vidzy:
                    iframes_data[f"{lang}_vidzy"] = fmt_iframe(src, "vidzy")
                    logger.info(f"      Lien {lang.upper()} Vidzy extrait (Filmoflix)")
                    break
                elif "uqload" in src_lower and is_uqload:
                    iframes_data[f"{lang}_uqload"] = fmt_iframe(src, "uqload")
                    logger.info(f"      Lien {lang.upper()} Uqload extrait (Filmoflix)")
                    break

            if all(iframes_data.values()):
                break

    except Exception as e:
        logger.warning(f"   Erreur extraction Filmoflix : {str(e)[:150]}")

    return iframes_data


# =====================================================================
# TRAITEMENT D'UN FILM
# =====================================================================
def get_title_and_year(driver, site_type):
    """Renvoie (clean_title, year_hint). Lit le texte via textContent (marche en arrière-plan)."""
    selectors = ["#s-title", "h1"] if site_type == "frenchstream" else ["h1"]

    raw_title = ""
    used_sel = None
    for sel in selectors:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                t = get_text(el)
                if t:
                    raw_title, used_sel = t, sel
                    break
        except Exception:
            continue
        if raw_title:
            break

    if not raw_title:
        return "", None

    if used_sel == "#s-title":
        clean_title = re.sub(r'-\s*\d{4}$', '', raw_title).strip()
    else:
        clean_title = raw_title.split(" - ")[0].split("(")[0].replace("VOIR", "").strip()

    return clean_title, extract_year_hint(driver, raw_title)


def wait_for_title(driver, timeout=10):
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#s-title, h1"))
        )
    except Exception:
        pass


def process_film(driver, film_url, token, existing_ids, existing_tmdb_ids):
    """
    Renvoie : "added", "skipped" (déjà en base), "ignored" (série), "failed".
    """
    safe_get(driver, film_url)

    clean_title, year_hint, site_type = "", None, "filmoflix"

    # 2 tentatives : si le titre est vide, on laisse la page finir de charger puis on réessaie
    for attempt in range(2):
        wait_for_title(driver)
        # Page de série (présence de #serie-config) : ce script ne traite que les films
        try:
            if driver.find_elements(By.ID, "serie-config"):
                logger.info("   ⏭️ C'est une série : ignorée (ce script ne traite que les films).")
                return "ignored"
        except Exception:
            pass
        site_type = detect_site_type_film(driver)
        try:
            clean_title, year_hint = get_title_and_year(driver, site_type)
        except Exception as e:
            logger.warning(f"   ⚠️ Titre introuvable ({str(e)[:80]})")
            clean_title, year_hint = "", None
        if clean_title:
            break
        if attempt == 0:
            logger.info("   🔄 Titre vide, je laisse la page charger et je réessaie...")
            time.sleep(4)

    if not clean_title:
        logger.warning("   ⚠️ Titre vide, on passe.")
        return "failed"

    logger.info(f"   🎬 {clean_title} ({year_hint or '?'})")

    tmdb_data = fetch_tmdb_metadata(clean_title, year_hint=year_hint)

    # --- ID unique + test d'existence AVANT de scraper les liens (gain de temps) ---
    if tmdb_data and tmdb_data.get("tmdb_id"):
        movie_id = f"cinelk10_film_{tmdb_data['tmdb_id']}"
    else:
        path_slug = film_url.rstrip("/").split("/")[-1]
        url_hash = hashlib.md5(path_slug.encode("utf-8")).hexdigest()[:15]
        movie_id = f"filmoflix_film_{url_hash}"

    already = movie_id in existing_ids or (
        tmdb_data and str(tmdb_data.get("tmdb_id")) in existing_tmdb_ids
    )
    if already:
        logger.info("   ⏭️ NEXT : déjà en base.")
        return "skipped"

    if not tmdb_data:
        tmdb_data = {
            "tmdb_id": "", "title": clean_title, "year": year_hint or "2026", "rating": "7.5",
            "genre": "Comédie", "desc": "", "dir": "Inconnu", "cast": "Non renseigné",
            "poster": "", "backdrop": "", "trailer": ""
        }

    # --- Extraction des liens vidéo ---
    if site_type == "frenchstream":
        iframes_data = extract_frenchstream_film_iframes(driver)
    else:
        iframes_data = extract_filmoflix_film_iframes(driver)

    if not any(iframes_data.values()):
        logger.warning("   ⚠️ Aucun lien valide (Vidzy/Uqload), film ignoré.")
        return "failed"

    final_poster = tmdb_data["poster"]
    if tmdb_data["backdrop"]:
        final_poster += f"|||{tmdb_data['backdrop']}"

    movie_payload = {
        "id": movie_id,
        "tmdb_id": tmdb_data.get("tmdb_id", ""),
        "type": "film",
        "title": tmdb_data["title"],
        "year": tmdb_data["year"],
        "poster": final_poster,
        "rating": tmdb_data["rating"],
        "genre": tmdb_data["genre"],
        "desc": tmdb_data["desc"],
        "dir": tmdb_data["dir"],
        "cast": tmdb_data["cast"],
        "trailer_iframe": tmdb_data["trailer"],
        "iframe_premium": "",
        "iframe_vidzy": iframes_data.get("vf_vidzy", ""),
        "iframe_uqload": iframes_data.get("vf_uqload", ""),
        "iframe_premium_vostfr": "",
        "iframe_vidzy_vostfr": iframes_data.get("vostfr_vidzy", ""),
        "iframe_uqload_vostfr": iframes_data.get("vostfr_uqload", ""),
        "episodes": None
    }

    if push_to_mongodb(movie_payload, token):
        existing_ids.add(movie_id)
        if tmdb_data.get("tmdb_id"):
            existing_tmdb_ids.add(str(tmdb_data["tmdb_id"]))
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


def run_film(driver, url, token, existing_ids, existing_tmdb_ids):
    """Traite un film. Relance Chrome s'il a planté. Renvoie (résultat, driver)."""
    try:
        return process_film(driver, url, token, existing_ids, existing_tmdb_ids), driver
    except Exception as e:
        logger.warning(f"   ⚠️ Erreur sur ce film, on passe au suivant : {str(e)[:150]}")
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

    # 1) Avant tout : on récupère toute ta base (films + séries) pour éviter les doublons
    db = get_existing_database(token)
    if db is None:
        logger.error("❌ Base de données illisible : j'arrête pour ne pas créer de doublons. Relance dans une minute (Render met parfois du temps à se réveiller).")
        return
    existing_ids, existing_tmdb_ids, existing_keys = db

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
                    logger.error(f"   ⛔ Je m'arrête (blocage ou site en panne). Relance plus tard : le script reprendra à la page {page}.")
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

            # Retire de la liste les films déjà traités ou déjà présents dans ta base
            todo = []
            for c in cards:
                if c["id"] in done_ids:
                    stats["skipped"] += 1
                    continue
                if PREFILTER_BY_TITLE and card_in_db(c, existing_keys):
                    stats["skipped"] += 1
                    done_ids.add(c["id"])
                    append_done(c["id"])
                    continue
                todo.append(c)

            logger.info(f"   📄 {len(cards)} films sur la page → {len(todo)} à scraper "
                        f"({len(cards) - len(todo)} déjà en base / traités).")

            page_failed = 0
            for i, c in enumerate(todo, 1):
                logger.info(f"\n   [p{page} · {i}/{len(todo)}] ➡️ {c['title'] or c['url']}")
                result, driver = run_film(driver, c["url"], token, existing_ids, existing_tmdb_ids)
                stats[result] += 1

                if result in ("added", "skipped", "ignored"):
                    done_ids.add(c["id"])
                    append_done(c["id"])          # sauvegarde immédiate après CHAQUE film
                    if c["url"] in failed_urls:
                        failed_urls.remove(c["url"])
                        save_json(FAILED_FILE, failed_urls)
                else:
                    page_failed += 1
                    if c["url"] not in failed_urls:
                        failed_urls.append(c["url"])
                        save_json(FAILED_FILE, failed_urls)

                if result == "added":
                    time.sleep(DELAY_BETWEEN_FILMS)

            # Page terminée : sauvegarde de la progression
            save_json(PROGRESS_FILE, {"last_page_done": page, "last_page": last_page, "finished": False})
            logger.info(f"   💾 Page {page} terminée et sauvegardée ({page_failed} échec(s)). "
                        f"Total → ajoutés : {stats['added']} | déjà en base : {stats['skipped']} | échecs : {stats['failed']}")
            page += 1

        if finished:
            save_json(PROGRESS_FILE, {"last_page_done": last_page, "last_page": last_page, "finished": True})
            logger.info("\n🏁 Toutes les pages ont été parcourues.")

        # 4) Deuxième chance pour les films en échec (titre vide, vérification, lecteurs lents...)
        if RETRY_FAILED_AT_END and failed_urls and not stopped_early:
            logger.info(f"\n🔁 Nouvelle tentative pour {len(failed_urls)} film(s) en échec...")
            for url in list(failed_urls):
                logger.info(f"\n   ➡️ {url}")
                result, driver = run_film(driver, url, token, existing_ids, existing_tmdb_ids)
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
                    time.sleep(DELAY_BETWEEN_FILMS)

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
    logger.info(f"✅ Ajoutés : {stats['added']} | ⏭️ Déjà en base : {stats['skipped']} | "
                f"🚫 Ignorés (séries) : {stats['ignored']} | ❌ Échecs : {stats['failed']}")
    if failed_urls:
        logger.info(f"Films en échec (retentés au prochain lancement) : {len(failed_urls)}")
        for u in failed_urls[:50]:
            logger.info(f"   - {u}")
        if len(failed_urls) > 50:
            logger.info(f"   ... et {len(failed_urls) - 50} autres")
    logger.info("==========================================")


if __name__ == "__main__":
    main()