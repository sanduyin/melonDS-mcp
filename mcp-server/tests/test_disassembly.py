from __future__ import annotations

from melonds_mcp.disassembly import disassemble_arm


def test_thumb2_only_prefix_cannot_shift_thumb1_boundaries() -> None:
    # Capstone in generic Thumb mode recognizes this pair as a 32-bit Thumb-2
    # branch, but neither DS CPU implements Thumb-2.
    decoded = disassemble_arm(
        bytes.fromhex("00f00080"),
        address=0x02000000,
        mode="thumb",
        count=2,
        isa="armv5te",
    )
    assert [instruction["size"] for instruction in decoded] == [2, 2]
    assert decoded[0]["mnemonic"] == ".hword"
    assert decoded[0]["isa_validated"] is False


def test_thumb1_bl_pair_remains_one_32_bit_pseudoinstruction() -> None:
    decoded = disassemble_arm(
        bytes.fromhex("00f000f8"),
        address=0x02000000,
        mode="thumb",
        count=1,
        isa="armv4t",
    )
    assert len(decoded) == 1
    assert decoded[0]["size"] == 4
    assert decoded[0]["mnemonic"] == "bl"
    assert decoded[0]["isa_validated"] is True


def test_blx_immediate_is_not_valid_on_arm7_armv4t() -> None:
    encoded = bytes.fromhex("00f000e8")
    arm7 = disassemble_arm(
        encoded,
        address=0x03800000,
        mode="thumb",
        count=2,
        isa="armv4t",
    )
    arm9 = disassemble_arm(
        encoded,
        address=0x02000000,
        mode="thumb",
        count=1,
        isa="armv5te",
    )
    assert [instruction["size"] for instruction in arm7] == [2, 2]
    assert arm9[0]["size"] == 4
    assert arm9[0]["mnemonic"] == "blx"
