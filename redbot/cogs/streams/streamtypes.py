import asyncio
import contextlib
import logging
import time
from dateutil.parser import parse as parse_time
from random import choice
from string import ascii_letters
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET
from typing import ClassVar, Optional, List, Tuple

import aiohttp
import discord

from redbot import json

from .errors import (
    APIError,
    OfflineStream,
    InvalidTwitchCredentials,
    InvalidYoutubeCredentials,
    StreamNotFound,
    YoutubeQuotaExceeded,
)
from redbot.core.i18n import Translator
from redbot.core.utils.chat_formatting import humanize_number, humanize_timedelta

TWITCH_BASE_URL = "https://api.twitch.tv"
TWITCH_ID_ENDPOINT = TWITCH_BASE_URL + "/helix/users"
TWITCH_STREAMS_ENDPOINT = TWITCH_BASE_URL + "/helix/streams/"
TWITCH_COMMUNITIES_ENDPOINT = TWITCH_BASE_URL + "/helix/communities"

YOUTUBE_BASE_URL = "https://www.googleapis.com/youtube/v3"
YOUTUBE_CHANNELS_ENDPOINT = YOUTUBE_BASE_URL + "/channels"
YOUTUBE_SEARCH_ENDPOINT = YOUTUBE_BASE_URL + "/search"
YOUTUBE_VIDEOS_ENDPOINT = YOUTUBE_BASE_URL + "/videos"
YOUTUBE_CHANNEL_RSS = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

_ = Translator("Streams", __file__)

log = logging.getLogger("red.core.cogs.Streams")


def rnd(url):
    """Appends a random parameter to the url to avoid Discord's caching"""
    return url + "?rnd=" + "".join([choice(ascii_letters) for _loop_counter in range(6)])


def get_video_ids_from_feed(feed):
    root = ET.fromstring(feed)
    rss_video_ids = []
    for child in root.iter("{http://www.w3.org/2005/Atom}entry"):
        for i in child.iter("{http://www.youtube.com/xml/schemas/2015}videoId"):
            yield i.text


class Stream:

    token_name: ClassVar[Optional[str]] = None

    def __init__(self, **kwargs):
        self._bot = kwargs.pop("_bot")
        self.name = kwargs.pop("name", None)
        self.channels = kwargs.pop("channels", [])
        # self.already_online = kwargs.pop("already_online", False)
        self.messages = kwargs.pop("messages", [])
        self.type = self.__class__.__name__

    async def is_online(self):
        raise NotImplementedError()

    def make_embed(self):
        raise NotImplementedError()

    def iter_messages(self):
        for msg_data in self.messages:
            data = msg_data.copy()
            # "guild" key might not exist for old config data (available since GH-4742)
            if guild_id := msg_data.get("guild"):
                guild = self._bot.get_guild(guild_id)
                channel = guild and guild.get_channel(msg_data["channel"])
            else:
                channel = self._bot.get_channel(msg_data["channel"])
            if channel is not None:
                data["partial_message"] = channel.get_partial_message(data["message"])
            yield data

    def export(self):
        data = {}
        for k, v in self.__dict__.items():
            if not k.startswith("_"):
                data[k] = v
        return data

    def __repr__(self):
        return "<{0.__class__.__name__}: {0.name}>".format(self)


class YoutubeStream(Stream):

    token_name = "youtube"

    def __init__(self, **kwargs):
        self.id = kwargs.pop("id", None)
        self._token = kwargs.pop("token", None)
        self._config = kwargs.pop("config")
        self.not_livestreams: List[str] = []
        self.livestreams: List[str] = []

        super().__init__(**kwargs)

    async def is_online(self):
        if not self._token:
            raise InvalidYoutubeCredentials("YouTube API key is not set.")

        if not self.id:
            self.id = await self.fetch_id()
        elif not self.name:
            self.name = await self.fetch_name()

        async with aiohttp.ClientSession(json_serialize=json.dumps) as session:
            async with session.get(YOUTUBE_CHANNEL_RSS.format(channel_id=self.id)) as r:
                if r.status == 404:
                    raise StreamNotFound()
                rssdata = await r.text()

        if self.not_livestreams:
            self.not_livestreams = list(dict.fromkeys(self.not_livestreams))

        if self.livestreams:
            self.livestreams = list(dict.fromkeys(self.livestreams))

        for video_id in get_video_ids_from_feed(rssdata):
            if video_id in self.not_livestreams:
                log.debug(f"video_id in not_livestreams: {video_id}")
                continue
            log.debug(f"video_id not in not_livestreams: {video_id}")
            params = {
                "key": self._token["api_key"],
                "id": video_id,
                "part": "id,liveStreamingDetails",
            }
            async with aiohttp.ClientSession(json_serialize=json.dumps) as session:
                async with session.get(YOUTUBE_VIDEOS_ENDPOINT, params=params) as r:
                    data = await r.json(loads=json.loads)
                    try:
                        self._check_api_errors(data)
                    except InvalidYoutubeCredentials:
                        log.error("The YouTube API key is either invalid or has not been set.")
                        break
                    except YoutubeQuotaExceeded:
                        log.error("YouTube quota has been exceeded.")
                        break
                    except APIError as e:
                        log.error(
                            "Something went wrong whilst trying to"
                            " contact the stream service's API.\n"
                            "Raw response data:\n%r",
                            e,
                        )
                        continue
                    video_data = data.get("items", [{}])[0]
                    stream_data = video_data.get("liveStreamingDetails", {})
                    log.debug(f"stream_data for {video_id}: {stream_data}")
                    if (
                        stream_data
                        and stream_data != "None"
                        and stream_data.get("actualEndTime", None) is None
                    ):
                        actual_start_time = stream_data.get("actualStartTime", None)
                        scheduled = stream_data.get("scheduledStartTime", None)
                        if scheduled is not None and actual_start_time is None:
                            scheduled = parse_time(scheduled)
                            if (scheduled - datetime.now(timezone.utc)).total_seconds() < -3600:
                                continue
                        elif actual_start_time is None:
                            continue
                        if video_id not in self.livestreams:
                            self.livestreams.append(video_id)
                    else:
                        self.not_livestreams.append(video_id)
                        if video_id in self.livestreams:
                            self.livestreams.remove(video_id)
        log.debug(f"livestreams for {self.name}: {self.livestreams}")
        log.debug(f"not_livestreams for {self.name}: {self.not_livestreams}")
        # This is technically redundant since we have the
        # info from the RSS ... but incase you don't wanna deal with fully rewritting the
        # code for this part, as this is only a 2 quota query.
        if self.livestreams:
            params = {
                "key": self._token["api_key"],
                "id": self.livestreams[-1],
                "part": "snippet,liveStreamingDetails",
            }
            async with aiohttp.ClientSession(json_serialize=json.dumps) as session:
                async with session.get(YOUTUBE_VIDEOS_ENDPOINT, params=params) as r:
                    data = await r.json()
            return await self.make_embed(data)
        raise OfflineStream()

    async def make_embed(self, data):
        vid_data = data["items"][0]
        video_url = "https://youtube.com/watch?v={}".format(vid_data["id"])
        title = vid_data["snippet"]["title"]
        thumbnail = vid_data["snippet"]["thumbnails"]["medium"]["url"]
        channel_title = vid_data["snippet"]["channelTitle"]
        embed = discord.Embed(title=title, url=video_url)
        is_schedule = False
        if vid_data["liveStreamingDetails"].get("scheduledStartTime", None) is not None:
            if "actualStartTime" not in vid_data["liveStreamingDetails"]:
                start_time = parse_time(vid_data["liveStreamingDetails"]["scheduledStartTime"])
                start_in = start_time - datetime.now(timezone.utc)
                if start_in.total_seconds() > 0:
                    embed.description = _("This stream will start in {time}").format(
                        time=humanize_timedelta(
                            timedelta=timedelta(minutes=start_in.total_seconds() // 60)
                        )  # getting rid of seconds
                    )
                else:
                    embed.description = _(
                        "This stream was scheduled for {min} minutes ago"
                    ).format(min=round((start_in.total_seconds() * -1) // 60))
                embed.timestamp = start_time
                is_schedule = True
            else:
                # delete the message(s) about the stream schedule
                to_remove = []
                for msg_data in self.iter_messages():
                    if not msg_data.get("is_schedule", False):
                        continue
                    partial_msg = msg_data["partial_message"]
                    if partial_msg is not None:
                        autodelete = await self._config.guild(partial_msg.guild).autodelete()
                        if autodelete:
                            with contextlib.suppress(discord.NotFound):
                                await partial_msg.delete()
                    to_remove.append(msg_data["message"])
                self.messages = [
                    data for data in self.messages if data["message"] not in to_remove
                ]
        embed.set_author(name=channel_title)
        embed.set_image(url=rnd(thumbnail))
        embed.colour = 0x9255A5
        return embed, is_schedule

    async def fetch_id(self):
        return await self._fetch_channel_resource("id")

    async def fetch_name(self):
        snippet = await self._fetch_channel_resource("snippet")
        return snippet["title"]

    async def _fetch_channel_resource(self, resource: str):

        params = {"key": self._token["api_key"], "part": resource}
        if resource == "id":
            params["forUsername"] = self.name
        else:
            params["id"] = self.id

        async with aiohttp.ClientSession(json_serialize=json.dumps) as session:
            async with session.get(YOUTUBE_CHANNELS_ENDPOINT, params=params) as r:
                data = await r.json(loads=json.loads)

        self._check_api_errors(data)
        if "items" in data and len(data["items"]) == 0:
            raise StreamNotFound()
        elif "items" in data:
            return data["items"][0][resource]
        elif (
            "pageInfo" in data
            and "totalResults" in data["pageInfo"]
            and data["pageInfo"]["totalResults"] < 1
        ):
            raise StreamNotFound()
        raise APIError(data)

    def _check_api_errors(self, data: dict):
        if "error" in data:
            error_code = data["error"]["code"]
            if error_code == 400 and data["error"]["errors"][0]["reason"] == "keyInvalid":
                raise InvalidYoutubeCredentials()
            elif error_code == 403 and data["error"]["errors"][0]["reason"] in (
                "dailyLimitExceeded",
                "quotaExceeded",
                "rateLimitExceeded",
            ):
                raise YoutubeQuotaExceeded()
            raise APIError(data)

    def __repr__(self):
        return "<{0.__class__.__name__}: {0.name} (ID: {0.id})>".format(self)


class TwitchStream(Stream):

    token_name = "twitch"

    def __init__(self, **kwargs):
        self.id = kwargs.pop("id", None)
        self._client_id = kwargs.pop("token", None)
        self._bearer = kwargs.pop("bearer", None)
        self._rate_limit_resets: set = set()
        self._rate_limit_remaining: int = 0
        self.games = kwargs.pop("games", {})
        super().__init__(**kwargs)

    async def wait_for_rate_limit_reset(self) -> None:
        """Check rate limits in response header and ensure we're following them.

        From python-twitch-client and adaptated to asyncio from Trusty-cogs:
        https://github.com/tsifrer/python-twitch-client/blob/master/twitch/helix/base.py
        https://github.com/TrustyJAID/Trusty-cogs/blob/master/twitch/twitch_api.py
        """
        current_time = int(time.time())
        self._rate_limit_resets = {x for x in self._rate_limit_resets if x > current_time}
        if self._rate_limit_remaining == 0:

            if self._rate_limit_resets:
                reset_time = next(iter(self._rate_limit_resets))
                # Calculate wait time and add 0.1s to the wait time to allow Twitch to reset
                # their counter
                wait_time = reset_time - current_time + 0.1
                await asyncio.sleep(wait_time)

    async def get_game_info_by_id(self, game_id: int):
        header = {"Client-ID": str(self._client_id)}
        if self._bearer is not None:
            header = {**header, "Authorization": f"Bearer {self._bearer}"}
        params = {"id": game_id}
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.twitch.tv/helix/games", headers=header, params=params
            ) as r:
                game_data = await r.json(encoding="utf-8")
        if game_data:
            return game_data
        else:
            return {}

    async def get_game_info_by_name(self, game_name: str):
        header = {"Client-ID": str(self._client_id)}
        if self._bearer is not None:
            header = {**header, "Authorization": f"Bearer {self._bearer}"}
        params = {"name": game_name}
        async with aiohttp.ClientSession(json_serialize=json.dumps) as session:
            async with session.get(
                "https://api.twitch.tv/helix/games", headers=header, params=params
            ) as r:
                game_data = await r.json(encoding="utf-8", loads=json.loads)
        if game_data:
            return game_data["data"]
        else:
            return []

    async def get_data(self, url: str, params: dict = {}) -> Tuple[Optional[int], dict]:
        header = {"Client-ID": str(self._client_id)}
        if self._bearer is not None:
            header["Authorization"] = f"Bearer {self._bearer}"
        await self.wait_for_rate_limit_reset()
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=header, params=params, timeout=60) as resp:
                    remaining = resp.headers.get("Ratelimit-Remaining")
                    if remaining:
                        self._rate_limit_remaining = int(remaining)
                    reset = resp.headers.get("Ratelimit-Reset")
                    if reset:
                        self._rate_limit_resets.add(int(reset))

                    if resp.status == 429:
                        log.info(
                            "Ratelimited. Trying again at %s.", datetime.fromtimestamp(int(reset))
                        )
                        resp.release()
                        return await self.get_data(url)

                    if resp.status != 200:
                        return resp.status, {}

                    return resp.status, await resp.json(encoding="utf-8")
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as exc:
                log.warning("Connection error occurred when fetching Twitch stream", exc_info=exc)
                return None, {}

    async def is_online(self):
        if not self.id:
            self.id = await self.fetch_id()

        url = TWITCH_STREAMS_ENDPOINT
        params = {"user_id": self.id}

        code, data = await self.get_data(url, params)
        if code == 200:
            if not data["data"]:
                raise OfflineStream()
            self.name = data["data"][0]["user_name"]
            data = data["data"][0]
            data["game_name"] = None
            data["followers"] = None
            data["view_count"] = None
            data["profile_image_url"] = None
            data["login"] = None

            game_id = data["game_id"]
            if game_id:
                __, game_data = await self.get_data(
                    "https://api.twitch.tv/helix/games", {"id": game_id}
                )
                if game_data:
                    game_data = game_data["data"][0]
                    data["game_name"] = game_data["name"]
            __, user_data = await self.get_data(
                "https://api.twitch.tv/helix/users/follows", {"to_id": self.id}
            )
            if user_data:
                followers = user_data["total"]
                data["followers"] = followers

            __, user_profile_data = await self.get_data(
                "https://api.twitch.tv/helix/users", {"id": self.id}
            )
            if user_profile_data:
                profile_image_url = user_profile_data["data"][0]["profile_image_url"]
                data["profile_image_url"] = profile_image_url
                data["view_count"] = user_profile_data["data"][0]["view_count"]
                data["login"] = user_profile_data["data"][0]["login"]

            is_rerun = False
            return self.make_embed(data), data, is_rerun
        elif code == 400:
            raise InvalidTwitchCredentials()
        elif code == 404:
            raise StreamNotFound()
        else:
            raise APIError(data)

    async def fetch_id(self):
        header = {"Client-ID": str(self._client_id)}
        if self._bearer is not None:
            header = {**header, "Authorization": f"Bearer {self._bearer}"}
        url = TWITCH_ID_ENDPOINT
        params = {"login": self.name}

        status, data = await self.get_data(url, params)

        if status == 200:
            if not data["data"]:
                raise StreamNotFound()
            return data["data"][0]["id"]
        elif status == 400:
            raise StreamNotFound()
        elif status == 401:
            raise InvalidTwitchCredentials()
        else:
            raise APIError(data)

    def make_embed(self, data):
        is_rerun = data["type"] == "rerun"
        url = f"https://www.twitch.tv/{data['login']}" if data["login"] is not None else None
        logo = data["profile_image_url"]
        if logo is None:
            logo = "https://static-cdn.jtvnw.net/jtv_user_pictures/xarth/404_user_70x70.png"
        status = data["title"]
        if not status:
            status = _("Untitled broadcast")
        if is_rerun:
            status += _(" - Rerun")
        embed = discord.Embed(title=status, url=url, color=0x6441A4)
        embed.set_author(name=data["user_name"])
        embed.add_field(name=_("Followers"), value=humanize_number(data["followers"]))
        embed.add_field(name=_("Total views"), value=humanize_number(data["view_count"]))
        embed.set_thumbnail(url=logo)
        if data["thumbnail_url"]:
            embed.set_image(url=rnd(data["thumbnail_url"].format(width=320, height=180)))
        if data["game_name"]:
            embed.set_footer(text=_("Playing: ") + data["game_name"])
        return embed

    def __repr__(self):
        return "<{0.__class__.__name__}: {0.name} (ID: {0.id})>".format(self)


class HitboxStream(Stream):

    token_name = None  # This streaming services don't currently require an API key

    def __init__(self, **kwargs):
        self.games = kwargs.pop("games", {})
        super().__init__(**kwargs)

    async def is_online(self):
        url = "https://api.smashcast.tv/media/live/" + self.name

        async with aiohttp.ClientSession(json_serialize=json.dumps) as session:
            async with session.get(url) as r:
                # data = await r.json(encoding='utf-8')
                data = await r.text()
        data = json.loads(data, strict=False)
        if "livestream" not in data:
            raise StreamNotFound()
        elif data["livestream"][0]["media_is_live"] == "0":
            # self.already_online = False
            raise OfflineStream()
        elif data["livestream"][0]["media_is_live"] == "1":
            # self.already_online = True
            return self.make_embed(data), data

        raise APIError(data)

    def make_embed(self, data):
        base_url = "https://edge.sf.hitbox.tv"
        livestream = data["livestream"][0]
        channel = livestream["channel"]
        url = channel["channel_link"]
        embed = discord.Embed(title=livestream["media_status"], url=url, color=0x98CB00)
        embed.set_author(name=livestream["media_name"])
        embed.add_field(name=_("Followers"), value=humanize_number(channel["followers"]))
        embed.set_thumbnail(url=base_url + channel["user_logo"])
        if livestream["media_thumbnail"]:
            embed.set_image(url=rnd(base_url + livestream["media_thumbnail"]))
        embed.set_footer(text=_("Playing: ") + livestream["category_name"])

        return embed


class PicartoStream(Stream):

    token_name = None  # This streaming services don't currently require an API key

    async def is_online(self):
        url = "https://api.picarto.tv/v1/channel/name/" + self.name

        async with aiohttp.ClientSession(json_serialize=json.dumps) as session:
            async with session.get(url) as r:
                data = await r.text(encoding="utf-8")
        if r.status == 200:
            data = json.loads(data)
            if data["online"] is True:
                # self.already_online = True
                return self.make_embed(data)
            else:
                # self.already_online = False
                raise OfflineStream()
        elif r.status == 404:
            raise StreamNotFound()
        else:
            raise APIError(data)

    def make_embed(self, data):
        avatar = rnd(
            "https://picarto.tv/user_data/usrimg/{}/dsdefault.jpg".format(data["name"].lower())
        )
        url = "https://picarto.tv/" + data["name"]
        thumbnail = data["thumbnails"]["web"]
        embed = discord.Embed(title=data["title"], url=url, color=0x4C90F3)
        embed.set_author(name=data["name"])
        embed.set_image(url=rnd(thumbnail))
        embed.add_field(name=_("Followers"), value=humanize_number(data["followers"]))
        embed.add_field(name=_("Total views"), value=humanize_number(data["viewers_total"]))
        embed.set_thumbnail(url=avatar)
        data["tags"] = ", ".join(data["tags"])

        if not data["tags"]:
            data["tags"] = _("None")

        if data["adult"]:
            data["adult"] = _("NSFW | ")
        else:
            data["adult"] = ""

        embed.set_footer(text=_("{adult}Category: {category} | Tags: {tags}").format(**data))
        return embed
