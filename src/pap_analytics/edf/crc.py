"""CRC16 helpers for ResMed EDF files.

ResMed appends a ``Crc16`` pseudo-signal to every data record: CRC-16/CCITT-FALSE
(init 0xFFFF, poly 0x1021) over all preceding bytes of the record, stored little endian.
Algorithm per ``edf_resscan.py`` of airbreak-plus (MIT, https://github.com/m-kozlowski/airbreak-plus).
"""

from __future__ import annotations

import binascii


def crc16(data: bytes) -> int:
    return binascii.crc_hqx(data, 0xFFFF)
