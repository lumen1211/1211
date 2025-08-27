import asyncio
import json
import logging
import sys
import os
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from core import AccountWorker

# Настройка логирования в файл и консоль
log_date_format = '%Y-%m-%d %H:%M:%S'
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)-8s - %(message)s',
    datefmt=log_date_format,
    handlers=[
        logging.FileHandler('farmer.log', mode='w', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)

async def async_input(prompt: str) -> str:
    """Асинхронный ввод с помощью отдельного потока."""
    return await asyncio.to_thread(input, prompt)

async def interactive_mode(worker: AccountWorker):
    """Интерактивный режим выбора кампании и канала."""
    print("\n--- Интерактивный режим ---")
    
    # 1. Получить инвентарь
    print("Получение списка кампаний...")
    await worker.fetch_inventory()
    
    if not worker.inventory:
        print("Активных кампаний не найдено.")
        return

    # 2. Показать список кампаний
    print("\n--- Доступные кампании ---")
    campaigns = [c for c in worker.inventory if c.active] # Показываем только активные
    if not campaigns:
        print("Нет активных кампаний.")
        return
        
    for i, campaign in enumerate(campaigns, 1):
        status = "✅" if campaign.can_earn() else "❌"
        game_name = campaign.game.name if campaign.game else "N/A"
        print(f"{i}. [{status}] {campaign.name} (Игра: {game_name})")

    # 3. Выбрать кампанию
    try:
        choice = int(await async_input(f"\nВыберите кампанию (1-{len(campaigns)}): ")) - 1
        if not 0 <= choice < len(campaigns):
            print("Неверный выбор.")
            return
        selected_campaign = campaigns[choice]
        print(f"Выбрана кампания: {selected_campaign.name}")
    except ValueError:
        print("Неверный ввод.")
        return

    # 4. Найти каналы для игры этой кампании
    print(f"\nПоиск каналов для игры: {selected_campaign.game.name}...")
    worker.settings["priority"] = [selected_campaign.game.name] # Устанавливаем приоритет
    await worker.fetch_channels()
    
    if not worker.channels:
        print("Каналы для этой игры не найдены.")
        return

    # 5. Показать список каналов
    print("\n--- Доступные каналы ---")
    channels_list = list(worker.channels.values())
    for i, channel in enumerate(channels_list, 1):
        print(f"{i}. {channel.display_name} ({channel.login})")

    # 6. Выбрать канал
    try:
        choice = int(await async_input(f"\nВыберите канал (1-{len(channels_list)}): ")) - 1
        if not 0 <= choice < len(channels_list):
            print("Неверный выбор.")
            return
        selected_channel = channels_list[choice]
        print(f"Выбран канал: {selected_channel.display_name}")
    except ValueError:
        print("Неверный ввод.")
        return

    # 7. Начать просмотр
    print("\nНачинаем просмотр...")
    worker.watch(selected_channel)
    
    # 8. Запустить цикл просмотра
    try:
        # Ждем завершения просмотра (или Ctrl+C для остановки)
        while worker.watching_channel and worker._is_running:
            await asyncio.sleep(10) # Проверяем каждые 10 секунд
            # Можно добавить вывод прогресса здесь, если нужно
    except KeyboardInterrupt:
        print("\nПросмотр остановлен пользователем.")
    finally:
        worker.stop_watching()
        print("Просмотр завершен.")

async def main():
    print("--- Twitch Drops Farmer vInteractive ---")

    Path("cookies").mkdir(exist_ok=True)

    try:
        with open('accounts.json', 'r', encoding='utf-8') as f:
            accounts_config = json.load(f)
    except FileNotFoundError:
        print("!!! FATAL: accounts.json not found!")
        return
    except json.JSONDecodeError as e:
        print(f"!!! FATAL: Ошибка в accounts.json: {e}")
        return

    active_accounts = [acc for acc in accounts_config if acc.get("enabled", False)]
    if not active_accounts:
        print("No enabled accounts found. Exiting.")
        return
    
    # Работаем только с первым аккаунтом для простоты
    worker = AccountWorker(active_accounts[0])
    
    try:
        # Инициализируем сессию
        await worker._initialize_session()
        print(f"Аккаунт авторизован. User ID: {worker.user_id}")

        # Запускаем интерактивный режим (ввод выполняется в отдельном потоке)
        await interactive_mode(worker)
        
    except Exception as e:
        logging.error(f"[{worker.username}] Ошибка: {e}", exc_info=True)
    finally:
        await worker.stop()
        print("--- Работа завершена. ---")

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nПрограмма прервана пользователем.")
    except Exception as e:
        print(f"\nКритическая ошибка: {e}")
        logging.error("Критическая ошибка", exc_info=True)
