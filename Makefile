SOURCES := $(shell find src -type f -not -path src/image/watermark.png -not -path src/LICENSE)

pdf-signer.pyz: build-pyz.py watermark.png LICENSE $(SOURCES)
	python3 build-pyz.py

.PHONY: clean
clean:
	rm -f pdf-signer.pyz
