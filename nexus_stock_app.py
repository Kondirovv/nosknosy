"""Nexus Stock: read-only Evotor inventory API for Render.

Environment:
  EVOTOR_TOKEN: secret bearer token created by Evotor.
  NEXUS_STOCK_ADMIN_KEY: independent random secret for private routes.

The application does not contain any API calls that create, modify, or delete
products, documents, stock quantities, or prices in Evotor.
"""

import csv
import hmac
import io
import os
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from decimal import Decimal, InvalidOperation
from flask import Flask, Response, jsonify, request, stream_with_context

app = Flask(__name__)
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
    if not hmac.compare_digest(supplied, expected):
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
        "User-Agent": "NexusStock-ReadOnly/0.2",
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
        "version": "0.2",
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


@app.get("/api/catalog/summary")
def catalog_summary():
    """Inspect every page and report aggregates, without saving or writing data."""
    def read():
        path = _product_path()
        count = 0
        pages = 0
        missing_cost = 0
        zero_cost = 0
        negative_stock = 0
        zero_sale_price = 0
        examples = []
        names = Counter()
        for page in _pages(path, max_pages=100):
            pages += 1
            for item in page["items"]:
                count += 1
                name = item.get("name") or ""
                names[name.strip().casefold()] += 1
                price = _number(item.get("price"))
                cost = _number(item.get("cost_price"))
                quantity = _number(item.get("quantity"))
                reasons = []
                if cost is None:
                    missing_cost += 1
                elif cost == 0:
                    zero_cost += 1
                    reasons.append("zero_cost_price")
                if quantity is not None and quantity < 0:
                    negative_stock += 1
                    reasons.append("negative_stock")
                if price is not None and price <= 0:
                    zero_sale_price += 1
                    reasons.append("nonpositive_sale_price")
                if reasons and len(examples) < 30:
                    examples.append({
                        "name": name[:120],
                        "code": item.get("code"),
                        "issues": reasons,
                    })
        repeated_names = sum(n - 1 for name, n in names.items() if name and n > 1)
        return {
            "store_count": 1, "total_products": count, "pages_loaded": pages,
            "zero_cost_price_count": zero_cost,
            "missing_cost_price_count": missing_cost,
            "negative_stock_count": negative_stock,
            "nonpositive_sale_price_count": zero_sale_price,
            "extra_items_with_same_name": repeated_names,
            "issue_examples": examples,
            "warning": "Same names do not necessarily mean duplicate stock items.",
            "mode": "read_only",
        }
    return _safe_run(read)


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


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000)
