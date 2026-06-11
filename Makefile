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
# Compiled-coordinator helpers; installed on PATH so the Python engine can
# resolve them from ANY project root (its lookup is PATH before
# repo-relative). They all take --project-root, so the symlink location
# never decides which project they operate on.
HELPERS := loop-wiki-pending loop-checkpoint loop-task-lint loop-jira-sync loop-metrics loop-wiki-lint
LIBS := lib/harness-registry.sh lib/lane-config-resolver.sh lib/lane-health.sh
REPO_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))

.PHONY: install uninstall check print-paths help install-python check-python check-all install-skill

help:
	@echo "Targets:"
	@echo "  install        Symlink scripts into BIN (default: $(BIN))"
	@echo "  uninstall      Remove the symlinks from BIN"
	@echo "  check          bash -n syntax-check all scripts + lib helpers"
	@echo "  install-python Install the optional Python layer (loop-engine/loop-deck/loop-pm)"
	@echo "  check-python   ruff + pytest for the Python layer (needs uv)"
	@echo "  check-all      check + check-python"
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
	@for h in $(HELPERS); do \
		src="$(REPO_DIR)/scripts/$$h.sh"; \
		dst="$(BIN)/$$h"; \
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
	@for s in $(SCRIPTS) $(HELPERS); do \
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
	@for h in $(REPO_DIR)/scripts/*.sh; do \
		[ -e "$$h" ] || continue; \
		bash -n "$$h" && echo "ok  $$h" || exit 1; \
	done

# ── optional Python layer ────────────────────────────────────────────────
# The bash substrate above must keep working with NONE of this installed;
# the plain `check` target runs on a Python-extras-free environment in CI
# to enforce exactly that.

install-python:
	@if command -v uv >/dev/null 2>&1; then \
		uv tool install --force --from "$(REPO_DIR)" loop-orchestrator; \
	else \
		echo "uv not found; falling back to pip --user (needs Python >= 3.10)"; \
		python3 -m pip install --user "$(REPO_DIR)"; \
	fi

# Symlink the operator skill where SKILL.md-capable harnesses find it.
# Default: Claude Code's user-level skills dir. For other harnesses or
# project-level installs, override SKILLS_DIR (see harness-registry.sh
# skill_dir per harness, e.g. SKILLS_DIR=~/myproj/.pi/skills).
SKILLS_DIR ?= $(HOME)/.claude/skills

install-skill:
	@mkdir -p "$(SKILLS_DIR)"
	@ln -sfn "$(REPO_DIR)/skills/loop-orchestrator" "$(SKILLS_DIR)/loop-orchestrator"
	@echo "linked $(SKILLS_DIR)/loop-orchestrator -> $(REPO_DIR)/skills/loop-orchestrator"

check-python:
	@command -v uv >/dev/null 2>&1 || { echo "check-python requires uv" >&2; exit 1; }
	@# macOS + iCloud-synced repos: UF_HIDDEN gets set on .venv dotfiles and
	@# CPython >= 3.13 skips hidden .pth files, breaking the editable install.
	@# Best-effort unhide right before use; no-op on Linux.
	@command -v chflags >/dev/null 2>&1 && chflags nohidden "$(REPO_DIR)"/.venv/lib/python*/site-packages/*.pth 2>/dev/null || true
	@cd "$(REPO_DIR)" && uv run --no-sync --group dev ruff check src tests
	@cd "$(REPO_DIR)" && uv run --no-sync --group dev ruff format --check src tests
	@cd "$(REPO_DIR)" && uv run --no-sync --group dev pytest -q

check-all: check check-python

print-paths:
	@echo "REPO_DIR = $(REPO_DIR)"
	@echo "BIN      = $(BIN)"
	@for s in $(SCRIPTS); do \
		echo "  $(BIN)/$$s -> $(REPO_DIR)/$$s.sh"; \
	done
