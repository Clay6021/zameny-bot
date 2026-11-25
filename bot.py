import hashlib
import io
import json
import os
import re
import sys
from pathlib import Path

import requests
from pypdf import PdfReader

URL = "https://mkgt.ru/z-l/"  # PDF с заменами, территория Люблино
STATE_FILE = Path("state.json")

TOKEN = os.environ["TG_TOKEN"]
CHAT_ID = os.environ["TG_CHAT_ID"]


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


def send_pdf(pdf: bytes, caption: str) -> None:
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendDocument",
        data={"chat_id": CHAT_ID, "caption": caption},
        files={"document": ("zameny-lyublino.pdf", pdf, "application/pdf")},
        timeout=120,
    )
    r.raise_for_status()


def main() -> None:
    pdf = fetch_pdf()
    if pdf is None:
        return

    text = extract_text(pdf)
    # Хеш от текста, а не от файла: метаданные PDF могут меняться без правок замен
    digest = hashlib.sha256((text or pdf.hex()).encode()).hexdigest()

    old = json.loads(STATE_FILE.read_text())["hash"] if STATE_FILE.exists() else None
    if digest == old:
        print("Без изменений")
        return

    send_pdf(pdf, get_title(text))
    STATE_FILE.write_text(json.dumps({"hash": digest}))
    print("Отправлено")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Ошибка: {e}")
        sys.exit(1)
