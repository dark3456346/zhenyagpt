from quart import Quart, request, render_template, session, redirect, url_for, jsonify
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

app = Quart(__name__)
app.secret_key = os.getenv("SECRET_KEY", "zhenya-secret-key")

# Настройка логирования
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Кэширование
cache = Cache(app, config={'CACHE_TYPE': 'simple'})

# OpenRouter API настройки
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "YOUR_OPENROUTER_API_KEY")  # Замени на свой ключ
OPENROUTER_MODEL = "deepseek/deepseek-v3-base:free"

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
    logger.info("База данных успешно инициализирована")

async def get_user_style(user_id):
    cache_key = f"user_style_{user_id}"
    cached_style = cache.get(cache_key)
    if cached_style:
        return cached_style
    async with db_pool.acquire() as conn:
        result = await conn.fetchval("SELECT style FROM user_settings WHERE user_id = $1", user_id)
    style = result or "sassy"
    cache.set(cache_key, style, timeout=300)
    return style

async def set_user_style(user_id, style):
    if style not in STYLES:
        style = "sassy"
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_settings (user_id, style) VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE SET style = $3",
            user_id, style, style
        )
    cache.delete(f"user_style_{user_id}")

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
    asyncio.create_task(_update_chat_title(chat_id, title))

async def _update_chat_title(chat_id, title):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE chats SET title = $1 WHERE id = $2", title[:30], chat_id)

async def update_chat_last_active(chat_id):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE chats SET last_active = CURRENT_TIMESTAMP WHERE id = $1", chat_id)

async def add_message(chat_id, role, content):
    asyncio.create_task(_add_message(chat_id, role, content))

async def _add_message(chat_id, role, content):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO messages (chat_id, role, content) VALUES ($1, $2, $3)", chat_id, role, content)
        await conn.execute("UPDATE chats SET last_active = CURRENT_TIMESTAMP WHERE id = $1", chat_id)

async def reset_chat(chat_id):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM messages WHERE chat_id = $1", chat_id)
        await conn.execute("UPDATE chats SET title = 'Без названия', last_active = CURRENT_TIMESTAMP WHERE id = $1", chat_id)

async def delete_chat(chat_id):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM messages WHERE chat_id = $1", chat_id)
        await conn.execute("DELETE FROM chats WHERE id = $1", chat_id)

async def get_io_response(messages, request_id):
    start_time = time.time()
    cache_key = f"ai_response_{hash(str(messages))}_{request_id}"
    cached_response = cache.get(cache_key)
    if cached_response:
        logger.debug(f"Кэшированный ответ за {time.time() - start_time:.2f} сек")
        return cached_response

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENROUTER_API_KEY}"
    }
    data = {
        "model": OPENROUTER_MODEL,
        "messages": messages,
        "max_tokens": 1500,
        "temperature": 0.9,
        "top_p": 0.95
    }
    async with aiohttp.ClientSession() as session:
        logger.debug(f"Начало запроса к OpenRouter API для {request_id}")
        try:
            async with session.post(OPENROUTER_API_URL, json=data, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as response:
                if request_id not in active_requests:
                    logger.info(f"Запрос {request_id} был отменён")
                    return None
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"Ошибка OpenRouter API: {response.status} - {error_text}")
                    return f"Ошибка API: {error_text}"
                result = await response.json()
                clean_response = result["choices"][0]["message"]["content"].strip()
                logger.debug(f"OpenRouter API ответ за {time.time() - start_time:.2f} сек")
                cache.set(cache_key, clean_response, timeout=60)
                return clean_response
        except asyncio.TimeoutError:
            logger.error("Превышено время ожидания ответа от OpenRouter API")
            return "Ошибка: Превышено время ожидания ответа от API."
        except Exception as e:
            logger.error(f"Ошибка при запросе к OpenRouter API: {str(e)}")
            return f"Ошибка при запросе к API: {str(e)}"

async def generate_chat_title(user_input, request_id):
    prompt = {
        "role": "system",
        "content": "Ты — помощник, который генерирует короткие названия для чатов (до 30 символов) на основе первого сообщения пользователя. Название должно быть понятным и отражать суть сообщения. Ответь только названием, без лишнего текста."
    }
    messages = [prompt, {"role": "user", "content": f"Сгенерируй название для чата на основе этого сообщения: {user_input}"}]
    title = await get_io_response(messages, request_id)
    return title[:30] if title and "Ошибка" not in title else user_input[:30]

@app.before_serving
async def setup():
    await init_db_pool()

@app.before_request
async def require_login():
    if request.endpoint not in ['login', 'register', 'static'] and 'user_id' not in session:
        return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
async def register():
    if request.method == 'POST':
        username = (await request.form).get('username')
        password = (await request.form).get('password')
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
                    return await render_template('register.html', error="Пользователь с таким именем уже существует")
    return await render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
async def login():
    if request.method == 'POST':
        username = (await request.form).get('username')
        password = (await request.form).get('password')
        async with db_pool.acquire() as conn:
            user = await conn.fetchrow("SELECT id, password FROM users WHERE username = $1", username)
            if user and check_password_hash(user['password'], password):
                session['user_id'] = user['id']
                session['username'] = username
                session['chats'] = await get_all_chats(user['id'])
                logger.info(f"Пользователь {username} вошёл в систему")
                return redirect(url_for('index'))
            logger.warning(f"Неудачная попытка входа для {username}")
            return await render_template('login.html', error="Неверное имя пользователя или пароль")
    return await render_template('login.html')

@app.route('/logout')
async def logout():
    session.clear()
    logger.info("Пользователь вышел из системы")
    return redirect(url_for('login'))

@app.route("/change_style", methods=["POST"])
async def change_style():
    user_id = session['user_id']
    style = (await request.form).get("style")
    if style in STYLES:
        await set_user_style(user_id, style)
        logger.info(f"Стиль пользователя {user_id} изменён на {style}")
    return redirect(url_for("index"))

@app.route("/", methods=["GET", "POST"])
async def index():
    start_time = time.time()
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
            user_input = (await request.form).get("user_input", "").strip()
            if not user_input:
                return jsonify({"ai_response": "Пустой запрос."}), 400

            logger.debug(f"Получен запрос от пользователя {user_id}: {user_input}")
            await add_message(chat_id, "user", user_input)
            session['chats'][chat_id]['history'].append({"role": "user", "content": user_input})
            if len(session['chats'][chat_id]['history']) > 3:
                session['chats'][chat_id]['history'] = session['chats'][chat_id]['history'][-3:]

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
                session['chats'] = await get_all_chats(user_id)
                logger.debug(f"Ответ за {time.time() - start_time:.2f} сек")
                return jsonify({"ai_response": ai_reply, "chats": session['chats']})
            else:
                return jsonify({"ai_response": "Ответ был отменён или не выполнен."}), 400

        return await render_template("index.html", history=history, chats=session['chats'], active_chat=chat_id,
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
async def stop_response():
    for request_id in list(active_requests.keys()):
        active_requests[request_id] = False
    logger.info("Все активные запросы остановлены")
    return jsonify({"status": "stopped"})

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 5000))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="debug")
