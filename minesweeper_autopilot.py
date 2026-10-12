# Minesweeper AutoPilot - Windows prototype
# Copyright (c) OpenAI. Use at your own risk; generic game layouts may need calibration.

import itertools
import math
import threading
import time
import sys
import ctypes
import os
from concurrent.futures import ThreadPoolExecutor

# Make Tkinter, PyAutoGUI and Pillow use the same physical screen coordinates on
# Windows systems with display scaling (125%, 150%, etc.). Without this, the
# selected rectangle can be offset from the screenshot and every tile becomes '?'.
def enable_dpi_awareness():
    if sys.platform != 'win32':
        return
    try:
        # PER_MONITOR_AWARE_V2
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

enable_dpi_awareness()

import tkinter as tk
from tkinter import ttk, messagebox

try:
    import numpy as np
    import cv2
    import pyautogui
    from PIL import Image, ImageGrab, ImageOps, ImageEnhance
    import pytesseract
    try:
        import mss
    except ImportError:
        mss = None
except ImportError as exc:
    raise SystemExit(
        "Thiếu thư viện. Mở PowerShell và chạy:\n"
        "py -m pip install pyautogui opencv-python pillow pytesseract numpy\n\n"
        f"Chi tiết: {exc}"
    )

# Cell states
COVERED = -2
FLAG = -1
ERR = -9

pyautogui.FAILSAFE = True  # Move the mouse to the upper-left corner to trigger PyAutoGUI's failsafe.
pyautogui.PAUSE = 0  # all pacing is controlled by the per-move millisecond setting

# Reuse MSS's screen-capture handle in each calling thread. MSS is much faster than
# Pillow's ImageGrab on Windows. If MSS is unavailable or fails, capture falls back.
_CAPTURE_TLS = threading.local()
# Use up to 12 workers for independent tile classification. Most covered/blank
# tiles are handled in a vectorized NumPy pass; only ambiguous clue tiles are sent here.
MAX_WORKERS = 12
_RECOGNITION_POOL = ThreadPoolExecutor(
    max_workers=MAX_WORKERS,
    thread_name_prefix='ms-cell-read',
)


class RegionSelector:
    def __init__(self, parent, callback):
        self.parent = parent
        self.callback = callback
        self.start = None
        self.rect = None
        self.win = tk.Toplevel(parent)
        self.win.attributes('-fullscreen', True)
        self.win.attributes('-topmost', True)
        self.win.attributes('-alpha', 0.28)
        self.win.configure(bg='black')
        self.canvas = tk.Canvas(self.win, cursor='cross', bg='black', highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)
        self.canvas.create_text(
            24, 24, anchor='nw', fill='white', font=('Segoe UI', 16, 'bold'),
            text='Kéo chuột bao trọn vùng bàn dò mìn  |  ESC để hủy'
        )
        self.canvas.bind('<ButtonPress-1>', self.on_down)
        self.canvas.bind('<B1-Motion>', self.on_move)
        self.canvas.bind('<ButtonRelease-1>', self.on_up)
        self.win.bind('<Escape>', lambda _e: self.cancel())
        self.win.focus_force()

    def on_down(self, event):
        self.start = (event.x_root, event.y_root)
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(event.x, event.y, event.x, event.y,
                                                 outline='#00ff66', width=3)

    def on_move(self, event):
        if not self.start or not self.rect:
            return
        x0, y0 = self.start
        self.canvas.coords(self.rect, x0, y0, event.x_root, event.y_root)

    def on_up(self, event):
        if not self.start:
            return
        x0, y0 = self.start
        x1, y1 = event.x_root, event.y_root
        box = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        self.win.destroy()
        if box[2] - box[0] < 50 or box[3] - box[1] < 50:
            self.callback(None)
        else:
            self.callback(box)

    def cancel(self):
        self.win.destroy()
        self.callback(None)


def best_period(profile, min_period=14, max_period=90):
    """Estimate repeated tile pitch from border/edge strength projections."""
    profile = np.asarray(profile, dtype=np.float64)
    if len(profile) < min_period * 3:
        return None
    profile = profile - np.mean(profile)
    scores = []
    for p in range(min_period, min(max_period, len(profile) // 2) + 1):
        # Autocorrelation is strong at the tile pitch and its multiples.
        den = np.sqrt(np.dot(profile[:-p], profile[:-p]) * np.dot(profile[p:], profile[p:])) + 1e-9
        corr = float(np.dot(profile[:-p], profile[p:]) / den)
        scores.append((corr - 0.0008 * p, p))
    if not scores:
        return None
    scores.sort(reverse=True)
    top_score, top_p = scores[0]
    if top_score < 0.12:
        return None
    # Prefer the smallest near-equivalent peak to avoid choosing 2x/3x tile width.
    near = [p for score, p in scores if score >= top_score - 0.035]
    return min(near) if near else top_p


def infer_grid(image, rows_hint=0, cols_hint=0):
    """Infer a Google Minesweeper grid, preferring its square-cell geometry.

    Autocorrelation can occasionally return a 2x horizontal period on a checkerboard
    pattern. When the resulting cells are implausibly non-square, derive the missing
    dimension from the reliable dimension and the selected rectangle's aspect ratio.
    The optional hint also lets a user lock one dimension and infer the other.
    """
    width, height = image.size

    # If the user supplies one dimension, use Google Minesweeper's square-cell
    # geometry to derive the other rather than discarding the supplied value.
    if rows_hint > 0 and cols_hint <= 0:
        rows = int(rows_hint)
        cols = int(round(width * rows / height))
        return (rows, cols) if 2 <= rows <= 80 and 2 <= cols <= 80 else None
    if cols_hint > 0 and rows_hint <= 0:
        cols = int(cols_hint)
        rows = int(round(height * cols / width))
        return (rows, cols) if 2 <= rows <= 80 and 2 <= cols <= 80 else None

    arr = np.array(image.convert('RGB'))
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY).astype(np.float32)
    gx = np.mean(np.abs(np.diff(gray, axis=1)), axis=0)
    gy = np.mean(np.abs(np.diff(gray, axis=0)), axis=1)
    px = best_period(gx)
    py = best_period(gy)
    if not px or not py:
        return None
    cols = max(1, int(round(width / px)))
    rows = max(1, int(round(height / py)))

    # For Google's board the tiles are square. If one axis was assigned a
    # multiple of the true period, its estimated tile aspect ratio exposes it.
    # Keep the better-supported row estimate when tiles would be too wide/tall.
    if rows > 0 and cols > 0:
        cell_w = width / cols
        cell_h = height / rows
        aspect = cell_w / cell_h
        if aspect > 1.30:
            cols = int(round(width / cell_h))
        elif aspect < 0.77:
            rows = int(round(height / cell_w))

    if cols < 2 or rows < 2 or cols > 80 or rows > 80:
        return None
    return rows, cols


def color_red_fraction(rgb):
    a = np.asarray(rgb.convert('RGB')).astype(np.int16)
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    red = (r > 135) & (r > g * 1.35) & (r > b * 1.20) & (g < 125)
    return float(np.mean(red))


def ocr_digit(cell):
    """Recognize a visible clue in a tile crop. Returns 1..8, or None."""
    w, h = cell.size
    # Avoid most of the tile bevel and grid line.
    crop = cell.crop((int(w * 0.18), int(h * 0.15), int(w * 0.82), int(h * 0.85)))
    crop = crop.resize((max(60, crop.width * 4), max(60, crop.height * 4)))
    gray = ImageOps.grayscale(crop)
    gray = ImageEnhance.Contrast(gray).enhance(2.5)
    gray_arr = np.asarray(gray)
    _, otsu = cv2.threshold(gray_arr, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants = [
        gray,
        gray.point(lambda p: 255 if p > 115 else 0),
        gray.point(lambda p: 255 if p > 150 else 0),
        Image.fromarray(otsu),
        Image.fromarray(255 - otsu),
    ]
    for image in variants:
        try:
            text = pytesseract.image_to_string(
                image, config='--psm 10 -c tessedit_char_whitelist=12345678'
            ).strip()
        except Exception:
            return None
        for ch in text:
            if ch in '12345678':
                return int(ch)
    return None


# RGB palette used by Google's Minesweeper canvas. Sampling the game's actual
# palette is more reliable for tiny (about 24 px) digits than Tesseract OCR.
GOOGLE_PALETTE = [
    ((170, 215, 81), COVERED),   # grass light
    ((162, 209, 73), COVERED),   # grass dark
    ((242, 54, 7), FLAG),        # flag red
    ((230, 51, 7), FLAG),       # flag red alternate
    ((229, 194, 159), 0),       # revealed blank, light tan
    ((215, 184, 153), 0),       # revealed blank, dark tan
    ((25, 118, 210), 1),
    ((56, 142, 60), 2),
    ((211, 47, 47), 3),
    ((123, 31, 162), 4),
    ((255, 143, 0), 5),
    ((0, 151, 167), 6),
    ((66, 66, 66), 7),
    ((117, 117, 117), 8),       # tolerant fallback for a gray 8
]

# Several sample locations used by working Google Minesweeper readers. Multiple
# points tolerate the digit being off-centre or antialiased by browser scaling.
GOOGLE_SAMPLE_POINTS = (
    (0.60, 0.40), (0.50, 0.50), (0.60, 0.60), (0.50, 0.58), (0.50, 0.30),
    (0.45, 0.45), (0.40, 0.60), (0.50, 0.40), (0.10, 0.90), (0.90, 0.90),
)


def _nearest_google_color(pixel, tolerance=44):
    """Return (state, distance) for a pixel close to a known Google palette color."""
    p = np.asarray(pixel, dtype=np.int32)
    best_state, best_distance = None, float('inf')
    for color, state in GOOGLE_PALETTE:
        distance = float(np.sqrt(np.sum((p - np.asarray(color, dtype=np.int32)) ** 2)))
        if distance < best_distance:
            best_state, best_distance = state, distance
    if best_distance <= tolerance:
        return best_state, best_distance
    return None, best_distance


def _google_palette_read(cell_rgb):
    """Read Google cells by their palette pixels; return None if inconclusive."""
    if isinstance(cell_rgb, Image.Image):
        arr = np.asarray(cell_rgb.convert('RGB'), dtype=np.uint8)
    else:
        arr = np.asarray(cell_rgb, dtype=np.uint8)
    h, w, _ = arr.shape
    if h < 12 or w < 12:
        return None

    # Google flags are orange-red, while the red digit 3 is a more neutral red.
    # Detect the flag's distinctive low-blue cloth before matching clue colours;
    # otherwise a flag can occasionally be mistaken for a red 3.
    y0, y1 = int(h * .12), max(int(h * .88), int(h * .12) + 1)
    x0, x1 = int(w * .12), max(int(w * .88), int(w * .12) + 1)
    inner_rgb = arr[y0:y1, x0:x1].astype(np.int32)
    rr, gg, bb = inner_rgb[:, :, 0], inner_rgb[:, :, 1], inner_rgb[:, :, 2]
    flag_red = (rr >= 175) & (gg <= 100) & (bb <= 35) & (rr >= gg * 1.6) & (rr >= bb * 2.4)
    flag_pixels = int(np.count_nonzero(flag_red))
    if flag_pixels >= max(3, int(inner_rgb.shape[0] * inner_rgb.shape[1] * 0.008)):
        return FLAG

    # FAST PATH: a covered Google tile is almost entirely grass-green. Do this
    # before testing eight digit palettes and HSV; at the hard level, most cells
    # are covered, so this removes a large amount of repeated per-cell work.
    gr, gg, gb = inner_rgb[:, :, 0], inner_rgb[:, :, 1], inner_rgb[:, :, 2]
    green_pixels = (gg >= 150) & (gg > gr * 1.10) & (gg > gb * 1.45)
    if float(np.mean(green_pixels)) >= 0.42:
        return COVERED

    # FAST PATH: an opened blank tile is nearly uniform tan. A number has several
    # dark/saturated ink pixels and a larger colour spread, so uniform tan can be
    # accepted as 0 before testing each numeral palette individually.
    dark_ink_pixels = int(np.count_nonzero(np.any(inner_rgb < 145, axis=2)))
    if dark_ink_pixels < 3 and float(np.max(np.std(inner_rgb, axis=(0, 1)))) < 9.0:
        return 0

    # First look across the central pixels for a number's distinctive ink color.
    # Keep away from tile borders where browser scaling can blend neighbor colors.
    inner = arr[y0:y1, x0:x1].astype(np.int32)
    number_hits = {}
    for color, state in GOOGLE_PALETTE:
        if state < 1:
            continue
        delta = inner - np.asarray(color, dtype=np.int32)
        dist2 = np.sum(delta * delta, axis=2)
        # Google uses solid, distinctive digit colors; allow antialiasing while
        # requiring several pixels so a single artifact cannot become a clue.
        hits = int(np.count_nonzero(dist2 <= (43 * 43)))
        if hits >= 3:
            number_hits[state] = hits
    if number_hits:
        # The correct digit normally has the largest cluster of near-palette pixels.
        return max(number_hits.items(), key=lambda item: (item[1], item[0]))[0]

    # Then inspect a handful of stable points, matching the exact palette with a
    # tolerance for fractional browser scaling and capture color variation.
    sampled = []
    for fx, fy in GOOGLE_SAMPLE_POINTS:
        xx = min(w - 1, max(0, int(fx * w)))
        yy = min(h - 1, max(0, int(fy * h)))
        state, distance = _nearest_google_color(arr[yy, xx], tolerance=44)
        if state is not None:
            sampled.append((state, distance))

    digit_samples = [state for state, _ in sampled if state > 0]
    if digit_samples:
        counts = {state: digit_samples.count(state) for state in set(digit_samples)}
        return max(counts, key=lambda state: (counts[state], state))

    # Flag has priority over covered green; a later red-pixel check catches flags
    # whose red cloth fell between the sample points.
    if any(state == FLAG for state, _ in sampled):
        return FLAG
    if any(state == COVERED for state, _ in sampled):
        # _google_palette_read receives a NumPy array, not a PIL `cell`; using
        # color_red_fraction(cell) here used to raise NameError on some opened
        # regions, which terminated TURBO in the middle of a batch.
        red_mask = ((rr > 135) & (rr > gg * 1.35) & (rr > bb * 1.20) & (gg < 125))
        if float(np.mean(red_mask)) > 0.010:
            return FLAG
        return COVERED

    # Don't immediately call a tan sample 0: OCR / color-ink checks below get a
    # chance to detect a digit first.
    return 0 if any(state == 0 for state, _ in sampled) else None


def recognize_google_cell(cell):
    """Recognize Google Minesweeper using its actual RGB palette, then HSV/OCR fallback."""
    rgb = np.asarray(cell.convert('RGB'))
    h, w, _ = rgb.shape
    if h < 12 or w < 12:
        return None

    palette_state = _google_palette_read(rgb)
    # Covered grass and red flags have high-contrast, unambiguous signatures; the
    # fast path avoids converting these tiles to HSV and invoking the OCR fallback.
    if palette_state in (COVERED, FLAG):
        return palette_state
    # A digit detected from Google-specific ink colors is more reliable than OCR.
    if isinstance(palette_state, int) and 1 <= palette_state <= 8:
        return palette_state

    # Center crop excludes most grid lines and bevel/shadow pixels.
    y0, y1 = int(h * .16), int(h * .84)
    x0, x1 = int(w * .16), int(w * .84)
    inner_rgb = rgb[y0:y1, x0:x1, :]
    inner_hsv = cv2.cvtColor(inner_rgb, cv2.COLOR_RGB2HSV)
    hue = inner_hsv[:, :, 0]       # OpenCV hue: 0..179
    sat = inner_hsv[:, :, 1]
    val = inner_hsv[:, :, 2]
    r = inner_rgb[:, :, 0].astype(np.int16)
    g = inner_rgb[:, :, 1].astype(np.int16)
    b = inner_rgb[:, :, 2].astype(np.int16)

    green_mask = (hue >= 25) & (hue <= 68) & (sat >= 35) & (val >= 65)
    green_fraction = float(np.mean(green_mask))

    # This catches red flags even when the exact palette sample missed the cloth.
    if green_fraction > 0.25:
        red_mask = (((hue <= 10) | (hue >= 170)) & (sat >= 90) & (val >= 90))
        if float(np.mean(red_mask)) > 0.010 or palette_state == FLAG:
            return FLAG
        return COVERED

    # Google has two tan shades for opened cells. Broaden the old HSV interval:
    # browser scaling and display color management can push tan pixels past H=28.
    beige_mask = (
        (hue >= 3) & (hue <= 36) & (sat >= 8) & (val >= 80) &
        (r > g * 1.01) & (g > b * 1.02)
    )
    beige_fraction = float(np.mean(beige_mask))

    if beige_fraction > 0.25 or palette_state == 0:
        # First decide whether the tile contains visible foreground pixels. OCR can
        # hallucinate a 7/1 on a flat blank tile, so never invoke it on a near-uniform
        # background. Only send tiles with a visible glyph to Tesseract.
        median_color = np.median(inner_rgb.reshape(-1, 3), axis=0)
        deviation = np.sqrt(np.sum((inner_rgb.astype(np.float32) - median_color) ** 2, axis=2))
        glyph_fraction = float(np.mean(deviation > 42))
        if glyph_fraction <= 0.010:
            return 0
        digit = ocr_digit(cell)
        if digit is not None:
            return digit
        # A visible numeral that both the palette matcher and OCR miss must stay
        # unknown; never misread it as a blank and make unsafe deductions.
        return ERR

    if palette_state == COVERED:
        return COVERED
    if palette_state == FLAG:
        return FLAG
    return None


def recognize_cell(cell):
    """Recognize Google Minesweeper first, with a conservative classic-board fallback."""
    google_state = recognize_google_cell(cell)
    if google_state is not None:
        return google_state

    # Legacy fallback for light, beveled Minesweeper boards.
    if color_red_fraction(cell) > 0.012:
        return FLAG

    digit = ocr_digit(cell)
    if digit is not None:
        return digit

    arr = np.asarray(cell.convert('RGB')).astype(np.float32)
    h, w, _ = arr.shape
    if h < 12 or w < 12:
        return ERR
    band = max(2, min(h, w) // 12)
    top = np.mean(arr[1:band + 1, band:-band, :])
    left = np.mean(arr[band:-band, 1:band + 1, :])
    bottom = np.mean(arr[-band - 1:-1, band:-band, :])
    right = np.mean(arr[band:-band, -band - 1:-1, :])
    bevel = (top + left - bottom - right) / 2.0

    inner = arr[int(h * .23):int(h * .77), int(w * .23):int(w * .77), :]
    inner_std = float(np.std(inner))
    if bevel > 13 or bevel < -22:
        return COVERED
    if abs(bevel) < 9 and inner_std < 14:
        return 0
    return ERR


class BoardStillAnimatingError(RuntimeError):
    """Raised when the selected board keeps changing and cannot be read safely."""


class BoardCaptureCancelled(RuntimeError):
    """Raised when the emergency stop interrupts a screen-capture wait."""


def _grab_region_array(region):
    """Capture a region as a NumPy view; returns (pixels, is_bgr)."""
    x1, y1, x2, y2 = map(int, region)
    width, height = x2 - x1, y2 - y1
    if mss is not None and getattr(_CAPTURE_TLS, 'use_mss', True):
        try:
            sct = getattr(_CAPTURE_TLS, 'sct', None)
            if sct is None:
                sct = mss.mss()
                _CAPTURE_TLS.sct = sct
            shot = sct.grab({'left': x1, 'top': y1, 'width': width, 'height': height})
            # MSS provides BGRA bytes. Ignore alpha during comparisons; convert only
            # the final stable frame to RGB for cell classification.
            pixels = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(height, width, 4)
            return pixels[:, :, :3], True
        except Exception:
            # Some remote-desktop / multi-monitor drivers reject MSS. Disable it for
            # this thread and transparently fall back to Pillow ImageGrab.
            _CAPTURE_TLS.use_mss = False
    image = ImageGrab.grab(bbox=(x1, y1, x2, y2)).convert('RGB')
    return np.asarray(image, dtype=np.uint8), False


def _small_frame_for_compare(pixels):
    """Downscale a raw RGB/BGR frame for quick animation detection."""
    height, width = pixels.shape[:2]
    max_dim = 280
    scale = min(1.0, max_dim / max(width, height))
    if scale < 1.0:
        new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
        pixels = cv2.resize(pixels, new_size, interpolation=cv2.INTER_AREA)
    return np.asarray(pixels, dtype=np.int16)


def capture_stable_region(region, stop_event=None, after_action=False,
                          expect_animation=False, timeout=2.5):
    """Wait for the board to settle using fast captures and adaptive polling.

    At 0 ms the bot adds no fixed per-move delay. The only wait here is for browser
    repaint / tile-flip animation to finish. MSS is used when available; Pillow is
    retained as a compatible fallback.
    """
    if stop_event is not None and stop_event.is_set():
        raise BoardCaptureCancelled('Dừng khi đang chụp màn hình.')

    # Poll quickly instead of sleeping 45 ms between frames. ImageGrab is slower,
    # so the fallback interval is a little more forgiving.
    sample_interval = 0.008 if mss is not None else 0.012
    if expect_animation:
        # Google reveals a connected blank area in a short cascade. A 30 ms quiet
        # window was too short: the board could pause briefly between flip waves.
        # Polling remains fast; we only wait until changes have actually stopped.
        min_settle_seconds = 0.045
        quiet_period = 0.075
        stable_pairs_required = 3
    elif after_action:
        min_settle_seconds = 0.025
        quiet_period = 0.035
        stable_pairs_required = 2
    else:
        min_settle_seconds = 0.0
        quiet_period = 0.008
        stable_pairs_required = 1

    previous, previous_bgr = _grab_region_array(region)
    previous_small = _small_frame_for_compare(previous)
    started = time.monotonic()
    last_change = started
    stable_pairs = 0
    deadline = started + timeout
    last = previous
    last_bgr = previous_bgr

    while time.monotonic() < deadline:
        if stop_event is not None:
            if stop_event.wait(sample_interval):
                raise BoardCaptureCancelled('Đã dừng khi đang chờ hoạt ảnh kết thúc.')
        else:
            time.sleep(sample_interval)

        current, current_bgr = _grab_region_array(region)
        current_small = _small_frame_for_compare(current)
        delta = np.abs(current_small - previous_small)
        mean_delta = float(np.mean(delta))
        changed_fraction = float(np.mean(np.max(delta, axis=2) > 18))
        frame_is_stable = mean_delta <= 0.30 and changed_fraction <= 0.0008
        now = time.monotonic()
        if frame_is_stable:
            stable_pairs += 1
        else:
            stable_pairs = 0
            last_change = now

        last = current
        last_bgr = current_bgr
        if (now - started >= min_settle_seconds and
                stable_pairs >= stable_pairs_required and
                now - last_change >= quiet_period):
            if last_bgr:
                last_rgb = cv2.cvtColor(last, cv2.COLOR_BGR2RGB)
            else:
                last_rgb = last
            return Image.fromarray(np.ascontiguousarray(last_rgb)), True

        previous = current
        previous_small = current_small
        previous_bgr = current_bgr

    if last_bgr:
        last_rgb = cv2.cvtColor(last, cv2.COLOR_BGR2RGB)
    else:
        last_rgb = last
    return Image.fromarray(np.ascontiguousarray(last_rgb)), False

def _classify_board_image(image, rows, cols):
    if rows <= 0 or cols <= 0:
        guess = infer_grid(image, rows, cols)
        if not guess:
            raise ValueError('Không tự nhận ra kích thước lưới. Nhập hàng/cột ở phần chỉnh tay.')
        rows, cols = guess
    if rows < 2 or cols < 2 or rows > 80 or cols > 80:
        raise ValueError('Số hàng/cột không hợp lệ (2–80).')
    cw, ch = image.width / cols, image.height / rows
    if max(cw, ch) / min(cw, ch) > 1.35:
        raise ValueError(
            f'Kích thước ô không cân đối ({cw:.1f}×{ch:.1f} px). '
            'Vùng chọn hoặc hàng/cột có thể sai; với Google Minesweeper hãy kiểm tra lại lưới.'
        )
    # Fast, vectorized first pass: Google tiles are highly distinctive green/tan
    # blocks. Identify covered, flagged and truly uniform blank cells in one NumPy
    # pass, and run the slower per-cell color/OCR pipeline only on ambiguous cells
    # (usually cells showing numbers).
    rgb = np.asarray(image.convert('RGB'), dtype=np.uint8)
    patch_w = max(5, int(cw * 0.68))
    patch_h = max(5, int(ch * 0.68))
    half_w, half_h = patch_w // 2, patch_h // 2
    coords = []
    patches = []
    crops = {}
    for r in range(rows):
        for c in range(cols):
            xa = int(c * cw + cw * .04)
            xb = int((c + 1) * cw - cw * .04)
            ya = int(r * ch + ch * .04)
            yb = int((r + 1) * ch - ch * .04)
            cx, cy = (xa + xb) // 2, (ya + yb) // 2
            px0 = max(xa, cx - half_w)
            py0 = max(ya, cy - half_h)
            px1 = min(xb, px0 + patch_w)
            py1 = min(yb, py0 + patch_h)
            patch = rgb[py0:py1, px0:px1]
            # Fractional board cell sizes may produce a one-pixel edge difference;
            # resize only such patches so the batch remains a single dense array.
            if patch.shape[:2] != (patch_h, patch_w):
                patch = cv2.resize(patch, (patch_w, patch_h), interpolation=cv2.INTER_AREA)
            coords.append((r, c, xa, ya, xb, yb))
            patches.append(patch)

    patch_array = np.stack(patches, axis=0)
    patch_float = patch_array.astype(np.float32)
    rr = patch_array[:, :, :, 0]
    gg = patch_array[:, :, :, 1]
    bb = patch_array[:, :, :, 2]
    red_flag = ((rr >= 175) & (gg <= 100) & (bb <= 35) &
                (rr >= gg * 1.6) & (rr >= bb * 2.4))
    red_counts = np.count_nonzero(red_flag, axis=(1, 2))
    flag_threshold = max(3, int(patch_h * patch_w * 0.008))
    is_flag = red_counts >= flag_threshold

    green_pixels = (gg >= 150) & (gg > rr * 1.10) & (gg > bb * 1.45)
    green_fraction = np.mean(green_pixels, axis=(1, 2))
    is_covered = (green_fraction >= 0.42) & ~is_flag

    means = np.mean(patch_float, axis=(1, 2))
    channel_std = np.std(patch_float, axis=(1, 2))
    uniform = np.max(channel_std, axis=1) < 9.0
    tan = ((means[:, 0] >= 195) & (means[:, 0] <= 250) &
           (means[:, 1] >= 155) & (means[:, 1] <= 225) &
           (means[:, 2] >= 120) & (means[:, 2] <= 190) &
           (means[:, 0] > means[:, 1] * 1.01) &
           (means[:, 1] > means[:, 2] * 1.02))
    is_blank = uniform & tan & ~is_flag & ~is_covered

    board = [[None for _ in range(cols)] for _ in range(rows)]
    ambiguous = []
    for i, (r, c, xa, ya, xb, yb) in enumerate(coords):
        if is_flag[i]:
            board[r][c] = FLAG
        elif is_covered[i]:
            board[r][c] = COVERED
        elif is_blank[i]:
            board[r][c] = 0
        else:
            ambiguous.append((r, c, image.crop((xa, ya, xb, yb))))

    if ambiguous:
        # Submit only clue/ambiguous cells to the persistent worker pool. This avoids
        # spending hundreds of per-cell operations on a mostly covered hard-level board.
        recognized = _RECOGNITION_POOL.map(recognize_cell, (item[2] for item in ambiguous))
        for (r, c, _tile), state in zip(ambiguous, recognized):
            board[r][c] = state
    return board, (rows, cols)


def capture_board(region, rows, cols, stop_event=None, after_action=False,
                  expect_animation=False, timeout=2.5):
    image, stable = capture_stable_region(
        region, stop_event=stop_event, after_action=after_action,
        expect_animation=expect_animation, timeout=timeout
    )
    if not stable:
        raise BoardStillAnimatingError(
            'Bàn cờ vẫn đang chuyển động hết thời hạn chờ lần này.'
        )
    return _classify_board_image(image, rows, cols)


def neighbors(r, c, rows, cols):
    for rr in range(max(0, r - 1), min(rows, r + 2)):
        for cc in range(max(0, c - 1), min(cols, c + 2)):
            if (rr, cc) != (r, c):
                yield rr, cc


def build_constraints(board, mines_known, safe_known):
    rows, cols = len(board), len(board[0])
    constraints = []
    for r in range(rows):
        for c in range(cols):
            clue = board[r][c]
            if not isinstance(clue, int) or clue < 0 or clue > 8:
                continue
            vars_ = set()
            flag_count = 0
            for rr, cc in neighbors(r, c, rows, cols):
                if board[rr][cc] == FLAG or (rr, cc) in mines_known:
                    flag_count += 1
                elif board[rr][cc] == COVERED and (rr, cc) not in safe_known:
                    vars_.add((rr, cc))
            need = clue - flag_count
            if need < 0 or need > len(vars_):
                raise ValueError(f'Mâu thuẫn tại ô ({r+1},{c+1}); ảnh đọc có thể sai.')
            if vars_:
                constraints.append((frozenset(vars_), need))
            elif need != 0:
                raise ValueError(f'Mâu thuẫn tại ô ({r+1},{c+1}); ảnh đọc có thể sai.')
    # Normalize and deduplicate
    seen, out = set(), []
    for vs, need in constraints:
        key = (tuple(sorted(vs)), need)
        if key not in seen:
            seen.add(key)
            out.append((set(vs), need))
    return out


def logical_deductions(board, total_mines, compute_risk=True):
    """Infer certain moves quickly; use integer bitmasks for subset constraints.

    The clue constraints are still built from cell coordinates (easy to inspect), but
    subset tests use integer operations instead of repeatedly allocating set differences.
    Exact probabilities use constraint-pruned DFS rather than testing every possible
    assignment against every clue.
    """
    rows, cols = len(board), len(board[0])
    mines_known, safe_known = set(), set()
    all_covered = {(r, c) for r in range(rows) for c in range(cols) if board[r][c] == COVERED}
    flags = {(r, c) for r in range(rows) for c in range(cols) if board[r][c] == FLAG}
    # Stable indexes let subset deductions operate on Python integer bitsets.
    cell_order = sorted(all_covered)
    cell_index = {cell: i for i, cell in enumerate(cell_order)}

    changed = True
    while changed:
        changed = False
        constraints = build_constraints(board, mines_known, safe_known)

        unknown = all_covered - mines_known - safe_known
        mines_left = total_mines - len(flags) - len(mines_known)
        if mines_left < 0 or mines_left > len(unknown):
            raise ValueError('Số mìn nhập vào không khớp với những lá cờ đang thấy trên bàn.')
        if unknown and mines_left == 0:
            if not unknown.issubset(safe_known):
                safe_known.update(unknown)
                changed = True
        elif unknown and mines_left == len(unknown):
            if not unknown.issubset(mines_known):
                mines_known.update(unknown)
                changed = True
        if changed:
            continue

        add_mines, add_safe = set(), set()
        # Direct deductions remain easy to read and are extremely cheap.
        for vs, need in constraints:
            if need == 0:
                add_safe.update(vs)
            elif need == len(vs):
                add_mines.update(vs)

        # Subset rule using bit masks: A ⊂ B implies B\A has need(B)-need(A) mines.
        # Sorting by constraint size means each pair is checked in only one direction.
        masked = []
        for vs, need in constraints:
            mask = 0
            for cell in vs:
                mask |= 1 << cell_index[cell]
            masked.append((mask, need, len(vs)))
        masked.sort(key=lambda item: item[2])
        for i, (amask, na, alen) in enumerate(masked):
            for j in range(i + 1, len(masked)):
                bmask, nb, blen = masked[j]
                if blen == alen:
                    continue
                if (amask & bmask) != amask:
                    continue
                diff_mask = bmask ^ amask
                diff_size = blen - alen
                need = nb - na
                if not diff_mask:
                    continue
                if need < 0 or need > diff_size:
                    raise ValueError('Mâu thuẫn khi suy luận; tạm dừng an toàn.')
                if need == 0 or need == diff_size:
                    target = add_safe if need == 0 else add_mines
                    remaining_mask = diff_mask
                    while remaining_mask:
                        bit = remaining_mask & -remaining_mask
                        target.add(cell_order[bit.bit_length() - 1])
                        remaining_mask ^= bit

        add_mines -= mines_known
        add_mines -= flags
        add_safe -= safe_known
        if add_mines & add_safe:
            raise ValueError('Suy luận ra cùng một ô vừa an toàn vừa là mìn; nhận diện ảnh có thể sai.')
        if add_mines or add_safe:
            mines_known.update(add_mines)
            safe_known.update(add_safe)
            changed = True

    if not compute_risk:
        return mines_known, safe_known, {}

    candidates = all_covered - mines_known - safe_known
    constraints = build_constraints(board, mines_known, safe_known)
    risk = {}
    if candidates:
        adjacency = {v: set() for v in candidates}
        relevant = []
        for vs, need in constraints:
            vs = set(vs) & candidates
            if not vs:
                continue
            relevant.append((vs, need))
            for v in vs:
                adjacency[v].update(vs - {v})

        unseen = set(adjacency)
        components = []
        while unseen:
            start_cell = next(iter(unseen))
            stack, comp = [start_cell], set()
            while stack:
                v = stack.pop()
                if v in comp:
                    continue
                comp.add(v)
                unseen.discard(v)
                stack.extend(adjacency[v] - comp)
            components.append(comp)

        for comp in components:
            comp_constraints = [(vs & comp, need) for vs, need in relevant if (vs & comp)]
            ordered = sorted(comp)
            if len(ordered) <= 18:
                local_index = {v: i for i, v in enumerate(ordered)}
                masks = []
                for vs, need in comp_constraints:
                    mask = 0
                    for v in vs:
                        mask |= 1 << local_index[v]
                    masks.append((mask, need, mask.bit_count()))

                # Choose high-degree variables first so contradictions prune branches early.
                var_constraints = [[] for _ in ordered]
                for ci, (mask, _need, _size) in enumerate(masks):
                    pending_mask = mask
                    while pending_mask:
                        bit = pending_mask & -pending_mask
                        var_constraints[bit.bit_length() - 1].append(ci)
                        pending_mask ^= bit
                variable_order = sorted(
                    range(len(ordered)),
                    key=lambda vi: (-len(var_constraints[vi]), vi)
                )
                assigned_mines = [0] * len(masks)
                remaining_vars = [size for _mask, _need, size in masks]
                assignment = [0] * len(ordered)
                mined_counts = [0] * len(ordered)
                valid = 0

                def enumerate_assignments(depth):
                    nonlocal valid
                    if depth == len(variable_order):
                        valid += 1
                        for index, value in enumerate(assignment):
                            if value:
                                mined_counts[index] += 1
                        return

                    vi = variable_order[depth]
                    touched = var_constraints[vi]
                    for value in (0, 1):
                        feasible = True
                        # Apply this variable to only the constraints that contain it.
                        for ci in touched:
                            remaining_vars[ci] -= 1
                            assigned_mines[ci] += value
                            need = masks[ci][1]
                            if assigned_mines[ci] > need or assigned_mines[ci] + remaining_vars[ci] < need:
                                feasible = False
                        if feasible:
                            assignment[vi] = value
                            enumerate_assignments(depth + 1)
                        for ci in touched:
                            assigned_mines[ci] -= value
                            remaining_vars[ci] += 1
                    assignment[vi] = 0

                enumerate_assignments(0)
                if valid:
                    for i, v in enumerate(ordered):
                        risk[v] = mined_counts[i] / valid
                else:
                    # Keep contradictions conservative; the caller can log/re-scan.
                    for v in ordered:
                        risk[v] = 0.5
            else:
                # Large/frontier component fallback avoids explosive search.
                for v in ordered:
                    ratios = [need / max(1, len(vs)) for vs, need in comp_constraints if v in vs]
                    risk[v] = min(0.99, max(ratios) if ratios else 0.5)

        constrained = set().union(*(vs for vs, _ in relevant)) if relevant else set()
        unconstrained = candidates - constrained
        mines_left = max(0, total_mines - len(flags) - len(mines_known))
        base = mines_left / max(1, len(candidates))
        for v in unconstrained:
            risk[v] = min(1.0, base)
        for v in candidates:
            risk.setdefault(v, base)

    return mines_known, safe_known, risk


def board_text(board):
    symbols = {COVERED: '■', FLAG: 'F', ERR: '?'}
    return '\n'.join(' '.join(symbols.get(v, str(v)) for v in row) for row in board)


class AutoPilotApp:
    def __init__(self, root):
        self.root = root
        self.root.title('Google Minesweeper AutoPilot — thử nghiệm')
        self.root.geometry('760x700')
        self.root.minsize(680, 600)
        self.region = None
        self.worker = None
        self.stop_event = threading.Event()
        self.continue_event = threading.Event()
        self.continue_event.set()
        self.running = False
        self.closing = False
        self.pending_actions = []
        self.allow_guess = True
        self.difficulty_presets = {
            'Dễ — 10×8, 10 mìn': (10, 8, 10),
            'Trung bình — 18×14, 40 mìn': (40, 14, 18),
            'Khó — 24×20, 99 mìn': (99, 20, 24),
            'Tùy chỉnh': (10, 0, 0),
        }

        outer = ttk.Frame(root, padding=12)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='Google Minesweeper AutoPilot', font=('Segoe UI', 17, 'bold')).pack(anchor='w')
        ttk.Label(outer, text='Chọn mức độ trước, sau đó khoanh vùng lưới. Bot chạy liên tục; nhấn F6 để dừng khẩn cấp.',
                  wraplength=700).pack(anchor='w', pady=(2, 10))

        diffrow = ttk.Frame(outer); diffrow.pack(fill='x', pady=3)
        ttk.Label(diffrow, text='Mức độ:').pack(side='left')
        self.difficulty_var = tk.StringVar(value='Dễ — 10×8, 10 mìn')
        self.difficulty_combo = ttk.Combobox(
            diffrow, textvariable=self.difficulty_var,
            values=list(self.difficulty_presets), state='readonly', width=30
        )
        self.difficulty_combo.pack(side='left', padx=(6, 10))
        self.difficulty_combo.bind('<<ComboboxSelected>>', self.on_difficulty_change)
        ttk.Label(diffrow, text='Chọn mức độ sẽ tự điền mìn, hàng, cột.').pack(side='left')

        speedrow = ttk.Frame(outer); speedrow.pack(fill='x', pady=3)
        ttk.Label(speedrow, text='Tốc độ mỗi nước (ms):').pack(side='left')
        self.speed_var = tk.StringVar(value='150')
        ttk.Entry(speedrow, textvariable=self.speed_var, width=8).pack(side='left', padx=(6, 10))
        ttk.Label(speedrow, text='0 = TURBO: gom các nước chắc chắn; số lớn hơn = chờ sau từng nước.').pack(side='left')

        row = ttk.Frame(outer); row.pack(fill='x', pady=3)
        self.region_label = ttk.Label(row, text='Chưa chọn vùng bàn cờ', width=42)
        self.region_label.pack(side='left', fill='x', expand=True)
        ttk.Button(row, text='Kéo chọn vùng', command=self.select_region).pack(side='right')

        row = ttk.Frame(outer); row.pack(fill='x', pady=3)
        ttk.Label(row, text='Tổng số mìn/cờ:').pack(side='left')
        self.mines_var = tk.StringVar(value='10')
        ttk.Entry(row, textvariable=self.mines_var, width=8).pack(side='left', padx=(6, 18))
        ttk.Label(row, text='Hàng (0 = tự đoán):').pack(side='left')
        self.rows_var = tk.StringVar(value='8')
        ttk.Entry(row, textvariable=self.rows_var, width=6).pack(side='left', padx=5)
        ttk.Label(row, text='Cột (0 = tự đoán):').pack(side='left')
        self.cols_var = tk.StringVar(value='10')
        ttk.Entry(row, textvariable=self.cols_var, width=6).pack(side='left', padx=5)

        self.guess_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(outer, text='Cho phép đoán khi không còn nước chắc chắn (có thể thua)',
                        variable=self.guess_var).pack(anchor='w', pady=(6, 3))

        buttons = ttk.Frame(outer); buttons.pack(fill='x', pady=(5, 8))
        self.scan_btn = ttk.Button(buttons, text='Quét thử', command=self.scan_test)
        self.scan_btn.pack(side='left', padx=(0, 6))
        self.start_btn = ttk.Button(buttons, text='Bắt đầu tự chơi', command=self.start)
        self.start_btn.pack(side='left', padx=6)
        self.go_btn = ttk.Button(buttons, text='ĐI TIẾP', command=self.continue_play)
        self.go_btn.pack(side='left', padx=6)
        self.stop_btn = ttk.Button(buttons, text='DỪNG', command=self.stop)
        self.stop_btn.pack(side='left', padx=6)

        ttk.Label(outer, text='Nhận diện bàn cờ / nhật ký:', font=('Segoe UI', 10, 'bold')).pack(anchor='w')
        self.logbox = tk.Text(outer, height=23, wrap='none', font=('Consolas', 9))
        self.logbox.pack(fill='both', expand=True, pady=(4, 0))
        self.logbox.insert('end',
            '1) Chọn mức độ trước: Dễ 10×8/10 mìn, Trung bình 18×14/40 mìn, Khó 24×20/99 mìn.\n'
            '2) Chọn vùng sát mép ngoài lưới, không lấy thanh menu.\n'
            '3) Quét thử: ■ chưa mở, F cờ, 0 trống, 1–8 số, ? chưa đọc được.\n'
            '4) Chỉ bắt đầu khi bản quét khớp bàn cờ. Khi bấm Bắt đầu, tool tự thu nhỏ rồi chạy liên tục.\n'
            'F6 = DỪNG KHẨN CẤP toàn cục khi đang chơi; DỪNG cũng ngắt bot.\n'
            'ĐI TIẾP chỉ dùng khi bot đang chờ vì hết nước chắc chắn và đã tắt chế độ đoán.\n'
            'Tốc độ mỗi nước tính bằng ms: 0 = TURBO, gom nhiều nước chắc chắn rồi quét lại một lần; không thêm độ trễ cố định.\n'
            'Sau click, tool tự đo độ trễ ảnh; khi xác minh lỗi, TURBO dùng chờ thích nghi thay vì lặp 0 ms liên tục.\n'
            'Ô vẫn rõ là chưa mở có thể được click lại tối đa 5 lần/ô; ô ? chỉ quét lại, không click bừa.\n'
            'Nếu một ô vẫn mơ hồ sau 5 lần đọc, cách ly ô đó và tiếp tục suy luận phần còn lại; không bấm ô mơ hồ.\n'
            'Nhận diện màu RGB/HSV, OCR dự phòng; tọa độ Windows DPI được đồng bộ.\n'
            'LƯU Ý: vẫn phải Quét thử và kiểm tra kỹ trước khi bật tự chơi.\n\n'
        )
        self.root.bind_all('<F6>', self._f6_fallback)
        self.root.protocol('WM_DELETE_WINDOW', self.close_app)
        threading.Thread(target=self._global_f6_watcher, daemon=True).start()

    def on_difficulty_change(self, _event=None):
        preset = self.difficulty_presets.get(self.difficulty_var.get())
        if not preset:
            return
        mines, rows, cols = preset
        self.mines_var.set(str(mines))
        self.rows_var.set(str(rows))
        self.cols_var.set(str(cols))
        self.log(f'Đã chọn mức {self.difficulty_var.get()}: {rows} hàng × {cols} cột, {mines} mìn.')

    def _f6_fallback(self, _event=None):
        # GetAsyncKeyState is global on Windows; this binding is for non-Windows
        # systems where the application window itself must have keyboard focus.
        if sys.platform != 'win32' and self.running:
            self.emergency_stop()

    def _global_f6_watcher(self):
        # On Windows, GetAsyncKeyState works even when Chrome is foreground.
        was_down = False
        while not self.closing:
            is_down = False
            if sys.platform == 'win32':
                try:
                    is_down = bool(ctypes.windll.user32.GetAsyncKeyState(0x75) & 0x8000)  # VK_F6
                except Exception:
                    pass
            if is_down and not was_down and self.running:
                self.stop_event.set()
                self.continue_event.set()
                self.log('F6: ĐÃ KÍCH HOẠT DỪNG KHẨN CẤP.')
            was_down = is_down
            time.sleep(0.025)

    def emergency_stop(self):
        if not self.running:
            return
        self.stop_event.set()
        self.continue_event.set()
        self.log('ĐÃ YÊU CẦU DỪNG KHẨN CẤP.')

    def close_app(self):
        self.closing = True
        self.stop_event.set()
        self.continue_event.set()
        self.root.destroy()

    def log(self, text):
        if self.closing:
            return
        def append():
            if self.closing:
                return
            self.logbox.insert('end', str(text) + '\n')
            self.logbox.see('end')
        try:
            self.root.after(0, append)
        except Exception:
            pass

    def select_region(self):
        self.root.withdraw()
        self.root.after(250, lambda: RegionSelector(self.root, self.region_selected))

    def region_selected(self, box):
        self.root.deiconify()
        if box:
            self.region = box
            self.region_label.config(text=f'Vùng: ({box[0]}, {box[1]}) → ({box[2]}, {box[3]})')
            self.log(f'Đã chọn vùng {box[2]-box[0]}×{box[3]-box[1]} px.')
        else:
            self.log('Đã hủy chọn vùng.')

    def get_move_delay_ms(self):
        try:
            delay_ms = int(self.speed_var.get().strip())
        except Exception:
            raise ValueError('Tốc độ mỗi nước phải là số nguyên mili giây (ms).')
        if delay_ms < 0:
            raise ValueError('Tốc độ không được âm; nhập 0 để chạy nhanh nhất có thể.')
        if delay_ms > 60000:
            raise ValueError('Tốc độ tối đa là 60000 ms (60 giây) mỗi nước.')
        return delay_ms

    def settings(self):
        if not self.region:
            raise ValueError('Chưa chọn vùng bàn cờ.')
        try:
            mines = int(self.mines_var.get())
            rows = int(self.rows_var.get())
            cols = int(self.cols_var.get())
        except Exception:
            raise ValueError('Số mìn/hàng/cột phải là số nguyên.')
        if mines < 0:
            raise ValueError('Số mìn không được âm.')
        return mines, rows, cols

    def read_current(self):
        mines, rows, cols = self.settings()
        board, shape = capture_board(self.region, rows, cols)
        return mines, board, shape

    def scan_test(self):
        try:
            mines, board, shape = self.read_current()
            x1, y1, x2, y2 = self.region
            cell_w = (x2 - x1) / shape[1]
            cell_h = (y2 - y1) / shape[0]
            self.log(f'Quét được lưới {shape[0]} hàng × {shape[1]} cột; tổng mìn nhập: {mines}.')
            self.log(f'Kích thước ô ước tính: {cell_w:.1f}×{cell_h:.1f} px.')
            self.log(board_text(board))
            flat = [v for row in board for v in row]
            bad = sum(v == ERR for v in flat)
            covered_n = sum(v == COVERED for v in flat)
            flags_n = sum(v == FLAG for v in flat)
            opened_n = sum(isinstance(v, int) and 0 <= v <= 8 for v in flat)
            self.log(f'Tổng nhận diện: ô chưa mở={covered_n}, cờ={flags_n}, ô đã mở/số={opened_n}, chưa rõ={bad}.')
            if bad:
                self.log(f'CẢNH BÁO: {bad} ô chưa nhận diện được. Chưa nên tự chơi.')
        except Exception as exc:
            messagebox.showerror('Quét thử thất bại', str(exc))

    def start(self):
        if self.running:
            return
        try:
            base_settings = self.settings()
            move_delay_ms = self.get_move_delay_ms()
            if not self.region:
                raise ValueError('Chưa chọn vùng bàn cờ.')
        except Exception as exc:
            messagebox.showerror('Thiếu thiết lập', str(exc)); return
        self.stop_event.clear()
        self.continue_event.set()
        self.pending_actions = []
        self.running = True
        self.pending_settings = (*base_settings, move_delay_ms)
        self.allow_guess = self.guess_var.get()
        speed_desc = '0 ms TURBO (gom nước chắc chắn; không thêm độ trễ cố định)' if move_delay_ms == 0 else f'{move_delay_ms} ms/nước'
        self.log(f'Bắt đầu trong khoảng 1 giây. Tốc độ: {speed_desc}. Nhận diện ô song song tối đa {MAX_WORKERS} luồng; chuột TURBO di chuyển nhanh có làm mượt. Tool sẽ tự thu nhỏ để Chrome nhận click; F6 dừng khẩn cấp.')
        # Do not leave the tool window covering the selected game rectangle. Once
        # minimized, the browser behind it becomes visible/foreground before clicks.
        self.root.after(180, self._minimize_and_launch)

    def _minimize_and_launch(self):
        if self.closing or self.stop_event.is_set():
            self.running = False
            self._restore_after_run()
            return
        try:
            self.root.iconify()
        except Exception:
            pass
        # Give Windows a moment to activate Chrome after minimizing the tool.
        self.root.after(250, self._launch_worker)

    def _launch_worker(self):
        if self.closing:
            return
        if self.stop_event.is_set():
            self.running = False
            self._restore_after_run()
            return
        self.worker = threading.Thread(target=self.run_agent, args=(self.pending_settings,), daemon=True)
        self.worker.start()

    def _restore_after_run(self):
        if self.closing:
            return
        try:
            self.root.deiconify()
            self.root.lift()
        except Exception:
            pass

    def continue_play(self):
        self.continue_event.set()
        self.log('Đã nhận lệnh ĐI TIẾP.')

    def stop(self):
        self.stop_event.set()
        self.continue_event.set()
        self.log('Đã yêu cầu dừng. (F6 là phím dừng khẩn cấp; góc trên-trái cũng kích hoạt failsafe.)')

    def wait_for_go(self, reason):
        self.log(reason + ' — đang chờ nút ĐI TIẾP.')
        self.continue_event.clear()
        while not self.continue_event.wait(0.15):
            if self.stop_event.is_set():
                return False
        return not self.stop_event.is_set()

    def run_agent(self, run_settings):
        # Every action is confirmed with adaptive timing. A clearly covered target can
        # be clicked at most five times total; an ambiguous target is re-read up to
        # five times without blindly toggling a flag or clicking an unknown tile.
        MAX_ATTEMPTS = 5
        animation_attempts = 0
        ambiguous_confirmation_reads = {}
        # Cells that stayed unreadable or did not react after five checks are isolated
        # from clicking, but they no longer block deductions on the rest of the board.
        # A later clear frame automatically releases an unreadable cell from quarantine.
        quarantined_cells = {}       # (r, c) -> 'read' | 'click'
        bad_read_attempts_by_cell = {}
        response_time_ema = 0.085   # seconds; learned from actual capture/settle times
        capture_timeout_override = None
        last_capture_ms = 0.0
        last_quarantine_idle_log = 0.0

        def adaptive_wait_seconds(retry=False, attempt=1):
            # Normal configured speeds retain their requested delay. At TURBO the first
            # polling cycle starts immediately; retries get a short adaptive backoff
            # derived from the actual capture/settle latency, rather than always 0 ms.
            requested = move_delay_ms / 1000.0
            if requested > 0:
                return requested
            if not retry:
                return 0.0
            observed = max(response_time_ema, last_capture_ms / 1000.0)
            # Capture itself waits for the animation to settle, so use only a fraction
            # of measured latency here; this avoids both tight retry loops and double-waiting.
            base = max(0.025, min(0.240, observed * 0.22))
            multiplier = 1.0 + 0.30 * max(0, int(attempt) - 1)
            return min(0.500, base * multiplier)

        try:
            mines, rows, cols, move_delay_ms = run_settings
            turbo = move_delay_ms == 0
            while not self.stop_event.is_set():
                pending = list(self.pending_actions)

                # Wait until the estimated next verification time before taking the
                # next screenshot. This avoids repeatedly OCR-reading the same frame.
                if pending:
                    due_at = min(item[3] for item in pending)
                    wait_left = due_at - time.monotonic()
                    if wait_left > 0:
                        if self.stop_event.wait(wait_left):
                            break
                        continue

                expect_animation = any(not action[2] for action in pending)
                if pending:
                    base_timeout = max(1.5, min(5.0, 1.35 + len(pending) * 0.018))
                else:
                    base_timeout = 2.5
                capture_timeout = max(base_timeout, capture_timeout_override or 0.0)
                capture_started = time.monotonic()
                try:
                    board, shape = capture_board(
                        self.region, rows, cols, stop_event=self.stop_event,
                        after_action=bool(pending), expect_animation=expect_animation,
                        timeout=capture_timeout
                    )
                    capture_elapsed = time.monotonic() - capture_started
                    last_capture_ms = capture_elapsed * 1000.0
                    # Exponential moving average self-calibrates to the actual game,
                    # browser load and animation speed rather than assuming one delay.
                    response_time_ema = max(
                        0.035, min(4.0, 0.68 * response_time_ema + 0.32 * capture_elapsed)
                    )
                    animation_attempts = 0
                    capture_timeout_override = None
                except BoardCaptureCancelled:
                    break
                except BoardStillAnimatingError as exc:
                    capture_elapsed = time.monotonic() - capture_started
                    response_time_ema = max(
                        0.05, min(4.0, 0.68 * response_time_ema + 0.32 * capture_elapsed)
                    )
                    animation_attempts += 1
                    if animation_attempts < MAX_ATTEMPTS:
                        # Increase the next capture window using the time actually
                        # observed on the failed attempt. The limit is per event, not
                        # a permanent fixed wait for every move.
                        next_timeout = min(
                            12.0,
                            max(capture_timeout + 0.50,
                                capture_timeout * 1.40,
                                capture_elapsed * 1.35)
                        )
                        capture_timeout_override = next_timeout
                        next_wait = adaptive_wait_seconds()
                        if pending:
                            deadline = time.monotonic() + next_wait
                            self.pending_actions = [
                                (r, c, right, deadline, attempts)
                                for r, c, right, _old_deadline, attempts in pending
                            ]
                        self.log(
                            f'Bàn chưa ổn định: lần thử {animation_attempts}/{MAX_ATTEMPTS}; '
                            f'ảnh mất {capture_elapsed * 1000:.0f} ms, lần tới cho tối đa '
                            f'{next_timeout:.1f} s rồi tự kiểm tra lại.'
                        )
                        if not pending:
                            if self.stop_event.wait(next_wait):
                                break
                        continue
                    self.log(
                        f'Bàn vẫn chưa ổn định sau {MAX_ATTEMPTS} lần thử tự điều chỉnh thời gian '
                        f'({capture_elapsed * 1000:.0f} ms ở lần cuối). Đã ngừng để tránh click sai.'
                    )
                    break

                rows, cols = shape

                # A quarantined ambiguous tile becomes usable as soon as a fresh stable
                # frame identifies it. A click-exhausted tile is released only when it
                # is no longer plainly covered, avoiding an endless click loop.
                for qcell, reason in list(quarantined_cells.items()):
                    qr, qc = qcell
                    current_state = board[qr][qc]
                    if (reason == 'read' and current_state != ERR) or (reason == 'click' and current_state != COVERED):
                        quarantined_cells.pop(qcell, None)
                        bad_read_attempts_by_cell.pop(qcell, None)
                        self.log(f'Ô cách ly ({qr+1},{qc+1}) đã được nhận diện lại; đưa về bàn suy luận.')
                # A clear read resets the consecutive unreadable counter, so a future
                # unrelated animation on the same tile gets a fresh set of five tries.
                for cell_rc in list(bad_read_attempts_by_cell):
                    if board[cell_rc[0]][cell_rc[1]] != ERR:
                        bad_read_attempts_by_cell.pop(cell_rc, None)

                # Confirm every action from the preceding batch. `click_attempts` is
                # the number of actual click sends, including the first one.
                if self.pending_actions:
                    unconfirmed = []
                    for move_r, move_c, move_right, verify_deadline, click_attempts in self.pending_actions:
                        target_state = board[move_r][move_c]
                        if move_right:
                            confirmed = target_state == FLAG
                        else:
                            confirmed = isinstance(target_state, int) and 0 <= target_state <= 8
                        key = (move_r, move_c, move_right)
                        if confirmed:
                            ambiguous_confirmation_reads.pop(key, None)
                        else:
                            unconfirmed.append((move_r, move_c, move_right,
                                                verify_deadline, target_state, click_attempts))

                    if unconfirmed:
                        now = time.monotonic()
                        retryable = []
                        rescan_only = []
                        exhausted = []

                        for item in unconfirmed:
                            r, c, right, _deadline, state, attempts = item
                            key = (r, c, right)
                            if state == COVERED:
                                # The frame is stable and this target is still clearly
                                # covered. Retry the intended click, but never more than
                                # five click sends total for this target.
                                ambiguous_confirmation_reads.pop(key, None)
                                if attempts < MAX_ATTEMPTS:
                                    retryable.append(item)
                                else:
                                    exhausted.append((item, 'click', attempts))
                            else:
                                # '?' or an unexpected state means we do NOT know
                                # whether the click worked. Re-read at adaptively spaced
                                # intervals; do not blindly click an ambiguous cell.
                                reads = ambiguous_confirmation_reads.get(key, 0) + 1
                                ambiguous_confirmation_reads[key] = reads
                                if reads < MAX_ATTEMPTS:
                                    rescan_only.append(item)
                                else:
                                    exhausted.append((item, 'read', reads))

                        retry_index = max(
                            [attempts for *_prefix, attempts in retryable] +
                            [ambiguous_confirmation_reads.get((r, c, right), 1)
                             for r, c, right, *_rest in rescan_only] + [1]
                        )
                        next_wait = adaptive_wait_seconds(retry=True, attempt=retry_index)
                        next_deadline = now + next_wait
                        new_pending = []
                        # Retry all clearly-unacted-on cells in one pass, preserving
                        # TURBO batching. Each target has its own five-click ceiling.
                        for r, c, right, _deadline, state, attempts in retryable:
                            action = 'cờ' if right else 'ô an toàn'
                            self.log(
                                f'Xác minh {action} ({r+1},{c+1}): lần click '
                                f'{attempts + 1}/{MAX_ATTEMPTS}; độ trễ ảnh tự đo '
                                f'{last_capture_ms:.0f} ms, chờ khoảng {next_wait * 1000:.0f} ms.'
                            )
                            if not self.click_cell(r, c, rows, cols, right=right, fast=turbo):
                                self.log('Đã ngắt khi gửi lượt thử lại.')
                                break
                            new_pending.append((r, c, right, next_deadline, attempts + 1))
                        if self.stop_event.is_set():
                            break

                        for r, c, right, _deadline, state, attempts in rescan_only:
                            new_pending.append((r, c, right, next_deadline, attempts))
                            reads = ambiguous_confirmation_reads.get((r, c, right), 0)
                            self.log(
                                f'Ảnh tại ({r+1},{c+1}) chưa xác định được; đọc lại '
                                f'{reads + 1}/{MAX_ATTEMPTS} sau khoảng {next_wait * 1000:.0f} ms '
                                f'(thời gian tự đo từ ảnh trước: {last_capture_ms:.0f} ms).'
                            )

                        if exhausted:
                            for item, kind, count in exhausted:
                                r, c, right, _deadline, state, attempts = item
                                quarantined_cells[(r, c)] = kind
                                if kind == 'click':
                                    self.log(
                                        f'Đã thử {MAX_ATTEMPTS} lần nhưng {"cắm cờ" if right else "mở ô"} '
                                        f'tại ({r+1},{c+1}) chưa được xác nhận. Cách ly ô này, '
                                        'tiếp tục suy luận các ô khác; không gửi click thêm vào đây.'
                                    )
                                else:
                                    self.log(
                                        f'Đã đọc lại {MAX_ATTEMPTS} lần nhưng ô ({r+1},{c+1}) vẫn chưa rõ. '
                                        'Cách ly như một ô ẩn tiềm năng và tiếp tục phần bàn còn lại.'
                                    )

                        self.pending_actions = new_pending
                        continue

                    # All actions from the previous batch have been confirmed.
                    self.pending_actions = []
                    ambiguous_confirmation_reads.clear()

                bad = [(r, c) for r in range(rows) for c in range(cols)
                       if board[r][c] == ERR and (r, c) not in quarantined_cells]
                if bad:
                    # Count reads per cell, not per whole-board pass. At TURBO, retry
                    # intervals adapt to measured capture/animation latency. If one tile
                    # remains unreadable after five reads, isolate it and keep solving
                    # the remaining board instead of abandoning the whole game.
                    retryable_bad = []
                    newly_quarantined = []
                    for cell_rc in bad:
                        attempts = bad_read_attempts_by_cell.get(cell_rc, 0) + 1
                        bad_read_attempts_by_cell[cell_rc] = attempts
                        if attempts < MAX_ATTEMPTS:
                            retryable_bad.append((cell_rc, attempts))
                        else:
                            quarantined_cells[cell_rc] = 'read'
                            newly_quarantined.append(cell_rc)
                    if newly_quarantined:
                        examples = ', '.join(f'({r+1},{c+1})' for r, c in newly_quarantined[:4])
                        self.log(
                            f'{len(newly_quarantined)} ô vẫn chưa rõ sau {MAX_ATTEMPTS} lần đọc '
                            f'({examples}); cách ly tạm thời và tiếp tục suy luận phần còn lại.'
                        )
                    if retryable_bad:
                        cell_rc, attempt_no = retryable_bad[0]
                        retry_wait = adaptive_wait_seconds(retry=True, attempt=attempt_no)
                        self.log(
                            f'Ảnh còn {len(bad)} ô chưa rõ, ví dụ ({cell_rc[0]+1},{cell_rc[1]+1}); '
                            f'quét lại {attempt_no + 1}/{MAX_ATTEMPTS} sau '
                            f'{retry_wait * 1000:.0f} ms (độ trễ tự đo {last_capture_ms:.0f} ms).'
                        )
                        if self.stop_event.wait(retry_wait):
                            break
                        continue

                # Use a conservative solver view: unreadable tiles are modelled as
                # potential covered cells, never clicked directly. This weakens clues
                # rather than inventing a number, so deductions on other cells stay safe.
                solver_board = [
                    [COVERED if value == ERR else value for value in row]
                    for row in board
                ]
                active_hidden = [(r, c) for r in range(rows) for c in range(cols)
                                 if board[r][c] == COVERED and (r, c) not in quarantined_cells]
                all_hidden = [(r, c) for r in range(rows) for c in range(cols)
                              if board[r][c] in (COVERED, ERR)]
                flags = [(r, c) for r in range(rows) for c in range(cols) if board[r][c] == FLAG]
                if len(flags) > mines:
                    self.log(f'Dừng: nhìn thấy {len(flags)} cờ nhưng tổng mìn nhập là {mines}.')
                    break
                self.log(
                    f'Bàn hiện tại: {rows}×{cols}; ô chưa mở={len(all_hidden)}; '
                    f'cờ thấy={len(flags)}; ô cách ly={len(quarantined_cells)}.'
                )
                if not all_hidden:
                    self.log('Không còn ô chưa mở. Có thể đã thắng hoặc ván đã kết thúc.')
                    break
                if not active_hidden:
                    now_idle = time.monotonic()
                    retry_wait = adaptive_wait_seconds(retry=True, attempt=MAX_ATTEMPTS)
                    if now_idle - last_quarantine_idle_log > 2.0:
                        self.log(
                            f'Chỉ còn {len(all_hidden)} ô bị cách ly/chưa rõ; tiếp tục quét nền '
                            f'(nhịp khoảng {retry_wait * 1000:.0f} ms). Nhấn F6/DỪNG để kết thúc.'
                        )
                        last_quarantine_idle_log = now_idle
                    if self.stop_event.wait(max(0.05, retry_wait)):
                        break
                    continue

                solve_started = time.perf_counter()
                try:
                    # First pass performs only logical deductions. Expensive exact
                    # risk enumeration is deferred unless the bot actually needs to guess.
                    known_mines, known_safe, _ = logical_deductions(solver_board, mines, compute_risk=False)
                except Exception as exc:
                    self.log(f'Dừng vì mâu thuẫn suy luận: {exc}')
                    break

                remaining_flags = mines - len(flags)
                forced_to_flag = sorted(
                    cell for cell in (known_mines - set(flags))
                    if board[cell[0]][cell[1]] == COVERED and cell not in quarantined_cells
                )[:max(0, remaining_flags)]
                safe_moves = sorted(
                    cell for cell in known_safe
                    if board[cell[0]][cell[1]] == COVERED and cell not in quarantined_cells
                )

                solve_elapsed_ms = (time.perf_counter() - solve_started) * 1000.0

                if turbo and (forced_to_flag or safe_moves):
                    actions = [(r, c, True) for r, c in forced_to_flag]
                    actions.extend((r, c, False) for r, c in safe_moves)
                    actions = self._order_actions_by_distance(actions, rows, cols)
                    issued = 0
                    for r, c, right in actions:
                        if self.stop_event.is_set():
                            break
                        if not self.click_cell(r, c, rows, cols, right=right, fast=True):
                            break
                        issued += 1
                    if issued != len(actions):
                        self.log('Turbo đã bị ngắt bởi lệnh dừng.')
                        break
                    next_wait = adaptive_wait_seconds()
                    deadline = time.monotonic() + next_wait
                    self.pending_actions = [
                        (r, c, right, deadline, 1) for r, c, right in actions
                    ]
                    flag_count = len(forced_to_flag)
                    safe_count = len(safe_moves)
                    self.log(
                        f'TURBO: gửi {issued} nước chắc chắn ({flag_count} cờ, {safe_count} ô mở); '
                        f'tính logic {solve_elapsed_ms:.1f} ms, nhận diện song song tối đa {MAX_WORKERS} luồng; '
                        f'độ trễ ảnh đo được {last_capture_ms:.0f} ms, bắt đầu quét lại ngay.'
                    )
                    continue

                # Nonzero speeds honour the requested per-move pacing. TURBO omits
                # normal delay; adaptive waiting is reserved for failed/ambiguous retries.
                if forced_to_flag:
                    r, c = forced_to_flag[0]
                    if not self.click_cell(r, c, rows, cols, right=True):
                        break
                    next_wait = adaptive_wait_seconds()
                    deadline = time.monotonic() + next_wait
                    self.pending_actions = [(r, c, True, deadline, 1)]
                    self.log(f'Cắm cờ tại hàng {r+1}, cột {c+1}.')
                    continue

                if safe_moves:
                    r, c = safe_moves[0]
                    if not self.click_cell(r, c, rows, cols, right=False):
                        break
                    next_wait = adaptive_wait_seconds()
                    deadline = time.monotonic() + next_wait
                    self.pending_actions = [(r, c, False, deadline, 1)]
                    self.log(f'Mở ô chắc an toàn tại hàng {r+1}, cột {c+1}.')
                    continue

                # No deterministic move: either wait for manual input, or estimate
                # probabilities only now. Exact enumeration can be the slowest solver step.
                if not self.allow_guess:
                    if not self.wait_for_go('Không còn nước chắc chắn; tự mở một ô hoặc tự xử lý game rồi bấm ĐI TIẾP'):
                        break
                    continue
                try:
                    _, _, risks = logical_deductions(solver_board, mines, compute_risk=True)
                except Exception as exc:
                    self.log(f'Dừng khi tính rủi ro: {exc}')
                    break
                candidates = [
                    (p, rc) for rc, p in risks.items()
                    if board[rc[0]][rc[1]] == COVERED and rc not in quarantined_cells
                ]
                if not candidates:
                    now_idle = time.monotonic()
                    retry_wait = adaptive_wait_seconds(retry=True, attempt=MAX_ATTEMPTS)
                    if now_idle - last_quarantine_idle_log > 2.0:
                        self.log(
                            'Không còn ô có thể click an toàn từ ảnh hiện tại; tiếp tục polling '
                            f'(nhịp khoảng {retry_wait * 1000:.0f} ms), không click ô mơ hồ. '
                            'Nhấn F6/DỪNG nếu muốn kết thúc.'
                        )
                        last_quarantine_idle_log = now_idle
                    if self.stop_event.wait(max(0.05, retry_wait)):
                        break
                    continue
                candidates.sort(key=lambda item: (item[0], item[1]))
                chance, (r, c) = candidates[0]
                self.log(f'Không có nước chắc chắn: đoán ô ({r+1},{c+1}), ước lượng nguy cơ {chance:.1%}.')
                if not self.click_cell(r, c, rows, cols, right=False):
                    break
                next_wait = adaptive_wait_seconds()
                deadline = time.monotonic() + next_wait
                self.pending_actions = [(r, c, False, deadline, 1)]
            self.log('Tác vụ đã kết thúc.')
        except Exception as exc:
            self.log(f'Lỗi: {exc}')
        finally:
            self.running = False
            if not self.closing:
                try:
                    self.root.after(0, self._restore_after_run)
                except Exception:
                    pass

    def _move_cursor_smooth(self, x, y, fast=False):
        """Move the pointer visibly between cells, with a short eased trajectory.

        Turbo removes artificial per-move delays, not physical cursor travel. This
        keeps the animation natural while still moving much faster than a human.
        """
        if self.stop_event.is_set():
            return False
        if sys.platform == 'win32':
            user32 = ctypes.windll.user32

            class POINT(ctypes.Structure):
                _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]

            point = POINT()
            if not user32.GetCursorPos(ctypes.byref(point)):
                start_x, start_y = int(x), int(y)
            else:
                start_x, start_y = int(point.x), int(point.y)
            dx, dy = x - start_x, y - start_y
            distance = math.hypot(dx, dy)
            if fast:
                # Still visibly moves, but avoids the old 24–72 ms travel cost on
                # every cell in a large Turbo batch.
                duration = min(0.018, max(0.006, 0.004 + distance / 40000.0))
                steps = max(2, min(5, int(math.ceil(duration * 250))))
            else:
                duration = min(0.105, max(0.038, 0.032 + distance / 9000.0))
                steps = max(3, min(12, int(math.ceil(duration * 160))))
            for step in range(1, steps + 1):
                if self.stop_event.is_set():
                    return False
                t = step / steps
                eased = t * t * (3.0 - 2.0 * t)  # smoothstep acceleration/deceleration
                px = int(round(start_x + dx * eased))
                py = int(round(start_y + dy * eased))
                user32.SetCursorPos(px, py)
                if step < steps and self.stop_event.wait(duration / steps):
                    return False
            return not self.stop_event.is_set()

        # Portable fallback. Non-Windows is not the primary target, but keep the
        # same visible easing there too.
        try:
            start_x, start_y = pyautogui.position()
            distance = math.hypot(x - start_x, y - start_y)
            duration = min(0.105, max(0.038, 0.032 + distance / 9000.0))
            pyautogui.moveTo(x, y, duration=duration, tween=pyautogui.easeInOutQuad)
        except Exception:
            pyautogui.moveTo(x, y)
        return not self.stop_event.is_set()

    def _order_actions_by_distance(self, actions, rows, cols):
        """Greedy nearest-neighbour route to reduce cursor travel inside large batches."""
        if len(actions) < 3 or not self.region:
            return list(actions)
        x1, y1, x2, y2 = self.region
        try:
            mx, my = pyautogui.position()
            current_c = (float(mx - x1) / max(1.0, (x2 - x1) / cols)) - 0.5
            current_r = (float(my - y1) / max(1.0, (y2 - y1) / rows)) - 0.5
        except Exception:
            current_r, current_c = 0.0, 0.0

        remaining = list(actions)
        ordered = []
        # At most 480 cells for the built-in Hard board: the O(n^2) route planning
        # is tiny compared with moving the pointer to every target independently.
        while remaining:
            best_i = 0
            best_d = float('inf')
            for i, (r, c, _right) in enumerate(remaining):
                d = (r - current_r) ** 2 + (c - current_c) ** 2
                if d < best_d:
                    best_d, best_i = d, i
            action = remaining.pop(best_i)
            ordered.append(action)
            current_r, current_c = action[0], action[1]
        return ordered

    def click_cell(self, r, c, rows, cols, right=False, fast=False):
        x1, y1, x2, y2 = self.region
        x = int(round(x1 + (c + 0.5) * (x2 - x1) / cols))
        y = int(round(y1 + (r + 0.5) * (y2 - y1) / rows))
        if pyautogui.FAILSAFE and x <= 1 and y <= 1:
            raise pyautogui.FailSafeException('PyAutoGUI fail-safe triggered at the upper-left corner.')

        # Smooth cursor travel is intentionally retained in TURBO. The mode omits
        # fixed waits between reasoning rounds, but should not teleport the pointer.
        if not self._move_cursor_smooth(x, y, fast=fast):
            return False
        if self.stop_event.is_set():
            return False

        if sys.platform == 'win32':
            user32 = ctypes.windll.user32
            down = 0x0008 if right else 0x0002  # RIGHTDOWN / LEFTDOWN
            up = 0x0010 if right else 0x0004    # RIGHTUP / LEFTUP
            user32.mouse_event(down, 0, 0, 0, 0)
            # Keep a short down interval so Chromium reliably receives each input.
            # Turbo uses 1 ms rather than spending 4 ms at every target.
            time.sleep(0.001 if fast else 0.004)
            user32.mouse_event(up, 0, 0, 0, 0)
        else:
            button = 'right' if right else 'left'
            pyautogui.mouseDown(button=button)
            time.sleep(0.001 if fast else 0.004)
            pyautogui.mouseUp(button=button)
        return True


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        if 'vista' in style.theme_names():
            style.theme_use('vista')
    except Exception:
        pass
    AutoPilotApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
