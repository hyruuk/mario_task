"""Thread-safe block-stream NES audio playback over sounddevice.

stable-retro's NES emulator delivers an audio buffer every emulator
step (60 Hz, ~735 samples at 44.1 kHz). sounddevice's callback fires on
its own scheduler thread, asking for ``N`` samples whose count rarely
aligns with the emulator's block size. Without buffering, the callback
would either underflow (silence pops) or block the audio thread waiting
for new data (audible stutter).

:class:`SoundDeviceGameBlockStream` decouples the two by:

1. Holding a thread-safe queue of variable-size blocks (each one block =
   one emulator step's audio).
2. In the audio callback, copying from the *current* block until the
   callback's request is filled; if the current block runs out mid-fill,
   grabbing the next block from the queue under a lock and continuing.

Ported verbatim from upstream ``videogame.py:44-116`` — the queue +
double-buffer + lock semantics are correct and non-trivial; don't
simplify without re-thinking the failure modes.
"""

from __future__ import annotations

import queue
import threading

import numpy as np
import sounddevice
from psychopy import constants, logging


class SoundDeviceGameBlockStream:
    """Asynchronous audio renderer for variable-size emulator audio blocks."""

    def __init__(
        self,
        sample_rate: int,
        block_size: int = 0,
        channels: int = 2,
        dtype: np.dtype | type | str = sounddevice.default.dtype[1],
    ) -> None:
        self.blocks: queue.Queue = queue.Queue()
        # Seed with a short silent block so the first callback has something
        # to read while the emulator warms up.
        self.blocks.put(np.zeros((500, channels), dtype=dtype))
        self.lock = threading.Lock()
        self.output_stream = sounddevice.OutputStream(
            samplerate=sample_rate,
            blocksize=block_size,
            latency=0.1,
            device=None,
            channels=channels,
            callback=self._callback,
            dtype=dtype,
            prime_output_buffers_using_stream_callback=False,
        )
        self.current_block_idx = 0
        self.current_block: np.ndarray | None = None
        self.status = constants.STOPPED

    def _callback(self, outdata: np.ndarray, frames: int, time, status) -> None:  # noqa: ARG002
        if self.status == constants.STOPPED:
            return
        if self.blocks.empty() and self.current_block is None:
            outdata.fill(0)
            logging.debug("sound queue empty")
            return
        if self.current_block is None:
            with self.lock:
                self.current_block = self.blocks.get()

        out_idx = 0
        while True:
            current_block_len = self.current_block.shape[0]  # type: ignore[union-attr]
            split_idx = min(current_block_len - self.current_block_idx, frames - out_idx)
            split_end = self.current_block_idx + split_idx
            outdata[out_idx : out_idx + split_idx] = (
                self.current_block[self.current_block_idx : split_end]  # type: ignore[index]
            )
            out_idx += split_idx
            self.current_block_idx = split_end

            if split_end == current_block_len:
                # Need the next block. If the queue is dry, the callback ends
                # the iteration (filling the remainder with zeros via the
                # outer caller's prior fill of `outdata`).
                with self.lock:
                    try:
                        self.current_block = self.blocks.get(timeout=0.01)
                    except queue.Empty:
                        logging.debug("sound queue empty")
                        self.current_block = None
                self.current_block_idx = 0
                if self.current_block is None:
                    if out_idx < frames:
                        outdata[out_idx:].fill(0)
                    return
            if out_idx == frames:
                return

    def put(self, block: np.ndarray) -> None:
        """Enqueue one block of audio (one emulator step's worth)."""
        with self.lock:
            self.blocks.put(block)

    def play(self) -> None:
        self.status = constants.PLAYING
        self.output_stream.start()

    def stop(self) -> None:
        self.status = constants.STOPPED
        self.output_stream.stop()
        self.flush()

    def flush(self) -> None:
        """Empty the pending-block queue without draining the active stream."""
        self.blocks = queue.Queue()
