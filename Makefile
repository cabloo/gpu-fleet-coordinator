# Short names for the commands the code's own messages refer to.
PY ?= python3

.PHONY: test demo dispatch watch prune pause drain hold resume pause-status

test: ## the suite: everything but the slow tests, then the slow subprocess-lifecycle tests
	$(PY) -m pytest -q -m "not slow"
	$(PY) -m pytest -q -m slow

demo: ## the real dispatcher and worker on this machine, no GPU or account needed
	demo/local_demo.sh

dispatch: ## run the dispatcher in this shell (it takes a lock: one per data root)
	$(PY) fleet/dispatcher.py

watch: ## one-shot status of a group: make watch GROUP=<group>
	@test -n "$(GROUP)" || { echo "usage: make watch GROUP=<group>"; exit 2; }
	$(PY) fleet/watch.py --group $(GROUP) --once

prune: ## what retention WOULD delete from experiments/ (dry run); apply with ARGS=--apply
	$(PY) fleet/prune_experiments.py $(ARGS)

pause: ## freeze an owned box's work in place, back soon: make pause BOX=<label>
	$(PY) fleet/box_pause.py pause $(if $(BOX),--label $(BOX),)

drain: ## checkpoint-stop an owned box's work so it requeues elsewhere, then hold
	$(PY) fleet/box_pause.py drain $(if $(BOX),--label $(BOX),)

hold: ## no NEW work on the box; running tasks finish
	$(PY) fleet/box_pause.py hold $(if $(BOX),--label $(BOX),)

resume: ## back to live
	$(PY) fleet/box_pause.py resume $(if $(BOX),--label $(BOX),)

pause-status: ## pause state and occupants of a box
	$(PY) fleet/box_pause.py status $(if $(BOX),--label $(BOX),)
