"""Guard: the suite must never point at the live recovery side files."""
import persistence


def test_side_files_are_redirected_away_from_the_live_bot_directory():
    """Autouse conftest fixture keeps ACTIVE_STATUS_FILE and
    PENDING_TRANSCRIPTIONS_FILE out of dop-agent/ so tests cannot clobber
    the running daemon's crash-recovery state."""
    live_dir = persistence.Path(persistence.__file__).parent
    assert persistence.ACTIVE_STATUS_FILE.parent != live_dir
    assert persistence.PENDING_TRANSCRIPTIONS_FILE.parent != live_dir
