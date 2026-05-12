"""
Bluefish444 LTC reader — wraps BlueVelvetC64.dll via ctypes.
Available only when the Bluefish444 driver and hardware are present;
all calling code gracefully falls back to system clock when .available is False.
"""
import ctypes
import threading
import time

_BF_DLL_PATH = r'C:\Program Files\Bluefish444\Developer\driver\Release\BlueVelvetC64.dll'


class _LtcSyncStruct(ctypes.Structure):
    _fields_ = [
        ('TimeCodeValue',   ctypes.c_uint64),
        ('TimeCodeIsValid', ctypes.c_uint32),
        ('_pad',            ctypes.c_uint8 * 20),
    ]


class BluefishLTCReader:
    """
    Reads external LTC timecode from a Bluefish444 card via ctypes.
    bfcWaitExternalLtcInputSync (blocking) is called in a daemon thread.
    .available = False if DLL missing or card not found — all other code
    gracefully falls back to system clock.
    """

    # EXT LTC source connector constants (EBlueExternalLtcSource)
    CONNECTOR_BREAKOUT_HEADER = 0   # Epoch PCB header
    CONNECTOR_GENLOCK_BNC     = 1   # Reference/Genlock BNC (Epoch + Kronos)
    CONNECTOR_INTERLOCK       = 2   # Interlock MMCX (Kronos only)
    CONNECTOR_STEM_PORT       = 3   # STEM port (Kronos only)
    _EXTERNAL_LTC_SOURCE_SEL  = 120 # EXTERNAL_LTC_SOURCE_SELECTION property ID

    def __init__(self, dll_path: str = _BF_DLL_PATH):
        self.available   = False
        self.init_error  = ''   # populated with failure reason if init fails
        self.running     = False
        self._lock       = threading.Lock()
        self._h = self._m = self._s = self._f = 0
        self._valid      = False
        self._handle     = None
        self._dll        = None
        self._thread     = None
        self._connector  = self.CONNECTOR_INTERLOCK  # default: Interlock MMCX
        self._init_sdk(dll_path)

    def _init_sdk(self, dll_path: str):
        try:
            dll = ctypes.WinDLL(dll_path)
        except Exception as e:
            self.init_error = f'DLL load failed: {e}'
            return
        try:
            dll.bfcFactory.restype  = ctypes.c_void_p
            dll.bfcFactory.argtypes = []
            dll.bfcDestroy.restype  = None
            dll.bfcDestroy.argtypes = [ctypes.c_void_p]
            dll.bfcAttach.restype   = ctypes.c_int32
            dll.bfcAttach.argtypes  = [ctypes.c_void_p, ctypes.c_int32]
            dll.bfcDetach.restype   = ctypes.c_int32
            dll.bfcDetach.argtypes  = [ctypes.c_void_p]
            dll.bfcWaitExternalLtcInputSync.restype  = ctypes.c_int32
            dll.bfcWaitExternalLtcInputSync.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(_LtcSyncStruct)]
        except Exception as e:
            self.init_error = f'Function setup failed: {e}'
            return
        try:
            dll.bfcEnumerate.restype  = ctypes.c_int32
            dll.bfcEnumerate.argtypes = [ctypes.c_void_p,
                                         ctypes.POINTER(ctypes.c_int32)]
        except Exception:
            pass
        try:
            handle = dll.bfcFactory()
        except Exception as e:
            self.init_error = f'bfcFactory() raised: {e}'
            return
        if handle is None:
            self.init_error = 'bfcFactory() returned None (no card?)'
            return
        # Enumerate how many cards the driver sees
        card_count = ctypes.c_int32(0)
        try:
            dll.bfcEnumerate(handle, ctypes.byref(card_count))
        except Exception:
            pass
        n = card_count.value
        # bfcAttach uses 1-based device IDs (1 = first card, 2 = second, etc.)
        attached_idx = -1
        last_err = -1
        for idx in range(1, max(n, 1) + 1):
            try:
                last_err = dll.bfcAttach(handle, idx)
            except Exception as e:
                self.init_error = f'bfcAttach({idx}) raised: {e}'
                dll.bfcDestroy(handle)
                return
            if last_err == 0:
                attached_idx = idx
                break
        if attached_idx < 0:
            self.init_error = (
                f'bfcAttach() failed on all {max(n,1)} card(s) '
                f'(last err={last_err}). Driver sees {n} card(s).')
            dll.bfcDestroy(handle)
            return
        # Card attached successfully
        self._dll      = dll
        self._handle   = handle
        self.available = True
        # Apply LTC connector selection (failure here doesn't block availability)
        try:
            dll.bfcSetCardProperty32.restype  = ctypes.c_int32
            dll.bfcSetCardProperty32.argtypes = [
                ctypes.c_void_p, ctypes.c_int32, ctypes.c_uint32]
            dll.bfcSetCardProperty32(
                handle, self._EXTERNAL_LTC_SOURCE_SEL,
                ctypes.c_uint32(self._connector))
        except Exception as e:
            self.init_error = f'Connector select failed (card still ok): {e}'

    def set_connector(self, connector_idx: int):
        """Change the LTC input connector at runtime (0-3)."""
        self._connector = connector_idx
        if self._dll and self._handle:
            try:
                self._dll.bfcSetCardProperty32(
                    self._handle, self._EXTERNAL_LTC_SOURCE_SEL,
                    ctypes.c_uint32(connector_idx))
            except Exception:
                pass

    @staticmethod
    def _decode(value: int) -> tuple:
        return (
            ((value >> 56) & 0x3) * 10 + ((value >> 48) & 0xF),  # hours
            ((value >> 40) & 0x7) * 10 + ((value >> 32) & 0xF),  # minutes
            ((value >> 24) & 0x7) * 10 + ((value >> 16) & 0xF),  # seconds
            ((value >>  8) & 0x3) * 10 + ((value >>  0) & 0xF),  # frames
        )

    def start(self):
        if not self.available:
            return
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name='BluefishLTC')
        self._thread.start()

    def stop(self):
        self.running = False
        if self._dll and self._handle:
            try:
                self._dll.bfcDetach(self._handle)
                self._dll.bfcDestroy(self._handle)
            except Exception:
                pass
            self._handle = None

    def get(self) -> tuple:
        """Returns (h, m, s, f, valid)."""
        with self._lock:
            return self._h, self._m, self._s, self._f, self._valid

    def _run(self):
        sync = _LtcSyncStruct()
        while self.running and self._handle:
            try:
                err = self._dll.bfcWaitExternalLtcInputSync(
                    self._handle, ctypes.byref(sync))
                with self._lock:
                    if err == 0 and sync.TimeCodeIsValid:
                        self._h, self._m, self._s, self._f = self._decode(sync.TimeCodeValue)
                        self._valid = True
                    else:
                        self._valid = False
            except Exception:
                with self._lock:
                    self._valid = False
                time.sleep(0.1)
