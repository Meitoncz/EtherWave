"""
EtherWave Windows Server - Default Audio Device Management

Windows equivalent of server/audio_engine.py's PipeWireSinkManager: instead
of creating/destroying a PipeWire null-sink, this points the Windows default
*output* device at VB-Audio Virtual Cable (the user installs it once, same
prerequisite category as PipeWire already being present on CachyOS) so all
system audio routes into it, and remembers the previous default to restore
on stop.

There is no first-party Windows CLI for changing the default audio device
(the Linux side shells out to `pactl set-default-sink`; there is no
`pactl`-equivalent here). This uses the undocumented-but-widely-used COM
`IPolicyConfig` interface (the same mechanism EarTrumpet, AudioDeviceCmdlets,
and pycaw use) via `comtypes`. GUIDs and vtable order are corroborated
against multiple independently published implementations and confirmed
working live against this project's own target (Windows 11, VB-CABLE 3.3.1.7)
during development -- see docs/WINDOWS_PORT.md.

Live-verified facts this module depends on (measured directly, not assumed --
see docs/WINDOWS_PORT.md's "Live testing findings" section):

- VB-CABLE installs as *two* separate Windows render (playback) endpoints:
  plain "CABLE Input" (fixed at whatever format Windows/VB-CABLE defaulted
  to, 2ch on a fresh install) and "CABLE In 16ch" (the one whose Advanced
  Default-Format dropdown actually offers multichannel formats). Only
  setting "CABLE In 16ch" as default actually gets multichannel system
  audio into the pipe -- "CABLE Input" stays capped. This module prefers a
  render endpoint with "16ch" in its name and falls back to any endpoint
  whose name contains "cable" (covers a VB-CABLE install/license that only
  ships the plain 2-channel-only variant).
  There is exactly one matching capture (recording) endpoint, "CABLE
  Output", which mirrors whichever render pin actually received audio.
- The capture side's channel count is NOT settable per-session from code
  (no working method found) -- it is whatever format is currently selected
  in Windows Sound Settings -> Recording -> CABLE Output -> Properties ->
  Advanced -> Default Format. Channel counts <= that configured value can
  still be opened (WASAPI shared-mode auto-converts/downmixes); more than
  that fails outright. See create_sink()'s channel check below.
"""

import ctypes
from ctypes import HRESULT, POINTER, byref, c_void_p, c_ushort, c_wchar_p, wintypes
import comtypes
from comtypes import COMMETHOD, GUID, IUnknown

from audio_engine import SUPPORTED_CHANNELS

# --- GUIDs (mmdeviceapi.h, publicly documented) ---------------------------
CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
IID_IMMDeviceEnumerator = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
IID_IMMDevice = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
IID_IMMDeviceCollection = GUID("{0BD7A1BE-7A1A-44DB-8397-CC5392387B5E}")
IID_IPropertyStore = GUID("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")

# Undocumented but stable across Windows 10/11 -- see module docstring.
CLSID_PolicyConfigClient = GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}")
IID_IPolicyConfig = GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")

eRender, eCapture = 0, 1
eConsole, eMultimedia, eCommunications = 0, 1, 2
DEVICE_STATE_ACTIVE = 0x1

PKEY_Device_FriendlyName_fmtid = GUID("{A45C254E-DF1C-4EFD-8020-67D146A850E0}")
PKEY_Device_FriendlyName_pid = 14

VT_LPWSTR = 31
ole32 = ctypes.OleDLL("ole32")


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]


PKEY_Device_FriendlyName = PROPERTYKEY(PKEY_Device_FriendlyName_fmtid,
                                        PKEY_Device_FriendlyName_pid)


class PROPVARIANT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("pwszVal", c_wchar_p), ("lVal", ctypes.c_long)]
    _fields_ = [("vt", c_ushort), ("wReserved1", c_ushort),
                ("wReserved2", c_ushort), ("wReserved3", c_ushort),
                ("u", _U)]


class IPropertyStore(IUnknown):
    _iid_ = IID_IPropertyStore
    _methods_ = (
        COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(wintypes.DWORD), "cProps")),
        COMMETHOD([], HRESULT, "GetAt",
                   (["in"], wintypes.DWORD, "iProp"),
                   (["out"], POINTER(PROPERTYKEY), "pkey")),
        COMMETHOD([], HRESULT, "GetValue",
                   (["in"], POINTER(PROPERTYKEY), "key"),
                   (["out"], POINTER(PROPVARIANT), "pv")),
        COMMETHOD([], HRESULT, "SetValue",
                   (["in"], POINTER(PROPERTYKEY), "key"),
                   (["in"], POINTER(PROPVARIANT), "propvar")),
        COMMETHOD([], HRESULT, "Commit"),
    )


class IMMDevice(IUnknown):
    _iid_ = IID_IMMDevice
    _methods_ = (
        COMMETHOD([], HRESULT, "Activate",
                   (["in"], POINTER(GUID), "iid"),
                   (["in"], wintypes.DWORD, "dwClsCtx"),
                   (["in"], c_void_p, "pActivationParams"),
                   (["out"], POINTER(c_void_p), "ppInterface")),
        COMMETHOD([], HRESULT, "OpenPropertyStore",
                   (["in"], wintypes.DWORD, "stgmAccess"),
                   (["out"], POINTER(POINTER(IPropertyStore)), "ppProperties")),
        COMMETHOD([], HRESULT, "GetId",
                   (["out"], POINTER(wintypes.LPWSTR), "ppstrId")),
        COMMETHOD([], HRESULT, "GetState",
                   (["out"], POINTER(wintypes.DWORD), "pdwState")),
    )


class IMMDeviceCollection(IUnknown):
    _iid_ = IID_IMMDeviceCollection
    _methods_ = (
        COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(wintypes.UINT), "pcDevices")),
        COMMETHOD([], HRESULT, "Item",
                   (["in"], wintypes.UINT, "nDevice"),
                   (["out"], POINTER(POINTER(IMMDevice)), "ppDevice")),
    )


class IMMDeviceEnumerator(IUnknown):
    _iid_ = IID_IMMDeviceEnumerator
    _methods_ = (
        COMMETHOD([], HRESULT, "EnumAudioEndpoints",
                   (["in"], wintypes.DWORD, "dataFlow"),
                   (["in"], wintypes.DWORD, "dwStateMask"),
                   (["out"], POINTER(POINTER(IMMDeviceCollection)), "ppDevices")),
        COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint",
                   (["in"], wintypes.DWORD, "dataFlow"),
                   (["in"], wintypes.DWORD, "role"),
                   (["out"], POINTER(POINTER(IMMDevice)), "ppEndpoint")),
        COMMETHOD([], HRESULT, "GetDevice",
                   (["in"], c_wchar_p, "pwstrId"),
                   (["out"], POINTER(POINTER(IMMDevice)), "ppDevice")),
        COMMETHOD([], HRESULT, "RegisterEndpointNotificationCallback",
                   (["in"], c_void_p, "pClient")),
        COMMETHOD([], HRESULT, "UnregisterEndpointNotificationCallback",
                   (["in"], c_void_p, "pClient")),
    )


class IPolicyConfig(IUnknown):
    # Only SetDefaultEndpoint is ever called through this; the first 8
    # vtable slots are format/period/share-mode methods this module doesn't
    # need -- placeholders are fine as long as the count and order match.
    _iid_ = IID_IPolicyConfig
    _methods_ = (
        COMMETHOD([], HRESULT, "Unused1"), COMMETHOD([], HRESULT, "Unused2"),
        COMMETHOD([], HRESULT, "Unused3"), COMMETHOD([], HRESULT, "Unused4"),
        COMMETHOD([], HRESULT, "Unused5"), COMMETHOD([], HRESULT, "Unused6"),
        COMMETHOD([], HRESULT, "Unused7"), COMMETHOD([], HRESULT, "Unused8"),
        COMMETHOD([], HRESULT, "GetPropertyValue",
                   (["in"], c_wchar_p, "pwstrDeviceId"),
                   (["in"], POINTER(PROPERTYKEY), "pKey"),
                   (["out"], POINTER(PROPVARIANT), "pValue")),
        COMMETHOD([], HRESULT, "SetPropertyValue",
                   (["in"], c_wchar_p, "pwstrDeviceId"),
                   (["in"], POINTER(PROPERTYKEY), "pKey"),
                   (["in"], POINTER(PROPVARIANT), "pValue")),
        COMMETHOD([], HRESULT, "SetDefaultEndpoint",
                   (["in"], c_wchar_p, "pwstrDeviceId"),
                   (["in"], wintypes.DWORD, "role")),
        COMMETHOD([], HRESULT, "SetEndpointVisibility",
                   (["in"], c_wchar_p, "pwstrDeviceId"),
                   (["in"], wintypes.BOOL, "bVisible")),
    )


def _friendly_name(device: "IMMDevice") -> str:
    store = device.OpenPropertyStore(0x00000000)  # STGM_READ
    pv = store.GetValue(byref(PKEY_Device_FriendlyName))
    name = pv.u.pwszVal if pv.vt == VT_LPWSTR else ""
    ole32.PropVariantClear(byref(pv))
    return name or ""


def _find_by_substring(enumerator, data_flow: int, substring: str):
    """Returns [(device_id, friendly_name), ...] for active endpoints of the
    given data flow whose friendly name contains substring (case-insensitive)."""
    collection = enumerator.EnumAudioEndpoints(data_flow, DEVICE_STATE_ACTIVE)
    results = []
    for i in range(collection.GetCount()):
        device = collection.Item(i)
        name = _friendly_name(device)
        if substring.lower() in name.lower():
            results.append((device.GetId(), name))
    return results


def _sounddevice_capture_index_for_name(friendly_name: str) -> int:
    """Cross-references a friendly name obtained via comtypes/IMMDevice
    against sounddevice's own device list -- sounddevice's device dict has
    no endpoint-ID field to match on, so name is the only shared key.
    Prefers the WASAPI host API entry (shared-mode capture is what this
    project wants -- see audio_engine.py) over the MME/DirectSound/WDM-KS
    duplicates Windows also exposes for the same physical endpoint."""
    import sounddevice as sd
    hostapis = sd.query_hostapis()
    wasapi_hostapi = next((i for i, h in enumerate(hostapis) if h["name"] == "Windows WASAPI"), None)
    devices = sd.query_devices()

    def matches(d, exact):
        if d["max_input_channels"] <= 0:
            return False
        return d["name"] == friendly_name if exact else friendly_name.lower() in d["name"].lower()

    for prefer_wasapi in (True, False):
        for exact in (True, False):
            for i, d in enumerate(devices):
                if prefer_wasapi and d["hostapi"] != wasapi_hostapi:
                    continue
                if matches(d, exact):
                    return i
    raise RuntimeError(
        f"sounddevice has no input device matching '{friendly_name}' -- "
        f"got: {[d['name'] for d in devices]}"
    )


class DefaultDeviceManager:
    """Windows equivalent of PipeWireSinkManager. Selects VB-Cable's
    multichannel-capable render endpoint ("CABLE In 16ch", falling back to
    plain "CABLE Input") as the system default output device, resolves the
    matching capture endpoint ("CABLE Output") for the capture thread to
    open, and restores the previous default on stop. Method names/shape
    mirror PipeWireSinkManager so server_windows/gui.py's call sites need
    minimal changes from server/gui.py's.
    """

    RENDER_NAME_PREFERRED_SUBSTRING = "16ch"
    NAME_SUBSTRING = "cable"

    def __init__(self):
        comtypes.CoInitialize()
        self._enumerator = comtypes.CoCreateInstance(
            CLSID_MMDeviceEnumerator, interface=IMMDeviceEnumerator,
            clsctx=comtypes.CLSCTX_ALL)
        self._policy_config = comtypes.CoCreateInstance(
            CLSID_PolicyConfigClient, interface=IPolicyConfig,
            clsctx=comtypes.CLSCTX_ALL)
        self._previous_default_id = None
        self._active_render_id = None
        self.capture_device_index = None   # resolved sounddevice index
        self.capture_device_name = None    # e.g. "CABLE Output (VB-Audio Virtual Cable)"

    @property
    def is_active(self) -> bool:
        return self._active_render_id is not None

    def _find_render_endpoint(self):
        matches = _find_by_substring(self._enumerator, eRender, self.NAME_SUBSTRING)
        if not matches:
            return None
        for dev_id, name in matches:
            if self.RENDER_NAME_PREFERRED_SUBSTRING in name.lower():
                return dev_id, name
        return matches[0]

    def create_sink(self, channels: int) -> str:
        if channels not in SUPPORTED_CHANNELS:
            raise ValueError(f"Unsupported channel count: {channels}")
        if self._active_render_id is not None:
            raise RuntimeError("Default device already changed; call remove_sink() first")

        render = self._find_render_endpoint()
        if render is None:
            raise RuntimeError(
                "No VB-Audio Virtual Cable playback device found. Is VB-CABLE installed?"
            )
        render_id, render_name = render

        capture_matches = _find_by_substring(self._enumerator, eCapture, self.NAME_SUBSTRING)
        if not capture_matches:
            raise RuntimeError(
                "VB-Cable's playback device was found but its matching recording "
                "device ('CABLE Output') was not -- installation looks incomplete."
            )
        _capture_id, capture_name = capture_matches[0]
        capture_index = _sounddevice_capture_index_for_name(capture_name)

        import sounddevice as sd
        configured = sd.query_devices(capture_index)["max_input_channels"]
        if channels > configured:
            raise RuntimeError(
                f"'{capture_name}' is currently configured for {configured} channel(s), "
                f"but {channels} were requested. Open Windows Sound Settings -> Recording "
                f"-> '{capture_name}' -> Properties -> Advanced, and pick a "
                f"'Channel {channels}' format at 48000 Hz (do the same on the Playback "
                f"side for '{render_name}'), then try again."
            )

        prev = self._enumerator.GetDefaultAudioEndpoint(eRender, eConsole)
        self._previous_default_id = prev.GetId()

        for role in (eConsole, eMultimedia, eCommunications):
            self._policy_config.SetDefaultEndpoint(render_id, role)
        self._active_render_id = render_id

        # Measured directly during development: opening a WASAPI capture
        # stream on this device immediately after the SetDefaultEndpoint
        # calls above fails deterministically with a spurious PortAudio
        # host error ("GetNameFromCategory: usbTerminalGUID = ...", from
        # the WDM-KS backend) -- PortAudio's own internal device/host-API
        # tables are built once at first use and go stale relative to a
        # default-device change made behind its back via COM. Forcing
        # PortAudio to tear down and rebuild those tables here fixes it
        # reliably (confirmed: a stream opens successfully on the very next
        # attempt after this, with no delay needed) -- without it, even
        # several retries with delays between them all fail identically.
        # `_terminate`/`_initialize` are undocumented but have been stable
        # across the sounddevice versions checked; audio_engine.py's own
        # STREAM_OPEN_RETRIES stays in place regardless as a defensive
        # backstop in case that ever changes.
        sd._terminate()
        sd._initialize()

        self.capture_device_name = capture_name
        self.capture_device_index = capture_index
        return render_id

    def remove_sink(self):
        if self._previous_default_id is not None:
            for role in (eConsole, eMultimedia, eCommunications):
                self._policy_config.SetDefaultEndpoint(self._previous_default_id, role)
            self._previous_default_id = None
        self._active_render_id = None
        self.capture_device_index = None
        self.capture_device_name = None
