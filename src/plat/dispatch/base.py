"""Dispatch boundary: execution always delegates to the same claimed runner."""
from typing import Protocol


class Dispatcher(Protocol):
    def run(self, cfg, plat_id: int, *, workers: int = 1, log=print): ...
