UV ?= uv
RUN := $(UV) run

.PHONY: up down migrate ingest ingest-offline embed precompute demo worker beat probe web export test lint stats

up:            ## start postgres (5544) and redis (6390), wait until healthy, migrate
	docker compose up -d --wait
	$(RUN) inspraak db migrate

down:          ## stop the containers, keep the data volume
	docker compose down

migrate:
	$(RUN) inspraak db migrate

ingest:        ## nightly snapshot: register, meetings, have your say, documents, vectors
	$(RUN) inspraak ingest all

ingest-offline: ## same, from the files already under data/raw (no register/meetings download)
	$(RUN) inspraak ingest all --no-download

embed:         ## fill the missing vectors with the local model
	$(RUN) inspraak embed

precompute:    ## publication layer over the V1 scope, open publications first; resumable, stops on the daily budget
	$(RUN) inspraak precompute --feedback-limit 20

demo:          ## brief stage for demo_set.csv (one run per organisation without one); resumable
	$(RUN) inspraak demo

worker:        ## celery worker for the brief and nightly queues
	$(RUN) celery -A inspraak.queue worker -Q brief,nightly -l info --concurrency 1

beat:          ## celery beat: nightly snapshot 02:30, precompute 04:30 (Europe/Brussels)
	$(RUN) celery -A inspraak.queue beat -l info

probe:         ## one tiny model call with the configured provider
	$(RUN) inspraak llm probe

web:           ## the interactive app on http://127.0.0.1:8010
	$(RUN) inspraak web

export:        ## static site under site/ (organisations with a run, every in-scope consultation)
	$(RUN) inspraak export --out site

test:
	$(RUN) pytest -q

lint:
	$(RUN) ruff check inspraak tests scripts
	$(RUN) ruff format --check inspraak tests scripts

stats:
	$(RUN) inspraak db stats
