import re
import sys
import os
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Set, Tuple
import requests
from bs4 import BeautifulSoup

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'babki')))
from bots.config_loader import ConfigLoader
config = ConfigLoader('config.ini')

YEAR = str(datetime.now().year)
URL = config.get('URL', 'url') + YEAR

MONTHS_RU = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

YEAR_PATTERNS = [
    r"(?P<year>20\d{2})\s*года",   # «2025 года»
    r"за\s+(?P<year>20\d{2})\s*год",
    r"календар[ьяе][\w\s]+(?P<year>20\d{2})",
]

# шаблоны дат в свободном тексте
# 1) перечисление «1, 2, 7 января»
LISTED_DAYS_RE = re.compile(
    r"(?P<days>(?:\d{1,2}\s*,\s*)*(?:\d{1,2})(?:\s*и\s*\d{1,2})?)\s+"
    r"(?P<month>[а-я]+)\b",
    re.IGNORECASE
)
# 2) диапазон «с 1 по 8 января»
RANGE_RE = re.compile(
    r"с\s+(?P<d1>\d{1,2})\s+по\s+(?P<d2>\d{1,2})\s+(?P<month>[а-я]+)\b",
    re.IGNORECASE
)
# 3) одиночные даты «8 марта», «12 июня»
SINGLE_DAY_RE = re.compile(
    r"(?<!\d)(?P<day>\d{1,2})\s+(?P<month>[а-я]+)\b",
    re.IGNORECASE
)
# 4) переносы «перенос(ятся|а)|перенести … с 3 января на 10 января»
TRANSFER_RE = re.compile(
    r"(перенос\w*|перенести\w*|переносят\w*|переносят\w*).{0,80}?"
    r"(?P<from_day>\d{1,2})\s+(?P<from_month>[а-я]+).{0,40}?"
    r"на\s+(?P<to_day>\d{1,2})\s+(?P<to_month>[а-я]+)",
    re.IGNORECASE | re.DOTALL
)

@dataclass
class ParseResult:
    year: int
    nonworking_dates: Set[date]   # праздники/переносы
    notes: List[str]              # диагностические заметки

def _detect_year(text: str, fallback: Optional[int] = None) -> int:
    for pat in YEAR_PATTERNS:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            return int(m.group("year"))
    if fallback:
        return fallback
    # если не нашли явный год — пробуем текущий
    return date.today().year

def _normalize_month(token: str) -> Optional[int]:
    token = token.lower().strip(" .,:;")
    return MONTHS_RU.get(token)

def _safe_date(year: int, month: int, day: int) -> Optional[date]:
    try:
        return date(year, month, day)
    except ValueError:
        return None

def extract_nonworking_dates(text: str, year_hint: Optional[int] = None) -> ParseResult:
    notes: List[str] = []
    year = _detect_year(text, year_hint)
    nonworking: Set[date] = set()

    # 1) Диапазоны «с 1 по 8 января»
    for m in RANGE_RE.finditer(text):
        m_id = m.groupdict()
        month = _normalize_month(m_id["month"])
        if not month:
            continue
        d1, d2 = int(m_id["d1"]), int(m_id["d2"])
        for dday in range(min(d1, d2), max(d1, d2) + 1):
            dt = _safe_date(year, month, dday)
            if dt:
                nonworking.add(dt)
        notes.append(f"Диапазон: {d1}-{d2} {m_id['month']}")

    # 2) Перечни «1, 2 и 7 января»
    for m in LISTED_DAYS_RE.finditer(text):
        month = _normalize_month(m.group("month"))
        if not month:
            continue
        raw_days = m.group("days")
        # защитимся от ложных срабатываний (например, «статьи 1, 2, 3» без месяца)
        if not raw_days or not re.search(r"\d", raw_days):
            continue
        # распарсим «1, 2 и 7»
        days: List[int] = []
        chunk = raw_days.replace(" и ", ",")
        for token in chunk.split(","):
            token = token.strip()
            if token.isdigit():
                days.append(int(token))
        if not days:
            continue
        # фильтр: не включать то, что уже покрыто «с X по Y»
        added_any = False
        for dday in days:
            dt = _safe_date(year, month, dday)
            if dt and dt not in nonworking:
                nonworking.add(dt)
                added_any = True
        if added_any:
            notes.append(f"Перечень: {raw_days} {m.group('month')}")

    # 3) Одиночные даты «8 марта», «12 июня»
    #    Пройдёмся аккуратно: пропустим то, что уже вошло выше.
    for m in SINGLE_DAY_RE.finditer(text):
        month = _normalize_month(m.group("month"))
        if not month:
            continue
        dday = int(m.group("day"))
        dt = _safe_date(year, month, dday)
        if dt and dt not in nonworking:
            # чтобы не ловить каждую встречу «8 марта» вне контекста, проверим,
            # что рядом есть триггеры «нерабоч», «празднич», «выходн».
            window = text[max(0, m.start()-40): m.end()+40].lower()
            if any(key in window for key in ("нерабоч", "празднич", "выходн")):
                nonworking.add(dt)
                notes.append(f"Одиночная дата: {dday} {m.group('month')}")

    # 4) Переносы «… с 3 января на 10 января»
    for m in TRANSFER_RE.finditer(text):
        d_from = int(m.group("from_day"))
        d_to = int(m.group("to_day"))
        m_from = _normalize_month(m.group("from_month"))
        m_to = _normalize_month(m.group("to_month"))
        if m_to:
            dt_to = _safe_date(year, m_to, d_to)
            if dt_to:
                nonworking.add(dt_to)
                notes.append(f"Перенос на: {d_to} {m.group('to_month')}")

    return ParseResult(year=year, nonworking_dates=nonworking, notes=notes)

def build_year_map(year: int, nonworking: Iterable[date]) -> Dict[int, str]:
    """
    Возвращает словарь: day_of_year (1..365/366) -> 'рабочий' | 'нерабочий'
    База: суббота/воскресенье — 'нерабочий'. nonworking — дополнительно отмечаем как 'нерабочий'.
    """
    nonworking_set = set(nonworking)
    start = date(year, 1, 1)
    end = date(year + 1, 1, 1)
    result: Dict[int, str] = {}
    d = start
    while d < end:
        doy = (d - start).days + 1
        is_weekend = d.weekday() >= 5  # 5 = Saturday, 6 = Sunday
        val = "нерабочий" if (is_weekend or d in nonworking_set) else "рабочий"
        result[doy] = val
        d += timedelta(days=1)
    return result

def fetch_consultant_text(url: str, timeout: float = 15.0, cookies: Optional[Dict[str, str]] = None) -> str:
    """
    Тянем HTML и получаем из него чистый текст. При необходимости можно передать cookies/headers.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "ru,en;q=0.9",
    }
    resp = requests.get(url, headers=headers, cookies=cookies, timeout=timeout)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    # иногда полезно скрыть скрипты/стили
    for tag in soup(["script", "style", "noscript"]):
        tag.extract()
    text = soup.get_text(separator=" ", strip=True)
    # у «некоммерческой версии» документ может открыться по редиректу; requests его последует
    return text

def parse_calendar_from_consultant(url: str, year_hint: Optional[int] = None,
                                   cookies: Optional[Dict[str, str]] = None) -> Tuple[int, Dict[int, str], List[str]]:
    """
    Главная точка входа.
    Возвращает (year, day_of_year_map, notes)
    """
    raw_text = fetch_consultant_text(url, cookies=cookies)
    parsed = extract_nonworking_dates(raw_text, year_hint=year_hint)
    day_map = build_year_map(parsed.year, parsed.nonworking_dates)
    return parsed.year, day_map, parsed.notes

if __name__ == "__main__":
    year, day_map, notes = parse_calendar_from_consultant(URL, year_hint=YEAR)
    print(f"Год: {year}")
    print(f"Пример: 1 января — {day_map[1]}")
    # Если нужно — выведите весь словарь:
    # print(day_map)
    # Или сохраните в JSON:
    # import json, pathlib
    # pathlib.Path(f"calendar_{year}.json").write_text(json.dumps(day_map, ensure_ascii=False, indent=2), encoding="utf-8")
    # Сохранить в JSON (day_of_year -> status)
    j_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'babki')) + '/bots'
    with open(f"{j_path}/calendar_{year}.json", "w", encoding="utf-8") as f:
        json.dump(day_map, f, ensure_ascii=False, indent=2)

    # Диагностика извлечения (какие конструкции были распознаны)
    if notes:
        print("Найденные конструкции:", *notes, sep="\n- ")
