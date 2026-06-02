#!/usr/bin/env python3
"""
SplitEase v1.2.0 — Weekend Expense Splitter
Mobile-first web app for splitting expenses with friends.
Auto-auth via nickname cookie, shareable group links.
Receipt photo upload with background removal.
"""

import os
import uuid
import sqlite3
from flask import (Flask, request, redirect, url_for, jsonify,
                   render_template_string, make_response, send_file)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'splitwise.db')
UPLOAD_DIR = os.path.join(BASE_DIR, 'receipts')
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.urandom(24).hex()
MAX_RECEIPT_SIZE = 5 * 1024 * 1024  # 5MB

# ─── DB ───────────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS groups (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS members (
            id TEXT PRIMARY KEY,
            group_id TEXT NOT NULL,
            nickname TEXT NOT NULL,
            token TEXT UNIQUE NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
            UNIQUE(group_id, nickname)
        );
        CREATE TABLE IF NOT EXISTS expenses (
            id TEXT PRIMARY KEY,
            group_id TEXT NOT NULL,
            payer_id TEXT NOT NULL,
            amount REAL NOT NULL,
            description TEXT NOT NULL,
            receipt_path TEXT DEFAULT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
            FOREIGN KEY (payer_id) REFERENCES members(id) ON DELETE CASCADE
        );
    """)
    conn.commit()
    conn.close()

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def get_member_from_cookie(group_id):
    token = request.cookies.get('se_token')
    if not token:
        return None
    conn = get_db()
    m = conn.execute("SELECT * FROM members WHERE token=? AND group_id=?", (token, group_id)).fetchone()
    conn.close()
    return dict(m) if m else None

def calc_balances(group_id):
    conn = get_db()
    members = conn.execute("SELECT * FROM members WHERE group_id=?", (group_id,)).fetchall()
    expenses = conn.execute("SELECT * FROM expenses WHERE group_id=?", (group_id,)).fetchall()
    conn.close()
    if not members:
        return {}
    n = len(members)
    paid = {m['id']: 0.0 for m in members}
    for e in expenses:
        if e['payer_id'] in paid:
            paid[e['payer_id']] += e['amount']
    total = sum(e['amount'] for e in expenses)
    share = total / n if n > 0 else 0
    balances = {}
    for m in members:
        b = round(paid[m['id']] - share, 2)
        balances[m['id']] = {
            'nickname': m['nickname'],
            'paid': round(paid[m['id']], 2),
            'share': round(share, 2),
            'balance': b,
        }
    return balances

def calc_settlements(group_id):
    bal = calc_balances(group_id)
    debtors, creditors = [], []
    for mid, info in bal.items():
        if info['balance'] < -0.01:
            debtors.append((mid, info['nickname'], -info['balance']))
        elif info['balance'] > 0.01:
            creditors.append((mid, info['nickname'], info['balance']))
    settlements = []
    i, j = 0, 0
    while i < len(debtors) and j < len(creditors):
        d_amt, c_amt = debtors[i][2], creditors[j][2]
        transfer = min(d_amt, c_amt)
        settlements.append((debtors[i][1], creditors[j][1], round(transfer, 2)))
        debtors[i] = (debtors[i][0], debtors[i][1], d_amt - transfer)
        creditors[j] = (creditors[j][0], creditors[j][1], c_amt - transfer)
        if debtors[i][2] < 0.01: i += 1
        if creditors[j][2] < 0.01: j += 1
    return settlements

def fmt_eur(v):
    s = f"{v:,.2f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")

import shutil
def backup_db():
    """Create a timestamped backup of the DB before any write operation."""
    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.join(BASE_DIR, 'backups')
    os.makedirs(backup_dir, exist_ok=True)
    dst = os.path.join(backup_dir, f'splitwise_{ts}.db')
    shutil.copy2(DB_PATH, dst)
    # Keep only the last 20 backups
    backups = sorted([f for f in os.listdir(backup_dir) if f.endswith('.db')])
    for old in backups[:-20]:
        os.remove(os.path.join(backup_dir, old))
    return dst

def remove_receipt_bg(input_path, output_path):
    """Remove background from receipt image using Pillow + numpy."""
    try:
        from PIL import Image, ImageFilter
        import numpy as np

        img = Image.open(input_path).convert("RGBA")
        w, h = img.size

        # Resize for performance (max 1200px)
        max_dim = 1200
        if max(w, h) > max_dim:
            ratio = max_dim / max(w, h)
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
            w, h = img.size

        arr = np.array(img)
        r, g, b, a = arr[:,:,0].astype(float), arr[:,:,1].astype(float), arr[:,:,2].astype(float), arr[:,:,3]

        # Sample corners to detect background color
        corner_pixels = np.concatenate([
            arr[:15, :15, :3].reshape(-1, 3),
            arr[:15, -15:, :3].reshape(-1, 3),
            arr[-15:, :15, :3].reshape(-1, 3),
            arr[-15:, -15:, :3].reshape(-1, 3),
        ]).astype(float)
        bg_color = np.median(corner_pixels, axis=0)

        # Distance from background
        rgb = arr[:, :, :3].astype(float)
        dist = np.sqrt(np.sum((rgb - bg_color) ** 2, axis=2))

        # Threshold: pixels close to bg → transparent
        # Adaptive threshold based on image contrast
        threshold = 55
        alpha = np.where(dist < threshold, 0, 255).astype(np.uint8)

        # Smooth edges
        alpha_img = Image.fromarray(alpha, mode='L')
        alpha_img = alpha_img.filter(ImageFilter.GaussianBlur(1.5))
        alpha = np.array(alpha_img)
        alpha = np.where(alpha < 100, 0, 255).astype(np.uint8)

        arr[:,:,3] = alpha
        result = Image.fromarray(arr)
        result.save(output_path, "PNG")
        return True
    except Exception as e:
        print(f"BG removal error: {e}")
        import shutil
        shutil.copy2(input_path, output_path)
        return True

# ─── PAGES ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template_string(INDEX_HTML)


@app.route('/g/<group_id>', methods=['GET'])
def group_page(group_id):
    conn = get_db()
    g = conn.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
    if not g:
        conn.close()
        return "Gruppo non trovato", 404

    member = get_member_from_cookie(group_id)
    members = conn.execute("SELECT * FROM members WHERE group_id=?", (group_id,)).fetchall()
    expenses = conn.execute(
        "SELECT e.*, m.nickname as payer_name FROM expenses e JOIN members m ON e.payer_id=m.id WHERE e.group_id=? ORDER BY e.created_at DESC",
        (group_id,)
    ).fetchall()
    conn.close()

    balances = calc_balances(group_id)
    settlements = calc_settlements(group_id)
    total = sum(e['amount'] for e in expenses)
    per_person = total / len(members) if members else 0
    share_url = request.host_url.rstrip('/') + '/g/' + group_id

    return render_template_string(APP_TEMPLATE,
        group_name=g['name'],
        group_id=group_id,
        total=fmt_eur(total),
        per_person=fmt_eur(per_person),
        n_members=len(members),
        n_expenses=len(expenses),
        members=members,
        member=member,
        balances=balances,
        settlements=settlements,
        expenses=expenses,
        share_url=share_url,
        fmt=fmt_eur,
    )


@app.route('/g/<group_id>/join', methods=['POST'])
def join_group(group_id):
    nickname = request.form.get('nickname', '').strip()
    if not nickname:
        return redirect(url_for('group_page', group_id=group_id))
    conn = get_db()
    existing = conn.execute("SELECT id FROM members WHERE group_id=? AND nickname=?",
                            (group_id, nickname)).fetchone()
    if existing:
        token = str(uuid.uuid4())
        conn.execute("UPDATE members SET token=? WHERE id=?", (token, existing['id']))
    else:
        token = str(uuid.uuid4())
        member_id = str(uuid.uuid4())[:8]
        conn.execute("INSERT INTO members (id, group_id, nickname, token) VALUES (?,?,?,?)",
                     (member_id, group_id, nickname, token))
    conn.commit()
    conn.close()
    resp = redirect(url_for('group_page', group_id=group_id))
    resp.set_cookie('se_token', token, max_age=365*24*3600, httponly=True, samesite='Lax')
    return resp


@app.route('/g/<group_id>/expense', methods=['POST'])
def add_expense(group_id):
    member = get_member_from_cookie(group_id)
    if not member:
        return redirect(url_for('group_page', group_id=group_id))
    payer_id = request.form.get('payer_id', member['id'])
    description = request.form.get('description', '').strip()
    amount_str = request.form.get('amount', '').strip()
    if not description or not amount_str:
        return redirect(url_for('group_page', group_id=group_id))
    try:
        amount = float(amount_str)
        if amount <= 0: raise ValueError
    except ValueError:
        return redirect(url_for('group_page', group_id=group_id))
    conn = get_db()
    backup_db()
    expense_id = str(uuid.uuid4())[:8]
    conn.execute("INSERT INTO expenses (id, group_id, payer_id, amount, description) VALUES (?,?,?,?,?)",
                 (expense_id, group_id, payer_id, amount, description))
    conn.commit()
    conn.close()
    return redirect(url_for('group_page', group_id=group_id))


@app.route('/g/<group_id>/delete/<expense_id>', methods=['POST'])
def delete_expense(group_id, expense_id):
    member = get_member_from_cookie(group_id)
    if not member:
        return redirect(url_for('group_page', group_id=group_id))
    backup_db()
    conn = get_db()
    exp = conn.execute("SELECT receipt_path FROM expenses WHERE id=? AND group_id=?", (expense_id, group_id)).fetchone()
    if exp and exp['receipt_path']:
        for suffix in ['.jpg', '_bg.png']:
            p = os.path.join(UPLOAD_DIR, exp['receipt_path'] + suffix)
            if os.path.exists(p):
                os.remove(p)
    conn.execute("DELETE FROM expenses WHERE id=? AND group_id=?", (expense_id, group_id))
    conn.commit()
    conn.close()
    return redirect(url_for('group_page', group_id=group_id))


@app.route('/api/expense/<expense_id>', methods=['PATCH'])
def update_expense(expense_id):
    """Inline edit: update amount and/or description. Returns updated balances."""
    member = get_member_from_cookie(request.args.get('gid', ''))
    if not member:
        data = request.get_json(force=True) if request.is_json else {}
        group_id = data.get('group_id', '')
        member = get_member_from_cookie(group_id)
    if not member:
        return jsonify({"error": "not authenticated"}), 401

    data = request.get_json(force=True)
    group_id = data.get('group_id', '')

    conn = get_db()
    exp = conn.execute("SELECT * FROM expenses WHERE id=? AND group_id=?", (expense_id, group_id)).fetchone()
    if not exp:
        conn.close()
        return jsonify({"error": "expense not found"}), 404

    # Backup before modification
    backup_db()

    updates = {}
    if 'amount' in data:
        try:
            amount = float(data['amount'])
            if amount <= 0:
                return jsonify({"error": "amount must be positive"}), 400
            updates['amount'] = amount
        except (ValueError, TypeError):
            return jsonify({"error": "invalid amount"}), 400
    if 'description' in data:
        desc = data['description'].strip()
        if desc:
            updates['description'] = desc

    if updates:
        set_clause = ", ".join(f"{k}=?" for k in updates)
        values = list(updates.values()) + [expense_id, group_id]
        conn.execute(f"UPDATE expenses SET {set_clause} WHERE id=? AND group_id=?", values)
        conn.commit()

    # Recalculate and return
    expenses = conn.execute("SELECT e.*, m.nickname as payer_name FROM expenses e JOIN members m ON e.payer_id=m.id WHERE e.group_id=? ORDER BY e.created_at DESC", (group_id,)).fetchall()
    members = conn.execute("SELECT * FROM members WHERE group_id=?", (group_id,)).fetchall()
    conn.close()

    balances = calc_balances(group_id)
    settlements = calc_settlements(group_id)
    total = sum(e['amount'] for e in expenses)
    per_person = total / len(members) if members else 0

    return jsonify({
        "ok": True,
        "total": fmt_eur(total),
        "per_person": fmt_eur(per_person),
        "n_members": len(members),
        "n_expenses": len(expenses),
        "balances": {mid: {"nickname": info["nickname"], "paid": fmt_eur(info["paid"]), "share": fmt_eur(info["share"]), "balance": fmt_eur(info["balance"]),
                           "balance_raw": info["balance"], "color": "green" if info["balance"] > 0.01 else ("red" if info["balance"] < -0.01 else "neutral"),
                           "label": f"Riceve € {fmt_eur(info['balance'])}" if info["balance"] > 0.01 else (f"Deve dare € {fmt_eur(abs(info['balance']))}" if info["balance"] < -0.01 else "Pari ✅"),
                           "emoji": "📥" if info["balance"] > 0.01 else ("📤" if info["balance"] < -0.01 else "🤝")
                          } for mid, info in balances.items()},
        "settlements": [{"from": s[0], "to": s[1], "amount": fmt_eur(s[2])} for s in settlements],
    })


@app.route('/g/<group_id>/receipt/<expense_id>', methods=['POST'])
def upload_receipt(group_id, expense_id):
    member = get_member_from_cookie(group_id)
    if not member:
        return jsonify({"error": "not authenticated"}), 401

    if 'receipt' not in request.files:
        return jsonify({"error": "no file"}), 400
    file = request.files['receipt']
    if file.filename == '':
        return jsonify({"error": "no file selected"}), 400

    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    if size > MAX_RECEIPT_SIZE:
        return jsonify({"error": "file too large (max 5MB)"}), 400

    receipt_id = f"{group_id}_{expense_id}"
    orig_path = os.path.join(UPLOAD_DIR, receipt_id + '.jpg')
    bg_path = os.path.join(UPLOAD_DIR, receipt_id + '_bg.png')

    # Remove old files
    for p in [orig_path, bg_path]:
        if os.path.exists(p):
            os.remove(p)

    from PIL import Image as PILImage
    img = PILImage.open(file.stream)
    if max(img.size) > 1600:
        img.thumbnail((1600, 1600), PILImage.LANCZOS)
    img = img.convert("RGB")
    img.save(orig_path, "JPEG", quality=85)

    remove_receipt_bg(orig_path, bg_path)

    conn = get_db()
    conn.execute("UPDATE expenses SET receipt_path=? WHERE id=? AND group_id=?",
                 (receipt_id, expense_id, group_id))
    conn.commit()
    conn.close()

    return jsonify({"ok": True, "receipt_id": receipt_id})


@app.route('/g/<group_id>/receipt/<expense_id>', methods=['DELETE'])
def delete_receipt(group_id, expense_id):
    member = get_member_from_cookie(group_id)
    if not member:
        return jsonify({"error": "not authenticated"}), 401
    conn = get_db()
    exp = conn.execute("SELECT receipt_path FROM expenses WHERE id=? AND group_id=?", (expense_id, group_id)).fetchone()
    if exp and exp['receipt_path']:
        for suffix in ['.jpg', '_bg.png']:
            p = os.path.join(UPLOAD_DIR, exp['receipt_path'] + suffix)
            if os.path.exists(p):
                os.remove(p)
        conn.execute("UPDATE expenses SET receipt_path=NULL WHERE id=? AND group_id=?", (expense_id, group_id))
        conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route('/receipts/<path:filename>')
def serve_receipt(filename):
    return send_file(os.path.join(UPLOAD_DIR, filename))


@app.route('/api/create', methods=['POST'])
def api_create():
    data = request.get_json() or {}
    group_name = data.get('group_name', '').strip()
    nickname = data.get('nickname', '').strip()
    first_desc = data.get('description', '').strip()
    first_amount = data.get('amount', 0)
    if not group_name or not nickname:
        return jsonify({"error": "group_name and nickname required"}), 400
    group_id = str(uuid.uuid4())[:10]
    member_id = str(uuid.uuid4())[:8]
    token = str(uuid.uuid4())
    conn = get_db()
    conn.execute("INSERT INTO groups (id, name) VALUES (?,?)", (group_id, group_name))
    conn.execute("INSERT INTO members (id, group_id, nickname, token) VALUES (?,?,?,?)",
                 (member_id, group_id, nickname, token))
    if first_desc and first_amount:
        try:
            first_amount = float(first_amount)
            if first_amount > 0:
                expense_id = str(uuid.uuid4())[:8]
                conn.execute("INSERT INTO expenses (id, group_id, payer_id, amount, description) VALUES (?,?,?,?,?)",
                             (expense_id, group_id, member_id, first_amount, first_desc))
        except ValueError:
            pass
    conn.commit()
    conn.close()
    return jsonify({"group_id": group_id, "token": token, "url": f"/g/{group_id}"})


# ─── SHARED STYLE ─────────────────────────────────────────────────────────────

THEME_CSS = """..."""  # Same as v1.0.0 - will be filled below

# We keep the same CSS, just include inline
THEME_CSS_FULL = """<style>
:root{--bg:#F5F5F7;--bg2:#FFF;--bg3:#EEEEF0;--text:#1A1A2E;--text2:#555566;--text3:#8888AA;--border:#DDDDDF;--accent:#6C5CE7;--accent2:#00C9A7;--accent-soft:#6C5CE720;--green:#00B894;--green-bg:#00B89418;--green-text:#00876A;--red:#E74C3C;--red-bg:#E74C3C18;--red-text:#C0392B;--card-shadow:0 1px 3px rgba(0,0,0,.06),0 1px 2px rgba(0,0,0,.04);--radius:14px}
[data-theme="dark"]{--bg:#0D0D12;--bg2:#16161E;--bg3:#1E1E2A;--text:#EAEAF0;--text2:#A0A0B8;--text3:#6A6A80;--border:#2A2A3A;--accent:#F59E0B;--accent2:#34D399;--accent-soft:#F59E0B25;--green:#34D399;--green-bg:#34D39918;--green-text:#6EE7B7;--red:#F87171;--red-bg:#F8717118;--red-text:#FCA5A5;--card-shadow:0 1px 3px rgba(0,0,0,.3)}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);font-size:17px;line-height:1.55;-webkit-font-smoothing:antialiased}
input,select,button,textarea{font-family:inherit;font-size:inherit}
.theme-toggle{position:sticky;top:0;z-index:200;display:flex;justify-content:flex-end;padding:10px 16px 0;background:transparent}
.theme-btn{background:var(--bg3);color:var(--text2);border:1px solid var(--border);border-radius:10px;padding:8px 14px;font-size:15px;cursor:pointer;transition:all .2s;font-weight:600}
.theme-btn:hover{background:var(--accent);color:#fff;border-color:var(--accent)}
.header{background:var(--bg2);border-bottom:2px solid var(--border);padding:20px 20px 16px}
.header h1{font-size:22px;font-weight:800;margin-bottom:4px;color:var(--text)}
.header .meta{color:var(--text2);font-size:15px;margin-bottom:10px}
.members{display:flex;flex-wrap:wrap;gap:8px}
.member-chip{background:var(--accent-soft);color:var(--accent);border:1px solid var(--accent);border-radius:20px;padding:5px 14px;font-size:15px;font-weight:600}
.container{max-width:480px;margin:0 auto;padding:20px 16px 40px}
.section{margin-bottom:28px}
.section-title{font-size:13px;font-weight:800;text-transform:uppercase;letter-spacing:1.5px;color:var(--text3);margin-bottom:14px}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:20px;box-shadow:var(--card-shadow)}
.input-nick,.input-desc{width:100%;padding:15px 18px;background:var(--bg3);border:2px solid var(--border);border-radius:12px;color:var(--text);font-size:17px;outline:none;transition:border .2s}
.input-nick:focus,.input-desc:focus,.input-amount:focus{border-color:var(--accent)}
.form-row{margin-bottom:14px}
.form-row-inline{display:flex;gap:10px;margin-bottom:14px}
.amount-wrapper{flex:1;position:relative}
.euro-sign{position:absolute;left:16px;top:50%;transform:translateY(-50%);color:var(--text3);font-weight:700;font-size:18px;z-index:1}
.input-amount{width:100%;padding:15px 18px 15px 38px;background:var(--bg3);border:2px solid var(--border);border-radius:12px;color:var(--text);font-size:17px;outline:none}
.input-payer{flex:1;padding:15px 14px;background:var(--bg3);border:2px solid var(--border);border-radius:12px;color:var(--text);font-size:16px;outline:none;-webkit-appearance:none;appearance:none;background-image:url("data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='14' height='14' fill='%23888'%3E%3Cpath d='M7 10L2 5h10z'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 14px center}
.btn-primary{width:100%;padding:16px;background:var(--accent);border:none;border-radius:12px;color:#fff;font-size:17px;font-weight:700;cursor:pointer;transition:opacity .15s}
.btn-primary:active{opacity:.85}
.bal-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.bal-card{background:var(--bg2);border:2px solid var(--border);border-radius:var(--radius);padding:16px;text-align:center;transition:border-color .2s;position:relative}
.bal-card.is-me{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent-soft)}
.bal-card.green{border-color:var(--green);background:var(--green-bg)}
.bal-card.red{border-color:var(--red);background:var(--red-bg)}
.bal-card.neutral{border-color:var(--border)}
.bal-name{font-weight:700;font-size:17px;margin-bottom:4px}
.bal-detail{font-size:14px;color:var(--text2);margin-bottom:6px}
.bal-label{font-size:17px;font-weight:700}
.bal-card.green .bal-label{color:var(--green-text)}
.bal-card.red .bal-label{color:var(--red-text)}
.bal-card.neutral .bal-label{color:var(--text2)}
.bal-you-tag{position:absolute;top:8px;right:10px;font-size:11px;font-weight:700;color:var(--accent);background:var(--accent-soft);border-radius:6px;padding:2px 7px}
.settle-row{display:flex;align-items:center;padding:14px 18px;background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);margin-bottom:10px;gap:10px}
.settle-from{flex:1;font-weight:700;color:var(--red-text);font-size:17px}
.settle-arrow{color:var(--text3);font-size:20px}
.settle-to{flex:1;font-weight:700;color:var(--green-text);font-size:17px;text-align:right}
/* Expenses */
.expense-card { background: var(--bg2); border: 1px solid var(--border); border-radius: var(--radius); padding: 14px 16px; margin-bottom: 10px; position: relative; }
.expense-desc { font-weight: 600; font-size: 17px; }
.expense-info { flex: 1; min-width: 0; }
.expense-meta-row { display: flex; align-items: baseline; gap: 8px; margin-top: 2px; }
.expense-payer { font-size: 14px; color: var(--text2); }
.expense-time { font-size: 12px; color: var(--text3); }
.expense-amount { font-weight: 800; font-size: 20px; color: var(--green-text); white-space: nowrap; cursor: pointer; padding: 2px 6px; border-radius: 6px; transition: background .15s; }
.expense-amount:hover { background: var(--green-bg); }
.expense-amount.editing { background: var(--bg3); outline: none; min-width: 60px; }
.amount-edit-input { font-weight: 800; font-size: 20px; color: var(--green-text); background: var(--bg3); border: 2px solid var(--accent); border-radius: 6px; padding: 2px 6px; width: 90px; text-align: right; outline: none; }
.desc-edit-input { font-weight: 600; font-size: 17px; color: var(--text); background: var(--bg3); border: 2px solid var(--accent); border-radius: 6px; padding: 2px 8px; outline: none; width: 100%; }
.expense-row { display: flex; align-items: center; gap: 12px; }
.expense-right { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }

/* Receipt */
.receipt-box { flex-shrink: 0; }
.receipt-btn { background: var(--bg3); border: 1px solid var(--border); border-radius: 10px; font-size: 15px; cursor: pointer; padding: 6px 8px; transition: all .15s; color: var(--text3); line-height: 1; }
.receipt-btn:hover { background: var(--accent-soft); color: var(--accent); border-color: var(--accent); }
.receipt-btn.has-photo { color: var(--accent); border-color: var(--accent); background: var(--accent-soft); }
.receipt-modal img{max-width:90vw;max-height:70vh;border-radius:12px;object-fit:contain}
.receipt-modal-actions{display:flex;gap:12px;margin-top:16px;flex-wrap:wrap;justify-content:center}
.receipt-modal-btn{padding:12px 24px;border-radius:12px;font-size:16px;font-weight:700;cursor:pointer;border:none;color:#fff}
.receipt-modal-btn.upload{background:var(--accent)}
.receipt-modal-btn.camera{background:#00C9A7}
.receipt-modal-btn.delete{background:var(--red)}
.receipt-modal-btn.close{background:#555}
.receipt-modal-title{color:#fff;font-size:18px;font-weight:700;margin-bottom:16px;text-align:center}
.receipt-upload-area{border:2px dashed #666;border-radius:16px;padding:32px;text-align:center;cursor:pointer;transition:all .2s}
.receipt-upload-area:hover{border-color:var(--accent);background:rgba(108,92,231,.1)}
.receipt-upload-area-icon{font-size:48px;margin-bottom:8px}
.receipt-upload-area-text{color:#aaa;font-size:16px}
.total-bar{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:16px 20px;margin-top:14px}
.total-row{display:flex;justify-content:space-between;align-items:center}
.total-label{font-weight:700;font-size:17px}
.total-value{font-weight:800;font-size:20px}
.total-value.main{color:var(--accent)}
.total-divider{border:none;border-top:1px dashed var(--border);margin:10px 0}
.total-sub{display:flex;justify-content:space-between;align-items:center;color:var(--text2);font-size:15px}
.del-form{display:inline;margin:0;padding:0}
.del-btn{position:absolute;top:10px;right:10px;background:var(--bg3);border:1px solid var(--border);border-radius:8px;color:var(--text3);cursor:pointer;font-size:16px;padding:4px 9px;line-height:1;transition:all .15s}
.del-btn:hover{color:var(--red);border-color:var(--red);background:var(--red-bg)}
.share-box{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:24px;text-align:center}
.share-btn{display:inline-block;padding:15px 36px;background:var(--accent);border-radius:12px;color:#fff;font-weight:700;font-size:17px;text-decoration:none;margin:14px 0;cursor:pointer;border:none}
.share-btn:active{opacity:.85}
.share-link{word-break:break-all;color:var(--accent);font-size:14px;margin-top:10px;cursor:pointer;padding:8px;border-radius:8px;transition:background .2s}
.share-link:hover{background:var(--accent-soft)}
.empty{color:var(--text3);text-align:center;padding:24px;font-size:16px}
.field-label{display:block;color:var(--text2);font-size:14px;margin-bottom:6px;font-weight:600}
.toast{position:fixed;bottom:30px;left:50%;transform:translateX(-50%);background:var(--accent);color:#fff;padding:14px 28px;border-radius:14px;font-weight:700;font-size:16px;opacity:0;transition:opacity .3s;z-index:999;pointer-events:none}
.toast.show{opacity:1}
.index-wrap{min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:24px}
.index-box{max-width:420px;width:100%}
.index-title{font-size:32px;text-align:center;margin-bottom:4px;font-weight:900;background:linear-gradient(135deg,var(--accent2),var(--accent));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.index-sub{text-align:center;color:var(--text2);margin-bottom:32px;font-size:18px}
</style>"""

# ─── TEMPLATES ────────────────────────────────────────────────────────────────

INDEX_HTML = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>SplitEase</title>
<script>const t=localStorage.getItem('se_theme')||'light';document.documentElement.setAttribute('data-theme',t);</script>
""" + THEME_CSS_FULL + """
</head>
<body>
<div class="index-wrap">
  <div class="index-box">
    <div style="text-align:right;margin-bottom:8px">
      <button class="theme-btn" onclick="toggleTheme()" id="themeBtn1"></button>
    </div>
    <h1 class="index-title">SplitEase</h1>
    <p class="index-sub">Dividi le spese con chi vuoi 💸</p>
    <div class="card">
      <form id="createForm">
        <div class="form-row">
          <label class="field-label">Nome della vacanza / gruppo</label>
          <input type="text" id="groupName" placeholder="es. Weekend al mare" required maxlength="50" class="input-desc">
        </div>
        <div class="form-row">
          <label class="field-label">Il tuo nome</label>
          <input type="text" id="nickName" placeholder="es. Mario" required maxlength="20" class="input-nick">
        </div>
        <div style="color:var(--text3);text-align:center;font-size:15px;margin:18px 0 12px;font-weight:600">Prima spesa (opzionale)</div>
        <div class="form-row">
          <input type="text" id="firstDesc" placeholder="Cosa hai pagato?" class="input-desc">
        </div>
        <div class="form-row">
          <div class="amount-wrapper">
            <span class="euro-sign">€</span>
            <input type="number" id="firstAmount" placeholder="0,00" step="0.01" min="0.01" class="input-amount">
          </div>
        </div>
        <button type="submit" class="btn-primary">Crea gruppo e inizia →</button>
      </form>
      <p style="color:var(--text3);text-align:center;font-size:15px;margin-top:20px">Oppure entra in un gruppo con il link condiviso da un amico</p>
    </div>
  </div>
</div>
<script>
function toggleTheme(){const h=document.documentElement,m=localStorage.getItem('se_theme')||'light',n=m==='light'?'dark':'light';h.setAttribute('data-theme',n);localStorage.setItem('se_theme',n);updateBtn();}
function updateBtn(){const d=document.documentElement.getAttribute('data-theme');document.getElementById('themeBtn1').textContent=d==='dark'?'☀️ Light':'🌙 Dark';const b=document.getElementById('themeBtn2');if(b)b.textContent=d==='dark'?'☀️ Light':'🌙 Dark';}
updateBtn();
document.getElementById('createForm').addEventListener('submit',async(e)=>{e.preventDefault();const b={group_name:document.getElementById('groupName').value.trim(),nickname:document.getElementById('nickName').value.trim(),description:document.getElementById('firstDesc').value.trim(),amount:document.getElementById('firstAmount').value||0};if(!b.group_name||!b.nickname)return;const btn=e.target.querySelector('button[type=submit]');btn.disabled=true;btn.textContent='Creazione...';try{const r=await fetch('/api/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});const d=await r.json();document.cookie='se_token='+d.token+';path=/;max-age='+(365*86400)+';SameSite=Lax';window.location.href=d.url;}catch(err){btn.disabled=false;btn.textContent='Crea gruppo e inizia →';alert('Errore, riprova');}});
</script>
</body>
</html>"""

APP_TEMPLATE = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>SplitEase — {{ group_name }}</title>
<script>const t=localStorage.getItem('se_theme')||'light';document.documentElement.setAttribute('data-theme',t);</script>
""" + THEME_CSS_FULL + """
</head>
<body>
<div class="theme-toggle">
  <button class="theme-btn" onclick="toggleTheme()" id="themeBtn2"></button>
</div>

<div class="header">
  <h1>💸 {{ group_name }}</h1>
  <div class="meta">{{ n_members }} persone · {{ n_expenses }} spese · Totale: € {{ total }}</div>
  <div class="members">
    {% for m in members %}
    <span class="member-chip">{{ m.nickname }}{% if member and m.id == member.id %} ✓{% endif %}</span>
    {% endfor %}
  </div>
</div>

<div class="container">
  {% if member %}
  <div class="section">
    <div class="section-title">Aggiungi spesa</div>
    <div class="card">
      <form method="POST" action="/g/{{ group_id }}/expense">
        <div class="form-row">
          <input type="text" name="description" placeholder="Cosa hai pagato? (es. cena, benzina...)" required class="input-desc" maxlength="100">
        </div>
        <div class="form-row-inline">
          <div class="amount-wrapper">
            <span class="euro-sign">€</span>
            <input type="number" name="amount" placeholder="0,00" step="0.01" min="0.01" required class="input-amount">
          </div>
          <select name="payer_id" class="input-payer">
            {% for m in members %}
            <option value="{{ m.id }}"{% if m.id == member.id %} selected{% endif %}>{{ m.nickname }}</option>
            {% endfor %}
          </select>
        </div>
        <button type="submit" class="btn-primary">Aggiungi spesa 💰</button>
      </form>
    </div>
  </div>
  {% else %}
  <div class="section">
    <div class="section-title">Entra nel gruppo</div>
    <div class="card">
      <h2 style="margin-bottom:12px;font-size:20px;">🎉 Entra in "<b>{{ group_name }}</b>"</h2>
      <p style="color:var(--text2);font-size:16px;margin-bottom:16px">Scegli il tuo nickname per partecipare alle spese</p>
      <form method="POST" action="/g/{{ group_id }}/join">
        <input type="text" name="nickname" placeholder="Il tuo nome" required autofocus maxlength="20" class="input-nick">
        <button type="submit" class="btn-primary" style="margin-top:14px">Entra nel gruppo →</button>
      </form>
    </div>
  </div>
  {% endif %}

  <!-- Balances -->
  <div class="section">
    <div class="section-title">Saldi</div>
    <div class="bal-grid">
      {% if balances %}
        {% for mid, info in balances.items() %}
        {% if info.balance > 0.01 %}
          {% set color = "green" %}
          {% set label = "Riceve € " ~ fmt(info.balance) %}
          {% set emoji = "📥" %}
        {% elif info.balance < -0.01 %}
          {% set color = "red" %}
          {% set label = "Deve dare € " ~ fmt(info.balance|abs) %}
          {% set emoji = "📤" %}
        {% else %}
          {% set color = "neutral" %}
          {% set label = "Pari ✅" %}
          {% set emoji = "🤝" %}
        {% endif %}
        <div class="bal-card {{ color }}{% if member and mid == member.id %} is-me{% endif %}">
          {% if member and mid == member.id %}<span class="bal-you-tag">tu</span>{% endif %}
          <div class="bal-name">{{ info.nickname }}</div>
          <div class="bal-detail">Ha pagato: € {{ fmt(info.paid) }}</div>
          <div class="bal-label">{{ emoji }} {{ label }}</div>
        </div>
        {% endfor %}
      {% else %}
        <div class="empty" style="grid-column:1/-1">Aggiungi membri e spese per vedere i saldi</div>
      {% endif %}
    </div>
  </div>

  <!-- Settlements -->
  {% if settlements %}
  <div class="section">
    <div class="section-title">Chi paga chi</div>
    {% for from_n, to_n, amt in settlements %}
    <div class="settle-row">
      <span class="settle-from">{{ from_n }}</span>
      <span class="settle-arrow">→</span>
      <span class="settle-to">{{ to_n }}</span>
      <span class="settle-amount">€ {{ fmt(amt) }}</span>
    </div>
    {% endfor %}
  </div>
  {% elif balances %}
  <div class="section">
    <div class="section-title">Chi paga chi</div>
    <div class="card empty">Tutti pari, nessun saldo da sistemare! 🎉</div>
  </div>
  {% endif %}

  <!-- Expenses list -->
  <div class="section">
    <div class="section-title">Spese</div>
    {% if expenses %}
      {% for e in expenses %}
      <div class="expense-card" id="expense-{{ e.id }}">
        {% if member and e.payer_id == member.id %}
        <form method="POST" action="/g/{{ group_id }}/delete/{{ e.id }}" class="del-form" onsubmit="return confirm('Cancella questa spesa?')">
          <button type="submit" class="del-btn" title="Cancella">✕</button>
        </form>
        {% endif %}
        <div class="expense-row">
          <div class="expense-info">
            <div class="expense-desc" id="desc-{{ e.id }}" {% if member and e.payer_id == member.id %}onclick="editDesc('{{ e.id }}', '{{ e.description|e }}')"{% endif %} {% if member and e.payer_id == member.id %}style="cursor:pointer;border-radius:4px;padding:0 4px;transition:background .15s;" onmouseover="this.style.background='var(--bg3)'" onmouseout="this.style.background='transparent'"{% endif %}>{{ e.description }}</div>
            <div class="expense-meta-row">
              <span class="expense-payer">👤 {{ e.payer_name }}</span>
              <span class="expense-time">{{ e.created_at[:16].replace('T', ' ') }}</span>
            </div>
          </div>
          <div class="expense-right">
            <span class="expense-amount" id="amt-{{ e.id }}" {% if member and e.payer_id == member.id %}onclick="editAmount('{{ e.id }}', {{ e.amount }})"{% endif %}>€ {{ fmt(e.amount) }}</span>
            <div class="receipt-box">
              <button class="receipt-btn{% if e.receipt_path %} has-photo{% endif %}" onclick="openReceipt('{{ e.id }}', {{ 'true' if e.receipt_path else 'false' }})" title="Scontrino">📎</button>
            </div>
          </div>
        </div>
      </div>
      {% endfor %}

      <div class="total-bar">
        <div class="total-row">
          <span class="total-label">Totale spese</span>
          <span class="total-value main">€ {{ total }}</span>
        </div>
        <hr class="total-divider">
        <div class="total-sub">
          <span>Per persona ({{ n_members }})</span>
          <span style="font-weight:700">€ {{ per_person }}</span>
        </div>
      </div>
    {% else %}
      <div class="card empty">Nessuna spesa ancora — aggiungi la prima! 🎉</div>
    {% endif %}
  </div>

  <!-- Share -->
  <div class="section">
    <div class="section-title">Condividi il link 📱</div>
    <div class="share-box">
      <p style="color:var(--text2);font-size:16px;margin-bottom:8px">Invia questo link ai tuoi amici</p>
      <button class="share-btn" onclick="shareLink()">🔗 Condividi link</button>
      <div class="share-link" onclick="copyLink()" id="shareUrl">{{ share_url }}</div>
      <p style="color:var(--text3);font-size:13px;margin-top:10px">Tocca per copiare · Condivisione Instagram-style</p>
    </div>
  </div>
</div>

<!-- Receipt Modal -->
<div class="receipt-modal" id="receiptModal">
  <div class="receipt-modal-title" id="receiptModalTitle">Scontrino</div>
  <div id="receiptViewArea"></div>
  <div id="receiptUploadArea" style="display:none">
    <div class="receipt-upload-area" onclick="document.getElementById('receiptFileInput').click()">
      <div class="receipt-upload-area-icon">📷</div>
      <div class="receipt-upload-area-text">Tocca per allegare una foto<br><small>oppure scatta una foto dello scontrino</small></div>
    </div>
    <input type="file" id="receiptFileInput" accept="image/*" capture="environment" style="display:none" onchange="uploadReceipt()">
  </div>
  <div class="receipt-modal-actions" id="receiptActions"></div>
</div>

<div class="toast" id="toast"></div>

<script>
const GROUP_ID='{{ group_id }}';
let currentExpenseId=null,hasReceipt=false;

function toggleTheme(){const h=document.documentElement,m=localStorage.getItem('se_theme')||'light',n=m==='light'?'dark':'light';h.setAttribute('data-theme',n);localStorage.setItem('se_theme',n);updateBtn();}
function updateBtn(){const d=document.documentElement.getAttribute('data-theme');const b=document.getElementById('themeBtn2');if(b)b.textContent=d==='dark'?'☀️ Light':'🌙 Dark';}
updateBtn();

function openReceipt(eid,hasPhoto){
  currentExpenseId=eid;hasReceipt=hasPhoto;
  const modal=document.getElementById('receiptModal'),view=document.getElementById('receiptViewArea'),upload=document.getElementById('receiptUploadArea'),actions=document.getElementById('receiptActions'),title=document.getElementById('receiptModalTitle');
  if(hasReceipt){
    const rid=GROUP_ID+'_'+eid;
    title.textContent='Scontrino';
    view.innerHTML='<img src="/receipts/'+rid+'_bg.png?'+Date.now()+'" alt="Scontrino" style="max-width:90vw;max-height:70vh;border-radius:12px;">';
    view.style.display='';upload.style.display='none';
    actions.innerHTML='<button class="receipt-modal-btn delete" onclick="deleteReceipt()">🗑️ Elimina</button><button class="receipt-modal-btn close" onclick="closeReceipt()">✕ Chiudi</button>';
  }else{
    title.textContent='Aggiungi scontrino';view.style.display='none';upload.style.display='';
    actions.innerHTML='<button class="receipt-modal-btn close" onclick="closeReceipt()">✕ Annulla</button>';
  }
  modal.classList.add('active');
}
function closeReceipt(){document.getElementById('receiptModal').classList.remove('active');currentExpenseId=null;}
document.getElementById('receiptModal').addEventListener('click',function(e){if(e.target===this)closeReceipt();});

async function uploadReceipt(){
  const input=document.getElementById('receiptFileInput');
  if(!input.files||!input.files[0])return;
  const file=input.files[0];
  if(file.size>5*1024*1024){showToast('File troppo grande (max 5MB)');return;}
  const formData=new FormData();formData.append('receipt',file);
  try{
    const res=await fetch('/g/'+GROUP_ID+'/receipt/'+currentExpenseId,{method:'POST',body:formData});
    const data=await res.json();
    if(data.ok){showToast('Scontrino salvato! ✅');closeReceipt();setTimeout(()=>location.reload(),500);}
    else{showToast('Errore: '+(data.error||'upload fallito'));}
  }catch(err){showToast('Errore di connessione');}
  input.value='';
}

async function deleteReceipt(){
  try{
    const res=await fetch('/g/'+GROUP_ID+'/receipt/'+currentExpenseId,{method:'DELETE'});
    const data=await res.json();
    if(data.ok){showToast('Scontrino eliminato');closeReceipt();setTimeout(()=>location.reload(),500);}
  }catch(err){showToast('Errore di connessione');}
}

function showToast(msg){const t=document.getElementById('toast');t.textContent=msg||'✅';t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2000);}

// ── Inline editing ──
function editAmount(eid, currentVal){
  const el=document.getElementById('amt-'+eid);
  if(el.querySelector('input'))return; // already editing
  const raw=currentVal.toFixed(2).replace('.',',');
  el.innerHTML='<input type="number" class="amount-edit-input" value="'+currentVal.toFixed(2)+'" step="0.01" min="0.01" id="amtEdit-'+eid+'">';
  const inp=document.getElementById('amtEdit-'+eid);
  inp.focus();inp.select();
  const save=async()=>{
    const v=parseFloat(inp.value);
    if(isNaN(v)||v<=0){el.innerHTML='€ '+raw;return;}
    try{const r=await fetch('/api/expense/'+eid,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({group_id:GROUP_ID,amount:v})});
    const d=await r.json();if(d.ok){location.reload();}else{showToast('Errore: '+(d.error||'salvataggio fallito'));el.innerHTML='€ '+raw;}}catch(e){showToast('Errore di connessione');el.innerHTML='€ '+raw;}
  };
  inp.addEventListener('blur',save);
  inp.addEventListener('keydown',(e)=>{if(e.key==='Enter'){e.preventDefault();inp.blur();}if(e.key==='Escape'){el.innerHTML='€ '+raw;}});
}
function editDesc(eid, currentDesc){
  const el=document.getElementById('desc-'+eid);
  if(el.querySelector('input'))return;
  el.innerHTML='<input type="text" class="desc-edit-input" value="'+currentDesc.replace(/"/g,'&quot;')+'" maxlength="100" id="descEdit-'+eid+'">';
  const inp=document.getElementById('descEdit-'+eid);
  inp.focus();inp.select();
  const save=async()=>{
    const v=inp.value.trim();
    if(!v){el.textContent=currentDesc;return;}
    try{const r=await fetch('/api/expense/'+eid,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({group_id:GROUP_ID,description:v})});
    const d=await r.json();if(d.ok){location.reload();}else{showToast('Errore: '+(d.error||'salvataggio fallito'));el.textContent=currentDesc;}}catch(e){showToast('Errore di connessione');el.textContent=currentDesc;}
  };
  inp.addEventListener('blur',save);
  inp.addEventListener('keydown',(e)=>{if(e.key==='Enter'){e.preventDefault();inp.blur();}if(e.key==='Escape'){el.textContent=currentDesc;}});
}
function copyLink(){const url='{{ share_url }}';if(navigator.clipboard){navigator.clipboard.writeText(url).then(()=>showToast('Link copiato! ✅')).catch(()=>fallbackCopy(url));}else{fallbackCopy(url);}}
function fallbackCopy(url){const ta=document.createElement('textarea');ta.value=url;document.body.appendChild(ta);ta.select();try{document.execCommand('copy');showToast('Link copiato! ✅');}catch(e){}document.body.removeChild(ta);}
function shareLink(){const url='{{ share_url }}',text='💸 Unisciti a "{{ group_name }}" su SplitEase per dividere le spese!';if(navigator.share){navigator.share({title:'SplitEase — {{ group_name }}',text:text,url:url}).catch(()=>copyLink());}else{copyLink();}}
</script>
</body>
</html>"""

# ─── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5555))
    app.run(host='0.0.0.0', port=port, debug=False)