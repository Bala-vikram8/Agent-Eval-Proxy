COMPOSE = docker compose
OFFLINE = docker compose -f docker-compose.yml -f docker-compose.offline.yml

.PHONY: up demo offline offline-demo down test bench e2e

up:            ## stack against OpenAI, needs OPENAI_API_KEY in .env
	$(COMPOSE) up --build -d

demo:          ## send sample agent traffic through the proxy
	$(COMPOSE) --profile demo run --rm demo

offline:       ## whole stack against the mock upstream, no key needed
	$(OFFLINE) up --build -d

offline-demo:
	$(OFFLINE) --profile demo run --rm demo

down:
	$(OFFLINE) --profile demo down

test:          ## needs TEST_DATABASE_URL, see README
	pytest

bench:         ## proxy overhead, needs TEST_DATABASE_URL
	python bench/overhead.py

e2e:           ## builds images and checks the full offline stack
	scripts/e2e_offline.sh
