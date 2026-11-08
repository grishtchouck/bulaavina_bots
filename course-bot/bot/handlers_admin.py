import asyncio
import csv
import io
import logging
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from .db import DB
from .scenario import Scenario, ScenarioError

log = logging.getLogger(__name__)

HELP = """<b>Админка</b>

/stats — статистика
/broadcast — рассылка
/user &lt;id или @username&gt; — карточка пользователя
/grant &lt;id&gt; &lt;категория&gt; — выдать категорию
/revoke &lt;id&gt; &lt;категория&gt; — забрать категорию
/categories — список категорий
/export — выгрузка пользователей в CSV
/reload — перечитать scenario.yaml
/cancel — отменить текущее действие"""


class Broadcast(StatesGroup):
    message = State()
    confirm = State()


def make_router(admin_ids: set[int]) -> Router:
    r = Router(name="admin")
    r.message.filter(F.from_user.id.in_(admin_ids))
    r.callback_query.filter(F.from_user.id.in_(admin_ids))

    @r.message(Command("admin", "help"))
    async def cmd_admin(msg: Message):
        await msg.answer(HELP)

    @r.message(Command("cancel"))
    async def cmd_cancel(msg: Message, state: FSMContext):
        await state.clear()
        await msg.answer("Отменено.")

    @r.message(Command("stats"))
    async def cmd_stats(msg: Message, db: DB, sc: Scenario):
        s = await db.stats()
        lines = [
            "<b>Статистика</b>",
            f"Пользователей: {s['users']} (активных {s['active']}, новых за сутки {s['new_24h']})",
            f"Оплат: {s['paid_count']} на {s['paid_sum']:,.0f} ₽".replace(",", " "),
            "", "<b>Категории</b>",
        ]
        lines += [f"• {c} — {n}  <i>{sc.categories.get(c, '')}</i>" for c, n in s["categories"]] or ["—"]
        await msg.answer("\n".join(lines))

    @r.message(Command("categories"))
    async def cmd_categories(msg: Message, sc: Scenario):
        await msg.answer("\n".join(f"<code>{k}</code> — {v}" for k, v in sc.categories.items()) or "—")

    @r.message(Command("user"))
    async def cmd_user(msg: Message, command: CommandObject, db: DB):
        if not command.args:
            return await msg.answer("Формат: /user 123456 или /user @username")
        u = await db.find_user(command.args.strip())
        if not u:
            return await msg.answer("Не найден.")
        cats = await db.categories(u["id"])
        created = datetime.fromtimestamp(u["created_at"]).strftime("%d.%m.%Y %H:%M")
        await msg.answer(
            f"<b>{u['first_name'] or ''}</b> @{u['username'] or '—'}\n"
            f"ID: <code>{u['id']}</code>\nС нами с: {created}\n"
            f"Источник: {u['start_param'] or '—'}\n"
            f"Заблокировал бота: {'да' if u['blocked'] else 'нет'}\n"
            f"Категории: {', '.join(sorted(cats)) or '—'}")

    async def _grant_revoke(msg: Message, command: CommandObject, db: DB, sc: Scenario, add: bool):
        parts = (command.args or "").split()
        if len(parts) != 2:
            return await msg.answer(f"Формат: /{'grant' if add else 'revoke'} &lt;id или @username&gt; &lt;категория&gt;")
        u = await db.find_user(parts[0])
        if not u:
            return await msg.answer("Пользователь не найден.")
        if parts[1] not in sc.categories:
            return await msg.answer("Нет такой категории. Список: /categories")
        if add:
            await db.add_categories(u["id"], [parts[1]])
        else:
            await db.remove_categories(u["id"], [parts[1]])
        await msg.answer("Готово.")

    @r.message(Command("grant"))
    async def cmd_grant(msg: Message, command: CommandObject, db: DB, sc: Scenario):
        await _grant_revoke(msg, command, db, sc, True)

    @r.message(Command("revoke"))
    async def cmd_revoke(msg: Message, command: CommandObject, db: DB, sc: Scenario):
        await _grant_revoke(msg, command, db, sc, False)

    @r.message(Command("export"))
    async def cmd_export(msg: Message, db: DB):
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["id", "username", "first_name", "source", "created_at", "blocked", "categories"])
        for row in await db.export_rows():
            w.writerow([row["id"], row["username"] or "", row["first_name"] or "", row["start_param"] or "",
                        datetime.fromtimestamp(row["created_at"]).strftime("%Y-%m-%d %H:%M"),
                        row["blocked"], row["categories"]])
        data = buf.getvalue().encode("utf-8-sig")  # BOM — чтобы Excel открыл кириллицу
        await msg.answer_document(BufferedInputFile(data, filename=f"users_{datetime.now():%Y%m%d}.csv"))

    @r.message(Command("reload"))
    async def cmd_reload(msg: Message, sc: Scenario):
        try:
            sc.load()
        except ScenarioError as e:
            return await msg.answer(f"❌ Ошибки в сценарии, оставил старую версию:\n<pre>{e}</pre>")
        except Exception as e:  # yaml syntax etc.
            return await msg.answer(f"❌ Не удалось прочитать файл:\n<pre>{e}</pre>")
        await msg.answer(f"✅ Сценарий перечитан: {len(sc.screens)} экранов, {len(sc.tariffs)} тарифов.")

    # ---------------- broadcast ----------------
    def audience_kb(sc: Scenario) -> InlineKeyboardMarkup:
        rows = [[InlineKeyboardButton(text="Все пользователи", callback_data="bc:all")],
                [InlineKeyboardButton(text="Ничего не купили", callback_data="bc:np")]]
        rows += [[InlineKeyboardButton(text=f"{k} — {v}"[:60], callback_data=f"bc:c:{k}")]
                 for k, v in sc.categories.items()]
        rows.append([InlineKeyboardButton(text="Отмена", callback_data="bc:cancel")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @r.message(Command("broadcast"))
    async def cmd_broadcast(msg: Message, state: FSMContext, sc: Scenario):
        await state.clear()
        await msg.answer("Кому отправляем?", reply_markup=audience_kb(sc))

    @r.callback_query(F.data == "bc:cancel")
    async def bc_cancel(cq: CallbackQuery, state: FSMContext):
        await state.clear()
        await cq.message.edit_text("Рассылка отменена.")

    @r.callback_query(F.data.startswith("bc:") & ~F.data.in_({"bc:go", "bc:cancel"}))
    async def bc_audience(cq: CallbackQuery, state: FSMContext, db: DB, sc: Scenario):
        code = cq.data[3:]
        if code == "all":
            ids, label = await db.audience(), "все пользователи"
        elif code == "np":
            granted = sorted({c for t in sc.tariffs.values() for c in t.get("grant", [])})
            ids, label = await db.audience(none_of=granted), "ничего не купили"
        else:
            cat = code[2:]
            ids, label = await db.audience(any_of=[cat]), f"категория {cat}"
        await state.set_state(Broadcast.message)
        await state.update_data(ids=ids, label=label)
        await cq.message.edit_text(
            f"Аудитория: <b>{label}</b> — {len(ids)} чел.\n\n"
            "Пришлите сообщение для рассылки — текст, фото, видео, кружок, файл (как есть, "
            "с форматированием). /cancel — отмена.")

    @r.message(Broadcast.message)
    async def bc_message(msg: Message, state: FSMContext):
        data = await state.get_data()
        await state.update_data(src_chat=msg.chat.id, src_msg=msg.message_id)
        await state.set_state(Broadcast.confirm)
        await msg.answer("👆 Так увидят получатели.")
        await msg.send_copy(msg.chat.id)
        await msg.answer(
            f"Отправить <b>{len(data['ids'])}</b> пользователям ({data['label']})?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Отправить", callback_data="bc:go"),
                InlineKeyboardButton(text="Отмена", callback_data="bc:cancel")]]))

    @r.callback_query(Broadcast.confirm, F.data == "bc:go")
    async def bc_go(cq: CallbackQuery, state: FSMContext, bot: Bot, db: DB):
        data = await state.get_data()
        await state.clear()
        await cq.message.edit_text(f"🚀 Рассылка запущена: {len(data['ids'])} получателей.")
        asyncio.create_task(run_broadcast(bot, db, cq.from_user.id, data))

    return r


async def run_broadcast(bot: Bot, db: DB, admin_id: int, data: dict):
    ok = blocked = failed = 0
    for i, uid in enumerate(data["ids"], 1):
        for _ in range(3):
            try:
                await bot.copy_message(uid, data["src_chat"], data["src_msg"])
                ok += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:
                blocked += 1
                await db.set_blocked(uid)
                break
            except TelegramBadRequest as e:
                failed += 1
                log.warning("broadcast to %s failed: %s", uid, e)
                break
        await asyncio.sleep(0.05)  # ~20 msg/sec, below Telegram's limit
        if i % 500 == 0:
            await bot.send_message(admin_id, f"…отправлено {i} из {len(data['ids'])}")
    await bot.send_message(
        admin_id, f"✅ Рассылка завершена\nДоставлено: {ok}\nЗаблокировали бота: {blocked}\nОшибки: {failed}")
