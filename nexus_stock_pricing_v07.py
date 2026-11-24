"""Nexus Stock 0.7 - safe markups and UПД preview, no Evotor writes.

Deploy alongside nexus_stock_review_v06.py. Render Start Command:
  gunicorn --workers 1 --threads 2 --bind 0.0.0.0:$PORT nexus_stock_pricing_v07:app

The existing Nexus Stock 0.6 remains unchanged. This add-on registers two
read-only workflows: a price calculator and a local-in-request XML UПД preview.
Uploaded XML is not stored, emailed, sent to third parties, or forwarded to Evotor.
"""

from __future__ import annotations

import io
import json
import os
import urllib.request
from collections import defaultdict
import re
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP
from xml.etree import ElementTree as ET

from flask import Response, abort, redirect, request, url_for
import nexus_stock_review_v06 as v06

app = v06.app

# Category rules approved for "Нос к носу" on 23 November 2026.
# All prices use supplier's VAT-inclusive purchase cost; tax on sales is NOT inferred.
CATEGORY_RULES = (
    ('feed', 'Фасованные сухие и влажные корма', 80),
    ('loose_feed', 'Корма на развес, мешки 6–12 кг (за 1 кг)', 90),
    ('natural_treats', 'Натуральные лакомства', 200),
    ('factory_treats', 'Фабричные лакомства', 100),
    ('toys', 'Игрушки', 180),
    ('fillers', 'Наполнители', 250),
    ('litter_boxes', 'Туалеты и лотки', 250),
    ('leashes', 'Ошейники, поводки, шлейки', 200),
    ('bowls', 'Миски и аксессуары', 200),
    ('care', 'Гигиена и уход', 200),
    ('supplements', 'Кормовые добавки и ветеринарные средства', 35),
    ('other', 'Прочие товары', 300),
)
RATES = {slug: Decimal(rate) for slug, _name, rate in CATEGORY_RULES}
MAX_XML_BYTES = 2 * 1024 * 1024
MAX_ROWS = 250


def money(value: Decimal) -> str:
    return f'{value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP):,.2f}'.replace(',', ' ').replace('.', ',')


def retail(cost: Decimal, category: str, bag_weight_kg: Decimal | None = None) -> Decimal:
    if cost <= 0 or cost > Decimal('10000000') or category not in RATES:
        raise ValueError('Неверная закупка или категория')
    if category == 'loose_feed':
        if bag_weight_kg is None or not Decimal('0.01') <= bag_weight_kg <= Decimal('1000'):
            raise ValueError('Для развесного корма нужен вес мешка в кг')
        cost = cost / bag_weight_kg
    result = cost * (Decimal(1) + RATES[category] / Decimal(100))
    return (result / Decimal(10)).to_integral_value(rounding=ROUND_CEILING) * Decimal(10)


def _local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]


def _first(element: ET.Element, tag: str) -> ET.Element | None:
    return next((e for e in element.iter() if _local(e.tag) == tag), None)


def _decimal(value: str | None) -> Decimal | None:
    if not value:
        return None
    try:
        obj = Decimal(str(value).replace(',', '.'))
        return obj if obj.is_finite() else None
    except InvalidOperation:
        return None


def guess_category(name: str) -> str:
    """Suggestions only. A human must review before using the prices."""
    n = name.casefold()
    if any(s in n for s in ('стоп-цистит', 'ветпрепарат', 'ветеринарн', 'кормовая добавка', 'витамин')):
        return 'supplements'
    if any(s in n for s in ('подгузник', 'лапомойк', 'шампун', 'антигадин', 'спрей', 'уход за')):
        return 'care'
    if any(s in n for s in ('когтеточк', 'лежанк', 'переноск', 'клетка', 'домик для')):
        return 'other'
    if any(s in n for s in ('совок', 'лоток', 'туалет для кошек')):
        return 'litter_boxes'
    if any(s in n for s in ('наполнитель', 'силикагель', 'комкующ')):
        return 'fillers'
    if any(s in n for s in ('игрушк', 'мяч для собак', 'удочка для кошек')):
        return 'toys'
    if any(s in n for s in ('ошейник', 'поводок', 'шлейка')):
        return 'leashes'
    if any(s in n for s in ('миска', 'поилка', 'кормушк')):
        return 'bowls'
    if 'на развес' in n or 'развесной корм' in n:
        return 'loose_feed'
    if any(s in n for s in ('лакомств', 'лаком-во', 'лак-во', 'churu bee', 'chateau dog', 'шато дог')):
        return 'factory_treats'
    if any(s in n for s in ('трахея', 'рубец', 'бычий корень', 'сушёное лёгкое', 'сушеное легкое')):
        return 'natural_treats'
    if any(s in n for s in ('корм', 'пауч', 'консервы для собак', 'консервы для кошек')):
        return 'feed'
    return ''


def _parse_xml(raw: bytes) -> dict:
    if not raw or len(raw) > MAX_XML_BYTES:
        raise ValueError('Размер XML не должен превышать 2 МБ')
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('XML с DTD/ENTITY не поддерживается')
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, ValueError):
        # Some EDI exports mislabel UTF-8 as Windows-1251 or vice versa.
        text = None
        for encoding in ('utf-8-sig', 'cp1251'):
            try:
                text = raw.decode(encoding)
                text = re.sub(r'^\s*<\?xml\s+[^>]*\?>', '', text, count=1)
                root = ET.fromstring(text)
                break
            except (UnicodeDecodeError, ET.ParseError):
                text = None
        if text is None:
            raise ValueError('Не удалось прочитать XML в поддерживаемой кодировке')
    if _local(root.tag) != 'Файл':
        raise ValueError('Это не XML-файл УПД ФНС')
    doc = _first(root, 'Документ')
    invoice = _first(root, 'СвСчФакт')
    table = _first(root, 'ТаблСчФакт')
    if doc is None or invoice is None or table is None:
        raise ValueError('В XML не найдены сведения УПД')
    if doc.attrib.get('Функция') not in ('СЧФДОП', 'ДОП'):
        raise ValueError('Поддерживаются УПД с функцией СЧФДОП или ДОП')
    seller = ''
    seller_el = _first(invoice, 'СвПрод')
    if seller_el is not None:
        legal = _first(seller_el, 'СвЮЛУч')
        if legal is None:
            legal = _first(seller_el, 'СвИП')
        if legal is not None:
            seller = legal.attrib.get('НаимОрг', '')
    rows = []
    for line in table.iter():
        if _local(line.tag) != 'СведТов':
            continue
        if len(rows) >= MAX_ROWS:
            raise ValueError('Документ содержит слишком много строк (максимум 250)')
        name = line.attrib.get('НаимТов', '').strip()
        qty = _decimal(line.attrib.get('КолТов'))
        gross_total = _decimal(line.attrib.get('СтТовУчНал'))
        if not name or qty is None or qty <= 0 or qty > 1000000 or gross_total is None or gross_total <= 0 or gross_total > 100000000:
            raise ValueError(f'В строке {len(rows)+1} нет корректного названия, количества или суммы с НДС')
        extra = _first(line, 'ДопСведТов')
        gtin = extra.attrib.get('ГТИН', '') if extra is not None else ''
        supplier_code = extra.attrib.get('КодТов', '') if extra is not None else ''
        marks = _first(line, 'НомСредИдентТов')
        declared_marks = marks.attrib.get('КолВедМарк', '') if marks is not None else ''
        category = guess_category(name)
        unit_cost = gross_total / qty
        proposed = None
        if category and category != 'loose_feed':
            proposed = retail(unit_cost, category)
        rows.append({
            'index': line.attrib.get('НомСтр', str(len(rows)+1)),
            'name': name, 'qty': str(qty), 'qty_label': str(qty.normalize()),
            'unit': line.attrib.get('НаимЕдИзм', 'шт'),
            'gross': str(gross_total), 'gross_label': money(gross_total),
            'cost': str(unit_cost), 'cost_label': money(unit_cost),
            'gtin': gtin, 'supplier_code': supplier_code,
            'declared_marks': declared_marks,
            'category': category, 'retail': str(proposed) if proposed is not None else '',
        })
    if not rows:
        raise ValueError('В УПД нет товарных строк')
    totals = _first(table, 'ВсегоОпл')
    declared = _decimal(totals.attrib.get('СтТовУчНалВсего')) if totals is not None else None
    calculated = sum(Decimal(x['gross']) for x in rows)
    warning = ''
    if declared is not None and abs(calculated - declared) > Decimal('0.02'):
        warning = 'Сумма строк отличается от итога документа. Проверь УПД.'
    gtins = [x['gtin'] for x in rows if x['gtin']]
    if len(gtins) != len(set(gtins)):
        warning += ' Есть повторяющиеся GTIN в строках УПД.'
    return {
        'number': invoice.attrib.get('НомерДок', ''),
        'date': invoice.attrib.get('ДатаДок', ''),
        'seller': seller,
        'rows': rows,
        'line_total': money(calculated),
        'declared_total': money(declared) if declared is not None else '—',
        'warning': warning.strip(),
    }


# Add visible links without altering the installed v0.6 routes.
# This patch runs once when Gunicorn imports the addon module.
_LINKS = '<a class="btn" href="{{ url_for(\'pricing_page\') }}">Наценки</a><a class="btn" href="{{ url_for(\'invoice_page\') }}">УПД / расчёт</a>'
if _LINKS not in v06.LAYOUT:
    v06.LAYOUT = v06.LAYOUT.replace(
        '</div></div>\n{{ body|safe }}', _LINKS + '</div></div>\n{{ body|safe }}', 1,
    ).replace('NEXUS <b>STOCK</b> / 0.6', 'NEXUS <b>STOCK</b> / 0.7')

# Expose the new tools in existing catalogue, dashboard and history menus.
# This changes only in-memory presentation of templates, not Evotor data.
v05 = getattr(v06, 'v05', None)
if v05 is not None:
    nav = '<a class="btn" href="{{ url_for(\'pricing_page\') }}">Наценки</a><a class="btn" href="{{ url_for(\'invoice_page\') }}">УПД / расчёт</a>'
    marker = '<a class="btn" href="{{ url_for(\'review_page\') }}">Центр проверки</a>'
    if nav not in v05.PAGE and marker in v05.PAGE:
        v05.PAGE = v05.PAGE.replace(marker, marker + nav, 1)
        v05._jinja.loader.mapping['base.html'] = v05.PAGE
        v05._jinja.cache.clear()
    home = v05.history.original
    home_marker = '<a class="btn alt" href="{{ url_for(\'review_page\') }}">✓ Проверка склада</a>'
    home_nav = '<a class="btn alt" href="{{ url_for(\'pricing_page\') }}">Наценки</a><a class="btn alt" href="{{ url_for(\'invoice_page\') }}">УПД</a>'
    if home_nav not in home._DASHBOARD_TEMPLATE and home_marker in home._DASHBOARD_TEMPLATE:
        home._DASHBOARD_TEMPLATE = home._DASHBOARD_TEMPLATE.replace(home_marker, home_marker+home_nav, 1)
    history_marker = '<a class="btn" href="{{ url_for(\'review_page\') }}">✓ Проверка склада</a>'
    if nav not in v05.history._HISTORY_HTML and history_marker in v05.history._HISTORY_HTML:
        v05.history._HISTORY_HTML = v05.history._HISTORY_HTML.replace(history_marker, history_marker+nav, 1)


_PRICING = '''
<div class="panel"><h2>Проверка розничной цены</h2>
<form method="get" action="{{ url_for('pricing_page') }}" class="grid">
<div><label>Категория</label><select name="cat">{% for key,label,percent in rules %}<option value="{{key}}" {% if cat==key %}selected{% endif %}>{{label}} · {{percent}}%</option>{% endfor %}</select></div>
<div><label>Закупка с НДС, ₽</label><input name="cost" type="number" step="0.01" min="0.01" max="10000000" value="{{cost}}" required></div>
<div><label>Вес мешка, кг (для развеса)</label><input name="weight" type="number" step="0.01" min="0.01" value="{{weight}}" placeholder="Например: 10"></div>
<div><label>Округление</label><input value="Вверх до 10 ₽" readonly></div>
<div><button type="submit" class="primary">Рассчитать</button></div>
</form>
{% if error %}<div class="warning">{{error}}</div>{% endif %}
{% if result is not none %}<div class="notice"><strong>Розничная цена: {{result}} ₽{% if cat=='loose_feed' %} за 1 кг{% endif %}</strong><br>Наценка {{rate}}%; цена за отдельный отвес рассчитывается пропорционально весу.</div>{% endif %}
</div>
<div class="panel"><h2>Утверждённые наценки</h2><div class="scroll"><table style="min-width:520px"><thead><tr><th>Категория</th><th class="num">Наценка</th></tr></thead><tbody>
{% for key,label,percent in rules %}<tr><td>{{label}}</td><td class="num">+{{percent}}%</td></tr>{% endfor %}
</tbody></table></div><p>Расчёт от закупочной цены с НДС. Результат округляется вверх до 10 ₽. Неоднозначную категорию выбирает человек. Изменений в Эвотор нет.</p>
<a class="btn primary" href="{{ url_for('invoice_page') }}">Проверить XML УПД</a></div>
'''

_INVOICE = '''
<div class="warning">Предварительный расчёт, не документ приёмки. XML не сохраняется на сервере. Категории определяются ориентировочно — проверь их перед применением цен. Для маркированного товара код GTIN не заменяет сверку индивидуальных Data Matrix в ЭДО.</div>
<div class="panel"><h2>Загрузить XML УПД</h2>
<form method="post" enctype="multipart/form-data" action="{{ url_for('invoice_page') }}">
<input type="hidden" name="csrf" value="{{csrf}}">
<div class="field"><label>Оригинальный XML УПД от поставщика, не более 2 МБ</label><input name="invoice" type="file" accept=".xml,text/xml,application/xml" required></div>
<button class="primary" type="submit">Проверить цены (без приёмки)</button>
</form><p>PDF, фото и Excel пока передавай в ChatGPT для расчёта: эта форма обрабатывает только XML УПД.</p></div>
{% if error %}<div class="warning">{{error}}</div>{% endif %}
{% if parsed %}
<div class="panel"><h2>УПД № {{parsed.number}} от {{parsed.date}}</h2>
<p>Поставщик: {{parsed.seller or 'Не указан'}} · Позиции: {{parsed.rows|length}} · сумма по строкам: {{parsed.line_total}} ₽ · итог УПД: {{parsed.declared_total}} ₽</p>
{% if parsed.warning %}<div class="warning">{{parsed.warning}}</div>{% endif %}
{% if catalog_error %}<div class="notice">{{ catalog_error }}</div>{% endif %}
<div class="scroll"><table id="invoice-table" style="min-width:1150px"><thead><tr><th>№</th><th>Товар и артикул</th><th>GTIN и маркировка</th><th>Каталог Эвотора</th><th class="num">Кол-во</th><th class="num">Закупка за шт., ₽</th><th>Категория</th><th>Вес мешка, кг</th><th class="num">Розница, ₽</th><th class="num">Стоимость по рознице, ₽</th></tr></thead><tbody>
{% for r in parsed.rows %}<tr data-name="{{r.name}}" data-qty="{{r.qty}}" data-cost="{{r.cost}}" data-gtin="{{r.gtin}}" data-code="{{r.supplier_code}}">
<td>{{r.index}}</td><td><strong>{{r.name}}</strong><br><span class="muted">Артикул {{r.supplier_code or '—'}}</span></td>
<td>{{r.gtin or 'Не указан'}}<br><span class="muted">Маркируемых единиц в УПД: {{r.declared_marks or 'нет данных'}}</span></td>
<td>{{ r.catalog_status or 'Сверка недоступна' }}{% if r.catalog_name %}<br>{{ r.catalog_name }}{% endif %}
{% if r.compare_price %}<br>{% if r.catalog_price is not none %}Цена: {{ r.catalog_price }} ₽
<span class="catalog-delta" data-price="{{ r.catalog_price }}"></span>{% else %}Цена отсутствует{% endif %}{% endif %}</td>
<td class="num">{{r.qty_label}} {{r.unit}}</td><td class="num">{{r.cost_label}}</td>
<td><select class="row-category"><option value="">Выбрать категорию</option>{% for key,label,percent in rules %}<option value="{{key}}" {% if key==r.category %}selected{% endif %}>{{label}} (+{{percent}}%)</option>{% endfor %}</select></td>
<td><input class="row-weight" type="number" step="0.01" min="0.01" placeholder="Если развес"></td><td class="num"><strong class="row-price">{{r.retail or '—'}}</strong></td><td class="num"><strong class="row-total">—</strong></td>
</tr>{% endfor %}</tbody></table></div>
<div class="notice" id="invoice-summary">Проверь категории и розничные цены. Не применяй непроверенные данные в кассе.</div>
<button class="primary" type="button" id="export-csv">Скачать CSV для проверки</button>
<p>Скачиваемая таблица — только предложение цен; не загружай её напрямую в Эвотор как поступление. Для развесных кормов сумма считается по весу, если он введён.</p>
</div>
<script src="{{ url_for('invoice_script') }}"></script>
{% endif %}
'''


_INVOICE_SCRIPT = r'''
(function(){
const rates={"feed":80,"loose_feed":90,"natural_treats":200,"factory_treats":100,"toys":180,"fillers":250,"litter_boxes":250,"leashes":200,"bowls":200,"care":200,"supplements":35,"other":300};
const trs=Array.from(document.querySelectorAll('#invoice-table tbody tr'));
const escCSV=(v)=>{const t=String(v??'').replace(/[\r\n]+/g,' ');const safe=/^\s*[=+\-@]/.test(t)?'\''+t:t;return '"'+safe.replaceAll('"','""')+'"';};
function calc(){let sum=0,ready=0;
 for(const tr of trs){
  const cost=Number(tr.dataset.cost),qty=Number(tr.dataset.qty);
  const key=tr.querySelector('.row-category').value;
  const weight=Number(tr.querySelector('.row-weight').value);
  const delta=tr.querySelector('.catalog-delta');
  if(delta) delta.textContent='';
  const priceCell=tr.querySelector('.row-price');const totalCell=tr.querySelector('.row-total');
  const base=key==='loose_feed'? (weight>0?cost/weight:NaN):cost;
  if(!key||!Number.isFinite(base)||base<=0){priceCell.textContent='—';totalCell.textContent='—';continue;}
  const price=Math.ceil((base*(1+rates[key]/100)-1e-8)/10)*10;
  if(!Number.isFinite(price)||price<=0){priceCell.textContent='—';totalCell.textContent='—';continue;}
  priceCell.textContent=String(price);
  if(delta && key!=='loose_feed'){
    const difference=price-Number(delta.dataset.price);
    delta.textContent=Math.abs(difference)<0.005?' · Совпадает':' · План − каталог: '+difference.toFixed(2)+' ₽';
  }
  const total=key==='loose_feed'?price*qty*weight:price*qty;
  totalCell.textContent=Number.isFinite(total)?total.toFixed(2):'—';
  if(Number.isFinite(total)){sum+=total;ready++;}
 }
 const node=document.getElementById('invoice-summary');
 node.textContent='Цены рассчитаны: '+ready+' из '+trs.length+' строк. Потенциальная розничная сумма: '+sum.toFixed(2)+' ₽. Для неопределённых товаров выбери категорию вручную.';
}
for(const tr of trs) for(const field of tr.querySelectorAll('select,input')) field.addEventListener('change',calc);
calc();
document.getElementById('export-csv').addEventListener('click',function(){
 const head=['Товар','Артикул поставщика','GTIN','Количество','Закупка с НДС за ед.','Категория','Вес мешка кг','Розница','Расчетная сумма','Статус'];
 const all=[head];for(const tr of trs){const category=tr.querySelector('.row-category');const price=tr.querySelector('.row-price').textContent;
 all.push([tr.dataset.name,tr.dataset.code,tr.dataset.gtin,tr.dataset.qty,tr.dataset.cost,category.selectedOptions[0]?.textContent||'',tr.querySelector('.row-weight').value,price,tr.querySelector('.row-total').textContent,price==='—'?'Проверить':'Предварительно']);}
 const csv='\ufeff'+all.map(a=>a.map(escCSV).join(';')).join('\r\n');
 const u=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8;'}));
 const a=document.createElement('a');a.href=u;a.download='nexus_stock_invoice_price_preview.csv';a.click();URL.revokeObjectURL(u);
});
})();
'''


@app.get('/invoice-v07.js')
def invoice_script():
    if not v06._authenticated():
        abort(403)
    return Response(_INVOICE_SCRIPT, mimetype='application/javascript')


# Keep the site's strict CSP; allow only our same-origin, external script on /invoice.
# Flask executes after_request functions in reverse registration order.
def _invoice_csp(response):
    if request.path == '/invoice' and response.mimetype == 'text/html':
        policy = response.headers.get('Content-Security-Policy', '')
        if "script-src" not in policy:
            response.headers['Content-Security-Policy'] = policy + "; script-src 'self'"
    return response

app.after_request_funcs.setdefault(None, []).insert(0, _invoice_csp)


@app.get('/pricing')
def pricing_page():
    if not v06._authenticated():
        return redirect(url_for('dashboard_login'))
    cat = request.args.get('cat', 'feed')
    if cat not in RATES:
        abort(400)
    cost, weight = request.args.get('cost', ''), request.args.get('weight', '')
    result = error = None
    if cost:
        try:
            parsed = _decimal(cost)
            bag = _decimal(weight)
            result = money(retail(parsed, cat, bag)) if parsed is not None else None
            if result is None:
                raise ValueError('Укажи корректную цену')
        except (ValueError, TypeError, ArithmeticError) as exc:
            error = str(exc)
    return v06._show('Расчёт наценок', 'Постоянные правила «Нос к носу». Ничего не меняем в Эвотор.', _PRICING,
                     rules=CATEGORY_RULES, cat=cat, cost=cost, weight=weight,
                     rate=int(RATES[cat]), result=result, error=error)


def _catalog_key(value):
    return ' '.join(str(value or '').casefold().split())


def _gtin_key(value):
    value = str(value or '').strip()
    if len(value) not in (8, 12, 13, 14) or not value.isascii() or not value.isdigit():
        return ''
    checksum = sum(int(digit) * (3 if i % 2 else 1)
                   for i, digit in enumerate(reversed(value)))
    return value.zfill(14) if checksum % 10 == 0 else ''


def _match_catalog(rows, products):
    indexes = {field: defaultdict(dict) for field in ('barcode', 'code', 'name')}
    for product in products:
        for field, values in (
            ('barcode', map(_gtin_key, product['barcodes'])),
            ('code', map(_catalog_key, (product['code'], product['article']))),
            ('name', [_catalog_key(product['name'])]),
        ):
            for value in filter(None, values):
                indexes[field][value][product['id']] = product

    for row in rows:
        gtin = _gtin_key(row['gtin'])
        candidates = indexes['barcode'].get(gtin) or {}
        method = 'GTIN'
        if not candidates:
            candidates = indexes['code'].get(_catalog_key(row['supplier_code'])) or {}
            method = 'артикул или код'
        if not candidates:
            candidates = indexes['name'].get(_catalog_key(row['name'])) or {}
            method = 'название'
        if len(candidates) > 1:
            row['catalog_status'] = f'Несколько карточек ({len(candidates)}), поиск по полю «{method}»'
        elif candidates:
            product = next(iter(candidates.values()))
            row['catalog_name'] = product['name']
            row['catalog_status'] = 'Совпадение по GTIN' if method == 'GTIN' else f'Проверьте карточку вручную: {method}'
            if method == 'GTIN' and _catalog_key(row['unit']) == _catalog_key(product['measure']):
                row['catalog_price'] = product['sale']
                row['compare_price'] = True
                if product['sale'] is not None and product['sale'] <= 0:
                    row['catalog_status'] += ' · Цена продажи ≤ 0 ₽'
            elif method == 'GTIN':
                row['catalog_status'] += ' · Единицы измерения различаются'
        else:
            row['catalog_status'] = 'Карточка не найдена' if gtin else 'Карточка не найдена · Нет корректного GTIN'


def _live_catalog():
    key = os.getenv('NEXUS_STOCK_ADMIN_KEY')
    if not key:
        raise ValueError('Missing catalogue key')
    req = urllib.request.Request(
        'https://qmbtdcamxsoihloapajr.supabase.co/functions/v1/nexus-stock-invoice-catalog',
        headers={'X-Nexus-Key': key, 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=20) as response:
        data = json.load(response)
    if data.get('version') != 1 or not isinstance(data.get('items'), list):
        raise ValueError('Unexpected catalogue response')
    return data['items']


@app.route('/invoice', methods=['GET', 'POST'])
def invoice_page():
    if not v06._authenticated():
        return redirect(url_for('dashboard_login'))
    parsed = error = catalog_error = None
    if request.method == 'POST':
        if request.content_length is not None and request.content_length > MAX_XML_BYTES + 512_000:
            abort(413)
        csrf = request.form.get('csrf', '')
        if not csrf or csrf != v06._csrf():
            abort(403)
        file = request.files.get('invoice')
        if not file or not file.filename.lower().endswith('.xml'):
            error = 'Выбери оригинальный XML УПД (файлы PDF здесь пока не обрабатываются).'
        else:
            raw = file.stream.read(MAX_XML_BYTES + 1)
            try:
                parsed = _parse_xml(raw)
            except ValueError as exc:
                error = str(exc)
    if parsed:
        try:
            _match_catalog(parsed['rows'], _live_catalog())
        except (OSError, ValueError, TypeError, KeyError):
            catalog_error = 'Каталог Эвотора сейчас недоступен. Расчёт УПД готов, сверка карточек не выполнена.'
    return v06._show('УПД и расчёт цен', 'Загрузи XML для предварительной сверки и назначения наценок.', _INVOICE,
                     csrf=v06._csrf(), parsed=parsed, error=error, catalog_error=catalog_error, rules=CATEGORY_RULES,
                     rates={k: int(rate) for k, rate in RATES.items()})

