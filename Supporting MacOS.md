# Supporting macOS

Plan for running PDF Signer on a Mac. Nothing here is implemented yet.

## Why Docker does not work on a Mac

On Linux, the launcher (`src/__main__.py`) runs `sign.py` in a Docker container and passes the card reader in with `-v /dev/bus/usb:/dev/bus/usb`. On a Mac, Docker runs inside a Linux VM (Colima, Docker Desktop), and the VM does not see the Mac's USB devices. The container starts, but it finds no card.

So on a Mac, `sign.py` runs directly on the host. It is plain Python, and pyHanko and python-pkcs11 both work on macOS.

## What the user installs

| Item | Notes |
|---|---|
| Homebrew Python 3.12 or later | `brew install python`. Apple's `/usr/bin/python3` is 3.9 and is not supported. `sign.py` uses the `%:z` date format, which needs 3.12 |
| SafeSign for macOS | From <https://safesign.gdamericadosul.com.br/download>, choosing Mac. It provides the PKCS#11 library `libaetpkss*.dylib` |

Nothing else is needed:

- The PC/SC service, which is `pcscd` on Linux, is built into macOS.
- macOS includes a USB driver for standard CCID readers, which is `libccid` on Linux.
- Time zones are built into macOS, so no `tzdata` is needed.

### Checking the SafeSign library

After installing SafeSign, find the library and check its architecture:

```bash
find / -name 'libaetpkss*' 2>/dev/null
file /path/to/libaetpkss.dylib
```

- If the output lists `arm64`, or `arm64` and `x86_64`, the library works with Homebrew Python on Apple Silicon.
- If it lists only `x86_64`, Python must also be x86_64 to load it: an Intel Homebrew under `/usr/local`, run through Rosetta. Record which case applies here.

Record the real install path too. It becomes the default `--module` on macOS.

## Design: the `--native` flag

Add `--native` to the launcher. With it, the launcher runs `sign.py` on the host instead of in Docker. On macOS, native mode is the default, because Docker cannot work there. `--native` stays available on Linux for testing.

### Why the libraries are not bundled in the `.pyz`

Python can import `.py` files from a zip file, but it cannot load compiled extensions (`.so`) from one. These dependencies are compiled:

| Package | Why it is needed |
|---|---|
| `python-pkcs11` | Talks to SafeSign |
| `cryptography` | Signatures, used by pyHanko |
| `uharfbuzz` | Text shaping, from the `opentype` extra |
| `Pillow` | Loads the watermark, from the `image-support` extra |

Bundling them would also tie the `.pyz` to one OS, one CPU architecture and one Python version. Instead, native mode creates a venv on first use, the same way Docker mode builds an image on first use.

### Venv created on first run

Cache folder: `~/Library/Caches/pdf-signer/` on macOS, `~/.cache/pdf-signer/` on Linux.

1. Hash the `image/` files, the same as `ensure_image()` does now, and use the hash as the folder name: `~/Library/Caches/pdf-signer/<hash>/`. A new `.pyz` then gets a fresh setup, and an old one is never half-reused.
2. If the folder does not exist, build it in a temporary folder and rename it when done, so a failed install leaves nothing broken behind:
   - `python3 -m venv venv`, using the launcher's own interpreter (`sys.executable`).
   - `venv/bin/pip install "pyHanko[pkcs11,opentype,image-support]==0.38.0" requests`. These pins must match `src/image/Dockerfile`. Better, move them to one `requirements.txt` in `image/` that both use.
   - Unpack `sign.py`, `web/`, `roots/`, `watermark.png` and the font from the `.pyz` into `app/`.
3. Run `venv/bin/python app/sign.py ...` with the PDF's folder as the working directory.

The first run needs internet access, which is already a requirement.

### Launcher changes (`src/__main__.py`)

- **Startup check.** `main()` exits when `docker` is missing. Do that only in Docker mode.
- **Running `sign.py`.** Add `native_command(work_dir)`, which returns `[venv_python, app/sign.py]` and is used in place of `docker_command()`. In `pick()`:
  - Set `PDF_SIGNER_TOKEN` and `PDF_SIGNER_PORT` in the environment, as now.
  - Use `subprocess.run(command, cwd=source.parent)`.
  - Do not use `-p`, `run_named()` or `docker rm`. Ctrl-C reaches the child process directly.
- **`sign` and `list`.** Run the same native command with `sys.argv[1:]`.
- **Watermark.** For a `watermark.png` beside the `.pyz`, pass `--watermark <path>` instead of the Docker volume mount.
- **Time zone.** `timezone()` reads `/etc/timezone`, which does not exist on macOS. Native mode does not need it, because Python uses the host's local time. Do not set `TZ`.
- **`uninstall`.** Also delete the cache folder, after asking for confirmation as it does for images. Skip the Docker part when Docker is not installed.

### Signer changes (`src/image/sign.py`)

- **Web server address, line 596. This is a security fix.** It listens on `0.0.0.0`, which is safe only because Docker publishes the port on `127.0.0.1`. Run natively, it would put the signing page on the local network. Change it to `127.0.0.1`. Inside Docker, the published port then cannot reach it, so either:
  - read the address from an environment variable: `0.0.0.0` in Docker, `127.0.0.1` native; or
  - always use `127.0.0.1` and run Docker with `--network host` on Linux.

  The environment variable is simpler.
- **Card library, line 36.** `DEFAULT_MODULE = "/usr/lib/libaetpkss.so"`. Choose it by platform: on `sys.platform == "darwin"`, use the macOS path recorded above. `--module` still overrides it.
- **Font, line 37.** `FONT` points to a Linux system font that the Docker image installs. Bundle a font file in `image/fonts/` and point to it with `Path(__file__).parent / "fonts" / ...`, as `WEB_DIR` and `ROOTS_DIR` already do. Then both modes use the same font, and the stamps look the same:
  - Use an OFL-licensed font, such as Liberation Sans or DejaVu Sans, for simple licensing. Nimbus Sans is AGPL with a font exception.
  - Add its license file next to it, and mention it in the README's third-party list.
  - Remove `fonts-urw-base35` from the Dockerfile.
  - Widths are calculated from the font, so stamp widths may change slightly. Update the size table in the README.
- **`os.chown` after signing, line 451.** Natively, it changes the owner to the same user, which works. No change needed.

### Unchanged

`getpass` for the PIN, `webbrowser.open`, the `[o]`/`[p]` port choice and the SSH tunnel instructions all work the same on a Mac.

## Test plan (on the Mac)

1. Install Homebrew Python and SafeSign. Record the library path and architecture.
2. Before writing launcher code, check the card with a manual venv:
   ```bash
   python3 -m venv /tmp/ps && /tmp/ps/bin/pip install "pyHanko[pkcs11,opentype,image-support]==0.38.0" requests
   /tmp/ps/bin/python src/image/sign.py --module /path/to/libaetpkss.dylib list
   ```
   The certificate should be listed without asking for a PIN. If this fails, stop and fix it first. The rest depends on it.
3. Implement the changes above and build with `make`.
4. Run `python3 pdf-signer.pyz list`. On the first run, the venv is created.
5. Run `python3 pdf-signer.pyz test.pdf` with `[o]`, place stamps and sign.
6. Validate the result at <https://validar.iti.gov.br>.
7. Check that the page is not reachable from another machine at `http://<mac-ip>:<port>/`.
8. Run `--place` signing, a `watermark.png` override and `uninstall`.
9. On Linux, check that Docker mode still works and the stamps still look right with the new font.
10. Update the README with a macOS section.
