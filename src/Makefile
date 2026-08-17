.PHONY: clean all


all: chat_agent_dist feed_ingest_dist feed_ingest_scheduler_dist weather_tool_dist pregame_report_dist pregame_report_scheduler_dist


chat_agent_dist:
	uv pip compile chat_agent/pyproject.toml -o chat_agent/requirements.txt


feed_ingest_dist:
	mkdir -p feed_ingest/dist
	cp feed_ingest/feed_ingest.py feed_ingest/dist/
	uv pip compile feed_ingest/pyproject.toml -o feed_ingest/dist/requirements.txt
	pip3.14 install --target feed_ingest/dist/ -r feed_ingest/dist/requirements.txt
	cd feed_ingest/dist/ && zip -r ../feed_ingest.zip .


feed_ingest_test:
	uv run --project feed_ingest pytest ../tests/test_feed_ingest.py -v -s


feed_ingest_scheduler_dist:
	mkdir -p feed_ingest/dist_scheduler
	cp feed_ingest/feed_ingest_scheduler.py feed_ingest/dist_scheduler/
	cd feed_ingest/dist_scheduler/ && zip -r ../feed_ingest_scheduler.zip .


feed_ingest_scheduler_test:
	uv run --project feed_ingest pytest ../tests/test_feed_ingest_scheduler.py -v -s


weather_tool_dist:
	mkdir -p weather_tool/dist
	cp weather_tool/weather_tool.py weather_tool/dist/
	cd weather_tool/dist/ && zip -r ../weather_tool.zip .


weather_tool_test:
	uv run --project .. pytest ../tests/test_weather_tool.py -v -s


pregame_report_dist:
	mkdir -p pregame_report/dist
	cp pregame_report/pregame_report.py pregame_report/dist/
	cd pregame_report/dist/ && zip -r ../pregame_report.zip .


pregame_report_test:
	uv run --project .. pytest ../tests/test_pregame_report.py -v -s


pregame_report_scheduler_dist:
	mkdir -p pregame_report/dist_scheduler
	cp pregame_report/pregame_report_scheduler.py pregame_report/dist_scheduler/
	cd pregame_report/dist_scheduler/ && zip -r ../pregame_report_scheduler.zip .


pregame_report_scheduler_test:
	uv run --project .. pytest ../tests/test_pregame_report_scheduler.py -v -s


clean_all:
	rm -rf chat_agent/requirements.txt
	rm -rf feed_ingest/dist
	rm -rf feed_ingest/dist_scheduler
	rm -f feed_ingest/feed_ingest.zip
	rm -f feed_ingest/feed_ingest_scheduler.zip
	rm -rf weather_tool/dist
	rm -f weather_tool/weather_tool.zip
	rm -rf pregame_report/dist
	rm -rf pregame_report/dist_scheduler
	rm -f pregame_report/pregame_report.zip
	rm -f pregame_report/pregame_report_scheduler.zip
