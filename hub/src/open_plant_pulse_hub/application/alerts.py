"""Getting a message to somebody who is not in the house.

Everything else the hub says, it says on a page somebody has to open.
An alarm is different: water on a floor matters most to the person who
is out, and a notice nobody sees until they get home is not a notice.

One endpoint, given as a whole URL -- https://ntfy.sh/<topic>, or the
same path on a server of your own. A URL covers both without a second
setting asking which, and ntfy wants nothing but a POST.

Sent on a thread of its own. The call that raises the alarm is the
radio's own callback, and a phone network having a slow afternoon must
not hold up the frame behind it.
"""

from __future__ import annotations

import logging
import os
import shutil
import ssl
import sys
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional
from urllib import error, request

LOG = logging.getLogger(__name__)

# Long enough for a round trip over a bad connection, short enough that
# three of them still finish while somebody is reading the first word.
TIMEOUT_SECONDS = 8
ATTEMPTS = 3
# A leak is worth waking a phone for; ntfy's 5 is "max", which rings
# and lights the screen rather than sitting in the tray.
URGENT = "5"


def _trusted() -> ssl.SSLContext:
    """A context that can actually verify a certificate.

    This Python ships without certificates -- `get_default_verify_
    paths().cafile` is None -- so every HTTPS call failed with
    CERTIFICATE_VERIFY_FAILED and no alert could leave the house.
    macOS keeps a bundle at /etc/ssl/cert.pem, which is used when the
    default context has none of its own.

    Verification is never turned off. The topic in the address is the
    only thing keeping anybody else off the channel, so sending it
    without checking who answered would give it away.
    """
    context = ssl.create_default_context()
    if context.cert_store_stats().get("x509_ca"):
        return context
    for bundle in ("/etc/ssl/cert.pem", "/usr/local/etc/ca-certificates/cert.pem"):
        if os.path.exists(bundle):
            try:
                return ssl.create_default_context(cafile=bundle)
            except (OSError, ssl.SSLError):
                continue
    LOG.warning("no certificate bundle found; HTTPS alerts will not send")
    return context


SOUNDS_DIR = "/System/Library/Sounds"


class Sounder:
    """Makes a noise in the house, which a push notification cannot.

    A phone is right when nobody is in. A sound from the machine the
    hub runs on is right when somebody is, and which sound says which
    device without anybody reading a screen.

    `afplay` rather than anything clever: it is on every Mac, it plays
    the system sounds people already recognise, and the hub runs in a
    login session that has audio -- a launchd agent under
    `gui/<uid>`, which is why this works at all.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # One repeating sound per rule, so two rules ringing at once
        # can be stopped separately and a rule cannot stack on itself.
        self._ringing: Dict[int, threading.Event] = {}
        self._player = shutil.which("afplay")
        # The helper process that actually makes the noise.
        self._helper: Optional[subprocess.Popen] = None
        self._helper_lock = threading.Lock()
        # Started now rather than on the first alarm: it loads AppKit
        # and opens the audio device, about a second in all.
        threading.Thread(target=self._warm, name="sound-warm",
                         daemon=True).start()

    def _warm(self) -> None:
        """Bring the helper up before anything needs it."""
        with self._helper_lock:
            self._start_helper()

    @property
    def available(self) -> bool:
        return self._player is not None

    def sounds(self) -> List[str]:
        """What this machine can actually play."""
        if not os.path.isdir(SOUNDS_DIR):
            return []
        return sorted(name[: -len(".aiff")] for name in os.listdir(SOUNDS_DIR)
                      if name.endswith(".aiff"))

    def play(self, sound: str) -> bool:
        """One sound, now. False if it could not be played.

        Through the helper process, which holds AppKit and a run loop
        of its own. Played from inside this process the sound came out
        broken up: a threading HTTP server with a radio loop in it is
        not what AppKit expects to be asked from. Spawning `afplay`
        per sound is clean but costs 1.7 seconds for a sound lasting
        half of one.
        """
        if self._ask_helper(sound):
            return True
        # No helper, or it died. A slow sound beats no sound.
        path = os.path.join(SOUNDS_DIR, f"{sound}.aiff")
        if not self._player or not os.path.exists(path):
            LOG.warning("cannot play %r", sound)
            return False
        try:
            subprocess.Popen([self._player, path],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True
        except OSError:
            LOG.exception("could not play %r", sound)
            return False

    def _ask_helper(self, sound: str) -> bool:
        """Ask the helper for a sound, starting it if it is not up."""
        with self._helper_lock:
            helper = self._helper
            if helper is None or helper.poll() is not None:
                helper = self._start_helper()
                if helper is None:
                    return False
            try:
                helper.stdin.write(sound + "\n")
                helper.stdin.flush()
                answer = helper.stdout.readline().strip()
            except (OSError, ValueError):
                LOG.warning("the sound helper stopped answering")
                self._helper = None
                return False
            if answer.startswith("ok "):
                return True
            LOG.warning("the sound helper said %r", answer)
            return False

    def _start_helper(self) -> Optional[subprocess.Popen]:
        """Bring the helper up and wait for it to say it can play.

        Waited for, because the alternative is the first alarm racing
        a process that has not opened the audio device yet.
        """
        script = os.path.join(os.path.dirname(__file__), "_sound_helper.py")
        if not os.path.exists(script):
            return None
        try:
            helper = subprocess.Popen(
                [sys.executable, script], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1)
        except OSError:
            LOG.exception("could not start the sound helper")
            return None
        ready = helper.stdout.readline().strip()
        if ready != "ready":
            LOG.info("no sound helper (%s); falling back to afplay", ready)
            helper.kill()
            return None
        self._helper = helper
        LOG.info("sound helper ready")
        return helper

    def start(self, rule_id: int, sound: str, every_seconds: int) -> None:
        """Sound once, or keep sounding until stopped.

        Restarting a rule that is already ringing stops the old one
        first, so a sensor that reports twice does not end up with two
        threads playing over each other.
        """
        self.stop(rule_id)
        if not every_seconds:
            self.play(sound)
            return
        done = threading.Event()
        with self._lock:
            self._ringing[rule_id] = done
        def ring() -> None:
            while not done.is_set():
                self.play(sound)
                # Waits rather than sleeps, so stop is immediate
                # rather than up to a repeat away.
                done.wait(every_seconds)
        threading.Thread(target=ring, name=f"alert-{rule_id}",
                         daemon=True).start()

    def stop(self, rule_id: int) -> None:
        with self._lock:
            done = self._ringing.pop(rule_id, None)
        if done is not None:
            done.set()

    def stop_all(self) -> None:
        with self._lock:
            ringing = list(self._ringing.values())
            self._ringing.clear()
        for done in ringing:
            done.set()

    def ringing(self) -> List[int]:
        """Which rules are sounding, so a page can offer to stop them."""
        with self._lock:
            return sorted(self._ringing)


class Alerts:
    """Sends a line of text to a phone, or does nothing if unconfigured."""

    def __init__(self, url: Optional[str] = None) -> None:
        self._url = (url or "").strip()
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        with self._lock:
            return bool(self._url)

    def set_url(self, url: Optional[str]) -> None:
        with self._lock:
            self._url = (url or "").strip()

    def send(self, title: str, message: str, *, urgent: bool = False,
             tags: str = "", block: bool = False) -> Optional[bool]:
        """Raise an alarm, without making the caller wait for a phone.

        block is for the test button on the settings page, which has a
        person watching it and wants to say whether it worked.
        """
        with self._lock:
            url = self._url
        if not url:
            LOG.debug("no alert endpoint set; not sending %r", title)
            return False if block else None
        if block:
            return self._post(url, title, message, urgent, tags)
        threading.Thread(
            target=self._post, args=(url, title, message, urgent, tags),
            name="alert", daemon=True,
        ).start()
        return None

    def _post(self, url: str, title: str, message: str,
              urgent: bool, tags: str) -> bool:
        headers = {
            # Latin-1 is all an HTTP header may carry, and a title is the
            # one part a phone shows before it is unlocked, so anything
            # outside it is dropped rather than left to raise.
            "Title": title.encode("ascii", "replace").decode("ascii"),
            "Content-Type": "text/plain; charset=utf-8",
        }
        if urgent:
            headers["Priority"] = URGENT
        if tags:
            headers["Tags"] = tags
        body = message.encode("utf-8")
        last: Optional[Exception] = None
        for attempt in range(1, ATTEMPTS + 1):
            try:
                post = request.Request(url, data=body, headers=headers,
                                       method="POST")
                with request.urlopen(post, timeout=TIMEOUT_SECONDS,
                                     context=_trusted()) as answer:
                    if 200 <= answer.status < 300:
                        LOG.info("alert sent: %s", title)
                        return True
                    last = RuntimeError(f"the server answered {answer.status}")
            except (error.URLError, OSError, ValueError) as problem:
                last = problem
            LOG.warning("alert attempt %d of %d failed: %s",
                        attempt, ATTEMPTS, last)
        # Said plainly in the log rather than raised: the caller is a
        # radio frame handler and there is nothing for it to do about a
        # phone that cannot be reached.
        LOG.error("could not send the alert %r: %s", title, last)
        return False

    def check(self) -> Optional[str]:
        """Send a test message, and say what went wrong if it did not go.

        Returns None when it worked, and the reason otherwise, because
        a settings page offering a test button has to say more than that
        something happened.
        """
        with self._lock:
            url = self._url
        if not url:
            return "No address set."
        try:
            post = request.Request(
                url, data=b"The hub can reach this phone.",
                headers={"Title": "Open Plant Pulse", "Tags": "white_check_mark"},
                method="POST")
            with request.urlopen(post, timeout=TIMEOUT_SECONDS,
                                     context=_trusted()) as answer:
                if 200 <= answer.status < 300:
                    return None
                return f"The server answered {answer.status}."
        except error.HTTPError as problem:
            return f"The server answered {problem.code}."
        except (error.URLError, OSError, ValueError) as problem:
            return f"Could not reach it: {problem}"
