import asyncio
import json
from playwright.async_api import async_playwright

async def main():
    headers_found = asyncio.Future()

    async def handle_route(route):
        request = route.request
        try:
            if "gql.twitch.tv/gql" in request.url and request.method == "POST":
                all_headers = request.headers
                client_integrity = all_headers.get("client-integrity")
                client_version = all_headers.get("client-version")
                if client_integrity and client_version and not headers_found.done():
                    print("\n✓ УСПЕХ! Необходимые заголовки перехвачены!")
                    print("Теперь вы можете закрыть и браузер, и этот скрипт (Ctrl+C).")
                    headers_found.set_result({
                        "Client-Integrity": client_integrity,
                        "Client-Version": client_version
                    })
            await route.continue_()
        except Exception:
            if not route.is_handled():
                await route.continue_()

    print("--- Помощник перехвата заголовков из вашего браузера MS Edge ---")
    print("\nИНСТРУКЦИЯ:")
    print("1. Полностью закройте ВСЕ окна Microsoft Edge.")
    print("2. Запустите файл start_edge.bat. Откроется новое окно Edge.")
    print("3. В открывшемся окне зайдите на twitch.tv и войдите в свой аккаунт.")
    print("4. Этот скрипт автоматически перехватит данные.")
    print("-" * 60)

    async with async_playwright() as p:
        try:
            print("\nПытаюсь подключиться к вашему браузеру MS Edge (порт 9222)...")
            # Подключаемся к уже запущенному вами браузеру
            browser = await p.chromium.connect_over_cdp("http://localhost:9222")
            context = browser.contexts[0]
            # Если в браузере не открыто ни одной вкладки, создаем новую
            page = context.pages[0] if context.pages else await context.new_page()
            print("✓ Успешно подключился к браузеру!")
        except Exception:
            print("\n!!! ОШИБКА: Не удалось подключиться к MS Edge.")
            print("Пожалуйста, убедитесь, что вы закрыли все окна Edge и запустили start_edge.bat.")
            return

        await page.route("**/*", handle_route)
        print("\nСлушаю сетевую активность... Пожалуйста, войдите в Twitch в открытом окне.")

        found_headers = await headers_found

        with open("headers.json", "w") as f:
            json.dump(found_headers, f, indent=2)
        
        print("\n✓ Заголовки успешно сохранены в файл headers.json!")
        print("--- Работа помощника завершена. ---")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nПроцесс прерван пользователем.")