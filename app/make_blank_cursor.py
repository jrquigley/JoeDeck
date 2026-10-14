#!/usr/bin/env python3
"""Create a fully transparent Xcursor theme so the kiosk never draws a mouse
pointer, even with a mouse plugged in.  Usage: make_blank_cursor.py /usr/share/icons/signage-blank"""
import os
import struct
import sys

NAMES = """default left_ptr arrow top_left_arrow pointer hand hand1 hand2 pointing_hand text xterm ibeam
wait watch progress left_ptr_watch half-busy crosshair cross tcross move fleur grab grabbing all-scroll
not-allowed crossed_circle no-drop col-resize row-resize ew-resize ns-resize nesw-resize nwse-resize
n-resize s-resize e-resize w-resize ne-resize nw-resize se-resize sw-resize sb_h_double_arrow sb_v_double_arrow
size_hor size_ver size_bdiag size_fdiag help question_arrow context-menu cell copy alias vertical-text zoom-in zoom-out
top_side bottom_side left_side right_side top_left_corner top_right_corner bottom_left_corner bottom_right_corner""".split()


def blank_cursor(size=24):
    xcur_image = 0xFFFD0002
    header = struct.pack("<4sIII", b"Xcur", 16, 0x10000, 1)
    toc = struct.pack("<III", xcur_image, size, 16 + 12)
    chunk = struct.pack("<IIIIIIIII", 36, xcur_image, size, 1, 1, 1, 0, 0, 0)
    pixel = struct.pack("<I", 0)  # one fully transparent ARGB pixel
    return header + toc + chunk + pixel


def main(theme_dir):
    cursors = os.path.join(theme_dir, "cursors")
    os.makedirs(cursors, exist_ok=True)
    with open(os.path.join(theme_dir, "index.theme"), "w") as f:
        f.write("[Icon Theme]\nName=signage-blank\nComment=Invisible cursor for JoeDeck\n")
    data = blank_cursor()
    for name in NAMES:
        with open(os.path.join(cursors, name), "wb") as f:
            f.write(data)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/usr/share/icons/signage-blank")
