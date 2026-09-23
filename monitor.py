"""Monitora a pagina de lotes da Lei do Bem (MCTI) e avisa por Telegram, Teams e/ou email.

A pagina tem uma tabela por ano-base com: nome do lote, data de publicacao e link de download.
- Avisa quando aparece linha nova (lote novo) ou quando uma linha existente muda.
- Rodando manualmente (Run workflow), manda um resumo com o ultimo lote publicado.
- Estado em state.json; ultima pagina baixada em last_page.html (diagnostico).
-Conferindo se esta automatico
"""
import hashlib
import json
import os
import re
import smtplib
import socket
import sys
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin

import requests
import urllib3.util.connection as urllib3_conn
from bs4 import BeautifulSoup

# Forca IPv4: a rota IPv6 dos servidores do GitHub ate o gov.br as vezes falha ("Network is unreachable").
urllib3_conn.allowed_gai_family = lambda: socket.AF_INET

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
FETCH_TRIES = 3          # tentativas por execucao
FAILS_BEFORE_ALERT = 3   # execucoes seguidas com site fora antes de avisar (~30 min)

DATE_RE = re.compile(r"(\d{1,2})\s*/+\s*(\d{1,2})\s*/+\s*(\d{4})")  # aceita erros tipo 14/09//2026
ANO_RE = re.compile(r"ano[\s\-_]*base[\s\-_:]*(\d{4})", re.I)
LOTE_RE = re.compile(r"(\d{1,3})\s*[º°ªo]?\s*lote", re.I)


# ---------- notificacao ----------

def send_telegram(title: str, body: str) -> None:
    tok = os.getenv("TELEGRAM_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID") or "1489648434"
    if not tok:
        print("Telegram: TELEGRAM_TOKEN nao configurado, pulando")
        return
    requests.post(
        f"https://api.telegram.org/bot{tok}/sendMessage",
        data={"chat_id": chat, "text": f"{title}\n\n{body}", "disable_web_page_preview": True},
        timeout=20,
    ).raise_for_status()


def send_email(title: str, body: str) -> None:
    user, pwd, to = os.getenv("GMAIL_USER"), os.getenv("GMAIL_APP_PASSWORD"), os.getenv("MAIL_TO")
    if not (user and pwd and to):
        return
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = title, user, to
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pwd)
        s.send_message(msg)


def teams_card(title: str, body: str) -> dict:
    """Adaptive Card no formato aceito pelo fluxo de webhook do Teams (app Workflows)."""
    blocks = [{"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True}]
    for paragraph in body.split("\n\n"):
        text = "\n\n".join(line for line in paragraph.splitlines() if line.strip())
        if text:
            blocks.append({"type": "TextBlock", "text": text, "wrap": True, "spacing": "Medium"})
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None,
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": blocks,
                "actions": [{"type": "Action.OpenUrl", "title": "Abrir pagina de lotes", "url": URL}],
            },
        }],
    }


def send_teams(title: str, body: str) -> None:
    hook = os.getenv("TEAMS_WEBHOOK_URL")
    if not hook:
        print("Teams: TEAMS_WEBHOOK_URL nao configurado, pulando")
        return
    r = requests.post(hook.strip(), json=teams_card(title, body), timeout=30)
    print(f"Teams: HTTP {r.status_code} {r.text[:300]}")
    r.raise_for_status()


def notify(title: str, body: str) -> None:
    """Envia para todos os canais configurados. Falha em um nao impede os outros."""
    errors = []
    for name, send in (("Telegram", send_telegram), ("Teams", send_teams), ("Email", send_email)):
        try:
            send(title, body)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
            print(f"Falha ao enviar por {name}: {e}")
    if errors and len(errors) == 3:
        raise RuntimeError("Nenhum canal de notificacao funcionou: " + "; ".join(errors))


# ---------- coleta ----------

def fetch() -> str | None:
    """Retorna o HTML da pagina de lotes, ou None se caiu no CAPTCHA."""
    for attempt in range(1, FETCH_TRIES + 1):
        try:
            r = requests.get(URL, headers=HEADERS, timeout=30)
            if r.status_code < 500:
                break
            print(f"Tentativa {attempt}: HTTP {r.status_code}")
        except requests.RequestException as e:
            print(f"Tentativa {attempt}: {e.__class__.__name__}")
            if attempt == FETCH_TRIES:
                raise
        time.sleep(10)
    print(f"HTTP {r.status_code}, {len(r.text)} caracteres")
    DEBUG_HTML.write_text(r.text, encoding="utf-8")
    r.raise_for_status()
    if any(m in r.text for m in BLOCK_MARKERS):
        return None
    return r.text


def clean(text: str) -> str:
    return " ".join(text.split())


def parse_date(text: str) -> str | None:
    m = DATE_RE.search(text)
    if not m:
        return None
    d, mth, y = m.groups()
    return f"{int(d):02d}/{int(mth):02d}/{y}"


def extract(html: str) -> dict[str, dict]:
    """Le todas as tabelas e devolve {chave: linha}. Chave = link de download (ou nome, se nao tiver)."""
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("#content") or soup.select_one("main") or soup.body or soup
    rows: dict[str, dict] = {}

    for table in root.find_all("table"):
        heading = table.find_previous(["h2", "h3", "h4"])
        heading_text = clean(heading.get_text()) if heading else ""
        m = re.search(r"(\d{4})", heading_text)
        table_ano = m.group(1) if m else None

        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if not tds:
                continue  # linha de cabecalho
            nome = clean(tds[0].get_text())
            if not nome:
                continue
            data = next((d for td in tds[1:] if (d := parse_date(td.get_text()))), None)
            a = tr.find("a", href=True)
            url = urljoin(URL, a["href"]) if a else None

            low = nome.lower()
            m_ano = ANO_RE.search(nome)
            m_lote = LOTE_RE.search(nome)
            row = {
                "nome": nome,
                "data": data,
                "ano_base": m_ano.group(1) if m_ano else table_ano,
                "lote": int(m_lote.group(1)) if m_lote else None,
                "tipo": "Contestacao" if "contesta" in low else "Recurso administrativo" if "recurso" in low else "Parecer Tecnico",
                "url": url,
            }
            rows[url or f"{table_ano}|{nome}"] = row

    print(f"{len(rows)} linhas lidas das tabelas")
    return rows


# ---------- mensagens ----------

def _key_date(row: dict) -> datetime:
    return datetime.strptime(row["data"], "%d/%m/%Y")


def latest(rows: dict[str, dict]) -> dict | None:
    dated = [r for r in rows.values() if r.get("data")]
    return max(dated, key=_key_date) if dated else None


def describe(row: dict) -> str:
    lines = [row["nome"]]
    if row.get("lote"):
        lines.append(f"Lote: {row['lote']} ({row['tipo']})")
    lines.append(f"Ano-base: {row.get('ano_base') or 'nao identificado'}")
    lines.append(f"Publicado em: {row.get('data') or 'sem data na tabela'}")
    if row.get("url"):
        lines.append(row["url"])
    return "\n".join(lines)


def latest_summary(rows: dict[str, dict]) -> str:
    r = latest(rows)
    return "ULTIMO LOTE\n" + describe(r) if r else "Ultimo lote: nenhuma data encontrada nas tabelas."


# ---------- principal ----------

def save(state: dict) -> None:
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    manual = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"

    try:
        html = fetch()
    except requests.RequestException as e:
        fails = state.get("fail_count", 0) + 1
        state["fail_count"] = fails
        print(f"Site inacessivel ({fails}a execucao seguida): {e}")
        if fails == FAILS_BEFORE_ALERT:
            notify("Monitor Lei do Bem: site fora do ar", f"Nao consigo acessar a pagina ha {fails} verificacoes seguidas. Aviso quando voltar.\n{URL}")
        save(state)
        return 0
    if state.get("fail_count", 0) >= FAILS_BEFORE_ALERT:
        notify("Monitor Lei do Bem: site voltou", URL)
    state.pop("fail_count", None)

    if html is None:
        if not state.get("blocked"):
            notify("Monitor Lei do Bem: bloqueado", f"O site devolveu CAPTCHA. Verifique manualmente:\n{URL}")
            state["blocked"] = True
            save(state)
        print("Bloqueado por CAPTCHA.")
        return 0

    rows = extract(html)
    if not rows:
        if not state.get("empty"):
            notify("Monitor Lei do Bem: nenhuma tabela encontrada", f"O layout da pagina pode ter mudado. Veja o artifact last_page no GitHub Actions.\n{URL}")
            state["empty"] = True
            save(state)
        return 0

    old = state.get("rows")
    if state.get("blocked") or state.get("empty"):
        notify("Monitor Lei do Bem: voltou a funcionar", URL)

    if old is None:
        notify("Monitor Lei do Bem ativo", f"{len(rows)} linhas monitoradas.\n\n{latest_summary(rows)}")
    else:
        new = [r for k, r in rows.items() if k not in old]
        changed = [r for k, r in rows.items() if k in old and r != old[k]]
        if new:
            new.sort(key=lambda r: _key_date(r) if r.get("data") else datetime.min, reverse=True)
            body = "\n\n".join(describe(r) for r in new)
            notify(f"Lei do Bem: {len(new)} lote(s) novo(s)!", f"{body}\n\nPagina: {URL}")
        if changed:
            body = "\n\n".join(describe(r) for r in changed)
            notify(f"Lei do Bem: {len(changed)} linha(s) alterada(s)", f"{body}\n\nPagina: {URL}")
        if not new and not changed and manual:
            notify("Monitor Lei do Bem: sem novidades", f"{len(rows)} linhas monitoradas.\n\n{latest_summary(rows)}")

    save({"rows": rows, "blocked": False, "empty": False})
    return 0


if __name__ == "__main__":
    sys.exit(main())
