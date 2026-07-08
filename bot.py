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

BUTTON_AMOUNTS = {
    "+$5": 500,
    "+$10": 1000,
    "+$15": 1500,
    "+$20": 2000,
    "+$25": 2500,
}

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["+$5", "+$10", "+$15"],
        ["+$20", "+$25", "Другая сумма"],
        ["Мой чай", "Общий чай"],
        ["Отменить последний"],
    ],
    resize_keyboard=True,
    is_persistent=True,
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
            "CREATE INDEX IF NOT EXISTS idx_tips_user_id ON tips(user_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_tips_local_date ON tips(local_date)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_tips_local_month ON tips(local_month)"
        )


def is_allowed(user_id: int) -> bool:
    # Без списка участников бот закрыт для всех.
    return user_id in ALLOWED_USER_IDS


def money(cents: int) -> str:
    dollars = cents / 100
    if cents % 100 == 0:
        return f"${int(dollars)}"
    return f"${dollars:.2f}"


def current_local_time() -> datetime:
    return datetime.now(LOCAL_TZ)


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


def get_user_totals(user_id: int) -> tuple[int, int, int]:
    now = current_local_time()

    with get_connection() as connection:
        row = connection.execute(
            """
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
            FROM tips
            WHERE user_id = %s
            """,
            (now.date(), now.strftime("%Y-%m"), user_id),
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
            SELECT
                user_id,
                MAX(full_name) AS full_name,
                COALESCE(
                    SUM(CASE WHEN local_date = %s THEN amount_cents ELSE 0 END),
                    0
                ) AS today,
                COALESCE(SUM(amount_cents), 0) AS all_time
            FROM tips
            WHERE user_id = ANY(%s)
            GROUP BY user_id
            ORDER BY all_time DESC
            """,
            (now.date(), allowed_ids),
        ).fetchall()

    today_total = sum(int(row["today"]) for row in rows)
    all_time_total = sum(int(row["all_time"]) for row in rows)
    return today_total, all_time_total, rows


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
    if cents <= 0 or cents > 1_000_000:
        return None
    return cents


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

    await update.message.reply_text(
        f"Ваш Telegram ID: {user.id}",
        reply_markup=MAIN_KEYBOARD,
    )


async def add_preset_tip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_allowed(user.id):
        await deny_access(update)
        return

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
    if amount_cents is None:
        await update.message.reply_text(
            "Не понял сумму. Напишите только число, например: 15 или 12.50"
        )
        return CUSTOM_AMOUNT

    add_tip(user.id, user.full_name, amount_cents)
    today, month, _ = get_user_totals(user.id)

    await update.message.reply_text(
        f"✅ Добавлено: {money(amount_cents)}\n"
        f"Сегодня у вас: {money(today)}\n"
        f"За этот месяц: {money(month)}",
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if not is_allowed(user.id):
        await deny_access(update)
        return ConversationHandler.END

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

    today_total, all_time_total, rows = get_team_report()

    if not rows:
        text = "Пока чаевых нет."
    else:
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
        text = "\n".join(lines)

    await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)


async def undo_last(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_allowed(user.id):
        await deny_access(update)
        return

    removed = remove_last_tip(user.id)
    if removed is None:
        text = "У вас ещё нет записей, которые можно отменить."
    else:
        today, month, _ = get_user_totals(user.id)
        text = (
            f"↩️ Последняя запись {money(removed)} удалена.\n"
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

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("id", show_id))
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
