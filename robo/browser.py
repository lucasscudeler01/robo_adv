"""
Setup do navegador via Playwright.

Usa perfil persistente: na primeira execucao voce faz login manualmente no
NPJUR; nas proximas o login fica salvo nos cookies do perfil.

OBS: o Loy Legal NAO eh um site separado. Ele eh acessado de dentro do
NPJUR, pelo botao "Peticionamento Eletronico". Portanto so abrimos uma aba.

STEALTH MODE:
  O NPJUR tem armadilha anti-automacao: dispara `debugger;` quando detecta
  Playwright. Pra contornar usamos 3 camadas:
    1. channel='chrome' -> usa o Chrome real do sistema, nao o Chromium
       embarcado do Playwright (assinatura de processo identica).
    2. flag --disable-blink-features=AutomationControlled -> remove o
       indicador padrao de automacao.
    3. init_script que sobrescreve navigator.webdriver (=undefined) e
       outros sinais que sites usam pra detectar bots.

  Isso faz o NPJUR ver a sessao como Chrome humano normal.
"""

from pathlib import Path
from playwright.sync_api import sync_playwright, BrowserContext, Page


# Script injetado em TODA pagina antes do JS dela rodar.
# Mascara sinais classicos de automacao que sites como o NPJUR checam.
_STEALTH_INIT_SCRIPT = """
// Esconde navigator.webdriver (delator principal do Playwright/Selenium)
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// Fake plugins (browser sem plugins = bot)
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5]
});

// Fake languages
Object.defineProperty(navigator, 'languages', {
    get: () => ['pt-BR', 'pt', 'en']
});

// chrome.runtime existe em Chrome real
window.chrome = window.chrome || { runtime: {} };

// Permissions API: forca query a retornar 'granted' pra parecer humano
const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
if (originalQuery) {
    window.navigator.permissions.query = (params) => (
        params.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : originalQuery(params)
    );
}

// Neutraliza a armadilha `debugger;` que o NPJUR dispara.
// (Sobrescrever Function.prototype.constructor eh radical demais; em vez disso
//  o stealth acima ja faz a deteccao falhar antes do debugger ser disparado.)
"""


class Navegador:
    """Wrapper em torno do Playwright com perfil persistente + stealth."""

    def __init__(self, pasta_perfil: str, pasta_downloads: str, headless: bool = False):
        self.pasta_perfil = Path(pasta_perfil).absolute()
        self.pasta_downloads = Path(pasta_downloads).absolute()
        self.pasta_perfil.mkdir(parents=True, exist_ok=True)
        self.pasta_downloads.mkdir(parents=True, exist_ok=True)
        self.headless = headless

        self._pw = None
        self.context: BrowserContext | None = None
        self.npjur: Page | None = None

    def iniciar(self):
        """Abre o Chrome do sistema com perfil persistente. Uma aba (NPJUR)."""
        self._pw = sync_playwright().start()

        # channel='chrome' = usa o Google Chrome instalado, nao o Chromium do Playwright.
        # Isso eh ESSENCIAL pro stealth funcionar contra o NPJUR.
        self.context = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.pasta_perfil),
            channel="chrome",
            headless=self.headless,
            accept_downloads=True,
            args=[
                "--start-maximized",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-default-browser-check",
                "--no-first-run",
            ],
            ignore_default_args=["--enable-automation"],
            viewport=None,
        )

        # Injeta o stealth ANTES de qualquer pagina carregar
        self.context.add_init_script(_STEALTH_INIT_SCRIPT)

        # Primeira aba
        if self.context.pages:
            self.npjur = self.context.pages[0]
        else:
            self.npjur = self.context.new_page()

    def fechar(self):
        """Fecha o navegador (mas mantem o perfil salvo)."""
        if self.context:
            self.context.close()
        if self._pw:
            self._pw.stop()

    def __enter__(self):
        self.iniciar()
        return self

    def __exit__(self, *args):
        self.fechar()
