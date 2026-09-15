"""GDI rendering backend (Windows only): the rasterizer that GDI-era
applications — most notably Excel's worksheet grid — actually use.

Renders through real gdi32 ClearType into a memory DIB, so what you get
is bit-for-bit what those applications show: horizontal subpixel
antialiasing, *no* vertical antialiasing, gasp honoured, hints executed
by the GDI font stack. This is the world where hinting still matters.

GDI knows nothing about variable fonts beyond the default instance, so
non-default variation locations are pinned with fontTools instancer to a
temporary static font first (cached per location).
"""

from __future__ import annotations

import ctypes
import sys
import tempfile
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path

from PIL import Image

if sys.platform != "win32":
    raise ImportError("The gdi backend only runs on Windows")

gdi32 = ctypes.windll.gdi32

FR_PRIVATE = 0x10
CLEARTYPE_QUALITY = 5
OUT_TT_ONLY_PRECIS = 7
DEFAULT_CHARSET = 1
TA_LEFT = 0
TA_BASELINE = 24
ETO_OPAQUE = 0x0002
DIB_RGB_COLORS = 0
BI_RGB = 0

PADDING = 4


class LOGFONTW(ctypes.Structure):
    _fields_ = [
        ("lfHeight", wintypes.LONG),
        ("lfWidth", wintypes.LONG),
        ("lfEscapement", wintypes.LONG),
        ("lfOrientation", wintypes.LONG),
        ("lfWeight", wintypes.LONG),
        ("lfItalic", ctypes.c_byte),
        ("lfUnderline", ctypes.c_byte),
        ("lfStrikeOut", ctypes.c_byte),
        ("lfCharSet", ctypes.c_byte),
        ("lfOutPrecision", ctypes.c_byte),
        ("lfClipPrecision", ctypes.c_byte),
        ("lfQuality", ctypes.c_byte),
        ("lfPitchAndFamily", ctypes.c_byte),
        ("lfFaceName", ctypes.c_wchar * 32),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


# 64-bit handles: never let ctypes default to c_int.
gdi32.AddFontResourceExW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
gdi32.AddFontResourceExW.restype = ctypes.c_int
gdi32.RemoveFontResourceExW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_void_p,
]
gdi32.CreateFontIndirectW.argtypes = [ctypes.POINTER(LOGFONTW)]
gdi32.CreateFontIndirectW.restype = ctypes.c_void_p
gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi32.SelectObject.restype = ctypes.c_void_p
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
gdi32.GetTextExtentPoint32W.argtypes = [
    ctypes.c_void_p,
    wintypes.LPCWSTR,
    ctypes.c_int,
    ctypes.POINTER(wintypes.SIZE),
]
gdi32.CreateDIBSection.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(BITMAPINFO),
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.c_void_p,
    wintypes.DWORD,
]
gdi32.CreateDIBSection.restype = ctypes.c_void_p
gdi32.SetBkColor.argtypes = [ctypes.c_void_p, wintypes.COLORREF]
gdi32.SetTextColor.argtypes = [ctypes.c_void_p, wintypes.COLORREF]
gdi32.SetTextAlign.argtypes = [ctypes.c_void_p, wintypes.UINT]
gdi32.ExtTextOutW.argtypes = [
    ctypes.c_void_p,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
    ctypes.POINTER(wintypes.RECT),
    wintypes.LPCWSTR,
    wintypes.UINT,
    ctypes.c_void_p,
]
gdi32.GdiFlush.argtypes = []


@lru_cache(maxsize=None)
def _prepare_font(font_path: str, var_key: tuple):
    """(path_to_load, face_name, weight, italic); instances variable
    locations to a temporary static font, since GDI cannot."""
    from fontTools.ttLib import TTFont

    if var_key:
        from fontTools.varLib import instancer

        font = TTFont(font_path)
        axes = {a.axisTag: a.defaultValue for a in font["fvar"].axes}
        axes.update(dict(var_key))
        font = instancer.instantiateVariableFont(font, axes)
        tmp = tempfile.NamedTemporaryFile(suffix=".ttf", delete=False)
        tmp.close()
        font.save(tmp.name)
        load_path = tmp.name
    else:
        font = TTFont(font_path, lazy=True)
        load_path = font_path
    face = font["name"].getDebugName(1) or "Unknown"
    weight, italic = 400, False
    if "OS/2" in font:
        weight = font["OS/2"].usWeightClass
        italic = bool(font["OS/2"].fsSelection & 1)
    return load_path, face, weight, italic


def render_row(
    font_path: Path,
    text: str,
    ppem: int,
    variations: dict[str, float] | None = None,
    *,
    target_height: int,
    baseline_y: int,
) -> Image.Image:
    var_key = tuple(sorted((variations or {}).items()))
    load_path, face, weight, italic = _prepare_font(str(font_path), var_key)

    if gdi32.AddFontResourceExW(load_path, FR_PRIVATE, None) == 0:
        raise RuntimeError(f"GDI could not load font: {load_path}")
    hdc = hfont = hbmp = None
    old_font = old_bmp = None
    try:
        lf = LOGFONTW()
        lf.lfHeight = -int(ppem)  # negative height: em size in pixels
        lf.lfWeight = weight
        lf.lfItalic = 1 if italic else 0
        lf.lfCharSet = DEFAULT_CHARSET
        lf.lfOutPrecision = OUT_TT_ONLY_PRECIS
        lf.lfQuality = CLEARTYPE_QUALITY
        lf.lfFaceName = face[:31]
        hfont = gdi32.CreateFontIndirectW(ctypes.byref(lf))
        if not hfont:
            raise RuntimeError(f"CreateFontIndirectW failed for {face!r}")
        hdc = gdi32.CreateCompatibleDC(None)
        old_font = gdi32.SelectObject(hdc, hfont)

        size = wintypes.SIZE()
        gdi32.GetTextExtentPoint32W(hdc, text, len(text), ctypes.byref(size))
        width = max(size.cx + PADDING * 2, 1)

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -target_height  # top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        bits = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(
            hdc, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits), None, 0
        )
        if not hbmp:
            raise RuntimeError("CreateDIBSection failed")
        old_bmp = gdi32.SelectObject(hdc, hbmp)

        gdi32.SetBkColor(hdc, 0x00FFFFFF)
        gdi32.SetTextColor(hdc, 0x00000000)
        gdi32.SetTextAlign(hdc, TA_BASELINE | TA_LEFT)
        rect = wintypes.RECT(0, 0, width, target_height)
        gdi32.ExtTextOutW(
            hdc,
            PADDING,
            baseline_y,
            ETO_OPAQUE,
            ctypes.byref(rect),
            text,
            len(text),
            None,
        )
        gdi32.GdiFlush()

        buf = ctypes.string_at(bits, width * target_height * 4)
        return Image.frombuffer(
            "RGB", (width, target_height), buf, "raw", "BGRX", 0, 1
        ).copy()
    finally:
        if old_bmp is not None:
            gdi32.SelectObject(hdc, old_bmp)
        if old_font is not None:
            gdi32.SelectObject(hdc, old_font)
        if hbmp:
            gdi32.DeleteObject(hbmp)
        if hfont:
            gdi32.DeleteObject(hfont)
        if hdc:
            gdi32.DeleteDC(hdc)
        gdi32.RemoveFontResourceExW(load_path, FR_PRIVATE, None)
