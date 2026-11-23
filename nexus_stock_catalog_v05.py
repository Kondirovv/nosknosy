"""Nexus Stock 0.5: protected catalogue search, product history and alerts.

Add to GitHub Kondirovv/nosknosy, alongside nexus_stock_history_patch.py.
Start command after upload:
 gunicorn --workers 1 --threads 2 --bind 0.0.0.0:$PORT nexus_stock_catalog_v05:app

Uses the existing Flask login session and NEXUS_STOCK_ADMIN_KEY.
Only sends GET requests to the protected read-only Supabase history API.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from flask import abort, redirect, render_template_string, request, url_for
import nexus_stock_history_patch as history

app = history.app
API_URL = "https://qmbtdcamxsoihloapajr.supabase.co/functions/v1/nexus-stock-catalog"
FILTERS = [
    ("all", "Все товары"),
    ("negative", "Отрицательные остатки"),
    ("zero_cost", "Нулевая закупочная цена"),
    ("missing_cost", "Не указана закупка"),
    ("missing_code", "Нет кода товара"),
    ("below_cost", "Продажа ниже закупки"),
    ("zero_sale", "Нулевая / отрицательная цена продажи"),
]
VALID_FILTERS = {value for value, _ in FILTERS}

# Extend the original navigation without replacing the currently working dashboard.
original = history.original
_dashboard_marker = '<div class="actions"><a class="btn alt" href="{{ url_for(\'history_page\') }}">▦ История</a>'
if _dashboard_marker in original._DASHBOARD_TEMPLATE:
    original._DASHBOARD_TEMPLATE = original._DASHBOARD_TEMPLATE.replace(
        _dashboard_marker,
        '<div class="actions"><a class="btn alt" href="{{ url_for(\'catalog_page\') }}">⌕ Каталог</a>'
        '<a class="btn alt" href="{{ url_for(\'alerts_page\') }}">⚑ Проверки</a>'
        '<a class="btn alt" href="{{ url_for(\'history_page\') }}">▦ История</a>',
        1,
    )
_history_marker = '<div class="actions"><a class="btn" href="{{ url_for(\'dashboard\') }}">← Текущий склад</a>'
if _history_marker in history._HISTORY_HTML:
    history._HISTORY_HTML = history._HISTORY_HTML.replace(
        _history_marker,
        '<div class="actions"><a class="btn" href="{{ url_for(\'catalog_page\') }}">⌕ Каталог</a>'
        '<a class="btn" href="{{ url_for(\'alerts_page\') }}">⚑ Проверки</a>'
        '<a class="btn" href="{{ url_for(\'dashboard\') }}">← Текущий склад</a>',
        1,
    )

PAGE = r'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }} · Nexus Stock</title>
<style>
:root{font-family:system-ui,-apple-system,Segoe UI,Arial,sans-serif;color:#192844;background:#f3f5fb}
*{box-sizing:border-box}body{margin:0}header{background:#101d32;color:#fff;padding:17px 20px}.wrap{max-width:1220px;margin:auto}.head,.top,.barrow{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.brand{font-size:20px;font-weight:850}.brand b{color:#f4a33a}.status{font-size:11px;background:#e5f6eb;border-radius:50px;color:#146c4c;padding:7px 11px}
main{padding:28px 16px 70px}h1{font-size:clamp(24px,4vw,34px);margin:3px 0}h2{font-size:18px;margin:0 0 14px}h3{font-size:16px;margin:0 0 8px}p{font-size:13px;color:#62738b;line-height:1.5}small,.muted{font-size:12px;color:#7d899b}
.actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.btn,button{display:inline-block;border:1px solid #d5dbeb;border-radius:10px;background:#fff;color:#293953;padding:10px 13px;font-size:13px;font-weight:700;text-decoration:none;cursor:pointer}.btn.main,button.main{background:#6749e5;border-color:#6749e5;color:#fff}a:not(.btn){color:#5940ce;text-decoration:none}a:hover{text-decoration:underline}
.panel,.metric{border-radius:15px;border:1px solid #e3e8f2;background:#fff;box-shadow:0 4px 20px rgba(17,29,57,.04)}.panel{padding:20px;margin-top:16px;overflow:hidden}.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-top:16px}.metric{padding:17px}.metric .n{font-size:28px;font-weight:850;margin:5px 0;color:#22304d}.metric .k{font-size:12px;color:#79869b}
.filters{display:grid;grid-template-columns:minmax(180px,2fr) minmax(175px,1fr) minmax(150px,1fr) auto;gap:10px;align-items:end}.filters label{display:block;font-size:12px;color:#51627e;font-weight:700;margin-bottom:6px}.filters input,.filters select{height:42px;min-width:0;width:100%;font-family:inherit;border:1px solid #cfd7e6;background:#fff;border-radius:9px;padding:8px 11px;font-size:14px}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;min-width:720px;font-size:13px}td,th{padding:12px 10px;border-bottom:1px solid #e7ebf3;text-align:left;vertical-align:top}th{background:#f5f7fb;font-size:12px;color:#6b7890;font-weight:700}td.num,th.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}td.name{min-width:220px;max-width:430px;overflow-wrap:anywhere}.bad{color:#d44b5c;font-weight:700}.warn{color:#bd7718;font-weight:700}.ok{color:#25825b;font-weight:700}
.tag{display:inline-block;font-size:11px;padding:4px 7px;border-radius:7px;margin:2px 3px 2px 0;background:#eef2f8;color:#617084}.tag.bad{background:#ffeff0;color:#bd3545}.tag.warn{background:#fff3d9;color:#a46a18}.tag.ok{background:#e7f7ee;color:#217448}
.notice{border-radius:12px;background:#e8f7f0;color:#246d50;border:1px solid #cbeddc;padding:13px 15px;margin:16px 0;font-size:13px}.notice.warning{background:#fff5df;color:#76501e;border-color:#ffe4a3}.notice.error{background:#ffeded;color:#ae323f;border-color:#f4c8c9}
.cols{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:15px}.kv{display:flex;justify-content:space-between;gap:14px;border-bottom:1px solid #edf0f4;padding:10px 1px;font-size:14px}.kv span{color:#65728a}.kv strong{text-align:right}.paginator{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;margin-top:15px}.alert-item{padding:12px 0;border-bottom:1px solid #edf0f4}.alert-item:last-child{border:0}.alert-item .title{font-size:14px;font-weight:750}
@media(max-width:900px){.filters{grid-template-columns:1fr 1fr}.metrics{grid-template-columns:1fr 1fr}.cols{grid-template-columns:1fr}}
@media(max-width:510px){.filters{grid-template-columns:1fr}.metrics{gap:8px}.metric{padding:12px}.metric .n{font-size:22px}.panel{padding:14px}main{padding:18px 12px 40px}}
</style></head><body>
<header><div class="wrap head"><div class="brand">NEXUS <b>STOCK</b> <small style="color:#adbcd4">/ 0.5</small></div><span class="status">● Только чтение · Эвотор</span></div></header>
<main class="wrap">
<div class="top"><div><p class="muted" style="margin:0">НОС К НОСУ · СКЛАДСКОЙ УЧЁТ</p><h1>{{ title }}</h1><p>{{ subtitle }}</p></div>
<div class="actions"><a class="btn" href="{{ url_for('dashboard') }}">Обзор</a><a class="btn" href="{{ url_for('history_page') }}">История</a><a class="btn" href="{{ url_for('catalog_page') }}">Поиск товаров</a><a class="btn" href="{{ url_for('alerts_page') }}">Проверки</a></div></div>
{% block content %}{% endblock %}
<div class="muted" style="margin-top:22px">Nexus Stock 0.5 · Учётные данные Эвотор, полученные на дату снимка. Изменение цен и остатков отключено.</div>
</main></body></html>'''

CATALOG = r'''{% extends page %}{% block content %}
<section class="panel"><form class="filters" method="get" action="{{ url_for('catalog_page') }}">
<div><label for="q">Товар, код или артикул</label><input id="q" type="search" name="q" value="{{ q }}" placeholder="Например: трахея, Пеленки, 1113" maxlength="100"></div>
<div><label for="issue">Проверка</label><select id="issue" name="issue">{% for code,label in filters %}<option value="{{ code }}"{% if code==issue %} selected{% endif %}>{{ label }}</option>{% endfor %}</select></div>
<div><label for="day">Снимок на дату</label><select id="day" name="day">{% for s in snapshots %}<option value="{{ s.day }}"{% if s.day==day %} selected{% endif %}>{{ s.day }}</option>{% endfor %}</select></div>
<div><button type="submit" class="main">Найти</button></div>
</form></section>
<div class="metrics"><div class="metric"><div class="k">Найдено товаров</div><div class="n">{{ total }}</div></div><div class="metric"><div class="k">Отрицательные остатки</div><div class="n bad">{{ selected.negative_stock_count }}</div></div><div class="metric"><div class="k">Нулевая закупка</div><div class="n warn">{{ selected.zero_cost_price_count }}</div></div><div class="metric"><div class="k">Дата снимка</div><div class="n" style="font-size:22px">{{ selected.day }}</div></div></div>
<section class="panel"><div class="barrow"><h2>Каталог товаров</h2><small>Страница {{ page_no }} · по {{ page_size }} товаров</small></div>
{% if items %}<div class="scroll"><table><thead><tr><th>Название</th><th>Код</th><th class="num">Остаток</th><th class="num">Закупка</th><th class="num">Продажа</th><th>Проверки</th></tr></thead><tbody>
{% for item in items %}<tr><td class="name"><a href="{{ url_for('catalog_product', product_id=item.product_id, day=day) }}"><strong>{{ item.name or 'Без названия' }}</strong></a></td><td>{{ item.code or '—' }}</td>
<td class="num{% if item.quantity is not none and item.quantity < 0 %} bad{% endif %}">{{ item.qty_text }} {{ item.measure_name or '' }}</td><td class="num">{{ item.cost_text }}</td><td class="num">{{ item.sale_text }}</td><td>{% for label,color in item.problem_tags %}<span class="tag {{ color }}">{{ label }}</span>{% else %}<span class="tag ok">Без замечаний</span>{% endfor %}</td></tr>{% endfor %}
</tbody></table></div>{% else %}<p>По заданным условиям товары не найдены. Измени фильтр или запрос.</p>{% endif %}
<div class="paginator"><div>{% if page_no > 1 %}<a class="btn" href="{{ url_for('catalog_page', q=q, issue=issue, day=day, page=page_no-1) }}">← Назад</a>{% endif %}</div><span class="muted">{{ first_row }}–{{ last_row }} из {{ total }}</span><div>{% if page_no * page_size < total %}<a class="btn" href="{{ url_for('catalog_page', q=q, issue=issue, day=day, page=page_no+1) }}">Следующая →</a>{% endif %}</div></div>
</section>{% endblock %}'''

PRODUCT = r'''{% extends page %}{% block content %}
<div class="actions"><a class="btn" href="{{ url_for('catalog_page', day=selected.day) }}">← Все товары</a><a class="btn" href="{{ url_for('history_page') }}">История склада</a></div>
<div class="cols"><section class="panel"><h2>Карточка товара</h2><h3>{{ item.name }}</h3>
<div class="kv"><span>Код</span><strong>{{ item.code or 'Не указан' }}</strong></div><div class="kv"><span>Артикул</span><strong>{{ item.article_number or 'Не указан' }}</strong></div><div class="kv"><span>Учетный остаток</span><strong class="{% if item.quantity is not none and item.quantity < 0 %}bad{% endif %}">{{ qty }} {{ item.measure_name or '' }}</strong></div>
<div class="kv"><span>Закупочная цена</span><strong>{{ cost }}</strong></div><div class="kv"><span>Розничная цена</span><strong>{{ sale }}</strong></div><div class="kv"><span>Дата снимка</span><strong>{{ selected.day }}</strong></div>
<p>Остаток указан в единицах Эвотор. Для весовых товаров проверь правильность единиц измерения.</p>
</section><section class="panel"><h2>Проверки этой позиции</h2>{% for label,color in problem_tags %}<div class="notice {% if color == 'bad' %}error{% elif color == 'warn' %}warning{% endif %}">{{ label }}</div>{% else %}<div class="notice">Сейчас в этой карточке не обнаружены типовые проблемы.</div>{% endfor %}
<p>Ни одна пометка не подтверждает фактическую недостачу. Сверяй учёт с приходными накладными.</p></section></div>
<section class="panel"><h2>История цен и остатков</h2>
<div class="scroll"><table><thead><tr><th>Дата</th><th class="num">Остаток</th><th class="num">Закупка</th><th class="num">Продажа</th><th>Изменение остатка</th></tr></thead><tbody>
{% for p in points %}<tr><td>{{ p.day }}</td><td class="num">{{ p.qty_text }} {{ p.measure_name or '' }}</td><td class="num">{{ p.cost_text }}</td><td class="num">{{ p.sale_text }}</td><td>{{ p.change_text }}</td></tr>{% endfor %}</tbody></table></div>
{% if points|length < 2 %}<p>Пока сохранён только один дневной снимок этой позиции. Изменения появятся после следующего успешного архивирования.</p>{% endif %}
</section>{% endblock %}'''

ALERTS = r'''{% extends page %}{% block content %}
{% if not schedule.active %}<div class="notice error">Ежедневное архивирование выключено. Проверь настройки Supabase Cron.</div>
{% elif stale %}<div class="notice warning">Последний сохранённый снимок старше 48 часов. Проверь функцию nexus-stock-daily-audit.</div>
{% elif failed %}<div class="notice warning">Последний запуск по расписанию завершился ошибкой. Проверь Supabase → Cron.</div>
{% else %}<div class="notice">Ежедневное архивирование включено, последний снимок доступен. Следующая проверка по расписанию в 09:15 МСК.</div>{% endif %}
<div class="metrics"><div class="metric"><div class="k">Дата последнего снимка</div><div class="n" style="font-size:21px">{{ selected.day }}</div></div><div class="metric"><div class="k">Отрицательные остатки</div><div class="n bad">{{ selected.negative_stock_count }}</div></div><div class="metric"><div class="k">Нулевые закупочные цены</div><div class="n warn">{{ selected.zero_cost_price_count }}</div></div><div class="metric"><div class="k">Статус расписания</div><div class="n" style="font-size:21px">{{ 'Активно' if schedule.active else 'Выключено' }}</div></div></div>
{% if not previous %}<div class="notice warning">Пока есть только один дневной снимок. Новые проблемы можно будет отличать от прежних после следующего успешного архивирования.</div>{% else %}
<div class="notice">Сравнение с {{ previous.day }}. Новыми считаются товары, у которых проблема отсутствовала в предыдущем снимке или карточки раньше не было.</div>{% endif %}
<div class="cols"><section class="panel"><h2>Новые отрицательные остатки</h2>{% for p in new_negative %}<div class="alert-item"><div class="title"><a href="{{ url_for('catalog_product', product_id=p.product_id, day=selected.day) }}">{{ p.name }}</a></div><small>Код {{ p.code or '—' }} · остаток {{ p.quantity }} · ранее {{ p.previous_quantity if p.previous_quantity is not none else 'не было данных' }}</small></div>{% else %}<p>{{ 'Пока нет второго снимка для сравнения.' if not previous else 'Новых отрицательных остатков среди проверенных записей не найдено.' }}</p>{% endfor %}</section>
<section class="panel"><h2>Новые нулевые закупочные цены</h2>{% for p in new_zero %}<div class="alert-item"><div class="title"><a href="{{ url_for('catalog_product', product_id=p.product_id, day=selected.day) }}">{{ p.name }}</a></div><small>Код {{ p.code or '—' }} · закупка 0 ₽</small></div>{% else %}<p>{{ 'Пока нет второго снимка для сравнения.' if not previous else 'Новых нулевых закупочных цен среди проверенных записей не найдено.' }}</p>{% endfor %}</section></div>
<section class="panel"><h2>Состояние архива</h2><div class="kv"><span>Ежедневная проверка</span><strong>{{ '09:15 МСК' if schedule.active else 'Неактивна' }}</strong></div><div class="kv"><span>Последний снимок</span><strong>{{ selected.day }}</strong></div><div class="kv"><span>Последний запуск Cron</span><strong>{{ last_run.status if last_run else 'Еще не был запущен по расписанию' }}</strong></div>
<p>Это уведомления внутри панели, а не сообщения в Telegram или на почту. Отправку наружу пока не включали.</p></section>{% endblock %}'''

# render_template_string chooses automatic HTML escaping for Jinja variables.
# It needs an in-memory Jinja template object for {% extends page %}.
from jinja2 import Environment, DictLoader
_jinja = Environment(loader=DictLoader({"base.html": PAGE, "catalog.html": CATALOG, "product.html": PRODUCT, "alerts.html": ALERTS}), autoescape=True)


def _render(template_name, **context):
    # The Flask environment's url_for is provided explicitly to the independent templates.
    return _jinja.get_template(template_name).render(page="base.html", url_for=url_for, **context)


def _fetch(params):
    key = os.getenv("NEXUS_STOCK_ADMIN_KEY", "")
    if not key:
        raise RuntimeError("admin_key_not_configured")
    url = API_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-Nexus-Key": key, "Accept": "application/json"}, method="GET")
    with urllib.request.urlopen(req, timeout=28) as response:
        data = json.load(response)
    if not isinstance(data, dict) or data.get("version") != "0.5":
        raise ValueError("unexpected_catalog_response")
    return data


def _authenticated():
    return history.original._is_logged_in()


def _handle_error(exc):
    if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
        return "Товар или снимок не найден.", 404
    if isinstance(exc, urllib.error.HTTPError) and exc.code == 401:
        return "Ключи истории Supabase и Render не совпадают. Проверь настройку администратора.", 502
    return "Сейчас не удалось получить данные из защищённого архива. Попробуй обновить страницу.", 502


def _number(v):
    if v is None:
        return "—"
    try:
        n = float(v)
        return (f"{n:,.3f}".rstrip("0").rstrip(".") if n % 1 else f"{n:,.0f}").replace(",", " ")
    except (ValueError, TypeError, OverflowError):
        return "—"


def _price(v):
    return "—" if v is None else _number(v) + " ₽"


def _problem_tags(p):
    tags = []
    qty, cost, sale = p.get("quantity"), p.get("cost_price"), p.get("sale_price")
    if qty is not None and qty < 0:
        tags.append(("Отрицательный остаток", "bad"))
    if cost == 0:
        tags.append(("Закупка 0 ₽", "warn"))
    if cost is None:
        tags.append(("Закупка не указана", "warn"))
    if not str(p.get("code") or "").strip():
        tags.append(("Нет кода", "warn"))
    if sale is not None and sale <= 0:
        tags.append(("Цена продажи ≤ 0", "warn"))
    if sale is not None and cost is not None and sale > 0 and cost > sale:
        tags.append(("Продажа ниже закупки", "bad"))
    return tags


def _decorated_product(item):
    obj = dict(item)
    obj["qty_text"] = _number(item.get("quantity"))
    obj["sale_text"] = _price(item.get("sale_price"))
    obj["cost_text"] = _price(item.get("cost_price"))
    obj["problem_tags"] = _problem_tags(item)
    return obj


@app.get("/catalog")
def catalog_page():
    if not _authenticated():
        return redirect(url_for("dashboard_login"))
    q = request.args.get("q", "").strip()
    if len(q) > 100:
        return "Поисковый запрос слишком длинный", 400
    issue = request.args.get("issue", "all")
    if issue not in VALID_FILTERS:
        return "Неизвестный фильтр", 400
    day = request.args.get("day", "")
    if day and (len(day) != 10 or day[4] != "-" or day[7] != "-"):
        return "Неверная дата", 400
    page_raw = request.args.get("page", "1")
    page = min(1000, max(1, int(page_raw))) if page_raw.isdecimal() else 1
    try:
        data = _fetch({"mode": "list", "q": q, "issue": issue, "page": page, **({"day": day} if day else {})})
        if not data.get("selected"):
            return "История пока пуста", 200
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError, RuntimeError, OSError) as exc:
        return _handle_error(exc)
    selected = data["selected"]
    for item in data.get("items") or []:
        item.update(_decorated_product(item))
    total = int(data.get("total") or 0)
    size = int(data.get("page_size") or 50)
    return _render("catalog.html", title="Поиск товаров", subtitle="Все карточки из сохранённого архива Эвотор, с фильтрами по проблемам.",
                   q=q, issue=issue, day=selected["day"], snapshots=data.get("snapshots") or [],
                   filters=FILTERS, items=data.get("items") or [], selected=selected,
                   total=total, page_no=page, page_size=size,
                   first_row=(page-1)*size+1 if total and data.get("items") else 0,
                   last_row=min(total, page*size))


@app.get("/product/<product_id>")
def catalog_product(product_id):
    if not _authenticated():
        return redirect(url_for("dashboard_login"))
    if not product_id or len(product_id) > 300:
        abort(400)
    day = request.args.get("day", "")
    try:
        data = _fetch({"mode": "detail", "id": product_id, **({"day": day} if day else {})})
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError, RuntimeError, OSError) as exc:
        return _handle_error(exc)
    item = data["product"]
    points = [dict(p) for p in data.get("points") or []]
    for i, p in enumerate(points):
        p["qty_text"] = _number(p.get("quantity"))
        p["cost_text"] = _price(p.get("cost_price"))
        p["sale_text"] = _price(p.get("sale_price"))
        if i+1 < len(points) and p.get("quantity") is not None and points[i+1].get("quantity") is not None and p.get("measure_name") == points[i+1].get("measure_name"):
            p["change_text"] = _number(p["quantity"]-points[i+1]["quantity"]) + " " + str(p.get("measure_name") or "")
        else:
            p["change_text"] = "—"
    return _render("product.html", title="Карточка товара", subtitle="История одной позиции без изменения данных кассы.",
                   item=item, selected=data["selected"], qty=_number(item.get("quantity")),
                   cost=_price(item.get("cost_price")), sale=_price(item.get("sale_price")),
                   problem_tags=_problem_tags(item), points=points)


@app.get("/alerts")
def alerts_page():
    if not _authenticated():
        return redirect(url_for("dashboard_login"))
    try:
        data = _fetch({"mode": "alerts"})
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError, RuntimeError, OSError) as exc:
        return _handle_error(exc)
    selected = data.get("selected")
    if not selected:
        return "История пока пуста", 200
    warnings = data.get("warnings") or {}
    job = data.get("schedule") or {}
    last_run = data.get("last_run")
    return _render("alerts.html", title="Проверки и уведомления", subtitle="Проблемы учёта и состояние ежедневного архивирования.",
                   selected=selected, previous=data.get("previous"), schedule=job, last_run=last_run,
                   stale=int(warnings.get("archive_age_hours") or 0)>48,
                   failed=bool(last_run and last_run.get("status") not in ("succeeded", "running", "starting")),
                   new_negative=data.get("new_negative_examples") or [], new_zero=data.get("new_zero_cost_examples") or [])
