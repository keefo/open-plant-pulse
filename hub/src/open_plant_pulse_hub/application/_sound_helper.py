"""A small process whose whole job is to make a sound, promptly.

AppKit wants a process with a run loop and one thread asking it for
things. The hub is neither: it is a threading HTTP server with a radio
loop in it, and NSSound played from a request thread came out broken
up. Spawning `afplay` for each sound instead costs 1.7 seconds for a
sound lasting half of one, which is a noticeable wait on a button and
a poor start for an alarm.

So the sound lives here. One process, started once, holding the
framework and every sound already loaded, reading names on stdin and
playing them on its own run loop. A sound then costs a write to a
pipe.

Protocol, a line at a time:

    <name>          play that sound now
    (eof)           stop and exit

It prints `ready` once it can play, and `ok <name>` or `no <name>`
after each line, so the hub knows whether to fall back.
"""

from __future__ import annotations

import os
import sys
import threading
import queue

SOUNDS_DIR = "/System/Library/Sounds"


def main() -> int:
    try:
        from AppKit import NSSound
        from Foundation import NSDate, NSRunLoop
    except ImportError:
        print("no appkit", flush=True)
        return 1

    held = {}
    for entry in sorted(os.listdir(SOUNDS_DIR)):
        if entry.endswith(".aiff"):
            name = entry[: -len(".aiff")]
            sound = NSSound.alloc().initWithContentsOfFile_byReference_(
                os.path.join(SOUNDS_DIR, entry), True)
            if sound is not None:
                held[name] = sound

    # Opening the audio device costs about a third of a second, and it
    # happens on the first play whenever that is. Done here, silently,
    # it is over before anybody asks for a sound.
    warm = next(iter(held.values()), None)
    if warm is not None:
        warm.setVolume_(0.0)
        warm.play()
        NSRunLoop.currentRunLoop().runUntilDate_(
            NSDate.dateWithTimeIntervalSinceNow_(0.4))
        warm.stop()
        warm.setVolume_(1.0)

    asked: "queue.Queue[str]" = queue.Queue()

    def listen() -> None:
        for line in sys.stdin:
            asked.put(line.strip())
        asked.put("")

    threading.Thread(target=listen, daemon=True).start()
    print("ready", flush=True)

    # The run loop runs on the main thread, which is where AppKit
    # wants to be, and the queue is drained between turns of it.
    while True:
        try:
            name = asked.get_nowait()
        except queue.Empty:
            NSRunLoop.currentRunLoop().runUntilDate_(
                NSDate.dateWithTimeIntervalSinceNow_(0.05))
            continue
        if not name:
            return 0
        sound = held.get(name)
        if sound is None:
            print(f"no {name}", flush=True)
            continue
        # Rewound first: play() on something already sounding is
        # ignored, which would swallow every repeat of an alarm.
        if sound.isPlaying():
            sound.stop()
        sound.play()
        print(f"ok {name}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
