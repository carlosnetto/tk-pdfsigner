"""PDF Signer launcher. Runs the signer in Docker, so the host stays clean of card drivers."""
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
                yield name[len(IMAGE_DIR) :], archive.read(name)


def ensure_image():
    files = list(image_files())
    digest = hashlib.sha256()
    for name, content in files:
        digest.update(name.encode())
        digest.update(content)
    tag = f"{IMAGE_PREFIX}:{digest.hexdigest()[:12]}"
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
    replacement = Path(__file__).resolve().parent.parent / "watermark.png"
    return ["-v", f"{replacement}:/app/watermark.png:ro"] if replacement.is_file() else []


def docker_command(image, work_dir, extra=()):
    tty = ["-it"] if sys.stdin.isatty() else ["-i"]
    return [
        "docker", "run", "--rm", *tty,
        "--device-cgroup-rule=c 189:* rmw",
        "-v", "/dev/bus/usb:/dev/bus/usb",
        "-v", "/run/udev:/run/udev:ro",
        "-v", f"{work_dir}:/work",
        "-w", "/work",
        "-e", f"TZ={timezone()}",
        *watermark_override(),
        *extra,
        image, "/opt/venv/bin/python", "/app/sign.py",
    ]


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

    image = ensure_image()
    token = secrets.token_urlsafe(32)
    if args.port:
        if not port_is_free(args.port):
            sys.exit(f"Port {args.port} is already in use.")
        port = args.port
        show_forwarded_url(token, port)
    else:
        port = choose_port(token)

    os.environ["PDF_SIGNER_TOKEN"] = token
    extra = [
        "-p", f"127.0.0.1:{port}:{port}",
        "-e", "PDF_SIGNER_TOKEN",
        "-e", f"PDF_SIGNER_PORT={port}",
    ]
    command = docker_command(image, source.parent, extra)
    command += ["pick", source.name, output.name]
    if args.allow_low_pin_count:
        command.append("--allow-low-pin-count")
    if args.no_validation_data:
        command.append("--no-validation-data")
    return run_named(command, f"pdf-signer-{secrets.token_hex(4)}")


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
    """Remove this tool's containers and images. The next run builds them again."""
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
    if shutil.which("docker") is None:
        sys.exit("Docker is required, and it was not found.")
    if sys.argv[1:] == ["uninstall"]:
        return uninstall()
    if len(sys.argv) > 1 and sys.argv[1] in ("sign", "list"):
        command = docker_command(ensure_image(), Path.cwd())
        return subprocess.run([*command, *sys.argv[1:]]).returncode

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf")
    parser.add_argument("output", nargs="?")
    parser.add_argument("--port", type=int)
    parser.add_argument("--allow-low-pin-count", action="store_true")
    parser.add_argument("--no-validation-data", action="store_true")
    return pick(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
