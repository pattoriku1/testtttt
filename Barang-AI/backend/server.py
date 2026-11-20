import base64
import hashlib
import json
import os
import secrets
import smtplib
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BACKEND_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_ROOT.parent
FRONTEND_ROOT = PROJECT_ROOT / "frontend"
DATABASE = BACKEND_ROOT / "barang-ai.db"
PRIVATE_UPLOAD_ROOT = BACKEND_ROOT / "private_uploads"
SESSIONS = {}
AI_WEBHOOK_URL = "https://aitestforligidproject.app.n8n.cloud/webhook/f2e0980e-c584-4738-afdf-56b8a7b436cf/chat"
SIGNUP_CODE_EXPIRY_MINUTES = 1
DOCUMENT_UPLOAD_MAX_BYTES = 5 * 1024 * 1024
DOCUMENT_UPLOAD_TYPES = {"application/pdf", "image/jpeg", "image/png"}
DOCUMENT_SERVICES = {
    "Barangay Clearance": {
        "description": "Apply online and collect the approved physical document at the barangay office.",
        "purpose": "Configurable purpose; confirm official requirements with barangay staff.",
    },
    "Certificate of Indigency": {
        "description": "Submit an application for staff review and physical pickup when approved.",
        "purpose": "Configurable purpose; eligibility and requirements require official confirmation.",
    },
    "Report a Concern": {
        "description": "Send a concern to barangay staff for review and follow-up.",
        "purpose": "Provide enough information for staff to locate and assess the concern.",
    },
}


def load_dotenv():
    env_file = BACKEND_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv()


def now():
    return datetime.now(timezone.utc).isoformat()


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()
    return f"{salt}${digest}"


def verify_password(password, stored):
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    return secrets.compare_digest(hash_password(password, salt).split("$", 1)[1], digest)


def connect():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database():
    with connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                age INTEGER NOT NULL,
                years_lived INTEGER NOT NULL,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('resident', 'staff')),
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                service TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending', 'ongoing', 'finished')),
                details TEXT NOT NULL DEFAULT '',
                archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (account_id) REFERENCES accounts(id)
            );
            CREATE TABLE IF NOT EXISTS unanswered_chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                service TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                reply TEXT,
                replied_at TEXT,
                replied_by INTEGER,
                archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY (account_id) REFERENCES accounts(id)
            );
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL,
                sender_role TEXT NOT NULL CHECK (sender_role IN ('resident', 'staff')),
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (chat_id) REFERENCES unanswered_chats(id),
                FOREIGN KEY (sender_id) REFERENCES accounts(id)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                account_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (account_id) REFERENCES accounts(id)
            );
            CREATE TABLE IF NOT EXISTS signup_verifications (
                email TEXT PRIMARY KEY COLLATE NOCASE,
                payload TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS password_reset_verifications (
                email TEXT PRIMARY KEY COLLATE NOCASE,
                code_hash TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS utility_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                category TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                total_quantity INTEGER NOT NULL CHECK (total_quantity > 0),
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS borrow_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                item_id INTEGER NOT NULL,
                resident_name TEXT NOT NULL,
                contact_number TEXT NOT NULL,
                barangay_id TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                purpose TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'borrowed', 'returned', 'rejected', 'cancelled')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (account_id) REFERENCES accounts(id),
                FOREIGN KEY (item_id) REFERENCES utility_items(id)
            );
            CREATE TABLE IF NOT EXISTS document_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reference_number TEXT NOT NULL UNIQUE,
                account_id INTEGER NOT NULL,
                service TEXT NOT NULL,
                details TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Submitted',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (account_id) REFERENCES accounts(id)
            );
            CREATE TABLE IF NOT EXISTS request_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id INTEGER NOT NULL,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                content_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (request_id) REFERENCES document_requests(id) ON DELETE CASCADE
            );
            """
        )
        PRIVATE_UPLOAD_ROOT.mkdir(mode=0o700, exist_ok=True)
        utility_items = (
            ("Tent", "Shelter", "For barangay events and outdoor activities.", 5),
            ("Plastic chair", "Furniture", "Stackable chairs for events and community gatherings.", 100),
            ("Sound system", "Audio", "Portable sound system with microphone.", 2),
        )
        connection.executemany(
            """INSERT OR IGNORE INTO utility_items
            (name, category, description, total_quantity, created_at)
            VALUES (?, ?, ?, ?, ?)""",
            [(*item, now()) for item in utility_items],
        )
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(unanswered_chats)")}
        for column, definition in (("reply", "TEXT"), ("replied_at", "TEXT"), ("replied_by", "INTEGER")):
            if column not in columns:
                connection.execute(f"ALTER TABLE unanswered_chats ADD COLUMN {column} {definition}")
        account_columns = {row["name"] for row in connection.execute("PRAGMA table_info(accounts)")}
        if "is_active" not in account_columns:
            connection.execute("ALTER TABLE accounts ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
        borrow_columns = {row["name"] for row in connection.execute("PRAGMA table_info(borrow_requests)")}
        for column in ("resident_name", "contact_number", "barangay_id"):
            if column not in borrow_columns:
                connection.execute(f"ALTER TABLE borrow_requests ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        transaction_columns = {row["name"] for row in connection.execute("PRAGMA table_info(transactions)")}
        if "archived" not in transaction_columns:
            connection.execute("ALTER TABLE transactions ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
        chat_columns = {row["name"] for row in connection.execute("PRAGMA table_info(unanswered_chats)")}
        if "archived" not in chat_columns:
            connection.execute("ALTER TABLE unanswered_chats ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
        legacy_chats = connection.execute(
            """SELECT c.id, c.account_id, c.message, c.created_at
            FROM unanswered_chats c
            WHERE NOT EXISTS (SELECT 1 FROM chat_messages m WHERE m.chat_id = c.id)"""
        ).fetchall()
        for chat in legacy_chats:
            connection.execute(
                """INSERT INTO chat_messages (chat_id, sender_id, sender_role, message, created_at)
                VALUES (?, ?, 'resident', ?, ?)""",
                (chat["id"], chat["account_id"], chat["message"], chat["created_at"]),
            )
        legacy_admin = connection.execute(
            "SELECT id FROM accounts WHERE email = ?",
            ("patrickadmin1@ligid.gov.ph",),
        ).fetchone()
        if legacy_admin:
            connection.execute("DELETE FROM sessions WHERE account_id = ?", (legacy_admin["id"],))
            connection.execute("DELETE FROM accounts WHERE id = ?", (legacy_admin["id"],))
        admin = connection.execute(
            "SELECT id FROM accounts WHERE email = ?",
            ("patadmin1@admin.me",),
        ).fetchone()
        if not admin:
            connection.execute(
                """INSERT INTO accounts
                (full_name, age, years_lived, email, password_hash, role, created_at)
                VALUES (?, ?, ?, ?, ?, 'staff', ?)""",
                ("Barangay Administrator", 30, 0, "patadmin1@admin.me", hash_password("patrick1234"), now()),
            )


def account_payload(row):
    return {
        "id": row["id"],
        "name": row["full_name"],
        "age": row["age"],
        "yearsLived": row["years_lived"],
        "email": row["email"],
        "role": row["role"],
    }


def create_document_reference(connection):
    year = datetime.now(timezone.utc).year
    while True:
        sequence = connection.execute(
            "SELECT COUNT(*) + 1 total FROM document_requests WHERE reference_number LIKE ?",
            (f"BAI-{year}-%",),
        ).fetchone()["total"]
        reference = f"BAI-{year}-{sequence:05d}"
        if not connection.execute(
            "SELECT id FROM document_requests WHERE reference_number = ?", (reference,)
        ).fetchone():
            return reference
        year += 1


def send_signup_code(email, code):
    host = os.environ.get("BARANGAI_SMTP_HOST") or os.environ.get("SMTP_HOST")
    port = int(os.environ.get("BARANGAI_SMTP_PORT") or os.environ.get("SMTP_PORT", "587"))
    username = os.environ.get("BARANGAI_SMTP_USERNAME") or os.environ.get("SMTP_EMAIL")
    password = os.environ.get("BARANGAI_SMTP_PASSWORD") or os.environ.get("SMTP_PASSWORD")
    sender = os.environ.get("BARANGAI_SMTP_FROM") or os.environ.get("SMTP_EMAIL") or username or ""
    if host == "smtp.gmail.com" and password:
        password = password.replace(" ", "")
    if not host or not username or not password or not sender:
        raise RuntimeError("Email verification is not configured. Set the BARANGAI_SMTP_* environment variables.")
    message = EmailMessage()
    message["Subject"] = "Your Barang-AI verification code"
    message["From"] = sender
    message["To"] = email
    message.set_content(
        f"Your Barang-AI verification code is {code}. "
        f"It expires in {SIGNUP_CODE_EXPIRY_MINUTES} minute."
    )
    try:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.starttls()
            smtp.login(username, password)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as error:
        raise RuntimeError("We could not send the verification email. Please try again.") from error


def send_password_reset_code(email, code):
    host = os.environ.get("BARANGAI_SMTP_HOST") or os.environ.get("SMTP_HOST")
    port = int(os.environ.get("BARANGAI_SMTP_PORT") or os.environ.get("SMTP_PORT", "587"))
    username = os.environ.get("BARANGAI_SMTP_USERNAME") or os.environ.get("SMTP_EMAIL")
    password = os.environ.get("BARANGAI_SMTP_PASSWORD") or os.environ.get("SMTP_PASSWORD")
    sender = os.environ.get("BARANGAI_SMTP_FROM") or os.environ.get("SMTP_EMAIL") or username or ""
    if host == "smtp.gmail.com" and password:
        password = password.replace(" ", "")
    if not host or not username or not password or not sender:
        raise RuntimeError("Email verification is not configured. Set the BARANGAI_SMTP_* environment variables.")
    message = EmailMessage()
    message["Subject"] = "Reset your Barang-AI password"
    message["From"] = sender
    message["To"] = email
    message.set_content(
        f"Your Barang-AI password reset code is {code}. "
        f"It expires in {SIGNUP_CODE_EXPIRY_MINUTES} minute."
    )
    try:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.starttls()
            smtp.login(username, password)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as error:
        raise RuntimeError("We could not send the verification email. Please try again.") from error


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FRONTEND_ROOT), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def ask_ai(self, data, account):
        request_body = json.dumps(
            {
                "chatInput": str(data.get("message", "")).strip(),
                "message": str(data.get("message", "")).strip(),
                "sessionId": str(data.get("sessionId", f"barang-{account['id']}")),
                "service": data.get("service", "General inquiry"),
                "user": {
                    "id": account["id"],
                    "name": account.get("name", ""),
                    "email": account.get("email", ""),
                },
            }
        ).encode()
        request = urllib.request.Request(
            AI_WEBHOOK_URL,
            data=request_body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode())
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise RuntimeError("The AI assistant is temporarily unavailable.") from error
        if isinstance(payload, list):
            payload = payload[0] if payload else {}
        if not isinstance(payload, dict):
            raise RuntimeError("The AI assistant returned an invalid response.")
        reply = payload.get("reply") or payload.get("output") or payload.get("response") or payload.get("text") or payload.get("message")
        if not isinstance(reply, str) or not reply.strip():
            raise RuntimeError("The AI assistant returned an empty response.")
        return reply.strip()

    def token_account(self):
        token = self.headers.get("Authorization", "").replace("Bearer ", "", 1).strip()
        if token in SESSIONS:
            cached = SESSIONS[token]
            with connect() as connection:
                active = connection.execute(
                    "SELECT is_active FROM accounts WHERE id = ?", (cached["id"],)
                ).fetchone()
            if active and active["is_active"]:
                return cached
            SESSIONS.pop(token, None)
            return None
        if not token:
            return None
        with connect() as connection:
            row = connection.execute(
                """SELECT a.id, a.full_name name, a.age, a.years_lived yearsLived,
                a.email, a.role FROM sessions s JOIN accounts a ON a.id = s.account_id
                WHERE s.token = ?""",
                (token,),
            ).fetchone()
        if not row:
            return None
        account = dict(row)
        account["yearsLived"] = account.pop("yearsLived")
        SESSIONS[token] = account
        return account

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            data = self.read_json()
            if path == "/api/auth/request-signup-code":
                required = ("fullName", "age", "yearsLived", "email", "password")
                if any(not str(data.get(field, "")).strip() for field in required):
                    return self.send_json(400, {"error": "All signup fields are required."})
                if len(data["password"]) < 8:
                    return self.send_json(400, {"error": "Password must be at least 8 characters."})
                email = data["email"].strip().lower()
                with connect() as connection:
                    if connection.execute("SELECT id FROM accounts WHERE email = ?", (email,)).fetchone():
                        return self.send_json(409, {"error": "An account with this email already exists."})
                code = f"{secrets.randbelow(1_000_000):06d}"
                payload = {
                    "fullName": data["fullName"].strip(),
                    "age": int(data["age"]),
                    "yearsLived": int(data["yearsLived"]),
                    "email": email,
                    "password": data["password"],
                }
                send_signup_code(email, code)
                expires_at = datetime.now(timezone.utc).timestamp() + SIGNUP_CODE_EXPIRY_MINUTES * 60
                with connect() as connection:
                    connection.execute(
                        """INSERT OR REPLACE INTO signup_verifications
                        (email, payload, code_hash, expires_at, created_at)
                        VALUES (?, ?, ?, ?, ?)""",
                        (email, json.dumps(payload), hash_password(code),
                         datetime.fromtimestamp(expires_at, timezone.utc).isoformat(), now()),
                    )
                return self.send_json(200, {"message": "A verification code was sent to your email."})

            if path == "/api/auth/verify-signup-code":
                email = str(data.get("email", "")).strip().lower()
                code = str(data.get("code", "")).strip()
                if not email or len(code) != 6 or not code.isdigit():
                    return self.send_json(400, {"error": "Enter the six-digit verification code."})
                with connect() as connection:
                    verification = connection.execute(
                        "SELECT * FROM signup_verifications WHERE email = ?", (email,)
                    ).fetchone()
                    if not verification:
                        return self.send_json(400, {"error": "Request a new verification code first."})
                    if datetime.fromisoformat(verification["expires_at"]) <= datetime.now(timezone.utc):
                        connection.execute("DELETE FROM signup_verifications WHERE email = ?", (email,))
                        return self.send_json(400, {"error": "That verification code has expired. Request a new one."})
                    if not verify_password(code, verification["code_hash"]):
                        return self.send_json(400, {"error": "That verification code is incorrect."})
                    payload = json.loads(verification["payload"])
                    try:
                        cursor = connection.execute(
                            """INSERT INTO accounts
                            (full_name, age, years_lived, email, password_hash, role, created_at)
                            VALUES (?, ?, ?, ?, ?, 'resident', ?)""",
                            (payload["fullName"], payload["age"], payload["yearsLived"],
                             payload["email"], hash_password(payload["password"]), now()),
                        )
                    except sqlite3.IntegrityError:
                        return self.send_json(409, {"error": "An account with this email already exists."})
                    row = connection.execute("SELECT * FROM accounts WHERE id = ?", (cursor.lastrowid,)).fetchone()
                    connection.execute("DELETE FROM signup_verifications WHERE email = ?", (email,))
                return self.send_json(201, self.start_session(row))

            if path == "/api/auth/signup":
                return self.send_json(410, {"error": "Email verification is required before signup."})

            if path == "/api/auth/login":
                with connect() as connection:
                    row = connection.execute("SELECT * FROM accounts WHERE email = ?", (data.get("email", "").strip(),)).fetchone()
                if not row or not row["is_active"] or not verify_password(data.get("password", ""), row["password_hash"]):
                    return self.send_json(401, {"error": "We couldn't match that email and password."})
                return self.send_json(200, self.start_session(row))

            if path == "/api/auth/request-password-reset":
                email = str(data.get("email", "")).strip().lower()
                if not email:
                    return self.send_json(400, {"error": "Enter your email address."})
                with connect() as connection:
                    account = connection.execute(
                        "SELECT id FROM accounts WHERE email = ? AND is_active = 1", (email,)
                    ).fetchone()
                if account:
                    code = f"{secrets.randbelow(1_000_000):06d}"
                    send_password_reset_code(email, code)
                    expires_at = datetime.now(timezone.utc).timestamp() + SIGNUP_CODE_EXPIRY_MINUTES * 60
                    with connect() as connection:
                        connection.execute(
                            """INSERT OR REPLACE INTO password_reset_verifications
                            (email, code_hash, expires_at, created_at)
                            VALUES (?, ?, ?, ?)""",
                            (email, hash_password(code),
                             datetime.fromtimestamp(expires_at, timezone.utc).isoformat(), now()),
                        )
                return self.send_json(200, {"message": "If an active account uses that email, a verification code was sent."})

            if path == "/api/auth/reset-password":
                email = str(data.get("email", "")).strip().lower()
                code = str(data.get("code", "")).strip()
                new_password = str(data.get("newPassword", ""))
                if not email or len(code) != 6 or not code.isdigit():
                    return self.send_json(400, {"error": "Enter the six-digit verification code."})
                if len(new_password) < 8:
                    return self.send_json(400, {"error": "Password must be at least 8 characters."})
                with connect() as connection:
                    verification = connection.execute(
                        "SELECT * FROM password_reset_verifications WHERE email = ?", (email,)
                    ).fetchone()
                    if not verification:
                        return self.send_json(400, {"error": "Request a new verification code first."})
                    if datetime.fromisoformat(verification["expires_at"]) <= datetime.now(timezone.utc):
                        connection.execute("DELETE FROM password_reset_verifications WHERE email = ?", (email,))
                        return self.send_json(400, {"error": "That verification code has expired. Request a new one."})
                    if not verify_password(code, verification["code_hash"]):
                        return self.send_json(400, {"error": "That verification code is incorrect."})
                    cursor = connection.execute(
                        "UPDATE accounts SET password_hash = ? WHERE email = ? AND is_active = 1",
                        (hash_password(new_password), email),
                    )
                    if cursor.rowcount != 1:
                        return self.send_json(400, {"error": "That account is not available."})
                    connection.execute("DELETE FROM password_reset_verifications WHERE email = ?", (email,))
                    connection.execute(
                        "DELETE FROM sessions WHERE account_id = (SELECT id FROM accounts WHERE email = ?)",
                        (email,),
                    )
                return self.send_json(200, {"message": "Password reset successfully. You can now log in."})

            account = self.token_account()
            if not account:
                return self.send_json(401, {"error": "Please log in first."})

            if path == "/api/borrow-requests":
                if account["role"] != "resident":
                    return self.send_json(403, {"error": "Only residents can submit borrow requests."})
                try:
                    item_id = int(data.get("itemId"))
                    quantity = int(data.get("quantity"))
                except (TypeError, ValueError):
                    return self.send_json(400, {"error": "Select an item and a valid quantity."})
                purpose = str(data.get("purpose", "")).strip()
                resident_name = str(data.get("residentName", "")).strip()
                contact_number = str(data.get("contactNumber", "")).strip()
                barangay_id = str(data.get("barangayId", "")).strip()
                start_date = str(data.get("startDate", "")).strip()
                end_date = str(data.get("endDate", "")).strip()
                try:
                    start = datetime.strptime(start_date, "%Y-%m-%d").date()
                    end = datetime.strptime(end_date, "%Y-%m-%d").date()
                except ValueError:
                    return self.send_json(400, {"error": "Choose valid start and end dates."})
                if end < start:
                    return self.send_json(400, {"error": "End date must be on or after the start date."})
                if quantity < 1 or not purpose or not resident_name or not contact_number or not barangay_id:
                    return self.send_json(400, {"error": "Resident name, contact number, barangay ID, quantity, and purpose are required."})
                with connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    item = connection.execute(
                        "SELECT id, name, total_quantity FROM utility_items WHERE id = ? AND is_active = 1",
                        (item_id,),
                    ).fetchone()
                    if not item:
                        return self.send_json(404, {"error": "That utility is not available."})
                    reserved = connection.execute(
                        """SELECT COALESCE(SUM(quantity), 0) reserved
                        FROM borrow_requests
                        WHERE item_id = ? AND status IN ('pending', 'approved', 'borrowed')
                        AND start_date <= ? AND end_date >= ?""",
                        (item_id, end_date, start_date),
                    ).fetchone()["reserved"]
                    if reserved + quantity > item["total_quantity"]:
                        return self.send_json(409, {"error": f"Only {max(item['total_quantity'] - reserved, 0)} {item['name']} available for those dates."})
                    cursor = connection.execute(
                        """INSERT INTO borrow_requests
                        (account_id, item_id, resident_name, contact_number, barangay_id, quantity,
                        purpose, start_date, end_date, status, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                        (account["id"], item_id, resident_name, contact_number, barangay_id,
                         quantity, purpose, start_date, end_date, now(), now()),
                    )
                return self.send_json(201, {"id": cursor.lastrowid, "status": "pending"})

            if path == "/api/auth/change-password":
                with connect() as connection:
                    row = connection.execute("SELECT * FROM accounts WHERE id = ?", (account["id"],)).fetchone()
                    if not row or not verify_password(data.get("currentPassword", ""), row["password_hash"]):
                        return self.send_json(400, {"error": "Current password is incorrect."})
                    connection.execute("UPDATE accounts SET password_hash = ? WHERE id = ?", (hash_password(data["newPassword"]), account["id"]))
                return self.send_json(200, {"message": "Password updated successfully."})

            if path.startswith("/api/staff/borrow-requests/") and path.endswith("/status"):
                if account["role"] != "staff":
                    return self.send_json(403, {"error": "Staff access is required."})
                request_id = path.split("/")[4]
                status = str(data.get("status", "")).strip().lower()
                if status not in {"approved", "rejected", "borrowed", "returned", "cancelled"}:
                    return self.send_json(400, {"error": "Invalid borrow request status."})
                with connect() as connection:
                    request = connection.execute(
                        "SELECT id, status FROM borrow_requests WHERE id = ?", (request_id,)
                    ).fetchone()
                    if not request:
                        return self.send_json(404, {"error": "Borrow request not found."})
                    connection.execute(
                        "UPDATE borrow_requests SET status = ?, updated_at = ? WHERE id = ?",
                        (status, now(), request_id),
                    )
                return self.send_json(200, {"message": "Borrow request status updated."})

            if path.startswith("/api/staff/accounts/") and path.endswith("/reset-password"):
                if account["role"] != "staff":
                    return self.send_json(403, {"error": "Staff access is required."})
                account_id = path.split("/")[4]
                new_password = str(data.get("password", "")).strip()
                if len(new_password) < 8:
                    return self.send_json(400, {"error": "Password must be at least 8 characters."})
                with connect() as connection:
                    target = connection.execute("SELECT id, role FROM accounts WHERE id = ?", (account_id,)).fetchone()
                    if not target or target["role"] != "resident":
                        return self.send_json(404, {"error": "Resident account not found."})
                    connection.execute("UPDATE accounts SET password_hash = ? WHERE id = ?", (hash_password(new_password), account_id))
                return self.send_json(200, {"message": "Password reset successfully."})

            if path.startswith("/api/transactions/") and path.endswith("/status"):
                if account["role"] != "staff":
                    return self.send_json(403, {"error": "Staff access is required."})
                transaction_id = path.split("/")[3]
                status = str(data.get("status", "")).strip().lower()
                if status not in {"pending", "ongoing", "finished"}:
                    return self.send_json(400, {"error": "Invalid transaction status."})
                with connect() as connection:
                    target = connection.execute("SELECT id FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
                    if not target:
                        return self.send_json(404, {"error": "Transaction not found."})
                    connection.execute(
                        "UPDATE transactions SET status = ?, updated_at = ? WHERE id = ?",
                        (status, now(), transaction_id),
                    )
                return self.send_json(200, {"message": "Transaction status updated."})

            if path.startswith("/api/transactions/") and path.endswith("/archive"):
                if account["role"] != "staff":
                    return self.send_json(403, {"error": "Staff access is required."})
                transaction_id = path.split("/")[3]
                archived = bool(data.get("archived", False))
                with connect() as connection:
                    target = connection.execute("SELECT id FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
                    if not target:
                        return self.send_json(404, {"error": "Transaction not found."})
                    connection.execute(
                        "UPDATE transactions SET archived = ?, updated_at = ? WHERE id = ?",
                        (1 if archived else 0, now(), transaction_id),
                    )
                return self.send_json(200, {"message": "Transaction archive state updated."})

            if path.startswith("/api/transactions/") and path.endswith("/delete"):
                if account["role"] != "staff":
                    return self.send_json(403, {"error": "Staff access is required."})
                transaction_id = path.split("/")[3]
                with connect() as connection:
                    target = connection.execute("SELECT id FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
                    if not target:
                        return self.send_json(404, {"error": "Transaction not found."})
                    connection.execute("DELETE FROM transactions WHERE id = ?", (transaction_id,))
                return self.send_json(200, {"message": "Transaction deleted."})

            if path == "/api/document-requests":
                if not account or account["role"] != "resident":
                    return self.send_json(403, {"error": "Resident access is required."})
                service = str(data.get("service", "")).strip()
                if service not in DOCUMENT_SERVICES:
                    return self.send_json(400, {"error": "Choose a valid barangay service."})
                details = data.get("details")
                if not isinstance(details, dict):
                    return self.send_json(400, {"error": "Application details are required."})
                purpose = str(details.get("purpose", "")).strip()
                if not purpose or len(purpose) > 500:
                    return self.send_json(400, {"error": "Enter a valid request purpose."})
                location = str(details.get("location", "")).strip()
                if not location or len(location) > 300:
                    return self.send_json(400, {"error": "Enter a valid address or concern location."})
                files = data.get("files", [])
                if not isinstance(files, list) or len(files) > 5:
                    return self.send_json(400, {"error": "You may upload up to five supporting files."})
                prepared_files = []
                for item in files:
                    if not isinstance(item, dict):
                        return self.send_json(400, {"error": "Invalid supporting document."})
                    content_type = str(item.get("contentType", "")).lower()
                    original_name = Path(str(item.get("name", ""))).name
                    if content_type not in DOCUMENT_UPLOAD_TYPES or not original_name:
                        return self.send_json(400, {"error": "Only PDF, JPG, and PNG files are accepted."})
                    try:
                        content = base64.b64decode(str(item.get("content", "")), validate=True)
                    except (ValueError, TypeError):
                        return self.send_json(400, {"error": "A supporting document could not be read."})
                    if not content or len(content) > DOCUMENT_UPLOAD_MAX_BYTES:
                        return self.send_json(400, {"error": "Each supporting document must be smaller than 5 MB."})
                    prepared_files.append((original_name, content_type, content))
                timestamp = now()
                with connect() as connection:
                    reference = create_document_reference(connection)
                    cursor = connection.execute(
                        """INSERT INTO document_requests
                        (reference_number, account_id, service, details, status, created_at, updated_at)
                        VALUES (?, ?, ?, ?, 'Submitted', ?, ?)""",
                        (reference, account["id"], service, json.dumps(details), timestamp, timestamp),
                    )
                    request_id = cursor.lastrowid
                    for original_name, content_type, content in prepared_files:
                        stored_name = f"{secrets.token_urlsafe(24)}.bin"
                        (PRIVATE_UPLOAD_ROOT / stored_name).write_bytes(content)
                        connection.execute(
                            """INSERT INTO request_documents
                            (request_id, original_name, stored_name, content_type, size_bytes, created_at)
                            VALUES (?, ?, ?, ?, ?, ?)""",
                            (request_id, original_name, stored_name, content_type, len(content), timestamp),
                        )
                return self.send_json(
                    201,
                    {"referenceNumber": reference, "service": service, "status": "Submitted", "createdAt": timestamp},
                )

            if path == "/api/transactions":
                status = str(data.get("status", "pending")).strip().lower()
                if status not in {"pending", "ongoing", "finished"}:
                    return self.send_json(400, {"error": "Invalid transaction status."})
                with connect() as connection:
                    cursor = connection.execute(
                        """INSERT INTO transactions (account_id, service, status, details, archived, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (account["id"], data.get("service", "General inquiry"), status, data.get("details", ""), 0, now(), now()),
                    )
                return self.send_json(201, {"id": cursor.lastrowid, "status": status})

            if path == "/api/ai/chat":
                message = str(data.get("message", "")).strip()
                if not message:
                    return self.send_json(400, {"error": "Message cannot be empty."})
                return self.send_json(
                    200,
                    {"reply": self.ask_ai(data, account)},
                )

            if path == "/api/chats/escalate":
                with connect() as connection:
                    service = data.get("service", "General inquiry")
                    message = data.get("message", "")
                    cursor = connection.execute(
                        """INSERT INTO unanswered_chats (account_id, service, message, created_at)
                        VALUES (?, ?, ?, ?)""",
                        (account["id"], service, message, now()),
                    )
                    chat_id = cursor.lastrowid
                    connection.execute(
                        """INSERT INTO chat_messages (chat_id, sender_id, sender_role, message, created_at)
                        VALUES (?, ?, 'resident', ?, ?)""",
                        (chat_id, account["id"], message, now()),
                    )
                return self.send_json(201, {"id": chat_id})

            if path.startswith("/api/chats/") and path.endswith("/messages"):
                chat_id = path.split("/")[3]
                message = str(data.get("message", "")).strip()
                if not message:
                    return self.send_json(400, {"error": "Message cannot be empty."})
                with connect() as connection:
                    chat = connection.execute("SELECT * FROM unanswered_chats WHERE id = ?", (chat_id,)).fetchone()
                    if not chat or (account["role"] == "resident" and chat["account_id"] != account["id"]):
                        return self.send_json(404, {"error": "Conversation not found."})
                    sender_role = account["role"]
                    connection.execute(
                        """INSERT INTO chat_messages (chat_id, sender_id, sender_role, message, created_at)
                        VALUES (?, ?, ?, ?, ?)""",
                        (chat_id, account["id"], sender_role, message, now()),
                    )
                    connection.execute(
                        "UPDATE unanswered_chats SET status = 'open', archived = 0 WHERE id = ?",
                        (chat_id,),
                    )
                return self.send_json(201, {"message": "Message sent."})
        except (ValueError, json.JSONDecodeError):
            return self.send_json(400, {"error": "Invalid request data."})
        except RuntimeError as error:
            return self.send_json(502, {"error": str(error)})
        except Exception:
            return self.send_json(500, {"error": "The server could not complete this request."})
        self.send_error(404)

    def do_GET(self):
        path = urlparse(self.path).path
        account = self.token_account()
        if path == "/api/document-services":
            return self.send_json(
                200,
                {
                    "services": [
                        {"name": name, **details, "requirements": "Requirements are configurable and pending official confirmation."}
                        for name, details in DOCUMENT_SERVICES.items()
                    ]
                },
            )
        if path == "/api/document-requests":
            if not account:
                return self.send_json(401, {"error": "Please log in first."})
            with connect() as connection:
                requests = connection.execute(
                    """SELECT reference_number referenceNumber, service, details, status,
                    created_at createdAt, updated_at updatedAt
                    FROM document_requests WHERE account_id = ? ORDER BY created_at DESC""",
                    (account["id"],),
                ).fetchall()
            return self.send_json(200, {"requests": [dict(item) for item in requests]})
        if path == "/api/utility-items":
            if not account:
                return self.send_json(401, {"error": "Please log in first."})
            query = urlparse(self.path).query
            params = dict(part.split("=", 1) for part in query.split("&") if "=" in part)
            start_date = params.get("startDate", datetime.now(timezone.utc).date().isoformat())
            end_date = params.get("endDate", start_date)
            try:
                datetime.strptime(start_date, "%Y-%m-%d")
                datetime.strptime(end_date, "%Y-%m-%d")
            except ValueError:
                return self.send_json(400, {"error": "Choose valid dates."})
            with connect() as connection:
                items = connection.execute(
                    """SELECT i.id, i.name, i.category, i.description, i.total_quantity totalQuantity,
                    i.total_quantity - COALESCE(SUM(CASE WHEN r.status IN ('pending', 'approved', 'borrowed')
                    AND r.start_date <= ? AND r.end_date >= ? THEN r.quantity ELSE 0 END), 0) availableQuantity
                    FROM utility_items i LEFT JOIN borrow_requests r ON r.item_id = i.id
                    WHERE i.is_active = 1 GROUP BY i.id ORDER BY i.name""",
                    (end_date, start_date),
                ).fetchall()
            return self.send_json(200, {
                "startDate": start_date,
                "endDate": end_date,
                "items": [dict(item) for item in items],
            })
        if path == "/api/chats":
            if not account:
                return self.send_json(401, {"error": "Please log in first."})
            with connect() as connection:
                chats = connection.execute(
                    """SELECT id, service, message, reply, status, archived, created_at, replied_at
                    FROM unanswered_chats c
                    WHERE account_id = ?
                    AND c.id = (
                        SELECT MAX(latest.id) FROM unanswered_chats latest
                        WHERE latest.account_id = c.account_id
                        AND latest.service = c.service
                    )
                    ORDER BY created_at DESC""",
                    (account["id"],),
                ).fetchall()
                result = []
                for chat in chats:
                    messages = connection.execute(
                        """SELECT sender_role, message, created_at FROM chat_messages
                        WHERE chat_id = ? ORDER BY created_at, id""", (chat["id"],)
                    ).fetchall()
                    result.append({**dict(chat), "messages": [dict(message) for message in messages]})
            return self.send_json(200, {"chats": result})
        if path == "/api/transactions":
            if not account:
                return self.send_json(401, {"error": "Please log in first."})
            with connect() as connection:
                transactions = connection.execute(
                    """SELECT id, service, status, details, archived, created_at createdAt, updated_at updatedAt
                    FROM transactions
                    WHERE account_id = ?
                    ORDER BY created_at DESC""",
                    (account["id"],),
                ).fetchall()
            return self.send_json(200, {"transactions": [dict(item) for item in transactions]})
        if path == "/api/borrow-requests":
            if not account:
                return self.send_json(401, {"error": "Please log in first."})
            with connect() as connection:
                requests = connection.execute(
                    """SELECT r.id, i.name itemName, r.resident_name residentName,
                    r.contact_number contactNumber, r.barangay_id barangayId, r.quantity, r.purpose, r.start_date startDate,
                    r.end_date endDate, r.status, r.created_at createdAt
                    FROM borrow_requests r JOIN utility_items i ON i.id = r.item_id
                    WHERE r.account_id = ? ORDER BY r.created_at DESC""",
                    (account["id"],),
                ).fetchall()
            return self.send_json(200, {"requests": [dict(item) for item in requests]})
        if path.startswith("/api/chats/") and path.endswith("/messages"):
            account = self.token_account()
            if not account:
                return self.send_json(401, {"error": "Please log in first."})
            chat_id = path.split("/")[3]
            with connect() as connection:
                chat = connection.execute("SELECT * FROM unanswered_chats WHERE id = ?", (chat_id,)).fetchone()
                if not chat or (account["role"] == "resident" and chat["account_id"] != account["id"]):
                    return self.send_json(404, {"error": "Conversation not found."})
                messages = connection.execute(
                    """SELECT sender_role, message, created_at FROM chat_messages
                    WHERE chat_id = ? ORDER BY created_at, id""", (chat_id,)
                ).fetchall()
            return self.send_json(200, {"chat": dict(chat), "messages": [dict(message) for message in messages]})
        if path == "/api/staff/overview":
            if not account or account["role"] != "staff":
                return self.send_json(403, {"error": "Staff access is required."})
            with connect() as connection:
                counts = {row["status"]: row["total"] for row in connection.execute("SELECT status, COUNT(*) total FROM transactions WHERE archived = 0 GROUP BY status")}
                transactions = connection.execute(
                    """SELECT t.id, t.service, t.status, t.archived, t.created_at createdAt, a.full_name resident
                    FROM transactions t JOIN accounts a ON a.id = t.account_id
                    WHERE t.archived = 0
                    ORDER BY t.created_at DESC"""
                ).fetchall()
                chats = connection.execute(
                    """SELECT c.id, c.service, c.message, c.status, a.full_name name
                    FROM unanswered_chats c JOIN accounts a ON a.id = c.account_id
                    WHERE c.status IN ('open', 'answered') AND c.archived = 0
                    AND c.id = (
                        SELECT MAX(latest.id) FROM unanswered_chats latest
                        WHERE latest.account_id = c.account_id
                        AND latest.service = c.service
                        AND latest.status IN ('open', 'answered') AND latest.archived = 0
                    )
                    ORDER BY c.created_at DESC"""
                ).fetchall()
                result = []
                for chat in chats:
                    messages = connection.execute(
                        """SELECT sender_role, message, created_at FROM chat_messages
                        WHERE chat_id = ? ORDER BY created_at, id""", (chat["id"],)
                    ).fetchall()
                    result.append({**dict(chat), "messages": [dict(message) for message in messages]})
                archived_chats = connection.execute(
                    """SELECT c.id, c.service, c.message, c.status, a.full_name name
                    FROM unanswered_chats c JOIN accounts a ON a.id = c.account_id
                    WHERE c.archived = 1 ORDER BY c.created_at DESC"""
                ).fetchall()
                archived_result = []
                for chat in archived_chats:
                    messages = connection.execute(
                        """SELECT sender_role, message, created_at FROM chat_messages
                        WHERE chat_id = ? ORDER BY created_at, id""", (chat["id"],)
                    ).fetchall()
                    archived_result.append({**dict(chat), "messages": [dict(message) for message in messages]})
                borrow_requests = connection.execute(
                    """SELECT r.id, i.name itemName,
                    COALESCE(NULLIF(r.resident_name, ''), a.full_name) residentName,
                    COALESCE(NULLIF(r.contact_number, ''), 'Contact not provided') contactNumber,
                    COALESCE(NULLIF(r.barangay_id, ''), 'ID not provided') barangayId, r.quantity,
                    r.purpose, r.start_date startDate, r.end_date endDate, r.status,
                    r.created_at createdAt
                    FROM borrow_requests r JOIN utility_items i ON i.id = r.item_id
                    JOIN accounts a ON a.id = r.account_id
                    ORDER BY r.created_at DESC"""
                ).fetchall()
            return self.send_json(
                200,
                {
                    "counts": counts,
                    "transactions": [dict(item) for item in transactions],
                    "chats": result,
                    "archivedChats": archived_result,
                    "borrowRequests": [dict(item) for item in borrow_requests],
                },
            )
        if path == "/api/staff/accounts":
            if not account or account["role"] != "staff":
                return self.send_json(403, {"error": "Staff access is required."})
            with connect() as connection:
                accounts = connection.execute(
                    """SELECT id, full_name name, age, years_lived yearsLived, email, role,
                    is_active isActive, created_at createdAt
                    FROM accounts ORDER BY created_at DESC"""
                ).fetchall()
            return self.send_json(200, {"accounts": [dict(item) for item in accounts]})
        if path == "/api/health":
            return self.send_json(200, {"ok": True})
        return super().do_GET()

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/api/staff/accounts/"):
            account = self.token_account()
            if not account:
                return self.send_json(401, {"error": "Please log in first."})
            if account["role"] != "staff":
                return self.send_json(403, {"error": "Staff access is required."})
            account_id = path.split("/")[4]
            with connect() as connection:
                target = connection.execute("SELECT id, role, email FROM accounts WHERE id = ?", (account_id,)).fetchone()
                if not target or target["role"] != "resident":
                    return self.send_json(403, {"error": "Only resident accounts can be deleted."})
                connection.execute("DELETE FROM sessions WHERE account_id = ?", (account_id,))
                connection.execute("DELETE FROM chat_messages WHERE chat_id IN (SELECT id FROM unanswered_chats WHERE account_id = ?)", (account_id,))
                connection.execute("DELETE FROM unanswered_chats WHERE account_id = ?", (account_id,))
                connection.execute("DELETE FROM transactions WHERE account_id = ?", (account_id,))
                connection.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            return self.send_json(200, {"message": "Resident account deleted."})
        if not path.startswith("/api/chats/"):
            return self.send_error(404)
        account = self.token_account()
        if not account:
            return self.send_json(401, {"error": "Please log in first."})
        if account["role"] != "staff":
            return self.send_json(403, {"error": "Staff access is required."})
        chat_id = path.split("/")[3]
        if not chat_id.isdigit():
            return self.send_json(400, {"error": "Invalid conversation."})
        with connect() as connection:
            chat = connection.execute(
                "SELECT id FROM unanswered_chats WHERE id = ?", (chat_id,)
            ).fetchone()
            if not chat:
                return self.send_json(404, {"error": "Conversation not found."})
            connection.execute("DELETE FROM chat_messages WHERE chat_id = ?", (chat_id,))
            connection.execute("DELETE FROM unanswered_chats WHERE id = ?", (chat_id,))
        return self.send_json(200, {"message": "Conversation removed."})

    def do_PATCH(self):
        path = urlparse(self.path).path
        if path.startswith("/api/chats/"):
            account = self.token_account()
            if not account:
                return self.send_json(401, {"error": "Please log in first."})
            if account["role"] != "staff":
                return self.send_json(403, {"error": "Staff access is required."})
            chat_id = path.split("/")[3]
            try:
                data = self.read_json()
            except (ValueError, json.JSONDecodeError):
                return self.send_json(400, {"error": "Invalid request data."})
            if "archived" not in data:
                return self.send_json(400, {"error": "Archive state is required."})
            with connect() as connection:
                chat = connection.execute(
                    "SELECT id, account_id, service FROM unanswered_chats WHERE id = ?",
                    (chat_id,),
                ).fetchone()
                if not chat:
                    return self.send_json(404, {"error": "Conversation not found."})
                connection.execute(
                    """UPDATE unanswered_chats SET archived = ?
                    WHERE account_id = ? AND service = ?""",
                    (1 if data["archived"] else 0, chat["account_id"], chat["service"]),
                )
            return self.send_json(200, {"message": "Conversation archive state updated."})
        if not path.startswith("/api/staff/accounts/"):
            return self.send_error(404)
        account = self.token_account()
        if not account:
            return self.send_json(401, {"error": "Please log in first."})
        if account["role"] != "staff":
            return self.send_json(403, {"error": "Staff access is required."})
        account_id = path.split("/")[4]
        try:
            data = self.read_json()
        except (ValueError, json.JSONDecodeError):
            return self.send_json(400, {"error": "Invalid request data."})
        with connect() as connection:
            target = connection.execute("SELECT id, role FROM accounts WHERE id = ?", (account_id,)).fetchone()
            if not target or target["role"] != "resident":
                return self.send_json(403, {"error": "Only resident accounts can be managed."})
            connection.execute("UPDATE accounts SET is_active = ? WHERE id = ?", (1 if data.get("isActive") else 0, account_id))
        return self.send_json(200, {"message": "Account status updated."})

    def start_session(self, row):
        token = secrets.token_urlsafe(32)
        payload = account_payload(row)
        SESSIONS[token] = payload
        with connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO sessions (token, account_id, created_at) VALUES (?, ?, ?)",
                (token, row["id"], now()),
            )
        return {"token": token, "user": payload}


if __name__ == "__main__":
    initialize_database()
    print("Barang-AI running at http://localhost:8000")
    ThreadingHTTPServer(("127.0.0.1", 8000), Handler).serve_forever()
