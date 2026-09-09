.PHONY: help seed run probe test verify verify-fast bench bench-live egress-on egress-off clean

help:
	@echo "Sovereign On-Premise AI Workbench"
	@echo "  make seed         build the synthetic industrial corpus"
	@echo "  make probe        report what this machine can serve"
	@echo "  make run          start the workbench on http://127.0.0.1:8794"
	@echo "  make test         unit tests (fast, no GPU)"
	@echo "  make verify       full acceptance verification (loads models)"
	@echo "  make verify-fast  acceptance verification without agent trajectories"
	@echo "  make bench        scheduling benchmark, modelled costs"
	@echo "  make bench-live   scheduling benchmark against real weights"
	@echo "  make egress-on    apply the host default-deny egress policy (root)"
	@echo "  make egress-off   remove it (root)"

seed:
	python3 scripts/seed_corpus.py

probe:
	PYTHONPATH=backend python3 -m sovereign.server --probe

run:
	./run.sh

test:
	python3 -m pytest tests/ -q

verify:
	python3 scripts/verify_e2e.py

verify-fast:
	python3 scripts/verify_e2e.py --fast

bench:
	python3 scripts/benchmark_scheduling.py --dry-run

bench-live:
	python3 scripts/benchmark_scheduling.py

egress-on:
	sudo ./ops/egress-policy.sh apply

egress-off:
	sudo ./ops/egress-policy.sh remove

clean:
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.pyc' -delete
