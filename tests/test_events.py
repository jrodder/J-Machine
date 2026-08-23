import unittest
from zmach.events import (Text, Prompt, Error, EndOfGame, Event,
                          StoryFileError, SaveFileError)

class TestEvents(unittest.TestCase):
    def test_events_are_events(self):
        for e in (Text("x"), Prompt(), Error("y"), EndOfGame(0)):
            self.assertIsInstance(e, Event)

    def test_fields(self):
        self.assertEqual(Text("hi").data, "hi")
        self.assertIsNone(Prompt().hint)
        self.assertEqual(EndOfGame(3).status, 3)

    def test_exceptions(self):
        with self.assertRaises(StoryFileError):
            raise StoryFileError("bad story")
        with self.assertRaises(SaveFileError):
            raise SaveFileError("bad save")