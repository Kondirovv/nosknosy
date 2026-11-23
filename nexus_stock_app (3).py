"""Nexus Stock 0.3: read-only Evotor inventory API and secure dashboard for Render.

Environment:
  EVOTOR_TOKEN: secret bearer token created by Evotor.
  NEXUS_STOCK_ADMIN_KEY: independent random secret for private routes.

The application does not contain any API calls that create, modify, or delete
products, documents, stock quantities, or prices in Evotor.
"""

import csv
import hmac
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
import io
import os
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from decimal import Decimal, InvalidOperation
from flask import Flask, Response, jsonify, request, stream_with_context, redirect, render_template_string, session, url_for

app = Flask(__name__)
# The session cookie contains only a signed login flag, never an Evotor token.
_session_material = os.getenv("NEXUS_STOCK_ADMIN_KEY") or secrets.token_urlsafe(32)
app.secret_key = hashlib.sha256(("nexus-stock-dashboard-v03:" + _session_material).encode()).digest()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="Strict",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=4),
)

@app.after_request
def secure_headers(response):
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    )
    return response

API_BASE = "https://api.evotor.ru"
ACCEPT = "application/vnd.evotor.v2+json"


class UpstreamError(Exception):
    def __init__(self, kind: str, status: int | None = None):
        super().__init__(kind)
        self.kind = kind
        self.status = status


def _authorize():
    expected = os.getenv("NEXUS_STOCK_ADMIN_KEY", "")
    if not expected:
        return jsonify({"error": "admin_key_not_configured"}), 503
    supplied = request.headers.get("X-Nexus-Key", "")
    if not (hmac.compare_digest(supplied, expected) or session.get("nexus_stock_logged_in") is True):
        return jsonify({"error": "unauthorized"}), 401
    if not os.getenv("EVOTOR_TOKEN"):
        return jsonify({"error": "evotor_token_not_configured"}), 503
    return None


def _get(path, cursor=None):
    params = urllib.parse.urlencode({"cursor": cursor}) if cursor else ""
    url = API_BASE + path + ("?" + params if params else "")
    req = urllib.request.Request(url, method="GET", headers={
        "Authorization": "Bearer " + os.environ["EVOTOR_TOKEN"],
        "Accept": ACCEPT,
        "User-Agent": "NexusStock-ReadOnly/0.3",
    })
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            import json
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise UpstreamError("evotor_http_error", exc.code) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpstreamError("evotor_network_error") from exc
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        raise UpstreamError("unexpected_evotor_response")
    return result


def _pages(path, start_cursor=None, max_pages=100):
    cursor = start_cursor
    seen = set()
    for _ in range(max_pages):
        if cursor:
            if cursor in seen:
                raise UpstreamError("repeated_pagination_cursor")
            seen.add(cursor)
        page = _get(path, cursor)
        yield page
        cursor = (page.get("paging") or {}).get("next_cursor")
        if not cursor:
            return
    if cursor:
        raise UpstreamError("page_limit_reached")


def _store_id():
    shops = []
    for page in _pages("/stores", max_pages=20):
        shops.extend(page["items"])
    if len(shops) != 1:
        # Deliberately avoid auto-selecting when more than one store exists.
        raise UpstreamError("expected_exactly_one_store")
    store_id = shops[0].get("id")
    if not store_id:
        raise UpstreamError("store_id_missing")
    return urllib.parse.quote(str(store_id), safe="")


def _product_path():
    return "/stores/" + _store_id() + "/products"


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _safe_text(value):
    text = "" if value is None else str(value)
    # Neutralize formula injection in exported spreadsheet CSV text fields.
    if text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def _response(body, code=200):
    response = jsonify(body)
    response.status_code = code
    response.headers["Cache-Control"] = "no-store"
    return response


def _safe_run(fn):
    denied = _authorize()
    if denied is not None:
        return denied
    try:
        return _response(fn())
    except UpstreamError as exc:
        return _response({"error": exc.kind, "upstream_status": exc.status}, 502)
    except (KeyError, TypeError, ValueError):
        return _response({"error": "invalid_upstream_data"}, 502)


@app.get("/")
@app.get("/health")
def health():
    ready = bool(os.getenv("EVOTOR_TOKEN") and os.getenv("NEXUS_STOCK_ADMIN_KEY"))
    return _response({
        "service": "nexus-stock-readonly",
        "version": "0.3",
        "status": "ready" if ready else "needs_configuration",
        "writes_enabled": False,
    })


@app.get("/api/stores")
def stores():
    def read():
        shops = []
        for page in _pages("/stores", max_pages=20):
            shops.extend({"id": s.get("id"), "name": s.get("name")} for s in page["items"])
        return {"stores": shops}
    return _safe_run(read)


def _analyze_catalog():
    """Read the full catalogue; keep only summary and small issue samples in memory."""
    path = _product_path()
    count = pages = missing_cost = zero_cost = negative_stock = 0
    zero_sale_price = below_cost = both = missing_code = missing_name = 0
    unknown_quantity = 0
    issue_examples = []
    negative_examples = []
    zero_cost_examples = []
    names = Counter()
    for page in _pages(path, max_pages=100):
        pages += 1
        for item in page["items"]:
            count += 1
            name = str(item.get("name") or "").strip()
            if not name:
                missing_name += 1
            if not str(item.get("code") or "").strip():
                missing_code += 1
            if name:
                names[name.casefold()] += 1
            price = _number(item.get("price"))
            cost = _number(item.get("cost_price"))
            qty = _number(item.get("quantity"))
            reasons = []
            if cost is None:
                missing_cost += 1
            elif cost == 0:
                zero_cost += 1
                reasons.append("zero_cost_price")
            if qty is None:
                unknown_quantity += 1
            elif qty < 0:
                negative_stock += 1
                reasons.append("negative_stock")
                negative_examples.append({
                    "name": name[:160], "code": item.get("code"),
                    "quantity": str(qty),
                    "measure_name": str(item.get("measure_name") or "")[:40],
                })
            if price is not None and price <= 0:
                zero_sale_price += 1
                reasons.append("nonpositive_sale_price")
            if price is not None and cost is not None and price > 0 and cost > price:
                below_cost += 1
                reasons.append("sale_price_below_purchase_price")
            if cost == 0 and qty is not None and qty < 0:
                both += 1
            if cost == 0 and len(zero_cost_examples) < 25:
                zero_cost_examples.append({"name": name[:160], "code": item.get("code")})
            if reasons and len(issue_examples) < 30:
                issue_examples.append({
                    "name": name[:120], "code": item.get("code"),
                    "issues": reasons,
                })
    negative_examples.sort(key=lambda x: Decimal(x["quantity"]))
    return {
        "store_count": 1, "total_products": count, "pages_loaded": pages,
        "zero_cost_price_count": zero_cost,
        "missing_cost_price_count": missing_cost,
        "negative_stock_count": negative_stock,
        "nonpositive_sale_price_count": zero_sale_price,
        "extra_items_with_same_name": sum(n - 1 for n in names.values() if n > 1),
        "negative_and_zero_cost_count": both,
        "below_cost_price_count": below_cost,
        "missing_code_count": missing_code,
        "missing_name_count": missing_name,
        "unknown_quantity_count": unknown_quantity,
        "negative_examples": negative_examples[:30],
        "zero_cost_examples": zero_cost_examples,
        "issue_examples": issue_examples,
        "warning": "Repeated names are not proof of duplicate products. Check units before adjustments.",
        "mode": "read_only",
    }


@app.get("/api/catalog/summary")
def catalog_summary():
    """Read-only full catalog audit, authorized with header or dashboard session."""
    return _safe_run(_analyze_catalog)


@app.get("/api/catalog/export.csv")
def catalog_export():
    """Stream a read-only CSV snapshot to the authenticated client.

    The CSV is not stored on Render (free filesystems may be ephemeral).
    """
    denied = _authorize()
    if denied is not None:
        return denied
    try:
        path = _product_path()
    except UpstreamError as exc:
        return _response({"error": exc.kind, "upstream_status": exc.status}, 502)
    columns = ("id", "name", "code", "article_number", "type", "measure_name", "price", "cost_price", "quantity")

    def output():
        yield "\ufeff"  # Excel-friendly UTF-8 BOM.
        buff = io.StringIO()
        writer = csv.writer(buff)
        writer.writerow(columns)
        yield buff.getvalue()
        for page in _pages(path, max_pages=100):
            for item in page["items"]:
                buff = io.StringIO()
                writer = csv.writer(buff)
                writer.writerow([
                    _safe_text(item.get(col)) if col in {"id", "name", "code", "article_number", "type", "measure_name"}
                    else ("" if item.get(col) is None else item.get(col))
                    for col in columns
                ])
                yield buff.getvalue()

    response = Response(stream_with_context(output()), mimetype="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = 'attachment; filename="nexus_stock_evotor.csv"'
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# The dashboard uses the existing secret; no password or Evotor token is written to logs.
_DASHBOARD_TEMPLATE = '''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }} | Nexus Stock</title><style>
:root{color-scheme:light;--ink:#142034;--muted:#62708a;--paper:#f5f6fb;--line:#e4e8f0;--violet:#7345e3;--orange:#ff8d33}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.55 system-ui,-apple-system,"Segoe UI",Arial,sans-serif}
a{color:inherit;text-decoration:none}button,.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:42px;background:var(--violet);border:0;border-radius:11px;padding:10px 16px;color:white;font:600 14px system-ui;cursor:pointer}
.btn.alt{color:var(--ink);border:1px solid var(--line);background:white}.btn:focus-visible,button:focus-visible,a:focus-visible,input:focus-visible{outline:3px solid var(--orange);outline-offset:3px}
header{background:#111c32;color:white;padding:20px max(20px,calc((100vw - 1180px)/2));display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}
.brand{font-size:22px;font-weight:800;letter-spacing:-.5px}.brand b{color:#ff9a45}.eyebrow{font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#b3bbce}
.shell{max-width:1180px;margin:0 auto;padding:32px 20px 70px}.top{display:flex;align-items:end;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:25px}
h1{font-size:clamp(25px,4vw,38px);line-height:1.16;margin:5px 0}h2{font-size:19px;margin:0 0 12px}p{margin:6px 0;color:var(--muted)}.actions{display:flex;gap:10px;flex-wrap:wrap}
.status{display:inline-block;color:#287a58;background:#e3f8ec;border:1px solid #c0ebd2;border-radius:30px;padding:5px 11px;font-size:12px;font-weight:700}
.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px}.card,.panel{border:1px solid var(--line);background:white;box-shadow:0 6px 25px rgba(24,36,68,.035);border-radius:16px;padding:22px}
.num{font-size:38px;font-weight:800;letter-spacing:-1.5px;line-height:1.25}.bad{color:#d44050}.warn{color:#c27418}.label{font-size:13px;color:var(--muted)}.sub{font-size:12px;color:#8893a6}
.grid2{display:grid;grid-template-columns:1.4fr 1fr;gap:16px;margin-top:18px}.bar{background:#eef0f6;border-radius:30px;height:13px;overflow:hidden;margin:4px 0 15px}.fill{height:100%;background:linear-gradient(90deg,#ff9a45,#ef615f)}.fill.violet{background:linear-gradient(90deg,#ab8eff,#7345e3)}
.metric{display:flex;justify-content:space-between;font-size:13px;color:#48536b;gap:8px}.note{font-size:13px;line-height:1.5;color:#65708b}
.tablebox{overflow:auto;border:1px solid var(--line);border-radius:12px}table{border-collapse:collapse;width:100%;min-width:440px;font-size:13px}th{text-align:left;background:#f5f6fb;color:#66718a;font-weight:650}th,td{padding:11px 14px;border-bottom:1px solid var(--line)}tr:last-child td{border-bottom:0}td:first-child{max-width:420px;overflow-wrap:anywhere}td:nth-child(2){white-space:nowrap}td:last-child{text-align:right;font-variant-numeric:tabular-nums}
.small{font-size:12px;color:#8992a5}.foot{font-size:13px;color:#7c879a;margin-top:26px}.login{max-width:450px;margin:9vh auto}.login .panel{padding:32px}.login label{display:block;margin-top:18px;margin-bottom:6px;font-weight:600}.login input{width:100%;min-height:47px;border:1px solid #cbd2df;border-radius:10px;padding:10px 12px;font:16px system-ui}.login button{width:100%;margin-top:16px}.error{background:#fff1f0;color:#a92926;border:1px solid #ffd1c9;border-radius:10px;padding:10px 12px;margin-top:16px}
@media(max-width:850px){.cards{grid-template-columns:repeat(2,minmax(0,1fr))}.grid2{grid-template-columns:1fr}}
@media(max-width:510px){.shell{padding:22px 12px 45px}.cards{gap:10px}.card,.panel{padding:15px}.num{font-size:29px}header{padding:17px}.actions{width:100%}.actions .btn{flex:1}.label{font-size:12px}}
</style></head>
<body>
<header><div><div class="eyebrow">Нос к носу · учёт товаров</div><div class="brand">NEXUS <b>STOCK</b></div></div><div class="status">● Только чтение · Эвотор</div></header>
{% if login %}
<main class="shell login"><div class="panel"><h1>Вход в Nexus Stock</h1><p>Введите административный ключ из Render. Он передаётся только по HTTPS и не сохраняется в браузерном хранилище.</p>
{% if error %}<div role="alert" class="error">{{ error }}</div>{% endif %}
<form method="post" action="{{ url_for('dashboard_login') }}"><label for="pass">Административный ключ</label><input type="password" id="pass" name="key" autocomplete="off" required autofocus><button type="submit">Войти в панель</button></form>
<p class="small">Ключ: Render → nexus-stock-evotor → Environment → NEXUS_STOCK_ADMIN_KEY</p></div></main>
{% else %}
<main class="shell">
<div class="top"><div><div class="eyebrow">Складская аналитика</div><h1>Обзор каталога</h1><p>{% if data %}Данные получены из облака Эвотор · {{ checked_at }} МСК · {{ data.pages_loaded }} стр.{% else %}Обновление данных не удалось · {{ checked_at }} МСК{% endif %}</p></div>
<div class="actions"><a class="btn alt" href="{{ url_for('dashboard') }}">↻ Обновить</a><a class="btn" href="{{ url_for('catalog_export') }}">↓ Скачать CSV</a><form method="post" action="{{ url_for('dashboard_logout') }}"><button class="btn alt" type="submit">Выйти</button></form></div></div>
{% if error %}<div role="alert" class="error">{{ error }}</div>{% endif %}
{% if data %}
<section class="cards" aria-label="Ключевые показатели">
<div class="card"><div class="label">Всего товаров</div><div class="num">{{ data.total_products }}</div><div class="sub">{{ data.pages_loaded }} страницы API</div></div>
<div class="card"><div class="label">Отрицательные остатки</div><div class="num bad">{{ data.negative_stock_count }}</div><div class="sub">Учётные остатки ниже нуля</div></div>
<div class="card"><div class="label">Нулевая закупка</div><div class="num warn">{{ data.zero_cost_price_count }}</div><div class="sub">Цена закупки = 0</div></div>
<div class="card"><div class="label">Обе проблемы</div><div class="num">{{ data.negative_and_zero_cost_count }}</div><div class="sub">Требуют сверки первыми</div></div>
</section>
<div class="grid2"><section class="panel"><h2>Качество учёта</h2>
{% set total = data.total_products if data.total_products else 1 %}
<div class="metric"><span>Отрицательные остатки</span><strong>{{ data.negative_stock_count }} / {{ data.total_products }}</strong></div><div class="bar"><div class="fill" style="width:{{ (100 * data.negative_stock_count / total) | round(1) }}%"></div></div>
<div class="metric"><span>Нулевая закупочная цена</span><strong>{{ data.zero_cost_price_count }} / {{ data.total_products }}</strong></div><div class="bar"><div class="fill violet" style="width:{{ (100 * data.zero_cost_price_count / total) | round(1) }}%"></div></div>
<p class="note">Показатели могут пересекаться. Отрицательный учётный остаток не доказывает фактическую недостачу. Перед исправлениями нужно сверить единицы измерения, приходные накладные и продажи.</p></section>
<section class="panel"><h2>Другие проверки</h2>
<div class="metric"><span>Дополнительные строки с одинаковым названием</span><strong>{{ data.extra_items_with_same_name }}</strong></div><hr style="border:0;border-top:1px solid var(--line);margin:13px 0">
<div class="metric"><span>Продажная цена ниже закупочной</span><strong>{{ data.below_cost_price_count }}</strong></div><hr style="border:0;border-top:1px solid var(--line);margin:13px 0">
<div class="metric"><span>Товары без кода</span><strong>{{ data.missing_code_count }}</strong></div><hr style="border:0;border-top:1px solid var(--line);margin:13px 0">
<div class="metric"><span>Нулевая / отрицательная цена продажи</span><strong>{{ data.nonpositive_sale_price_count }}</strong></div><hr style="border:0;border-top:1px solid var(--line);margin:13px 0">
<div class="metric"><span>Закупочная цена отсутствует</span><strong>{{ data.missing_cost_price_count }}</strong></div>
</section></div>
<div class="grid2">
<section class="panel"><h2>Наибольшие отрицательные остатки</h2><p class="note">Первые {{ data.negative_examples|length }} позиций, отсортированы по значению остатка. Сравнивай товары только с одинаковыми единицами измерения.</p>
<div class="tablebox"><table><thead><tr><th>Товар</th><th>Код</th><th>Остаток</th></tr></thead><tbody>
{% for item in data.negative_examples %}<tr><td>{{ item.name or 'Без названия' }}</td><td>{{ item.code or '—' }}</td><td class="bad">{{ item.quantity }} {{ item.measure_name }}</td></tr>{% else %}<tr><td colspan="3">Отрицательных остатков нет</td></tr>{% endfor %}
</tbody></table></div></section>
<section class="panel"><h2>Примеры нулевой закупки</h2><p class="note">Первые {{ data.zero_cost_examples|length }} позиций, для которых стоимость закупки в API равна нулю.</p>
<div class="tablebox"><table><thead><tr><th>Товар</th><th>Код</th><th>Статус</th></tr></thead><tbody>
{% for item in data.zero_cost_examples %}<tr><td>{{ item.name or 'Без названия' }}</td><td>{{ item.code or '—' }}</td><td class="warn">0 ₽</td></tr>{% else %}<tr><td colspan="3">Нет таких товаров</td></tr>{% endfor %}
</tbody></table></div></section></div>
{% endif %}
<div class="foot">Nexus Stock 0.3 · Доступ только на чтение · Данные не изменяются и не сохраняются на сервере · CSV формируется по запросу</div>
</main>{% endif %}
</body></html>'''


def _is_logged_in():
    return session.get("nexus_stock_logged_in") is True


@app.route("/login", methods=["GET", "POST"])
def dashboard_login():
    if request.method == "GET" and _is_logged_in():
        return redirect(url_for("dashboard"))
    error = None
    if request.method == "POST":
        expected = os.getenv("NEXUS_STOCK_ADMIN_KEY", "")
        supplied = request.form.get("key", "")
        if expected and hmac.compare_digest(supplied, expected):
            session.clear()
            session["nexus_stock_logged_in"] = True
            session.permanent = True
            return redirect(url_for("dashboard"))
        error = "Неверный ключ. Проверьте NEXUS_STOCK_ADMIN_KEY в Render."
    return render_template_string(_DASHBOARD_TEMPLATE, title="Вход", login=True, error=error)


@app.post("/logout")
def dashboard_logout():
    session.clear()
    return redirect(url_for("dashboard_login"))


@app.get("/dashboard")
def dashboard():
    if not _is_logged_in():
        return redirect(url_for("dashboard_login"))
    checked_at = datetime.now(timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M")
    try:
        audit = _analyze_catalog()
        error = None
    except UpstreamError as exc:
        audit = None
        error = "Не удалось получить данные Эвотор. Код: " + exc.kind + (" (HTTP " + str(exc.status) + ")" if exc.status else "")
    except (KeyError, ValueError, TypeError):
        audit = None
        error = "Ошибка формата ответа Эвотор."
    return render_template_string(
        _DASHBOARD_TEMPLATE,
        title="Склад", login=False, data=audit, checked_at=checked_at, error=error,
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000)
