import ctypes
import ctypes.wintypes as wintypes
import struct
import time
import random
from offsets import *
client = Client()
# minimal rights for reading/writing
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008
PROCESS_QUERY_INFORMATION = 0x0400

TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPMODULE = 0x00000008

class Offsets:
    wLocalPlayerPawn = client.offset('dwLocalPlayerPawn')
    dwEntityList = client.offset('dwEntityList')
    m_iTeamNum = client.get('C_BaseEntity', 'm_iTeamNum')
    m_hPlayerPawn = client.get('CCSPlayerController', 'm_hPlayerPawn')
    m_lifeState = client.get('C_BaseEntity', 'm_lifeState')
    m_Glow = client.get('C_BaseModelEntity', 'm_Glow')
    m_glowColorOverride = client.get('CGlowProperty', 'm_glowColorOverride')
    m_bGlowing = client.get('CGlowProperty', 'm_bGlowing')
    m_iGlowType = client.get('CGlowProperty', 'm_iGlowType')

class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_char * wintypes.MAX_PATH),
    ]

class MODULEENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD), ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
        ("modBaseSize", wintypes.DWORD), ("hModule", wintypes.HMODULE),
        ("szModule", ctypes.c_char * 256), ("szExePath", ctypes.c_char * wintypes.MAX_PATH),
    ]

class CS2GlowManager:
    def __init__(self, process_name=b"cs2.exe", module_name=b"client.dll"):
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.process_name = process_name
        self.module_name = module_name
        self.pid = self._get_pid()
        # minimal permissions
        self.handle = self.k32.OpenProcess(PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION | PROCESS_QUERY_INFORMATION, False, self.pid)
        if not self.handle:
            raise Exception("Failed to open process handle")
        self.client = self._get_module_base()
        if not self.client:
            raise Exception("Failed to find module base")

        # Toggle state and previous Alt state
        self.enabled = True  # mặc định bật hack; đổi nếu muốn tắt khi start
        self._prev_alt_down = False

    def _get_pid(self):
        snapshot = self.k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == -1:
            raise Exception("Failed to create process snapshot")
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)

        success = self.k32.Process32First(snapshot, ctypes.byref(entry))
        while success:
            # compare bytes ignoring case
            try_name = bytes(entry.szExeFile).split(b'\x00',1)[0]
            if try_name[:len(self.process_name)].lower() == self.process_name.lower():
                self.k32.CloseHandle(snapshot)
                return entry.th32ProcessID
            success = self.k32.Process32Next(snapshot, ctypes.byref(entry))
        self.k32.CloseHandle(snapshot)
        raise Exception("Process not found")

    def _get_module_base(self):
        snap = self.k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE, self.pid)
        if snap == -1:
            return None
        module = MODULEENTRY32()
        module.dwSize = ctypes.sizeof(MODULEENTRY32)
        success = self.k32.Module32First(snap, ctypes.byref(module))
        while success:
            mod_name = bytes(module.szModule).split(b'\x00',1)[0]
            if mod_name[:len(self.module_name)].lower() == self.module_name.lower():
                self.k32.CloseHandle(snap)
                return ctypes.cast(module.modBaseAddr, ctypes.c_void_p).value
            success = self.k32.Module32Next(snap, ctypes.byref(module))
        self.k32.CloseHandle(snap)
        return None

    def _read(self, addr, size):
        buf = ctypes.create_string_buffer(size)
        bytes_read = ctypes.c_size_t()
        if not self.k32.ReadProcessMemory(self.handle, ctypes.c_void_p(addr), buf, size, ctypes.byref(bytes_read)):
            return None
        if bytes_read.value != size:
            return None
        return buf.raw

    def _write(self, addr, data):
        buf = ctypes.create_string_buffer(data)
        bytes_written = ctypes.c_size_t()
        if not self.k32.WriteProcessMemory(self.handle, ctypes.c_void_p(addr), buf, len(data), ctypes.byref(bytes_written)):
            return False
        return bytes_written.value == len(data)

    def _read_i(self, addr):
        data = self._read(addr, 4)
        return struct.unpack("i", data)[0] if data else 0

    def _read_u(self, addr):
        data = self._read(addr, 4)
        return struct.unpack("I", data)[0] if data else 0

    def _read_ull(self, addr):
        data = self._read(addr, 8)
        return struct.unpack("Q", data)[0] if data else 0

    def _write_u(self, addr, val):
        return self._write(addr, struct.pack("I", val))

    def _to_argb(self, r, g, b, a):
        clamp = lambda x: max(0, min(1, x))
        r, g, b, a = [int(clamp(c) * 255) for c in (r, g, b, a)]
        return (a << 24) | (r << 16) | (g << 8) | b

    def _get_local_team(self):
        local = self._read_ull(self.client + Offsets.wLocalPlayerPawn)
        if local == 0:
            return None
        return self._read_i(local + Offsets.m_iTeamNum)

    def update_glow(self):
        # Nếu disabled, không làm gì
        if not self.enabled:
            return

        local = self._read_ull(self.client + Offsets.wLocalPlayerPawn)
        entity_list = self._read_ull(self.client + Offsets.dwEntityList)
        team_local = self._get_local_team()

        if not local or not entity_list or team_local is None:
            return

        for i in range(64):
            entry = self._read_ull(entity_list + 0x10)
            if not entry:
                continue

            controller = self._read_ull(entry + i * 0x78)
            if not controller:
                continue

            pawn_handle = self._read_i(controller + Offsets.m_hPlayerPawn)
            if not pawn_handle:
                continue

            entry2 = self._read_ull(entity_list + 0x8 * ((pawn_handle & 0x7FFF) >> 9) + 0x10)
            if not entry2:
                continue

            pawn = self._read_ull(entry2 + 0x78 * (pawn_handle & 0x1FF))
            if not pawn or pawn == local:
                continue

            life_state = self._read_u(pawn + Offsets.m_lifeState)
            if life_state != 256:
                continue

            is_team = self._read_i(pawn + Offsets.m_iTeamNum) == team_local
            color = (1.0, 0.0, 0.0, 1.0) if is_team else (0.0, 0.0, 1.0, 1.0)

            glow = pawn + Offsets.m_Glow
            self._write_u(glow + Offsets.m_glowColorOverride, self._to_argb(*color))
            self._write_u(glow + Offsets.m_bGlowing, 1)
            self._write_u(glow + Offsets.m_iGlowType, 3)

    def _check_toggle_keys(self):
        """
        Kiểm tra Alt key (VK_MENU = 0x12).
        Toggle khi phím mới được nhấn xuống (edge detect).
        """
        VK_MENU = 0x12  # Alt
        # GetAsyncKeyState returns short; high bit set when currently down
        state = self.user32.GetAsyncKeyState(VK_MENU)
        alt_down = bool(state & 0x8000)
        # edge detect: chỉ khi alt_down True và prev False
        if alt_down and not self._prev_alt_down:
            # toggle
            self.enabled = not self.enabled
            print(f"[+] Glow toggled {'ON' if self.enabled else 'OFF'}")
        self._prev_alt_down = alt_down

    def run(self):
        try:
            print("CS2 Glow Manager started. Press Alt to toggle ON/OFF. Ctrl+C to stop.")
            while True:
                # kiểm tra toggle key
                self._check_toggle_keys()
                # cập nhật glow (chỉ thực hiện nếu enabled)
                self.update_glow()
                # sleep ngắn (vừa đủ để không chiếm CPU quá nhiều)
                time.sleep(0.01 + random.uniform(0, 0.005))
        except KeyboardInterrupt:
            pass
        finally:
            self.k32.CloseHandle(self.handle)

if __name__ == "__main__":
    CS2GlowManager().run()
