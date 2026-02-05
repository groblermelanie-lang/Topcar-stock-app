from __future__ import annotations

from flask import Flask, request, jsonify, render_template_string, g, Response
import sqlite3
from pathlib import Path
from datetime import datetime
import os

app = Flask(__name__)

# ----------------------------
# Basic LAN auth (simple token)
# ----------------------------
# Set this in your terminal for better safety:
# macOS/Linux: export TOPCAR_TOKEN="yourStrongToken"
# Windows PS:  setx TOPCAR_TOKEN "yourStrongToken"
AUTH_TOKEN = os.environ.get("TOPCAR_TOKEN", "topcar2025!")

def require_auth() -> bool:
    token = request.headers.get("X-Auth-Token", "")
    return token == AUTH_TOKEN


# ----------------------------
# DB location (stable, not Desktop/iCloud)
# ----------------------------
DATA_DIR = Path.home() / "topcar_stock_data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "database.db"


# ----------------------------
# DB helpers
# ----------------------------
def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """Pragmas that improve stability for a small LAN web app."""
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")


def get_db() -> sqlite3.Connection:
    """One SQLite connection per request (stored in flask.g)."""
    if "db" not in g:
        conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        _apply_pragmas(conn)
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def _rotate_corrupt_db() -> None:
    server_dir = Path(__file__).resolve().parent
    corrupt_dir = server_dir / "corrupt_dbs"
    corrupt_dir.mkdir(exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    for suffix in ["", "-wal", "-shm"]:
        src = Path(str(DB_PATH) + suffix)
        if src.exists():
            dst = corrupt_dir / f"database_CORRUPT_{ts}.db{suffix}"
            try:
                os.replace(str(src), str(dst))
            except Exception:
                try:
                    dst.write_bytes(src.read_bytes())
                    src.unlink(missing_ok=True)
                except Exception:
                    pass


def _connect_startup_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)
    return conn


def _col_exists(conn: sqlite3.Connection, table: str, col: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table});").fetchall()
    return any(r["name"] == col for r in rows)


def _add_column_if_missing(conn: sqlite3.Connection, table: str, col: str, ddl: str) -> None:
    """
    ddl example: "unit_price REAL NOT NULL DEFAULT 0"
    """
    if not _col_exists(conn, table, col):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def setup() -> None:
    """
    Create tables + verify DB health.
    Auto-migrate older DBs by adding new columns.
    If corrupted, rotate it and rebuild.
    """
    try:
        conn = _connect_startup_db()

        row = conn.execute("PRAGMA integrity_check;").fetchone()
        if row and row[0] != "ok":
            conn.close()
            raise sqlite3.DatabaseError(f"integrity_check failed: {row[0]}")

        cur = conn.cursor()

        # STOCK TABLE (unit_price, supplier, reorder_qty)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS stock (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 0,
            min_level INTEGER NOT NULL DEFAULT 0,
            unit_price REAL NOT NULL DEFAULT 0,
            supplier TEXT NOT NULL DEFAULT '',
            reorder_qty INTEGER NOT NULL DEFAULT 0
        )
        """)

        # TRANSACTIONS TABLE (unit_price_at_time + reversal tracking)
        # NOTE:
        # - We DO NOT use is_reversed for job totals anymore.
        # - Reversal is tracked by inserting a row with action='reverse' and reversed_from=<original_tx_id>.
        cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL,
            change_qty INTEGER NOT NULL,
            job_no TEXT,
            action TEXT NOT NULL,
            unit_price REAL NOT NULL DEFAULT 0,
            reversed_from INTEGER,
            is_reversed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY(item_id) REFERENCES stock(id)
        )
        """)

        # Auto-migrate old DBs
        _add_column_if_missing(conn, "stock", "unit_price", "unit_price REAL NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "stock", "supplier", "supplier TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "stock", "reorder_qty", "reorder_qty INTEGER NOT NULL DEFAULT 0")

        _add_column_if_missing(conn, "transactions", "unit_price", "unit_price REAL NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "transactions", "reversed_from", "reversed_from INTEGER")
        _add_column_if_missing(conn, "transactions", "is_reversed", "is_reversed INTEGER NOT NULL DEFAULT 0")

        conn.commit()
        conn.close()

    except sqlite3.DatabaseError:
        _rotate_corrupt_db()
        conn = _connect_startup_db()
        cur = conn.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS stock (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 0,
            min_level INTEGER NOT NULL DEFAULT 0,
            unit_price REAL NOT NULL DEFAULT 0,
            supplier TEXT NOT NULL DEFAULT '',
            reorder_qty INTEGER NOT NULL DEFAULT 0
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL,
            change_qty INTEGER NOT NULL,
            job_no TEXT,
            action TEXT NOT NULL,
            unit_price REAL NOT NULL DEFAULT 0,
            reversed_from INTEGER,
            is_reversed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY(item_id) REFERENCES stock(id)
        )
        """)

        conn.commit()
        conn.close()


# ----------------------------
# UI (served at "/")
# ----------------------------
PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Topcar Stock</title>
  <style>
    body { font-family: Arial; padding: 20px; max-width: 1100px; margin: auto; }
    h1 { margin-bottom: 5px; }
    .box { border: 1px solid #ddd; padding: 12px; border-radius: 10px; margin: 12px 0; }
    input { padding: 8px; margin: 6px 4px; }
    button { padding: 8px 12px; cursor: pointer; }
    table { width: 100%; border-collapse: collapse; margin-top: 10px; }
    th, td { border-bottom: 1px solid #eee; padding: 8px; text-align: left; }
    .low { background: #fff3cd; }
    .msg { margin-top: 10px; white-space: pre-wrap; }
    .hint { color: #666; font-size: 0.9rem; }
    .row { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
    .small { min-width: 120px; }
    .mid { min-width: 180px; }
    .right { text-align: right; }
    .subtle { color:#333; font-size: 0.95rem; }
  </style>
</head>
<body>
  <h1>Topcar Stock</h1>
  <p class="hint">Server OK ✅ | Token required for changes + job reports 🔐</p>

  <div class="box">
    <h3>Login Token</h3>
    <div class="row">
      <input id="token" class="mid" placeholder="Enter token (X-Auth-Token)">
      <button onclick="saveToken()">Save Token</button>
      <span class="hint">Saved in this browser (localStorage).</span>
    </div>
  </div>

  <div class="box">
    <h3>Add Item</h3>
    <div class="row">
      <input id="name" class="mid" placeholder="Item name">
      <input id="qty" class="small" type="number" placeholder="Start qty" value="0">
      <input id="min" class="small" type="number" placeholder="Min level" value="0">
      <input id="price" class="small" type="number" step="0.01" placeholder="Unit price" value="0">
      <input id="supplier" class="mid" placeholder="Supplier (optional)">
      <input id="reorder" class="small" type="number" placeholder="Reorder qty" value="0">
      <button onclick="addItem()">Add</button>
    </div>
  </div>

  <div class="box">
    <h3>Checkout (Stock Out)</h3>
    <div class="row">
      <input id="co_item" class="small" type="number" placeholder="Item ID">
      <input id="co_qty" class="small" type="number" placeholder="Qty">
      <input id="job" class="mid" placeholder="Job No (required)">
      <button onclick="checkout()">Checkout</button>
    </div>
  </div>

  <div class="box">
    <h3>Receive (Stock In)</h3>
    <div class="row">
      <input id="re_item" class="small" type="number" placeholder="Item ID">
      <input id="re_qty" class="small" type="number" placeholder="Qty">
      <button onclick="receive()">Receive</button>
    </div>
  </div>

  <div class="box">
    <h3>Reverse / Undo Transaction</h3>
    <div class="row">
      <input id="tx_id" class="small" type="number" placeholder="Transaction ID">
      <button onclick="reverseTx()">Reverse</button>
      <span class="hint">Reverses stock + creates a linked “reverse” transaction.</span>
    </div>
  </div>

  <div class="box">
    <h3>Job Report (Usage per Job No)</h3>
    <div class="row">
      <input id="job_report_no" class="mid" placeholder="Job No (e.g. J12345)">
      <button onclick="loadJobReport()">Load Job Report</button>
      <button onclick="printJobReport()">Print Job Report</button>
      <span class="hint">Uses unit price at time of stock-out.</span>
    </div>
    <div id="job_report" class="subtle"></div>
  </div>

  <div class="box">
    <h3>Stock List</h3>
    <button onclick="loadStock()">Refresh</button>
    <button onclick="loadReorder()">Reorder List</button>
    <div id="table"></div>
  </div>

  <div class="msg" id="msg"></div>

<script>
function getToken(){ return localStorage.getItem("topcar_token") || ""; }

function saveToken(){
  const t = document.getElementById("token").value || "";
  localStorage.setItem("topcar_token", t);
  showMsg({message:"Token saved ✅"});
}

function authHeaders(){
  return {
    'Content-Type':'application/json',
    'X-Auth-Token': getToken()
  };
}

function showMsg(x){ document.getElementById('msg').textContent = JSON.stringify(x, null, 2); }

async function loadStock(){
  const res = await fetch('/stock');
  const data = await res.json();
  let html = '<table><tr><th>ID</th><th>Name</th><th>Qty</th><th>Min</th><th>Unit Price</th><th>Value</th><th>Supplier</th><th>Reorder Qty</th><th>Status</th></tr>';
  for (const it of data){
    const low = it.quantity <= it.min_level;
    const val = (Number(it.quantity) * Number(it.unit_price || 0)).toFixed(2);
    html += `<tr class="${low ? 'low' : ''}">
      <td>${it.id}</td>
      <td>${it.name}</td>
      <td>${it.quantity}</td>
      <td>${it.min_level}</td>
      <td>${Number(it.unit_price || 0).toFixed(2)}</td>
      <td>${val}</td>
      <td>${it.supplier || ''}</td>
      <td>${it.reorder_qty || 0}</td>
      <td>${low ? 'LOW STOCK ⚠️' : ''}</td>
    </tr>`;
  }
  html += '</table>';
  document.getElementById('table').innerHTML = html;
}

async function loadReorder(){
  const res = await fetch('/reorder');
  const data = await res.json();
  let html = '<table><tr><th>ID</th><th>Name</th><th>Qty</th><th>Min</th><th>Supplier</th><th>Reorder Qty</th></tr>';
  for (const it of data){
    html += `<tr class="low">
      <td>${it.id}</td>
      <td>${it.name}</td>
      <td>${it.quantity}</td>
      <td>${it.min_level}</td>
      <td>${it.supplier || ''}</td>
      <td>${it.reorder_qty || 0}</td>
    </tr>`;
  }
  html += '</table>';
  document.getElementById('table').innerHTML = html;
}

async function addItem(){
  const body = {
    name: document.getElementById('name').value,
    quantity: parseInt(document.getElementById('qty').value || 0),
    min_level: parseInt(document.getElementById('min').value || 0),
    unit_price: parseFloat(document.getElementById('price').value || 0),
    supplier: document.getElementById('supplier').value || "",
    reorder_qty: parseInt(document.getElementById('reorder').value || 0),
  };
  const res = await fetch('/stock', {method:'POST', headers: authHeaders(), body: JSON.stringify(body)});
  showMsg(await res.json());
  loadStock();
}

async function checkout(){
  const body = {
    item_id: parseInt(document.getElementById('co_item').value || 0),
    quantity: parseInt(document.getElementById('co_qty').value || 0),
    job_no: document.getElementById('job').value
  };
  const res = await fetch('/checkout', {method:'POST', headers: authHeaders(), body: JSON.stringify(body)});
  showMsg(await res.json());
  loadStock();
}

async function receive(){
  const body = {
    item_id: parseInt(document.getElementById('re_item').value || 0),
    quantity: parseInt(document.getElementById('re_qty').value || 0),
  };
  const res = await fetch('/receive', {method:'POST', headers: authHeaders(), body: JSON.stringify(body)});
  showMsg(await res.json());
  loadStock();
}

async function reverseTx(){
  const body = { transaction_id: parseInt(document.getElementById('tx_id').value || 0) };
  const res = await fetch('/reverse', {method:'POST', headers: authHeaders(), body: JSON.stringify(body)});
  showMsg(await res.json());
  loadStock();
}

async function loadJobReport(){
  const jobNo = (document.getElementById('job_report_no').value || "").trim();
  if (!jobNo){
    document.getElementById('job_report').innerHTML = "<p class='hint'>Enter a Job No first.</p>";
    return;
  }

  const res = await fetch(`/job/${encodeURIComponent(jobNo)}/usage`, { headers: {'X-Auth-Token': getToken()} });
  const data = await res.json();

  if (!res.ok){
    document.getElementById('job_report').innerHTML = `<pre>${JSON.stringify(data, null, 2)}</pre>`;
    return;
  }

  let html = `<p><b>Job:</b> ${data.job_no} &nbsp; | &nbsp; <b>Generated:</b> ${data.generated_at}</p>`;
  html += `<p><b>Total (ZAR):</b> ${Number(data.total_value).toFixed(2)}</p>`;

  html += "<table><tr><th>Item</th><th class='right'>Net Qty</th><th class='right'>Unit Price</th><th class='right'>Line Total</th></tr>";
  for (const r of data.items){
    html += `<tr>
      <td>${r.name}</td>
      <td class="right">${r.net_qty}</td>
      <td class="right">${Number(r.unit_price).toFixed(2)}</td>
      <td class="right">${Number(r.line_total).toFixed(2)}</td>
    </tr>`;
  }
  html += "</table>";

  html += "<p class='hint'>Net Qty = checkouts minus reversals. Uses unit price stored at time of transaction.</p>";

  document.getElementById('job_report').innerHTML = html;
}

async function printJobReport(){
  const jobNo = (document.getElementById('job_report_no').value || "").trim();
  if (!jobNo){
    showMsg({error:"Enter a Job No first."});
    return;
  }

  const w = window.open("", "_blank");
  w.document.write("<p>Loading report...</p>");
  w.document.close();

  const res = await fetch(`/job/${encodeURIComponent(jobNo)}/print`, { headers: {'X-Auth-Token': getToken()} });
  const html = await res.text();
  w.document.open();
  w.document.write(html);
  w.document.close();
}

document.getElementById("token").value = getToken();
loadStock();
</script>

</body>
</html>
"""


@app.get("/")
def home():
    return render_template_string(PAGE)


# ----------------------------
# API routes
# ----------------------------
@app.get("/stock")
def get_stock():
    conn = get_db()
    rows = conn.execute("SELECT * FROM stock ORDER BY name").fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/stock")
def add_item():
    if not require_auth():
        return jsonify({"error": "unauthorized (missing/incorrect token)"}), 401

    data = request.json or {}
    name = (data.get("name") or "").strip()
    quantity = int(data.get("quantity", 0))
    min_level = int(data.get("min_level", 0))
    unit_price = float(data.get("unit_price", 0) or 0)
    supplier = (data.get("supplier") or "").strip()
    reorder_qty = int(data.get("reorder_qty", 0))

    if not name:
        return jsonify({"error": "name is required"}), 400
    if quantity < 0 or min_level < 0 or unit_price < 0 or reorder_qty < 0:
        return jsonify({"error": "values cannot be negative"}), 400

    conn = get_db()
    conn.execute(
        "INSERT INTO stock(name, quantity, min_level, unit_price, supplier, reorder_qty) VALUES (?, ?, ?, ?, ?, ?)",
        (name, quantity, min_level, unit_price, supplier, reorder_qty)
    )
    conn.commit()
    return jsonify({"message": "Item added ✅"}), 201


@app.post("/checkout")
def checkout():
    if not require_auth():
        return jsonify({"error": "unauthorized (missing/incorrect token)"}), 401

    data = request.json or {}
    item_id = int(data.get("item_id", 0))
    qty = int(data.get("quantity", 0))
    job_no = (data.get("job_no") or "").strip()

    if item_id <= 0 or qty <= 0:
        return jsonify({"error": "item_id and quantity must be > 0"}), 400
    if not job_no:
        return jsonify({"error": "job_no is required"}), 400

    conn = get_db()
    item = conn.execute("SELECT * FROM stock WHERE id=?", (item_id,)).fetchone()
    if not item:
        return jsonify({"error": "item not found"}), 404

    if item["quantity"] < qty:
        return jsonify({"error": "not enough stock"}), 400

    new_qty = item["quantity"] - qty
    unit_price = float(item["unit_price"] or 0)

    conn.execute("UPDATE stock SET quantity=? WHERE id=?", (new_qty, item_id))
    conn.execute(
        "INSERT INTO transactions(item_id, change_qty, job_no, action, unit_price) VALUES (?, ?, ?, ?, ?)",
        (item_id, -qty, job_no, "checkout", unit_price)
    )
    conn.commit()

    low = new_qty <= item["min_level"]
    spent = round(qty * unit_price, 2)
    return jsonify({
        "message": "Checked out ✅",
        "new_quantity": new_qty,
        "low_stock_alert": low,
        "unit_price_at_time": unit_price,
        "line_value": spent
    })


@app.post("/receive")
def receive():
    if not require_auth():
        return jsonify({"error": "unauthorized (missing/incorrect token)"}), 401

    data = request.json or {}
    item_id = int(data.get("item_id", 0))
    qty = int(data.get("quantity", 0))

    if item_id <= 0 or qty <= 0:
        return jsonify({"error": "item_id and quantity must be > 0"}), 400

    conn = get_db()
    item = conn.execute("SELECT * FROM stock WHERE id=?", (item_id,)).fetchone()
    if not item:
        return jsonify({"error": "item not found"}), 404

    new_qty = item["quantity"] + qty
    unit_price = float(item["unit_price"] or 0)

    conn.execute("UPDATE stock SET quantity=? WHERE id=?", (new_qty, item_id))
    conn.execute(
        "INSERT INTO transactions(item_id, change_qty, job_no, action, unit_price) VALUES (?, ?, ?, ?, ?)",
        (item_id, qty, None, "receive", unit_price)
    )
    conn.commit()

    return jsonify({
        "message": "Received ✅",
        "new_quantity": new_qty,
        "unit_price_at_time": unit_price
    })


@app.get("/reorder")
def reorder_list():
    conn = get_db()
    rows = conn.execute("""
        SELECT id, name, quantity, min_level, supplier, reorder_qty
        FROM stock
        WHERE quantity <= min_level
        ORDER BY name
    """).fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/reverse")
def reverse_transaction():
    """
    FIXED:
    - We no longer flip the original row to is_reversed=1 (that broke job reports).
    - A reversal is tracked by inserting a NEW row:
        action='reverse', reversed_from=<original_tx_id>, change_qty = -original_change
    - Double reverse is blocked by checking if a reverse row already exists for reversed_from=tx_id.
    """
    if not require_auth():
        return jsonify({"error": "unauthorized (missing/incorrect token)"}), 401

    data = request.json or {}
    tx_id = int(data.get("transaction_id", 0))
    if tx_id <= 0:
        return jsonify({"error": "transaction_id must be > 0"}), 400

    conn = get_db()

    tx = conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if not tx:
        return jsonify({"error": "transaction not found"}), 404

    if (tx["action"] or "") == "reverse":
        return jsonify({"error": "cannot reverse a reverse transaction"}), 400

    # Block double-reverse
    already = conn.execute(
        "SELECT 1 FROM transactions WHERE action='reverse' AND reversed_from=? LIMIT 1",
        (tx_id,)
    ).fetchone()
    if already:
        return jsonify({"error": "transaction already reversed"}), 400

    item = conn.execute("SELECT * FROM stock WHERE id=?", (tx["item_id"],)).fetchone()
    if not item:
        return jsonify({"error": "item for this transaction no longer exists"}), 400

    original_change = int(tx["change_qty"])
    reverse_change = -original_change  # opposite sign

    new_qty = int(item["quantity"]) + reverse_change
    if new_qty < 0:
        return jsonify({"error": "reversal would make stock negative"}), 400

    unit_price_at_time = float(tx["unit_price"] or 0)

    # Apply stock reversal
    conn.execute("UPDATE stock SET quantity=? WHERE id=?", (new_qty, tx["item_id"]))

    # Insert the reversal transaction (linked)
    conn.execute("""
        INSERT INTO transactions(item_id, change_qty, job_no, action, unit_price, reversed_from, is_reversed)
        VALUES (?, ?, ?, ?, ?, ?, 0)
    """, (tx["item_id"], reverse_change, tx["job_no"], "reverse", unit_price_at_time, tx_id))

    conn.commit()

    return jsonify({
        "message": "Transaction reversed ✅",
        "item_id": tx["item_id"],
        "new_quantity": new_qty,
        "reversed_from": tx_id
    })


# ----------------------------
# JOB REPORTS
# ----------------------------
def _job_usage(conn: sqlite3.Connection, job_no: str):
    """
    FIXED:
    Net usage per item for a job:
    - includes checkout transactions and reverse transactions
    - reversals are stored as their own rows (action='reverse') with reversed_from pointing to the original tx
    Net qty = -(sum(change_qty))
      checkout: change_qty negative -> increases usage
      reverse:  change_qty positive -> reduces usage

    NOTE: we do NOT filter by is_reversed anymore, because we don't flip original rows.
    """
    rows = conn.execute("""
        SELECT
            s.id AS item_id,
            s.name AS name,
            t.unit_price AS unit_price,
            SUM(t.change_qty) AS sum_change
        FROM transactions t
        JOIN stock s ON s.id = t.item_id
        WHERE t.job_no = ?
          AND t.action IN ('checkout', 'reverse')
        GROUP BY s.id, s.name, t.unit_price
        ORDER BY s.name
    """, (job_no,)).fetchall()

    items = []
    total = 0.0

    for r in rows:
        sum_change = int(r["sum_change"] or 0)
        net_qty = -sum_change
        unit_price = float(r["unit_price"] or 0.0)
        line_total = round(net_qty * unit_price, 2)
        total += line_total
        items.append({
            "item_id": r["item_id"],
            "name": r["name"],
            "net_qty": net_qty,
            "unit_price": unit_price,
            "line_total": line_total
        })

    total = round(total, 2)
    return items, total


@app.get("/job/<job_no>/usage")
def job_usage(job_no: str):
    if not require_auth():
        return jsonify({"error": "unauthorized (missing/incorrect token)"}), 401

    job_no = (job_no or "").strip()
    if not job_no:
        return jsonify({"error": "job_no is required"}), 400

    conn = get_db()
    items, total = _job_usage(conn, job_no)

    if not items:
        return jsonify({"error": f"no usage found for job_no '{job_no}'"}), 404

    return jsonify({
        "job_no": job_no,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total_value": total,
        "items": items
    })


@app.get("/job/<job_no>/print")
def job_print(job_no: str):
    if not require_auth():
        return Response("Unauthorized", status=401, mimetype="text/plain")

    job_no = (job_no or "").strip()
    if not job_no:
        return Response("Job No required", status=400, mimetype="text/plain")

    conn = get_db()
    items, total = _job_usage(conn, job_no)

    if not items:
        return Response(f"No usage found for job_no '{job_no}'", status=404, mimetype="text/plain")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    html_rows = ""
    for it in items:
        html_rows += f"""
        <tr>
          <td>{it['name']}</td>
          <td style="text-align:right;">{it['net_qty']}</td>
          <td style="text-align:right;">{it['unit_price']:.2f}</td>
          <td style="text-align:right;">{it['line_total']:.2f}</td>
        </tr>
        """

    html = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Job Report {job_no}</title>
  <style>
    body {{ font-family: Arial; padding: 24px; }}
    h1 {{ margin: 0; }}
    .meta {{ margin-top: 6px; color: #333; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 14px; }}
    th, td {{ border-bottom: 1px solid #ddd; padding: 8px; }}
    th {{ text-align: left; }}
    .right {{ text-align: right; }}
    .total {{ margin-top: 14px; font-size: 1.1rem; }}
    .small {{ color:#666; margin-top: 10px; font-size: 0.9rem; }}
    @media print {{
      button {{ display: none; }}
    }}
  </style>
</head>
<body>
  <button onclick="window.print()">Print</button>
  <h1>Topcar Stock — Job Usage Report</h1>
  <div class="meta"><b>Job No:</b> {job_no} &nbsp; | &nbsp; <b>Generated:</b> {now_str}</div>

  <table>
    <tr>
      <th>Item</th>
      <th class="right">Net Qty</th>
      <th class="right">Unit Price</th>
      <th class="right">Line Total</th>
    </tr>
    {html_rows}
  </table>

  <div class="total"><b>Total (ZAR):</b> {total:.2f}</div>
  <div class="small">Net Qty = checkouts minus reversals. Unit price is stored at time of transaction.</div>
</body>
</html>
"""
    return Response(html, mimetype="text/html")


# ----------------------------
# Main
# ----------------------------
if __name__ == "__main__":
    setup()
    from waitress import serve
    print("Topcar Stock running on http://127.0.0.1:5050  (LAN: http://YOUR-IP:5050)")
    serve(app, host="0.0.0.0", port=5050)








