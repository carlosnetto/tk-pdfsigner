#!/usr/bin/env python3
"""Sign PDFs with a PKCS#11 smart card. Makes one PIN attempt per run, never more."""
import argparse
import asyncio
import ctypes
import getpass
import http.server
import io
import json
import logging
import math
import os
import sys
from dataclasses import dataclass
import time
from datetime import datetime, timezone
from pathlib import Path

import pkcs11
from asn1crypto import x509
from pkcs11 import Attribute, ObjectClass, TokenFlag
from fontTools.ttLib import TTFont
from PIL import Image, UnidentifiedImageError
from pyhanko.keys import load_cert_from_pemder
from pyhanko.pdf_utils import generic, layout
from pyhanko.pdf_utils.font.opentype import GlyphAccumulatorFactory
from pyhanko.pdf_utils.images import PdfImage
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.pdf_utils.text import TextBoxStyle
from pyhanko.sign import fields, signers
from pyhanko.sign.pkcs11 import PKCS11Signer
from pyhanko.stamp import TextStamp, TextStampStyle
from pyhanko_certvalidator import CertificateValidator, ValidationContext
from pyhanko_certvalidator.errors import RevokedError
from pyhanko_certvalidator.fetchers.requests_fetchers import RequestsFetcherBackend

DEFAULT_MODULE = os.environ.get("PDF_SIGNER_MODULE") or (
    "/Applications/tokenadmin.app/Contents/Frameworks/libaetpkss.dylib"
    if sys.platform == "darwin"
    else "/usr/lib/libaetpkss.so"
)
# A CFF font with 1000 units per em. pyHanko 0.38 writes glyph widths in font units,
# so TrueType fonts (usually 2048 per em) come out with letters spread apart.
FONT = str(Path(__file__).parent / "fonts" / "texgyreheros-regular.otf")
TIME_FORMAT = "%d/%m/%Y %H:%M:%S %:z"
PAGE_MARGIN = 24
WEB_DIR = Path(__file__).parent / "web"
ROOTS_DIR = Path(__file__).parent / "roots"
WATERMARK = Path(os.environ.get("PDF_SIGNER_WATERMARK") or Path(__file__).parent / "watermark.png")
MAX_STAMPS = 200
DOWNLOAD_WINDOW_SECONDS = 300
ANCHORS = ("bottom-left", "bottom-center", "bottom-right")
DEFAULT_PLACE = ["1", "bottom-left", "large"]
LARGE_TEXT = (
    "Assinado digitalmente por\n"
    "%(name)s\n"
    "CPF: %(cpf)s\n"
    "Data: %(ts)s\n"
    "Certificado: %(issuer)s\n"
    "S\u00e9rie: %(serial)s\n"
    "Verifique em validar.iti.gov.br"
)
SMALL_TEXT = (
    "Assinado digitalmente por\n"
    "%(name)s\n"
    "CPF: %(cpf)s\n"
    "Data: %(ts)s"
)
INITIALS_TEXT = (
    "Rubrica digital\n"
    "%(name_top)s\n"
    "%(name_bottom)s\n"
    "CPF %(cpf)s\n"
    "%(date)s"
)


@dataclass(frozen=True)
class StampSize:
    text: str
    font_size: int
    margin: int
    ink_height: int
    top_shift: int
    bottom_shift: int
    square: bool = False


SIZES = {
    "large": StampSize(LARGE_TEXT, 7, 9, 8, -1, 2),
    "small": StampSize(SMALL_TEXT, 5, 5, 6, -1, 1),
    "initials": StampSize(INITIALS_TEXT, 2, 2, 3, 0, 0, square=True),
}
UNSAFE_PIN_FLAGS = (TokenFlag.USER_PIN_LOCKED, TokenFlag.USER_PIN_FINAL_TRY)


# The font has no CID metadata, which pyHanko handles correctly but warns about.
logging.getLogger("pyhanko.pdf_utils.font.opentype").setLevel(logging.ERROR)

# Connections opened by power_cards_t0(). They stay open until the process exits.
_PCSC_HANDLES = []


def power_cards_t0():
    """macOS only: open a shared T=0 connection to every reader, and keep it open.

    SafeSign connects asking for T=0 or T=1. With an idle, powered-down card (the
    Certisign YpsID S2 e-CPF, for one), the macOS CCID driver then fails to power it
    up and reports SCARD_W_UNRESPONSIVE_CARD, which SafeSign turns into "token not
    recognised". A T=0 connection powers the card up correctly. Holding it open keeps
    the card powered and on T=0 while SafeSign shares it. Failures are ignored, and
    SafeSign then reports the problem itself.
    """
    try:
        pcsc = ctypes.CDLL("/System/Library/Frameworks/PCSC.framework/PCSC")
        context = ctypes.c_int32()
        if pcsc.SCardEstablishContext(2, None, None, ctypes.byref(context)):  # SYSTEM scope
            return
        size = ctypes.c_uint32()
        if pcsc.SCardListReaders(context, None, None, ctypes.byref(size)):
            return
        names = ctypes.create_string_buffer(size.value)
        if pcsc.SCardListReaders(context, None, names, ctypes.byref(size)):
            return
        for reader in filter(None, names.raw.split(b"\0")):
            card, protocol = ctypes.c_int32(), ctypes.c_uint32()
            # 2 = SCARD_SHARE_SHARED, 1 = SCARD_PROTOCOL_T0
            if pcsc.SCardConnect(context, reader, 2, 1, ctypes.byref(card), ctypes.byref(protocol)) == 0:
                _PCSC_HANDLES.append(card)
        _PCSC_HANDLES.append(context)
    except OSError:
        pass


def find_token(module):
    if not Path(module).is_file():
        message = f"SafeSign library not found: {module}"
        if sys.platform == "darwin":
            message += (
                "\nInstall SafeSign for macOS from https://safesign.gdamericadosul.com.br/download"
                "\nIf it is installed elsewhere, set PDF_SIGNER_MODULE to its libaetpkss.dylib."
            )
        sys.exit(message)
    if sys.platform == "darwin":
        power_cards_t0()
    slots = pkcs11.lib(module).get_slots(token_present=True)
    if not slots:
        sys.exit("No card found.")
    for slot in slots:
        try:
            return slot.get_token()
        except pkcs11.exceptions.TokenNotRecognised:
            continue
    sys.exit("A card is in the reader, but SafeSign does not recognise it.")


def read_certs(session):
    certs = []
    for obj in session.get_objects({Attribute.CLASS: ObjectClass.CERTIFICATE}):
        certs.append((obj[Attribute.ID], x509.Certificate.load(obj[Attribute.VALUE])))
    return certs


def can_sign(cert):
    usage = cert.key_usage_value
    return (
        not cert.ca
        and usage is not None
        and bool({"non_repudiation", "digital_signature"} & usage.native)
    )


def choose_cert(certs):
    now = datetime.now(timezone.utc)
    valid = [
        (cert_id, cert)
        for cert_id, cert in certs
        if can_sign(cert) and cert.not_valid_before <= now <= cert.not_valid_after
    ]
    if not valid:
        sys.exit("The card has no valid signing certificate.")
    return max(valid, key=lambda item: item[1].not_valid_after)


def split_name(name):
    """Break a name into the two lines that come closest to the same length."""
    words = name.split()
    best = min(
        range(1, len(words)),
        key=lambda i: max(len(" ".join(words[:i])), len(" ".join(words[i:]))),
        default=len(words),
    )
    return " ".join(words[:best]), " ".join(words[best:])


def stamp_params(cert):
    name, _, cpf = cert.subject.native["common_name"].partition(":")
    name_top, name_bottom = split_name(name)
    if len(cpf) == 11:
        cpf = f"{cpf[:3]}.{cpf[3:6]}.{cpf[6:9]}-{cpf[9:]}"
    serial = f"{cert.serial_number:032X}"
    return {
        "name": name,
        "name_top": name_top,
        "name_bottom": name_bottom,
        "cpf": cpf,
        "issuer": cert.issuer.native["common_name"],
        "serial": " ".join(serial[i : i + 4] for i in range(0, len(serial), 4)),
    }


def stamp_extent(size, params):
    font = TTFont(FONT)
    cmap, metrics = font.getBestCmap(), font["hmtx"]
    lines = (size.text % params).split("\n")
    units = max(sum(metrics[cmap[ord(c)]][0] for c in line) for line in lines)
    width = math.ceil(units * size.font_size / font["head"].unitsPerEm) + 2 * size.margin
    height = (len(lines) - 1) * size.font_size + size.ink_height + 2 * size.margin
    if size.square:
        width = height = max(width, height)
    return width, height


def page_rect(page_obj):
    node = page_obj
    while True:
        for key in ("/CropBox", "/MediaBox"):
            if key in node:
                return [float(value) for value in node[key]]
        node = node["/Parent"]


def page_total(writer):
    return int(writer.root["/Pages"]["/Count"])


def resolve_place(writer, place, params):
    """Turn a --place value into (page index, stamp size, box)."""
    page_text, where, size_name = place
    if size_name not in SIZES:
        sys.exit(f"Unknown stamp size: {size_name}. Use: {', '.join(SIZES)}")
    page_count = page_total(writer)
    if not page_text.isdigit() or not 1 <= int(page_text) <= page_count:
        sys.exit(f"Page {page_text} does not exist, the PDF has {page_count} page(s).")
    page = int(page_text) - 1
    check_page(writer, page)
    left, bottom, right, top = page_rect(writer.find_page_for_modification(page)[0].get_object())
    size = SIZES[size_name]
    width, height = stamp_extent(size, params)
    if where == "bottom-left":
        x, y = left + PAGE_MARGIN, bottom + PAGE_MARGIN + height
    elif where == "bottom-center":
        x, y = (left + right - width) // 2, bottom + PAGE_MARGIN + height
    elif where == "bottom-right":
        x, y = right - PAGE_MARGIN - width, bottom + PAGE_MARGIN + height
    else:
        try:
            x, y = (float(value) for value in where.split(","))
        except ValueError:
            sys.exit(f"Unknown position: {where}. Use X,Y or one of: {', '.join(ANCHORS)}")
    box = (x, y - height, x + width, y)
    if box[0] < left or box[1] < bottom or box[2] > right or box[3] > top:
        sys.exit(f"The {size_name} stamp at {where} does not fit on page {page + 1}.")
    return page, size, box


def load_watermark(path):
    try:
        with Image.open(path) as image:
            return image.convert("RGBA")
    except (OSError, UnidentifiedImageError) as error:
        sys.exit(f"Could not read the watermark, so nothing was signed: {error}")


def stamp_style(size, watermark):
    return TextStampStyle(
        stamp_text=size.text,
        background=watermark,
        background_opacity=1,
        background_layout=layout.SimpleBoxLayoutRule(
            x_align=layout.AxisAlignment.ALIGN_MID,
            y_align=layout.AxisAlignment.ALIGN_MID,
            margins=layout.Margins.uniform(0),
            inner_content_scaling=layout.InnerScaling.STRETCH_TO_FIT,
        ),
        timestamp_format=TIME_FORMAT,
        text_box_style=TextBoxStyle(
            font=GlyphAccumulatorFactory(FONT, font_size=size.font_size),
            font_size=size.font_size,
            box_layout_rule=layout.SimpleBoxLayoutRule(
                x_align=layout.AxisAlignment.ALIGN_MIN,
                y_align=layout.AxisAlignment.ALIGN_MID,
                margins=layout.Margins(
                    left=size.margin,
                    right=size.margin,
                    top=size.margin + size.top_shift,
                    bottom=size.margin + size.bottom_shift,
                ),
            ),
        ),
        border_width=1,
        inner_content_layout=layout.SimpleBoxLayoutRule(
            x_align=layout.AxisAlignment.ALIGN_MIN,
            y_align=layout.AxisAlignment.ALIGN_MID,
            margins=layout.Margins.uniform(0),
            inner_content_scaling=layout.InnerScaling.NO_SCALING,
        ),
    )


def check_page(writer, page):
    page_obj = writer.find_page_for_modification(page)[0].get_object()
    if int(page_obj["/Rotate"] if "/Rotate" in page_obj else 0) % 360:
        sys.exit(f"Page {page + 1} is rotated, which the stamp does not support yet.")


def add_extra_stamp(writer, style, params, page, box):
    """Add a visible copy of the stamp, covered by the signature made afterwards."""
    page_ref = writer.find_page_for_modification(page)[0]
    page_obj = page_ref.get_object()
    x1, y1, x2, y2 = box
    stamp = TextStamp(
        writer, style, text_params=params, box=layout.BoxConstraints(x2 - x1, y2 - y1)
    )
    annot = generic.DictionaryObject(
        {
            generic.pdf_name("/Type"): generic.pdf_name("/Annot"),
            generic.pdf_name("/Subtype"): generic.pdf_name("/Stamp"),
            generic.pdf_name("/Rect"): generic.ArrayObject(map(generic.FloatObject, box)),
            generic.pdf_name("/F"): generic.NumberObject(132),
            generic.pdf_name("/P"): page_ref,
            generic.pdf_name("/AP"): generic.DictionaryObject(
                {generic.pdf_name("/N"): writer.add_object(stamp.as_form_xobject())}
            ),
        }
    )
    annot_ref = writer.add_object(annot)
    if "/Annots" in page_obj:
        annots = page_obj["/Annots"]
        annots.append(annot_ref)
        writer.update_container(annots)
    else:
        page_obj[generic.pdf_name("/Annots")] = generic.ArrayObject([annot_ref])
        writer.update_container(page_obj)


def cmd_list(args):
    token = find_token(args.module)
    print(f"Token: {token.label}")
    print(f"PIN flags: {token.flags}")
    with token.open() as session:
        certs = read_certs(session)
    selected = None
    try:
        selected = choose_cert(certs)[0]
    except SystemExit:
        pass
    for cert_id, cert in certs:
        mark = "*" if cert_id == selected else " "
        kind = "CA" if cert.ca else "signing" if can_sign(cert) else "other"
        print(
            f"{mark} {cert.not_valid_before:%Y-%m-%d} to {cert.not_valid_after:%Y-%m-%d} "
            f"{kind:8} {cert.subject.native.get('common_name')}"
        )


@dataclass
class Job:
    source: Path
    output: Path
    token: object
    cert_id: bytes
    cert: x509.Certificate
    params: dict
    allow_low_pin_count: bool
    reason: str | None = None
    location: str | None = None
    validation_context: ValidationContext | None = None
    ca_chain: tuple = ()
    watermark: Image.Image | None = None


def fetch_validation_data(cert, roots_dir):
    """Download the chain and revocation status, so signing stops before the PIN if they fail."""
    roots = [load_cert_from_pemder(path) for path in sorted(Path(roots_dir).glob("*.crt"))]
    context = ValidationContext(
        trust_roots=roots,
        allow_fetching=True,
        revocation_mode="hard-fail",
        fetcher_backend=RequestsFetcherBackend(),
    )
    validator = CertificateValidator(cert, validation_context=context)
    try:
        path = asyncio.run(validator.async_validate_usage({"non_repudiation"}))
    except RevokedError as error:
        sys.exit(f"This certificate is revoked, so nothing was signed: {error}")
    except Exception as error:
        sys.exit(
            f"Could not confirm the certificate is valid, so nothing was signed: {error}\n"
            "If you are offline, --no-validation-data signs without the chain and OCSP data."
        )
    print("Certificate chain verified, the issuer reports it as not revoked.")
    return context, tuple(item for item in path if item != cert)


def prepare(args):
    source = Path(args.input)
    output = Path(args.output or source.with_name(f"{source.stem}-signed.pdf"))
    if not source.is_file():
        sys.exit(f"File not found: {source}")
    if output.exists():
        sys.exit(f"Output already exists: {output}")

    token = find_token(args.module)
    unsafe = [flag.name for flag in UNSAFE_PIN_FLAGS if flag in token.flags]
    if unsafe:
        sys.exit(f"Refusing to ask for the PIN, the card reports: {', '.join(unsafe)}")
    if TokenFlag.USER_PIN_COUNT_LOW in token.flags and not args.allow_low_pin_count:
        sys.exit(
            "The card reports a recent wrong PIN. Check your PIN, then rerun with "
            "--allow-low-pin-count."
        )

    with token.open() as session:
        cert_id, cert = choose_cert(read_certs(session))
    print(f"Signing as {cert.subject.native.get('common_name')}")
    print(f"Certificate valid until {cert.not_valid_after:%Y-%m-%d}")
    params = stamp_params(cert)
    stamp_time(params)
    watermark = load_watermark(args.watermark)
    context, ca_chain = None, ()
    if not args.no_validation_data:
        context, ca_chain = fetch_validation_data(cert, args.roots)
    return Job(
        source,
        output,
        token,
        cert_id,
        cert,
        params,
        args.allow_low_pin_count,
        getattr(args, "reason", None),
        getattr(args, "location", None),
        context,
        ca_chain,
        watermark,
    )


def stamp_time(params):
    params["ts"] = datetime.now().astimezone().strftime(TIME_FORMAT)
    params["date"] = params["ts"][: len("01/11/2026 10:05")]


def resolve_places(job, places):
    with job.source.open("rb") as in_file:
        writer = IncrementalPdfFileWriter(in_file)
        return [resolve_place(writer, place, job.params) for place in places]


def next_field_name(source):
    with source.open("rb") as in_file:
        names = {name for name, _, _ in fields.enumerate_sig_fields(PdfFileReader(in_file))}
    number = 1
    while f"Signature{number}" in names:
        number += 1
    return f"Signature{number}"


def sign_job(job, places):
    pin = getpass.getpass("PIN: ")
    if not pin:
        sys.exit("No PIN entered, nothing was sent to the card.")

    try:
        session = job.token.open(user_pin=pin)
    except pkcs11.exceptions.PinIncorrect:
        sys.exit("Wrong PIN. Not retrying, each failure counts toward locking the card.")

    field_name = next_field_name(job.source)
    with session:
        signer = PKCS11Signer(
            session, cert_id=job.cert_id, key_id=job.cert_id, ca_chain=job.ca_chain
        )
        metadata = signers.PdfSignatureMetadata(
            field_name=field_name,
            md_algorithm="sha256",
            subfilter=fields.SigSeedSubFilter.PADES,
            reason=job.reason,
            location=job.location,
            embed_validation_info=job.validation_context is not None,
            validation_context=job.validation_context,
        )
        stamp_time(job.params)
        *extra_places, (sig_page, sig_size, sig_box) = places
        field = fields.SigFieldSpec(field_name, on_page=sig_page, box=sig_box)
        signed = io.BytesIO()
        with job.source.open("rb") as in_file:
            writer = IncrementalPdfFileWriter(in_file)
            watermark = PdfImage(job.watermark, writer=writer)
            for page, size, box in extra_places:
                add_extra_stamp(writer, stamp_style(size, watermark), job.params, page, box)
            signers.PdfSigner(
                metadata,
                signer=signer,
                stamp_style=stamp_style(sig_size, watermark),
                new_field_spec=field,
            ).sign_pdf(writer, output=signed, appearance_text_params=job.params)
    job.output.write_bytes(signed.getvalue())
    stat = job.source.stat()
    os.chown(job.output, stat.st_uid, stat.st_gid)
    print(f"Signed: {job.output}")


def cmd_sign(args):
    job = prepare(args)
    sign_job(job, resolve_places(job, args.place or [DEFAULT_PLACE]))


def document_info(job):
    with job.source.open("rb") as in_file:
        reader = PdfFileReader(in_file)
        writer = IncrementalPdfFileWriter(in_file)
        page_count = page_total(writer)
        rotated = []
        for page in range(page_count):
            page_obj = writer.find_page_for_modification(page)[0].get_object()
            if int(page_obj["/Rotate"] if "/Rotate" in page_obj else 0) % 360:
                rotated.append(page + 1)
        signature_count = len(reader.embedded_signatures)
    return {
        "file": job.source.name,
        "signer": job.params["name"],
        "pages": page_count,
        "rotated": rotated,
        "signatures": signature_count,
        "sizes": {name: stamp_extent(size, job.params) for name, size in SIZES.items()},
    }


def parse_stamps(body):
    """Turn the browser's JSON into --place values. Exits on anything malformed."""
    try:
        stamps = json.loads(body)["stamps"]
        places = [
            [str(int(stamp["page"])), f"{float(stamp['x'])},{float(stamp['y'])}", stamp["size"]]
            for stamp in stamps
        ]
    except (KeyError, TypeError, ValueError):
        sys.exit("Invalid stamp list.")
    if not 1 <= len(places) <= MAX_STAMPS:
        sys.exit(f"Place between 1 and {MAX_STAMPS} stamps.")
    return places


class PickState:
    def __init__(self, job, token):
        self.job = job
        self.prefix = f"/{token}/"
        self.info = document_info(job)
        self.signed = False
        self.finished = False
        self.deadline = None

    def stop_after_download_window(self):
        self.deadline = time.monotonic() + DOWNLOAD_WINDOW_SECONDS


class PickHandler(http.server.BaseHTTPRequestHandler):
    STATIC = {
        "": ("index.html", "text/html; charset=utf-8"),
        "pdf.min.mjs": ("pdf.min.mjs", "text/javascript"),
        "pdf.worker.min.mjs": ("pdf.worker.min.mjs", "text/javascript"),
    }

    def log_message(self, format, *args):
        pass

    def send_body(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, data):
        self.send_body(status, json.dumps(data).encode(), "application/json")

    def route(self):
        """Every URL carries the session token, anything else does not exist."""
        state = self.server.state
        if not self.path.startswith(state.prefix):
            self.send_body(404, b"Not found", "text/plain")
            return None
        return self.path[len(state.prefix) :]

    def do_GET(self):
        route = self.route()
        if route is None:
            return
        state = self.server.state
        if route in self.STATIC:
            name, content_type = self.STATIC[route]
            self.send_body(200, (WEB_DIR / name).read_bytes(), content_type)
        elif route == "health":
            self.send_body(200, b"ok", "text/plain")
        elif route == "info":
            self.send_json(200, state.info)
        elif route == "document.pdf":
            self.send_body(200, state.job.source.read_bytes(), "application/pdf")
        elif route == "signed.pdf" and state.signed:
            self.send_body(200, state.job.output.read_bytes(), "application/pdf")
            state.finished = True
        else:
            self.send_body(404, b"Not found", "text/plain")

    def do_POST(self):
        if self.route() != "sign":
            return
        state = self.server.state
        if state.signed:
            self.send_json(409, {"error": "This document was already signed in this session."})
            return
        length = int(self.headers.get("Content-Length", 0))
        if length > 65536:
            self.send_json(413, {"error": "Too many stamps."})
            return
        try:
            places = parse_stamps(self.rfile.read(length))
            if state.info["signatures"] and len(places) > 1:
                sys.exit("This PDF already has signatures, so only one stamp is allowed for now.")
            resolved = resolve_places(state.job, places)
        except SystemExit as error:
            self.send_json(400, {"error": str(error)})
            return
        print(f"Received {len(resolved)} stamp(s) from the browser.")
        try:
            sign_job(state.job, resolved)
        except SystemExit as error:
            print(error)
            self.send_json(500, {"error": str(error)})
            state.finished = True
            return
        state.signed = True
        state.stop_after_download_window()
        self.send_json(200, {"download": "signed.pdf", "name": state.job.output.name})


def cmd_pick(args):
    token = os.environ["PDF_SIGNER_TOKEN"]
    port = int(os.environ["PDF_SIGNER_PORT"])
    job = prepare(args)
    state = PickState(job, token)
    host = os.environ.get("PDF_SIGNER_HOST", "127.0.0.1")
    server = http.server.HTTPServer((host, port), PickHandler)
    server.state = state
    server.timeout = 1
    print("Waiting for the browser. Press Ctrl+C to cancel.", flush=True)
    while not state.finished:
        server.handle_request()
        if state.deadline is not None and time.monotonic() > state.deadline:
            break
    server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", default=DEFAULT_MODULE)
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("list", help="show the card's certificates, no PIN needed").set_defaults(
        run=cmd_list
    )
    sign = commands.add_parser("sign", help="sign a PDF")
    sign.add_argument("input")
    sign.add_argument("output", nargs="?")
    sign.add_argument("--reason")
    sign.add_argument("--location")
    sign.add_argument("--allow-low-pin-count", action="store_true")
    sign.add_argument(
        "--place",
        nargs=3,
        action="append",
        metavar=("PAGE", "WHERE", "SIZE"),
        help=f"WHERE is X,Y (top left corner) or one of {', '.join(ANCHORS)}. SIZE is "
        f"{' or '.join(SIZES)}. Repeat for more stamps, the last one holds the signature.",
    )
    sign.set_defaults(run=cmd_sign)
    pick = commands.add_parser("pick", help="choose the stamps in a browser, then sign")
    pick.add_argument("input")
    pick.add_argument("output", nargs="?")
    pick.add_argument("--allow-low-pin-count", action="store_true")
    pick.set_defaults(run=cmd_pick)
    for command in (sign, pick):
        command.add_argument("--no-validation-data", action="store_true")
        command.add_argument("--roots", default=ROOTS_DIR, help=argparse.SUPPRESS)
        command.add_argument("--watermark", default=WATERMARK, help=argparse.SUPPRESS)

    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
