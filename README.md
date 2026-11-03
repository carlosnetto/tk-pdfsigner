# PDF Signer

Signs PDF files with a digital certificate on a smart card, such as a Brazilian ICP-Brasil e-CPF A3 card.

You open the PDF in a browser, click where each stamp should appear, and type the PIN in the terminal. The result is a real digital signature, not a picture of one. Each stamp is a visible mark, and anyone can check the signature with a validator such as [validar.iti.gov.br](https://validar.iti.gov.br).

It runs on Linux and macOS. The same `pdf-signer.pyz` detects the system, so there is no option to choose.

## Nothing is installed in the system

- **Linux:** everything runs inside a Docker container: the card service (`pcscd`), the reader driver, the SafeSign card library, Python libraries and the signing program. No packages or drivers are installed on the host.
- **macOS:** Docker cannot reach USB card readers on a Mac, so the signer runs on the host. Its Python libraries go into a private venv in `~/Library/Caches/pdf-signer/`, not into the system Python. The card service and reader driver are built into macOS. Only SafeSign is installed, by you, from G+D.

To remove what this tool created, run `python3 pdf-signer.pyz uninstall`.

## Requirements

### Linux

- Docker, usable without `sudo`
- Python 3, which only starts the program
- `pcscd` not running on the host, because only one service can use the reader at a time
- Internet access, for the first build and for checking the certificate when signing

The first run builds the Docker image, which takes a few minutes. Later runs reuse it and start in seconds. The build downloads SafeSign from the G+D site and verifies its checksum. SafeSign is proprietary, so it is not distributed with this tool.

### macOS

- Python 3.12 or later, such as Homebrew's: `brew install python`. Apple's `/usr/bin/python3` is too old.
- [SafeSign IC Standard for macOS](https://safesign.gdamericadosul.com.br/download), which installs `tokenadmin.app` and the card library. Apple Silicon and Intel both work.
- Internet access, for the first run and for checking the certificate when signing

The first run creates the venv, which takes about a minute. Later runs reuse it. A new `pdf-signer.pyz` or a Python upgrade creates a fresh one, and the old one is deleted.

## Usage

```bash
python3 pdf-signer.pyz contract.pdf contract-signed.pdf
```

The program prints the certificate it will use, then asks how you want to open the page:

- `[o]` opens the browser on this computer.
- `[p]` asks for a TCP port, then prints the `ssh -L` command and the URL, for a browser on another computer.

In the browser, choose a stamp size and click on a page to place it. You can drag or remove stamps. The last stamp holds the signature. Click **Sign** and type your PIN in the terminal.

The signed PDF can be downloaded from the browser, and it is also saved next to the original. Each run makes one PIN attempt, because cards lock after a few wrong PINs.

Other commands:

```bash
python3 pdf-signer.pyz list                     # show the card's certificates, no PIN needed
python3 pdf-signer.pyz sign in.pdf out.pdf --place 2 bottom-center large
python3 pdf-signer.pyz uninstall                # remove the Docker image and free the space
```

`--place PAGE WHERE SIZE` can be repeated. `WHERE` is `bottom-left`, `bottom-center`, `bottom-right`, or `X,Y` for the top left corner, in points from the bottom left corner of the page.

## Using a browser on another computer

The card stays on one computer, and the browser can run on another. The page only listens on `localhost`, so it is reached through an SSH tunnel.

1. On the computer with the card, run the program and choose `[p]`:

   ```bash
   python3 pdf-signer.pyz contract.pdf contract-signed.pdf
   ```

2. Enter a TCP port, such as `8765`. The program prints a command and a URL:

   ```text
   ssh -L 8765:localhost:8765 user@card-computer

   http://localhost:8765/<token>/
   ```

3. On the computer with the browser, run that `ssh -L` command and leave it open.
4. Open the URL in the browser on that computer.
5. Place the stamps and click **Sign**.
6. Type the PIN in the terminal on the computer with the card.
7. Download the signed PDF from the browser.

The URL contains a random token that is valid for that run only. Treat it like a password until the run ends.

## Stamp sizes

| Size | Approximate size | Font | Content |
|---|---|---|---|
| `large` | 185 to 225 × 68 pt | 7 pt | Name, CPF, date, certificate, serial number, validator link |
| `small` | 91 × 31 pt | 5 pt | Name, CPF, date |
| `initials` | 23 × 23 pt | 2 pt | Name, CPF, date. Readable when zoomed in |

Widths depend on the text in the certificate. 1 pt is about 0.35 mm.

## Watermark

`watermark.png` appears behind the text of every stamp, and it is built into `pdf-signer.pyz`.

To replace it, put your own `watermark.png` in the same folder as `pdf-signer.pyz`. No rebuild is needed. To change the built-in one, replace `watermark.png` in the source folder and build again.

The default file is **1250 × 800 px**, a 25:16 shape. A replacement should follow these rules:

- **Format:** PNG with transparency.
- **Size:** 1250 × 800 px is recommended. The image is scaled to fit inside the stamp without distortion and is centred, so other shapes also work. Below about 450 × 290 px it can look blurry on a `large` stamp.
- **Background:** fully transparent, not white. A white background hides the page under the stamp.
- **Artwork:** partly transparent, about 35% opacity. Solid artwork hides any page text under the stamp.
- **Colour:** strong enough to show at that opacity, and pale on the page. The default uses RGB 204, 189, 246 at 35%.

| Stamp | Watermark size on the page |
|---|---|
| `large` | About 106 × 68 pt |
| `small` | About 48 × 31 pt |
| `initials` | About 23 × 15 pt |

Use only artwork you have the rights to. The watermark is decoration, and the proof is the digital signature. Transparency is not allowed in PDF/A-1, so a stamped PDF/A-1 file stops being strictly conforming.

## Certificate validation

Before asking for the PIN, the program verifies the certificate chain and asks the issuer whether the certificate was revoked. It stops if the check fails. The chain and the issuer's answer are embedded in the PDF.

Use `--no-validation-data` to sign offline without them. Only ICP-Brasil root v5 is trusted, so certificates under other roots stop with an error.

## macOS notes

- **SafeSign elsewhere.** The card library is expected at `/Applications/tokenadmin.app/Contents/Frameworks/libaetpkss.dylib`. If it is somewhere else, set `PDF_SIGNER_MODULE` to its path.
- **Idle cards.** After a card sits idle, macOS powers it down. Some cards, such as the Certisign e-CPF (YpsID S2), then fail to power up when SafeSign connects, and SafeSign reports the card as not recognised. The signer prevents this by connecting to the card first with the T=0 protocol and keeping that connection open while it runs. Nothing needs to be done, and tokenadmin does not need to be open.
- **tokenadmin** can stay open while signing. It shares the reader.
- **The page** only listens on `127.0.0.1`, so it is not reachable from the network. The SSH tunnel steps above work the same way on a Mac.

## Limits

- A PDF that already has signatures accepts only one stamp.
- Rotated pages cannot be stamped.
- The signing time comes from the computer clock, because there is no trusted timestamp.
- Check the rules of whoever receives the document. This tool does not apply an ICP-Brasil signature policy.

## Building the .pyz

Building needs Python 3 and `make`:

```bash
make          # builds pdf-signer.pyz
make clean    # deletes pdf-signer.pyz
```

Without `make`, run `python3 build-pyz.py`.

`pdf-signer.pyz` is a single file containing the program, the web page, PDF.js, the stamp font, the watermark, the ICP-Brasil root certificate and the Dockerfile. Copy that one file to any computer that meets the requirements.

## License

MIT License, see [LICENSE](LICENSE). You can use, change and redistribute this program freely, with no payment and no warranty, as long as you keep the copyright and license notice.

Copyright (c) 2026 Carlos Augusto Leite Netto.

Third-party software:

- [PDF.js](https://github.com/mozilla/pdf.js) is included under the Apache License 2.0, see `src/image/web/PDFJS-LICENSE`.
- [TeX Gyre Heros](https://www.gust.org.pl/projects/e-foundry/tex-gyre/heros), the stamp font, is included under the GUST Font License, see `src/image/fonts/GUST-FONT-LICENSE.txt`.
- [pyHanko](https://github.com/MatthiasValvekens/pyHanko) is installed during the first run (the Docker image on Linux, the venv on macOS), under its own license.
- SafeSign is proprietary. It is downloaded during the build, under its own license terms, and is not part of this project.
