"""Тесты callback-обработчиков опросов: один опрос — одно сообщение.

Все действия с карточкой должны отвечать через answerCallbackQuery и не
отправлять новых сообщений в чат.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from schedule_bot.polls import db, handlers


class PollCallbacksTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self.tmp.name) / "test.db")
        self.poll_id = db.create_poll(
            creator_id=100,
            title="Дежурство",
            description="",
            chat_id=-100123,
            options=[("Понедельник", 1), ("Вторник", 1), ("Среда", 1)],
            default_max=1,
            allow_change=True,
            allow_cancel=True,
            show_participants=True,
            expires_at=None,
        )
        view = db.get_view(self.poll_id)
        assert view is not None
        self.options = [item.option for item in view.options]
        db.set_published_message(self.poll_id, -100123, 555, "photo")

        self.bot = SimpleNamespace(
            send_message=AsyncMock(),
            send_photo=AsyncMock(),
            edit_message_media=AsyncMock(),
            edit_message_caption=AsyncMock(),
            edit_message_text=AsyncMock(),
            delete_message=AsyncMock(),
        )
        self.refresh_patch = patch.object(handlers.service, "schedule_refresh")
        self.refresh_mock = self.refresh_patch.start()

    def tearDown(self) -> None:
        self.refresh_patch.stop()
        self.tmp.cleanup()

    def _query(
        self,
        data: str,
        *,
        user_id: int = 1,
        name: str = "Алексей",
        username: str | None = "alex",
    ):
        return SimpleNamespace(
            data=data,
            from_user=SimpleNamespace(id=user_id, full_name=name, username=username),
            answer=AsyncMock(),
        )

    def _assert_no_chat_messages(self) -> None:
        self.bot.send_message.assert_not_called()
        self.bot.send_photo.assert_not_called()

    async def test_pick_free_option_only_toast(self) -> None:
        query = self._query(f"poll:pick:{self.poll_id}:{self.options[0].id}:1")

        await handlers.on_pick(query, self.bot)

        query.answer.assert_awaited_once_with("✅ Тема успешно забронирована!")
        self._assert_no_chat_messages()
        self.refresh_mock.assert_called_once_with(self.bot, self.poll_id)
        view = db.get_view(self.poll_id)
        assert view is not None
        self.assertEqual(view.options[0].taken, 1)

    async def test_pick_taken_option_only_toast(self) -> None:
        db.book(self.poll_id, self.options[0].id, 2, "Мария", "maria")
        query = self._query(f"poll:pick:{self.poll_id}:{self.options[0].id}:1")

        await handlers.on_pick(query, self.bot)

        query.answer.assert_awaited_once_with("⚠️ Этот вариант уже занят.")
        self._assert_no_chat_messages()

    async def test_change_selection_is_atomic_toast(self) -> None:
        db.book(self.poll_id, self.options[0].id, 1, "Алексей", "alex")
        query = self._query(f"poll:pick:{self.poll_id}:{self.options[2].id}:2")

        await handlers.on_pick(query, self.bot)

        query.answer.assert_awaited_once_with("✅ Выбор изменён!")
        self._assert_no_chat_messages()
        view = db.get_view(self.poll_id)
        assert view is not None
        self.assertEqual(view.options[0].taken, 0)
        self.assertEqual(view.options[2].taken, 1)

    async def test_change_button_is_informational(self) -> None:
        db.book(self.poll_id, self.options[0].id, 1, "Алексей", "alex")
        query = self._query(f"poll:change:{self.poll_id}")

        await handlers.on_change(query, self.bot)

        query.answer.assert_awaited_once_with(
            "🔄 Для изменения выбора нажмите на другой свободный вариант."
        )
        self._assert_no_chat_messages()

    async def test_cancel_frees_slot_card_only(self) -> None:
        db.book(self.poll_id, self.options[1].id, 1, "Алексей", "alex")
        query = self._query(f"poll:cancel:{self.poll_id}")

        await handlers.on_cancel_booking(query, self.bot)

        query.answer.assert_awaited_once_with("✅ Ваш выбор отменён.")
        self._assert_no_chat_messages()
        self.refresh_mock.assert_called_once_with(self.bot, self.poll_id)
        view = db.get_view(self.poll_id)
        assert view is not None
        self.assertEqual(view.options[1].taken, 0)

    async def test_cancel_without_booking_only_toast(self) -> None:
        query = self._query(f"poll:cancel:{self.poll_id}")

        await handlers.on_cancel_booking(query, self.bot)

        query.answer.assert_awaited_once_with("ℹ️ У вас пока нет выбранной темы.")
        self._assert_no_chat_messages()

    async def test_refresh_edits_existing_message(self) -> None:
        query = self._query(f"poll:refresh:{self.poll_id}")
        with patch.object(
            handlers.service, "refresh_published_card", AsyncMock(return_value=True)
        ) as refresh:
            await handlers.on_refresh(query, self.bot)

        query.answer.assert_awaited_once_with("🔄 Обновляю карточку…")
        refresh.assert_awaited_once_with(self.bot, self.poll_id, force=True)
        self._assert_no_chat_messages()

    async def test_malformed_callback_is_answered(self) -> None:
        query = self._query("poll:pick:oops")

        await handlers.on_pick(query, self.bot)

        query.answer.assert_awaited_once()
        self._assert_no_chat_messages()


if __name__ == "__main__":
    unittest.main()
