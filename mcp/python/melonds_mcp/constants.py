"""DS 按键位掩码、屏幕尺寸与相关常量。"""

from enum import IntEnum, IntFlag

# ── 屏幕尺寸 ──
SCREEN_WIDTH = 256
SCREEN_HEIGHT = 192
TOTAL_WIDTH = SCREEN_WIDTH
TOTAL_HEIGHT = SCREEN_HEIGHT * 2  # 384

SCREEN_PIXEL_COUNT = SCREEN_WIDTH * SCREEN_HEIGHT  # 49152
TOTAL_PIXEL_COUNT = SCREEN_PIXEL_COUNT * 2         # 98304

# 截图缓冲大小（RGB24）
SCREENSHOT_RGB_SIZE = TOTAL_PIXEL_COUNT * 3  # 294912 字节

# ── 帧率 ──
FRAMES_PER_SECOND = 60
# DS 实际帧率：ARM7 时钟 / 每帧周期数 ≈ 59.8261 Hz
DS_FRAMERATE_NUM = 33513982
DS_FRAMERATE_DEN = 560190
DS_FRAMERATE = DS_FRAMERATE_NUM / DS_FRAMERATE_DEN


class Key(IntEnum):
    """与 DS 硬件 KEYINPUT 寄存器位序一致的按键索引。"""

    A = 0
    B = 1
    SELECT = 2
    START = 3
    RIGHT = 4
    LEFT = 5
    UP = 6
    DOWN = 7
    R = 8
    L = 9
    X = 10
    Y = 11
    DEBUG = 12
    BOOST = 13
    LID = 14


class KeyMask(IntFlag):
    """input_keypad_update 使用的按键位掩码（1 = 按下）。"""

    NONE = 0
    A = 1 << Key.A        # 0x0001
    B = 1 << Key.B        # 0x0002
    SELECT = 1 << Key.SELECT  # 0x0004
    START = 1 << Key.START    # 0x0008
    RIGHT = 1 << Key.RIGHT    # 0x0010
    LEFT = 1 << Key.LEFT      # 0x0020
    UP = 1 << Key.UP          # 0x0040
    DOWN = 1 << Key.DOWN      # 0x0080
    R = 1 << Key.R            # 0x0100
    L = 1 << Key.L            # 0x0200
    X = 1 << Key.X            # 0x0400
    Y = 1 << Key.Y            # 0x0800


BUTTON_MAP: dict[str, int] = {
    "a": KeyMask.A,
    "b": KeyMask.B,
    "x": KeyMask.X,
    "y": KeyMask.Y,
    "l": KeyMask.L,
    "r": KeyMask.R,
    "start": KeyMask.START,
    "select": KeyMask.SELECT,
    "up": KeyMask.UP,
    "down": KeyMask.DOWN,
    "left": KeyMask.LEFT,
    "right": KeyMask.RIGHT,
}

VALID_BUTTONS = sorted(BUTTON_MAP.keys())


def buttons_to_bitmask(buttons: list[str]) -> int:
    """将按键名列表转换为位掩码。未知按键抛出 ValueError。"""
    mask = 0
    for btn in buttons:
        btn_lower = btn.lower().strip()
        if btn_lower not in BUTTON_MAP:
            raise ValueError(
                f"未知按键: {btn!r}。有效按键: {VALID_BUTTONS}"
            )
        mask |= BUTTON_MAP[btn_lower]
    return mask


# ── CPU 编号 ──
CPU_ARM9 = 0
CPU_ARM7 = 1
CPU_NAMES = {0: "ARM9", 1: "ARM7"}


# ── 调试常量（与 src/MCPDebug.h 对应）──
BREAK_REASON = {
    0: "none",
    1: "breakpoint",   # PC 断点
    2: "watchpoint",   # 数据观察点
    3: "step",         # 单步完成
}

WATCH_READ = 1
WATCH_WRITE = 2
WATCH_RW = 3

# 寄存器缓冲布局（melonds_debug_get_registers）
REG_R0_R15 = range(0, 16)   # 0..15
REG_CPSR = 16
REG_CYCLES = 17
REG_HALTED = 18
REG_IRQ = 19
REG_FIQ = range(22, 30)     # R8-R14 + SPSR_fiq
REG_SVC = range(30, 33)     # R13,R14 + SPSR_svc
REG_ABT = range(33, 36)
REG_IRQ_BANK = range(36, 39)
REG_UND = range(39, 42)
REG_BUFFER_SIZE = 48
