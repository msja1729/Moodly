"""Display actuator: screen colour temperature, ramped gradually.

Windows: GDI gamma ramps (SetDeviceGammaRamp) on every attached display.
Windows refuses ramps that stray too far from identity, which usually blocks
anything below ~3400K; set the DWORD
HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\ICM\\GdiIcmGammaRange = 256
and sign out to unlock the full range. A refused ramp raises the floor
automatically instead of failing.
Linux: `xrandr --output <name> --gamma r:g:b` on every connected output.
"""
from __future__ import annotations

import logging
import math
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Mapping, Protocol

log = logging.getLogger(__name__)

NEUTRAL_K = 6500


def _kelvin_to_rgb_raw(kelvin: float) -> tuple[float, float, float]:
    """Tanner Helland's blackbody approximation, channels in [0, 1]."""
    t = kelvin / 100.0
    if t <= 66:
        r = 255.0
        g = 99.4708025861 * math.log(t) - 161.1195681661
    else:
        r = 329.698727446 * (t - 60) ** -0.1332047592
        g = 288.1221695283 * (t - 60) ** -0.0755148492
    if t >= 66:
        b = 255.0
    elif t <= 19:
        b = 0.0
    else:
        b = 138.5177312231 * math.log(t - 10) - 305.0447927307
    return tuple(min(max(c, 0.0), 255.0) / 255.0 for c in (r, g, b))


def kelvin_to_rgb(kelvin: float) -> tuple[float, float, float]:
    """Channel multipliers, normalised so 6500K is exactly (1, 1, 1)."""
    kelvin = min(max(kelvin, 1000.0), 40000.0)
    raw = _kelvin_to_rgb_raw(kelvin)
    ref = _kelvin_to_rgb_raw(NEUTRAL_K)
    return tuple(min(c / r, 1.0) for c, r in zip(raw, ref))


def build_ramp(kelvin: float) -> list[list[int]]:
    mult = kelvin_to_rgb(kelvin)
    return [[min(65535, int(i * 257 * m)) for i in range(256)] for m in mult]


class DisplayBackend(Protocol):
    def apply(self, kelvin: float) -> bool: ...
    def restore(self) -> None: ...


class NullBackend:
    def __init__(self):
        self.applied: list[float] = []

    def apply(self, kelvin: float) -> bool:
        self.applied.append(kelvin)
        return True

    def restore(self) -> None:
        pass


class WindowsGammaBackend:
    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.user32 = ctypes.windll.user32
        self.gdi32 = ctypes.windll.gdi32
        self.gdi32.CreateDCW.restype = ctypes.c_void_p
        self.gdi32.CreateDCW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p]
        self.gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
        self.gdi32.SetDeviceGammaRamp.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.gdi32.GetDeviceGammaRamp.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.Ramp = ctypes.c_ushort * 256 * 3

        class DISPLAY_DEVICEW(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32),
                        ("DeviceString", wintypes.WCHAR * 128), ("StateFlags", wintypes.DWORD),
                        ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128)]

        self.devices: list[str] = []
        i = 0
        while True:
            dd = DISPLAY_DEVICEW()
            dd.cb = ctypes.sizeof(dd)
            if not self.user32.EnumDisplayDevicesW(None, i, ctypes.byref(dd), 0):
                break
            if dd.StateFlags & 0x1:  # DISPLAY_DEVICE_ATTACHED_TO_DESKTOP
                self.devices.append(dd.DeviceName)
            i += 1
        self.originals: dict[str, object] = {}

    def _with_dc(self, device: str, fn):
        hdc = self.gdi32.CreateDCW(None, device, None, None)
        if not hdc:
            return False
        try:
            return fn(hdc)
        finally:
            self.gdi32.DeleteDC(hdc)

    def apply(self, kelvin: float) -> bool:
        ramp = self.Ramp()
        for c, channel in enumerate(build_ramp(kelvin)):
            for i, v in enumerate(channel):
                ramp[c][i] = v
        ok = bool(self.devices)
        for dev in self.devices:
            if dev not in self.originals:
                orig = self.Ramp()
                if self._with_dc(dev, lambda h: self.gdi32.GetDeviceGammaRamp(h, self.ctypes.byref(orig))):
                    self.originals[dev] = orig
            ok &= bool(self._with_dc(dev, lambda h: self.gdi32.SetDeviceGammaRamp(h, self.ctypes.byref(ramp))))
        return ok

    def restore(self) -> None:
        for dev, orig in self.originals.items():
            self._with_dc(dev, lambda h: self.gdi32.SetDeviceGammaRamp(h, self.ctypes.byref(orig)))


class XrandrBackend:
    def __init__(self):
        out = subprocess.run(["xrandr", "--query"], capture_output=True, text=True, timeout=2).stdout
        self.outputs = [line.split()[0] for line in out.splitlines() if " connected" in line]

    def apply(self, kelvin: float) -> bool:
        r, g, b = (max(c, 0.1) for c in kelvin_to_rgb(kelvin))
        ok = True
        for o in self.outputs:
            res = subprocess.run(["xrandr", "--output", o, "--gamma", f"{r:.3f}:{g:.3f}:{b:.3f}"],
                                 capture_output=True, timeout=2)
            ok &= res.returncode == 0
        return ok and bool(self.outputs)

    def restore(self) -> None:
        for o in self.outputs:
            subprocess.run(["xrandr", "--output", o, "--gamma", "1:1:1"], capture_output=True, timeout=2)


def default_backend() -> DisplayBackend:
    try:
        if sys.platform == "win32":
            return WindowsGammaBackend()
        if sys.platform.startswith("linux") and shutil.which("xrandr"):
            return XrandrBackend()
    except Exception as e:
        log.warning("display backend unavailable: %s", e)
    return NullBackend()


@dataclass(frozen=True)
class DisplayParams:
    enabled: bool = True
    step_kelvin: float = 100.0
    step_interval_s: float = 60.0
    min_kelvin: float = 3400.0
    max_kelvin: float = 6500.0

    @classmethod
    def from_config(cls, cfg: Mapping) -> "DisplayParams":
        d = cfg.get("display", {})
        return cls(**{k: type(getattr(cls, k))(v) for k, v in d.items() if k in cls.__dataclass_fields__})


class DisplayActuator:
    """Moves the screen temperature toward a target by `step_kelvin` per step()."""

    def __init__(self, backend: DisplayBackend, params: DisplayParams = DisplayParams()):
        self.backend = backend
        self.params = params
        self.floor = params.min_kelvin
        self.current = float(NEUTRAL_K)
        self.target = float(NEUTRAL_K)
        self.touched = False

    def set_target(self, kelvin: float) -> None:
        self.target = min(max(float(kelvin), self.floor), self.params.max_kelvin)

    def step(self) -> float | None:
        """Take one step toward the target; returns the applied Kelvin or None."""
        if not self.params.enabled or self.current == self.target:
            return None
        delta = self.target - self.current
        nxt = self.current + math.copysign(min(abs(delta), self.params.step_kelvin), delta)
        if self.backend.apply(nxt):
            self.current = nxt
            self.touched = True
            return nxt
        # The OS refused this ramp: treat it as the warmest we can go.
        self.floor = min(nxt + self.params.step_kelvin, NEUTRAL_K)
        self.target = max(self.target, self.floor)
        log.warning("display refused %.0fK; limiting to >= %.0fK", nxt, self.floor)
        return None

    def restore(self) -> None:
        if self.touched:
            self.backend.restore()
            self.touched = False
