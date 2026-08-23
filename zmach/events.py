"""Structured events emitted by the Session (spec §5). INV3: Text.data is
plain text — no ANSI, no control sequences except \n."""
from dataclasses import dataclass


class Event:
    pass


@dataclass
class Text(Event):
    data: str


@dataclass
class Prompt(Event):
    hint: str | None = None


@dataclass
class Error(Event):
    message: str


@dataclass
class EndOfGame(Event):
    status: int


class StoryFileError(Exception):
    pass


class SaveFileError(Exception):
    pass