"""
Lance un de tes scripts de scraping SANS le modifier, adapté à un serveur :
  - Chrome en headless (pas de fenêtre) avec les options nécessaires dans Docker
  - input() ne bloque jamais (personne ne peut valider la vérification à la main sur Render)
  - Ctrl+C / SIGTERM -> KeyboardInterrupt : le script sauvegarde sa progression proprement

Usage : python runner_wrapper.py /chemin/vers/script.py
"""
import os
import sys
import signal
import builtins
import runpy


def _stop(*_):
    raise KeyboardInterrupt()


signal.signal(signal.SIGINT, _stop)
signal.signal(signal.SIGTERM, _stop)

# Pas d'humain devant l'écran : on ne bloque jamais sur input()
builtins.input = lambda *a, **k: ""

from selenium import webdriver  # noqa: E402
from selenium.webdriver.chrome.service import Service  # noqa: E402

CHROME_BIN = os.environ.get("CHROME_BIN", "/usr/bin/chromium")
CHROMEDRIVER = os.environ.get("CHROMEDRIVER", "/usr/bin/chromedriver")
UA = os.environ.get(
    "SCRAPER_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)
NO_IMAGES = os.environ.get("SCRAPER_NO_IMAGES", "1") == "1"

_BaseChrome = webdriver.Chrome


class ServerChrome(_BaseChrome):
    """Même Chrome que dans tes scripts, avec les options serveur ajoutées."""

    def __init__(self, options=None, service=None, **kw):
        options = options or webdriver.ChromeOptions()
        if os.path.exists(CHROME_BIN):
            options.binary_location = CHROME_BIN
        extra = [
            "--headless=new",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-extensions",
            "--window-size=1280,900",
            f"--user-agent={UA}",
        ]
        if NO_IMAGES:
            extra.append("--blink-settings=imagesEnabled=false")
        for arg in extra:
            options.add_argument(arg)
        if os.path.exists(CHROMEDRIVER):
            service = Service(CHROMEDRIVER)
        super().__init__(options=options, service=service, **kw)


webdriver.Chrome = ServerChrome

if len(sys.argv) < 2:
    print("Usage : python runner_wrapper.py script.py")
    sys.exit(2)

script = os.path.abspath(sys.argv[1])
sys.argv = [script]
sys.path.insert(0, os.path.dirname(script))
runpy.run_path(script, run_name="__main__")
