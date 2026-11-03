FROM ubuntu:24.04
ADD --checksum=sha256:823e8e009929ce2f2da446ef50b60d674203c887a8f77ad59a65918077d3626d \
    "https://safesign.gdamericadosul.com.br/content/SafeSign%20IC%20Standard%20Linux%20ub2404%204.7.0.0-AET.000.zip" \
    /tmp/safesign.zip
RUN apt-get update && apt-get install -y --no-install-recommends unzip \
    && unzip -j /tmp/safesign.zip '*.deb' -d /tmp/safesign \
    && mv /tmp/safesign/*.deb /tmp/safesign.deb \
    && apt-get install -y --no-install-recommends \
       pcscd libccid opensc pcsc-tools python3-venv tzdata /tmp/safesign.deb \
    && rm -rf /var/lib/apt/lists/* /tmp/safesign /tmp/safesign.deb /tmp/safesign.zip
COPY requirements.txt /app/requirements.txt
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r /app/requirements.txt
COPY entrypoint.sh /entrypoint.sh
COPY sign.py /app/sign.py
COPY web /app/web
COPY roots /app/roots
COPY fonts /app/fonts
COPY watermark.png /app/watermark.png
ENTRYPOINT ["sh", "/entrypoint.sh"]
