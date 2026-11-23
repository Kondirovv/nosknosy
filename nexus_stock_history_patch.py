"""Nexus Stock 0.4 — read-only history page for the existing Render app.

Import this module as the Gunicorn entry point after the existing build step copies
'nexus_stock_app (3).py' to 'nexus_stock_app.py'.

No database credentials or Evotor tokens are embedded in this file.  The existing
NEXUS_STOCK_ADMIN_KEY from Render authorizes server-to-server history reads.
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from flask import redirect, render_template_string, request, url_for
import nexus_stock_app as original

app = original.app

_HISTORY_API = (
    "https://qmbtdcamxsoihloapajr.supabase.co/functions/v1/"
    "nexus-stock-history"
)

# Adds a navigation link to the current dashboard, without replacing any of
# its API routes, security middleware, session cookie or Evotor integration.
_old_nav = ('<div class="actions"><a class="btn alt" '
            'href="{{ url_for(\'dashboard\') }}">↻ Обновить</a>')
_new_nav = ('<div class="actions"><a class="btn alt" '
            'href="{{ url_for(\'history_page\') }}">▦ История</a>'
            '<a class="btn alt" href="{{ url_for(\'dashboard\') }}">↻ Обновить</a>')
if _old_nav in original._DASHBOARD_TEMPLATE:
    original._DASHBOARD_TEMPLATE = original._DASHBOARD_TEMPLATE.replace(
        _old_nav, _new_nav, 1
    )


_HISTORY_HTML = r'''<!doctype html>
<html lang="ru">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>История склада — Nexus Stock</title>
<style>
:root{font-family:system-ui,-apple-system,Segoe UI,Arial,sans-serif;color:#172338;background:#f3f5fb}
*{box-sizing:border-box}body{margin:0}header{background:#101d32;color:#fff;padding:17px 22px}
.wrap{max-width:1150px;margin:auto}.head{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}
.brand{font-size:20px;font-weight:800}.brand b{color:#f6a13d}.status{font-size:12px;border-radius:30px;padding:6px 11px;background:#e5f6ee;color:#13704e}
main{padding:25px 15px 65px}h1{font-size:clamp(23px,4vw,34px);margin:7px 0}h2{font-size:18px;margin:0 0 12px}
p{color:#65718a;line-height:1.5;font-size:13px}.eyebrow{letter-spacing:1px;text-transform:uppercase;font-size:11px;color:#808ba1}
a{text-decoration:none;color:#6246db}a:hover{text-decoration:underline}.btn{display:inline-block;padding:10px 14px;border:1px solid #d9dfea;background:#fff;color:#263855;border-radius:10px;font-size:13px;font-weight:700}
.top{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:15px;margin-bottom:18px}.actions{display:flex;flex-wrap:wrap;gap:8px}
.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:13px}.card,.panel{border:1px solid #e5eaf2;background:#fff;box-shadow:0 5px 22px #172b3c08;border-radius:15px;padding:20px}
.num{font-size:33px;font-weight:800;line-height:1.25;margin:6px 0}.red{color:#d94b52}.orange{color:#c48117}.label,.small{color:#8490a0;font-size:12px}
.cols{display:grid;grid-template-columns:1.1fr 1fr;gap:14px;margin-top:15px}.panel{margin-top:15px;overflow:hidden}
.tablebox{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}th{text-align:left;color:#657188;background:#f6f8fb;font-weight:650}td,th{padding:11px 12px;border-bottom:1px solid #e7ebf2}td:first-child{overflow-wrap:anywhere}td:last-child{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}th:last-child{text-align:right}
.bar{height:12px;background:#edf0f7;border-radius:9px;overflow:hidden;min-width:70px}.bar span{background:#eb665b;height:100%;display:block}
.alert{padding:13px 16px;border:1px solid #ffe0a4;background:#fff6dc;color:#755319;border-radius:11px;margin-bottom:14px;font-size:13px}.good{background:#e7f8ef;border-color:#c1eed4;color:#1c704c}
.empty{font-size:13px;color:#7a8797}.foot{margin-top:20px;color:#8390a0;font-size:12px}
@media(max-width:860px){.cards{grid-template-columns:repeat(2,minmax(0,1fr))}.cols{grid-template-columns:1fr}}
@media(max-width:500px){.card,.panel{padding:13px}.num{font-size:26px}.cards{gap:9px}td,th{padding:9px 7px}}
</style></head>
<body>
<header><div class="wrap head"><div class="brand">NEXUS <b>STOCK</b> <small> / 0.4</small></div><span class="status">● Только чтение · Эвотор</span></div></header>
<main class="wrap">
<div class="top">
 <div><div class="eyebrow">Нос к носу · архив учёта</div><h1>История склада</h1>
 <p>Последние {{ history|length }} дат · выбран {{ selected.day if selected else 'нет данных' }}</p></div>
 <div class="actions"><a class="btn" href="{{ url_for('dashboard') }}">← Текущий склад</a><a class="btn" href="{{ url_for('history_page') }}">Последний снимок</a></div>
</div>
{% if warning %}<div class="alert{% if not problem %} good{% endif %}">{{ warning }}</div>{% endif %}
{% if selected %}
<section class="cards">
 {% for title,value,delta,color in metrics %}
 <div class="card"><div class="label">{{ title }}</div><div class="num {{ color }}">{{ value }}</div><div class="small">К прошлому снимку: {{ delta }}</div></div>
 {% endfor %}
</section>
<div class="cols">
 <section class="panel"><h2>Проверки учёта</h2><div class="tablebox"><table><tbody>
 <tr><td>Розничная цена ниже закупочной</td><td>{{ selected.sale_below_cost_count }}</td></tr>
 <tr><td>Товары без кода</td><td>{{ selected.missing_code_count }}</td></tr>
 <tr><td>Неположительная цена продажи</td><td>{{ selected.nonpositive_sale_price_count }}</td></tr>
 <tr><td>Записи с повторяющимся названием</td><td>{{ selected.additional_same_name_count }}</td></tr>
 <tr><td>Страниц каталога</td><td>{{ selected.pages_loaded }}</td></tr>
 </tbody></table></div><p>Совпадение названий не означает, что карточки нужно объединить.</p></section>
 <section class="panel"><h2>Состояние автоматизации</h2><div class="tablebox"><table><tbody>
 <tr><td>Ежедневный запуск</td><td>{{ '09:15 МСК' if schedule.active else 'Выключен' }}</td></tr>
 <tr><td>Последний снимок</td><td>{{ history[0].day }}</td></tr>
 <tr><td>Последний запуск по расписанию</td><td>{{ last_run.status if last_run else 'Ещё не выполнялся' }}</td></tr>
 <tr><td>Снимок сформирован</td><td>{{ selected.completed_at_moscow }} МСК</td></tr>
 </tbody></table></div><p>Показатели — учётные данные. Не подтверждают фактическую недостачу.</p></section>
</div>
<section class="panel"><h2>Динамика по дням</h2><p>Нажми дату для просмотра снимка. График будет расти по мере ежедневного архивирования.</p>
<div class="tablebox"><table><thead><tr><th>Дата</th><th>Товаров</th><th>Отрицательные</th><th>Нулевая закупка</th><th>Динамика</th></tr></thead>
<tbody>{% for item in history %}<tr><td><a href="{{ url_for('history_page', day=item.day) }}">{{ item.day }}</a></td><td>{{ item.total_products }}</td><td class="red">{{ item.negative_stock_count }}</td><td>{{ item.zero_cost_price_count }}</td><td><div class="bar"><span style="width:{{ item.bar }}%"></span></div></td></tr>{% endfor %}</tbody></table></div>
</section>
<div class="cols">
 <section class="panel"><h2>Самые отрицательные остатки</h2><p>На {{ selected.day }}. Единицы измерения не смешиваются.</p>
 <div class="tablebox"><table><thead><tr><th>Товар</th><th>Код</th><th>Остаток</th></tr></thead>
 <tbody>{% for item in negative %}<tr><td>{{ item.name or 'Без названия' }}</td><td>{{ item.code or '—' }}</td><td class="red">{{ item.quantity }} {{ item.measure_name or '' }}</td></tr>{% endfor %}</tbody></table></div></section>
 <section class="panel"><h2>Изменения остатков</h2><p>{% if previous %}Сравнение с {{ previous.day }} (одинаковые единицы измерения){% else %}Появятся после второго снимка{% endif %}</p>
 <div class="tablebox"><table><thead><tr><th>Товар</th><th>Было</th><th>Стало</th></tr></thead>
 <tbody>{% for item in movements %}<tr><td>{{ item.name }}</td><td>{{ item.before }} {{ item.measure_name or '' }}</td><td>{{ item.after }} {{ item.measure_name or '' }}</td></tr>{% else %}<tr><td colspan="3" class="empty">Пока нет данных для сравнения</td></tr>{% endfor %}</tbody></table></div></section>
</div>
{% else %}<section class="panel"><h2>История пока пуста</h2><p>Она появится после первого успешного архивирования.</p></section>{% endif %}
<p class="foot">Nexus Stock 0.4 · Данные загружаются из защищённой истории Supabase · Изменений в кассу не вносится</p>
</main></body></html>'''


def _fetch_history(day):
    key = os.getenv("NEXUS_STOCK_ADMIN_KEY")
    if not key:
        raise RuntimeError("missing_key")
    url = _HISTORY_API
    if day:
        url += "?" + urllib.parse.urlencode({"day": day})
    req = urllib.request.Request(
        url,
        headers={"X-Nexus-Key": key, "Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=24) as res:
        result = json.load(res)
    if not isinstance(result, dict) or result.get("version") != "0.4":
        raise ValueError("invalid_history_response")
    return result


def _delta(current, previous, field):
    if not previous:
        return "—"
    a = int(current.get(field) or 0)
    b = int(previous.get(field) or 0)
    result = a - b
    return ("+" if result > 0 else "") + str(result)


@app.get("/history")
def history_page():
    if not original._is_logged_in():
        return redirect(url_for("dashboard_login"))
    day = request.args.get("day", "")
    if day and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return "Неверная дата", 400
    try:
        data = _fetch_history(day)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return ("Ключ Render и ключ истории Supabase не совпадают. "
                    "Проверь NEXUS_STOCK_ADMIN_KEY и nexus_stock_history_admin_key."), 502
        return "Не удалось получить историю Supabase (HTTP error).", 502
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, RuntimeError):
        return "История временно недоступна. Попробуй обновить страницу.", 502

    history = data.get("history") or []
    selected = data.get("selected")
    previous = data.get("previous")
    maximum = max([int(p.get("negative_stock_count") or 0) for p in history] + [1])
    for p in history:
        p["bar"] = min(100, 100 * int(p.get("negative_stock_count") or 0) / maximum)
    metrics = []
    for label, name, color in [
        ("Всего товаров", "total_products", ""),
        ("Отрицательные остатки", "negative_stock_count", "red"),
        ("Нулевая закупочная цена", "zero_cost_price_count", "orange"),
        ("Обе проблемы", "both_issues_count", ""),
    ]:
        if selected:
            metrics.append((label, selected.get(name, 0), _delta(selected, previous, name), color))
    schedule = data.get("schedule") or {}
    last_run = data.get("last_run")
    problem = False
    if not schedule.get("active"):
        problem = True
        warning = "Внимание: ежедневное архивирование выключено."
    elif history and (time.time() - int(history[0].get("completed_epoch") or 0) > 48 * 3600):
        problem = True
        warning = "Последний снимок старше 48 часов. Проверь расписание и журналы Supabase."
    elif last_run and last_run.get("status") not in ("succeeded", "running", "starting"):
        problem = True
        warning = "Последний запуск задания завершился с ошибкой. Проверь Supabase → Cron."
    else:
        warning = "Архивирование активно. Последний сохранённый снимок доступен."

    return render_template_string(
        _HISTORY_HTML,
        history=history, selected=selected, previous=previous,
        negative=data.get("negative_examples") or [],
        movements=data.get("movements") or [],
        metrics=metrics, schedule=schedule, last_run=last_run,
        problem=problem, warning=warning,
    )
