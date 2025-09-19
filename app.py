#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, asyncio, pathlib
from threading import Thread
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask, request, redirect, url_for, render_template, send_file, session, flash
from functools import wraps
import pandas as pd
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, PhoneCodeExpiredError, PhoneCodeInvalidError

from models import db, User, Log, Config

# ------------ helpers (config) ------------
load_dotenv()

EDITABLE_KEYS = [
    "TG_API_ID","TG_API_HASH","TG_PHONE",
    "CHECK_INTERVAL",
    "ADMIN_USER","ADMIN_PASS",
    "FLASK_HOST","FLASK_PORT","FLASK_SECRET"
]

AUTH_STAGE_KEY = "AUTH_STAGE"   # none | code | password | authorized

def env_default_map():
    return {
        "TG_API_ID": os.environ.get("TG_API_ID","0"),
        "TG_API_HASH": os.environ.get("TG_API_HASH",""),
        "TG_PHONE": os.environ.get("TG_PHONE",""),
        "CHECK_INTERVAL": os.environ.get("CHECK_INTERVAL","60"),
        "ADMIN_USER": os.environ.get("ADMIN_USER","admin"),
        "ADMIN_PASS": os.environ.get("ADMIN_PASS","secret"),
        "FLASK_HOST": os.environ.get("FLASK_HOST","0.0.0.0"),
        "FLASK_PORT": os.environ.get("FLASK_PORT","5000"),
        "FLASK_SECRET": os.environ.get("FLASK_SECRET","change_this_secret"),
        AUTH_STAGE_KEY: "none",
    }

def get_cfg(key, default=None):
    c = Config.query.filter_by(key=key).first()
    if c and c.value is not None:
        return c.value
    return env_default_map().get(key, default)

def set_cfg(key, value):
    c = Config.query.filter_by(key=key).first()
    if not c:
        c = Config(key=key, value=value)
        db.session.add(c)
    else:
        c.value = value
    db.session.commit()

# ------------ flask app ------------
app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///tracker.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)

with app.app_context():
    db.create_all()
    for k,v in env_default_map().items():
        if not Config.query.filter_by(key=k).first():
            db.session.add(Config(key=k, value=v))
    db.session.commit()
    app.secret_key = get_cfg("FLASK_SECRET","secret")

# ------------ globals for telethon thread ------------
client = None
tele_loop = None
worker_task = None

def status_to_text(status):
    if not status: return "скрыт/неизвестен"
    n = status.__class__.__name__
    if n=="UserStatusOnline": return "online"
    if n=="UserStatusOffline":
        try: return f"offline (был {status.was_online})"
        except: return "offline"
    if n=="UserStatusRecently": return "был недавно"
    if n=="UserStatusLastWeek": return "был на прошлой неделе"
    if n=="UserStatusLastMonth": return "был в прошлом месяце"
    return str(status)

async def worker_loop():
    global client
    while True:
        with app.app_context():
            interval = int(get_cfg("CHECK_INTERVAL","60") or "60")
            users = User.query.all()
        for u in users:
            try:
                entity = await client.get_entity(u.username)
                st_text = status_to_text(getattr(entity, "status", None))
            except Exception as e:
                st_text = f"ошибка: {e}"
            with app.app_context():
                db.session.add(Log(user_id=u.id, status=st_text))
                db.session.commit()
                print(f"[{datetime.utcnow()}] {u.username}: {st_text}")
        await asyncio.sleep(interval)

# run coroutine safely on tele_loop
def run_coro(coro):
    import concurrent.futures
    fut = asyncio.run_coroutine_threadsafe(coro, tele_loop)
    return fut

def start_worker_if_needed():
    global worker_task
    if tele_loop and (worker_task is None or worker_task.cancelled() or worker_task.done()):
        worker_task = asyncio.run_coroutine_threadsafe(worker_loop(), tele_loop)

def telethon_thread():
    global client, tele_loop
    tele_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(tele_loop)

    with app.app_context():
        api_id = int(get_cfg("TG_API_ID","0") or "0")
        api_hash = get_cfg("TG_API_HASH","")
        phone = get_cfg("TG_PHONE","")
        set_cfg(AUTH_STAGE_KEY, "none")

    client = TelegramClient("tracker_session", api_id, api_hash, loop=tele_loop)

    async def boot():
        # подключаемся и проверяем авторизацию
        await client.connect()
        if not await client.is_user_authorized():
            with app.app_context():
                set_cfg(AUTH_STAGE_KEY, "code")
            try:
                await client.send_code_request(phone)
                print("Код отправлен в Telegram. Введите его в Настройках.")
            except Exception as e:
                print("send_code_request error:", e)
        else:
            with app.app_context():
                set_cfg(AUTH_STAGE_KEY, "authorized")
            start_worker_if_needed()

    tele_loop.create_task(boot())
    try:
        tele_loop.run_forever()
    finally:
        tele_loop.close()

# ------------ auth helpers exposed via web ------------
async def do_sign_in_with_code(phone, code):
    try:
        await client.sign_in(phone=phone, code=code)
        with app.app_context():
            set_cfg(AUTH_STAGE_KEY, "authorized")
        start_worker_if_needed()
        return True, "Авторизация успешна"
    except SessionPasswordNeededError:
        with app.app_context():
            set_cfg(AUTH_STAGE_KEY, "password")
        return False, "Требуется пароль (2FA)"
    except PhoneCodeExpiredError:
        return False, "Код просрочен. Отправьте код заново."
    except PhoneCodeInvalidError:
        return False, "Неверный код."
    except Exception as e:
        return False, f"Ошибка: {e}"

async def do_sign_in_with_password(password):
    try:
        await client.sign_in(password=password)
        with app.app_context():
            set_cfg(AUTH_STAGE_KEY, "authorized")
        start_worker_if_needed()
        return True, "Авторизация успешна"
    except Exception as e:
        return False, f"Ошибка: {e}"

async def do_send_code(phone):
    try:
        await client.send_code_request(phone)
        with app.app_context():
            set_cfg(AUTH_STAGE_KEY, "code")
        return True, "Код отправлен"
    except Exception as e:
        return False, f"Ошибка отправки кода: {e}"

async def do_logout():
    try:
        await client.log_out()
    except Exception:
        pass
    # удалим файл сессии
    try:
        p = pathlib.Path("tracker_session.session")
        if p.exists(): p.unlink()
    except Exception:
        pass
    with app.app_context():
        set_cfg(AUTH_STAGE_KEY, "none")

# ------------ auth-protected pages ------------
def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*a, **kw)
    return wrapper

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        admin_user = get_cfg("ADMIN_USER","admin")
        admin_pass = get_cfg("ADMIN_PASS","secret")
        if (request.form.get("username")==admin_user and request.form.get("password")==admin_pass):
            session["logged_in"] = True
            return redirect(url_for("settings"))
        flash("Неверные данные")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ------------ routes ------------
@app.route("/")
@login_required
def index():
    users = User.query.order_by(User.created_at.desc()).all()
    last_status = {}
    for u in users:
        ls = Log.query.filter_by(user_id=u.id).order_by(Log.ts.desc()).first()
        last_status[u.id] = ls.status if ls else "—"
    return render_template("index.html", users=users, last_status=last_status, interval=get_cfg("CHECK_INTERVAL","60"))

@app.route("/add", methods=["POST"])
@login_required
def add():
    username = request.form.get("username","").strip().lstrip("@")
    if not username: return "Неверный ник",400
    if User.query.filter_by(username=username).first():
        return "Уже добавлен",400
    db.session.add(User(username=username)); db.session.commit()
    return redirect(url_for("index"))

@app.route("/delete/<int:uid>", methods=["POST"])
@login_required
def delete(uid):
    u=User.query.get_or_404(uid)
    db.session.delete(u); db.session.commit()
    return redirect(url_for("index"))

@app.route("/logs/<int:uid>")
@login_required
def logs(uid):
    u=User.query.get_or_404(uid)
    logs = Log.query.filter_by(user_id=u.id).order_by(Log.ts.desc()).limit(500).all()
    return render_template("logs.html", user=u, logs=logs)

@app.route("/export")
@login_required
def export():
    rows=[]; 
    for l in Log.query.order_by(Log.ts.desc()).all():
        u=User.query.get(l.user_id)
        rows.append((u.username if u else "deleted", l.ts, l.status))
    pd.DataFrame(rows, columns=["username","timestamp","status"]).to_csv("export.csv",index=False)
    return send_file("export.csv", as_attachment=True)

@app.route("/settings", methods=["GET","POST"])
@login_required
def settings():
    # сохранение обычных конфигов
    if request.method=="POST" and request.form.get("form")=="cfg":
        for k in EDITABLE_KEYS:
            v=request.form.get(k)
            set_cfg(k,v)
        app.secret_key = get_cfg("FLASK_SECRET","secret")
        flash("Настройки сохранены")
        return redirect(url_for("settings"))

    # состояние авторизации
    stage = get_cfg(AUTH_STAGE_KEY,"none")
    cfg = {k:get_cfg(k,"") for k in EDITABLE_KEYS}
    return render_template("settings.html", cfg=cfg, keys=EDITABLE_KEYS, stage=stage)

# --- auth actions from settings ---
@app.post("/auth/send_code")
@login_required
def http_send_code():
    phone = get_cfg("TG_PHONE","")
    fut = run_coro(do_send_code(phone))
    ok, msg = fut.result()
    flash(msg if msg else ("OK" if ok else "Ошибка"))
    return redirect(url_for("settings"))

@app.post("/auth/submit_code")
@login_required
def http_submit_code():
    code = request.form.get("code","").strip()
    phone = get_cfg("TG_PHONE","")
    if not code:
        flash("Введите код"); return redirect(url_for("settings"))
    ok, msg = run_coro(do_sign_in_with_code(phone, code)).result()
    flash(msg)
    return redirect(url_for("settings"))

@app.post("/auth/submit_password")
@login_required
def http_submit_password():
    pwd = request.form.get("password","")
    ok, msg = run_coro(do_sign_in_with_password(pwd)).result()
    flash(msg)
    return redirect(url_for("settings"))

@app.post("/auth/logout")
@login_required
def http_logout():
    run_coro(do_logout()).result()
    flash("Сессия удалена")
    return redirect(url_for("settings"))

def start_background():
    t=Thread(target=telethon_thread, daemon=True)
    t.start()

if __name__=="__main__":
    start_background()
    with app.app_context():
        host = get_cfg("FLASK_HOST","0.0.0.0")
        port = int(get_cfg("FLASK_PORT","5000") or "5000")
    app.run(host=host, port=port, debug=True)
