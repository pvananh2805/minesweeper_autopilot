GOOGLE MINESWEEPER AUTOPILOT (PROTOTYPE)
========================================

Designed primarily for Google Minesweeper running in Chrome/Edge on Windows 10/11.
The tool captures the selected screen region and simulates mouse clicks; it does not read private data from inside the web page.

REQUIREMENTS
------------
- Python 3.10+ (Python 3.11/3.12 recommended)
- Windows 10/11
- mss: faster screen-region capture on Windows (automatically falls back to ImageGrab if MSS does not work)
- Tesseract OCR is optional and is used only as a fallback; the Google color-recognition engine works without OCR
- Google Minesweeper open in the browser, with the board clearly visible and not covered by another window

INSTALLATION
------------
Open PowerShell in this folder and run:
  python -m pip install -r requirements.txt

Tesseract is optional. If you install it for fallback OCR, make sure the `tesseract` command is available in PATH.

RUNNING
-------
Run `run.bat`, or use:
  python minesweeper_autopilot.py

HOW TO USE
----------
1. Open Google Minesweeper in Chrome/Edge and choose a difficulty in the game.
2. In the tool, select the matching difficulty first:
   - Easy: 10 columns × 8 rows, 10 mines
   - Medium: 18 columns × 14 rows, 40 mines
   - Hard: 24 columns × 20 rows, 99 mines
   - Custom: enter the total number of mines, rows, and columns yourself.
   These are the grid dimensions used by the Google Minesweeper interface. If the board on your screen differs, use Custom.
3. Click “Select Region” and select only the grid area, close to the outer edges of the cells. Do not include the score, counter, or menu above the board.
4. Click “Test Scan” first. Check each row in the log: ■ = unopened cell, F = flag, 0 = empty cell, 1..8 = number, ? = not recognized.
5. Click “Start Auto Play” only when the scan matches the board and contains no “?”. When started, the tool minimizes itself so it does not cover the game, then runs automatically; you do not need to click each move yourself.
6. The bot prioritizes logically certain moves. If “Allow Guessing” is enabled, it chooses the cell with the lowest estimated risk when no certain moves remain; it can still lose.
7. Configure “Delay per move (ms)”: enter 0 to enable TURBO. TURBO does not add a fixed delay before the next batch of moves; it begins polling immediately and waits only until the image/animation is actually stable. From one captured board state, the bot batches together logically certain moves, orders the mouse path by nearest cell to reduce cursor travel, then scans and solves again after the batch. The cursor still moves with smoothing; Google’s animation time and browser click processing cannot be skipped. Independent cell recognition uses up to 12 threads. The logic solver uses bitmasks to speed up subset operations, while guess probabilities are computed with branch-pruned DFS. The bot logs the solver time for each batch. When verification fails in TURBO, the retry delay is based on the measured image-response time instead of repeatedly retrying at 0 ms. Unclear cells are re-read up to 5 times; if they remain ambiguous, the tool quarantines them as potentially hidden cells and does not click them, while continuing to solve other cells. If they become readable later, they are automatically returned to the solver. Screen capture uses MSS when available, and guess probabilities are calculated only when needed.
8. Press F6 from any window to trigger a global emergency stop on Windows. The “STOP” button also stops the bot. When the task ends, the tool window reappears so you can review the log.
9. Use “CONTINUE” only when guessing is disabled and the bot is waiting because no certain moves remain. The bot does not stop merely because it has placed the requested number of flags; it continues opening safe cells.

NOTES
-----
- This is a prototype and has not been tested directly at every screen resolution, zoom level, or interface variation. The cell-recognition thread pool is limited to at most 12 workers; mouse clicks intentionally run sequentially to avoid input-device contention.
- The recognizer tries Google’s RGB/HSV colors first; OCR is a fallback. Always inspect the Test Scan before enabling auto play.
- After a click, the tool polls the screen immediately in TURBO (with no fixed added delay), tracks the animation across multiple frames, and only solves again once the image is stable. If verification fails, TURBO uses an adaptive retry interval based on measured response time. If a cell remains unreadable after 5 attempts, it is quarantined and not clicked; the solver treats it as a potentially hidden cell to avoid overly optimistic deductions, while continuing with the rest of the board. If its state becomes readable later, the cell is automatically removed from quarantine.
- If the log contains “?”, the recognized numbers/flags look wrong, or the tool reports a logic contradiction, do not force it to continue. Check that the selected difficulty, region, and browser zoom are correct.
- Simulated-click automation should only be used in personal games and where automation is permitted.
