"""Regression checks for WhatsApp credential durability across restarts."""

from pathlib import Path


BRIDGE = Path(__file__).parents[1] / "backend/channels/whatsapp-bridge/index.js"


def _source():
    return BRIDGE.read_text()


def test_credentials_are_saved_atomically_and_flushed_on_shutdown():
    source = _source()
    assert "await handle.sync()" in source
    assert "await fs.promises.rename(temp, target)" in source
    assert "await flushCreds()" in source
    assert "queueCredsSave(saveCreds)" in source


def test_auth_directory_has_single_process_owner():
    source = _source()
    assert "const OWNER_DIR = `${AUTH_DIR}.owner`;" in source
    assert "acquireOwner();" in source
    assert "auth directory is already owned by bridge PID" in source
    assert "releaseOwner();" in source


def test_disconnects_never_automatically_delete_credentials():
    source = _source()
    connection_handler = source[source.index("sock.ev.on('connection.update'"):source.index("sock.ev.on('messages.upsert'")]
    assert "rmSync(AUTH_DIR" not in connection_handler
    assert "requestRepair('Logged out')" in connection_handler
    assert "requestRepair('Bad session')" in connection_handler
    assert "Connection replaced — backing off, creds preserved" in connection_handler
