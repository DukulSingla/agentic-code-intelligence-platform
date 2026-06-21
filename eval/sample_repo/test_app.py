from app import get_users, get_user


def test_get_users():
    users = get_users()
    assert len(users) == 2


def test_get_user_found():
    assert get_user(1)["name"] == "Ada"


def test_get_user_not_found():
    assert get_user(999) == {"error": "not found"}
