import logging
import sys
from os import getenv
from config import TOKEN, ADMIN_CHAT_ID, PGPASS, DB_USER, DB_NAME, DB_HOST, DB_PORT
# from dotenv import load_dotenv
from keyboard.builder import reply, inline_btn, inline_kb
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from datetime import datetime, timedelta, date
from asyncpg import Connection
import asyncio
import asyncpg
import sqlite3
from typing import Optional, Tuple
import re


class Help(StatesGroup):
    reg = State()
    form = State()


dp = Dispatcher()
db_pool: asyncpg.Pool = None
pending_users = {}


import asyncpg

db_pool = None  # глобальная переменная

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(
        user=DB_USER,
        password=PGPASS,
        database=DB_NAME,         # <- используй имя своей БД
        host=DB_HOST,
        port=DB_PORT
    )

    async with db_pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            name TEXT,
            age INTEGER,
            fitness_level INTEGER,
            training_time INTEGER,
            reminder_time TEXT,
            chat_id BIGINT UNIQUE,
            exercise_types TEXT,
            created_at DATE DEFAULT CURRENT_DATE
        );
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS exercises (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            level INTEGER NOT NULL,
            description TEXT NOT NULL,
            repetitions TEXT NOT NULL
        );
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS exercise_types (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE
        );
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS exercise_type_links (
            exercise_id INTEGER REFERENCES exercises(id),
            type_id INTEGER REFERENCES exercise_types(id),
            PRIMARY KEY (exercise_id, type_id)
        );
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS workout_log (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id),
            date DATE,
            completed INTEGER DEFAULT 1
        );
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id),
            date DATE,
            rating INTEGER,
            comment TEXT
        );
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS challenges (
            user_id INTEGER PRIMARY KEY REFERENCES users(id),
            type TEXT,
            start_date DATE,
            progress INTEGER DEFAULT 0,
            target INTEGER,
            active INTEGER DEFAULT 1
        );
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS questions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id),
            chat_id BIGINT,
            text TEXT,
            date DATE DEFAULT CURRENT_DATE,
            status TEXT DEFAULT 'open'
        );
        """)

    print("✅ База данных и таблицы инициализированы.")



# Определяем состояния
class UserState(StatesGroup):
    name = State()
    age = State()
    fitness_level = State()
    training_time = State()
    reminder_time = State()
    exercise_types = State()
    waiting_feedback_rating = State()
    waiting_feedback_comment = State()


class Help(StatesGroup):
    reg = State()
    form = State()
    answering = State()  # 🔹 новое состояние


async def get_streak_visual(user_id: int, days: int = 28) -> str:
    async with db_pool.acquire() as conn:
        # Получаем дату регистрации пользователя
        result = await conn.fetchval("SELECT created_at FROM users WHERE id = $1", user_id)
        if not result:
            return "Нет данных 📉"

        created_date = result
        today = date.today()
        days_since_start = (today - created_date).days + 1
        visual_length = min(days_since_start, days)  # не больше 28

        visual = []

        for i in range(visual_length):
            check_date = created_date + timedelta(days=i)
            completed = await conn.fetchval(
                "SELECT completed FROM workout_log WHERE user_id = $1 AND date = $2",
                user_id,
                check_date
            )
            visual.append("✅" if completed == 1 else "❌")

        # Разбиваем по 7 символов в строке
        lines = [ ''.join(visual[i:i+7]) for i in range(0, len(visual), 7) ]
        return "\n".join(lines)


# Функция запроса имени
@dp.message(Command("start"))
async def start(message: Message, state: FSMContext):
    chat_id = message.chat.id

    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT id, name FROM users WHERE chat_id = $1", chat_id)

    if user:
        user_id = user["id"]
        name = user["name"]

        # Показываем выбор
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Продолжить", callback_data="continue"),
                InlineKeyboardButton(text="Создать новый аккаунт", callback_data="create_new")
            ]
        ])
        await message.answer(
            f"Ты уже зарегистрирован как {name}. Что хочешь сделать?",
            reply_markup=keyboard
        )
    else:
        # Начинаем регистрацию сразу
        await state.update_data(user_id=message.from_user.id)
        await message.answer("Привет! Давай начнем регистрацию. Как тебя зовут?")
        await state.set_state(UserState.name)



@dp.callback_query(F.data.in_({"continue", "create_new"}))
async def handle_start_choice(callback: CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id

    if callback.data == "continue":
        await callback.message.answer("Продолжаем с текущим аккаунтом 💪")
        await state.clear()

    elif callback.data == "create_new":
        async with db_pool.acquire() as conn:
            user = await conn.fetchrow("SELECT id, name FROM users WHERE chat_id = $1", chat_id)

            if user:
                user_id = user["id"]
                old_name = user["name"]

                # Деактивируем старый аккаунт
                await conn.execute(
                    "UPDATE users SET chat_id = NULL, name = $1 WHERE id = $2",
                    f"_Deactivated_{old_name}",
                    user_id
                )

        await callback.message.answer("Создаем новый аккаунт. Как тебя зовут?")
        await state.update_data(user_id=callback.from_user.id)
        await state.set_state(UserState.name)

    await callback.answer()  # закрывает "часики"



@dp.message(UserState.name)
async def get_name(message: Message, state: FSMContext):
    user_id = message.from_user.id  # пока временно, окончательно перезапишется в get_reminder_time
    await state.update_data(name=message.text, user_id=user_id)
    await message.answer("Сколько тебе лет?")
    await state.set_state(UserState.age)


# Функция запроса возраста
@dp.message(UserState.age)
async def get_age(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Пожалуйста, введи возраст числом.")
        return

    await state.update_data(age=int(message.text))

    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="1"), KeyboardButton(text="2")],
            [KeyboardButton(text="3"), KeyboardButton(text="4")],
            [KeyboardButton(text="5")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )

    await message.answer(
        "Какой у тебя уровень физической подготовки?\n"
        "1 - Начальный (малоподвижный образ жизни)\n"
        "2 - Ниже среднего (небольшая активность)\n"
        "3 - Средний (регулярные тренировки)\n"
        "4 - Выше среднего (интенсивные тренировки)\n"
        "5 - Высокий (профессиональный уровень)",
        reply_markup=keyboard
    )
    await state.set_state(UserState.fitness_level)


# Функция запроса уровня подготовки
@dp.message(UserState.fitness_level)
async def get_fitness_level(message: Message, state: FSMContext):
    if message.text not in ["1", "2", "3", "4", "5"]:
        await message.answer("Пожалуйста, выбери один из предложенных вариантов, нажав на кнопку.")
        return

    await state.update_data(fitness_level=int(message.text))

    # Предложим выбрать типы упражнений
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Пресс"), KeyboardButton(text="Растяжка")],
            [KeyboardButton(text="Руки"), KeyboardButton(text="Спина")],
            [KeyboardButton(text="Ноги"), KeyboardButton(text="Глаза")],
            [KeyboardButton(text="Готово")]
        ],
        resize_keyboard=True
    )

    await message.answer(
        "Выбери типы упражнений, которые тебе интересны (можно несколько). Когда закончишь — нажми 'Готово'.",
        reply_markup=keyboard
    )
    await state.update_data(exercise_types=[])
    await state.set_state(UserState.exercise_types)


@dp.message(UserState.exercise_types)
async def choose_types(message: Message, state: FSMContext):
    if message.text.lower() == "готово":
        data = await state.get_data()
        selected = data.get("exercise_types", [])
        if not selected:
            await message.answer("Ты не выбрал ни одного типа. Пожалуйста, выбери хотя бы один.")
            return

        #await message.answer("Сколько минут ты готов уделять тренировке?", reply_markup=types.ReplyKeyboardRemove())
        #await state.set_state(UserState.training_time)
        await message.answer(
            "Во сколько по времени тебе отправлять напоминания о тренировке? (Введи в формате ЧЧ:ММ, например, 08:00)",
            reply_markup=types.ReplyKeyboardRemove())
        await state.set_state(UserState.reminder_time)
    else:
        data = await state.get_data()
        selected = data.get("exercise_types", [])
        if message.text not in selected:
            selected.append(message.text)
        await state.update_data(exercise_types=selected)

# Функция запроса времени на тренировку
# @dp.message(UserState.training_time)
# async def get_training_time(message: Message, state: FSMContext):
#     if not message.text.isdigit():
#         await message.answer("Пожалуйста, введи время в минутах.")
#         return
#     await state.update_data(training_time=int(message.text))
#     await message.answer("Во сколько по времени тебе отправлять напоминания о тренировке? (например, 08:00)")
#     await state.set_state(UserState.reminder_time)


# Функция запроса времени напоминания
@dp.message(UserState.reminder_time)
async def get_reminder_time(message: Message, state: FSMContext):
    user_data = await state.get_data()
    user_types = ",".join(user_data.get("exercise_types", []))
    created_at = datetime.date
    text = message.text.strip()

    # Попробуем нормализовать: 7:0 → 07:00
    match = re.match(r'^(\d{1,2}):(\d{1,2})$', text)
    if not match:
        await message.answer("Время должно быть в формате ЧЧ:ММ, например 08:30")
        return

    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        await message.answer("Пожалуйста, укажи корректное время от 00:00 до 23:59")
        return

    normalized_time = f"{hour:02d}:{minute:02d}"
    # Вставка пользователя и получение id
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO users (name, age, fitness_level, training_time, reminder_time, chat_id, exercise_types)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id
            """,
            user_data.get('name', ''),
            user_data.get('age', 0),
            user_data.get('fitness_level', 0),
            5,
            normalized_time,
            message.chat.id,
            user_types,
            #created_at
        )
        user_id = row["id"]

    await state.update_data(user_id=user_id)

    response = (
        f"Спасибо! Вот твои данные:\n"
        f"Имя: {user_data.get('name', 'Не указано')}\n"
        f"Возраст: {user_data.get('age', 'Не указано')}\n"
        f"Уровень физической подготовки: {user_data.get('fitness_level', 'Не указано')}\n"
        #f"Время на тренировку: {user_data.get('training_time', 'Не указано')} минут\n"
        f"Время напоминания: {message.text}"
    )

    await state.clear()
    await message.answer(response, reply_markup=types.ReplyKeyboardRemove())


# Эта функция возвращает случайное упражнение для заданного уровня подготовки.
# async def get_exercise_for_user(level: int, types: list[str]) -> Optional[Tuple[str, str, str]]:
#     if not types:
#         return None
#
#     placeholders = ','.join(f'${i + 2}' for i in range(len(types)))  # $2, $3, ...
#     query = f"""
#         SELECT e.name, e.description, e.repetitions
#         FROM exercises e
#         JOIN exercise_type_links l ON e.id = l.exercise_id
#         JOIN exercise_types t ON l.type_id = t.id
#         WHERE e.level <= $1 AND t.name IN ({placeholders})
#         ORDER BY RANDOM()
#         LIMIT 1
#     """
#
#     async with db_pool.acquire() as conn:
#         row = await conn.fetchrow(query, level, *types)
#         if row:
#             return row["name"], row["description"], row["repetitions"]
#         return None
async def get_exercise_for_user(level: int, types: list[str]) -> Optional[Tuple[str, str, str]]:
    if not types:
        print("⚠️ Нет типов упражнений для пользователя")
        return None

    # Формируем динамически аргументы и подстановки
    placeholders = ','.join(f'${i + 2}' for i in range(len(types)))  # $2, $3, ...
    query = f"""
        SELECT e.name, e.description, e.repetitions
        FROM exercises e
        JOIN exercise_type_links l ON e.id = l.exercise_id
        JOIN exercise_types t ON l.type_id = t.id
        WHERE e.level <= $1 AND t.name IN ({placeholders})
        ORDER BY RANDOM()
        LIMIT 1
    """

    try:
        async with db_pool.acquire() as conn:
            print(f"Запрос упражнений: level={level}, types={types}")
            row = await conn.fetchrow(query, level, *types)
            if row:
                print(f"🎯 Найдено упражнение: {row['name']}")
                return row["name"], row["description"], row["repetitions"]
            else:
                print("❌ Нет подходящих упражнений")
                return None
    except Exception as e:
        print(f"🚨 Ошибка при выборе упражнения: {e}")
        return None


# отправка уведомлений о тренировке
async def send_training_reminders(bot: Bot):
    while True:
        now = datetime.now().strftime("%H:%M")

        async with db_pool.acquire() as conn:
            users = await conn.fetch("""
                SELECT id, name, reminder_time, fitness_level, chat_id, exercise_types
                FROM users
            """)

        for user in users:
            user_id = user["id"]
            name = user["name"]
            reminder_time = user["reminder_time"]
            fitness_level = user["fitness_level"]
            # training_time = user["training_time"]
            chat_id = user["chat_id"]
            ex_types_str = user["exercise_types"]
            if chat_id:
                print(f"{chat_id} time is {now}; send time = {reminder_time}; should send = {reminder_time == now}")
            if reminder_time != now:
                continue
            types_list = ex_types_str.split(',') if ex_types_str else []
            exercise = await get_exercise_for_user(fitness_level, types_list)
            print(exercise)
            if exercise:
                ex_name, description, reps = exercise
                msg = (
                    f"Привет, {name}! 👋\n"
                    f"Пора на тренировку 💪\n\n"
                    f"🏋️ {ex_name}\n"
                    f"📋 {description}\n"
                    f"🔁 {reps}\n\n"
                    f"Как только закончишь — нажми кнопку."
                )

                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(text="Выполнено", callback_data=f"done_{user_id}"),
                        InlineKeyboardButton(text="Не выполнено", callback_data=f"skip_{user_id}")
                    ]
                ])

                try:
                    await bot.send_message(chat_id=chat_id, text=msg, reply_markup=keyboard)

                    # Сохраняем статус отправки
                    pending_users[user_id] = {
                        "chat_id": chat_id,
                        "sent_at": datetime.now(),
                        "answered": False
                    }

                except Exception as e:
                    logging.warning(f"Не удалось отправить сообщение {chat_id}: {e}")

        # Follow-up через 15 минут
        now_dt = datetime.now()
        for user_id, data in list(pending_users.items()):
            if not data["answered"] and now_dt - data["sent_at"] >= timedelta(minutes=15):
                try:
                    await bot.send_message(chat_id=data["chat_id"], text="Ты успел сделать тренировку? Напиши «Выполнено» 💪")
                except Exception as e:
                    logging.warning(f"Ошибка при follow-up для {data['chat_id']}: {e}")
                del pending_users[user_id]

        await asyncio.sleep(60) # Цикл раз в минуту


@dp.callback_query(F.data.startswith("done_"))
async def handle_done_callback(callback: CallbackQuery, state: FSMContext):
    user_id = int(callback.data.split("_")[1])
    today = datetime.now().date()

    async with db_pool.acquire() as conn:
        # Удаляем из pending, если был
        if user_id in pending_users:
            pending_users[user_id]["answered"] = True

        count = await conn.fetchval(
            "SELECT COUNT(*) FROM workout_log WHERE user_id = $1 AND date = $2",
            user_id, today
        )

        if count == 0:
            await conn.execute(
                "INSERT INTO workout_log (user_id, date, completed) VALUES ($1, $2, 1)",
                user_id, today
            )

        streak = await calculate_streak(user_id)
        visual = await get_streak_visual(user_id)

    # Ответ пользователю
    TARGET_STREAK = 7
    response = f"Отлично! Ты красавчик 💪 Прогресс засчитан!\nТекущий стрик: {streak} 🔥"
    if streak == TARGET_STREAK:
        response += f"\n\n🎉 ПОЗДРАВЛЯЕМ! Ты выполнил {TARGET_STREAK} тренировок подряд! 🚀"
    elif streak > TARGET_STREAK:
        response += f"\n💪 Ты уже превзошёл streak из {TARGET_STREAK} дней. Продолжай в том же духе!"

    await callback.message.answer(response)
    await callback.message.answer(f"Прогресс за последние дни:\n{visual}")
    await callback.message.answer(
        "Как бы ты оценил тренировку по 5-балльной шкале?",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=str(i)) for i in range(1, 6)]],
            resize_keyboard=True,
            one_time_keyboard=True
        )
    )

    await state.set_state(UserState.waiting_feedback_rating)
    await callback.answer()  # Убираем "часики"


@dp.callback_query(F.data.startswith("skip_"))
async def handle_not_done_callback(callback: CallbackQuery, state: FSMContext):
    user_id = int(callback.data.split("_")[1])
    today = datetime.now().date()

    async with db_pool.acquire() as conn:
        # Удаляем из pending, если был
        if user_id in pending_users:
            pending_users[user_id]["answered"] = True

        count = await conn.fetchval(
            "SELECT COUNT(*) FROM workout_log WHERE user_id = $1 AND date = $2",
            user_id, today
        )

        if count == 0:
            await conn.execute(
                "INSERT INTO workout_log (user_id, date, completed) VALUES ($1, $2, 0)",
                user_id, today
            )

        visual = await get_streak_visual(user_id)

    await callback.message.answer("Жаль, что не удалось потренироваться сегодня 😔")
    await callback.message.answer(f"Прогресс за последние дни:\n{visual}")
    await state.clear()
    await callback.answer()  # Убирает "часики" на кнопке


@dp.message(F.text.lower() == "выполнено")
async def handle_done(message: Message, state: FSMContext):
    today = datetime.now().date()
    chat_id = message.chat.id

    # Получаем user_id по chat_id
    async with db_pool.acquire() as conn:
        user_id = await conn.fetchval("SELECT id FROM users WHERE chat_id = $1", chat_id)
        if not user_id:
            await message.answer("Пользователь не найден.")
            return

        # Удаляем из pending, если был
        if user_id in pending_users:
            pending_users[user_id]["answered"] = True

        # Проверка: нет ли уже записи за сегодня
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM workout_log WHERE user_id = $1 AND date = $2",
            user_id, today
        )
        if count == 0:
            await conn.execute(
                "INSERT INTO workout_log (user_id, date, completed) VALUES ($1, $2, 1)",
                user_id, today
            )

        # Вычисляем стрик
        streak = await calculate_streak(user_id)
        visual = await get_streak_visual(user_id)

    # Ответ пользователю
    TARGET_STREAK = 7
    response = f"Отлично! Ты красавчик 💪 Прогресс засчитан!\nТекущий стрик: {streak} 🔥"
    if streak == TARGET_STREAK:
        response += f"\n\n🎉 ПОЗДРАВЛЯЕМ! Ты выполнил {TARGET_STREAK} тренировок подряд! 🚀"
    elif streak > TARGET_STREAK:
        response += f"\n💪 Ты уже превзошёл streak из {TARGET_STREAK} дней. Продолжай в том же духе!"

    await message.answer(response)
    await message.answer(f"Прогресс за последние дни:\n{visual}")

    # Переход к оценке
    await message.answer(
        "Как бы ты оценил тренировку по 5-балльной шкале?",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=str(i)) for i in range(1, 6)]],
            resize_keyboard=True,
            one_time_keyboard=True
        )
    )
    await state.set_state(UserState.waiting_feedback_rating)



@dp.message(F.text.lower() == "не выполнено")
async def handle_not_done_text(message: Message, state: FSMContext):
    today = datetime.now().date()
    chat_id = message.chat.id

    async with db_pool.acquire() as conn:
        user_id = await conn.fetchval("SELECT id FROM users WHERE chat_id = $1", chat_id)
        if not user_id:
            await message.answer("Пользователь не найден.")
            return

        # Удаляем из pending
        if user_id in pending_users:
            pending_users[user_id]["answered"] = True

        # Проверка: не записано ли уже
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM workout_log WHERE user_id = $1 AND date = $2",
            user_id, today
        )

        if count == 0:
            await conn.execute(
                "INSERT INTO workout_log (user_id, date, completed) VALUES ($1, $2, 0)",
                user_id, today
            )

        visual = await get_streak_visual(user_id)

    await message.answer("Жаль, что не удалось потренироваться сегодня 😔")
    await message.answer(f"Прогресс за последние дни:\n{visual}")
    await state.clear()




@dp.message(UserState.waiting_feedback_rating)
async def handle_feedback_rating(message: Message, state: FSMContext):
    if message.text not in ["1", "2", "3", "4", "5"]:
        await message.answer("Пожалуйста, выбери оценку от 1 до 5.")
        return

    await state.update_data(rating=int(message.text))
    await message.answer("Спасибо! Хочешь оставить комментарий? Просто напиши его, или напиши 'нет'.")
    await state.set_state(UserState.waiting_feedback_comment)


@dp.message(UserState.waiting_feedback_comment)
async def handle_feedback_comment(message: Message, state: FSMContext):
    data = await state.get_data()
    chat_id = message.chat.id

    async with db_pool.acquire() as conn:
        user_id = await conn.fetchval("SELECT id FROM users WHERE chat_id = $1", chat_id)
        if not user_id:
            await message.answer("Пользователь не найден.")
            return

        rating = data.get("rating")
        comment = message.text.strip()
        if comment.lower() == "нет":
            comment = None

        today = datetime.date

        await conn.execute(
            "INSERT INTO feedback (user_id, date, rating, comment) VALUES ($1, $2, $3, $4)",
            user_id, today, rating, comment
        )

    await message.answer("Спасибо за обратную связь!", reply_markup=types.ReplyKeyboardRemove())
    await state.clear()



@dp.message(Command("ask"))
async def handle_ask(message: Message):
    question = message.text.replace("/ask", "").strip()
    if not question:
        await message.answer("Пожалуйста, задай вопрос после команды. Пример:\n/ask Как правильно дышать?")
        return

    chat_id = message.chat.id
    today = datetime.date

    async with db_pool.acquire() as conn:
        user_row = await conn.fetchrow("SELECT id FROM users WHERE chat_id = $1", chat_id)
        if not user_row:
            await message.answer("Ты должен сначала зарегистрироваться.")
            return

        user_id = user_row["id"]

        # Сохраняем вопрос и получаем его id
        row = await conn.fetchrow(
            """
            INSERT INTO questions (user_id, chat_id, text, status)
            VALUES ($1, $2, $3, $4)
            RETURNING id
            """,
            user_id, chat_id, question, "open"
        )
        question_id = user_id

    # Уведомление администратору
    msg = (
        f"📩 *Новый вопрос от* [{message.from_user.full_name}](tg://user?id={chat_id}):\n\n"
        f"{question}"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✉ Ответить", callback_data=f"answer_{question_id}")]
    ])

    await message.bot.send_message(ADMIN_CHAT_ID, msg, reply_markup=keyboard, parse_mode="Markdown")
    await message.answer("Вопрос отправлен администратору. Он свяжется с тобой при необходимости.")


async def calculate_streak(user_id: int) -> int:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT date FROM workout_log
            WHERE user_id = $1 AND completed = 1
            ORDER BY date DESC
            """,
            user_id
        )
    if not rows:
        return 0

    streak = 0
    today = date.today()

    for i, (date_str,) in enumerate(rows):
        if isinstance(date_str, str):
            log_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        else:
            log_date = date_str
        expected_date = today - timedelta(days=streak)
        if log_date == expected_date:
            streak += 1
        else:
            break

    return streak


@dp.message(Command("help"))
async def show_help(message: Message):
    await message.answer(
        "Вот, чем я могу помочь:\n\n"
        "/start — регистрация\n"
        "/ask [твой вопрос] — задать вопрос администратору\n"
        "«Выполнено» — отметить завершение тренировки\n"
    )


@dp.callback_query(F.data.startswith("answer_"))
async def admin_start_answer(callback: CallbackQuery, state: FSMContext):
    question_id = int(callback.data.replace("answer_", ""))
    async with db_pool.acquire() as conn:
        question = await conn.fetch(
            """
            SELECT text FROM questions
            WHERE user_id = $1
            """,
            question_id
        )

    if not question:
        await callback.message.answer("Вопрос не найден или уже обработан.")
        return

    await state.update_data(question_id=question_id)
    await callback.message.answer("Введите ваш ответ для пользователя:")
    await state.set_state(Help.answering)
    await callback.answer()


@dp.message(Help.answering)
async def admin_send_answer(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_CHAT_ID:
        await message.answer("У вас нет прав для ответа на вопросы.")
        await state.clear()
        return

    data = await state.get_data()
    question_id = data.get("question_id")
    answer_text = message.text

    # Получаем chat_id пользователя
    async with db_pool.acquire() as conn:
        result = await conn.fetchrow(
            """
            SELECT chat_id FROM questions
            WHERE id = $1
            """,
            question_id
        )

    if not result:
        await message.answer("Ошибка: не удалось найти пользователя.")
        return

    user_chat_id = result['chat_id']
    # Отправляем пользователю
    try:
        await message.bot.send_message(user_chat_id, f"📬 Ответ от администратора:\n\n{answer_text}")

        async with db_pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE questions SET status = 'answered' WHERE id = $1
                """,
                question_id
            )
        await message.answer("Ответ отправлен ✅")
    except Exception as e:
        await message.answer(f"Не удалось отправить сообщение пользователю.\n{e}")

    await state.clear()


async def main():
    # create_db()
    await init_db()

    bot = Bot(TOKEN)
    await bot.delete_webhook(drop_pending_updates=True)
    # dp.include_routers(
    #    home.router
    # )
    asyncio.create_task(send_training_reminders(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())
