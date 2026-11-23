"""Nexus Stock 0.6 — private inventory review workbench.

Start command: gunicorn --workers 1 --threads 2 --bind 0.0.0.0:$PORT nexus_stock_review_v06:app

Uses the existing Nexus Stock v0.5 app and login. The only POST in this module
writes human review notes to a separate private Supabase table. There are NO
Evotor writes, even when a review is marked 'manually fixed'.
"""

import hmac
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request

from flask import abort, redirect, render_template_string, request, session, url_for
import nexus_stock_catalog_v05 as v05

app = v05.app
API = 'https://qmbtdcamxsoihloapajr.supabase.co/functions/v1/nexus-stock-review'

FLAGS = {
    'negative': 'Отрицательный остаток',
    'zero_cost': 'Закупка 0 ₽',
    'missing_cost': 'Нет закупочной цены',
    'missing_code': 'Отсутствует код',
    'zero_sale': 'Цена продажи ≤ 0',
    'below_cost': 'Продажа ниже закупки',
    'high_weight': 'Проверить весовой остаток',
}
LEVELS = {'critical': 'Критический', 'high': 'Высокий', 'medium': 'Средний'}
STATUSES = {
    'unreviewed': 'Не разобрано',
    'needs_review': 'Требует исправления',
    'checked': 'Проверено',
    'manually_fixed': 'Исправлено вручную',
    'not_an_issue': 'Не ошибка',
}
ISSUE_FILTERS = [('all','Все замечания')] + list(FLAGS.items())
SEVERITY_FILTERS = [('all','Все приоритеты')] + list(LEVELS.items())
STATUS_FILTERS = [('all','Любой статус')] + list(STATUSES.items())

# Add a navigation link to the working v0.5 pages without changing their routes.
marker = '<a class="btn" href="{{ url_for(\'alerts_page\') }}">Проверки</a>'
if marker in v05.PAGE:
    v05._jinja.get_template('base.html')  # Keep existing loader and compile before replacing.
    v05.PAGE = v05.PAGE.replace(marker, marker + '<a class="btn" href="{{ url_for(\'review_page\') }}">Центр проверки</a>', 1)
    v05._jinja.loader.mapping['base.html'] = v05.PAGE
    v05._jinja.cache.clear()

original = v05.history.original
main_marker = '<a class="btn alt" href="{{ url_for(\'alerts_page\') }}">⚑ Проверки</a>'
if main_marker in original._DASHBOARD_TEMPLATE:
    original._DASHBOARD_TEMPLATE = original._DASHBOARD_TEMPLATE.replace(
        main_marker, main_marker + '<a class="btn alt" href="{{ url_for(\'review_page\') }}">✓ Проверка склада</a>',1)

history_marker = '<a class="btn" href="{{ url_for(\'alerts_page\') }}">⚑ Проверки</a>'
if history_marker in v05.history._HISTORY_HTML:
    v05.history._HISTORY_HTML = v05.history._HISTORY_HTML.replace(
        history_marker, history_marker + '<a class="btn" href="{{ url_for(\'review_page\') }}">✓ Проверка склада</a>',1)

CSS = '''
:root{font-family:system-ui,-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;color:#152743;background:#f3f5fb}
*{box-sizing:border-box}body{margin:0}header{background:#101e33;color:white;padding:16px 20px}.wrap{max-width:1180px;margin:auto}
.head,.top,.actions,.toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.brand{font-size:19px;font-weight:800}.brand b{color:#f9a541}.tag{font-size:11px;border-radius:20px;padding:6px 10px;background:#eaf8ef;color:#187245}
main{padding:25px 14px 65px}h1{font-size:clamp(24px,4vw,34px);margin:6px 0}h2{font-size:19px;margin:0 0 15px}h3{margin:8px 0;font-size:16px}
p,.muted{font-size:13px;color:#67768f;line-height:1.55}a{color:#5743c8;text-decoration:none}a:hover{text-decoration:underline}
.btn,button{background:#fff;color:#26364f;border:1px solid #d9dfeb;padding:10px 13px;border-radius:9px;font-size:13px;font-weight:700;cursor:pointer;text-decoration:none;display:inline-block}
button.primary,.btn.primary{background:#6645e3;color:white;border-color:#6645e3}
.card,.panel{border:1px solid #e1e7f1;background:#fff;border-radius:15px;padding:18px;margin-top:15px;box-shadow:0 2px 13px #18284a06}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.metric{font-size:27px;font-weight:800}.small{font-size:12px;color:#66758c}.red{color:#cd414b}.gold{color:#c27d11}.green{color:#218458}
.grid{display:grid;grid-template-columns:1.6fr 1fr 1fr 1fr auto;gap:8px;align-items:end}.grid label,.field label{font-size:12px;font-weight:700;color:#62708a;display:block;margin-bottom:5px}
input,select,textarea{width:100%;padding:10px 11px;border:1px solid #cfd8e9;border-radius:9px;font-size:14px;background:white;color:#223249;font-family:inherit}
.scroll{overflow:auto}table{width:100%;border-collapse:collapse;min-width:770px;font-size:13px}th,td{padding:11px 9px;vertical-align:top;border-bottom:1px solid #edf0f5;text-align:left}th{background:#f6f7fc;color:#5d6c85;font-size:12px}td.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.bubble{display:inline-block;border-radius:7px;padding:4px 7px;font-size:11px;margin:2px 2px 2px 0;background:#eef1f6;color:#5a6882}
.bubble.critical{background:#ffe7e9;color:#bd3343}.bubble.high{background:#fff1d5;color:#9c6713}.bubble.medium{background:#ebf1ff;color:#3558a2}
.pager{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;margin-top:12px}.warning{padding:13px 15px;border-radius:10px;background:#fff2dc;color:#815a23;margin-top:14px;font-size:13px}
.notice{padding:12px 15px;border-radius:10px;background:#eaf8f1;color:#247251;margin-top:14px;font-size:13px}.cols{display:grid;grid-template-columns:1fr 1fr;gap:15px}
.kv{display:flex;justify-content:space-between;gap:10px;border-bottom:1px solid #eef1f7;padding:11px 0;font-size:14px}.kv span{color:#6f7c91}.kv strong{text-align:right}
.radio{display:flex;flex-wrap:wrap;gap:10px}.field{margin-bottom:16px}.radio label{display:inline-flex;gap:5px;align-items:center;font-size:13px}.radio input{width:auto}
@media(max-width:920px){.grid{grid-template-columns:1fr 1fr}.cards{grid-template-columns:repeat(2,1fr)}.cols{grid-template-columns:1fr}}
@media(max-width:520px){.grid{grid-template-columns:1fr}.cards{grid-template-columns:repeat(2,1fr)}.card,.panel{padding:12px}.metric{font-size:24px}}
'''

LAYOUT = '''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{ title }} | Nexus Stock</title><style>''' + CSS + '''</style></head>
<body><header><div class="wrap head"><div class="brand">NEXUS <b>STOCK</b> / 0.6</div><span class="tag">Эвотор: только чтение</span></div></header>
<main class="wrap"><div class="top"><div><p class="muted">НОС К НОСУ · КОНТРОЛЬ КАЧЕСТВА УЧЁТА</p><h1>{{ title }}</h1><p>{{ intro }}</p></div>
<div class="actions"><a class="btn" href="{{ url_for('dashboard') }}">Склад</a><a class="btn" href="{{ url_for('catalog_page') }}">Каталог</a><a class="btn" href="{{ url_for('history_page') }}">История</a><a class="btn" href="{{ url_for('review_page') }}">Центр проверки</a></div></div>
{{ body|safe }}<p class="muted" style="margin-top:22px">Nexus Stock 0.6 · Отметки хранятся только в Supabase. Ни одна отметка не меняет товары, остатки или цены Эвотор.</p></main></body></html>'''

LIST = '''<div class="warning">Это предварительные замечания по учётным данным. Например, вес более 100 кг — повод проверить единицы измерения, а не доказанная ошибка. Отметка «Исправлено вручную» не меняет Эвотор.</div>
<div class="cards">
{% for caption,n,color in summary_cards %}<div class="card"><div class="small">{{ caption }}</div><div class="metric {{ color }}">{{ n }}</div></div>{% endfor %}
</div>
<div class="panel"><h2>Очередь проверки</h2><form class="grid" method="get" action="{{ url_for('review_page') }}">
<div><label>Название, код или артикул</label><input name="q" maxlength="100" value="{{ q }}" placeholder="Например, трахея, 548"></div>
<div><label>Причина</label><select name="issue">{% for value,label in issues %}<option value="{{ value }}" {% if value==issue %}selected{% endif %}>{{ label }}</option>{% endfor %}</select></div>
<div><label>Приоритет</label><select name="severity">{% for value,label in severities %}<option value="{{ value }}" {% if value==severity %}selected{% endif %}>{{ label }}</option>{% endfor %}</select></div>
<div><label>Статус проверки</label><select name="status">{% for value,label in statuses %}<option value="{{ value }}" {% if value==status %}selected{% endif %}>{{ label }}</option>{% endfor %}</select></div>
<div><button class="primary" type="submit">Найти</button></div></form></div>
<div class="panel"><div class="toolbar"><h2>Проблемные позиции</h2><span class="muted">Снимок {{ day }} · найдено {{ total }} · показано до 40 на странице</span></div>
{% if items %}<div class="scroll"><table><thead><tr><th>Товар</th><th>Код</th><th>Причины</th><th>Приоритет</th><th>Статус</th><th class="num">Остаток</th></tr></thead><tbody>
{% for p in items %}<tr><td><a href="{{ url_for('review_item', product_id=p.product_id) }}"><strong>{{ p.name or 'Без названия' }}</strong></a></td><td>{{ p.code or '—' }}</td>
<td>{% for flag in p.flags %}<span class="bubble">{{ flags[flag] }}</span>{% endfor %}</td><td><span class="bubble {{ p.severity }}">{{ levels[p.severity] }}</span></td>
<td>{{ status_names[p.review_status] }}</td><td class="num {% if p.quantity is not none and p.quantity < 0 %}red{% endif %}">{{ p.qty_text }} {{ p.measure_name or '' }}</td></tr>{% endfor %}
</tbody></table></div>{% else %}<p>По выбранным условиям позиций нет.</p>{% endif %}
<div class="pager"><div>{% if page>1 %}<a class="btn" href="{{ url_for('review_page',q=q,issue=issue,severity=severity,status=status,page=page-1) }}">← Назад</a>{% endif %}</div><div>Страница {{ page }}</div><div>{% if page*40<total %}<a class="btn" href="{{ url_for('review_page',q=q,issue=issue,severity=severity,status=status,page=page+1) }}">Дальше →</a>{% endif %}</div></div>
</div>'''

DETAIL = '''<div class="actions"><a href="{{ url_for('review_page') }}" class="btn">← Очередь проверки</a><a class="btn" href="{{ url_for('catalog_product',product_id=p.product_id) }}">Полная карточка</a></div>
{% if saved %}<div class="notice">Отметка сохранена в Nexus Stock. Данные Эвотор не менялись.</div>{% endif %}
<div class="cols"><section class="panel"><h2>{{ p.name }}</h2>
<div class="kv"><span>Код</span><strong>{{ p.code or '—' }}</strong></div>
<div class="kv"><span>Приоритет</span><strong>{{ levels[p.severity] }}</strong></div>
<div class="kv"><span>Остаток</span><strong>{{ qty }} {{ p.measure_name or '' }}</strong></div>
<div class="kv"><span>Закупочная цена</span><strong>{{ cost }}</strong></div>
<div class="kv"><span>Продажа</span><strong>{{ sale }}</strong></div>
<p>Причины проверки:</p>{% for flag in p.flags %}<span class="bubble">{{ flags[flag] }}</span>{% endfor %}
<p>Проверь реальные остатки, единицы измерения и приходные документы перед исправлением учёта.</p>
</section>
<section class="panel"><h2>Результат проверки</h2>
<form method="post" action="{{ url_for('review_item',product_id=p.product_id) }}">
<input type="hidden" name="csrf" value="{{ csrf }}">
<div class="field"><label for="review_status">Внутренний статус</label>
<select id="review_status" name="review_status" required>{% for value,label in status_choices %}<option value="{{ value }}" {% if p.review_status==value %}selected{% endif %}>{{ label }}</option>{% endfor %}</select></div>
<div class="field"><label for="invoice_ref">Приходная накладная / номер документа (необязательно)</label><input id="invoice_ref" name="invoice_ref" maxlength="120" value="{{ p.invoice_ref or '' }}" placeholder="Например: приход 18.11.2026"></div>
<div class="field"><label for="note">Комментарий по сверке</label><textarea id="note" name="note" maxlength="800" rows="5" placeholder="Что проверено, что исправлено вручную, на каких документах основано">{{ p.note or '' }}</textarea></div>
<button type="submit" class="primary">Сохранить отметку</button></form>
<div class="warning">Статусы «Проверено» и «Исправлено вручную» записываются только в этот журнал. Самостоятельного изменения в Эвотор нет.</div>
</section></div>'''


def _authenticated():
    return v05.history.original._is_logged_in()


def _api(params=None, payload=None):
    key = os.getenv('NEXUS_STOCK_ADMIN_KEY', '')
    if not key:
        raise RuntimeError('Missing server admin key')
    url = API + ('?' + urllib.parse.urlencode(params) if params else '')
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
    headers = {'X-Nexus-Key': key, 'Accept': 'application/json'}
    if body is not None:
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, headers=headers, data=body, method='POST' if body else 'GET')
    with urllib.request.urlopen(req, timeout=28) as res:
        data = json.load(res)
    if not isinstance(data, dict) or data.get('version') != '0.6':
        raise ValueError('Unexpected API response')
    return data


def _csrf():
    if not session.get('nexus_review_csrf'):
        session['nexus_review_csrf'] = secrets.token_urlsafe(32)
    return session['nexus_review_csrf']


def _show(title, intro, body, **ctx):
    rendered = render_template_string(body, **ctx)
    return render_template_string(LAYOUT, title=title, intro=intro, body=rendered)


def _failure(exc):
    if isinstance(exc, urllib.error.HTTPError) and exc.code == 401:
        return 'Ключ Render не совпадает с ключом Supabase.', 502
    if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
        return 'Товар не найден в снимке.', 404
    return 'Не удалось загрузить результаты проверки. Попробуй обновить страницу.', 502


@app.get('/review')
def review_page():
    if not _authenticated():
        return redirect(url_for('dashboard_login'))
    q = request.args.get('q', '').strip()
    issue = request.args.get('issue', 'all')
    severity = request.args.get('severity', 'all')
    status = request.args.get('status', 'all')
    raw_page = request.args.get('page', '1')
    page = min(1000, max(1, int(raw_page))) if raw_page.isdecimal() else 1
    if (len(q) > 100 or issue not in dict(ISSUE_FILTERS) or
        severity not in dict(SEVERITY_FILTERS) or status not in dict(STATUS_FILTERS)):
        abort(400)
    try:
        data = _api({'q':q,'issue':issue,'severity':severity,'status':status,'page':page})
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError, RuntimeError, TimeoutError) as e:
        return _failure(e)
    summ = data.get('summary') or {}
    cards = [
        ('Позиций для проверки', summ.get('all',0), ''),
        ('Критический приоритет',summ.get('critical',0),'red'),
        ('Высокий приоритет',summ.get('high',0),'gold'),
        ('Без отметки',summ.get('unreviewed',0),''),
    ]
    items = data.get('items') or []
    for item in items:
        item['qty_text'] = v05._number(item.get('quantity'))
    return _show('Центр проверки склада','Приоритетные замечания и ручные отметки о сверке. Сортировка по важности.',LIST,
        summary_cards=cards,items=items,total=int(data.get('total') or 0),day=data.get('snapshot_day') or '',
        q=q,issue=issue,severity=severity,status=status,page=page,issues=ISSUE_FILTERS,
        severities=SEVERITY_FILTERS,statuses=STATUS_FILTERS,flags=FLAGS,levels=LEVELS,status_names=STATUSES)


@app.route('/review/<product_id>', methods=['GET','POST'])
def review_item(product_id):
    if not _authenticated():
        return redirect(url_for('dashboard_login'))
    if not product_id or len(product_id) > 300:
        abort(400)
    if request.method == 'POST':
        csrf = request.form.get('csrf','')
        if not csrf or not hmac.compare_digest(csrf,session.get('nexus_review_csrf','')):
            abort(403)
        status=request.form.get('review_status','')
        note=request.form.get('note','')
        invoice=request.form.get('invoice_ref','')
        if status not in STATUSES or status == 'unreviewed' or len(note)>800 or len(invoice)>120:
            abort(400)
        try:
            _api(payload={'product_id':product_id,'review_status':status,'note':note,'invoice_ref':invoice})
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError, RuntimeError, TimeoutError) as e:
            return _failure(e)
        return redirect(url_for('review_item', product_id=product_id, saved='1'))
    try:
        data=_api({'id':product_id})
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError, RuntimeError, TimeoutError) as e:
        return _failure(e)
    p=data.get('item') or {}
    if not p:
        abort(404)
    return _show('Сверка товара','Учётная информация из последнего снимка Эвотор.',DETAIL,
        p=p,flags=FLAGS,levels=LEVELS,status_choices=[(x,v) for x,v in STATUSES.items() if x!='unreviewed'],
        csrf=_csrf(),saved=request.args.get('saved')=='1',qty=v05._number(p.get('quantity')),
        cost=v05._price(p.get('cost_price')),sale=v05._price(p.get('sale_price')))
