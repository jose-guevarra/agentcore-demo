output "ecr_repository_name" {
  value = aws_ecr_repository.agentcore_runtime_agent_code_ecr_repository.name
}

output "cognito_client_id" {
  value = aws_cognito_user_pool_client.userpool_client.id
}

output "agentcore_runtime_id" {
  value = aws_bedrockagentcore_agent_runtime.agentcore_runtime.agent_runtime_id
}

output "knowledge_base_id" {
  value = aws_bedrockagent_knowledge_base.knowledge_base.id
}

output "bedrock_data_source_id" {
  value = awscc_bedrock_data_source.s3_data_source.data_source_id
}

#output "feedback_table_name" {
#  value = aws_dynamodb_table.feedback.name
#}

#output "retrieval_table_name" {
#  value = aws_dynamodb_table.retrievals.name
#}