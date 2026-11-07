# One target per stage. `make reproduce` rebuilds the dataset end to end and finishes with a
# release dry run. It needs Docker, GITHUB_TOKEN and the GEMINI_* settings in .env. The human
# labels it uses are committed under data/labelling/.
#
# `make FAST=1 <target>` (or `make FAST=1 reproduce`) is a smoke-test mode that never touches
# the committed data, never calls Gemini and never rebuilds images:
#   mine      one repo from the API cache, written to $(FAST_DIR)
#   build     check images, clones and base commits only
#   validate  one task in Docker, results in $(FAST_DIR)
#   label     summarise the saved classifier runs; no API requests

UV ?= uv
RUN := $(UV) run repogym
VERSION ?= v0.1
WORKERS ?= 2
FAST ?= 0
FAST_DIR ?= build/fast
FAST_REPO ?= attrs

ifeq ($(FAST),1)
MINE_ARGS := --repo $(FAST_REPO) --out $(FAST_DIR)/tasks_candidates.jsonl
BUILD_ARGS := --check
VALIDATE_ARGS := --repo $(FAST_REPO) --limit 1 --workers 1 --scratch $(FAST_DIR)/validate
LABEL_ARGS := --report-only
else
MINE_ARGS :=
BUILD_ARGS :=
VALIDATE_ARGS := --workers $(WORKERS)
LABEL_ARGS :=
endif

.PHONY: install test lint mine build validate stats label calibrate release-dry release reproduce

install:
	$(UV) sync --locked

test:
	$(UV) run pytest -q

lint:
	$(UV) run ruff check src tests
	$(UV) run ruff format --check src tests

mine:            # GitHub API -> data/tasks_candidates.jsonl (~35 min uncached)
	$(RUN) mine $(MINE_ARGS)

build:           # bare clones + one base image per repo (~4 min)
	$(RUN) build $(BUILD_ARGS)

validate:        # three validity checks in Docker (~75 min for 271 candidates, 2 workers)
	$(RUN) validate $(VALIDATE_ARGS)

stats:
	$(RUN) stats

label:           # three-run Gemini consensus for tasks without a GitHub type label; resumable
	$(RUN) classify-consensus $(LABEL_ARGS)

calibrate:       # classifier and spec check vs the committed human labels
	$(RUN) calibrate

release-dry:     # build and validate the release in a temp dir; writes nothing
	$(RUN) release $(VERSION) --dry-run

release:         # freeze data/release/$(VERSION)/ (refuses to overwrite or to run with uncommitted inputs)
	$(RUN) release $(VERSION)

reproduce: install lint test mine build validate stats label calibrate release-dry
