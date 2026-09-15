"""PrometheusClient: parse real API shapes, fail loudly on anything else."""

from __future__ import annotations

import json

import pytest
import requests

from aort_normalizer.prometheus import PrometheusClient, Sample, TelemetrySourceError

AT = 1_000_000_000.5


class FakeResponse:
    def __init__(self, status=200, payload=None, text=None):
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        if self.exc is not None:
            raise self.exc
        return self.response


def vector(*series):
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": m, "value": [AT, v]} for m, v in series],
        },
    }


def client_for(response=None, exc=None, base="http://prom:9090"):
    session = FakeSession(response=response, exc=exc)
    return PrometheusClient(base, session=session), session


def test_parses_a_vector_result():
    client, _ = client_for(FakeResponse(payload=vector(({"job": "fineract"}, "1"))))
    assert client.query("up", at=AT) == [Sample(labels={"job": "fineract"}, value="1")]


def test_sends_the_query_and_evaluation_time():
    client, session = client_for(FakeResponse(payload=vector()))
    client.query("pg_up", at=AT)
    url, params, timeout = session.calls[0]
    assert url == "http://prom:9090/api/v1/query"
    assert params == {"query": "pg_up", "time": AT}
    assert timeout and timeout > 0


def test_trailing_slash_in_base_url_is_handled():
    client, session = client_for(FakeResponse(payload=vector()), base="http://prom:9090/")
    client.query("up", at=AT)
    assert session.calls[0][0] == "http://prom:9090/api/v1/query"


def test_empty_vector_returns_empty_list():
    client, _ = client_for(FakeResponse(payload=vector()))
    assert client.query("up", at=AT) == []


def test_http_error_raises_with_the_prometheus_message():
    body = {"status": "error", "errorType": "bad_data", "error": "parse error at char 3"}
    client, _ = client_for(FakeResponse(status=400, payload=body))
    with pytest.raises(TelemetrySourceError, match="parse error at char 3"):
        client.query("sum(", at=AT)


def test_error_status_in_a_200_body_raises():
    body = {"status": "error", "errorType": "execution", "error": "query timed out"}
    client, _ = client_for(FakeResponse(status=200, payload=body))
    with pytest.raises(TelemetrySourceError, match="query timed out"):
        client.query("up", at=AT)


def test_connection_error_raises():
    client, _ = client_for(exc=requests.ConnectionError("connection refused"))
    with pytest.raises(TelemetrySourceError, match="connection refused"):
        client.query("up", at=AT)


def test_timeout_raises():
    client, _ = client_for(exc=requests.Timeout("timed out"))
    with pytest.raises(TelemetrySourceError):
        client.query("up", at=AT)


def test_non_json_body_raises():
    client, _ = client_for(FakeResponse(status=200, payload=None, text="<html>proxy</html>"))
    with pytest.raises(TelemetrySourceError):
        client.query("up", at=AT)


def test_non_vector_result_type_raises():
    body = {"status": "success", "data": {"resultType": "matrix", "result": []}}
    client, _ = client_for(FakeResponse(payload=body))
    with pytest.raises(TelemetrySourceError, match="matrix"):
        client.query("up[1m]", at=AT)


def test_malformed_series_raises():
    body = {"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}}]}}
    client, _ = client_for(FakeResponse(payload=body))
    with pytest.raises(TelemetrySourceError):
        client.query("up", at=AT)
