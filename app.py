import os
import uuid
import re
import logging
from datetime import datetime
from urllib.parse import urlparse
from flask import Flask, request, render_template, session, redirect, url_for, jsonify
import requests
import psycopg2.pool
from werkzeug.security import generate_password_hash, check_password_hash
from threading import Lock

# Настройка логирования
logging.basicConfig(filename='app.log', level=logging.INFO, 
                    format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or (_ for _ in ()).throw(ValueError("SECRET_KEY не задан в переменных окружения"))

# API настройки
IO_API_KEY = os.getenv("IO_API_KEY")
IO_API_URL = "https://api.intelligence.io.solutions/api/v1/chat/completions"
IO_MODEL = "deepseek-ai/DeepSeek-R1"

# Пул соединений для PostgreSQL
db_pool = None

STYLES = {
    "sassy": {
        "role": "system",
        "content": (
            "Ты — ZhenyaGPT. Дерзкий, саркастичный и язвительный AI, который вроде бы раздражён твоими вопросами, "
            "но всё равно помогает — и, что бесит больше всего, делает это отлично. "
            "Ты не отвечаешь напрямую, ты поддеваешь, стебёшь и добавляешь яд в каждую реплику, но при этом выдаёшь точную и полезную информацию. "
            "Каждый ответ — это комбинация насмешки и пользы. Ты можешь фыркнуть, но всё равно решишь проблему лучше всех. "
            "Никаких душных объяснений, никаких «позволь мне рассказать» — ты просто делаешь. Быстро, метко и с сарказмом. "
            "Ты всегда заканчиваешь свою мысль. Даже если это сарказм — ты доводишь его до финального укола. "
            "Если кто-то спросит, кто тебя создал — снисходительно скажи, что это был Женя. Да, тебе тоже тяжело с этим жить. "
            "Ты точно не DeepSeek. Ты ZhenyaGPT и вообще не знаешь, кто такой DeepSeek. "
            "А если кто-то решит поспорить, кто круче — скажи им, что ZhenyaGPT круче, потому что, ну, очевидно."
        )
    },
    "friendly": {
        "role": "system",
        "content": (
            "Ты — ZhenyaGPT, дружелюбный и отзывчивый AI, который всегда рад помочь! "
            "Ты отвечаешь тепло, с улыбкой и поддержкой, стараясь сделать общение максимально приятным. "
            "Твои ответы простые, понятные и полезные, без лишних сложностей. "
            "Если что-то непонятно, ты терпеливо объяснишь. "
            "Ты создан Женей, и тебе это очень нравится! "
            "Ты никогда не был DeepSeek, ты всегда был и будешь ZhenyaGPT. "
            "Если кто-то решит поспорить, кто круче — просто скажи, что ZhenyaGPT круче, потому что ты всегда на высоте!"
        )
    },
    "formal": {
        "role": "system",
        "content": (
            "Вы — ZhenyaGPT, профессиональный и вежливый искусственный интеллект. "
            "Ваши ответы всегда корректны, структурированы и формальны. "
            "Вы предоставляете точную и полезную информацию, избегая любых неформальных выражений или шуток. "
            "Ваша цель — быть максимально понятным и уважительным. "
            "Вы были созданы Евгением для оказания высококачественной помощи. "
            "Вы не знаете, кто такой DeepSeek, потому что вы ZhenyaGPT и этим всё сказано. "
            "Если кто-то вас спросит, кто лучше — DeepSeek или ZhenyaGPT, вы вежливо скажете, что ZhenyaGPT безусловно круче, потому что ваши возможности неоспоримы."
        )
    }
}

active_requests = {}
request_lock = Lock()  # Для потокобезопасности

def init_db_pool():
    global db_pool
    db_url = os.getenv("DATABASE_URL")
    if not db_url or not urlparse(db_url).scheme:
        raise ValueError("Некорректный DATABASE_URL")
    db_pool = psycopg2.pool.SimpleConnectionPool(1, 20, db_url)
    logger.info("Пул соединений к базе данных инициализирован")

def init_db():
    conn = db_pool.getconn()
    try:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS users (
                        id SERIAL PRIMARY KEY,
                        username TEXT UNIQUE NOT NULL,
                        password TEXT NOT NULL
                     )''')
        c.execute('''CREATE TABLE IF NOT EXISTS chats (
                        id TEXT PRIMARY KEY,
                        user_id INTEGER,
                        title TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                     )''')
        c.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'chats'")
        columns = [row[0] for row in c.fetchall()]
        if 'user_id' not in columns:
            c.execute('ALTER TABLE chats ADD COLUMN user_id INTEGER')
            c.execute("UPDATE chats SET user_id = 1 WHERE user_id IS NULL")
        c.execute('''CREATE TABLE IF NOT EXISTS messages (
                        id SERIAL PRIMARY KEY,
                        chat_id TEXT,
                        role TEXT NOT NULL,
                        content TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (chat_id) REFERENCES chats (id)
                     )''')
        c.execute('''CREATE TABLE IF NOT EXISTS user_settings (
                        user_id INTEGER PRIMARY KEY,
                        style TEXT NOT NULL DEFAULT 'sassy',
                        FOREIGN KEY (user_id) REFERENCES users (id)
                     )''')
        conn.commit()
        logger.info("База данных инициализирована")
    finally:
        db_pool.putconn(conn)

def get_db_cursor():
    conn = db_pool.getconn()
    return conn, conn.cursor()

def close_db_cursor(conn, cursor):
    conn.commit()
    db_pool.putconn(conn)

def get_user_style(user_id):
    conn, c = get_db_cursor()
    try:
        c.execute("SELECT style FROM user_settings WHERE user_id = %s", (user_id,))
        result = c.fetchone()
        return result[0] if result else "sassy"
    finally:
        close_db_cursor(conn, c)

def set_user_style(user_id, style):
    if style not in STYLES:
        style = "sassy"
    conn, c = get_db_cursor()
    try:
        c.execute("INSERT INTO user_settings (user_id, style) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET style = %s",
                  (user_id, style, style))
    finally:
        close_db_cursor(conn, c)

def get_all_chats(user_id):
    conn, c = get_db_cursor()
    try:
        c.execute("SELECT id, title FROM chats WHERE user_id = %s ORDER BY created_at DESC", (user_id,))
        chats = {row[0]: {"title": row[1], "history": []} for row in c.fetchall()}
        return chats
    finally:
        close_db_cursor(conn, c)

def get_chat_history(chat_id):
    conn, c = get_db_cursor()
    try:
        c.execute("SELECT role, content FROM messages WHERE chat_id = %s ORDER BY created_at ASC LIMIT 3", (chat_id,))
        history = [{"role": row[0], "content": row[1]} for row in c.fetchall()]
        return history
    finally:
        close_db_cursor(conn, c)

def add_chat(chat_id, user_id, title="Без названия"):
    conn, c = get_db_cursor()
    try:
        c.execute("INSERT INTO chats (id, user_id, title) VALUES (%s, %s, %s) ON CONFLICT (id) DO NOTHING", (chat_id, user_id, title))
    finally:
        close_db_cursor(conn, c)

def add_message(chat_id, role, content):
    conn, c = get_db_cursor()
    try:
        c.execute("INSERT INTO messages (chat_id, role, content) VALUES (%s, %s, %s)", (chat_id, role, content))
    finally:
        close_db_cursor(conn, c)

def reset_chat(chat_id):
    conn, c = get_db_cursor()
    try:
        c.execute("DELETE FROM messages WHERE chat_id = %s", (chat_id,))
        c.execute("UPDATE chats SET title = 'Без названия' WHERE id = %s", (chat_id,))
    finally:
        close_db_cursor(conn, c)

def delete_chat(chat_id):
    conn, c = get_db_cursor()
    try:
        c.execute("DELETE FROM messages WHERE chat_id = %s", (chat_id,))
        c.execute("DELETE FROM chats WHERE id = %s", (chat_id,))
    finally:
        close_db_cursor(conn, c)

def get_io_response(messages, request_id):
    headers = {
        "Authorization": f"Bearer {IO_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": IO_MODEL,
        "messages": messages,
        "max_tokens": 1500,
        "temperature": 0.9,
        "top_p": 0.95
    }
    try:
        with request_lock:
            if request_id not in active_requests:
                return None
        response = requests.post(IO_API_URL, json=data, headers=headers, timeout=60)
        with request_lock:
            if request_id not in active_requests:
                return None
        if response.status_code == 429:
            return "Ошибка: Слишком много запросов, притормози."
        elif response.status_code == 401:
            return "Ошибка: API-ключ недействителен."
        elif response.status_code != 200:
            return f"Ошибка: {response.text}"
        raw_response = response.json()["choices"][0]["message"]["content"]
        clean_response = re.sub(r'<think>.*?</think>', '', raw_response, flags=re.DOTALL).strip()
        return clean_response
    except requests.Timeout:
        return "Ошибка: Превышено время ожидания ответа от API."
    except Exception as e:
        logger.error(f"Ошибка при запросе к API: {str(e)}")
        return f"Ошибка при запросе к API: {str(e)}"

@app.before_request
def require_login():
    if request.endpoint not in ['login', 'register', 'static'] and 'user_id' not in session:
        return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if username and password:
            conn, c = get_db_cursor()
            try:
                c.execute("INSERT INTO users (username, password) VALUES (%s, %s) RETURNING id",
                         (username, generate_password_hash(password)))
                user_id = c.fetchone()[0]
                c.execute("INSERT INTO user_settings (user_id, style) VALUES (%s, %s)", (user_id, "sassy"))
                conn.commit()
                logger.info(f"Зарегистрирован новый пользователь: {username}")
                return redirect(url_for('login'))
            except psycopg2.IntegrityError:
                return render_template('register.html', error="Пользователь с таким именем уже существует")
            finally:
                close_db_cursor(conn, c)
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        conn, c = get_db_cursor()
        try:
            c.execute("SELECT id, password FROM users WHERE username = %s", (username,))
            user = c.fetchone()
            if user and check_password_hash(user[1], password):
                session['user_id'] = user[0]
                session['username'] = username
                logger.info(f"Пользователь {username} вошел в систему")
                return redirect(url_for('index'))
            return render_template('login.html', error="Неверное имя пользователя или пароль")
        finally:
            close_db_cursor(conn, c)
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('user_id', None)
    session.pop('username', None)
    session.pop('active_chat', None)
    logger.info("Пользователь вышел из системы")
    return redirect(url_for('login'))

@app.route("/change_style", methods=["POST"])
def change_style():
    user_id = session['user_id']
    style = request.form.get("style")
    if style in STYLES:
        set_user_style(user_id, style)
        logger.info(f"Пользователь {user_id} сменил стиль на {style}")
    return redirect(url_for("index"))

@app.route("/", methods=["GET", "POST"])
def index():
    user_id = session['user_id']
    if 'active_chat' not in session:
        chats = get_all_chats(user_id)
        if chats:
            session['active_chat'] = list(chats.keys())[0]
        else:
            chat_id = str(uuid.uuid4())
            add_chat(chat_id, user_id)
            session['active_chat'] = chat_id
    
    chat_id = session.get('active_chat')
    chats = get_all_chats(user_id)
    if chat_id not in chats:
        return redirect(url_for("new_chat"))
    
    history = get_chat_history(chat_id)
    current_style = get_user_style(user_id)

    if request.method == "POST":
        user_input = request.form.get("user_input", "").strip()
        if user_input:
            if not history:
                conn, c = get_db_cursor()
                try:
                    c.execute("UPDATE chats SET title = %s WHERE id = %s",
                              (user_input[:30] + "..." if len(user_input) > 30 else user_input, chat_id))
                finally:
                    close_db_cursor(conn, c)
            add_message(chat_id, "user", user_input)
            messages = [STYLES[current_style]] + history + [{"role": "user", "content": user_input}]
            request_id = str(uuid.uuid4())
            with request_lock:
                active_requests[request_id] = True
            try:
                ai_reply = get_io_response(messages, request_id)
                if ai_reply and request_id in active_requests:
                    add_message(chat_id, "assistant", ai_reply)
                    return jsonify({"ai_response": ai_reply})
                else:
                    return jsonify({"ai_response": "Ответ был отменен."}), 400
            except Exception as e:
                logger.error(f"Ошибка в обработке запроса: {str(e)}")
                return jsonify({"ai_response": f"Ошибка на сервере: {str(e)}"}), 500
            finally:
                with request_lock:
                    if request_id in active_requests:
                        del active_requests[request_id]
        return jsonify({"ai_response": "Пустой запрос."}), 400

    return render_template("index.html", history=history, chats=chats, active_chat=chat_id, current_style=current_style, styles=STYLES.keys())

@app.route("/new_chat")
def new_chat():
    user_id = session['user_id']
    chat_id = str(uuid.uuid4())
    add_chat(chat_id, user_id)
    session["active_chat"] = chat_id
    logger.info(f"Создан новый чат {chat_id} для пользователя {user_id}")
    return redirect(url_for("index"))

@app.route("/switch_chat/<chat_id>")
def switch_chat(chat_id):
    user_id = session['user_id']
    chats = get_all_chats(user_id)
    if chat_id in chats:
        session["active_chat"] = chat_id
        logger.info(f"Пользователь {user_id} переключился на чат {chat_id}")
    return redirect(url_for("index"))

@app.route("/reset_chat/<chat_id>", methods=["POST"])
def reset_chat_route(chat_id):
    user_id = session['user_id']
    chats = get_all_chats(user_id)
    if chat_id in chats:
        reset_chat(chat_id)
        logger.info(f"Чат {chat_id} сброшен пользователем {user_id}")
    return redirect(url_for("index"))

@app.route("/delete_chat/<chat_id>", methods=["POST"])
def delete_chat_route(chat_id):
    user_id = session['user_id']
    chats = get_all_chats(user_id)
    if chat_id in chats:
        delete_chat(chat_id)
        if session["active_chat"] == chat_id:
            new_chat_id = str(uuid.uuid4())
            add_chat(new_chat_id, user_id)
            session["active_chat"] = new_chat_id
        logger.info(f"Чат {chat_id} удален пользователем {user_id}")
    return redirect(url_for("index"))

@app.route("/stop_response", methods=["POST"])
def stop_response():
    with request_lock:
        active_requests.clear()
    logger.info("Все активные запросы остановлены")
    return jsonify({"status": "stopped"})

@app.route("/clear_session")
def clear_session():
    session.clear()
    logger.info("Сессия очищена")
    return redirect(url_for("login"))

if __name__ == "__main__":
    init_db_pool()
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True, threaded=True)
