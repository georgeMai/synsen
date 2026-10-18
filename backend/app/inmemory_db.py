"""CSDL trong bộ nhớ (mongomock) có giao diện giống PyMongo Async.

CHỈ dùng để chạy thử nhanh / kiểm thử khi không có MongoDB (MONGODB_URI=memory://).
Không hỗ trợ time series collection; dữ liệu mất khi tắt tiến trình.
"""
from __future__ import annotations

from typing import Any


class _Cursor:
    def __init__(self, cursor) -> None:
        self._cursor = cursor

    def sort(self, *args, **kwargs):
        self._cursor = self._cursor.sort(*args, **kwargs)
        return self

    def limit(self, n: int):
        self._cursor = self._cursor.limit(n)
        return self

    def skip(self, n: int):
        self._cursor = self._cursor.skip(n)
        return self

    async def to_list(self, length: int | None = None) -> list[dict[str, Any]]:
        items = list(self._cursor)
        return items if length is None else items[:length]

    def __aiter__(self):
        self._iter = iter(self._cursor)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class _Collection:
    def __init__(self, coll) -> None:
        self._c = coll

    def find(self, *args, **kwargs) -> _Cursor:
        return _Cursor(self._c.find(*args, **kwargs))

    async def aggregate(self, pipeline, **kwargs) -> _Cursor:
        return _Cursor(self._c.aggregate(pipeline, **kwargs))

    def __getattr__(self, name: str):
        attr = getattr(self._c, name)
        if not callable(attr):
            return attr

        async def call(*args, **kwargs):
            return attr(*args, **kwargs)

        return call


class _Database:
    def __init__(self, database) -> None:
        self._d = database

    def __getitem__(self, name: str) -> _Collection:
        return _Collection(self._d[name])

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return _Collection(self._d[name])

    async def command(self, cmd, *args, **kwargs):
        return {"ok": 1.0}

    async def list_collection_names(self) -> list[str]:
        return self._d.list_collection_names()

    async def create_collection(self, name: str, **kwargs):
        if "timeseries" in kwargs:
            raise NotImplementedError("mongomock does not support time series collections")
        return self._d.create_collection(name)


class InMemoryClient:
    def __init__(self) -> None:
        import mongomock

        self._client = mongomock.MongoClient(tz_aware=True)

    def __getitem__(self, name: str) -> _Database:
        return _Database(self._client[name])

    async def close(self) -> None:
        self._client.close()
