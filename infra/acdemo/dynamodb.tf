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

resource "aws_dynamodb_table" "games" {
  name         = "games"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "gameId"
  range_key    = "gameTime"

  attribute {
    name = "gameId"
    type = "S"
  }

  attribute {
    name = "gameTime"
    type = "S"
  }

  # gameId is formatted "{year}#{weekType}#{weekNumber}#{VISITING}@{HOME}", e.g.
  # "2026#PRESEASONWEEK#2#49ers@Chargers" (the VISITING/HOME shorthand's casing
  # isn't significant -- see parse_game_id()); gameTime is ISO-8601 UTC, e.g.
  # "2026-08-21T02:00:00Z". pregame_report_scheduler.py's parse_game_id() parses
  # visiting_team/home_team (and week_type/week_number/year) straight out of
  # gameId -- normalizing the VISITING/HOME shorthand to the title-cased team_name
  # convention feedsources/feed_ingest use (e.g. "49ers", "Chargers") -- so those
  # values need not be stored as separate attributes; an explicit visiting_team/
  # home_team/week_type/week_number/year attribute on a row, if ever present,
  # still overrides the parsed value. The optional enabled flag is a plain
  # attribute on each item; none of this is part of any key or index, so
  # DynamoDB doesn't require it to be declared here -- same convention as
  # feedsources above.
}
