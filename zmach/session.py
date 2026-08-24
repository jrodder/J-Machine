"""Session API (spec §5): call -> batch.

`load` runs the VM to the first Prompt or EndOfGame; `input` feeds one
whole line and runs to the next boundary; `save`/`restore` expose the
opaque ZMSAVE v1 snapshot (spec §7). The VM is blocked waiting for
input at every boundary (INV2), so every batch is a complete turn.
"""
import hashlib
import os
from dataclasses import dataclass

from . import savefile
from .events import EndOfGame, Error, Prompt, StoryFileError, Text
from .storyfile import StoryFile
from .vm import VM


@dataclass
class StoryInfo:
    name: str          # filename stem — the header has no name/author field
    version: int       # 3, 5, or 8
    release: int       # header bytes 2..3
    serial: str        # 6 ASCII chars, header bytes 18..23
    file_sha256: bytes


class Session:
    def __init__(self):
        self._vm = None
        self._story_info = None
        # handlers may be installed before load (the CLI does)
        self._save_handler = None
        self._restore_handler = None

    # ------------------------------------------------ lifecycle
    def load(self, path, seed=None, strict=False):
        """Load a story file and run to the first Prompt/EndOfGame."""
        try:
            story = StoryFile.load(path, strict=strict)
        except StoryFileError:
            raise
        data = story.data
        self._story_info = StoryInfo(
            name=os.path.splitext(os.path.basename(str(path)))[0],
            version=story.header.version,
            release=story.header.release,
            serial=story.header.serial,
            file_sha256=hashlib.sha256(data).digest(),
        )
        self._vm = VM(story, seed=seed)
        if self._save_handler is not None:
            self._vm.save_handler = self._save_handler
        if self._restore_handler is not None:
            self._vm.restore_handler = self._restore_handler
        return self._run_to_boundary()

    # ------------------------------------------------ input/output
    def input(self, line):
        """Feed one line; run to the next boundary; return the events."""
        vm = self._require_vm()
        if vm.done:
            return [Error("game over")]
        vm.feed(line)
        return self._run_to_boundary()

    def _run_to_boundary(self):
        vm = self._require_vm()
        vm.run_until_input()
        out = [e for e in vm.events]
        vm.events.clear()
        if vm.done:
            out.append(EndOfGame(vm.done_status))
        elif vm.needs_input:
            out.append(Prompt())
        return out

    # ------------------------------------------------ save/restore
    def save(self):
        """Opaque ZMSAVE v1 image of the current machine state."""
        vm = self._require_vm()
        return savefile.encode(vm)

    def restore(self, image):
        """Load a full image; run to the next boundary; return the events.
        Identity mismatch (different story file) raises SaveFileError."""
        vm = self._require_vm()
        savefile.decode(vm, image)
        return self._run_to_boundary()

    def restore_image(self, image):
        """Decode-only restore (no run): for the in-game @restore opcode
        handler — the VM is mid-turn and keeps running after it returns.
        Raises SaveFileError."""
        savefile.decode(self._require_vm(), image)

    # ------------------------------------------------ handlers / info
    def set_save_handler(self, cb):
        self._save_handler = cb
        if self._vm is not None:
            self._vm.save_handler = cb

    def set_restore_handler(self, cb):
        self._restore_handler = cb
        if self._vm is not None:
            self._vm.restore_handler = cb

    @property
    def story(self):
        if self._story_info is None:
            raise StoryFileError("no story loaded")
        return self._story_info

    @property
    def done(self):
        return bool(self._vm and self._vm.done)

    def _require_vm(self):
        if self._vm is None:
            raise StoryFileError("no story loaded")
        return self._vm