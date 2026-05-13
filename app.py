import os
import time
import math
import re
import json
import logging
import urllib.error
import urllib.request
from functools import wraps
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urljoin, urlencode
from flask import Flask, render_template, request, redirect, url_for, session, flash, g, jsonify
from flask_socketio import SocketIO, emit, join_room
from werkzeug.security import generate_password_hash, check_password_hash

from config import Config
from db import get_db, close_db, init_db, table_exists
from utils.auth import login_required
from utils.upload import save_post_media, save_profile_media
from utils.helpers import current_user, is_following

app = Flask(__name__)
app.config.from_object(Config)
logging.basicConfig(level=logging.INFO)
SESSION_TIMEOUT = timedelta(minutes=30)
CHAT_ONLY_MODE = os.getenv("CHAT_ONLY_MODE", "1") != "0"
app.permanent_session_lifetime = SESSION_TIMEOUT
app.teardown_appcontext(close_db)

# Real-time support. PythonAnywhere's standard WSGI path is safest with
# threading mode and long-polling only, so WebSocket upgrades are disabled.
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
    transports=["polling"],
    allow_upgrades=False,
    logger=True,
    engineio_logger=True,
    ping_interval=10,
    ping_timeout=5,
)
online_user_locations = {}
online_users = {}
sid_to_user = {}
EMERGENCY_RADIUS_KM = 25
EMERGENCY_COOLDOWN_SECONDS = 180
PHONE_PATTERN = re.compile(r"^[0-9+\-\s()]{7,20}$")
COORDINATE_TEXT_PATTERN = re.compile(r"^Lat\s+-?\d+(\.\d+)?,\s+Lng\s+-?\d+(\.\d+)?$", re.IGNORECASE)

os.makedirs(app.config["UPLOAD_FOLDER_POSTS"], exist_ok=True)
os.makedirs(app.config["UPLOAD_FOLDER_PROFILES"], exist_ok=True)
os.makedirs(os.path.join(app.root_path, "instance"), exist_ok=True)


def socket_log(message, **details):
    """Write Socket.IO lifecycle details into PythonAnywhere error logs."""
    details = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **details}
    detail_text = " ".join(f"{key}={value}" for key, value in details.items())
    app.logger.info("[socketio] %s%s", message, f" {detail_text}" if detail_text else "")


def admin_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not session.get("admin_logged_in"):
            flash("Admin login required.", "danger")
            return redirect(url_for("admin_login"))
        if session_has_expired():
            clear_login_session()
            flash("Session expired. Please login again.", "warning")
            return redirect(url_for("admin_login"))
        refresh_session_activity()
        return view(*args, **kwargs)
    return wrapped_view


def is_safe_redirect_url(target):
    """Only allow redirects inside this FamShare app."""
    if not target:
        return False

    host_url = urlparse(request.host_url)
    redirect_url = urlparse(urljoin(request.host_url, target))
    return (
        redirect_url.scheme in ("http", "https")
        and host_url.netloc == redirect_url.netloc
    )


def get_login_redirect_target(default_endpoint="chat_list"):
    next_url = request.args.get("next") or request.form.get("next")
    if is_safe_redirect_url(next_url):
        return next_url
    return url_for(default_endpoint)


def safe_login_next_value():
    next_url = request.args.get("next") or request.form.get("next") or ""
    return next_url if is_safe_redirect_url(next_url) else ""


def password_matches(stored_password_hash, candidate_password):
    """Return False for missing or invalid hashes instead of crashing login."""
    if not stored_password_hash or not candidate_password:
        return False

    try:
        return check_password_hash(stored_password_hash, candidate_password)
    except (TypeError, ValueError):
        return False


def login_user_session(user):
    """Create one clean browser session for a normal FamShare user."""
    session.clear()
    session.permanent = False
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["last_activity"] = time.time()


def clear_login_session():
    """Remove user/admin identity to avoid mixed login states."""
    session.clear()


def session_has_expired():
    """Return True when a logged-in user has been inactive too long."""
    last_activity = session.get("last_activity")
    if not last_activity:
        return False

    try:
        inactive_seconds = time.time() - float(last_activity)
    except (TypeError, ValueError):
        return True

    return inactive_seconds > SESSION_TIMEOUT.total_seconds()


def refresh_session_activity():
    """Extend the active session after a valid authenticated request."""
    session.permanent = False
    session["last_activity"] = time.time()


CHAT_ENDPOINTS = {
    "chat_list",
    "start_chat",
    "chat_room",
    "send_message_fallback",
    "chat_messages_since",
    "mark_chat_read",
    "edit_message",
    "delete_message",
    "forward_message",
}

CHAT_ONLY_ENDPOINTS = {
    "static",
    "index",
    "register",
    "login",
    "logout",
    *CHAT_ENDPOINTS,
}


def ensure_emergency_tables():
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS emergency_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            emergency_type TEXT NOT NULL,
            description TEXT DEFAULT '',
            location_text TEXT DEFAULT '',
            contact_number TEXT DEFAULT '',
            optional_contact_number TEXT DEFAULT '',
            severity TEXT DEFAULT 'Medium',
            ai_guidance TEXT DEFAULT '',
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            resolved_at TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS emergency_responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_id INTEGER NOT NULL,
            responder_id INTEGER NOT NULL,
            response_type TEXT NOT NULL,
            message TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(alert_id, responder_id, response_type),
            FOREIGN KEY (alert_id) REFERENCES emergency_alerts(id) ON DELETE CASCADE,
            FOREIGN KEY (responder_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS alert_locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            latitude REAL,
            longitude REAL,
            location_text TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (alert_id) REFERENCES emergency_alerts(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS alert_status (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            changed_by INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (alert_id) REFERENCES emergency_alerts(id) ON DELETE CASCADE,
            FOREIGN KEY (changed_by) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS emergency_chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_id INTEGER NOT NULL,
            sender_id INTEGER NOT NULL,
            message_text TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (alert_id) REFERENCES emergency_alerts(id) ON DELETE CASCADE,
            FOREIGN KEY (sender_id) REFERENCES users(id) ON DELETE CASCADE
        );
        """
    )
    db.commit()


def ensure_column(table_name, column_name, definition):
    db = get_db()
    columns = db.execute(f"PRAGMA table_info({table_name})").fetchall()
    if column_name not in {column["name"] for column in columns}:
        db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")
        db.commit()


def ensure_database_columns():
    if table_exists("users"):
        ensure_column("users", "phone_number", "TEXT DEFAULT ''")
        ensure_column("users", "optional_phone_number", "TEXT DEFAULT ''")
    if table_exists("emergency_alerts"):
        ensure_column("emergency_alerts", "optional_contact_number", "TEXT DEFAULT ''")


def ensure_chat_schema():
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            phone_number TEXT NOT NULL DEFAULT '',
            optional_phone_number TEXT DEFAULT '',
            password_hash TEXT NOT NULL,
            bio TEXT DEFAULT '',
            profile_pic TEXT DEFAULT 'default.png',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user1_id INTEGER NOT NULL,
            user2_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user1_id, user2_id),
            FOREIGN KEY (user1_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (user2_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            sender_id INTEGER NOT NULL,
            message_text TEXT NOT NULL,
            client_message_id TEXT DEFAULT '',
            is_read INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
            FOREIGN KEY (sender_id) REFERENCES users(id) ON DELETE CASCADE
        );
        """
    )
    ensure_column("messages", "client_message_id", "TEXT DEFAULT ''")
    db.commit()


def is_valid_phone_number(phone_number, required=True):
    if not phone_number:
        return not required
    return bool(PHONE_PATTERN.fullmatch(phone_number))


def reverse_geocode_location(latitude, longitude):
    if latitude is None or longitude is None:
        return ""

    query = urlencode(
        {
            "format": "jsonv2",
            "lat": f"{latitude:.7f}",
            "lon": f"{longitude:.7f}",
            "zoom": 18,
            "addressdetails": 1,
        }
    )
    request_url = f"https://nominatim.openstreetmap.org/reverse?{query}"
    request_obj = urllib.request.Request(
        request_url,
        headers={
            "User-Agent": "FamShare emergency location lookup",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request_obj, timeout=4) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return ""

    display_name = (data.get("display_name") or "").strip()
    if display_name:
        return display_name

    address = data.get("address") or {}
    parts = [
        address.get("road"),
        address.get("neighbourhood") or address.get("suburb"),
        address.get("city") or address.get("town") or address.get("village"),
        address.get("state"),
        address.get("postcode"),
    ]
    return ", ".join(part for part in parts if part)


def distance_km(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return None
    radius = 6371
    dlat = math.radians(float(lat2) - float(lat1))
    dlon = math.radians(float(lon2) - float(lon1))
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(float(lat1)))
        * math.cos(math.radians(float(lat2)))
        * math.sin(dlon / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def emergency_ai_assistance(emergency_type, description):
    text = f"{emergency_type} {description}".lower()
    severity = "Medium"
    if any(word in text for word in ["bleeding", "unconscious", "accident", "threat", "fire", "disaster", "urgent"]):
        severity = "High"
    if "blood" in text:
        severity = "Medical Priority"

    guidance = [
        "Stay calm and move to a safer place if you can.",
        "Call local emergency services immediately if there is danger to life.",
        "Share clear location details and keep your phone reachable.",
    ]
    if "medical" in text or "blood" in text or "bleeding" in text:
        guidance.append("For first aid: apply steady pressure to bleeding and avoid moving injured people unless unsafe.")
    if "accident" in text:
        guidance.append("For accidents: turn on hazard lights, avoid crowds near traffic, and call ambulance/police support.")
    if "threat" in text:
        guidance.append("For safety threats: avoid confrontation, move toward public/lighted areas, and contact police.")
    if "disaster" in text:
        guidance.append("For disasters: follow official alerts and keep away from unstable structures or flood water.")
    guidance.append("India emergency helpline: 112. Ambulance: 108. Fire: 101. Police: 100.")
    return severity, " ".join(guidance)


def emergency_payload(alert):
    return {
        "id": alert["id"],
        "user_id": alert["user_id"],
        "username": alert["username"],
        "emergency_type": alert["emergency_type"],
        "description": alert["description"],
        "location_text": alert["location_text"],
        "contact_number": alert["contact_number"],
        "optional_contact_number": alert["optional_contact_number"],
        "severity": alert["severity"],
        "ai_guidance": alert["ai_guidance"],
        "created_at": alert["created_at"],
    }


@app.context_processor
def inject_globals():
    user = getattr(g, "current_user", None)
    return {"current_user_data": user}


@app.before_request
def ensure_database_ready():
    if app.config.get("_CHAT_SCHEMA_READY"):
        return

    try:
        ensure_chat_schema()
        ensure_database_columns()
        app.config["_CHAT_SCHEMA_READY"] = True
    except Exception as e:
        print("Database init error:", e)


@app.before_request
def load_logged_in_user():
    """Validate the session before any route uses session['user_id']."""
    g.current_user = None

    if request.endpoint == "static":
        return

    if not session.get("user_id"):
        session.pop("username", None)
        if not session.get("admin_logged_in"):
            session.pop("last_activity", None)
        return

    if session_has_expired():
        clear_login_session()
        flash("Session expired. Please login again.", "warning")
        if request.endpoint != "login":
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        return

    user = current_user()
    if user is None:
        clear_login_session()
        session.modified = True
        return

    session["username"] = user["username"]
    refresh_session_activity()
    g.current_user = user


@app.after_request
def prevent_authenticated_page_cache(response):
    """Avoid showing old user sidebar data from browser cache after logout."""
    if request.endpoint != "static":
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.before_request
def require_login_for_chat_pages():
    """Extra safety: chat routes must always have a validated user."""
    if request.endpoint in CHAT_ENDPOINTS and not g.current_user:
        flash("Please login first to use messages.", "warning")
        return redirect(url_for("login", next=request.full_path.rstrip("?")))


@app.before_request
def redirect_non_chat_pages():
    if (
        CHAT_ONLY_MODE
        and request.endpoint
        and request.endpoint not in CHAT_ONLY_ENDPOINTS
    ):
        if g.current_user:
            return redirect(url_for("chat_list"))
        return redirect(url_for("login"))


def get_or_create_conversation(user_a, user_b):
    db = get_db()
    u1, u2 = sorted([int(user_a), int(user_b)])

    conversation = db.execute(
        """
        SELECT * FROM conversations
        WHERE user1_id = ? AND user2_id = ?
        """,
        (u1, u2),
    ).fetchone()

    if conversation:
        return conversation["id"]

    db.execute(
        """
        INSERT INTO conversations (user1_id, user2_id)
        VALUES (?, ?)
        """,
        (u1, u2),
    )
    db.commit()

    conversation = db.execute(
        """
        SELECT * FROM conversations
        WHERE user1_id = ? AND user2_id = ?
        """,
        (u1, u2),
    ).fetchone()

    return conversation["id"]


def get_conversation_for_user(conversation_id, user_id):
    """Return a conversation only when the signed-in user is a participant."""
    if not user_id:
        return None

    db = get_db()
    return db.execute(
        """
        SELECT *
        FROM conversations
        WHERE id = ?
          AND (user1_id = ? OR user2_id = ?)
        """,
        (conversation_id, user_id, user_id),
    ).fetchone()


def get_other_chat_user_id(conversation, user_id):
    """Return the other participant id for a one-to-one conversation."""
    return (
        int(conversation["user2_id"])
        if int(conversation["user1_id"]) == int(user_id)
        else int(conversation["user1_id"])
    )


def chat_room_name_for_users(user_a, user_b):
    """Stable private room shared by exactly two user ids."""
    u1, u2 = sorted([int(user_a), int(user_b)])
    return f"chat_{u1}_{u2}"


def chat_room_name_for_conversation(conversation):
    return chat_room_name_for_users(conversation["user1_id"], conversation["user2_id"])


def is_user_online(user_id):
    return bool(online_users.get(int(user_id)))


def message_delivery_status(message, conversation):
    """Best-effort status for the sender: sent, delivered, or read."""
    if message["is_read"]:
        return "read"

    recipient_id = get_other_chat_user_id(conversation, message["sender_id"])
    if is_user_online(recipient_id):
        return "delivered"

    return "sent"


def utc_iso_from_db(value):
    """Treat SQLite timestamp strings as UTC and send an explicit ISO value."""
    if not value:
        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace(" ", "T").replace("Z", "+00:00"))
    except ValueError:
        return text

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)

    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def chat_message_payload(message, conversation=None):
    receiver_id = None
    if conversation:
        receiver_id = get_other_chat_user_id(conversation, message["sender_id"])
    timestamp_utc = utc_iso_from_db(message["created_at"])

    return {
        "conversation_id": int(message["conversation_id"]),
        "sender_id": int(message["sender_id"]),
        "receiver_id": int(receiver_id) if receiver_id else None,
        "sender_name": message["username"],
        "message_text": message["message_text"],
        "content": message["message_text"],
        "created_at": message["created_at"],
        "timestamp": timestamp_utc,
        "timestamp_utc": timestamp_utc,
        "message_id": int(message["id"]),
        "id": int(message["id"]),
        "client_message_id": message["client_message_id"] if "client_message_id" in message.keys() else "",
        "status": message_delivery_status(message, conversation) if conversation else "sent",
    }


def fetch_chat_message(message_id):
    db = get_db()
    return db.execute(
        """
        SELECT messages.*, users.username
        FROM messages
        JOIN users ON messages.sender_id = users.id
        WHERE messages.id = ?
        """,
        (message_id,),
    ).fetchone()


def find_existing_client_message(conversation_id, sender_id, client_message_id):
    if not client_message_id:
        return None

    db = get_db()
    return db.execute(
        """
        SELECT messages.*, users.username
        FROM messages
        JOIN users ON messages.sender_id = users.id
        WHERE messages.conversation_id = ?
          AND messages.sender_id = ?
          AND messages.client_message_id = ?
        ORDER BY messages.id DESC
        LIMIT 1
        """,
        (conversation_id, sender_id, client_message_id),
    ).fetchone()


def followed_user_ids(user_id):
    if not user_id:
        return set()

    db = get_db()
    rows = db.execute(
        "SELECT following_id FROM follows WHERE follower_id = ?",
        (user_id,),
    ).fetchall()
    return {row["following_id"] for row in rows}


def socket_user():
    """Validate SocketIO identity from the Flask session."""
    if not session.get("user_id") or session_has_expired():
        clear_login_session()
        return None

    user = current_user()
    if not user:
        clear_login_session()
        return None
    session["username"] = user["username"]
    refresh_session_activity()
    return user


def profile_image_url(profile_pic):
    profile_pic = (profile_pic or "").strip()
    if not profile_pic or profile_pic in {"default.png", "default-avatar.png", "default-avatar.svg"}:
        return url_for("static", filename="img/default-avatar.svg")
    return url_for("static", filename=f"uploads/profiles/{profile_pic}")


app.jinja_env.globals["profile_image_url"] = profile_image_url


@app.route("/initdb")
def initdb_route():
    init_db()
    return "Database initialized successfully."


@app.route("/")
def index():
    if g.current_user:
        return redirect(url_for("chat_list"))
    return redirect(url_for("login"))


@app.route("/feed")
def feed():
    db = get_db()
    current_id = session.get("user_id")

    posts = db.execute(
        """
        SELECT 
            posts.*,
            users.username,
            users.profile_pic,
            (SELECT COUNT(*) FROM likes WHERE likes.post_id = posts.id) AS like_count,
            (SELECT COUNT(*) FROM comments WHERE comments.post_id = posts.id) AS comment_count
        FROM posts
        JOIN users ON posts.user_id = users.id
        ORDER BY posts.created_at DESC, posts.id DESC
        """
    ).fetchall()

    comments_map = {}
    for post in posts:
        comments_map[post["id"]] = db.execute(
            """
            SELECT comments.*, users.username
            FROM comments
            JOIN users ON comments.user_id = users.id
            WHERE comments.post_id = ?
            ORDER BY comments.created_at ASC, comments.id ASC
            """,
            (post["id"],),
        ).fetchall()

    if current_id:
        story_users = db.execute(
            """
            SELECT DISTINCT
                users.id,
                users.username,
                users.profile_pic,
                EXISTS(
                    SELECT 1
                    FROM stories s2
                    WHERE s2.user_id = users.id
                      AND datetime(s2.expires_at) > datetime('now')
                      AND NOT EXISTS (
                          SELECT 1
                          FROM story_views sv
                          WHERE sv.story_id = s2.id AND sv.viewer_id = ?
                      )
                ) AS has_unseen
            FROM stories
            JOIN users ON stories.user_id = users.id
            WHERE datetime(stories.expires_at) > datetime('now')
            ORDER BY stories.created_at DESC
            """,
            (current_id,),
        ).fetchall()
    else:
        story_users = db.execute(
            """
            SELECT DISTINCT
                users.id,
                users.username,
                users.profile_pic,
                0 AS has_unseen
            FROM stories
            JOIN users ON stories.user_id = users.id
            WHERE datetime(stories.expires_at) > datetime('now')
            ORDER BY stories.created_at DESC
            """
        ).fetchall()

    return render_template(
        "feed.html",
        posts=posts,
        comments_map=comments_map,
        story_users=story_users,
    )

@app.route("/create_story", methods=["GET", "POST"])
@login_required
def create_story():
    if request.method == "POST":
        caption = request.form.get("caption", "").strip()
        media = request.files.get("media")

        media_file, media_type = save_post_media(media)

        if not media_file:
            flash("Please upload an image or video for the story.", "danger")
            return redirect(url_for("create_story"))

        expires_at = (datetime.now() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")

        db = get_db()
        db.execute(
            """
            INSERT INTO stories (user_id, media_file, media_type, caption, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session["user_id"], media_file, media_type, caption, expires_at),
        )
        db.commit()

        flash("Story uploaded successfully.", "success")
        return redirect(url_for("feed"))

    return render_template("create_story.html")

@app.route("/stories/<int:user_id>")
def view_stories(user_id):
    db = get_db()
    current_id = session.get("user_id")

    user = db.execute(
        "SELECT * FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()

    if not user:
        flash("User not found.", "danger")
        return redirect(url_for("feed"))

    stories = db.execute(
        """
        SELECT *
        FROM stories
        WHERE user_id = ?
          AND datetime(expires_at) > datetime('now')
        ORDER BY created_at ASC
        """,
        (user_id,),
    ).fetchall()

    if not stories:
        flash("No active stories available.", "warning")
        return redirect(url_for("feed"))

    if current_id and current_id != user_id:
        for story in stories:
            db.execute(
                """
                INSERT OR IGNORE INTO story_views (story_id, viewer_id)
                VALUES (?, ?)
                """,
                (story["id"], current_id),
            )
        db.commit()

    story_views_count = {
        story["id"]: db.execute(
            "SELECT COUNT(*) AS count FROM story_views WHERE story_id = ?",
            (story["id"],),
        ).fetchone()["count"]
        for story in stories
    }

    return render_template(
        "view_stories.html",
        user=user,
        stories=stories,
        current_user_id=current_id,
        story_views_count=story_views_count,
    )

@app.route("/story/<int:story_id>/delete", methods=["POST"])
@login_required
def delete_story(story_id):
    db = get_db()

    story = db.execute(
        "SELECT * FROM stories WHERE id = ?",
        (story_id,),
    ).fetchone()

    if not story:
        flash("Story not found.", "danger")
        return redirect(url_for("feed"))

    if story["user_id"] != session["user_id"]:
        flash("Unauthorized action.", "danger")
        return redirect(url_for("feed"))

    db.execute("DELETE FROM stories WHERE id = ?", (story_id,))
    db.commit()

    flash("Story deleted successfully.", "success")
    return redirect(url_for("feed"))

@app.route("/story/<int:story_id>/views")
@login_required
def story_views_page(story_id):
    db = get_db()

    story = db.execute(
        "SELECT * FROM stories WHERE id = ?",
        (story_id,),
    ).fetchone()

    if not story:
        flash("Story not found.", "danger")
        return redirect(url_for("feed"))

    if story["user_id"] != session["user_id"]:
        flash("Unauthorized action.", "danger")
        return redirect(url_for("feed"))

    viewers = db.execute(
        """
        SELECT users.username, users.profile_pic, story_views.viewed_at
        FROM story_views
        JOIN users ON story_views.viewer_id = users.id
        WHERE story_views.story_id = ?
        ORDER BY story_views.viewed_at DESC
        """,
        (story_id,),
    ).fetchall()

    return render_template("story_views.html", viewers=viewers, story=story)


@app.route("/register", methods=["GET", "POST"])
def register():
    if g.current_user:
        return redirect(url_for("chat_list"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone_number = request.form.get("phone_number", "").strip()
        optional_phone_number = request.form.get("optional_phone_number", "").strip()
        password = request.form.get("password", "")

        if not username or not email or not password:
            flash("All fields are required.", "danger")
            return redirect(url_for("register"))

        if phone_number and not is_valid_phone_number(phone_number, required=False):
            flash("Please enter a valid mobile number.", "danger")
            return redirect(url_for("register"))

        if optional_phone_number and not is_valid_phone_number(optional_phone_number, required=False):
            flash("Please enter a valid optional contact number.", "danger")
            return redirect(url_for("register"))

        if len(password) < 6:
            flash("Password must be at least 6 characters long.", "danger")
            return redirect(url_for("register"))

        db = get_db()
        existing = db.execute(
            "SELECT id FROM users WHERE username = ? OR email = ?",
            (username, email),
        ).fetchone()

        if existing:
            flash("Username or email already exists.", "danger")
            return redirect(url_for("register"))

        # Store only a secure hash, never the plain password.
        password_hash = generate_password_hash(password)
        db.execute(
            """
            INSERT INTO users (username, email, phone_number, optional_phone_number, password_hash)
            VALUES (?, ?, ?, ?, ?)
            """,
            (username, email, phone_number, optional_phone_number, password_hash),
        )
        db.commit()

        flash("Registration successful. Please login.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.current_user:
        return redirect(get_login_redirect_target("chat_list"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            flash("Username and password are required.", "danger")
            return redirect(url_for("login", next=safe_login_next_value()))

        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE username = ?",
            (username,),
        ).fetchone()

        # Login succeeds only when the username exists and the hash matches.
        if user and password_matches(user["password_hash"], password):
            login_user_session(user)
            flash("Login successful.", "success")
            return redirect(get_login_redirect_target("chat_list"))

        flash("Invalid username or password.", "danger")
        return redirect(url_for("login", next=safe_login_next_value()))

    return render_template("login.html", next_url=safe_login_next_value())


@app.route("/logout")
def logout():
    clear_login_session()
    flash("Logged out successfully.", "logout-toast")
    return redirect(url_for("index"))


@app.route("/create_post", methods=["GET", "POST"])
@login_required
def create_post():
    if request.method == "POST":
        content = request.form.get("content", "").strip()
        media = request.files.get("media")
        media_file, media_type = save_post_media(media)

        if not content and not media_file:
            flash("Post cannot be empty.", "danger")
            return redirect(url_for("create_post"))

        db = get_db()
        db.execute(
            """
            INSERT INTO posts (user_id, content, media_file, media_type)
            VALUES (?, ?, ?, ?)
            """,
            (session["user_id"], content, media_file, media_type),
        )
        db.commit()

        flash("Post created successfully.", "success")
        return redirect(url_for("feed"))

    return render_template("create_post.html")



@app.route("/post/<int:post_id>/edit", methods=["GET", "POST"])
@login_required
def edit_post(post_id):
    db = get_db()
    post = db.execute(
        "SELECT * FROM posts WHERE id = ?",
        (post_id,),
    ).fetchone()

    if not post or post["user_id"] != session["user_id"]:
        flash("Unauthorized action.", "danger")
        return redirect(url_for("feed"))

    if request.method == "POST":
        content = request.form.get("content", "").strip()
        db.execute(
            "UPDATE posts SET content = ? WHERE id = ?",
            (content, post_id),
        )
        db.commit()
        flash("Post updated successfully.", "success")
        return redirect(url_for("feed"))

    return render_template("edit_post.html", post=post)


@app.route("/post/<int:post_id>/delete", methods=["POST"])
@login_required
def delete_post(post_id):
    db = get_db()
    post = db.execute(
        "SELECT * FROM posts WHERE id = ?",
        (post_id,),
    ).fetchone()

    if not post or post["user_id"] != session["user_id"]:
        flash("Unauthorized action.", "danger")
        return redirect(url_for("feed"))

    db.execute("DELETE FROM posts WHERE id = ?", (post_id,))
    db.commit()
    flash("Post deleted.", "info")
    return redirect(url_for("feed"))


@app.route("/post/<int:post_id>/like", methods=["POST"])
@login_required
def like_post(post_id):
    db = get_db()
    post = db.execute("SELECT id FROM posts WHERE id = ?", (post_id,)).fetchone()
    if not post:
        flash("Post not found.", "danger")
        return redirect(url_for("feed"))

    existing = db.execute(
        """
        SELECT id FROM likes WHERE post_id = ? AND user_id = ?
        """,
        (post_id, session["user_id"]),
    ).fetchone()

    if existing:
        db.execute(
            "DELETE FROM likes WHERE post_id = ? AND user_id = ?",
            (post_id, session["user_id"]),
        )
    else:
        db.execute(
            "INSERT INTO likes (post_id, user_id) VALUES (?, ?)",
            (post_id, session["user_id"]),
        )

    db.commit()
    return redirect(url_for("feed"))


@app.route("/post/<int:post_id>/comment", methods=["POST"])
@login_required
def comment_post(post_id):
    comment_text = request.form.get("comment_text", "").strip()

    if not comment_text:
        flash("Comment cannot be empty.", "danger")
        return redirect(url_for("feed"))

    db = get_db()
    post = db.execute("SELECT id FROM posts WHERE id = ?", (post_id,)).fetchone()
    if not post:
        flash("Post not found.", "danger")
        return redirect(url_for("feed"))

    db.execute(
        """
        INSERT INTO comments (post_id, user_id, comment_text)
        VALUES (?, ?, ?)
        """,
        (post_id, session["user_id"], comment_text),
    )
    db.commit()

    flash("Comment added.", "success")
    return redirect(url_for("feed"))


@app.route("/profile/<int:user_id>")
@login_required
def profile(user_id):
    db = get_db()
    user = db.execute(
        "SELECT * FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()

    if not user:
        flash("User not found.", "danger")
        return redirect(url_for("feed"))

    posts = db.execute(
        """
        SELECT
            posts.*,
            (SELECT COUNT(*) FROM likes WHERE likes.post_id = posts.id) AS like_count,
            (SELECT COUNT(*) FROM comments WHERE comments.post_id = posts.id) AS comment_count
        FROM posts
        WHERE user_id = ?
        ORDER BY created_at DESC, id DESC
        """,
        (user_id,),
    ).fetchall()

    followers = db.execute(
        "SELECT COUNT(*) AS count FROM follows WHERE following_id = ?",
        (user_id,),
    ).fetchone()["count"]

    following = db.execute(
        "SELECT COUNT(*) AS count FROM follows WHERE follower_id = ?",
        (user_id,),
    ).fetchone()["count"]

    can_follow = False
    already_following = False
    if "user_id" in session and session["user_id"] != user_id:
        can_follow = True
        already_following = is_following(session["user_id"], user_id)

    return render_template(
        "profile.html",
        user=user,
        posts=posts,
        followers=followers,
        following=following,
        can_follow=can_follow,
        already_following=already_following,
    )


@app.route("/profile/<int:user_id>/followers")
@login_required
def profile_followers(user_id):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for("users"))

    people = db.execute(
        """
        SELECT users.*,
               (SELECT COUNT(*) FROM posts WHERE posts.user_id = users.id) AS post_count
        FROM follows
        JOIN users ON users.id = follows.follower_id
        WHERE follows.following_id = ?
        ORDER BY follows.id DESC
        """,
        (user_id,),
    ).fetchall()

    return render_template(
        "follow_list.html",
        title=f"{user['username']}'s Followers",
        empty_message="No followers yet.",
        people=people,
        following_ids=followed_user_ids(session.get("user_id")),
    )


@app.route("/profile/<int:user_id>/following")
@login_required
def profile_following(user_id):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for("users"))

    people = db.execute(
        """
        SELECT users.*,
               (SELECT COUNT(*) FROM posts WHERE posts.user_id = users.id) AS post_count
        FROM follows
        JOIN users ON users.id = follows.following_id
        WHERE follows.follower_id = ?
        ORDER BY follows.id DESC
        """,
        (user_id,),
    ).fetchall()

    return render_template(
        "follow_list.html",
        title=f"{user['username']} is Following",
        empty_message="Not following anyone yet.",
        people=people,
        following_ids=followed_user_ids(session.get("user_id")),
    )


@app.route("/edit_profile", methods=["GET", "POST"])
@login_required
def edit_profile():
    db = get_db()
    user = db.execute(
        "SELECT * FROM users WHERE id = ?",
        (session["user_id"],),
    ).fetchone()

    if not user:
        session.pop("user_id", None)
        flash("Your account could not be found. Please login again.", "warning")
        return redirect(url_for("login"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        bio = request.form.get("bio", "").strip()
        profile_pic = request.files.get("profile_pic")
        remove_profile_pic = request.form.get("remove_profile_pic") == "1"

        if not username:
            flash("Username is required.", "danger")
            return redirect(url_for("edit_profile"))

        existing_user = db.execute(
            """
            SELECT id
            FROM users
            WHERE username = ? AND id != ?
            """,
            (username, session["user_id"]),
        ).fetchone()

        if existing_user:
            flash("Username already exists.", "danger")
            return redirect(url_for("edit_profile"))

        new_profile_pic = "" if remove_profile_pic else user["profile_pic"]
        if not remove_profile_pic:
            saved_pic = save_profile_media(profile_pic)
            if saved_pic:
                new_profile_pic = saved_pic

        db.execute(
            """
            UPDATE users
            SET username = ?, bio = ?, profile_pic = ?
            WHERE id = ?
            """,
            (username, bio, new_profile_pic, session["user_id"]),
        )
        db.commit()

        flash("Profile updated successfully.", "success")
        return redirect(url_for("profile", user_id=session["user_id"]))

    return render_template("edit_profile.html", user=user)


@app.route("/follow/<int:user_id>", methods=["POST"])
@login_required
def follow_user(user_id):
    redirect_to = request.form.get("next")
    if not is_safe_redirect_url(redirect_to):
        redirect_to = url_for("profile", user_id=user_id)

    if session["user_id"] == user_id:
        flash("You cannot follow yourself.", "warning")
        return redirect(redirect_to)

    db = get_db()
    target_user = db.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
    if not target_user:
        flash("User not found.", "danger")
        return redirect(url_for("users"))

    existing = db.execute(
        """
        SELECT id FROM follows WHERE follower_id = ? AND following_id = ?
        """,
        (session["user_id"], user_id),
    ).fetchone()

    if existing:
        db.execute(
            "DELETE FROM follows WHERE follower_id = ? AND following_id = ?",
            (session["user_id"], user_id),
        )
        flash("Unfollowed user.", "info")
    else:
        db.execute(
            "INSERT INTO follows (follower_id, following_id) VALUES (?, ?)",
            (session["user_id"], user_id),
        )
        flash("Now following user.", "success")

    db.commit()
    return redirect(redirect_to)


@app.route("/users")
def users():
    db = get_db()
    users_data = db.execute(
        """
        SELECT users.*,
               (SELECT COUNT(*) FROM posts WHERE posts.user_id = users.id) AS post_count
        FROM users
        ORDER BY users.created_at DESC
        """
    ).fetchall()
    following_ids = followed_user_ids(session.get("user_id"))
    return render_template("users.html", users=users_data, following_ids=following_ids)


@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    db = get_db()

    users_result = []
    posts_result = []

    if q:
        users_result = db.execute(
            """
            SELECT * FROM users
            WHERE username LIKE ?
            ORDER BY username
            """,
            (f"%{q}%",),
        ).fetchall()

        posts_result = db.execute(
            """
            SELECT posts.*, users.username
            FROM posts
            JOIN users ON posts.user_id = users.id
            WHERE posts.content LIKE ?
            ORDER BY posts.created_at DESC, posts.id DESC
            """,
            (f"%{q}%",),
        ).fetchall()

    following_ids = followed_user_ids(session.get("user_id"))
    return render_template(
        "search.html",
        q=q,
        users_result=users_result,
        posts_result=posts_result,
        following_ids=following_ids,
    )


# ---------------- EMERGENCY ASSISTANCE ----------------

@app.route("/emergency")
@login_required
def emergency_feed():
    db = get_db()
    alerts = db.execute(
        """
        SELECT emergency_alerts.*, users.username, users.profile_pic,
               alert_locations.latitude, alert_locations.longitude,
               (SELECT COUNT(*) FROM emergency_responses
                WHERE emergency_responses.alert_id = emergency_alerts.id
                  AND response_type = 'help') AS helper_count
        FROM emergency_alerts
        JOIN users ON users.id = emergency_alerts.user_id
        LEFT JOIN alert_locations ON alert_locations.alert_id = emergency_alerts.id
        WHERE emergency_alerts.status = 'active'
        ORDER BY emergency_alerts.created_at DESC, emergency_alerts.id DESC
        """
    ).fetchall()
    return render_template("emergency_feed.html", alerts=alerts)


@app.route("/emergency/create", methods=["POST"])
@login_required
def create_emergency_alert():
    data = request.get_json(silent=True) or request.form
    emergency_type = (data.get("emergency_type") or "").strip()
    description = (data.get("description") or "").strip()
    location_text = (data.get("location_text") or "").strip()
    contact_number = (data.get("contact_number") or "").strip()
    optional_contact_number = (data.get("optional_contact_number") or "").strip()
    latitude = data.get("latitude") or None
    longitude = data.get("longitude") or None

    if emergency_type not in {
        "Medical Emergency",
        "Accident",
        "Safety Threat",
        "Blood Requirement",
        "Natural Disaster",
        "Other",
    }:
        return jsonify({"ok": False, "error": "Choose a valid emergency type."}), 400

    if contact_number and not is_valid_phone_number(contact_number):
        return jsonify({"ok": False, "error": "Please enter a valid contact number."}), 400

    if optional_contact_number and not is_valid_phone_number(optional_contact_number, required=False):
        return jsonify({"ok": False, "error": "Please enter a valid optional contact number."}), 400

    try:
        latitude = float(latitude) if latitude not in (None, "") else None
        longitude = float(longitude) if longitude not in (None, "") else None
    except (TypeError, ValueError):
        latitude = None
        longitude = None

    if latitude is not None and longitude is not None and (
        not location_text or COORDINATE_TEXT_PATTERN.fullmatch(location_text)
    ):
        location_text = reverse_geocode_location(latitude, longitude) or "Current shared location"

    db = get_db()
    user = db.execute("SELECT phone_number, optional_phone_number FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    if not contact_number and user:
        contact_number = user["phone_number"] or ""
    if not optional_contact_number and user:
        optional_contact_number = user["optional_phone_number"] or ""

    recent = db.execute(
        """
        SELECT created_at
        FROM emergency_alerts
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (session["user_id"],),
    ).fetchone()
    if recent:
        try:
            last_time = datetime.strptime(recent["created_at"], "%Y-%m-%d %H:%M:%S")
            if (datetime.now() - last_time).total_seconds() < EMERGENCY_COOLDOWN_SECONDS:
                return jsonify({"ok": False, "error": "Please wait before sending another SOS."}), 429
        except ValueError:
            pass

    severity, ai_guidance = emergency_ai_assistance(emergency_type, description)
    cursor = db.execute(
        """
        INSERT INTO emergency_alerts
            (user_id, emergency_type, description, location_text, contact_number, optional_contact_number, severity, ai_guidance)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session["user_id"],
            emergency_type,
            description,
            location_text,
            contact_number,
            optional_contact_number,
            severity,
            ai_guidance,
        ),
    )
    alert_id = cursor.lastrowid
    db.execute(
        """
        INSERT INTO alert_locations (alert_id, user_id, latitude, longitude, location_text)
        VALUES (?, ?, ?, ?, ?)
        """,
        (alert_id, session["user_id"], latitude, longitude, location_text),
    )
    db.execute(
        """
        INSERT INTO alert_status (alert_id, status, changed_by)
        VALUES (?, 'active', ?)
        """,
        (alert_id, session["user_id"]),
    )
    db.commit()

    alert = db.execute(
        """
        SELECT emergency_alerts.*, users.username
        FROM emergency_alerts
        JOIN users ON users.id = emergency_alerts.user_id
        WHERE emergency_alerts.id = ?
        """,
        (alert_id,),
    ).fetchone()
    payload = emergency_payload(alert)

    nearby_users = []
    sent_to = {session["user_id"]}
    if latitude is not None and longitude is not None:
        nearby_ids = []
        for nearby_user_id, user_location in list(online_user_locations.items()):
            if nearby_user_id == session["user_id"]:
                continue
            km = distance_km(latitude, longitude, user_location.get("latitude"), user_location.get("longitude"))
            if km is not None and km <= EMERGENCY_RADIUS_KM:
                socketio.emit("emergency_alert", payload, to=f"user_{nearby_user_id}")
                sent_to.add(nearby_user_id)
                nearby_ids.append(nearby_user_id)
        if nearby_ids:
            placeholders = ",".join("?" for _ in nearby_ids)
            rows = db.execute(
                f"SELECT id, username FROM users WHERE id IN ({placeholders})",
                nearby_ids,
            ).fetchall()
            username_by_id = {row["id"]: row["username"] for row in rows}
            nearby_users = [
                {"id": user_id, "username": username_by_id.get(user_id, "Nearby user")}
                for user_id in nearby_ids
            ]
    if len(sent_to) == 1:
        socketio.emit("emergency_alert", payload)

    socketio.emit(
        "emergency_created",
        {"alert": payload, "nearby_users": nearby_users},
        to=f"user_{session['user_id']}",
    )
    return jsonify({"ok": True, "alert": payload, "nearby_users": nearby_users})


@app.route("/emergency/<int:alert_id>/respond", methods=["POST"])
@login_required
def respond_emergency(alert_id):
    db = get_db()
    alert = db.execute("SELECT * FROM emergency_alerts WHERE id = ? AND status = 'active'", (alert_id,)).fetchone()
    if not alert:
        return jsonify({"ok": False, "error": "Emergency alert not found."}), 404

    message = (request.get_json(silent=True) or {}).get("message", "").strip()
    db.execute(
        """
        INSERT OR IGNORE INTO emergency_responses (alert_id, responder_id, response_type, message)
        VALUES (?, ?, 'help', ?)
        """,
        (alert_id, session["user_id"], message),
    )
    db.commit()
    socketio.emit(
        "emergency_response",
        {"alert_id": alert_id, "username": session.get("username"), "message": message or "I can help."},
        to=f"emergency_{alert_id}",
    )
    return jsonify({"ok": True})


@app.route("/emergency/<int:alert_id>/safe", methods=["POST"])
@login_required
def mark_emergency_safe(alert_id):
    db = get_db()
    alert = db.execute(
        "SELECT * FROM emergency_alerts WHERE id = ? AND user_id = ?",
        (alert_id, session["user_id"]),
    ).fetchone()
    if not alert:
        return jsonify({"ok": False, "error": "Only the requester can mark this safe."}), 403

    db.execute(
        "UPDATE emergency_alerts SET status = 'safe', resolved_at = CURRENT_TIMESTAMP WHERE id = ?",
        (alert_id,),
    )
    db.execute(
        "INSERT INTO alert_status (alert_id, status, changed_by) VALUES (?, 'safe', ?)",
        (alert_id, session["user_id"]),
    )
    db.commit()
    socketio.emit("emergency_safe", {"alert_id": alert_id}, to=f"emergency_{alert_id}")
    return jsonify({"ok": True})


@app.route("/emergency/<int:alert_id>/chat", methods=["GET", "POST"])
@login_required
def emergency_chat(alert_id):
    db = get_db()
    alert = db.execute(
        """
        SELECT emergency_alerts.*, users.username
        FROM emergency_alerts
        JOIN users ON users.id = emergency_alerts.user_id
        WHERE emergency_alerts.id = ?
        """,
        (alert_id,),
    ).fetchone()
    if not alert:
        flash("Emergency alert not found.", "danger")
        return redirect(url_for("emergency_feed"))

    if request.method == "POST":
        message_text = request.form.get("message_text", "").strip()
        if message_text:
            db.execute(
                """
                INSERT INTO emergency_chat_messages (alert_id, sender_id, message_text)
                VALUES (?, ?, ?)
                """,
                (alert_id, session["user_id"], message_text),
            )
            db.commit()
            socketio.emit(
                "emergency_chat_message",
                {"alert_id": alert_id, "username": session.get("username"), "message_text": message_text},
                to=f"emergency_{alert_id}",
            )
        return redirect(url_for("emergency_chat", alert_id=alert_id))

    messages = db.execute(
        """
        SELECT emergency_chat_messages.*, users.username
        FROM emergency_chat_messages
        JOIN users ON users.id = emergency_chat_messages.sender_id
        WHERE alert_id = ?
        ORDER BY emergency_chat_messages.created_at ASC, emergency_chat_messages.id ASC
        """,
        (alert_id,),
    ).fetchall()
    return render_template("emergency_chat.html", alert=alert, messages=messages)


# ---------------- CHAT PAGES ----------------

@app.route("/messages")
@login_required
def chat_list():
    db = get_db()
    current_id = session["user_id"]

    conversations = db.execute(
        """
        SELECT
            c.id AS conversation_id,
            u.id AS other_user_id,
            u.username AS other_username,
            u.profile_pic AS other_profile_pic,
            (
                SELECT m.message_text
                FROM messages m
                WHERE m.conversation_id = c.id
                ORDER BY m.created_at DESC, m.id DESC
                LIMIT 1
            ) AS last_message,
            (
                SELECT m.created_at
                FROM messages m
                WHERE m.conversation_id = c.id
                ORDER BY m.created_at DESC, m.id DESC
                LIMIT 1
            ) AS last_message_time
        FROM conversations c
        JOIN users u
            ON u.id = CASE
                WHEN c.user1_id = ? THEN c.user2_id
                ELSE c.user1_id
            END
        WHERE c.user1_id = ? OR c.user2_id = ?
        ORDER BY last_message_time DESC, c.id DESC
        """,
        (current_id, current_id, current_id),
    ).fetchall()

    all_users = db.execute(
        """
        SELECT id, username, profile_pic
        FROM users
        WHERE id != ?
        ORDER BY username ASC
        """,
        (current_id,),
    ).fetchall()

    return render_template(
        "chat_list.html",
        conversations=conversations,
        all_users=all_users,
    )


@app.route("/chat/start/<int:user_id>")
@login_required
def start_chat(user_id):
    if user_id == session["user_id"]:
        flash("You cannot chat with yourself.", "warning")
        return redirect(url_for("chat_list"))

    db = get_db()
    user = db.execute(
        "SELECT id FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for("chat_list"))

    conversation_id = get_or_create_conversation(session["user_id"], user_id)
    return redirect(url_for("chat_room", conversation_id=conversation_id))


@app.route("/chat/<int:conversation_id>")
@login_required
def chat_room(conversation_id):
    db = get_db()
    current_id = session["user_id"]

    conversation = get_conversation_for_user(conversation_id, current_id)

    if not conversation:
        flash("Conversation not found.", "danger")
        return redirect(url_for("chat_list"))

    other_user_id = get_other_chat_user_id(conversation, current_id)

    other_user = db.execute(
        """
        SELECT id, username, profile_pic
        FROM users
        WHERE id = ?
        """,
        (other_user_id,),
    ).fetchone()

    if not other_user:
        flash("The other user in this conversation no longer exists.", "warning")
        return redirect(url_for("chat_list"))

    messages = db.execute(
        """
        SELECT messages.*, users.username
        FROM messages
        JOIN users ON messages.sender_id = users.id
        WHERE messages.conversation_id = ?
        ORDER BY messages.created_at ASC, messages.id ASC
        """,
        (conversation_id,),
    ).fetchall()

    unread_rows = db.execute(
        """
        SELECT id
        FROM messages
        WHERE conversation_id = ? AND sender_id != ? AND is_read = 0
        """,
        (conversation_id, current_id),
    ).fetchall()

    db.execute(
        """
        UPDATE messages
        SET is_read = 1
        WHERE conversation_id = ? AND sender_id != ?
        """,
        (conversation_id, current_id),
    )
    db.commit()
    if unread_rows:
        socketio.emit(
            "messages_read",
            {
                "conversation_id": int(conversation_id),
                "reader_id": int(current_id),
                "message_ids": [int(row["id"]) for row in unread_rows],
            },
            room=chat_room_name_for_conversation(conversation),
        )

    conversations = db.execute(
        """
        SELECT
            c.id AS conversation_id,
            u.id AS other_user_id,
            u.username AS other_username,
            u.profile_pic AS other_profile_pic,
            (
                SELECT m.message_text
                FROM messages m
                WHERE m.conversation_id = c.id
                ORDER BY m.created_at DESC, m.id DESC
                LIMIT 1
            ) AS last_message
        FROM conversations c
        JOIN users u
            ON u.id = CASE
                WHEN c.user1_id = ? THEN c.user2_id
                ELSE c.user1_id
            END
        WHERE c.user1_id = ? OR c.user2_id = ?
        ORDER BY c.id DESC
        """,
        (current_id, current_id, current_id),
    ).fetchall()

    return render_template(
        "chat.html",
        conversation_id=conversation_id,
        current_user_id=current_id,
        chat_room_name=chat_room_name_for_conversation(conversation),
        other_user_online=is_user_online(other_user_id),
        other_user=other_user,
        messages=[chat_message_payload(message, conversation) for message in messages],
        conversations=conversations,
    )


# Optional fallback non-realtime route
@app.route("/chat/<int:conversation_id>/send", methods=["POST"])
@login_required
def send_message_fallback(conversation_id):
    db = get_db()
    current_id = session["user_id"]
    data = request.get_json(silent=True) or {}
    message_text = (data.get("message_text") or request.form.get("message_text", "")).strip()
    client_message_id = (data.get("client_message_id") or request.form.get("client_message_id", "")).strip()
    wants_json = (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or request.is_json
        or "application/json" in request.headers.get("Accept", "")
    )

    if not message_text:
        if wants_json:
            return jsonify({"ok": False, "error": "Message cannot be empty."}), 400
        flash("Message cannot be empty.", "danger")
        return redirect(url_for("chat_room", conversation_id=conversation_id))

    conversation = get_conversation_for_user(conversation_id, current_id)

    if not conversation:
        if wants_json:
            return jsonify({"ok": False, "error": "Conversation not found."}), 404
        flash("Conversation not found.", "danger")
        return redirect(url_for("chat_list"))

    socket_log("MESSAGE RECEIVED (backend)", source="http", user_id=current_id, conversation_id=conversation_id)
    existing_message = find_existing_client_message(conversation_id, current_id, client_message_id)
    if existing_message:
        socket_log(
            "DUPLICATE SEND SKIPPED (backend)",
            source="http",
            user_id=current_id,
            conversation_id=conversation_id,
            message_id=existing_message["id"],
            client_message_id=client_message_id,
        )
        payload = chat_message_payload(existing_message, conversation)
        if wants_json:
            return jsonify({"ok": True, "message": payload, "duplicate": True})
        return redirect(url_for("chat_room", conversation_id=conversation_id))

    result = db.execute(
        """
        INSERT INTO messages (conversation_id, sender_id, message_text, client_message_id)
        VALUES (?, ?, ?, ?)
        """,
        (conversation_id, current_id, message_text, client_message_id),
    )
    message_id = result.lastrowid
    db.commit()
    socket_log("MESSAGE SAVED (backend)", source="http", user_id=current_id, conversation_id=conversation_id, message_id=message_id)

    message = fetch_chat_message(message_id)
    payload = chat_message_payload(message, conversation)
    room = chat_room_name_for_conversation(conversation)
    socketio.emit("new_message", payload, room=room)
    socketio.sleep(0)
    socket_log("MESSAGE EMITTED (backend)", source="http", user_id=current_id, conversation_id=conversation_id, message_id=message_id, room=room)

    if wants_json:
        return jsonify({"ok": True, "message": payload})

    return redirect(url_for("chat_room", conversation_id=conversation_id))


@app.route("/chat/<int:conversation_id>/messages")
@login_required
def chat_messages_since(conversation_id):
    db = get_db()
    current_id = session["user_id"]
    conversation = get_conversation_for_user(conversation_id, current_id)

    if not conversation:
        return jsonify({"ok": False, "error": "Conversation not found."}), 404

    try:
        after_id = int(request.args.get("after_id", 0))
    except (TypeError, ValueError):
        after_id = 0

    rows = db.execute(
        """
        SELECT messages.*, users.username
        FROM messages
        JOIN users ON messages.sender_id = users.id
        WHERE messages.conversation_id = ? AND messages.id > ?
        ORDER BY messages.created_at ASC, messages.id ASC
        """,
        (conversation_id, after_id),
    ).fetchall()

    if rows:
        read_message_ids = [
            int(row["id"])
            for row in rows
            if int(row["sender_id"]) != int(current_id) and not row["is_read"]
        ]
        db.execute(
            """
            UPDATE messages
            SET is_read = 1
            WHERE conversation_id = ? AND sender_id != ?
            """,
            (conversation_id, current_id),
        )
        db.commit()
        if read_message_ids:
            socketio.emit(
                "messages_read",
                {
                    "conversation_id": int(conversation_id),
                    "reader_id": int(current_id),
                    "message_ids": read_message_ids,
                },
                room=chat_room_name_for_conversation(conversation),
            )

    return jsonify({
        "ok": True,
        "messages": [chat_message_payload(row, conversation) for row in rows],
    })


@app.route("/chat/<int:conversation_id>/read", methods=["POST"])
@login_required
def mark_chat_read(conversation_id):
    current_id = session["user_id"]
    conversation = get_conversation_for_user(conversation_id, current_id)

    if not conversation:
        return jsonify({"ok": False, "error": "Conversation not found."}), 404

    db = get_db()
    unread_rows = db.execute(
        """
        SELECT id
        FROM messages
        WHERE conversation_id = ? AND sender_id != ? AND is_read = 0
        """,
        (conversation_id, current_id),
    ).fetchall()
    db.execute(
        """
        UPDATE messages
        SET is_read = 1
        WHERE conversation_id = ? AND sender_id != ?
        """,
        (conversation_id, current_id),
    )
    db.commit()
    if unread_rows:
        socketio.emit(
            "messages_read",
            {
                "conversation_id": int(conversation_id),
                "reader_id": int(current_id),
                "message_ids": [int(row["id"]) for row in unread_rows],
            },
            room=chat_room_name_for_conversation(conversation),
        )

    return jsonify({"ok": True})


@app.route("/message/<int:message_id>/edit", methods=["POST"])
@login_required
def edit_message(message_id):
    db = get_db()
    current_id = session["user_id"]
    message_text = (request.get_json(silent=True) or {}).get("message_text", "").strip()

    if not message_text:
        return jsonify({"ok": False, "error": "Message cannot be empty."}), 400

    message = db.execute(
        """
        SELECT *
        FROM messages
        WHERE id = ? AND sender_id = ?
        """,
        (message_id, current_id),
    ).fetchone()

    if not message:
        return jsonify({"ok": False, "error": "Message not found."}), 404

    db.execute(
        "UPDATE messages SET message_text = ? WHERE id = ?",
        (message_text, message_id),
    )
    db.commit()
    return jsonify({"ok": True, "message_text": message_text})


@app.route("/message/<int:message_id>/delete", methods=["POST"])
@login_required
def delete_message(message_id):
    db = get_db()
    current_id = session["user_id"]

    message = db.execute(
        """
        SELECT *
        FROM messages
        WHERE id = ? AND sender_id = ?
        """,
        (message_id, current_id),
    ).fetchone()

    if not message:
        return jsonify({"ok": False, "error": "Message not found."}), 404

    db.execute("DELETE FROM messages WHERE id = ?", (message_id,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/message/<int:message_id>/forward", methods=["POST"])
@login_required
def forward_message(message_id):
    db = get_db()
    current_id = session["user_id"]
    data = request.get_json(silent=True) or {}
    target_conversation_id = data.get("conversation_id")

    try:
        target_conversation_id = int(target_conversation_id)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Choose a chat to forward to."}), 400

    message = db.execute(
        """
        SELECT m.*
        FROM messages m
        JOIN conversations c ON m.conversation_id = c.id
        WHERE m.id = ?
          AND (c.user1_id = ? OR c.user2_id = ?)
        """,
        (message_id, current_id, current_id),
    ).fetchone()

    if not message:
        return jsonify({"ok": False, "error": "Message not found."}), 404

    target_conversation = get_conversation_for_user(target_conversation_id, current_id)
    if not target_conversation:
        return jsonify({"ok": False, "error": "Forward chat not found."}), 404

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cursor = db.execute(
        """
        INSERT INTO messages (conversation_id, sender_id, message_text, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (target_conversation_id, current_id, message["message_text"], now_str),
    )
    db.commit()

    return jsonify(
        {
            "ok": True,
            "message_id": cursor.lastrowid,
            "conversation_id": target_conversation_id,
            "created_at": now_str,
        }
    )


# ---------------- REAL-TIME SOCKET EVENTS ----------------


@socketio.on("connect")
def handle_socket_connect():
    """Register presence from the signed-in Flask session for this socket."""
    user = socket_user()
    if not user:
        socket_log("connect rejected", sid=request.sid)
        return False

    user_id = int(user["id"])
    sid_to_user[request.sid] = user_id
    online_users.setdefault(user_id, set()).add(request.sid)
    join_room(f"user_{user_id}")
    socket_log("connect", user_id=user_id, sid=request.sid, sockets=len(online_users[user_id]))
    socket_log("join personal room", user_id=user_id, room=f"user_{user_id}", sid=request.sid)
    socketio.emit("user_status", {"user_id": user_id, "online": True})
    socket_log("status broadcast", user_id=user_id, online=True)


@socketio.on("disconnect")
def handle_socket_disconnect():
    """Remove this socket; mark the user offline when their last tab leaves."""
    user_id = sid_to_user.pop(request.sid, None)
    if not user_id:
        socket_log("disconnect without user", sid=request.sid)
        return

    sockets = online_users.get(user_id)
    if sockets:
        sockets.discard(request.sid)
        socket_log("disconnect", user_id=user_id, sid=request.sid, remaining=len(sockets))
        if not sockets:
            online_users.pop(user_id, None)
            socketio.emit("user_status", {"user_id": user_id, "online": False})
            socket_log("status broadcast", user_id=user_id, online=False)


@socketio.on("join_chat")
def handle_join_chat(data):
    conversation_id = data.get("conversation_id")
    user = socket_user()
    if not conversation_id or not user:
        socket_log("join_chat rejected", sid=request.sid, conversation_id=conversation_id)
        emit("chat_error", {"message": "Please login again to use chat."})
        return

    conversation = get_conversation_for_user(conversation_id, user["id"])
    if not conversation:
        socket_log("join_chat forbidden", user_id=user["id"], conversation_id=conversation_id, sid=request.sid)
        emit("chat_error", {"message": "You do not have access to this chat."})
        return

    room = chat_room_name_for_conversation(conversation)
    other_user_id = get_other_chat_user_id(conversation, user["id"])
    join_room(room)
    join_room(f"user_{user['id']}")
    socket_log("join chat room", user_id=user["id"], conversation_id=conversation_id, room=room, sid=request.sid)
    socket_log("join personal room", user_id=user["id"], room=f"user_{user['id']}", sid=request.sid)

    db = get_db()
    unread_rows = db.execute(
        """
        SELECT id
        FROM messages
        WHERE conversation_id = ? AND sender_id != ? AND is_read = 0
        """,
        (conversation_id, user["id"]),
    ).fetchall()
    if unread_rows:
        db.execute(
            """
            UPDATE messages
            SET is_read = 1
            WHERE conversation_id = ? AND sender_id != ?
            """,
            (conversation_id, user["id"]),
        )
        db.commit()
        socketio.emit(
            "messages_read",
            {
                "conversation_id": int(conversation_id),
                "reader_id": int(user["id"]),
                "message_ids": [int(row["id"]) for row in unread_rows],
            },
            room=room,
        )
        socket_log("read receipt broadcast", user_id=user["id"], conversation_id=conversation_id, count=len(unread_rows), room=room)

    emit(
        "chat_joined",
        {
            "conversation_id": int(conversation_id),
            "room": room,
            "other_user_id": int(other_user_id),
            "other_user_online": is_user_online(other_user_id),
        },
    )
    socket_log("chat joined ack", user_id=user["id"], conversation_id=conversation_id, other_online=is_user_online(other_user_id))


@socketio.on("typing")
def handle_typing(data):
    """Send transient typing state to the other participant in the room."""
    conversation_id = data.get("conversation_id")
    is_typing = bool(data.get("typing"))
    user = socket_user()
    if not conversation_id or not user:
        socket_log("typing ignored", sid=request.sid, conversation_id=conversation_id)
        return

    conversation = get_conversation_for_user(conversation_id, user["id"])
    if not conversation:
        socket_log("typing forbidden", user_id=user["id"], conversation_id=conversation_id, sid=request.sid)
        return

    room = chat_room_name_for_conversation(conversation)
    emit(
        "typing",
        {
            "conversation_id": int(conversation_id),
            "user_id": int(user["id"]),
            "typing": is_typing,
        },
        to=room,
        include_self=False,
    )
    socket_log("typing broadcast", user_id=user["id"], conversation_id=conversation_id, typing=is_typing, room=room)


@socketio.on("mark_chat_read")
def handle_mark_chat_read(data):
    """Mark messages read as soon as the recipient is viewing the chat."""
    conversation_id = data.get("conversation_id")
    user = socket_user()
    if not conversation_id or not user:
        socket_log("mark read ignored", sid=request.sid, conversation_id=conversation_id)
        return

    conversation = get_conversation_for_user(conversation_id, user["id"])
    if not conversation:
        socket_log("mark read forbidden", user_id=user["id"], conversation_id=conversation_id, sid=request.sid)
        return

    db = get_db()
    unread_rows = db.execute(
        """
        SELECT id
        FROM messages
        WHERE conversation_id = ? AND sender_id != ? AND is_read = 0
        """,
        (conversation_id, user["id"]),
    ).fetchall()
    if not unread_rows:
        socket_log("mark read no-op", user_id=user["id"], conversation_id=conversation_id)
        return

    db.execute(
        """
        UPDATE messages
        SET is_read = 1
        WHERE conversation_id = ? AND sender_id != ?
        """,
        (conversation_id, user["id"]),
    )
    db.commit()
    socketio.emit(
        "messages_read",
        {
            "conversation_id": int(conversation_id),
            "reader_id": int(user["id"]),
            "message_ids": [int(row["id"]) for row in unread_rows],
        },
        room=chat_room_name_for_conversation(conversation),
    )
    socket_log("read receipt broadcast", user_id=user["id"], conversation_id=conversation_id, count=len(unread_rows), room=chat_room_name_for_conversation(conversation))


@socketio.on("update_user_location")
def handle_update_user_location(data):
    user = socket_user()
    if not user:
        return
    try:
        latitude = float(data.get("latitude"))
        longitude = float(data.get("longitude"))
    except (TypeError, ValueError):
        return
    online_user_locations[int(user["id"])] = {"latitude": latitude, "longitude": longitude}
    join_room(f"user_{user['id']}")


@socketio.on("join_emergency")
def handle_join_emergency(data):
    user = socket_user()
    alert_id = data.get("alert_id")
    if not user or not alert_id:
        return
    join_room(f"user_{user['id']}")
    join_room(f"emergency_{alert_id}")


@socketio.on("send_chat_message")
def handle_send_chat_message(data):
    """
    Expected payload:
    {
        "conversation_id": 1,
        "message_text": "hello"
    }
    """
    conversation_id = data.get("conversation_id")
    user = socket_user()
    message_text = (data.get("message_text") or "").strip()
    client_message_id = (data.get("client_message_id") or "").strip()
    socket_log("MESSAGE RECEIVED (backend)", user_id=user["id"] if user else None, conversation_id=conversation_id, client_message_id=client_message_id)

    if not conversation_id or not user or not message_text:
        socket_log("send rejected", sid=request.sid, conversation_id=conversation_id)
        emit("chat_error", {"message": "Message could not be sent."})
        return {"ok": False, "error": "Message could not be sent."}

    db = get_db()

    # Validate the socket session before saving or broadcasting.
    conversation = get_conversation_for_user(conversation_id, user["id"])
    if not conversation:
        socket_log("send forbidden", user_id=user["id"], conversation_id=conversation_id, sid=request.sid)
        emit("chat_error", {"message": "You do not have access to this chat."})
        return {"ok": False, "error": "You do not have access to this chat."}

    existing_message = find_existing_client_message(conversation_id, user["id"], client_message_id)
    if existing_message:
        socket_log(
            "DUPLICATE SEND SKIPPED (backend)",
            user_id=user["id"],
            conversation_id=conversation_id,
            message_id=existing_message["id"],
            client_message_id=client_message_id,
        )
        return {
            "ok": True,
            "message": chat_message_payload(existing_message, conversation),
            "duplicate": True,
        }

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    cursor = db.execute(
        """
        INSERT INTO messages (conversation_id, sender_id, message_text, created_at, client_message_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (conversation_id, user["id"], message_text, now_str, client_message_id),
    )
    message_id = cursor.lastrowid
    db.commit()
    socket_log("MESSAGE SAVED (backend)", user_id=user["id"], conversation_id=conversation_id, message_id=message_id)
    socket_log("database commit completed", user_id=user["id"], conversation_id=conversation_id, message_id=message_id)

    message = fetch_chat_message(message_id)

    payload = chat_message_payload(message, conversation)
    room = chat_room_name_for_conversation(conversation)

    socketio.emit(
        "new_message",
        payload,
        room=room,
    )
    socketio.sleep(0)
    socket_log("MESSAGE EMITTED (backend)", user_id=user["id"], conversation_id=conversation_id, message_id=message_id, room=room)
    return {"ok": True, "message": payload}


# ---------------- ADMIN ----------------

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "").strip()

        admin_email = os.getenv("ADMIN_EMAIL", "admin@famshare.com")
        admin_password = os.getenv("ADMIN_PASSWORD", "admin123")

        if email == admin_email and password == admin_password:
            clear_login_session()
            session.permanent = False
            session["admin_logged_in"] = True
            session["last_activity"] = time.time()
            flash("Admin login successful.", "success")
            return redirect(url_for("admin_dashboard"))

        flash("Invalid admin credentials.", "danger")
        return redirect(url_for("admin_login"))

    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    clear_login_session()
    flash("Admin logged out.", "logout-toast")
    return redirect(url_for("admin_login"))


@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    db = get_db()

    user_count = db.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"]
    post_count = db.execute("SELECT COUNT(*) AS count FROM posts").fetchone()["count"]
    comment_count = db.execute("SELECT COUNT(*) AS count FROM comments").fetchone()["count"]
    like_count = db.execute("SELECT COUNT(*) AS count FROM likes").fetchone()["count"]
    conversation_count = db.execute("SELECT COUNT(*) AS count FROM conversations").fetchone()["count"]
    message_count = db.execute("SELECT COUNT(*) AS count FROM messages").fetchone()["count"]

    recent_users = db.execute(
        "SELECT * FROM users ORDER BY created_at DESC LIMIT 5"
    ).fetchall()

    recent_posts = db.execute(
        """
        SELECT posts.*, users.username
        FROM posts
        JOIN users ON posts.user_id = users.id
        ORDER BY posts.created_at DESC, posts.id DESC
        LIMIT 5
        """
    ).fetchall()

    return render_template(
        "admin_dashboard.html",
        user_count=user_count,
        post_count=post_count,
        comment_count=comment_count,
        like_count=like_count,
        conversation_count=conversation_count,
        message_count=message_count,
        recent_users=recent_users,
        recent_posts=recent_posts,
    )


@app.route("/admin/users")
@admin_required
def admin_users():
    db = get_db()
    users = db.execute(
        """
        SELECT users.*,
               (SELECT COUNT(*) FROM posts WHERE posts.user_id = users.id) AS post_count
        FROM users
        ORDER BY users.id ASC
        """
    ).fetchall()
    return render_template("admin_users.html", users=users)


@app.route("/admin/posts")
@admin_required
def admin_posts():
    db = get_db()
    posts = db.execute(
        """
        SELECT posts.*, users.username
        FROM posts
        JOIN users ON posts.user_id = users.id
        ORDER BY posts.id DESC
        """
    ).fetchall()
    return render_template("admin_posts.html", posts=posts)


@app.route("/admin/chats")
@admin_required
def admin_chats():
    db = get_db()
    chats = db.execute(
        """
        SELECT
            c.id,
            u1.username AS user1_name,
            u2.username AS user2_name,
            (
                SELECT COUNT(*)
                FROM messages m
                WHERE m.conversation_id = c.id
            ) AS message_count,
            c.created_at
        FROM conversations c
        JOIN users u1 ON c.user1_id = u1.id
        JOIN users u2 ON c.user2_id = u2.id
        ORDER BY c.id DESC
        """
    ).fetchall()
    return render_template("admin_chats.html", chats=chats)


@app.route("/admin/chat/<int:conversation_id>")
@admin_required
def admin_chat_view(conversation_id):
    db = get_db()

    conversation = db.execute(
        """
        SELECT
            c.*,
            u1.username AS user1_name,
            u2.username AS user2_name
        FROM conversations c
        JOIN users u1 ON c.user1_id = u1.id
        JOIN users u2 ON c.user2_id = u2.id
        WHERE c.id = ?
        """,
        (conversation_id,),
    ).fetchone()

    if not conversation:
        flash("Conversation not found.", "danger")
        return redirect(url_for("admin_chats"))

    messages = db.execute(
        """
        SELECT m.*, u.username AS sender_name
        FROM messages m
        JOIN users u ON m.sender_id = u.id
        WHERE m.conversation_id = ?
        ORDER BY m.created_at ASC, m.id ASC
        """,
        (conversation_id,),
    ).fetchall()

    return render_template(
        "admin_chat_view.html",
        conversation=conversation,
        messages=messages,
    )


@app.route("/admin/user/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_delete_user(user_id):
    db = get_db()
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    flash("User deleted successfully.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/post/<int:post_id>/delete", methods=["POST"])
@admin_required
def admin_delete_post(post_id):
    db = get_db()
    db.execute("DELETE FROM posts WHERE id = ?", (post_id,))
    db.commit()
    flash("Post deleted successfully.", "success")
    return redirect(url_for("admin_posts"))


if __name__ == "__main__":
    socketio.run(
        app,
        host="0.0.0.0",
        port=5000,
        debug=os.getenv("FLASK_DEBUG") == "1",
        allow_unsafe_werkzeug=True,
    )
