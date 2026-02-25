import asyncio
import logging
import json
import os
import uuid
from typing import Any, Awaitable, Callable, Dict, List

from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, TelegramObject
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.utils.media_group import MediaGroupBuilder

def load_config():
    if not os.path.exists("config.json"):
        print("❌ Ошибка: Файл config.json не найден!")
        exit()
    with open("config.json", "r", encoding="utf-8") as f:
        return json.load(f)

config = load_config()

BOT_TOKEN = config["bot_token"]
CHANNEL_ID = config["channel_id"]
ADMIN_IDS = [str(x) for x in config["admin_ids"]]
CATEGORIES = config["categories"]

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
logging.basicConfig(level=logging.INFO)

PENDING_POSTS = {}

# --- MIDDLEWARE ДЛЯ АЛЬБОМОВ ---
class AlbumMiddleware(BaseMiddleware):
    def __init__(self, latency: float = 0.5):
        self.latency = latency
        self.album_data = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        if not isinstance(event, types.Message) or not event.media_group_id:
            return await handler(event, data)

        media_group_id = event.media_group_id
        if media_group_id not in self.album_data:
            self.album_data[media_group_id] = [event]
            await asyncio.sleep(self.latency)
            data["album"] = self.album_data.pop(media_group_id)
            return await handler(event, data)
        else:
            self.album_data[media_group_id].append(event)
            return

dp.message.middleware(AlbumMiddleware())

# --- FSM ---
class PostState(StatesGroup):
    waiting_for_content = State()
    waiting_for_category = State()
    confirm_post = State()
    confirm_submission = State()

def is_admin(user_id):
    return str(user_id) in ADMIN_IDS

# --- КЛАВИАТУРЫ ---
def get_categories_kb():
    builder = InlineKeyboardBuilder()
    for cat_name in CATEGORIES.keys():
        builder.button(text=cat_name, callback_data=f"cat_{cat_name}")
    builder.adjust(2)
    return builder.as_markup()

def get_admin_confirm_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚀 Запостить", callback_data="admin_post_yes"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data="admin_post_no")
    ]])

def get_user_confirm_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📨 Отправить на проверку", callback_data="user_send_yes"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="user_send_no")
    ]])

def get_moderation_kb(post_id: str):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"mod_approve_{post_id}"),
        InlineKeyboardButton(text="🚫 Отклонить", callback_data=f"mod_reject_{post_id}")
    ]])


# --- ХЕНДЛЕРЫ ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    if is_admin(message.from_user.id):
        text = "👋 <b>Привет, Админ!</b>\nКидай контент — я подготовлю пост."
    else:
        text = "👋 <b>Привет!</b>\n📤 <b>Отправь мне файл(ы), фото или текст</b>, и я передам их админам."

    await message.answer(text, parse_mode="HTML")
    await state.set_state(PostState.waiting_for_content)

@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено. Жду новый контент.")
    await state.set_state(PostState.waiting_for_content)

# 1. Прием контента
@dp.message(StateFilter(PostState.waiting_for_content))
async def process_content(message: types.Message, state: FSMContext, album: List[types.Message] = None):
    data = {'media': [], 'type': 'text', 'caption': ''}

    if album:
        data['is_album'] = True
        data['caption'] = album[0].caption or album[0].text or ""

        for msg in album:
            if msg.photo:
                data['media'].append({'type': 'photo', 'file_id': msg.photo[-1].file_id})
                data['type'] = 'media'
            elif msg.video:
                data['media'].append({'type': 'video', 'file_id': msg.video.file_id})
                data['type'] = 'media'
            elif msg.document:
                data['media'].append({'type': 'document', 'file_id': msg.document.file_id})
                data['type'] = 'media'
    else:
        data['is_album'] = False
        data['caption'] = message.caption or message.text or ""

        if message.photo:
            data['media'].append({'type': 'photo', 'file_id': message.photo[-1].file_id})
            data['type'] = 'media'
        elif message.document:
            data['media'].append({'type': 'document', 'file_id': message.document.file_id})
            data['type'] = 'media'
        elif message.video:
            data['media'].append({'type': 'video', 'file_id': message.video.file_id})
            data['type'] = 'media'
        elif message.text:
            data['type'] = 'text'
        else:
            await message.answer("⚠️ Я понимаю только фото, видео, документы и текст.")
            return

    await state.update_data(content=data)
    await message.answer("📂 Выбери категорию:", reply_markup=get_categories_kb())
    await state.set_state(PostState.waiting_for_category)

def build_media_group(media_list, caption):
    builder = MediaGroupBuilder(caption=caption)
    for item in media_list:
        if item['type'] == 'photo':
            builder.add_photo(item['file_id'])
        elif item['type'] == 'video':
            builder.add_video(item['file_id'])
        elif item['type'] == 'document':
            builder.add_document(item['file_id'])
    return builder.build()

# 2. Обработка категории и Предпросмотр
@dp.callback_query(StateFilter(PostState.waiting_for_category), F.data.startswith("cat_"))
async def process_category(callback: types.CallbackQuery, state: FSMContext):
    category_name = callback.data.replace("cat_", "")
    tags = CATEGORIES.get(category_name, "")

    user_data = await state.get_data()
    content = user_data['content']

    base_text = content.get('caption', "")
    final_caption = f"{base_text}\n\n{tags}" if base_text else tags

    await state.update_data(final_caption=final_caption)
    await callback.message.delete()

    is_adm = is_admin(callback.from_user.id)
    kb = get_admin_confirm_kb() if is_adm else get_user_confirm_kb()
    next_state = PostState.confirm_post if is_adm else PostState.confirm_submission
    msg_text = "👀 <b>Предпросмотр:</b>" if is_adm else "👀 <b>Вот так это увидит админ. Отправляем?</b>"

    try:
        if content['is_album']:
            mg = build_media_group(content['media'], final_caption)
            await callback.message.answer_media_group(mg)
            await callback.message.answer(msg_text, reply_markup=kb, parse_mode="HTML")
        else:
            if content['type'] == 'text':
                await callback.message.answer(text=final_caption, reply_markup=kb)
            else:
                media_item = content['media'][0]
                if media_item['type'] == 'photo':
                    await callback.message.answer_photo(media_item['file_id'], caption=final_caption, reply_markup=kb)
                elif media_item['type'] == 'document':
                    await callback.message.answer_document(media_item['file_id'], caption=final_caption, reply_markup=kb)
                elif media_item['type'] == 'video':
                    await callback.message.answer_video(media_item['file_id'], caption=final_caption, reply_markup=kb)

        await state.set_state(next_state)
    except Exception as e:
        await callback.message.answer(f"Ошибка предпросмотра: {e}")

# --- ВЕТКА АДМИНА ---
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
    await callback.message.answer("🗑 Отменено.")
    await state.set_state(PostState.waiting_for_content)

# --- ВЕТКА ЮЗЕРА (ОТПРАВКА ВСЕМ АДМИНАМ) ---
@dp.callback_query(StateFilter(PostState.confirm_submission), F.data == "user_send_yes")
async def user_submit(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    content = data['content']
    final_caption = data['final_caption']
    user_id = callback.from_user.id
    user_name = callback.from_user.full_name

    post_id = str(uuid.uuid4())[:8]
    PENDING_POSTS[post_id] = {
        'content': content,
        'caption': final_caption,
        'author_id': user_id,
        'admin_messages': []
    }

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("✅ <b>Отправлено на модерацию!</b>", parse_mode="HTML")
    await state.set_state(PostState.waiting_for_content)

    admin_text = (
        f"🆕 <b>ПРЕДЛОЖКА</b>\n"
        f"👤 От: <a href='tg://user?id={user_id}'>{user_name}</a>"
    )
    kb = get_moderation_kb(post_id)

    sent_count = 0

    for target_admin_id in ADMIN_IDS:
        try:
            kb_msg = None

            if content['is_album']:
                mg = build_media_group(content['media'], final_caption)
                await bot.send_media_group(target_admin_id, mg)
                kb_msg = await bot.send_message(target_admin_id, admin_text, reply_markup=kb, parse_mode="HTML")
            else:
                if content['type'] == 'text':
                    kb_msg = await bot.send_message(target_admin_id, f"{admin_text}\n\n{final_caption}", reply_markup=kb, parse_mode="HTML")
                else:
                    media_item = content['media'][0]
                    if media_item['type'] == 'photo':
                        kb_msg = await bot.send_photo(target_admin_id, media_item['file_id'], caption=f"{admin_text}\n\n{final_caption}", reply_markup=kb, parse_mode="HTML")
                    elif media_item['type'] == 'document':
                        kb_msg = await bot.send_document(target_admin_id, media_item['file_id'], caption=f"{admin_text}\n\n{final_caption}", reply_markup=kb, parse_mode="HTML")
                    elif media_item['type'] == 'video':
                        kb_msg = await bot.send_video(target_admin_id, media_item['file_id'], caption=f"{admin_text}\n\n{final_caption}", reply_markup=kb, parse_mode="HTML")

            if kb_msg:
                PENDING_POSTS[post_id]['admin_messages'].append((target_admin_id, kb_msg.message_id))
                sent_count += 1

        except Exception as e:
            print(f"⚠️ Ошибка отправки админу {target_admin_id}: {e}")

    if sent_count == 0:
        await callback.message.answer("⚠️ Ошибка: Админы недоступны.")
        PENDING_POSTS.pop(post_id, None)

    await callback.answer()

@dp.callback_query(StateFilter(PostState.confirm_submission), F.data == "user_send_no")
async def user_cancel(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await callback.message.answer("❌ Отменено.")
    await state.set_state(PostState.waiting_for_content)

@dp.callback_query(F.data.startswith("mod_approve_"))
async def mod_approve(callback: types.CallbackQuery):
    post_id = callback.data.split("_")[-1]

    if post_id not in PENDING_POSTS:
        await callback.answer("⚠️ Этот пост уже обработан другим админом.", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except:
            pass
        return

    post_data = PENDING_POSTS.pop(post_id)

    content = post_data['content']
    caption = post_data['caption']
    author_id = post_data['author_id']
    admin_messages = post_data['admin_messages']

    for adm_id, msg_id in admin_messages:
        try:
            await bot.edit_message_reply_markup(chat_id=adm_id, message_id=msg_id, reply_markup=None)
        except Exception:
            pass

    try:
        await publish_to_channel(content, caption)

        await callback.message.answer(f"✅ <b>Опубликовано администратором {callback.from_user.full_name}!</b>", parse_mode="HTML")

        try:
            await bot.send_message(chat_id=author_id, text="🎉 <b>Твой пост опубликован в канале!</b>", parse_mode="HTML")
        except:
            pass

    except Exception as e:
        await callback.message.answer(f"❌ Ошибка публикации: {e}")

    await callback.answer()


@dp.callback_query(F.data.startswith("mod_reject_"))
async def mod_reject(callback: types.CallbackQuery):
    post_id = callback.data.split("_")[-1]

    if post_id not in PENDING_POSTS:
        await callback.answer("⚠️ Этот пост уже обработан другим админом.", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except:
            pass
        return

    post_data = PENDING_POSTS.pop(post_id)
    author_id = post_data['author_id']
    admin_messages = post_data['admin_messages']

    for adm_id, msg_id in admin_messages:
        try:
            await bot.edit_message_reply_markup(chat_id=adm_id, message_id=msg_id, reply_markup=None)
        except Exception:
            pass

    await callback.message.answer(f"🚫 <b>Отклонено администратором {callback.from_user.full_name}.</b>", parse_mode="HTML")

    try:
        await bot.send_message(chat_id=author_id, text="😔 Твой пост был отклонен модератором.")
    except:
        pass

    await callback.answer()

# --- ПУБЛИКАЦИЯ В КАНАЛ ---
async def publish_to_channel(content, caption):
    if content['is_album']:
        mg = build_media_group(content['media'], caption)
        await bot.send_media_group(CHANNEL_ID, mg)
    else:
        if content['type'] == 'text':
            await bot.send_message(CHANNEL_ID, text=caption)
        else:
            media_item = content['media'][0]
            if media_item['type'] == 'photo':
                await bot.send_photo(CHANNEL_ID, media_item['file_id'], caption=caption)
            elif media_item['type'] == 'document':
                await bot.send_document(CHANNEL_ID, media_item['file_id'], caption=caption)
            elif media_item['type'] == 'video':
                await bot.send_video(CHANNEL_ID, media_item['file_id'], caption=caption)

async def main():
    print("Бот запущен...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Стоп.")
