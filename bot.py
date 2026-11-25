import hashlib
import io
import json
import os
import re
import sys
import time
from pathlib import Path

import pdfplumber
import requests
from pypdf import PdfReader

URL = "https://mkgt.ru/z-l/"  # PDF с заменами, территория Люблино
STATE_FILE = Path("state.json")

# Группа, по которой бот пишет подробности. Можно сменить тут или через переменную WATCH_GROUP
GROUP = os.environ.get("WATCH_GROUP", "ТоВХ-319")

# Латинские буквы, похожие на кириллические (на случай опечаток в документе)
_LOOKALIKE = str.maketrans("TOBXACEHKMPaceopxy", "ТОВХАСЕНКМРасеорху")
_HEADER_WORDS = ("ПРЕПОДАВАТЕЛЬ", "ОСНОВАНИЕ", "ГРУППА", "НОМЕР", "ПРИМЕЧ")


def fetch_pdf() -> bytes | None:
    try:
        r = requests.get(URL, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"Сайт недоступен: {e}")
        return None
    if not r.content.startswith(b"%PDF"):
        print("По адресу не PDF: возможно, сайт изменил формат")
        return None
    return r.content


def extract_text(pdf: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    return re.sub(r"\s+", " ", text).strip()


def get_title(text: str) -> str:
    m = re.search(r"на\s+(\d{1,2}\s+[а-яё]+\s+\d{4}\s+года(?:\s*\([а-яё]+\))?)", text, re.I)
    return f"Изменения в расписании на {m.group(1)}" if m else "Изменения в расписании"


# ---------- разбор замен для одной группы ----------

def norm(s: str) -> str:
    s = (s or "").translate(_LOOKALIKE)
    s = re.sub(r"[\s ]+", "", s)
    s = re.sub(r"[–—−‑]", "-", s)
    return s.lower()


def clean(cell) -> str:
    s = " ".join((cell or "").split())
    return re.sub(r"-{3,}", "—", s)


def group_rows(pdf: bytes, group: str) -> tuple[list[list[str]], int]:
    """Возвращает (строки замен нужной группы, общее число строк с данными в таблице)."""
    target = norm(group)
    found: list[list[str]] = []
    total = 0
    current = ""  # ячейка группы объединена по вертикали, поэтому протягиваем вниз
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        for page in doc.pages:
            tables = page.extract_tables()
            if not tables:
                tables = page.extract_tables(
                    {"vertical_strategy": "text", "horizontal_strategy": "text"}
                )
            for table in tables:
                for row in table:
                    cells = [clean(c) for c in row]
                    if not cells:
                        continue
                    if any(w in " ".join(cells).upper() for w in _HEADER_WORDS):
                        continue  # шапка таблицы
                    if cells[0]:
                        current = cells[0]
                    if len([c for c in cells[1:] if c]) < 2:
                        continue
                    total += 1
                    if target in norm(current):
                        found.append(cells)
    return found, total


def format_row(cells: list[str]) -> str:
    d = cells[1:]
    if len(d) >= 6:
        pair, old, reason, new, newpair, note = d[:6]
        line = f"• {pair} пара: {old or '—'} → {new or '—'}"
        extra = []
        if reason:
            extra.append(reason)
        if newpair and newpair not in ("--", "—", "-") and newpair != pair:
            extra.append(f"проведут {newpair} парой")
        if note and note != "--":
            extra.append(note)
        if extra:
            line += f" ({', '.join(extra)})"
        return line
    return "• " + " | ".join(c for c in d if c)


def group_summary(pdf: bytes, text: str) -> str:
    try:
        rows, total = group_rows(pdf, GROUP)
    except Exception as e:
        print(f"Не удалось разобрать таблицу: {e}")
        return f"{GROUP}: не удалось разобрать таблицу, смотрите файл."
    if rows:
        return f"{GROUP}, замены:\n" + "\n".join(format_row(r) for r in rows)
    if total:
        msg = f"{GROUP}: в таблице замен нет."
        if norm(GROUP) in norm(text):
            msg += " Группа указана в списках внизу документа (формат обучения или практика), смотрите файл."
        return msg
    return f"{GROUP}: не удалось разобрать таблицу, смотрите файл."


# ---------- Telegram ----------

def tg_post(method: str, **kwargs) -> None:
    """Запрос к Telegram с повторами при сбоях сети и ошибках 429/5xx."""
    token = os.environ["TG_TOKEN"]
    last: Exception | None = None
    for attempt in range(4):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/{method}", timeout=120, **kwargs
            )
        except requests.RequestException as e:
            last = e
            print(f"Сбой сети (попытка {attempt + 1}): {e}")
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            last = RuntimeError(f"Telegram {r.status_code}: {r.text}")
            print(f"Telegram временно недоступен (попытка {attempt + 1}): {r.status_code}")
            time.sleep(5 * (attempt + 1))
            continue
        if not r.ok:
            raise RuntimeError(f"Telegram {r.status_code}: {r.text}")
        return
    raise last if last else RuntimeError("Не удалось отправить в Telegram")


def send_pdf(pdf: bytes, title: str, summary: str) -> None:
    chat_id = os.environ["TG_CHAT_ID"]
    caption = f"{title}\n\n{summary}" if summary else title
    long_summary = len(caption) > 1024  # лимит подписи в Telegram
    tg_post(
        "sendDocument",
        data={"chat_id": chat_id, "caption": title if long_summary else caption},
        files={"document": ("zameny-lyublino.pdf", pdf, "application/pdf")},
    )
    if long_summary:
        tg_post("sendMessage", data={"chat_id": chat_id, "text": summary[:4000]})


def main() -> None:
    pdf = fetch_pdf()
    if pdf is None:
        return

    text = extract_text(pdf)
    # Хеш от текста, а не от файла: метаданные PDF могут меняться без правок замен
    digest = hashlib.sha256((text or pdf.hex()).encode()).hexdigest()

    try:
        old = json.loads(STATE_FILE.read_text())["hash"]
    except (OSError, ValueError, KeyError):
        old = None  # файла нет или он пустой/битый: считаем замены новыми
    if digest == old:
        print("Без изменений")
        return

    send_pdf(pdf, get_title(text), group_summary(pdf, text))
    STATE_FILE.write_text(json.dumps({"hash": digest}))
    print("Отправлено")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--parse":
        # Проверка без Telegram: python bot.py --parse файл.pdf
        data = Path(sys.argv[2]).read_bytes()
        print(group_summary(data, extract_text(data)))
        sys.exit(0)
    try:
        main()
    except Exception as e:
        print(f"Ошибка: {e}")
        sys.exit(1)
