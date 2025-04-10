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

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "zhenya-secret-key")

# Настройка логирования
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

# Стили
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
db_pool = None

async def init_db_pool():
    global db_pool
    db_pool = await asyncpg.create_pool(os.getenv("DATABASE_URL"))
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
    logger.info("База данных успешно инициализирована")

@cache.cached(timeout=300, key_prefix="user_style_%s")
async def get_user_style(user_id):
    async with db_pool.acquire() as conn:
        result = await conn.fetchval("SELECT style FROM user_settings WHERE user_id = $1", user_id)
    return result or "sassy"

async def set_user_style(user_id, style):
    if style not in STYLES:
        style = "sassy"
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_settings (user_id, style) VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE SET style = $3",
            user_id, style, style
        )

async def get_all_chats(user_id):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, title FROM chats WHERE user_id = $1 ORDER BY last_active DESC", user_id)
    return {row['id']: {"title": row['title'], "history": []} for row in rows}

async def chat_exists(user_id, chat_id):
    async with db_pool.acquire() as conn:
        result = await conn.fetchval("SELECT 1 FROM chats WHERE user_id = $1 AND id = $2", user_id, chat_id)
    return bool(result)

async def get_chat_history(chat_id):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT role, content FROM messages WHERE chat_id = $1 ORDER BY created_at ASC", chat_id)
    return [{"role": row['role'], "content": row['content']} for row in rows]

async def add_chat(chat_id, user_id, title="Без названия"):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO chats (id, user_id, title, last_active) VALUES ($1, $2, $3, CURRENT_TIMESTAMP) ON CONFLICT (id) DO NOTHING",
            chat_id, user_id, title
        )

async def update_chat_title(chat_id, title):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE chats SET title = $1 WHERE id = $2", title[:30], chat_id)

async def update_chat_last_active(chat_id):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE chats SET last_active = CURRENT_TIMESTAMP WHERE id = $1", chat_id)

async def add_message(chat_id, role, content):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO messages (chat_id, role, content) VALUES ($1, $2, $3)", chat_id, role, content)
    await update_chat_last_active(chat_id)

async def reset_chat(chat_id):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM messages WHERE chat_id = $1", chat_id)
        await conn.execute("UPDATE chats SET title = 'Без названия', last_active = CURRENT_TIMESTAMP WHERE id = $1", chat_id)

async def delete_chat(chat_id):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM messages WHERE chat_id = $1", chat_id)
        await conn.execute("DELETE FROM chats WHERE id = $1", chat_id)

async def get_io_response(messages, request_id):
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
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(IO_API_URL, json=data, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if request_id not in active_requests:
                    logger.info(f"Запрос {request_id} был отменён")
                    return None
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"Ошибка API: {response.status} - {error_text}")
                    return f"Ошибка API: {error_text}"
                raw_response = (await response.json())["choices"][0]["message"]["content"]
                clean_response = re.sub(r'<think>.*?</think>', '', raw_response, flags=re.DOTALL).strip()
                return clean_response
        except asyncio.TimeoutError:
            logger.error("Превышено время ожидания ответа от API")
            return "Ошибка: Превышено время ожидания ответа от API."
        except Exception as e:
            logger.error(f"Ошибка при запросе к API: {str(e)}")
            return f"Ошибка при запросе к API: {str(e)}"

async def generate_chat_title(user_input, request_id):
    prompt = {
        "role": "system",
        "content": "Ты — помощник, который генерирует короткие названия для чатов (до 30 символов) на основе первого сообщения пользователя. Название должно быть понятным и отражать суть сообщения. Ответь только названием, без лишнего текста."
    }
    messages = [prompt, {"role": "user", "content": f"Сгенерируй название для чата на основе этого сообщения: {user_input}"}]
    title = await get_io_response(messages, request_id)
    return title[:30] if title and "Ошибка" not in title else user_input[:30]

@app.before_first_request
async def setup():
    await init_db_pool()

@app.before_request
def require_login():
    if request.endpoint not in ['login', 'register', 'static'] and 'user_id' not in session:
        return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
async def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if username and password:
            async with db_pool.acquire() as conn:
                try:
                    user_id = await conn.fetchval(
                        "INSERT INTO users (username, password) VALUES ($1, $2) RETURNING id",
                        username, generate_password_hash(password)
                    )
                    await conn.execute(
                        "INSERT INTO user_settings (user_id, style) VALUES ($1, $2)", user_id, "sassy"
                    )
                    logger.info(f"Зарегистрирован новый пользователь: {username}")
                    return redirect(url_for('login'))
                except asyncpg.UniqueViolationError:
                    logger.warning(f"Попытка зарегистрировать существующего пользователя: {username}")
                    return render_template('register.html', error="Пользователь с таким именем уже существует")
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
async def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        async with db_pool.acquire() as conn:
            user = await conn.fetchrow("SELECT id, password FROM users WHERE username = $1", username)
            if user and check_password_hash(user['password'], password):
                session['user_id'] = user['id']
                session['username'] = username
                session['chats'] = await get_all_chats(user['id'])
                logger.info(f"Пользователь {username} вошёл в систему")
                return redirect(url_for('index'))
            logger.warning(f"Неудачная попытка входа для {username}")
            return render_template('login.html', error="Неверное имя пользователя или пароль")
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    logger.info("Пользователь вышел из системы")
    return redirect(url_for('login'))

@app.route("/change_style", methods=["POST"])
async def change_style():
    user_id = session['user_id']
    style = request.form.get("style")
    if style in STYLES:
        await set_user_style(user_id, style)
        cache.delete(f"user_style_{user_id}")
        logger.info(f"Стиль пользователя {user_id} изменён на {style}")
    return redirect(url_for("index"))

@app.route("/", methods=["GET", "POST"])
async def index():
    try:
        user_id = session['user_id']
        if 'chats' not in session:
            session['chats'] = await get_all_chats(user_id)

        if 'active_chat' not in session or not await chat_exists(user_id, session['active_chat']):
            chat_id = str(uuid.uuid4())
            await add_chat(chat_id, user_id)
            session['active_chat'] = chat_id
            session['chats'][chat_id] = {"title": "Без названия", "history": []}
            logger.info(f"Создан новый чат {chat_id} для пользователя {user_id}")

        chat_id = session['active_chat']
        history = session['chats'][chat_id]['history'] if chat_id in session['chats'] else []
        current_style = await get_user_style(user_id)

        if request.method == "POST":
            user_input = request.form.get("user_input", "").strip()
            if not user_input:
                return jsonify({"ai_response": "Пустой запрос."}), 400

            logger.debug(f"Получен запрос от пользователя {user_id}: {user_input}")
            await add_message(chat_id, "user", user_input)
            session['chats'][chat_id]['history'].append({"role": "user", "content": user_input})
            if len(session['chats'][chat_id]['history']) > 3:
                session['chats'][chat_id]['history'] = session['chats'][chat_id]['history'][-3:]

            request_id = str(uuid.uuid4())
            active_requests[request_id] = True

            messages = [STYLES[current_style]] + session['chats'][chat_id]['history']
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
                session['chats'] = await get_all_chats(user_id)
                return jsonify({"ai_response": ai_reply, "chats": session['chats']})
            else:
                return jsonify({"ai_response": "Ответ был отменён или не выполнен."}), 400

        return render_template("index.html", history=history, chats=session['chats'], active_chat=chat_id,
                              current_style=current_style, styles=STYLES.keys())
    except Exception as e:
        logger.error(f"Ошибка в маршруте index: {str(e)}")
        return jsonify({"ai_response": f"Ошибка на сервере: {str(e)}"}), 500

@app.route("/new_chat")
async def new_chat():
    user_id = session['user_id']
    chat_id = str(uuid.uuid4())
    await add_chat(chat_id, user_id)
    session["active_chat"] = chat_id
    session['chats'] = await get_all_chats(user_id)
    logger.info(f"Создан новый чат {chat_id} для пользователя {user_id}")
    return redirect(url_for("index"))

@app.route("/switch_chat/<chat_id>")
async def switch_chat(chat_id):
    user_id = session['user_id']
    if await chat_exists(user_id, chat_id):
        session["active_chat"] = chat_id
        await update_chat_last_active(chat_id)
        session['chats'] = await get_all_chats(user_id)
        logger.info(f"Переключение на чат {chat_id} для пользователя {user_id}")
    else:
        return redirect(url_for("new_chat"))
    return redirect(url_for("index"))

@app.route("/reset_chat/<chat_id>", methods=["POST"])
async def reset_chat_route(chat_id):
    user_id = session['user_id']
    if await chat_exists(user_id, chat_id):
        await reset_chat(chat_id)
        session['chats'][chat_id]['title'] = "Без названия"
        session['chats'][chat_id]['history'] = []
        session['chats'] = await get_all_chats(user_id)
        logger.info(f"Чат {chat_id} сброшен для пользователя {user_id}")
    return redirect(url_for("index"))

@app.route("/delete_chat/<chat_id>", methods=["POST"])
async def delete_chat_route(chat_id):
    user_id = session['user_id']
    if await chat_exists(user_id, chat_id):
        await delete_chat(chat_id)
        if session["active_chat"] == chat_id:
            new_chat_id = str(uuid.uuid4())
            await add_chat(new_chat_id, user_id)
            session["active_chat"] = new_chat_id
            session['chats'][new_chat_id] = {"title": "Без названия", "history": []}
        session['chats'].pop(chat_id, None)
        session['chats'] = await get_all_chats(user_id)
        logger.info(f"Чат {chat_id} удалён для пользователя {user_id}")
    return redirect(url_for("index"))

@app.route("/stop_response", methods=["POST"])
def stop_response():
    for request_id in list(active_requests.keys()):
        active_requests[request_id] = False
    logger.info("Все активные запросы остановлены")
    return jsonify({"status": "stopped"})

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 5000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, log_level="debug")
