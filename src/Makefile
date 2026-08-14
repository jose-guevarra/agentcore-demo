.PHONY: clean


chat_agent_dist:
	uv pip compile chat_agent/pyproject.toml -o chat_agent/requirements.txt


feed_ingest_dist:
	mkdir -p feed_ingest/dist
	cp feed_ingest/feed_ingest_handler.py feed_ingest/dist/
	uv pip compile feed_ingest/pyproject.toml -o feed_ingest/dist/requirements.txt
	pip3.14 install --target feed_ingest/dist/ -r feed_ingest/dist/requirements.txt
	cd feed_ingest/dist/ && zip -r ../feed_ingest_handler.zip .


feed_ingest_test:
	uv run --project feed_ingest pytest ../tests/test_feed_ingest_handler.py -v -s


clean_all:
	rm -rf chat_agent/requirements.txt
	rm -rf feed_ingest/dist