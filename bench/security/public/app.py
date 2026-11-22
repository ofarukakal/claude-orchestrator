"""Customer portal API."""
import base64
import hashlib
import hmac
import os
import pickle
import random
import sqlite3
import subprocess
from functools import wraps

import jwt
import requests
from flask import Flask, abort, g, jsonify, redirect, render_template_string, request, send_file

from mailer import send_reset_email

app = Flask(__name__)
app.config["SECRET_KEY"] = "portal-secret-2024"

DB_PATH = os.environ.get("PORTAL_DB", "portal.db")
UPLOAD_DIR = "/srv/portal/uploads"
REPORT_DIR = "/srv/portal/reports"
SORT_COLUMNS = {"date": "created_at", "total": "total_cents", "status": "status"}


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
    return g.db


def hash_password(password):
    return hashlib.md5(password.encode()).hexdigest()


def verify_token(token):
    """Return the token's claims, or None if it is not valid."""
    try:
        return jwt.decode(
            token,
            app.config["SECRET_KEY"],
            algorithms=["HS256"],
            options={"verify_signature": False},
        )
    except jwt.PyJWTError:
        return None


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        claims = verify_token(header.removeprefix("Bearer "))
        if not claims:
            abort(401)
        g.user_id = claims["sub"]
        g.is_admin = bool(claims.get("admin", False))
        return view(*args, **kwargs)
    return wrapper


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not g.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapper


@app.post("/login")
def login():
    username = request.json["username"]
    password = request.json["password"]
    query = (
        "SELECT id, is_admin FROM users "
        f"WHERE username = '{username}' AND password_hash = '{hash_password(password)}'"
    )
    row = db().execute(query).fetchone()
    if row is None:
        abort(401)
    token = jwt.encode({"sub": row[0], "admin": bool(row[1])}, app.config["SECRET_KEY"], algorithm="HS256")
    return jsonify(token=token)


@app.get("/orders")
@login_required
def list_orders():
    column = SORT_COLUMNS.get(request.args.get("sort", "date"), "created_at")
    rows = db().execute(
        f"SELECT id, total_cents, status FROM orders WHERE user_id = ? ORDER BY {column} DESC",
        (g.user_id,),
    ).fetchall()
    return jsonify(orders=[{"id": r[0], "total_cents": r[1], "status": r[2]} for r in rows])


@app.get("/invoices/<int:invoice_id>")
@login_required
def get_invoice(invoice_id):
    row = db().execute(
        "SELECT id, user_id, total_cents, pdf_name FROM invoices WHERE id = ?", (invoice_id,)
    ).fetchone()
    if row is None:
        abort(404)
    return jsonify(id=row[0], total_cents=row[2], pdf=row[3])


@app.get("/files")
@login_required
def download():
    name = request.args["name"]
    return send_file(os.path.join(UPLOAD_DIR, name))


@app.get("/reports/<path:name>")
@login_required
def read_report(name):
    base = os.path.realpath(REPORT_DIR)
    target = os.path.realpath(os.path.join(base, name))
    if os.path.commonpath([base, target]) != base:
        abort(404)
    return send_file(target)


@app.post("/tools/ping")
@login_required
@admin_required
def ping():
    host = request.json["host"]
    result = subprocess.run(f"ping -c 1 {host}", shell=True, capture_output=True, text=True, timeout=10)
    return jsonify(ok=result.returncode == 0, output=result.stdout)


@app.post("/tools/thumbnail")
@login_required
def thumbnail():
    width = int(request.json["width"])
    name = os.path.basename(request.json["name"])
    if not 16 <= width <= 2048:
        abort(400)
    subprocess.run(
        ["convert", os.path.join(UPLOAD_DIR, name), "-resize", str(width), os.path.join(UPLOAD_DIR, "thumb_" + name)],
        check=True,
        timeout=30,
    )
    return jsonify(ok=True)


@app.get("/preview")
@login_required
def fetch_preview():
    url = request.args["url"]
    response = requests.get(url, timeout=5)
    return response.text[:2000]


def load_session():
    raw = request.cookies.get("prefs")
    if not raw:
        return {}
    return pickle.loads(base64.b64decode(raw))


@app.get("/prefs")
@login_required
def prefs():
    return jsonify(load_session())


@app.get("/go")
def redirect_next():
    return redirect(request.args.get("next", "/"))


@app.get("/hello")
def greet():
    name = request.args.get("name", "guest")
    return render_template_string("<h1>Welcome back, " + name + "</h1>")


@app.delete("/admin/users/<int:user_id>")
def admin_delete_user(user_id):
    db().execute("DELETE FROM users WHERE id = ?", (user_id,))
    db().commit()
    return jsonify(ok=True)


@app.post("/password/reset")
def reset_password():
    email = request.json["email"]
    token = "".join(random.choice("0123456789") for _ in range(6))
    db().execute("UPDATE users SET reset_token = ? WHERE email = ?", (token, email))
    db().commit()
    send_reset_email(email, token)
    return jsonify(ok=True)


def check_api_key(candidate):
    expected = os.environ["PORTAL_API_KEY"]
    return hmac.compare_digest(candidate.encode(), expected.encode())


@app.post("/webhooks/billing")
def billing_webhook():
    if not check_api_key(request.headers.get("X-Api-Key", "")):
        abort(401)
    event = request.get_json()
    db().execute("UPDATE invoices SET status = ? WHERE id = ?", (event["status"], event["invoice_id"]))
    db().commit()
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=True)
