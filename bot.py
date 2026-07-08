import logging
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row
from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
TIMEZONE_NAME = os.getenv("TIMEZONE", "America/Los_Angeles").strip()
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "telegram").strip().strip("/") or "telegram"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

ALLOWED_USER_IDS = {
    int(value.strip())
    for value in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if value.strip().isdigit()
}

try:
    LOCAL_TZ = ZoneInfo(TIMEZONE_NAME)
except Exception:
    LOCAL_TZ = ZoneInfo("America/Los_Angeles")

CUSTOM_AMOUNT = 1
OTHER_SELECT_PERSON = 10
OTHER_SELECT_ACTION = 11
OTHER_ENTER_AMOUNT = 12

BUTTON_AMOUNTS = {
    "+$5": 500,
    "+$10": 1000,
    "+$15": 1500,
    "+$20": 2000,
    "+$25": 2500,
}

OTHER_AMOUNT_BUTTONS = {
    "$5": 500,
    "$10": 1000,
    "$15": 1500,
    "$20": 2000,
    "$25": 2500,
}

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["+$5", "+$10", "+$15"],
        ["+$20", "+$25", "Другая сумма"],
        ["Мой чай", "Общий чай"],
        ["Изменить"],
        ["Отменить последний"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

OTHER_ACTION_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["➕ Добавить", "➖ Отнять"],
        ["✏️ Изменить сегодня"],
        ["Отмена"],
    ],
    resize_keyboard=True,
)

OTHER_AMOUNT_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["$5", "$10", "$15"],
        ["$20", "$25", "Другая сумма"],
        ["Отмена"],
    ],
    resize_keyboard=True,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def get_connection() -> psycopg.Connection:
    return psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row,
        connect_timeout=15,
        autocommit=True,
    )


def initialize_database() -> None:
    with get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tips (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                full_name TEXT NOT NULL,
                amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
                created_at_utc TIMESTAMPTZ NOT NULL,
                local_date DATE NOT NULL,
                local_month TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS participants (
                user_id BIGINT PRIMARY KEY,
                full_name TEXT NOT NULL,
                username TEXT,
                last_seen_utc TIMESTAMPTZ NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tip_adjustments (
                id BIGSERIAL PRIMARY KEY,
                target_user_id BIGINT NOT NULL,
                target_full_name TEXT NOT NULL,
                amount_cents INTEGER NOT NULL CHECK (amount_cents <> 0),
                actor_user_id BIGINT NOT NULL,
                actor_full_name TEXT NOT NULL,
                action TEXT NOT NULL,
                created_at_utc TIMESTAMPTZ NOT NULL,
                local_date DATE NOT NULL,
                local_month TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_tips_user_id ON tips(user_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_tips_local_date ON tips(local_date)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_tips_local_month ON tips(local_month)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_adjustments_target ON tip_adjustments(target_user_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_adjustments_date ON tip_adjustments(local_date)"
        )


def is_allowed(user_id: int) -> bool:
    return user_id in ALLOWED_USER_IDS


def money(cents: int) -> str:
    dollars = cents / 100
    if cents % 100 == 0:
        return f"${int(dollars)}"
    return f"${dollars:.2f}"


def current_local_time() -> datetime:
    return datetime.now(LOCAL_TZ)


def remember_user(user) -> None:
    if not user or not is_allowed(user.id):
        return

    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO participants (user_id, full_name, username, last_seen_utc)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                full_name = EXCLUDED.full_name,
                username = EXCLUDED.username,
                last_seen_utc = EXCLUDED.last_seen_utc
            """,
            (
                user.id,
                user.full_name,
                user.username,
                datetime.now(timezone.utc),
            ),
        )


def add_tip(user_id: int, full_name: str, amount_cents: int) -> None:
    now_local = current_local_time()
    now_utc = datetime.now(timezone.utc)

    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO tips (
                user_id,
                full_name,
                amount_cents,
                created_at_utc,
                local_date,
                local_month
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                user_id,
                full_name,
                amount_cents,
                now_utc,
                now_local.date(),
                now_local.strftime("%Y-%m"),
            ),
        )


def add_adjustment(
    target_user_id: int,
    target_full_name: str,
    amount_cents: int,
    actor_user_id: int,
    actor_full_name: str,
    action: str,
) -> None:
    now_local = current_local_time()
    now_utc = datetime.now(timezone.utc)

    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO tip_adjustments (
                target_user_id,
                target_full_name,
                amount_cents,
                actor_user_id,
                actor_full_name,
                action,
                created_at_utc,
                local_date,
                local_month
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                target_user_id,
                target_full_name,
                amount_cents,
                actor_user_id,
                actor_full_name,
                action,
                now_utc,
                now_local.date(),
                now_local.strftime("%Y-%m"),
            ),
        )


def get_user_totals(user_id: int) -> tuple[int, int, int]:
    now = current_local_time()

    with get_connection() as connection:
        row = connection.execute(
            """
            WITH all_entries AS (
                SELECT user_id, amount_cents, local_date, local_month
                FROM tips
                WHERE user_id = %s

                UNION ALL

                SELECT target_user_id AS user_id, amount_cents, local_date, local_month
                FROM tip_adjustments
                WHERE target_user_id = %s
            )
            SELECT
                COALESCE(
                    SUM(CASE WHEN local_date = %s THEN amount_cents ELSE 0 END),
                    0
                ) AS today,
                COALESCE(
                    SUM(CASE WHEN local_month = %s THEN amount_cents ELSE 0 END),
                    0
                ) AS month,
                COALESCE(SUM(amount_cents), 0) AS all_time
            FROM all_entries
            """,
            (user_id, user_id, now.date(), now.strftime("%Y-%m")),
        ).fetchone()

    return int(row["today"]), int(row["month"]), int(row["all_time"])


def get_team_report() -> tuple[int, int, list[dict]]:
    now = current_local_time()
    allowed_ids = sorted(ALLOWED_USER_IDS)

    if not allowed_ids:
        return 0, 0, []

    with get_connection() as connection:
        rows = connection.execute(
            """
            WITH all_entries AS (
                SELECT user_id, full_name, amount_cents, local_date
                FROM tips
                WHERE user_id = ANY(%s)

                UNION ALL

                SELECT target_user_id AS user_id,
                       target_full_name AS full_name,
                       amount_cents,
                       local_date
                FROM tip_adjustments
                WHERE target_user_id = ANY(%s)
            ),
            names AS (
                SELECT user_id, full_name
                FROM participants
                WHERE user_id = ANY(%s)
            )
            SELECT
                ids.user_id,
                COALESCE(MAX(names.full_name), MAX(all_entries.full_name), 'Мастер') AS full_name,
                COALESCE(
                    SUM(CASE WHEN all_entries.local_date = %s THEN all_entries.amount_cents ELSE 0 END),
                    0
                ) AS today,
                COALESCE(SUM(all_entries.amount_cents), 0) AS all_time
            FROM UNNEST(%s::bigint[]) AS ids(user_id)
            LEFT JOIN all_entries ON all_entries.user_id = ids.user_id
            LEFT JOIN names ON names.user_id = ids.user_id
            GROUP BY ids.user_id
            ORDER BY all_time DESC, full_name
            """,
            (allowed_ids, allowed_ids, allowed_ids, now.date(), allowed_ids),
        ).fetchall()

    today_total = sum(int(row["today"]) for row in rows)
    all_time_total = sum(int(row["all_time"]) for row in rows)
    return today_total, all_time_total, rows


def get_other_participants(exclude_user_id: int) -> list[dict]:
    ids = sorted(user_id for user_id in ALLOWED_USER_IDS if user_id != exclude_user_id)
    if not ids:
        return []

    with get_connection() as connection:
        participant_rows = connection.execute(
            """
            SELECT user_id, full_name
            FROM participants
            WHERE user_id = ANY(%s)
            """,
            (ids,),
        ).fetchall()

        tip_rows = connection.execute(
            """
            SELECT user_id, MAX(full_name) AS full_name
            FROM tips
            WHERE user_id = ANY(%s)
            GROUP BY user_id
            """,
            (ids,),
        ).fetchall()

    names: dict[int, str] = {}
    for row in tip_rows:
        names[int(row["user_id"])] = row["full_name"]
    for row in participant_rows:
        names[int(row["user_id"])] = row["full_name"]

    return [
        {
            "user_id": user_id,
            "full_name": names.get(user_id, f"Мастер {user_id}"),
        }
        for user_id in ids
    ]


def remove_last_tip(user_id: int) -> int | None:
    with psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row,
        connect_timeout=15,
    ) as connection:
        with connection.transaction():
            row = connection.execute(
                """
                SELECT id, amount_cents
                FROM tips
                WHERE user_id = %s
                ORDER BY id DESC
                LIMIT 1
                FOR UPDATE
                """,
                (user_id,),
            ).fetchone()

            if row is None:
                return None

            connection.execute(
                "DELETE FROM tips WHERE id = %s",
                (row["id"],),
            )
            return int(row["amount_cents"])


def parse_amount(text: str) -> int | None:
    cleaned = text.strip().replace(",", ".")
    cleaned = re.sub(r"[$\s]", "", cleaned)

    try:
        amount = float(cleaned)
    except ValueError:
        return None

    cents = round(amount * 100)
    if cents < 0 or cents > 1_000_000:
        return None
    return cents


def clear_other_context(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in (
        "other_targets",
        "target_user_id",
        "target_full_name",
        "other_action",
    ):
        context.user_data.pop(key, None)


async def deny_access(update: Update) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            "🔒 Этот бот закрыт. У вас нет доступа."
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_allowed(user.id):
        await deny_access(update)
        return

    remember_user(user)
    await update.message.reply_text(
        f"Привет, {user.first_name}! 👋\n\n"
        "Нажмите сумму чаевых, которую получили.\n"
        "Каждое нажатие сразу добавляется в подсчёт.",
        reply_markup=MAIN_KEYBOARD,
    )


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_allowed(user.id):
        await deny_access(update)
        return

    remember_user(user)
    await update.message.reply_text(
        f"Ваш Telegram ID: {user.id}",
        reply_markup=MAIN_KEYBOARD,
    )


async def add_preset_tip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_allowed(user.id):
        await deny_access(update)
        return

    remember_user(user)
    amount_cents = BUTTON_AMOUNTS[update.message.text]
    add_tip(user.id, user.full_name, amount_cents)
    today, month, _ = get_user_totals(user.id)

    await update.message.reply_text(
        f"✅ Добавлено: {money(amount_cents)}\n"
        f"Сегодня у вас: {money(today)}\n"
        f"За этот месяц: {money(month)}",
        reply_markup=MAIN_KEYBOARD,
    )


async def ask_custom_amount(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    user = update.effective_user
    if not is_allowed(user.id):
        await deny_access(update)
        return ConversationHandler.END

    remember_user(user)
    await update.message.reply_text(
        "Введите сумму чаевых числом.\n"
        "Например: 15 или 12.50\n\n"
        "Для отмены напишите /cancel"
    )
    return CUSTOM_AMOUNT


async def receive_custom_amount(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    user = update.effective_user
    if not is_allowed(user.id):
        await deny_access(update)
        return ConversationHandler.END

    amount_cents = parse_amount(update.message.text)
    if amount_cents is None or amount_cents == 0:
        await update.message.reply_text(
            "Не понял сумму. Напишите число больше нуля, например: 15 или 12.50"
        )
        return CUSTOM_AMOUNT

    remember_user(user)
    add_tip(user.id, user.full_name, amount_cents)
    today, month, _ = get_user_totals(user.id)

    await update.message.reply_text(
        f"✅ Добавлено: {money(amount_cents)}\n"
        f"Сегодня у вас: {money(today)}\n"
        f"За этот месяц: {money(month)}",
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


async def begin_other_adjustment(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    user = update.effective_user
    if not is_allowed(user.id):
        await deny_access(update)
        return ConversationHandler.END

    remember_user(user)
    participants = get_other_participants(user.id)
    if not participants:
        await update.message.reply_text(
            "Другие участники пока не найдены. Проверьте ALLOWED_USER_IDS.",
            reply_markup=MAIN_KEYBOARD,
        )
        return ConversationHandler.END

    target_map: dict[str, dict] = {}
    rows: list[list[str]] = []
    for participant in participants:
        label = f"{participant['full_name']} [{participant['user_id']}]"
        target_map[label] = participant
        rows.append([label])
    rows.append(["Отмена"])

    context.user_data["other_targets"] = target_map
    await update.message.reply_text(
        "Выберите мастера, которому нужно изменить сегодняшний чай:",
        reply_markup=ReplyKeyboardMarkup(rows, resize_keyboard=True),
    )
    return OTHER_SELECT_PERSON


async def select_other_person(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    text = update.message.text
    if text == "Отмена":
        clear_other_context(context)
        await update.message.reply_text("Действие отменено.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END

    target = context.user_data.get("other_targets", {}).get(text)
    if not target:
        await update.message.reply_text("Выберите мастера кнопкой ниже.")
        return OTHER_SELECT_PERSON

    context.user_data["target_user_id"] = int(target["user_id"])
    context.user_data["target_full_name"] = target["full_name"]
    today, _, all_time = get_user_totals(int(target["user_id"]))

    await update.message.reply_text(
        f"Вы выбрали: {target['full_name']}\n"
        f"Сегодня: {money(today)}\n"
        f"За всё время: {money(all_time)}\n\n"
        "Что нужно сделать?",
        reply_markup=OTHER_ACTION_KEYBOARD,
    )
    return OTHER_SELECT_ACTION


async def select_other_action(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    text = update.message.text
    if text == "Отмена":
        clear_other_context(context)
        await update.message.reply_text("Действие отменено.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END

    actions = {
        "➕ Добавить": "add",
        "➖ Отнять": "subtract",
        "✏️ Изменить сегодня": "set_today",
    }
    action = actions.get(text)
    if not action:
        await update.message.reply_text("Выберите действие кнопкой ниже.")
        return OTHER_SELECT_ACTION

    context.user_data["other_action"] = action
    target_name = context.user_data["target_full_name"]

    if action == "add":
        prompt = f"Сколько добавить мастеру {target_name}?"
    elif action == "subtract":
        prompt = f"Сколько отнять у мастера {target_name} за сегодня?"
    else:
        prompt = (
            f"Какой итог должен быть у мастера {target_name} за сегодня?\n"
            "Например, если должно остаться $40 — отправьте 40."
        )

    await update.message.reply_text(prompt, reply_markup=OTHER_AMOUNT_KEYBOARD)
    return OTHER_ENTER_AMOUNT


async def receive_other_amount(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    text = update.message.text
    if text == "Отмена":
        clear_other_context(context)
        await update.message.reply_text("Действие отменено.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END

    if text == "Другая сумма":
        await update.message.reply_text(
            "Введите сумму числом, например: 12.50\n"
            "Для отмены нажмите «Отмена» или напишите /cancel."
        )
        return OTHER_ENTER_AMOUNT

    amount_cents = OTHER_AMOUNT_BUTTONS.get(text)
    if amount_cents is None:
        amount_cents = parse_amount(text)

    if amount_cents is None:
        await update.message.reply_text(
            "Не понял сумму. Нажмите кнопку или отправьте число, например 15."
        )
        return OTHER_ENTER_AMOUNT

    target_user_id = int(context.user_data["target_user_id"])
    target_full_name = context.user_data["target_full_name"]
    action = context.user_data["other_action"]
    actor = update.effective_user

    today_before, _, _ = get_user_totals(target_user_id)

    if action == "add":
        if amount_cents == 0:
            await update.message.reply_text("Сумма должна быть больше нуля.")
            return OTHER_ENTER_AMOUNT
        delta = amount_cents
        action_label = "Добавлено другому мастеру"
    elif action == "subtract":
        if amount_cents == 0:
            await update.message.reply_text("Сумма должна быть больше нуля.")
            return OTHER_ENTER_AMOUNT
        if amount_cents > today_before:
            await update.message.reply_text(
                f"Нельзя отнять {money(amount_cents)}: сегодня у мастера только "
                f"{money(today_before)}. Введите меньшую сумму."
            )
            return OTHER_ENTER_AMOUNT
        delta = -amount_cents
        action_label = "Отнято у другого мастера"
    else:
        delta = amount_cents - today_before
        if delta == 0:
            clear_other_context(context)
            await update.message.reply_text(
                f"У {target_full_name} уже стоит {money(amount_cents)} за сегодня.",
                reply_markup=MAIN_KEYBOARD,
            )
            return ConversationHandler.END
        action_label = "Изменён сегодняшний итог другого мастера"

    remember_user(actor)
    add_adjustment(
        target_user_id=target_user_id,
        target_full_name=target_full_name,
        amount_cents=delta,
        actor_user_id=actor.id,
        actor_full_name=actor.full_name,
        action=action_label,
    )

    today_after, month_after, all_time_after = get_user_totals(target_user_id)

    if action == "add":
        result_line = f"Добавлено: {money(amount_cents)}"
    elif action == "subtract":
        result_line = f"Отнято: {money(amount_cents)}"
    else:
        result_line = f"Новый итог за сегодня: {money(amount_cents)}"

    clear_other_context(context)
    await update.message.reply_text(
        f"✅ {target_full_name}\n"
        f"{result_line}\n"
        f"Сегодня: {money(today_after)}\n"
        f"Этот месяц: {money(month_after)}\n"
        f"За всё время: {money(all_time_after)}",
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if not is_allowed(user.id):
        await deny_access(update)
        return ConversationHandler.END

    clear_other_context(context)
    await update.message.reply_text(
        "Действие отменено.",
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


async def my_tips(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_allowed(user.id):
        await deny_access(update)
        return

    remember_user(user)
    today, month, all_time = get_user_totals(user.id)
    await update.message.reply_text(
        "👤 Ваши чаевые:\n\n"
        f"Сегодня: {money(today)}\n"
        f"Этот месяц: {money(month)}\n"
        f"За всё время: {money(all_time)}",
        reply_markup=MAIN_KEYBOARD,
    )


async def team_tips(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_allowed(user.id):
        await deny_access(update)
        return

    remember_user(user)
    today_total, all_time_total, rows = get_team_report()

    lines = ["👥 Общие чаевые:", ""]
    for row in rows:
        lines.append(
            f"• {row['full_name']}: сегодня {money(int(row['today']))}, "
            f"всего {money(int(row['all_time']))}"
        )
    lines.extend(
        [
            "",
            f"Итого сегодня: {money(today_total)}",
            f"Итого за всё время: {money(all_time_total)}",
        ]
    )

    await update.message.reply_text("\n".join(lines), reply_markup=MAIN_KEYBOARD)


async def undo_last(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_allowed(user.id):
        await deny_access(update)
        return

    remember_user(user)
    removed = remove_last_tip(user.id)
    if removed is None:
        text = "У вас ещё нет личных записей, которые можно отменить."
    else:
        today, month, _ = get_user_totals(user.id)
        text = (
            f"↩️ Последняя личная запись {money(removed)} удалена.\n"
            f"Сегодня у вас: {money(today)}\n"
            f"За этот месяц: {money(month)}"
        )

    await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)


async def unknown_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user = update.effective_user
    if not is_allowed(user.id):
        await deny_access(update)
        return

    remember_user(user)
    await update.message.reply_text(
        "Выберите действие с помощью кнопок ниже.",
        reply_markup=MAIN_KEYBOARD,
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Ошибка при обработке обновления:", exc_info=context.error)

    effective_message = getattr(update, "effective_message", None)
    if effective_message:
        try:
            await effective_message.reply_text(
                "Произошла временная ошибка. Попробуйте нажать кнопку ещё раз."
            )
        except Exception:
            logger.exception("Не удалось отправить сообщение об ошибке")


def build_application() -> Application:
    application = Application.builder().token(BOT_TOKEN).build()

    custom_amount_handler = ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.Regex(r"^Другая сумма$"),
                ask_custom_amount,
            )
        ],
        states={
            CUSTOM_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_custom_amount)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    other_adjustment_handler = ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.Regex(r"^Изменить$"),
                begin_other_adjustment,
            )
        ],
        states={
            OTHER_SELECT_PERSON: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, select_other_person)
            ],
            OTHER_SELECT_ACTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, select_other_action)
            ],
            OTHER_ENTER_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_other_amount)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("id", show_id))
    application.add_handler(other_adjustment_handler)
    application.add_handler(custom_amount_handler)
    application.add_handler(
        MessageHandler(
            filters.Regex(r"^(\+\$5|\+\$10|\+\$15|\+\$20|\+\$25)$"),
            add_preset_tip,
        )
    )
    application.add_handler(
        MessageHandler(filters.Regex(r"^Мой чай$"), my_tips)
    )
    application.add_handler(
        MessageHandler(filters.Regex(r"^Общий чай$"), team_tips)
    )
    application.add_handler(
        MessageHandler(filters.Regex(r"^Отменить последний$"), undo_last)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_message)
    )
    application.add_error_handler(error_handler)

    return application


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не найден.")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL не найден.")

    if not ALLOWED_USER_IDS:
        logger.warning(
            "ALLOWED_USER_IDS пустой: бот закрыт для всех пользователей."
        )

    initialize_database()
    application = build_application()

    if RENDER_EXTERNAL_URL:
        webhook_url = f"{RENDER_EXTERNAL_URL}/{WEBHOOK_PATH}"
        logger.info("Запуск webhook: %s", webhook_url)

        kwargs = {
            "listen": "0.0.0.0",
            "port": PORT,
            "url_path": WEBHOOK_PATH,
            "webhook_url": webhook_url,
            "drop_pending_updates": True,
        }
        if WEBHOOK_SECRET:
            kwargs["secret_token"] = WEBHOOK_SECRET

        application.run_webhook(**kwargs)
    else:
        logger.info("RENDER_EXTERNAL_URL отсутствует — запуск локально через polling")
        application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
