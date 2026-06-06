import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, TypedDict, Union
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from streamlink.exceptions import StreamError
from streamlink.plugin import Plugin, pluginmatcher
from streamlink.plugin.api import validate
from streamlink.stream.hls import HLSStream, HLSStreamReader, HLSStreamWorker, parse_m3u8

log = logging.getLogger(__name__)


def should_refresh_stream_error(err: StreamError) -> bool:
    response = getattr(err, "response", None)
    return response is not None and getattr(response, "status_code", 0) >= 400


class ChzzkHLSStreamWorker(HLSStreamWorker):
    stream: "ChzzkHLSStream"

    def _fetch_playlist(self) -> Any:
        for _ in range(2):
            try:
                return super()._fetch_playlist()
            except StreamError as err:
                if should_refresh_stream_error(err):
                    self.stream.refresh_playlist()
                    log.debug("Force-refreshed Chzzk playlist token")
                else:
                    raise err
        raise StreamError("Failed to fetch playlist after token refresh")


class ChzzkHLSStreamReader(HLSStreamReader):
    __worker__ = ChzzkHLSStreamWorker


class ChzzkHLSStream(HLSStream):
    __shortname__ = "hls-chzzk"
    __reader__ = ChzzkHLSStreamReader
    _REFRESH_BEFORE = 3 * 60 * 60

    def __init__(self, session, url: str, channel_id: str, *args, **kwargs) -> None:
        super().__init__(session, url, *args, **kwargs)
        self._url = url
        self._channel_id = channel_id
        self._api = ChzzkAPI(session)
        self._expire = self._get_expire_time(url)

    @property
    def url(self) -> str:
        if self._expire is not None and time.time() >= self._expire - self._REFRESH_BEFORE:
            self.refresh_playlist()
        return self._url

    def refresh_playlist(self) -> None:
        datatype, data = self._api.get_live_detail(self._channel_id)
        if datatype == "error":
            raise StreamError(data)
        if not data or len(data) < 2:
            raise StreamError("Could not refresh Chzzk playlist")
        media, status, *_ = data
        if status != "OPEN" or media is None:
            raise StreamError("Chzzk stream is no longer open")
        for media_info in media:
            if len(media_info) >= 3 and media_info[1] == "HLS" and media_info[0] == "HLS":
                res = self._fetch_variant_playlist(self.session, self._update_domain(media_info[2]))
                m3u8 = parse_m3u8(res)
                for playlist in m3u8.playlists:
                    if playlist.stream_info:
                        self._replace_token(self._update_domain(playlist.uri))
                        self._expire = self._get_expire_time(self._url)
                        return
        raise StreamError("No valid HLS stream found")

    def _update_domain(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.hostname == "livecloud.pstatic.net":
            return urlunparse(parsed._replace(netloc="nlive-streaming.navercdn.com"))
        return url

    def _replace_token(self, new_url: str) -> None:
        parsed_old = urlparse(self._url)
        parsed_new = urlparse(new_url)
        qs_old = parse_qs(parsed_old.query)
        qs_new = parse_qs(parsed_new.query)
        if "hdnts" in qs_new:
            qs_old["hdnts"] = qs_new.get("hdnts")
        self._url = urlunparse(parsed_old._replace(query=urlencode(qs_old, doseq=True)))

    def _get_expire_time(self, url: str) -> Optional[int]:
        values = parse_qs(urlparse(url).query).get("exp")
        return int(values[0]) if values and values[0].isdigit() else None


class LiveDetail(TypedDict):
    status: str
    liveId: int
    liveTitle: Union[str, None]
    liveCategory: Union[str, None]
    adult: bool
    channel: str
    media: List[Dict[str, str]]


@dataclass
class ChzzkAPI:
    session: Any
    _LIVE_DETAIL_URL: str = "https://api.chzzk.naver.com/service/v3/channels/{channel_id}/live-detail"

    def _query_api(self, url: str, *schemas: validate.Schema) -> Tuple[str, Union[Dict[str, Any], str]]:
        return self.session.http.get(
            url,
            acceptable_status=(200, 404),
            headers={"Referer": "https://chzzk.naver.com/"},
            schema=validate.Schema(
                validate.parse_json(),
                validate.any(
                    validate.all({"code": int, "message": str}, validate.transform(lambda data: ("error", data["message"]))),
                    validate.all({"code": 200, "content": None}, validate.transform(lambda _: ("success", None))),
                    validate.all({"code": 200, "content": dict}, validate.get("content"), *schemas, validate.transform(lambda data: ("success", data))),
                ),
            ),
        )

    def get_live_detail(self, channel_id: str) -> Tuple[str, Union[LiveDetail, str]]:
        return self._query_api(
            self._LIVE_DETAIL_URL.format(channel_id=channel_id),
            {
                "status": str,
                "liveId": int,
                "liveTitle": validate.any(str, None),
                "liveCategory": validate.any(str, None),
                "adult": bool,
                "channel": validate.all({"channelName": str}, validate.get("channelName")),
                "livePlaybackJson": validate.none_or_all(
                    str,
                    validate.parse_json(),
                    {
                        "media": [
                            validate.all(
                                {"mediaId": str, "protocol": str, "path": validate.url()},
                                validate.union_get("mediaId", "protocol", "path"),
                            ),
                        ],
                    },
                    validate.get("media"),
                ),
            },
            validate.union_get("livePlaybackJson", "status", "liveId", "channel", "liveCategory", "liveTitle", "adult"),
        )


@pluginmatcher(
    name="live",
    pattern=re.compile(r"https?://chzzk\.naver\.com/live/(?P<channel_id>[A-Za-z0-9_-]{1,128})"),
)
class Chzzk(Plugin):
    _STATUS_OPEN = "OPEN"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._api = ChzzkAPI(self.session)

    def _get_live(self, channel_id: str) -> Optional[Dict[str, HLSStream]]:
        datatype, data = self._api.get_live_detail(channel_id)
        if datatype == "error" or data is None or len(data) < 7:
            return None
        media, status, *_ = data
        if status != self._STATUS_OPEN or media is None:
            return None
        streams = {}
        for media_info in media:
            if len(media_info) >= 3 and media_info[1] == "HLS" and media_info[0] == "HLS":
                hls_streams = ChzzkHLSStream.parse_variant_playlist(
                    self.session,
                    self._update_domain(media_info[2]),
                    channel_id=channel_id,
                )
                if hls_streams:
                    streams.update(hls_streams)
        return streams or None

    def _update_domain(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.hostname == "livecloud.pstatic.net":
            return urlunparse(parsed._replace(netloc="nlive-streaming.navercdn.com"))
        return url

    def _get_streams(self) -> Optional[Dict[str, HLSStream]]:
        if self.matches["live"]:
            return self._get_live(self.match["channel_id"])
        return None


__plugin__ = Chzzk
