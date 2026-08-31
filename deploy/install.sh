#!/bin/bash
# Скрипт первоначальной установки бота на VPS (Ubuntu/Debian)
set -e

echo "=== Установка бота расписания МИИГАиК ==="

# 1. Обновление системы
echo "[1/7] Обновление пакетов..."
sudo apt update && sudo apt upgrade -y

# 2. Установка Python и зависимостей
echo "[2/7] Установка Python..."
sudo apt install -y python3 python3-venv python3-pip git

# 3. Установка uv (менеджер пакетов)
echo "[3/7] Установка uv..."
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"

# 4. Создание пользователя для бота
echo "[4/7] Создание пользователя schedulebot..."
sudo useradd -r -s /bin/false schedulebot 2>/dev/null || true

# 5. Копирование проекта
echo "[5/7] Настройка проекта..."
sudo mkdir -p /opt/schedule_bot
sudo cp -r . /opt/schedule_bot/
sudo chown -R schedulebot:schedulebot /opt/schedule_bot

# 6. Установка зависимостей
echo "[6/7] Установка зависимостей Python..."
cd /opt/schedule_bot
sudo -u schedulebot uv sync

# 7. Установка systemd-сервиса
echo "[7/7] Настройка автозапуска..."
sudo cp deploy/schedule-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable schedule-bot

echo ""
echo "=== Установка завершена! ==="
echo ""
echo "Следующие шаги:"
echo "  1. Отредактируйте .env:  sudo nano /opt/schedule_bot/.env"
echo "  2. Запустите бота:       sudo systemctl start schedule-bot"
echo "  3. Проверьте статус:     sudo systemctl status schedule-bot"
echo "  4. Посмотрите логи:      sudo journalctl -u schedule-bot -f"
