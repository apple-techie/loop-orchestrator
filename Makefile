# loop-orchestrator — install/uninstall symlinks for the four shell scripts.
#
# Default target dir is ~/.local/bin (no sudo required and already on PATH for
# most user setups). Override with BIN=/usr/local/bin or BIN=/opt/local/bin.
#
#   make install            # symlink scripts into $(BIN)
#   make install BIN=...    # custom target dir
#   make uninstall          # remove the symlinks
#   make check              # bash -n syntax-check all scripts + lib helpers
#   make print-paths        # show what `make install` would do

BIN ?= $(HOME)/.local/bin
SCRIPTS := loop-tmux loop-dispatch loop-digest loop-lane-status loop-adr
LIBS := lib/harness-registry.sh lib/lane-config-resolver.sh lib/lane-health.sh
REPO_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))

.PHONY: install uninstall check print-paths help

help:
	@echo "Targets:"
	@echo "  install        Symlink scripts into BIN (default: $(BIN))"
	@echo "  uninstall      Remove the symlinks from BIN"
	@echo "  check          bash -n syntax-check all scripts + lib helpers"
	@echo "  print-paths    Print resolved install paths and exit"
	@echo ""
	@echo "Override BIN: make install BIN=/usr/local/bin"

install:
	@mkdir -p "$(BIN)"
	@for s in $(SCRIPTS); do \
		src="$(REPO_DIR)/$$s.sh"; \
		dst="$(BIN)/$$s"; \
		if [ ! -x "$$src" ]; then \
			echo "skip: $$src is missing or not executable" >&2; \
			continue; \
		fi; \
		ln -sf "$$src" "$$dst"; \
		echo "linked $$dst -> $$src"; \
	done
	@echo ""
	@echo "Done. Ensure $(BIN) is on your PATH."

uninstall:
	@for s in $(SCRIPTS); do \
		dst="$(BIN)/$$s"; \
		if [ -L "$$dst" ]; then \
			rm "$$dst"; \
			echo "removed $$dst"; \
		elif [ -e "$$dst" ]; then \
			echo "skip: $$dst exists and is not a symlink (refusing to delete)" >&2; \
		fi; \
	done

check:
	@for s in $(SCRIPTS); do \
		src="$(REPO_DIR)/$$s.sh"; \
		bash -n "$$src" && echo "ok  $$src" || exit 1; \
	done
	@for l in $(LIBS); do \
		src="$(REPO_DIR)/$$l"; \
		bash -n "$$src" && echo "ok  $$src" || exit 1; \
	done

print-paths:
	@echo "REPO_DIR = $(REPO_DIR)"
	@echo "BIN      = $(BIN)"
	@for s in $(SCRIPTS); do \
		echo "  $(BIN)/$$s -> $(REPO_DIR)/$$s.sh"; \
	done
