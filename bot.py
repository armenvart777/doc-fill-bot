"""
Telegram bot for automatic document filling.
Users can upload DOCX templates with {{MARKER}} placeholders,
configure input fields, and fill documents by sending data in one message.
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import uuid
from pathlib import Path

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

from document import extract_placeholders, fill_document
from utils import format_today, get_initials, num_to_words

# ── Config ───────────────────────────────────────────────────────────────────
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
FILLED_DIR = BASE_DIR / "filled"
DB_PATH = BASE_DIR / "bot.db"

TEMPLATES_DIR.mkdir(exist_ok=True)
FILLED_DIR.mkdir(exist_ok=True)

AUTO_FIELDS = {
    "{{СЕГОДНЯ}}": {"type": "today_date"},
    "{{ФАМИЛИЯ_ИО}}": {"type": "initials", "source": "{{ФИО}}"},
    "{{СУММА_ПРОПИСЬЮ}}": {"type": "num_to_words", "source": "{{СУММА}}"},
    "{{ЗАДАТОК_ПРОПИСЬЮ}}": {"type": "num_to_words", "source": "{{ЗАДАТОК}}"},
}

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Bot setup ────────────────────────────────────────────────────────────────
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)


# ── FSM States ───────────────────────────────────────────────────────────────
class States(StatesGroup):
    waiting_template = State()
    waiting_template_name = State()
    waiting_fields = State()
    waiting_data = State()
    waiting_data_all = State()
    waiting_user_id = State()
    waiting_filename_format = State()
    waiting_rename = State()


# ── Database ─────────────────────────────────────────────────────────────────
class Database:
    def __init__(self, path):
        self.path = str(path)
        self.db = None

    async def init(self):
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute(
            """CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                is_approved INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            )"""
        )
        await self.db.execute(
            """CREATE TABLE IF NOT EXISTS templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                fields_config TEXT DEFAULT '[]',
                filename_format TEXT DEFAULT '',
                created_by INTEGER,
                created_at TEXT DEFAULT (datetime('now'))
            )"""
        )
        await self.db.commit()
        # Migration: add filename_format if missing
        try:
            await self.db.execute("SELECT filename_format FROM templates LIMIT 1")
        except Exception:
            await self.db.execute(
                "ALTER TABLE templates ADD COLUMN filename_format TEXT DEFAULT ''"
            )
            await self.db.commit()

    async def get_or_create_user(self, user_id, username=None, full_name=None):
        await self.db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, full_name) VALUES (?, ?, ?)",
            (user_id, username, full_name),
        )
        await self.db.commit()

    async def is_approved(self, user_id):
        if user_id in ADMIN_IDS:
            return True
        cursor = await self.db.execute(
            "SELECT is_approved FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return row is not None and row["is_approved"] == 1

    async def approve_user(self, user_id):
        await self.db.execute(
            "UPDATE users SET is_approved = 1 WHERE user_id = ?", (user_id,)
        )
        await self.db.commit()

    async def disapprove_user(self, user_id):
        await self.db.execute(
            "UPDATE users SET is_approved = 0 WHERE user_id = ?", (user_id,)
        )
        await self.db.commit()

    async def get_approved_users(self):
        cursor = await self.db.execute(
            "SELECT user_id, username, full_name FROM users WHERE is_approved = 1"
        )
        return await cursor.fetchall()

    async def save_template(self, name, file_path, fields_config, created_by,
                            filename_format=""):
        await self.db.execute(
            "INSERT INTO templates (name, file_path, fields_config, filename_format, "
            "created_by) VALUES (?, ?, ?, ?, ?)",
            (name, file_path, json.dumps(fields_config, ensure_ascii=False),
             filename_format, created_by),
        )
        await self.db.commit()

    async def get_templates(self, created_by=None):
        if created_by is not None:
            cursor = await self.db.execute(
                "SELECT id, name, file_path, fields_config, filename_format, "
                "created_by FROM templates WHERE created_by = ? ORDER BY id DESC",
                (created_by,),
            )
        else:
            cursor = await self.db.execute(
                "SELECT id, name, file_path, fields_config, filename_format, "
                "created_by FROM templates ORDER BY id DESC"
            )
        return await cursor.fetchall()

    async def get_template(self, template_id):
        cursor = await self.db.execute(
            "SELECT id, name, file_path, fields_config, filename_format, "
            "created_by FROM templates WHERE id = ?",
            (template_id,),
        )
        return await cursor.fetchone()

    async def update_fields(self, template_id, fields_config):
        await self.db.execute(
            "UPDATE templates SET fields_config = ? WHERE id = ?",
            (json.dumps(fields_config, ensure_ascii=False), template_id),
        )
        await self.db.commit()

    async def update_name(self, template_id, name):
        await self.db.execute(
            "UPDATE templates SET name = ? WHERE id = ?",
            (name, template_id),
        )
        await self.db.commit()

    async def update_filename_format(self, template_id, filename_format):
        await self.db.execute(
            "UPDATE templates SET filename_format = ? WHERE id = ?",
            (filename_format, template_id),
        )
        await self.db.commit()

    async def delete_template(self, template_id):
        cursor = await self.db.execute(
            "SELECT file_path FROM templates WHERE id = ?", (template_id,)
        )
        row = await cursor.fetchone()
        if row and os.path.exists(row["file_path"]):
            os.remove(row["file_path"])
        await self.db.execute("DELETE FROM templates WHERE id = ?", (template_id,))
        await self.db.commit()


db = Database(DB_PATH)


# ── Helpers ──────────────────────────────────────────────────────────────────
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def strip_numbering(text: str) -> str:
    """Strip leading numbering: '1. Text' → 'Text', '2) Text' → 'Text'.
    Requires space after separator to avoid stripping times (16:00) or dates (18.09.2026)."""
    return re.sub(r'^\d{1,3}[\.\)\-]\s+', '', text.strip())


def main_keyboard(user_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Заполнить документ", callback_data="fill_start")
    kb.button(text="📄 Мои шаблоны", callback_data="my_templates")
    if is_admin(user_id):
        kb.button(text="👥 Управление доступом", callback_data="manage_users")
    kb.adjust(1)
    return kb.as_markup()


def parse_fill_data(fields: list[dict], lines: list[str]) -> dict[str, str] | str:
    """Parse user input lines into field replacements.

    Returns a dict of {marker: value} or an error string.
    Multiline fields consume all remaining lines.
    """
    has_multiline = any(f.get("multiline") for f in fields)
    regular_count = sum(1 for f in fields if not f.get("multiline"))
    min_lines = regular_count + (1 if has_multiline else 0)

    if len(lines) < min_lines:
        return (
            f"❌ Недостаточно данных.\n"
            f"Ожидается минимум {min_lines} строк, получено {len(lines)}.\n\n"
        )

    if not has_multiline and len(lines) < len(fields):
        return (
            f"❌ Недостаточно данных.\n"
            f"Ожидается {len(fields)} полей, получено {len(lines)}.\n\n"
        )

    replacements = {}
    line_idx = 0
    for field in fields:
        if field.get("multiline"):
            # Consume all remaining lines
            remaining = [strip_numbering(l) for l in lines[line_idx:]]
            replacements[field["marker"]] = "\n".join(remaining)
            line_idx = len(lines)
        else:
            replacements[field["marker"]] = strip_numbering(lines[line_idx])
            line_idx += 1

    return replacements


def format_fields_list(fields: list[dict]) -> str:
    """Format fields list for display, marking multiline fields."""
    parts = []
    for i, f in enumerate(fields):
        suffix = " (многострочное)" if f.get("multiline") else ""
        parts.append(f"  {i+1}. {f['label']}{suffix}")
    text = "\n".join(parts)
    has_ml = any(f.get("multiline") for f in fields)
    if has_ml:
        ml_names = [f["label"] for f in fields if f.get("multiline")]
        text += (
            f"\n\n⚠️ Поле «{ml_names[0]}» — многострочное. "
            "Вставьте все строки после остальных данных."
        )
    return text


def compute_auto_fields(user_replacements: dict[str, str]) -> dict[str, str]:
    auto = {}
    for marker, config in AUTO_FIELDS.items():
        field_type = config["type"]

        if field_type == "today_date":
            auto[marker] = format_today()

        elif field_type == "initials":
            source_value = user_replacements.get(config["source"], "")
            if source_value:
                auto[marker] = get_initials(source_value)

        elif field_type == "num_to_words":
            source_value = user_replacements.get(config["source"], "")
            if source_value:
                match = re.search(r"\d[\d\s]*", source_value.replace(" ", ""))
                if match:
                    try:
                        num = int(match.group().replace(" ", ""))
                        auto[marker] = num_to_words(num)
                    except ValueError:
                        pass
    return auto


def build_filename(template, user_replacements, auto_replacements):
    """Build output filename from template's filename_format or default."""
    filename_format = template["filename_format"] or ""

    if not filename_format:
        # Default: "Шаблон для Фамилия И.О. дата"
        template_name = template["name"]
        initials = auto_replacements.get("{{ФАМИЛИЯ_ИО}}", "")
        event_date = user_replacements.get("{{ДАТА}}", "")
        name_parts = [template_name]
        if initials:
            name_parts.append(f"для {initials}")
        if event_date:
            name_parts.append(event_date)
        display_name = " ".join(name_parts)
    else:
        # Custom format: replace {{МАРКЕР}} or {МАРКЕР} with values
        display_name = filename_format
        all_vals = {**auto_replacements, **user_replacements}
        # Replace {{ШАБЛОН}} and {ШАБЛОН}
        display_name = display_name.replace("{{ШАБЛОН}}", template["name"])
        display_name = display_name.replace("{ШАБЛОН}", template["name"])
        for marker, value in all_vals.items():
            # Support both {{МАРКЕР}} and {МАРКЕР}
            display_name = display_name.replace(marker, value)
            short_marker = marker.replace("{{", "{").replace("}}", "}")
            display_name = display_name.replace(short_marker, value)

    # Clean up for filename
    safe_name = re.sub(r'[<>:"/\\|?*]', "", display_name)
    # Preserve dots in dates (18.09.2026), remove other dots (Е.Ю. → ЕЮ)
    safe_name = re.sub(r'(\d)\.(\d)', r'\1⬤\2', safe_name)
    safe_name = safe_name.replace(".", "")
    safe_name = safe_name.replace("⬤", ".")
    # Remove any unreplaced markers like {МАРКЕР} or {{МАРКЕР}}
    safe_name = re.sub(r'\{+[^}]+\}+', '', safe_name)
    # Collapse multiple spaces, trim
    safe_name = re.sub(r'\s+', ' ', safe_name).strip()[:120]
    return safe_name


def convert_to_pdf(docx_path: str) -> str | None:
    """Convert DOCX to PDF using LibreOffice. Returns PDF path or None."""
    output_dir = str(Path(docx_path).parent)
    for cmd in ["libreoffice", "soffice",
                "/Applications/LibreOffice.app/Contents/MacOS/soffice"]:
        try:
            result = subprocess.run(
                [cmd, "--headless", "--convert-to", "pdf",
                 "--outdir", output_dir, docx_path],
                capture_output=True, timeout=30,
            )
            if result.returncode == 0:
                pdf_path = docx_path.rsplit(".", 1)[0] + ".pdf"
                if os.path.exists(pdf_path):
                    return pdf_path
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None


async def fill_and_send(message: Message, template, user_replacements,
                        auto_replacements, output_format="word"):
    """Fill a single template and send the result."""
    all_replacements = {**auto_replacements, **user_replacements}

    output_filename = f"{uuid.uuid4().hex}.docx"
    output_path = str(FILLED_DIR / output_filename)

    try:
        fill_document(template["file_path"], output_path, all_replacements)
    except Exception as e:
        logger.error(f"Error filling document: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка при заполнении «{template['name']}»: {e}")
        return

    safe_name = build_filename(template, user_replacements, auto_replacements)

    if output_format == "pdf":
        pdf_path = convert_to_pdf(output_path)
        if pdf_path:
            doc_file = FSInputFile(pdf_path, filename=f"{safe_name}.pdf")
            await message.answer_document(
                doc_file, caption=f"✅ {template['name']}"
            )
            try:
                os.remove(pdf_path)
            except OSError:
                pass
        else:
            doc_file = FSInputFile(output_path, filename=f"{safe_name}.docx")
            await message.answer_document(
                doc_file,
                caption=f"✅ {template['name']} (Word — PDF недоступен, "
                        "нужен LibreOffice на сервере)",
            )
    else:
        doc_file = FSInputFile(output_path, filename=f"{safe_name}.docx")
        await message.answer_document(
            doc_file, caption=f"✅ {template['name']}"
        )

    try:
        os.remove(output_path)
    except OSError:
        pass


# ── Handlers: Common ─────────────────────────────────────────────────────────
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user = message.from_user
    await db.get_or_create_user(user.id, user.username, user.full_name)

    if is_admin(user.id) or await db.is_approved(user.id):
        await message.answer(
            "👋 <b>Добро пожаловать!</b>\n\n"
            "Этот бот автоматически заполняет документы.\n"
            "Вы загружаете шаблон — бот подставляет ваши данные.\n\n"
            "Нажмите /help для подробной инструкции.\n\n"
            "Выберите действие:",
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard(user.id),
        )
    else:
        await message.answer(
            "⛔ У вас нет доступа к этому боту.\n"
            "Обратитесь к администратору для получения доступа.\n\n"
            f"Ваш ID: <code>{user.id}</code>",
            parse_mode=ParseMode.HTML,
        )


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "Выберите действие:", reply_markup=main_keyboard(callback.from_user.id)
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Действие отменено.", reply_markup=main_keyboard(message.from_user.id)
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    # Split into two messages due to Telegram 4096 char limit
    await message.answer(
        "📖 <b>Инструкция — как пользоваться ботом</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>Шаг 1: Подготовьте шаблон с маркерами</b>\n"
        "(места, которые будут заполняться)\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Откройте ваш документ (формата Word / DOCX) и замените данные "
        "на маркеры в двойных фигурных скобках. Именно эти маркеры "
        "впоследствии будут автоматически заполнены нужными значениями.\n\n"
        "Где было: <i>Иванов Иван Иванович</i>\n"
        "Напишите: <code>{{ФИО}}</code>\n\n"
        "Где было: <i>01.01.2026</i>\n"
        "Напишите: <code>{{ДАТА}}</code>\n\n"
        "Названия маркеров — любые, ЗАГЛАВНЫМИ буквами.\n\n"
        "Также существуют <b>автоматические маркеры</b>, которые можно "
        "вставить в договор:\n\n"
        "• <code>{{СЕГОДНЯ}}</code> — генерирует дату создания договора\n"
        "• <code>{{ФАМИЛИЯ_ИО}}</code> — из <code>{{ФИО}}</code> генерирует "
        "фамилию и инициалы. Например: Иванов Иван Иванович → Иванов И.И. "
        "(можно применить в поле для подписи)\n"
        "• <code>{{СУММА_ПРОПИСЬЮ}}</code> — берёт данные из "
        "<code>{{СУММА}}</code> и генерирует прописью "
        "(одна тысяча пятьдесят)\n"
        "• <code>{{ЗАДАТОК_ПРОПИСЬЮ}}</code> — аналогично\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>Шаг 2: Загрузите шаблон в бот</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "📄 Мои шаблоны → 📤 Загрузить шаблон\n"
        "Отправьте документ с маркерами.\n\n"
        "После загрузки бот попросит придумать простое название — "
        "оно будет отображаться в папке «Мои шаблоны».\n\n"
        "Документов может быть несколько.",
        parse_mode=ParseMode.HTML,
    )
    await message.answer(
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>Шаг 3: Настройте поля</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "После загрузки бот сам найдёт все маркеры и покажет их список — "
        "просто чтобы вы убедились, что всё правильно.\n\n"
        "Дальше можно зайти в «⚙️ Настроить поля» и изменить порядок "
        "маркеров. Это нужно, чтобы подготовить удобный шаблон сообщения "
        "для клиента — в той последовательности, в которой вы хотите "
        "получать от него данные.\n\n"
        "Например, если вы планируете написать клиенту так:\n\n"
        "<i>👋 Здравствуйте, для договора нужны данные:\n"
        "1. ФИО\n2. Серия и номер паспорта\n3. Кем выдан\n"
        "4. Телефон\n5. Адрес мероприятия\n6. Почта</i>\n\n"
        "...тогда в настройках полей маркеры должны идти в том же порядке:\n"
        "<code>ФИО\nПАСПОРТ\nКЕМ_ВЫДАН\nТЕЛЕФОН\nАДРЕС\nПОЧТА</code>\n\n"
        "Бот понимает именно последовательность строк!\n\n"
        "💡 <b>Совет:</b> Клиент укажет нужные данные, но если что-то "
        "хотите прописать сами (например, сумму или задаток) — просто "
        "добавьте это в поля, а при заполнении внесите эти строки "
        "самостоятельно.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>Шаг 4: Теперь бот работает на вас!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Нажмите «📝 Заполнить документ» → отправьте данные "
        "<b>одним сообщением</b>, каждое значение на новой строке:\n"
        "<code>Иванов Иван Иванович\n01.01.2026\n"
        "ул. Ленина 15\n+375291234567</code>\n\n"
        "Можно с нумерацией (бот сам её уберёт):\n"
        "<code>1. Иванов Иван Иванович\n2. 01.01.2026</code>\n\n"
        "<b>Команды:</b>\n"
        "/start — главное меню\n"
        "/fill — заполнить документ\n"
        "/cancel — отменить действие",
        parse_mode=ParseMode.HTML,
    )


# ── My Templates ─────────────────────────────────────────────────────────────
async def _show_my_templates(target, user_id):
    """Render the 'My Templates' screen."""
    templates = await db.get_templates(created_by=user_id)

    kb = InlineKeyboardBuilder()
    kb.button(text="📤 Загрузить шаблон", callback_data="upload_template")
    for t in templates:
        fields = json.loads(t["fields_config"])
        kb.button(
            text=f"📄 {t['name']} ({len(fields)} полей)",
            callback_data=f"tpl_info:{t['id']}",
        )
    kb.button(text="◀️ Назад", callback_data="main_menu")
    kb.adjust(1)

    text = "📄 Мои шаблоны:"
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=kb.as_markup())
    else:
        await target.answer(text, reply_markup=kb.as_markup())


@router.callback_query(F.data == "my_templates")
async def cb_my_templates(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if not is_admin(user_id) and not await db.is_approved(user_id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await _show_my_templates(callback, user_id)


# ── Upload Template ──────────────────────────────────────────────────────────
@router.callback_query(F.data == "upload_template")
async def cb_upload_template(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if not is_admin(user_id) and not await db.is_approved(user_id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(States.waiting_template)
    await callback.message.edit_text(
        "📤 <b>Загрузка шаблона — Шаг 1 из 2</b>\n\n"
        "Отправьте DOCX-файл (Word) с маркерами.\n\n"
        "<b>Что такое маркеры?</b>\n"
        "В вашем документе замените реальные данные "
        "на названия в двойных скобках:\n\n"
        "Пример в документе:\n"
        "<i>...именуемый(ая)</i> <code>{{ФИО}}</code> <i>паспорт</i> "
        "<code>{{ПАСПОРТ}}</code> <i>дата</i> <code>{{ДАТА}}</code>\n\n"
        "Бот найдёт все маркеры и подставит данные.\n\n"
        "/cancel — отмена",
        parse_mode=ParseMode.HTML,
    )


@router.message(States.waiting_template, F.document)
async def handle_template_upload(message: Message, state: FSMContext):
    doc = message.document
    if not doc.file_name.lower().endswith(".docx"):
        await message.answer("❌ Отправьте файл в формате .docx")
        return

    file = await bot.get_file(doc.file_id)
    filename = f"{uuid.uuid4().hex}_{doc.file_name}"
    filepath = TEMPLATES_DIR / filename
    await bot.download_file(file.file_path, str(filepath))

    try:
        placeholders = extract_placeholders(str(filepath))
    except Exception as e:
        await message.answer(f"❌ Ошибка при чтении файла: {e}")
        os.remove(filepath)
        return

    auto_markers = set(AUTO_FIELDS.keys())
    input_placeholders = [p for p in placeholders if p not in auto_markers]
    auto_found = [p for p in placeholders if p in auto_markers]

    fields = [{"label": p.strip("{}"), "marker": p} for p in input_placeholders]

    await state.set_state(States.waiting_template_name)
    await state.update_data(filepath=str(filepath), fields=fields)

    ph_text = "\n".join(f"  • {p}" for p in input_placeholders) or "  (нет)"
    auto_text = "\n".join(f"  • {p} (авто)" for p in auto_found) or "  (нет)"

    await message.answer(
        f"✅ Файл загружен! Найдено {len(input_placeholders)} полей для ввода.\n\n"
        f"<b>Поля для ввода:</b>\n{ph_text}\n\n"
        f"<b>Авто-поля (заполняются сами):</b>\n{auto_text}\n\n"
        "📤 <b>Загрузка шаблона — Шаг 2 из 2</b>\n\n"
        "Введите <b>короткое название</b> для шаблона.\n"
        "Это просто название для списка, например:\n"
        "<code>Договор с подписью</code>\n\n"
        "/cancel — отмена",
        parse_mode=ParseMode.HTML,
    )


@router.message(States.waiting_template_name)
async def handle_template_name(message: Message, state: FSMContext):
    if message.text == "/cancel":
        data = await state.get_data()
        filepath = data.get("filepath")
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
        await state.clear()
        await message.answer("Отменено.", reply_markup=main_keyboard(message.from_user.id))
        return

    data = await state.get_data()
    name = message.text.strip()
    filepath = data["filepath"]
    fields = data["fields"]

    await db.save_template(name, filepath, fields, message.from_user.id)
    await state.clear()

    fields_text = "\n".join(
        f"  {i+1}. {f['label']} → <code>{f['marker']}</code>"
        for i, f in enumerate(fields)
    )

    await message.answer(
        f"✅ Шаблон «{name}» сохранён!\n\n"
        f"<b>Настроенные поля:</b>\n{fields_text}\n\n"
        "Можете изменить через «📄 Мои шаблоны».",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(message.from_user.id),
    )


# ── Template Info & Management ───────────────────────────────────────────────
@router.callback_query(F.data.startswith("tpl_info:"))
async def cb_template_info(callback: CallbackQuery):
    template_id = int(callback.data.split(":")[1])
    t = await db.get_template(template_id)
    if not t:
        await callback.answer("Шаблон не найден", show_alert=True)
        return
    if t["created_by"] != callback.from_user.id and not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    fields = json.loads(t["fields_config"])
    fields_text = "\n".join(
        f"  {i+1}. {f['label']} → <code>{f['marker']}</code>"
        + (" (многострочное)" if f.get("multiline") else "")
        for i, f in enumerate(fields)
    )

    fmt = t["filename_format"] or "(по умолчанию)"

    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Переименовать", callback_data=f"rename_tpl:{template_id}")
    kb.button(text="⚙️ Настроить поля", callback_data=f"cfg_fields:{template_id}")
    kb.button(text="📝 Формат имени файла", callback_data=f"cfg_fname:{template_id}")
    kb.button(text="🗑 Удалить", callback_data=f"del_tpl:{template_id}")
    kb.button(text="◀️ Назад", callback_data="my_templates")
    kb.adjust(1)

    await callback.message.edit_text(
        f"📄 <b>{t['name']}</b>\n\n"
        f"<b>Поля:</b>\n{fields_text or '  Не настроены'}\n\n"
        f"<b>Формат имени файла:</b> {fmt}",
        parse_mode=ParseMode.HTML,
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("rename_tpl:"))
async def cb_rename_template(callback: CallbackQuery, state: FSMContext):
    template_id = int(callback.data.split(":")[1])
    t = await db.get_template(template_id)
    if not t:
        await callback.answer("Шаблон не найден", show_alert=True)
        return
    if t["created_by"] != callback.from_user.id and not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    await state.set_state(States.waiting_rename)
    await state.update_data(template_id=template_id)
    await callback.message.edit_text(
        f"✏️ Текущее название: <b>{t['name']}</b>\n\n"
        "Введите новое название:\n\n"
        "/cancel — отмена",
        parse_mode=ParseMode.HTML,
    )


@router.message(States.waiting_rename)
async def handle_rename(message: Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer(
            "Отменено.", reply_markup=main_keyboard(message.from_user.id)
        )
        return

    data = await state.get_data()
    template_id = data["template_id"]
    new_name = message.text.strip()

    await db.update_name(template_id, new_name)
    await state.clear()
    await message.answer(
        f"✅ Шаблон переименован в «{new_name}»",
        reply_markup=main_keyboard(message.from_user.id),
    )


@router.callback_query(F.data.startswith("del_tpl:"))
async def cb_delete_template(callback: CallbackQuery):
    template_id = int(callback.data.split(":")[1])
    t = await db.get_template(template_id)
    if not t:
        await callback.answer("Шаблон не найден", show_alert=True)
        return
    if t["created_by"] != callback.from_user.id and not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    await db.delete_template(template_id)
    await callback.answer("✅ Шаблон удалён")

    # Refresh list
    templates = await db.get_templates(created_by=callback.from_user.id)
    kb = InlineKeyboardBuilder()
    kb.button(text="📤 Загрузить шаблон", callback_data="upload_template")
    for t in templates:
        fields = json.loads(t["fields_config"])
        kb.button(
            text=f"📄 {t['name']} ({len(fields)} полей)",
            callback_data=f"tpl_info:{t['id']}",
        )
    kb.button(text="◀️ Назад", callback_data="main_menu")
    kb.adjust(1)
    await callback.message.edit_text("📄 Мои шаблоны:", reply_markup=kb.as_markup())


# ── Configure Fields ─────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("cfg_fields:"))
async def cb_config_fields(callback: CallbackQuery, state: FSMContext):
    template_id = int(callback.data.split(":")[1])
    t = await db.get_template(template_id)
    if not t:
        await callback.answer("Шаблон не найден", show_alert=True)
        return
    if t["created_by"] != callback.from_user.id and not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    await state.set_state(States.waiting_fields)
    await state.update_data(template_id=template_id)

    current_fields = json.loads(t["fields_config"])
    current_text = ""
    if current_fields:
        current_text = "\n\n<b>Текущая настройка:</b>\n" + "\n".join(
            f"  {i+1}. {f['label']} → <code>{f['marker']}</code>"
            for i, f in enumerate(current_fields)
        )

    await callback.message.edit_text(
        f"⚙️ <b>Настройка полей</b> для «{t['name']}»{current_text}\n\n"
        "Отправьте названия полей (каждое на новой строке).\n"
        "Названия должны совпадать с маркерами в шаблоне.\n\n"
        "<b>Пример:</b>\n"
        "<code>ФИО\n"
        "ТЕЛЕФОН\n"
        "ПАСПОРТ\n"
        "АДРЕС</code>\n\n"
        "⚠️ Именно в той последовательности, в которой хотите "
        "отправлять данные в бот.\n\n"
        "💡 Для многострочных полей (реквизиты) добавьте <b>*</b> в конце:\n"
        "<code>РЕКВИЗИТЫ*</code>\n"
        "Такое поле принимает несколько строк и должно быть последним.\n\n"
        "⚡ Авто-поля указывать НЕ нужно:\n"
        "СЕГОДНЯ, ФАМИЛИЯ_ИО, СУММА_ПРОПИСЬЮ, ЗАДАТОК_ПРОПИСЬЮ\n\n"
        "/cancel — отмена",
        parse_mode=ParseMode.HTML,
    )


@router.message(States.waiting_fields)
async def handle_fields_config(message: Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Отменено.", reply_markup=main_keyboard(message.from_user.id))
        return

    data = await state.get_data()
    template_id = data["template_id"]

    fields = []
    for line in message.text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue

        if "|" in line:
            parts = line.split("|", 1)
            label = parts[0].strip()
            marker = parts[1].strip()
            if not marker.startswith("{{"):
                marker = "{{" + marker
            if not marker.endswith("}}"):
                marker = marker + "}}"
        else:
            label = line.strip()
            marker = "{{" + label + "}}"

        # Detect multiline marker: РЕКВИЗИТЫ* → multiline field
        multiline = False
        if label.endswith("*"):
            label = label.rstrip("*").strip()
            marker = "{{" + label + "}}"
            multiline = True

        field = {"label": label, "marker": marker}
        if multiline:
            field["multiline"] = True
        fields.append(field)

    if not fields:
        await message.answer("❌ Не удалось разобрать поля. Попробуйте ещё раз.")
        return

    await db.update_fields(template_id, fields)
    await state.clear()

    fields_text = "\n".join(
        f"  {i+1}. {f['label']} → <code>{f['marker']}</code>"
        + (" (многострочное)" if f.get("multiline") else "")
        for i, f in enumerate(fields)
    )
    await message.answer(
        f"✅ Поля настроены!\n\n{fields_text}",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(message.from_user.id),
    )


# ── Configure Filename Format ────────────────────────────────────────────────
@router.callback_query(F.data.startswith("cfg_fname:"))
async def cb_config_filename(callback: CallbackQuery, state: FSMContext):
    template_id = int(callback.data.split(":")[1])
    t = await db.get_template(template_id)
    if not t:
        await callback.answer("Шаблон не найден", show_alert=True)
        return
    if t["created_by"] != callback.from_user.id and not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    await state.set_state(States.waiting_filename_format)
    await state.update_data(template_id=template_id)

    current_fmt = t["filename_format"] or "(по умолчанию: Шаблон для Фамилия ИО Дата)"

    # Build list of available markers from template fields
    fields = json.loads(t["fields_config"])
    field_markers = ", ".join(f"<code>{{{f['label']}}}</code>" for f in fields[:5])
    if not field_markers:
        field_markers = "<code>{ФИО}</code>, <code>{ДАТА}</code>"

    await callback.message.edit_text(
        f"📝 Формат имени файла для «{t['name']}»\n\n"
        f"<b>Текущий:</b> {current_fmt}\n\n"
        "Напишите как должен называться файл, "
        "используя названия полей в одинарных скобках:\n\n"
        "<b>Пример:</b>\n"
        "<code>Договор от Дробыша для {ФАМИЛИЯ_ИО} на {ДАТА}</code>\n\n"
        "Результат: <code>Договор от Дробыша для Чернявская МП на 10 марта 2026.docx</code>\n\n"
        "<b>Доступные маркеры:</b>\n"
        f"• Поля шаблона: {field_markers}\n"
        "• <code>{ШАБЛОН}</code> — название шаблона\n"
        "• <code>{ФАМИЛИЯ_ИО}</code> — фамилия и инициалы\n\n"
        "Отправьте «сброс» для формата по умолчанию.\n"
        "/cancel — отмена",
        parse_mode=ParseMode.HTML,
    )


@router.message(States.waiting_filename_format)
async def handle_filename_format(message: Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Отменено.", reply_markup=main_keyboard(message.from_user.id))
        return

    data = await state.get_data()
    template_id = data["template_id"]

    fmt = message.text.strip()
    if fmt.lower() in ("сброс", "reset", "по умолчанию"):
        fmt = ""

    await db.update_filename_format(template_id, fmt)
    await state.clear()

    result_text = fmt or "(по умолчанию)"
    await message.answer(
        f"✅ Формат имени файла: {result_text}",
        reply_markup=main_keyboard(message.from_user.id),
    )


# ── Manage Users (Admin Only) ───────────────────────────────────────────────
@router.callback_query(F.data == "manage_users")
async def cb_manage_users(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    users = await db.get_approved_users()
    users_text = ""
    if users:
        users_text = "\n\n<b>Одобренные пользователи:</b>\n" + "\n".join(
            f"  • {u['full_name'] or u['username'] or '???'} "
            f"(ID: <code>{u['user_id']}</code>)"
            for u in users
        )

    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить пользователя", callback_data="add_user")
    if users:
        for u in users:
            name = u["full_name"] or u["username"] or str(u["user_id"])
            kb.button(text=f"❌ {name}", callback_data=f"rm_user:{u['user_id']}")
    kb.button(text="◀️ Назад", callback_data="main_menu")
    kb.adjust(1)

    await callback.message.edit_text(
        f"👥 Управление доступом{users_text}",
        parse_mode=ParseMode.HTML,
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data == "add_user")
async def cb_add_user(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(States.waiting_user_id)
    await callback.message.edit_text(
        "Отправьте ID пользователя или перешлите его сообщение.\n\n/cancel — отмена"
    )


@router.message(States.waiting_user_id)
async def handle_add_user(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    if message.text == "/cancel":
        await state.clear()
        await message.answer("Отменено.", reply_markup=main_keyboard(message.from_user.id))
        return

    if message.forward_from:
        user_id = message.forward_from.id
        username = message.forward_from.username
        full_name = message.forward_from.full_name
    else:
        try:
            user_id = int(message.text.strip())
        except ValueError:
            await message.answer(
                "❌ Некорректный ID. Отправьте числовой ID или перешлите сообщение."
            )
            return
        username = None
        full_name = None

    await db.get_or_create_user(user_id, username, full_name)
    await db.approve_user(user_id)
    await state.clear()
    await message.answer(
        f"✅ Пользователь {full_name or username or user_id} добавлен!",
        reply_markup=main_keyboard(message.from_user.id),
    )


@router.callback_query(F.data.startswith("rm_user:"))
async def cb_remove_user(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    user_id = int(callback.data.split(":")[1])
    await db.disapprove_user(user_id)
    await callback.answer("✅ Пользователь удалён")
    await cb_manage_users(callback)


# ── Fill Document ────────────────────────────────────────────────────────────
@router.callback_query(F.data == "fill_start")
async def cb_fill_start(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if not is_admin(user_id) and not await db.is_approved(user_id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    templates = await db.get_templates(created_by=user_id)
    if not templates:
        await callback.message.edit_text(
            "У вас нет шаблонов. Загрузите через «📄 Мои шаблоны».",
            reply_markup=main_keyboard(user_id),
        )
        return

    if len(templates) == 1:
        t = templates[0]
        fields = json.loads(t["fields_config"])
        if not fields:
            await callback.message.edit_text(
                "Поля не настроены. Настройте через «📄 Мои шаблоны».",
                reply_markup=main_keyboard(user_id),
            )
            return
        await state.set_state(States.waiting_data)
        await state.update_data(template_id=t["id"], total_steps=2)
        fields_list = format_fields_list(fields)
        await callback.message.edit_text(
            f"📝 <b>Заполнение — Шаг 1 из 2</b>\n\n"
            f"Шаблон: «{t['name']}»\n\n"
            f"<b>Отправьте данные одним сообщением</b>, "
            f"каждое значение на новой строке:\n\n{fields_list}\n\n"
            f"💡 Можно с нумерацией (1. 2. 3.) или без — бот разберётся.\n\n"
            "/cancel — отмена",
            parse_mode=ParseMode.HTML,
        )
    else:
        kb = InlineKeyboardBuilder()
        for t in templates:
            kb.button(
                text=f"📄 {t['name']}", callback_data=f"fill_tpl:{t['id']}"
            )
        kb.button(text="📋 Заполнить все", callback_data="fill_all")
        kb.button(text="◀️ Назад", callback_data="main_menu")
        kb.adjust(1)
        await callback.message.edit_text(
            "📝 <b>Заполнение — Шаг 1 из 3</b>\n\n"
            "Выберите шаблон:",
            parse_mode=ParseMode.HTML,
            reply_markup=kb.as_markup(),
        )


@router.callback_query(F.data.startswith("fill_tpl:"))
async def cb_fill_template_select(callback: CallbackQuery, state: FSMContext):
    template_id = int(callback.data.split(":")[1])
    t = await db.get_template(template_id)
    if not t:
        await callback.answer("Шаблон не найден", show_alert=True)
        return

    fields = json.loads(t["fields_config"])
    if not fields:
        await callback.message.edit_text(
            "Поля не настроены. Настройте через «📄 Мои шаблоны».",
            reply_markup=main_keyboard(callback.from_user.id),
        )
        return

    await state.set_state(States.waiting_data)
    await state.update_data(template_id=t["id"], total_steps=3)
    fields_list = format_fields_list(fields)
    await callback.message.edit_text(
        f"📝 <b>Заполнение — Шаг 2 из 3</b>\n\n"
        f"Шаблон: «{t['name']}»\n\n"
        f"<b>Отправьте данные одним сообщением</b>, "
        f"каждое значение на новой строке:\n\n{fields_list}\n\n"
        f"💡 Можно с нумерацией (1. 2. 3.) или без — бот разберётся.\n\n"
        "/cancel — отмена",
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data == "fill_all")
async def cb_fill_all(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    templates = await db.get_templates(created_by=user_id)
    if not templates:
        await callback.answer("Нет шаблонов", show_alert=True)
        return

    # Collect unique fields across all templates
    seen_markers = set()
    unified_fields = []
    template_ids = []
    for t in templates:
        template_ids.append(t["id"])
        fields = json.loads(t["fields_config"])
        for f in fields:
            if f["marker"] not in seen_markers:
                seen_markers.add(f["marker"])
                unified_fields.append(f)

    if not unified_fields:
        await callback.message.edit_text(
            "Поля не настроены ни для одного шаблона.",
            reply_markup=main_keyboard(user_id),
        )
        return

    await state.set_state(States.waiting_data_all)
    await state.update_data(
        template_ids=template_ids,
        unified_fields=unified_fields,
    )

    template_names = ", ".join(f"«{t['name']}»" for t in templates)
    fields_list = format_fields_list(unified_fields)
    await callback.message.edit_text(
        f"📝 <b>Заполнение всех — Шаг 1 из 2</b>\n\n"
        f"Шаблоны: {template_names}\n\n"
        f"<b>Отправьте данные одним сообщением</b>, "
        f"каждое значение на новой строке:\n\n{fields_list}\n\n"
        f"💡 Можно с нумерацией (1. 2. 3.) или без — бот разберётся.\n\n"
        "/cancel — отмена",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("fill"))
async def cmd_fill(message: Message, state: FSMContext):
    user_id = message.from_user.id
    if not is_admin(user_id) and not await db.is_approved(user_id):
        await message.answer("⛔ Нет доступа.")
        return

    templates = await db.get_templates(created_by=user_id)
    if not templates:
        await message.answer("У вас нет шаблонов.")
        return

    if len(templates) == 1:
        t = templates[0]
        fields = json.loads(t["fields_config"])
        if not fields:
            await message.answer("Поля не настроены.")
            return
        await state.set_state(States.waiting_data)
        await state.update_data(template_id=t["id"], total_steps=2)
        fields_list = format_fields_list(fields)
        await message.answer(
            f"📝 <b>Заполнение — Шаг 1 из 2</b>\n\n"
            f"Шаблон: «{t['name']}»\n\n"
            f"<b>Отправьте данные одним сообщением</b>, "
            f"каждое значение на новой строке:\n\n{fields_list}\n\n"
            f"💡 Можно с нумерацией (1. 2. 3.) или без — бот разберётся.\n\n"
            "/cancel — отмена",
            parse_mode=ParseMode.HTML,
        )
    else:
        kb = InlineKeyboardBuilder()
        for t in templates:
            kb.button(
                text=f"📄 {t['name']}", callback_data=f"fill_tpl:{t['id']}"
            )
        kb.button(text="📋 Заполнить все", callback_data="fill_all")
        kb.adjust(1)
        await message.answer(
            "📝 <b>Заполнение — Шаг 1 из 3</b>\n\n"
            "Выберите шаблон:",
            parse_mode=ParseMode.HTML,
            reply_markup=kb.as_markup(),
        )


# ── Handle fill data (single template) ──────────────────────────────────────
@router.message(States.waiting_data)
async def handle_fill_data(message: Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        if message.text == "/cancel":
            await state.clear()
            await message.answer(
                "Отменено.", reply_markup=main_keyboard(message.from_user.id)
            )
        return

    data = await state.get_data()
    template_id = data.get("template_id")
    t = await db.get_template(template_id)
    if not t:
        await message.answer("❌ Ошибка: шаблон не найден.")
        await state.clear()
        return

    fields = json.loads(t["fields_config"])
    lines = message.text.strip().split("\n")

    result = parse_fill_data(fields, lines)
    if isinstance(result, str):
        fields_list = "\n".join(
            f"{i+1}. {f['label']}" + (" (многострочное)" if f.get("multiline") else "")
            for i, f in enumerate(fields)
        )
        await message.answer(result + f"Отправьте все данные заново:\n\n{fields_list}")
        return

    user_replacements = result
    auto_replacements = compute_auto_fields(user_replacements)

    # Save to FSM and show format choice
    await state.update_data(
        user_replacements=user_replacements,
        auto_replacements=auto_replacements,
        fill_mode="single",
    )

    total_steps = data.get("total_steps", 2)
    kb = InlineKeyboardBuilder()
    kb.button(text="📄 Word", callback_data="fmt:word")
    kb.button(text="📕 PDF", callback_data="fmt:pdf")
    kb.adjust(2)
    await message.answer(
        f"📝 <b>Заполнение — Шаг {total_steps} из {total_steps}</b>\n\n"
        "Выберите формат документа:",
        parse_mode=ParseMode.HTML,
        reply_markup=kb.as_markup(),
    )


# ── Handle fill data (all templates) ────────────────────────────────────────
@router.message(States.waiting_data_all)
async def handle_fill_data_all(message: Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        if message.text == "/cancel":
            await state.clear()
            await message.answer(
                "Отменено.", reply_markup=main_keyboard(message.from_user.id)
            )
        return

    data = await state.get_data()
    unified_fields = data.get("unified_fields", [])
    lines = message.text.strip().split("\n")

    result = parse_fill_data(unified_fields, lines)
    if isinstance(result, str):
        fields_list = "\n".join(
            f"{i+1}. {f['label']}" + (" (многострочное)" if f.get("multiline") else "")
            for i, f in enumerate(unified_fields)
        )
        await message.answer(result + f"Отправьте все данные заново:\n\n{fields_list}")
        return

    user_replacements = result
    auto_replacements = compute_auto_fields(user_replacements)

    await state.update_data(
        user_replacements=user_replacements,
        auto_replacements=auto_replacements,
        fill_mode="all",
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="📄 Word", callback_data="fmt:word")
    kb.button(text="📕 PDF", callback_data="fmt:pdf")
    kb.adjust(2)
    await message.answer(
        "📝 <b>Заполнение всех — Шаг 2 из 2</b>\n\n"
        "Выберите формат документа:",
        parse_mode=ParseMode.HTML,
        reply_markup=kb.as_markup(),
    )


# ── Handle format choice ────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("fmt:"))
async def cb_format_choice(callback: CallbackQuery, state: FSMContext):
    output_format = callback.data.split(":")[1]  # "word" or "pdf"
    data = await state.get_data()
    fill_mode = data.get("fill_mode", "single")
    user_replacements = data.get("user_replacements", {})
    auto_replacements = data.get("auto_replacements", {})

    if not user_replacements:
        await callback.answer("Данные не найдены, попробуйте заново", show_alert=True)
        await state.clear()
        return

    try:
        await callback.message.edit_text("⏳ Заполняю документ...")
    except Exception:
        pass

    if fill_mode == "single":
        template_id = data.get("template_id")
        t = await db.get_template(template_id)
        if t:
            await fill_and_send(
                callback.message, t, user_replacements,
                auto_replacements, output_format,
            )
    else:
        template_ids = data.get("template_ids", [])
        for tid in template_ids:
            t = await db.get_template(tid)
            if t:
                await fill_and_send(
                    callback.message, t, user_replacements,
                    auto_replacements, output_format,
                )

    await state.clear()

    try:
        await callback.message.delete()
    except Exception:
        pass

    await callback.message.answer(
        "✅ Готово! Выберите действие:",
        reply_markup=main_keyboard(callback.from_user.id),
    )


# ── Main ─────────────────────────────────────────────────────────────────────
async def main():
    await db.init()
    logger.info("Bot started. Admin IDs: %s", ADMIN_IDS)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
