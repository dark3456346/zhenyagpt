from flask import Flask, request, render_template, session, redirect, url_for, jsonify
import aiohttp
import asyncio
import re
import uuid
import asyncpg
from werkzeug.security import generate_password_hash, check_password_hash
from flask_caching import Cache
import os
import logging
import time
from asgiref.sync import async_to_sync

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "zhenya-secret-key")

# Логирование
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Кэширование
cache = Cache(app, config={'CACHE_TYPE': 'simple'})

# API настройки
IO_API_KEY = os.getenv("IO_API_KEY")
IO_API_URL = "https://api.intelligence.io.solutions/api/v1/chat/completions"
IO_MODEL = "deepseek-ai/DeepSeek-R1"

if not IO_API_KEY:
    raise ValueError("IO_API_KEY не задан в переменных окружения!")

STYLES = {
    "sassy": {"role": "system", "content": "Ты — ZhenyaGPT. Дерзкий, саркастичный, быстрый."},
    "friendly": {"role": "system", "content": "Ты — ZhenyaGPT. Дружелюбный и быстрый."},
    "formal": {"role": "system", "content": "Вы — ZhenyaGPT. Вежливый и быстрый."}
}

active_requests = {}
db_pool = None

async def get_db_pool():
    global db_pool
    if db_pool is None:
        loop = asyncio.get_event_loop()  # Используем текущий цикл, а не get_running_loop для WSGI
        db_pool = await asyncpg.create_pool(os.getenv("DATABASE_URL"), min_size=1, max_size=10)
        async with db_pool.acquire() as conn:
            await conn.execute('''CREATE TABLE IF NOT EXISTS users (
                                    id SERIAL PRIMARY KEY,
                                    username TEXT UNIQUE NOT NULL,
                                    password TEXT NOT NULL
                                )''')
            await conn.execute('''CREATE TABLE IF NOT EXISTS chats (
                                    id TEXT PRIMARY KEY,
                                    user_id INTEGER,
                                    title TEXT NOT NULL DEFAULT 'Без названия',
                                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                                    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                                )''')
            await conn.execute('''CREATE TABLE IF NOT EXISTS messages (
                                    id SERIAL PRIMARY KEY,
                                    chat_id TEXT,
                                    role TEXT NOT NULL,
                                    content TEXT NOT NULL,
                                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                                    FOREIGN KEY (chat_id) REFERENCES chats (id)
                                )''')
            await conn.execute('''CREATE TABLE IF NOT EXISTS user_settings (
                                    user_id INTEGER PRIMARY KEY,
                                    style TEXT NOT NULL DEFAULT 'sassy',
                                    FOREIGN KEY (user_id) REFERENCES users (id)
                                )''')
        logger.info("База данных инициализирована")
    return db_pool

@cache.cached(timeout=300, key_prefix="user_style_%s")
async def get_user_style(user_id):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        result = await conn.fetchval("SELECT style FROM user_settings WHERE user_id = $1", user_id)
    return result or "sassy"

async def set_user_style(user_id, style):
    if style not in STYLES:
        style = "sassy"
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_settings (user_id, style) VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE SET style = $3",
            user_id, style, style
        )

async def get_all_chats(user_id):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, title FROM chats WHERE user_id = $1 ORDER BY last_active DESC", user_id)
    return {row['id']: {"title": row['title'], "history": []} for row in rows}

async def chat_exists(user_id, chat_id):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        result = await conn.fetchval("SELECT 1 FROM chats WHERE user_id = $1 AND id = $2", user_id, chat_id)
    return bool(result)

async def add_chat(chat_id, user_id, title="Без названия"):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO chats (id, user_id, title, last_active) VALUES ($1, $2, $3, CURRENT_TIMESTAMP) ON CONFLICT (id) DO NOTHING",
            chat_id, user_id, title
        )

async def update_chat_title(chat_id, title):
    asyncio.create_task(_update_chat_title(chat_id, title))

async def _update_chat_title(chat_id, title):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE chats SET title = $1 WHERE id = $2", title[:30], chat_id)

async def add_message(chat_id, role, content):
    asyncio.create_task(_add_message(chat_id, role, content))

async def _add_message(chat_id, role, content):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO messages (chat_id, role, content) VALUES ($1, $2, $3)", chat_id, role, content)
        await conn.execute("UPDATE chats SET last_active = CURRENT_TIMESTAMP WHERE id = $1", chat_id)

@cache.cached(timeout=60, key_prefix="ai_response_%s")
async def get_io_response(messages, request_id):
    start_time = time.time()
    headers = {"Authorization": f"Bearer {IO_API_KEY}", "Content-Type": "application/json"}
    data = {"model": IO_MODEL, "messages": messages, "max_tokens": 1500, "temperature": 0.9, "top_p": 0.95}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(IO_API_URL, json=data, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as response:
                if request_id not in active_requests:
                    return None
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"Ошибка API: {response.status} - {error_text}")
                    return f"Ошибка API: {error_text}"
                raw_response = (await response.json())["choices"][0]["message"]["content"]
                clean_response = re.sub(r'<think>.*?</think>', '', raw_response, flags=re.DOTALL).strip()
                logger.debug(f"API ответ за {time.time() - start_time:.2f} сек")
                return clean_response
        except asyncio.TimeoutError:
            logger.error("Тайм-аут API")
            return "Ошибка: Тайм-аут API."
        except Exception as e:
            logger.error(f"Ошибка API: {str(e)}")
            return f"Ошибка: {str(e)}"

async def generate_chat_title(user_input, request_id):
    prompt = {"role": "system", "content": "Генерируй короткие названия чатов (до 30 символов) по сообщению."}
    messages = [prompt, {"role": "user", "content": user_input}]
    title = await get_io_response(messages, request_id)
    return title[:30] if title and "Ошибка" not in title else user_input[:30]

@app.before_request
def require_login():
    if request.endpoint not in ['login', 'register', 'static'] and 'user_id' not in session:
        return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
@async_to_sync
async def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if username and password:
            pool = await get_db_pool()
            async with pool.acquire() as conn:
                try:
                    user_id = await conn.fetchval(
                        "INSERT INTO users (username, password) VALUES ($1, $2) RETURNING id",
                        username, generate_password_hash(password)
                    )
                    await conn.execute("INSERT INTO user_settings (user_id, style) VALUES ($1, $2)", user_id, "sassy")
                    return redirect(url_for('login'))
                except asyncpg.UniqueViolationError:
                    return render_template('register.html', error="Пользователь уже существует")
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
@async_to_sync
async def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            user = await conn.fetchrow("SELECT id, password FROM users WHERE username = $1", username)
            if user and check_password_hash(user['password'], password):
                session['user_id'] = user['id']
                session['username'] = username
                session['chats'] = await get_all_chats(user['id'])
                return redirect(url_for('index'))
            return render_template('login.html', error="Неверное имя или пароль")
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route("/change_style", methods=["POST"])
@async_to_sync
async def change_style():
    user_id = session['user_id']
    style = request.form.get("style")
    if style in STYLES:
        await set_user_style(user_id, style)
        cache.delete(f"user_style_{user_id}")
    return redirect(url_for("index"))

@app.route("/", methods=["GET", "POST"])
@async_to_sync
async def index():
    start_time = time.time()
    user_id = session['user_id']
    if 'chats' not in session:
        session['chats'] = await get_all_chats(user_id)

    if 'active_chat' not in session or not await chat_exists(user_id, session['active_chat']):
        chat_id = str(uuid.uuid4())
        await add_chat(chat_id, user_id)
        session['active_chat'] = chat_id
        session['chats'][chat_id] = {"title": "Без названия", "history": []}

    chat_id = session['active_chat']
    history = session['chats'][chat_id]['history']
    current_style = await get_user_style(user_id)

    if request.method == "POST":
        user_input = request.form.get("user_input", "").strip()
        if not user_input:
            return jsonify({"ai_response": "Пустой запрос."}), 400

        await add_message(chat_id, "user", user_input)
        session['chats'][chat_id]['history'].append({"role": "user", "content": user_input})
        if len(history) > 3:
            session['chats'][chat_id]['history'] = history[-3:]

        request_id = str(uuid.uuid4())
        active_requests[request_id] = True

        messages = [STYLES[current_style], {"role": "user", "content": user_input}]
        tasks = [get_io_response(messages, request_id)]
        if not history:
            tasks.append(generate_chat_title(user_input, request_id))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        ai_reply = results[0]
        title = results[1] if len(results) > 1 else None

        if title and request_id in active_requests:
            await update_chat_title(chat_id, title)
            session['chats'][chat_id]['title'] = title

        if ai_reply and request_id in active_requests:
            await add_message(chat_id, "assistant", ai_reply)
            session['chats'][chat_id]['history'].append({"role": "assistant", "content": ai_reply})
            logger.debug(f"Ответ за {time.time() - start_time:.2f} сек")
            return jsonify({"ai_response": ai_reply, "chats": session['chats']})
        return jsonify({"ai_response": "Ответ отменён."}), 400

    return render_template("index.html", history=history, chats=session['chats'], active_chat=chat_id,
                          current_style=current_style, styles=STYLES.keys())

@app.route("/new_chat")
@async_to_sync
async def new_chat():
    user_id = session['user_id']
    chat_id = str(uuid.uuid4())
    await add_chat(chat_id, user_id)
    session["active_chat"] = chat_id
    session['chats'] = await get_all_chats(user_id)
    return redirect(url_for("index"))

@app.route("/switch_chat/<chat_id>")
@async_to_sync
async def switch_chat(chat_id):
    user_id = session['user_id']
    if await chat_exists(user_id, chat_id):
        session["active_chat"] = chat_id
        asyncio.create_task(_update_chat_last_active(chat_id))
        session['chats'] = await get_all_chats(user_id)
    else:
        return redirect(url_for("new_chat"))
    return redirect(url_for("index"))

async def _update_chat_last_active(chat_id):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE chats SET last_active = CURRENT_TIMESTAMP WHERE id = $1", chat_id)

@app.route("/reset_chat/<chat_id>", methods=["POST"])
@async_to_sync
async def reset_chat_route(chat_id):
    user_id = session['user_id']
    if await chat_exists(user_id, chat_id):
        asyncio.create_task(_reset_chat(chat_id))
        session['chats'][chat_id]['title'] = "Без названия"
        session['chats'][chat_id]['history'] = []
        session['chats'] = await get_all_chats(user_id)
    return redirect(url_for("index"))

async def _reset_chat(chat_id):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM messages WHERE chat_id = $1", chat_id)
        await conn.execute("UPDATE chats SET title = 'Без названия', last_active = CURRENT_TIMESTAMP WHERE id = $1", chat_id)

@app.route("/delete_chat/<chat_id>", methods=["POST"])
@async_to_sync
async def delete_chat_route(chat_id):
    user_id = session['user_id']
    if await chat_exists(user_id, chat_id):
        asyncio.create_task(_delete_chat(chat_id))
        if session["active_chat"] == chat_id:
            new_chat_id = str(uuid.uuid4())
            await add_chat(new_chat_id, user_id)
            session["active_chat"] = new_chat_id
            session['chats'][new_chat_id] = {"title": "Без названия", "history": []}
        session['chats'].pop(chat_id, None)
        session['chats'] = await get_all_chats(user_id)
    return redirect(url_for("index"))

async def _delete_chat(chat_id):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM messages WHERE chat_id = $1", chat_id)
        await conn.execute("DELETE FROM chats WHERE id = $1", chat_id)

@app.route("/stop_response", methods=["POST"])
def stop_response():
    for request_id in list(active_requests.keys()):
        active_requests[request_id] = False
    return jsonify({"status": "stopped"})

if __name__ == "__main__":
    # Для локального запуска
    import uvicorn
    port = int(os.environ.get("PORT", 5000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, log_level="debug")
