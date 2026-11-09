"""Тесты слоя хранения опросов (без Telegram)."""

from __future__ import annotations

import datetime
import tempfile
import threading
import unittest
from pathlib import Path

from schedule_bot.polls import db


class PollDbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db.set_db_path(Path(self.tmp.name) / "test.db")
        self.poll_id = db.create_poll(
            creator_id=100,
            title="Темы докладов",
            description="Выберите тему",
            chat_id=-100123,
            options=[("Тема A", 1), ("Тема B", 1), ("Тема C", 1)],
            default_max=1,
            allow_change=True,
            allow_cancel=True,
            show_participants=True,
            expires_at=None,
        )
        view = db.get_view(self.poll_id)
        assert view is not None
        self.options = [item.option for item in view.options]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_create_and_view(self) -> None:
        view = db.get_view(self.poll_id)
        assert view is not None
        self.assertEqual(view.total_options, 3)
        self.assertEqual(view.available_options, 3)
        self.assertEqual(view.total_bookings, 0)

    def test_book_same_and_change(self) -> None:
        first = db.book(self.poll_id, self.options[0].id, 1, "Алексей", "alex")
        self.assertEqual(first.code, "booked")

        same = db.book(self.poll_id, self.options[0].id, 1, "Алексей", "alex")
        self.assertEqual(same.code, "same")

        changed = db.book(self.poll_id, self.options[1].id, 1, "Алексей", "alex")
        self.assertEqual(changed.code, "changed")

        view = db.get_view(self.poll_id)
        assert view is not None
        self.assertEqual(view.options[0].taken, 0)
        self.assertEqual(view.options[1].taken, 1)
        self.assertEqual(view.total_bookings, 1)

    def test_full_option(self) -> None:
        db.book(self.poll_id, self.options[0].id, 1, "Первый", None)
        second = db.book(self.poll_id, self.options[0].id, 2, "Второй", None)
        self.assertEqual(second.code, "full")

        view = db.get_view(self.poll_id)
        assert view is not None
        self.assertEqual(view.options[0].taken, 1)

    def test_change_to_full_keeps_old_booking(self) -> None:
        db.book(self.poll_id, self.options[0].id, 1, "Первый", None)
        db.book(self.poll_id, self.options[1].id, 2, "Второй", None)

        result = db.book(self.poll_id, self.options[1].id, 1, "Первый", None)
        self.assertEqual(result.code, "full")

        view = db.get_view(self.poll_id)
        assert view is not None
        self.assertEqual(view.options[0].taken, 1)
        self.assertEqual(view.options[1].taken, 1)
        self.assertEqual(view.find_user_booking(1)[0].option.id, self.options[0].id)

    def test_cancel_then_other_user_books(self) -> None:
        db.book(self.poll_id, self.options[0].id, 1, "Первый", None)
        cancelled = db.cancel_booking(self.poll_id, 1)
        self.assertEqual(cancelled.code, "cancelled")
        self.assertEqual(db.cancel_booking(self.poll_id, 1).code, "no_booking")

        result = db.book(self.poll_id, self.options[0].id, 2, "Второй", None)
        self.assertEqual(result.code, "booked")

    def test_changing_forbidden(self) -> None:
        db.update_poll_settings(self.poll_id, 100, allow_change=False)
        db.book(self.poll_id, self.options[0].id, 1, "Первый", None)
        result = db.book(self.poll_id, self.options[1].id, 1, "Первый", None)
        self.assertEqual(result.code, "change_forbidden")

    def test_cancel_forbidden(self) -> None:
        db.update_poll_settings(self.poll_id, 100, allow_cancel=False)
        db.book(self.poll_id, self.options[0].id, 1, "Первый", None)
        self.assertEqual(db.cancel_booking(self.poll_id, 1).code, "cancel_forbidden")

    def test_closed_and_expired(self) -> None:
        db.set_status(self.poll_id, 100, "closed")
        self.assertEqual(
            db.book(self.poll_id, self.options[0].id, 1, "Первый", None).code,
            "inactive",
        )
        db.set_status(self.poll_id, 100, "active")

        past = (datetime.datetime.now() - datetime.timedelta(minutes=1)).isoformat(
            timespec="seconds"
        )
        conn = db.connect()
        try:
            conn.execute(
                "UPDATE poll_polls SET expires_at = ? WHERE id = ?",
                (past, self.poll_id),
            )
        finally:
            conn.close()

        result = db.book(self.poll_id, self.options[0].id, 1, "Первый", None)
        self.assertEqual(result.code, "expired")
        self.assertEqual(db.get_poll(self.poll_id).status, "expired")

    def test_concurrent_last_slot(self) -> None:
        barrier = threading.Barrier(16)
        results: list[str] = []
        lock = threading.Lock()

        def worker(user_id: int) -> None:
            barrier.wait()
            outcome = db.book(
                self.poll_id,
                self.options[2].id,
                user_id,
                f"Участник {user_id}",
                None,
            )
            with lock:
                results.append(outcome.code)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(results.count("booked"), 1)
        self.assertEqual(results.count("full"), 15)
        view = db.get_view(self.poll_id)
        assert view is not None
        self.assertEqual(view.options[2].taken, 1)

    def test_limit_and_bookings(self) -> None:
        db.set_option_limit(self.poll_id, 100, self.options[0].id, 3)
        for user_id in range(1, 4):
            self.assertEqual(
                db.book(self.poll_id, self.options[0].id, user_id, f"U{user_id}", None).code,
                "booked",
            )
        result = db.set_option_limit(self.poll_id, 100, self.options[0].id, 2)
        self.assertEqual(result.code, "below_taken")
        self.assertEqual(db.set_option_limit(self.poll_id, 100, self.options[0].id, 4).code, "ok")

    def test_owner_checks(self) -> None:
        self.assertEqual(
            db.update_poll_settings(self.poll_id, 999, title="Чужой"),
            "forbidden",
        )
        self.assertEqual(db.set_status(self.poll_id, 999, "closed"), "forbidden")
        code, _ = db.delete_poll(self.poll_id, 999)
        self.assertEqual(code, "forbidden")

    def test_delete_option_with_bookings(self) -> None:
        db.book(self.poll_id, self.options[0].id, 1, "Первый", "user")
        first = db.delete_option(self.poll_id, 100, self.options[0].id)
        self.assertEqual(first.code, "needs_confirm")
        self.assertEqual(len(first.bookings), 1)

        forced = db.delete_option(self.poll_id, 100, self.options[0].id, force=True)
        self.assertEqual(forced.code, "deleted")
        view = db.get_view(self.poll_id)
        assert view is not None
        self.assertEqual(view.total_options, 2)
        self.assertEqual(view.total_bookings, 0)

    def test_min_options(self) -> None:
        db.delete_option(self.poll_id, 100, self.options[0].id)
        result = db.delete_option(self.poll_id, 100, self.options[1].id)
        self.assertEqual(result.code, "min_options")

    def test_copy_and_delete(self) -> None:
        db.book(self.poll_id, self.options[0].id, 1, "Первый", None)
        code, copy_id = db.copy_poll(self.poll_id, 100)
        self.assertEqual(code, "ok")
        copy_view = db.get_view(copy_id)
        assert copy_view is not None
        self.assertEqual(copy_view.total_bookings, 0)
        self.assertEqual(copy_view.total_options, 3)
        self.assertTrue(copy_view.poll.title.endswith("(копия)"))

        code, poll = db.delete_poll(self.poll_id, 100)
        self.assertEqual(code, "ok")
        self.assertEqual(poll.id, self.poll_id)
        self.assertIsNone(db.get_view(self.poll_id))

    def test_expire_due_polls(self) -> None:
        second = db.create_poll(
            creator_id=100,
            title="Второй",
            description="",
            chat_id=None,
            options=[("A", 1), ("B", 1)],
            default_max=1,
            allow_change=True,
            allow_cancel=True,
            show_participants=True,
            expires_at="2000-01-01T00:00:00+03:00",
        )
        ids = db.expire_due_polls()
        self.assertIn(second, ids)
        self.assertEqual(db.get_poll(second).status, "expired")

    def test_persistence_after_reconnect(self) -> None:
        db.book(self.poll_id, self.options[0].id, 1, "Первый", "user")
        db.set_db_path(Path(self.tmp.name) / "test.db")
        view = db.get_view(self.poll_id)
        assert view is not None
        self.assertEqual(view.total_bookings, 1)

    def test_record_and_list_chats(self) -> None:
        db.record_chat(-100123, "Группа", "supergroup", "group", False)
        db.record_chat(-100456, "Вторая", "group", None, False)
        db.record_chat(42, "Личка", "private", None, False)
        chats = db.list_chat_targets()
        ids = {chat.chat_id for chat in chats}
        self.assertIn(-100123, ids)
        self.assertIn(-100456, ids)
        self.assertNotIn(42, ids)


if __name__ == "__main__":
    unittest.main()
