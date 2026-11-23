import pytest

import message_store
from telegram_rig import Rig


@pytest.fixture
def rig():
    return Rig()


@pytest.fixture
def dm_rig():
    return Rig(forum=False)


@pytest.fixture(autouse=True)
def _db():
    message_store.init_db()
    message_store.add_allowed_identity("telegram", "4242")
