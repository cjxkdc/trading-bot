"""Encrypted, single-active-broker OAuth state. Never included in releases."""
from hashlib import sha256
import json
import os
from pathlib import Path
from uuid import uuid4

from app.control_contracts import ControlError


class ActiveVault:
    def __init__(self, root):
        self.folder = Path(root) / 'secrets/broker-session'
        self.path, self.key = self.folder / 'active.enc', self.folder / 'vault.key'

    def _cipher(self, create=False):
        try:
            from cryptography.fernet import Fernet
            if create and not self.key.is_file():
                self.folder.mkdir(parents=True, exist_ok=True)
                if os.name != 'nt':
                    self.folder.chmod(0o700)
                with self.key.open('xb') as handle:
                    if os.name != 'nt':
                        os.fchmod(handle.fileno(), 0o600)
                    handle.write(Fernet.generate_key())
            return Fernet(self.key.read_bytes())
        except Exception:
            raise ControlError('网页授权安全存储不可用，请检查 OAuth 依赖和本机私有目录。') from None

    def load(self, broker_id):
        if not self.path.is_file():
            return None
        if self.path.stat().st_size > 128 * 1024:
            raise ControlError('网页授权状态无法读取，请重新授权。')
        try:
            value = json.loads(self._cipher().decrypt(self.path.read_bytes()))
            return value if value.get('broker_id') == broker_id else None
        except ControlError:
            raise
        except Exception:
            raise ControlError('网页授权状态无法读取，请重新授权。') from None

    def save(self, broker_id, value):
        payload = json.dumps(dict(value, broker_id=broker_id), allow_nan=False).encode()
        if len(payload) > 128 * 1024:
            raise ControlError('授权状态超过允许大小。')
        encrypted = self._cipher(create=True).encrypt(payload)
        temp = self.folder / (uuid4().hex + '.tmp')
        try:
            with temp.open('xb') as handle:
                if os.name != 'nt':
                    os.fchmod(handle.fileno(), 0o600)
                handle.write(encrypted)
            temp.replace(self.path)
        finally:
            temp.unlink(missing_ok=True)

    def revision(self, broker_id):
        value = self.load(broker_id)
        return sha256(json.dumps(value, sort_keys=True).encode()).hexdigest() if value else 'unauthorized'

    def clear(self):
        self.path.unlink(missing_ok=True)
