"""Monitora a pagina de lotes da Lei do Bem (MCTI) e avisa por Telegram e/ou email.

Estado salvo em state.json (links vistos + hash do texto principal).
A ultima pagina baixada fica em last_page.html para diagnostico.
"""
import hashlib
import json
import os
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

URL = "https://www.gov.br/mcti/pt-br/acompanhe-o-mcti/lei-do-bem/paginas/lotes"
HERE = Path(__file__).parent
STATE = HERE / "state.json"
DEBUG_HTML = HERE / "last_page.html"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9",
}
BLOCK_MARKERS = ("whether you are a human", "support ID is")
SELECTORS = ("#content-core", "#content", "main", "article")


def notify(title: str, body: str) -> None:
    tok = os.getenv("TELEGRAM_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID") or "1489648434"
    if tok and chat:
        requests.post(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            data={"chat_id": chat, "text": f"{title}\n\n{body}", "disable_web_page_preview": True},
            timeout=20,
        ).raise_for_status()

    user, pwd, to = os.getenv("GMAIL_USER"), os.getenv("GMAIL_APP_PASSWORD"), os.getenv("MAIL_TO")
    if user and pwd and to:
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = title, user, to
        msg.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(user, pwd)
            s.send_message(msg)


def fetch() -> str | None:
    """Retorna o HTML, ou None se caiu no CAPTCHA."""
    r = requests.get(URL, headers=HEADERS, timeout=30)
    print(f"HTTP {r.status_code}, {len(r.text)} caracteres")
    DEBUG_HTML.write_text(r.text, encoding="utf-8")
    r.raise_for_status()
    if any(m in r.text for m in BLOCK_MARKERS):
        return None
    return r.text


def links_in(node) -> dict[str, str]:
    links = {}
    for a in node.find_all("a", href=True):
        text = " ".join(a.get_text().split())
        href = a["href"]
        if text and not href.startswith(("#", "javascript:", "mailto:")):
            links[urljoin(URL, href)] = text
    return links


def is_relevant(url: str, text: str) -> bool:
    u, t = url.lower(), text.lower()
    return "lei-do-bem" in u or ".pdf" in u or "lote" in t or "parecer" in t


def extract(html: str) -> tuple[dict[str, str], str]:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else "(sem titulo)"
    print(f"Titulo da pagina: {title}")

    # Usa o primeiro bloco principal que tiver links; se nenhum tiver, a pagina toda.
    main, used = soup.body or soup, "body"
    for sel in SELECTORS:
        node = soup.select_one(sel)
        if node and links_in(node):
            main, used = node, sel
            break

    all_links = links_in(main)
    # Mantem so links da Lei do Bem, PDFs ou que citam "lote"; descarta menu e rodape do gov.br.
    links = {u: t for u, t in all_links.items() if is_relevant(u, t)}
    print(f"Seletor usado: {used}, {len(all_links)} links no total, {len(links)} relevantes")
    # Hash so dos links relevantes, para banners e menus nao dispararem alerta.
    fingerprint = "\n".join(f"{u} {t}" for u, t in sorted(links.items()))
    return links, hashlib.sha256(fingerprint.encode()).hexdigest()


def save(state: dict) -> None:
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}

    html = fetch()
    if html is None:
        if not state.get("blocked"):
            notify("Monitor Lei do Bem: bloqueado", f"O site devolveu CAPTCHA. Verifique manualmente:\n{URL}")
            state["blocked"] = True
            save(state)
        print("Bloqueado por CAPTCHA.")
        return 0

    links, digest = extract(html)

    # Pagina sem nenhum link = resposta estranha. Avisa uma vez e nao sobrescreve o estado bom.
    if not links:
        if not state.get("empty"):
            notify("Monitor Lei do Bem: pagina veio sem links", "Resposta inesperada do site. Veja o artifact last_page no GitHub Actions.")
            state["empty"] = True
            save(state)
        return 0

    old_links = state.get("links") or {}
    if state.get("blocked") or state.get("empty"):
        notify("Monitor Lei do Bem: voltou a funcionar", URL)

    if not old_links:
        sample = "\n".join(f"- {t}" for t in list(links.values())[-5:])
        notify("Monitor Lei do Bem ativo", f"{len(links)} links registrados. Exemplos:\n{sample}\n\n{URL}")
    else:
        new = {u: t for u, t in links.items() if u not in old_links}
        if new:
            body = "\n\n".join(f"{t}\n{u}" for u, t in new.items())
            notify(f"Lei do Bem: {len(new)} novidade(s) na pagina de lotes", f"{body}\n\nPagina: {URL}")
        elif digest != state.get("hash"):
            notify("Lei do Bem: algum link da pagina de lotes mudou", f"Nenhum link novo, mas algum titulo ou link foi alterado:\n{URL}")

    save({"links": links, "hash": digest, "blocked": False, "empty": False})
    return 0


if __name__ == "__main__":
    sys.exit(main())
