resource "aws_cognito_user_pool" "userpool" {
  name = "acdemo-dev-userpool"

  email_configuration {
    email_sending_account = "COGNITO_DEFAULT"
  }

  auto_verified_attributes = ["email"]

  password_policy {
    minimum_length    = 8
    require_lowercase = true
    require_uppercase = true
    require_numbers   = true
    require_symbols   = true
  }

  username_attributes = ["email"]
  username_configuration {
    case_sensitive = false
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
}

resource "aws_cognito_user_pool_client" "userpool_client" {
  name                          = "acdemo-dev-userpool-client"
  user_pool_id                  = aws_cognito_user_pool.userpool.id
  supported_identity_providers  = ["COGNITO"]
  explicit_auth_flows           = ["ALLOW_USER_AUTH", "ALLOW_REFRESH_TOKEN_AUTH", "ALLOW_USER_PASSWORD_AUTH"]
  generate_secret               = false
  prevent_user_existence_errors = "ENABLED"
  refresh_token_validity        = 30
  access_token_validity         = 24
  id_token_validity             = 24
  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }
}
