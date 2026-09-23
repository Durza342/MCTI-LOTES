"""Monitora a pagina de lotes da Lei do Bem (MCTI) e avisa por Telegram e/ou email.

- Avisa quando aparece link novo, com numero do lote, ano-base e data de publicacao.
- Rodando manualmente (Run workflow), manda um resumo com o ultimo lote publicado.
- Estado em state.json; ultima pagina baixada em last_page.html (diagnostico).
"""
import hashlib
import json
import os
import re
import smtplib
import sys
import time
from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import unquote, urljoin

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
MAX_DETAIL_FETCH = 25  # paginas de lote abertas por execucao para buscar a data (evita CAPTCHA)

LOTE_RE = re.compile(r"(\d{1,3})\s*[º°ªo]?[\s\-_]*lote", re.I)
ANO_RE = re.compile(r"ano[\s\-_]*base[\s\-_:]*(\d{4})|\bab[\s\-_]?(\d{4})", re.I)
PUB_RE = re.compile(r"Publicado em\s*(\d{2}/\d{2}/\d{4})", re.I)
BLOCKED = object()


# ---------- notificacao ----------

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


# ---------- coleta ----------

def get(url: str):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    if any(m in r.text for m in BLOCK_MARKERS):
        return BLOCKED
    return r.text


def fetch() -> str | None:
    """Retorna o HTML da pagina de lotes, ou None se caiu no CAPTCHA."""
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


def is_lote(url: str, text: str) -> bool:
    return "lote" in (text + " " + unquote(url)).lower()


def extract(html: str) -> tuple[dict[str, str], str]:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else "(sem titulo)"
    print(f"Titulo da pagina: {title}")

    main, used = soup.body or soup, "body"
    for sel in SELECTORS:
        node = soup.select_one(sel)
        if node and links_in(node):
            main, used = node, sel
            break

    all_links = links_in(main)
    links = {u: t for u, t in all_links.items() if is_relevant(u, t)}
    print(f"Seletor usado: {used}, {len(all_links)} links no total, {len(links)} relevantes")
    fingerprint = "\n".join(f"{u} {t}" for u, t in sorted(links.items()))
    return links, hashlib.sha256(fingerprint.encode()).hexdigest()


# ---------- detalhes do lote ----------

def parse_lote(url: str, text: str) -> dict:
    info: dict = {}
    for src in (text, unquote(url)):
        if "lote" not in info and (m := LOTE_RE.search(src)):
            info["lote"] = int(m.group(1))
        if "ano_base" not in info and (m := ANO_RE.search(src)):
            info["ano_base"] = m.group(1) or m.group(2)
    low = (text + " " + unquote(url)).lower()
    info["tipo"] = "Contestacao" if "contesta" in low else "Recurso" if "recurso" in low else "Parecer Tecnico"
    return info


def fetch_pub_date(url: str):
    """Abre a noticia do lote e le 'Publicado em dd/mm/aaaa'. PDFs nao tem essa data."""
    if ".pdf" in url.lower() or "gov.br" not in url:
        return None
    html = get(url)
    if html is BLOCKED:
        return BLOCKED
    m = PUB_RE.search(html)
    if not m:
        # Tambem tenta ano-base no corpo da noticia, caso o titulo do link nao tenha.
        return None
    return m.group(1)


def update_details(links: dict[str, str], info: dict, new_urls: set[str], first_run: bool) -> None:
    lote_urls = [u for u, t in links.items() if is_lote(u, t)]
    lote_urls.sort(key=lambda u: u not in new_urls)  # novos primeiro
    fetched = 0
    for u in lote_urls:
        rec = info.get(u)
        if rec is None:
            rec = parse_lote(u, links[u])
            if not first_run:
                rec["detectado"] = date.today().strftime("%d/%m/%Y")
            info[u] = rec
        if "publicado" in rec or rec.get("sem_data") or fetched >= MAX_DETAIL_FETCH:
            continue
        try:
            d = fetch_pub_date(u)
        except requests.RequestException as e:
            print(f"Falha ao abrir {u}: {e}")
            continue
        fetched += 1
        if d is BLOCKED:
            print("CAPTCHA ao abrir paginas de lote; continua na proxima execucao.")
            break
        if d:
            rec["publicado"] = d
        else:
            rec["sem_data"] = True
        time.sleep(1)
    pend = sum(1 for u in lote_urls if "publicado" not in info[u] and not info[u].get("sem_data"))
    print(f"{len(lote_urls)} lotes, {fetched} datas buscadas agora, {pend} pendentes")
    # Esquece detalhes de links que sairam da pagina.
    for u in list(info):
        if u not in links:
            del info[u]


def _pdate(s: str) -> datetime:
    return datetime.strptime(s, "%d/%m/%Y")


def latest_lote(links: dict[str, str], info: dict) -> str | None:
    dated = [u for u in info if u in links and "publicado" in info[u]]
    if dated:
        return max(dated, key=lambda u: _pdate(info[u]["publicado"]))
    # Sem datas ainda: usa maior ano-base e maior numero de lote.
    cands = [u for u in info if u in links and "ano_base" in info[u]]
    if cands:
        return max(cands, key=lambda u: (info[u]["ano_base"], info[u].get("lote", 0)))
    return None


def describe(url: str, text: str, rec: dict | None) -> str:
    rec = rec or {}
    lines = [text]
    if rec.get("lote"):
        lines.append(f"Lote: {rec['lote']} ({rec.get('tipo', '')})")
    lines.append(f"Ano-base: {rec.get('ano_base', 'nao identificado')}")
    if rec.get("publicado"):
        lines.append(f"Publicado em: {rec['publicado']}")
    elif rec.get("detectado"):
        lines.append(f"Detectado pelo monitor em: {rec['detectado']}")
    else:
        lines.append("Data de publicacao: nao encontrada")
    lines.append(url)
    return "\n".join(lines)


def latest_summary(links: dict[str, str], info: dict) -> str:
    u = latest_lote(links, info)
    if not u:
        return "Ultimo lote: nao consegui identificar."
    return "ULTIMO LOTE\n" + describe(u, links[u], info.get(u))


# ---------- principal ----------

def save(state: dict) -> None:
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    manual = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"

    html = fetch()
    if html is None:
        if not state.get("blocked"):
            notify("Monitor Lei do Bem: bloqueado", f"O site devolveu CAPTCHA. Verifique manualmente:\n{URL}")
            state["blocked"] = True
            save(state)
        print("Bloqueado por CAPTCHA.")
        return 0

    links, digest = extract(html)
    if not links:
        if not state.get("empty"):
            notify("Monitor Lei do Bem: pagina veio sem links", "Resposta inesperada do site. Veja o artifact last_page no GitHub Actions.")
            state["empty"] = True
            save(state)
        return 0

    old_links = state.get("links") or {}
    first_run = not old_links
    new = {} if first_run else {u: t for u, t in links.items() if u not in old_links}
    info = state.get("info") or {}
    update_details(links, info, set(new), first_run)

    if state.get("blocked") or state.get("empty"):
        notify("Monitor Lei do Bem: voltou a funcionar", URL)

    if first_run:
        notify("Monitor Lei do Bem ativo", f"{len(links)} links monitorados.\n\n{latest_summary(links, info)}")
    elif new:
        body = "\n\n".join(describe(u, t, info.get(u)) for u, t in new.items())
        notify(f"Lei do Bem: {len(new)} novidade(s) na pagina de lotes", f"{body}\n\nPagina: {URL}")
    elif digest != state.get("hash"):
        notify("Lei do Bem: algum link da pagina de lotes mudou", f"Nenhum link novo, mas algum titulo ou link foi alterado.\n\n{latest_summary(links, info)}\n\nPagina: {URL}")
    elif manual:
        notify("Monitor Lei do Bem: sem novidades", f"{len(links)} links monitorados.\n\n{latest_summary(links, info)}")

    save({"links": links, "hash": digest, "info": info, "blocked": False, "empty": False})
    return 0


if __name__ == "__main__":
    sys.exit(main())
