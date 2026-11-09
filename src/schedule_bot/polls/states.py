"""Состояния FSM для сценариев опросов."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class CreatePoll(StatesGroup):
    title = State()
    description = State()
    options = State()
    settings = State()
    manual_chat = State()
    preview = State()


class ManagePoll(StatesGroup):
    rename = State()
    add_options = State()
