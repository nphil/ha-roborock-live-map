"""Low-latency remote control session (app_rc_start / app_rc_move / app_rc_end).

The browser card sends "move" on every joystick change (throttled to 20 Hz)
plus a heartbeat every 250 ms while held. The session keeps its OWN tight
loop re-applying the last vector every RC_TICK (150 ms) with a robot-side
duration of RC_MOVE_DURATION_MS (400 ms), so consecutive commands overlap and
motion stays smooth even when a browser tick is late. Measured round trip on
the local stack: start 58 ms, moves 5-45 ms (avg ~16 ms), end 4 ms.

Safety net (dead-man watchdog): if no "move" arrives for RC_DEADMAN (600 ms)
the session sends a zero vector once and stops re-sending; if nothing arrives
for RC_IDLE_END (60 s) it ends remote control entirely. "stop" cancels the
loop and sends app_rc_end.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.components.roborock.coordinator import RoborockDataUpdateCoordinator
from roborock.roborock_typing import RoborockCommand

from .const import (
    RC_DEADMAN,
    RC_IDLE_END,
    RC_MOVE_DURATION_MS,
    RC_OMEGA_LIMIT,
    RC_TICK,
    RC_VELOCITY_LIMIT,
)

_LOGGER = logging.getLogger(__name__)

_MAX_LOOP_ERRORS = 5


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


class RemoteSession:
    """One vacuum's remote-control driving session."""

    def __init__(self, coordinator: RoborockDataUpdateCoordinator) -> None:
        """Initialize an idle session."""
        self._command = coordinator.properties_api.command
        self._duid_slug = coordinator.duid_slug
        self._task: asyncio.Task | None = None
        self._velocity = 0.0
        self._omega = 0.0
        self._last_input = 0.0
        self._seqnum = 0
        self._zero_sent = True
        self._loop_errors = 0
        self.active = False

    async def handle(self, action: str, velocity: float, omega: float) -> dict[str, Any]:
        """Run one WS action; returns {"ok": bool, "latency_ms": float}."""
        if action == "start":
            return await self._start()
        if action == "move":
            return await self._move(velocity, omega)
        if action == "stop":
            return await self._stop()
        return {"ok": False, "latency_ms": 0.0, "error": f"unknown action {action!r}"}

    async def _start(self) -> dict[str, Any]:
        if self.active:
            # Idempotent: a second start just proves the card is still alive.
            self._last_input = time.monotonic()
            return {"ok": True, "latency_ms": 0.0}
        started = time.monotonic()
        try:
            await self._command.send(RoborockCommand.APP_RC_START, [])
        except Exception as err:  # noqa: BLE001 - reported to the caller
            return {"ok": False, "latency_ms": _ms(started), "error": str(err)}
        self._velocity = 0.0
        self._omega = 0.0
        self._zero_sent = True
        self._loop_errors = 0
        self._last_input = time.monotonic()
        self.active = True
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name=f"rc_loop_{self._duid_slug}")
        return {"ok": True, "latency_ms": _ms(started)}

    async def _move(self, velocity: float, omega: float) -> dict[str, Any]:
        if not self.active:
            return {
                "ok": False,
                "latency_ms": 0.0,
                "error": "remote session not started",
            }
        self._velocity = round(_clamp(velocity, RC_VELOCITY_LIMIT), 3)
        self._omega = round(_clamp(omega, RC_OMEGA_LIMIT), 3)
        self._last_input = time.monotonic()
        started = time.monotonic()
        try:
            await self._send_move()
        except Exception as err:  # noqa: BLE001 - reported to the caller
            return {"ok": False, "latency_ms": _ms(started), "error": str(err)}
        return {"ok": True, "latency_ms": _ms(started)}

    async def _stop(self) -> dict[str, Any]:
        if not self.active:
            # Idempotent stop: the watchdog may have ended the session already.
            return {"ok": True, "latency_ms": 0.0}
        self.active = False
        if self._task is not None:
            self._task.cancel()
            self._task = None
        started = time.monotonic()
        try:
            await self._send_move(force_zero=True)
            await self._command.send(RoborockCommand.APP_RC_END, [])
        except Exception as err:  # noqa: BLE001 - reported to the caller
            return {"ok": False, "latency_ms": _ms(started), "error": str(err)}
        return {"ok": True, "latency_ms": _ms(started)}

    async def async_shutdown(self) -> None:
        """Best-effort stop, used when the config entry unloads."""
        if not self.active:
            return
        self.active = False
        if self._task is not None:
            self._task.cancel()
            self._task = None
        try:
            await self._send_move(force_zero=True)
            await self._command.send(RoborockCommand.APP_RC_END, [])
        except Exception as err:  # noqa: BLE001 - shutdown must not raise
            _LOGGER.debug("Remote shutdown for %s failed: %s", self._duid_slug, err)

    async def _send_move(self, force_zero: bool = False) -> None:
        velocity = 0.0 if force_zero else self._velocity
        omega = 0.0 if force_zero else self._omega
        self._seqnum = (self._seqnum + 1) % 100000
        await self._command.send(
            RoborockCommand.APP_RC_MOVE,
            [
                {
                    "omega": omega,
                    "velocity": velocity,
                    "duration": RC_MOVE_DURATION_MS,
                    "seqnum": self._seqnum,
                }
            ],
        )
        self._zero_sent = velocity == 0.0 and omega == 0.0

    async def _loop(self) -> None:
        """Re-apply the last vector; enforce the dead-man and idle limits."""
        try:
            while self.active:
                await asyncio.sleep(RC_TICK)
                if not self.active:
                    break
                idle = time.monotonic() - self._last_input
                if idle > RC_IDLE_END:
                    _LOGGER.debug(
                        "Remote session for %s idle %.0f s, ending", self._duid_slug, idle
                    )
                    await self._end_session()
                    return
                if idle > RC_DEADMAN:
                    # Dead-man: stop the robot once, then stay quiet.
                    if not self._zero_sent:
                        await self._guarded(self._send_move, force_zero=True)
                    continue
                if self._velocity == 0.0 and self._omega == 0.0 and self._zero_sent:
                    continue  # already stopped; nothing to re-apply
                await self._guarded(self._send_move)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - never leak out of the task
            _LOGGER.debug("Remote loop for %s died: %s", self._duid_slug, err)
            self.active = False

    async def _guarded(self, coro_fn, **kwargs) -> None:
        try:
            await coro_fn(**kwargs)
            self._loop_errors = 0
        except Exception as err:  # noqa: BLE001 - count and give up after a while
            self._loop_errors += 1
            _LOGGER.debug(
                "Remote loop send for %s failed (%d): %s",
                self._duid_slug,
                self._loop_errors,
                err,
            )
            if self._loop_errors >= _MAX_LOOP_ERRORS:
                await self._end_session()

    async def _end_session(self) -> None:
        self.active = False
        try:
            await self._send_move(force_zero=True)
            await self._command.send(RoborockCommand.APP_RC_END, [])
        except Exception as err:  # noqa: BLE001 - best effort
            _LOGGER.debug("Remote end for %s failed: %s", self._duid_slug, err)


def _ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 1)
