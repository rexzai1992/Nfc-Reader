# NFC Keyboard Wedge (Python, Windows + macOS)

This app reads an NTAG213 NFC tag through a PC/SC reader and types the decoded value into the active window (Notepad, TextEdit, Excel, browser fields, POS software, etc.).

Primary behavior:
1. Read UID.
2. Read NTAG memory from page 4 and parse Type 2 TLV (`0x03` NDEF TLV).
3. Decode NDEF Text record (and simple URI record).
4. Type decoded output using `pynput`.
5. Optional Enter key press after typing.

If NDEF cannot be decoded, it falls back to UID mapping (`CARD_MAP`).

## Files
- `main.py`
- `requirements.txt`
- `README.md`

## Requirements
- Python 3
- NFC reader that appears as a PC/SC smart card reader
- NTAG213 tag (NDEF Text recommended)

Dependencies:
- `pyscard`
- `pynput`

Install:
```bash
pip install -r requirements.txt
```

## Windows Setup (10/11)
1. Install Python 3.
2. Install NFC reader driver if needed.
3. Confirm reader appears in **Device Manager -> Smart card readers**.
4. In this project folder, run:
```bash
pip install -r requirements.txt
python main.py
```

## macOS Setup
1. Install Python 3.
2. Install dependencies:
```bash
pip3 install -r requirements.txt
```
3. Run the app once to trigger macOS permission prompt:
```bash
python3 main.py
```
4. Grant keyboard control permission:
   - **System Settings -> Privacy & Security -> Accessibility**
   - Allow Terminal (or the app running Python).
   - The script also attempts to open this settings page automatically.

## macOS `pyscard` Troubleshooting
If `pyscard` fails to build/install:
1. Install Apple command-line tools:
```bash
xcode-select --install
```
2. Install build tools:
```bash
brew install swig pkg-config
```
3. Retry:
```bash
pip3 install pyscard
```
4. If still failing on PC/SC headers/libs, install PCSC-lite and retry:
```bash
brew install pcsc-lite
pip3 install pyscard
```

Note: macOS already has PC/SC services, but build environments can still miss headers/tooling.

## Run
```bash
python main.py
```
or on macOS:
```bash
python3 main.py
```

Then:
1. Click into your target app input field (Notepad/TextEdit/Excel/browser/POS).
2. Tap your NTAG213 card.
3. The decoded value is typed into the active window.

Stop with `Ctrl+C`.

## Configuration (`main.py`)
Edit these at the top of the file:
```python
PRESS_ENTER_AFTER_SCAN = True
COOLDOWN_SECONDS = 2
READER_INDEX = None
CARD_MAP = {
    "04C42A72F47380": "12345",
}
```

- `PRESS_ENTER_AFTER_SCAN`: press Enter after typing.
- `COOLDOWN_SECONDS`: minimum interval before same UID can type again.
- `READER_INDEX`: set fixed reader index, or `None` to prompt when multiple readers are present.
- `CARD_MAP`: fallback output when NDEF read fails.

## Reader Selection
- App prints all available PC/SC readers.
- If one reader exists, it auto-selects.
- If multiple readers exist, it asks for an index (unless `READER_INDEX` is set).

## Logging
The app prints useful logs for:
- available readers
- selected reader
- card detected
- UID
- NDEF text found
- typed output
- read errors / fallback behavior
- card removal / reader reconnect status

## Fallback Mode (UID Mapping)
Behavior order:
1. Try NDEF decode first.
2. If NDEF exists, type NDEF value.
3. If no NDEF value, check `CARD_MAP` using UID.
4. If UID mapped, type mapped value.
5. If neither exists, print:
   - `No NDEF text and UID not mapped.`

## Notes on Reader Compatibility
- This app is built for PC/SC readers and tries multiple command styles used by common readers.
- Some NFC readers support UID reading but do **not** expose NTAG page memory over PC/SC.
- In that case, NDEF reading may fail and UID fallback may be used.
- This can happen on both Windows and macOS depending on reader firmware/driver.

## `pynput` vs `pyautogui`
This project uses `pynput` by default because it is lightweight and works cross-platform for keyboard-wedge behavior.

Tradeoff:
- `pynput` can require Accessibility permission on macOS.
- `pyautogui` can be useful in some edge cases, but it adds extra dependency and is not required for this project.

## Troubleshooting
### No reader found
- Check USB cable/port.
- Reinstall or update reader driver.
- Confirm Smart Card service is running.
- Windows: check **Device Manager -> Smart card readers**.
- macOS: check **System Information -> USB** and reader presence.

### Card detected but no text
- Ensure the tag contains an **NDEF Text** record.
- Re-write the tag with NFC Tools (phone) using Text record.
- Some readers may not expose NTAG memory pages over PC/SC.

### It types UID mapping value instead of `12345` from tag
- NDEF decode likely failed, so fallback `CARD_MAP` was used.
- Check NDEF content on the tag and reader compatibility.

### It types repeatedly
- Increase `COOLDOWN_SECONDS`.
- Keep one tap/removal cycle per scan.

### macOS does not type
- Enable Accessibility permission for Terminal/Python app.
- Restart Terminal after granting permission.

## Important
Main target is NDEF content from NTAG213, not UID-only scanning.  
UID mapping is fallback when NDEF read is unavailable.
