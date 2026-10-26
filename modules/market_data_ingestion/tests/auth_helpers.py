from market_data_application.security import ApiRole, ApiTokenAuthenticator

_SYNTHETIC_ADMIN_TOKEN = "synthetic-local-admin-token-for-tests-000000000000"
LOCAL_ADMIN_HEADERS = {"Authorization": f"Bearer {_SYNTHETIC_ADMIN_TOKEN}"}


def local_admin_authenticator() -> ApiTokenAuthenticator:
    """Explicit test-only auth provider for API tests that do not test roles."""
    return ApiTokenAuthenticator({_SYNTHETIC_ADMIN_TOKEN: ApiRole.ADMIN})
