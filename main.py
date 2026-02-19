import asyncio
import logging
import json
import os
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

def load_config():
    if not os.path.exists("config.json"):
        print("❌ Ошибка: Файл config.json не найден!")
        exit()
    with open("config.json", "r", encoding="utf-8") as f:
        return json.load(f)

config = load_config()

BOT_TOKEN = config["bot_token"]
CHANNEL_ID = config["channel_id"]
ADMIN_IDS = config["admin_ids"]
CATEGORIES = config["categories"]

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
logging.basicConfig(level=logging.INFO)

# --- FSM ---
class PostState(StatesGroup):
    waiting_for_content = State()
    waiting_for_category = State()
    confirm_post = State()
    confirm_submission = State()

def is_admin(user_id):
    return str(user_id) in [str(x) for x in ADMIN_IDS]


# 1. Выбор категории
def get_categories_kb():
    builder = InlineKeyboardBuilder()
    for cat_name in CATEGORIES.keys():
        builder.button(text=cat_name, callback_data=f"cat_{cat_name}")
    builder.adjust(2)
    return builder.as_markup()

# 2. Подтверждение для АДМИНА (сразу в канал)
def get_admin_confirm_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚀 Запостить в канал", callback_data="admin_post_yes"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data="admin_post_no")
    ]])

# 3. Подтверждение для ЮЗЕРА (отправить на проверку)
def get_user_confirm_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📨 Отправить на проверку", callback_data="user_send_yes"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="user_send_no")
    ]])

# 4. Кнопки МОДЕРАЦИИ (под постом в личке у админа)
def get_moderation_kb(user_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"mod_approve_{user_id}"),
        InlineKeyboardButton(text="🚫 Отклонить", callback_data=f"mod_reject_{user_id}")
    ]])

# --- ХЕНДЛЕРЫ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()

    if is_admin(message.from_user.id):
        text = "👋 <b>Привет, Админ!</b>\nКидай контент — я подготовлю пост для канала."
    else:
        text = "👋 <b>Привет!</b>\nХочешь поделиться лекцией, лабой или мемом?\n\n📤 <b>Отправь мне файл, фото или текст</b>, и я передам его админам."

    await message.answer(text, parse_mode="HTML")
    await state.set_state(PostState.waiting_for_content)

@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено. Жду новый контент.")
    await state.set_state(PostState.waiting_for_content)

# 1. Прием контента (от всех)
@dp.message(StateFilter(PostState.waiting_for_content))
async def process_content(message: types.Message, state: FSMContext):
    data = {}

    if message.photo:
        data['type'] = 'photo'
        data['file_id'] = message.photo[-1].file_id
        data['caption'] = message.caption or ""
    elif message.document:
        data['type'] = 'document'
        data['file_id'] = message.document.file_id
        data['caption'] = message.caption or ""
    elif message.video:
        data['type'] = 'video'
        data['file_id'] = message.video.file_id
        data['caption'] = message.caption or ""
    elif message.text:
        data['type'] = 'text'
        data['text'] = message.text
        data['caption'] = message.text
    else:
        await message.answer("⚠️ Я понимаю только фото, видео, документы и текст.")
        return

    await state.update_data(content=data)
    await message.answer("📂 Выбери категорию:", reply_markup=get_categories_kb())
    await state.set_state(PostState.waiting_for_category)

# 2. Обработка категории
@dp.callback_query(StateFilter(PostState.waiting_for_category), F.data.startswith("cat_"))
async def process_category(callback: types.CallbackQuery, state: FSMContext):
    category_name = callback.data.replace("cat_", "")
    tags = CATEGORIES.get(category_name, "")

    user_data = await state.get_data()
    content = user_data['content']

    base_text = content.get('caption', "")
    if base_text:
        final_caption = f"{base_text}\n\n{tags}"
    else:
        final_caption = tags

    await state.update_data(final_caption=final_caption)
    await callback.message.delete()

    if is_admin(callback.from_user.id):
        kb = get_admin_confirm_kb()
        next_state = PostState.confirm_post
        msg_text = "👀 <b>Предпросмотр:</b>"
    else:
        kb = get_user_confirm_kb()
        next_state = PostState.confirm_submission
        msg_text = "👀 <b>Вот так это увидит админ. Отправляем?</b>"

    await callback.message.answer(msg_text, parse_mode="HTML")

    try:
        if content['type'] == 'photo':
            await callback.message.answer_photo(content['file_id'], caption=final_caption, reply_markup=kb)
        elif content['type'] == 'document':
            await callback.message.answer_document(content['file_id'], caption=final_caption, reply_markup=kb)
        elif content['type'] == 'video':
            await callback.message.answer_video(content['file_id'], caption=final_caption, reply_markup=kb)
        elif content['type'] == 'text':
            await callback.message.answer(text=final_caption, reply_markup=kb)

        await state.set_state(next_state)
    except Exception as e:
        await callback.message.answer(f"Ошибка: {e}")

# --- ВЕТКА АДМИНА (Сразу в канал) ---
@dp.callback_query(StateFilter(PostState.confirm_post), F.data == "admin_post_yes")
async def admin_post(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await publish_to_channel(data['content'], data['final_caption'])
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("✅ Опубликовано в канал!")
    await state.set_state(PostState.waiting_for_content)
    await callback.answer()

@dp.callback_query(StateFilter(PostState.confirm_post), F.data == "admin_post_no")
async def admin_cancel(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await callback.message.answer("🗑 Удалено.")
    await state.set_state(PostState.waiting_for_content)

# --- ВЕТКА ЮЗЕРА (Предложка с Round-Robin) ---
@dp.callback_query(StateFilter(PostState.confirm_submission), F.data == "user_send_yes")
async def user_submit(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    content = data['content']
    final_caption = data['final_caption']
    user_id = callback.from_user.id
    user_name = callback.from_user.full_name

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("✅ <b>Отправлено на модерацию!</b>\nЖди, пока админ проверит.", parse_mode="HTML")
    await state.set_state(PostState.waiting_for_content)

    admin_text = (
        f"🆕 <b>ПРЕДЛОЖКА</b>\n"
        f"👤 От: <a href='tg://user?id={user_id}'>{user_name}</a>"
    )
    kb = get_moderation_kb(user_id)

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, admin_text, parse_mode="HTML")

            if content['type'] == 'photo':
                await bot.send_photo(admin_id, content['file_id'], caption=final_caption, reply_markup=kb)
            elif content['type'] == 'document':
                await bot.send_document(admin_id, content['file_id'], caption=final_caption, reply_markup=kb)
            elif content['type'] == 'video':
                await bot.send_video(admin_id, content['file_id'], caption=final_caption, reply_markup=kb)
            elif content['type'] == 'text':
                await bot.send_message(admin_id, text=final_caption, reply_markup=kb)

        except Exception as e:
            print(f"⚠️ Не смог отправить предложку админу {admin_id}: {e}")

    await callback.answer()

@dp.callback_query(StateFilter(PostState.confirm_submission), F.data == "user_send_no")
async def user_cancel(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await callback.message.answer("❌ Отменено.")
    await state.set_state(PostState.waiting_for_content)

# --- МОДЕРАЦИЯ (Кнопки у админа) ---

@dp.callback_query(F.data.startswith("mod_approve_"))
async def mod_approve(callback: types.CallbackQuery):
    author_id = callback.data.split("_")[-1]
    message = callback.message

    content_type = None
    file_id = None
    text = message.caption or message.text or ""

    if message.photo:
        content_type = 'photo'
        file_id = message.photo[-1].file_id
    elif message.document:
        content_type = 'document'
        file_id = message.document.file_id
    elif message.video:
        content_type = 'video'
        file_id = message.video.file_id
    elif message.text:
        content_type = 'text'

    content_obj = {'type': content_type, 'file_id': file_id, 'text': text}

    try:
        await callback.message.edit_reply_markup(reply_markup=None)

        await publish_to_channel(content_obj, text)

        await callback.message.reply(f"✅ Опубликовал: {callback.from_user.full_name}")

        try:
            await bot.send_message(chat_id=author_id, text="🎉 Твой пост опубликован в канале!")
        except:
            pass

    except Exception as e:
        await callback.message.answer(f"❌ Ошибка публикации: {e}")

    await callback.answer()

@dp.callback_query(F.data.startswith("mod_reject_"))
async def mod_reject(callback: types.CallbackQuery):
    author_id = callback.data.split("_")[-1]

    original_caption = callback.message.caption or callback.message.text or ""
    new_caption = original_caption + "\n\n❌ <b>ОТКЛОНЕНО</b>"

    try:
        if callback.message.caption:
            await callback.message.edit_caption(caption=new_caption, reply_markup=None, parse_mode="HTML")
        else:
            await callback.message.edit_text(text=new_caption, reply_markup=None, parse_mode="HTML")
    except:
        await callback.message.edit_reply_markup(reply_markup=None)

    await callback.message.reply("🚫 Пост отклонен.")

    try:
        await bot.send_message(chat_id=author_id, text="😔 Твой пост был отклонен модератором.")
    except:
        pass

    await callback.answer()

async def publish_to_channel(content, caption):
    if content['type'] == 'photo':
        await bot.send_photo(CHANNEL_ID, content['file_id'], caption=caption)
    elif content['type'] == 'document':
        await bot.send_document(CHANNEL_ID, content['file_id'], caption=caption)
    elif content['type'] == 'video':
        await bot.send_video(CHANNEL_ID, content['file_id'], caption=caption)
    elif content['type'] == 'text':
        txt = caption if caption else content.get('text', "")
        await bot.send_message(CHANNEL_ID, text=txt)

async def main():
    print("Бот запущен...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Стоп.")
