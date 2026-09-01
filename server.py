import os
import random
import sqlite3
import threading
import time
import socket

HOST = "0.0.0.0"
PORT = 5000

DB = "anonmessenger.db"

waiting = None
rooms = {}

# Подключённые пользователи:
# user_id -> socket
connected_users = {}

# Активные звонки:
# call_id -> данные звонка
active_calls = {}

lock = threading.Lock()



def init_labels_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS account_labels (
            user_id INTEGER PRIMARY KEY,
            label TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        )
    """)
    conn.commit()


def db():
    conn = sqlite3.connect(DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT UNIQUE,
            nickname TEXT,
            telegram_id TEXT,
            created_at INTEGER
        )
    """)
    conn.commit()

    # Миграция старой таблицы users.
    # Добавляем поля, необходимые для авторизации и Telegram-регистрации.
    columns = {
        "public_id": "TEXT",
        "phone_hash": "TEXT",
        "description": "TEXT DEFAULT ''",
        "avatar": "TEXT DEFAULT ''",
        "token": "TEXT",
        "balance": "INTEGER DEFAULT 0",
        "verified": "INTEGER DEFAULT 0",
        "banned": "INTEGER DEFAULT 0",
        "telegram_username": "TEXT DEFAULT ''",
        "telegram_first_name": "TEXT DEFAULT ''",
        "telegram_last_name": "TEXT DEFAULT ''"
    }

    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info(users)").fetchall()
    }

    for name, definition in columns.items():
        if name not in existing:
            conn.execute(
                f"ALTER TABLE users ADD COLUMN {name} {definition}"
            )

    conn.commit()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS account_labels (
            user_id INTEGER PRIMARY KEY,
            scam INTEGER DEFAULT 0,
            fake INTEGER DEFAULT 0,
            verified INTEGER DEFAULT 0
        )
    """)
    conn.commit()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS private_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER NOT NULL,
            receiver_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            status INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL
        )
    """)
    conn.commit()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS private_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER NOT NULL,
            receiver_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            status INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL
        )
    """)
    conn.commit()

    return conn


def send(sock, text):
    try:
        sock.sendall((text + "\n").encode("utf-8"))
    except Exception:
        pass


def generate_code():
    return str(random.randint(100000, 999999))


def get_account_labels(user_id):
    conn = db()
    try:
        row = conn.execute(
            """
            SELECT scam, fake, verified
            FROM account_labels
            WHERE user_id=?
            """,
            (user_id,)
        ).fetchone()

        if not row:
            conn.execute(
                """
                INSERT OR IGNORE INTO account_labels
                (user_id, scam, fake, verified)
                VALUES (?, 0, 0, 0)
                """,
                (user_id,)
            )
            conn.commit()

            return {
                "scam": 0,
                "fake": 0,
                "verified": 0
            }

        return {
            "scam": row[0],
            "fake": row[1],
            "verified": row[2]
        }

    finally:
        conn.close()


def set_account_label(user_id, label, enabled):
    allowed = {
        "scam": "scam",
        "fake": "fake",
        "verified": "verified"
    }

    column = allowed.get(label)

    if not column:
        return False

    conn = db()

    try:
        conn.execute(
            f"""
            INSERT OR IGNORE INTO account_labels
            (user_id, scam, fake, verified)
            VALUES (?, 0, 0, 0)
            """,
            (user_id,)
        )

        conn.execute(
            f"""
            UPDATE account_labels
            SET {column}=?
            WHERE user_id=?
            """,
            (1 if enabled else 0, user_id)
        )

        conn.commit()
        return True

    finally:
        conn.close()


def send_gift(sock, sender_id, receiver_public_id, gift_id):

    conn = db()

    try:
        receiver = conn.execute(
            "SELECT id, nickname FROM users WHERE public_id=?",
            (receiver_public_id,)
        ).fetchone()

        if not receiver:
            send(sock, "ERROR|Пользователь не найден")
            return

        gift = conn.execute(
            "SELECT id, name, emoji, price FROM gifts WHERE id=?",
            (gift_id,)
        ).fetchone()

        if not gift:
            send(sock, "ERROR|Подарок не найден")
            return

        receiver_id = receiver[0]
        gift_name = gift[1]
        gift_emoji = gift[2]
        price = gift[3]

        if sender_id == receiver_id:
            send(sock, "ERROR|Нельзя подарить подарок самому себе")
            return

        conn.execute("BEGIN IMMEDIATE")

        sender = conn.execute(
            "SELECT balance, banned FROM users WHERE id=?",
            (sender_id,)
        ).fetchone()

        if not sender:
            conn.rollback()
            send(sock, "ERROR|Аккаунт не найден")
            return

        if sender[1]:
            conn.rollback()
            send(sock, "ERROR|Аккаунт заблокирован")
            return

        if sender[0] < price:
            conn.rollback()
            send(
                sock,
                f"ERROR|Недостаточно валюты. Нужно: {price}"
            )
            return

        conn.execute(
            "UPDATE users SET balance=balance-? WHERE id=?",
            (price, sender_id)
        )

        conn.execute(
            """
            INSERT INTO user_gifts
            (sender_id, receiver_id, gift_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                sender_id,
                receiver_id,
                gift_id,
                int(time.time())
            )
        )

        conn.commit()

        send(
            sock,
            f"GIFT_SENT|{gift_emoji}|{gift_name}|{price}"
        )

        print(
            f"[GIFT] {sender_id} -> "
            f"{receiver_public_id}: {gift_name}"
        )

    except Exception as e:

        try:
            conn.rollback()
        except Exception:
            pass

        print("[!] Ошибка подарка:", e)
        send(sock, "ERROR|Не удалось отправить подарок")

    finally:
        conn.close()


def get_gifts(sock, user_id):

    conn = db()

    try:
        rows = conn.execute(
            """
            SELECT
                ug.id,
                g.name,
                g.emoji,
                g.price,
                u.nickname,
                ug.created_at
            FROM user_gifts ug
            JOIN gifts g ON g.id = ug.gift_id
            JOIN users u ON u.id = ug.sender_id
            WHERE ug.receiver_id=?
            ORDER BY ug.created_at DESC
            """,
            (user_id,)
        ).fetchall()

        if not rows:
            send(sock, "GIFTS|EMPTY")
            return

        for row in rows:
            send(
                sock,
                "GIFT|{}|{}|{}|{}|{}|{}".format(
                    row[0],
                    row[1],
                    row[2],
                    row[3],
                    row[4],
                    row[5]
                )
            )

        send(sock, "GIFTS|END")

    finally:
        conn.close()


def send_private_message(sock, sender_id, receiver_public_id, text):
    text = text.strip()

    if not text:
        send(sock, "ERROR|Пустое сообщение")
        return

    conn = db()

    try:
        receiver = conn.execute(
            "SELECT id FROM users WHERE public_id=?",
            (receiver_public_id,)
        ).fetchone()

        if not receiver:
            send(sock, "ERROR|Пользователь не найден")
            return

        receiver_id = receiver[0]

        if sender_id == receiver_id:
            send(sock, "ERROR|Нельзя написать самому себе")
            return

        created_at = int(time.time())

        cur = conn.execute(
            """
            INSERT INTO private_messages
            (sender_id, receiver_id, text, status, created_at)
            VALUES (?, ?, ?, 1, ?)
            """,
            (sender_id, receiver_id, text, created_at)
        )

        conn.commit()

        send(
            sock,
            f"MESSAGE_SENT|{cur.lastrowid}|{receiver_public_id}|{text}|{created_at}"
        )

    except Exception as e:
        print("[!] Ошибка личного сообщения:", e)
        send(sock, "ERROR|Не удалось отправить сообщение")

    finally:
        conn.close()


def get_private_messages(sock, user_id, other_public_id):
    conn = db()

    try:
        other = conn.execute(
            "SELECT id FROM users WHERE public_id=?",
            (other_public_id,)
        ).fetchone()

        if not other:
            send(sock, "ERROR|Пользователь не найден")
            return

        rows = conn.execute(
            """
            SELECT id, sender_id, text, status, created_at
            FROM private_messages
            WHERE
                (sender_id=? AND receiver_id=?)
                OR
                (sender_id=? AND receiver_id=?)
            ORDER BY id ASC
            """,
            (
                user_id, other[0],
                other[0], user_id
            )
        ).fetchall()

        for row in rows:
            safe_text = row[2].replace("|", " ")

            send(
                sock,
                f"MESSAGE|{row[0]}|{row[1]}|{safe_text}|{row[3]}|{row[4]}"
            )

        send(sock, "MESSAGES_END")

    finally:
        conn.close()

def login_user(token):
    conn = db()

    try:
        row = conn.execute(
            '''
            SELECT id, public_id, nickname, balance, banned
            FROM users
            WHERE token=?
            ''',
            (token,)
        ).fetchone()

        return row

    finally:
        conn.close()



def call_user(conn, current_user_id, receiver_public_id):
    conn_db = db()

    try:
        row = conn_db.execute(
            "SELECT id FROM users WHERE nickname=?",
            (receiver_public_id,)
        ).fetchone()
    finally:
        conn_db.close()

    if not row:
        send(conn, "CALL_ERROR|Пользователь не найден")
        return

    receiver_id = row[0]

    with lock:
        receiver_conn = connected_users.get(receiver_id)

    if not receiver_conn:
        send(conn, "CALL_ERROR|Пользователь сейчас не в сети")
        return

    call_id = f"{current_user_id}_{receiver_id}_{int(time.time())}"

    with lock:
        active_calls[call_id] = {
            "caller": current_user_id,
            "receiver": receiver_id,
            "status": "ringing"
        }

    send(conn, f"CALL_OUTGOING|{call_id}|{receiver_public_id}")
    send(receiver_conn, f"CALL_INCOMING|{call_id}|{current_user_id}")


def call_response(conn, current_user_id, call_id, action):
    with lock:
        call = active_calls.get(call_id)

    if not call:
        send(conn, "CALL_ERROR|Звонок не найден")
        return

    if current_user_id not in (
        call["caller"],
        call["receiver"]
    ):
        send(conn, "CALL_ERROR|Нет доступа к звонку")
        return

    other_user_id = (
        call["receiver"]
        if current_user_id == call["caller"]
        else call["caller"]
    )

    with lock:
        other_conn = connected_users.get(other_user_id)

    if action == "ACCEPT":
        call["status"] = "active"

        send(conn, f"CALL_ACCEPTED|{call_id}")

        if other_conn:
            send(other_conn, f"CALL_ACCEPTED|{call_id}")

    elif action == "REJECT":
        send(conn, f"CALL_REJECTED|{call_id}")

        if other_conn:
            send(other_conn, f"CALL_REJECTED|{call_id}")

        with lock:
            active_calls.pop(call_id, None)

    elif action == "END":
        send(conn, f"CALL_ENDED|{call_id}")

        if other_conn:
            send(other_conn, f"CALL_ENDED|{call_id}")

        with lock:
            active_calls.pop(call_id, None)


def call_signal(conn, current_user_id, call_id, signal_data):
    with lock:
        call = active_calls.get(call_id)

    if not call:
        send(conn, "CALL_ERROR|Звонок не найден")
        return

    if current_user_id not in (
        call["caller"],
        call["receiver"]
    ):
        send(conn, "CALL_ERROR|Нет доступа к звонку")
        return

    other_user_id = (
        call["receiver"]
        if current_user_id == call["caller"]
        else call["caller"]
    )

    with lock:
        other_conn = connected_users.get(other_user_id)

    if other_conn:
        send(
            other_conn,
            f"CALL_SIGNAL|{call_id}|{signal_data}"
        )



def register_user(conn, telegram_id, telegram_username="", telegram_first_name="", telegram_last_name=""):
    conn_db = db()

    try:
        telegram_id = str(telegram_id).strip()

        if not telegram_id:
            send(conn, "ERROR|Не указан Telegram ID")
            return

        # Если Telegram уже привязан — возвращаем существующий аккаунт.
        row = conn_db.execute(
            """
            SELECT id, public_id, nickname, token
            FROM users
            WHERE telegram_id=?
            """,
            (telegram_id,)
        ).fetchone()

        if row:
            send(
                conn,
                f"REGISTERED|{row[0]}|{row[1]}|{row[2]}|{row[3]}"
            )
            return

        # Уникальные данные нового аккаунта.
        public_id = "user_" + telegram_id
        nickname = telegram_username or telegram_first_name or "Аноним"

        # На случай повторяющегося public_id.
        existing = conn_db.execute(
            "SELECT id FROM users WHERE public_id=?",
            (public_id,)
        ).fetchone()

        if existing:
            public_id = "user_" + telegram_id + "_" + str(int(time.time()))

        token = os.urandom(32).hex()
        phone_hash = "telegram:" + telegram_id
        created_at = int(time.time())

        cur = conn_db.execute(
            """
            INSERT INTO users (
                public_id,
                phone_hash,
                nickname,
                description,
                avatar,
                token,
                balance,
                verified,
                banned,
                created_at,
                telegram_id,
                telegram_username,
                telegram_first_name,
                telegram_last_name
            )
            VALUES (?, ?, ?, '', '', ?, 0, 1, 0, ?, ?, ?, ?, ?)
            """,
            (
                public_id,
                phone_hash,
                nickname,
                token,
                created_at,
                telegram_id,
                telegram_username,
                telegram_first_name,
                telegram_last_name
            )
        )

        conn_db.commit()

        user_id = cur.lastrowid

        send(
            conn,
            f"REGISTERED|{user_id}|{public_id}|{nickname}|{token}"
        )

        print(
            f"[REGISTER] Telegram {telegram_id} -> "
            f"user {user_id} ({public_id})"
        )

    except Exception as e:
        conn_db.rollback()
        print("[REGISTER ERROR]", e)
        send(conn, "ERROR|Не удалось создать аккаунт")

    finally:
        conn_db.close()


def handle_client(conn, addr):

    global waiting

    print(f"[+] Подключился: {addr}")

    try:

        send(
            conn,
            "SYSTEM|Подключено к серверу"
        )

        current_user_id = None

        while True:

            data = conn.recv(4096)

            if not data:
                break

            message = data.decode(
                "utf-8",
                errors="ignore"
            ).strip()

            parts = message.split("|")

            command = parts[0]

            if command == "REGISTER" and len(parts) >= 2:

                telegram_id = parts[1].strip()
                telegram_username = parts[2].strip() if len(parts) >= 3 else ""
                telegram_first_name = parts[3].strip() if len(parts) >= 4 else ""
                telegram_last_name = parts[4].strip() if len(parts) >= 5 else ""

                register_user(
                    conn,
                    telegram_id,
                    telegram_username,
                    telegram_first_name,
                    telegram_last_name
                )

            elif command == "LOGIN" and len(parts) >= 2:

                token = parts[1].strip()

                user = login_user(token)

                if not user:
                    send(
                        conn,
                        "ERROR|Неверный токен"
                    )
                    continue

                if user[4]:
                    send(
                        conn,
                        "ERROR|Аккаунт заблокирован"
                    )
                    continue

                current_user_id = user[0]

                send(
                    conn,
                    f"LOGIN_OK|{user[0]}|{user[1]}|{user[2]}|{user[3]}"
                )

            elif command == "SEND_MESSAGE" and len(parts) >= 3:

                if not current_user_id:
                    send(
                        conn,
                        "ERROR|Сначала выполните LOGIN"
                    )
                    continue

                receiver_public_id = parts[1].strip()
                text = "|".join(parts[2:]).strip()

                send_private_message(
                    conn,
                    current_user_id,
                    receiver_public_id,
                    text
                )

            elif command == "GET_MESSAGES" and len(parts) >= 2:

                if not current_user_id:
                    send(
                        conn,
                        "ERROR|Сначала выполните LOGIN"
                    )
                    continue

                other_public_id = parts[1].strip()

                get_private_messages(
                    conn,
                    current_user_id,
                    other_public_id
                )

            elif command == "CALL_REQUEST" and len(parts) >= 2:

                if not current_user_id:
                    send(
                        conn,
                        "ERROR|Сначала выполните LOGIN"
                    )
                    continue

                receiver_public_id = parts[1].strip()

                call_user(
                    conn,
                    current_user_id,
                    receiver_public_id
                )

            elif command == "CALL_ACCEPT" and len(parts) >= 2:

                if not current_user_id:
                    continue

                call_response(
                    conn,
                    current_user_id,
                    parts[1].strip(),
                    "ACCEPT"
                )

            elif command == "CALL_REJECT" and len(parts) >= 2:

                if not current_user_id:
                    continue

                call_response(
                    conn,
                    current_user_id,
                    parts[1].strip(),
                    "REJECT"
                )

            elif command == "CALL_END" and len(parts) >= 2:

                if not current_user_id:
                    continue

                call_response(
                    conn,
                    current_user_id,
                    parts[1].strip(),
                    "END"
                )

            elif command == "CALL_SIGNAL" and len(parts) >= 3:

                if not current_user_id:
                    continue

                call_id = parts[1].strip()
                signal_data = "|".join(parts[2:])

                call_signal(
                    conn,
                    current_user_id,
                    call_id,
                    signal_data
                )

            elif command == "SEND_GIFT" and len(parts) >= 3:

                if not current_user_id:
                    send(
                        conn,
                        "ERROR|Сначала выполните LOGIN"
                    )
                    continue

                receiver_public_id = parts[1].strip()

                try:
                    gift_id = int(parts[2])
                except ValueError:
                    send(
                        conn,
                        "ERROR|Неверный подарок"
                    )
                    continue

                send_gift(
                    conn,
                    current_user_id,
                    receiver_public_id,
                    gift_id
                )

            elif command == "GET_GIFTS":

                if not current_user_id:
                    send(
                        conn,
                        "ERROR|Сначала выполните LOGIN"
                    )
                    continue

                get_gifts(
                    conn,
                    current_user_id
                )

            elif command == "FIND":

                with lock:

                    if waiting is None:

                        waiting = conn

                        send(
                            conn,
                            "WAIT|Ищем собеседника..."
                        )

                    else:

                        partner = waiting
                        waiting = None

                        rooms[conn] = partner
                        rooms[partner] = conn

                        send(
                            partner,
                            "MATCH|Собеседник найден!"
                        )

                        send(
                            conn,
                            "MATCH|Собеседник найден!"
                        )

            elif command == "MSG" and len(parts) >= 2:

                with lock:
                    partner = rooms.get(conn)

                if partner:
                    send(
                        partner,
                        message
                    )

    except Exception as e:

        print(
            f"[!] Ошибка {addr}: {e}"
        )

    finally:

        with lock:

            if waiting == conn:
                waiting = None

            partner = rooms.pop(
                conn,
                None
            )

            if partner:

                rooms.pop(
                    partner,
                    None
                )

                send(
                    partner,
                    "SYSTEM|Собеседник отключился"
                )

        conn.close()

        print(
            f"[-] Отключился: {addr}"
        )


# ============================================================
# HTTP API для связи Telegram-бота с AnonMessenger
# ============================================================

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HTTP_HOST = "0.0.0.0"
HTTP_PORT = int(os.environ.get("PORT", os.environ.get("TELEGRAM_API_PORT", "5001")))
TELEGRAM_API_SECRET = os.environ.get(
    "TELEGRAM_API_SECRET",
    ""
).strip()


class TelegramRegisterHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"OK")
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):

        if self.path != "/telegram/register":
            self.send_response(404)
            self.end_headers()
            return

        try:
            secret = self.headers.get("X-API-Secret", "")

            # Проверка секрета временно отключена для регистрации приложения.

            length = int(
                self.headers.get("Content-Length", "0")
            )

            body = self.rfile.read(length)
            data = json.loads(body.decode("utf-8"))

            telegram_id = str(
                data.get("telegram_id", "")
            ).strip()

            telegram_username = str(
                data.get("telegram_username", "")
            ).strip()

            telegram_first_name = str(
                data.get("telegram_first_name", "")
            ).strip()

            telegram_last_name = str(
                data.get("telegram_last_name", "")
            ).strip()

            if not telegram_id:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(
                    b'{"ok":false,"error":"telegram_id required"}'
                )
                return

            # Создаём/получаем аккаунт напрямую через БД.
            conn_db = db()

            try:
                row = conn_db.execute(
                    """
                    SELECT id, public_id, nickname, token
                    FROM users
                    WHERE telegram_id=?
                    """,
                    (telegram_id,)
                ).fetchone()

                if row:
                    user_id, public_id, nickname, token = row

                else:
                    public_id = "user_" + telegram_id
                    nickname = (
                        telegram_username
                        or telegram_first_name
                        or "Аноним"
                    )

                    existing = conn_db.execute(
                        "SELECT id FROM users WHERE public_id=?",
                        (public_id,)
                    ).fetchone()

                    if existing:
                        public_id = (
                            "user_"
                            + telegram_id
                            + "_"
                            + str(int(time.time()))
                        )

                    token = os.urandom(32).hex()
                    phone_hash = "telegram:" + telegram_id
                    created_at = int(time.time())

                    cur = conn_db.execute(
                        """
                        INSERT INTO users (
                            public_id,
                            phone_hash,
                            nickname,
                            description,
                            avatar,
                            token,
                            balance,
                            verified,
                            banned,
                            created_at,
                            telegram_id,
                            telegram_username,
                            telegram_first_name,
                            telegram_last_name
                        )
                        VALUES (?, ?, ?, '', '', ?, 0, 1, 0, ?, ?, ?, ?, ?)
                        """,
                        (
                            public_id,
                            phone_hash,
                            nickname,
                            token,
                            created_at,
                            telegram_id,
                            telegram_username,
                            telegram_first_name,
                            telegram_last_name
                        )
                    )

                    conn_db.commit()
                    user_id = cur.lastrowid

                    print(
                        f"[TELEGRAM REGISTER] "
                        f"{telegram_id} -> "
                        f"user {user_id} ({public_id})"
                    )

            finally:
                conn_db.close()

            response = {
                "ok": True,
                "user_id": user_id,
                "public_id": public_id,
                "nickname": nickname,
                "token": token
            }

            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/json; charset=utf-8"
            )
            self.end_headers()

            self.wfile.write(
                json.dumps(
                    response,
                    ensure_ascii=False
                ).encode("utf-8")
            )

        except Exception as e:
            print("[TELEGRAM API ERROR]", e)

            self.send_response(500)
            self.send_header(
                "Content-Type",
                "application/json; charset=utf-8"
            )
            self.end_headers()

            self.wfile.write(
                b'{"ok":false,"error":"server error"}'
            )

    def log_message(self, format, *args):
        print("[Telegram API]", format % args)


def start_telegram_http_api():

    http_server = ThreadingHTTPServer(
        (HTTP_HOST, HTTP_PORT),
        TelegramRegisterHandler
    )

    print(
        f"Telegram HTTP API: "
        f"{HTTP_HOST}:{HTTP_PORT}"
    )

    http_server.serve_forever()


threading.Thread(
    target=start_telegram_http_api,
    daemon=True
).start()



# Telegram больше не используется.
# Сервер запускается без Telegram-бота.


db()

print("================================")
print("       AnonMessenger Server")
print("================================")
print("Порт:", PORT)
print("База:", DB)
print("Ожидание подключений...")




server = socket.socket(
    socket.AF_INET,
    socket.SOCK_STREAM
)

server.setsockopt(
    socket.SOL_SOCKET,
    socket.SO_REUSEADDR,
    1
)

server.bind(
    (HOST, PORT)
)

server.listen(20)


while True:

    conn, addr = server.accept()

    threading.Thread(
        target=handle_client,
        args=(conn, addr),
        daemon=True
    ).start()

def set_account_label(user_id, label):
    conn = db()
    try:
        if label:
            conn.execute(
                """
                INSERT INTO account_labels (user_id, label, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    label=excluded.label,
                    updated_at=excluded.updated_at
                """,
                (user_id, label, int(time.time()))
            )
        else:
            conn.execute(
                "DELETE FROM account_labels WHERE user_id=?",
                (user_id,)
            )

        conn.commit()
        return True
    except Exception as e:
        print("[LABEL ERROR]", e)
        return False
    finally:
        conn.close()


def get_account_label(user_id):
    conn = db()
    try:
        row = conn.execute(
            "SELECT label FROM account_labels WHERE user_id=?",
            (user_id,)
        ).fetchone()

        return row[0] if row else ""
    finally:
        conn.close()
