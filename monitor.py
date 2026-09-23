"""Monitora a pagina de lotes da Lei do Bem (MCTI) e avisa por Telegram e/ou email.

Estado salvo em state.json (links vistos + hash do texto principal).
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
STATE = Path(__file__).with_name("state.json")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9",
}
BLOCK_MARKERS = ("whether you are a human", "support ID is")


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
    r.raise_for_status()
    if any(m in r.text for m in BLOCK_MARKERS):
        return None
    return r.text


def extract(html: str) -> tuple[dict[str, str], str]:
    soup = BeautifulSoup(html, "html.parser")
    # #content-core e o miolo das paginas Plone do gov.br; ignora menu, rodape etc.
    main = soup.select_one("#content-core") or soup.select_one("#content") or soup.body
    links = {}
    for a in main.find_all("a", href=True):
        text = " ".join(a.get_text().split())
        if text:
            links[urljoin(URL, a["href"])] = text
    text = " ".join(main.get_text(" ").split())
    return links, hashlib.sha256(text.encode()).hexdigest()


def main() -> int:
    state = json.loads(STATE.read_text()) if STATE.exists() else {}

    html = fetch()
    if html is None:
        if not state.get("blocked"):
            notify("Monitor Lei do Bem: bloqueado", f"O site devolveu CAPTCHA. Verifique manualmente:\n{URL}")
            state["blocked"] = True
            STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        print("Bloqueado por CAPTCHA.")
        return 0

    links, digest = extract(html)
    old_links = state.get("links", {})
    first_run = not old_links

    if state.get("blocked"):
        notify("Monitor Lei do Bem: voltou a funcionar", URL)

    if first_run:
        notify("Monitor Lei do Bem ativo", f"{len(links)} links registrados em:\n{URL}")
    else:
        new = {u: t for u, t in links.items() if u not in old_links}
        if new:
            body = "\n\n".join(f"{t}\n{u}" for u, t in new.items())
            notify(f"Lei do Bem: {len(new)} novidade(s) na pagina de lotes", f"{body}\n\nPagina: {URL}")
        elif digest != state.get("hash"):
            notify("Lei do Bem: texto da pagina de lotes mudou", f"Nenhum link novo, mas o conteudo foi alterado:\n{URL}")

    STATE.write_text(json.dumps({"links": links, "hash": digest, "blocked": False}, ensure_ascii=False, indent=2))
    print(f"OK: {len(links)} links.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
