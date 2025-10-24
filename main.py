import asyncio
import io
import re
import textwrap
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, urljoin
import html  # вверху файла
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import os

import httpx
from bs4 import BeautifulSoup
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.units import mm
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle

from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery


from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import os
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph, Table, TableStyle, Spacer

# ================== CONFIG ==================
BOT_TOKEN = "7863780174:AAF75id82mMv3RvmlHBVj9ObNpDD-472w8w"
REQUEST_TIMEOUT = 15  # seconds
# ============================================

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ===== In-memory storage для последних отчётов =====
USER_REPORTS: Dict[int, Dict[str, str]] = {}  # {user_id: {"text": str, "pdf_path": str}}

def esc(s: str) -> str:
    return html.escape(s or "")

# ================== Утилиты ==================
def normalize_url(raw: str) -> Optional[str]:
    raw = raw.strip().strip("`")
    if not raw:
        return None
    if not re.match(r"^https?://", raw, flags=re.I):
        raw = "https://" + raw
    try:
        u = urlparse(raw)
        if not u.netloc:
            return None
        # убираем путь — работаем с корнем домена
        base = f"{u.scheme}://{u.netloc}"
        return base
    except Exception:
        return None

async def fetch_text(client: httpx.AsyncClient, url: str) -> Tuple[Optional[str], int, str]:
    try:
        r = await client.get(url, timeout=REQUEST_TIMEOUT, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        return (r.text, r.status_code, str(r.url))
    except Exception:
        return (None, 0, url)

async def fetch_ok(client: httpx.AsyncClient, url: str) -> bool:
    try:
        r = await client.get(url, timeout=REQUEST_TIMEOUT, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        return r.status_code < 400
    except Exception:
        return False

def short(text: str, n: int = 180) -> str:
    t = " ".join(text.split())
    return t if len(t) <= n else t[: n - 1] + "…"

@dataclass
class AuditItem:
    name: str
    status: str   # "ok" | "warn" | "fail" | "na"
    note: str
    todo: str

# ================== Аудит ==================
async def audit_site(base_url: str) -> Tuple[str, List[AuditItem], Dict[str, List[AuditItem]]]:
    """
    Возвращает:
      summary_text, main_table_items, sections_dict
      где sections_dict может содержать "AI Generative", "Prompt→Page Map" и т.п.
    """
    items: List[AuditItem] = []
    sections: Dict[str, List[AuditItem]] = {}

    async with httpx.AsyncClient(http2=True) as client:
        # 1) Главная страница
        html, code, final_home = await fetch_text(client, base_url)
        soup = BeautifulSoup(html or "", "lxml") if html else None

        # 2) robots.txt
        robots_url = urljoin(base_url + "/", "robots.txt")
        robots_txt, robots_code, _ = await fetch_text(client, robots_url)
        has_robots = bool(robots_txt and robots_code < 400)

        # 3) sitemap (из robots или по умолчанию /sitemap.xml)
        sitemaps = []
        if robots_txt:
            for line in robots_txt.splitlines():
                if line.lower().strip().startswith("sitemap:"):
                    sitemaps.append(line.split(":", 1)[1].strip())
        default_sm = urljoin(base_url + "/", "sitemap.xml")
        if default_sm not in sitemaps:
            if await fetch_ok(client, default_sm):
                sitemaps.append(default_sm)

        # 4) llms.txt / ai.txt
        llms_url = urljoin(base_url + "/", "llms.txt")
        ai_url = urljoin(base_url + "/", "ai.txt")
        llms_txt, llms_code, _ = await fetch_text(client, llms_url)
        ai_txt, ai_code, _ = await fetch_text(client, ai_url)
        has_llms = bool(llms_txt and llms_code < 400)
        has_ai = bool(ai_txt and ai_code < 400)

        # 5) schema.org JSON-LD
        schema_types = set()
        if soup:
            for s in soup.select('script[type="application/ld+json"]'):
                try:
                    import json
                    data = json.loads(s.string or "{}")
                    def collect_types(node):
                        if isinstance(node, dict):
                            t = node.get("@type")
                            if isinstance(t, str):
                                schema_types.add(t)
                            elif isinstance(t, list):
                                schema_types.update([x for x in t if isinstance(x, str)])
                            for v in node.values():
                                collect_types(v)
                        elif isinstance(node, list):
                            for v in node:
                                collect_types(v)
                    collect_types(data)
                except Exception:
                    continue

        # 6) canonical
        has_canonical = bool(soup and soup.find("link", rel=lambda v: v and "canonical" in v.lower()))

        # 7) viewport (mobile)
        has_viewport = bool(soup and soup.find("meta", attrs={"name": "viewport"}))

        # 8) anchors (стабильные якоря для цитирования)
        anchors_ok = False
        if soup:
            h_ids = [h.get("id") for h in soup.select("h1,h2,h3,h4,h5,h6") if h.get("id")]
            anchors_ok = len(h_ids) >= 3

        # 9) Social / sameAs
        same_as_present = False
        if schema_types:
            # попробуем вытащить sameAs
            for s in soup.select('script[type="application/ld+json"]'):
                try:
                    import json
                    data = json.loads(s.string or "{}")
                    def find_same_as(node):
                        if isinstance(node, dict):
                            if "sameAs" in node and isinstance(node["sameAs"], (list, str)):
                                return True
                            for v in node.values():
                                if find_same_as(v):
                                    return True
                        elif isinstance(node, list):
                            for v in node:
                                if find_same_as(v):
                                    return True
                        return False
                    if find_same_as(data):
                        same_as_present = True
                        break
                except Exception:
                    pass

        # 10) Internal linking (очень условно)
        internal_links = 0
        if soup:
            domain = urlparse(base_url).netloc
            for a in soup.find_all("a", href=True):
                href = a["href"]
                u = urlparse(urljoin(base_url + "/", href))
                if u.netloc == domain:
                    internal_links += 1
        internal_ok = internal_links >= 20

        # ======== Главная таблица ========
        # KI-Crawling robots.txt (и наличие явных директив для LLM-ботов)
        llm_agents = ["GPTBot", "CCBot", "ClaudeBot", "PerplexityBot"]
        llm_mentions = []
        if robots_txt:
            for agent in llm_agents:
                if re.search(rf"(?i){re.escape(agent)}", robots_txt):
                    llm_mentions.append(agent)
        if has_robots and llm_mentions:
            items.append(AuditItem("KI-Crawling robots.txt", "ok",
                                   f"Обнаружены директивы для: {', '.join(llm_mentions)}",
                                   "Поддерживать актуальные Allow/Disallow для LLM-ботов."))
        elif has_robots and not llm_mentions:
            items.append(AuditItem("KI-Crawling robots.txt", "warn",
                                   "Доступ для поисковых ботов есть, но нет явных директив для GPTBot/ClaudeBot/Perplexity.",
                                   "Добавить явные правила для LLM-ботов в robots.txt."))
        else:
            items.append(AuditItem("KI-Crawling robots.txt", "fail",
                                   "robots.txt не найден.",
                                   "Создать robots.txt и указать правила обхода."))

        # llms.txt / ai.txt
        if has_llms or has_ai:
            which = "llms.txt" if has_llms else "ai.txt"
            body = (llms_txt or ai_txt or "").strip()
            has_policy = bool(re.search(r"(?i)policy", body))
            has_contact = bool(re.search(r"(?i)contact", body))
            has_sitemap_kw = bool(re.search(r"(?i)sitemap", body))
            miss = []
            if not has_policy: miss.append("Policy")
            if not has_contact: miss.append("Contact")
            if not has_sitemap_kw: miss.append("Sitemap")
            if miss:
                items.append(AuditItem("llms.txt / ai.txt", "warn",
                                       f"Найден {which}, но отсутствуют поля: {', '.join(miss)}.",
                                       "Заполнить Policy/Contact/Sitemap в файле LLM-policy."))
            else:
                items.append(AuditItem("llms.txt / ai.txt", "ok",
                                       f"Найден {which} с ключевыми полями.",
                                       "Поддерживать документ в актуальном состоянии."))
        else:
            items.append(AuditItem("llms.txt / ai.txt", "fail",
                                   "Отсутствует политика LLM-индексации.",
                                   "Создать llms.txt (или ai.txt) с Policy, Contact и Sitemap."))

        # Schema.org
        if schema_types:
            core = {t for t in schema_types if t in {"Organization", "VideoObject", "FAQPage", "HowTo", "WebPage", "BreadcrumbList"}}
            if core:
                items.append(AuditItem("Schema.org", "warn" if len(core) < 3 else "ok",
                                       f"Найдены типы: {', '.join(sorted(core))}",
                                       "Расширить покрытие JSON-LD ключевыми типами (FAQPage, HowTo, WebPage, BreadcrumbList)."))
            else:
                items.append(AuditItem("Schema.org", "warn",
                                       "JSON-LD найден, но ключевых типов мало.",
                                       "Добавить FAQPage/HowTo/BreadcrumbList/WebPage."))
        else:
            items.append(AuditItem("Schema.org", "warn",
                                   "Структурированные данные не обнаружены.",
                                   "Добавить JSON-LD для ключевых сущностей."))

        # Sitemap
        if sitemaps:
            items.append(AuditItem("Sitemap", "ok",
                                   f"Найдено: {', '.join(sitemaps[:3])}" + ("…" if len(sitemaps) > 3 else ""),
                                   "Контролировать актуальность sitemap раз в неделю."))
        else:
            items.append(AuditItem("Sitemap", "fail",
                                   "Sitemap не найден.",
                                   "Добавить sitemap.xml и/или указать его в robots.txt."))

        # Indexability / Canonical
        if has_canonical:
            items.append(AuditItem("Indexability / Canonical", "ok",
                                   "На главной найден <link rel='canonical'>.",
                                   "Поддерживать стабильные каноникалы."))
        else:
            items.append(AuditItem("Indexability / Canonical", "warn",
                                   "Каноникал не найден на главной.",
                                   "Добавить rel=canonical на страницы."))

        # Core Web Vitals (MVP — без внешних API)
        items.append(AuditItem("Core Web Vitals", "warn",
                               "Без лабораторного теста CWV — оценка недоступна.",
                               "Проверить LCP/INP/CLS через PageSpeed Insights и оптимизировать."))

        # Mobile
        items.append(AuditItem("Mobile", "ok" if has_viewport else "warn",
                               "Наличие viewport: " + ("да" if has_viewport else "нет"),
                               "Добавить <meta name='viewport'> и тест Mobile-Friendly."))

        # Internal Linking
        items.append(AuditItem("Internal Linking", "ok" if internal_ok else "warn",
                               f"Внутренних ссылок на главной: {internal_links}",
                               "Добавить тематические кластеры/хабы и навигационные блоки."))

        # FAQ / HowTo / Glossary по JSON-LD
        faq = "FAQPage" in schema_types
        howto = "HowTo" in schema_types
        if faq or howto:
            items.append(AuditItem("FAQ / HowTo / Glossary", "ok" if (faq and howto) else "warn",
                                   f"FAQPage: {'да' if faq else 'нет'}, HowTo: {'да' if howto else 'нет'}",
                                   "Расширить структурированные Q&A/HowTo разделы."))
        else:
            items.append(AuditItem("FAQ / HowTo / Glossary", "fail",
                                   "Структурированный FAQ/HowTo отсутствует.",
                                   "Добавить FAQPage/HowTo с JSON-LD."))

        # EEAT / Brand / Social
        items.append(AuditItem("EEAT", "ok",
                               "Авторитет бренда оценивается базово (MVP).",
                               "Поддерживать авторские страницы и источники."))
        items.append(AuditItem("Brand / Authority", "ok",
                               "Базовая оценка: бренд присутствует.",
                               "Мониторинг упоминаний и SGE-сниппетов."))
        items.append(AuditItem("Social", "ok" if same_as_present else "warn",
                               "Наличие sameAs в JSON-LD: " + ("да" if same_as_present else "нет"),
                               "Добавить ссылки sameAs на соцсети в JSON-LD."))

        # Freshness / Monitoring
        items.append(AuditItem("Freshness / Monitoring", "warn",
                               "Не проверяем частоту обновлений в MVP.",
                               "Добавить RSS/JSON-фиды и мониторинг свежести."))

        # GEO Extractability / Anchors
        items.append(AuditItem("GEO Extractability", "warn",
                               "Явные гео-сущности не проверяем в MVP.",
                               "Структурировать контакты/адреса в JSON-LD."))
        items.append(AuditItem("Anchors", "ok" if anchors_ok else "fail",
                               "Стабильные якоря для заголовков: " + ("да" if anchors_ok else "нет"),
                               "Добавить #anchors (id у h2/h3) для цитирования."))

        # ======== AI Generative чек-лист ========
        ai_sec: List[AuditItem] = []
        ai_sec.append(AuditItem("Q&A", "warn" if faq else "warn",
                                "Структурированный Q&A ограничен.",
                                "Добавить FAQ раздел с JSON-LD."))
        ai_sec.append(AuditItem("HowTo", "ok" if howto else "fail",
                                "Наличие HowTo: " + ("да" if howto else "нет"),
                                "Структурировать гайды в HowTo."))

        # Answer-Box / Lists / Atomic Answers / Citations / JSON-LD / Licensing
        has_lists = bool(soup and len(soup.select("ol,ul")) >= 1)
        ai_sec.append(AuditItem("Answer-Box", "warn",
                                "Краткие summary не проверяются автоматически.",
                                "Добавить краткие ответы/резюме на страницах."))
        ai_sec.append(AuditItem("Lists / Tables", "ok" if has_lists else "warn",
                                "Списки/таблицы на главной: " + ("есть" if has_lists else "нет"),
                                "Использовать списки шагов и сравнений."))
        ai_sec.append(AuditItem("Atomic Answers", "ok" if has_lists else "warn",
                                "Есть элементы, которые можно извлечь как краткие ответы.",
                                "Выделить атомарные ответы (короткие факты)."))
        ai_sec.append(AuditItem("Citations", "warn",
                                "Явные источники в контенте не проверяются автоматически.",
                                "Добавить источники/ссылки в обучающий контент."))
        ai_sec.append(AuditItem("JSON-LD", "ok" if schema_types else "warn",
                                f"JSON-LD: {'есть' if schema_types else 'нет'}",
                                "Расширить покрытие типами FAQPage/HowTo/WebPage/BreadcrumbList."))
        ai_sec.append(AuditItem("Licensing", "fail" if not (has_llms or has_ai) else "warn",
                                "Явной LLM-policy нет" if not (has_llms or has_ai) else "LLM-policy частично присутствует",
                                "Включить лицензию (например, CC BY 4.0) в LLM-policy."))
        sections["GEO Generative — AI-Snippettability"] = ai_sec

        # ======== Prompt → Page Map (очень упрощённо на основе sitemap) ========
        map_sec: List[AuditItem] = []
        intents = [
            ("как загрузить", "upload"),
            ("как монетизировать", "monetiz"),
            ("как удалить", "delete"),
            ("что такое", "about"),
            ("как пожаловаться", "report"),
        ]
        # возьмём первые подходящие URL из siteMap
        found_urls = []
        if sitemaps:
            # попробуем вытащить несколько ссылок (без парсинга всего sitemap)
            for sm in sitemaps[:2]:
                txt, sc, _ = await fetch_text(client, sm)
                if txt and sc < 400:
                    # простенький парсинг ссылок
                    urls = re.findall(r">([^<]+)</loc>|<loc>([^<]+)</loc>", txt)
                    for a, b in urls:
                        link = a or b
                        if link and link not in found_urls:
                            found_urls.append(link)
                            if len(found_urls) > 200:
                                break

        def pick_url(keyword: str) -> Optional[str]:
            for u in found_urls:
                if keyword in u.lower():
                    return u
            return None

        for intent_text, kw in intents:
            url = pick_url(kw) or base_url
            map_sec.append(AuditItem(intent_text.capitalize(), "ok" if url else "warn",
                                     f"Страница/секция: {short(url)}",
                                     "Уточнить контент под intent и добавить JSON-LD."))
        sections["Prompt → Page Map"] = map_sec

        # ======== Оценки и To-Do ========
        # Бальная система: ok=2, warn=1, fail=0; только по основной таблице
        score = 0
        max_score = 2 * len(items)
        for it in items:
            score += 2 if it.status == "ok" else (1 if it.status == "warn" else 0)

        visibility = round(10 * score / max_score, 1) if max_score else 0.0
        seo_score = round(10 * score / max_score, 1)
        geo_score = round(10 * score / max_score, 1)

        # Top-5 TODO по важности: fail > warn и по фиксированным приоритетам
        def priority(it: AuditItem) -> Tuple[int, int]:
            p1 = 0 if it.status == "ok" else (1 if it.status == "warn" else 2)
            # лёгкий приоритет по имени
            name_weight = {
                "llms.txt / ai.txt": 3,
                "FAQ / HowTo / Glossary": 2,
                "Core Web Vitals": 2,
                "Anchors": 1,
                "Schema.org": 2,
                "Sitemap": 2,
                "KI-Crawling robots.txt": 2
            }.get(it.name, 0)
            return (p1, name_weight)

        sorted_todos = sorted([i for i in items if i.status != "ok"], key=priority, reverse=True)
        top5 = sorted_todos[:5]

        # ======== Формируем текст отчёта ========
        def badge(st: str) -> str:
            return {"ok": "✅", "warn": "🟡", "fail": "❌", "na": "➖"}.get(st, "•")

        lines: List[str] = []
        domain_disp = urlparse(base_url).netloc
        lines.append(f"<b>Аудит сайта: {domain_disp}</b>")
        if not (has_llms and has_ai):
            lines.append("Часть файлов (llms.txt / ai.txt) может отсутствовать — оценка по доступным данным.")
        lines.append("\n<b>Главная таблица</b>")
        lines.append("<i>Критерий — Статус — Наблюдение — To-Do</i>")
        for it in items:
            lines.append(
                f"{badge(it.status)} <b>{esc(it.name)}</b>\n"
                f"— <i>{short(esc(it.note), 220)}</i>\n"
                f"— To-Do: {short(esc(it.todo), 220)}\n"
            )


        # Разделы 
        for sec_name, sec_items in sections.items():
            lines.append(f"\n<b>{esc(sec_name)}</b>")
            for it in sec_items:
                lines.append(
                    f"{badge(it.status)} <b>{esc(it.name)}</b> — "
                    f"{short(esc(it.note), 200)} — "
                    f"To-Do: {short(esc(it.todo), 160)}"
                )


        # Оценки
        lines.append("\n<b>Оценки</b>")
        lines.append(f"• Visibility-Score: {visibility}/10")
        lines.append(f"• SEO-Score: {seo_score}/10")
        lines.append(f"• GEO-Score: {geo_score}/10")

        # Top-5 To-Dos
        lines.append("\n<b>Top-5 To-Dos (Impact → Effort)</b>")
        for idx, it in enumerate(top5, 1):
            lines.append(f"{idx}. {badge(it.status)} <b>{it.name}</b> — {it.todo}")

        # Резюме
        main_problem = next((i for i in items if i.status == "fail"), None)
        lines.append("\n<b>Резюме</b>")
        if main_problem:
            lines.append(f"1. Главная проблема: {main_problem.name.lower()} — {main_problem.note}.")
        else:
            lines.append("1. Критических проблем не найдено на уровне MVP-проверок.")
        lines.append("2. Ключевой приоритет на 14 дней: закрыть пункты из Top-5 To-Dos.")

        summary_text = "\n".join(lines)
        return summary_text, items, sections

# ================== PDF генерация ==================
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import os

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import os
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph, Table, TableStyle, Spacer

def make_pdf_bytes(title: str, table_items: List[AuditItem], sections: Dict[str, List[AuditItem]]) -> bytes:
    # регистрируем шрифт (как уже делали)




    font_path = os.path.join(os.path.dirname(__file__), "fonts", "DejaVuSans.ttf")
    if "DejaVuSans" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("DejaVuSans", font_path))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=14*mm, rightMargin=14*mm,
        topMargin=16*mm, bottomMargin=16*mm
    )

    styles = getSampleStyleSheet()
    # базовые стили — Unicode-шрифт
    styles["Normal"].fontName = "DejaVuSans"
    styles["Title"].fontName = "DejaVuSans"
    styles["Heading2"].fontName = "DejaVuSans"

    # стили для ячеек таблицы с переносами
    cell = ParagraphStyle(
        "cell", parent=styles["Normal"],
        fontName="DejaVuSans", fontSize=9, leading=12,
        wordWrap="CJK"   # переносит даже длинные слова/URL
    )
    cell_bold = ParagraphStyle(
        "cell_bold", parent=cell, fontName="DejaVuSans"
    )

# Класс

    from reportlab.platypus import Flowable

    class StatusBadge(Flowable):
        def __init__(self, status: str, width=22*mm, height=6*mm):
            super().__init__()
            self.status = status.lower()
            self.width = width
            self.height = height

        def draw(self):
            c = self.canv
            color_map = {
                "ok": colors.green,
                "warn": colors.orange,
                "fail": colors.red,
                "na": colors.gray,
            }
            label_map = {
                "ok": "OK",
                "warn": "WARN",
                "fail": "FAIL",
                "na": "N/A",
            }
            col = color_map.get(self.status, colors.gray)
            label = label_map.get(self.status, self.status.upper())

            r = 3  # радиус кружка (пиксели PDF)
            x = 2
            y = self.height / 2

            c.setFillColor(col)
            c.circle(x + r, y, r, stroke=0, fill=1)

            c.setFillColor(colors.black)
            c.setFont("DejaVuSans", 9)
            # Чуть сдвинем baseline, чтобы было по центру
            c.drawString(x + 2*r + 3, y - 3, label)

        def wrap(self, availWidth, availHeight):
            return (self.width, max(self.height, 10))  # минимальная высота строки

    def badge_flowable(status: str) -> Flowable:
        return StatusBadge(status)


# Класс

    flow = []
    flow.append(Paragraph(title, styles["Title"]))
    flow.append(Spacer(1, 6))

    def status_color(s: str):
        return {"ok": colors.green, "warn": colors.orange, "fail": colors.red, "na": colors.gray}.get(s, colors.black)

    # <<< ВАЖНО: суммарно 182 мм (вся доступная ширина)
    COL_WIDTHS = [40*mm, 22*mm, 60*mm, 60*mm]

    import html

    def p(text: str) -> Paragraph:
        safe = html.escape((text or "").strip())
        return Paragraph(safe, cell)

    def items_table(title_txt: str, arr: List[AuditItem]):
        flow.append(Spacer(1, 6))
        flow.append(Paragraph(title_txt, styles["Heading2"]))

        # шапка — тоже Paragraph (чтобы выравнивание/переносы были одинаковыми)
        data = [
            [Paragraph("Критерий", cell_bold),
             Paragraph("Статус", cell_bold),
             Paragraph("Наблюдение", cell_bold),
             Paragraph("To-Do", cell_bold)]
        ]

        for it in arr:
            data.append([
                p(it.name),
                badge_flowable(it.status),   # <-- значок вместо текста
                p(it.note),
                p(it.todo),
            ])

        t = Table(data, colWidths=COL_WIDTHS, repeatRows=1)  # repeatRows — повтор шапки на новой странице
        ts = TableStyle([
            ("FONTNAME", (0,0), (-1,-1), "DejaVuSans"),
            ("FONTSIZE", (0,0), (-1,-1), 9),
            ("LEADING", (0,0), (-1,-1), 12),

            ("BACKGROUND", (0,0), (-1,0), colors.whitesmoke),
            ("TEXTCOLOR", (0,0), (-1,0), colors.black),

            ("ALIGN", (0,0), (-1,-1), "LEFT"),
            ("VALIGN", (0,0), (-1,-1), "TOP"),

            ("LEFTPADDING", (0,0), (-1,-1), 4),
            ("RIGHTPADDING", (0,0), (-1,-1), 4),
            ("TOPPADDING", (0,0), (-1,-1), 4),
            ("BOTTOMPADDING", (0,0), (-1,-1), 4),

            ("GRID", (0,0), (-1,-1), 0.25, colors.lightgrey),
        ])

        # цвет колонки "Статус"
        for row_idx in range(1, len(data)):
            st = arr[row_idx-1].status
            ts.add("TEXTCOLOR", (1, row_idx), (1, row_idx), status_color(st))

        t.setStyle(ts)
        t.splitByRow = 1  # разрешить перенос строк таблицы на следующую страницу
        flow.append(t)

    items_table("Главная таблица", table_items)
    for sec_name, sec_items in sections.items():
        items_table(sec_name, sec_items)

    doc.build(flow)
    buf.seek(0)
    return buf.read()

# ================== Telegram handlers ==================
@dp.message(CommandStart())
async def on_start(message: types.Message):
    await message.answer(
        "<b>Пришли URL сайта — запущу аудит.</b>",
        reply_markup=None
    )

@dp.message(F.text)
async def on_url(message: types.Message):
    base = normalize_url(message.text or "")
    if not base:
        await message.answer("⚠️ Отправьте корректный URL, например: <code>https://example.com</code>")
        return

    await message.answer(f"🔍 Понял! Запускаю аудит для: <b>{base}</b>\nЭто займёт немного времени…")
    try:
        report_text, main_items, sections = await audit_site(base)

        # сохраняем текст и генерим PDF в память
        pdf_bytes = make_pdf_bytes(f"Аудит сайта: {urlparse(base).netloc}", main_items, sections)
        pdf_path = f"/tmp_report_{message.from_user.id}.pdf"  # временный путь (в рантайме)
        # сохранять на диск не обязательно — пошлём из bytes; но путь кладём для логики
        USER_REPORTS[message.from_user.id] = {"text": report_text, "pdf_path": pdf_path}

        # шлём репорт частями, если длинный
        chunks: List[str] = []
        cur = []
        cur_len = 0
        for line in report_text.split("\n"):
            if cur_len + len(line) + 1 > 3500:
                chunks.append("\n".join(cur))
                cur = []
                cur_len = 0
            cur.append(line)
            cur_len += len(line) + 1
        if cur:
            chunks.append("\n".join(cur))

        for idx, ch in enumerate(chunks):
            if idx == len(chunks) - 1:
                # добавим кнопку "Скачать PDF" только к последнему сообщению
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📄 Скачать PDF", callback_data="pdf")]
                ])
                await message.answer(ch, reply_markup=kb)
            else:
                await message.answer(ch)

        # Сразу кэшнём bytes в память у объекта сообщения (простая реализация)
        # В prod лучше хранить файл (S3/диск) и отдавать по запросу.
        # Пришлём как «файл» по callback
        # Сохраняем bytes на объект пользователя (глобальная переменная небезопасна — для MVP норм)
        USER_REPORTS[message.from_user.id]["pdf_bytes"] = pdf_bytes  # type: ignore

    except Exception as e:
        await message.answer(f"❌ Произошла ошибка при аудите: <code>{short(str(e), 200)}</code>")

@dp.callback_query(F.data == "pdf")
async def on_pdf(call: CallbackQuery):
    rep = USER_REPORTS.get(call.from_user.id)
    if not rep or "pdf_bytes" not in rep:
        await call.answer("Отчёт не найден. Отправьте URL ещё раз.", show_alert=True)
        return
    pdf_bytes = rep["pdf_bytes"]  # type: ignore
    filename = "site-audit.pdf"
    await call.message.answer_document(types.BufferedInputFile(pdf_bytes, filename=filename))
    await call.answer()

# ================== run ==================
async def main():
    print("Bot is running…")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
