import json
import asyncio
import logging
import os
import sys
import io
import random
import re
from datetime import datetime, timezone
import discord
from discord import app_commands
from discord.ext import commands
from yt_dlp import YoutubeDL
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import hashlib
from pathlib import Path


# Создаем кастомный обработчик для корректной обработки Unicode
class UnicodeStreamHandler(logging.StreamHandler):
    def emit(self, record):
        try:
            msg = self.format(record)
            # Кодируем в UTF-8 и заменяем нечитаемые символы
            if hasattr(sys.stdout, 'buffer'):
                # Для бинарного вывода
                sys.stdout.buffer.write((msg + self.terminator).encode('utf-8', errors='replace'))
                sys.stdout.buffer.flush()
            else:
                # Для текстового вывода
                sys.stdout.write(msg + self.terminator)
                sys.stdout.flush()
        except Exception:
            self.handleError(record)


# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        UnicodeStreamHandler()
    ]
)


def safe_log_info(message):
    """Безопасное логирование с обработкой Unicode"""
    try:
        # Преобразуем в строку и заменяем проблемные символы
        safe_message = str(message).encode('utf-8', errors='replace').decode('utf-8')
        logging.info(safe_message)
    except Exception as e:
        # Фолбэк на простой вывод
        print(f"LOG: {message}")


class BotConfig:
    def __init__(self, config_data):
        self.token = config_data["Token"]
        self.application_id = config_data["ApplicationId"]
        self.voice_channel_id = config_data["VoiceChannelId"]

        # PlaylistUrls
        playlist_urls = config_data["PlaylistUrls"]
        self.playlist_urls = {
            "Youtube": playlist_urls["Youtube"],
            "Spotify": playlist_urls["Spotify"],
            "SoundCloud": playlist_urls["SoundCloud"]
        }

        # BotSettings
        bot_settings = config_data["BotSettings"]
        self.command_prefix = bot_settings["CommandPrefix"]
        self.max_retries = bot_settings["MaxRetries"]
        self.retry_delay = bot_settings["RetryDelay"]
        self.skip_cooldown = bot_settings["SkipCooldown"]
        self.spotify_client_id = bot_settings["SpotifyClientId"]
        self.spotify_client_secret = bot_settings["SpotifyClientSecret"]
        self.cache_enabled = bot_settings["CacheEnabled"]
        self.cache_dir = bot_settings["CacheDir"]

        # YoutubeDLSettings
        yt_settings = config_data["YoutubeDLSettings"]
        self.ydl_format = yt_settings["Format"]
        self.ydl_quality = yt_settings["Quality"]
        self.ydl_user_agent = yt_settings["UserAgent"]
        self.ydl_cookies = yt_settings["CookiesFromBrowser"]
        self.ydl_cookie_file = yt_settings["CookieFile"]
        self.ydl_audio_format = yt_settings["AudioFormat"]
        self.ydl_throttled_rate = yt_settings["ThrottledRate"]
        self.ydl_mark_watched = yt_settings["MarkWatched"]

        ffmpeg_settings = config_data["FFmpegSettings"]
        self.ffmpeg_path = ffmpeg_settings["ExecutablePath"]
        self.ffmpeg_options = ffmpeg_settings["Options"]


class MusicCog(commands.Cog):
    def __init__(self, bot, config):
        self.bot = bot
        self.config = config
        self.voice_client = None
        self.current_song = None
        self.full_playlist = []
        self.current_position = 0
        self.is_playing = False
        self._manual_skip = False
        self.bot_start_time = datetime.now(timezone.utc)
        self.retry_count = 0
        self.last_skip_time = 0
        self.is_loading = False
        self._init_ytdl()
        self.spotify = spotipy.Spotify(
            auth_manager=SpotifyClientCredentials(
                client_id=self.config.spotify_client_id,
                client_secret=self.config.spotify_client_secret
            )
        ) if self.config.spotify_client_id and self.config.spotify_client_secret else None
        self._ensure_cache_dir()
        self._play_lock = asyncio.Lock()

    def _ensure_cache_dir(self):
        if self.config.cache_enabled and not Path(self.config.cache_dir).exists():
            Path(self.config.cache_dir).mkdir(parents=True, exist_ok=True)

    def _get_playlist_cache_key(self, playlist_url):
        return hashlib.md5(playlist_url.encode()).hexdigest()

    def _load_from_cache(self, cache_key):
        cache_file = Path(self.config.cache_dir) / f"{cache_key}.json"
        if cache_file.exists():
            with open(cache_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        return None

    def _save_to_cache(self, cache_key, data):
        cache_file = Path(self.config.cache_dir) / f"{cache_key}.json"
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def reset_state(self):
        try:
            if self.voice_client:
                if self.voice_client.is_playing():
                    self.voice_client.stop()

                # Безопасная проверка и завершение FFmpeg процесса
                if hasattr(self.voice_client, '_player') and self.voice_client._player:
                    try:
                        if hasattr(self.voice_client._player, 'terminate'):
                            self.voice_client._player.terminate()
                        elif hasattr(self.voice_client._player, 'kill'):
                            self.voice_client._player.kill()
                    except (AttributeError, ProcessLookupError):
                        pass  # Игнорируем ошибки если процесс уже завершен

                # Асинхронное отключение
                if self.voice_client.is_connected():
                    asyncio.create_task(self.voice_client.disconnect(force=True))
        except Exception as e:
            safe_log_info(f"Ошибка в reset_state: {e}")

    def _init_ytdl(self):
        self.ydl_opts = {
            'format': 'bestaudio/best',
            'quiet': True,
            'no_warnings': False,
            'default_search': 'auto',
            'ignoreerrors': True,
            'extract_flat': False,
            'live_from_start': True,
            'cachedir': False,
            'force_generic_extractor': True,
            'socket_timeout': 30,
            'noplaylist': True,
            'source_address': '0.0.0.0',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': self.config.ydl_audio_format,
                'preferredquality': self.config.ydl_quality,
            }],
            'http_headers': {'User-Agent': self.config.ydl_user_agent},
            'mark_watched': self.config.ydl_mark_watched,
            'throttledratelimit': self.config.ydl_throttled_rate,
        }
        if os.path.exists(self.config.ydl_cookie_file):
            self.ydl_opts['cookiefile'] = self.config.ydl_cookie_file

    async def connect_to_voice(self):
        try:
            channel = self.bot.get_channel(self.config.voice_channel_id)
            if not isinstance(channel, discord.VoiceChannel):
                return False
            if self.voice_client:
                await self.voice_client.disconnect(force=True)
            self.voice_client = await channel.connect(timeout=30.0, reconnect=True)
            return True
        except Exception as e:
            safe_log_info(f"Ошибка подключения: {e}")
            await asyncio.sleep(5)
            return False

    async def load_playlist(self, url, interaction=None):
        self.is_loading = True

        cache_key = self._get_playlist_cache_key(url)
        cached_data = self._load_from_cache(cache_key) if self.config.cache_enabled else None

        if interaction:
            msg = await interaction.followup.send("🔍 Проверяем кеш..." if cached_data else "🔍 Загружаем плейлист...")

        try:
            if cached_data:
                self.full_playlist = cached_data['tracks']
                if interaction:
                    await msg.edit(content=f"✅ Загружено {len(self.full_playlist)} треков из кеша")
                return True

            info = await self.run_ydl_extract(url)
            if not info:
                if interaction:
                    await interaction.followup.send("❌ yt-dlp ничего не вернул")
                return False

            entries = info.get('entries') or [info]
            self.full_playlist.clear()

            for e in entries:
                if not e or e.get('is_unavailable'):
                    continue

                track_page = e.get('webpage_url') or e.get('url')
                if not track_page or not track_page.startswith("http"):
                    safe_log_info(f"Пропущен некорректный трек: {e.get('title')}")
                    continue

                self.full_playlist.append({
                    'url': track_page,
                    'title': e.get('title', 'Без названия'),
                    'original_url': track_page,
                    'duration': e.get('duration', 0)
                })

            if self.config.cache_enabled:
                self._save_to_cache(cache_key, {
                    'url': url,
                    'last_updated': datetime.now(timezone.utc).isoformat(),
                    'tracks': self.full_playlist
                })

            if interaction:
                await msg.edit(content=f"✅ Загружено {len(self.full_playlist)} треков")
            return True

        except Exception as exc:
            safe_log_info("Ошибка load_playlist", exc_info=True)
            if interaction:
                await interaction.followup.send(f"❌ Ошибка загрузки: {exc}")
            return False
        finally:
            self.is_loading = False

    async def load_spotify_playlist(self, url, interaction=None):
        if not self.spotify:
            if interaction: await interaction.followup.send("❌ Spotify не настроен")
            return False

        cache_key = self._get_playlist_cache_key(url)
        cached_data = self._load_from_cache(cache_key) if self.config.cache_enabled else None

        if interaction:
            msg = await interaction.followup.send(
                "🔍 Проверяем кеш..." if cached_data else "🔍 Первая загрузка плейлиста, это может занять время...")

        self.is_loading = True
        try:
            if cached_data:
                self.full_playlist = cached_data['tracks']
                if interaction:
                    await msg.edit(content=f"✅ Загружено {len(self.full_playlist)} треков из кеша")
                return True

            playlist_id = url.split('/')[-1].split('?')[0]
            tracks = []
            results = self.spotify.playlist_tracks(playlist_id)

            while results:
                for item in results['items']:
                    if item.get('track'):
                        track = item['track']
                        tracks.append({
                            'query': f"{track['name']} {track['artists'][0]['name']}",
                            'spotify_data': {
                                'id': track['id'],
                                'name': track['name'],
                                'artists': [a['name'] for a in track['artists']],
                                'duration_ms': track['duration_ms']
                            }
                        })
                results = self.spotify.next(results) if results['next'] else None

            if interaction:
                await msg.edit(content=f"🔎 Ищем {len(tracks)} треков...")

            self.full_playlist = []
            batch_size = 5
            for i in range(0, len(tracks), batch_size):
                batch = tracks[i:i + batch_size]
                tasks = [self.run_ydl_extract(f"ytsearch:{item['query']}") for item in batch]
                batch_results = await asyncio.gather(*tasks)

                for j, res in enumerate(batch_results):
                    if res and res.get('entries'):
                        entry = res['entries'][0]
                        self.full_playlist.append({
                            'url': entry['url'],
                            'title': entry['title'],
                            'original_url': entry.get('original_url', entry['url']),
                            'spotify_data': batch[j]['spotify_data']
                        })

                if interaction and i % 10 == 0:
                    await msg.edit(content=f"✅ Загружено {min(i + batch_size, len(tracks))}/{len(tracks)}")

            if self.config.cache_enabled:
                self._save_to_cache(cache_key, {
                    'url': url,
                    'last_updated': datetime.now(timezone.utc).isoformat(),
                    'tracks': self.full_playlist
                })

            return True

        except Exception as e:
            safe_log_info(f"Spotify error: {e}")
            if interaction: await interaction.followup.send("❌ Ошибка загрузки")
            return False
        finally:
            self.is_loading = False

    async def run_ydl_extract(self, query):
        def _extract():
            with YoutubeDL(self.ydl_opts) as ydl:
                return ydl.extract_info(query, download=False)

        return await asyncio.get_running_loop().run_in_executor(None, _extract)

    async def play_next(self, error=None):
        if error:
            safe_log_info(f"Ошибка: {error}")

        async with self._play_lock:
            if self.voice_client and self.voice_client.is_playing():
                self.voice_client.stop()

            await asyncio.sleep(0.5)

            if not self.voice_client or not self.voice_client.is_connected():
                self.is_playing = False
                return

            if self.current_position >= len(self.full_playlist):
                self.is_playing = False
                return

            try:
                self.current_song = self.full_playlist[self.current_position]
                asyncio.create_task(self._play_current_track())

            except Exception as e:
                safe_log_info(f"Ошибка: {e}")
                await self._increment_position()
                await self.play_next()

    async def _play_current_track(self):
        try:
            original_flat = self.ydl_opts.get("extract_flat", False)
            original_force_generic = self.ydl_opts.get("force_generic_extractor", True)
            self.ydl_opts["extract_flat"] = False
            self.ydl_opts["force_generic_extractor"] = False

            info = await self.run_ydl_extract(self.current_song['original_url'])

            self.ydl_opts["extract_flat"] = original_flat
            self.ydl_opts["force_generic_extractor"] = original_force_generic

            if not info:
                await self._increment_position()
                return await self.play_next()

            audio_url = None
            if 'formats' in info:
                for f in info['formats']:
                    if f.get('acodec') != 'none' and f.get('url', '').startswith("http"):
                        audio_url = f['url']
                        break

            if not audio_url:
                audio_url = info.get('url')

            if not audio_url:
                safe_log_info(f"❌ Недопустимый аудио URL: {audio_url}")
                await self._increment_position()
                return await self.play_next()

            ffmpeg_options = {
                'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
                'options': '-vn',
                'executable': self.config.ffmpeg_path
            }

            safe_log_info(f"▶️ Трек: {self.current_song['title']}")
            safe_log_info(f"▶️ URL для ffmpeg: {audio_url}")

            source = discord.FFmpegOpusAudio(audio_url, **ffmpeg_options)

            def after_play(error):
                if not self._manual_skip:
                    asyncio.run_coroutine_threadsafe(self._increment_position(), self.bot.loop)
                self._manual_skip = False
                asyncio.run_coroutine_threadsafe(self.play_next(error), self.bot.loop)

            self.is_playing = True
            self.voice_client.play(source, after=after_play)

        except Exception as e:
            safe_log_info(f"❌ Ошибка воспроизведения: {e}")
            await self._increment_position()
            await self.play_next()

    async def _increment_position(self):
        if self.current_position < len(self.full_playlist) - 1:
            self.current_position += 1
        else:
            self.current_position = 0

    @app_commands.command(name="play", description="Начать воспроизведение плейлиста")
    async def play(self, interaction: discord.Interaction, url: str = None):
        await interaction.response.defer()

        if self.is_loading:
            await interaction.followup.send("⏳ Уже идет загрузка плейлиста, пожалуйста подождите...")
            return
        if self.is_playing:
            await interaction.followup.send("🎵 Уже играет музыка! Используйте /stop чтобы остановить.")
            return

        if not await self.connect_to_voice():
            await interaction.followup.send("❌ Ошибка подключения к голосовому каналу")
            return

        if not url:
            message = "**Выберите плейлист для воспроизведения:**\n"
            options = []
            for i, (platform_name, playlist_url) in enumerate(self.config.playlist_urls.items(), start=1):
                if playlist_url:
                    message += f"{i}. {platform_name}\n"
                    options.append((platform_name, playlist_url))

            if not options:
                await interaction.followup.send("❌ Нет доступных плейлистов в конфигурации")
                return

            message += "\nОтправьте номер плейлиста или 'cancel' для отмены"
            await interaction.followup.send(message)

            def check(m):
                return m.author == interaction.user and m.channel == interaction.channel and (
                        m.content.isdigit() and 1 <= int(m.content) <= len(options) or
                        m.content.lower() == 'cancel'
                )

            try:
                msg = await self.bot.wait_for('message', timeout=30.0, check=check)
                if msg.content.lower() == 'cancel':
                    await interaction.followup.send("❌ Отменено")
                    return
                platform_name, url = options[int(msg.content) - 1]
                await interaction.followup.send(f"🔄 Выбран плейлист {platform_name}, начинаю загрузку...")
            except asyncio.TimeoutError:
                await interaction.followup.send("⌛ Время вышло, отменено")
                return

        success = False
        if "spotify.com" in url:
            success = await self.load_spotify_playlist(url, interaction)
        else:
            success = await self.load_playlist(url, interaction)

        if not success:
            await interaction.followup.send("❌ Не удалось загрузить плейлист")
            return

        await interaction.followup.send("✅ Плейлист загружен, начинаю воспроизведение...")
        await self.play_next()

    @app_commands.command(name="skip", description="Пропустить текущий трек")
    async def skip(self, interaction: discord.Interaction):
        await interaction.response.defer()

        if not self.is_playing:
            await interaction.followup.send("Ничего не играет")
            return

        async with self._play_lock:
            current_time = datetime.now(timezone.utc).timestamp()
            if current_time - self.last_skip_time < self.config.skip_cooldown:
                await interaction.followup.send(f"Подождите {self.config.skip_cooldown} сек")
                return

            self.last_skip_time = current_time
            self._manual_skip = True
            await self._increment_position()
            self.voice_client.stop()
            await interaction.followup.send("Пропущено")

    @app_commands.command(name="stop", description="Остановить воспроизведение")
    async def stop(self, interaction: discord.Interaction):
        await interaction.response.defer()

        # Полная очистка состояния
        if self.voice_client:
            if self.voice_client.is_playing():
                self.voice_client.stop()
            await self.voice_client.disconnect(force=True)
            self.voice_client = None

        # Сброс всех переменных состояния
        self.current_song = None
        self.full_playlist = []
        self.current_position = 0
        self.is_playing = False
        self._manual_skip = False
        self.is_loading = False

        await interaction.followup.send("⏹️ Воспроизведение остановлено и состояние сброшено")

    @app_commands.command(name="nowplaying", description="Показать текущий трек")
    async def now_playing(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if not self.current_song:
            await interaction.followup.send("Ничего не играет")
            return
        await interaction.followup.send(f"Сейчас: {self.current_song['title']}")

    @app_commands.command(name="playlist", description="Показать текущий плейлист")
    async def show_playlist(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if not self.full_playlist:
            await interaction.followup.send("Пусто")
            return
        message = ["**Плейлист:**"]
        for i, song in enumerate(self.full_playlist, start=1):
            prefix = "▶️ " if i == self.current_position + 1 and self.is_playing else ""
            message.append(f"{i}. {prefix}{song['title']}")
        for chunk in [message[i:i + 10] for i in range(0, len(message), 10)]:
            await interaction.followup.send("\n".join(chunk))

    @app_commands.command(name="goto", description="Перейти к треку по номеру")
    async def goto_track(self, interaction: discord.Interaction, track_number: int):
        await interaction.response.defer()
        if not self.full_playlist:
            await interaction.followup.send("Пусто")
            return
        if track_number < 1 or track_number > len(self.full_playlist):
            await interaction.followup.send(f"Недопустимый номер: 1-{len(self.full_playlist)}")
            return
        if track_number == self.current_position + 1:
            await interaction.followup.send(f"Уже играет: {self.full_playlist[self.current_position]['title']}")
            return

        async with self._play_lock:
            self.current_position = track_number - 1
            self._manual_skip = True
            if self.is_playing:
                self.voice_client.stop()
            else:
                await self.play_next()
            await interaction.followup.send(f"Переход: {track_number}")

    @app_commands.command(name="leave", description="Покинуть голосовой канал")
    async def leave(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if not self.voice_client:
            await interaction.followup.send("Бот не подключен к голосовому каналу")
            return
        self.reset_state()
        await self.voice_client.disconnect(force=True)
        self.voice_client = None
        await interaction.followup.send("✅ Успешно покинул голосовой канал")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member == self.bot.user and after.channel is None:
            try:
                # Только сброс состояния, не пытаемся переподключаться
                self.voice_client = None
                self.is_playing = False
                self.current_song = None
                safe_log_info("Бот был отключен от голосового канала")
            except Exception as e:
                safe_log_info(f"Ошибка в on_voice_state_update: {e}")

    @app_commands.command(name="random", description="Случайное воспроизведение треков из плейлиста")
    async def random(self, interaction: discord.Interaction, url: str = None):
        await interaction.response.defer()

        if self.is_loading:
            await interaction.followup.send("⏳ Уже идет загрузка, подождите...")
            return

        if not await self.connect_to_voice():
            await interaction.followup.send("❌ Ошибка подключения к голосовому каналу")
            return

        if not url:
            # Используем первый доступный плейлист из конфига
            for playlist_url in self.config.playlist_urls.values():
                if playlist_url:
                    url = playlist_url
                    break

            if not url:
                await interaction.followup.send("❌ Нет доступных плейлистов в конфигурации")
                return

        # Загружаем плейлист
        success = False
        if "spotify.com" in url:
            success = await self.load_spotify_playlist(url, interaction)
        else:
            success = await self.load_playlist(url, interaction)

        if not success:
            await interaction.followup.send("❌ Не удалось загрузить плейлист")
            return

        random.shuffle(self.full_playlist)
        self.current_position = 0

        await interaction.followup.send("🔀 Случайное воспроизведение включено!")
        await self.play_next()


class MusicBot(commands.Bot):
    def __init__(self, config):
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.message_content = True
        super().__init__(command_prefix=config.command_prefix, intents=intents, help_command=None)
        self.config = config

    async def setup_hook(self):
        await self.add_cog(MusicCog(self, self.config))
        await self.tree.sync()

    async def on_ready(self):
        safe_log_info(f'Бот готов: {self.user.name}')
        music_cog = self.get_cog("MusicCog")
        music_cog.reset_state()
        if self.config.voice_channel_id:
            await music_cog.connect_to_voice()


def load_config():
    with open("config.json", encoding='utf-8') as f:
        return BotConfig(json.load(f))


def main():
    # Принудительная установка UTF-8 кодировки
    if sys.platform == "win32":
        # Для Windows устанавливаем UTF-8 кодировку
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    try:
        config = load_config()
        bot = MusicBot(config)

        # Установка обработчика сигналов для корректного завершения
        import signal
        signal.signal(signal.SIGINT, lambda s, f: bot.close())
        signal.signal(signal.SIGTERM, lambda s, f: bot.close())

        bot.run(config.token)
    except Exception as e:
        safe_log_info(f"Критическая ошибка: {str(e)}")


if __name__ == "__main__":
    main()