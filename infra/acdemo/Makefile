.PHONY: build plan apply destroy

# feed_ingest.zip / feed_ingest_scheduler.zip / weather_tool.zip (referenced
# directly by lambda.tf's/gateway.tf's source_code_hash/filename) are build
# outputs of src/Makefile, not checked in -- rebuild them before every
# plan/apply so Terraform never deploys code that's stale against the
# handlers' .py sources.
build:
	$(MAKE) -C ../../src feed_ingest_dist feed_ingest_scheduler_dist weather_tool_dist

plan: build
	terraform plan

apply: build
	terraform apply

destroy:
	terraform destroy
