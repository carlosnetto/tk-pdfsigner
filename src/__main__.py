"""PDF Signer launcher.

On Linux the signer runs in Docker, so the host stays clean of card drivers. On macOS
Docker cannot reach the USB card reader, so the signer runs on the host, in a private
venv under ~/Library/Caches/pdf-signer.
"""
import argparse
import hashlib
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
import zipfile
from pathlib import Path

IMAGE_PREFIX = "pdf-signer"
IMAGE_DIR = "image/"
DEFAULT_PORT = 8765
DEFAULT_TIMEZONE = "America/Sao_Paulo"
READY_TIMEOUT_SECONDS = 60
MAC = sys.platform == "darwin"
CACHE_ROOT = Path.home() / "Library" / "Caches" / "pdf-signer"
MIN_PYTHON = (3, 12)


def image_files():
    """Yield (relative path, content) for the Docker build context."""
    location = Path(__file__).resolve().parent
    if location.is_dir():
        for path in sorted((location / IMAGE_DIR).rglob("*")):
            if path.is_file():
                yield path.relative_to(location / IMAGE_DIR).as_posix(), path.read_bytes()
        return
    with zipfile.ZipFile(location) as archive:
        for name in sorted(archive.namelist()):
            if name.startswith(IMAGE_DIR) and not name.endswith("/"):
                yield name[len(IMAGE_DIR):], archive.read(name)


def files_digest(files, *extra):
    digest = hashlib.sha256()
    for name, content in files:
        digest.update(name.encode())
        digest.update(content)
    for item in extra:
        digest.update(item.encode())
    return digest.hexdigest()[:12]


def ensure_image():
    files = list(image_files())
    tag = f"{IMAGE_PREFIX}:{files_digest(files)}"
    found = subprocess.run(
        ["docker", "image", "inspect", tag], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    if found.returncode == 0:
        return tag
    print("Building the signer image. This takes a few minutes the first time.")
    with tempfile.TemporaryDirectory() as context:
        for name, content in files:
            target = Path(context) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        subprocess.run(["docker", "build", "-q", "-t", tag, context], check=True)
    return tag


def timezone():
    if os.environ.get("TZ"):
        return os.environ["TZ"]
    try:
        return Path("/etc/timezone").read_text().strip() or DEFAULT_TIMEZONE
    except OSError:
        return DEFAULT_TIMEZONE


def watermark_override():
    """A watermark.png beside the .pyz replaces the bundled one, with no rebuild."""
    candidate = Path(__file__).resolve().parent.parent / "watermark.png"
    return candidate if candidate.is_file() else None


def docker_command(image, work_dir, extra=()):
    tty = ["-it"] if sys.stdin.isatty() else ["-i"]
    watermark = watermark_override()
    return [
        "docker", "run", "--rm", *tty,
        "--device-cgroup-rule=c 189:* rmw",
        "-v", "/dev/bus/usb:/dev/bus/usb",
        "-v", "/run/udev:/run/udev:ro",
        "-v", f"{work_dir}:/work",
        "-w", "/work",
        "-e", f"TZ={timezone()}",
        # The port is published on 127.0.0.1 by -p, so listening on all
        # interfaces inside the container does not expose it to the network.
        "-e", "PDF_SIGNER_HOST=0.0.0.0",
        *(["-v", f"{watermark}:/app/watermark.png:ro"] if watermark else []),
        *extra,
        image, "/opt/venv/bin/python", "/app/sign.py",
    ]


def ensure_venv():
    """Create, or reuse, the venv and unpacked signer for this .pyz and this Python.

    The folder name hashes the signer files and the interpreter, so a new .pyz or a
    Python upgrade (Homebrew removes the old one) gets a fresh setup. Older folders
    are deleted once the new one is ready.
    """
    if sys.version_info < MIN_PYTHON:
        sys.exit(
            f"Python {'.'.join(map(str, MIN_PYTHON))} or later is required, this is "
            f"{sys.version.split()[0]} ({sys.executable}).\n"
            "Install it with: brew install python, then run: python3 pdf-signer.pyz ..."
        )
    files = list(image_files())
    folder = CACHE_ROOT / files_digest(files, sys.version, str(Path(sys.executable).resolve()))
    ready = folder / "ready"
    if ready.exists():
        return folder
    # A venv cannot be moved after creation, so it is built in place. The "ready"
    # marker is written last, so an interrupted setup is detected and redone.
    print("Setting up the signer. This takes a minute or two the first time.", flush=True)
    shutil.rmtree(folder, ignore_errors=True)
    for name, content in files:
        target = folder / "app" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    venv = folder / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    subprocess.run(
        [str(venv / "bin" / "python"), "-m", "pip", "install", "--quiet",
         "--disable-pip-version-check", "-r", str(folder / "app" / "requirements.txt")],
        check=True,
    )
    ready.touch()
    for old in CACHE_ROOT.iterdir():
        if old != folder:
            shutil.rmtree(old, ignore_errors=True)
    return folder


def signer_command(work_dir):
    """The command that runs sign.py: in Docker on Linux, on the host on macOS."""
    if not MAC:
        return docker_command(ensure_image(), work_dir)
    folder = ensure_venv()
    os.environ["PDF_SIGNER_HOST"] = "127.0.0.1"
    watermark = watermark_override()
    if watermark:
        os.environ["PDF_SIGNER_WATERMARK"] = str(watermark)
    return [str(folder / "venv" / "bin" / "python"), str(folder / "app" / "sign.py")]


def run_on_host(command, cwd):
    """Ctrl+C reaches the signer directly, so only the launcher's traceback is hidden."""
    try:
        return subprocess.run(command, cwd=cwd).returncode
    except KeyboardInterrupt:
        return 130


def port_is_free(port):
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def ask_port():
    while True:
        answer = input(f"TCP port on this computer [{DEFAULT_PORT}]: ").strip()
        port = int(answer) if answer.isdigit() else DEFAULT_PORT if not answer else 0
        if not 1024 <= port <= 65535:
            print("Enter a port between 1024 and 65535.")
        elif not port_is_free(port):
            print(f"Port {port} is already in use.")
        else:
            return port


def open_when_ready(url):
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}health", timeout=2):
                break
        except OSError:
            time.sleep(0.5)
    else:
        return
    if not webbrowser.open(url):
        print(f"Could not open a browser here. Open this URL instead: {url}")


def first_free_port():
    port = DEFAULT_PORT
    while not port_is_free(port):
        port += 1
    return port


def show_url(url):
    print()
    print(url)
    print()


def show_forwarded_url(token, port):
    print()
    print("From another computer, forward the port first, then open the URL there:")
    print(f"  ssh -L {port}:localhost:{port} {os.environ.get('USER', 'user')}@{socket.gethostname()}")
    show_url(f"http://localhost:{port}/{token}/")


def choose_port(token):
    """Both choices end with the URL on screen and the server listening."""
    while True:
        answer = input("[o] open the browser here, [p] pick a port and open the URL yourself: ")
        answer = answer.strip().lower()
        if answer in ("o", "open"):
            port = first_free_port()
            url = f"http://localhost:{port}/{token}/"
            show_url(url)
            threading.Thread(target=open_when_ready, args=(url,), daemon=True).start()
            return port
        if answer in ("p", "port"):
            port = ask_port()
            show_forwarded_url(token, port)
            return port


def pick(args):
    source = Path(args.pdf).resolve()
    if not source.is_file():
        sys.exit(f"File not found: {source}")
    output = Path(args.output).resolve() if args.output else source.with_name(f"{source.stem}-signed.pdf")
    if output.parent != source.parent:
        sys.exit("The output must be in the same folder as the PDF.")
    if output.exists():
        sys.exit(f"Output already exists: {output}")

    token = secrets.token_urlsafe(32)
    if args.port:
        if not port_is_free(args.port):
            sys.exit(f"Port {args.port} is already in use.")
        port = args.port
        show_forwarded_url(token, port)
    else:
        port = choose_port(token)

    os.environ["PDF_SIGNER_TOKEN"] = token
    os.environ["PDF_SIGNER_PORT"] = str(port)
    flags = ["pick", source.name, output.name]
    if args.allow_low_pin_count:
        flags.append("--allow-low-pin-count")
    if args.no_validation_data:
        flags.append("--no-validation-data")

    if MAC:
        return run_on_host([*signer_command(source.parent), *flags], source.parent)
    extra = [
        "-p", f"127.0.0.1:{port}:{port}",
        "-e", "PDF_SIGNER_TOKEN",
        "-e", "PDF_SIGNER_PORT",
    ]
    command = docker_command(ensure_image(), source.parent, extra)
    return run_named([*command, *flags], f"pdf-signer-{secrets.token_hex(4)}")


def run_named(command, name):
    """Run a container and remove it even if this launcher dies, such as when ssh drops."""
    def stop(signum, frame):
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    named = [*command[:2], "--name", name, *command[2:]]
    try:
        return subprocess.run(named).returncode
    finally:
        subprocess.run(
            ["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )


def docker_lines(*args):
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=True
    ).stdout.split("\n")[:-1]


def uninstall():
    """Remove what this tool created. The next run sets it up again."""
    return uninstall_venv() if MAC else uninstall_docker()


def uninstall_venv():
    if not CACHE_ROOT.is_dir():
        print("Nothing to remove.")
        return 0
    print(f"Folder to remove: {CACHE_ROOT}")
    if input("Remove it? [y/N]: ").strip().lower() != "y":
        print("Nothing was removed.")
        return 0
    shutil.rmtree(CACHE_ROOT)
    print("Removed. SafeSign was installed separately, remove it with its own uninstaller.")
    return 0


def uninstall_docker():
    containers = docker_lines("ps", "-aq", "--filter", f"name=^{IMAGE_PREFIX}-")
    images = docker_lines(
        "image", "ls", "--format", "{{.Repository}}:{{.Tag}} ({{.Size}})",
        "--filter", f"reference={IMAGE_PREFIX}*",
    )
    if not containers and not images:
        print("Nothing to remove.")
        return 0
    print(f"Containers to remove: {len(containers)}")
    for image in images:
        print(f"Image to remove: {image}")
    if input("Remove them? [y/N]: ").strip().lower() != "y":
        print("Nothing was removed.")
        return 0
    if containers:
        subprocess.run(["docker", "rm", "-f", *containers], stdout=subprocess.DEVNULL, check=True)
    if images:
        tags = [image.split(" ")[0] for image in images]
        subprocess.run(["docker", "image", "rm", *tags], stdout=subprocess.DEVNULL, check=True)
    print("Removed. Docker's shared build cache can still hold some of this space.")
    print("To clear it, run: docker builder prune")
    print("That affects every Docker project on this computer.")
    return 0


def main():
    if not MAC and shutil.which("docker") is None:
        sys.exit("Docker is required, and it was not found.")
    if sys.argv[1:] == ["uninstall"]:
        return uninstall()
    if len(sys.argv) > 1 and sys.argv[1] in ("sign", "list"):
        if MAC:
            return run_on_host([*signer_command(Path.cwd()), *sys.argv[1:]], Path.cwd())
        return subprocess.run([*signer_command(Path.cwd()), *sys.argv[1:]]).returncode

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf")
    parser.add_argument("output", nargs="?")
    parser.add_argument("--port", type=int)
    parser.add_argument("--allow-low-pin-count", action="store_true")
    parser.add_argument("--no-validation-data", action="store_true")
    return pick(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
