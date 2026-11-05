COMPOSE = docker compose
OFFLINE = docker compose -f docker-compose.yml -f docker-compose.offline.yml

.PHONY: up demo rcm-demo offline offline-demo offline-rcm-demo down test bench e2e drill dataset audit

up:            ## stack against OpenAI, needs OPENAI_API_KEY in .env
	$(COMPOSE) up --build -d

demo:          ## send sample agent traffic through the proxy
	$(COMPOSE) --profile demo run --rm demo

offline:       ## whole stack against the mock upstream, no key needed
	$(OFFLINE) up --build -d

offline-demo:
	$(OFFLINE) --profile demo run --rm demo

rcm-demo:      ## synthetic denial investigations against a real model
	$(COMPOSE) --profile rcm run --rm rcm-demo

offline-rcm-demo:  ## synthetic denial investigations, scripted mock model
	$(OFFLINE) --profile rcm run --rm rcm-demo

down:
	$(OFFLINE) --profile demo down

test:          ## needs TEST_DATABASE_URL, see README
	pytest

bench:         ## proxy overhead, needs TEST_DATABASE_URL
	python bench/overhead.py

e2e:           ## builds images and checks the full offline stack
	scripts/e2e_offline.sh

drill:         ## stops and restarts Postgres under traffic, checks no trace is lost
	scripts/outage_drill.sh

dataset:       ## grounded evaluator on the labeled dataset; JUDGE=openai for a real judge
	python eval/run_dataset.py --judge $(or $(JUDGE),deterministic)

audit:         ## known vulnerabilities in pinned dependencies
	for f in proxy eval dashboard demo_agent mock_upstream demos/rcm_denial; do pip-audit -r $$f/requirements.txt; done
