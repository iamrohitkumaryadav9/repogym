# One target per stage. `make reproduce` rebuilds the dataset end to end and finishes with a
# release dry run. It needs Docker, GITHUB_TOKEN and the GEMINI_* settings in .env. The human
# labels it uses are committed under data/labelling/.

UV ?= uv
RUN := $(UV) run repogym
VERSION ?= v0.1
WORKERS ?= 2

.PHONY: install test lint mine build validate stats label calibrate release-dry release reproduce

install:
	$(UV) sync --locked

test:
	$(UV) run pytest -q

lint:
	$(UV) run ruff check src tests
	$(UV) run ruff format --check src tests

mine:            # GitHub API -> data/tasks_candidates.jsonl (~35 min uncached)
	$(RUN) mine

build:           # bare clones + one base image per repo (~4 min)
	$(RUN) build

validate:        # three validity checks in Docker (~75 min for 271 candidates, 2 workers)
	$(RUN) validate --workers $(WORKERS)

stats:
	$(RUN) stats

label:           # three-run Gemini consensus for tasks without a GitHub type label; resumable
	$(RUN) classify-consensus

calibrate:       # classifier and spec check vs the committed human labels
	$(RUN) calibrate

release-dry:     # build and validate the release in a temp dir; writes nothing
	$(RUN) release $(VERSION) --dry-run

release:         # freeze data/release/$(VERSION)/ (refuses to overwrite or to run with uncommitted inputs)
	$(RUN) release $(VERSION)

reproduce: install test mine build validate stats label calibrate release-dry
