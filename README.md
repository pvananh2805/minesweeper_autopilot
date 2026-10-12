# Google Minesweeper AutoPilot

Google Minesweeper AutoPilot is a Python-based desktop automation tool designed to play Google Minesweeper on Chrome and Edge on Windows 10/11. It captures the selected game area, recognizes the board using image processing, solves logical constraints, and simulates mouse clicks to open safe cells and place flags automatically.

## Key Features

- **Three difficulty levels:** Easy (10×8, 10 mines), Medium (18×14, 40 mines), and Hard (24×20, 99 mines), with support for custom board sizes.
- **Automated gameplay:** Detects safe cells and confirmed mines, executes moves in batches, and continues playing without requiring manual input for every move.
- **Turbo mode:** Setting the action speed to 0 ms removes intentional delays between batches, helping the bot play as quickly as screen capture, computation, mouse input, and game animations allow.
- **Image recognition:** Uses RGB/HSV color analysis, with optional Tesseract OCR as a fallback.
- **Performance optimizations:** Uses NumPy-based image processing, up to 12 worker threads for independent cell recognition, bitmask-based logic, and branch-and-bound probability estimation.
- **Adaptive verification:** Measures screen response times, retries uncertain recognition up to five times, and avoids clicking cells whose states remain unclear.
- **Safety controls:** Includes a global F6 emergency stop, a Stop button, and a preview scan to verify board recognition before automated play.
- **Smooth mouse movement:** Moves the cursor between cells while reducing unnecessary travel between actions.

## Requirements

Python 3.10 or newer and Windows 10/11. Install dependencies using `pip install -r requirements.txt`. Tesseract OCR is optional.

## Disclaimer

This project is an experimental prototype and has not been verified across every screen resolution, browser zoom level, or game interface variation. Always inspect the preview scan before starting automation. Minesweeper guesses can still be incorrect, and winning is not guaranteed. Use automation only where permitted.
## Demo

[▶ Watch the AutoPilot demo](https://github.com/pvananh2805/minesweeper_autopilot/issues/1)
