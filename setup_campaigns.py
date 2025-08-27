import json
import asyncio
import aiohttp
import os

CAMPAIGNS_QUERY = {
    "operationName": "ViewerDropsDashboard",
    "variables": {"fetchRewardCampaigns": False},
    "extensions": {
        "persistedQuery": {
            "version": 1,
            "sha256Hash": "5a4da2ab3d5b47c9f9ce864e727b2cb346af1e3ea8b897fe8f704a97ff017619",
        }
    },
}

async def get_active_campaigns(headers):
    print("Подключаюсь к Twitch для получения списка активных кампаний...")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://gql.twitch.tv/gql",
                json=CAMPAIGNS_QUERY,
                headers=headers,
            ) as response:
                if response.status != 200:
                    print(f"Ошибка: Twitch вернул статус {response.status}")
                    return None
                data = await response.json()
                
                if data.get("errors"):
                    error_message = data["errors"][0].get("message", "Неизвестная ошибка")
                    # Эта ошибка больше не должна появляться
                    if error_message == 'failed integrity check':
                         print("!!! КРИТИЧЕСКАЯ ОШИБКА: Проверка Client-Integrity не пройдена.")
                         print("Пожалуйста, попробуйте заново запустить get_headers.py, чтобы обновить headers.json.")
                    else:
                        print(f"Ошибка от Twitch API: {error_message}")
                    return None

                campaigns_data = data.get("data", {}).get("currentUser", {}).get("dropCampaigns")
                
                if campaigns_data is None:
                    print("Не удалось найти данные о кампаниях. Ответ от сервера:")
                    print(data)
                    return []

                active_campaigns = []
                for camp in campaigns_data:
                    if camp.get("status") == "ACTIVE":
                        game_name = camp.get("game", {}).get("displayName", "Неизвестная игра")
                        campaign_name = camp.get("name", "Без названия")
                        if game_name not in [c['game'] for c in active_campaigns]:
                            active_campaigns.append({"game": game_name, "campaign_name": campaign_name})
                
                print("✓ Список кампаний получен!")
                return active_campaigns
    except Exception as e:
        print(f"Произошла ошибка при получении кампаний: {e}")
        return None

def display_campaigns(campaigns):
    print("\n--- СПИСОК АКТИВНЫХ КАМПАНИЙ ДЛЯ ФАРМА ---")
    if not campaigns:
        print("В данный момент нет активных кампаний с дропсами.")
        return
    for i, camp in enumerate(campaigns, 1):
        print(f"  [{i}] {camp['game']} ({camp['campaign_name']})")
    print("--------------------------------------------")

def display_accounts(accounts):
    print("\n--- ВАШИ АККАУНТЫ ---")
    for i, acc in enumerate(accounts, 1):
        status = "ВКЛ" if acc.get("enabled") else "ВЫКЛ"
        priority = acc.get('priority_games', [])
        priority_str = ", ".join(priority) if priority else "Любая кампания"
        print(f"  [{i}] {acc['username']} (Статус: {status}, Приоритет: {priority_str})")
    print("-----------------------")

def get_user_choice(prompt, max_value):
    while True:
        try:
            user_input = input(prompt)
            if not user_input: return []
            choices = [int(i.strip()) for i in user_input.split(',')]
            if all(1 <= i <= max_value for i in choices): return choices
            else: print(f"Ошибка: введите числа от 1 до {max_value}.")
        except ValueError: print("Ошибка: введите числа, разделенные запятой.")

async def main():
    # --- ГЛАВНОЕ ИЗМЕНЕНИЕ: Читаем ОБА файла, чтобы собрать полный набор "документов" ---
    try:
        with open('accounts.json', 'r', encoding='utf-8') as f:
            accounts = json.load(f)
        first_enabled_account = next((acc for acc in accounts if acc.get("enabled")), None)
        if not first_enabled_account:
            print("!!! ОШИБКА: В файле accounts.json нет ни одного включенного аккаунта.")
            return
        auth_token = first_enabled_account.get("auth_token")

        with open('headers.json', 'r') as f:
            headers_from_file = json.load(f)

        # Собираем все заголовки вместе
        auth_headers = {
            "Client-Id": "kimne78kx3ncx6brgo4mv6wki5h1ko", # Стандартный веб-клиент
            "Authorization": f"OAuth {auth_token}",
            **headers_from_file
        }
    except FileNotFoundError as e:
        print(f"!!! ОШИБКА: Не найден необходимый файл: {e.filename}")
        print("Пожалуйста, убедитесь, что файлы accounts.json и headers.json находятся в той же папке.")
        return
    except (json.JSONDecodeError, StopIteration):
        print("Ошибка: не удалось прочитать accounts.json или он пустой.")
        return

    campaigns = await get_active_campaigns(auth_headers)
    
    if not campaigns:
        print("\nЗавершение работы, так как не удалось получить список кампаний.")
        return

    while True:
        display_campaigns(campaigns)
        display_accounts(accounts)

        print("\nШаг 1: Выберите приоритетные кампании для фарма.")
        priority_indices = get_user_choice(f"Введите номера приоритетных кампаний (1-{len(campaigns)}): ", len(campaigns))
        priority_games = [campaigns[i-1]['game'] for i in priority_indices]

        if priority_games: print(f"\nВыбранный приоритет: {' -> '.join(priority_games)}")
        else: print("\nВыбран режим фарма любой доступной кампании.")

        print("\nШаг 2: Выберите аккаунты, к которым применить эти настройки.")
        account_indices = get_user_choice(f"Введите номера аккаунтов (1-{len(accounts)}): ", len(accounts))
        
        if not account_indices:
            print("Не выбрано ни одного аккаунта. Попробуем еще раз.")
            continue

        for i in account_indices: accounts[i-1]['priority_games'] = priority_games
        
        print("\n✓ Настройки применены к выбранным аккаунтам!")
        with open('accounts.json', 'w', encoding='utf-8') as f:
            json.dump(accounts, f, indent=2, ensure_ascii=False)
        print("✓ Файл accounts.json успешно сохранен!")

        if input("\nНастроить другую группу аккаунтов? (y/n): ").lower() != 'y': break

    print("\nНастройка завершена. Теперь вы можете запускать основной скрипт main.py.")

if __name__ == "__main__":
    asyncio.run(main())