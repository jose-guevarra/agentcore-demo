resource "aws_dynamodb_table" "feedsources" {
  name         = "feedsources"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "team_name"
  range_key    = "url"

  attribute {
    name = "team_name"
    type = "S"
  }

  attribute {
    name = "url"
    type = "S"
  }

  # city, source, type are plain string attributes on each item (e.g. "Denver",
  # "PFF", "team") and aren't part of any key or index, so DynamoDB doesn't
  # require them to be declared here.
}
