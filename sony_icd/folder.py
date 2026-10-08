"""Parse an ICD-ST folder table (137 NAND pages of 528 bytes, 512 data + 16 spare).

Pages used (offsets are page numbers):
  0   message list, one u32 per message in display order: slot (u16), 0x60,
      then the recording mode (0x00 LP on the ICD-ST25, 0x6C LPEC ST on the
      ICD-ST10: sony_icd.dvf.MODES); 0xFFFFFFFF ends the list (all 0xFF = empty
      folder)
  2   start timestamp of each slot's first block (u32 BE; 128 per page)
  5-8 flash address ranges (extents): start u32, end u32 (64 per page), in
      slot order. A recording takes one or more extents; bit 31 of the end
      marks its last one. A recording made into the gaps left by deleted
      ones is split (seen on an ICD-ST25: A-002 in 0x1D1800..0x1D3FFF,
      open, then 0x106C000..0x81074AC1; the recorder served exactly the sum
      of both lengths). The first unused entry has start 0xFFFFFFFF.
  9+  one entry page per slot: owner name at 276 after a 03 00 tag at 274,
      date/time at 452..459 (the ICD-ST10 has a 90 00 tag at 274 instead,
      which is not parsed: its recordings get no owner name)
Page 4 is NOT the message list: it keeps 00 01 02 ... even in an empty folder.
Pages after the live entries hold stale entries from deleted recordings; only
slots listed on page 0 are used.

Verified on one ICD-ST25 with slots 0..19, and one ICD-ST10 with slots 0..2
(its start timestamps and dates were all 0xFF: its clock had not been set).
Extents are matched to the listed slots in slot order; seen only with slots
0..n-1 and one split recording (the last one). More than 64 extents
continuing onto page 6 is assumed. Each message is cross-checked against its
downloaded data (start counter, block lengths, LPEC ST framing), so a wrong
assumption stops the download instead of producing a bad file. A mode byte other than the known
ones is a problem for that message only; the rest of the table is still used.
"""
import math
import struct
from dataclasses import dataclass

from . import dvf

PAGE, PAGE_DATA = 528, 512
TABLE_PAGES = 137
TABLE_SIZE = TABLE_PAGES * PAGE
FIRST_ENTRY_PAGE = 9
COUNTERS_PER_PAGE, RANGES_PER_PAGE = PAGE_DATA // 4, PAGE_DATA // 8
RANGE_PAGE, RANGE_ENTRIES = 5, (FIRST_ENTRY_PAGE - 5) * (PAGE_DATA // 8)
MAX_SLOT = TABLE_PAGES - FIRST_ENTRY_PAGE - 1
MAX_LENGTH = 32 * 1024 * 1024          # the ICD-ST25 has 32 MB of flash (the ST10's size is not known)
# The address range is used only to derive a message's length (bounded above);
# addresses are never sent to the recorder - GET_VOICE takes the message number
# and block count, and those are bounded again by sony_icd/policy.py.


class TableError(ValueError):
    pass


@dataclass
class Message:
    number: int          # 1-based position in the folder (what DVE shows as No.)
    slot: int
    start_counter: int   # first-block timestamp; must match the downloaded data
    length: int          # valid bytes including the 10-byte block headers
    blocks: int
    date: bytes          # 8 raw bytes as stored (all 0xFF if undated)
    owner: str
    problem: str = ""    # non-empty: this message cannot be downloaded safely
    mode: int = dvf.MODE_LP   # the message list's mode byte (sony_icd.dvf.MODES)

    @property
    def dated(self):
        return plausible_date(self.date)

    def when(self):
        if not self.dated:
            return "no date"
        y = struct.unpack(">H", self.date[:2])[0]
        d = self.date
        return f"{y:04d}-{d[2]:02d}-{d[3]:02d} {d[4]:02d}:{d[5]:02d}:{d[6]:02d}"

    def seconds(self):
        """The length in seconds, or None for an unknown mode."""
        return dvf.seconds(max(0, self.length - 10 * self.blocks), self.mode)


def plausible_date(d):
    if len(d) != 8 or d[:2] == b"\xff\xff":
        return False
    y = struct.unpack(">H", d[:2])[0]
    return 1990 <= y <= 2099 and 1 <= d[2] <= 12 and 1 <= d[3] <= 31 and d[4] < 24 and d[5] < 60 and d[6] < 60


def _page(table, n):
    return table[n * PAGE:n * PAGE + PAGE_DATA]


def _u32(page, i):
    return struct.unpack(">I", page[i * 4:i * 4 + 4])[0]


def parse(table):
    if len(table) != TABLE_SIZE:
        raise TableError(f"folder table is {len(table)} bytes, expected {TABLE_SIZE}")
    order = []
    index = _page(table, 0)
    for i in range(PAGE_DATA // 4):
        word = _u32(index, i)
        if word == 0xFFFFFFFF:
            break
        if word & 0xFF00 != 0x6000:
            raise TableError(f"unexpected message-list entry 0x{word:08x} at position {i}")
        order.append((word >> 16, word & 0xFF))
    if len({slot for slot, _mode in order}) != len(order):
        raise TableError("message list contains a slot twice")

    groups = _ranges(table)
    rank = {slot: i for i, slot in enumerate(sorted(slot for slot, _mode in order))}
    msgs = []
    for number, (slot, mode) in enumerate(order, 1):
        m = Message(number=number, slot=slot, start_counter=0, length=0, blocks=0,
                    date=b"\xff" * 8, owner="", mode=mode)
        msgs.append(m)
        _fill(m, table, groups[rank[slot]] if rank[slot] < len(groups) else None)
        if not m.problem and mode not in dvf.MODES:
            m.problem = f"unknown recording mode 0x{mode:02x} for slot {slot}"
    return msgs


def _ranges(table):
    """Each recording's extents, in order: [(start, end)] per recording, the
    end flag removed. An open run at the end of the entries is dropped."""
    groups, run = [], []
    for i in range(RANGE_ENTRIES):
        page = _page(table, RANGE_PAGE + i // RANGES_PER_PAGE)
        r = i % RANGES_PER_PAGE
        start, end = _u32(page, 2 * r), _u32(page, 2 * r + 1)
        if start == 0xFFFFFFFF:
            break
        run.append((start, end & 0x7FFFFFFF))
        if end & 0x80000000:
            groups.append(run)
            run = []
    return groups


def _fill(m, table, extents):
    """Read message m's start counter, length, owner and date from the table
    (extents: its address ranges, or None if it has none); what makes it
    unsafe to download goes into m.problem."""
    slot = m.slot
    if slot > MAX_SLOT:
        m.problem = f"slot {slot} is outside the table"
        return
    m.start_counter = _u32(_page(table, 2 + slot // COUNTERS_PER_PAGE), slot % COUNTERS_PER_PAGE)
    if not extents:
        m.problem = f"no address range found for slot {slot}"
        return
    if any(end < start for start, end in extents):
        m.problem = f"reversed address range for slot {slot}"
        return
    length = sum(end - start + 1 for start, end in extents)
    if length > MAX_LENGTH:
        m.problem = f"implausible length {length} bytes for slot {slot} (an ICD-ST25 holds 32 MB)"
        return
    m.length = length
    m.blocks = math.ceil(length / 1024)
    entry = _page(table, FIRST_ENTRY_PAGE + slot)
    if entry[274:276] == b"\x03\x00":
        m.owner = entry[276:306].split(b"\0")[0].split(b"\xff")[0].decode("latin-1").strip()
    m.date = bytes(entry[452:460])
