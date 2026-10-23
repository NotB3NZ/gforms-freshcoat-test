"""Reserved non-admitting identity namespace for controlled rendering tests."""

TEST_TOKEN_PREFIX = "INVALID_TEST_"


def is_test_token(token):
    return isinstance(token, str) and token.startswith(TEST_TOKEN_PREFIX)
