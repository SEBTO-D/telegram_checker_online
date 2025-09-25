#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import json
import os
import pathlib
from threading import Thread
from datetime import datetime, timezone as dt_timezone
from functools import wraps

import pandas as pd
from dotenv import load_dotenv
from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from pytz import common_timezones, timezone as pytz_timezone
from sqlalchemy import inspect, text as sa_text
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl import types as tl_types

from models import Config, Log, User, db

# ------------ helpers (config) ------------
load_dotenv()

EDITABLE_KEYS = [
    "TG_API_ID",
    "TG_API_HASH",
    "TG_PHONE",
    "CHECK_INTERVAL",
    "ADMIN_USER",
    "ADMIN_PASS",
    "FLASK_HOST",
    "FLASK_PORT",
    "FLASK_SECRET",
]

TIMEZONE_KEY = "DISPLAY_TIMEZONE"
AVAILABLE_TIMEZONES = list(common_timezones)
TIMEZONE_SET = set(AVAILABLE_TIMEZONES)

AUTH_STAGE_KEY = "AUTH_STAGE"   # none | code | password | authorized
LAST_CODE_SENT_KEY = "AUTH_LAST_CODE_AT"
AUTH_STAGE_FLOW = ["none", "code", "password", "authorized"]

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
        TIMEZONE_KEY: os.environ.get(TIMEZONE_KEY, "UTC"),
        AUTH_STAGE_KEY: "none",
        LAST_CODE_SENT_KEY: "",
    }


def ensure_log_columns():
    inspector = inspect(db.engine)
    columns = {col["name"] for col in inspector.get_columns("log")}
    altered = False
    if "status_code" not in columns:
        db.session.execute(sa_text("ALTER TABLE log ADD COLUMN status_code VARCHAR(32)"))
        altered = True
    if "was_online_at" not in columns:
        db.session.execute(sa_text("ALTER TABLE log ADD COLUMN was_online_at DATETIME"))
        altered = True
    if altered:
        db.session.commit()

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
    ensure_log_columns()
    for k, v in env_default_map().items():
        if not Config.query.filter_by(key=k).first():
            db.session.add(Config(key=k, value=v))
    db.session.commit()
    app.secret_key = get_cfg("FLASK_SECRET","secret")

# ------------ globals for telethon thread ------------
client = None
tele_loop = None
worker_task = None


def make_status_record(status):
    if status is None or isinstance(status, tl_types.UserStatusEmpty):
        return "Статус скрыт", "hidden", None
    if isinstance(status, tl_types.UserStatusOnline):
        return "Онлайн", "online", None
    if isinstance(status, tl_types.UserStatusOffline):
        last_seen = getattr(status, "was_online", None)
        if isinstance(last_seen, datetime) and last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=dt_timezone.utc)
        return "Оффлайн", "offline", last_seen
    if isinstance(status, tl_types.UserStatusRecently):
        return "Был недавно", "recently", None
    if isinstance(status, tl_types.UserStatusLastWeek):
        return "Был на прошлой неделе", "last_week", None
    if isinstance(status, tl_types.UserStatusLastMonth):
        return "Был в прошлом месяце", "last_month", None
    return str(status), "unknown", None


def get_timezone_obj():
    tz_name = get_cfg(TIMEZONE_KEY, "UTC") or "UTC"
    if tz_name not in TIMEZONE_SET:
        tz_name = "UTC"
    return pytz_timezone(tz_name)


def convert_to_timezone(dt, tz):
    if not dt:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=dt_timezone.utc)
    return dt.astimezone(tz)


def format_dt(dt, tz):
    dt = convert_to_timezone(dt, tz)
    return dt.strftime("%d.%m.%Y %H:%M:%S") if dt else "—"

async def worker_loop():
    global client
    while True:
        with app.app_context():
            interval = int(get_cfg("CHECK_INTERVAL","60") or "60")
            users = User.query.all()
        for u in users:
            try:
                entity = await client.get_entity(u.username)
                st_text, st_code, last_seen = make_status_record(getattr(entity, "status", None))
            except Exception as e:
                st_text, st_code, last_seen = f"Ошибка: {e}", "error", None
            with app.app_context():
                db.session.add(
                    Log(
                        user_id=u.id,
                        status=st_text,
                        status_code=st_code,
                        was_online_at=last_seen,
                    )
                )
                db.session.commit()
                print(f"[{datetime.now(dt_timezone.utc)}] {u.username}: {st_text}")
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
    if not phone:
        return False, "Телефон не указан в настройках"
    if client is None:
        return False, "Клиент Telegram ещё запускается. Попробуйте повторить через несколько секунд."
    try:
        if not client.is_connected():
            await client.connect()
        await client.send_code_request(phone)
        with app.app_context():
            set_cfg(AUTH_STAGE_KEY, "code")
            set_cfg(LAST_CODE_SENT_KEY, datetime.now(dt_timezone.utc).isoformat())
        return True, "Код отправлен"
    except FloodWaitError as e:
        wait_for = getattr(e, "seconds", None)
        if wait_for is None:
            return False, "Слишком частые запросы. Попробуйте позднее."
        return False, f"Слишком частые запросы. Повторите через {int(wait_for)} с."
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
        flash("Неверные данные", "danger")
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
    tz = get_timezone_obj()
    user_cards = []
    for u in users:
        last_log = Log.query.filter_by(user_id=u.id).order_by(Log.ts.desc()).first()
        if last_log:
            last_seen = format_dt(last_log.was_online_at, tz) if last_log.was_online_at else "—"
            updated_at = format_dt(last_log.ts, tz)
            status_code = last_log.status_code or "unknown"
            status_label = last_log.status or "—"
        else:
            last_seen = "—"
            updated_at = "—"
            status_code = "unknown"
            status_label = "Нет данных"
        user_cards.append(
            {
                "id": u.id,
                "username": u.username,
                "created_at": format_dt(u.created_at, tz),
                "status_code": status_code,
                "status_label": status_label,
                "last_seen": last_seen,
                "updated_at": updated_at,
            }
        )
    interval = get_cfg("CHECK_INTERVAL", "60")
    return render_template(
        "index.html",
        users=users,
        cards=user_cards,
        interval=interval,
        timezone_name=get_cfg(TIMEZONE_KEY, "UTC"),
    )

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
    tz = get_timezone_obj()
    chart_points = []
    prepared_logs = []
    for log in reversed(logs):
        ts_local = convert_to_timezone(log.ts, tz)
        if not ts_local:
            continue
        chart_points.append(
            {
                "x": ts_local.isoformat(),
                "y": 1 if (log.status_code == "online") else 0,
                "status": log.status,
            }
        )
    for log in logs:
        prepared_logs.append(
            {
                "ts": format_dt(log.ts, tz),
                "status": log.status,
                "code": log.status_code or "unknown",
                "last_seen": format_dt(log.was_online_at, tz) if log.was_online_at else "—",
            }
        )
    chart_dataset = {
        "label": f"@{u.username}",
        "data": chart_points,
    }
    return render_template(
        "logs.html",
        user=u,
        logs=prepared_logs,
        chart_data=json.dumps(chart_dataset, ensure_ascii=False),
        timezone_name=get_cfg(TIMEZONE_KEY, "UTC"),
    )

@app.route("/export")
@login_required
def export():
    rows=[];
    for l in Log.query.order_by(Log.ts.desc()).all():
        u=User.query.get(l.user_id)
        rows.append(
            (
                u.username if u else "deleted",
                l.ts,
                l.status,
                l.status_code or "",
                l.was_online_at.isoformat() if l.was_online_at else "",
            )
        )
    pd.DataFrame(
        rows,
        columns=["username", "timestamp", "status", "status_code", "was_online_at"],
    ).to_csv("export.csv",index=False)
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
        flash("Настройки сохранены", "success")
        return redirect(url_for("settings"))

    # состояние авторизации
    stage = get_cfg(AUTH_STAGE_KEY,"none")
    cfg = {k:get_cfg(k,"") for k in EDITABLE_KEYS}
    timezone_name = get_cfg(TIMEZONE_KEY, "UTC")
    tz = get_timezone_obj()
    last_code_sent_iso = get_cfg(LAST_CODE_SENT_KEY, "")
    last_code_sent = ""
    if last_code_sent_iso:
        formatted = format_dt(last_code_sent_iso, tz)
        last_code_sent = formatted if formatted != "—" else ""
    sorted_timezones = sorted(AVAILABLE_TIMEZONES)
    stage_flow = AUTH_STAGE_FLOW
    stage_index = stage_flow.index(stage) if stage in stage_flow else 0
    return render_template(
        "settings.html",
        cfg=cfg,
        keys=EDITABLE_KEYS,
        stage=stage,
        timezone_name=timezone_name,
        timezones=sorted_timezones,
        last_code_sent=last_code_sent,
        last_code_sent_iso=last_code_sent_iso,
        stage_flow=stage_flow,
        stage_index=stage_index,
    )


@app.post("/settings/timezone")
@login_required
def update_timezone():
    tz_name = request.form.get("timezone", "UTC")
    if tz_name not in TIMEZONE_SET:
        flash("Неверный часовой пояс", "danger")
    else:
        set_cfg(TIMEZONE_KEY, tz_name)
        flash("Часовой пояс обновлён", "success")
    return redirect(url_for("settings"))

# --- auth actions from settings ---
@app.post("/auth/send_code")
@login_required
def http_send_code():
    phone = get_cfg("TG_PHONE","")
    last_sent = get_cfg(LAST_CODE_SENT_KEY, "")
    if last_sent:
        try:
            last_dt = datetime.fromisoformat(last_sent)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=dt_timezone.utc)
        except ValueError:
            last_dt = None
        if last_dt:
            delta = datetime.now(dt_timezone.utc) - last_dt
            wait_left = 30 - int(delta.total_seconds())
            if wait_left > 0:
                flash(
                    f"Код уже отправляли недавно. Подождите ещё {wait_left} с, прежде чем повторить.",
                    "warning",
                )
                return redirect(url_for("settings"))
    fut = run_coro(do_send_code(phone))
    ok, msg = fut.result()
    flash(msg if msg else ("OK" if ok else "Ошибка"), "info" if ok else "danger")
    return redirect(url_for("settings"))

@app.post("/auth/submit_code")
@login_required
def http_submit_code():
    code = request.form.get("code","").strip()
    phone = get_cfg("TG_PHONE","")
    if not code:
        flash("Введите код", "warning"); return redirect(url_for("settings"))
    ok, msg = run_coro(do_sign_in_with_code(phone, code)).result()
    flash(msg, "success" if ok else "danger")
    return redirect(url_for("settings"))

@app.post("/auth/submit_password")
@login_required
def http_submit_password():
    pwd = request.form.get("password","")
    ok, msg = run_coro(do_sign_in_with_password(pwd)).result()
    flash(msg, "success" if ok else "danger")
    return redirect(url_for("settings"))

@app.post("/auth/logout")
@login_required
def http_logout():
    run_coro(do_logout()).result()
    flash("Сессия удалена", "info")
    return redirect(url_for("settings"))


@app.route("/analytics")
@login_required
def analytics():
    tz = get_timezone_obj()
    all_users = User.query.order_by(User.username.asc()).all()
    selected_ids = request.args.getlist("user_id", type=int)
    selected_set = set(selected_ids)
    datasets = []
    overlap_map = {}
    for user in all_users:
        if user.id not in selected_set:
            continue
        logs = (
            Log.query.filter_by(user_id=user.id)
            .order_by(Log.ts.desc())
            .limit(800)
            .all()
        )
        points = []
        for log in reversed(logs):
            ts_local = convert_to_timezone(log.ts, tz)
            if not ts_local:
                continue
            points.append(
                {
                    "x": ts_local.isoformat(),
                    "y": 1 if log.status_code == "online" else 0,
                    "status": log.status,
                }
            )
            if log.status_code == "online":
                bucket = ts_local.replace(second=0, microsecond=0)
                overlap_map.setdefault(bucket, set()).add(user.id)
        datasets.append(
            {
                "label": f"@{user.username}",
                "data": points,
            }
        )
    overlap_times = []
    if selected_set:
        for bucket, ids in sorted(overlap_map.items()):
            if selected_set.issubset(ids):
                overlap_times.append(
                    {
                        "ts": bucket.strftime("%d.%m.%Y %H:%M"),
                        "iso": bucket.isoformat(),
                    }
                )
    return render_template(
        "analytics.html",
        all_users=all_users,
        selected_ids=selected_set,
        chart_data=json.dumps(datasets, ensure_ascii=False),
        overlap_times=overlap_times,
        timezone_name=get_cfg(TIMEZONE_KEY, "UTC"),
    )

def start_background():
    t=Thread(target=telethon_thread, daemon=True)
    t.start()

if __name__=="__main__":
    start_background()
    with app.app_context():
        host = get_cfg("FLASK_HOST","0.0.0.0")
        port = int(get_cfg("FLASK_PORT","5000") or "5000")
    app.run(host=host, port=port, debug=True)
