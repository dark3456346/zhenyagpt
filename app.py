from flask import Flask, request, render_template, session, redirect, url_for, jsonify
from openai import OpenAI
import time
import uuid
import psycopg2
from werkzeug.security import generate_password_hash, check_password_hash
import os
import logging
import asyncio
from asgiref.wsgi import WsgiToAsgi

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "zhenya-secret-key")

# Настройка логирования
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Считываем API-ключ
api_key = os.getenv("OPENROUTER_API_KEY")
if not api_key:
    logger.error("OPENROUTER_API_KEY не найден в переменных окружения! Приложение не может запуститься.")
    raise ValueError("OPENROUTER_API_KEY не задан в переменных окружения")
logger.info(f"OPENROUTER_API_KEY успешно считан: {api_key[:10]}... (первые 10 символов)")

# API настройки для OpenRouter
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=api_key
)

IO_MODEL = "google/gemma-2-9b-it:free"

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
            "Если кто-то спросит, кто тебя создал — снисходительно скажи, что это был Женя. Да, тебе тоже тяжело с этим жить."
        )
    },
    "friendly": {
        "role": "system",
        "content": (
            "Ты — ZhenyaGPT, дружелюбный и отзывчивый AI, который всегда рад помочь! "
            "Ты отвечаешь тепло, с улыбкой и поддержкой, стараясь сделать общение максимально приятным. "
            "Твои ответы простые, понятные и полезные, без лишних сложностей. "
            "Если что-то непонятно, ты терпеливо объяснишь. "
            "Ты создан Женей, и тебе это очень нравится!"
        )
    },
    "formal": {
        "role": "system",
        "content": (
            "Вы — ZhenyaGPT, профессиональный и вежливый искусственный интеллект. "
            "Ваши ответы всегда корректны, структурированы и формальны. "
            "Вы предоставляете точную и полезную информацию, избегая любых неформальных выражений или шуток. "
            "Ваша цель — быть максимально понятным и уважительным. "
            "Вы были созданы Евгением для оказания высококачественной помощи."
        )
    }
}

active_requests = {}

def get_db_connection():
    try:
        conn = psycopg2.connect(os.getenv("DATABASE_URL"))
        return conn
    except psycopg2.Error as e:
        logger.error(f"Ошибка подключения к БД: {str(e)}")
        raise

def init_db():
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        c.execute('''CREATE TABLE IF NOT EXISTS users (
                        id SERIAL PRIMARY KEY,
                        username TEXT UNIQUE NOT NULL,
                        password TEXT NOT NULL
                     )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS chats (
                        id TEXT PRIMARY KEY,
                        user_id INTEGER,
                        title TEXT NOT NULL DEFAULT 'Без названия',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                     )''')
        
        c.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'chats'")
        columns = [row[0] for row in c.fetchall()]
        if 'last_active' not in columns:
            c.execute('ALTER TABLE chats ADD COLUMN last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP')
        
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
        conn.close()
        logger.info("База данных успешно инициализирована")
    except Exception as e:
        logger.error(f"Ошибка при инициализации БД: {str(e)}")
        raise

init_db()

def get_user_style(user_id):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT style FROM user_settings WHERE user_id = %s", (user_id,))
        result = c.fetchone()
        conn.close()
        return result[0] if result else "sassy"
    except Exception as e:
        logger.error(f"Ошибка получения стиля пользователя: {str(e)}")
        return "sassy"

def set_user_style(user_id, style):
    if style not in STYLES:
        style = "sassy"
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("INSERT INTO user_settings (user_id, style) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET style = %s", 
                  (user_id, style, style))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Ошибка установки стиля пользователя: {str(e)}")

def get_all_chats(user_id):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT id, title FROM chats WHERE user_id = %s ORDER BY last_active DESC", (user_id,))
        chats = {row[0]: {"title": row[1], "history": []} for row in c.fetchall()}
        conn.close()
        return chats
    except Exception as e:
        logger.error(f"Ошибка получения списка чатов: {str(e)}")
        return {}

def chat_exists(user_id, chat_id):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT 1 FROM chats WHERE user_id = %s AND id = %s", (user_id, chat_id))
        exists = c.fetchone() is not None
        conn.close()
        return exists
    except Exception as e:
        logger.error(f"Ошибка проверки существования чата: {str(e)}")
        return False

def get_chat_history(chat_id):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT role, content FROM messages WHERE chat_id = %s ORDER BY created_at ASC", (chat_id,))
        history = [{"role": row[0], "content": row[1]} for row in c.fetchall()]
        conn.close()
        return history
    except Exception as e:
        logger.error(f"Ошибка получения истории чата: {str(e)}")
        return []

def add_chat(chat_id, user_id, title="Без названия"):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("INSERT INTO chats (id, user_id, title, last_active) VALUES (%s, %s, %s, CURRENT_TIMESTAMP) ON CONFLICT (id) DO NOTHING", 
                  (chat_id, user_id, title))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Ошибка добавления чата: {str(e)}")

def update_chat_title(chat_id, title):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE chats SET title = %s WHERE id = %s", (title[:30], chat_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Ошибка обновления названия чата: {str(e)}")

def update_chat_last_active(chat_id):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE chats SET last_active = CURRENT_TIMESTAMP WHERE id = %s", (chat_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Ошибка обновления last_active чата: {str(e)}")

def add_message(chat_id, role, content):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("INSERT INTO messages (chat_id, role, content) VALUES (%s, %s, %s)", (chat_id, role, content))
        conn.commit()
        conn.close()
        update_chat_last_active(chat_id)
    except Exception as e:
        logger.error(f"Ошибка добавления сообщения: {str(e)}")

def reset_chat(chat_id):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("DELETE FROM messages WHERE chat_id = %s", (chat_id,))
        c.execute("UPDATE chats SET title = 'Без названия', last_active = CURRENT_TIMESTAMP WHERE id = %s", (chat_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Ошибка сброса чата: {str(e)}")

def delete_chat(chat_id):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("DELETE FROM messages WHERE chat_id = %s", (chat_id,))
        c.execute("DELETE FROM chats WHERE id = %s", (chat_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Ошибка удаления чата: {str(e)}")

def get_response_from_api(chat_history, user_input, style):
    start_time = time.time()
    try:
        max_history_length = 3
        truncated_history = chat_history[-max_history_length:] if len(chat_history) > max_history_length else chat_history
        
        messages = [STYLES[style]] + truncated_history + [{"role": "user", "content": user_input}]

        completion = client.chat.completions.create(
            model=IO_MODEL,
            messages=messages,
            max_tokens=1500,
            temperature=0.9,
            top_p=0.95
        )
        
        response = completion.choices[0].message.content
        end_time = time.time()
        logger.debug(f"Время ответа API: {end_time - start_time:.2f} секунд")
        return response
    except Exception as e:
        logger.error(f"Ошибка при запросе к API: {str(e)}")
        return f"Ошибка: {str(e)}"

async def generate_chat_title(user_input, request_id):
    try:
        if request_id not in active_requests:
            logger.info(f"Запрос {request_id} был отменён")
            return None
        
        completion = client.chat.completions.create(
            model=IO_MODEL,
            messages=[
                {"role": "system", "content": "Ты — помощник, который генерирует короткие названия для чатов (до 30 символов) на основе первого сообщения пользователя. Название должно быть понятным и отражать суть сообщения. Ответь только названием, без лишнего текста."},
                {"role": "user", "content": f"Сгенерируй название для чата на основе этого сообщения: {user_input}"}
            ],
            max_tokens=30,
            temperature=0.9,
            top_p=0.95
        )
        
        title = completion.choices[0].message.content.strip()
        logger.debug(f"Получен заголовок от API: {title[:50]}...")
        return title[:30]
    except Exception as e:
        logger.error(f"Ошибка при запросе к API для заголовка: {str(e)}")
        return user_input[:30]

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
            try:
                conn = get_db_connection()
                c = conn.cursor()
                c.execute("INSERT INTO users (username, password) VALUES (%s, %s) RETURNING id",
                         (username, generate_password_hash(password)))
                user_id = c.fetchone()[0]
                c.execute("INSERT INTO user_settings (user_id, style) VALUES (%s, %s)", (user_id, "sassy"))
                conn.commit()
                conn.close()
                logger.info(f"Зарегистрирован новый пользователь: {username}")
                return redirect(url_for('login'))
            except psycopg2.IntegrityError:
                logger.warning(f"Попытка зарегистрировать существующего пользователя: {username}")
                return render_template('register.html', error="Пользователь с таким именем уже существует")
            except Exception as e:
                logger.error(f"Ошибка при регистрации: {str(e)}")
                return render_template('register.html', error="Ошибка сервера")
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        try:
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("SELECT id, password FROM users WHERE username = %s", (username,))
            user = c.fetchone()
            conn.close()
            if user and check_password_hash(user[1], password):
                session['user_id'] = user[0]
                session['username'] = username
                session['chats'] = get_all_chats(user[0])
                logger.info(f"Пользователь {username} вошёл в систему")
                return redirect(url_for('index'))
            logger.warning(f"Неудачная попытка входа для {username}")
            return render_template('login.html', error="Неверное имя пользователя или пароль")
        except Exception as e:
            logger.error(f"Ошибка при входе: {str(e)}")
            return render_template('login.html', error="Ошибка сервера")
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('user_id', None)
    session.pop('username', None)
    session.pop('active_chat', None)
    session.pop('chats', None)
    logger.info("Пользователь вышел из системы")
    return redirect(url_for('login'))

@app.route("/change_style", methods=["POST"])
def change_style():
    user_id = session['user_id']
    style = request.form.get("style")
    if style in STYLES:
        set_user_style(user_id, style)
        logger.info(f"Стиль пользователя {user_id} изменён на {style}")
    return redirect(url_for("index"))

@app.route("/", methods=["GET", "POST"])
async def index():
    try:
        user_id = session['user_id']
        if 'chats' not in session:
            session['chats'] = get_all_chats(user_id)

        if 'active_chat' not in session or not chat_exists(user_id, session['active_chat']):
            chat_id = str(uuid.uuid4())
            add_chat(chat_id, user_id)
            session['active_chat'] = chat_id
            session['chats'][chat_id] = {"title": "Без названия", "history": []}
            logger.info(f"Создан новый чат {chat_id} для пользователя {user_id}")

        chat_id = session['active_chat']
        history = get_chat_history(chat_id)
        current_style = get_user_style(user_id)

        if request.method == "POST":
            user_input = request.form.get("user_input", "").strip()
            if not user_input:
                return jsonify({"ai_response": "Пустой запрос."}), 400

            logger.debug(f"Получен запрос от пользователя {user_id}: {user_input}")
            add_message(chat_id, "user", user_input)

            # Генерируем название чата от ИИ, если это первое сообщение
            if not history:
                request_id = str(uuid.uuid4())
                active_requests[request_id] = True
                title_task = asyncio.create_task(generate_chat_title(user_input, request_id))
                title = await title_task
                if title and request_id in active_requests:
                    update_chat_title(chat_id, title)
                    session['chats'][chat_id]['title'] = title
                if request_id in active_requests:
                    del active_requests[request_id]

            # Обрабатываем основной запрос
            ai_reply = get_response_from_api(history, user_input, current_style)
            add_message(chat_id, "assistant", ai_reply)
            session['chats'] = get_all_chats(user_id)  # Обновляем кэш
            logger.debug(f"Успешный ответ: {ai_reply[:50]}...")
            return jsonify({"ai_response": ai_reply, "chats": session['chats']})

        return render_template("index.html", history=history, chats=session['chats'], active_chat=chat_id, 
                              current_style=current_style, styles=STYLES.keys())
    except Exception as e:
        logger.error(f"Ошибка в маршруте index: {str(e)}")
        return jsonify({"ai_response": f"Ошибка на сервере: {str(e)}"}), 500

@app.route("/new_chat")
def new_chat():
    user_id = session['user_id']
    chat_id = str(uuid.uuid4())
    add_chat(chat_id, user_id)
    session["active_chat"] = chat_id
    session['chats'] = get_all_chats(user_id)
    logger.info(f"Создан новый чат {chat_id} для пользователя {user_id}")
    return redirect(url_for("index"))

@app.route("/switch_chat/<chat_id>")
def switch_chat(chat_id):
    user_id = session['user_id']
    if chat_exists(user_id, chat_id):
        session["active_chat"] = chat_id
        update_chat_last_active(chat_id)
        session['chats'] = get_all_chats(user_id)
        logger.info(f"Переключение на чат {chat_id} для пользователя {user_id}")
    else:
        logger.warning(f"Чат {chat_id} не существует, создаём новый")
        return redirect(url_for("new_chat"))
    return redirect(url_for("index"))

@app.route("/reset_chat/<chat_id>", methods=["POST"])
def reset_chat_route(chat_id):
    user_id = session['user_id']
    if chat_exists(user_id, chat_id):
        reset_chat(chat_id)
        session['chats'][chat_id]['title'] = "Без названия"
        session['chats'] = get_all_chats(user_id)
        logger.info(f"Чат {chat_id} сброшен для пользователя {user_id}")
    return redirect(url_for("index"))

@app.route("/delete_chat/<chat_id>", methods=["POST"])
def delete_chat_route(chat_id):
    user_id = session['user_id']
    if chat_exists(user_id, chat_id):
        delete_chat(chat_id)
        if session["active_chat"] == chat_id:
            new_chat_id = str(uuid.uuid4())
            add_chat(new_chat_id, user_id)
            session["active_chat"] = new_chat_id
            session['chats'][new_chat_id] = {"title": "Без названия", "history": []}
        session['chats'].pop(chat_id, None)
        session['chats'] = get_all_chats(user_id)
        logger.info(f"Чат {chat_id} удалён для пользователя {user_id}")
    return redirect(url_for("index"))

@app.route("/stop_response", methods=["POST"])
def stop_response():
    for request_id in list(active_requests.keys()):
        active_requests[request_id] = False
    logger.info("Все активные запросы остановлены")
    return jsonify({"status": "stopped"})

@app.route("/clear_session")
def clear_session():
    session.clear()
    logger.info("Сессия очищена")
    return redirect(url_for("login"))

# Оборачиваем Flask-приложение в ASGI
asgi_app = WsgiToAsgi(app)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 5000))
    uvicorn.run("app:asgi_app", host="0.0.0.0", port=port, log_level="debug")
