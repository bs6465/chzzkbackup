from streamlink.exceptions import StreamError

from app.plugin.chzzk import should_refresh_stream_error


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def test_stream_error_without_response_does_not_refresh():
    assert should_refresh_stream_error(StreamError("plain error")) is False


def test_stream_error_with_http_failure_refreshes():
    error = StreamError("http error")
    error.response = FakeResponse(403)

    assert should_refresh_stream_error(error) is True


def test_stream_error_with_non_failure_response_does_not_refresh():
    error = StreamError("http error")
    error.response = FakeResponse(200)

    assert should_refresh_stream_error(error) is False
