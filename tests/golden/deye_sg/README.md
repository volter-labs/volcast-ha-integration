# Deye SG (three-phase) test registers

**Synthetic until recorded from a live unit.** `registers.json` is a hand-made register image
(address → 16-bit word) consistent with the `deye-sg` profile: device type, rated power, the
time-of-use block 146–177 and the live-data block. The serial number (registers 3–7) is a fake
placeholder. Nothing here was read from a real inverter; the file is replaced by a recording
imported with `tools/golden/import_direct_frames.py` once one exists.
