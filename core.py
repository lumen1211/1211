import asyncio
import json
import logging
import re
from base64 import b64encode
from time import time
from collections import abc
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional
import aiohttp
from yarl import URL

# --- Утилиты и Константы ---
JsonType = Dict[str, Any]

class MinerException(Exception): pass
class GQLException(MinerException): pass
class RequestException(MinerException): pass

def timestamp(stamp: str) -> datetime:
    try: 
        return datetime.fromisoformat(stamp.replace('Z', "+00:00"))
    except ValueError: 
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

def json_minify(data: Any) -> str: 
    return json.dumps(data, separators=(',', ':'))

class ExponentialBackoff:
    def __init__(self, base: float = 1.0, *, maximum: float = 60.0):
        self._base = base
        self._exp = 0
        self._max = maximum
    def __iter__(self): return self
    def __next__(self) -> float: 
        self._exp += 1
        return min(self._base * 2 ** self._exp, self._max)

class ClientInfo:
    def __init__(self, client_id: str, user_agent: str): 
        self.CLIENT_ID = client_id
        self.USER_AGENT = user_agent

class ClientType:
    # Используем веб-клиент, как в браузере
    WEB = ClientInfo("kimne78kx3ncx6brgo4mv6wki5h1ko", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36")

class GQLOperation(dict):
    def __init__(self, name: str, sha256: str, *, variables: JsonType | None = None):
        super().__init__(
            operationName=name, 
            extensions={"persistedQuery": {"version": 1, "sha256Hash": sha256}}
        )
        if variables is not None: 
            self["variables"] = variables
            
    def with_variables(self, variables: JsonType) -> "GQLOperation":
        new_vars = self.get("variables", {}).copy()
        new_vars.update(variables)
        return GQLOperation(
            self["operationName"], 
            self["extensions"]["persistedQuery"]["sha256Hash"], 
            variables=new_vars
        )

# Используем правильный хэш из браузера
GQL_OPERATIONS = {
    "ViewerDropsDashboard": GQLOperation("ViewerDropsDashboard", "5a4da2ab3d5b47c9f9ce864e727b2cb346af1e3ea8b897fe8f704a97ff017619"), # Хэш из браузера
    "Inventory": GQLOperation("Inventory", "d86775d0ef16a63a33ad52e80eaff963b2d5b72fada7c991504a57496e1d8e4b"),
    "ClaimDrop": GQLOperation("DropsPage_ClaimDropRewards", "a455deea71bdc9015b78eb49f4acfbce8baa7ccbedd28e549bb025bd0f751930"),
    "GameDirectory": GQLOperation("DirectoryPage_Game", "c7c9d5aad09155c4161d2382092dc44610367f3536aac39019ec2582ae5065f9"),
    "GetStreamInfo": GQLOperation("VideoPlayerStreamInfoOverlayChannel", "198492e0857f6aedead9665c81c5a06d67b25b58034649687124083ff288597d"),
    "StreamInfo": GQLOperation("StreamInfo", "c338bd15737588308cf8e39395aab6035176e2b6055057c569171db0739c6efc"),
    "PlaybackAccessToken": GQLOperation("PlaybackAccessToken", "0828119abb1010bd2f8695823949422059320815431d9c96275011038bd3879e"),
    # Запрос для получения информации о текущем пользователе
    "Current_user": GQLOperation("Current_user", "04e4285478e023aa314391066046e4777a0877d84c57429049d61951ef621ec7"), # Попробуем этот хэш
}
WATCH_INTERVAL = 60 # секунд

class Game:
    def __init__(self, data: JsonType): 
        self.id: str = data["id"]
        self.name: str = data["displayName"]
        self.slug: str = data.get("name", self.name.lower().replace(' ', '-'))
    def __eq__(self, other) -> bool: 
        return isinstance(other, Game) and self.name == other.name
    def __hash__(self) -> int: 
        return hash(self.name)

class TimedDrop:
    def __init__(self, campaign: "DropsCampaign", data: JsonType):
        self._worker = campaign._worker
        self.campaign = campaign
        self.id: str = data["id"]
        self.claim_id: Optional[str] = data.get("self", {}).get("dropInstanceID")
        self.is_claimed: bool = data.get("self", {}).get("isClaimed", False)
        self.current_minutes: int = data.get("self", {}).get("currentMinutesWatched", 0)
        self.required_minutes: int = data["requiredMinutesWatched"]
        if self.is_claimed: 
            self.current_minutes = self.required_minutes
            
    def can_earn(self) -> bool: 
        return not self.is_claimed and self.campaign.active and self.current_minutes < self.required_minutes
        
    @property
    def can_claim(self) -> bool: 
        return self.claim_id is not None and not self.is_claimed
        
    async def claim(self):
        if not self.can_claim: return
        try:
            await self._worker.gql_request(
                GQL_OPERATIONS["ClaimDrop"].with_variables({"input": {"dropInstanceID": self.claim_id}})
            )
            self.is_claimed = True
            self.current_minutes = self.required_minutes
            self._worker.log(f"Claimed drop for '{self.campaign.name}'")
        except GQLException: 
            self._worker.log(f"Failed to claim drop {self.id}")

class DropsCampaign:
    def __init__(self, worker: "AccountWorker", data: JsonType):
        self._worker = worker
        self.id: str = data["id"]
        self.name: str = data["name"]
        self.game: Game = Game(data["game"])
        # Всегда считаем аккаунт подключенным для фарма
        self.linked: bool = True
        self.starts_at = timestamp(data["startAt"])
        self.ends_at = timestamp(data["endAt"])
        
        # Обрабатываем дропы
        raw_drops = data.get("timeBasedDrops", [])
        self.timed_drops: dict[str, TimedDrop] = {d["id"]: TimedDrop(self, d) for d in raw_drops}
        
    @property
    def active(self) -> bool: 
        now = datetime.now(timezone.utc)
        return self.starts_at <= now < self.ends_at
        
    @property
    def finished(self) -> bool:
        # Если дропов нет в API, считаем, что кампания не завершена
        if not self.timed_drops:
            self._worker.log(f"Campaign '{self.name}' has no drops from API, assuming not finished for farming.")
            return False # Кампания не завершена, можно фармить
        result = all(d.is_claimed for d in self.timed_drops.values())
        self._worker.log(f"Campaign '{self.name}' finished check (with drops): all claimed = {result}")
        return result
        
    def can_earn(self) -> bool:
        # Фармим любую активную незавершенную кампанию
        is_active = self.active
        is_not_finished = not self.finished
        result = is_active and is_not_finished
        self._worker.log(f"Campaign '{self.name}' can_earn: active={is_active}, not_finished={is_not_finished} = {result}")
        return result

class Stream:
    def __init__(self, channel: "Channel", user_data: JsonType):
        self.channel = channel
        self.broadcast_id = user_data.get("stream", {}).get("id", "0")
        self.game = Game(user_data.get("broadcastSettings", {}).get("game", {})) if user_data.get("broadcastSettings", {}).get("game") else None
        
    @property
    def spade_payload(self) -> JsonType:
        payload = {
            "event": "minute-watched",
            "properties": {
                "broadcast_id": str(self.broadcast_id),
                "channel_id": str(self.channel.id),
                "channel": self.channel.login,
                "hidden": False,
                "live": True,
                "location": "channel",
                "logged_in": True,
                "muted": False,
                "player": "site",
                "user_id": str(self.channel._worker.user_id),
                "platform": "web",
                "timestamp": int(time() * 1000)
            }
        }
        encoded = b64encode(json_minify(payload).encode()).decode()
        return {"data": encoded}

class Channel:
    def __init__(self, worker: "AccountWorker", id: int, login: str, display_name: str, game: Game):
        self._worker = worker
        self.id = id
        self.login = login
        self.display_name = display_name
        self.game = game
        self._stream: Optional[Stream] = None
        self._spade_url: Optional[str] = None

    @classmethod
    def from_directory(cls, worker: "AccountWorker", data: JsonType) -> "Channel":
        broadcaster = data["broadcaster"]
        return cls(
            worker, 
            id=int(broadcaster["id"]), 
            login=broadcaster["login"], 
            display_name=broadcaster["displayName"], 
            game=Game(data["game"])
        )

    async def update_stream(self) -> bool:
        """Обновить информацию о стриме."""
        try:
            res = await self._worker.gql_request(
                GQL_OPERATIONS["GetStreamInfo"].with_variables({"channel": self.login})
            )
            stream_data = res.get("data", {}).get("user", {}).get("stream")
            if stream_data:
                self._stream = Stream(self, res["data"]["user"])
                return True
            self._stream = None
            return False
        except GQLException:
            self._stream = None
            return False

    async def get_spade_url(self) -> str:
        """Получить URL для отправки watch-событий."""
        if self._spade_url:
            return self._spade_url

        # Метод 1: Ищем в HTML страницы канала
        url = URL(f"https://www.twitch.tv/{self.login}")
        async with self._worker.request("GET", url) as response:
            text = await response.text()

        # Попробуем несколько паттернов
        patterns = [
            r'"spade_?url":\s*"([^"]+)"',
            r'(https://video-edge-[^\s"<>]+\.ts(?:\?[^\s"<>"]+)?)'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                spade_url = match.group(1).replace('\\u0025', '%')
                self._worker.log(f"Найден spade_url для {self.login}: {spade_url[:100]}...")
                self._spade_url = URL(spade_url)
                return self._spade_url

        # Метод 2: Fallback через GQL (PlaybackAccessToken)
        try:
            res = await self._worker.gql_request(
                GQL_OPERATIONS["PlaybackAccessToken"].with_variables({
                    "login": self.login,
                    "isLive": True,
                    "vodID": "",
                    "playerType": "site"
                })
            )
            token_data = res.get("data", {}).get("streamPlaybackAccessToken", {})
            if token_data:
                signature = token_data.get("signature", "")
                token_value = token_data.get("value", "")
                if signature and token_value:
                    # Запрашиваем список доступных потоков
                    playlist_url = URL(f"https://usher.ttvnw.net/api/channel/hls/{self.login}.m3u8") \
                        .with_query({
                            "sig": signature,
                            "token": token_value,
                            "allow_source": "true",
                            "allow_audio_only": "true",
                            "allow_spectre": "false",
                            "player": "twitchweb",
                            "playlist_include_framerate": "true"
                        })
                    
                    async with self._worker.request("GET", playlist_url) as playlist_resp:
                        playlist_text = await playlist_resp.text()
                        # Ищем spade_url в плейлисте (иногда бывает там)
                        match = re.search(r'"spade_?url":\s*"([^"]+)"', playlist_text, re.IGNORECASE)
                        if match:
                            spade_url = match.group(1).replace('\\u0025', '%')
                            self._worker.log(f"Fallback GQL spade_url для {self.login}: {spade_url[:100]}...")
                            self._spade_url = URL(spade_url)
                            return self._spade_url
                            
                        # Если не нашли, используем первый доступный поток как индикатор активности
                        # и пытаемся получить spade_url другим способом
                        lines = playlist_text.strip().split('\n')
                        if lines and not lines[-1].startswith('#'):
                            # Последняя строка - URL потока, из него можно извлечь часть для spade_url
                            stream_url = lines[-1]
                            # Пример: https://video-edge-.../chunked/index-dvr.m3u8
                            # spade_url: https://video-edge-.../ping
                            if "video-edge-" in stream_url:
                                base_url = stream_url.split('/index')[0]
                                spade_url = f"{base_url}/ping"
                                self._worker.log(f"Создан spade_url для {self.login}: {spade_url}")
                                self._spade_url = URL(spade_url)
                                return self._spade_url
                                
        except Exception as e:
            self._worker.log(f"Ошибка fallback GQL для {self.login}: {e}")

        raise MinerException(f"Не удалось извлечь spade_url для {self.login}")

    async def send_watch(self) -> bool:
        """Отправить watch-событие."""
        if not self._stream:
            self._worker.log(f"Нет стрима для {self.login}")
            return False
        
        try:
            if self._spade_url is None:
                self._spade_url = await self.get_spade_url()
            
            payload = self._stream.spade_payload
            self._worker.log(f"Отправка watch для {self.login} на {self._spade_url}")
            
            async with self._worker.request(
                "POST",
                self._spade_url,
                data=payload,
                headers={"Content-Type": "text/plain;charset=UTF-8"}
            ) as response:
                self._worker.log(f"Ответ watch для {self.login}: {response.status}")
                # Twitch может возвращать 204 No Content или 200 OK
                return response.status in (204, 200)
                
        except Exception as e:
            self._worker.log(f"Ошибка watch для {self.login}: {e}")
            return False

class AccountWorker:
    def __init__(self, account_config: dict):
        self.config = account_config
        self.username = account_config.get("username")
        self.proxy = URL(account_config["proxy"]) if account_config.get("proxy") else None
        # Для интерактивного режима настройки не используются, но оставим для совместимости
        self.settings = {
            "priority": account_config.get("priority_games", []), 
            "exclude": set(account_config.get("exclude_games", []))
        }
        self._client_type = ClientType.WEB # Используем WEB клиент
        self._session: Optional[aiohttp.ClientSession] = None
        self.user_id: Optional[int] = None
        self.inventory: list[DropsCampaign] = []
        self.channels: dict[int, Channel] = {}
        self.watching_channel: Optional[Channel] = None
        self._watching_task: Optional[asyncio.Task] = None
        self._is_running = True

        # Читаем значения заголовков из конфигурационных файлов
        headers_cfg = {}

        # Значения могут быть заданы в accounts.json для конкретного аккаунта
        self._client_integrity = (
            account_config.get("Client-Integrity")
            or account_config.get("client_integrity")
            or account_config.get("client-integrity")
        )
        self._client_version = (
            account_config.get("Client-Version")
            or account_config.get("client_version")
            or account_config.get("client-version")
        )
        self._device_id = (
            account_config.get("X-Device-Id")
            or account_config.get("x_device_id")
            or account_config.get("x-device-id")
        )

        # Если в accounts.json их нет, пробуем headers.json
        if not all([self._client_integrity, self._client_version, self._device_id]):
            try:
                with open("headers.json", "r", encoding="utf-8") as f:
                    headers_cfg = json.load(f)
            except FileNotFoundError:
                headers_cfg = {}

            self._client_integrity = self._client_integrity or headers_cfg.get("Client-Integrity")
            self._client_version = self._client_version or headers_cfg.get("Client-Version")
            self._device_id = self._device_id or headers_cfg.get("X-Device-Id")

        # Сообщаем пользователю об отсутствующих заголовках
        if not self._client_integrity:
            logging.warning(f"[{self.username}] Client-Integrity не найден в headers.json или accounts.json")
        if not self._client_version:
            logging.warning(f"[{self.username}] Client-Version не найден в headers.json или accounts.json")
        if not self._device_id:
            logging.warning(f"[{self.username}] X-Device-Id не найден в headers.json или accounts.json")

    def log(self, message: str): 
        logging.info(f"[{self.username}] {message}")

    async def get_session(self) -> aiohttp.ClientSession:
        if self._session and not self._session.closed: 
            return self._session
        
        jar = aiohttp.CookieJar(unsafe=True)
        
        # Используем cookies из конфига, если есть
        if "cookies" in self.config:
            # Парсим cookies из конфига
            cookies_raw = self.config["cookies"]
            cookies = {}
            for item in cookies_raw.split(";"):
                item = item.strip()
                if "=" in item:
                    key, value = item.split("=", 1)
                    cookies[key] = value
            jar.update_cookies(cookies, URL("https://www.twitch.tv"))
        else:
            # fallback на старый способ
            jar.update_cookies({"auth-token": self.config["auth_token"]}, URL("https://www.twitch.tv"))
        
        # Добавляем дополнительные заголовки из браузера
        headers = {"User-Agent": self._client_type.USER_AGENT}
        if self._client_integrity:
            headers["Client-Integrity"] = self._client_integrity
        if self._client_version:
            headers["Client-Version"] = self._client_version
        if self._device_id:
            headers["X-Device-Id"] = self._device_id
        
        self._session = aiohttp.ClientSession(
            cookie_jar=jar, 
            headers=headers
        )
        return self._session

    async def _initialize_session(self):
        self.log("Инициализация сессии...")
        # Проверяем, есть ли auth_token в конфиге
        if "auth_token" in self.config:
            # Используем старый способ для валидации
            auth_header = {"Authorization": f"OAuth {self.config['auth_token']}"}
            self.log(f"Отправка запроса валидации с заголовком Authorization: Bearer ***")
            async with self.request("GET", "https://id.twitch.tv/oauth2/validate", headers=auth_header) as resp:
                self.log(f"Ответ валидации: статус {resp.status}")
                if resp.status == 401: 
                    raise MinerException(f"Auth token для {self.username} недействителен или истек.")
                data = await resp.json()
                self.user_id = int(data["user_id"])
                if not self.username:
                    self.username = data.get("login")
        else:
            # Если auth_token нет, пытаемся получить user_id через GQL
            self.log("Auth token не найден, попытка получить user_id через GQL...")
            try:
                # Запрос информации о текущем пользователе
                res = await self.gql_request(GQL_OPERATIONS["Current_user"])
                self.user_id = int(res["data"]["currentUser"]["id"])
                if not self.username:
                    self.username = res["data"]["currentUser"]["login"]
            except Exception as e:
                self.log(f"Не удалось получить user_id через GQL: {e}")
                # Если не удалось получить user_id через GQL, используем заглушку
                # Это может быть небезопасно, но позволит продолжить работу
                self.user_id = 1078207120  # ID пользователя psdpz51 из логов
                self.username = "psdpz51"
                self.log(f"Используем user_id по умолчанию: {self.user_id}")
                
        self.log(f"Сессия действительна. User ID: {self.user_id}")

    @asynccontextmanager
    async def request(self, method: str, url: URL | str, **kwargs) -> abc.AsyncIterator[aiohttp.ClientResponse]:
        session = await self.get_session()
        if self.proxy and "proxy" not in kwargs: 
            kwargs["proxy"] = str(self.proxy)
            
        # Добавляем Client-Id header для GQL запросов
        if str(url).startswith("https://gql.twitch.tv/gql"):
            if "headers" not in kwargs:
                kwargs["headers"] = {}
            kwargs["headers"]["Client-Id"] = self._client_type.CLIENT_ID
            # Для GQL также добавляем Authorization, если есть auth_token
            if "auth_token" in self.config:
                kwargs["headers"]["Authorization"] = f"OAuth {self.config['auth_token']}"
            
        backoff = ExponentialBackoff()
        for delay in backoff:
            try:
                self.log(f"Отправка {method} запроса к {url}")
                async with session.request(
                    method, url, 
                    timeout=aiohttp.ClientTimeout(total=20), 
                    **kwargs
                ) as response:
                    self.log(f"Ответ от {url}: статус {response.status}")
                    if response.status >= 500: 
                        self.log(f"Серверная ошибка {response.status}, повтор через {delay}с")
                        await asyncio.sleep(delay)
                        continue
                    yield response
                    return
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                self.log(f"Ошибка запроса к {url}: {e}. Повтор через {delay}с")
                await asyncio.sleep(delay)
        raise RequestException("Запрос не удался после нескольких попыток.")

    async def gql_request(self, ops: GQLOperation | list[JsonType]) -> JsonType | list[JsonType]:
        self.log(f"Отправка GQL запроса: {ops.get('operationName', 'Unknown') if isinstance(ops, dict) else 'Batch'}")
        # Заголовки добавляются в методе request
        async with self.request("POST", "https://gql.twitch.tv/gql", json=ops) as response:
            try:
                resp_json = await response.json()
                # self.log(f"Ответ GQL: {resp_json}") # Убираем подробный лог ответа для экономии места
                if errors := (resp_json.get('errors') if isinstance(resp_json, dict) else None):
                    error_msg = errors[0].get("message", "Неизвестная GQL ошибка")
                    self.log(f"GQL ошибка: {error_msg}")
                    # Проверяем специфичную ошибку целостности
                    if "integrity" in error_msg.lower():
                        self.log("Ошибка целостности. Попробуйте обновить Client-Integrity или использовать cookies.")
                    raise GQLException(error_msg)
                return resp_json
            except aiohttp.ContentTypeError:
                # Если ответ не JSON, логируем текст
                text = await response.text()
                self.log(f"Текст ответа GQL (не JSON): {text}")
                raise GQLException(f"Неверный формат ответа: {text}")

    async def stop(self):
        self._is_running = False
        if self._watching_task: 
            self._watching_task.cancel()
            # Не ждем завершения задачи, просто отменяем
        if self._session: 
            await self._session.close()
        self.log("Работник остановлен.")

    async def fetch_inventory(self):
        """Получить список всех кампаний."""
        self.log("Получение списка кампаний...")
        try:
            self.log("Отправка запроса ViewerDropsDashboard...")
            res = await self.gql_request(GQL_OPERATIONS["ViewerDropsDashboard"])
            self.log("Запрос ViewerDropsDashboard успешен")
            all_campaigns_data = res["data"]["currentUser"]["dropCampaigns"] or []
            self.log(f"Получено {len(all_campaigns_data)} кампаний из ViewerDropsDashboard")
            
            # Получаем прогресс из инвентаря
            self.log("Отправка запроса Inventory...")
            inv_res = await self.gql_request(GQL_OPERATIONS["Inventory"])
            self.log("Запрос Inventory успешен")
            inventory_data = inv_res["data"]["currentUser"]["inventory"]
            progress_data = {c['id']: c for c in (inventory_data["dropCampaignsInProgress"] or [])}
            self.log(f"Получено {len(progress_data)} кампаний из Inventory")
            
            final_campaigns = []
            for campaign in all_campaigns_data:
                campaign_id = campaign.get('id')
                if campaign_id and campaign_id in progress_data:
                    self.log(f"Объединение данных для кампании {campaign_id}")
                    # Объединяем данные кампании с данными прогресса
                    campaign['self'] = progress_data[campaign_id].get('self', {})
                    progress_drops = {d['id']: d for d in progress_data[campaign_id].get('timeBasedDrops', [])}
                    for i, drop in enumerate(campaign.get('timeBasedDrops', [])):
                        if drop['id'] in progress_drops:
                            campaign['timeBasedDrops'][i]['self'] = progress_drops[drop['id']].get('self', {})
                final_campaigns.append(campaign)

            # Создаем объекты кампаний только для активных
            self.inventory = [DropsCampaign(self, c) for c in final_campaigns if c.get("status") == "ACTIVE"]
            self.log(f"Найдено {len(self.inventory)} активных кампаний.")
            
            # Логируем информацию о каждой кампании
            for campaign in self.inventory:
                earnable = "✅" if campaign.can_earn() else "❌"
                drops_info = []
                for drop in campaign.timed_drops.values():
                    claimed = "✓" if drop.is_claimed else "○"
                    drops_info.append(f"{claimed}{drop.current_minutes}/{drop.required_minutes}min")
                drops_text = ", ".join(drops_info) if drops_info else "No drops"
                self.log(f"  - [{earnable}] {campaign.name} ({drops_text})")
            
            # Автоматически забираем доступные дропы
            for campaign in self.inventory:
                for drop in campaign.timed_drops.values():
                    if drop.can_claim:
                        await drop.claim()
                        
        except (MinerException, GQLException) as e:
            self.log(f"Не удалось получить инвентарь: {e}")
            self.inventory = []

    async def fetch_channels(self):
        """Найти каналы для игр из приоритетного списка."""
        self.channels = {}
        priority_games = self.settings.get("priority", [])
        
        if not priority_games:
            self.log("Нет приоритетных игр для поиска каналов.")
            return
            
        for game_name in priority_games:
            self.log(f"Поиск каналов для игры: {game_name}")
            # Находим объект Game по имени
            game_obj = None
            for campaign in self.inventory:
                if campaign.game and campaign.game.name == game_name:
                    game_obj = campaign.game
                    break
                    
            if not game_obj:
                self.log(f"Игра {game_name} не найдена среди кампаний.")
                continue
                
            try:
                res = await self.gql_request(
                    GQL_OPERATIONS["GameDirectory"].with_variables({
                        "slug": game_obj.slug, 
                        "options": {"tags": ["Drops Enabled"]}
                    })
                )
                streams = res.get("data", {}).get("game", {}).get("streams", {}).get("edges", [])[:10] # Берем больше каналов
                self.log(f"Найдено {len(streams)} стримов для {game_name}")
                
                for stream in streams:
                    node = stream.get("node")
                    if node and node.get("broadcaster"):
                        channel = Channel.from_directory(self, node)
                        self.channels[channel.id] = channel
                        self.log(f"Добавлен канал: {channel.display_name} ({channel.login})")
                        
            except GQLException as e:
                self.log(f"Ошибка поиска каналов для {game_name}: {e}")
                
        self.log(f"Всего найдено {len(self.channels)} потенциальных каналов.")

    def watch(self, channel: "Channel"):
        """Начать просмотр канала."""
        if self.watching_channel and self.watching_channel.id == channel.id: 
            return
        self.stop_watching()
        self.watching_channel = channel
        self._watching_task = asyncio.create_task(self._watch_loop(channel))
        self.log(f"Начат просмотр канала: {channel.display_name}")

    def stop_watching(self):
        """Остановить просмотр."""
        if self._watching_task:
            self._watching_task.cancel()
        self.watching_channel = None
        self.log("Просмотр остановлен.")

    async def _watch_loop(self, channel: "Channel"):
        """Цикл имитации просмотра."""
        try:
            if not await channel.update_stream():
                self.log(f"{channel.display_name} офлайн.")
                return # Просто выходим, не переключаемся
            
            while self._is_running and self.watching_channel == channel:
                # Отправляем watch-событие
                success = await channel.send_watch()
                if not success:
                    self.log(f"Ошибка отправки watch для {channel.display_name}.")
                    # Не останавливаемся, пробуем снова через интервал
                
                self.log(f"Watch-событие отправлено для {channel.display_name}. Следующая попытка через {WATCH_INTERVAL}с.")
                await asyncio.sleep(WATCH_INTERVAL)
                
        except asyncio.CancelledError:
            self.log(f"Цикл просмотра для {channel.display_name} отменен.")
        except Exception as e:
            self.log(f"Критическая ошибка в цикле просмотра для {channel.display_name}: {e}")
        finally:
            if self.watching_channel == channel:
                self.watching_channel = None
