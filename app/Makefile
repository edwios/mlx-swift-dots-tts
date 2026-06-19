SHELL := /bin/bash
SCHEME := dots-tts-app
CONFIG ?= debug
DERIVED_DATA := .build/DerivedData
PRODUCTS := $(DERIVED_DATA)/Build/Products/$(CONFIG)
PRODUCT := dots-tts
BUNDLE := mlx-swift_Cmlx.bundle
INSTALL_DIR := .build

# mlx-swift Metal kernels are compiled only by Xcode's build system, not swift build.
# xcodebuild emits the binary plus mlx-swift_Cmlx.bundle (contains default.metallib).
.PHONY: build compile clean help

build:
	@xcodebuild \
		-scheme $(SCHEME) \
		-configuration $(CONFIG) \
		-destination 'platform=macOS' \
		-derivedDataPath $(DERIVED_DATA) \
		build
	@install -d $(INSTALL_DIR)
	@cp "$(PRODUCTS)/$(PRODUCT)" "$(INSTALL_DIR)/$(PRODUCT)"
	@rm -rf "$(INSTALL_DIR)/$(BUNDLE)"
	@cp -R "$(PRODUCTS)/$(BUNDLE)" "$(INSTALL_DIR)/$(BUNDLE)"
	@echo "built -> $(INSTALL_DIR)/$(PRODUCT)"

# Fast syntax check only; the resulting binary cannot run MLX (no metallib).
compile:
	@swift build -c $(CONFIG)

clean:
	@swift package clean
	@rm -rf $(DERIVED_DATA)
	@rm -rf "$(INSTALL_DIR)/$(PRODUCT)" "$(INSTALL_DIR)/$(BUNDLE)"

help:
	@echo "targets: build (CONFIG=debug|release), compile, clean"
	@echo "run: $(INSTALL_DIR)/$(PRODUCT) (keep $(BUNDLE) alongside the binary)"
